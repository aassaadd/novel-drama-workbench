"""角色删除保护：引用统计口径、默认拦截、确认后清理引用。

全部离线运行：使用临时工程铺好七类引用，验证删除前的影响面与清理结果。
运行：`.venv/bin/python -m pytest -q tests/test_character_delete.py`。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novel_drama.adapters.persistence import ProjectStore
from novel_drama.adapters.persistence.settings_store import SettingsStore
from novel_drama.application.cast.character_crud import (
    character_refs,
    remove_character,
)
from novel_drama.domain.shared.errors import PreconditionError
from novel_drama.models import entities
from novel_drama.models.ids import new_id


class CharacterDeleteTest(unittest.TestCase):
    """验证删除角色时的影响面统计与「默认拦截、确认后清理」。"""

    def setUp(self):
        """建临时工程，并给目标角色铺满七类引用。"""
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ProjectStore(self.tmp.name)
        self.store.create("角色删除测试", author="ndw")
        table = self.store.load_characters()
        self.target = entities.new_character("顾三", role="supporting")
        self.other = entities.new_character("林见夏", role="supporting")
        self.other["state"] = {
            "relationship_notes": {self.target["id"]: "旧识"},
            "relations": [{"target_id": self.target["id"], "label": "旧识", "sentiment": "neutral"}],
        }
        table["characters"].extend([self.target, self.other])
        self.store.save_characters(table)

        bible = SettingsStore(self.tmp.name).ensure()
        act = SettingsStore.new_act("第一幕", "")
        chapter = SettingsStore.new_outline_chapter(new_id("chp"), 1, "第一章", "")
        chapter["pov_character_id"] = self.target["id"]
        chapter["character_arcs"] = [{"character_id": self.target["id"], "to_state": "决定离开"}]
        chapter["beats"].append(SettingsStore.new_beat("顾三把信放下", character_ids=[self.target["id"]]))
        act["chapters"].append(chapter)
        bible["outline"]["acts"].append(act)
        SettingsStore(self.tmp.name).save(bible)

        chapter_file = self.store.create_chapter("第一章", content="测试正文", index=1)
        scene = entities.new_scene(chapter_file["id"], 0, "门口")
        scene["character_ids"] = [self.target["id"]]
        scene["lines"].append(entities.new_line(scene["id"], 0, "你终于来了。", speaker_id=self.target["id"]))
        chapter_file["scenes"] = [scene]
        chapter_file["voice_overrides"] = {self.target["id"]: {"reason": "临时换音色"}}
        self.store.save_chapter(chapter_file)
        self.chapter_id = chapter_file["id"]

    def tearDown(self):
        """清理临时工程。"""
        self.tmp.cleanup()

    def test_refs_cover_every_reference_source(self):
        """引用统计要覆盖大纲、章节、关系三类共七种来源。"""
        refs = character_refs(self.store, self.target["id"])
        self.assertEqual(refs["outline"], {"pov": 1, "beats": 1, "arcs": 1})
        self.assertEqual(refs["chapter_refs"], {"scenes": 1, "lines": 1, "overrides": 1})
        self.assertEqual(refs["total"], 8)
        keys = {item["key"] for item in refs["sources"]}
        self.assertEqual(keys, {"pov", "beats", "arcs", "scenes", "lines", "overrides", "relations"})
        for source in refs["sources"]:
            self.assertTrue(source["where"], source["key"])
        self.assertEqual(refs["relations"][0]["name"], "林见夏")

    def test_refs_reject_missing_character(self):
        """角色不存在时报可读错误，而不是返回空清单。"""
        with self.assertRaises(PreconditionError):
            character_refs(self.store, "chr_missing")

    def test_remove_is_blocked_until_confirmed(self):
        """默认拦截：角色仍在，提示里带总量与来源清单。"""
        with self.assertRaises(PreconditionError) as caught:
            remove_character(self.store, self.target["id"])
        message = str(caught.exception)
        self.assertIn("共 8 处", message)
        self.assertIn("大纲节拍选角", message)
        ids = [item["id"] for item in self.store.load_characters()["characters"]]
        self.assertIn(self.target["id"], ids)

    def test_force_remove_cleans_every_reference(self):
        """确认后删除：七类引用全部清理，盘上没有残留 ID。"""
        payload = remove_character(self.store, self.target["id"], force=True)
        self.assertEqual(payload["references"], 8)
        self.assertEqual(
            payload["cleaned"],
            {"pov": 1, "beats": 1, "arcs": 1, "scenes": 1, "lines": 1, "overrides": 1, "relations": 2},
        )

        table = self.store.load_characters()
        self.assertNotIn(self.target["id"], [item["id"] for item in table["characters"]])
        other = next(item for item in table["characters"] if item["id"] == self.other["id"])
        self.assertNotIn(self.target["id"], other["state"]["relationship_notes"])
        self.assertEqual(other["state"]["relations"], None)

        chapter = self.store.load_chapter(self.chapter_id)
        self.assertEqual(chapter["scenes"][0]["character_ids"], [])
        self.assertEqual(chapter["scenes"][0]["lines"][0]["speaker_id"], entities.UNKNOWN_SPEAKER_ID)
        self.assertNotIn("voice_overrides", chapter)

        bible = SettingsStore(self.tmp.name).load()
        outline = bible["outline"]["acts"][0]["chapters"][0]
        self.assertIsNone(outline["pov_character_id"])
        self.assertEqual([beat["character_ids"] for beat in outline["beats"]], [[]])
        self.assertNotIn("character_arcs", outline)

        with self.assertRaises(PreconditionError):
            character_refs(self.store, self.target["id"])

    def test_zero_reference_character_deletes_directly(self):
        """没有引用的角色不需要确认就能删。"""
        table = self.store.load_characters()
        spare = entities.new_character("纸扎喜娘", role="minor")
        table["characters"].append(spare)
        self.store.save_characters(table)

        payload = remove_character(self.store, spare["id"])
        self.assertEqual(payload["removed"], spare["id"])
        self.assertEqual(sum(payload["cleaned"].values()), 0)
        self.assertNotIn(spare["id"], [item["id"] for item in self.store.load_characters()["characters"]])

    def test_narrator_cannot_be_removed(self):
        """旁白是保留角色。"""
        with self.assertRaises(ValueError):
            remove_character(self.store, entities.NARRATOR_ID)

    def test_web_refs_and_delete_use_same_rules(self):
        """页面接口与用例同口径：未确认 412，确认后 200 并清理。"""
        from fastapi.testclient import TestClient

        from novel_drama.interface.http.app import create_app

        with TestClient(create_app(project_root=self.tmp.name)) as client:
            refs = client.get("/api/ui/characters/refs", params={"path": self.tmp.name, "character": self.target["id"]})
            self.assertEqual(refs.status_code, 200)
            self.assertEqual(refs.json()["total"], 8)

            blocked = client.post("/api/ui/characters/delete", json={"path": self.tmp.name, "id": self.target["id"]})
            self.assertEqual(blocked.status_code, 412)

            removed = client.post(
                "/api/ui/characters/delete",
                json={"path": self.tmp.name, "id": self.target["id"], "force": True},
            )
            self.assertEqual(removed.status_code, 200)
            self.assertEqual(sum(removed.json()["cleaned"].values()), 8)

            gone = client.get("/api/ui/characters/refs", params={"path": self.tmp.name, "character": self.target["id"]})
            self.assertEqual(gone.status_code, 412)


if __name__ == "__main__":
    unittest.main()
