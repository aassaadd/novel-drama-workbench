"""角色与正文联动：提示词注入、POV 阻断、绑定对账、关系/弧线、回流与一致性。

全部离线运行：使用临时工程 + 显式 offline 适配器，验证异常与边界。
运行：`.venv/bin/python -m pytest -q tests/test_character_linkage.py`。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novel_drama.adapters.base import AdapterError
from novel_drama.cli_character import _clean_speech_style
from novel_drama.config import load_config
from novel_drama.models import entities
from novel_drama.store import ProjectStore
from novel_drama.store.settings_store import SettingsStore
from novel_drama.text.context import ContextAssembler
from novel_drama.application.cast.character_updates import (
    apply_character_updates,
    load_character_updates,
    propose_character_updates,
)
from novel_drama.application.story.consistency import check_chapter_consistency
from novel_drama.application.story.service import (
    apply_outline,
    bind_beat_characters,
    chapter_binding,
    generate_chapter,
    generate_outline,
    sync_chapter_pov,
)


class CharacterLinkageTest(unittest.TestCase):
    """覆盖 P0/P1/P2 的角色-正文联动关键路径。"""

    def setUp(self):
        """建临时工程：两章大纲落地，造两个角色并写入视角/节拍选角。"""
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ProjectStore(self.tmp.name)
        self.store.create("角色联动测试", author="ndw")
        self.config = load_config(str(self._runtime_config()))
        generate_outline(
            self.store,
            self.config,
            acts=1,
            chapters_per_act=2,
            beats_per_chapter=2,
            chapter_words=400,
        )
        apply_outline(self.store)
        table = self.store.load_characters()
        self.lead = entities.new_character("沈砚", role="protagonist")
        self.lead["personality"] = ["克制", "多疑"]
        self.lead["speech_style"] = {
            "tempo": "slow",
            "pitch": "low",
            "accent": "北方口音",
            "catchphrases": ["先别急"],
        }
        self.lead["state"] = {
            "situation": "刚到档案楼",
            "knowledge": ["旧信的存在"],
            "relationship_notes": {},
            "relations": None,
            "as_of_chapter_id": None,
        }
        self.peer = entities.new_character("林见夏", role="supporting")
        self.peer["speech_style"] = {
            "tempo": "fast",
            "pitch": "high",
            "accent": "南方口音",
            "catchphrases": ["我早说过"],
        }
        table["characters"].extend([self.lead, self.peer])
        self.store.save_characters(table)
        settings = SettingsStore(self.tmp.name)
        bible = settings.load()
        self.outline_chapters = bible["outline"]["acts"][0]["chapters"]
        first = self.outline_chapters[0]
        first["pov_character_id"] = self.lead["id"]
        first["pov_mode"] = None
        first["character_arcs"] = [
            {"character_id": self.lead["id"], "from_state": "只想查账", "to_state": "决定追查旧信", "note": ""}
        ]
        first["beats"][0]["character_ids"] = [self.lead["id"]]
        first["beats"][1]["character_ids"] = [self.lead["id"], self.peer["id"]]
        # 第二章故意不设 POV，用来验证生成阻断
        second = self.outline_chapters[1]
        second["pov_character_id"] = None
        second["pov_mode"] = None
        for beat in second["beats"]:
            beat["character_ids"] = [self.lead["id"], self.peer["id"]]
        settings.save(bible)
        self.chapter_id = first["id"]
        self.chapter_two = second["id"]

    def tearDown(self):
        """清理临时工程。"""
        self.tmp.cleanup()

    def _runtime_config(self) -> Path:
        """生成最小离线配置，避免写入工程目录之外。"""
        root = Path(self.tmp.name)
        path = root / "offline.yaml"
        path.write_text(
            "\n".join(
                [
                    "adapters:",
                    "  writing: offline",
                    "  sfx: offline",
                    "gpu:",
                    f"  lock_path: {root / 'gpu.lock'}",
                    "tts:",
                    f"  hf_home: {root / 'hf'}",
                    "  builtin_voices:",
                    f"    root: {root / 'voices'}",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        return path

    def _scene(self, scene_id: str, chapter_id: str, lines: list[dict]) -> dict:
        """构造一个通过 schema 的场景（含必填字段）。"""
        return {
            "id": scene_id,
            "chapter_id": chapter_id,
            "index": 0,
            "title": None,
            "location": None,
            "time_of_day": "unknown",
            "mood": None,
            "goal": None,
            "summary": None,
            "source_text": None,
            "character_ids": sorted({row["speaker_id"] for row in lines if row["speaker_id"] != "chr_narrator"}),
            "target_duration_ms": None,
            "status": "draft",
            "lines": lines,
            "sfx_events": [],
            "key_images": [],
            "video_segments": [],
            "revision": 1,
        }

    def _line(self, line_id: str, scene_id: str, speaker_id: str, text: str, index: int = 0) -> dict:
        """构造一行通过 schema 的台词。"""
        return {
            "id": line_id,
            "scene_id": scene_id,
            "index": index,
            "kind": "dialogue",
            "speaker_id": speaker_id,
            "text": text,
            "emotion": "neutral",
            "revision": 1,
        }

    def test_context_includes_pov_beat_cast_and_speech(self):
        """提示词必须真的带视角人物、每拍出场角色与说话方式，且不含 chr_ ID。"""
        bible = SettingsStore(self.tmp.name).load()
        chapter = self.store.load_chapter(self.chapter_id)
        assembler = ContextAssembler(
            work={"title": "角色联动测试"},
            bible=bible,
            characters=self.store.load_characters()["characters"],
            chapter=chapter,
            outline_chapter=self.outline_chapters[0],
            pov_character_id=self.lead["id"],
            planned_counts={self.lead["id"]: 2, self.peer["id"]: 1},
            pov_mode=None,
        )
        req = assembler.build("chapter", "写本章")
        system = req.system
        user = req.messages[-1].content
        self.assertIn("视角人物：沈砚（第三人称有限视角）", user)
        self.assertIn("出场：沈砚）", user)
        self.assertIn("出场：沈砚、林见夏", user)
        self.assertIn("说话方式：语速慢；音高偏低；北方口音；口头禅「先别急」", system)
        self.assertIn("当前处境：刚到档案楼", system)
        self.assertIn("已知信息：旧信的存在", system)
        self.assertIn("沈砚：只想查账 → 决定追查旧信", user)
        self.assertNotIn("chr_", system + user)

    def test_relationship_notes_render_names_not_ids(self):
        """关系键即使是内部 ID，提示词也只出现姓名。"""
        table = self.store.load_characters()
        lead = next(item for item in table["characters"] if item["id"] == self.lead["id"])
        lead["state"]["relationship_notes"] = {self.peer["id"]: "互相试探"}
        self.store.save_characters(table)
        bible = SettingsStore(self.tmp.name).load()
        assembler = ContextAssembler(
            bible=bible,
            characters=self.store.load_characters()["characters"],
            outline_chapter=self.outline_chapters[0],
            pov_character_id=self.lead["id"],
        )
        req = assembler.build("chapter", "写本章")
        system = req.system
        user = req.messages[-1].content
        self.assertIn("关系：林见夏——互相试探", system)
        self.assertNotIn(self.peer["id"], system)

    def test_missing_pov_blocks_generation(self):
        """缺 POV 且非全知时必须阻断，不允许静默生成。"""
        with self.assertRaises(AdapterError) as ctx:
            generate_chapter(self.store, self.config, self.chapter_two)
        self.assertIn("视角人物", str(ctx.exception))
        self.assertEqual((ctx.exception.detail or {}).get("code"), "BINDING_POV_MISSING")

    def test_omniscient_can_generate_without_pov(self):
        """显式全知视角可不绑定角色并正常生成。"""
        sync_chapter_pov(self.store, self.chapter_two, None, pov_mode="third_omniscient", update_mode=True)
        generate_chapter(self.store, self.config, self.chapter_two)
        chapter = self.store.load_chapter(self.chapter_two)
        self.assertTrue(chapter["content"].strip())
        binding = chapter_binding(self.store, self.chapter_two)
        self.assertEqual(binding["pov_mode"], "third_omniscient")
        self.assertFalse(any(item["code"] == "BINDING_POV_MISSING" for item in binding["blocking"]))

    def test_sync_pov_writes_through_to_chapter(self):
        """POV 以设定库为主源，同时写通章节副本。"""
        sync_chapter_pov(self.store, self.chapter_two, self.lead["id"], update_mode=True)
        chapter = self.store.load_chapter(self.chapter_two)
        bible = SettingsStore(self.tmp.name).load()
        entry = next(ch for ch in bible["outline"]["acts"][0]["chapters"] if ch["id"] == self.chapter_two)
        self.assertEqual(chapter["pov_character_id"], self.lead["id"])
        self.assertEqual(entry["pov_character_id"], self.lead["id"])

    def test_bind_beat_characters_rejects_unknown(self):
        """节拍选角只接受存在的角色 ID。"""
        beat = self.outline_chapters[0]["beats"][0]
        with self.assertRaises(ValueError):
            bind_beat_characters(self.store, self.chapter_id, beat["id"], ["chr_missing000000"])
        result = bind_beat_characters(self.store, self.chapter_id, beat["id"], [self.peer["id"]])
        row = next(item for item in result["beats"] if item["beat_id"] == beat["id"])
        self.assertEqual(row["character_ids"], [self.peer["id"]])

    def test_generation_output_reflects_pov_and_speech(self):
        """离线最小生成：输出必须体现视角人物与口头禅，证明提示词影响了结果。"""
        generate_chapter(self.store, self.config, self.chapter_id)
        chapter = self.store.load_chapter(self.chapter_id)
        self.assertIn("沈砚", chapter["content"])
        self.assertIn("先别急", chapter["content"])

    def test_binding_audit_derives_actual_from_scenes_and_lines(self):
        """实际出场从场景与台词派生，计划 vs 实际能对账。"""
        chapter = self.store.load_chapter(self.chapter_id)
        scene_id = "scn_000000000001"
        chapter["scenes"] = [
            self._scene(
                scene_id,
                self.chapter_id,
                [
                    self._line("lin_000000000001", scene_id, self.peer["id"], "我早说过。", 0),
                    self._line("lin_000000000002", scene_id, entities.UNKNOWN_SPEAKER_ID, "谁在那儿？", 1),
                ],
            )
        ]
        self.store.save_chapter(chapter)
        binding = chapter_binding(self.store, self.chapter_id)
        peer = next(row for row in binding["characters"] if row["id"] == self.peer["id"])
        self.assertEqual(peer["lines"], 1)
        self.assertEqual(peer["scenes"], 1)
        self.assertTrue(binding["audit"]["pov_not_in_prose"])
        self.assertEqual(binding["audit"]["unknown_speakers"][0]["speaker_id"], entities.UNKNOWN_SPEAKER_ID)

    def test_character_updates_require_confirmation(self):
        """回流提案只落预览；确认后才改角色卡。"""
        generate_chapter(self.store, self.config, self.chapter_id)
        record = propose_character_updates(self.store, self.config, self.chapter_id)
        self.assertTrue(record["updates"])
        before = next(
            item for item in self.store.load_characters()["characters"] if item["id"] == self.lead["id"]
        )
        self.assertEqual((before.get("state") or {}).get("situation"), "刚到档案楼")
        pending = load_character_updates(self.store, self.chapter_id)
        self.assertFalse(pending["applied"])
        result = apply_character_updates(self.store, self.chapter_id)
        self.assertTrue(result["applied"])
        after = next(
            item for item in self.store.load_characters()["characters"] if item["id"] == self.lead["id"]
        )
        self.assertNotEqual((after.get("state") or {}).get("situation"), "刚到档案楼")
        self.assertEqual((after.get("state") or {}).get("as_of_chapter_id"), self.chapter_id)

    def test_consistency_check_lists_derived_issues(self):
        """一致性检查包含派生问题（计划未出场/未知说话人）。"""
        chapter = self.store.load_chapter(self.chapter_id)
        chapter["content"] = "沈砚站在档案楼门口，听见有人靠近。\n"
        scene_id = "scn_000000000002"
        chapter["scenes"] = [
            self._scene(
                scene_id,
                self.chapter_id,
                [self._line("lin_000000000003", scene_id, entities.UNKNOWN_SPEAKER_ID, "站住。", 0)],
            )
        ]
        self.store.save_chapter(chapter)
        record = check_chapter_consistency(self.store, self.config, self.chapter_id)
        kinds = {item["kind"] for item in record["issues"]}
        self.assertIn("unplanned", kinds)
        self.assertIn("other", kinds)
        self.assertGreaterEqual(record["counts"]["total"], 2)


class CleanSpeechStyleTest(unittest.TestCase):
    """说话方式清洗：历史非法值原样提交时放行，改成新的非法值仍然拒绝。"""

    def test_unchanged_legacy_value_passes(self):
        """只修语速时，另一处历史非法音高原样保留，不再连带报错。"""
        cleaned = _clean_speech_style(
            {"tempo": "fast", "pitch": "高", "accent": "北方口音"},
            {"tempo": "快", "pitch": "高"},
        )
        self.assertEqual(cleaned["tempo"], "fast")
        self.assertEqual(cleaned["pitch"], "高")
        self.assertEqual(cleaned["accent"], "北方口音")

    def test_new_illegal_value_rejected(self):
        """新写入的非法语速仍然抛出中文错误并列出可选值。"""
        with self.assertRaises(ValueError) as ctx:
            _clean_speech_style({"tempo": "飞快"}, {"tempo": "快"})
        self.assertIn("语速", str(ctx.exception))
        self.assertIn("可选值", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
