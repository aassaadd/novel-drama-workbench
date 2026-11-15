"""大纲扩写的共用件：模式/重写阈值、模型增量上报、上下文与规范化。"""

from __future__ import annotations

import copy
from collections import Counter
from dataclasses import replace

from ...adapters.errors import AdapterError, ErrorCode
from ...adapters.llm.contracts import Message, TextGenRequest
from ...adapters.persistence import ProjectStore
from ...text.titles import strip_index_prefix
from ..support.llm_calls import (
    _progress,
)
from ..support.llm_schemas import _gen_tool

#: 支持的插入方式
MODES = ("after_act", "after_chapter", "append")

#: 重写上限：连续两次仍越界就停下报告
REWRITE_LIMIT = 2

def _text(value, limit: int = 300) -> str:
    """取字符串并裁掉超长部分。

    参数:
        value: 任意值。
        limit: 最大长度。

    返回:
        去空白后的字符串。
    """
    return str(value or "").strip()[:limit]

def _most_common(values: list[int], fallback: int) -> int:
    """取出现次数最多的值（用于推断已有大纲的常用章数/节拍数）。

    参数:
        values: 候选值列表。
        fallback: 列表为空时的兜底值。

    返回:
        整数。
    """
    rows = [int(item) for item in values if int(item or 0) > 0]
    if not rows:
        return max(1, int(fallback))
    return Counter(rows).most_common(1)[0][0]

def _bounded_tool(step: str, expected: int, tol: int) -> dict:
    """给工具数组加 minItems/maxItems，从 schema 层约束模型给出的数量。

    之前章数只写在提示词里，schema 没有数量约束，模型很容易一次吐 20 章；
    这里把允许区间直接写进工具定义，配合逐层闸门双重把关。

    参数:
        step: outline_acts / outline_chapters / outline_beats。
        expected: 目标数量。
        tol: 允许偏差。

    返回:
        新的工具定义 dict（深拷贝，避免改动模块级 schema）。
    """
    tool = copy.deepcopy(_gen_tool(step) or {})
    key = {"outline_acts": "acts", "outline_chapters": "chapters", "outline_beats": "beats"}.get(step)
    node = ((tool.get("function") or {}).get("parameters") or {}).get("properties", {}).get(key or "")
    if isinstance(node, dict):
        node["minItems"] = max(1, expected - tol)
        node["maxItems"] = expected + tol
    return tool

def _delta(state: dict, on_progress, unit: str, index: int, total: int, name: str, extra: dict | None = None):
    """构造统计接收字数的增量回调。

    参数:
        state: 累积状态 `{"chars": n}`。
        on_progress: 进度回调。
        unit: act / beat（页面据此选择文案模板，不能把章数写成「幕」）。
        index: 当前序号（幕阶段为 0，表示尚未拆分）。
        total: 当前层总数。
        name: 当前项名称。
        extra: 附加字段（phase / act_index / chapter_index / percent 等）。

    返回:
        on_delta 函数。
    """

    def on_delta(text: str) -> None:
        """接收增量并上报 chars 事件。"""
        if not text:
            return
        state["chars"] += len(str(text))
        payload = {"type": "chars", "chars": state["chars"], "unit": unit, "index": index, "total": total, "name": name}
        payload.update(extra or {})
        _progress(on_progress, payload)

    return on_delta

def _beat_delta(
    on_progress,
    done_before: int,
    beat_total: int,
    chapter_index: int,
    chapter_total: int,
    act_index: int,
    act_total: int,
    chapter_title: str,
):
    """节拍阶段的增量回调：按已出现的节拍概要数上报「第 y/200 节拍」。"""
    state = {"chars": 0, "raw": ""}

    def on_delta(text: str) -> None:
        """接收增量并按节拍概要计数。"""
        if not text:
            return
        state["chars"] += len(str(text))
        state["raw"] = (state.get("raw") or "") + str(text)
        index = min(beat_total, done_before + max(1, state["raw"].count('"summary"')))
        _progress(
            on_progress,
            {
                "type": "chars",
                "chars": state["chars"],
                "unit": "beat",
                "phase": "beats",
                "index": index,
                "total": beat_total,
                "beat_index": index,
                "beat_total": beat_total,
                "act_index": act_index,
                "act_total": act_total,
                "chapter_index": chapter_index,
                "chapter_total": chapter_total,
                "chapter": chapter_title,
            },
        )

    return on_delta

