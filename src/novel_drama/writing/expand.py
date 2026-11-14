"""大纲扩充与续写：在某幕后 / 某章后插入新内容，或在全书结尾续写。

与 `outline_hier` 使用同一套「幕 → 章 → 节拍」逐层闸门：

- 每一层生成后立即校验数量（幕数 ±1、章数 ±1 且不超过 20%、节拍数 ±20%）；
- 越界只重写该层（最多 2 次，带「上一版给了 N，本次要求 M」的纠正提示），不裁剪模型产出；
- 仍越界就抛 `AdapterError`，`detail` 里写明层级、设定值、实际值、尝试次数与原因。

插入策略（只新增，不重写已有内容）：

- 已有幕/章沿用原 ID，`beats` 与章节文件的正文、标注、配音都按 ID 引用，因此不会失效；
- 插入后大纲里的章序号全局重排，已有的 `chapters/*.json` 只同步 `index` 字段（ID 不变）；
- 不在大纲里的章节文件保持原序号，若与大纲序号冲突则顺延到大纲之后；
- 新增对象带 `origin` 标注来源（expansion/continuation）、插入方式与锚点 ID，供界面区分。
"""

from __future__ import annotations

import copy
from collections import Counter
from dataclasses import replace

from ..adapters.base import AdapterError, ErrorCode, Message, TextGenRequest
from ..config import AppConfig
from ..models.ids import new_id, now_iso
from ..store import ProjectStore
from ..store.settings_store import SettingsStore
from ..text.titles import strip_index_prefix
from .service import (
    _act_validation,
    _characters_payload,
    _generate_json,
    _gen_tool,
    _progress,
    _tolerance,
    _tool_max_tokens,
    _validation_report,
    bind_outline_context,
    build_writing_adapter,
    outline_binding_stats,
    sanitize_outline_character_ids,
)

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
        max_tokens=_tool_max_tokens(config, "outline_acts", expected),
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
        max_tokens=_tool_max_tokens(config, "outline_chapters", expected),
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
        节拍列表（未裁剪）。

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
    ]
    req = TextGenRequest(
        task="outline",
        system="你是长篇小说的结构编辑，只按工具参数输出本章全部节拍，不要解释。",
        messages=[Message("user", "\n".join(lines))],
        stream=True,
        max_tokens=_tool_max_tokens(config, "outline_beats", expected),
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
    return rows


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
                )
            )
        built["chapters"].append(outline_chapter)
    return built


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


