"""一键批量生成音色：按角色逐个生成候选并自动采用第 1 条。

设计要点：
- 任务落盘在 `assets/voice/batch_jobs/<job_id>/job.json`，页面刷新/中断后可续跑；
- 逐角色串行：一个角色结束（成功或失败）再处理下一个，不并发占 GPU；
- 复用 `voice_design` 的候选批次、SSE 事件与落盘结构，不另造一套；
- 默认跳过已有音色、缺性别、缺角色描述的角色，并在开始前给出统计；
- 中断只停当前角色，已完成角色保留；续跑只补未完成角色，候选批次按缺失序号续生成。
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
from . import voice_design

#: 批量任务：job_id -> {"cancel": Event}
_RUNNING: dict[str, dict] = {}
_RUNNING_LOCK = threading.Lock()

#: 批量任务线程
_THREADS: dict[str, threading.Thread] = {}
_THREADS_LOCK = threading.Lock()

#: SSE 订阅者：job_id -> 队列集合
_SUBSCRIBERS: dict[str, set[queue.Queue]] = {}
_SUB_LOCK = threading.Lock()

#: 允许的性别枚举（「其他」只在角色档案明确为其他时使用）
_GENDERS = {"male", "female", "other"}

#: 角色出现在章节里的判定：场景 character_ids + 台词 speaker_id
_NARRATOR = getattr(entities, "NARRATOR_ID", "chr_narrator")
_UNKNOWN = getattr(entities, "UNKNOWN_SPEAKER_ID", "chr_unknown")


def _now() -> str:
    """当前 UTC 时间（ISO 8601）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _job_dir(store, job_id: str) -> Path:
    """任务目录（相对工程，可整体迁移）。"""
    return store.voice_asset_dir("batch_jobs") / job_id


