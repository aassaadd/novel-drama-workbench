"""章内角色识别补跑：已有节拍但从未同步角色时，用一次 cast-only 调用补齐。

角色卡的主线仍由 `emit_beats` / `emit_act` 随节拍一次产出；本模块只处理
「节拍先于角色同步功能存在」的历史工程：按章读取现有节拍与角色表，单独发一次
`emit_cast` 结构化请求，再交给 `apply_chapter_cast()` 做同一套确定性落地。

约定：
- 每章只补一次：补完在设定库章节上写 `cast_synced`，正文生成不再重复触发；
- 识别不到角色也写标记并明确提示，避免每次生成都静默多打一次调用；
- 节拍已经绑定现有角色（旧版本已同步过）时只补标记，不调用模型。
"""

from __future__ import annotations

import json
import threading

from ..adapters.base import Message, TextGenRequest
from ..models.ids import new_id, now_iso
from ..store import ProjectStore
from ..store.settings_store import SettingsStore
from .cast_sync import (
    _norm,
    apply_chapter_cast,
    cast_brief,
    cast_prompt_block,
    load_pending,
    normalize_cast,
)
from .service import _progress, build_writing_adapter

#: 补跑一次角色识别的预估耗时（秒/章），只用于弹窗里的如实提示
CAPTURE_SECONDS_PER_CHAPTER = 8

#: 手动补跑任务文件（只保留最近一次，页面刷新/重进工程后仍可看到进度与失败原因）
_CAPTURE_JOB_FILE = "cast_capture_job.json"
#: 运行中的补跑任务：job_id -> 取消信号
_RUNNING: dict[str, threading.Event] = {}
#: 补跑线程：job_id -> Thread
_THREADS: dict[str, threading.Thread] = {}
_JOB_LOCK = threading.Lock()

#: 工具名与描述（只做角色识别，不生成节拍与正文）
CAST_TOOL_NAME = "emit_cast"


def _cast_tool() -> dict:
    """构造 `emit_cast` 工具定义（复用节拍 schema 里的 cast 项）。

    返回:
        OpenAI tools 单项。
    """
    from .service import _CAST_ITEM_SCHEMA

    return {
        "type": "function",
        "function": {
            "name": CAST_TOOL_NAME,
            "description": "输出本章出场角色（只做角色识别，不生成节拍与正文）",
            "parameters": {
                "type": "object",
                "properties": {
                    "cast": {
                        "type": "array",
                        "description": "本章出场角色；每项必须给 beats 绑定依据",
                        "items": _CAST_ITEM_SCHEMA,
                    }
                },
                "required": ["cast"],
            },
        },
    }


def _existing_characters(store: ProjectStore) -> list[dict]:
    """读取现有角色（不含旁白）。

    参数:
        store: 工程。

    返回:
        角色列表。
    """
    return [item for item in store.load_characters()["characters"] if item.get("role") != "narrator"]


def chapter_cast_state(store: ProjectStore, entry: dict | None) -> str:
    """判断章节的角色同步状态。

    - `synced`：已补跑过（设定库有 cast_synced 标记）；
    - `bound`：旧版已同步，节拍里引用了现有角色（只需补标记）；
    - `missing`：有节拍但没有任何现有角色绑定，需要补跑；
    - `absent`：设定库里没有本章条目或没有节拍。

    参数:
        store: 工程。
        entry: 设定库大纲章节。

    返回:
        状态字符串。
    """
    if not isinstance(entry, dict) or not (entry.get("beats") or []):
        return "absent"
    if entry.get("cast_synced"):
        return "synced"
    known = {str(item.get("id")) for item in _existing_characters(store) if item.get("id")}
    for beat in entry.get("beats") or []:
        for character_id in beat.get("character_ids") or []:
            if str(character_id) in known:
                return "bound"
    return "missing"


def _mark_synced(settings: SettingsStore, bible: dict, entry: dict, report: dict | None) -> None:
    """把角色同步结果写到设定库章节并保存。

    参数:
        settings: 设定库读写。
        bible: 设定库 dict（就地修改）。
        entry: 大纲章节（就地修改）。
        report: 角色识别结果；None 表示只补标记（旧版已绑定）。

    返回:
        无。
    """
    entry["cast_synced"] = True
    entry["cast_synced_at"] = now_iso()
    if report is not None:
        entry["cast_created"] = list(report.get("created") or [])
        entry["cast_reused"] = list(report.get("reused") or [])
        entry["cast_extras"] = list(report.get("extras") or [])
        entry["cast_pending"] = list(report.get("pending_names") or [])
    else:
        entry.setdefault("cast_created", [])
        entry.setdefault("cast_reused", [])
        entry.setdefault("cast_extras", [])
        entry.setdefault("cast_pending", [])
    settings.save(bible)


