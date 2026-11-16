"""LLM 调用采集：给写作适配器的 HTTP 入口装钩子，把每次调用记进操作日志。

覆盖范围：`_request_json`（generate / generate_tool 非流式）与
`_iter_sse_with_retry`（generate / generate_stream / generate_tool 流式 / stream），
记录的请求体就是真正发出去的那一份（含 tools、chat_template_kwargs 等）。

同时保存「最近一次调用摘要」的进程内快照（`last_llm_call`），供设置页与
任务页自查；写入仍复用 `oplog` 的队列与脱敏，不重复落盘。
"""

from __future__ import annotations

import contextvars
import functools
import json
import socket
import threading
import time
import traceback
from collections import deque
from typing import Any

from .oplog import PARENT_JOB, _CFG, KIND_LLM, _clip, _enqueue, _new_id, _now, current_trace

#: LLM 调用/流中断的原因码与中文标签（日志与页面共用同一份口径）。
#: 补这些码的目的：把「调用方提前关闭流」这一句笼统文案拆成可统计的来源。
LLM_REASON_LABELS = {
    "natural_end": "正常结束（上游已给 finish_reason，消费方在 done 后关闭迭代器）",
    "user_cancel": "用户点了停止",
    "page_unload": "页面离开或刷新导致断线",
    "sse_reconnect": "SSE 预连接失败后重试",
    "degeneration": "退化熔断中止本轮（或由此触发的续写/重写）",
    "watchdog": "看门狗截停（或由此触发的续写）",
    "reasoning_loop": "思考通道检测到重复循环，提前中止本次调用",
    "empty_body_retry": "正文为空（思考占满额度），关闭思考重试一次",
    "thinking_exhausted": "思考占满额度且重试仍无正文，本轮未写入",
    "content_retry": "内容校验未通过后的重试",
    "superseded": "被新请求取代",
    "timeout": "HTTP 超时",
    "unknown": "未标记的调用方关闭",
}
#: 当前流被中断的原因（由消费方在关闭迭代器前设置，同线程传递）
_ABORT_REASON: contextvars.ContextVar = contextvars.ContextVar("ndw_llm_abort_reason", default=None)
#: 本次调用的来因（如内容校验重试 / 退化续写），在发起请求前设置，只作用于下一次调用
_CALL_REASON: contextvars.ContextVar = contextvars.ContextVar("ndw_llm_call_reason", default=None)


def set_llm_abort_reason(code: str, site: str = "") -> None:
    """标记「当前这次流」被中断的原因码与调用点。

    消费方在主动关闭流迭代器（取消、熔断、页面断开）之前调用；日志钩子读取后
    写进记录的 `reason` 字段。只在同一线程内生效。

    参数:
        code: 原因码，取值见 `LLM_REASON_LABELS`。
        site: 触发调用点（如 `stream_api: on_delta`）。

    返回:
        无。
    """
    _ABORT_REASON.set({"code": str(code or ""), "site": str(site or "")})


def clear_llm_abort_reason() -> None:
    """清空中断原因标记（同一线程内新发起调用前可调用，避免串味）。

    返回:
        无。
    """
    _ABORT_REASON.set(None)


def set_llm_call_reason(code: str, site: str = "") -> None:
    """标记「下一次 LLM 调用为什么发起」（重试 / 续写 / 重写）。

    参数:
        code: 原因码，取值见 `LLM_REASON_LABELS`。
        site: 发起调用点。

    返回:
        无。
    """
    _CALL_REASON.set({"code": str(code or ""), "site": str(site or "")})


#: 流式事件最多保留条数（首尾各保留一半，中间丢弃并记录总数）
_MAX_SSE_EVENTS = 300


# ---------------------------------------------------------------- LLM 采集


