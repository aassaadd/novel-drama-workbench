"""一键补齐全部音色：扫描 → 复用绑定 → 缺失的生成并采用第 1 条。

设计要点：
- 状态落盘在 `assets/voice/fill/job.json`，停止或刷新后可继续补齐；
- 打分与龙套池规则直接复用 `voice_casting.preview`，不另写一套；
- 旁白只走固定人格，不参与随机生成；性别缺失或档案不足的角色标「需要人工」；
- 生成复用 `voice_design` 的候选批次与选中落盘；
- 事件通过进程内队列广播：SSE 断线只退订，不取消任务（停止请调用 cancel）。
"""

from __future__ import annotations

import json
import queue
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from ..config import AppConfig
from ..models import entities
from ..store.voice_store import find_voice
from . import voice_casting, voice_design
from .voice_batch import brief_text

#: 可参与生成的性别；缺失或「未知」时标「需要人工」，不做猜测
_GENDERS = {"male", "female", "other"}

#: 当前运行中的任务：job_id -> {"cancel": Event}
_RUNNING: dict[str, dict] = {}
_RUNNING_LOCK = threading.Lock()

#: 任务线程
_THREADS: dict[str, threading.Thread] = {}
_THREADS_LOCK = threading.Lock()

#: SSE 订阅者：job_id -> 队列集合
_SUBSCRIBERS: dict[str, set[queue.Queue]] = {}
_SUB_LOCK = threading.Lock()


