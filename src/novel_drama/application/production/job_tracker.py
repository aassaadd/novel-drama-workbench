"""界面生成任务记录：`.ndw/ui_jobs.json`。

AI 生成在 Web 进程内以 SSE / 同步请求执行，不经过 GPU 队列；
为了让「任务」页能看到这些生成，这里单独记录一份轻量任务列表，
由 `/api/ui/queue` 与 GPU 队列合并展示。

记录只做展示：状态为 queued/running/succeeded/partial/failed/canceled/interrupted，
带类型、通道、动作、目标、进度、阶段与错误原因。文件写入用进程内锁串行化，
避免多个生成同时更新时互相覆盖。Web 进程退出后，遗留的 running 任务在下次读取时
标记为 interrupted。

任务结构版本为 2：旧版（`schema_version != 2`）文件直接丢弃，下次写入时覆盖重建。
"""

from __future__ import annotations

import json
import threading
import time

from ...adapters.persistence.project_store import ProjectStore
from ...cancel import is_requested as _cancel_requested
from ...cancel import register as _cancel_register
from ...cancel import release as _cancel_release
from ...cancel import request as _cancel_request
from ...models.enum_check import check_ui_jobs
from ...models.ids import new_id, now_iso

#: 任务文件结构版本；旧版本文件读取时直接丢弃
_SCHEMA_VERSION = 2
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

#: 已结束的任务状态（可清理）
TERMINAL_STATUSES = ("succeeded", "partial", "failed", "canceled", "interrupted")


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
    """UI 任务文件路径。

    参数:
        store: 工程。

    返回:
        `.ndw/ui_jobs.json` 的 Path。
    """
    return store.root / ".ndw" / "ui_jobs.json"


def _read(store: ProjectStore) -> list[dict]:
    """读取任务列表；旧结构或损坏文件一律按空列表处理。

    参数:
        store: 工程。

    返回:
        任务列表；`schema_version != 2` 时返回空列表（旧文件不兼容）。
    """
    path = _file(store)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    try:
        version = int(data.get("schema_version") or 0)
    except (TypeError, ValueError):
        version = 0
    if version != _SCHEMA_VERSION:
        return []
    jobs = data.get("jobs", [])
    return jobs if isinstance(jobs, list) else []


def _write(store: ProjectStore, jobs: list[dict]) -> None:
    """覆盖写入任务列表（先校验枚举，只保留最近若干条）。

    参数:
        store: 工程。
        jobs: 界面任务列表。

    异常:
        EnumValueError: 任务类型或状态不是已定义值。
    """
    check_ui_jobs(jobs, baseline=_read(store))
    path = _file(store)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"schema_version": _SCHEMA_VERSION, "jobs": jobs[-_MAX_JOBS:]}, ensure_ascii=False),
        encoding="utf-8",
    )


def _sync_progress(job: dict) -> None:
    """把顶层 percent/current 同步到 `progress`（任务页与 LLM 代码都读这个字段）。

    参数:
        job: 任务记录（原地修改）。

    返回:
        无。
    """
    job["progress"] = {"percent": job.get("percent"), "current": job.get("current") or ""}


def job_snapshot(job: dict) -> dict:
    """把任务记录整理成统一快照（补齐默认值，供任务页与媒体总线共用）。

    参数:
        job: 任务记录。

    返回:
        含 `id/job_id/kind/action/channel/parent_id/cancelable/title/target/status/percent/
        stage/current/error/artifacts/progress/timings/created_at/meta` 的 dict。
    """
    progress = job.get("progress") if isinstance(job.get("progress"), dict) else {}
    percent = job.get("percent", progress.get("percent"))
    current = job.get("current") or progress.get("current") or ""
    return {
        "id": job.get("id"),
        "job_id": job.get("job_id") or job.get("id"),
        "kind": job.get("kind") or "",
        "action": job.get("action") or "",
        "channel": job.get("channel") or "",
        "parent_id": job.get("parent_id"),
        "cancelable": bool(job.get("cancelable", True)),
        "title": job.get("title") or "",
        "target": dict(job.get("target") or {}),
        "status": job.get("status") or "",
        "percent": percent,
        "stage": job.get("stage") or "",
        "current": current,
        "error": job.get("error"),
        "artifacts": list(job.get("artifacts") or []),
        "progress": {"percent": percent, "current": current},
        "timings": dict(job.get("timings") or {}),
        "created_at": job.get("created_at") or "",
        "meta": dict(job.get("meta") or {}),
    }


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
                    job["percent"] = None
                    job["current"] = job.get("current") or "任务中断"
                    _sync_progress(job)
                    job.setdefault("timings", {})["finished_at"] = now_iso()
                    changed = True
            if changed:
                _write(store, jobs)
    return jobs


