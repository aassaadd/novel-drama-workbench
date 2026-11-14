"""一键补齐全部音色：扫描 → 龙套建池 → 复用绑定 → 缺失生成。

设计要点：
- 状态落盘在 `assets/voice/fill/job.json`，停止或刷新后可继续补齐；
- 打分与龙套池规则复用 `voice_casting.preview` 与 `cast_autofill` 的分组推荐；
- 龙套按「性别:年龄段」建池：池里没音色时先生成 4 条候选并采用第 1 条，
  再写回音色池、stock_defaults 映射与 `assets/voice/casting.json`；
- 已有手动指定的池槽位、映射、旁白人格一律不覆盖；
- 旁白只走固定人格；性别缺失、普通角色缺描述才标「需要人工」；
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
from .cast_autofill import _BAND_SLOT, _GENDER_LABEL, _SLOT_LABEL, _rank_for_group, _usage
from .voice_batch import brief_text

#: 可参与生成的性别；缺失或「未知」时标「需要人工」，不做猜测
_GENDERS = {"male", "female", "other"}

#: 池槽位后缀 → 存储用的 age_feel（与页面默认 6 个槽位保持一致）
_AGE_FEEL = {"young": "young_adult", "mid": "adult", "elder": "elderly"}

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


def _cast_config(store) -> dict:
    """读当前工程的音色编排配置（不存在时返回空 dict）。"""
    project = store.load_project() or {}
    return ((project.get("settings") or {}).get("tts") or {}).get("cast") or {}


def _pool_slot(character: dict) -> tuple[str, str]:
    """按角色性别与年龄段算池槽位与后缀。"""
    band = voice_casting.age_band(character.get("age_range")) or "adult"
    suffix = _BAND_SLOT.get(band, "mid")
    return f"{character.get('gender')}_{suffix}", suffix


def _pool_label(gender: str, suffix: str) -> str:
    """池槽位的中文展示名，例如「中年男声」。"""
    return f"{_SLOT_LABEL.get(suffix, '中年')}{_GENDER_LABEL.get(gender, '')}声"


def _usable_voices(store) -> list[dict]:
    """当前音色库里可用的音色。"""
    return [voice for voice in store.load_voices().get("voices") or [] if voice_casting._usable(voice)]


def _make_pool_item(group: dict, action: str, voice: dict | None, reason: str, label: str) -> dict:
    """构造一个龙套池条目。"""
    return {
        "slot": group["slot"],
        "label": label,
        "group_key": f"{group['gender']}:{voice_casting.age_band(group['characters'][0].get('age_range')) or 'adult'}",
        "gender": group["gender"],
        "suffix": group["suffix"],
        "action": action,
        "status": "ready" if action == "ready" else "pending",
        "voice_id": (voice or {}).get("id"),
        "voice_name": (voice or {}).get("name"),
        "reason": reason,
        "error": None,
        "batch_id": None,
        "candidate_index": None,
        "candidates_done": 0,
        "candidates_total": 0,
        "character_ids": [row.get("id") for row in group["characters"]],
    }


def _make_item(
    character: dict,
    plan: str,
    status: str,
    reason: str,
    voice_id: str | None = None,
    voice_name: str | None = None,
    pool_slot: str | None = None,
) -> dict:
    """构造一个待补齐角色条目（plan 表示计划动作，status 表示执行状态）。"""
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
        "pool_slot": pool_slot,
    }


def _scan_stats(items: list[dict], pool_items: list[dict]) -> dict:
    """汇总扫描结果：角色计数 + 龙套池计数。"""
    stats = {
        "total": len(items),
        "reuse": sum(1 for item in items if item.get("plan") == "reuse"),
        "generate": sum(1 for item in items if item.get("plan") == "generate"),
        "ready": sum(1 for item in items if item.get("plan") == "ready"),
        "manual": sum(1 for item in items if item.get("plan") == "manual"),
        "pool_total": len(pool_items),
        "pool_reuse": sum(1 for item in pool_items if item.get("action") == "reuse"),
        "pool_generate": sum(1 for item in pool_items if item.get("action") == "generate"),
        "pool_ready": sum(1 for item in pool_items if item.get("action") == "ready"),
        "pool_manual": 0,
        "bound": 0,
        "generated": 0,
        "skipped": 0,
        "failed": 0,
        "pools_configured": 0,
        "pools_generated": 0,
        "pools_failed": 0,
    }
    stats["skipped"] = stats["ready"] + stats["manual"]
    return stats


def _refresh_stats(job: dict) -> None:
    """按角色与池子条目的当前状态刷新统计。"""
    items = job.get("items") or []
    pools = job.get("pool_items") or []
    stats = job.setdefault("stats", {})
    stats.update(
        {
            "total": len(items),
            "reuse": sum(1 for item in items if item.get("plan") == "reuse"),
            "generate": sum(1 for item in items if item.get("plan") == "generate"),
            "ready": sum(1 for item in items if item.get("plan") == "ready"),
            "manual": sum(1 for item in items if item.get("plan") == "manual"),
            "pool_total": len(pools),
            "pool_reuse": sum(1 for item in pools if item.get("action") == "reuse"),
            "pool_generate": sum(1 for item in pools if item.get("action") == "generate"),
            "pool_ready": sum(1 for item in pools if item.get("action") == "ready"),
            "bound": sum(1 for item in items if item.get("status") == "bound"),
            "generated": sum(1 for item in items if item.get("status") == "generated"),
            "skipped": sum(1 for item in items if item.get("status") in ("ready", "manual")),
            "failed": sum(1 for item in items if item.get("status") == "failed")
            + sum(1 for item in pools if item.get("status") == "failed"),
            "pools_configured": sum(1 for item in pools if item.get("status") == "configured"),
            "pools_generated": sum(1 for item in pools if item.get("status") == "generated"),
            "pools_failed": sum(1 for item in pools if item.get("status") == "failed"),
        }
    )


def _plan_pools(store, characters: list[dict], groups: dict[str, dict], cast: dict, overwrite: bool = False) -> list[dict]:
    """规划龙套池：已有池保持不动，缺的优先复用，仍缺则生成。

    参数:
        store: 工程。
        characters: 全部人物档案。
        groups: `_pool_slot` 分组结果。
        cast: 当前音色编排配置。
        overwrite: True 时已有音色池也重新生成（覆盖原池音色）。

    返回:
        池条目列表（action 为 ready/reuse/generate）。
    """
    voices = _usable_voices(store)
    table = store.load_voices()
    usage = _usage(characters)
    by_slot = {item.get("slot"): item for item in cast.get("voice_pool") or []}
    bound = {row.get("id"): voice_casting.bound_voice(store, row, table) for row in characters}
    pool_items: list[dict] = []
    for slot in sorted(groups):
        group = groups[slot]
        existing = by_slot.get(slot) or {}
        label = str(existing.get("label") or _pool_label(group["gender"], group["suffix"]))
        existing_voice = None
        voice_id = str(existing.get("voice_id") or "")
        if voice_id:
            candidate = find_voice(table, voice_id)
            existing_voice = candidate if candidate and voice_casting._usable(candidate) else None
        if existing_voice and not overwrite:
            pool_items.append(_make_pool_item(group, "ready", existing_voice, "已有音色池，保持不动", label))
            continue
        if overwrite:
            # 重新生成模式：忽略已有池音色与可复用音色，直接为该池生成新音色
            pool_items.append(
                _make_pool_item(group, "generate", None, "重新生成音色池（覆盖已有）", label)
            )
            continue
        first_bound = next((bound[row.get("id")] for row in group["characters"] if bound.get(row.get("id"))), None)
        # 只复用年龄感也匹配的库内音色；年龄不符时宁可为该池生成新音色
        band = voice_casting.age_band(group["characters"][0].get("age_range")) or "adult"
        matched = [voice for voice in voices if voice_casting._voice_age_band(voice) == band]
        ranked, detail = _rank_for_group(matched, group["characters"], usage)
        voice = first_bound or ranked
        if voice:
            reason = "复用同组已绑定音色" if first_bound else f"复用音色库：{detail}"
            pool_items.append(_make_pool_item(group, "reuse", voice, reason, label))
            continue
        pool_items.append(
            _make_pool_item(group, "generate", None, "音色池没有音色，需要生成", label)
        )
    return pool_items


def scan(store, overwrite: bool = False) -> dict:
    """扫描全部角色与龙套分组，产出补齐计划（只读，不落盘、不生成）。

    参数:
        store: 工程。
        overwrite: True 时已有音色的角色 / 龙套池也纳入重新生成计划（覆盖原绑定）。

    返回:
        `{items, pool_items, stats, minor_pool}`；plan 为 ready/reuse/generate/manual。
    """
    characters = store.load_characters().get("characters") or []
    table = store.load_voices()
    cast = _cast_config(store)
    plan = voice_casting.preview(store)
    suggestions = {item.get("character_id"): item for item in plan.get("items") or []}
    groups: dict[str, dict] = {}
    for character in characters:
        if str(character.get("role") or "") != "minor":
            continue
        if character.get("gender") not in ("male", "female"):
            continue
        slot, suffix = _pool_slot(character)
        groups.setdefault(slot, {"slot": slot, "suffix": suffix, "gender": character.get("gender"), "characters": []})[
            "characters"
        ].append(character)
    pool_items = _plan_pools(store, characters, groups, cast, overwrite=overwrite)
    pools_by_slot = {item["slot"]: item for item in pool_items}
    items: list[dict] = []
    for character in characters:
        bound = voice_casting.bound_voice(store, character, table)
        if bound and not (overwrite and str(character.get("role") or "") != "narrator"):
            items.append(
                _make_item(character, "ready", "ready", "已有可用音色，跳过", bound.get("id"), bound.get("name"))
            )
            continue
        role = str(character.get("role") or "")
        if role == "narrator":
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
        if role == "minor":
            slot, _ = _pool_slot(character)
            pool_item = pools_by_slot.get(slot)
            if pool_item:
                if pool_item.get("voice_id"):
                    items.append(
                        _make_item(
                            character,
                            "reuse",
                            "pending",
                            f"复用龙套音色池「{pool_item['label']}」",
                            pool_item.get("voice_id"),
                            pool_item.get("voice_name"),
                            slot,
                        )
                    )
                else:
                    items.append(
                        _make_item(
                            character,
                            "reuse",
                            "pending",
                            f"等待生成龙套音色池「{pool_item['label']}」",
                            None,
                            None,
                            slot,
                        )
                    )
                continue
            if not brief_text(character):
                items.append(_make_item(character, "manual", "manual", "缺少角色描述，需要人工补充"))
                continue
        if overwrite:
            # 重新生成模式：主角 / 配角不再复用已有音色，直接重新生成并覆盖绑定
            items.append(_make_item(character, "generate", "pending", "重新生成音色（覆盖已有）"))
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
    return {
        "items": items,
        "pool_items": pool_items,
        "stats": _scan_stats(items, pool_items),
        "minor_pool": plan.get("minor_pool") or {},
    }


def has_remaining(job: dict | None) -> bool:
    """任务是否还有可自动补齐的剩余角色或龙套池。"""
    if not job:
        return False
    chars = any(
        item.get("plan") in ("reuse", "generate") and item.get("status") in ("pending", "failed")
        for item in job.get("items") or []
    )
    pools = any(
        item.get("action") in ("reuse", "generate") and item.get("status") in ("pending", "failed")
        for item in job.get("pool_items") or []
    )
    return chars or pools


def _finish(job: dict, cancelled: bool) -> dict:
    """收敛任务状态与文案，并刷新最终统计。"""
    if cancelled:
        for item in job.get("items") or []:
            if item.get("status") in ("binding", "generating"):
                item["status"] = "pending"
                item["error"] = "已停止，可继续补齐"
        for item in job.get("pool_items") or []:
            if item.get("status") in ("configuring", "generating"):
                item["status"] = "pending"
                item["error"] = "已停止，可继续补齐"
    _refresh_stats(job)
    pending = [item for item in job.get("items") or [] if item.get("status") == "pending"] + [
        item for item in job.get("pool_items") or [] if item.get("status") == "pending"
    ]
    failed = [item for item in job.get("items") or [] if item.get("status") == "failed"] + [
        item for item in job.get("pool_items") or [] if item.get("status") == "failed"
    ]
    if cancelled:
        job["status"] = "stopped"
        job["note"] = f"已停止，还有 {len(pending)} 项未完成，可点「继续补齐」继续"
    elif failed:
        job["status"] = "partial"
        job["note"] = f"{len(failed)} 项补齐失败，可单独重试"
    elif pending:
        job["status"] = "stopped"
        job["note"] = f"还有 {len(pending)} 项未完成，可点「继续补齐」继续"
    else:
        job["status"] = "ready"
        job["note"] = "全部角色与龙套音色池已补齐"
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
    for item in job.get("pool_items") or []:
        if item.get("status") in ("configuring", "generating"):
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


def _start_thread(store, cfg: AppConfig, job: dict, char_targets: set[str] | None, pool_targets: set[str] | None) -> None:
    """为任务启动后台线程（targets 为空表示处理全部条目）。"""
    job_id = job["job_id"]
    with _RUNNING_LOCK:
        _RUNNING[job_id] = {"cancel": threading.Event()}
    thread = threading.Thread(target=_run_job, args=(store, cfg, job_id, char_targets, pool_targets), daemon=True)
    with _THREADS_LOCK:
        _THREADS[job_id] = thread
    thread.start()


def start_job(store, cfg: AppConfig, count: int | None = None, overwrite: bool = False) -> dict:
    """创建并启动一键补齐任务。

    参数:
        store: 工程。
        cfg: 本机配置（AppConfig）。
        count: 每个角色/每个池生成的候选数；为空时取 `voice.design.candidates`。
        overwrite: True 时已有音色的角色 / 龙套池也重新生成并覆盖原绑定。

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
    scanned = scan(store, overwrite=overwrite)
    if not any(item.get("plan") in ("reuse", "generate") for item in scanned["items"]) and not any(
        item.get("action") in ("reuse", "generate") for item in scanned["pool_items"]
    ):
        raise ValueError("没有需要补齐的角色或龙套池")
    design = cfg.get("voice.design") or {}
    count = max(1, min(6, int(count or design.get("candidates", 4))))
    job = {
        "job_id": entities.new_id("vfj"),
        "status": "running",
        "stage": "scan",
        "count": count,
        "overwrite": bool(overwrite),
        "current": None,
        "progress": None,
        "items": scanned["items"],
        "pool_items": scanned["pool_items"],
        "stats": scanned["stats"],
        "note": None,
        "created_at": _now(),
        "updated_at": _now(),
    }
    _record_undo(store, job)
    _write_job(store, job)
    _start_thread(store, cfg, job, None, None)
    return job


