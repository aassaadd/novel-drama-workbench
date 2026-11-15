"""写作模型适配（omlx，OpenAI 兼容 HTTP）。

实测结论（2026-11-09，服务 v0.7.3）：
- 形态：OpenAI 兼容本地 HTTP，地址由配置提供（无内置默认值），鉴权 ``Authorization: Bearer <key>``；
- 能力：``GET /v1/models``、``POST /v1/chat/completions``（支持 SSE 流式）、
  ``POST /v1/models/{model_id}/unload`` 显式卸载、``GET /health`` 查看显存占用；
- omlx 不会自动卸载模型（``idle_timeout=null``），必须显式卸载；内存天花板约 29 GB，
  因此文本生成与 ComfyUI 必须串行，且按调度策略在文本任务后卸载。

本模块只负责协议映射与错误归一，不做重试编排以外的业务判断。
模型目录（列表 / limits / 健康检查）见 `catalog.py`。
"""

from __future__ import annotations

import http.client
import json
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Iterator

from ...domain.production.rules.thinking_policy import resolve_thinking
from ...domain.production.rules.watchdog import apply_context_limit, watchdog_tokens
from ...domain.production.rules.watchdog import describe as describe_watchdog
from ..errors import AdapterError, ErrorCode
from ..observability import record_budget_stats, record_model_call
from .catalog import ModelCatalogMixin
from .contracts import TextGenChunk, TextGenRequest, TextGenResult, Usage
from .helpers import (
    _LIMIT_CACHE,
    _REPETITION_TASKS,
    _THINKING_EFFORTS,
    CONNECT_TIMEOUT_S,
    DEFAULT_REPETITION_PENALTY,
    DEFAULT_TIMEOUT_S,
    DRY_DEFAULTS,
    FIRST_TOKEN_TIMEOUT_S,
    MAX_REPETITION_PENALTY,
    STREAM_IDLE_TIMEOUT_S,
    _call_snippet,
    _HttpResponse,
    _log_reasoning,
    _split_delta,
    _to_float,
    logger,
    probe_sampling_support,
)


