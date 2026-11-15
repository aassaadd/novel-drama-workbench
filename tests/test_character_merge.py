"""角色合并：影响面统计 + 引用迁移（大纲、节拍、台词、关系、音色覆盖）。

运行：`.venv/bin/python -m pytest -q tests/test_character_merge.py`。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novel_drama.application.cast.sync_cast import load_pending, save_pending
from novel_drama.character_merge import merge_characters, merge_impact
from novel_drama.models import entities
from novel_drama.models.ids import new_id, now_iso
from novel_drama.store import ProjectStore
from novel_drama.store.settings_store import SettingsStore


class CharacterMergeTest(unittest.TestCase):
    """验证把新建角色并到已有角色后引用不留悬空。"""

    def setUp(self):
        """建临时工程与两个角色，并铺好各类引用。"""
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ProjectStore(self.tmp.name)
        self.store.create("角色合并测试", author="ndw")
        table = self.store.load_characters()
        self.source = entities.new_character("顾三", role="supporting")
        self.target = entities.new_character("沈砚", role="protagonist")
        self.other = entities.new_character("林见夏", role="supporting")
        self.other["state"] = {
            "relationship_notes": {self.source["id"]: "旧识"},
            "relations": [{"target_id": self.source["id"], "label": "旧识", "sentiment": "neutral"}],
        }
        table["characters"].extend([self.source, self.target, self.other])
        self.store.save_characters(table)

        bible = SettingsStore(self.tmp.name).ensure()
        act = SettingsStore.new_act("第一幕", "")
        chapter = SettingsStore.new_outline_chapter(new_id("chp"), 1, "第一章", "")
        chapter["pov_character_id"] = self.source["id"]
        chapter["character_arcs"] = [{"character_id": self.source["id"], "to_state": "决定离开"}]
        chapter["beats"].append(SettingsStore.new_beat("顾三把信放下", character_ids=[self.source["id"]]))
        act["chapters"].append(chapter)
        bible["outline"]["acts"].append(act)
        SettingsStore(self.tmp.name).save(bible)

        chapter_file = self.store.create_chapter("第一章", content="测试正文", index=1)
        scene = entities.new_scene(chapter_file["id"], 0, "门口")
        scene["character_ids"] = [self.source["id"]]
        scene["lines"].append(entities.new_line(scene["id"], 0, "你终于来了。", speaker_id=self.source["id"]))
        chapter_file["scenes"] = [scene]
        chapter_file["pov_character_id"] = self.source["id"]
        chapter_file["voice_overrides"] = {self.source["id"]: {"reason": "临时换音色"}}
        chapter_file["continuations"] = [
            {
                "id": new_id("cnt"),
                "mode": "continue",
                "instruction": None,
                "target_words": 100,
                "outline_ref": None,
                "range": None,
                "context": {"include_bible": True, "include_character_cards": [self.source["id"]]},
                "status": "done",
                "created_at": now_iso(),
            }
        ]
        self.store.save_chapter(chapter_file)
        self.chapter_id = chapter_file["id"]

    def tearDown(self):
        """清理临时工程。"""
        self.tmp.cleanup()

    def test_impact_counts_every_reference(self):
        """影响面要覆盖大纲、章节与关系三类引用。"""
        impact = merge_impact(self.store, self.source["id"], self.target["id"])
        self.assertEqual(impact["outline"], {"beats": 1, "pov": 1, "arcs": 1})
        self.assertEqual(impact["chapters"], {"scenes": 1, "lines": 1, "overrides": 1})
        self.assertEqual(impact["relations"], 2)
        self.assertEqual(impact["total"], 8)

    def test_merge_migrates_references_and_removes_source(self):
        """合并后所有引用指向目标角色，源角色被删除且姓名变别名。"""
        merge_characters(self.store, self.source["id"], self.target["id"])
        table = self.store.load_characters()
        ids = [item["id"] for item in table["characters"]]
        self.assertNotIn(self.source["id"], ids)
        target = next(item for item in table["characters"] if item["id"] == self.target["id"])
        self.assertIn("顾三", target["aliases"])

        other = next(item for item in table["characters"] if item["id"] == self.other["id"])
        relations = other["state"]["relations"]
        self.assertEqual([row["target_id"] for row in relations], [self.target["id"]])
        self.assertIn(self.target["id"], other["state"]["relationship_notes"])
        self.assertNotIn(self.source["id"], other["state"]["relationship_notes"])

        bible = SettingsStore(self.tmp.name).load()
        chapter = bible["outline"]["acts"][0]["chapters"][0]
        self.assertEqual(chapter["pov_character_id"], self.target["id"])
        self.assertEqual(chapter["character_arcs"][0]["character_id"], self.target["id"])
        self.assertEqual(chapter["beats"][0]["character_ids"], [self.target["id"]])

        saved = self.store.load_chapter(self.chapter_id)
        self.assertEqual(saved["pov_character_id"], self.target["id"])
        self.assertEqual(saved["scenes"][0]["character_ids"], [self.target["id"]])
        self.assertEqual(saved["scenes"][0]["lines"][0]["speaker_id"], self.target["id"])
        self.assertIn(self.target["id"], saved["voice_overrides"])
        self.assertNotIn(self.source["id"], saved["voice_overrides"])
        self.assertEqual(
            saved["continuations"][0]["context"]["include_character_cards"], [self.target["id"]]
        )

    def test_merge_cleans_pending_registry(self):
        """合并后同名候选不再待定，避免下一章又造一个重复角色。"""
        save_pending(self.store, {"顾三": {"name": "顾三", "chapter": 1, "chapter_title": "第一章", "aliases": []}})
        merge_characters(self.store, self.source["id"], self.target["id"])
        self.assertEqual(load_pending(self.store), {})

    def test_rejects_invalid_pairs(self):
        """不能合并到自己，也不能碰旁白。"""
        with self.assertRaises(ValueError):
            merge_characters(self.store, self.source["id"], self.source["id"])
        with self.assertRaises(ValueError):
            merge_characters(self.store, self.source["id"], entities.NARRATOR_ID)

    def test_web_endpoints(self):
        """页面用到的三个接口：影响面、合并、本章新建。"""
        from fastapi.testclient import TestClient

        from novel_drama.web.app import create_app

        config_path = Path(self.tmp.name) / "offline.yaml"
        config_path.write_text(
            "\n".join(
                [
                    "adapters:",
                    "  writing: offline",
                    "  sfx: offline",
                    "gpu:",
                    f"  lock_path: {Path(self.tmp.name) / 'gpu.lock'}",
                    "tts:",
                    f"  hf_home: {Path(self.tmp.name) / 'hf'}",
                    "  builtin_voices:",
                    f"    root: {Path(self.tmp.name) / 'voices'}",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        client = TestClient(create_app(project_root=str(self.store.root), config_path=str(config_path)))
        impact = client.get(
            "/api/ui/characters/merge/impact",
            params={"path": str(self.store.root), "source": self.source["id"], "target": self.target["id"]},
        )
        self.assertEqual(impact.status_code, 200, impact.text)
        self.assertEqual(impact.json()["total"], 8)

        merged = client.post(
            "/api/ui/characters/merge",
            json={"path": str(self.store.root), "source": self.source["id"], "target": self.target["id"]},
        )
        self.assertEqual(merged.status_code, 200, merged.text)
        self.assertTrue(merged.json()["merged"])

        created = client.get(
            "/api/ui/characters/new-this-chapter", params={"path": str(self.store.root)}
        )
        self.assertEqual(created.status_code, 200, created.text)
        self.assertIn("chapters", created.json())


if __name__ == "__main__":
    unittest.main()
