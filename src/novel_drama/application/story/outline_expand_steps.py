"""大纲扩写的分步生成：幕、章、节拍三段与编号重排。"""

from __future__ import annotations

from ...adapters.llm.contracts import Message, TextGenRequest
from ...adapters.persistence import ProjectStore
from ...adapters.persistence.settings_store import SettingsStore
from ...application.cast.sync_cast import (
    apply_chapter_cast,
    cast_prompt_block,
    normalize_cast,
    summarize_cast,
)
from ...models.ids import new_id
from ..support.llm_calls import (
    _generate_json,
    _progress,
)
from .generate_outline import _tolerance
from .outline_expand_common import (
    REWRITE_LIMIT,
    _beat_delta,
    _bounded_tool,
    _correction,
    _count_reason,
    _delta,
    _gate_error,
    _normalize_acts,
    _normalize_beats,
    _normalize_chapters,
    _rewrite,
)


def _gen_acts(adapter, config, context: str, meta: dict, expected: int, brief: str, on_progress):
    """生成新增幕列表，并按幕数闸门重写。

    参数:
        adapter: 写作适配器。
        config: 本机配置。
        context: 作品上下文。
        meta: 请求 metadata。
        expected: 目标幕数。
        brief: 用户补充要求。
        on_progress: 进度回调。

    返回:
        幕列表（未裁剪）。

    异常:
        AdapterError: 重写后仍越界。
    """
    prompt = (
        f"{context}\n\n任务：在已有大纲的基础上新增 {expected} 幕。\n"
        f"{meta.get('placement') or ''}\n"
        "只输出新增的幕：每幕给 title、summary、chapters_count（预计章数）。\n"
        "不要重写、不要重复已有幕，也不要输出章节或节拍。"
    )
    if brief:
        prompt += f"\n补充要求：{brief}"
    req = TextGenRequest(
        task="outline",
        system="你是长篇小说的结构编辑，只按工具参数输出新增幕结构，不要解释。",
        messages=[Message("user", prompt)],
        stream=True,
        metadata=meta,
    )
    tol = _tolerance(expected)
    tool = _bounded_tool("outline_acts", expected, tol)
    payload = _generate_json(
        adapter, req, "outline_acts", on_delta=_delta({"chars": 0}, on_progress, "act", 0, expected, "新增幕", {"phase": "acts", "percent": 0}), tool=tool
    )
    rows = _normalize_acts(payload, expected)
    attempts = 0
    while abs(len(rows) - expected) > tol and attempts < REWRITE_LIMIT:
        attempts += 1
        _progress(
            on_progress,
            {
                "type": "rewrite",
                "level": "acts",
                "attempt": attempts,
                "index": 1,
                "total": expected,
                "reason": _count_reason("acts", len(rows), expected),
            },
        )
        payload = _generate_json(
            adapter,
            _rewrite(req, _correction("幕", len(rows), expected)),
            "outline_acts",
            on_delta=_delta({"chars": 0}, on_progress, "act", 0, expected, "新增幕", {"phase": "acts", "percent": 0}),
            tool=tool,
        )
        rows = _normalize_acts(payload, expected)
    if abs(len(rows) - expected) > tol:
        state = "新增幕为空" if not rows else f"实际 {len(rows)} 幕"
        raise _gate_error(
            "acts",
            f"扩充停在「幕」层：{state}，需要 {expected} 幕，已尝试 {attempts} 次。",
            act_index=1,
            act_name="新增幕",
            attempts=attempts,
            expected=expected,
            actual=len(rows),
            reason=_count_reason("acts", len(rows), expected),
        )
    return rows

