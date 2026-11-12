"""写作模型适配（omlx，OpenAI 兼容 HTTP）。

实测结论（2026-11-09，服务 v0.7.3）：
- 形态：OpenAI 兼容本地 HTTP，地址由配置提供（无内置默认值），鉴权 ``Authorization: Bearer <key>``；
- 能力：``GET /v1/models``、``POST /v1/chat/completions``（支持 SSE 流式）、
  ``POST /v1/models/{model_id}/unload`` 显式卸载、``GET /health`` 查看显存占用；
- omlx 不会自动卸载模型（``idle_timeout=null``），必须显式卸载；内存天花板约 29 GB，
  因此文本生成与 ComfyUI 必须串行，且按调度策略在文本任务后卸载。

本模块只负责协议映射与错误归一，不做重试编排以外的业务判断。
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

from .base import AdapterError, ErrorCode, HealthStatus, TextGenChunk, TextGenRequest, TextGenResult, Usage

# 一次非流式请求的默认超时（秒）；长文任务已改走流式，这里只覆盖短调用
DEFAULT_TIMEOUT_S = 300.0
# 连接超时（秒）；建立 TCP 连接的最长等待，用于快速判断服务是否可达
CONNECT_TIMEOUT_S = 5.0
#: 首字超时（秒）：从发出请求到收到 SSE 第一个内容分片的最长等待
FIRST_TOKEN_TIMEOUT_S = 60.0
#: 流式空闲超时（秒）：两个内容分片之间的最长间隔
STREAM_IDLE_TIMEOUT_S = 60.0
# 工具调用时的最小输出预算；thinking 模型的 reasoning_tokens 也计入 max_tokens，给少了参数会被截断
TOOL_MIN_MAX_TOKENS = 4096
#: 用户既未填写 max_tokens、也没有检测值时的协议层兜底：
#: OpenAI 兼容接口必须给一个正整数，这里只保证请求合法，不代表推荐值或模型上限；
#: 设置页会优先用「检测连接」返回的最大输出并落盘。
PROTOCOL_DEFAULT_MAX_TOKENS = 2048
# 检测到的上下文上限缓存：`(endpoint, model) -> tokens`，供请求体预算校验复用，避免每次请求再发 HTTP
_LIMIT_CACHE: dict[tuple[str, str], int] = {}
#: 各 OpenAI 兼容实现可能使用的候选字段名（上下文 / 最大输出）
_CONTEXT_FIELDS = ("max_model_len", "context_length", "context_window", "model_context_length", "max_context_window", "n_ctx")
_OUTPUT_FIELDS = ("max_output_tokens", "max_completion_tokens", "max_tokens", "max_new_tokens")
#: 探测上限时额外尝试的非标准端点（没有的会记录 404 / 非 JSON，不抛错）
_LIMIT_PROBE_PATHS = ("/props", "/v1/internal/model/info", "/v1/model/info", "/health")
#: 模型列表缓存：`(endpoint, api_key) -> (抓取时间, id 列表)`；只用于调用前轻量校验，短 TTL
_MODEL_CACHE: dict[tuple[str, str], tuple[float, list[str]]] = {}
_MODEL_CACHE_TTL_S = 30.0


class _HttpResponse:
    """http.client 响应的轻量包装：关闭时一并关闭连接，并支持动态调整读超时。"""

    def __init__(self, conn, resp):
        """绑定连接与响应。

        参数:
            conn: http.client.HTTPConnection/HTTPSConnection。
            resp: conn.getresponse() 返回的响应对象。
        """
        self._conn = conn
        self._resp = resp
        self.status = int(getattr(resp, "status", 0))

    def read(self):
        """读取全部响应体。"""
        return self._resp.read()

    def readline(self):
        """读取一行（SSE 逐行读取用）。"""
        return self._resp.readline()

    def __iter__(self):
        """按行迭代响应体。"""
        return iter(self._resp)

    def settimeout(self, seconds: float) -> None:
        """调整底层 socket 的读超时；连接已关闭时静默忽略。

        参数:
            seconds: 读超时秒数。
        """
        sock = getattr(self._conn, "sock", None)
        if sock is not None:
            try:
                sock.settimeout(max(0.05, float(seconds)))
            except OSError:
                pass

    def close(self) -> None:
        """关闭响应与连接。"""
        try:
            self._resp.close()
        finally:
            self._conn.close()

    def __enter__(self):
        """进入 with 语句。"""
        return self

    def __exit__(self, exc_type, exc, tb):
        """离开 with 语句时关闭。"""
        self.close()


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
                "omlx 未配置地址：请在设置页填写服务地址（或在 config/config.yaml 的 omlx.endpoint 配置）",
            )
        data = self._request_json("GET", "/v1/models", timeout_s=timeout_s)
        models = data.get("data") if isinstance(data, dict) else None
        return models if isinstance(models, list) else []

    def model_ids(self) -> list[str]:
        """返回模型 ID 列表。"""
        return [str(item.get("id")) for item in self.list_models() if item.get("id")]

    def fetch_model_catalog(self, timeout_s: float = 2.0) -> dict:
        """实时读取服务端模型列表（设置页「测试连接」与模型下拉的唯一来源）。

        只访问 OpenAI 兼容的 `GET /v1/models`，不读本机配置文件；单次请求短超时，
        失败不抛异常，统一转成中文说明，便于页面直接展示。

        参数:
            timeout_s: 单次请求超时秒数（默认 2 秒）。

        返回:
            dict：`{ok, connected, status, models, count, message}`；
            `models` 条目含 `id/owned_by/created/max_model_len`，字段存在上限时
            额外带 `context_length/max_output_tokens`（来源固定为 server）。
        """
        if not self.endpoint:
            return {
                "ok": False,
                "connected": False,
                "status": None,
                "models": [],
                "count": 0,
                "message": "未配置接口地址：请先在设置页填写 omlx.endpoint",
            }
        status, data, error = self._probe("/v1/models", timeout_s)
        if status in (401, 403):
            return {
                "ok": False,
                "connected": True,
                "status": status,
                "models": [],
                "count": 0,
                "message": f"密钥无效或无权限（HTTP {status}）：请检查 omlx.api_key 是否与服务端一致",
            }
        if status is None:
            reason = str(error or "无响应")
            return {
                "ok": False,
                "connected": False,
                "status": None,
                "models": [],
                "count": 0,
                "message": f"无法连接服务（{self.endpoint}）：{reason}；请确认服务已启动、地址正确",
            }
        if status != 200 or not isinstance(data, dict):
            snippet = str(data)[:200] if data else "响应不是 JSON"
            return {
                "ok": False,
                "connected": True,
                "status": status,
                "models": [],
                "count": 0,
                "message": f"服务返回 HTTP {status}：{snippet}",
            }
        rows = data.get("data")
        if not isinstance(rows, list):
            rows = data.get("models") if isinstance(data.get("models"), list) else []
        models: list[dict] = []
        for row in rows:
            if not isinstance(row, dict) or not row.get("id"):
                continue
            context, output, _hits = self._pick_limits(row)
            entry = {
                "id": str(row.get("id")),
                "owned_by": row.get("owned_by"),
                "created": row.get("created"),
                "max_model_len": row.get("max_model_len"),
            }
            if context:
                entry["context_length"] = context
                entry["context_source"] = "server"
            if output:
                entry["max_output_tokens"] = output
                entry["max_output_source"] = "server"
            models.append(entry)
        if not models:
            return {
                "ok": False,
                "connected": True,
                "status": status,
                "models": [],
                "count": 0,
                "message": "服务可达但未返回任何模型",
            }
        _MODEL_CACHE[(self.endpoint, self.api_key)] = (time.monotonic(), [item["id"] for item in models])
        return {
            "ok": True,
            "connected": True,
            "status": status,
            "models": models,
            "count": len(models),
            "message": f"连接成功，已获取 {len(models)} 个模型",
        }

    def invalidate_model_cache(self) -> None:
        """清掉本适配器对应的模型列表缓存（设置页刷新后调用）。"""
        _MODEL_CACHE.pop((self.endpoint, self.api_key), None)

    def _check_model_available(self) -> None:
        """调用前轻量校验配置的模型仍在服务端列表中。

        缓存 30 秒，避免每次生成都多打一次 `/v1/models`；列表接口本身不可用时
        不阻断调用（由真实请求给出可读错误），只在确认列表为空或缺少目标模型时报错。

        异常:
            AdapterError(INVALID_INPUT): 配置模型不在服务端返回的模型列表里。
        """
        if not self.endpoint or not self.model:
            return
        key = (self.endpoint, self.api_key)
        cached = _MODEL_CACHE.get(key)
        if cached and time.monotonic() - cached[0] < _MODEL_CACHE_TTL_S:
            ids = cached[1]
        else:
            status, data, _error = self._probe("/v1/models", 2.0)
            # 列表接口失败（401/超时/非标准实现）交给真实请求处理，避免误伤可用服务
            if status != 200 or not isinstance(data, dict):
                return
            rows = data.get("data") if isinstance(data.get("data"), list) else []
            ids = [str(row.get("id")) for row in rows if isinstance(row, dict) and row.get("id")]
            if not ids:
                return
            _MODEL_CACHE[key] = (time.monotonic(), ids)
        if self.model not in ids:
            raise AdapterError(
                ErrorCode.INVALID_INPUT,
                f"模型 {self.model} 不在服务端模型列表中，当前可用：{'、'.join(ids)}。"
                "请到「设置 → 写作模型」重新选择模型后再试",
                detail={"model": self.model, "available": ids, "endpoint": self.endpoint},
            )

    def _probe(self, path: str, timeout_s: float) -> tuple[int | None, dict | None, str | None]:
        """快速 GET 一个端点，用于上限探测。

        参数:
            path: 以 / 开头的接口路径。
            timeout_s: 超时秒数（设置页检测用 2 秒级）。

        返回:
            `(HTTP 状态, JSON dict 或 None, 错误文本)`；404、非 JSON、网络错误都不抛异常。
        """
        request = urllib.request.Request(self._url(path), headers=self._headers(), method="GET")
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                try:
                    data = json.loads(raw) if raw.strip() else {}
                except json.JSONDecodeError:
                    data = None
                return int(getattr(resp, "status", 200)), data if isinstance(data, dict) else None, None
        except urllib.error.HTTPError as exc:
            return int(exc.code), None, None
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
            return None, None, str(getattr(exc, "reason", exc) or exc)

    @staticmethod
    def _pick_limits(data: dict) -> tuple[int | None, int | None, list[str]]:
        """从任意字典里挑选上下文/输出上限字段（兼容不同实现的命名）。

        参数:
            data: 接口返回的模型对象或详情 dict。

        返回:
            `(上下文上限, 最大输出, 命中字段名列表)`；取不到时为 None。
        """

        def first(fields: tuple[str, ...]) -> int | None:
            for key in fields:
                value = data.get(key)
                if isinstance(value, (int, float)) and int(value) > 0:
                    return int(value)
            return None

        context = first(_CONTEXT_FIELDS)
        output = first(_OUTPUT_FIELDS)
        hits = [
            key
            for key in _CONTEXT_FIELDS + _OUTPUT_FIELDS
            if isinstance(data.get(key), (int, float)) and int(data[key]) > 0
        ]
        return context, output, hits

    @staticmethod
    def _merge_limits(entry: dict, context: int | None, output: int | None) -> None:
        """把探测到的上限合并进模型条目，只补缺失值，来源记为 server。

        参数:
            entry: 模型条目 dict（会被原地修改）。
            context: 探测到的上下文上限。
            output: 探测到的最大输出。
        """
        if context and not entry.get("context_length"):
            entry["context_length"] = context
            entry["context_source"] = "server"
        if output and not entry.get("max_output_tokens"):
            entry["max_output_tokens"] = output
            entry["max_output_source"] = "server"

    def fetch_model_limits(self, model: str | None = None, timeout_s: float = 2.0) -> dict:
        """探测模型的上下文与最大输出上限（只读 OpenAI 兼容接口，不读本机文件）。

        探测顺序：`/v1/models` 列表项候选字段 → `/v1/models/{id}` 详情 →
        `/v1/models/status` 等服务特有端点 → `/props` 等通用候选端点。

        参数:
            model: 目标模型；缺省用配置里的 model。
            timeout_s: 单个端点的超时秒数（设置页检测用 2 秒级，失败快速返回）。

        返回:
            dict：`{model, context_length, context_source, max_output_tokens,
            max_output_source, models, probes, message}`；
            来源只有 `server`（接口字段）与 `unknown`（未取到）两种。
        """
        target = str(model or self.model or "").strip()
        probes: list[dict] = []
        result = {
            "model": target,
            "context_length": None,
            "context_source": "unknown",
            "max_output_tokens": None,
            "max_output_source": "unknown",
            "models": [],
            "probes": probes,
        }
        if not self.endpoint:
            result["message"] = "未配置接口地址：请先填写 omlx.endpoint 后再检测"
            return result

        # 1) 列表接口：不同实现的候选字段都试一遍
        status, data, error = self._probe("/v1/models", timeout_s)
        fields: list[str] = []
        models: list[dict] = []
        rows = (data or {}).get("data") if isinstance(data, dict) else None
        if isinstance(rows, list):
            for item in rows:
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                context, output, hits = self._pick_limits(item)
                fields.extend(hits)
                models.append(
                    {
                        "id": str(item.get("id")),
                        "owned_by": item.get("owned_by"),
                        "context_length": context,
                        "context_source": "server" if context else "unknown",
                        "max_output_tokens": output,
                        "max_output_source": "server" if output else "unknown",
                    }
                )
        probes.append({"path": "/v1/models", "status": status, "fields": sorted(set(fields)), "error": error})
        if not isinstance(rows, list):
            result["message"] = (
                f"读取 /v1/models 失败（{error or status or '无响应'}）："
                "接口不可达或返回非 JSON；可稍后重试，或手动填写最大输出"
            )
            return result

        selected = next((item for item in models if item["id"] == target), None)
        if not target:
            # 未选模型：只返回列表供页面下拉使用，绝不自动挑一个模型去探测/请求
            result["models"] = models
            result["message"] = "未选择模型，请在设置页检测连接后选择"
            return result
        if selected is None:
            # 已保存的模型不在当前列表：明确报错，不静默换成其他模型
            result["models"] = models
            result["message"] = f"已保存的模型 {target} 不在当前服务列表中，请重新选择"
            return result
        # 2) 单模型详情（有的实现支持）
        if target:
            path = f"/v1/models/{urllib.parse.quote(target, safe='')}"
            status, data, error = self._probe(path, timeout_s)
            fields = []
            context = output = None
            if isinstance(data, dict):
                context, output, fields = self._pick_limits(data)
                for key in ("model", "data"):
                    nested = data.get(key)
                    if isinstance(nested, dict):
                        nested_context, nested_output, nested_fields = self._pick_limits(nested)
                        context = context or nested_context
                        output = output or nested_output
                        fields.extend(nested_fields)
            probes.append({"path": path, "status": status, "fields": sorted(set(fields)), "error": error})
            if selected is not None:
                self._merge_limits(selected, context, output)
        # 3) 服务特有端点（omlx 有 /v1/models/status；换服务时可能 404，忽略即可）
        status, data, error = self._probe("/v1/models/status", timeout_s)
        fields = []
        if isinstance(data, dict):
            # omlx 实测形态：上限字段直接放在顶层（model_context_length / max_context_window / max_tokens）
            top_context, top_output, top_hits = self._pick_limits(data)
            if top_hits:
                fields.extend(top_hits)
                # 顶层字段可能带模型标识；只有未标注或标注与目标一致时才并入当前目标模型
                # （不用裸 id：部分实现会把它当作请求标识，容易误判）
                owner = ""
                for key in ("model", "model_id", "current_model", "loaded_model"):
                    if data.get(key):
                        owner = str(data[key])
                        break
                if selected is not None and (not owner or owner == target):
                    self._merge_limits(selected, top_context, top_output)
        if isinstance(data, dict) and isinstance(data.get("models"), list):
            for item in data["models"]:
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                context, output, hits = self._pick_limits(item)
                fields.extend(hits)
                entry = next((row for row in models if row["id"] == str(item["id"])), None)
                if entry is None:
                    entry = {
                        "id": str(item["id"]),
                        "owned_by": None,
                        "context_length": None,
                        "context_source": "unknown",
                        "max_output_tokens": None,
                        "max_output_source": "unknown",
                    }
                    models.append(entry)
                self._merge_limits(entry, context, output)
                if target and entry["id"] == target:
                    selected = entry
        probes.append({"path": "/v1/models/status", "status": status, "fields": sorted(set(fields)), "error": error})
        # 4) 通用候选端点：能识别就识别，识别不到只记录 404
        for path in _LIMIT_PROBE_PATHS:
            status, data, error = self._probe(path, timeout_s)
            fields = []
            context = output = None
            if isinstance(data, dict):
                context, output, fields = self._pick_limits(data)
                for key in ("model", "data"):
                    nested = data.get(key)
                    if isinstance(nested, dict):
                        nested_context, nested_output, nested_fields = self._pick_limits(nested)
                        context = context or nested_context
                        output = output or nested_output
                        fields.extend(nested_fields)
            probes.append({"path": path, "status": status, "fields": sorted(set(fields)), "error": error})
            if selected is not None:
                self._merge_limits(selected, context, output)

        if selected is not None:
            result["context_length"] = selected["context_length"]
            result["context_source"] = selected["context_source"]
            result["max_output_tokens"] = selected["max_output_tokens"]
            result["max_output_source"] = selected["max_output_source"]
        if result["context_length"]:
            # 缓存供请求体预算校验使用（不落盘，进程内共享）
            _LIMIT_CACHE[(self.endpoint, target)] = int(result["context_length"])
        result["models"] = models
        if result["context_length"] and result["max_output_tokens"]:
            result["message"] = (
                f"已按 {target} 检测：上下文 {result['context_length']} tokens / "
                f"最大输出 {result['max_output_tokens']} tokens（来源：服务端字段）"
            )
        elif result["context_length"]:
            result["message"] = (
                f"已按 {target} 检测到上下文 {result['context_length']} tokens（来源：服务端字段）；"
                "服务端未返回最大输出，可手动填写"
            )
        elif result["max_output_tokens"]:
            result["message"] = (
                f"已按 {target} 检测到最大输出 {result['max_output_tokens']} tokens（来源：服务端字段）；"
                "服务端未返回上下文上限"
            )
        elif not result.get("message"):
            result["message"] = (
                f"服务端未返回 {target or '所选模型'} 的上下文/输出上限"
                "（已尝试 /v1/models、/v1/models/{id}、/v1/models/status 等）：可手动填写最大输出"
            )
        return result

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
            return HealthStatus(ok=False, detail="omlx 未配置 endpoint：请在设置页填写服务地址（或配置 omlx.endpoint）")
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
                detail=f"模型 {self.model} 不在服务列表中：可用模型 {ids}；请在设置页重新检测并选择模型",
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

    def _fit_output_budget(self, messages: list[dict], max_tokens: int) -> int:
        """按上下文上限约束输出预算：超限时自动下调，完全放不下时报中文错误。

        参数:
            messages: 已拼好的 OpenAI messages 列表。
            max_tokens: 请求期望的输出预算。

        返回:
            调整后的 max_tokens。

        异常:
            AdapterError(INVALID_INPUT): prompt 已占满上下文，无法再留出输出空间。
        """
        limit = self._context_limit()
        if not limit:
            return max_tokens
        prompt_tokens = self._estimate_prompt_tokens(messages)
        available = limit - prompt_tokens
        if available < 256:
            raise AdapterError(
                ErrorCode.INVALID_INPUT,
                f"输入约 {prompt_tokens} tokens，超过模型上下文上限 {limit} tokens："
                "请缩短输入或减少单次生成数量后重试",
                detail={"prompt_tokens": prompt_tokens, "context_limit": limit},
            )
        return min(max_tokens, available)

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
        max_tokens = self._fit_output_budget(
            messages,
            int(req.max_tokens or self.settings.get("max_tokens") or PROTOCOL_DEFAULT_MAX_TOKENS),
        )
        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
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
        text = message.get("content") or message.get("reasoning_content") or ""
        return TextGenResult(
            text=text,
            finish_reason=str(choices[0].get("finish_reason") or "stop"),
            model=str(data.get("model") or self.model),
            usage=self._parse_usage(data.get("usage")),
            raw=data,
        )

    def _generate_stream(self, req: TextGenRequest) -> TextGenResult:
        """用 SSE 增量聚合出与非流式一致的 TextGenResult。

        参数:
            req: 统一文本请求。

        返回:
            TextGenResult；usage 取流中最后出现的用量事件。
        """
        payload = self._build_payload(req, stream=True)
        parts: list[str] = []
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
            text = delta.get("content") or delta.get("reasoning_content") or ""
            if text:
                parts.append(text)
            if choices[0].get("finish_reason"):
                finish = str(choices[0]["finish_reason"])
        return TextGenResult(text="".join(parts), finish_reason=finish, model=model, usage=usage, raw={"stream": True})

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
        # thinking 模型的 reasoning_tokens 同样占用 max_tokens，预算太小会把参数截断；
        # 结构化输出默认关闭思考，调用方可用 metadata.enable_thinking 显式覆盖
        thinking = (req.metadata or {}).get("enable_thinking")
        payload.setdefault("chat_template_kwargs", {})["enable_thinking"] = bool(thinking) if thinking is not None else False
        payload["max_tokens"] = max(int(payload.get("max_tokens") or 0), TOOL_MIN_MAX_TOKENS)

        if req.stream:
            # 流式：tool_calls 会按 index 分片下发，先拼接 name/arguments 再交给同一套解析逻辑
            data = self._aggregate_tool_stream(payload)
        else:
            data = self._request_json("POST", "/v1/chat/completions", payload, timeout_s=timeout_s)
        return self._tool_result(data, name, stream=bool(req.stream), required=required)

    def _aggregate_tool_stream(self, payload: dict) -> dict:
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
        for event in self._iter_sse_with_retry(payload):
            usage = self._parse_usage(event.get("usage")) if event.get("usage") else None
            choices = event.get("choices") or []
            if not choices:
                if usage:
                    yield TextGenChunk(delta="", done=False, usage=usage)
                continue
            delta = choices[0].get("delta") or {}
            text = delta.get("content") or delta.get("reasoning_content") or ""
            finish = choices[0].get("finish_reason")
            if text:
                yield TextGenChunk(delta=text, done=False, usage=usage)
            if finish:
                yield TextGenChunk(delta="", done=True, usage=usage)
                return
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