def _now() -> str:
    """当前 UTC 时间（ISO 8601）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _job_file(store) -> Path:
    """任务状态文件（相对工程，可整体迁移）。"""
    return store.voice_asset_dir("fill") / "job.json"


def _read_job(store) -> dict:
    """读任务 JSON；不存在或损坏时返回空 dict。"""
    try:
        return json.loads(_job_file(store).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_job(store, job: dict) -> None:
    """原子写任务 JSON（先写临时名再替换）。"""
    target = _job_file(store)
    target.parent.mkdir(parents=True, exist_ok=True)
    job["updated_at"] = _now()
    tmp = target.with_name(f"job.json.tmp-{id(job)}")
    tmp.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(target)


def _subscribe(job_id: str) -> queue.Queue:
    """订阅任务事件，返回该连接专属队列。"""
    channel: queue.Queue = queue.Queue()
    with _SUB_LOCK:
        _SUBSCRIBERS.setdefault(job_id, set()).add(channel)
    return channel


def _unsubscribe(job_id: str, channel: queue.Queue) -> None:
    """退订任务事件。"""
    with _SUB_LOCK:
        items = _SUBSCRIBERS.get(job_id)
        if not items:
            return
        items.discard(channel)
        if not items:
            _SUBSCRIBERS.pop(job_id, None)


def _publish(job_id: str, event: dict) -> None:
    """向该任务的所有订阅者广播事件（无订阅者时丢弃）。"""
    with _SUB_LOCK:
        items = list(_SUBSCRIBERS.get(job_id) or ())
    for channel in items:
        channel.put(event)


def _require_config(cfg) -> None:
    """校验配置对象类型：只接受 AppConfig，传错时给出可读错误。"""
    if not isinstance(cfg, AppConfig):
        raise ValueError(
            f"配置对象类型不正确：需要 AppConfig，收到 {type(cfg).__name__}；"
            "请传 load_config() 的返回值，不要传 dict"
        )


def _find_character(store, character_id: str | None) -> dict | None:
    """按 ID 取人物档案；不存在时返回 None。"""
    return next(
        (row for row in store.load_characters().get("characters") or [] if row.get("id") == character_id),
        None,
    )


def _make_item(
    character: dict,
    plan: str,
    status: str,
    reason: str,
    voice_id: str | None = None,
    voice_name: str | None = None,
) -> dict:
    """构造一个待补齐条目（plan 表示计划动作，status 表示执行状态）。"""
    return {
        "character_id": character.get("id"),
        "name": character.get("name"),
        "role": character.get("role"),
        "plan": plan,
        "status": status,
        "voice_id": voice_id,
        "voice_name": voice_name,
        "reason": reason,
        "error": None,
        "batch_id": None,
        "candidate_index": None,
        "candidates_done": 0,
        "candidates_total": 0,
        "pool_key": voice_casting.pool_key(character) if character.get("role") == "minor" else None,
    }


def _scan_stats(items: list[dict]) -> dict:
    """汇总扫描结果：总数、可复用、需生成、已就绪、需要人工。"""
    stats = {
        "total": len(items),
        "reuse": sum(1 for item in items if item.get("plan") == "reuse"),
        "generate": sum(1 for item in items if item.get("plan") == "generate"),
        "ready": sum(1 for item in items if item.get("plan") == "ready"),
        "manual": sum(1 for item in items if item.get("plan") == "manual"),
        "bound": 0,
        "generated": 0,
        "skipped": 0,
        "failed": 0,
    }
    stats["skipped"] = stats["ready"] + stats["manual"]
    return stats


def _refresh_stats(job: dict) -> None:
    """按条目当前状态刷新统计（扫描计数保持不变）。"""
    items = job.get("items") or []
    stats = job.setdefault("stats", {})
    stats.update(
        {
            "total": len(items),
            "reuse": sum(1 for item in items if item.get("plan") == "reuse"),
            "generate": sum(1 for item in items if item.get("plan") == "generate"),
            "ready": sum(1 for item in items if item.get("plan") == "ready"),
            "manual": sum(1 for item in items if item.get("plan") == "manual"),
            "bound": sum(1 for item in items if item.get("status") == "bound"),
            "generated": sum(1 for item in items if item.get("status") == "generated"),
            "skipped": sum(1 for item in items if item.get("status") in ("ready", "manual")),
            "failed": sum(1 for item in items if item.get("status") == "failed"),
        }
    )


def scan(store) -> dict:
    """扫描全部角色，产出补齐计划（只读，不落盘、不生成）。

    参数:
        store: 工程。

    返回:
        `{items, stats, minor_pool}`；plan 为 ready/reuse/generate/manual。
    """
    characters = store.load_characters().get("characters") or []
    table = store.load_voices()
    plan = voice_casting.preview(store)
    suggestions = {item.get("character_id"): item for item in plan.get("items") or []}
    items: list[dict] = []
    for character in characters:
        bound = voice_casting.bound_voice(store, character, table)
        if bound:
            items.append(
                _make_item(character, "ready", "ready", "已有可用音色，跳过", bound.get("id"), bound.get("name"))
            )
            continue
        if str(character.get("role") or "") == "narrator":
            suggestion = suggestions.get(character.get("id"))
            if suggestion:
                items.append(
                    _make_item(
                        character,
                        "reuse",
                        "pending",
                        suggestion.get("reason") or "旁白固定人格",
                        suggestion.get("voice_id"),
                        suggestion.get("voice_name"),
                    )
                )
            else:
                items.append(_make_item(character, "manual", "manual", "旁白未指定固定人格或固定音色不可用，需要人工"))
            continue
        if character.get("gender") not in _GENDERS:
            items.append(_make_item(character, "manual", "manual", "缺少性别，需要人工补充"))
            continue
        suggestion = suggestions.get(character.get("id"))
        if suggestion:
            items.append(
                _make_item(
                    character,
                    "reuse",
                    "pending",
                    suggestion.get("reason") or "音色库有可复用音色",
                    suggestion.get("voice_id"),
                    suggestion.get("voice_name"),
                )
            )
            continue
        if not brief_text(character):
            items.append(_make_item(character, "manual", "manual", "缺少角色描述，需要人工补充"))
            continue
        items.append(_make_item(character, "generate", "pending", "音色库没有合适音色，需要生成"))
    return {"items": items, "stats": _scan_stats(items), "minor_pool": plan.get("minor_pool") or {}}


def has_remaining(job: dict | None) -> bool:
    """任务是否还有可自动补齐的剩余角色。"""
    if not job:
        return False
    return any(
        item.get("plan") in ("reuse", "generate") and item.get("status") in ("pending", "failed")
        for item in job.get("items") or []
    )


def _finish(job: dict, cancelled: bool) -> dict:
    """收敛任务状态与文案，并刷新最终统计。"""
    if cancelled:
        # 被中断时把运行中的条目退回 pending，保证下次能续跑
        for item in job.get("items") or []:
            if item.get("status") in ("binding", "generating"):
                item["status"] = "pending"
                item["error"] = "已停止，可继续补齐"
    _refresh_stats(job)
    pending = [item for item in job.get("items") or [] if item.get("status") == "pending"]
    failed = [item for item in job.get("items") or [] if item.get("status") == "failed"]
    if cancelled:
        job["status"] = "stopped"
        job["note"] = f"已停止，还有 {len(pending)} 个角色未补齐，可点「继续补齐」继续"
    elif failed:
        job["status"] = "partial"
        job["note"] = f"{len(failed)} 个角色补齐失败，可单独重试"
    elif pending:
        job["status"] = "stopped"
        job["note"] = f"还有 {len(pending)} 个角色未补齐，可点「继续补齐」继续"
    else:
        job["status"] = "ready"
        job["note"] = "全部需要音色的角色已补齐"
    job["stage"] = "done"
    job["current"] = None
    job["progress"] = None
    return job


def _recover_stale(store, job: dict) -> dict:
    """服务重启后把残留的运行态收敛为可续跑的 stopped。"""
    job_id = str(job.get("job_id") or "")
    with _RUNNING_LOCK:
        active = job_id in _RUNNING
    if active:
        return job
    changed = False
    for item in job.get("items") or []:
        if item.get("status") in ("binding", "generating"):
            item["status"] = "pending"
            item["error"] = "服务重启导致中断，可继续补齐"
            changed = True
    if job.get("status") == "running":
        job = _finish(job, True)
        changed = True
    if changed:
        _write_job(store, job)
    return job


def read_job(store) -> dict:
    """读取补齐任务（含服务重启后的状态收敛）。"""
    job = _read_job(store)
    if not job:
        return {}
    return _recover_stale(store, job)


def state(store) -> dict:
    """补齐面板所需的当前状态。

    返回:
        `{job, has_remaining}`；没有任务时 job 为 None。
    """
    job = read_job(store)
    return {"job": job or None, "has_remaining": has_remaining(job)}


def _record_undo(store, job: dict) -> None:
    """记录整批补齐前的绑定快照，供撤销最近一次生成使用。"""
    try:
        ids = {item.get("character_id") for item in job.get("items") or [] if item.get("plan") in ("reuse", "generate")}
        characters = [row for row in store.load_characters().get("characters") or [] if row.get("id") in ids]
        if characters:
            voice_casting.record_batch_undo(store, characters)
    except Exception:  # noqa: BLE001 - 撤销日志失败不阻断补齐
        pass


def _start_thread(store, cfg: AppConfig, job: dict, targets: set[str] | None) -> None:
    """为任务启动后台线程（targets 为空表示处理全部条目）。"""
    job_id = job["job_id"]
    with _RUNNING_LOCK:
        _RUNNING[job_id] = {"cancel": threading.Event()}
    thread = threading.Thread(target=_run_job, args=(store, cfg, job_id, targets), daemon=True)
    with _THREADS_LOCK:
        _THREADS[job_id] = thread
    thread.start()


def start_job(store, cfg: AppConfig, count: int | None = None) -> dict:
    """创建并启动一键补齐任务。

    参数:
        store: 工程。
        cfg: 本机配置（AppConfig）。
        count: 每个角色生成候选数；为空时取 `voice.design.candidates`。

    返回:
        任务元数据。

    异常:
        ValueError: 配置类型错误、任务仍在运行或没有可补齐的角色。
    """
    _require_config(cfg)
    existing = _read_job(store)
    existing_id = str(existing.get("job_id") or "")
    with _RUNNING_LOCK:
        if existing_id and existing_id in _RUNNING:
            raise ValueError("补齐任务仍在进行中，请先停止或等待完成")
    scanned = scan(store)
    if not any(item.get("plan") in ("reuse", "generate") for item in scanned["items"]):
        raise ValueError("没有需要补齐音色的角色（均已就绪或需要人工）")
    design = cfg.get("voice.design") or {}
    count = max(1, min(6, int(count or design.get("candidates", 4))))
    job = {
        "job_id": entities.new_id("vfj"),
        "status": "running",
        "stage": "scan",
        "count": count,
        "current": None,
        "progress": None,
        "items": scanned["items"],
        "stats": scanned["stats"],
        "note": None,
        "created_at": _now(),
        "updated_at": _now(),
    }
    _record_undo(store, job)
    _write_job(store, job)
    _start_thread(store, cfg, job, None)
    return job


def resume_job(store, cfg: AppConfig, character_ids: list[str] | None = None) -> dict:
    """继续补齐：只处理未完成/失败的角色，或指定角色重试。

    参数:
        store: 工程。
        cfg: 本机配置（AppConfig）。
        character_ids: 只补齐这些角色；为空时处理全部 pending/failed 项。

    返回:
        更新后的任务元数据。

    异常:
        ValueError: 配置类型错误、任务不存在、仍在运行或没有可继续补齐的角色。
    """
    _require_config(cfg)
    job = _read_job(store)
    if not job:
        raise ValueError("没有可继续补齐的任务")
    job = _recover_stale(store, job)
    job_id = str(job.get("job_id") or "")
    with _RUNNING_LOCK:
        if job_id in _RUNNING:
            raise ValueError("补齐任务仍在进行中，请先停止或等待完成")
    wanted = {str(item) for item in (character_ids or []) if item}
    if wanted:
        # 指定重试时重新扫描这些角色：档案可能已补全，计划会随之变化
        refreshed = {item.get("character_id"): item for item in scan(store)["items"]}
        for item in job.get("items") or []:
            if item.get("character_id") not in wanted:
                continue
            fresh = refreshed.get(item.get("character_id"))
            if not fresh:
                continue
            keep = {
                "batch_id": item.get("batch_id"),
                "candidates_done": item.get("candidates_done"),
                "candidates_total": item.get("candidates_total"),
            }
            item.update(fresh)
            item.update(keep)
        targets = [
            item
            for item in job.get("items") or []
            if item.get("character_id") in wanted and item.get("plan") in ("reuse", "generate")
        ]
    else:
        targets = [
            item
            for item in job.get("items") or []
            if item.get("plan") in ("reuse", "generate") and item.get("status") in ("pending", "failed")
        ]
    if not targets:
        raise ValueError("没有需要继续补齐的角色")
    for item in targets:
        item["status"] = "pending"
        item["error"] = None
    job["status"] = "running"
    job["stage"] = "scan"
    job["note"] = None
    job["current"] = None
    job["progress"] = None
    _refresh_stats(job)
    _write_job(store, job)
    _start_thread(store, cfg, job, {item["character_id"] for item in targets})
    return job


def cancel_job(store) -> dict:
    """停止补齐：当前角色停止，已绑定/已生成的结果保留。

    参数:
        store: 工程。

    返回:
        收敛后的任务元数据。
    """
    job = _read_job(store)
    if not job:
        return {}
    job_id = str(job.get("job_id") or "")
    with _RUNNING_LOCK:
        state = _RUNNING.get(job_id)
        if state:
            state["cancel"].set()
    current = next(
        (item for item in job.get("items") or [] if item.get("character_id") == job.get("current")),
        None,
    )
    if current and current.get("batch_id"):
        try:
            voice_design.cancel_batch(store, current["batch_id"])
        except Exception:  # noqa: BLE001 - 批次已结束时忽略
            pass
    with _THREADS_LOCK:
        thread = _THREADS.get(job_id)
    if thread is not None and thread.is_alive():
        thread.join(timeout=30)
    job = _read_job(store) or job
    if job.get("status") == "running":
        job = _finish(job, True)
        _write_job(store, job)
        _publish(job_id, {"type": "finished", "status": job.get("status"), "job": job, "stats": job.get("stats")})
    return job


def _run_job(store, cfg: AppConfig, job_id: str, targets: set[str] | None) -> None:
    """任务主流程：先复用绑定，再逐个生成，失败不中断。"""
    job = _read_job(store)
    with _RUNNING_LOCK:
        state = _RUNNING.setdefault(job_id, {"cancel": threading.Event()})
    cancel = state["cancel"]
    try:
        _publish(job_id, {"type": "scan", "stats": job.get("stats") or {}, "count": job.get("count")})
        pool = voice_casting.load_minor_pool(store)
        pool_changed = False
        reuse_items = [
            item
            for item in job.get("items") or []
            if item.get("plan") == "reuse"
            and item.get("status") == "pending"
            and (not targets or item.get("character_id") in targets)
        ]
        for position, item in enumerate(reuse_items, start=1):
            if cancel.is_set():
                break
            pool_changed = _run_bind(store, job, item, position, len(reuse_items), pool) or pool_changed
            _refresh_stats(job)
            _write_job(store, job)
            _publish(job_id, {"type": "item", "item": item})
        if pool_changed:
            voice_casting.save_minor_pool(store, pool)
        generate_items = [
            item
            for item in job.get("items") or []
            if item.get("plan") == "generate"
            and item.get("status") in ("pending", "failed")
            and (not targets or item.get("character_id") in targets)
        ]
        for position, item in enumerate(generate_items, start=1):
            if cancel.is_set():
                break
            _run_generate(store, cfg, job, item, position, len(generate_items), int(job.get("count") or 4), cancel)
            _refresh_stats(job)
            _write_job(store, job)
            _publish(job_id, {"type": "item", "item": item})
        job = _finish(job, cancel.is_set())
    except Exception as exc:  # noqa: BLE001 - 任务级异常收敛为失败，不抛到线程外
        job = _finish(job, False)
        job["status"] = "failed"
        job["note"] = f"补齐任务中断：{exc}"
    finally:
        _write_job(store, job)
        with _RUNNING_LOCK:
            _RUNNING.pop(job_id, None)
        with _THREADS_LOCK:
            _THREADS.pop(job_id, None)
        _publish(job_id, {"type": "finished", "status": job.get("status"), "job": job, "stats": job.get("stats")})


def _suggestion_for(store, character_id: str | None) -> dict | None:
    """按现有编排规则重新取一次单角色建议（音色库变化后的兜底）。"""
    plan = voice_casting.preview(store)
    return next(
        (item for item in plan.get("items") or [] if item.get("character_id") == character_id),
        None,
    )


def _run_bind(store, job: dict, item: dict, position: int, total: int, pool: dict) -> bool:
    """绑定一个可复用音色；返回是否改动了龙套池。"""
    job["stage"] = "bind"
    job["current"] = item.get("character_id")
    progress = {
        "type": "progress",
        "stage": "bind",
        "index": position,
        "total": total,
        "name": item.get("name"),
        "voice_name": item.get("voice_name"),
        "role": item.get("role"),
    }
    job["progress"] = progress
    _publish(job["job_id"], progress)
    item["status"] = "binding"
    _write_job(store, job)
    voice = find_voice(store.load_voices(), str(item.get("voice_id") or "")) if item.get("voice_id") else None
    if voice is None:
        suggestion = _suggestion_for(store, item.get("character_id"))
        if suggestion and suggestion.get("voice_id"):
            item["voice_id"] = suggestion.get("voice_id")
            item["voice_name"] = suggestion.get("voice_name")
            item["reason"] = suggestion.get("reason") or item.get("reason")
            voice = find_voice(store.load_voices(), str(suggestion.get("voice_id")))
    if voice is None:
        item["status"] = "failed"
        item["error"] = "音色库没有可用音色，请先补充音色库"
        return False
    try:
        voice_casting.bind_one(store, str(item["character_id"]), str(voice.get("id")), record_undo=False)
    except Exception as exc:  # noqa: BLE001 - 单角色绑定失败只影响该角色
        item["status"] = "failed"
        item["error"] = f"绑定失败：{exc}"
        return False
    item["status"] = "bound"
    item["voice_id"] = voice.get("id")
    item["voice_name"] = voice.get("name")
    item["error"] = None
    if item.get("pool_key"):
        pool[item["pool_key"]] = voice.get("id")
        return True
    return False


def _writing_adapter(cfg: AppConfig, store):
    """构造写作适配器（失败时由 build_profile 回退规则模板）。"""
    try:
        from ..adapters import build_adapters

        return build_adapters(cfg, store.settings()).writing
    except Exception:  # noqa: BLE001 - 适配器不可用不阻断生成
        return None


def _emit_generate(
    job: dict, item: dict, position: int, total: int, chars: int, sub_index: int, sub_total: int
) -> None:
    """广播生成阶段进度（第 k/N 个角色 · 已接收 X 字）。"""
    progress = {
        "type": "progress",
        "stage": "generate",
        "index": position,
        "total": total,
        "name": item.get("name"),
        "chars": int(chars or 0),
        "sub_index": int(sub_index or 0),
        "sub_total": int(sub_total or 0),
    }
    job["progress"] = progress
    _publish(job["job_id"], progress)


def _run_generate(
    store,
    cfg: AppConfig,
    job: dict,
    item: dict,
    position: int,
    total: int,
    count: int,
    cancel: threading.Event,
) -> None:
    """为一个角色生成候选并自动采用第 1 条；失败只影响该角色。"""
    job["stage"] = "generate"
    job["current"] = item.get("character_id")
    character = _find_character(store, item.get("character_id"))
    if character is None:
        item["status"] = "failed"
        item["error"] = "角色已删除"
        return
    bound = voice_casting.bound_voice(store, character)
    if bound:
        item["status"] = "ready"
        item["voice_id"] = bound.get("id")
        item["voice_name"] = bound.get("name")
        item["error"] = None
        return
    item["status"] = "generating"
    item["error"] = None
    item["candidates_total"] = count
    _emit_generate(job, item, position, total, 0, 0, 0)
    _write_job(store, job)
    try:
        batch_id = item.get("batch_id")
        meta = voice_design.read_batch(store, batch_id) if batch_id else {}
        if meta and meta.get("status") == "running" and not voice_design.is_running(batch_id):
            # 服务重启后残留的 running：先按中断收尾，避免无限等待
            voice_design.cancel_batch(store, batch_id)
            meta = voice_design.read_batch(store, batch_id)
        if not meta:
            adapter = _writing_adapter(cfg, store)
            built = voice_design.build_profile(adapter, character)
            meta = voice_design.create_batch(
                store, cfg, character, built["profile"]["design_prompt"], count, built["profile"]
            )
            item["batch_id"] = meta["batch_id"]
            _write_job(store, job)
        elif meta.get("status") != "running":
            done = {int(row.get("index") or 0) for row in meta.get("candidates") or []}
            missing = [index for index in range(1, int(meta.get("total") or count) + 1) if index not in done]
            if missing:
                try:
                    meta = voice_design.resume_batch(store, cfg, batch_id)
                except ValueError:
                    pass
        batch_id = meta["batch_id"]
        item["batch_id"] = batch_id
        seen = 0
        while True:
            if cancel.is_set():
                voice_design.cancel_batch(store, batch_id)
                item["status"] = "pending"
                item["error"] = "已停止，可继续补齐"
                return
            current = voice_design.read_batch(store, batch_id)
            if current.get("status") == "running" and not voice_design.is_running(batch_id):
                voice_design.cancel_batch(store, batch_id)
                current = voice_design.read_batch(store, batch_id)
            done = len(current.get("candidates") or [])
            if done > seen:
                seen = done
                item["candidates_done"] = done
                chars = int(current.get("script_chars") or 0) * done
                _emit_generate(job, item, position, total, chars, done, int(current.get("total") or count))
                _write_job(store, job)
            if current.get("status") != "running":
                meta = current
                break
            time.sleep(0.2)
        candidates = sorted(meta.get("candidates") or [], key=lambda row: int(row.get("index") or 0))
        item["candidates_done"] = len(candidates)
        if not candidates:
            item["status"] = "failed"
            item["error"] = meta.get("error") or meta.get("note") or "候选生成失败"
            return
        gender = character.get("gender") if character.get("gender") in _GENDERS else "unknown"
        try:
            result = voice_design.select_candidate(
                store,
                batch_id,
                int(candidates[0]["index"]),
                str(character.get("name") or "生成音色"),
                gender,
                "character",
                True,
                record_undo=False,
            )
        except Exception as exc:  # noqa: BLE001 - 采用失败只影响该角色
            item["status"] = "failed"
            item["error"] = f"已生成候选，但自动采用失败：{exc}"
            return
        item["status"] = "generated"
        item["candidate_index"] = int(candidates[0]["index"])
        item["voice_id"] = (result.get("voice") or {}).get("id")
        item["voice_name"] = (result.get("voice") or {}).get("name")
        item["variant_id"] = result.get("variant_id")
        item["error"] = None
    except Exception as exc:  # noqa: BLE001 - 单角色生成失败不中断整批
        item["status"] = "failed"
        item["error"] = str(exc) or "生成失败"


def job_events(store):
    """产出补齐任务事件：先回放快照，再转实时广播。

    参数:
        store: 工程。

    产出:
        `(event_name, payload)`；event_name 为 scan/item/progress/ping/finished。

    异常:
        ValueError: 任务不存在。
    """
    job = read_job(store)
    if not job:
        raise ValueError("补齐任务不存在")
    job_id = str(job["job_id"])
    channel = _subscribe(job_id)
    try:
        # 订阅后重新读盘：任务可能已推进到绑定/生成阶段，避免漏掉最新进度
        job = _read_job(store) or job
        yield ("scan", {"stats": job.get("stats") or {}, "count": job.get("count")})
        for item in job.get("items") or []:
            yield ("item", item)
        if job.get("status") != "running":
            yield ("finished", {"status": job.get("status"), "job": job, "stats": job.get("stats") or {}})
            return
        if job.get("progress"):
            yield ("progress", job["progress"])
        while True:
            try:
                event = channel.get(timeout=15)
            except queue.Empty:
                latest = _read_job(store)
                if latest.get("status") != "running":
                    yield (
                        "finished",
                        {"status": latest.get("status"), "job": latest, "stats": latest.get("stats") or {}},
                    )
                    return
                yield ("ping", {})
                continue
            kind = str(event.get("type") or "")
            if kind == "finished":
                yield ("finished", event)
                return
            if kind in ("scan", "progress", "item", "status"):
                yield (kind, event)
    finally:
        _unsubscribe(job_id, channel)