class OmlxWritingAdapter(ModelCatalogMixin):
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
        self.first_token_timeout_s = float(self.settings.get("first_token_timeout_s") or FIRST_TOKEN_TIMEOUT_S)
        self.stream_idle_timeout_s = float(self.settings.get("stream_idle_timeout_s") or STREAM_IDLE_TIMEOUT_S)
        # 总时长上限：空 / 0 表示不限，由用户在设置页按需填写
        raw_total = self.settings.get("total_timeout_s")
        try:
            self.total_timeout_s = float(raw_total) if raw_total not in (None, "", 0, "0") else None
        except (TypeError, ValueError):
            self.total_timeout_s = None
        self.max_retries = max(0, int(self.settings.get("max_retries") or 0))
        self.enable_thinking = self.settings.get("enable_thinking")
        #: 设置页按调用点的思考覆盖：`{stage: bool}`；没有条目即「系统自动」
        self.thinking_stages = dict(self.settings.get("thinking_stages") or {})
        # 思考强度：low / medium / high；仅在开启思考时随请求下发，不按阶段分档
        self.reasoning_effort = str(self.settings.get("reasoning_effort") or "").strip().lower()
        #: 最近一次请求的上下文/提示词/输出空间统计（供设置页与日志展示）
        self._budget_stats: dict = {}
        # 反重复采样参数：显式配置优先；为空时正文类任务用保守默认值
        self.repetition_penalty = _to_float(self.settings.get("repetition_penalty"))
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
                "omlx 未配置地址：请在设置页填写服务地址（或在 config/config.yaml 的 omlx.endpoint 配置）",
            )
        if not self.model:
            raise AdapterError(
                ErrorCode.INVALID_INPUT,
                "未选择模型，请在设置页检测连接后选择（或在 config/config.yaml 的 omlx.model 填写 /v1/models 返回的 id）",
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

    @staticmethod
    def _upstream_message(body: str) -> str:
        """从上游错误响应里提取可读说明（OpenAI 兼容格式或纯文本）。

        参数:
            body: 原始响应体文本。

        返回:
            截断到 300 字的中文/原始说明；无法解析时返回原文片段。
        """
        text = (body or "").strip()
        if not text:
            return ""
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return text[:300]
        if isinstance(data, dict):
            error = data.get("error")
            if isinstance(error, dict) and error.get("message"):
                return str(error["message"])[:300]
            if isinstance(error, str):
                return error[:300]
            if data.get("message"):
                return str(data["message"])[:300]
            detail = data.get("detail")
            if isinstance(detail, str):
                return detail[:300]
            if isinstance(detail, dict) and detail.get("message"):
                return str(detail["message"])[:300]
        return text[:300]

    def _map_error(self, status: int, body: str, path: str) -> AdapterError:
        """把 HTTP 错误映射为统一错误码。

        参数:
            status: HTTP 状态码（上游可能返回非标准码，统一在此转中文）。
            body: 响应体文本（截断后用于提示）。
            path: 请求路径，便于定位。

        返回:
            AdapterError。
        """
        snippet = self._upstream_message(body) or (body or "").strip()[:300]
        if status in (401, 403):
            return AdapterError(
                ErrorCode.INVALID_INPUT,
                f"omlx 鉴权失败（HTTP {status}）：检查 config/config.yaml 的 omlx.api_key 是否与服务一致。响应：{snippet}",
                detail={"status": status, "path": path},
            )
        if status == 404:
            # 404 既可能是路径不存在，也可能是模型不存在：优先按上游说明区分
            lowered = snippet.lower()
            if "model" in lowered and ("not found" in lowered or "不存在" in snippet):
                return AdapterError(
                    ErrorCode.INVALID_INPUT,
                    f"omlx 未找到模型（HTTP 404，{path}）：{snippet}；请在设置页重新选择模型",
                    detail={"status": status, "path": path},
                )
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
            f"omlx 拒绝请求（HTTP {status}，{path}）：{snippet or '服务未返回说明'}；"
            "请在设置页「测试连接」确认模型与密钥后再试",
            detail={"status": status, "path": path},
        )

    def _http_target(self, path: str) -> tuple[str, str, str, int]:
        """把 endpoint + path 解析成 http.client 需要的连接参数。

        参数:
            path: 以 / 开头的接口路径。

        返回:
            `(scheme, host, request_target, port)`。

        异常:
            AdapterError(INVALID_INPUT): 地址缺失或不是 http(s) URL。
        """
        if not self.endpoint:
            raise AdapterError(ErrorCode.INVALID_INPUT, "omlx 未配置地址：请先在设置页填写接口地址")
        parsed = urllib.parse.urlsplit(self._url(path))
        scheme = parsed.scheme.lower()
        if scheme not in ("http", "https") or not parsed.hostname:
            raise AdapterError(
                ErrorCode.INVALID_INPUT,
                f"omlx 地址无效：{self.endpoint}；需以 http:// 或 https:// 开头",
            )
        port = parsed.port or (443 if scheme == "https" else 80)
        target = parsed.path or "/"
        if parsed.query:
            target = f"{target}?{parsed.query}"
        return scheme, parsed.hostname, target, port

    def _nonstream_timeout(self, timeout_s: float | None) -> float:
        """计算非流式调用的读超时（连接超时单独由 connect_timeout_s 控制）。

        短调用没有分片可判断，这里用空闲超时兜底防止无限等待；
        调用方显式给的更小超时与总时长上限也会取最小值。

        参数:
            timeout_s: 调用方显式指定的超时；None 表示用 omlx.request_timeout_s。

        返回:
            读超时秒数。
        """
        base = float(timeout_s if timeout_s is not None else self.timeout_s)
        if self.stream_idle_timeout_s:
            base = min(base, self.stream_idle_timeout_s)
        if self.total_timeout_s:
            base = min(base, self.total_timeout_s)
        return max(0.5, base)

    def _open(
        self,
        method: str,
        path: str,
        payload: dict | None,
        timeout_s: float | None = None,
        stream: bool = False,
    ) -> _HttpResponse:
        """发起一次 HTTP 请求并返回响应对象。

        连接阶段使用 connect_timeout_s（默认 5 秒）；
        读阶段：流式用首字超时（后续由 `_read_sse` 动态改为空闲超时），
        非流式用 `_nonstream_timeout`。

        参数:
            method: GET / POST 等。
            path: 接口路径。
            payload: JSON 请求体，GET 时为 None。
            timeout_s: 非流式读超时；None 表示用 omlx.request_timeout_s。
            stream: 是否请求 SSE 流。

        返回:
            `_HttpResponse` 包装对象。

        异常:
            AdapterError: 连接失败、超时或非 2xx 响应。
        """
        scheme, host, target, port = self._http_target(path)
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        headers = self._headers()
        if stream:
            headers["Accept"] = "text/event-stream"
        conn_cls = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
        conn = conn_cls(host, port, timeout=self.connect_timeout_s)
        read_timeout = self.first_token_timeout_s if stream else self._nonstream_timeout(timeout_s)
        try:
            conn.connect()
            sock = getattr(conn, "sock", None)
            if sock is not None:
                sock.settimeout(read_timeout)
            conn.request(method, target, body=data, headers=headers)
            resp = conn.getresponse()
        except (socket.timeout, TimeoutError) as exc:
            conn.close()
            raise AdapterError(
                ErrorCode.TIMEOUT,
                f"omlx 连接失败（{self.endpoint}）：连接或等待响应超时；确认服务已启动且地址端口正确，"
                "或调大 omlx.connect_timeout_s",
                retryable=True,
                detail={"path": path, "stage": "connect"},
            ) from exc
        except (OSError, http.client.HTTPException) as exc:
            conn.close()
            raise AdapterError(
                ErrorCode.UNAVAILABLE,
                f"omlx 连接失败（{self.endpoint}）：确认服务已启动（如 `omlx serve --port 9000`）且地址端口正确。原因：{exc}",
                retryable=True,
                detail={"path": path, "stage": "connect"},
            ) from exc
        wrapper = _HttpResponse(conn, resp)
        if resp.status >= 400:
            body = resp.read().decode("utf-8", errors="replace")
            wrapper.close()
            raise self._map_error(resp.status, body, path)
        return wrapper

    def _request_json(self, method: str, path: str, payload: dict | None = None, timeout_s: float | None = None) -> dict:
        """发起非流式请求并解析 JSON，按配置自动重试。

        参数:
            method: HTTP 方法。
            path: 接口路径。
            payload: JSON 请求体。
            timeout_s: 非流式读超时。

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
            except (socket.timeout, TimeoutError) as exc:
                # 非流式读取阶段超时：没有任何内容产出，允许按重试策略重来
                if attempt >= self.max_retries:
                    raise AdapterError(
                        ErrorCode.TIMEOUT,
                        f"omlx 生成卡住（{self._nonstream_timeout(timeout_s):.0f} 秒无响应，{path}）："
                        "可重试，或调大 omlx.stream_idle_timeout_s / omlx.total_timeout_s",
                        retryable=True,
                        detail={"path": path},
                    ) from exc
            except json.JSONDecodeError as exc:
                raise AdapterError(ErrorCode.UPSTREAM_ERROR, f"omlx 返回非 JSON（{path}）：{exc}", detail={"path": path}) from exc
            attempt += 1
            time.sleep(min(2.0 * attempt, 5.0))

    def _context_limit(self) -> int | None:
        """返回请求预算可用的上下文上限（检测缓存与配置取较小值，避免超限）。

        返回:
            tokens；都没有时为 None（不做裁剪）。
        """
        cached = _LIMIT_CACHE.get((self.endpoint, self.model))
        configured = self.settings.get("context_window")
        values = [int(value) for value in (cached, configured) if isinstance(value, (int, float)) and int(value) > 0]
        return min(values) if values else None

    @staticmethod
    def _estimate_prompt_tokens(messages: list[dict]) -> int:
        """粗略估算 prompt 的 token 数（中文按 1 字≈1 token 保守计）。

        参数:
            messages: 已拼好的 OpenAI messages 列表。

        返回:
            估算 token 数（含 256 的固定开销）。
        """
        chars = sum(len(str(item.get("content") or "")) for item in messages)
        return chars + 256

    def _available_output_tokens(self, messages: list[dict]) -> int | None:
        """返回本次请求可用的输出空间：上下文窗口 − 已用提示词，不设人为余量。

        上下文窗口未知时返回 None（请求体不带 max_tokens，由模型自身上限决定）。
        提示词已经占满或超过窗口时抛出中文错误，避免静默截断。

        参数:
            messages: 已拼好的 OpenAI messages 列表。

        返回:
            可用的 max_tokens；未知上下文窗口时为 None。

        异常:
            AdapterError(INVALID_INPUT): 提示词已占满上下文窗口。
        """
        limit = self._context_limit()
        prompt_tokens = self._estimate_prompt_tokens(messages)
        if not limit:
            self._budget_stats = {"context_limit": None, "prompt_tokens": prompt_tokens, "max_tokens": None}
            return None
        available = limit - prompt_tokens
        if available < 1:
            raise AdapterError(
                ErrorCode.INVALID_INPUT,
                f"输入约 {prompt_tokens} tokens，已占满模型上下文窗口 {limit} tokens："
                "请缩短输入或减少单次生成数量后重试（输出侧未设置人为上限）",
                detail={"prompt_tokens": prompt_tokens, "context_limit": limit},
            )
        self._budget_stats = {
            "context_limit": limit,
            "prompt_tokens": prompt_tokens,
            "max_tokens": available,
        }
        return available

    def _watchdog_for(self, req: TextGenRequest, available: int | None) -> tuple[int, str] | None:
        """按本轮目标字数计算看门狗（止损线），并把计算口径写进日志。

        口径统一由 `writing.watchdog` 给出：目标字数 × 1.65 × 0.886 token/字，
        末轮加摘要额度，再与上下文剩余空间取小。

        参数:
            req: 统一文本请求；`metadata.watchdog_target_words` 优先，
                其次 `metadata.target_words`。
            available: 上下文剩余可用 token；None 表示窗口未知。

        返回:
            `(max_tokens, 计算说明)`；非正文任务或目标字数未知时返回 None。
        """
        if str(req.task or "") not in _REPETITION_TASKS:
            return None
        meta = req.metadata or {}
        raw = meta.get("watchdog_target_words", meta.get("target_words"))
        try:
            target = int(raw or 0)
        except (TypeError, ValueError):
            target = 0
        if target <= 0:
            return None
        with_summary = bool(meta.get("with_summary"))
        desired = watchdog_tokens(target, with_summary)
        value, _clamped = apply_context_limit(desired, available)
        note = describe_watchdog(target, with_summary, available, value)
        self._budget_stats["max_tokens"] = value
        return value, note

    def _thinking_for(self, req: TextGenRequest) -> tuple[bool, str, str, str]:
        """解析一次请求的思考开关、强度、来源与调用点。

        优先级：全局关闭（最高）> 调用点显式值 > 阶段设置 > 系统自动清单。

        参数:
            req: 统一文本请求。

        返回:
            `(enabled, effort, source, stage)`；source 取 global-off / call / stage / auto。
        """
        meta = req.metadata or {}
        stage = str(meta.get("thinking_stage") or "")
        explicit = meta.get("enable_thinking")
        enabled, source = resolve_thinking(
            self.enable_thinking,
            self.thinking_stages,
            stage,
            None if explicit is None else bool(explicit),
        )
        effort = str(meta.get("reasoning_effort") or "").strip().lower()
        if not effort:
            effort = self.reasoning_effort
        return enabled, effort, source, stage

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
        # 输出侧不设人为上限：有上下文窗口时传「窗口 − 已用提示词」的剩余空间，
        # 不额外留余量；窗口未知时不传 max_tokens，由模型自身决定何时停止。
        available = self._available_output_tokens(messages)
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": float(req.temperature if req.temperature is not None else self.settings.get("temperature", 0.8)),
            "stream": stream,
        }
        # 正文类任务：按本轮目标字数算看门狗（止损线）；其它任务仍用上下文剩余空间。
        watchdog = self._watchdog_for(req, available)
        if watchdog is not None:
            value, note = watchdog
            payload["max_tokens"] = value
            logger.info("看门狗 max_tokens task=%s %s", str(req.task or ""), note)
        elif available is not None:
            payload["max_tokens"] = available
        record_budget_stats(
            at=time.strftime("%Y-%m-%dT%H:%M:%S"),
            task=str(req.task or ""),
            model=self.model,
            **self._budget_stats,
        )
        stop = req.stop or list(self.settings.get("stop") or [])
        if stop:
            payload["stop"] = stop
        # 思考按调用点解析：全局关闭 > 调用点显式值 > 阶段设置 > 系统自动清单；
        # 强度统一跟随全局，不按阶段分档
        enabled, effort, thinking_source, thinking_stage = self._thinking_for(req)
        payload["chat_template_kwargs"] = {"enable_thinking": bool(enabled)}
        if bool(enabled) and effort in _THINKING_EFFORTS:
            payload["reasoning_effort"] = effort
        logger.info(
            "思考生效 task=%s stage=%s enabled=%s source=%s effort=%s",
            str(req.task or ""),
            thinking_stage or "(未标注)",
            enabled,
            thinking_source,
            effort or "(未设置)",
        )
        # 多轮续写前缀稳定时显式请求前缀缓存；服务不支持该字段会忽略
        if req.prompt_cache_key and bool(self.settings.get("cache_prompt", True)):
            payload["cache_prompt"] = True
            payload["prompt_cache_key"] = req.prompt_cache_key
        # 退化保护：正文类任务下发温和档反重复采样参数（先探测服务端支持哪些字段）
        sampling, sampling_source = self._sampling_for(req)
        if sampling:
            payload.update(sampling)
            logger.info(
                "反重复采样参数 task=%s %s（来源：%s）",
                str(req.task or ""),
                sampling,
                sampling_source,
            )
        extra_body = (req.metadata or {}).get("extra_body")
        if isinstance(extra_body, dict):
            payload.update(extra_body)
        return payload

    def _sampling_for(self, req: TextGenRequest) -> tuple[dict, str]:
        """决定本次请求下发哪些温和档反重复采样参数。

        口径来自调研 6.2：先探测服务端认哪些字段，只开温和档。
        - vLLM 系 `repetition_penalty` 会连提示词一起罚，强度 ≤1.05；
        - llama.cpp 系在支持时可加 DRY 温和档；
        - 不开 `no_repeat_ngram_size`，也不用高强度 `repetition_penalty`。

        参数:
            req: 统一文本请求。

        返回:
            `(payload 片段, 来源说明)`；不需要下发时返回 `({}, "")`。
        """
        if str(req.task or "") not in _REPETITION_TASKS:
            return {}, ""
        info = probe_sampling_support(self.endpoint, self.api_key)
        fields = info["fields"]
        params: dict = {}
        source: list[str] = []
        if self.repetition_penalty is not None:
            value = float(self.repetition_penalty)
            if value > MAX_REPETITION_PENALTY:
                logger.warning(
                    "配置的 repetition_penalty=%s 超过温和档上限 %s：可能破坏正常行文",
                    value,
                    MAX_REPETITION_PENALTY,
                )
            params["repetition_penalty"] = value
            source.append("config: omlx.repetition_penalty")
        elif not fields or "repetition_penalty" in fields:
            params["repetition_penalty"] = DEFAULT_REPETITION_PENALTY
            source.append(f"温和档默认 {DEFAULT_REPETITION_PENALTY}")
        if info["family"] == "llama.cpp" and (not fields or "dry_multiplier" in fields):
            params.update(DRY_DEFAULTS)
            source.append("llama.cpp 系 DRY 温和档")
        if not source:
            source.append("服务端未暴露反重复采样字段，仅靠检测与提示词兜底")
        return params, "；".join(source)

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
        """生成文本：`req.stream=True` 走 SSE 流式聚合，否则一次性请求。

        参数:
            req: 统一文本请求。

        返回:
            TextGenResult（text/finish_reason/model/usage）。

        异常:
            AdapterError: 配置缺失、服务不可达、鉴权失败或上游错误。
        """
        self._require_ready()
        self._check_model_available()
        if req.stream:
            return self._generate_stream(req)
        payload = self._build_payload(req, stream=False)
        data = self._request_json("POST", "/v1/chat/completions", payload)
        choices = data.get("choices") or []
        if not choices:
            raise AdapterError(ErrorCode.UPSTREAM_ERROR, f"omlx 返回空 choices：{str(data)[:300]}", detail={"path": "/v1/chat/completions"})
        message = choices[0].get("message") or {}
        # 只取 content：reasoning_content 是模型的思考通道，绝不能当成正文返回
        text = str(message.get("content") or "")
        _log_reasoning(req.task, str(message.get("reasoning_content") or ""))
        return TextGenResult(
            text=text,
            finish_reason=str(choices[0].get("finish_reason") or "stop"),
            model=str(data.get("model") or self.model),
            usage=self._parse_usage(data.get("usage")),
            raw=data,
        )

    def generate_stream(self, req: TextGenRequest, on_delta=None) -> TextGenResult:
        """流式生成文本，边到达边把增量交给 on_delta。

        参数:
            req: 统一文本请求（内部强制走流式）。
            on_delta: 增量回调；回调抛异常会中断生成（用于页面取消）。

        返回:
            与非流式一致的 TextGenResult（text/finish_reason/model/usage）。
        """
        if on_delta is None:
            return self.generate(req)
        return self._generate_stream(req, on_delta=on_delta)

    def _generate_stream(self, req: TextGenRequest, on_delta=None) -> TextGenResult:
        """用 SSE 增量聚合出与非流式一致的 TextGenResult。

        参数:
            req: 统一文本请求。
            on_delta: 可选增量回调，逐片收到正文文本。

        返回:
            TextGenResult；usage 取流中最后出现的用量事件。
        """
        payload = self._build_payload(req, stream=True)
        parts: list[str] = []
        reasoning_parts: list[str] = []
        finish = "stop"
        model = self.model
        usage: Usage | None = None
        for event in self._iter_sse_with_retry(payload):
            if event.get("model"):
                model = str(event["model"])
            if event.get("usage"):
                usage = self._parse_usage(event["usage"])
            choices = event.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            # 思考与正文分开累计：reasoning 只进日志，不拼接、不回调
            text, reasoning = _split_delta(delta)
            if reasoning:
                reasoning_parts.append(reasoning)
            if text:
                parts.append(text)
                if on_delta is not None:
                    on_delta(text)
            if choices[0].get("finish_reason"):
                finish = str(choices[0]["finish_reason"])
        _log_reasoning(req.task, "".join(reasoning_parts))
        return TextGenResult(text="".join(parts), finish_reason=finish, model=model, usage=usage, raw={"stream": True})

    def generate_tool(
        self,
        req: TextGenRequest,
        tool: dict,
        timeout_s: float | None = None,
        on_delta=None,
    ) -> dict:
        """以工具调用（function calling）方式生成结构化结果。

        用 `tool_choice` 强制模型只调用指定函数，模型参数即结构化结果，
        避免自由文本里夹杂解释导致 JSON 解析失败。

        参数:
            req: 统一文本请求（system/messages/temperature 等照常使用）。
            tool: 单个工具定义，形如
                {"type": "function", "function": {"name", "description", "parameters"}}。
            timeout_s: 覆盖默认超时；结构化输出较长可放宽。
            on_delta: 接收增量文本的回调 `(text) -> None`；用于页面显示「已接收 N 字」，
                流式时为 content/arguments 分片，非流式时一次给出全文。

        返回:
            工具参数解析后的 dict（等同 tool_calls[0].function.arguments）。

        异常:
            AdapterError: 配置缺失、服务不可达、未返回 tool_calls 或参数不是 JSON 对象。
        """
        self._require_ready()
        self._check_model_available()
        function = (tool or {}).get("function") or {}
        name = str(function.get("name") or "").strip()
        if not name:
            raise AdapterError(ErrorCode.INVALID_INPUT, "工具定义缺少 function.name")
        parameters = function.get("parameters") or {}
        required = [str(key) for key in (parameters.get("required") or [])]

        payload = self._build_payload(req, stream=bool(req.stream))
        payload["tools"] = [tool]
        payload["tool_choice"] = {"type": "function", "function": {"name": name}}
        # 思考开关与强度已由 _build_payload 按调用点解析并记录日志；
        # 结构化输出不再叠加任何人为预算

        started = time.monotonic()
        url = f"{str(self.endpoint).rstrip('/')}/v1/chat/completions"
        try:
            if req.stream:
                # 流式：tool_calls 会按 index 分片下发，先拼接 name/arguments 再交给同一套解析逻辑
                data = self._aggregate_tool_stream(payload, on_delta=on_delta)
            else:
                data = self._request_json("POST", "/v1/chat/completions", payload, timeout_s=timeout_s)
                if on_delta is not None:
                    self._emit_tool_text(data, on_delta)
            result = self._tool_result(data, name, stream=bool(req.stream), required=required)
        except AdapterError as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            elapsed = int((time.monotonic() - started) * 1000)
            record_model_call(
                at=time.strftime("%Y-%m-%dT%H:%M:%S"),
                url=url,
                status=detail.get("status"),
                elapsed_ms=elapsed,
                ok=False,
                finish_reason=detail.get("finish_reason"),
                tool_calls=detail.get("tool_calls"),
                snippet=str(detail.get("content_head") or "")[:300],
                error=exc.message,
            )
            logger.warning(
                "LLM 调用失败 url=%s status=%s elapsed=%sms error=%s",
                url,
                detail.get("status"),
                elapsed,
                exc.message,
            )
            raise
        elapsed = int((time.monotonic() - started) * 1000)
        snippet, finish, call_count = _call_snippet(data)
        record_model_call(
            at=time.strftime("%Y-%m-%dT%H:%M:%S"),
            url=url,
            status=200,
            elapsed_ms=elapsed,
            ok=True,
            finish_reason=finish,
            tool_calls=call_count,
            snippet=snippet,
            error="",
        )
        logger.info(
            "LLM 调用成功 url=%s status=200 elapsed=%sms finish_reason=%s tool_calls=%s snippet=%s",
            url,
            elapsed,
            finish,
            call_count,
            snippet[:200],
        )
        return result

    @staticmethod
    def _emit_tool_text(data: dict, on_delta) -> None:
        """非流式响应一次性把 content 与 tool_calls 参数交给进度回调。

        参数:
            data: 非流式响应。
            on_delta: 增量回调。

        返回:
            无。
        """
        choices = data.get("choices") or []
        if not choices:
            return
        message = choices[0].get("message") or {}
        pieces = [str(message.get("content") or "")]
        for call in message.get("tool_calls") or []:
            pieces.append(str(((call or {}).get("function") or {}).get("arguments") or ""))
        text = "".join(pieces)
        if text:
            on_delta(text)

    def _aggregate_tool_stream(self, payload: dict, on_delta=None) -> dict:
        """把流式 tool_calls 分片拼接成非流式响应结构。

        不同实现会把 `function.name` 与 `function.arguments` 拆成多片，
        这里按 `index` 归并并逐片累加，避免只取第一片导致参数残缺。

        参数:
            payload: 已带 tools/tool_choice 的请求体（stream=true）。

        返回:
            形如非流式响应的 dict：`{model, choices:[{message:{tool_calls,content},finish_reason}], usage}`。
        """
        slots: dict[int, dict] = {}
        contents: list[str] = []
        finish = ""
        model = self.model
        usage: dict | None = None
        for event in self._iter_sse_with_retry(payload):
            if event.get("model"):
                model = str(event["model"])
            if isinstance(event.get("usage"), dict):
                usage = event["usage"]
            for choice in event.get("choices") or []:
                if not isinstance(choice, dict):
                    continue
                if choice.get("finish_reason"):
                    finish = str(choice["finish_reason"])
                delta = choice.get("delta") or {}
                # 不支持的模型会把 JSON 写在 content 里，必须累计下来供兜底解析
                if delta.get("content"):
                    contents.append(str(delta["content"]))
                    if on_delta is not None:
                        on_delta(str(delta["content"]))
                for shard in delta.get("tool_calls") or []:
                    if not isinstance(shard, dict):
                        continue
                    # 缺 index 时按同一次调用（0）处理，不丢弃分片
                    try:
                        index = int(shard.get("index") or 0)
                    except (TypeError, ValueError):
                        index = 0
                    slot = slots.setdefault(
                        index,
                        {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                    )
                    if shard.get("id"):
                        slot["id"] = str(shard["id"])
                    if shard.get("type"):
                        slot["type"] = str(shard["type"])
                    function = shard.get("function") or {}
                    if function.get("name"):
                        slot["function"]["name"] += str(function["name"])
                    arguments = function.get("arguments")
                    if isinstance(arguments, dict):
                        # 个别实现直接给对象，保留结构，避免 str(dict) 变成非法 JSON
                        slot["function"]["arguments"] = arguments
                    elif arguments:
                        current = slot["function"]["arguments"]
                        if isinstance(current, dict):
                            current = json.dumps(current, ensure_ascii=False)
                        slot["function"]["arguments"] = current + str(arguments)
                        if on_delta is not None:
                            on_delta(str(arguments))
        message: dict = {"role": "assistant", "content": "".join(contents)}
        if slots:
            message["tool_calls"] = [slots[key] for key in sorted(slots)]
        return {"model": model, "choices": [{"message": message, "finish_reason": finish or "stop"}], "usage": usage or {}}

    @staticmethod
    def _extract_json_object(text: str) -> dict | None:
        """从模型自由文本里提取第一个 JSON 对象。

        容忍 ```json 代码块包裹与前后说明文字：先试整段，再试代码块，
        最后从每个 `{` 起用 raw_decode 扫描。

        参数:
            text: 模型返回的 content 文本。

        返回:
            dict；提取不到返回 None。
        """
        source = str(text or "").strip()
        if not source:
            return None
        try:
            whole = json.loads(source)
        except json.JSONDecodeError:
            whole = None
        if isinstance(whole, dict):
            return whole
        for block in re.findall(r"```(?:json)?\s*(.*?)```", source, flags=re.S | re.I):
            candidate = block.strip()
            if not candidate:
                continue
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                return parsed
        decoder = json.JSONDecoder()
        for index, char in enumerate(source):
            if char != "{":
                continue
            try:
                parsed, _end = decoder.raw_decode(source, index)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                return parsed
        return None

    @staticmethod
    def _tool_error_text(
        name: str, finish: str, calls_len: int, content: str, stream: bool, has_content_json: bool = False
    ) -> str:
        """拼装工具调用失败的可读原因（含诊断字段）。

        参数:
            name: 工具函数名。
            finish: finish_reason。
            calls_len: tool_calls 数量。
            content: message.content 原文。
            stream: 是否流式。
            has_content_json: content 里是否提取到了 JSON（用于区分「不支持 tools」与「schema 不符」）。

        返回:
            中文错误说明。
        """
        snippet = content[:200].replace("\n", " ") or "（空）"
        diagnostics = (
            f"[finish_reason={finish or '空'}，tool_calls={calls_len}，content={len(content)} 字，"
            f"流式={'是' if stream else '否'}，content 前 200 字：{snippet}]"
        )
        if calls_len:
            lead = f"工具调用 {name} 的参数不可用：可重试，或改用文本模式（要求模型直接输出 JSON）"
        elif has_content_json:
            lead = (
                f"模型未返回 tool_calls，content 中的 JSON 不符合工具 {name} 的 schema："
                "请让模型按 schema 输出，或换用支持 tools 的模型"
            )
        else:
            lead = (
                "当前模型不支持工具调用（未返回 tool_calls，content 中也提取不到 JSON）："
                "请换用支持 tools 的模型或改用文本模式（要求模型直接输出 JSON）"
            )
        return lead + diagnostics

    def _tool_result(self, data: dict, name: str, stream: bool = False, required: list[str] | None = None) -> dict:
        """解析工具调用响应（流式聚合结果与非流式响应共用）。

        优先用 `tool_calls[0].function.arguments`；模型不支持 tools 时
        （200 但 tool_calls 为空、JSON 写在 content 里）转向 content 兜底提取，
        并校验工具 schema 的 required 字段。

        参数:
            data: 非流式响应或 `_aggregate_tool_stream` 的聚合结果。
            name: 工具函数名，用于中文错误提示。
            stream: 本次是否流式，用于诊断。
            required: 工具参数 schema 的必需字段。

        返回:
            工具参数解析后的 dict。

        异常:
            AdapterError: 空 choices、没有可用 tool_calls 且 content 也提不出 JSON、
                或缺少必需字段。
        """
        choices = data.get("choices") or []
        if not choices:
            raise AdapterError(
                ErrorCode.UPSTREAM_ERROR,
                f"omlx 返回空 choices（流式={'是' if stream else '否'}）：{str(data)[:300]}",
                detail={"path": "/v1/chat/completions", "stream": stream},
            )
        finish = str(choices[0].get("finish_reason") or "")
        message = choices[0].get("message") or {}
        calls = message.get("tool_calls") or []
        if not isinstance(calls, list):
            calls = []
        content = str(message.get("content") or "")
        usage = self._parse_usage(data.get("usage"))
        required = [str(key) for key in (required or [])]
        detail = {
            "tool": name,
            "stream": stream,
            "finish_reason": finish,
            "tool_calls": len(calls),
            "content_length": len(content),
            "content_head": content[:200].replace("\n", " "),
            "prompt_tokens": getattr(usage, "prompt_tokens", 0),
            "completion_tokens": getattr(usage, "completion_tokens", 0),
        }

        failure = "tool_calls 为空"
        if calls:
            arguments = ((calls[0] or {}).get("function") or {}).get("arguments")
            if isinstance(arguments, dict):
                missing = [key for key in required if key not in arguments]
                if not missing:
                    return arguments
                failure = f"tool_calls[0].function.arguments 缺少必需字段：{'、'.join(missing)}"
            else:
                raw_args = str(arguments or "").strip()
                if not raw_args:
                    failure = "tool_calls[0].function.arguments 为空"
                else:
                    try:
                        parsed = json.loads(raw_args)
                    except json.JSONDecodeError as exc:
                        failure = f"tool_calls[0].function.arguments 不是合法 JSON：{exc}"
                    else:
                        if not isinstance(parsed, dict):
                            failure = f"tool_calls[0].function.arguments 不是 JSON 对象：{str(parsed)[:200]}"
                        else:
                            missing = [key for key in required if key not in parsed]
                            if not missing:
                                return parsed
                            failure = f"tool_calls[0].function.arguments 缺少必需字段：{'、'.join(missing)}"

        # 兜底：模型不支持 tools 时通常把 JSON 写在 content；按同一 schema 的 required 校验
        parsed = self._extract_json_object(content)
        has_content_json = isinstance(parsed, dict)
        if has_content_json:
            missing = [key for key in required if key not in parsed]
            if not missing:
                return parsed
            failure = f"content 中的 JSON 缺少必需字段：{'、'.join(missing)}"

        headline = self._tool_error_text(name, finish, len(calls), content, stream, has_content_json)
        if finish == "length":
            headline = f"模型输出被截断（finish_reason=length），工具调用 {name} 未完整返回；{headline}"
        raise AdapterError(ErrorCode.UPSTREAM_ERROR, f"{headline}（{failure}）", retryable=True, detail=detail)

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
        self._check_model_available()
        payload = self._build_payload(req, stream=True)
        reasoning_parts: list[str] = []
        for event in self._iter_sse_with_retry(payload):
            usage = self._parse_usage(event.get("usage")) if event.get("usage") else None
            choices = event.get("choices") or []
            if not choices:
                if usage:
                    yield TextGenChunk(delta="", done=False, usage=usage)
                continue
            delta = choices[0].get("delta") or {}
            # 只把 content 交给调用方；reasoning 单独累计，结束后只写日志
            text, reasoning = _split_delta(delta)
            if reasoning:
                reasoning_parts.append(reasoning)
            finish = choices[0].get("finish_reason")
            if text:
                yield TextGenChunk(delta=text, done=False, usage=usage)
            if finish:
                _log_reasoning(req.task, "".join(reasoning_parts))
                yield TextGenChunk(delta="", done=True, usage=usage)
                return
        _log_reasoning(req.task, "".join(reasoning_parts))
        yield TextGenChunk(delta="", done=True)

    @staticmethod
    def _event_has_output(event: dict) -> bool:
        """判断一个 SSE 事件是否已经产生可见输出（内容、推理或工具分片）。

        参数:
            event: 解析后的 SSE JSON。

        返回:
            布尔值；用于决定是否结束「首字超时」阶段。
        """
        choices = event.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            return False
        if choices[0].get("finish_reason"):
            return True
        delta = choices[0].get("delta") or {}
        return bool(delta.get("content") or delta.get("reasoning_content") or delta.get("tool_calls"))

    def _read_sse(self, payload: dict) -> Iterator[dict]:
        """读取 SSE 流并逐条产出 JSON 事件，执行首字/空闲/总时长三级超时。

        参数:
            payload: 请求体（stream=true）。

        返回:
            逐条解析出的 JSON dict。

        异常:
            AdapterError(TIMEOUT):
                - 首字超时：发出请求后指定秒数内没有任何内容；
                - 生成卡住：两个分片之间超过空闲上限；
                - 超过总时长上限。
        """
        # 首字超时从「发出请求」开始计时：建连与等待响应头的时间也计入
        started = time.monotonic()
        resp = self._open("POST", "/v1/chat/completions", payload, stream=True)
        first_at: float | None = None
        last_at = started
        with resp:
            while True:
                now = time.monotonic()
                # 总时长上限优先判定：已经生成的内容不会回滚，因此不可重试
                if self.total_timeout_s and now - started >= self.total_timeout_s:
                    raise AdapterError(
                        ErrorCode.TIMEOUT,
                        f"omlx 超过总时长上限（{self.total_timeout_s:.0f} 秒，/v1/chat/completions）："
                        "已生成内容不会回滚；可调大 omlx.total_timeout_s 或分批生成",
                        retryable=False,
                        detail={"stage": "total"},
                    )
                if first_at is None:
                    budget = self.first_token_timeout_s - (now - started)
                else:
                    budget = self.stream_idle_timeout_s - (now - last_at)
                if self.total_timeout_s:
                    budget = min(budget, self.total_timeout_s - (now - started))
                # 每轮读之前把 socket 超时收紧到当前预算，超时即可定位到具体阶段
                resp.settimeout(max(0.05, budget))
                try:
                    raw = resp.readline()
                except (socket.timeout, TimeoutError) as exc:
                    elapsed = time.monotonic() - started
                    if self.total_timeout_s and elapsed >= self.total_timeout_s - 0.05:
                        raise AdapterError(
                            ErrorCode.TIMEOUT,
                            f"omlx 超过总时长上限（{self.total_timeout_s:.0f} 秒，/v1/chat/completions）："
                            "已生成内容不会回滚；可调大 omlx.total_timeout_s 或分批生成",
                            retryable=False,
                            detail={"stage": "total"},
                        ) from exc
                    if first_at is None:
                        raise AdapterError(
                            ErrorCode.TIMEOUT,
                            f"omlx 首字超时（{self.first_token_timeout_s:.0f} 秒内未收到任何内容，"
                            "/v1/chat/completions）：模型可能正在加载或排队；可稍后重试，"
                            "或调大 omlx.first_token_timeout_s",
                            retryable=True,
                            detail={"stage": "first_token"},
                        ) from exc
                    gap = time.monotonic() - last_at
                    raise AdapterError(
                        ErrorCode.TIMEOUT,
                        f"omlx 生成卡住（{gap:.0f} 秒无新内容，超过空闲上限 "
                        f"{self.stream_idle_timeout_s:.0f} 秒）：可重试，或调大 omlx.stream_idle_timeout_s",
                        retryable=True,
                        detail={"stage": "idle"},
                    ) from exc
                if not raw:
                    return
                line = raw.decode("utf-8", errors="replace").strip()
                if not line or line.startswith(":") or not line.startswith("data:"):
                    continue
                chunk_text = line[5:].strip()
                if chunk_text == "[DONE]":
                    return
                try:
                    event = json.loads(chunk_text)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                last_at = time.monotonic()
                if first_at is None and self._event_has_output(event):
                    # 收到首个内容分片：后续改用空闲超时约束分片间隔
                    first_at = last_at
                yield event

    def _iter_sse_with_retry(self, payload: dict) -> Iterator[dict]:
        """带重试的 SSE 事件迭代：只在尚未收到任何事件前允许整体重试。

        一旦已经开始输出，就不再重试，避免重复生成与重复计费。

        参数:
            payload: 请求体（stream=true）。

        返回:
            逐条 SSE 事件。
        """
        attempt = 0
        while True:
            emitted = False
            try:
                for event in self._read_sse(payload):
                    emitted = True
                    yield event
                return
            except AdapterError as exc:
                if emitted or not exc.retryable or attempt >= self.max_retries:
                    raise
            attempt += 1
            time.sleep(min(2.0 * attempt, 5.0))

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
