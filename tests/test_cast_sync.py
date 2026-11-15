"""大纲阶段的角色设定：分层大纲与续写两条路径的离线用例。

覆盖：缺失角色当场建卡、已有角色复用并绑定节拍、续跑不重复建卡、
预览（persist=False / dry_run）不落盘、没有节拍时直接报错。
运行：`.venv/bin/python -m pytest -q tests/test_cast_sync.py`。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novel_drama.adapters.offline_writing import OfflineWritingAdapter
from novel_drama.config import load_config
from novel_drama.models import entities
from novel_drama.store import ProjectStore
from novel_drama.store.settings_store import SettingsStore
from novel_drama.writing.cast_sync import sync_chapter_cast
from novel_drama.writing.continuation import continue_outline
from novel_drama.writing.expand import expand_outline
from novel_drama.writing.outline_hier import generate_hierarchical_outline, load_work, save_work
from novel_drama.writing.service import generate_outline


class CastSyncTest(unittest.TestCase):
    """验证「每章节拍完成后立刻对齐角色卡」的主路径。"""

    def setUp(self):
        """建临时工程与离线配置。"""
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ProjectStore(self.tmp.name)
        self.store.create("大纲角色设定测试", author="ndw")
        self.config = load_config(str(self._runtime_config()))

    def tearDown(self):
        """清理临时工程。"""
        self.tmp.cleanup()

    def _runtime_config(self) -> Path:
        """生成最小离线配置，避免写入工程目录之外。

        返回:
            运行时配置文件路径。
        """
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

    def _add_characters(self, names: list[tuple[str, str]]) -> dict[str, str]:
        """写入若干角色卡并返回「姓名 → ID」。

        参数:
            names: [(姓名, 角色定位)]。

        返回:
            姓名到 ID 的映射。
        """
        table = self.store.load_characters()
        mapping: dict[str, str] = {}
        for name, role in names:
            character = entities.new_character(name, role=role)
            table["characters"].append(character)
            mapping[name] = character["id"]
        self.store.save_characters(table)
        return mapping

    def _character_names(self) -> list[str]:
        """返回当前角色卡里的非旁白姓名。

        返回:
            姓名列表。
        """
        return [
            item.get("name")
            for item in self.store.load_characters()["characters"]
            if item.get("role") != "narrator"
        ]

    def test_hier_outline_creates_missing_cast(self):
        """分层大纲：节拍完成后新建缺失角色，并把每个节拍绑定到具体角色。"""
        result = generate_hierarchical_outline(
            self.store,
            self.config,
            acts=1,
            chapters_per_act=2,
            beats_per_chapter=2,
            chapter_words=400,
            binding="auto",
        )
        self.assertGreaterEqual(result["characters_created"], 1, result)
        self.assertIn("主人公", self._character_names())

        valid_ids = {item["id"] for item in self.store.load_characters()["characters"]}
        work = load_work(self.store)
        self.assertIsNotNone(work)
        for act in work["acts"]:
            for chapter in act["chapters"]:
                self.assertTrue(chapter.get("cast_synced"), chapter)
                for beat in chapter["beats"]:
                    self.assertTrue(beat.get("character_ids"), beat)
                    self.assertTrue(set(beat["character_ids"]) <= valid_ids, beat)

        bible = SettingsStore(self.tmp.name).load()
        for chapter in bible["outline"]["acts"][0]["chapters"]:
            for beat in chapter["beats"]:
                self.assertTrue(beat.get("character_ids"), beat)
                self.assertTrue(set(beat["character_ids"]) <= valid_ids, beat)

    def test_hier_outline_reuses_existing_cast(self):
        """分层大纲：已有同名角色一律复用，不重复建卡。"""
        ids = self._add_characters([("沈砚", "protagonist"), ("林见夏", "supporting")])
        before = len(self.store.load_characters()["characters"])

        result = generate_hierarchical_outline(
            self.store,
            self.config,
            acts=1,
            chapters_per_act=2,
            beats_per_chapter=2,
            chapter_words=400,
            binding="auto",
        )
        self.assertEqual(result["characters_created"], 0, result)
        self.assertGreaterEqual(result["characters_reused"], 2, result)
        # 旁白 + 两个已有角色：不应新增
        self.assertEqual(len(self.store.load_characters()["characters"]), before)

        used = set()
        for chapter in result["acts"][0]["chapters"]:
            for beat in chapter["beats"]:
                used.update(beat["character_ids"])
        self.assertTrue(used <= set(ids.values()), used)

    def test_hier_outline_resume_does_not_recreate(self):
        """续跑：节拍已齐且已做过角色设定时不重复建卡。"""
        generate_hierarchical_outline(
            self.store,
            self.config,
            acts=1,
            chapters_per_act=1,
            beats_per_chapter=2,
            chapter_words=300,
            binding="auto",
        )
        before = sorted(self._character_names())

        again = generate_hierarchical_outline(
            self.store,
            self.config,
            acts=1,
            chapters_per_act=1,
            beats_per_chapter=2,
            chapter_words=300,
            binding="auto",
            resume=True,
        )
        self.assertEqual(again["characters_created"], 0, again)
        self.assertEqual(again["characters_reused"], 0, again)
        self.assertEqual(sorted(self._character_names()), before)

    def test_hier_outline_resume_backfills_missing_cast(self):
        """续跑补做：节拍已齐但没有角色设定记录时，补做角色设定并建卡。"""
        generate_hierarchical_outline(
            self.store,
            self.config,
            acts=1,
            chapters_per_act=1,
            beats_per_chapter=2,
            chapter_words=300,
            binding="auto",
        )
        work = load_work(self.store)
        for act in work["acts"]:
            for chapter in act["chapters"]:
                chapter.pop("cast_synced", None)
        save_work(self.store, work)
        table = self.store.load_characters()
        table["characters"] = [item for item in table["characters"] if item.get("role") == "narrator"]
        self.store.save_characters(table)

        again = generate_hierarchical_outline(
            self.store,
            self.config,
            acts=1,
            chapters_per_act=1,
            beats_per_chapter=2,
            chapter_words=300,
            binding="auto",
            resume=True,
        )
        self.assertGreaterEqual(again["characters_created"], 1, again)
        self.assertEqual(again["cast_chapters"], 1, again)
        self.assertIn("主人公", self._character_names())

    def test_sync_chapter_cast_preview_does_not_persist(self):
        """persist=False：只预览角色表与节拍绑定，不写 characters.json。"""
        chapter = {
            "index": 3,
            "title": "夜雨敲门",
            "summary": "沈砚在档案楼遇到林见夏。",
            "beats": [
                {"summary": "沈砚在档案楼门口停下。"},
                {"summary": "林见夏把旧信推过来。"},
            ],
        }
        before = self._character_names()
        report = sync_chapter_cast(
            self.store,
            self.config,
            chapter,
            adapter=OfflineWritingAdapter(),
            persist=False,
            chapter_index=3,
        )
        self.assertEqual(sorted(report["created"]), ["林见夏", "沈砚"])
        self.assertEqual(self._character_names(), before)
        for beat in chapter["beats"]:
            self.assertTrue(beat.get("character_ids"), beat)
        self.assertEqual(len(report["new_characters"]), 2)

    def test_sync_chapter_cast_requires_beats(self):
        """没有节拍时直接报错，不允许跳过角色设定。"""
        with self.assertRaises(ValueError):
            sync_chapter_cast(
                self.store,
                self.config,
                {"index": 1, "title": "空章", "summary": "", "beats": []},
                adapter=OfflineWritingAdapter(),
                persist=False,
            )

    def test_expand_outline_creates_cast(self):
        """扩充/续写（落盘）：新生成的章节先做角色设定再落盘。"""
        generate_outline(self.store, self.config, acts=1, chapters_per_act=1, beats_per_chapter=1, chapter_words=200)
        # 清掉已有角色，模拟「大纲里出现的人物还没有角色卡」
        table = self.store.load_characters()
        table["characters"] = [item for item in table["characters"] if item.get("role") == "narrator"]
        self.store.save_characters(table)

        result = expand_outline(
            self.store,
            self.config,
            mode="append",
            new_acts=1,
            chapters_per_act=1,
            beats_per_chapter=2,
            chapter_words=300,
            dry_run=False,
        )
        self.assertGreaterEqual(result["characters_created"], 1, result)
        self.assertIn("主人公", self._character_names())

        valid_ids = {item["id"] for item in self.store.load_characters()["characters"]}
        inserted = {item["chapter_id"] for item in result["inserted_chapters"]}
        self.assertTrue(inserted, result)
        bible = SettingsStore(self.tmp.name).load()
        for act in bible["outline"]["acts"]:
            for chapter in act["chapters"]:
                if chapter.get("id") not in inserted:
                    continue
                for beat in chapter["beats"]:
                    self.assertTrue(beat.get("character_ids"), beat)
                    self.assertTrue(set(beat["character_ids"]) <= valid_ids, beat)

    def test_expand_outline_preview_does_not_persist(self):
        """扩充预览：角色设定照做，但不写角色卡与设定库。"""
        generate_outline(self.store, self.config, acts=1, chapters_per_act=1, beats_per_chapter=1, chapter_words=200)
        table = self.store.load_characters()
        table["characters"] = [item for item in table["characters"] if item.get("role") == "narrator"]
        self.store.save_characters(table)
        before_acts = len(SettingsStore(self.tmp.name).load()["outline"]["acts"])

        result = expand_outline(
            self.store,
            self.config,
            mode="append",
            new_acts=1,
            chapters_per_act=1,
            beats_per_chapter=2,
            chapter_words=300,
            dry_run=True,
        )
        self.assertGreaterEqual(result["characters_created"], 1, result)
        self.assertEqual(self._character_names(), [])
        self.assertEqual(len(SettingsStore(self.tmp.name).load()["outline"]["acts"]), before_acts)

    def test_continue_outline_creates_cast(self):
        """续写（落盘）：新增章节同样先做角色设定再保存。"""
        result = continue_outline(
            self.store,
            self.config,
            chapters=2,
            chapters_per_act=1,
            beats_per_chapter=2,
            chapter_words=300,
            dry_run=False,
        )
        self.assertNotIn("error", result, result)
        self.assertGreaterEqual(result["characters_created"], 1, result)
        self.assertIn("主人公", self._character_names())

        valid_ids = {item["id"] for item in self.store.load_characters()["characters"]}
        bible = SettingsStore(self.tmp.name).load()
        for act in bible["outline"]["acts"]:
            for chapter in act["chapters"]:
                for beat in chapter["beats"]:
                    self.assertTrue(beat.get("character_ids"), beat)
                    self.assertTrue(set(beat["character_ids"]) <= valid_ids, beat)

    def test_continue_outline_preview_does_not_persist(self):
        """续写预览：角色设定照做，但不写角色卡与设定库。"""
        result = continue_outline(
            self.store,
            self.config,
            chapters=1,
            chapters_per_act=1,
            beats_per_chapter=2,
            chapter_words=300,
            dry_run=True,
        )
        self.assertNotIn("error", result, result)
        self.assertGreaterEqual(result["characters_created"], 1, result)
        self.assertEqual(self._character_names(), [])
        self.assertEqual(SettingsStore(self.tmp.name).load()["outline"]["acts"], [])


if __name__ == "__main__":
    unittest.main()