def _cast_message(report: dict) -> str:
    """把一次角色识别的结果压成可读反馈。

    参数:
        report: `apply_chapter_cast()` 的返回值，外加 `pending_names`。

    返回:
        中文反馈文案。
    """
    names: list[str] = []
    for source in (
        [str(item.get("name") or "") for item in report.get("cast") or []],
        list(report.get("pending_names") or []),
        list(report.get("extras") or []),
    ):
        for name in source:
            if name and name not in names:
                names.append(name)
    created = list(report.get("created") or [])
    reused = list(report.get("reused") or [])
    pending = list(report.get("pending_names") or [])
    extras = [name for name in report.get("extras") or [] if name not in pending]
    if not names:
        return "角色识别完成：未识别到可建卡的角色；本章按旁白归属，后续不再重复识别。"
    parts = [f"角色识别完成：识别到 {len(names)} 个角色（{'、'.join(names)}）"]
    if created:
        parts.append(f"新建 {len(created)} 张卡（{'、'.join(created)}）")
    if reused:
        parts.append(f"复用现有 {len(reused)} 个（{'、'.join(reused)}）")
    if pending:
        parts.append(f"{len(pending)} 个进入待定（{'、'.join(pending)}，第二次出现时建卡）")
    if extras:
        parts.append(f"{len(extras)} 个按龙套处理（{'、'.join(extras)}，不建卡）")
    if not created and not reused and not pending and not extras:
        parts.append("没有可落地的角色")
    return "；".join(parts) + "。"


def _build_request(bible: dict, entry: dict, characters: list[dict]) -> TextGenRequest:
    """构造角色识别请求（只带现有节拍与角色表）。

    参数:
        bible: 设定库。
        entry: 大纲章节。
        characters: 现有角色表。

    返回:
        `TextGenRequest`。
    """
    work = (bible.get("premise") or {}).get("logline") or ""
    beats = entry.get("beats") or []
    beat_lines = [
        f"{index}. {str(beat.get('summary') or '').strip()}"
        + (
            f"（龙套：{'、'.join(str(name) for name in beat.get('extra_characters') or [])}）"
            if beat.get("extra_characters")
            else ""
        )
        for index, beat in enumerate(beats, start=1)
    ]
    instruction = "\n".join(
        [
            f"为第 {int(entry.get('index') or 0)} 章《{entry.get('title') or ''}》做角色识别。",
            f"本章概要：{entry.get('summary') or '（无）'}",
            f"全书一句话：{work or '（无）'}",
            "本章节拍（序号. 发生了什么）：",
            *beat_lines,
            cast_prompt_block(characters),
            "现有角色表里的姓名是唯一口径：本章出现简称、绰号、「老X」「小X」时，必须映射到现有角色并使用其原名，不要另起新名。",
            "只输出本章 cast：不要复述节拍，不要写正文，不要新增节拍。",
        ]
    )
    return TextGenRequest(
        task="cast_capture",
        system="你是长篇小说的角色统筹编辑，只负责从既有节拍里识别出场角色并复用现有角色卡。只调用给定工具，不要解释。",
        messages=[Message("user", instruction)],
        metadata={
            "cast_only": True,
            "chapter_id": entry.get("id"),
            "chapter_index": int(entry.get("index") or 0),
            "cast_brief": cast_brief(characters),
            "beats": beats,
        },
    )


