"""LLM 调用取消令牌与父媒体任务关联。

前端每次模型调用生成一个 `call_id`，通过 `X-LLM-Call-Id` 请求头带入；
总线在分片之间调用 `ensure_active`，命中取消则抛 `LlmCancelled`。
取消是正常结果，不是错误。

媒体任务内嵌的 LLM 调用额外登记「父任务」：媒体总线在执行器线程里
`begin_media_job` 绑定当前任务 ID，`LlmBus` 构造时把 call_id 登记到该父任务下；
取消父媒体任务会级联取消这些子调用，让模型在下一个分片检查时退出。

注册表按 LRU 保留最近若干 call_id，避免进程内无限增长；即使调用已经结束，
再次取消也只会幂等返回 False，不影响任何结果。
"""

from __future__ import annotations

import threading
import uuid
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar

from ...adapters.observability.oplog import PARENT_JOB as _PARENT_JOB

#: 当前请求的 LLM call_id；HTTP 中间件按请求头写入，SSE 工作线程显式继承
_CURRENT: ContextVar[str] = ContextVar("novel_drama_llm_call_id", default="")
#: 最多保留的已登记调用数；超过后淘汰最久未使用的条目
_MAX_CALLS = 1024
#: 每个父任务最多登记的子调用数（超出淘汰最早登记），以及最多保留的父任务数
_MAX_CHILDREN = 512
_MAX_PARENTS = 256
_lock = threading.Lock()
_tokens: "OrderedDict[str, threading.Event]" = OrderedDict()
#: 父任务 → 其下仍在进行的子调用 ID（按登记顺序，用于上限淘汰）
_children: "OrderedDict[str, OrderedDict[str, None]]" = OrderedDict()
#: 父任务 → 本次执行的调用计数器（由 `begin_media_job` 创建）
_counters: dict[str, "_ChildCounter"] = {}


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


class _ChildCounter:
    """一次媒体任务执行期间登记的 LLM 子调用计数器。

    属性:
        count: 已登记的调用次数（同一 call_id 重新登记会再次计数）。
    """

    def __init__(self) -> None:
        """初始化计数为 0。"""
        self.count = 0


def bind_parent_job(job_id: str):
    """把当前媒体任务 ID 写入上下文（供子调用登记使用）。

    参数:
        job_id: 媒体任务 ID；空串表示不关联父任务。

    返回:
        ContextVar token，供 `reset_parent_job` 还原。
    """
    return _PARENT_JOB.set(str(job_id or ""))


def reset_parent_job(token) -> None:
    """还原上下文里的父媒体任务 ID。

    参数:
        token: `bind_parent_job` 返回的 token。

    返回:
        无。
    """
    try:
        _PARENT_JOB.reset(token)
    except (ValueError, LookupError):
        # 跨线程/异步边界还原失败时直接清空，避免把旧任务 ID 留给下一个任务
        _PARENT_JOB.set("")


def current_parent_job() -> str:
    """读取当前上下文的父媒体任务 ID。

    返回:
        任务 ID；不在媒体任务内执行时返回空串。
    """
    return str(_PARENT_JOB.get() or "")


def register_child(parent_job_id: str, call_id: str) -> None:
    """把一个 LLM 调用登记到父媒体任务下（重复登记只计一次）。

    参数:
        parent_job_id: 父媒体任务 ID。
        call_id: 子调用标识。

    返回:
        无。
    """
    parent = str(parent_job_id or "")
    call = str(call_id or "")
    if not parent or not call:
        return
    with _lock:
        bucket = _children.get(parent)
        if bucket is None:
            bucket = OrderedDict()
            _children[parent] = bucket
        else:
            _children.move_to_end(parent)
        if call in bucket:
            bucket.move_to_end(call)
        else:
            bucket[call] = None
            counter = _counters.get(parent)
            if counter is not None:
                counter.count += 1
        while len(bucket) > _MAX_CHILDREN:
            bucket.popitem(last=False)
        while len(_children) > _MAX_PARENTS:
            _children.popitem(last=False)


def release_child(parent_job_id: str, call_id: str) -> None:
    """移除一个子调用登记（调用结束或异常时调用，可重复调用）。

    参数:
        parent_job_id: 父媒体任务 ID。
        call_id: 子调用标识。

    返回:
        无。
    """
    parent = str(parent_job_id or "")
    call = str(call_id or "")
    if not parent or not call:
        return
    with _lock:
        bucket = _children.get(parent)
        if not bucket:
            return
        bucket.pop(call, None)


def cancel_children(parent_job_id: str) -> int:
    """取消父媒体任务下正在进行的全部 LLM 子调用。

    参数:
        parent_job_id: 父媒体任务 ID。

    返回:
        实际命中的子调用数量；父任务没有子调用时返回 0。
    """
    parent = str(parent_job_id or "")
    if not parent:
        return 0
    with _lock:
        bucket = _children.get(parent)
        if not bucket:
            return 0
        calls = list(bucket)
        _children.move_to_end(parent)
    return sum(1 for call_id in calls if cancel(call_id))


@contextmanager
def begin_media_job(job_id: str):
    """绑定一次媒体任务执行：写入父任务并在退出时清理子调用登记。

    参数:
        job_id: 媒体任务 ID。

    返回:
        上下文管理器；`with ... as counter` 的 `counter.count` 为本次登记的
        LLM 调用次数。

    用法:
        `with begin_media_job(job_id) as counter: ...; job.set_meta(llm_calls=counter.count)`
    """
    parent = str(job_id or "")
    counter = _ChildCounter()
    token = bind_parent_job(parent)
    if parent:
        with _lock:
            _counters[parent] = counter
    try:
        yield counter
    finally:
        if parent:
            with _lock:
                _counters.pop(parent, None)
                _children.pop(parent, None)
        reset_parent_job(token)

