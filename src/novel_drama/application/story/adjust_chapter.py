"""用例：超纲压缩与不足补写。"""

from __future__ import annotations

import logging
from dataclasses import replace

from ...adapters.base import (
    AdapterError,
    ErrorCode,
    Message,
    TextGenRequest,
)
from ...config import AppConfig, resolve_temperature
from ...domain.story.rules.chapter_rules import bump_revision
from ...models.ids import now_iso
from ...store import ProjectStore
from ...text.dialogue import normalize_dialogue
from . import chapter_checkpoint
from .chapter_context import build_chapter_context
from .chapter_limits import OVERSHOOT_ACTION_RATIO, UNDERFILL_ACTION_RATIO
from .chapter_rounds import _generate_validated
from .chapter_text import (
    _append_segment,
    _count_words,
    _split_beat_sections,
    _split_supplement_sections,
    _uncovered_beats,
    _word_deviation,
)

logger = logging.getLogger(__name__)


def _compress_prompt(chapter: dict, content: str, beats: list[dict], target_words: int) -> str:
    """构造「按目标字数压缩」的提示词：逐节拍分段，保住全部要点。

    参数:
        chapter: 章节 dict（用于标题）。
        content: 现有正文。
        beats: 本章节拍列表。
        target_words: 目标字数。

    返回:
        中文提示词。
    """
    beat_lines = []
    for index, beat in enumerate(beats):
        beat_words = int(beat.get("target_words") or 0)
        suffix = f"（目标约 {beat_words} 字）" if beat_words else ""
        beat_lines.append(f"第 {index + 1} 个：{str(beat.get('summary') or '').strip() or '（无摘要）'}{suffix}")
    return "\n".join(
        [
            f"下面是《{chapter.get('title') or ''}》已写好的正文，明显超出目标字数，需要压缩。",
            f"目标字数：{target_words} 字；当前字数：{_count_words(content)} 字。",
            "",
            "本章节拍（必须全部保留，不得删除、合并或跳过；每个节拍的核心事件、人物关系、转折与结果都不能丢）：",
            *beat_lines,
            "",
            "要求：",
            "1. 按【节拍 1】【节拍 2】…逐段输出压缩后的正文，段数和顺序与上面的节拍一一对应；",
            "2. 只压缩描写、铺陈、重复表达与冗余过渡，不得删除任何节拍，也不得把两个节拍并成一段；",
            "3. 不新增情节、不改变人物关系与结局，不输出解释、字数统计或除分段标记外的任何标题；",
            "4. 尽量压缩到目标字数附近（允许小幅浮动），保持原有人称、时态与叙事口吻。",
            "",
            "【原文】",
            content,
        ]
    )

