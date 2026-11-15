"""章节正文的纯文本工具：字数、节拍切分、补写覆盖判定、字数偏差。"""

from __future__ import annotations

import re

from ...domain.story.rules.chapter_rules import count_words
from .chapter_limits import (
    OVERSHOOT_ACTION_RATIO,
    OVERSHOOT_ACTIONS,
    OVERSHOOT_NOTICE_RATIO,
    SUPPLEMENT_COVERAGE_RATIO,
    UNDERFILL_ACTION_RATIO,
    UNDERFILL_ACTIONS,
)
from .length_plan import length_instruction, length_plan


def _count_words(text: str) -> int:
    """统计正文有效字数（去掉空白后的字符数）。

    参数:
        text: 正文文本。

    返回:
        字数；None 视为 0。
    """
    return count_words(text)

def _split_beat_sections(text: str, total_beats: int) -> tuple[str, str | None]:
    """按「【节拍 N】」切分压缩结果，确认 1..N 全部存在且非空。

    这是「不能靠删节拍压缩」的结构化兜底：段数、顺序与非空都校验，
    通过后才拼回正文；正文里不保留分段标记。

    参数:
        text: 压缩模型原始输出。
        total_beats: 本章节拍总数；0 时不做分段要求。

    返回:
        `(body, error)`：body 为去掉标记后的正文；error 为中文原因（成功为 None）。
    """
    raw = (text or "").strip()
    if total_beats <= 0:
        return raw, None
    matches = list(re.finditer(r"(?m)^\s*【\s*节拍\s*(\d+)\s*】\s*", raw))
    if not matches:
        return "", "压缩结果缺少「【节拍 N】」分段标记"
    sections: dict[int, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(raw)
        segment = raw[match.end() : end].strip()
        if segment:
            sections[int(match.group(1))] = segment
    missing = [str(number) for number in range(1, total_beats + 1) if number not in sections]
    if missing:
        return "", f"压缩结果缺少第 {'、'.join(missing)} 个节拍（不能靠删节拍压缩）"
    for number in sections:
        if number > total_beats:
            return "", f"压缩结果出现多余的【节拍 {number}】（本章共 {total_beats} 个节拍）"
    return "\n\n".join(sections[number] for number in sorted(sections)), None

def _beat_key_tokens(summary: str) -> list[str]:
    """把节拍摘要拆成用于覆盖判定的关键词（中文按 2 字滑窗，英文/数字整段）。

    参数:
        summary: 节拍摘要文本。

    返回:
        去重后的关键词列表；摘要为空时返回空列表。
    """
    chunks = re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9]{2,}", summary or "")
    tokens: list[str] = []
    for chunk in chunks:
        if len(chunk) <= 3:
            tokens.append(chunk)
        else:
            tokens.extend(chunk[index : index + 2] for index in range(len(chunk) - 1))
    return list(dict.fromkeys(tokens))

def _uncovered_beats(content: str, beats: list[dict]) -> list[tuple[int, dict]]:
    """找出正文里尚未覆盖的节拍要点。

    判定口径：节拍摘要关键词在正文里的命中率低于 `SUPPLEMENT_COVERAGE_RATIO`
    即视为没写到。这是为了决定「补写哪几个要点」，不是字数判断。

    参数:
        content: 现有正文。
        beats: 本章节拍列表。

    返回:
        `[(节拍序号从 1 开始, 节拍 dict)]`，按原顺序；全部覆盖时为空列表。
    """
    text = content or ""
    rows: list[tuple[int, dict]] = []
    for index, beat in enumerate(beats or [], start=1):
        tokens = _beat_key_tokens(str(beat.get("summary") or ""))
        if not tokens:
            continue
        hit = sum(1 for token in tokens if token in text)
        if hit / len(tokens) < SUPPLEMENT_COVERAGE_RATIO:
            rows.append((index, beat))
    return rows

