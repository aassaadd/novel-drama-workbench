"""分层大纲生成结果的规范化：幕/章/节拍三层与角色绑定。"""

from __future__ import annotations

import logging

from ...adapters.base import AdapterError, ErrorCode
from ...application.cast.sync_cast import (
    apply_chapter_cast,
    normalize_cast,
    summarize_cast,
)
from ...store import ProjectStore
from ...store.world_store import WorldStore
from ...text.titles import strip_index_prefix
from ..support.llm_calls import (
    _progress,
)
from .outline_bind import (
    bind_outline_context,
)

logger = logging.getLogger(__name__)


def _text(value, limit: int = 300) -> str:
    """取字符串并裁掉超长部分。

    参数:
        value: 任意值。
        limit: 最大长度。

    返回:
        去空白后的字符串。
    """
    return str(value or "").strip()[:limit]

def _count_correction(kind: str, got: int, expected: int, existing: list[str]) -> str:
    """生成「章数/节拍数不符」的重试提示。

    参数:
        kind: chapters / beats。
        got: 上一次实际数量（0 表示模型没返回任何条目）。
        expected: 设定数量。
        existing: 上一次已给出的标题或概要，用于避免重复。

    返回:
        中文纠正提示；空数组时明确写「没有返回任何节拍/章节」。
    """
    noun = {"acts": "幕", "chapters": "章节", "beats": "节拍"}.get(kind, kind)
    unit = {"acts": "幕", "chapters": "章", "beats": "个节拍"}.get(kind, kind)
    subject = {"acts": "整体", "chapters": "本幕", "beats": "本章"}.get(kind, "本次")
    if got <= 0:
        return (
            f"上一版没有返回任何{noun}，{subject}需要 {expected} {unit}。"
            f"请完整给出 {expected} {unit}，一条都不要省略。"
        )
    listed = "、".join(title for title in existing if title)
    return (
        f"上一次只给了 {got} {unit}，必须恰好 {expected} {unit}。"
        f"请补齐到 {expected} {unit}，不要少给、不要多给，也不要重复已有内容：{listed or '（无）'}"
    )

def _count_reason(kind: str, got: int, expected: int) -> str:
    """生成数量不符时的进度说明（空数组写明「为空」，供页面直接展示）。

    参数:
        kind: chapters / beats / acts。
        got: 上一次实际数量。
        expected: 设定数量。

    返回:
        中文说明。
    """
    if kind == "chapters":
        return f"本幕章节为空，需要 {expected} 章" if got <= 0 else f"上一版给了 {got} 章，本幕要求 {expected} 章"
    if kind == "beats":
        return f"本章节拍为空，需要 {expected} 个节拍" if got <= 0 else f"上一版给了 {got} 个节拍，本章要求 {expected} 个"
    return f"上一版给了 {got} 幕，本次要求 {expected} 幕"

def _normalize_acts(payload: dict, acts: int, chapters_per_act: int) -> list[dict]:
    """清洗阶段一的幕列表。

    参数:
        payload: `emit_acts` 的返回值。
        acts: 目标幕数。
        chapters_per_act: 未给出预计章数时的默认值。

    返回:
        幕列表，每项 `{title, summary, chapters, status, error}`。

    异常:
        AdapterError: 未返回可用幕。
    """
    raw_acts = payload.get("acts") if isinstance(payload, dict) else None
    if not isinstance(raw_acts, list) or not raw_acts:
        raise AdapterError(ErrorCode.INVALID_INPUT, "分层大纲阶段一失败：emit_acts 未返回非空 acts 数组")
    rows: list[dict] = []
    for index, raw in enumerate(raw_acts[: max(1, int(acts))], start=1):
        if not isinstance(raw, dict):
            continue
        title = strip_index_prefix(_text(raw.get("title"), 80)) or f"第{index}幕"
        # 章数以用户设定为准：模型给的 chapters_count 只作为参考，不写进中间结果
        try:
            suggested = int(raw.get("chapters_count") or chapters_per_act)
        except (TypeError, ValueError):
            suggested = chapters_per_act
        if suggested != max(1, int(chapters_per_act)):
            logger.warning(
                "大纲第 %s 幕模型建议 %s 章，按设定强制为 %s 章", index, suggested, max(1, int(chapters_per_act))
            )
        rows.append(
            {
                "title": title,
                "summary": _text(raw.get("summary") or raw.get("goal"), 400),
                "chapters_count": max(1, int(chapters_per_act)),
                "chapters": [],
                "status": "pending",
                "error": None,
            }
        )
    if not rows:
        raise AdapterError(ErrorCode.INVALID_INPUT, "分层大纲阶段一失败：幕列表里没有可用对象")
    return rows