def resume_job(
    store,
    cfg: AppConfig,
    character_ids: list[str] | None = None,
    pool_slots: list[str] | None = None,
) -> dict:
    """继续补齐：只处理未完成/失败的角色或龙套池。

    参数:
        store: 工程。
        cfg: 本机配置（AppConfig）。
        character_ids: 只补齐这些角色；为空时处理全部 pending/failed 项。
        pool_slots: 只重试这些龙套池槽位。

    返回:
        更新后的任务元数据。

    异常:
        ValueError: 配置类型错误、任务不存在、仍在运行或没有可继续补齐的项。
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
    wanted_chars = {str(item) for item in (character_ids or []) if item}
    wanted_pools = {str(item) for item in (pool_slots or []) if item}
    if wanted_chars:
        refreshed = {item.get("character_id"): item for item in scan(store)["items"]}
        for item in job.get("items") or []:
            if item.get("character_id") not in wanted_chars:
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
        for item in job.get("items") or []:
            if item.get("character_id") in wanted_chars and item.get("pool_slot"):
                wanted_pools.add(str(item["pool_slot"]))
    if wanted_chars or wanted_pools:
        char_targets = [
            item
            for item in job.get("items") or []
            if (not wanted_chars or item.get("character_id") in wanted_chars)
            and item.get("plan") in ("reuse", "generate")
            and (not wanted_pools or not item.get("pool_slot") or item.get("pool_slot") in wanted_pools)
        ]
        pool_targets = [
            item
            for item in job.get("pool_items") or []
            if item.get("slot") in wanted_pools and item.get("action") in ("reuse", "generate")
        ]
    else:
        char_targets = [
            item
            for item in job.get("items") or []
            if item.get("plan") in ("reuse", "generate") and item.get("status") in ("pending", "failed")
        ]
        pool_targets = [
            item
            for item in job.get("pool_items") or []
            if item.get("action") in ("reuse", "generate") and item.get("status") in ("pending", "failed")
        ]
    if not char_targets and not pool_targets:
        raise ValueError("没有需要继续补齐的角色或龙套池")
    for item in char_targets:
        item["status"] = "pending"
        item["error"] = None
    for item in pool_targets:
        item["status"] = "pending"
        item["error"] = None
    job["status"] = "running"
    job["stage"] = "scan"
    job["note"] = None
    job["current"] = None
    job["progress"] = None
    _refresh_stats(job)
    _write_job(store, job)
    _start_thread(
        store,
        cfg,
        job,
        {item["character_id"] for item in char_targets},
        {item["slot"] for item in pool_targets},
    )
    return job


def cancel_job(store) -> dict:
    """停止补齐：当前项停止，已完成的结果保留。

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
    current_pool = next(
        (item for item in job.get("pool_items") or [] if item.get("slot") == job.get("current")),
        None,
    )
    if current_pool and current_pool.get("batch_id"):
        try:
            voice_design.cancel_batch(store, current_pool["batch_id"])
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