def _split_supplement_sections(text: str, expected: list[int]) -> tuple[str, str | None]:
    """按「【补写节拍 N】」切分补写结果，校验要求的要点全部存在。

    参数:
        text: 补写模型原始输出。
        expected: 本次要求补写的节拍序号列表；为空时不做分段校验。

    返回:
        `(body, error)`：body 为去掉标记后的新增正文；error 为中文原因（成功为 None）。
    """
    raw = (text or "").strip()
    if not expected:
        return raw, None
    matches = list(re.finditer(r"(?m)^\s*【\s*补写节拍\s*(\d+)\s*】\s*", raw))
    if not matches:
        return "", "补写结果缺少「【补写节拍 N】」分段标记"
    sections: dict[int, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(raw)
        segment = raw[match.end() : end].strip()
        if segment:
            sections[int(match.group(1))] = segment
    missing = [str(number) for number in expected if number not in sections]
    if missing:
        return "", f"补写结果缺少第 {'、'.join(missing)} 个节拍要点"
    extra = sorted(number for number in sections if number not in expected)
    if extra:
        return "", f"补写结果出现未要求的【补写节拍 {'、'.join(str(number) for number in extra)}】"
    return "\n\n".join(sections[number] for number in sorted(sections)), None

def _append_segment(text: str, segment: str, max_overlap: int = 200) -> str:
    """把新增段落追加到正文末尾，并裁掉与结尾重复的重叠文本。

    参数:
        text: 已生成正文。
        segment: 本轮新增段落。
        max_overlap: 最多比较的重叠字数。

    返回:
        拼接后的正文；空段落返回原文，整段重复时不会重复追加。
    """
    base = (text or "").rstrip()
    piece = (segment or "").strip()
    if not piece:
        return text or ""
    if not base:
        return piece
    limit = min(max_overlap, len(base), len(piece))
    overlap = 0
    for size in range(limit, 0, -1):
        if base[-size:] == piece[:size]:
            overlap = size
            break
    if overlap:
        piece = piece[overlap:].lstrip()
    if not piece:
        return base
    return f"{base}\n\n{piece}"

def _continue_instruction(segment_words: int) -> str:
    """构造正文续写轮的用户短指令（供 `continuation.py` 使用）。

    参数:
        segment_words: 本段目标字数。

    返回:
        一句话续写指令。
    """
    return f"继续写下一段，紧接上文，不要重复已写内容，保持人物人设与文风。本段目标约 {segment_words} 字，只输出新增正文。"

def _group_words(group: list[dict], total_beats: int, chapter_words: int) -> int:
    """估算一组节拍的参考字数。

    参数:
        group: 本轮要写的节拍列表。
        total_beats: 本章节拍总数。
        chapter_words: 全章参考字数。

    返回:
        参考字数：优先用节拍目标字数之和，缺失时按节拍占比分摊全章字数。
    """
    total = 0
    for beat in group:
        try:
            total += int(beat.get("target_words") or 0)
        except (TypeError, ValueError):
            continue
    if total > 0:
        return total
    if total_beats <= 0:
        return max(1, chapter_words)
    return max(100, round(chapter_words * len(group) / total_beats))

def _beats_round_instruction(
    groups: list[list[dict]],
    index: int,
    total_beats: int,
    chapter_words: int,
    name_by_id: dict | None = None,
    written_chars: int = 0,
) -> str:
    """构造第 index 轮（0 起）的续写指令。

    参数:
        groups: 按轮切分好的节拍分组。
        index: 当前轮下标（0 起；首轮不走本函数）。
        total_beats: 本章节拍总数。
        chapter_words: 全章参考字数。
        name_by_id: 角色 ID → 姓名；用于写明本轮每个节拍的出场角色。
        written_chars: 已写正文字数；写进指令形成「已写 A，剩余 B」的进度口径。

    返回:
        一句话续写指令，写明本轮节拍范围、出场角色、篇幅区间与进度。
    """
    start = sum(len(group) for group in groups[:index])
    group = groups[index]
    end = start + len(group)
    names = name_by_id or {}

    def beat_line(offset: int, beat: dict) -> str:
        """渲染单条节拍：摘要 + 出场角色姓名（不输出内部 ID）。"""
        present = [
            names.get(str(cid)) or "已删除角色"
            for cid in (beat.get("character_ids") or [])
            if str(cid).strip()
        ]
        return f"第 {start + offset + 1} 个：{beat.get('summary') or ''}（出场：{'、'.join(present) if present else '未指定'}）"

    beat_lines = "；".join(beat_line(offset, beat) for offset, beat in enumerate(group))
    next_note = f"不要写第 {end + 1} 个及之后的节拍。" if end < total_beats else "本章节拍到此结束。"
    plan = length_plan(chapter_words)
    remaining = max(0, int(chapter_words or 0) - int(written_chars or 0))
    return (
        f"继续写第 {start + 1}–{end} 个节拍（全章共 {total_beats} 个），紧接上文，不要重复已写内容。"
        f"本轮节拍：{beat_lines}。只让这些出场角色行动，未出场的角色不得突然出现；"
        f"写完停在第 {end} 个节拍边界，{next_note}"
        f"{length_instruction(_group_words(group, total_beats, chapter_words))}"
        f"本章目标 {plan['low']}–{plan['high']} 字；当前已写 {int(written_chars or 0)} 字，剩余约 {remaining} 字；"
        "接近目标且写到句末即可收尾，不要为凑字数续写。只输出新增正文。"
    )

def _word_deviation(actual: int, target: int) -> dict:
    """计算实际字数与目标字数的偏差，按超纲阈值给出中文说明与可选动作。

    口径：生成过程不限制长度，写完后按目标字数判偏差；
    - < 0.7 倍：明显不足，提供「补写」动作（按缺口与未覆盖要点续写并合并）；
    - 0.7–1.5 倍：只在结果里标实际 / 目标字数；
    - > 1.5 倍且 < 2 倍：提示超纲，可自行精简，不提供动作；
    - ≥ 2 倍：视为明显超纲，返回「按目标字数压缩」「重写」两个动作（需用户点击才执行）。

    参数:
        actual: 实际字数。
        target: 目标字数（节拍目标之和）。

    返回:
        {"target", "actual", "ratio", "level", "notice_ratio", "action_ratio",
         "under_ratio", "message", "actions"}；目标未知时 ratio 为 None、level 为 none。
    """
    actions = [dict(item) for item in OVERSHOOT_ACTIONS]
    base = {
        "target": max(0, int(target or 0)),
        "actual": max(0, int(actual or 0)),
        "level": "none",
        "notice_ratio": OVERSHOOT_NOTICE_RATIO,
        "action_ratio": OVERSHOOT_ACTION_RATIO,
        "under_ratio": UNDERFILL_ACTION_RATIO,
        "actions": [],
    }
    if target <= 0:
        return {**base, "ratio": None, "message": ""}
    ratio_value = actual / target
    ratio = round(ratio_value, 2)
    if ratio_value < UNDERFILL_ACTION_RATIO:
        message = (
            f"实际 {actual} 字，目标 {target} 字（{ratio} 倍）：明显不足。"
            f"当前阈值低于 {UNDERFILL_ACTION_RATIO} 倍可补写；补写会按缺口字数与尚未覆盖的情节要点续写并合并，"
            "需要你点击后才执行"
        )
        base["level"] = "action"
        base["actions"] = [dict(item) for item in UNDERFILL_ACTIONS]
    elif ratio_value < 0.8:
        message = f"实际 {actual} 字，比目标 {target} 字少 {int(round((1 - ratio_value) * 100))}%；节拍已写完，可按需续写补足"
    elif ratio_value >= OVERSHOOT_ACTION_RATIO:
        message = (
            f"实际 {actual} 字，目标 {target} 字（{ratio} 倍）：明显超纲。"
            f"当前阈值 {OVERSHOOT_NOTICE_RATIO} 倍仅提示、{OVERSHOOT_ACTION_RATIO} 倍及以上可压缩或重写；"
            "两个动作都需要你点击后才执行"
        )
        base["level"] = "action"
        base["actions"] = actions
    elif ratio_value > OVERSHOOT_NOTICE_RATIO:
        message = (
            f"实际 {actual} 字，目标 {target} 字（{ratio} 倍）：已超纲，可考虑精简；"
            f"达到 {OVERSHOOT_ACTION_RATIO} 倍时可选择压缩或重写"
        )
        base["level"] = "notice"
    else:
        message = f"实际 {actual} 字，目标 {target} 字（{ratio} 倍）：在 {OVERSHOOT_NOTICE_RATIO} 倍以内"
    return {**base, "ratio": ratio, "message": message}