def _read_job(store, job_id: str) -> dict:
    """读任务 JSON；不存在或损坏时返回空 dict。"""
    try:
        return json.loads((_job_dir(store, job_id) / "job.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_job(store, job: dict) -> None:
    """原子写任务 JSON（先写临时名再替换）。"""
    target = _job_dir(store, job["job_id"])
    target.mkdir(parents=True, exist_ok=True)
    job["updated_at"] = _now()
    tmp = target / f"job.json.tmp-{id(job)}"
    tmp.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(target / "job.json")


def read_job(store, job_id: str) -> dict:
    """读取批量任务元数据（供接口与页面使用）。

    参数:
        store: 工程。
        job_id: 任务 ID。

    返回:
        任务 dict；不存在时为空 dict。
    """
    return _read_job(store, job_id)


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


def _has_voice(character: dict) -> bool:
    """角色是否已绑定音色（任一绑定键存在即视为已有音色）。"""
    binding = character.get("voice_binding") or {}
    return bool(binding.get("voice_id") or binding.get("voice_library_id") or binding.get("default_variant_id"))


def brief_text(character: dict) -> str:
    """按角色档案拼出音色描述文本（与页面带入逻辑一致）。

    参数:
        character: 人物档案。

    返回:
        多行中文描述；没有可用字段时返回空串。
    """
    style = character.get("speech_style") or {}
    details = []
    personality = character.get("personality") or []
    if personality:
        details.append("性格：" + "、".join(str(item) for item in personality))
    if character.get("background"):
        details.append(f"背景：{character.get('background')}")
    if style.get("tempo"):
        details.append(f"语速：{style.get('tempo')}")
    if style.get("pitch"):
        details.append(f"音区：{style.get('pitch')}")
    if style.get("accent"):
        details.append(f"口音：{style.get('accent')}")
    if not details:
        return ""
    head = []
    if character.get("age_range"):
        head.append(f"年龄：{character.get('age_range')}")
    return "\n".join(head + details)


def skip_reason(character: dict) -> str | None:
    """判断角色是否跳过批量生成，返回中文原因。

    参数:
        character: 人物档案。

    返回:
        跳过原因；可生成时返回 None。
    """
    if _has_voice(character):
        return "已有音色"
    if character.get("gender") not in _GENDERS:
        return "缺少性别，请先在人物页补充"
    if not brief_text(character):
        return "缺少角色描述，请先在人物页补充"
    return None


def _chapter_character_ids(store, chapter_id: str) -> set[str]:
    """收集章节里出现的角色 ID（场景引用 + 台词说话人）。

    参数:
        store: 工程。
        chapter_id: 章节 ID。

    返回:
        角色 ID 集合（排除旁白与未知角色）。
    """
    chapter = store.load_chapter(chapter_id)
    ids: set[str] = set()
    for scene in chapter.get("scenes") or []:
        ids.update(str(item) for item in scene.get("character_ids") or [])
        for line in scene.get("lines") or []:
            speaker = line.get("speaker_id")
            if speaker:
                ids.add(str(speaker))
    ids.discard(_NARRATOR)
    ids.discard(_UNKNOWN)
    return ids


def _require_config(cfg) -> None:
    """校验配置对象类型：只接受 AppConfig，传错时给出可读错误。

    参数:
        cfg: 调用方传入的本机配置。

    异常:
        ValueError: 不是 AppConfig（例如误传 dict），提示改用 load_config() 的返回值。
    """
    if not isinstance(cfg, AppConfig):
        raise ValueError(
            f"配置对象类型不正确：需要 AppConfig，收到 {type(cfg).__name__}；"
            "请传 load_config() 的返回值，不要传 dict"
        )


def plan(store, scope: str = "unbound", chapter_id: str | None = None) -> dict:
    """计算批量生成范围与跳过统计（开始前预览）。

    参数:
        store: 工程。
        scope: `unbound`=未绑定音色的角色；`chapter`=指定章节出现的角色。
        chapter_id: `chapter` 范围必填。

    返回:
        `{scope, chapter_id, total, pending, skipped, chapters}`；skipped 含中文原因。

    异常:
        ValueError: 选择章节范围但未给章节。
    """
    characters = store.load_characters()["characters"]
    scope = scope if scope in ("unbound", "chapter") else "unbound"
    allowed: set[str] | None = None
    if scope == "chapter":
        if not chapter_id:
            raise ValueError("请选择章节")
        allowed = _chapter_character_ids(store, chapter_id)
    pending, skipped = [], []
    for character in characters:
        reason = None
        if allowed is not None and character.get("id") not in allowed:
            reason = "未在所选章节出现"
        else:
            reason = skip_reason(character)
        row = {"character_id": character.get("id"), "name": character.get("name"), "reason": reason}
        (skipped if reason else pending).append(row)
    return {
        "scope": scope,
        "chapter_id": chapter_id,
        "total": len(characters),
        "pending": pending,
        "skipped": skipped,
        "chapters": [
            {"id": item.get("id"), "title": item.get("title"), "index": item.get("index")}
            for item in store.list_chapters()
        ],
    }


def start_job(
    store, cfg: AppConfig, scope: str = "unbound", chapter_id: str | None = None, count: int | None = None
) -> dict:
    """创建并启动批量任务（后台线程逐角色串行执行）。

    参数:
        store: 工程。
        cfg: 本机配置（AppConfig，读取 `voice.design.*`）。
        scope: `unbound` 或 `chapter`。
        chapter_id: 章节范围的章节 ID。
        count: 每个角色的候选数；为空时取配置 `voice.design.candidates`。

    返回:
        任务元数据。

    异常:
        ValueError: 配置类型不正确或没有需要生成的角色。
    """
    _require_config(cfg)
    preview = plan(store, scope, chapter_id)
    if not preview["pending"]:
        raise ValueError("没有需要生成音色的角色（均已绑定或缺少档案字段）")
    design = cfg.get("voice.design") or {}
    count = max(1, min(6, int(count or design.get("candidates", 4))))
    job_id = entities.new_id("vbj")
    items = [
        {
            "character_id": row["character_id"],
            "name": row.get("name"),
            "status": "pending",
            "reason": None,
            "batch_id": None,
            "candidate_index": None,
            "voice_id": None,
            "variant_id": None,
            "candidates_done": 0,
            "candidates_total": count,
            "error": None,
        }
        for row in preview["pending"]
    ] + [
        {
            "character_id": row["character_id"],
            "name": row.get("name"),
            "status": "skipped",
            "reason": row.get("reason"),
            "batch_id": None,
            "candidate_index": None,
            "voice_id": None,
            "variant_id": None,
            "candidates_done": 0,
            "candidates_total": 0,
            "error": None,
        }
        for row in preview["skipped"]
    ]
    job = {
        "job_id": job_id,
        "status": "running",
        "scope": scope,
        "chapter_id": chapter_id,
        "count": count,
        "current": None,
        "items": items,
        "note": None,
        "created_at": _now(),
        "updated_at": _now(),
    }
    _refresh_counts(job)
    _write_job(store, job)
    # 整批撤销快照：记录开始前所有待生成角色的绑定，撤销时一次性恢复
    try:
        from . import voice_casting

        pending_ids = {item["character_id"] for item in items if item.get("status") == "pending"}
        pending_chars = [
            row for row in store.load_characters().get("characters") or [] if row.get("id") in pending_ids
        ]
        if pending_chars:
            voice_casting.record_batch_undo(store, pending_chars)
    except Exception:  # noqa: BLE001 - 撤销日志失败不阻断生成
        pass
    with _RUNNING_LOCK:
        _RUNNING[job_id] = {"cancel": threading.Event()}
    thread = threading.Thread(target=_run_job, args=(store, cfg, job_id, count), daemon=True)
    with _THREADS_LOCK:
        _THREADS[job_id] = thread
    thread.start()
    return job


def _refresh_counts(job: dict) -> None:
    """按条目状态刷新任务统计。"""
    items = job.get("items") or []
    job["done"] = sum(1 for item in items if item.get("status") == "ready")
    job["failed"] = sum(1 for item in items if item.get("status") == "failed")
    job["skipped"] = sum(1 for item in items if item.get("status") == "skipped")
    job["pending"] = sum(1 for item in items if item.get("status") in ("pending", "running"))


def _finish_job(job: dict, cancelled: bool) -> dict:
    """收敛任务状态与文案。"""
    if cancelled:
        for item in job.get("items") or []:
            if item.get("status") == "running":
                item["status"] = "pending"
                item["error"] = "已中断，可续跑"
        _refresh_counts(job)
        if not job.get("pending") and not job.get("failed"):
            # 中断时所有角色其实都已完成：按正常完成处理
            job["status"] = "ready"
            job["note"] = f"已为 {job.get('done') or 0} 个角色生成并自动采用第 1 条候选，可在候选里改"
        else:
            job["status"] = "stopped"
            job["note"] = "已停止生成，未完成的角色可续跑"
    else:
        _refresh_counts(job)
        if job.get("failed"):
            ready = job.get("done") or 0
            job["status"] = "partial" if ready else "failed"
            job["note"] = f"{job['failed']} 个角色失败，可单独重试或重试全部失败"
        else:
            job["status"] = "ready"
            job["note"] = f"已为 {job.get('done') or 0} 个角色生成并自动采用第 1 条候选，可在候选里改"
    job["current"] = None
    _refresh_counts(job)
    return job


def _recover_stale(store, job: dict) -> dict:
    """服务重启后收尾残留的 running 任务，避免页面一直等待。"""
    if job.get("status") != "running":
        return job
    with _RUNNING_LOCK:
        active = job.get("job_id") in _RUNNING
    if active:
        return job
    for item in job.get("items") or []:
        if item.get("status") == "running":
            item["status"] = "pending"
            item["error"] = "服务重启导致中断，可续跑"
    job = _finish_job(job, True)
    _write_job(store, job)
    return job


def _run_job(store, cfg: AppConfig, job_id: str, count: int) -> None:
    """逐角色串行执行批量任务，并在每个角色结束后广播进度。"""
    job = _read_job(store, job_id)
    with _RUNNING_LOCK:
        state = _RUNNING.setdefault(job_id, {"cancel": threading.Event()})
    cancel = state["cancel"]
    # 进度分母只算需要生成的角色（skipped 不占序号）
    queue_items = [item for item in job.get("items") or [] if item.get("status") != "skipped"]
    total = len(queue_items)
    try:
        for position, item in enumerate(queue_items, start=1):
            if cancel.is_set():
                break
            if item.get("status") == "ready":
                continue
            job["current"] = item.get("character_id")
            _run_item(store, cfg, job, item, position, total, count, cancel)
            _refresh_counts(job)
            _write_job(store, job)
            _publish(job_id, {"type": "item", "item": item})
        job = _finish_job(job, cancel.is_set())
    except Exception as exc:  # noqa: BLE001 - 任务级异常收敛为失败，不抛到线程外
        job = _finish_job(job, False)
        job["status"] = "failed"
        job["note"] = f"批量生成中断：{exc}"
    finally:
        _write_job(store, job)
        with _RUNNING_LOCK:
            _RUNNING.pop(job_id, None)
        with _THREADS_LOCK:
            _THREADS.pop(job_id, None)
        _publish(job_id, {"type": "finished", "status": job.get("status"), "job": job})


def _run_item(store, cfg: AppConfig, job: dict, item: dict, position: int, total: int, count: int, cancel) -> None:
    """生成单个角色：准备描述 → 候选批次 → 逐条进度 → 采用第 1 条。"""
    job_id = job["job_id"]
    character = next(
        (row for row in store.load_characters()["characters"] if row.get("id") == item.get("character_id")), None
    )
    if character is None:
        item["status"] = "failed"
        item["error"] = "角色已删除"
        return
    reason = skip_reason(character)
    if reason:
        item["status"] = "skipped"
        item["reason"] = reason
        return
    item["status"] = "running"
    item["error"] = None
    _publish(job_id, {"type": "item", "item": item})
    _publish(
        job_id,
        {
            "type": "batch",
            "index": position,
            "total": total,
            "name": character.get("name"),
            "unit": "character",
        },
    )

    batch_id = item.get("batch_id")
    meta = voice_design.read_batch(store, batch_id) if batch_id else {}
    missing = [
        index
        for index in range(1, int((meta or {}).get("total") or count) + 1)
        if index not in {int(row.get("index") or 0) for row in (meta or {}).get("candidates") or []}
    ]
    if meta and meta.get("status") == "running":
        pass  # 上次运行的批次仍在生成：直接接着等
    elif meta and missing:
        try:
            meta = voice_design.resume_batch(store, cfg, batch_id)
        except ValueError:
            meta = {}
    if not meta:
        adapter = None
        try:
            from ..adapters import build_adapters

            adapter = build_adapters(cfg, store.settings()).writing
        except Exception:  # noqa: BLE001 - 适配器不可用时由 build_profile 回退规则模板
            adapter = None
        built = voice_design.build_profile(adapter, character)
        meta = voice_design.create_batch(store, cfg, character, built["profile"]["design_prompt"], count, built["profile"])
        item["batch_id"] = meta["batch_id"]
        item["candidates_total"] = count
        _write_job(store, job)
    batch_id = meta["batch_id"]
    item["batch_id"] = batch_id
    seen = 0
    while True:
        if cancel.is_set():
            voice_design.cancel_batch(store, batch_id)
            item["status"] = "pending"
            item["error"] = "已中断，可续跑"
            return
        current = voice_design.read_batch(store, batch_id)
        if current.get("status") == "running" and not voice_design.is_running(batch_id):
            # 服务重启后残留的 running：先按中断收尾，避免无限等待
            voice_design.cancel_batch(store, batch_id)
            current = voice_design.read_batch(store, batch_id)
        done = len(current.get("candidates") or [])
        if done > seen:
            seen = done
            item["candidates_done"] = done
            script_chars = int(current.get("script_chars") or 0)
            _publish(
                job_id,
                {
                    "type": "chars",
                    "chars": script_chars * done,
                    "sub_index": done,
                    "sub_total": int(current.get("total") or count),
                    "index": position,
                    "total": total,
                    "name": character.get("name"),
                    "unit": "character",
                },
            )
            _write_job(store, job)
        if current.get("status") != "running":
            break
        time.sleep(0.2)
    candidates = sorted(current.get("candidates") or [], key=lambda row: int(row.get("index") or 0))
    item["candidates_done"] = len(candidates)
    if not candidates:
        item["status"] = "failed"
        item["error"] = current.get("error") or current.get("note") or "候选生成失败"
        return
    # 自动采用第 1 条（与页面逐条采用一致）；撤销由整批日志恢复采用前的绑定
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
    except Exception as exc:  # noqa: BLE001 - 单角色采用失败只影响该角色
        item["status"] = "ready"
        item["adopted"] = False
        item["candidate_index"] = None
        item["error"] = f"已生成候选，但自动采用失败：{exc}"
        return
    item["status"] = "ready"
    item["adopted"] = True
    item["candidate_index"] = int(candidates[0]["index"])
    item["voice_id"] = (result.get("voice") or {}).get("id")
    item["variant_id"] = result.get("variant_id")
    item["error"] = None


def cancel_job(store, job_id: str) -> dict:
    """停止批量任务：终止当前角色批次，已完成角色保留。

    参数:
        store: 工程。
        job_id: 任务 ID。

    返回:
        收敛后的任务元数据。
    """
    job = _read_job(store, job_id)
    if not job:
        return {}
    with _RUNNING_LOCK:
        state = _RUNNING.get(job_id)
        if state:
            state["cancel"].set()
    current = next(
        (item for item in job.get("items") or [] if item.get("character_id") == job.get("current")),
        None,
    )
    if current and current.get("batch_id"):
        voice_design.cancel_batch(store, current["batch_id"])
    with _THREADS_LOCK:
        thread = _THREADS.get(job_id)
    if thread is not None and thread.is_alive():
        thread.join(timeout=30)
    job = _read_job(store, job_id) or job
    if job.get("status") == "running":
        job = _finish_job(job, True)
        _write_job(store, job)
        _publish(job_id, {"type": "finished", "status": job.get("status"), "job": job})
    return job


def resume_job(store, cfg: AppConfig, job_id: str, character_ids: list[str] | None = None) -> dict:
    """续跑或重试批量任务：只处理未完成/失败的角色。

    参数:
        store: 工程。
        cfg: 本机配置（AppConfig）。
        job_id: 任务 ID。
        character_ids: 只重试这些角色；为空时处理全部未完成与失败项。

    返回:
        更新后的任务元数据。

    异常:
        ValueError: 配置类型不正确、任务不存在、仍在运行或没有可续跑的项。
    """
    _require_config(cfg)
    job = _read_job(store, job_id)
    if not job:
        raise ValueError("批量任务不存在")
    if job.get("status") == "running":
        raise ValueError("批量任务仍在生成中，请稍后再试")
    wanted = set(character_ids or [])
    targets = [
        item
        for item in job.get("items") or []
        if item.get("status") in ("pending", "failed") and (not wanted or item.get("character_id") in wanted)
    ]
    if not targets:
        raise ValueError("没有需要续跑的角色")
    for item in targets:
        item["status"] = "pending"
        item["error"] = None
    job["status"] = "running"
    job["note"] = None
    _refresh_counts(job)
    _write_job(store, job)
    with _RUNNING_LOCK:
        _RUNNING[job_id] = {"cancel": threading.Event()}
    thread = threading.Thread(target=_run_job, args=(store, cfg, job_id, int(job.get("count") or 4)), daemon=True)
    with _THREADS_LOCK:
        _THREADS[job_id] = thread
    thread.start()
    _publish(job_id, {"type": "status", "status": "running", "job": job})
    return job


def latest_job(store) -> dict | None:
    """返回最近一次批量任务（用于页面刷新后恢复/续跑）。"""
    root = store.voice_asset_dir("batch_jobs")
    if not root.exists():
        return None
    latest = None
    for target in root.iterdir():
        if not target.is_dir():
            continue
        job = _read_job(store, target.name)
        if not job.get("job_id"):
            continue
        if latest is None or str(job.get("created_at") or "") > str(latest.get("created_at") or ""):
            latest = job
    return _recover_stale(store, latest) if latest else None


def job_events(store, job_id: str):
    """产出批量任务事件序列：先回放条目，再转实时广播。

    参数:
        store: 工程。
        job_id: 任务 ID。

    产出:
        `(event_name, payload)`；event_name 为 item/batch/chars/ping/done。

    异常:
        ValueError: 任务不存在。
    """
    job = _read_job(store, job_id)
    if not job:
        raise ValueError("批量任务不存在")
    job = _recover_stale(store, job)
    channel = _subscribe(job_id)
    try:
        job = _read_job(store, job_id)
        for item in job.get("items") or []:
            yield ("item", item)
        if job.get("status") != "running":
            yield ("done", {"result": job})
            return
        queue_items = [item for item in job.get("items") or [] if item.get("status") != "skipped"]
        total = len(queue_items)
        position = next(
            (index for index, item in enumerate(queue_items, start=1) if item.get("character_id") == job.get("current")),
            1,
        )
        current = next((item for item in job.get("items") or [] if item.get("character_id") == job.get("current")), None)
        yield (
            "batch",
            {
                "type": "batch",
                "index": position,
                "total": total,
                "name": (current or {}).get("name"),
                "unit": "character",
            },
        )
        while True:
            try:
                event = channel.get(timeout=15)
            except queue.Empty:
                latest = _read_job(store, job_id)
                if latest.get("status") != "running":
                    yield ("done", {"result": latest})
                    return
                yield ("ping", {})
                continue
            kind = str(event.get("type") or "")
            if kind == "finished":
                yield ("done", {"result": _read_job(store, job_id) or event.get("job") or {}})
                return
            if kind == "item":
                yield ("item", event.get("item") or {})
            elif kind in ("batch", "chars"):
                yield (kind, event)
            elif kind == "status":
                yield (
                    "batch",
                    {
                        "type": "batch",
                        "index": int(event.get("index") or 0),
                        "total": len(
                            [item for item in (event.get("job") or {}).get("items") or [] if item.get("status") != "skipped"]
                        ),
                        "unit": "character",
                    },
                )
    finally:
        _unsubscribe(job_id, channel)