def _run_job(
    store,
    cfg: AppConfig,
    job_id: str,
    char_targets: set[str] | None,
    pool_targets: set[str] | None,
) -> None:
    """任务主流程：先建龙套池，再复用绑定，最后逐角色生成。"""
    job = _read_job(store)
    with _RUNNING_LOCK:
        state = _RUNNING.setdefault(job_id, {"cancel": threading.Event()})
    cancel = state["cancel"]
    try:
        _publish(job_id, {"type": "scan", "stats": job.get("stats") or {}, "count": job.get("count")})
        pool_items = [
            item
            for item in job.get("pool_items") or []
            if item.get("action") in ("reuse", "generate")
            and item.get("status") in ("pending", "failed")
            and (pool_targets is None or item.get("slot") in pool_targets)
        ]
        for position, item in enumerate(pool_items, start=1):
            if cancel.is_set():
                break
            _run_pool(store, cfg, job, item, position, len(pool_items), int(job.get("count") or 4), cancel)
            _refresh_stats(job)
            _write_job(store, job)
            _publish(job_id, {"type": "pool", "pool": item})
        reuse_items = [
            item
            for item in job.get("items") or []
            if item.get("plan") == "reuse"
            and item.get("status") == "pending"
            and item.get("voice_id")
            and (char_targets is None or item.get("character_id") in char_targets)
        ]
        pool_changed = False
        pool = voice_casting.load_minor_pool(store)
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
            and (char_targets is None or item.get("character_id") in char_targets)
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


