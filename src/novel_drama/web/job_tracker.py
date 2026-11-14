"""界面生成任务记录：`.ndw/ui_jobs.json`。

AI 生成在 Web 进程内以 SSE / 同步请求执行，不经过 GPU 队列；
为了让「任务」页能看到这些生成，这里单独记录一份轻量任务列表，
由 `/api/ui/queue` 与 GPU 队列合并展示。

记录只做展示：状态为 queued/running/succeeded/failed/canceled/interrupted，
带类型、目标章节、进度与错误原因。文件写入用进程内锁串行化，避免多个 SSE
生成同时更新时互相覆盖。Web 进程退出后，遗留的 running 任务在下次读取时
标记为 interrupted。
"""

from __future__ import annotations

import json
import threading
import time

from ..models.ids import new_id, now_iso
from ..store.project_store import ProjectStore

#: 进程内写锁：所有 UI 任务都写同一个文件
_LOCK = threading.Lock()
#: 已做过「进程重启中断修正」的工程根目录
_MARKED: set[str] = set()
#: 最多保留的历史任务数，避免文件无限增长
_MAX_JOBS = 300
#: 本进程启动时间：只把早于它的 running 任务视为上次进程遗留
_PROCESS_START = now_iso()
#: 本进程内仍在运行的任务（任务 ID → 类型），用于阻止同类后台任务重复启动
_ACTIVE: dict[str, str] = {}
_ACTIVE_LOCK = threading.Lock()


def active_jobs(kind: str = "") -> list[str]:
    """本进程内仍在运行的任务 ID。

    参数:
        kind: 只统计该类型；空串表示全部类型。

    返回:
        任务 ID 列表。
    """
    with _ACTIVE_LOCK:
        return [job_id for job_id, item_kind in _ACTIVE.items() if not kind or item_kind == kind]


def _file(store: ProjectStore):
    """UI 任务文件路径。"""
    return store.root / ".ndw" / "ui_jobs.json"


def _read(store: ProjectStore) -> list[dict]:
    """读取任务列表，文件损坏时按空列表处理。"""
    path = _file(store)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    jobs = data.get("jobs", []) if isinstance(data, dict) else []
    return jobs if isinstance(jobs, list) else []


def _write(store: ProjectStore, jobs: list[dict]) -> None:
    """覆盖写入任务列表（只保留最近若干条）。"""
    path = _file(store)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"schema_version": 1, "jobs": jobs[-_MAX_JOBS:]}, ensure_ascii=False),
        encoding="utf-8",
    )


def load_ui_jobs(store: ProjectStore) -> list[dict]:
    """读取 UI 任务；首次读取时修正上次进程遗留的运行中任务。

    参数:
        store: 工程。

    返回:
        任务列表（按创建时间升序）。
    """
    key = str(store.root)
    with _LOCK:
        jobs = _read(store)
        if key not in _MARKED:
            _MARKED.add(key)
            changed = False
            for job in jobs:
                # 只修正本进程启动前遗留的任务；刚创建的任务不能误标为中断
                if job.get("status") in ("queued", "running") and str(job.get("created_at") or "") < _PROCESS_START:
                    job["status"] = "interrupted"
                    job["error"] = "页面进程已退出，任务中断"
                    job.setdefault("timings", {})["finished_at"] = now_iso()
                    changed = True
            if changed:
                _write(store, jobs)
    return jobs


#: 已结束的任务状态（可清理）
TERMINAL_STATUSES = ("succeeded", "failed", "canceled", "interrupted")


def update_job(store: ProjectStore, job_id: str, **fields) -> dict | None:
    """按 ID 修改任务字段（供终止/兜底使用）。

    参数:
        store: 工程。
        job_id: 任务 ID。
        **fields: 要覆盖的字段（例如 status / error）。

    返回:
        修改后的任务；任务不存在时返回 None。
    """
    with _LOCK:
        jobs = _read(store)
        for item in jobs:
            if item.get("id") != job_id:
                continue
            item.update(fields)
            item.setdefault("timings", {})["finished_at"] = now_iso()
            _write(store, jobs)
            return item
    return None


def clear_finished(store: ProjectStore) -> int:
    """删除所有已结束的 UI 任务行（不影响正文、音频等产物）。

    参数:
        store: 工程。

    返回:
        删除的行数。
    """
    with _LOCK:
        jobs = _read(store)
        keep = [item for item in jobs if item.get("status") not in TERMINAL_STATUSES]
        removed = len(jobs) - len(keep)
        if removed:
            _write(store, keep)
        return removed


