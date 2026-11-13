"""说话人名字归一化与匹配：模型标注常带 `*`、项目符号、引号、括号与「说/道」后缀。

标注与角色表匹配前先归一化，尽量映射到正确角色，避免因符号差异落到 `chr_unknown`。
"""

from __future__ import annotations

import re

#: 开头项目符号 / 星号 / 箭头
_PREFIX = re.compile(r"^[\s\-\*﹡•·◆◇■□●○>~～]+")

#: 成对的括号及其内容（模型常写「沈砚（主角）」「沈砚【男主】」）
_BRACKETS = re.compile(r"[（(【\[《<][^）)】\]》>]*[）)】\]》>]")

#: 需要从首尾剥掉的引号与冒号
_TRIM = " \t\"'“”‘’「」『』`：:，,。.、"

#: 常见说话后缀（长的放前面，避免「说道」只剥掉「道」）
_SUFFIXES = (
    "低声说道",
    "轻声说道",
    "冷笑道",
    "沉声道",
    "说道",
    "问道",
    "答道",
    "喊道",
    "叫道",
    "低声说",
    "轻声说",
    "笑道",
    "说",
    "道",
    "问",
    "答",
    "喊",
    "叫",
)


def normalize_speaker_name(name: str) -> str:
    """把标注里的说话人写法归一成可匹配的名字。

    处理：去项目符号与 `*`、去括号及其内容、去引号、去全部空白、
    去「说/道/问/答」这类后缀。

    参数:
        name: 原始说话人文本。

    返回:
        归一化后的名字；空输入返回空串。
    """
    value = str(name or "").strip()
    value = _PREFIX.sub("", value)
    value = _BRACKETS.sub("", value)
    value = value.replace("*", "").replace("﹡", "")
    value = re.sub(r"\s+", "", value)
    value = value.strip(_TRIM)
    for suffix in _SUFFIXES:
        if value.endswith(suffix) and len(value) > len(suffix):
            value = value[: -len(suffix)].strip(_TRIM)
            break
    return value


def match_speaker(name: str, characters: list[dict]) -> tuple[str, float] | None:
    """把说话人写法匹配到角色表里的非旁白角色。

    先精确匹配姓名/别名；再允许长度 ≥2 的前缀互含（覆盖「简称」）。
    旁白不在本函数处理，由调用方判断。

    参数:
        name: 原始说话人文本。
        characters: 角色表。

    返回:
        (角色 ID, 置信度)；未命中返回 None。
    """
    key = normalize_speaker_name(name)
    if not key:
        return None
    candidates: list[tuple[str, str]] = []
    for character in characters:
        if character.get("role") == "narrator":
            continue
        for raw in [character.get("name") or ""] + list(character.get("aliases") or []):
            normalized = normalize_speaker_name(raw)
            if normalized:
                candidates.append((normalized, character["id"]))
    for normalized, character_id in candidates:
        if normalized == key:
            return character_id, 1.0
    # 简称：至少两个字才做前缀互含，避免单字误命中
    if len(key) >= 2:
        for normalized, character_id in candidates:
            if len(normalized) >= 2 and (normalized.startswith(key) or key.startswith(normalized)):
                return character_id, 0.8
    return None
