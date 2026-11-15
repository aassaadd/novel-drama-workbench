"""任务队列、编排、GPU 占用与停止/续跑。"""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from fastapi import HTTPException, Query

from novel_drama.application.production.job_tracker import (
    UiJob,
    active_jobs,
    clear_finished,
    load_ui_jobs,
    request_cancel,
    update_job,
)
from novel_drama.application.production.queue_query import queue_jobs, run_record
from novel_drama.application.production.tts_plan import plan_chapter
from novel_drama.composition import ProjectStore, QueueStore
from novel_drama.infrastructure.config import load_config
from novel_drama.interface.http import use_cases
from novel_drama.interface.http.deps import store_for
from novel_drama.interface.http.executor import spawn_application
from novel_drama.interface.http.routers.common import (
    _ACTIVE_TTS_PROJECTS,
    _is_tts_process,
    _require,
    _stop_tts_project,
)
from novel_drama.models.ids import now_iso
from novel_drama.tts import TtsService

_PRODUCE_GUARD = threading.Lock()

_PRODUCE_PROCS: dict[str, dict] = {}

_PRODUCE_PROCS_LOCK = threading.Lock()

def _reconcile_produce_jobs(store: ProjectStore) -> None:
    """兜底：运行中但本进程已无进程句柄的编排任务，标记为已中断。

    参数:
        store: 工程。

    返回:
        无。
    """
    from datetime import datetime, timezone

    running = [
        item
        for item in load_ui_jobs(store)
        if item.get("kind") == "produce" and item.get("status") in ("queued", "running")
    ]
    if not running:
        return
    with _PRODUCE_PROCS_LOCK:
        known = set(_PRODUCE_PROCS)
    now = datetime.now(timezone.utc)
    for item in running:
        if item.get("id") in known:
            continue
        try:
            created = datetime.fromisoformat(str(item.get("created_at") or "").replace("Z", "+00:00"))
        except ValueError:
            continue
        # 给启动流程 90 秒注册缓冲，避免刚点启动就被判死
        if (now - created).total_seconds() < 90:
            continue
        update_job(
            store,
            str(item.get("id")),
            status="interrupted",
            error="编排进程已不存在（服务重启或后台线程异常），任务已标记为中断",
        )

def _cancel_produce_job(store: ProjectStore, job_id: str) -> dict:
    """终止编排子进程，并把任务与编排记录落成「已取消」。

    参数:
        store: 工程。
        job_id: 任务 ID。

    返回:
        `{canceled, message, status?, exit_code?}`。

    异常:
        HTTPException 404: 任务不存在。
    """
    from novel_drama.orchestration import produce as produce_mod

    current = next((item for item in load_ui_jobs(store) if item.get("id") == job_id), None)
    if current is None:
        raise HTTPException(status_code=404, detail={"code": 4, "message": "任务不存在，可能已被清理"})
    if current.get("status") in ("succeeded", "failed", "canceled", "interrupted"):
        return {
            "canceled": False,
            "message": f"任务已结束（{current.get('status')}），无需终止",
            "status": current.get("status"),
        }

    with _PRODUCE_PROCS_LOCK:
        entry = _PRODUCE_PROCS.get(job_id)
        if entry:
            entry["canceled"] = True
    if entry is None:
        # 服务重启等导致拿不到进程句柄：只要任务还是运行中，就落「已取消」避免永远挂起
        update_job(store, job_id, status="canceled", error="编排进程不在本进程内，已标记取消")
        return {"canceled": True, "message": "编排进程已不在本进程，任务已标记为已取消", "status": "canceled"}

    process = entry["process"]
    code = process.poll()
    kill_error = ""
    if code is None:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(os.getpgid(process.pid), sig)
            except PermissionError:
                kill_error = f"无权限终止进程（pid {process.pid}）"
            except ProcessLookupError:
                # 进程刚好已退出：下一轮 poll/wait 会拿到退出码
                pass
            try:
                code = process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                continue
            break

    # 编排记录若还停在运行中，写成 canceled，避免下次续跑误读
    try:
        run = produce_mod.latest_run(store) or {}
        if str(run.get("status") or "") in ("running", "queued", "pending", "") and str(
            run.get("run_id") or ""
        ) != "":
            run["status"] = "canceled"
            run["finished_at"] = now_iso()
            for stage in run.get("stages") or []:
                if stage.get("status") == "running":
                    stage["status"] = "canceled"
                    stage["finished_at"] = now_iso()
                    stage["note"] = "用户终止"
            produce_mod.save_run(store, run)
    except Exception:  # noqa: BLE001 - 记录收尾失败不影响任务终态
        pass
    if code is None:
        # 进程没在 5 秒内退出：把可读原因写进任务，状态仍落取消，避免永远占着运行中
        reason = f"{kill_error or '编排进程未响应终止信号'}，未能在 5 秒内退出（pid {process.pid}）；任务已标记为已取消"
        update_job(store, job_id, status="canceled", error=reason)
        return {"canceled": True, "message": reason, "status": "canceled"}
    update_job(store, job_id, status="canceled", error=f"已手动终止编排（退出码 {code}）")
    return {"canceled": True, "message": "已终止编排进程，任务标记为已取消", "status": "canceled", "exit_code": code}

