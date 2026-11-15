"""角色捕获后台任务：作业文件、运行态、启动/取消/收尾。"""

from __future__ import annotations

import json
import threading

from ...models.ids import new_id, now_iso
from ...store import ProjectStore
from ...store.settings_store import SettingsStore
from .capture_cast import (
    _outline_acts,
    capture_chapter_cast,
    cast_capture_plan,
    chapter_cast_state,
)

#: 手动补跑任务文件（只保留最近一次，页面刷新/重进工程后仍可看到进度与失败原因）
_CAPTURE_JOB_FILE = "cast_capture_job.json"

#: 运行中的补跑任务：job_id -> 取消信号
_RUNNING: dict[str, threading.Event] = {}

#: 补跑线程：job_id -> Thread
_THREADS: dict[str, threading.Thread] = {}

_JOB_LOCK = threading.Lock()

def _capture_job_path(store: ProjectStore):
    """补跑任务文件路径。

    参数:
        store: 工程。

    返回:
        `.ndw/cast_capture_job.json` 的 Path。
    """
    return store.root / ".ndw" / _CAPTURE_JOB_FILE

def read_capture_job(store: ProjectStore) -> dict:
    """读取最近一次手动补跑任务；不存在或损坏时返回空 dict。

    参数:
        store: 工程。

    返回:
        任务 dict。
    """
    try:
        data = json.loads(_capture_job_path(store).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}

def _write_capture_job(store: ProjectStore, job: dict) -> None:
    """原子写入补跑任务文件。

    参数:
        store: 工程。
        job: 任务 dict（就地更新时间戳）。

    返回:
        无。
    """
    path = _capture_job_path(store)
    path.parent.mkdir(parents=True, exist_ok=True)
    job["updated_at"] = now_iso()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(job, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)

def _refresh_job_counts(job: dict) -> None:
    """按条目状态刷新任务统计。

    参数:
        job: 任务 dict（就地修改）。

    返回:
        无。
    """
    items = job.get("items") or []
    job["done"] = sum(1 for item in items if item.get("status") == "captured")
    job["failed"] = sum(1 for item in items if item.get("status") == "failed")
    job["skipped"] = sum(1 for item in items if item.get("status") == "skipped")
    job["pending"] = sum(1 for item in items if item.get("status") == "pending")

def latest_capture_job(store: ProjectStore) -> dict:
    """读取最近一次补跑任务；服务重启后残留的 running 按失败收尾。

    参数:
        store: 工程。

    返回:
        任务 dict；没有记录时为空 dict。
    """
    job = read_capture_job(store)
    if not job or job.get("status") != "running":
        return job
    with _JOB_LOCK:
        active = str(job.get("job_id") or "") in _RUNNING
    if active:
        return job
    for item in job.get("items") or []:
        if item.get("status") == "running":
            item["status"] = "failed"
            item["error"] = "服务重启导致中断，可重新补跑"
    job["status"] = "failed"
    job["note"] = "服务重启导致中断，可重新补跑"
    job["current"] = None
    job["finished_at"] = now_iso()
    _refresh_job_counts(job)
    _write_capture_job(store, job)
    return job

def _empty_plan_reason(store: ProjectStore, chapter_id: str) -> str:
    """补跑计划为空时给出可读原因。

    参数:
        store: 工程。
        chapter_id: 单章 ID；为空表示整幕。

    返回:
        中文原因。
    """
    if chapter_id:
        settings = SettingsStore(store.root)
        bible = settings.ensure()
        try:
            index = int(store.load_chapter(chapter_id).get("index") or 0)
        except (OSError, ValueError, KeyError):
            index = 0
        state = chapter_cast_state(store, settings.find_outline_chapter(bible, chapter_id, index=index))
        if state == "absent":
            return "本章还没有节拍，暂时无法做角色识别"
        if state in ("synced", "bound"):
            return "本章已完成角色识别，不需要补跑"
    return "没有需要补跑角色识别的章节"

def _act_title(store: ProjectStore, act_id: str, act_index: int | None) -> str:
    """取整幕补跑用的幕标题（ID 缺失时按序号兜底）。

    参数:
        store: 工程。
        act_id: 幕 ID。
        act_index: 幕序号。

    返回:
        例如「第 2 幕《风起》」；找不到时返回空串。
    """
    bible = SettingsStore(store.root).ensure()
    for position, act in _outline_acts(bible, act_id or None, act_index):
        return f"第 {int(act.get('index') or position)} 幕《{act.get('title') or ''}》"
    return ""