def _gen_chapters(
    adapter,
    config,
    context: str,
    meta: dict,
    act_pos: int,
    act: dict,
    expected: int,
    brief: str,
    on_progress,
    total_acts: int,
    chapter_total: int | None = None,
    chapters_done: int | None = None,
):
    """生成某一幕的章节列表，并按章数闸门重写。

    参数:
        adapter: 写作适配器。
        config: 本机配置。
        context: 作品上下文。
        meta: 请求 metadata。
        act_pos: 幕位置（1 开始）。
        act: 幕对象。
        expected: 目标章数。
        brief: 用户补充要求。
        on_progress: 进度回调。
        total_acts: 幕总数（用于进度展示）。
        chapter_total: 本次新增的章总数（进度展示用）。
        chapters_done: 本幕之前已完成的章数。

    返回:
        章节列表（未裁剪）。

    异常:
        AdapterError: 重写后仍越界。
    """
    name = act.get("title") or f"第{act_pos}幕"
    lines = [
        context,
        "",
        f"新增幕《{name}》：{act.get('summary') or ''}",
        f"{meta.get('placement') or ''}",
        f"请只输出本幕新增的 {expected} 个章节：每章 title 与 summary，不写节拍。",
    ]
    if meta.get("anchor_note"):
        lines.append(str(meta["anchor_note"]))
    if brief:
        lines.append(f"补充要求：{brief}")
    req = TextGenRequest(
        task="outline",
        system="你是长篇小说的结构编辑，只按工具参数输出本幕新增章节，不要解释。",
        messages=[Message("user", "\n".join(lines))],
        stream=True,
        metadata=meta,
    )
    tol = _tolerance(expected)
    tool = _bounded_tool("outline_chapters", expected, tol)
    payload = _generate_json(
        adapter,
        req,
        "outline_chapters",
        on_delta=_delta(
                {"chars": 0},
                on_progress,
                "act",
                act_pos,
                total_acts,
                name,
                {
                    "phase": "chapters",
                    "chapters_done": (act_pos - 1) * expected if chapters_done is None else chapters_done,
                    "chapters_expected": expected,
                    "chapters_total": total_acts * expected if chapter_total is None else chapter_total,
                },
            ),
        tool=tool,
    )
    rows = _normalize_chapters(payload)
    attempts = 0
    while abs(len(rows) - expected) > tol and attempts < REWRITE_LIMIT:
        attempts += 1
        _progress(
            on_progress,
            {
                "type": "rewrite",
                "level": "chapters",
                "index": act_pos,
                "total": total_acts,
                "attempt": attempts,
                "reason": _count_reason("chapters", len(rows), expected),
            },
        )
        payload = _generate_json(
            adapter,
            _rewrite(req, _correction("章", len(rows), expected)),
            "outline_chapters",
            on_delta=_delta(
                {"chars": 0},
                on_progress,
                "act",
                act_pos,
                total_acts,
                name,
                {
                    "phase": "chapters",
                    "chapters_done": (act_pos - 1) * expected if chapters_done is None else chapters_done,
                    "chapters_expected": expected,
                    "chapters_total": total_acts * expected if chapter_total is None else chapter_total,
                },
            ),
            tool=tool,
        )
        rows = _normalize_chapters(payload)
    if abs(len(rows) - expected) > tol:
        state = "章节为空" if not rows else f"实际 {len(rows)} 章"
        raise _gate_error(
            "chapters",
            f"扩充停在第 {act_pos} 幕《{name}》的「章」层：{state}，需要 {expected} 章，已尝试 {attempts} 次。",
            act_index=act_pos,
            act_name=name,
            attempts=attempts,
            expected=expected,
            actual=len(rows),
            reason=_count_reason("chapters", len(rows), expected),
        )
    return rows