def compress_chapter(
    store: ProjectStore,
    config: AppConfig,
    chapter_id: str,
    on_delta=None,
    on_notice=None,
    on_progress=None,
) -> dict:
    """把已有章节正文按目标字数压缩，逐节拍分段校验，保住全部节拍要点。

    只在用户点击「按目标字数压缩」时调用，不自动触发。压缩结果必须按
    「【节拍 N】」逐段覆盖 1..N，缺段或多段都拒绝写入并报中文原因；
    成功后写入新版本（revision +1），保留原摘要不重新生成。

    参数:
        store: 工程。
        config: 本机配置。
        chapter_id: 章节 ID。
        on_delta: 流式增量回调；传入时改走流式。
        on_notice: 自动补救说明回调（格式重试成功时上报）。
        on_progress: 进度回调；上报 `{type, round, rounds_total, chars}` 供页面显示。

    返回:
        与 `generate_chapter` 同形的结果 dict，另含 `before_words`、
        `overshoot_note`，`mode="compress"`。

    异常:
        ValueError: 本章还没有正文。
        AdapterError: 压缩结果为空、缺少节拍或不是小说正文。
    """
    chapter = store.load_chapter(chapter_id)
    content = (chapter.get("content") or "").strip()
    if not content:
        raise ValueError(f"章节 {chapter_id} 还没有正文，无法压缩；请先生成正文")

    context = build_chapter_context(store, config, chapter)
    beats = context["beats"] or []
    target_words = int(context["metadata"].get("target_words") or context["chapter_words"] or 0)
    before_words = int(chapter.get("word_count") or 0) or _count_words(content)
    notices: list[str] = []
    if on_progress:
        on_progress({"type": "round", "round": 1, "rounds_total": 1, "beats_done": 0, "chars": 0})

    def notify(text: str) -> None:
        """收集压缩过程的自动补救说明。"""
        notices.append(text)
        if on_notice:
            on_notice(text)

    request = TextGenRequest(
        task="chapter_compress",
        system="你是资深小说编辑，只在保留全部节拍要点的前提下压缩正文。只输出压缩后的正文。",
        messages=[Message("user", _compress_prompt(chapter, content, beats, target_words))],
        stream=on_delta is not None,
        temperature=resolve_temperature(config, "writing"),
        metadata={
            "chapter_id": chapter_id,
            "index": chapter.get("index"),
            "title": chapter.get("title"),
            "target_words": target_words,
            "watchdog_target_words": target_words,
            "with_summary": False,
            "before_words": before_words,
            "thinking_stage": "compress",
            "beats": [
                {"summary": str(beat.get("summary") or ""), "target_words": int(beat.get("target_words") or 0)}
                for beat in beats
            ],
            "content": content,
        },
    )

    def splitter(text: str) -> tuple[str, dict | None, str | None]:
        """按节拍分段拆出正文；缺段时返回中文原因。"""
        body, error = _split_beat_sections(text, len(beats))
        return body, None, error

    counter = {"calls": 0}
    body, cover_error = "", None
    result = None
    for attempt in range(2):
        result, body, _payload, cover_error = _generate_validated(
            context["adapter"],
            request,
            on_delta,
            "压缩正文",
            on_notice=notify,
            counter=counter,
            extract=splitter,
            target_chars=target_words,
            signals=("suffix",),
        )
        if not cover_error:
            break
        logger.warning("压缩结果未保住全部节拍：%s；追加约束后重试一次", cover_error)
        if attempt == 0:
            request = replace(
                request,
                messages=list(request.messages)
                + [
                    Message(
                        "user",
                        f"上一次输出不合格（{cover_error}）。请严格按【节拍 1】…【节拍 {max(1, len(beats))}】"
                        "逐段输出，每个节拍都要有正文，不要合并或省略。",
                    )
                ],
            )
    if cover_error:
        raise AdapterError(
            ErrorCode.UPSTREAM_ERROR,
            f"压缩结果未保住全部节拍（{cover_error}）：未写入章节，请重试或改用「重写」",
            retryable=True,
        )

    body = (body or "").strip()
    if not body:
        raise AdapterError(ErrorCode.UPSTREAM_ERROR, "压缩模型返回了空正文", retryable=True)
    body, dialogue_warnings = normalize_dialogue(
        body, [str(item.get("name") or "") for item in context["characters"]]
    )
    words = _count_words(body)
    word_deviation = _word_deviation(words, target_words)
    before_ratio = round(before_words / target_words, 2) if target_words else None
    ratio_text = f"{before_ratio} 倍" if before_ratio is not None else "倍率未知"
    overshoot_note = (
        f"已按「按目标字数压缩」处理：原 {before_words} 字（目标 {target_words} 字，{ratio_text}，"
        f"达到 {OVERSHOOT_ACTION_RATIO} 倍动作阈值）→ 压缩后 {words} 字；"
        f"本章 {len(beats)} 个节拍全部保留"
    )

    chapter["content"] = body + "\n"
    chapter["word_count"] = words
    chapter["status"] = "written"
    bump_revision(chapter)
    chapter["updated_at"] = now_iso()
    store.save_chapter(chapter)
    # 压缩替换了正文：旧的生成断点已失去上下文，直接作废，避免下次续写覆盖压缩结果
    chapter_checkpoint.clear(store, chapter_id)

    return {
        "chapter_id": chapter_id,
        "status": chapter["status"],
        "words": words,
        "word_count": words,
        "provider": result.model if result is not None else "",
        "content": None,
        "rounds": 0,
        "beats_total": len(beats),
        "beats_done": len(beats),
        "beats_remaining": 0,
        "multi_turn": False,
        "stop_reason": "compressed",
        "word_deviation": word_deviation,
        "dialogue_warnings": dialogue_warnings,
        "notices": notices,
        "summary": None,
        "summary_error": None,
        "summary_mode": None,
        "resume": {"resumed": False},
        "mode": "compress",
        "overshoot_note": overshoot_note,
        "before_words": before_words,
        "requests": {"body": int(counter["calls"]), "summary_fallback": 0, "total": int(counter["calls"])},
        "revision": chapter["revision"],
    }