class _LlmCall:
    """一次 LLM 调用的采集器：请求体、流式事件、耗时与错误。"""

    def __init__(self, adapter: Any, payload: dict, stream: bool):
        """初始化采集器。

        参数:
            adapter: 写作适配器实例（取 endpoint / model）。
            payload: 真正发出的请求体。
            stream: 是否流式。
        """
        self.adapter = adapter
        self.payload = payload or {}
        self.stream = stream
        self.started = time.monotonic()
        self.ts = _now()
        self.parts: list[str] = []
        self.reasoning: list[str] = []
        #: 原始 SSE 事件：首段与最新段各保留一半，避免长流把单条记录撑爆
        self.events_head: list[dict] = []
        self.events_tail: deque = deque(maxlen=_MAX_SSE_EVENTS // 2)
        self.events_total = 0
        self.usage: dict | None = None
        self.finish_reason = ""
        self.model = str(self.payload.get("model") or getattr(adapter, "model", "") or "")
        self.first_token_ms: int | None = None
        self.status_code: int | None = None
        self.tool_args: list[str] = []
        #: 非流式响应原文（由 `_request_json` 钩子写入）
        self._data: dict = {}
        #: 本次调用的来因（内容校验重试 / 退化续写等）；只作用于当前这一次调用
        self.call_reason: dict | None = _CALL_REASON.get()
        _CALL_REASON.set(None)

    def feed(self, event: dict) -> None:
        """吞掉一条 SSE 事件并累积文本 / 用量 / 工具参数。

        参数:
            event: 解析后的 SSE JSON。

        返回:
            无。
        """
        self.events_total += 1
        if len(self.events_head) < _MAX_SSE_EVENTS // 2:
            self.events_head.append(event)
        else:
            self.events_tail.append(event)
        if event.get("model"):
            self.model = str(event["model"])
        if isinstance(event.get("usage"), dict):
            self.usage = event["usage"]
        choices = event.get("choices") or []
        if not choices:
            return
        choice = choices[0] if isinstance(choices[0], dict) else {}
        if choice.get("finish_reason"):
            self.finish_reason = str(choice["finish_reason"])
        delta = choice.get("delta") or {}
        text = delta.get("content") or ""
        reasoning = delta.get("reasoning_content") or ""
        if reasoning:
            self.reasoning.append(str(reasoning))
        if text:
            if self.first_token_ms is None:
                self.first_token_ms = int((time.monotonic() - self.started) * 1000)
            self.parts.append(str(text))
        for call in delta.get("tool_calls") or []:
            arguments = (call.get("function") or {}).get("arguments")
            if arguments:
                self.tool_args.append(str(arguments))

    def _base_detail(self) -> dict:
        """组装请求侧详情（模型、参数、完整提示词、原始请求体）。

        返回:
            detail dict。
        """
        messages = self.payload.get("messages") or []
        system = ""
        dialog: list[dict] = []
        for item in messages if isinstance(messages, list) else []:
            if not isinstance(item, dict):
                continue
            if item.get("role") == "system" and not system:
                system = str(item.get("content") or "")
            else:
                dialog.append({"role": item.get("role"), "content": item.get("content")})
        params = {
            key: value
            for key, value in self.payload.items()
            if key not in ("messages", "tools", "model")
        }
        tools = self.payload.get("tools")
        return {
            "http": {
                "method": "POST",
                "path": "/v1/chat/completions",
                "url": f"{getattr(self.adapter, 'endpoint', '')}/v1/chat/completions",
                "status": self.status_code,
            },
            "model": self.model,
            "stream": bool(self.stream),
            "params": params,
            "system": system,
            "messages": dialog,
            "tools": tools or [],
            "payload": self.payload,
        }

    def _response_detail(self, data: dict, started: float) -> dict:
        """组装响应侧详情（完整文本、用量、结束原因、工具参数）。

        参数:
            data: 非流式响应（流式时为空 dict）。
            started: 开始时间（monotonic）。

        返回:
            response 段 dict。
        """
        if self.stream:
            text = "".join(self.parts)
            reasoning = "".join(self.reasoning)
            tool_arguments = "".join(self.tool_args)
        else:
            choices = data.get("choices") or []
            message = (choices[0].get("message") if choices else {}) or {}
            text = str(message.get("content") or "")
            reasoning = str(message.get("reasoning_content") or "")
            tool_calls = message.get("tool_calls") or []
            tool_arguments = json.dumps(
                [(call.get("function") or {}).get("arguments") for call in tool_calls if isinstance(call, dict)],
                ensure_ascii=False,
            ) if tool_calls else ""
            if not self.finish_reason and choices:
                self.finish_reason = str(choices[0].get("finish_reason") or "")
            self.usage = data.get("usage") or self.usage
        return {
            "text": text,
            "reasoning": reasoning,
            "tool_arguments": tool_arguments,
            "finish_reason": self.finish_reason,
            "usage": self.usage or {},
            "usage_note": "" if self.usage else "上游未返回 token 用量",
            "duration_ms": int((time.monotonic() - started) * 1000),
            "first_token_ms": self.first_token_ms,
            "raw": data if not self.stream else {
                "events_total": self.events_total,
                "events": self.events_head + list(self.events_tail) if self.events_total > len(self.events_head) else self.events_head,
                "events_note": f"共 {self.events_total} 条事件；超限时只保留首段与最新事件",
            },
        }

    def finish(self, status: str, error: str = "", trace_text: str = "", reason: str = "", site: str = "") -> None:
        """写出一条 LLM 调用记录。

        参数:
            status: `ok` / `error` / `partial`。
            error: 失败原因或备注。
            trace_text: 异常堆栈（有则一并落盘）。
            reason: 原因码（流中断或本次调用来因），取值见 `LLM_REASON_LABELS`。
            site: 触发该原因的调用点。

        返回:
            无。
        """
        duration_ms = int((time.monotonic() - self.started) * 1000)
        detail = self._base_detail()
        if self.stream:
            detail["response"] = self._response_detail({}, self.started)
        else:
            detail["response"] = self._response_detail(self._data, self.started)
        reason_info: dict = {}
        code = str(reason or (self.call_reason or {}).get("code") or "")
        if code:
            reason_info = {
                "code": code,
                "label": LLM_REASON_LABELS.get(code, code),
                "site": str(site or (self.call_reason or {}).get("site") or ""),
            }
            detail["reason"] = reason_info
        if self.first_token_ms is not None:
            detail["steps"] = [{"at": f"+{self.first_token_ms}ms", "label": "收到首个内容分片", "detail": ""}]
        if trace_text:
            detail["error"] = {"message": error, "traceback": trace_text}
        ctx = current_trace()
        detail["trigger"] = ctx.get("action") or ""
        system_hint = str(detail.get("system") or "").strip().replace("\n", " ")[:60]
        prompt_hint = ""
        if detail.get("messages"):
            prompt_hint = str(detail["messages"][-1].get("content") or "").strip().replace("\n", " ")[:60]
        record = {
            "id": _new_id("lg"),
            "ts": self.ts,
            "kind": KIND_LLM,
            "action": "llm.chat",
            "label": "LLM 调用",
            "trace_id": ctx.get("trace_id") or "",
            # 媒体任务内嵌的模型调用登记父任务，便于按父任务排查与级联取消
            "parent_job_id": PARENT_JOB.get() or None,
            "project": ctx.get("project") or "",
            "target": f"{self.model or '未指定模型'}" + (f" · {ctx.get('action')}" if ctx.get("action") else ""),
            "summary": f"{'流式' if self.stream else '非流式'} · 提示 {system_hint or prompt_hint}",
            "status": status,
            "duration_ms": duration_ms,
            "error": _clip(error or "", 4000),
            "reason": reason_info,
            "detail": detail,
        }
        _enqueue(record)


def _begin_llm(adapter: Any, payload: dict, stream: bool) -> "_LlmCall":
    """创建采集器（供钩子调用）。

    参数:
        adapter: 适配器实例。
        payload: 请求体。
        stream: 是否流式。

    返回:
        `_LlmCall`。
    """
    return _LlmCall(adapter, payload, stream)


def _trace_text(exc: BaseException) -> str:
    """取异常堆栈文本。

    参数:
        exc: 异常对象。

    返回:
        堆栈字符串（含异常类型与信息）。
    """
    try:
        return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    except Exception:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"


def _error_status(exc: BaseException) -> int | None:
    """从适配器错误里取上游 HTTP 状态码。

    参数:
        exc: 异常对象。

    返回:
        状态码；取不到时为 None。
    """
    detail = getattr(exc, "detail", None)
    if isinstance(detail, dict):
        value = detail.get("status")
        if isinstance(value, int):
            return value
    return None


def _is_timeout(exc: BaseException) -> bool:
    """判断异常是否为 HTTP / socket 超时（用于打 `timeout` 原因码）。

    参数:
        exc: 捕获到的异常。

    返回:
        是超时返回 True。
    """
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return True
    text = str(exc).lower()
    return "timed out" in text or "timeout" in text


def _error_reason_code(exc: BaseException) -> str:
    """从适配器异常里取主动中止的原因码（`AdapterError.detail.code`）。

    适配器主动中止（例如思考通道检测到重复循环）时会把原因码放进 detail，
    这里把它映射成日志里的 `reason.code`，便于统计「谁把这次调用停掉的」。

    参数:
        exc: 捕获到的异常。

    返回:
        已知原因码；没有或不在清单里时返回空串。
    """
    detail = getattr(exc, "detail", None)
    if not isinstance(detail, dict):
        return ""
    code = str(detail.get("code") or "")
    return code if code in LLM_REASON_LABELS else ""


def install_llm_hooks() -> None:
    """给写作适配器的两个 HTTP 入口装采集钩子（幂等）。

    覆盖范围：
    - `_request_json`：generate(stream=False) 与 generate_tool(stream=False)；
    - `_iter_sse_with_retry`：generate/generate_stream/generate_tool(stream=True)/stream。

    返回:
        无。
    """
    try:
        from ..llm import OmlxWritingAdapter
    except Exception:  # noqa: BLE001 - 适配器缺失时静默跳过
        return
    if getattr(OmlxWritingAdapter, "_ndw_log_hooked", False):
        return

    original_request_json = OmlxWritingAdapter._request_json
    original_sse = OmlxWritingAdapter._iter_sse_with_retry

    @functools.wraps(original_request_json)
    def _request_json(self, method, path, payload=None, timeout_s=None):
        """非流式请求包装：只对 chat/completions 记录日志。"""
        is_chat = str(method).upper() == "POST" and str(path) == "/v1/chat/completions" and isinstance(payload, dict)
        if not is_chat or not _CFG["enabled"]:
            return original_request_json(self, method, path, payload, timeout_s)
        call = _begin_llm(self, payload, stream=False)
        try:
            data = original_request_json(self, method, path, payload, timeout_s)
        except BaseException as exc:  # noqa: BLE001 - 记录后原样抛出
            call.status_code = _error_status(exc)
            call.finish(
                "error",
                str(exc),
                _trace_text(exc),
                reason="timeout" if _is_timeout(exc) else _error_reason_code(exc),
                site="adapter: 非流式请求异常" if _is_timeout(exc) else ("adapter: 主动中止" if _error_reason_code(exc) else ""),
            )
            raise
        call.status_code = 200
        call._data = data if isinstance(data, dict) else {}
        call.finish("ok")
        return data

    @functools.wraps(original_sse)
    def _iter_sse_with_retry(self, payload):
        """流式请求包装：累积事件并在流结束/中断/报错时记录。"""
        if not isinstance(payload, dict) or not _CFG["enabled"]:
            yield from original_sse(self, payload)
            return
        call = _begin_llm(self, payload, stream=True)
        try:
            for event in original_sse(self, payload):
                call.feed(event)
                yield event
        except GeneratorExit:
            call.status_code = 200
            # 消费方主动关闭迭代器：按「是否已有 finish_reason」区分正常结束与真中断。
            # 之前所有「收到 done 后 break」的调用都被记成「调用方提前关闭流」，
            # 是这类日志大量出现的主因。
            info = _ABORT_REASON.get() or {}
            code = str(info.get("code") or "")
            site = str(info.get("site") or "")
            if not code and call.finish_reason:
                code = "natural_end"
            if not code:
                code = "unknown"
            if code == "natural_end":
                call.finish(
                    "ok",
                    "流正常结束（上游已给 finish_reason，消费方在 done 后关闭迭代器）",
                    reason=code,
                    site=site or "adapter: done 后关闭迭代器",
                )
            else:
                label = LLM_REASON_LABELS.get(code, code)
                call.finish(
                    "partial",
                    f"流被中断：{label}" + (f"（触发点：{site}）" if site else ""),
                    reason=code,
                    site=site,
                )
            raise
        except BaseException as exc:  # noqa: BLE001 - 记录后原样抛出
            call.status_code = _error_status(exc)
            if _is_timeout(exc):
                reason, site = "timeout", "adapter: 流读取超时"
            elif bool(getattr(exc, "retryable", False)) and call.events_total == 0:
                # 还没收到任何事件就失败：适配器会自动重连，这类不是用户中断
                reason, site = "sse_reconnect", "adapter: 流建立阶段失败（随后自动重试）"
            else:
                # 适配器主动中止（如思考通道检测到重复循环）：沿用 detail.code 作为原因码
                reason = _error_reason_code(exc)
                site = "adapter: 主动中止" if reason else ""
            call.finish("error", str(exc), _trace_text(exc), reason=reason, site=site)
            raise
        else:
            call.status_code = 200
            call.finish("ok", reason="natural_end", site="adapter: SSE 迭代正常结束")

    OmlxWritingAdapter._request_json = _request_json
    OmlxWritingAdapter._iter_sse_with_retry = _iter_sse_with_retry
    OmlxWritingAdapter._ndw_log_hooked = True


# ---------------------------------------------------------------- 最近一次调用摘要

#: 最近一次写作 LLM 调用摘要（进程级），供任务页 / 设置页随时自查
_LAST_CALL: dict = {}
#: 最近一次请求的上下文 / 提示词 / 输出空间统计（进程级）
_LAST_BUDGET: dict = {}
_LAST_CALL_LOCK = threading.Lock()


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


def record_budget_stats(**fields) -> None:
    """覆盖写入最近一次请求的上下文 / 提示词 / 输出空间统计（进程内）。

    参数:
        **fields: 统计字段（context_limit、prompt_tokens、max_tokens、task 等）。

    返回:
        无。
    """
    with _LAST_CALL_LOCK:
        _LAST_BUDGET.clear()
        _LAST_BUDGET.update(fields)


def record_model_call(**fields) -> None:
    """覆盖写入最近一次写作 LLM 调用摘要（进程内）。

    参数:
        **fields: 摘要字段（at、url、status、elapsed_ms、ok、finish_reason 等）。

    返回:
        无。
    """
    with _LAST_CALL_LOCK:
        _LAST_CALL.clear()
        _LAST_CALL.update(fields)
