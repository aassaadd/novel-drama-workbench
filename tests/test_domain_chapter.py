"""章节实体（Chapter）的往返与规则测试。

黄金样本说明：
- `chapter_draft_real.json`、`chapter_written_real.json` 直接取自真实工程
  `/Volumes/other/ndw_runs/tt1/chapters/`，字段与真实落盘完全一致；
- `chapter_scenes_real.json` 取自真实工程中带 6 个 v1 场景的章节，仅截短正文与台词条数；
- 另外用合成样本覆盖段落块、导入快照、续写会话等真实工程暂未出现的字段。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from novel_drama.adapters.persistence.schema_validator import validate
from novel_drama.domain.shared.errors import InvariantViolation
from novel_drama.domain.shared.text import count_words
from novel_drama.domain.story.entities.chapter import Chapter

FIXTURES = Path(__file__).parent / "fixtures" / "domain"
REAL_SAMPLES = ["chapter_draft_real.json", "chapter_written_real.json", "chapter_scenes_real.json"]


def _load(name: str) -> dict:
    """读取样本 JSON。"""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _minimal(**overrides) -> dict:
    """构造一份满足必填项的最小章节字典。"""
    data = {
        "schema_version": 1,
        "id": "chp_01M7C19017ABCDEFGHJKMNPQRS",
        "work_id": "wrk_01M7C19017ABCDEFGHJKMNPQRS",
        "index": 1,
        "title": "第一章",
        "status": "draft",
        "revision": 1,
        "created_at": "2026-11-14T03:23:04Z",
        "updated_at": "2026-11-14T03:23:04Z",
    }
    data.update(overrides)
    return data


@pytest.mark.parametrize("name", REAL_SAMPLES)
def test_roundtrip_is_idempotent(name: str):
    """真实样本：实体 → 字典 → 实体 → 字典，两次结果完全一致，且通过 schema 校验。"""
    raw = _load(name)
    once = Chapter.from_dict(raw).to_dict()
    twice = Chapter.from_dict(once).to_dict()
    assert once == twice
    validate(once, "chapter.schema.json")


@pytest.mark.parametrize("name", REAL_SAMPLES)
def test_roundtrip_preserves_every_source_key(name: str):
    """真实样本：回写结果与源文件逐键一致（字数按正文重算，单独断言）。

    保持一致的含义：不新增 `source: null`、`blocks: []` 这类源文件里没有的键，
    否则「读一次再存」就会在用户工程里产生无意义 diff。
    """
    raw = _load(name)
    out = Chapter.from_dict(raw).to_dict()
    assert out["word_count"] == count_words(raw.get("content"))
    assert out == raw


def test_word_count_is_derived_not_trusted():
    """落盘字数与正文不一致时，以正文为准（避免脏字数进入上下文预算）。"""
    raw = _minimal(content="一二三 四五六", word_count=999)
    chapter = Chapter.from_dict(raw)
    assert chapter.word_count == 6
    assert chapter.to_dict()["word_count"] == 6


def test_legacy_empty_content_preserved_and_rewrite_normalises():
    """历史空串 `""` 原样保留；只有走 `rewrite_content` 的新写入才归一成 None。"""
    chapter = Chapter.from_dict(_minimal(content=""))
    assert chapter.content == ""
    assert chapter.to_dict()["content"] == ""

    chapter.rewrite_content("   \n  ")
    assert chapter.content is None
    assert chapter.to_dict()["content"] is None


def test_blocks_and_source_and_continuations_roundtrip():
    """真实工程暂未出现的字段（段落块、导入快照、续写会话）必须无损往返。"""
    raw = _minimal(
        content="第一段正文。\n\n第二段正文。",
        blocks=[
            {
                "id": "blk_0123456789abcdef",
                "index": 0,
                "kind": "paragraph",
                "char_start": 0,
                "char_end": 6,
                "text_hash": "sha256:" + "a" * 64,
            }
        ],
        source={
            "snapshot_id": "snp_01M7C19017ABCDEFGHJKMNPQRS",
            "file": ".ndw/imports/chp_01M7C19017ABCDEFGHJKMNPQRS.txt",
            "sha256": "sha256:" + "b" * 64,
            "chars": 12,
            "imported_at": "2026-11-14T03:23:04Z",
            "source_path": "/Users/someone/novel.txt",
            "tool": "novel import",
        },
        continuations=[
            {
                "id": "cnt_01M7C19017ABCDEFGHJKMNPQRS",
                "mode": "continue",
                "context": {"include_bible": True},
                "status": "done",
                "created_at": "2026-11-14T03:23:04Z",
            }
        ],
        scenes=[
            {
                "id": "scn_0123456789abcdef",
                "chapter_id": "chp_01M7C19017ABCDEFGHJKMNPQRS",
                "index": 1,
                "status": "draft",
                "revision": 1,
            }
        ],
    )
    out = Chapter.from_dict(raw).to_dict()
    assert out["blocks"] == raw["blocks"]
    assert out["source"] == raw["source"]
    assert out["continuations"] == raw["continuations"]
    assert out["scenes"] == raw["scenes"]
    validate(out, "chapter.schema.json")


def test_voice_override_requires_reason():
    """章节级音色覆盖必须写原因。"""
    raw = _minimal(voice_overrides={"chr_01M7C19017ABCDEFGHJKMNPQRS": {"voice_id": "voc_01M7C19017ABCDEFGH"}})
    with pytest.raises(InvariantViolation) as caught:
        Chapter.from_dict(raw)
    assert "reason" in str(caught.value)


def test_legacy_enum_value_is_preserved_but_strict_mode_rejects():
    """历史非法状态：默认原样保留（不静默改数据），严格模式直接拒绝。"""
    raw = _minimal(status="in_progress")
    assert Chapter.from_dict(raw).status == "in_progress"
    with pytest.raises(InvariantViolation):
        Chapter.from_dict(raw, strict=True)


def test_rewrite_content_bumps_revision_and_clears_blocks():
    """改写正文：版本自增、字数重算、旧块清空、下游状态作废。"""
    raw = _load("chapter_written_real.json")
    raw["status"] = "scripted"
    raw["script_path"] = "scripts/chp_x.json"
    chapter = Chapter.from_dict(raw)
    revision = chapter.revision
    chapter.rewrite_content("新的正文：只有一句。")
    assert chapter.revision == revision + 1
    assert chapter.word_count == count_words("新的正文：只有一句。")
    assert chapter.blocks == []
    assert chapter.status == "stale"


def test_required_fields_are_enforced():
    """必填项缺失必须抛领域异常，而不是等落盘时才由 schema 报错。"""
    with pytest.raises(InvariantViolation):
        Chapter.from_dict(_minimal(title=""))
    with pytest.raises(InvariantViolation):
        Chapter.from_dict(_minimal(index=0))
    with pytest.raises(InvariantViolation):
        Chapter.from_dict(_minimal(id="bad_id"))
    with pytest.raises(InvariantViolation):
        Chapter.from_dict(_minimal(revision=0))
