"""写作适配的模块级工具：调用摘要记录、采样能力探测、SSE 增量解析与 HTTP 响应包装。

这些常量与函数被 `catalog`、`omlx` 共用，独立成模块避免两边各存一份。
"""

from __future__ import annotations

import logging
import threading
import urllib.request

#: 日志：每次写作 LLM HTTP 调用记录地址、状态码、耗时与返回片段
logger = logging.getLogger(__name__)

#: 最近一次写作 LLM 调用摘要（进程级），供任务页 / 设置页随时自查
_LAST_CALL: dict = {}
_LAST_CALL_LOCK = threading.Lock()


#: 最近一次请求的上下文/提示词/输出空间统计（进程级）
_LAST_BUDGET: dict = {}


def last_llm_call() -> dict:
    """返回最近一次写作 LLM HTTP 调用摘要（含上下文与提示词占用）。

    返回:
        `{at, url, status, elapsed_ms, ok, finish_reason, tool_calls, snippet, error,
        context_limit, prompt_tokens, max_tokens, task}`；尚未调用时为空 dict。
    """
    with _LAST_CALL_LOCK:
        result = dict(_LAST_CALL)
        result.update(_LAST_BUDGET)
        return result


def _record_budget_stats(**fields) -> None:
    """覆盖写入最近一次请求的上下文/提示词/输出空间统计（进程内）。"""
    with _LAST_CALL_LOCK:
        _LAST_BUDGET.clear()
        _LAST_BUDGET.update(fields)


def _record_llm_call(**fields) -> None:
    """覆盖写入最近一次调用摘要（进程内）。"""
    with _LAST_CALL_LOCK:
        _LAST_CALL.clear()
        _LAST_CALL.update(fields)


def _call_snippet(data: dict | None, limit: int = 300) -> tuple[str, str, int]:
    """从非流式响应里提取正文/工具参数片段、finish_reason 与 tool_calls 数量。"""
    if not isinstance(data, dict):
        return "", "", 0
    choices = data.get("choices") or [{}]
    choice = choices[0] if isinstance(choices[0], dict) else {}
    message = choice.get("message") or {}
    pieces = [str(message.get("content") or "")]
    calls = message.get("tool_calls") or []
    for call in calls:
        pieces.append(str(((call or {}).get("function") or {}).get("arguments") or ""))
    text = "".join(piece for piece in pieces if piece).strip()
    return text[:limit], str(choice.get("finish_reason") or ""), len(calls)

# 一次非流式请求的默认超时（秒）；长文任务已改走流式，这里只覆盖短调用
DEFAULT_TIMEOUT_S = 300.0
# 连接超时（秒）；建立 TCP 连接的最长等待，用于快速判断服务是否可达
CONNECT_TIMEOUT_S = 5.0
#: 首字超时（秒）：从发出请求到收到 SSE 第一个内容分片的最长等待
FIRST_TOKEN_TIMEOUT_S = 60.0
#: 流式空闲超时（秒）：两个内容分片之间的最长间隔
STREAM_IDLE_TIMEOUT_S = 60.0
# 检测到的上下文上限缓存：`(endpoint, model) -> tokens`，供请求体预算校验复用，避免每次请求再发 HTTP
_LIMIT_CACHE: dict[tuple[str, str], int] = {}
#: 各 OpenAI 兼容实现可能使用的候选字段名（上下文 / 最大输出）
_CONTEXT_FIELDS = ("max_model_len", "context_length", "context_window", "model_context_length", "max_context_window", "n_ctx")
_OUTPUT_FIELDS = ("max_output_tokens", "max_completion_tokens", "max_tokens", "max_new_tokens")
#: 思考强度合法档位（与设置页下拉一致）
_THINKING_EFFORTS = ("low", "medium", "high")
#: 思考内容进日志时的截断长度（完整思考仍由 oplog 的 response.reasoning 单独保存）
_REASONING_LOG_CHARS = 300
#: 需要压制退化重复的正文类任务（结构化 JSON 任务不下发，避免破坏格式）
_REPETITION_TASKS = ("chapter", "chapter_compress", "chapter_supplement")
#: 反重复采样参数的温和档默认值：调研 6.2 指出 vLLM 的 `repetition_penalty`
#: 会把提示词里的字一起罚，强度必须 ≤1.05；这里统一取 1.05，
#: 配置 `omlx.repetition_penalty` 可覆盖，实际下发值写日志。
DEFAULT_REPETITION_PENALTY = 1.05
#: 温和档上限：超过该值的 `repetition_penalty` 会破坏正常行文（调研 1.4 / 6.2）
MAX_REPETITION_PENALTY = 1.05
#: llama.cpp 系 DRY 的温和档默认值（调研 6.2）
DRY_DEFAULTS = {
    "dry_multiplier": 0.8,
    "dry_base": 1.75,
    "dry_allowed_length": 2,
    "dry_penalty_last_n": 512,
}
#: 采样能力探测缓存：endpoint -> {"fields": set[str], "family": str, "checked": bool}
_SAMPLING_CACHE: dict[str, dict] = {}
#: 探测 `/openapi.json` 时关注的采样字段名
_SAMPLING_FIELDS = (
    "repetition_penalty",
    "presence_penalty",
    "frequency_penalty",
    "repetition_context_size",
    "dry_multiplier",
    "dry_base",
    "dry_allowed_length",
    "dry_penalty_last_n",
    "xtc_probability",
    "min_p",
)


