"""媒体页音效生成接口（后台任务 + 轮询进度）。

页面只做三件事：取 provider 健康状态、提交条目生成、轮询任务进度。
生成逻辑全部复用 `media/sfx.py`，与 CLI `sfx generate` / `media sfx run` 同一实现。
"""

from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime

from fastapi import HTTPException

from ..config import load_config
from ..media.sfx import generate_sfx_item, get_provider, sfx_engine
from ..sfx import SfxError
from ..store.project_store import ProjectStore

#: 已完成任务保留数量（页面只关心最近一次）
_MAX_JOBS = 30
_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()
#: 同一时刻只允许一条音效生成，避免章节状态并发写
_RUN_LOCK = threading.Lock()


def _now() -> str:
    """当前本地时间（秒级）。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _find_item(chapter: dict, item_id: str) -> dict | None:
    """在章节里按 id 找音效条目。"""
    for scene in chapter.get("scenes") or []:
        for item in scene.get("sfx_events") or []:
            if item.get("id") == item_id:
                return item
    return None


def _estimate_seconds(duration_s: float, warm: bool) -> float:
    """估算本次生成耗时：热推理约 0.45s/秒音频，冷启动加模型加载。

    参数:
        duration_s: 音频时长（秒）。
        warm: 常驻 worker 是否已在运行。

    返回:
        预计秒数（保留 1 位）。
    """
    return round((0.0 if warm else 13.0) + 1.0 + duration_s * 0.45, 1)


def _run_job(job_id: str, config_path: str | None, path: str, chapter_id: str, item_id: str, prompt_en: str | None) -> None:
    """后台线程：加载工程与章节，生成一条音效并写回状态。

    参数:
        job_id: 任务 ID。
        config_path: 配置文件路径。
        path: 工程根目录。
        chapter_id: 章节 ID。
        item_id: 音效条目 ID。
        prompt_en: 页面手填的英文提示词（可空）。
    """
    job = _JOBS[job_id]
    started = time.time()
    try:
        with _RUN_LOCK:
            job["stage"] = "loading"
            config = load_config(config_path)
            store = ProjectStore(path)
            chapter = store.load_chapter(chapter_id)
            item = _find_item(chapter, item_id)
            if item is None:
                raise SfxError("BAD_REQUEST", f"章节里找不到音效条目：{item_id}")
            pid, provider = get_provider(config)
            warm = bool(getattr(provider, "is_loaded", lambda: False)())
            duration_s = float(item.get("duration_ms") or 0) / 1000.0
            job["estimate_s"] = _estimate_seconds(duration_s, warm)
            job["stage"] = "generating"
            job["provider_id"] = pid
            produced = generate_sfx_item(store, config, item, prompt_en=prompt_en, provider=provider, provider_id=pid)
            chapter["revision"] = int(chapter.get("revision") or 1) + 1
            chapter["updated_at"] = datetime.now().isoformat(timespec="seconds")
            store.save_chapter(chapter)
            job["result"] = produced
            job["status"] = "done"
            job["stage"] = "done"
    except SfxError as exc:
        job["error"] = {"code": exc.code, "message": exc.message, "hint": exc.hint}
        job["status"] = "failed"
        job["stage"] = "failed"
    except Exception as exc:  # noqa: BLE001 - 后台线程必须把异常落回任务
        job["error"] = {"code": type(exc).__name__, "message": str(exc)}
        job["status"] = "failed"
        job["stage"] = "failed"
    finally:
        job["finished_at"] = _now()
        job["elapsed_s"] = round(time.time() - started, 2)


def register_sfx_routes(app, config_path: str | None = None) -> None:
    """注册音效相关接口。

    参数:
        app: FastAPI 应用。
        config_path: 本机配置文件路径。
    """

    @app.get("/api/ui/sfx/health")
    def ui_sfx_health() -> dict:
        """返回音效引擎与 provider 可用性（页面据此显示下载/检查入口）。"""
        config = load_config(config_path)
        engine = sfx_engine(config)
        if engine == "offline":
            return {"engine": "offline", "ok": True, "detail": "当前为 ffmpeg 离线合成（sfx.engine=offline）", "provider_id": None}
        try:
            pid, provider = get_provider(config)
            health = provider.health()
            return {
                "engine": "model",
                "ok": health.ok,
                "detail": health.detail,
                "code": health.code,
                "provider_id": pid,
                "warm": bool(getattr(provider, "is_loaded", lambda: False)()),
                "idle_timeout_s": getattr(provider, "idle_timeout_s", None),
            }
        except SfxError as exc:
            return {"engine": "model", "ok": False, "detail": exc.message, "code": exc.code, "provider_id": None}

    @app.post("/api/ui/sfx/generate")
    def ui_sfx_generate(body: dict) -> dict:
        """提交一条音效生成任务（后台执行，页面轮询进度）。"""
        for field in ("path", "chapter", "id"):
            if not body.get(field):
                raise HTTPException(status_code=400, detail={"code": 2, "message": f"缺少参数：{field}"})
        if _RUN_LOCK.locked():
            raise HTTPException(status_code=409, detail={"code": 2, "message": "已有音效在生成，请稍候"})
        job_id = uuid.uuid4().hex[:12]
        job = {
            "job_id": job_id,
            "status": "running",
            "stage": "queued",
            "started_at": _now(),
            "finished_at": None,
            "elapsed_s": 0.0,
            "estimate_s": None,
            "item_id": body["id"],
            "provider_id": None,
            "result": None,
            "error": None,
        }
        with _JOBS_LOCK:
            _JOBS[job_id] = job
            # 只保留最近若干任务，避免长驻进程内存增长
            for stale in list(_JOBS)[:-_MAX_JOBS]:
                _JOBS.pop(stale, None)
        threading.Thread(
            target=_run_job,
            args=(job_id, config_path, body["path"], body["chapter"], body["id"], body.get("prompt_en")),
            daemon=True,
            name=f"ndw-sfx-{job_id}",
        ).start()
        return {"job_id": job_id}

    @app.get("/api/ui/sfx/job")
    def ui_sfx_job(job_id: str) -> dict:
        """查询音效任务状态；运行中按耗时/预估给出百分比。"""
        job = _JOBS.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail={"code": 2, "message": f"任务不存在：{job_id}"})
        data = dict(job)
        if data["status"] == "running":
            elapsed = round(time.time() - _epoch(job["started_at"]), 2)
            data["elapsed_s"] = elapsed
            estimate = float(job.get("estimate_s") or 8.0)
            data["percent"] = round(min(95.0, elapsed * 100.0 / max(estimate, 1.0)), 1)
        else:
            data["percent"] = 100.0 if data["status"] == "done" else None
        return data


def _epoch(stamp: str) -> float:
    """把 `YYYY-mm-dd HH:MM:SS` 解析成时间戳；解析失败返回当前时间。"""
    try:
        return datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").timestamp()
    except (TypeError, ValueError):
        return time.time()