def _supplement_prompt(
    chapter: dict,
    content: str,
    target_words: int,
    gap_words: int,
    uncovered: list[tuple[int, dict]],
    tail_chars: int = 1200,
) -> str:
    """构造「补写」提示词：按缺口字数与未覆盖要点续写，只输出新增正文。

    参数:
        chapter: 章节 dict（用于标题）。
        content: 现有正文。
        target_words: 本章目标字数。
        gap_words: 缺口字数（目标 − 现有）。
        uncovered: `_uncovered_beats` 的结果。
        tail_chars: 附上的正文结尾字数（让模型接得上）。

    返回:
        中文提示词。
    """
    actual = _count_words(content)
    lines = [
        f"《{chapter.get('title') or ''}》已写好的正文只有 {actual} 字，比目标 {target_words} 字少约 {gap_words} 字，需要补写。",
        "要求：",
        f"1. 只输出新增正文，紧接已有正文的结尾继续写，约 {gap_words} 字；不要复述已有内容，不要重写已经写过的情节。",
        "2. 禁止复述同一句对白、同一句式或同一段落结构；每次对话都要推进新信息，禁止原地来回的重复对白。",
    ]
    if uncovered:
        lines.append("3. 按下列要点逐段补写，每段都要有正文，不得省略、合并或跳过：")
        for number, beat in uncovered:
            summary = str(beat.get("summary") or "").strip() or "（无摘要）"
            lines.append(f"【补写节拍 {number}】：{summary}")
        lines.append(
            "输出格式：每段以「【补写节拍 N】」开头，段与段之间空一行；"
            "除这些标记外不要写标题、不要解释、不要输出字数统计。"
        )
    else:
        lines.append("3. 本章节拍要点已覆盖，请顺着上文继续推进情节、补足篇幅，同样不要复述已有内容。")
        lines.append("输出格式：只输出新增正文，不要写标题、不要解释、不要输出字数统计。")
    lines.extend(["", "【已有正文结尾】", content[-tail_chars:] if tail_chars > 0 else content])
    return "\n".join(lines)

