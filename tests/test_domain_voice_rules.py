"""阶段 3 音色 10 级解析的并跑比对：领域优先级与旧实现逐条一致。

合成一套覆盖 10 级优先级的工程（音色库 + 角色表 + 章节覆盖 + cast 配置），
逐条比对旧入口 `store.voice_cast.resolve_cast_voice` 与领域实现；
另外用真实工程（tt1）的章节/角色表比对旁白人格、展开表与描述哈希。

    NDW_GOLDEN_PROJECTS=/Volumes/other/ndw_runs/tt1 \\
        .venv/bin/python -m pytest -q tests/test_domain_voice_rules.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from novel_drama.adapters.persistence import voice_cast as legacy
from novel_drama.domain.speech.rules import narrators as domain_narrators
from novel_drama.domain.speech.rules.voice_ladder import (
    profile_source_hash,
    resolve_voice,
)
from novel_drama.models import entities

PROJECTS = [Path(item) for item in os.environ.get("NDW_GOLDEN_PROJECTS", "").split(":") if item]


class _FakeStore:
    """最小工程替身：只提供解析需要的方法。"""

    def __init__(self, root: Path, settings: dict, voices: dict, chapters: list[dict]):
        """保存工程根、设置、音色库与章节列表。"""
        self.root = root
        self._settings = settings
        self._voices = voices
        self._chapters = chapters

    def settings(self) -> dict:
        """返回工程设置。"""
        return self._settings

    def load_voices(self) -> dict:
        """返回音色库文档。"""
        return self._voices

    def list_chapters(self) -> list[dict]:
        """返回章节元信息。"""
        return self._chapters


def _voice(voice_id: str, name: str, *, status: str = "ready", variant_id: str | None = None, emotion: str | None = None) -> dict:
    """构造一个音色（默认带一个可用的克隆参考变体）。"""
    return {
        "id": voice_id,
        "name": name,
        "status": status,
        "variants": [
            {
                "id": variant_id or f"{voice_id}_v1",
                "status": "ready",
                "emotion_tags": [emotion] if emotion else ["default"],
                "reference": {"audio_path": f"assets/ref/{voice_id}.wav", "text": f"{name}参考"},
            }
        ],
    }


def _fixture() -> tuple[_FakeStore, dict, dict]:
    """构造覆盖 10 级优先级的工程；返回 (store, 角色表, 章节)。"""
    voices = {
        "voices": [
            _voice("voc_a", "顾三音", emotion="neutral"),
            _voice("voc_b", "沈砚音", emotion="sad"),
            _voice("voc_line", "行级音"),
            _voice("voc_chapter", "章节音"),
            _voice("voc_persona", "旁白甲"),
            _voice("voc_stock", "龙套音"),
            _voice("voc_minor", "龙套兜底"),
            _voice("voc_narr_fb", "旁白兜底"),
            _voice("voc_unknown", "路人音"),
            _voice("voc_default", "工程默认"),
            _voice("voc_dead", "停用音", status="unusable"),
        ]
    }
    settings = {
        "tts": {
            "default_voice_id": "voc_default",
            "cast": {
                "narrators": [{"id": "per_a", "label": "旁白甲", "voice_id": "voc_persona"}],
                "default_narrator_id": "per_a",
                "narrator_switches": [],
                "stock_defaults": {"守卫": "voc_stock"},
                "minor_fallback_voice_id": "voc_minor",
                "narrator_fallback_voice_id": "voc_narr_fb",
                "unknown_voice_id": "voc_unknown",
                "honor_line_override": True,
            },
        }
    }
    chapters = [
        {"id": "chp_1", "index": 1, "title": "第一章"},
        {"id": "chp_2", "index": 2, "title": "第二章"},
    ]
    characters = {
        "schema_version": 1,
        "characters": [
            {"id": "chr_a", "name": "顾三", "role": "protagonist", "voice_binding": {"voice_id": "voc_a"}},
            {"id": "chr_b", "name": "沈砚", "role": "supporting", "voice_binding": {"voice_id": "voc_b"}},
            {"id": "chr_minor", "name": "路人甲", "role": "minor"},
            {"id": "chr_stock", "name": "守卫", "role": "supporting", "stock": {"slot": "守卫"}},
            {"id": "chr_dead", "name": "停用者", "role": "supporting", "voice_binding": {"voice_id": "voc_dead"}},
            {"id": "chr_plain", "name": "未绑定者", "role": "supporting"},
            {"id": "chr_narrator", "name": "旁白", "role": "narrator"},
        ],
    }
    store = _FakeStore(Path("."), settings, voices, chapters)
    chapter = {
        "id": "chp_1",
        "index": 1,
        "title": "第一章",
        "voice_overrides": {
            "chr_narrator": {"voice_id": "voc_chapter", "reason": "本章低沉"},
            "chr_a": {"voice_id": "voc_chapter", "reason": "主角不许覆盖"},
            "chr_b": {"voice_id": "voc_b"},
        },
    }
    return store, characters, chapter


def _line(speaker_id: str, **overrides) -> dict:
    """构造一条台词行。"""
    line = {"id": f"lin_{speaker_id}", "speaker_id": speaker_id, "emotion": "neutral", "text": "台词"}
    line.update(overrides)
    return line


def _domain(store: _FakeStore, characters_table: dict, chapter: dict, line: dict, voices: dict | None = None) -> dict:
    """按适配层口径准备输入并调用领域优先级。"""
    voices_doc = voices if voices is not None else store.load_voices()
    cast = legacy.cast_config(store)
    characters = {item.get("id"): item for item in characters_table.get("characters") or []}
    speaker_id = line.get("speaker_id")
    persona = legacy.narrator_persona(cast, chapter, legacy._chapter_order(store)) if speaker_id == entities.NARRATOR_ID else None
    return resolve_voice(
        line,
        characters.get(speaker_id),
        chapter,
        cast,
        store.settings(),
        lambda voice_id, emotion, variant_id: legacy._entry_from_voice(store, voices_doc, voice_id, emotion, variant_id),
        persona=persona,
        is_narrator=speaker_id == entities.NARRATOR_ID,
        is_unknown_speaker=speaker_id == entities.UNKNOWN_SPEAKER_ID,
        honor_line_override=cast.get("honor_line_override", True),
    )


def _level_cases() -> list[tuple[str, dict]]:
    """覆盖 10 级优先级的台词行。"""
    return [
        ("line_variant", _line("chr_b", voice={"variant_id": "voc_line_v1", "voice_id": "voc_line", "reason": "临时"})),
        ("line_voice", _line("chr_b", voice={"voice_id": "voc_line", "reason": "临时"})),
        ("line_no_reason", _line("chr_b", voice={"voice_id": "voc_line"})),
        ("chapter_override_narrator", _line(entities.NARRATOR_ID)),
        ("chapter_override_invalid", _line("chr_a")),
        ("chapter_override_no_reason", _line("chr_b")),
        ("narrator_persona", _line(entities.NARRATOR_ID, **{"voice": None})),
        ("binding", _line("chr_a")),
        ("binding_unusable", _line("chr_dead")),
        ("stock_slot", _line("chr_stock")),
        ("minor_fallback", _line("chr_minor")),
        ("unknown", _line(entities.UNKNOWN_SPEAKER_ID)),
        ("missing_character", _line("chr_ghost")),
        ("project_default", _line("chr_plain")),
        ("missing_everything", _line("chr_ghost2")),
    ]


@pytest.mark.parametrize("name,line", _level_cases(), ids=[case[0] for case in _level_cases()])
def test_voice_ladder_matches_legacy(name, line):
    """10 级优先级：领域实现与旧入口逐字段一致（含 warnings / decided_by）。"""
    store, characters, chapter = _fixture()
    if name == "missing_everything":
        # 去掉所有兜底，验证 source=missing
        store._settings["tts"].pop("default_voice_id", None)
        store._settings["tts"]["cast"].pop("unknown_voice_id", None)
        store._settings["tts"]["cast"].pop("minor_fallback_voice_id", None)
        line = _line("chr_minor")
    old = legacy.resolve_cast_voice(store, characters, chapter, line)
    new = _domain(store, characters, chapter, line)
    assert old == new, name


def test_voice_ladder_covers_all_levels():
    """确认用例确实走到了 1/2/3/4/5/6/7/8/9/10 级。"""
    store, characters, chapter = _fixture()
    levels = {name: _domain(store, characters, chapter, line)["decided_by"]["level"] for name, line in _level_cases()}
    plain = {key: value for key, value in chapter.items() if key != "voice_overrides"}
    levels["narrator_persona"] = _domain(store, characters, plain, dict(_level_cases())["narrator_persona"])["decided_by"]["level"]
    assert levels["line_variant"] == 1
    assert levels["line_voice"] == 2
    assert levels["chapter_override_narrator"] == 3
    assert levels["narrator_persona"] == 4
    assert levels["binding"] == 5
    assert levels["stock_slot"] == 6
    assert levels["minor_fallback"] == 7
    assert levels["unknown"] == 8
    assert levels["missing_character"] == 8
    assert levels["project_default"] == 9


def test_narrator_persona_and_plan_match_legacy():
    """旁白人格与逐章展开表：领域实现与旧实现一致（含区间缺省延续）。"""
    cast = {
        "narrators": [{"id": "per_a", "label": "甲", "voice_id": "voc_a"}, {"id": "per_b", "label": "乙", "voice_id": "voc_b"}],
        "default_narrator_id": "per_a",
        "narrator_switches": [
            {"at_chapter_id": "chp_2", "persona_id": "per_b", "reason": "换人"},
            {"at_chapter_id": "chp_3", "persona_id": "per_a", "reason": "换回", "scope": "this_chapter_only"},
        ],
    }
    chapters = [{"id": f"chp_{index}", "index": index, "title": f"第{index}章"} for index in range(1, 5)]
    order = {item["id"]: item["index"] for item in chapters}
    for chapter in chapters:
        assert domain_narrators.narrator_persona(cast, chapter, order) == legacy.narrator_persona(cast, chapter, order)
    assert domain_narrators.narrator_plan(cast, chapters) == legacy.narrator_plan(cast, chapters)


def test_profile_hash_matches_legacy_on_real_characters():
    """音色描述哈希：真实角色与边界值上与旧实现一致。"""
    samples = [{"gender": None, "age_range": None, "personality": [], "background": "", "speech_style": {}, "appearance_prompt": ""}]
    for root in PROJECTS:
        path = root / "characters.json"
        if path.exists():
            samples.extend(json.loads(path.read_text(encoding="utf-8")).get("characters") or [])
    for character in samples:
        assert profile_source_hash(character) == legacy.profile_source_hash(character)
