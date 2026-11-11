"""写作模型适配（omlx，OpenAI 兼容 HTTP）。

实测结论（2026-11-09，服务 v0.7.3）：
- 形态：OpenAI 兼容本地 HTTP，默认 ``http://127.0.0.1:9000``，鉴权 ``Authorization: Bearer <key>``；
- 能力：``GET /v1/models``、``POST /v1/chat/completions``（支持 SSE 流式）、
  ``POST /v1/models/{model_id}/unload`` 显式卸载、``GET /health`` 查看显存占用；
- omlx 不会自动卸载模型（``idle_timeout=null``），必须显式卸载；内存天花板约 29 GB，
  因此文本生成与 ComfyUI 必须串行，且按调度策略在文本任务后卸载。

本模块只负责协议映射与错误归一，不做重试编排以外的业务判断。
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Iterator

from .base import AdapterError, ErrorCode, HealthStatus, TextGenChunk, TextGenRequest, TextGenResult, Usage

# 一次请求的默认超时（秒）；长文续写可能耗时较久，允许配置覆盖
DEFAULT_TIMEOUT_S = 300.0
# 连接超时（秒）；用于快速判断服务是否可达
CONNECT_TIMEOUT_S = 5.0
# 工具调用时的最小输出预算；thinking 模型的 reasoning_tokens 也计入 max_tokens，给少了参数会被截断
TOOL_MIN_MAX_TOKENS = 4096


class OmlxWritingAdapter:
    """omlx 写作适配器：OpenAI 兼容协议的真实实现。"""

    def __init__(self, settings: dict):
        """绑定 omlx 配置。

        参数:
            settings: 形如 {protocol, endpoint, api_key, model, context_window, stop,
                temperature, max_tokens, request_timeout_s, connect_timeout_s, max_retries,
                unload: {enabled, path, policy}}。
        """
        self.settings = settings or {}
        self.endpoint = str(self.settings.get("endpoint") or "").rstrip("/")
        self.api_key = str(self.settings.get("api_key") or self.settings.get("token") or "")
        self.model = str(self.settings.get("model") or "")
        self.timeout_s = float(self.settings.get("request_timeout_s") or DEFAULT_TIMEOUT_S)
        self.connect_timeout_s = float(self.settings.get("connect_timeout_s") or CONNECT_TIMEOUT_S)
        self.max_retries = max(0, int(self.settings.get("max_retries") or 0))
        self.enable_thinking = self.settings.get("enable_thinking")
        self.unload_settings = dict(self.settings.get("unload") or {})

    @property
    def protocol(self) -> str | None:
        """返回协议类型（固定为 openai_compatible）。"""
        return self.settings.get("protocol")

    def _require_ready(self) -> None:
        """校验必要配置，缺失时给出可操作提示。

        异常:
            AdapterError(INVALID_INPUT): 未配置地址或模型。
        """
        if not self.endpoint:
            raise AdapterError(
                ErrorCode.INVALID_INPUT,
                "omlx 未配置地址：请在 config/config.yaml 的 omlx.endpoint 填写（例如 http://127.0.0.1:9000）",
            )
        if not self.model:
            raise AdapterError(
                ErrorCode.INVALID_INPUT,
                "omlx 未配置模型：请在 config/config.yaml 的 omlx.model 填写 /v1/models 返回的 id",
            )

    def _headers(self) -> dict:
        """构造请求头（含 Bearer 鉴权）。"""
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _url(self, path: str) -> str:
        """拼接完整 URL。

        参数:
            path: 以 / 开头的接口路径。

        返回:
            完整 URL。
        """
        return f"{self.endpoint}{path}"

    def _map_error(self, status: int, body: str, path: str) -> AdapterError:
        """把 HTTP 错误映射为统一错误码。

        参数:
            status: HTTP 状态码。
            body: 响应体文本（截断后用于提示）。
            path: 请求路径，便于定位。

        返回:
            AdapterError。
        """
        snippet = (body or "").strip()[:300]
        if status in (401, 403):
            return AdapterError(
                ErrorCode.INVALID_INPUT,
                f"omlx 鉴权失败（HTTP {status}）：检查 config/config.yaml 的 omlx.api_key 是否与服务一致。响应：{snippet}",
                detail={"status": status, "path": path},
            )
        if status == 404:
            return AdapterError(
                ErrorCode.INVALID_INPUT,
                f"omlx 接口不存在（HTTP 404，{path}）：确认服务版本支持该接口，或检查 omlx.endpoint 是否指向 omlx 而非其他服务。响应：{snippet}",
                detail={"status": status, "path": path},
            )
        if status == 429:
            return AdapterError(
                ErrorCode.RATE_LIMITED,
                f"omlx 限流（HTTP 429）：稍后重试或降低并发。响应：{snippet}",
                retryable=True,
                detail={"status": status, "path": path},
            )
        if status >= 500:
            return AdapterError(
                ErrorCode.UPSTREAM_ERROR,
                f"omlx 服务端错误（HTTP {status}，{path}）：查看 omlx 日志；若显存不足先卸载其他模型。响应：{snippet}",
                retryable=True,
                detail={"status": status, "path": path},
            )
        return AdapterError(
            ErrorCode.INVALID_INPUT,
            f"omlx 请求被拒绝（HTTP {status}，{path}）：响应：{snippet}",
            detail={"status": status, "path": path},
        )

    def _open(self, method: str, path: str, payload: dict | None, timeout_s: float | None = None):
        """发起一次 HTTP 请求并返回响应对象。

        参数:
            method: GET / POST 等。
            path: 接口路径。
            payload: JSON 请求体，GET 时为 None。
            timeout_s: 超时秒数，默认使用请求超时。

        返回:
            http.client.HTTPResponse 或 urllib 响应对象。

        异常:
            AdapterError: 网络错误、超时或非 2xx 响应。
        """
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(self._url(path), data=data, headers=self._headers(), method=method)
        try:
            return urllib.request.urlopen(req, timeout=timeout_s or self.timeout_s)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            raise self._map_error(exc.code, body, path) from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, (socket.timeout, TimeoutError)):
                raise AdapterError(
                    ErrorCode.TIMEOUT,
                    f"omlx 请求超时（{path}）：模型可能正在加载，或输入过长；可调大 omlx.request_timeout_s",
                    retryable=True,
                ) from exc
            raise AdapterError(
                ErrorCode.UNAVAILABLE,
                f"omlx 不可达（{self.endpoint}）：确认服务已启动（如 `omlx serve --port 9000`）且地址端口正确。原因：{reason}",
                retryable=True,
            ) from exc
        except (socket.timeout, TimeoutError) as exc:
            raise AdapterError(
                ErrorCode.TIMEOUT,
                f"omlx 连接超时（{path}）：可调大 omlx.request_timeout_s 或检查服务负载",
                retryable=True,
            ) from exc

    def _request_json(self, method: str, path: str, payload: dict | None = None, timeout_s: float | None = None) -> dict:
        """发起请求并解析 JSON，按配置自动重试。

        参数:
            method: HTTP 方法。
            path: 接口路径。
            payload: JSON 请求体。
            timeout_s: 超时秒数。

        返回:
            解析后的 JSON dict。

        异常:
            AdapterError: 重试耗尽后仍失败。
        """
        attempt = 0
        while True:
            try:
                with self._open(method, path, payload, timeout_s) as resp:
                    raw = resp.read().decode("utf-8", errors="replace")
                if not raw.strip():
                    return {}
                return json.loads(raw)
            except AdapterError as exc:
                if not exc.retryable or attempt >= self.max_retries:
                    raise
            except json.JSONDecodeError as exc:
                raise AdapterError(ErrorCode.UPSTREAM_ERROR, f"omlx 返回非 JSON（{path}）：{exc}", detail={"path": path}) from exc
            attempt += 1
            time.sleep(min(2.0 * attempt, 5.0))

    def list_models(self, timeout_s: float | None = None) -> list[dict]:
        """获取服务端模型列表（GET /v1/models）。

        参数:
            timeout_s: 可选，覆盖默认超时（设置页自检时用较短超时）。

        返回:
            模型 dict 列表（含 id、max_model_len 等）。

        异常:
            AdapterError(INVALID_INPUT): 未配置地址。
        """
        # 列表接口不依赖 model，未选模型时也允许拉取（设置页「检测」要先看到可选项）
        if not self.endpoint:
            raise AdapterError(
                ErrorCode.INVALID_INPUT,
                "omlx 未配置地址：请在 config/config.yaml 的 omlx.endpoint 填写（例如 http://127.0.0.1:9000）",
            )
        data = self._request_json("GET", "/v1/models", timeout_s=timeout_s)
        models = data.get("data") if isinstance(data, dict) else None
        return models if isinstance(models, list) else []

    def model_ids(self) -> list[str]:
        """返回模型 ID 列表。"""
        return [str(item.get("id")) for item in self.list_models() if item.get("id")]

    def model_status(self) -> dict:
        """获取模型加载状态（GET /v1/models/status）。

        返回:
            含 final_ceiling、current_model_memory、models 的 dict；接口不可用时返回空 dict。
        """
        try:
            return self._request_json("GET", "/v1/models/status")
        except AdapterError:
            return {}

    def loaded_models(self) -> list[str]:
        """返回当前已加载的模型 ID 列表。"""
        status = self.model_status()
        return [str(item.get("id")) for item in status.get("models") or [] if item.get("loaded")]

    def health(self, timeout_s: float | None = None) -> HealthStatus:
        """检查服务可达性与配置模型是否存在。

        参数:
            timeout_s: 覆盖默认超时（自检时用较短超时）。

        返回:
            HealthStatus；ok=False 时 detail 给出可操作原因。
        """
        if not self.endpoint:
            return HealthStatus(ok=False, detail="omlx 未配置 endpoint：请在 config/config.yaml 的 omlx.endpoint 填写（例如 http://127.0.0.1:9000）")
        started = time.monotonic()
        try:
            models = self.list_models() if timeout_s is None else self.list_models(timeout_s)
        except AdapterError as exc:
            return HealthStatus(ok=False, detail=exc.message, extra={"code": exc.code.value})
        latency = int((time.monotonic() - started) * 1000)
        ids = [str(item.get("id")) for item in models if item.get("id")]
        extra = {"models": ids, "endpoint": self.endpoint}
        if self.model and self.model not in ids:
            return HealthStatus(
                ok=False,
                detail=f"模型 {self.model} 不在服务列表中：可用模型 {ids}；请改 config/config.yaml 的 omlx.model",
                latency_ms=latency,
                extra=extra,
            )
        status = {}
        try:
            status = self._request_json("GET", "/v1/models/status", timeout_s=timeout_s) if timeout_s else self.model_status()
        except AdapterError:
            status = {}
        if status:
            extra.update(
                {
                    "loaded": [str(item.get("id")) for item in status.get("models") or [] if item.get("loaded")],
                    "memory_bytes": status.get("current_model_memory"),
                    "ceiling_bytes": status.get("final_ceiling"),
                }
            )
        return HealthStatus(ok=True, detail=f"omlx 可达，模型 {self.model} 存在（共 {len(ids)} 个）", latency_ms=latency, extra=extra)

    def _build_payload(self, req: TextGenRequest, stream: bool) -> dict:
        """把统一请求映射为 OpenAI 兼容请求体。

        参数:
            req: 统一文本请求。
            stream: 是否流式。

        返回:
            请求体 dict。
        """
        messages = []
        if req.system:
            messages.append({"role": "system", "content": req.system})
        for msg in req.messages:
            messages.append({"role": msg.role, "content": msg.content})
        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": int(req.max_tokens or self.settings.get("max_tokens") or 2048),
            "temperature": float(req.temperature if req.temperature is not None else self.settings.get("temperature", 0.8)),
            "stream": stream,
        }
        stop = req.stop or list(self.settings.get("stop") or [])
        if stop:
            payload["stop"] = stop
        # omlx 支持通过 chat_template_kwargs 关闭思考，降低延迟与 token 消耗
        if self.enable_thinking is not None:
            payload["chat_template_kwargs"] = {"enable_thinking": bool(self.enable_thinking)}
        # 多轮续写前缀稳定时显式请求前缀缓存；服务不支持该字段会忽略
        if req.prompt_cache_key and bool(self.settings.get("cache_prompt", True)):
            payload["cache_prompt"] = True
            payload["prompt_cache_key"] = req.prompt_cache_key
        extra_body = (req.metadata or {}).get("extra_body")
        if isinstance(extra_body, dict):
            payload.update(extra_body)
        return payload

    @staticmethod
    def _parse_usage(usage: dict | None) -> Usage:
        """把上游 `usage` 映射为统一用量结构，缺失缓存字段时保持 None。

        参数:
            usage: OpenAI 兼容响应里的 usage dict；可能为 None 或缺少字段。

        返回:
            Usage；`cached_tokens` 依次尝试 `prompt_tokens_details.cached_tokens`、
            `cache_n`、`cached_tokens`，都取不到时为 None。
        """
        usage = usage if isinstance(usage, dict) else {}
        details = usage.get("prompt_tokens_details")
        cached = details.get("cached_tokens") if isinstance(details, dict) else None
        if cached is None:
            cached = usage.get("cache_n")
        if cached is None:
            cached = usage.get("cached_tokens")
        cached_tokens: int | None = None
        if cached is not None:
            try:
                cached_tokens = int(cached)
            except (TypeError, ValueError):
                cached_tokens = None
        try:
            prompt_tokens = int(usage.get("prompt_tokens") or 0)
        except (TypeError, ValueError):
            prompt_tokens = 0
        try:
            completion_tokens = int(usage.get("completion_tokens") or 0)
        except (TypeError, ValueError):
            completion_tokens = 0
        return Usage(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens, cached_tokens=cached_tokens)

    def generate(self, req: TextGenRequest) -> TextGenResult:
        """非流式生成文本（POST /v1/chat/completions，stream=false）。

        参数:
            req: 统一文本请求。

        返回:
            TextGenResult（text/finish_reason/model/usage）。

        异常:
            AdapterError: 配置缺失、服务不可达、鉴权失败或上游错误。
        """
        self._require_ready()
        payload = self._build_payload(req, stream=False)
        data = self._request_json("POST", "/v1/chat/completions", payload)
        choices = data.get("choices") or []
        if not choices:
            raise AdapterError(ErrorCode.UPSTREAM_ERROR, f"omlx 返回空 choices：{str(data)[:300]}", detail={"path": "/v1/chat/completions"})
        message = choices[0].get("message") or {}
        text = message.get("content") or message.get("reasoning_content") or ""
        return TextGenResult(
            text=text,
            finish_reason=str(choices[0].get("finish_reason") or "stop"),
            model=str(data.get("model") or self.model),
            usage=self._parse_usage(data.get("usage")),
            raw=data,
        )

    def generate_tool(self, req: TextGenRequest, tool: dict, timeout_s: float | None = None) -> dict:
        """以工具调用（function calling）方式生成结构化结果。

        用 `tool_choice` 强制模型只调用指定函数，模型参数即结构化结果，
        避免自由文本里夹杂解释导致 JSON 解析失败。

        参数:
            req: 统一文本请求（system/messages/temperature 等照常使用）。
            tool: 单个工具定义，形如
                {"type": "function", "function": {"name", "description", "parameters"}}。
            timeout_s: 覆盖默认超时；结构化输出较长可放宽。

        返回:
            工具参数解析后的 dict（等同 tool_calls[0].function.arguments）。

        异常:
            AdapterError: 配置缺失、服务不可达、未返回 tool_calls 或参数不是 JSON 对象。
        """
        self._require_ready()
        function = (tool or {}).get("function") or {}
        name = str(function.get("name") or "").strip()
        if not name:
            raise AdapterError(ErrorCode.INVALID_INPUT, "工具定义缺少 function.name")

        payload = self._build_payload(req, stream=False)
        payload["tools"] = [tool]
        payload["tool_choice"] = {"type": "function", "function": {"name": name}}
        # thinking 模型的 reasoning_tokens 同样占用 max_tokens，预算太小会把参数截断
        payload["max_tokens"] = max(int(payload.get("max_tokens") or 0), TOOL_MIN_MAX_TOKENS)

        data = self._request_json("POST", "/v1/chat/completions", payload, timeout_s=timeout_s)
        choices = data.get("choices") or []
        if not choices:
            raise AdapterError(
                ErrorCode.UPSTREAM_ERROR,
                f"omlx 返回空 choices：{str(data)[:300]}",
                detail={"path": "/v1/chat/completions"},
            )
        finish = str(choices[0].get("finish_reason") or "")
        message = choices[0].get("message") or {}
        calls = message.get("tool_calls") or []
        if not calls:
            raise AdapterError(
                ErrorCode.UPSTREAM_ERROR,
                f"omlx 未返回工具调用 {name}（finish_reason={finish or '空'}）",
                detail={"raw": str(data)[:1000]},
            )
        arguments = ((calls[0] or {}).get("function") or {}).get("arguments")
        if isinstance(arguments, dict):
            return arguments
        try:
            parsed = json.loads(str(arguments or ""))
        except json.JSONDecodeError as exc:
            raise AdapterError(
                ErrorCode.UPSTREAM_ERROR,
                f"工具调用 {name} 的参数不是合法 JSON：{exc}",
                detail={"arguments": str(arguments)[:1000]},
            ) from exc
        if not isinstance(parsed, dict):
            raise AdapterError(
                ErrorCode.UPSTREAM_ERROR,
                f"工具调用 {name} 的参数不是 JSON 对象：{str(parsed)[:200]}",
                detail={"arguments": str(arguments)[:1000]},
            )
        return parsed

    def stream(self, req: TextGenRequest) -> Iterator[TextGenChunk]:
        """流式生成文本（POST /v1/chat/completions，stream=true，SSE）。

        参数:
            req: 统一文本请求。

        返回:
            TextGenChunk 迭代器；结束块 done=True 并尽量携带 usage。

        异常:
            AdapterError: 建立连接失败或流中错误。已产出的增量不会回滚。
        """
        self._require_ready()
        payload = self._build_payload(req, stream=True)
        attempt = 0
        while True:
            try:
                resp = self._open("POST", "/v1/chat/completions", payload)
                break
            except AdapterError as exc:
                if not exc.retryable or attempt >= self.max_retries:
                    raise
                attempt += 1
                time.sleep(min(2.0 * attempt, 5.0))

        with resp:
            for raw_line in resp:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line or not line.startswith("data:"):
                    continue
                chunk_text = line[5:].strip()
                if chunk_text == "[DONE]":
                    yield TextGenChunk(delta="", done=True)
                    return
                try:
                    data = json.loads(chunk_text)
                except json.JSONDecodeError:
                    continue
                choices = data.get("choices") or []
                usage = data.get("usage")
                usage_obj = None
                if usage:
                    usage_obj = self._parse_usage(usage)
                if not choices:
                    if usage_obj:
                        yield TextGenChunk(delta="", done=False, usage=usage_obj)
                    continue
                delta = choices[0].get("delta") or {}
                text = delta.get("content") or delta.get("reasoning_content") or ""
                finish = choices[0].get("finish_reason")
                if text:
                    yield TextGenChunk(delta=text, done=False, usage=usage_obj)
                if finish:
                    yield TextGenChunk(delta="", done=True, usage=usage_obj)
                    return
        yield TextGenChunk(delta="", done=True)

    def unload(self, model: str | None = None) -> dict:
        """显式卸载模型，释放显存（omlx 不会自动卸载）。

        参数:
            model: 要卸载的模型 ID，默认使用配置模型。

        返回:
            服务端响应 dict，至少含 {"status": ...}。

        异常:
            AdapterError: 卸载被禁用、接口不可用或服务拒绝。
        """
        if not self.unload_settings.get("enabled", True):
            return {"status": "skipped", "reason": "omlx.unload.enabled=false"}
        target = model or self.model
        if not target:
            raise AdapterError(ErrorCode.INVALID_INPUT, "卸载模型需要指定 model：请在 config/config.yaml 的 omlx.model 填写")
        path_template = str(self.unload_settings.get("path") or "/v1/models/{model_id}/unload")
        path = path_template.format(model_id=urllib.parse.quote(target, safe=""))
        try:
            data = self._request_json("POST", path, None, timeout_s=self.connect_timeout_s)
        except AdapterError as exc:
            raise AdapterError(
                exc.code,
                f"卸载模型 {target} 失败：{exc.message}。可先用 `ndw llm status` 查看加载状态，"
                f"或在 omlx 管理页（{self.endpoint}/admin）手动卸载",
                retryable=exc.retryable,
                detail=exc.detail,
            ) from exc
        return {"status": data.get("status") or "ok", "model_id": target, "raw": data}


class UnavailableWritingAdapter:
    """未装配写作适配器时的兜底实现，所有调用直接报错，避免静默失败。"""

    def __init__(self, reason: str = "未配置写作适配器"):
        """记录不可用原因。

        参数:
            reason: 面向用户的原因。
        """
        self.reason = reason

    def generate(self, req: TextGenRequest) -> TextGenResult:
        """始终抛出 NOT_IMPLEMENTED。"""
        raise AdapterError(ErrorCode.NOT_IMPLEMENTED, f"写作能力不可用：{self.reason}")

    def stream(self, req: TextGenRequest) -> Iterator[TextGenChunk]:
        """始终抛出 NOT_IMPLEMENTED。"""
        raise AdapterError(ErrorCode.NOT_IMPLEMENTED, f"写作能力不可用：{self.reason}")

    def health(self, timeout_s: float | None = None) -> HealthStatus:
        """返回不可用状态。"""
        return HealthStatus(ok=False, detail=self.reason)

    def unload(self, model: str | None = None) -> dict:
        """未装配时无法卸载。"""
        raise AdapterError(ErrorCode.NOT_IMPLEMENTED, f"写作能力不可用：{self.reason}")

    def generate_tool(self, req: TextGenRequest, tool: dict, timeout_s: float | None = None) -> dict:
        """未装配时无法做工具调用。"""
        raise AdapterError(ErrorCode.NOT_IMPLEMENTED, f"写作能力不可用：{self.reason}")

    def list_models(self) -> list[dict]:
        """未装配时没有模型列表。"""
        raise AdapterError(ErrorCode.NOT_IMPLEMENTED, f"写作能力不可用：{self.reason}")
