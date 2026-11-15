"""角色（Character）与角色表（Cast）的往返与规则测试。

黄金样本：
- `characters_real.json` 直接取自真实工程 `/Volumes/other/ndw_runs/tt1/characters.json`；
- `character_full.json` 是按真实 schema 补齐全部字段的样本（真实工程里还没有用满
  voice_binding / voice_profile / state 的角色），其中 `voice_profile.custom_note`
  是故意留的扩展键，用于验证「schema 允许扩展字段」时不会丢键。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from novel_drama.domain.cast.entities.cast import Cast
from novel_drama.domain.cast.entities.character import Character
from novel_drama.domain.cast.value_objects.voice import Stock, VoiceBinding
from novel_drama.domain.shared.errors import InvariantViolation
from novel_drama.store.schema_validator import validate

FIXTURES = Path(__file__).parent / "fixtures" / "domain"


def _load(name: str) -> dict:
    """读取样本 JSON。"""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _minimal_character(**overrides) -> dict:
    """构造一份满足必填项的最小角色字典。"""
    data = {
        "id": "chr_01M7C19017ABCDEFGHJKMNPQRS",
        "name": "林九斤",
        "role": "protagonist",
        "created_at": "2026-11-14T03:23:04Z",
        "updated_at": "2026-11-14T03:23:04Z",
    }
    data.update(overrides)
    return data


def test_cast_roundtrip_preserves_real_table():
    """真实角色表：往返幂等、逐字段完全一致、按基线通过 schema 校验。

    真实工程 `tt1` 里存在历史遗留值（`age_range="30-40"`、`speech_style.tempo="快"`），
    这类值必须原样保留，所以 schema 校验带 baseline（与现有落盘校验口径一致）。
    """
    raw = _load("characters_real.json")
    cast = Cast.from_dict(raw)
    out = cast.to_dict()
    assert Cast.from_dict(out).to_dict() == out
    validate(out, "character.schema.json", baseline=raw)
    assert out == raw


def test_chinese_label_in_storage_is_not_silently_converted():
    """落盘里的中文标签不静默改成英文键，避免用户下次保存时发现数据被系统改过。"""
    raw = _minimal_character(speech_style={"tempo": "快", "pitch": "高"})
    character = Character.from_dict(raw)
    assert character.speech_style.tempo == "快"
    assert character.to_dict()["speech_style"]["tempo"] == "快"
    with pytest.raises(InvariantViolation):
        Character.from_dict(raw, strict=True)


def test_character_full_roundtrip_keeps_extension_keys():
    """全字段角色：音色绑定/音色描述/状态卡/素材全部保真，扩展键不丢。"""
    raw = _load("character_full.json")
    character = Character.from_dict(raw)
    out = character.to_dict()
    assert Character.from_dict(out).to_dict() == out
    assert out["voice_profile"]["custom_note"] == raw["voice_profile"]["custom_note"]
    validate({"schema_version": 1, "characters": [out]}, "character.schema.json")


def test_narrator_must_use_reserved_id():
    """旁白角色的 ID 必须是 chr_narrator。"""
    with pytest.raises(InvariantViolation) as caught:
        Character.from_dict(_minimal_character(role="narrator"))
    assert "chr_narrator" in str(caught.value)

    narrator = Character.from_dict(_minimal_character(id="chr_narrator", name="旁白", role="narrator"))
    assert narrator.role.value == "narrator"


def test_legacy_enum_values_survive_load():
    """旧工程的自由文本年龄段：默认保留原值，严格模式拒绝（避免静默改用户数据）。"""
    raw = _minimal_character(age_range="30-40")
    assert Character.from_dict(raw).age_range == "30-40"
    with pytest.raises(InvariantViolation):
        Character.from_dict(raw, strict=True)


def test_cast_rejects_duplicate_names_and_aliases():
    """姓名/别名归一化后必须全书唯一。"""
    first = Character.from_dict(_minimal_character(name="林九斤", aliases=["九斤"]))
    second = Character.from_dict(
        _minimal_character(id="chr_01M7C19017ABCDEFGHJKMNPQRT", name="九 斤".replace(" ", ""))
    )
    with pytest.raises(InvariantViolation) as caught:
        Cast(characters=[first, second]).assert_unique_names()
    assert "重复" in str(caught.value)


def test_cast_find_by_name_matches_aliases():
    """按别名也能查到角色，且归一化忽略空白与大小写。"""
    cast = Cast.from_dict(_load("characters_real.json"))
    target = cast.characters[1]
    assert cast.find_by_name(target.name) is target
    assert cast.find_by_name(f" {target.name} ") is target


def test_cast_cannot_remove_narrator():
    """旁白不可删除。"""
    cast = Cast.from_dict(_load("characters_real.json"))
    with pytest.raises(InvariantViolation):
        cast.remove("chr_narrator")


def test_voice_binding_invariants():
    """音色绑定：至少一个音色 ID、速度区间、变体前缀都要校验。"""
    with pytest.raises(InvariantViolation):
        VoiceBinding.from_dict({"adapter": "qwen3-tts"})
    with pytest.raises(InvariantViolation):
        VoiceBinding.from_dict({"adapter": "qwen3-tts", "voice_id": "v1", "speed": 3.0})
    with pytest.raises(InvariantViolation):
        VoiceBinding.from_dict(
            {"adapter": "qwen3-tts", "voice_id": "v1", "emotion_variant_map": {"angry": "bad_id"}}
        )
    binding = VoiceBinding.from_dict({"adapter": "qwen3-tts", "voice_id": "v1"})
    assert binding is not None and binding.to_dict()["voice_id"] == "v1"


def test_stock_slot_pattern_and_minor_rule():
    """槽位键必须是稳定小写键；非 minor 角色带槽位只在严格模式下报错。"""
    with pytest.raises(InvariantViolation):
        Stock.from_dict({"slot": "Bad Slot"})

    raw = _minimal_character(role="supporting", stock={"slot": "waiter"})
    assert Character.from_dict(raw).stock.slot == "waiter"
    with pytest.raises(InvariantViolation):
        Character.from_dict(raw, strict=True)


def test_relation_cannot_point_to_self():
    """关系不能指向角色自己。"""
    character_id = "chr_01M7C19017ABCDEFGHJKMNPQRS"
    raw = _minimal_character(
        state={"relations": [{"target_id": character_id, "label": "自己"}]}
    )
    with pytest.raises(InvariantViolation) as caught:
        Character.from_dict(raw)
    assert "自己" in str(caught.value)


def test_rename_and_alias_refresh_updated_at():
    """改名与新增别名都会刷新更新时间，且别名去重。"""
    character = Character.from_dict(_minimal_character())
    character.rename("林九")
    assert character.name == "林九"
    character.add_alias("九斤")
    character.add_alias("九斤")
    assert character.aliases == ["九斤"]
    assert character.updated_at != "2026-11-14T03:23:04Z"