def _normalize_chapters(payload: dict, expected: int) -> list[dict]:
    """清洗阶段二某一幕的章节列表。

    参数:
        payload: `emit_chapters` 的返回值。
        expected: 本幕计划章数（缺失时补足到该数量）。

    返回:
        章节列表，每项 `{title, summary, beats, status, error}`；
        未返回可用章节时返回空列表（交给章数闸门重写，而不是直接记失败）。
    """
    raw = payload.get("chapters") if isinstance(payload, dict) else None
    if not isinstance(raw, list) or not raw:
        return []
    rows: list[dict] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "title": strip_index_prefix(_text(item.get("title"), 80)) or f"第{index}章",
                "summary": _text(item.get("summary"), 400),
                "beats": [],
                "status": "pending",
                "error": None,
            }
        )
    return rows

def _normalize_character_arcs(payload: dict, cast_ids: set[str]) -> list[dict]:
    """清洗阶段三返回的本章角色弧线。

    参数:
        payload: `emit_beats` 的返回值。
        cast_ids: 允许引用的角色 ID 集合。

    返回:
        最多 3 条弧线；角色不存在或状态为空时丢弃。
    """
    raw = payload.get("character_arcs") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return []
    rows: list[dict] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        character_id = str(item.get("character_id") or "").strip()
        if not character_id or character_id not in cast_ids or character_id in seen:
            continue
        seen.add(character_id)
        to_state = _text(item.get("to_state"), 500)
        if not to_state:
            continue
        rows.append(
            {
                "character_id": character_id,
                "from_state": _text(item.get("from_state"), 500),
                "to_state": to_state,
                "note": _text(item.get("note"), 500),
            }
        )
        if len(rows) >= 3:
            break
    return rows

def _normalize_beats(payload: dict, expected: int, chapter_words: int, beats_per_chapter: int) -> list[dict]:
    """清洗阶段三某一章的节拍列表。

    参数:
        payload: `emit_beats` 的返回值。
        expected: 本章计划节拍数。
        chapter_words: 单章目标字数。
        beats_per_chapter: 未给出目标字数时的默认等分数。

    返回:
        节拍列表；未返回可用节拍时返回空列表
        （交给节拍数闸门按数量越界重写，而不是直接记失败）。
    """
    raw = payload.get("beats") if isinstance(payload, dict) else None
    if not isinstance(raw, list) or not raw:
        return []
    default_words = max(1, int(chapter_words) // max(1, int(beats_per_chapter)))
    rows: list[dict] = []
    for index, item in enumerate(raw[: max(1, int(expected))], start=1):
        if not isinstance(item, dict):
            continue
        ids = [str(value) for value in (item.get("character_ids") or []) if str(value).strip()]
        try:
            words = int(item.get("target_words") or default_words)
        except (TypeError, ValueError):
            words = default_words
        rows.append(
            {
                "summary": _text(item.get("summary"), 240) or f"第{index}个节拍",
                "character_ids": ids,
                "extra_characters": [
                    _text(name, 60) for name in (item.get("extra_characters") or []) if _text(name, 60)
                ],
                "turning_point": bool(item.get("turning_point")),
                "target_words": words,
            }
        )
    return rows

def _apply_chapter_cast(
    store: ProjectStore,
    chapter: dict,
    payload: dict,
    chapter_pos: int,
    characters: list[dict],
    pending: dict,
    dry_run: bool,
    on_progress,
) -> dict:
    """把本次节拍调用里的 cast 落到角色卡与节拍绑定（不再单独请求模型）。

    参数:
        store: 工程。
        chapter: 大纲章节（节拍已生成，会被原地写入 character_ids）。
        payload: `emit_beats` 的同一次返回（含 beats 与 cast）。
        chapter_pos: 全文章序号。
        characters: 当前内存角色表（不含旁白）。
        pending: 待定角色登记（第二次出现才建卡）。
        dry_run: True 时只预览，不写角色卡与登记。
        on_progress: 进度回调。

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
    title = chapter.get("title") or f"第{chapter_pos}章"
    summary = f"第 {chapter_pos} 章《{title}》角色对齐：{summarize_cast(report)}"
    logger.info(summary)
    _progress(on_progress, {"type": "notice", "message": summary})
    return report

def _bind_outline_characters(store: ProjectStore, bible: dict, characters: list[dict]) -> dict:
    """按给定角色表绑定大纲里的世界与 POV（不重新读盘）。

    参数:
        store: 工程（只读取 worlds.json）。
        bible: 设定库 dict（就地修改）。
        characters: 本次生成使用的角色表（含新建角色）。

    返回:
        `bind_outline_context` 的统计结果。
    """
    world_store = WorldStore(store.root)
    worlds = world_store.list_worlds() if world_store.exists() else []
    return bind_outline_context(bible, worlds, characters)
