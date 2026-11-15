"""LLM 调用取消令牌。

前端每次模型调用生成一个 `call_id`，通过 `X-LLM-Call-Id` 请求头带入；
总线在分片之间调用 `ensure_active`，命中取消则抛 `LlmCancelled`。
取消是正常结果，不是错误。

注册表按 LRU 保留最近若干 call_id，避免进程内无限增长；即使调用已经结束，
再次取消也只会幂等返回 False，不影响任何结果。
"""

from __future__ import annotations

import threading
import uuid
from collections import OrderedDict
from contextvars import ContextVar

#: 当前请求的 LLM call_id；HTTP 中间件按请求头写入，SSE 工作线程显式继承
_CURRENT: ContextVar[str] = ContextVar("novel_drama_llm_call_id", default="")
#: 最多保留的已登记调用数；超过后淘汰最久未使用的条目
_MAX_CALLS = 1024
_lock = threading.Lock()
_tokens: "OrderedDict[str, threading.Event]" = OrderedDict()


class LlmCancelled(Exception):
    """用户取消了一次 LLM 调用；调用方应停止读取且不写产物。"""

    def __init__(self, call_id: str = "", message: str = "已取消本次模型调用"):
        """构造取消异常。

        参数:
            call_id: 被取消的调用标识。
            message: 面向用户的中文提示。
        """
        super().__init__(message)
        self.call_id = call_id
        self.message = message


def new_call_id() -> str:
    """生成一个新的调用标识。

    返回:
        形如 `llm_<32 位十六进制>` 的字符串。
    """
    return f"llm_{uuid.uuid4().hex}"


def set_current_call_id(call_id: str):
    """把 call_id 写入当前上下文。

    参数:
        call_id: 调用标识；空串表示清理。

    返回:
        ContextVar token，供 `reset_current_call_id` 还原。
    """
    return _CURRENT.set(str(call_id or ""))


def reset_current_call_id(token) -> None:
    """还原上下文里的 call_id。

    参数:
        token: `set_current_call_id` 返回的 token。

    返回:
        无。
    """
    try:
        _CURRENT.reset(token)
    except (ValueError, LookupError):
        # 跨线程/异步边界还原失败时直接清理，避免把旧 id 留给下一个请求
        _CURRENT.set("")


def current_call_id() -> str:
    """读取当前上下文的 call_id。

    返回:
        call_id；没有时返回空串。
    """
    return str(_CURRENT.get() or "")


def register(call_id: str) -> threading.Event:
    """登记一个调用并返回取消令牌。

    参数:
        call_id: 调用标识；空串直接返回一个未触发的事件（不可取消）。

    返回:
        `threading.Event`；被 `cancel` 命中后置位。
    """
    if not call_id:
        return threading.Event()
    with _lock:
        event = _tokens.get(call_id)
        if event is None:
            event = threading.Event()
            _tokens[call_id] = event
            while len(_tokens) > _MAX_CALLS:
                _tokens.popitem(last=False)
        else:
            _tokens.move_to_end(call_id)
        return event


def cancel(call_id: str) -> bool:
    """触发取消；未登记或已结束时返回 False。

    参数:
        call_id: 调用标识。

    返回:
        是否命中了本次调用。
    """
    if not call_id:
        return False
    with _lock:
        event = _tokens.get(call_id)
        if event is None:
            return False
        event.set()
        _tokens.move_to_end(call_id)
        return True


def is_cancelled(call_id: str) -> bool:
    """判断调用是否已被取消。

    参数:
        call_id: 调用标识。

    返回:
        已取消返回 True；未登记或未取消返回 False。
    """
    if not call_id:
        return False
    with _lock:
        event = _tokens.get(call_id)
        return bool(event and event.is_set())


def ensure_active(call_id: str) -> None:
    """若调用已被取消则抛出 `LlmCancelled`。

    参数:
        call_id: 调用标识。

    返回:
        无。

    异常:
        LlmCancelled: 调用已被取消。
    """
    if call_id and is_cancelled(call_id):
        raise LlmCancelled(call_id)


def release(call_id: str) -> None:
    """释放调用登记（调用正常结束时可调用）。

    参数:
        call_id: 调用标识。

    返回:
        无。
    """
    if not call_id:
        return
    with _lock:
        _tokens.pop(call_id, None)