def update_job(store: ProjectStore, job_id: str, **fields) -> dict | None:
    """按 ID 修改任务字段（供终止/兜底使用）。

    修改 status 或 error 时同步 percent/current/progress：
    成功类固定 100%，失败/取消/中断清除百分比。

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
            if item.get("id") != job_id and item.get("job_id") != job_id:
                continue
            item.update(fields)
            if "status" in fields:
                status = str(item.get("status") or "")
                if status in ("succeeded", "partial"):
                    item["percent"] = 100
                    item["stage"] = item.get("stage") or "完成"
                elif status in TERMINAL_STATUSES:
                    item["percent"] = None
                    item["stage"] = item.get("stage") or "已结束"
                    if not item.get("current"):
                        item["current"] = item.get("error") or "任务已结束"
            _sync_progress(item)
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

    def __init__(
        self,
        store: ProjectStore,
        kind: str,
        target: dict | None = None,
        title: str = "",
        action: str = "",
        channel: str = "",
        parent_id: str | None = None,
        cancelable: bool = True,
    ):
        """创建任务（尚未写入）。

        参数:
            store: 工程。
            kind: 任务类型（UiJobKind 落盘值）。
            target: 目标信息，例如 `{chapter_id, chapter_title}`。
            title: 展示标题。
            action: 媒体动作键（LLM 任务可留空）。
            channel: 通道（audio/image/video/llm，可留空）。
            parent_id: 父任务 ID（媒体任务内嵌的 LLM 调用使用）。
            cancelable: 是否允许取消。
        """
        self.store = store
        self.kind = kind
        self.target = target or {}
        self.title = title
        self.action = action
        self.channel = channel
        self.parent_id = parent_id
        self.cancelable = bool(cancelable)
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

    def start(self, current: str = "已提交生成请求", stage: str = "提交") -> "UiJob":
        """写入一条运行中任务。

        参数:
            current: 当前进度文案。
            stage: 当前阶段（中文文案）。

        返回:
            self，便于链式调用。
        """
        job_id = new_id("job")
        started_at = now_iso()
        self.job = {
            "id": job_id,
            "job_id": job_id,
            "kind": self.kind,
            "action": self.action,
            "channel": self.channel,
            "parent_id": self.parent_id,
            "cancelable": self.cancelable,
            "title": self.title,
            "target": self.target,
            "status": "running",
            "percent": None,
            "stage": stage,
            "current": current,
            "artifacts": [],
            "error": None,
            "progress": {"percent": None, "current": current},
            "meta": {},
            "attempts": 0,
            "priority": 100,
            "idempotency_key": f"ui:{self.kind}:{new_id('key')}",
            "created_at": started_at,
            "timings": {"started_at": started_at},
        }
        self._save()
        with _ACTIVE_LOCK:
            _ACTIVE[job_id] = self.kind
        _cancel_register(job_id)
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
        stage: str | None = None,
        force: bool = False,
        clear_percent: bool = False,
    ) -> None:
        """更新进度（默认最多每秒写一次，避免高频写盘）。

        参数:
            current: 当前进度文案。
            percent: 百分比；None 表示不修改。
            stage: 当前阶段（中文文案）；None 表示不修改。
            force: True 时忽略节流立即写入。
            clear_percent: True 时把百分比置为「未知」，界面显示进行中而不是停在 0%。
        """
        if not self.job or self._done or self.cancel_requested():
            return
        if stage is not None:
            self.job["stage"] = str(stage)
        if current is not None:
            self.job["current"] = str(current)
        if clear_percent:
            self.job["percent"] = None
        elif percent is not None:
            self.job["percent"] = max(0, min(100, round(float(percent))))
        _sync_progress(self.job)
        now = time.monotonic()
        if not force and now - self._last_write < 1.0:
            return
        self._last_write = now
        self._save()

    def stage(self, name: str) -> None:
        """只更新阶段文案（立即写入）。

        参数:
            name: 阶段中文名，例如「载入」「合成」。
        """
        if not name:
            return
        self.progress(stage=str(name), force=True)

    def set_meta(self, **fields) -> None:
        """写入任务元数据（例如执行结果摘要、嵌套 LLM 调用次数）。

        任务落成终态后仍允许补写元数据（不改变状态与进度）。

        参数:
            **fields: 要合并进 `meta` 的字段。

        返回:
            无。
        """
        if not self.job:
            return
        meta = self.job.setdefault("meta", {})
        meta.update(fields)
        self._save()

    def snapshot(self) -> dict:
        """返回统一任务快照。

        返回:
            字段见 `job_snapshot`；任务尚未创建时返回空 dict。
        """
        if not self.job:
            return {}
        return job_snapshot(self.job)

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
            text = _event_text(data, name)
            if data.get("percent") is not None:
                self.progress(text or None, _num(data.get("percent")))
            elif total > 1:
                self.progress(text, ((index - 1) / total) * 100)
            else:
                # 单批任务没有「批次推进」可展示：直接显示进行中，字数由 chars 事件累加
                self.progress(text or None, clear_percent=True)
        elif kind == "chars":
            chars = _num(data.get("chars"))
            batch = self._batch or {}
            merged = dict(batch)
            merged.update(data)
            if chars:
                head = _event_text(merged, str(data.get("name") or batch.get("name") or ""))
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
        elif kind == "rewrite":
            # 逐层闸门的按项重写：把「哪一层次、第几次、为什么」写进任务进度
            level = {"acts": "幕", "chapters": "章", "beats": "节拍"}.get(str(data.get("level") or ""), "幕")
            attempt = int(_num(data.get("attempt")) or 1)
            act_index = int(_num(data.get("index")) or 0)
            chapter_index = int(_num(data.get("chapter_index")) or 0)
            chapter = str(data.get("chapter") or "").strip()
            who = (
                f"第 {act_index} 幕 · 第 {chapter_index} 章《{chapter}》"
                if chapter and act_index and chapter_index
                else (f"第 {act_index} 幕" if act_index else "整体")
            )
            reason = str(data.get("reason") or "").strip()
            text = f"第 {attempt} 次重写 {who}（{level}层）"
            if reason:
                text = f"{text}：{reason}"
            self.progress(text, force=True, clear_percent=True)
        elif kind == "notice":
            message = str(data.get("message") or "").strip()
            if message:
                # 上下文超限 / finish_reason=length 等提醒：留在任务进度里，避免只在服务端日志里
                self.progress(f"⚠ {message}", force=True, clear_percent=True)
        elif kind == "stage":
            stage = str(data.get("stage") or data.get("message") or "").strip()
            stage = {"acts": "规划幕结构", "chapters": "补全章节", "beats": "补齐节拍"}.get(stage, stage)
            if stage:
                self.progress(stage, stage=stage, force=True)
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

    def _finish_with(
        self,
        status: str,
        message: str,
        percent: int | None,
        stage: str,
        error: str | None = None,
    ) -> None:
        """把任务落成终态，并同步 percent/stage/current 与 progress。

        参数:
            status: 终态状态值。
            message: 进度文案。
            percent: 百分比；None 表示未知。
            stage: 阶段文案。
            error: 错误列文案；None 表示保留原值。
        """
        self._done = True
        self._retire()
        self.job["status"] = status
        self.job["percent"] = percent
        self.job["stage"] = stage
        self.job["current"] = message
        if error is not None:
            self.job["error"] = error
        _sync_progress(self.job)
        self.job.setdefault("timings", {})["finished_at"] = now_iso()
        self._save()

    def finish(self, message: str = "生成完成") -> None:
        """标记成功并保留结果摘要。

        若用户已点停止，则落「已取消」而不是「成功」，避免停止后仍显示完成。

        参数:
            message: 结果摘要（显示在进度列）。
        """
        if not self.job or self._done:
            return
        if self.cancel_requested():
            self.cancel("用户已停止生成")
            return
        self._finish_with("succeeded", str(message or "生成完成"), 100, "完成")

    def partial(self, message: str) -> None:
        """标记「部分完成」：有产出也有失败时使用，进度固定 100% 并写入汇总。

        参数:
            message: 汇总文案（例如「成功 3 章 / 跳过 0 章 / 失败 2 章」）。
        """
        if not self.job or self._done:
            return
        if self.cancel_requested():
            self.cancel("用户已停止生成")
            return
        self._finish_with("partial", str(message or "部分完成"), 100, "部分完成")

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
        reason = str(message or "生成失败")
        self._finish_with("failed", str(current or "生成失败"), None, "失败", error=reason)

    def cancel(self, message: str | None = None) -> None:
        """标记取消（页面断开、主动停止）。

        参数:
            message: 取消原因；省略时按是否用户主动停止给出默认文案。
        """
        if not self.job or self._done:
            return
        requested = self.cancel_requested()
        reason = str(message or ("用户已停止生成" if requested else "页面连接已断开，生成已取消"))
        self._finish_with("canceled", reason, None, "已取消", error=reason)


def _num(value) -> float:
    """把事件里的数字安全转成 float，失败返回 0。"""
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0


#: 批次单位中文名（空单位按「批」展示）
_BATCH_UNIT = {"act": "幕", "chapter": "章", "beat": "个节拍", "outline": "", "batch": "批", "world": "个世界", "character": "个角色"}


def _event_text(data: dict, name: str = "") -> str:
    """按阶段拼进度文案，避免把章数/节拍数错配到「幕」的单位上。

    - 幕阶段：`第 k/10 幕`（一次请求出全量时显示 `规划 10 幕`）；
    - 章阶段：`第 x/10 幕 · 已完成 k/50 章`；
    - 节拍阶段：`第 x/10 幕 · 第 k/50 章 · 第 y/200 节拍`；
    - 其他（世界 / 角色）：`第 2/3 批`。

    参数:
        data: 事件负载（可含 phase/act_index/act_total/chapter_index/chapter_total/beat_index/beat_total）。
        name: 当前项名称。

    返回:
        中文进度前缀。
    """
    phase = str(data.get("phase") or "").strip()
    act_total = int(_num(data.get("act_total") or (data.get("total") if phase in ("acts", "chapters") else 0)))
    act_index = int(_num(data.get("act_index") or data.get("index")))
    chapter_total = int(_num(data.get("chapter_total") or data.get("chapters_total")))
    chapter_index = int(_num(data.get("chapter_index")))
    beat_total = int(_num(data.get("beat_total")))
    beat_index = int(_num(data.get("beat_index") or data.get("index")))
    if phase == "acts":
        return f"规划 {act_total} 幕" if act_total else ""
    if phase == "chapters":
        done = int(_num(data.get("chapters_done")))
        head = f"第 {act_index}/{act_total} 幕 · 已完成 {done}/{chapter_total} 章"
        return f"{head} · {name}" if name else head
    if phase == "beats":
        head = (
            f"第 {act_index}/{act_total} 幕 · 第 {chapter_index}/{chapter_total} 章"
            f" · 第 {max(1, beat_index)}/{beat_total} 节拍"
        )
        return f"{head} · {name}" if name else head
    index = _num(data.get("index") or data.get("batch"))
    total = _num(data.get("total")) or 1
    unit = str(data.get("unit") or "").strip()
    return _batch_text(index, total, unit, name)


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