def capture_chapter_cast(
    store: ProjectStore,
    chapter_id: str,
    *,
    config=None,
    adapter=None,
    characters: list[dict] | None = None,
) -> dict:
    """对一章补跑一次 cast-only 识别并落库，同时在设定库章节上写同步标记。

    参数:
        store: 工程。
        chapter_id: 章节 ID。
        config: 本机配置；adapter 为 None 时用于装配写作适配器。
        adapter: 已装配的写作适配器（反推大纲路径复用同一实例）。
        characters: 现有角色（不含旁白）；None 时从工程读取。

    返回:
        `{triggered, state, found, created, reused, extras, pending, narrator_beats,
          empty, message}`；已同步/无条目时 triggered=False。
    """
    settings = SettingsStore(store.root)
    bible = settings.ensure()
    # 章节文件可能尚未落地：优先用文件里的序号兜底，缺失时只按大纲 ID 定位（手动整幕补跑会覆盖未落章）
    try:
        index = int(store.load_chapter(chapter_id).get("index") or 0)
    except (OSError, ValueError, KeyError):
        index = 0
    entry = settings.find_outline_chapter(bible, chapter_id, index=index or None)
    if entry is None:
        raise ValueError("找不到本章的大纲条目，无法做角色识别")
    state = chapter_cast_state(store, entry)
    if state in ("synced", "absent"):
        return {"triggered": False, "state": state, "message": ""}
    if state == "bound":
        _mark_synced(settings, bible, entry, None)
        return {"triggered": False, "state": "bound", "message": ""}

    existing = list(characters) if characters is not None else _existing_characters(store)
    if adapter is None:
        adapter = build_writing_adapter(config, bible)
    payload = adapter.generate_tool(_build_request(bible, entry, existing), _cast_tool(), timeout_s=180)
    rows = normalize_cast(payload if isinstance(payload, dict) else {})
    before_pending = load_pending(store)
    # apply_chapter_cast 会原地修改 pending，先记下键集合用于识别「本次新增的待定角色」
    before_keys = set(before_pending)
    report: dict = {
        "created": [],
        "reused": [],
        "extras": [],
        "cast": [],
        "pending": before_pending,
        "pending_names": [],
        "narrator_beats": 0,
    }
    if rows:
        report = apply_chapter_cast(
            store,
            entry,
            rows,
            characters=existing,
            pending=before_pending,
            persist=True,
            chapter_index=int(entry.get("index") or 0),
            chapter_title=str(entry.get("title") or ""),
        )
        after_pending = report.get("pending") or {}
        report["pending_names"] = [
            str((after_pending.get(key) or {}).get("name") or key)
            for key in after_pending
            if key not in before_keys
        ]
    _mark_synced(settings, bible, entry, report)
    message = _cast_message(report)
    return {
        "triggered": True,
        "state": "captured",
        "found": [str(item.get("name") or "") for item in report.get("cast") or []],
        "created": list(report.get("created") or []),
        "reused": list(report.get("reused") or []),
        "extras": list(report.get("extras") or []),
        "pending": list(report.get("pending_names") or []),
        "narrator_beats": int(report.get("narrator_beats") or 0),
        "empty": not rows,
        "message": message,
    }


def ensure_chapter_cast(
    store: ProjectStore,
    config=None,
    chapter_id: str = "",
    *,
    adapter=None,
    on_notice=None,
    on_progress=None,
) -> dict:
    """正文生成前的角色兜底：未同步的章节先补一次识别，已同步的直接跳过。

    参数:
        store: 工程。
        config: 本机配置。
        chapter_id: 章节 ID。
        adapter: 已装配的写作适配器（可选）。
        on_notice: 反馈回调 `(text) -> None`（可读文案，页面与任务通知都会展示）。
        on_progress: 进度回调 `(event) -> None`。

    返回:
        `capture_chapter_cast()` 的结果。
    """
    result = capture_chapter_cast(store, chapter_id, config=config, adapter=adapter)
    if result.get("message"):
        _progress(on_progress, {"type": "notice", "message": result["message"]})
        if on_notice:
            on_notice(result["message"])
    return result


def _outline_acts(bible: dict, act_id: str | None = None, act_index: int | None = None) -> list[tuple[int, dict]]:
    """按幕 ID 或幕序号筛出大纲里的幕。

    幕 ID 在旧工程里可能缺失，因此同时支持按序号兜底；只给 act_id 时按 ID 精确匹配。

    参数:
        bible: 设定库。
        act_id: 幕 ID（可选）。
        act_index: 幕序号（从 1 开始，可选）。

    返回:
        `[(幕序号, 幕 dict)]`；未匹配时为空列表。
    """
    rows: list[tuple[int, dict]] = []
    for position, act in enumerate((bible.get("outline") or {}).get("acts") or [], start=1):
        if not isinstance(act, dict):
            continue
        if act_id and str(act.get("id") or "") != str(act_id):
            continue
        if act_index and int(act.get("index") or position) != int(act_index):
            continue
        rows.append((position, act))
    return rows


def cast_capture_plan(
    store: ProjectStore,
    *,
    chapter_id: str | None = None,
    act_id: str | None = None,
    act_index: int | None = None,
) -> dict:
    """列出仍需要补跑角色识别的章节（供生成弹窗与手动补跑提前如实提示）。

    参数:
        store: 工程。
        chapter_id: 只检查这一章（单章）。
        act_id: 只检查这一幕（整幕）。
        act_index: 幕序号兜底（幕 ID 缺失时使用）。

    返回:
        `{chapters: [{id, index, title, state}], count, seconds, seconds_per_chapter}`。
    """
    settings = SettingsStore(store.root)
    bible = settings.ensure()
    entries: list[dict] = []
    if chapter_id:
        try:
            index = int(store.load_chapter(chapter_id).get("index") or 0)
        except (OSError, ValueError, KeyError):  # 章节文件缺失/损坏时按不存在处理
            index = 0
        entry = settings.find_outline_chapter(bible, chapter_id, index=index)
        entries = [entry] if entry else []
    else:
        for _position, act in _outline_acts(bible, act_id, act_index):
            entries.extend(act.get("chapters") or [])
    rows: list[dict] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        state = chapter_cast_state(store, entry)
        if state == "missing":
            rows.append(
                {
                    "id": entry.get("id"),
                    "index": entry.get("index"),
                    "title": entry.get("title"),
                    "state": state,
                }
            )
    return {
        "chapters": rows,
        "count": len(rows),
        "seconds": len(rows) * CAPTURE_SECONDS_PER_CHAPTER,
        "seconds_per_chapter": CAPTURE_SECONDS_PER_CHAPTER,
    }