def expand_outline(
    store: ProjectStore,
    config: AppConfig,
    mode: str = "append",
    anchor_act: int | None = None,
    anchor_chapter: int | None = None,
    new_acts: int = 1,
    new_chapters: int | None = None,
    chapters_per_act: int | None = None,
    beats_per_chapter: int | None = None,
    chapter_words: int | None = None,
    brief: str = "",
    dry_run: bool = False,
    binding: str = "auto",
    on_progress=None,
) -> dict:
    """扩充或续写大纲：只新增，不重写已有内容。

    参数:
        store: 工程。
        config: 本机配置。
        mode: after_act=在某幕后插入；after_chapter=在某章后插入；append=结局后续写。
        anchor_act: 锚点幕序号（1 开始），after_act 必填。
        anchor_chapter: 锚点章序号（全书序号，1 开始），after_chapter 必填。
        new_acts: 新增幕数（after_act / append）。
        new_chapters: 新增章数（after_chapter），默认沿用每幕章数。
        chapters_per_act: 每幕章数；省略时按已有大纲推断。
        beats_per_chapter: 每章节拍数；省略时按已有大纲推断。
        chapter_words: 单章目标字数；省略时按已有大纲推断。
        brief: 用户补充要求。
        dry_run: 只生成不落盘。
        binding: auto=把节拍绑定到已有人物（非法引用过滤后回落到 POV）；
            none=只过滤非法角色引用（人名/空串/重复 ID），不回落、不改世界绑定。
        on_progress: 进度回调（stage/batch/chars/rewrite）。

    返回:
        `{mode, inserted_acts, inserted_chapters, validation, summary, total, ...}`。

    异常:
        AdapterError: 没有大纲、锚点不合法，或某层重写后仍越界。
    """
    settings_store = SettingsStore(store.root)
    bible = settings_store.ensure()
    acts = (bible.get("outline") or {}).get("acts") or []
    if not acts:
        raise AdapterError(ErrorCode.INVALID_INPUT, "还没有大纲，无法扩充或续写：请先生成一份完整的分层大纲。")
    mode = mode if mode in MODES else "append"
    project = store.load_project()
    work_info = project.get("work") or {}
    characters = [item for item in store.load_characters()["characters"] if item.get("role") != "narrator"]
    char_rows = _characters_payload(characters)
    adapter = build_writing_adapter(config, bible)

    # 推断默认值：优先沿用参数，其次取已有大纲里最常见的规模
    inferred_chapters = _most_common([len(act.get("chapters") or []) for act in acts], 5)
    beat_counts = [len(chapter.get("beats") or []) for act in acts for chapter in (act.get("chapters") or [])]
    inferred_beats = _most_common(beat_counts, 4)
    word_sums = [
        sum(int(beat.get("target_words") or 0) for beat in (chapter.get("beats") or []))
        for act in acts
        for chapter in (act.get("chapters") or [])
    ]
    inferred_words = _most_common([item for item in word_sums if item > 0], 3000)
    expected_chapters = max(1, int(chapters_per_act or inferred_chapters))
    expected_beats = max(1, int(beats_per_chapter or inferred_beats))
    expected_words = max(1, int(chapter_words or inferred_words))

    # 锚点定位与来源标注
    origin = {"kind": "continuation" if mode == "append" else "expansion", "mode": mode, "created_at": now_iso()}
    anchor_act_obj = None
    anchor_chapter_obj = None
    if mode == "after_act":
        index = max(1, min(len(acts), int(anchor_act or len(acts))))
        anchor_act_obj = acts[index - 1]
        anchor_act = index
        origin["anchor_act_id"] = anchor_act_obj.get("id")
        origin["anchor_act_title"] = anchor_act_obj.get("title") or ""
        placement = f"插入位置：第 {index} 幕《{anchor_act_obj.get('title') or ''}》之后（原有后续幕顺延）。"
    elif mode == "after_chapter":
        flat = [(act_pos, act, chapter) for act_pos, act in enumerate(acts, start=1) for chapter in (act.get("chapters") or [])]
        if not flat:
            raise AdapterError(ErrorCode.INVALID_INPUT, "大纲里还没有章节，无法在某章之后插入；请改用「在某幕之后」或「末尾续写」。")
        number = max(1, min(len(flat), int(anchor_chapter or len(flat))))
        act_pos, act_obj, chapter_obj = flat[number - 1]
        anchor_act = act_pos
        anchor_act_obj = act_obj
        anchor_chapter = number
        anchor_chapter_obj = chapter_obj
        origin["anchor_act_id"] = act_obj.get("id")
        origin["anchor_act_title"] = act_obj.get("title") or ""
        origin["anchor_chapter_id"] = chapter_obj.get("id")
        origin["anchor_chapter_title"] = chapter_obj.get("title") or ""
        placement = f"插入位置：第 {number} 章《{chapter_obj.get('title') or ''}》之后（同属第 {act_pos} 幕，后续章节顺延）。"
    else:
        placement = "插入位置：全书结局之后（第二部式延展）。"

    context = _base_context(store, bible, work_info, char_rows, acts)
    meta = {
        "title": work_info.get("title"),
        "logline": (bible.get("premise") or {}).get("logline"),
        "genre": work_info.get("genre") or [],
        "characters": char_rows,
        "acts": len(acts),
        "chapters_per_act": expected_chapters,
        "beats_per_chapter": expected_beats,
        "mode": mode,
        "placement": placement,
    }
    _progress(on_progress, {"type": "batch", "unit": "expand", "index": 1, "total": 1, "name": placement})

    new_blocks: list[dict] = []
    if mode == "after_chapter":
        count = max(1, int(new_chapters or expected_chapters))
        block = {"title": anchor_act_obj.get("title") or "新增章", "summary": "", "chapters": []}
        chapters = _gen_chapters(
            adapter,
            config,
            context,
            {**meta, "anchor_note": f"上一章是第 {anchor_chapter} 章《{anchor_chapter_obj.get('title') or ''}》，新章要承接它并在其后续章节之前收束。"},
            anchor_act,
            {"title": anchor_act_obj.get("title") or f"第{anchor_act}幕", "summary": anchor_act_obj.get("summary") or ""},
            count,
            brief,
            on_progress,
            len(acts),
            chapter_total=count,
            chapters_done=0,
        )
        planned_beats = count * expected_beats
        for offset, chapter in enumerate(chapters, start=1):
            chapter["beats"] = _gen_beats(
                adapter,
                config,
                context,
                meta,
                anchor_act,
                anchor_act_obj,
                offset,
                chapter,
                expected_beats,
                expected_words,
                on_progress,
                len(acts),
                chapter_total=count,
                beats_before=(offset - 1) * expected_beats,
                beat_total=planned_beats,
            )
        block["chapters"] = chapters
        new_blocks.append(block)
    else:
        count = max(1, int(new_acts or 1))
        work_acts = _gen_acts(adapter, config, context, meta, count, brief, on_progress)
        planned_chapters = count * expected_chapters
        planned_beats = planned_chapters * expected_beats
        for act_pos, act in enumerate(work_acts, start=1):
            act_name = act.get("title") or f"新增第{act_pos}幕"
            _progress(
                on_progress,
                {
                    "type": "batch",
                    "unit": "act",
                    "phase": "chapters",
                    "index": act_pos,
                    "total": len(work_acts),
                    "name": act_name,
                    "chapters_done": (act_pos - 1) * expected_chapters,
                    "chapters_expected": expected_chapters,
                    "chapters_total": planned_chapters,
                },
            )
            act["chapters"] = _gen_chapters(
                adapter,
                config,
                context,
                meta,
                act_pos,
                act,
                expected_chapters,
                brief,
                on_progress,
                len(work_acts),
                chapter_total=count * expected_chapters,
                chapters_done=(act_pos - 1) * expected_chapters,
            )
            for local, chapter in enumerate(act["chapters"], start=1):
                chapter_pos = (act_pos - 1) * expected_chapters + local
                chapter["beats"] = _gen_beats(
                    adapter,
                    config,
                    context,
                    meta,
                    act_pos,
                    act,
                    chapter_pos,
                    chapter,
                    expected_beats,
                    expected_words,
                    on_progress,
                    len(work_acts),
                    chapter_total=planned_chapters,
                    beats_before=(chapter_pos - 1) * expected_beats,
                    beat_total=planned_beats,
                )
            new_blocks.append(act)

    # 插入：新内容先转成设定库结构，再按模式拼接；已有对象原样保留
    built_blocks = [
        _to_outline_act(block, origin, expected_beats, expected_words) for block in new_blocks
    ]
    if mode == "after_act":
        insert_at = int(anchor_act or len(acts))
        acts[insert_at:insert_at] = built_blocks
    elif mode == "after_chapter":
        # 新章插在锚点章之后；章对象来自同一幕的章节块
        act_obj = anchor_act_obj
        position = list(act_obj.get("chapters") or []).index(anchor_chapter_obj) + 1
        act_obj["chapters"][position:position] = built_blocks[0]["chapters"]
    else:
        acts.extend(built_blocks)

    bible.setdefault("outline", {})["acts"] = acts
    _renumber(bible, store, dry_run=dry_run)
    # 角色引用：先清洗非法引用（人名/空串/重复 ID）；auto 再按幕绑定世界并回落到 POV，none 只清洗
    # 只处理本次新增的章节，旧章节的引用保持原样
    new_view = (
        {"outline": {"acts": [{"chapters": built_blocks[0]["chapters"]}]}}
        if mode == "after_chapter"
        else {"outline": {"acts": built_blocks}}
    )
    stats = sanitize_outline_character_ids(new_view, characters)
    ignored_character_refs = int(stats["dropped"])
    if binding == "auto":
        # 只给新增章节绑世界/POV，已合格的旧章节保持原绑定不动
        from ..store.world_store import WorldStore

        world_store = WorldStore(store.root)
        worlds = world_store.list_worlds() if world_store.exists() else []
        if mode == "after_chapter":
            bind_outline_context({"outline": {"acts": [{"chapters": built_blocks[0]["chapters"]}]}}, worlds, characters)
        else:
            bind_outline_context({"outline": {"acts": built_blocks}}, worlds, characters)
    if ignored_character_refs:
        suffix = "已回落到本章 POV" if binding == "auto" else "binding=none 不自动选角"
        message = f"已忽略 {ignored_character_refs} 个无法识别的角色引用（{suffix}）"
        _progress(on_progress, {"type": "notice", "message": message})
    bound_stats = outline_binding_stats(new_view)
    if not dry_run:
        bible = settings_store.save(bible)

    # 逐层校验摘要：只看新增部分，设定值与实际值都列清楚
    rows: list[dict] = []
    inserted_acts: list[dict] = []
    inserted_chapters: list[dict] = []
    if mode == "after_chapter":
        # 某章后扩充只新增章：校验口径按「本次新增章数」算，位置沿用锚点幕
        block = built_blocks[0]
        expected_rows = max(1, len(block.get("chapters") or []))
        row = _act_validation(
            {"title": anchor_act_obj.get("title") or f"第{anchor_act}幕", "chapters": block.get("chapters") or []},
            anchor_act,
            expected_rows,
            expected_beats,
            expected_words,
            "",
        )
        row["origin"] = "新增章"
        rows.append(row)
        for chapter in block.get("chapters") or []:
            inserted_chapters.append(
                {
                    "chapter_id": chapter.get("id"),
                    "index": chapter.get("index"),
                    "title": chapter.get("title"),
                    "act_index": anchor_act,
                }
            )
        report_chapters = expected_rows
    else:
        for block in built_blocks:
            act_row = next((item for item in acts if item.get("id") == block.get("id")), None)
            position = acts.index(act_row) + 1 if act_row else 1
            row = _act_validation({"title": block.get("title"), "chapters": block.get("chapters")}, position,
                                  expected_chapters, expected_beats, expected_words, "")
            row["origin"] = "新增"
            rows.append(row)
            inserted_acts.append(
                {
                    "act_id": block.get("id"),
                    "title": block.get("title"),
                    "position": position,
                    "chapters": len(block.get("chapters") or []),
                }
            )
            for chapter in block.get("chapters") or []:
                inserted_chapters.append(
                    {
                        "chapter_id": chapter.get("id"),
                        "index": chapter.get("index"),
                        "title": chapter.get("title"),
                        "act_index": position,
                    }
                )
        report_chapters = expected_chapters

    report = _validation_report(rows, report_chapters, expected_beats, expected_words, len(rows) or 1)
    if mode == "after_chapter":
        # 只新增章的场景不写「幕」的合计，避免出现「设定 1 幕」的误导
        row = rows[0]
        report_text = (
            f"第 {anchor_act} 幕《{row['title']}》新增章：设定 {row['expected']['chapters']} 章 / "
            f"实际 {row['actual']['chapters']} 章（偏差 {row['delta']['chapters']:+d}，允许 "
            f"{row['range']['chapters'][0]}–{row['range']['chapters'][1]} 章）"
            + ("✓ 在允许区间" if row["in_range"] else "⚠ 超出允许区间")
            + (("；原因：" + "；".join(row["reasons"])) if row["reasons"] else "")
        )
    else:
        report_text = report["summary"]
    total_chapters = sum(len(act.get("chapters") or []) for act in acts)
    total_beats = sum(len(chapter.get("beats") or []) for act in acts for chapter in (act.get("chapters") or []))
    label = "续写" if mode == "append" else "扩充"
    lines = [f"{placement}", report_text,
             f"全书现为 {len(acts)} 幕 / {total_chapters} 章 / {total_beats} 个节拍；已有内容未改动，只新增。"]
    new_act_count = 0 if mode == "after_chapter" else len(built_blocks)
    return {
        "mode": mode,
        "origin": origin,
        "label": label,
        "inserted_acts": inserted_acts,
        "inserted_chapters": inserted_chapters,
        "expected": {"acts": new_act_count, "chapters_per_act": report_chapters, "beats_per_chapter": expected_beats},
        "actual": {"acts": new_act_count, "chapters": len(inserted_chapters)},
        "matches": report["matches"],
        "validation": report,
        "summary": "\n".join(lines),
        "total": {"acts": len(acts), "chapters": total_chapters, "beats": total_beats},
        "provider": getattr(adapter, "model", "") or "offline-writing",
        "settings_revision": bible.get("settings_revision"),
        "binding": binding,
        "ignored_character_refs": ignored_character_refs,
        "bound_characters": bound_stats["characters"],
        "bound_character_refs": bound_stats["refs"],
        "dry_run": dry_run,
    }