def _outline_digest(acts: list[dict], limit: int = 30) -> str:
    """把已有大纲压成提示词可用的文本。

    参数:
        acts: 已有幕列表。
        limit: 最多列出的章节数（避免提示词过长）。

    返回:
        多行文本。
    """
    lines: list[str] = []
    shown = 0
    for act_pos, act in enumerate(acts, start=1):
        chapters = act.get("chapters") or []
        lines.append(f"第 {act_pos} 幕《{act.get('title') or ''}》：{_text(act.get('summary'), 200)}（共 {len(chapters)} 章）")
        titles = []
        for chapter in chapters:
            if shown >= limit:
                titles.append("……")
                break
            shown += 1
            titles.append(f"第 {chapter.get('index')} 章《{chapter.get('title') or ''}》")
        if titles:
            lines.append("  " + "、".join(titles))
    return "\n".join(lines)

def _written_digest(store: ProjectStore, acts: list[dict], limit: int = 12) -> str:
    """列出已写章节的正文摘要，供扩充时保持连贯。

    参数:
        store: 工程。
        acts: 已有幕列表。
        limit: 最多列出多少章已写正文。

    返回:
        多行文本；没有正文时返回空串。
    """
    summaries = {}
    for act in acts:
        for chapter in act.get("chapters") or []:
            if chapter.get("id"):
                summaries[chapter["id"]] = _text(chapter.get("summary"), 120)
    rows: list[str] = []
    for item in store.list_chapters():
        if len(rows) >= limit:
            break
        try:
            chapter = store.load_chapter(item["id"])
        except (OSError, ValueError):
            continue
        content = _text(chapter.get("content"), 160)
        if not content:
            continue
        title = chapter.get("title") or "未命名"
        outline = summaries.get(item["id"]) or ""
        rows.append(f"第 {chapter.get('index') or '?'} 章《{title}》" + (f"（大纲：{outline}）" if outline else "") + f"：{content}")
    return "\n".join(rows)

def _base_context(store: ProjectStore, bible: dict, work: dict, chars: list[dict], acts: list[dict]) -> str:
    """拼扩充/续写共用的作品上下文。

    参数:
        store: 工程。
        bible: 设定库。
        work: 作品信息。
        chars: 可用人物（不含旁白）。
        acts: 已有幕列表。

    返回:
        多行文本。
    """
    lines = [f"作品：《{work.get('title') or '未命名'}》"]
    logline = (bible.get("premise") or {}).get("logline")
    if logline:
        lines.append(f"一句话故事：{logline}")
    ending = (bible.get("premise") or {}).get("ending_intent")
    if ending:
        lines.append(f"结局意图：{_text(ending, 300)}")
    genres = work.get("genre") or []
    if genres:
        lines.append("题材：" + "、".join(str(item) for item in genres))
    if chars:
        lines.append("可用人物（id=姓名）：" + "、".join(f"{item['id']}={item.get('name')}" for item in chars))
    lines.append("")
    lines.append("已有大纲：")
    lines.append(_outline_digest(acts))
    written = _written_digest(store, acts)
    if written:
        lines.append("")
        lines.append("已写章节正文摘要（保持连贯，不要重复这些情节）：")
        lines.append(written)
    return "\n".join(lines)

def _normalize_acts(payload: dict, expected: int) -> list[dict]:
    """清洗阶段一的幕列表。

    参数:
        payload: `emit_acts` 的返回值。
        expected: 目标幕数。

    返回:
        幕列表，每项 `{title, summary, chapters: []}`；
        未返回可用幕时返回空列表（交给幕数闸门重写，而不是直接记失败）。
    """
    raw = payload.get("acts") if isinstance(payload, dict) else None
    if not isinstance(raw, list) or not raw:
        return []
    rows: list[dict] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "title": strip_index_prefix(_text(item.get("title"), 80)) or f"新增第{index}幕",
                "summary": _text(item.get("summary") or item.get("goal"), 400),
                "chapters": [],
            }
        )
    return rows

