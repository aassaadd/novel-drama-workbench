"""规则优先的切分：章节 → 场景 → 台词行。

设计原则：
- 切分必须可复现、可解释，因此默认走规则；
- 规则无法覆盖的歧义段落（无引号对白、多人抢话）留给写作模型，且只替换该段；
- 未识别说话人一律标记 `chr_unknown`，进入待校对列表，不猜。
"""

from __future__ import annotations

import re

from ..models.entities import NARRATOR_ID, UNKNOWN_SPEAKER_ID

# 显式场景分隔符
_SCENE_SEPARATOR = re.compile(r"^\s*(?:-{3,}|\*{3,}|#{3,}|【[^】]{1,20}】)\s*$")

# 场景转场提示词（出现在段首时倾向切场）
_TRANSITION_WORDS = (
    "第二天",
    "次日",
    "与此同时",
    "半小时后",
    "一小时后",
    "数日后",
    "几天后",
    "当晚",
    "深夜",
    "清晨",
    "傍晚",
    "凌晨",
    "回到",
    "门外",
    "屋内",
    "另一个",
)

# 对话引号（中英文、直角引号）
# 不设长度上限：超长独白同样算对白，块级切分交给 TTS 切分逻辑处理；
# 同时要求必须有闭合引号才匹配，避免未闭合的引号把后半章吞成一句对白
_QUOTE_PATTERN = re.compile(r"“([^”]+)”|「([^」]+)」|\"([^\"]+)\"")

# 句末标点，用于切分过长旁白
_SENTENCE_SPLIT = re.compile(r"(?<=[。！？])")


def split_scenes(content: str, max_chars: int = 1200) -> list[dict]:
    """把章节正文切成场景草案。

    参数:
        content: 章节正文（纯文本/Markdown）。
        max_chars: 单场景软上限，超过则在此处切分。

    返回:
        [{"title": 场景标题, "content": 场景正文}]，至少返回一项（正文为空时返回空列表）。
    """
    if not content or not content.strip():
        return []

    scenes: list[dict] = []
    buffer: list[str] = []
    buffer_chars = 0

    def flush() -> None:
        """把当前缓冲区落成一个场景。"""
        nonlocal buffer, buffer_chars
        text = "\n".join(buffer).strip()
        if text:
            scenes.append({"title": f"场景 {len(scenes) + 1}", "content": text})
        buffer = []
        buffer_chars = 0

    for raw_paragraph in re.split(r"\n\s*\n", content):
        paragraph = raw_paragraph.strip()
        if not paragraph:
            continue
        if _SCENE_SEPARATOR.match(paragraph):
            flush()
            continue
        starts_transition = paragraph.startswith(_TRANSITION_WORDS)
        if buffer and (starts_transition or buffer_chars + len(paragraph) > max_chars):
            flush()
        buffer.append(paragraph)
        buffer_chars += len(paragraph)

    flush()
    return scenes


def _alias_index(characters: list[dict]) -> list[tuple[str, str]]:
    """构建「称呼 → 角色 ID」索引，按名称长度降序以优先匹配长名。

    参数:
        characters: 角色表。

    返回:
        [(称呼, 角色 ID)]，排除旁白。
    """
    pairs: list[tuple[str, str]] = []
    for character in characters:
        if character.get("role") == "narrator" or character.get("id") == NARRATOR_ID:
            continue
        names = [character.get("name") or ""] + list(character.get("aliases") or [])
        for name in names:
            if name:
                pairs.append((name, character["id"]))
    return sorted(pairs, key=lambda item: len(item[0]), reverse=True)


def _infer_speaker(prefix: str, aliases: list[tuple[str, str]]) -> tuple[str, float]:
    """从引号前的叙述文字中反推说话人。

    参数:
        prefix: 引号之前最近的叙述片段。
        aliases: 称呼 → 角色 ID 索引。

    返回:
        (角色 ID, 置信度)；未命中返回 (chr_unknown, 0.0)。
    """
    window = prefix[-30:]
    for name, character_id in aliases:
        if name in window:
            return character_id, 0.75
    return UNKNOWN_SPEAKER_ID, 0.0


def _push_narration(lines: list[dict], text: str) -> None:
    """把一段叙述文本按句末标点切分后追加为旁白行。

    参数:
        lines: 输出列表（原地追加）。
        text: 叙述文本。
    """
    cleaned = text.strip()
    if len(cleaned) < 2:
        return
    chunks = [chunk.strip() for chunk in _SENTENCE_SPLIT.split(cleaned) if chunk.strip()]
    for chunk in chunks or [cleaned]:
        lines.append({"kind": "narration", "text": chunk, "speaker_id": NARRATOR_ID, "confidence": 1.0})


def split_lines(scene_text: str, characters: list[dict]) -> list[dict]:
    """把场景正文切成台词行（对白/旁白/内心独白）。

    参数:
        scene_text: 场景正文。
        characters: 角色表（用于说话人识别）。

    返回:
        [{"kind", "text", "speaker_id", "confidence"}]，保持原文顺序。

    说明:
        引号内为对白；引号外叙述为旁白；引号前出现「心想/暗道」或紧邻「（」时视为内心独白。
        说话人只从角色表匹配，未命中标记 chr_unknown 交由人工校对。
    """
    if not scene_text or not scene_text.strip():
        return []

    aliases = _alias_index(characters)
    lines: list[dict] = []
    cursor = 0

    for match in _QUOTE_PATTERN.finditer(scene_text):
        dialogue = next(group for group in match.groups() if group)
        prefix = scene_text[cursor:match.start()]
        _push_narration(lines, prefix)

        speaker_id, confidence = _infer_speaker(prefix, aliases)
        before = prefix.rstrip()
        is_inner = before.endswith("心想") or before.endswith("暗道") or before.endswith("（") or before.endswith("(")
        if is_inner and speaker_id == UNKNOWN_SPEAKER_ID and characters:
            # 内心独白通常紧贴最近出现的角色，仍需人工确认，置信度调低
            confidence = 0.3
        lines.append(
            {
                "kind": "inner_monologue" if is_inner else "dialogue",
                "text": dialogue.strip(),
                "speaker_id": speaker_id,
                "confidence": confidence,
            }
        )
        cursor = match.end()

    _push_narration(lines, scene_text[cursor:])
    return lines
