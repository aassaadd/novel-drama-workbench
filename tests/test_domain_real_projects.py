"""真实工程回归：把工程目录里的章节与角色表读成实体再写回，要求与磁盘逐字段一致。

默认跳过；用环境变量指定工程根目录（多个用 `:` 分隔）：

    NDW_GOLDEN_PROJECTS=/path/to/proj1:/path/to/proj2 \
        .venv/bin/python -m pytest -q tests/test_domain_real_projects.py

这条用例是阶段 1 的验收依据：实体不接调用方，但必须能无损承载真实数据。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from novel_drama.domain.cast.entities.cast import Cast
from novel_drama.domain.shared.text import count_words
from novel_drama.domain.story.entities.chapter import Chapter

PROJECTS = [Path(item) for item in os.environ.get("NDW_GOLDEN_PROJECTS", "").split(":") if item]

pytestmark = pytest.mark.skipif(not PROJECTS, reason="未设置 NDW_GOLDEN_PROJECTS，跳过真实工程回归")


def _diff(source: dict, result: dict) -> dict[str, tuple]:
    """列出两个字典的值差异，键为字段名。"""
    return {
        key: (source.get(key), result.get(key))
        for key in set(source) | set(result)
        if source.get(key) != result.get(key)
    }


@pytest.mark.parametrize("root", PROJECTS, ids=[path.name for path in PROJECTS])
def test_project_chapters_roundtrip(root: Path):
    """工程内每一章：实体往返后与磁盘文件逐字段一致（字数按正文重算）。"""
    files = sorted((root / "chapters").glob("*.json"))
    if not files:
        pytest.skip(f"{root} 没有章节文件")
    problems = []
    for path in files:
        raw = json.loads(path.read_text(encoding="utf-8"))
        out = Chapter.from_dict(raw).to_dict()
        diff = _diff(raw, out)
        if "word_count" in diff:
            # 落盘字数与正文不一致属于数据漂移，单独记录；实体侧以正文为准
            diff.pop("word_count")
            if raw.get("word_count") != count_words(raw.get("content")):
                problems.append(f"{path.name}: 落盘字数 {raw.get('word_count')} ≠ 正文实算 {count_words(raw.get('content'))}")
        if diff:
            problems.append(f"{path.name}: {diff}")
    assert problems == [], "\n".join(problems)


@pytest.mark.parametrize("root", PROJECTS, ids=[path.name for path in PROJECTS])
def test_project_characters_roundtrip(root: Path):
    """工程角色表：往返后与磁盘逐字段一致（历史非法枚举值原样保留）。"""
    path = root / "characters.json"
    if not path.exists():
        pytest.skip(f"{root} 没有角色表")
    raw = json.loads(path.read_text(encoding="utf-8"))
    out = Cast.from_dict(raw).to_dict()
    diff = _diff(raw, out)
    assert diff == {}, f"{path}: {diff}"