def _normalize_chapters(payload: dict) -> list[dict]:
    """清洗阶段二的章节列表。

    参数:
        payload: `emit_chapters` 的返回值。

    返回:
        章节列表，每项 `{title, summary, beats: []}`；
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
                "title": strip_index_prefix(_text(item.get("title"), 80)) or f"新增第{index}章",
                "summary": _text(item.get("summary"), 400),
                "beats": [],
            }
        )
    return rows

def _normalize_beats(payload: dict, expected: int, chapter_words: int, beats_per_chapter: int) -> list[dict]:
    """清洗阶段三的节拍列表。

    参数:
        payload: `emit_beats` 的返回值。
        expected: 本章计划节拍数。
        chapter_words: 单章目标字数。
        beats_per_chapter: 每章节拍数（用于均分字数）。

    返回:
        节拍列表；未返回可用节拍时返回空列表
        （交给节拍数闸门按数量越界重写，而不是直接记失败）。
    """
    raw = payload.get("beats") if isinstance(payload, dict) else None
    if not isinstance(raw, list) or not raw:
        return []
    default_words = max(1, int(chapter_words) // max(1, int(beats_per_chapter)))
    rows: list[dict] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            continue
        try:
            words = int(item.get("target_words") or default_words)
        except (TypeError, ValueError):
            words = default_words
        rows.append(
            {
                "summary": _text(item.get("summary"), 240) or f"第{index}个节拍",
                "character_ids": [str(value) for value in (item.get("character_ids") or []) if str(value).strip()],
                "extra_characters": [
                    _text(name, 60) for name in (item.get("extra_characters") or []) if _text(name, 60)
                ],
                "turning_point": bool(item.get("turning_point")),
                "target_words": words,
            }
        )
    return rows

def _correction(label: str, got: int, expected: int) -> str:
    """生成「数量不符」的纠正提示。

    参数:
        label: 幕 / 章 / 个节拍。
        got: 上一次实际数量（0 表示模型没返回任何条目）。
        expected: 设定数量。

    返回:
        中文提示；空数组时明确写「没有返回任何」。
    """
    noun = "章节" if label == "章" else ("节拍" if "节拍" in label else label)
    if got <= 0:
        return f"上一版没有返回任何{noun}，本次需要 {expected} {label}。请完整给出 {expected} {label}，一条都不要省略。"
    return f"上一版给了 {got} {label}，本次要求 {expected} {label}（允许 ±1，且不超过 20%）。请只输出 {expected} {label}，不要多给或少给。"

def _count_reason(kind: str, got: int, expected: int) -> str:
    """生成数量不符时的进度说明（空数组写明「为空」，供页面直接展示）。

    参数:
        kind: acts / chapters / beats。
        got: 上一次实际数量。
        expected: 设定数量。

    返回:
        中文说明。
    """
    if kind == "beats":
        return f"本章节拍为空，需要 {expected} 个节拍" if got <= 0 else f"上一版给了 {got} 个节拍，本章要求 {expected} 个"
    if kind == "chapters":
        return f"本幕章节为空，需要 {expected} 章" if got <= 0 else f"上一版给了 {got} 章，本幕要求 {expected} 章"
    return f"上一版给了 {got} 幕，本次要求 {expected} 幕"

def _gate_error(stage: str, message: str, **detail) -> AdapterError:
    """构造逐层闸门错误（页面据此显示卡住位置）。

    参数:
        stage: acts / chapters / beats。
        message: 中文说明。
        detail: 附加字段（act_index/attempts/expected/actual/reason 等）。

    返回:
        AdapterError。
    """
    payload = {"stage": stage, "gate": True, "resume": False}
    payload.update(detail)
    payload.setdefault(
        "failures",
        [{"act_index": detail.get("act_index", 1), "act_name": detail.get("act_name") or "扩充", "kind": f"{stage} 层越界", "raw": ""}],
    )
    return AdapterError(ErrorCode.UPSTREAM_ERROR, message, retryable=True, detail=payload)

def _rewrite(req: TextGenRequest, hint: str) -> TextGenRequest:
    """在原有消息后追加一条纠正提示。

    参数:
        req: 原请求。
        hint: 纠正提示。

    返回:
        新请求。
    """
    return replace(req, messages=list(req.messages) + [Message("user", hint)])
