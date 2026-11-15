"""领域公共件的单元测试：ID、时间、异常与文本工具。"""

from __future__ import annotations

import pytest

from novel_drama.domain.shared import text as text_tools
from novel_drama.domain.shared.errors import DomainError, InvariantViolation, require
from novel_drama.domain.shared.ids import ID_PREFIXES, has_prefix, new_entity_id, new_id
from novel_drama.domain.shared.timestamps import now_iso, parse_iso


def test_new_id_shape_and_uniqueness():
    """ID 形如 `<前缀>_<26 位 Base32>`，且连续生成不重复。"""
    ids = {new_id("chr") for _ in range(200)}
    assert len(ids) == 200
    sample = next(iter(ids))
    prefix, _, body = sample.partition("_")
    assert prefix == "chr"
    assert len(body) == 26
    assert has_prefix(sample, "chr")
    assert not has_prefix(sample, "chp")


def test_new_entity_id_uses_registry():
    """实体名走登记表取前缀。"""
    assert new_entity_id("chapter").startswith("chp_")
    assert new_entity_id("line").startswith("lin_")
    with pytest.raises(KeyError):
        new_entity_id("not_registered")


def test_id_prefixes_are_unique():
    """前缀不得重复，否则无法从 ID 反推实体类型。"""
    values = list(ID_PREFIXES.values())
    assert len(values) == len(set(values))


def test_new_id_rejects_empty_prefix():
    """空前缀直接拒绝。"""
    with pytest.raises(ValueError):
        new_id("")


def test_now_iso_and_parse():
    """时间戳为秒级 UTC 字符串且可回读。"""
    value = now_iso()
    assert value.endswith("Z")
    parsed = parse_iso(value)
    assert parsed is not None and parsed.tzinfo is not None
    assert parse_iso("") is None
    assert parse_iso("不是时间") is None


def test_normalize_name_matches_existing_behaviour():
    """全半角统一、去空白、英文小写；中文不受影响。"""
    assert text_tools.normalize_name(" 林　晚 ") == "林晚"
    assert text_tools.normalize_name("Ｌｉｎ　Ｗａｎ") == "linwan"
    assert text_tools.normalize_name("Lin Wan") == "linwan"
    assert text_tools.normalize_name(None) == ""


def test_count_words_ignores_whitespace():
    """字数按非空白字符统计。"""
    assert text_tools.count_words("你好 世界\n第二行") == 7
    assert text_tools.count_words(None) == 0


def test_text_hash_format():
    """哈希为 sha256: 前缀 + 64 位十六进制，同文同值。"""
    value = text_tools.text_hash("林晚")
    assert value.startswith("sha256:")
    assert len(value) == len("sha256:") + 64
    assert value == text_tools.text_hash("林晚")
    assert value != text_tools.text_hash("林晚 ")


def test_require_and_invariant_violation():
    """不变式断言失败时抛领域异常，消息含字段名与中文说明。"""
    require(True, "title", "不应触发")
    with pytest.raises(InvariantViolation) as caught:
        require(False, "title", "标题不能为空")
    assert isinstance(caught.value, DomainError)
    assert caught.value.field == "title"
    assert "标题不能为空" in str(caught.value)