def supplement_chapter(
    store: ProjectStore,
    config: AppConfig,
    chapter_id: str,
    on_delta=None,
    on_notice=None,
    on_progress=None,
) -> dict:
    """按缺口字数与未覆盖要点给已有正文补写，并合并为新版本。

    只在用户点击「补写」时调用，不自动触发。补写只追加、不改写已有正文；
    有未覆盖要点时要求模型按「【补写节拍 N】」逐段输出，缺段即拒绝写入。

    参数:
        store: 工程。
        config: 本机配置。
        chapter_id: 章节 ID。
        on_delta: 流式增量回调；传入时改走流式。
        on_notice: 自动补救说明回调（重试成功时上报）。
        on_progress: 进度回调；上报 `{type, round, rounds_total, chars}` 供页面显示。

    返回:
        与 `generate_chapter` 同形的结果 dict，另含 `before_words`、`added_words`、
        `gap_words`、`supplemented_beats`、`overshoot_note`，`mode="supplement"`。

    异常:
        ValueError: 本章还没有正文，或当前已不低于补写阈值。
        AdapterError: 补写结果为空、缺少要求的要点或没有新增正文。
    """
    chapter = store.load_chapter(chapter_id)
    content = (chapter.get("content") or "").strip()
    if not content:
        raise ValueError(f"章节 {chapter_id} 还没有正文，无法补写；请先生成正文")

    # 补写会新增台词，同样需要角色绑定；未同步时先补一次识别（每章一次性）
    try:
        from ...application.cast.capture_cast import ensure_chapter_cast

        ensure_chapter_cast(store, config, chapter_id, on_notice=on_notice, on_progress=on_progress)
    except Exception as exc:  # noqa: BLE001 - 角色识别失败不阻断补写，但记录原因
        logger.warning("补写前角色识别失败：%s", exc)

    context = build_chapter_context(store, config, chapter)
    beats = context["beats"] or []
    target_words = int(context["metadata"].get("target_words") or context["chapter_words"] or 0)
    before_words = int(chapter.get("word_count") or 0) or _count_words(content)
    gap_words = max(0, target_words - before_words)
    if target_words > 0 and before_words >= int(target_words * UNDERFILL_ACTION_RATIO):
        raise ValueError(
            f"当前正文 {before_words} 字，已达到目标 {target_words} 字的 {UNDERFILL_ACTION_RATIO} 倍补写阈值，无需补写"
        )
    uncovered = _uncovered_beats(content, beats)
    notices: list[str] = []
    if on_progress:
        on_progress({"type": "round", "round": 1, "rounds_total": 1, "beats_done": 0, "chars": 0})

    def notify(text: str) -> None:
        """收集补写过程的自动补救说明。"""
        notices.append(text)
        if on_notice:
            on_notice(text)

    request = TextGenRequest(
        task="chapter_supplement",
        system="你是资深小说作者，只在已有正文之后续写补足篇幅，不改写已写内容。只输出新增正文。",
        messages=[Message("user", _supplement_prompt(chapter, content, target_words, gap_words, uncovered))],
        stream=on_delta is not None,
        temperature=resolve_temperature(config, "writing"),
        metadata={
            "chapter_id": chapter_id,
            "index": chapter.get("index"),
            "title": chapter.get("title"),
            "target_words": target_words,
            "watchdog_target_words": max(gap_words, 200),
            "with_summary": False,
            "before_words": before_words,
            "gap_words": gap_words,
            "thinking_stage": "supplement",
            "uncovered_beats": [
                {"index": number, "summary": str(beat.get("summary") or "")} for number, beat in uncovered
            ],
            "beats": [
                {"summary": str(beat.get("summary") or ""), "target_words": int(beat.get("target_words") or 0)}
                for beat in beats
            ],
            "content": content,
        },
    )

    expected = [number for number, _beat in uncovered]

    def splitter(text: str) -> tuple[str, dict | None, str | None]:
        """按「【补写节拍 N】」拆出新增正文；缺段时返回中文原因。"""
        body, error = _split_supplement_sections(text, expected)
        return body, None, error

    counter = {"calls": 0}
    body, cover_error = "", None
    result = None
    for attempt in range(2):
        result, body, _payload, cover_error = _generate_validated(
            context["adapter"],
            request,
            on_delta,
            "补写正文",
            on_notice=notify,
            counter=counter,
            extract=splitter,
            target_chars=max(gap_words, 200),
        )
        if not cover_error:
            break
        logger.warning("补写结果缺少要点：%s；追加约束后重试一次", cover_error)
        if attempt == 0:
            marks = "、".join(f"【补写节拍 {number}】" for number in expected)
            request = replace(
                request,
                messages=list(request.messages)
                + [
                    Message(
                        "user",
                        f"上一次输出不合格（{cover_error}）。请严格按 {marks} 逐段输出新增正文，"
                        "每个要点都要有正文，不要合并或省略。",
                    )
                ],
            )
    if cover_error:
        raise AdapterError(
            ErrorCode.UPSTREAM_ERROR,
            f"补写结果未覆盖要求的节拍要点（{cover_error}）：未写入章节，请重试或改用「重写」",
            retryable=True,
        )

    body = (body or "").strip()
    if not body:
        raise AdapterError(ErrorCode.UPSTREAM_ERROR, "补写模型返回了空正文", retryable=True)
    body, dialogue_warnings = normalize_dialogue(
        body, [str(item.get("name") or "") for item in context["characters"]]
    )
    # 只追加不改写：裁掉与已有结尾重叠的部分，避免拼接处复述
    merged = _append_segment(content, body).strip()
    words = _count_words(merged)
    added_words = words - before_words
    if added_words <= 0:
        raise AdapterError(
            ErrorCode.UPSTREAM_ERROR,
            "补写没有产生新增内容（与已有正文重复）：未写入章节，请重试",
            retryable=True,
        )
    word_deviation = _word_deviation(words, target_words)
    before_ratio = round(before_words / target_words, 2) if target_words else None
    ratio_text = f"{before_ratio} 倍" if before_ratio is not None else "倍率未知"
    if expected:
        beats_text = "第 " + "、".join(str(number) for number in expected) + " 个节拍"
    else:
        beats_text = "本章节拍要点已覆盖，按缺口字数顺延续写"
    overshoot_note = (
        f"已按「补写」处理：原 {before_words} 字（目标 {target_words} 字，{ratio_text}，"
        f"低于 {UNDERFILL_ACTION_RATIO} 倍补写阈值）→ 补写后 {words} 字，新增 {added_words} 字；"
        f"补写要点：{beats_text}"
    )

    chapter["content"] = merged + "\n"
    chapter["word_count"] = words
    chapter["status"] = "written"
    bump_revision(chapter)
    chapter["updated_at"] = now_iso()
    store.save_chapter(chapter)
    # 正文已变化：旧的生成断点已失去上下文，直接作废
    chapter_checkpoint.clear(store, chapter_id)

    return {
        "chapter_id": chapter_id,
        "status": chapter["status"],
        "words": words,
        "word_count": words,
        "provider": result.model if result is not None else "",
        "content": None,
        "rounds": 0,
        "beats_total": len(beats),
        "beats_done": len(beats),
        "beats_remaining": 0,
        "multi_turn": False,
        "stop_reason": "supplemented",
        "word_deviation": word_deviation,
        "dialogue_warnings": dialogue_warnings,
        "notices": notices,
        "summary": None,
        "summary_error": None,
        "summary_mode": None,
        "resume": {"resumed": False},
        "mode": "supplement",
        "overshoot_note": overshoot_note,
        "before_words": before_words,
        "added_words": added_words,
        "gap_words": gap_words,
        "supplemented_beats": [
            {"index": number, "summary": str(beat.get("summary") or "")} for number, beat in uncovered
        ],
        "requests": {"body": int(counter["calls"]), "summary_fallback": 0, "total": int(counter["calls"])},
        "revision": chapter["revision"],
    }
