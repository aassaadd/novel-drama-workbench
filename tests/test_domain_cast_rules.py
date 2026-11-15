"""阶段 3 角色规则的并跑比对：命名与合并的领域函数与旧实现逐条一致。

比对基准是改造前的旧函数；其中「命名/别名」三条在改造后已删除生产实现，
因此这里保留改造前的**冻结副本**（`_legacy_*`）作为比对基准，避免基准随实现一起漂移。
合并的四个纯变换仍可在 `character_merge` 上取到（已指向 domain），
其真实的「旧 vs 新」比对结果记录在阶段 3 修改记录里（改造前运行：8 项全过）。

    NDW_GOLDEN_PROJECTS=/Volumes/other/ndw_runs/tt1 \\
        .venv/bin/python -m pytest -q tests/test_domain_cast_rules.py
"""

from __future__ import annotations

import copy
import json
import os
import re
import unicodedata
from pathlib import Path

import pytest

from novel_drama import character_merge as legacy_merge
from novel_drama.domain.cast.rules import merge as domain_merge
from novel_drama.domain.cast.rules.naming import (
    add_aliases,
    alias_key,
    is_descriptor,
    merge_aliases,
)
from novel_drama.domain.shared.text import normalize_name
from novel_drama.writing import cast_sync as legacy_cast

#: 改造前的合并侧归一化（冻结副本，不做 NFKC）
_LEGACY_SPACE_RE = re.compile(r"\s+")


def _legacy_cast_norm(name: str) -> str:
    """冻结副本：`cast_sync._norm` 改造前的实现（NFKC → 去空白 → casefold）。"""
    text = unicodedata.normalize("NFKC", str(name or ""))
    return _LEGACY_SPACE_RE.sub("", text).casefold()


def _legacy_merge_norm(name: str) -> str:
    """冻结副本：`character_merge._norm` 改造前的实现。"""
    return _LEGACY_SPACE_RE.sub("", str(name or "")).casefold()


def _legacy_merge_aliases(target: dict, source: dict) -> None:
    """冻结副本：`character_merge._merge_aliases` 改造前的实现。"""
    merged = list(target.get("aliases") or [])
    known = {_legacy_merge_norm(item) for item in [target.get("name"), *merged] if _legacy_merge_norm(item)}
    for alias in [source.get("name"), *(source.get("aliases") or [])]:
        norm = _legacy_merge_norm(alias)
        if norm and norm not in known:
            merged.append(alias)
            known.add(norm)
    if merged:
        target["aliases"] = merged


def _legacy_add_alias(character: dict, aliases: list[str]) -> None:
    """冻结副本：`cast_sync._add_alias` 改造前的实现。"""
    merged = list(character.get("aliases") or [])
    known = {normalize_name(item) for item in [character.get("name"), *merged] if normalize_name(item)}
    for alias in aliases:
        norm = normalize_name(alias)
        if norm and norm not in known:
            merged.append(alias)
            known.add(norm)
    if merged:
        character["aliases"] = merged

PROJECTS = [Path(item) for item in os.environ.get("NDW_GOLDEN_PROJECTS", "").split(":") if item]


def _tables() -> list[dict]:
    """读取真实工程的角色表（未设置环境变量时返回空列表）。"""
    rows: list[dict] = []
    for root in PROJECTS:
        path = root / "characters.json"
        if not path.exists():
            continue
        try:
            rows.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return rows


def _chapters() -> list[dict]:
    """读取真实工程的章节。"""
    rows: list[dict] = []
    for root in PROJECTS:
        directory = root / "chapters"
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.json")):
            try:
                rows.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
    return rows


def _bibles() -> list[dict]:
    """读取真实工程的设定库（bible.json）。"""
    rows: list[dict] = []
    for root in PROJECTS:
        path = root / "settings" / "bible.json"
        if not path.exists():
            continue
        try:
            rows.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return rows


TABLES = _tables()
CHAPTERS = _chapters()
BIBLES = _bibles()

#: 命名样本：真实名字之外补全角/空白/大小写/代称边界
_NAME_SAMPLES: tuple[str, ...] = (
    "",
    "   ",
    "顾三",
    "顾 三",
    "Ｌｕｃｙ",
    "Lucy",
    "lucy",
    "他们",
    "她",
    "女人",
    "一个男人",
    "陌生人",
    "旁白",
)


def test_name_normalization_matches_legacy():
    """归一化：领域函数与 `cast_sync._norm` / `character_merge._norm` 逐条一致。"""
    samples = list(_NAME_SAMPLES)
    for table in TABLES:
        for item in table.get("characters") or []:
            samples.append(str(item.get("name") or ""))
            samples.extend(str(alias) for alias in item.get("aliases") or [])
    for name in samples:
        assert normalize_name(name) == _legacy_cast_norm(name), name
        # 合并侧历史口径不做 NFKC，单独比对
        assert alias_key(name) == _legacy_merge_norm(name), name


def test_descriptor_matches_legacy():
    """叙述代称：领域函数与旧实现一致（含真实工程角色名）。"""
    samples = list(_NAME_SAMPLES)
    for table in TABLES:
        for item in table.get("characters") or []:
            samples.append(str(item.get("name") or ""))
    for name in samples:
        assert is_descriptor(name) == legacy_cast.is_descriptor(name), name