def _configure_pool(store, pool_item: dict, voice: dict) -> None:
    """把池音色写回音色池、stock_defaults 映射与 casting.json（只补空值）。

    参数:
        store: 工程。
        pool_item: 池条目。
        voice: 选定的音色。
    """
    project = store.load_project() or {}
    settings = project.setdefault("settings", {})
    tts = settings.setdefault("tts", {})
    cast = tts.setdefault("cast", {})
    pool = [dict(item) for item in cast.get("voice_pool") or []]
    slot = pool_item["slot"]
    existing = next((item for item in pool if item.get("slot") == slot), None)
    if existing is None:
        pool.append(
            {
                "slot": slot,
                "label": pool_item["label"],
                "gender": pool_item["gender"],
                "age_feel": _AGE_FEEL.get(pool_item.get("suffix"), "adult"),
                "voice_id": voice.get("id"),
            }
        )
    elif not existing.get("voice_id"):
        existing["voice_id"] = voice.get("id")
        existing.setdefault("label", pool_item["label"])
        existing.setdefault("gender", pool_item["gender"])
        existing.setdefault("age_feel", _AGE_FEEL.get(pool_item.get("suffix"), "adult"))
    cast["voice_pool"] = pool
    defaults = dict(cast.get("stock_defaults") or {})
    keys = {slot}
    for character_id in pool_item.get("character_ids") or []:
        character = _find_character(store, character_id)
        stock = (character or {}).get("stock") or {}
        for key in (stock.get("slot"), stock.get("voice_slot")):
            if key:
                keys.add(str(key))
    for key in keys:
        if not defaults.get(key):
            defaults[key] = voice.get("id")
    cast["stock_defaults"] = defaults
    tts["cast"] = cast
    store.save_project(project)
    minor_pool = voice_casting.load_minor_pool(store)
    for character_id in pool_item.get("character_ids") or []:
        character = _find_character(store, character_id)
        if not character:
            continue
        key = voice_casting.pool_key(character)
        if not minor_pool.get(key):
            minor_pool[key] = voice.get("id")
    voice_casting.save_minor_pool(store, minor_pool)