class UiJob:
    """一条界面生成任务的写入器（start/progress/finish/fail/cancel）。"""

    def __init__(self, store: ProjectStore, kind: str, target: dict | None = None, title: str = ""):
        """创建任务（尚未写入）。

        参数:
            store: 工程。
            kind: 任务类型（chapter / world / outline / character / script / voice / setting / produce）。
            target: 目标信息，例如 `{chapter_id, chapter_title}`。
            title: 展示标题。
        """
        self.store = store
        self.kind = kind
        self.target = target or {}
        self.title = title
        self.job: dict | None = None
        self._last_write = 0.0
        self._done = False

    def _save(self, **patch) -> None:
        """把字段补丁写回任务文件。"""
        with _LOCK:
            if not self.job:
                return
            jobs = _read(self.store)
            for index, item in enumerate(jobs):
                if item.get("id") == self.job.get("id"):
                    jobs[index] = self.job
                    break
            else:
                jobs.append(self.job)
            _write(self.store, jobs)

    def start(self, current: str = "已提交生成请求") -> "UiJob":
        """写入一条运行中任务。

        参数:
            current: 当前进度文案。

        返回:
            self，便于链式调用。
        """
        self.job = {
            "id": new_id("job"),
            "kind": self.kind,
            "title": self.title,
            "target": self.target,
            "status": "running",
            "attempts": 0,
            "artifacts": [],
            "error": None,
            "progress": {"percent": None, "current": current},
            "priority": 100,
            "idempotency_key": f"ui:{self.kind}:{new_id('key')}",
            "created_at": now_iso(),
            "timings": {"started_at": now_iso()},
        }
        self._save()
        with _ACTIVE_LOCK:
            _ACTIVE[self.job["id"]] = self.kind
        return self

    def progress(self, current: str | None = None, percent: float | None = None, force: bool = False) -> None:
        """更新进度（默认最多每秒写一次，避免高频写盘）。

        参数:
            current: 当前进度文案。
            percent: 百分比；None 表示不确定。
            force: True 时忽略节流立即写入。
        """
        if not self.job or self._done:
            return
        now = time.monotonic()
        if not force and now - self._last_write < 1.0:
            return
        self._last_write = now
        if current is not None:
            self.job["progress"]["current"] = str(current)
        if percent is not None:
            self.job["progress"]["percent"] = max(0, min(100, round(float(percent))))
        self._save()

    def set_artifacts(self, items: list) -> None:
        """写入产物列表（任务队列「产物」列直接取长度）。

        参数:
            items: 产物路径列表。
        """
        if not self.job or self._done:
            return
        rows = [str(item) for item in items][-200:]
        if rows == self.job.get("artifacts"):
            return
        self.job["artifacts"] = rows
        self._save()

    def event(self, kind: str, data: dict | None = None) -> None:
        """按生成事件更新进度文案。

        参数:
            kind: 事件名（batch/chars/item/stage/plan）。
            data: 事件负载。
        """
        data = data or {}
        if kind == "batch":
            index = _num(data.get("index"))
            total = _num(data.get("total")) or 1
            name = str(data.get("name") or data.get("title") or "").strip()
            percent = ((index - 1) / total) * 100 if total else None
            text = f"第 {int(index)}/{int(total)} 批"
            if name:
                text += f" · {name}"
            self.progress(text, percent)
        elif kind == "chars":
            chars = _num(data.get("chars"))
            if chars:
                self.progress(f"已接收 {int(chars)} 字")
        elif kind == "item":
            name = str(data.get("name") or "").strip()
            status = str(data.get("status") or "").strip()
            if name:
                self.progress(f"{name}{' · 已完成' if status == 'done' else ''}", force=status == "done")
        elif kind == "stage":
            stage = str(data.get("stage") or data.get("message") or "").strip()
            if stage:
                self.progress(stage, force=True)
        elif kind == "plan":
            total = Number(data.get("total"))
            if total:
                self.progress(f"已规划 {int(total)} 个角色", 0, force=True)

    def _retire(self) -> None:
        """任务进入终态后，从本进程运行中集合移除。"""
        if not self.job:
            return
        with _ACTIVE_LOCK:
            _ACTIVE.pop(self.job.get("id"), None)

    def close(self) -> None:
        """结束跟踪但不改任务状态（外部已把任务写成终态时使用）。"""
        self._retire()

    def finish(self, message: str = "生成完成") -> None:
        """标记成功并保留结果摘要。"""
        if not self.job or self._done:
            return
        self._done = True
        self._retire()
        self.job["status"] = "succeeded"
        self.job["progress"] = {"percent": 100, "current": message}
        self.job["timings"]["finished_at"] = now_iso()
        self._save()

    def fail(self, message: str) -> None:
        """标记失败并保留中文原因。"""
        if not self.job or self._done:
            return
        self._done = True
        self._retire()
        self.job["status"] = "failed"
        self.job["error"] = str(message or "生成失败")
        self.job["timings"]["finished_at"] = now_iso()
        self._save()

    def cancel(self, message: str = "页面连接已断开，生成已取消") -> None:
        """标记取消（页面断开、主动停止）。"""
        if not self.job or self._done:
            return
        self._done = True
        self._retire()
        self.job["status"] = "canceled"
        self.job["error"] = message
        self.job["timings"]["finished_at"] = now_iso()
        self._save()


def _num(value) -> float:
    """把事件里的数字安全转成 float，失败返回 0。"""
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0
