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

from ..cancel import is_requested as _cancel_requested
from ..cancel import register as _cancel_register
from ..cancel import release as _cancel_release
from ..cancel import request as _cancel_request
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


def request_cancel(job_id: str) -> bool:
    """请求停止运行中的生成任务。

    参数:
        job_id: 任务 ID。

    返回:
        True 表示确实通知到了本进程内的生成线程；False 表示查无进程（僵尸任务）。
    """
    return _cancel_request(job_id)


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
        #: 当前批次上下文（index/total/unit/name），char 事件缺字段时沿用
        self._batch: dict = {}
        #: 当前批次是否已经由 chars 事件清空过百分比
        self._batch_touched = False

    def _save(self, **patch) -> None:
        """把字段补丁写回任务文件。

        若文件里已被外部（任务队列「停止」）落成终态，则以文件为准，
        避免运行线程用旧快照把「已取消」覆盖回「运行中」。
        """
        with _LOCK:
            if not self.job:
                return
            jobs = _read(self.store)
            for index, item in enumerate(jobs):
                if item.get("id") == self.job.get("id"):
                    if item.get("status") in TERMINAL_STATUSES and self.job.get("status") not in TERMINAL_STATUSES:
                        self.job = item
                        self._done = True
                        return
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
        _cancel_register(self.job["id"])
        return self

    def cancel_requested(self) -> bool:
        """任务是否已被任务队列「停止」请求取消。

        返回:
            True 表示用户已点过停止，后续进度与产物写入都应跳过。
        """
        if not self.job:
            return False
        return _cancel_requested(str(self.job.get("id") or ""))

    def progress(
        self,
        current: str | None = None,
        percent: float | None = None,
        force: bool = False,
        clear_percent: bool = False,
    ) -> None:
        """更新进度（默认最多每秒写一次，避免高频写盘）。

        参数:
            current: 当前进度文案。
            percent: 百分比；None 表示不修改。
            force: True 时忽略节流立即写入。
            clear_percent: True 时把百分比置为「未知」，界面显示进行中而不是停在 0%。
        """
        if not self.job or self._done or self.cancel_requested():
            return
        now = time.monotonic()
        if not force and now - self._last_write < 1.0:
            return
        self._last_write = now
        if current is not None:
            self.job["progress"]["current"] = str(current)
        if clear_percent:
            self.job["progress"]["percent"] = None
        elif percent is not None:
            self.job["progress"]["percent"] = max(0, min(100, round(float(percent))))
        self._save()

    def set_artifacts(self, items: list) -> None:
        """写入产物列表（任务队列「产物」列直接取长度）。

        参数:
            items: 产物路径列表。
        """
        if not self.job or self._done or self.cancel_requested():
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
        if self.cancel_requested():
            return
        if kind == "batch":
            index = _num(data.get("index"))
            total = _num(data.get("total")) or 1
            name = str(data.get("name") or data.get("title") or "").strip()
            unit = str(data.get("unit") or "").strip()
            self._batch = {"index": index, "total": total, "unit": unit, "name": name}
            self._batch_touched = False
            text = _batch_text(index, total, unit, name)
            if total > 1:
                self.progress(text, ((index - 1) / total) * 100)
            else:
                # 单批任务没有「批次推进」可展示：直接显示进行中，字数由 chars 事件累加
                self.progress(text or None, clear_percent=True)
        elif kind == "chars":
            chars = _num(data.get("chars"))
            batch = self._batch or {}
            index = _num(data.get("index") or data.get("batch") or batch.get("index"))
            total = _num(data.get("total") or batch.get("total")) or 1
            unit = str(data.get("unit") or batch.get("unit") or "").strip()
            if chars:
                head = _batch_text(index, total, unit)
                text = f"{head} · 已接收 {int(chars)} 字" if head else f"已接收 {int(chars)} 字"
                # 当前批还没结束：百分比停在批首（常见 0%），改为「进行中」并持续累加字数
                force = not self._batch_touched
                self._batch_touched = True
                self.progress(text, force=force, clear_percent=True)
        elif kind == "item":
            name = str(data.get("name") or "").strip()
            status = str(data.get("status") or "").strip()
            if name and status == "failed":
                reason = str(data.get("message") or "").strip()
                self.progress(f"{name} · 失败{('（' + reason + '）') if reason else ''}", force=True)
            elif name:
                self.progress(f"{name}{' · 已完成' if status == 'done' else ''}", force=status == "done")
        elif kind == "stage":
            stage = str(data.get("stage") or data.get("message") or "").strip()
            stage = {"acts": "规划幕结构", "chapters": "补全章节", "beats": "补齐节拍"}.get(stage, stage)
            if stage:
                self.progress(stage, force=True)
        elif kind == "plan":
            total = _num(data.get("total"))
            if total:
                self.progress(f"已规划 {int(total)} 个角色", 0, force=True)

    def _retire(self) -> None:
        """任务进入终态后，从本进程运行中集合与取消登记里移除。"""
        if not self.job:
            return
        job_id = str(self.job.get("id") or "")
        with _ACTIVE_LOCK:
            _ACTIVE.pop(job_id, None)
        _cancel_release(job_id)

    def close(self) -> None:
        """结束跟踪但不改任务状态（外部已把任务写成终态时使用）。"""
        self._retire()

    def finish(self, message: str = "生成完成") -> None:
        """标记成功并保留结果摘要。

        若用户已点停止，则落「已取消」而不是「成功」，避免停止后仍显示完成。
        """
        if not self.job or self._done:
            return
        if self.cancel_requested():
            self.cancel("用户已停止生成")
            return
        self._done = True
        self._retire()
        self.job["status"] = "succeeded"
        self.job["progress"] = {"percent": 100, "current": message}
        self.job["timings"]["finished_at"] = now_iso()
        self._save()

    def fail(self, message: str, current: str | None = None) -> None:
        """标记失败并保留中文原因。

        参数:
            message: 失败原因（写入错误列）。
            current: 进度列的简短失败定位；省略时写「生成失败」，避免残留最后一次运行中进度。
        """
        if not self.job or self._done:
            return
        if self.cancel_requested():
            self.cancel("用户已停止生成")
            return
        self._done = True
        self._retire()
        self.job["status"] = "failed"
        self.job["error"] = str(message or "生成失败")
        self.job["progress"] = {"percent": None, "current": str(current or "生成失败")}
        self.job["timings"]["finished_at"] = now_iso()
        self._save()

    def cancel(self, message: str | None = None) -> None:
        """标记取消（页面断开、主动停止）。

        参数:
            message: 取消原因；省略时按是否用户主动停止给出默认文案。
        """
        if not self.job or self._done:
            return
        requested = self.cancel_requested()
        self._done = True
        self._retire()
        self.job["status"] = "canceled"
        self.job["error"] = str(message or ("用户已停止生成" if requested else "页面连接已断开，生成已取消"))
        self.job["timings"]["finished_at"] = now_iso()
        self._save()


def _num(value) -> float:
    """把事件里的数字安全转成 float，失败返回 0。"""
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0


#: 批次单位中文名（空单位按「批」展示）
_BATCH_UNIT = {"act": "幕", "chapter": "章", "outline": "", "world": "个世界", "character": "个角色"}


def _batch_text(index: float, total: float, unit: str, name: str = "") -> str:
    """把批次上下文格式化成「第 1/10 幕 · 幕名」。

    参数:
        index: 当前批次序号（从 1 开始）。
        total: 批次总数。
        unit: 批次单位（act/chapter/world/character/outline，空串按「批」）。
        name: 批次名称（幕名 / 角色名等，可空）。

    返回:
        中文进度前缀；无法定位批次时返回空串。
    """
    label = _BATCH_UNIT.get(unit, "批")
    head = f"第 {int(index)}/{int(total)} {label}" if total > 1 else ""
    if name:
        head = f"{head} · {name}" if head else name
    return head