def _sync_pool_characters(store, job: dict, pool_item: dict, voice: dict | None, error: str | None = None) -> None:
    """把池结果同步到同组角色条目（不覆盖已绑定角色）。"""
    for item in job.get("items") or []:
        if item.get("pool_slot") != pool_item["slot"]:
            continue
        if item.get("status") in ("ready", "bound", "generated"):
            continue
        if voice:
            item["plan"] = "reuse"
            item["status"] = "pending"
            item["voice_id"] = voice.get("id")
            item["voice_name"] = voice.get("name")
            item["reason"] = f"复用龙套音色池「{pool_item['label']}」"
            item["error"] = None
        else:
            item["status"] = "pending"
            item["error"] = error or "等待龙套音色池生成"
        _publish(job["job_id"], {"type": "item", "item": item})


def _existing_pool_voice(store, slot: str) -> dict | None:
    """取池槽位当前的可用音色（用于避免覆盖手动指定）。"""
    cast = _cast_config(store)
    for item in cast.get("voice_pool") or []:
        if item.get("slot") != slot:
            continue
        if not item.get("voice_id"):
            return None
        voice = find_voice(store.load_voices(), str(item["voice_id"]))
        return voice if voice and voice_casting._usable(voice) else None
    return None


def _run_pool(
    store,
    cfg: AppConfig,
    job: dict,
    item: dict,
    position: int,
    total: int,
    count: int,
    cancel: threading.Event,
) -> None:
    """处理一个龙套池：已有音色直接配置，缺的优先复用，仍缺则生成。"""
    job["stage"] = "pool"
    job["current"] = item["slot"]
    if item.get("action") == "reuse":
        progress = {
            "type": "progress",
            "stage": "pool",
            "action": "reuse",
            "index": position,
            "total": total,
            "name": item["label"],
        }
        job["progress"] = progress
        item["status"] = "configuring"
        _publish(job["job_id"], progress)
        _write_job(store, job)
        voice = find_voice(store.load_voices(), str(item.get("voice_id") or ""))
        if not voice or not voice_casting._usable(voice):
            voices = _usable_voices(store)
            characters = [row for row in store.load_characters().get("characters") or [] if row.get("id") in item["character_ids"]]
            ranked, _ = _rank_for_group(voices, characters, _usage(store.load_characters().get("characters") or []))
            voice = ranked
        if not voice:
            item["status"] = "failed"
            item["error"] = "音色库没有可用音色，请补充音色库"
            _sync_pool_characters(store, job, item, None, "龙套音色池配置失败")
            return
        _configure_pool(store, item, voice)
        item["status"] = "configured"
        item["voice_id"] = voice.get("id")
        item["voice_name"] = voice.get("name")
        item["error"] = None
        _sync_pool_characters(store, job, item, voice)
        return
    # 生成前再确认一次池槽位：可能已被手动补齐，保持不动；重新生成模式除外
    existing = None if job.get("overwrite") else _existing_pool_voice(store, item["slot"])
    if existing:
        item["action"] = "reuse"
        item["voice_id"] = existing.get("id")
        item["voice_name"] = existing.get("name")
        _configure_pool(store, item, existing)
        item["status"] = "configured"
        _sync_pool_characters(store, job, item, existing)
        return
    characters = [
        row for row in store.load_characters().get("characters") or [] if row.get("id") in (item.get("character_ids") or [])
    ]
    if not characters:
        item["status"] = "failed"
        item["error"] = "龙套池没有可用角色档案"
        return
    representative = next((row for row in characters if brief_text(row)), characters[0])
    item["status"] = "generating"
    result = _run_batch_voice(
        store,
        cfg,
        job,
        item,
        representative,
        position,
        total,
        count,
        cancel,
        stage="pool",
        name=item["label"],
        bind=False,
    )
    if result.get("cancelled"):
        return
    if result.get("error"):
        item["status"] = "failed"
        item["error"] = result["error"]
        _sync_pool_characters(store, job, item, None, f"龙套音色池生成失败：{result['error']}")
        return
    voice = result.get("voice") or {}
    _configure_pool(store, item, voice)
    item["status"] = "generated"
    item["voice_id"] = voice.get("id")
    item["voice_name"] = voice.get("name")
    item["error"] = None
    _sync_pool_characters(store, job, item, voice)


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