def _to_float(value) -> float | None:
    """把配置值转成浮点数，空值与非法值返回 None。

    参数:
        value: 配置里的原始值（可能是字符串、数字或 None）。

    返回:
        浮点数；无法解析时返回 None（调用方按「未配置」处理）。
    """
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def probe_sampling_support(endpoint: str, api_key: str = "", timeout_s: float = 5.0) -> dict:
    """探测本机 OpenAI 兼容服务支持哪些采样字段（读 `/openapi.json`，结果按 endpoint 缓存）。

    只做字段名匹配，不实际发采样请求：调研 6.5 建议的「字段探测」在服务端有
    schema 时可用；探测失败时返回空字段集，调用方按兼容模式处理。

    参数:
        endpoint: 服务地址（不含 /v1）。
        api_key: Bearer 鉴权。
        timeout_s: 探测超时（秒）。

    返回:
        `{"fields": set[str], "family": str, "checked": bool}`；
        family 为 llama.cpp / vllm / mlx / unknown。
    """
    key = (endpoint or "").rstrip("/")
    if not key:
        return {"fields": set(), "family": "unknown", "checked": False}
    cached = _SAMPLING_CACHE.get(key)
    if cached is not None:
        return cached
    info: dict = {"fields": set(), "family": "unknown", "checked": False}
    try:
        request = urllib.request.Request(
            f"{key}/openapi.json",
            headers={"Accept": "application/json"},
        )
        if api_key:
            request.add_header("Authorization", f"Bearer {api_key}")
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            text = response.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001 - 探测失败不能影响生成
        logger.info("采样能力探测失败（%s）：%s；按兼容模式处理", key, exc)
        _SAMPLING_CACHE[key] = info
        return info
    lowered = text.lower()
    info["fields"] = {name for name in _SAMPLING_FIELDS if name in lowered}
    if "dry_multiplier" in info["fields"] or "llama" in lowered:
        info["family"] = "llama.cpp"
    elif "vllm" in lowered or "repetition_detection" in lowered:
        info["family"] = "vllm"
    elif "mlx" in lowered:
        info["family"] = "mlx"
    info["checked"] = True
    _SAMPLING_CACHE[key] = info
    logger.info(
        "采样能力探测：endpoint=%s family=%s fields=%s",
        key,
        info["family"],
        sorted(info["fields"]),
    )
    return info


def _split_delta(delta: dict) -> tuple[str, str]:
    """拆分一个流式增量里的正文与思考内容。

    参数:
        delta: SSE `choices[0].delta`。

    返回:
        `(content, reasoning)`；两者互不回落，content 为空时就是空串，
        避免把思考内容当成正文累加。
    """
    content = delta.get("content")
    reasoning = delta.get("reasoning_content")
    return (str(content) if content else "", str(reasoning) if reasoning else "")


def _log_reasoning(task: str, reasoning: str) -> None:
    """把思考内容单独记到日志（截断），绝不进入正文。

    参数:
        task: 请求任务名，便于在日志里区分章节 / 大纲等用途。
        reasoning: 模型返回的完整思考文本。

    返回:
        无。
    """
    if not reasoning:
        return
    head = reasoning[:_REASONING_LOG_CHARS].replace("\n", " ")
    logger.info("模型思考（仅日志，不入正文）task=%s chars=%s head=%s", task, len(reasoning), head)


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