def pending_overview(store: ProjectStore) -> dict:
    """读取待定角色队列，并补上出现次数与归属线索（只读，不写盘）。

    参数:
        store: 工程。

    返回:
        `{items: [{key, name, aliases, first_index, first_title, occurrences,
          chapter_count, chapters, clue}], count}`；按首次出现章节排序。
    """
    settings = SettingsStore(store.root)
    bible = settings.ensure()
    pending = load_pending(store)
    chapters: list[dict] = []
    for _position, act in _outline_acts(bible):
        chapters.extend(entry for entry in act.get("chapters") or [] if isinstance(entry, dict))
    items: list[dict] = []
    for key, record in pending.items():
        if not isinstance(record, dict):
            continue
        names = [str(record.get("name") or key), *[str(item) for item in record.get("aliases") or []]]
        norms = {_norm(name) for name in names if _norm(name)}
        occurrences = 0
        hits: list[dict] = []
        clues: list[str] = []
        for entry in chapters:
            beat_hits: list[str] = []
            for beat in entry.get("beats") or []:
                extras = {_norm(str(item)) for item in beat.get("extra_characters") or []}
                summary = _norm(str(beat.get("summary") or ""))
                if extras & norms or any(norm in summary for norm in norms):
                    beat_hits.append(str(beat.get("summary") or "").strip())
            if not beat_hits:
                continue
            occurrences += len(beat_hits)
            hits.append({"index": int(entry.get("index") or 0), "title": str(entry.get("title") or "")})
            for text in beat_hits:
                if text and len(clues) < 2:
                    clues.append(text[:60])
        if not clues:
            first = next(
                (entry for entry in chapters if int(entry.get("index") or 0) == int(record.get("chapter") or 0)),
                None,
            )
            if first:
                clues.append(str(first.get("summary") or first.get("title") or "")[:60])
        items.append(
            {
                "key": str(key),
                "name": str(record.get("name") or key),
                "aliases": [str(item) for item in record.get("aliases") or []],
                "first_index": int(record.get("chapter") or 0),
                "first_title": str(record.get("chapter_title") or ""),
                "occurrences": occurrences,
                "chapter_count": len(hits),
                "chapters": hits,
                "clue": "；".join(item for item in clues if item),
            }
        )
    items.sort(key=lambda row: (row["first_index"], row["name"]))
    return {"items": items, "count": len(items)}


def cast_status_overview(
    store: ProjectStore,
    *,
    act_id: str | None = None,
    act_index: int | None = None,
) -> dict:
    """逐章返回角色识别状态（口径与 `cast_capture_plan` 完全一致）。

    参数:
        store: 工程。
        act_id: 只看这一幕（可选）。
        act_index: 幕序号兜底（可选）。

    返回:
        `{chapters: [{id, index, title, act_id, act_index, state, synced_at,
          created, reused, pending, running, last_error}], counts, job}`。
    """
    settings = SettingsStore(store.root)
    bible = settings.ensure()
    job = latest_capture_job(store)
    running_id = ""
    if job.get("status") == "running" and isinstance(job.get("current"), dict):
        running_id = str(job["current"].get("chapter_id") or "")
    errors: dict[str, str] = {}
    for item in job.get("items") or []:
        if item.get("status") == "failed":
            errors[str(item.get("chapter_id") or "")] = str(item.get("error") or "")
    rows: list[dict] = []
    for position, act in _outline_acts(bible, act_id, act_index):
        for entry in act.get("chapters") or []:
            if not isinstance(entry, dict):
                continue
            chapter_id = str(entry.get("id") or "")
            rows.append(
                {
                    "id": chapter_id,
                    "index": int(entry.get("index") or 0),
                    "title": str(entry.get("title") or ""),
                    "act_id": str(act.get("id") or ""),
                    "act_index": int(act.get("index") or position),
                    "state": chapter_cast_state(store, entry),
                    "synced_at": entry.get("cast_synced_at"),
                    "created": list(entry.get("cast_created") or []),
                    "reused": list(entry.get("cast_reused") or []),
                    "pending": list(entry.get("cast_pending") or []),
                    "running": chapter_id == running_id,
                    "last_error": errors.get(chapter_id) or "",
                }
            )
    counts = {key: 0 for key in ("synced", "bound", "missing", "absent")}
    for row in rows:
        counts[row["state"]] = counts.get(row["state"], 0) + 1
    return {"chapters": rows, "counts": counts, "job": job or None}


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
