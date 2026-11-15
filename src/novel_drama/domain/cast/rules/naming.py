"""角色命名规则：归一化、叙述代称识别与别名合并。

三条规则原先分散在写作侧与合并侧，这里收敛为单点：

1. 归一化：`normalize_name` 与 `writing/cast_sync._norm` 逐字一致（NFKC → 去空白 → casefold）；
2. 叙述代称：「他」「她」「女人」「对方」这类泛称不建卡、不进角色表；
3. 别名合并：姓名与别名按归一化键去重，正式名优先，顺序保持既有插入顺序。

`alias_key` 保留合并路径的历史口径（去空白 + casefold，**不做 NFKC**）：合并判定与写作侧
归一化口径不同是既有行为，这里不改，避免把「全角／半角写法」的别名悄悄合成一条。
"""

from __future__ import annotations

import re

from novel_drama.domain.shared.text import normalize_name

#: 叙述代称：一律不建卡
DESCRIPTOR_WORDS: frozenset[str] = frozenset(
    {
        "他",
        "她",
        "它",
        "他们",
        "她们",
        "它们",
        "我",
        "我们",
        "你",
        "你们",
        "您",
        "大家",
        "众人",
        "有人",
        "某人",
        "对方",
        "旁人",
        "那人",
        "这人",
        "此人",
        "女人",
        "男人",
        "女孩",
        "男孩",
        "女子",
        "男子",
        "妇人",
        "少女",
        "少年",
        "老人",
        "老者",
        "孩子",
        "小孩",
        "陌生人",
        "声音",
    }
)

#: 归一化用的空白
_SPACE_RE = re.compile(r"\s+")

#: 代词兜底（带「们」或单字代词都算代称）
_PRONOUN_RE = re.compile(r"(他们|她们|它们|你|我|他|她|它)")


def alias_key(value: str | None) -> str:
    """合并路径的姓名键：去空白 + casefold，不做 NFKC。

    与 `normalize_name` 的差别仅在全半角统一：合并判定沿用既有口径，
    避免把「全角/半角写法」的别名悄悄合成一条（行为变更需单独确认）。

    参数:
        value: 姓名或别名。

    返回:
        归一化字符串；无效输入返回空串。
    """
    return _SPACE_RE.sub("", str(value or "")).casefold()


def is_descriptor(name: str | None) -> bool:
    """判断是否是叙述代称（代词或「女人」「男人」「对方」这类泛称）。

    参数:
        name: 候选姓名。

    返回:
        True 表示不应建卡。
    """
    key = normalize_name(name)
    if not key:
        return True
    if key in DESCRIPTOR_WORDS:
        return True
    return bool(_PRONOUN_RE.fullmatch(key))


def add_aliases(character: dict, aliases: list[str], key=normalize_name) -> None:
    """把候选别名补进角色卡（跳过与正式名/已有别名重复的项）。

    参数:
        character: 角色 dict（就地修改）。
        aliases: 候选别名。
        key: 去重用的归一化函数，默认写作侧口径。

    返回:
        无。
    """
    merged = list(character.get("aliases") or [])
    known = {key(item) for item in [character.get("name"), *merged] if key(item)}
    for alias in aliases:
        norm = key(alias)
        if norm and norm not in known:
            merged.append(alias)
            known.add(norm)
    if merged:
        character["aliases"] = merged


def merge_aliases(target: dict, source: dict, key=alias_key) -> None:
    """把源角色的姓名与别名并入目标角色。

    参数:
        target: 目标角色（就地修改）。
        source: 源角色。
        key: 去重用的归一化函数，默认合并侧口径（不做 NFKC）。

    返回:
        无。
    """
    merged = list(target.get("aliases") or [])
    known = {key(item) for item in [target.get("name"), *merged] if key(item)}
    for alias in [source.get("name"), *(source.get("aliases") or [])]:
        norm = key(alias)
        if norm and norm not in known:
            merged.append(alias)
            known.add(norm)
    if merged:
        target["aliases"] = merged