def _log_tail(path: Path, lines: int = 8, limit: int = 1200) -> str:
    """读取日志文件末尾若干行，用于把子进程失败原因暴露到界面。

    参数:
        path: 日志文件路径。
        lines: 保留的最后行数。
        limit: 最多保留的字符数（避免任务队列里塞进整篇 traceback）。

    返回:
        末尾文本（无内容或读取失败时返回空串）。
    """
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    text = data[-16384:].decode("utf-8", "replace")
    rows = [row.rstrip() for row in text.splitlines() if row.strip()]
    return "\n".join(rows[-lines:])[-limit:]

def register_jobs_routes(app, project_root: str | None = None, config_path: str | None = None) -> None:
    """注册「任务队列、编排、GPU 占用与停止/续跑」相关路由。

    参数:
        app: FastAPI 实例。
        project_root: 默认工程目录（请求未带 path 时使用）。
        config_path: 本机配置文件路径（与 serve 同源）。

    返回:
        无。
    """
    _store = store_for(project_root)

    @app.get("/api/ui/plan")
    def ui_plan(path: str = Query(...), chapter: str = Query(...)):
        """本次将生成多少行（`tts plan --json`）。"""
        store = _store(path)
        plan = use_cases.call(
            lambda: plan_chapter(store, load_config(config_path), chapter)
        )
        return {
            "scope": plan["scope"],
            "selected": plan["selected"],
            "counts": plan["counts"],
            "jobs": len(plan["jobs"]),
            "adapter": plan["adapter"],
        }

    @app.get("/api/ui/queue")
    def ui_queue(path: str = Query(...)):
        """队列快照：GPU 队列 + 界面 AI 生成任务，按创建时间倒序。

        参数:
            path: 工程根目录。

        返回:
            任务数组（字段与 `queue list --json` 一致，界面任务额外带 progress）。
        """
        store = _store(path)
        jobs = use_cases.call(lambda: queue_jobs(store))
        _reconcile_produce_jobs(store)
        # 打上 ui 标记：只有界面发起、能在这里停的生成任务才在「操作」列给停止按钮
        ui_jobs = [{**item, "ui": True} for item in load_ui_jobs(store)]
        merged = [*ui_jobs, *jobs]
        merged.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return merged

    @app.get("/api/ui/gpu/status")
    def ui_gpu_status(path: str = Query(...)) -> dict:
        """GPU 锁与并发明细：锁文件持有人 + 队列状态计数（任务页展示）。

        参数:
            path: 工程根目录。

        返回:
            `{lock, queue, paused, max_concurrent, poll_interval_s}`。
        """
        from novel_drama.orchestration.gpu_queue import GpuLock

        store = _store(path)
        cfg = load_config(config_path) if config_path else load_config()
        lock_path = str(cfg.get("gpu.lock_path") or "")
        lock = GpuLock(lock_path).inspect() if lock_path else {
            "path": "",
            "exists": False,
            "alive": False,
            "holder": None,
        }
        jobs = QueueStore(store).load()
        counts = {"total": len(jobs), "queued": 0, "running": 0, "failed": 0, "succeeded": 0}
        for job in jobs:
            status = str(job.get("status") or "")
            if status in counts:
                counts[status] += 1
        return {
            "lock": lock,
            "queue": counts,
            "paused": QueueStore(store).paused(),
            "max_concurrent": int(cfg.get("gpu.max_concurrent") or 1),
            "poll_interval_s": float(cfg.get("gpu.poll_interval_s") or 2),
        }

    @app.post("/api/ui/gpu/lock/release")
    def ui_gpu_lock_release(path: str = Query(...)) -> dict:
        """手动释放僵死的 GPU 锁（持锁进程仍存活时拒绝）。

        参数:
            path: 工程根目录（用于解析配置与提示文案）。

        返回:
            `{released, message, lock}`。

        异常:
            HTTPException 400: 未配置 gpu.lock_path。
            HTTPException 409: 持锁进程仍存活，不能强制释放。
            HTTPException 500: 锁文件删除失败（权限或只读卷）。
        """
        from novel_drama.orchestration.gpu_queue import GpuLock

        cfg = load_config(config_path) if config_path else load_config()
        lock_path = str(cfg.get("gpu.lock_path") or "").strip()
        if not lock_path:
            raise HTTPException(status_code=400, detail={"code": 2, "message": "没有配置 gpu.lock_path，无需释放"})
        lock = GpuLock(lock_path)
        info = lock.inspect()
        if not info["exists"]:
            return {"released": False, "message": "锁文件不存在，GPU 本来就空闲", "lock": info}
        if info["alive"]:
            holder = info.get("holder") or {}
            raise HTTPException(
                status_code=409,
                detail={
                    "code": 6,
                    "message": (
                        f"锁的持锁进程仍在运行（pid {holder.get('pid') or '?'}），不能强制释放；"
                        "等它结束或退出后会自动释放"
                    ),
                },
            )
        try:
            removed = lock.break_stale()
        except OSError as exc:
            raise HTTPException(
                status_code=500, detail={"code": 3, "message": f"删除锁文件失败：{exc}"}
            ) from exc
        if not removed:
            raise HTTPException(
                status_code=409,
                detail={"code": 6, "message": "锁刚被别的进程接管，已停止释放；请刷新后再看"},
            )
        return {
            "released": True,
            "message": "已释放僵死锁，下一次 GPU 任务可以直接抢锁",
            "lock": lock.inspect(),
        }

    @app.get("/api/ui/readiness")
    def ui_readiness() -> dict:
        """配音链是否可开工：任一已启用 provider 可用才算就绪。

        模型缺失时不静默降级，直接把原因回给界面，由界面禁用「开始」。
        """
        config = load_config(config_path)
        service = TtsService(config, config.project_root or None)
        try:
            rows = service.doctor()
        except Exception as error:  # noqa: BLE001 - 读取失败与「没有可用模型」分开回报，便于排查
            return {"ready": False, "reason": f"读取 provider 状态失败：[FAILED] {error}", "providers": []}
        finally:
            service.close()
        usable = [row for row in rows if row.get("enabled") and row.get("available")]
        if usable:
            return {"ready": True, "reason": "", "providers": rows}
        blocked = [row for row in rows if row.get("enabled")]
        if blocked:
            reason = "；".join(
                f"{row['provider_id']} 不可用（{row.get('code') or '无原因'}：{row.get('detail')}）" for row in blocked
            )
        else:
            reason = "配置里没有启用的 TTS provider（检查 config 的 tts.providers）"
        return {"ready": False, "reason": f"{reason}。先运行 ndw models list / verify 确认权重就位", "providers": rows}

    @app.post("/api/ui/generate")
    def ui_generate(body: dict) -> dict:
        """入队并启动后台消费进程；请求立即返回，进度一律靠 `queue list` 反映。

        消费放在独立进程里（`tts run --wait`），避免阻塞请求线程，
        也避免在请求线程里重定向 stdout 与其它请求相互干扰。
        """
        _require(
            body,
            "本接口需要 path（工程目录）与 chapter（章节 ID）",
            path="工程目录",
            chapter="章节 ID",
        )
        path = body["path"]
        chapter = body["chapter"]
        # regenerate：已就绪行也重新合成（绕过同键去重与产物缓存）；continue：只补未生成/过期/失败
        mode = "regenerate" if str(body.get("mode") or "") == "regenerate" else "continue"
        # 前置检查：权重缺失/不可用时直接拒绝，不静默降级，也不启动一个必然失败的后台进程
        ready = ui_readiness()
        if not ready["ready"]:
            raise HTTPException(status_code=412, detail={"code": 4, "message": ready["reason"]})
        store = ProjectStore(path)
        jobs = use_cases.call(lambda: queue_jobs(store))
        active = [job for job in jobs if job.get("status") in ("queued", "running")]
        if active:
            # 接管已在执行的一批：since 取这批任务里最早的创建时间，避免把历史任务算进本次进度
            since = min((job.get("created_at") or "") for job in active) or now_iso()
            return {"started": False, "active": len(active), "since": since}

        store = ProjectStore(path)
        # 主动启动意味着「继续跑」：清掉暂停标记，避免新批次刚开始就卡住
        (store.root / ".ndw" / "tts.paused").unlink(missing_ok=True)
        QueueStore(store).set_paused(False)
        # pid 文件显示已有进程活着时直接拒绝，避免重复启动第二个批次
        pid_file = store.root / ".ndw" / "web-tts.pid"
        if pid_file.is_file():
            try:
                old_pid = int(pid_file.read_text(encoding="utf-8").strip())
            except ValueError:
                old_pid = 0
            if old_pid and _is_tts_process(old_pid):
                return {
                    "started": False,
                    "active": 0,
                    "since": now_iso(),
                    "pid": old_pid,
                    "reason": "已有配音进程在运行",
                }
        log_dir = store.root / ".ndw" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        since = now_iso()
        log_path = log_dir / f"web-tts-{since.replace(':', '').replace('-', '')}.log"
        process = spawn_application(
            "novel_drama.application.production.tts_run:run_chapter_entry",
            {
                "project_root": path,
                "config_path": config_path,
                "chapter_id": chapter,
                # regenerate：整章连同已就绪行一起重跑；continue：只补未生成/过期/失败
                "scope": "chapter" if mode == "regenerate" else None,
                "force": mode == "regenerate",
                "wait": True,
            },
            log_path,
        )
        # 记录后台进程 pid，供「停止生成」终止（进程组由 start_new_session 独立出来）
        pid_file = store.root / ".ndw" / "web-tts.pid"
        pid_file.write_text(str(process.pid), encoding="utf-8")
        _ACTIVE_TTS_PROJECTS.add(str(store.root))
        return {"started": True, "mode": mode, "pid": process.pid, "log": str(log_path), "since": since}

    @app.post("/api/ui/stop")
    def ui_stop(body: dict) -> dict:
        """停止配音生成：终止后台消费进程，并把未完成任务标记为已取消。

        参数:
            body: {path 工程目录}。

        返回:
            {stopped 是否确实停下, killed 是否终止了进程, canceled 被取消的任务数}。
        """
        _require(body, "本接口需要 path（工程目录）", path="工程目录")
        store = ProjectStore(body["path"])
        result = _stop_tts_project(body["path"])
        _ACTIVE_TTS_PROJECTS.discard(str(store.root))
        # 停止优先于暂停：清掉两个标记，避免下次启动直接卡在暂停态
        (store.root / ".ndw" / "tts.paused").unlink(missing_ok=True)
        QueueStore(store).set_paused(False)
        return result

    @app.post("/api/ui/produce")
    def ui_produce(body: dict) -> dict:
        """启动全链路编排（后台进程，进度靠 `run show` 反映）。

        工程级阶段（设定库、大纲）不需要章节；章节级阶段在未指定 chapter 时
        按全书逐章执行，指定 chapter 时只跑该章。

        参数:
            body: path / chapter / from / to / force / keep_going / no_resume。

        返回:
            {started, pid, log}。

        异常:
            HTTPException 400: 缺工程路径、阶段非法、顺序不成立、区间需要章节但工程没有章节。
            HTTPException 404: 指定章节不存在。
        """
        from novel_drama.orchestration.produce import (
            STAGE_LABELS,
            STAGES,
            stages_needing_chapter,
        )

        path = str(body.get("path") or "").strip()
        chapter = str(body.get("chapter") or "").strip()
        from_stage = str(body.get("from") or "setting").strip()
        to_stage = str(body.get("to") or "export").strip()
        if not path:
            raise HTTPException(status_code=400, detail={"code": 2, "message": "缺少工程路径"})
        stage_text = "、".join(f"{key}（{STAGE_LABELS.get(key, key)}）" for key in STAGES)
        if from_stage not in STAGES:
            raise HTTPException(
                status_code=400,
                detail={"code": 2, "message": f"未知的起始阶段「{from_stage}」，可选：{stage_text}"},
            )
        if to_stage not in STAGES:
            raise HTTPException(
                status_code=400,
                detail={"code": 2, "message": f"未知的结束阶段「{to_stage}」，可选：{stage_text}"},
            )
        if STAGES.index(from_stage) > STAGES.index(to_stage):
            raise HTTPException(
                status_code=400,
                detail={
                    "code": 2,
                    "message": (
                        f"阶段区间不成立：「从」{STAGE_LABELS.get(from_stage, from_stage)}"
                        f" 必须排在「到」{STAGE_LABELS.get(to_stage, to_stage)} 之前"
                    ),
                },
            )
        store = ProjectStore(path)
        need_chapters = stages_needing_chapter(from_stage, to_stage)
        if need_chapters and chapter:
            try:
                store.load_chapter(chapter)
            except FileNotFoundError as exc:
                raise HTTPException(status_code=404, detail={"code": 4, "message": str(exc)}) from exc
        elif need_chapters and not store.list_chapters():
            labels = "、".join(STAGE_LABELS.get(stage, stage) for stage in need_chapters)
            raise HTTPException(
                status_code=400,
                detail={
                    "code": 2,
                    "message": (
                        f"所选区间包含需要章节的阶段：{labels}。工程还没有章节："
                        "请先落地章节或导入正文，或只选择工程级阶段（设定库 / 大纲）"
                    ),
                },
            )

        log_dir = store.root / ".ndw" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)

        from novel_drama.orchestration import produce as produce_mod

        previous_run = str((produce_mod.latest_run(store) or {}).get("run_id") or "")
        target = {"chapter_id": chapter} if chapter and need_chapters else {"label": "全书" if need_chapters else "工程级"}
        scope_text = "全书" if need_chapters and not chapter else ("当前章" if chapter else "工程级")
        with _PRODUCE_GUARD:
            if active_jobs("produce"):
                raise HTTPException(
                    status_code=409,
                    detail={"code": 6, "message": "已有编排在运行：等它结束，或到任务队列里确认状态后再启动"},
                )
            job = UiJob(store, "produce", target, "全链路编排")
            job.start(f"已提交编排请求（{from_stage} → {to_stage} · {scope_text}）")

        # 用临时文件占位，避免同一秒内多次启动互相覆盖日志
        log_fd, log_name = tempfile.mkstemp(prefix="web-produce-", suffix=".log", dir=log_dir)
        os.close(log_fd)
        log_path = Path(log_name)
        try:
            process = spawn_application(
                "novel_drama.application.production.produce_run:run_produce_entry",
                {
                    "project_root": path,
                    "config_path": config_path,
                    "chapter_id": chapter if chapter and need_chapters else "",
                    "from_stage": from_stage,
                    "to_stage": to_stage,
                    "resume": not bool(body.get("no_resume")),
                    "force": bool(body.get("force")),
                    "keep_going": bool(body.get("keep_going")),
                },
                log_path,
            )
        except OSError as exc:
            job.fail(f"编排进程启动失败：{exc}")
            raise HTTPException(
                status_code=500, detail={"code": 3, "message": f"编排进程启动失败：{exc}"}
            ) from exc

        job_id = job.job["id"]
        with _PRODUCE_PROCS_LOCK:
            _PRODUCE_PROCS[job_id] = {"process": process, "store": store, "log_path": log_path, "canceled": False}

        def _fail_with_log(hint: str = "") -> None:
            """退出码 + 日志末尾写进任务队列（不允许只报「未产生编排记录」）。"""
            code = process.poll()
            if code is None:
                head = "编排进程异常"
            elif code < 0:
                head = f"编排进程被信号 {-code} 终止（退出码 {code}）"
            else:
                head = f"编排进程退出（退出码 {code}）"
            if hint:
                head += f"：{hint}"
            tail = _log_tail(log_path)
            job.fail(f"{head}\n{tail}" if tail else head)

        def _run_artifacts(run: dict) -> list[str]:
            """汇总各阶段产物路径，写入任务「产物」列。

            参数:
                run: 编排记录。

            返回:
                产物路径列表（忽略空值）。
            """
            rows: list[str] = []
            for stage in run.get("stages") or []:
                for item in stage.get("artifacts") or []:
                    rows.append(str(item.get("path") or "") if isinstance(item, dict) else str(item))
            return [row for row in rows if row]

        def _canceled() -> bool:
            """是否已被用户终止（终止接口先置标记再杀进程）。"""
            with _PRODUCE_PROCS_LOCK:
                return bool((_PRODUCE_PROCS.get(job_id) or {}).get("canceled"))

        def watch_produce() -> None:
            """轮询编排记录，把阶段进度与终态写回任务队列（异常不退出线程）。"""
            deadline = time.time() + 8 * 3600
            waiting = 0
            last_sig = None
            last_change = time.time()
            try:
                while time.time() < deadline:
                    time.sleep(2)
                    if _canceled():
                        return
                    try:
                        run = produce_mod.latest_run(store) or {}
                    except Exception:  # noqa: BLE001 - 记录短暂不可读时等下一轮，不能让 watcher 退出
                        continue
                    stages = run.get("stages") or []
                    total = len(stages)
                    done = sum(1 for item in stages if item.get("status") in ("succeeded", "skipped"))
                    status = str(run.get("status") or "")
                    mine = str(run.get("run_id") or "") not in ("", previous_run)
                    if mine:
                        sig = (str(run.get("run_id") or ""), status, total, done)
                        if sig != last_sig:
                            last_sig, last_change = sig, time.time()
                    quiet = time.time() - last_change

                    if not mine:
                        # 进程已退出且没产生新记录：说明编排根本没启动成功，带日志结尾报错
                        if process.poll() is not None:
                            waiting += 1
                            if waiting >= 2:
                                _fail_with_log("没有写出编排记录，请按下方输出排查")
                                return
                        elif quiet > 600:
                            job.progress("疑似卡住：仍未写出编排记录，可点「终止」", None, force=True)
                        continue

                    job.set_artifacts(_run_artifacts(run))
                    if status in ("running", "queued", "pending", ""):
                        if process.poll() is not None:
                            # 兜底：记录没写终态但进程已退出，按阶段结果判定，不允许停在运行中
                            waiting += 1
                            if waiting < 2:
                                continue
                            failed = next((item for item in stages if item.get("status") == "failed"), None)
                            if failed:
                                detail = failed.get("error") or "编排失败"
                                code = process.poll()
                                if code not in (None, 0):
                                    detail = f"{detail}（进程退出码 {code}）"
                                tail = _log_tail(log_path)
                                job.fail(f"{detail}\n{tail}" if tail else detail)
                            elif total and done == total:
                                job.progress("编排完成", 100, force=True)
                                job.finish(f"进程已退出，按阶段结果判定成功 · {done}/{total} 个阶段")
                            else:
                                _fail_with_log("编排记录停在运行中且未写完所有阶段")
                            return
                        waiting = 0
                        last = next(
                            (item for item in reversed(stages) if item.get("status") not in ("pending", "skipped")),
                            None,
                        )
                        label = (last or {}).get("label") or "准备阶段"
                        # 100% 只留给终态：阶段跑满但任务还在收尾时最多显示 99%
                        percent = min(99, done / total * 100) if total else None
                        if quiet > 600:
                            job.progress(
                                f"{label} · {done}/{total} · 疑似卡住（已 {int(quiet // 60)} 分钟无新记录，可终止）",
                                percent,
                                force=True,
                            )
                        else:
                            job.progress(f"{label} · {done}/{total}", percent)
                        continue

                    waiting = 0
                    if _canceled():
                        return
                    if status == "canceled":
                        job.cancel("编排已取消")
                    elif status in ("succeeded", "success", "done"):
                        job.progress("编排完成", 100, force=True)
                        job.finish(f"编排完成 · {done}/{total} 个阶段")
                    else:
                        failed = next((item for item in stages if item.get("status") == "failed"), None)
                        detail = (failed or {}).get("error") or "编排失败"
                        code = process.poll()
                        if code not in (None, 0):
                            detail = f"{detail}（进程退出码 {code}）"
                        job.fail(detail)
                    return
                job.fail("编排超时未完成")
            finally:
                job.close()
                with _PRODUCE_PROCS_LOCK:
                    _PRODUCE_PROCS.pop(job_id, None)

        threading.Thread(target=watch_produce, name="ndw-job-produce", daemon=True).start()
        return {"started": True, "pid": process.pid, "job_id": job_id, "log": str(log_path)}

    @app.post("/api/ui/jobs/clear")
    def ui_jobs_clear(body: dict) -> dict:
        """清理已结束的 UI 任务行（不删除正文、音频等产物）。

        参数:
            body: {path}。

        返回:
            {removed, message}。

        异常:
            HTTPException 400: 缺工程路径。
        """
        path = str(body.get("path") or "").strip()
        if not path:
            raise HTTPException(status_code=400, detail={"code": 2, "message": "缺少工程路径"})
        removed = clear_finished(ProjectStore(path))
        return {
            "removed": removed,
            "message": f"已清理 {removed} 条已结束任务" if removed else "没有可清理的已结束任务",
        }

    @app.post("/api/ui/produce/cancel")
    def ui_produce_cancel(body: dict) -> dict:
        """终止编排子进程，并把任务与编排记录落成「已取消」。

        参数:
            body: {path, id}。

        返回:
            {canceled, message, exit_code}。

        异常:
            HTTPException 400: 缺工程路径或任务 ID。
            HTTPException 404: 任务不存在。
        """
        path = str(body.get("path") or "").strip()
        job_id = str(body.get("id") or "").strip()
        if not path or not job_id:
            raise HTTPException(status_code=400, detail={"code": 2, "message": "缺少工程路径或任务 ID"})
        return _cancel_produce_job(ProjectStore(path), job_id)

    @app.post("/api/ui/jobs/cancel")
    def ui_jobs_cancel(body: dict) -> dict:
        """停止一条界面任务：生成线程协作取消 / 编排杀子进程 / 僵尸任务强制取消。

        参数:
            body: `{path, id, force?}`；force=True 时不等待生成线程退出，直接落「已取消」。

        返回:
            `{canceled, message, status, forced}`。

        异常:
            HTTPException 400: 缺工程路径或任务 ID。
            HTTPException 404: 任务不存在。
        """
        path = str(body.get("path") or "").strip()
        job_id = str(body.get("id") or "").strip()
        if not path or not job_id:
            raise HTTPException(status_code=400, detail={"code": 2, "message": "缺少工程路径或任务 ID"})
        store = ProjectStore(path)
        current = next((item for item in load_ui_jobs(store) if item.get("id") == job_id), None)
        if current is None:
            raise HTTPException(status_code=404, detail={"code": 4, "message": "任务不存在，可能已被清理"})
        status = str(current.get("status") or "")
        if status in ("succeeded", "failed", "canceled", "interrupted"):
            return {
                "canceled": False,
                "status": status,
                "forced": False,
                "message": f"任务已结束（{status}），无需停止",
            }
        # 编排类任务要杀子进程，交给编排取消逻辑
        if current.get("kind") == "produce":
            result = _cancel_produce_job(store, job_id)
            result.setdefault("forced", False)
            return result

        force = bool(body.get("force"))
        # 能找到取消登记说明本进程确有生成线程；找不到就是僵尸任务
        running = request_cancel(job_id)
        if running and not force:
            # 等生成线程在下一次模型输出 / 落盘前自行退出（最多 5 秒）
            for _ in range(50):
                latest = next((item for item in load_ui_jobs(store) if item.get("id") == job_id), {})
                if latest.get("status") in ("succeeded", "failed", "canceled", "interrupted"):
                    return {
                        "canceled": True,
                        "status": latest.get("status"),
                        "forced": False,
                        "message": "已停止生成，任务标记为已取消；已接收内容与已完成产物保留",
                    }
                time.sleep(0.1)

        latest = next((item for item in load_ui_jobs(store) if item.get("id") == job_id), {})
        latest_status = str(latest.get("status") or "")
        if latest_status in ("succeeded", "failed", "canceled", "interrupted"):
            return {
                "canceled": True,
                "status": latest_status,
                "forced": not running,
                "message": "任务已结束，无需再停止",
            }
        if running:
            reason = "停止请求已发出，生成线程会在下一次模型输出时退出；已先标记为已取消"
            message = "已发出停止请求，任务标记为已取消；后台生成会在当前模型输出结束后退出"
        else:
            reason = "后端已无对应生成进程（僵尸任务），已强制标记为已取消；已接收进度与已有产物保留"
            message = "后端已无对应生成进程，已强制标记为已取消"
        update_job(store, job_id, status="canceled", error=reason)
        return {"canceled": True, "status": "canceled", "forced": not running, "message": message}

    @app.get("/api/ui/produce/latest")
    def ui_produce_latest(path: str = Query(...), run_id: str | None = Query(default=None)) -> dict:
        """最近一次（或指定）编排记录（`run show --json`）。"""
        store = _store(path)
        try:
            record = run_record(store, run_id)
        except (FileNotFoundError, KeyError) as error:
            return {"run": None, "message": f"[NOT_FOUND] {error}"}
        except ValueError as error:
            return {"run": None, "message": f"[USAGE] {error}"}
        if record is None:
            return {"run": None, "message": "[NOT_FOUND] 还没有任何编排记录：先执行 ndw produce"}
        return {"run": record}
