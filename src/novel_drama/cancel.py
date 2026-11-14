"""生成任务的协作式取消。

页面任务队列点「停止」时先在这里登记取消请求；生成线程在每次模型增量 /
进度上报时检查该标志并抛出 `GenerationCanceled`，从而在落盘前中止。

`GenerationCanceled` 继承 `BaseException`（与 `asyncio.CancelledError` 类似），
确保各生成入口里「单批/单章失败不影响其它项」的 `except Exception` 不会把它
吞掉继续往下跑；生成线程里显式捕获它即可。

注册表只保存在 Web 进程内：查不到登记的任务说明后端已无对应生成线程，
调用方应把它当作僵尸任务强制标记为已取消。
"""

from __future__ import annotations

import threading

#: 取消标志表：任务 ID → 取消事件
_FLAGS: dict[str, threading.Event] = {}
#: 保护标志表的进程内锁
_LOCK = threading.Lock()


class GenerationCanceled(BaseException):
    """用户主动停止生成：向上冒泡到生成线程，阻止继续写入产物。"""


def register(job_id: str) -> threading.Event:
    """登记一条运行中的任务并返回它的取消事件。

    参数:
        job_id: 任务 ID。

    返回:
        该任务的取消事件（已存在时复用）。
    """
    with _LOCK:
        return _FLAGS.setdefault(job_id, threading.Event())


def release(job_id: str) -> None:
    """任务结束后移除登记（幂等）。

    参数:
        job_id: 任务 ID。

    返回:
        无。
    """
    with _LOCK:
        _FLAGS.pop(job_id, None)


def request(job_id: str) -> bool:
    """请求停止任务。

    参数:
        job_id: 任务 ID。

    返回:
        True 表示本进程确实有这条运行中的任务；False 表示没有登记（僵尸任务）。
    """
    with _LOCK:
        event = _FLAGS.get(job_id)
    if event is None:
        return False
    event.set()
    return True


def is_requested(job_id: str) -> bool:
    """任务是否已被请求停止。

    参数:
        job_id: 任务 ID。

    返回:
        True 表示用户已点过停止。
    """
    with _LOCK:
        event = _FLAGS.get(job_id)
    return bool(event and event.is_set())


def is_active(job_id: str) -> bool:
    """本进程是否仍有该任务的生成线程登记。

    参数:
        job_id: 任务 ID。

    返回:
        True 表示任务在本进程内运行中。
    """
    with _LOCK:
        return job_id in _FLAGS