def start_capture_job(
    store: ProjectStore,
    config=None,
    *,
    chapter_id: str = "",
    act_id: str = "",
    act_index: int | None = None,
) -> dict:
    """启动手动补跑：只做角色识别，不写正文。

    参数:
        store: 工程。
        config: 本机配置（装配写作适配器）。
        chapter_id: 单章补跑；为空表示整幕。
        act_id: 整幕补跑时的幕 ID。
        act_index: 幕序号兜底（幕 ID 缺失时使用）。

    返回:
        任务 dict。

    异常:
        ValueError: 未指定范围、已有任务在跑，或没有需要补跑的章节。
    """
    if not chapter_id and not act_id and not act_index:
        raise ValueError("请指定要补跑角色识别的章节或幕")
    with _JOB_LOCK:
        active = next(iter(_RUNNING), "")
    if active:
        raise ValueError("已有角色识别任务在运行，请等待完成或先停止")
    plan = cast_capture_plan(
        store,
        chapter_id=chapter_id or None,
        act_id=act_id or None,
        act_index=act_index,
    )
    rows = plan["chapters"]
    if not rows:
        raise ValueError(_empty_plan_reason(store, chapter_id))
    first = rows[0]
    if chapter_id:
        title = f"第 {int(first.get('index') or 0)} 章《{first.get('title') or ''}》"
    else:
        title = _act_title(store, act_id, act_index) or "整幕"
    job = {
        "job_id": new_id("ccj"),
        "status": "running",
        "scope": "chapter" if chapter_id else "act",
        "chapter_id": chapter_id,
        "act_id": act_id,
        "act_index": int(act_index or 0),
        "title": title,
        "total": len(rows),
        "done": 0,
        "failed": 0,
        "skipped": 0,
        "pending": len(rows),
        "current": None,
        "items": [
            {
                "chapter_id": row.get("id"),
                "index": row.get("index"),
                "title": row.get("title"),
                "status": "pending",
                "message": "",
                "error": "",
                "created": [],
                "reused": [],
                "pending": [],
            }
            for row in rows
        ],
        "note": None,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "finished_at": None,
    }
    event = threading.Event()
    # 先登记运行中，再写任务文件：避免页面在「文件已写、运行登记未完成」的窗口里误判为中断
    with _JOB_LOCK:
        _RUNNING[job["job_id"]] = event
    _write_capture_job(store, job)
    thread = threading.Thread(target=_run_capture_job, args=(store, config, job["job_id"], event), daemon=True)
    with _JOB_LOCK:
        _THREADS[job["job_id"]] = thread
    thread.start()
    return job

def _finish_capture_job(job: dict, canceled: bool) -> None:
    """收敛任务状态与文案。

    参数:
        job: 任务 dict（就地修改）。
        canceled: 是否被用户停止。

    返回:
        无。
    """
    _refresh_job_counts(job)
    total = int(job.get("total") or 0)
    done = int(job.get("done") or 0)
    failed = int(job.get("failed") or 0)
    if canceled:
        job["status"] = "canceled"
        job["note"] = f"已停止：完成 {done} 章，未执行 {int(job.get('pending') or 0)} 章"
    elif failed and done:
        job["status"] = "partial"
        job["note"] = f"完成 {done}/{total} 章，{failed} 章失败"
    elif failed:
        job["status"] = "failed"
        job["note"] = f"{failed}/{total} 章失败"
    else:
        job["status"] = "succeeded"
        job["note"] = f"完成 {done}/{total} 章"
    job["current"] = None
    job["finished_at"] = now_iso()
    _refresh_job_counts(job)

def _run_capture_job(store: ProjectStore, config, job_id: str, event: threading.Event) -> None:
    """后台逐章执行补跑，写任务文件供页面轮询。

    参数:
        store: 工程。
        config: 本机配置。
        job_id: 任务 ID。
        event: 取消信号（章间检查）。

    返回:
        无。
    """
    job = read_capture_job(store)
    try:
        for position, item in enumerate(job.get("items") or [], start=1):
            if event.is_set():
                item["status"] = "skipped"
                item["error"] = "已停止，未执行"
                continue
            item["status"] = "running"
            item["error"] = ""
            job["current"] = {
                "chapter_id": item.get("chapter_id"),
                "index": item.get("index"),
                "title": item.get("title"),
                "position": position,
            }
            _refresh_job_counts(job)
            _write_capture_job(store, job)
            try:
                result = capture_chapter_cast(store, str(item.get("chapter_id") or ""), config=config)
            except Exception as exc:  # noqa: BLE001 - 单章失败不中断整批，原因写进条目
                item["status"] = "failed"
                item["error"] = str(getattr(exc, "message", None) or exc or "未知错误")
            else:
                item["message"] = str(result.get("message") or "")
                item["created"] = list(result.get("created") or [])
                item["reused"] = list(result.get("reused") or [])
                item["pending"] = list(result.get("pending") or [])
                if result.get("triggered"):
                    item["status"] = "captured"
                else:
                    item["status"] = "skipped"
                    item["error"] = "已识别过，本次跳过"
            _refresh_job_counts(job)
            _write_capture_job(store, job)
        _finish_capture_job(job, event.is_set())
    except Exception as exc:  # noqa: BLE001 - 线程级异常收敛为失败
        job["status"] = "failed"
        job["note"] = f"补跑中断：{exc}"
        job["current"] = None
        job["finished_at"] = now_iso()
        _refresh_job_counts(job)
    finally:
        _write_capture_job(store, job)
        with _JOB_LOCK:
            _RUNNING.pop(job_id, None)
            _THREADS.pop(job_id, None)

def cancel_capture_job(store: ProjectStore, job_id: str = "") -> dict:
    """停止手动补跑：当前章节识别结束后生效，已完成章节保留。

    参数:
        store: 工程。
        job_id: 任务 ID；为空时停止最近一次任务。

    返回:
        收敛后的任务 dict。

    异常:
        ValueError: 指定的任务与当前任务不一致。
    """
    job = read_capture_job(store)
    if not job:
        return {}
    current_id = str(job.get("job_id") or "")
    if job_id and job_id != current_id:
        raise ValueError("任务已变化，请刷新后重试")
    with _JOB_LOCK:
        event = _RUNNING.get(current_id)
        thread = _THREADS.get(current_id)
    if event:
        event.set()
    if thread is not None and thread.is_alive():
        thread.join(timeout=30)
    job = read_capture_job(store) or job
    if job.get("status") == "running":
        job["cancel_requested"] = True
        _write_capture_job(store, job)
    return job