def test_add_aliases_matches_legacy():
    """写作侧补别名：结果与 `cast_sync._add_alias` 完全一致。"""
    cases = [
        ({"name": "顾三", "aliases": ["三哥"]}, ["顾三", "三哥", "阿三", "阿三"]),
        ({"name": "", "aliases": []}, [" Lucy", "lucy", "LUcy"]),
        ({"name": "沈砚", "aliases": []}, []),
    ]
    for character, aliases in cases:
        old = copy.deepcopy(character)
        new = copy.deepcopy(character)
        _legacy_add_alias(old, aliases)
        add_aliases(new, aliases)
        assert old == new, (character, aliases)


def test_merge_aliases_matches_legacy():
    """合并侧并入姓名与别名：结果与 `_merge_aliases` 完全一致。"""
    cases = [
        ({"name": "沈砚", "aliases": ["砚哥"]}, {"name": "顾三", "aliases": ["三哥", "砚哥"]}),
        ({"name": "Lucy", "aliases": []}, {"name": "Ｌｕｃｙ", "aliases": []}),
    ]
    for target, source in cases:
        old = copy.deepcopy(target)
        new = copy.deepcopy(target)
        _legacy_merge_aliases(old, source)
        merge_aliases(new, source)
        assert old == new, (target, source)


def test_merge_fields_matches_legacy_on_real_characters():
    """字段并入：真实角色两两组合，结果与 `_merge_fields` 一致。"""
    if not TABLES:
        pytest.skip("未设置 NDW_GOLDEN_PROJECTS，跳过真实角色字段并跑")
    for table in TABLES:
        items = [item for item in table.get("characters") or [] if item.get("id")]
        for source in items:
            for target in items:
                if source is target:
                    continue
                old = copy.deepcopy(target)
                new = copy.deepcopy(target)
                legacy_merge._merge_fields(old, source)
                domain_merge.merge_fields(new, source)
                assert old == new, (source.get("id"), target.get("id"))


def test_merge_relation_refs_matches_legacy():
    """关系改指：在真实角色表上给每个角色铺关系后，结果与旧实现一致。"""
    if not TABLES:
        pytest.skip("未设置 NDW_GOLDEN_PROJECTS，跳过真实关系并跑")
    for table in TABLES:
        items = [item for item in table.get("characters") or [] if item.get("id")]
        if len(items) < 2:
            continue
        source_id, target_id = items[0]["id"], items[1]["id"]
        seeded = copy.deepcopy(table)
        for item in seeded.get("characters") or []:
            item["state"] = {
                "relationship_notes": {source_id: "旧识", item.get("id"): "自指备注"},
                "relations": [
                    {"target_id": source_id, "label": "旧识"},
                    {"target_id": item.get("id"), "label": "自指"},
                    {"target_id": target_id, "label": "重复"},
                    {"target_id": source_id, "label": "重复"},
                ],
            }
        old = copy.deepcopy(seeded)
        new = copy.deepcopy(seeded)
        legacy_merge._merge_relation_refs(old, source_id, target_id)
        domain_merge.merge_relation_refs(new, source_id, target_id)
        assert old == new, (source_id, target_id)


def test_merge_outline_matches_legacy():
    """大纲迁移：真实设定库上逐对角色比对（无设定库时跳过）。"""
    if not BIBLES:
        pytest.skip("真实工程没有 bible.json，跳过大纲并跑")
    for bible in BIBLES:
        ids = [
            str(cid)
            for act in (bible.get("outline") or {}).get("acts") or []
            for chapter in act.get("chapters") or []
            for beat in chapter.get("beats") or []
            for cid in beat.get("character_ids") or []
        ]
        for source_id in dict.fromkeys(ids):
            for target_id in list(dict.fromkeys(ids))[:3]:
                if source_id == target_id:
                    continue
                old = copy.deepcopy(bible)
                new = copy.deepcopy(bible)
                legacy_merge._merge_outline(old, source_id, target_id)
                domain_merge.merge_outline(new, source_id, target_id)
                assert old == new, (source_id, target_id)


def test_merge_chapter_file_matches_legacy():
    """章节引用迁移：真实章节里把源角色塞进每类引用后，结果与旧实现一致。"""
    if not CHAPTERS:
        pytest.skip("未设置 NDW_GOLDEN_PROJECTS，跳过真实章节并跑")
    for chapter in CHAPTERS:
        ids = [
            item.get("id")
            for scene in chapter.get("scenes") or []
            for item in [{"id": (scene.get("character_ids") or [None])[0]}]
            if item.get("id")
        ]
        ids += [line.get("speaker_id") for scene in chapter.get("scenes") or [] for line in scene.get("lines") or [] if line.get("speaker_id")]
        ids = list(dict.fromkeys(ids))
        if len(ids) < 2:
            continue
        source_id, target_id = ids[0], ids[1]
        old = copy.deepcopy(chapter)
        new = copy.deepcopy(chapter)
        old_changed = legacy_merge._merge_chapter_file(old, source_id, target_id)
        new_changed = domain_merge.merge_chapter_file(new, source_id, target_id)
        assert old_changed == new_changed
        assert old == new, chapter.get("id")
