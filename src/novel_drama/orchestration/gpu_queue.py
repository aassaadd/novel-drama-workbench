"""串行 GPU 队列：单卡同一时刻只跑一个任务，并与 ComfyUI 共用同一把锁。

锁文件协议：
    内容为 {"pid": 进程号, "job_id": 任务 ID, "started_at": 时间}；
    通过 O_CREAT|O_EXCL 原子创建；已存在时判断持锁进程是否存活，
    存活则等待，僵死或内容无效（空文件、没有 pid）则记录一行说明并接管，
    避免断电、touch 出来的空文件导致永久卡死或空等满 timeout。
"""

from __future__ import annotations

import json
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from ..adapters import Adapters
from ..adapters.base import AdapterError, ErrorCode
from ..models.ids import now_iso
from ..store import ProjectStore, QueueStore
from .jobs import execute_job, mark_success, record_failure


def _warn(message: str) -> None:
    """把锁相关的异常情况写一行日志到 stderr（不静默）。

    参数:
        message: 说明文本。
    """
    print(f"[gpu.lock] {message}", file=sys.stderr, flush=True)


class GpuLock:
    """基于锁文件的跨进程互斥（同时覆盖 ComfyUI 的直接调用）。"""

    def __init__(self, path: str | Path, poll_interval_s: float = 2.0, timeout_s: float = 3600.0):
        """绑定锁文件。

        参数:
            path: 锁文件绝对路径（默认与 ComfyUI 共用，如 /Volumes/other/gpu.lock）。
            poll_interval_s: 抢锁轮询间隔（秒）。
            timeout_s: 最长等待（秒），超时抛 TIMEOUT。
        """
        self.path = Path(path)
        self.poll_interval_s = poll_interval_s
        self.timeout_s = timeout_s

    def _write_lock(self, job_id: str) -> bool:
        """原子创建锁文件。

        参数:
            job_id: 当前任务 ID。

        返回:
            是否创建成功。
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            return False
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"pid": os.getpid(), "job_id": job_id, "started_at": now_iso()}, handle)
        return True

    def _holder_alive(self) -> bool:
        """判断当前持锁进程是否仍存活。

        判定规则（按顺序）：
        1. 文件不存在 → 无锁；
        2. 文件刚被创建（2 秒内）但还没写入内容 → 视为「正在写入」，保守等待；
        3. 内容无法解析、为空、或缺 pid → **按无效锁处理，视为无锁**并记一行日志；
        4. 有 pid 且进程存活 → 有锁；进程不存在 → 僵死锁，可接管。

        返回:
            True 表示锁仍有效（需等待）；False 表示可接管。
        """
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return False
        except OSError as exc:
            _warn(f"读取锁文件失败，按无锁处理：{self.path}（{exc}）")
            return False

        try:
            data = json.loads(raw or "{}")
            pid = int(data.get("pid") or 0)
        except (json.JSONDecodeError, ValueError):
            if self._just_created():
                return True
            _warn(f"锁内容无法解析（{len(raw)} 字节：{raw.strip()[:60]!r}），按无效锁处理并接管：{self.path}")
            return False
        if pid <= 0:
            if self._just_created():
                return True
            _warn(f"锁内没有有效 pid（内容 {raw.strip()[:60]!r}），按无效锁处理并接管：{self.path}")
            return False
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            _warn(f"锁的持锁进程 {pid} 已不存在，按僵死锁处理并接管：{self.path}")
            return False
        except PermissionError:
            return True

    def _just_created(self, window_s: float = 2.0) -> bool:
        """判断锁文件是否刚刚创建（写入内容的窗口期）。

        参数:
            window_s: 判定窗口（秒）。

        返回:
            True 表示可能是别的进程刚创建、还没来得及写内容。
        """
        try:
            return (time.time() - self.path.stat().st_mtime) < window_s
        except OSError:
            return False

    def acquire(self, job_id: str = "") -> None:
        """阻塞抢锁，直到成功或超时。

        参数:
            job_id: 当前任务 ID，写入锁内容便于排查。

        异常:
            AdapterError(TIMEOUT): 超过 ``timeout_s`` 仍无法获得锁。
        """
        deadline = time.monotonic() + self.timeout_s
        while True:
            if self._write_lock(job_id):
                return
            if not self._holder_alive():
                try:  # 僵死锁：接管
                    self.path.unlink()
                except FileNotFoundError:
                    pass
                continue
            if time.monotonic() >= deadline:
                raise AdapterError(ErrorCode.TIMEOUT, f"等待 GPU 锁超时: {self.path}", retryable=True)
            time.sleep(self.poll_interval_s)

    def release(self) -> None:
        """释放锁（仅当锁内容里的 pid 是自己）。"""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8") or "{}")
            if int(data.get("pid") or 0) == os.getpid():
                self.path.unlink()
        except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
            return

    @contextmanager
    def hold(self, job_id: str = "") -> Iterator[None]:
        """以 with 语法持有锁。"""
        self.acquire(job_id)
        try:
            yield
        finally:
            self.release()


class GpuQueue:
    """串行任务队列：取任务 → 抢锁 → 执行 → 落盘结果。"""

    def __init__(self, store: ProjectStore, adapters: Adapters, lock_path: str | Path, poll_interval_s: float = 2.0):
        """绑定工程、适配器与锁。

        参数:
            store: 工程读写入口。
            adapters: 已装配适配器。
            lock_path: 与 ComfyUI 共用的锁文件路径。
            poll_interval_s: 队列轮询间隔（秒）。
        """
        self.store = store
        self.queue = QueueStore(store)
        self.adapters = adapters
        self.lock = GpuLock(lock_path, poll_interval_s=poll_interval_s)
        self.poll_interval_s = poll_interval_s

    def enqueue(self, job: dict) -> tuple[dict, bool]:
        """入队（同幂等键去重）。

        参数:
            job: 渲染任务 dict。

        返回:
            (任务, 是否新增)。
        """
        job.setdefault("logs_path", f".ndw/logs/{job['id']}.log")
        return self.queue.append(job)

    def run_once(self) -> dict | None:
        """取出并执行一条排队任务。

        返回:
            已执行的任务 dict；队列为空时返回 None。
        """
        jobs = self.queue.load()
        pending = next((job for job in jobs if job.get("status") == "queued"), None)
        if pending is None:
            return None

        pending["status"] = "running"
        pending.setdefault("timings", {})["started_at"] = now_iso()
        self.queue.update(pending)

        try:
            with self.lock.hold(pending["id"]):
                artifacts = execute_job(pending, self.store, self.adapters)
            pending = mark_success(pending, artifacts)
        except AdapterError as exc:
            pending = record_failure(pending, exc)
        except Exception as exc:  # 未预期异常也落盘，避免任务永远停在 running
            pending = record_failure(pending, AdapterError(ErrorCode.UPSTREAM_ERROR, f"执行异常: {exc}", retryable=False))
        self.queue.update(pending)
        return pending

    def run(self, once: bool = False) -> int:
        """消费队列。

        参数:
            once: 为 True 时只执行一条任务（用于脚本/测试）。

        返回:
            本次处理的任务数。
        """
        self.queue.mark_interrupted()
        processed = 0
        while True:
            job = self.run_once()
            if job is None:
                if once or processed == 0:
                    return processed
                time.sleep(self.poll_interval_s)
                if self.queue.load() and any(item.get("status") == "queued" for item in self.queue.load()):
                    continue
                return processed
            processed += 1
            if once:
                return processed