def _emit_voice_progress(
    job: dict,
    item: dict,
    position: int,
    total: int,
    stage: str,
    name: str | None,
    chars: int,
    sub_index: int,
    sub_total: int,
) -> None:
    """广播生成阶段进度（第 k/N 个 · 已接收 X 字）。"""
    progress = {
        "type": "progress",
        "stage": stage,
        "action": "generate",
        "index": position,
        "total": total,
        "name": name or item.get("name"),
        "chars": int(chars or 0),
        "sub_index": int(sub_index or 0),
        "sub_total": int(sub_total or 0),
    }
    job["progress"] = progress
    _publish(job["job_id"], progress)


def _run_batch_voice(
    store,
    cfg: AppConfig,
    job: dict,
    item: dict,
    character: dict,
    position: int,
    total: int,
    count: int,
    cancel: threading.Event,
    stage: str,
    name: str | None = None,
    bind: bool = True,
) -> dict:
    """生成一个音色并采用第 1 条候选。

    参数:
        store: 工程。
        cfg: 本机配置。
        job: 任务（用于广播进度与落盘）。
        item: 角色或池条目。
        character: 用于描述音色的代表角色。
        position/total: 进度序号。
        count: 候选数。
        cancel: 停止事件。
        stage: `generate`（角色）或 `pool`（龙套池）。
        name: 进度里显示的名字。
        bind: 是否把选中音色绑定到代表角色（池为 False）。

    返回:
        `{voice, variant_id}`；停止返回 `{cancelled: True}`；失败返回 `{error}`。
    """
    item["status"] = "generating"
    item["error"] = None
    item["candidates_total"] = count
    _emit_voice_progress(job, item, position, total, stage, name, 0, 0, 0)
    _write_job(store, job)
    try:
        batch_id = item.get("batch_id")
        meta = voice_design.read_batch(store, batch_id) if batch_id else {}
        if meta and meta.get("status") == "running" and not voice_design.is_running(batch_id):
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
                return {"cancelled": True}
            current = voice_design.read_batch(store, batch_id)
            if current.get("status") == "running" and not voice_design.is_running(batch_id):
                voice_design.cancel_batch(store, batch_id)
                current = voice_design.read_batch(store, batch_id)
            done = len(current.get("candidates") or [])
            if done > seen:
                seen = done
                item["candidates_done"] = done
                chars = int(current.get("script_chars") or 0) * done
                _emit_voice_progress(
                    job, item, position, total, stage, name, chars, done, int(current.get("total") or count)
                )
                _write_job(store, job)
            if current.get("status") != "running":
                meta = current
                break
            time.sleep(0.2)
        candidates = sorted(meta.get("candidates") or [], key=lambda row: int(row.get("index") or 0))
        item["candidates_done"] = len(candidates)
        if not candidates:
            return {"error": meta.get("error") or meta.get("note") or "候选生成失败"}
        gender = character.get("gender") if character.get("gender") in _GENDERS else "unknown"
        try:
            result = voice_design.select_candidate(
                store,
                batch_id,
                int(candidates[0]["index"]),
                str(name or character.get("name") or "生成音色"),
                gender,
                "character",
                bind,
                record_undo=False,
            )
        except Exception as exc:  # noqa: BLE001 - 采用失败只影响该角色
            return {"error": f"已生成候选，但自动采用失败：{exc}"}
        item["candidate_index"] = int(candidates[0]["index"])
        return {"voice": result.get("voice") or {}, "variant_id": result.get("variant_id")}
    except Exception as exc:  # noqa: BLE001 - 单角色生成失败不中断整批
        return {"error": str(exc) or "生成失败"}


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
    # 重新生成模式忽略已绑定音色，强制走模型生成并覆盖绑定
    bound = None if job.get("overwrite") else voice_casting.bound_voice(store, character)
    if bound:
        item["status"] = "ready"
        item["voice_id"] = bound.get("id")
        item["voice_name"] = bound.get("name")
        item["error"] = None
        return
    if item.get("voice_id"):
        item["status"] = "pending"
        return
    result = _run_batch_voice(
        store, cfg, job, item, character, position, total, count, cancel, stage="generate", bind=True
    )
    if result.get("cancelled"):
        return
    if result.get("error"):
        item["status"] = "failed"
        item["error"] = result["error"]
        return
    voice = result.get("voice") or {}
    item["status"] = "generated"
    item["voice_id"] = voice.get("id")
    item["voice_name"] = voice.get("name")
    item["variant_id"] = result.get("variant_id")
    item["error"] = None


def job_events(store):
    """产出补齐任务事件：先回放快照，再转实时广播。

    参数:
        store: 工程。

    产出:
        `(event_name, payload)`；event_name 为 scan/item/pool/progress/ping/finished。

    异常:
        ValueError: 任务不存在。
    """
    job = read_job(store)
    if not job:
        raise ValueError("补齐任务不存在")
    job_id = str(job["job_id"])
    channel = _subscribe(job_id)
    try:
        # 订阅后重新读盘：任务可能已推进到建池/生成阶段，避免漏掉最新进度
        job = _read_job(store) or job
        yield ("scan", {"stats": job.get("stats") or {}, "count": job.get("count")})
        for item in job.get("items") or []:
            yield ("item", item)
        for item in job.get("pool_items") or []:
            yield ("pool", item)
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
            if kind in ("scan", "progress", "item", "pool", "status"):
                yield (kind, event)
    finally:
        _unsubscribe(job_id, channel)