def _gen_beats(
    adapter,
    config,
    context: str,
    meta: dict,
    act_pos: int,
    act: dict,
    chapter_pos: int,
    chapter: dict,
    expected: int,
    chapter_words: int,
    on_progress,
    total_acts: int,
    chapter_total: int = 0,
    beats_before: int = 0,
    beat_total: int = 0,
):
    """生成某一章的节拍列表，并按节拍数闸门重写。

    参数:
        adapter: 写作适配器。
        config: 本机配置。
        context: 作品上下文。
        meta: 请求 metadata。
        act_pos: 幕位置。
        act: 幕对象。
        chapter_pos: 全书章序号（用于展示）。
        chapter: 章节对象。
        expected: 目标节拍数。
        chapter_words: 单章目标字数。
        on_progress: 进度回调。
        total_acts: 幕总数。
        chapter_total: 本次新增的章总数（进度展示用）。
        beats_before: 本章之前已完成的节拍数。
        beat_total: 本次新增的节拍总数。

    返回:
        `(节拍列表, 最后一次工具返回)`；节拍未裁剪，cast 从同一次返回里取。

    异常:
        AdapterError: 重写后仍越界。
    """
    chapter_title = chapter.get("title") or f"第{chapter_pos}章"
    lines = [
        context,
        "",
        f"第 {act_pos} 幕《{act.get('title') or ''}》：{act.get('summary') or ''}",
        f"新增章节《{chapter_title}》：{chapter.get('summary') or ''}",
        f"{meta.get('placement') or ''}",
        f"请一次补齐本章的 {expected} 个节拍：只写节拍，不要重复章节标题。",
        cast_prompt_block(meta.get("characters") or []),
    ]
    req = TextGenRequest(
        task="outline",
        system="你是长篇小说的结构编辑，只按工具参数输出本章全部节拍，不要解释。",
        messages=[Message("user", "\n".join(lines))],
        stream=True,
        metadata={**meta, "chapter_words": max(1, int(chapter_words)), "beats_per_chapter": expected},
    )
    tol = _tolerance(expected)
    tool = _bounded_tool("outline_beats", expected, tol)
    payload = _generate_json(
        adapter,
        req,
        "outline_beats",
        on_delta=_beat_delta(
            on_progress, beats_before, beat_total, chapter_pos, chapter_total, act_pos, total_acts, chapter_title
        ),
        tool=tool,
    )
    rows = _normalize_beats(payload, expected, chapter_words, expected)
    attempts = 0
    while abs(len(rows) - expected) > tol and attempts < REWRITE_LIMIT:
        attempts += 1
        _progress(
            on_progress,
            {
                "type": "rewrite",
                "level": "beats",
                "index": act_pos,
                "total": total_acts,
                "chapter_index": chapter_pos,
                "chapter": chapter_title,
                "attempt": attempts,
                "actual": len(rows),
                "expected": expected,
                "empty": not rows,
                "reason": _count_reason("beats", len(rows), expected),
            },
        )
        payload = _generate_json(
            adapter,
            _rewrite(req, _correction("个节拍", len(rows), expected)),
            "outline_beats",
            on_delta=_beat_delta(
            on_progress, beats_before, beat_total, chapter_pos, chapter_total, act_pos, total_acts, chapter_title
        ),
            tool=tool,
        )
        rows = _normalize_beats(payload, expected, chapter_words, expected)
    if abs(len(rows) - expected) > tol:
        state = "节拍为空" if not rows else f"实际 {len(rows)} 个节拍"
        raise _gate_error(
            "beats",
            f"扩充停在第 {act_pos} 幕 · 第 {chapter_pos} 章《{chapter_title}》的「节拍」层：{state}，"
            f"需要 {expected} 个，已尝试 {attempts} 次。",
            act_index=act_pos,
            act_name=act.get("title") or f"第{act_pos}幕",
            chapter_index=chapter_pos,
            chapter=chapter_title,
            attempts=attempts,
            expected=expected,
            actual=len(rows),
            reason=_count_reason("beats", len(rows), expected),
        )
    return rows, payload

