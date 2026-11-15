"""阶段 3 章节规则的并跑比对：领域函数与旧实现表达式逐条一致。

验收口径（阶段 3 用户要求）：
- 字数、版本号、存档判定是用户可见结果（版本列表、字数、回滚），不能只看单测通过；
- 因此这里保留「旧实现的原始表达式」作为比对基准，与 `domain.story.rules.chapter_rules`
  在同一批真实工程数据与边界样本上逐条比对。

真实工程用环境变量开启（写操作只发生在临时副本，本文件只读）：

    NDW_GOLDEN_PROJECTS=/Volumes/other/ndw_runs/tt1 \\
        .venv/bin/python -m pytest -q tests/test_domain_chapter_rules.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from novel_drama.domain.shared.text import count_words
from novel_drama.domain.story.rules.chapter_rules import (
    bump_revision,
    next_revision,
    should_archive_version,
    version_id_of,
)

PROJECTS = [Path(item) for item in os.environ.get("NDW_GOLDEN_PROJECTS", "").split(":") if item]

#: 边界样本：None / 空串 / 纯空白 / 无正文键
_TEXT_SAMPLES: tuple[str | None, ...] = (
    None,
    "",
    "   \n\t ",
    "第一段。\n\n第二段。",
    "全角　空格　测试",
    "abc 123\n456",
)


def _legacy_count(text: str | None) -> int:
    """旧实现的字数表达式（`sum(not isspace)`）。"""
    return sum(1 for char in (text or "") if not char.isspace())


def _legacy_join_count(text: str | None) -> int:
    """另一路旧实现（`len("".join(text.split()))`），用于证明两套旧写法同值。"""
    return len("".join((text or "").split()))


def _legacy_should_archive(current: dict, incoming: dict) -> bool:
    """旧实现的存档判定表达式（`ProjectStore.save_chapter` 原文）。"""
    return bool(current and str(current.get("content") or "").strip() and current.get("content") != incoming.get("content"))


def _legacy_next_revision(chapter: dict) -> int:
    """旧实现的版本号自增表达式。"""
    return int(chapter.get("revision") or 1) + 1


def _chapter_payloads() -> list[dict]:
    """读取真实工程的章节（未设置环境变量时返回空列表）。"""
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


PAYLOADS = _chapter_payloads()


def test_word_count_matches_both_legacy_formulas():
    """字数：领域函数与两套旧写法在边界样本上逐条一致。"""
    for text in _TEXT_SAMPLES:
        assert count_words(text) == _legacy_count(text) == _legacy_join_count(text), repr(text)


def test_word_count_matches_real_projects():
    """字数：真实工程每一章都与旧写法一致（数据可能为空，按实际比对）。"""
    if not PAYLOADS:
        pytest.skip("未设置 NDW_GOLDEN_PROJECTS，跳过真实工程字数比对")
    for chapter in PAYLOADS:
        content = chapter.get("content")
        assert count_words(content) == _legacy_count(content), chapter.get("id")
        assert count_words(content) == _legacy_join_count(content), chapter.get("id")


def test_archive_decision_matches_legacy_on_matrix():
    """存档判定：新建、空旧正文、正文未变、正文已变四种情形与旧表达式一致。"""
    cases = [
        ({}, {"content": "新正文"}),
        ({"content": ""}, {"content": "新正文"}),
        ({"content": "   "}, {"content": "新正文"}),
        ({"content": "旧正文", "revision": 3}, {"content": "旧正文", "revision": 4, "title": "改了标题"}),
        ({"content": "旧正文", "revision": 3}, {"content": "新正文", "revision": 4}),
        ({"content": "旧正文", "revision": 3}, {"revision": 3}),
    ]
    for current, incoming in cases:
        assert should_archive_version(current, incoming) == _legacy_should_archive(current, incoming), (current, incoming)


def test_archive_decision_matches_legacy_on_real_projects():
    """存档判定：真实章节「自己 vs 自己」「自己 vs 改一字」与旧表达式一致。"""
    if not PAYLOADS:
        pytest.skip("未设置 NDW_GOLDEN_PROJECTS，跳过真实工程存档比对")
    for chapter in PAYLOADS:
        same = dict(chapter)
        edited = dict(chapter, content=(chapter.get("content") or "") + "补一句。")
        for incoming in (same, edited, {"content": ""}):
            assert should_archive_version(chapter, incoming) == _legacy_should_archive(chapter, incoming), chapter.get("id")


def test_revision_rules_match_legacy():
    """版本号：缺失/0/正常值三种输入与旧表达式一致，bump 就地写回。"""
    for value in (None, 0, 1, 7):
        chapter = {} if value is None else {"revision": value}
        assert next_revision(chapter) == _legacy_next_revision(chapter)
        expected = _legacy_next_revision(chapter)
        assert bump_revision(chapter) == expected
        assert chapter["revision"] == expected


def test_version_id_of_matches_archive_filename():
    """存档文件名：与旧实现 `str(int(revision or 1))` 一致。"""
    for value in (None, 0, 1, 12):
        snapshot = {} if value is None else {"revision": value}
        assert version_id_of(snapshot) == str(int(snapshot.get("revision") or 1))