def _to_outline_act(act: dict, origin: dict, beats_per_chapter: int, chapter_words: int) -> dict:
    """把扩充生成的工作对象转换成设定库结构。

    参数:
        act: 工作对象（title/summary/chapters[beats]）。
        origin: 来源标注。
        beats_per_chapter: 每章节拍数。
        chapter_words: 单章目标字数。

    返回:
        带稳定 ID 的幕 dict（章 index 稍后统一重排）。
    """
    built = SettingsStore.new_act(act.get("title") or "新增幕", act.get("summary") or "")
    built["origin"] = dict(origin)
    for chapter in act.get("chapters") or []:
        outline_chapter = SettingsStore.new_outline_chapter(
            new_id("chp"), 0, chapter.get("title") or "新增章", chapter.get("summary") or ""
        )
        outline_chapter["origin"] = dict(origin)
        for beat in chapter.get("beats") or []:
            outline_chapter["beats"].append(
                SettingsStore.new_beat(
                    beat.get("summary") or "节拍",
                    character_ids=beat.get("character_ids") or [],
                    turning_point=bool(beat.get("turning_point")),
                    target_words=int(beat.get("target_words") or max(1, int(chapter_words) // max(1, beats_per_chapter))),
                    extra_characters=beat.get("extra_characters") or [],
                )
            )
        for key in ("cast_synced", "cast_synced_at", "cast_created", "cast_reused", "cast_extras", "cast_pending"):
            if key in chapter:
                outline_chapter[key] = chapter[key]
        built["chapters"].append(outline_chapter)
    return built

def _apply_expand_cast(
    store: ProjectStore,
    chapter: dict,
    payload: dict,
    chapter_pos: int,
    dry_run: bool,
    on_progress,
    characters: list[dict],
    pending: dict,
) -> dict:
    """把本次节拍调用里的 cast 落到角色卡与节拍绑定（不额外请求模型）。

    参数:
        store: 工程。
        chapter: 工作对象的章节（节拍已生成，会被原地写入 character_ids）。
        payload: `emit_beats` 的同一次返回（含 beats 与 cast）。
        chapter_pos: 章序号（用于提示与登记）。
        dry_run: True 时不写 characters.json，新角色只留在内存里。
        on_progress: 进度回调。
        characters: 当前内存角色表。
        pending: 待定角色登记（第二次出现才建卡）。

    返回:
        `apply_chapter_cast()` 的返回值。
    """
    report = apply_chapter_cast(
        store,
        chapter,
        normalize_cast(payload),
        characters=characters,
        pending=pending,
        persist=not dry_run,
        chapter_index=chapter_pos,
        chapter_title=chapter.get("title") or "",
    )
    # 标记写回章节，随大纲一起落库；正文生成据此判断不必再补跑
    chapter["cast_synced"] = True
    chapter["cast_created"] = list(report.get("created") or [])
    chapter["cast_reused"] = list(report.get("reused") or [])
    chapter["cast_extras"] = list(report.get("extras") or [])
    title = chapter.get("title") or f"第{chapter_pos}章"
    summary = f"第 {chapter_pos} 章《{title}》角色对齐：{summarize_cast(report)}"
    _progress(on_progress, {"type": "notice", "message": summary})
    return report

def _renumber(bible: dict, store: ProjectStore, dry_run: bool = False) -> None:
    """按幕顺序重排大纲章序号，并同步已有章节文件的 index 字段。

    已有对象的 ID 不变，因此正文、标注、配音等按 ID 的引用不受影响；
    不在大纲里的章节文件保持原序号，冲突时顺延到大纲之后。
    dry_run=True 时只改内存里的大纲序号，不写章节文件。

    参数:
        bible: 设定库（原地修改）。
        store: 工程。
        dry_run: 只预览不写盘。

    返回:
        无。
    """
    acts = (bible.get("outline") or {}).get("acts") or []
    outline_index: dict[str, int] = {}
    counter = 0
    for act in acts:
        for chapter in act.get("chapters") or []:
            counter += 1
            chapter["index"] = counter
            if chapter.get("id"):
                outline_index[chapter["id"]] = counter

    if dry_run:
        return

    files = store.list_chapters()
    for item in files:
        chapter_id = item.get("id")
        if not chapter_id or chapter_id not in outline_index:
            continue
        desired = outline_index[chapter_id]
        if int(item.get("index") or 0) == desired:
            continue
        try:
            chapter = store.load_chapter(chapter_id)
        except (OSError, ValueError):
            continue
        chapter["index"] = desired
        try:
            store.save_chapter(chapter)
        except Exception:  # 单个章节文件写失败不影响大纲本身
            continue

    used = set(outline_index.values())
    next_index = max(used) if used else 0
    for item in files:
        chapter_id = item.get("id")
        if not chapter_id or chapter_id in outline_index:
            continue
        current = int(item.get("index") or 0)
        if current not in used:
            used.add(current)
            continue
        next_index += 1
        while next_index in used:
            next_index += 1
        try:
            chapter = store.load_chapter(chapter_id)
        except (OSError, ValueError):
            continue
        chapter["index"] = next_index
        used.add(next_index)
        try:
            store.save_chapter(chapter)
        except Exception:  # 同上：顺延失败不阻断大纲保存
            continue
