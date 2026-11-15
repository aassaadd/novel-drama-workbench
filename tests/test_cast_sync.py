"""角色对齐：与节拍同一次调用产出，三条路径共用同一套确定性逻辑。

覆盖：一次调用同时产节拍与 cast、已有角色按姓名/别名复用并补别名、
叙述代称不建卡、第二次出现才建卡、一次性龙套按 minor 处理、
节拍没点名时回退旁白、续跑不重复建卡、扩充/续写同一逻辑。
运行：`.venv/bin/python -m pytest -q tests/test_cast_sync.py`。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novel_drama.adapters.offline_writing import OfflineWritingAdapter
from novel_drama.config import load_config
from novel_drama.models import entities
from novel_drama.store import ProjectStore
from novel_drama.store.settings_store import SettingsStore
from novel_drama.writing.cast_sync import apply_chapter_cast
from novel_drama.writing.continuation import continue_outline
from novel_drama.writing.expand import expand_outline
from novel_drama.writing.outline_hier import (
    generate_hierarchical_outline,
    load_work,
)
from novel_drama.writing.service import generate_outline

#: 旁白保留 ID
NARRATOR_ID = entities.NARRATOR_ID


class CountingAdapter(OfflineWritingAdapter):
    """记录每次工具调用的离线适配器（用于校验调用次数）。"""

    def __init__(self):
        """初始化调用记录。"""
        super().__init__()
        self.calls: list[str] = []

    def generate_tool(self, req, tool, timeout_s=None, on_delta=None):
        """记录工具名后交给离线规则。"""
        name = str(((tool or {}).get("function") or {}).get("name") or "")
        self.calls.append(name)
        return super().generate_tool(req, tool, timeout_s, on_delta)


class CastSyncTest(unittest.TestCase):
    """验证「节拍 + 角色」同一次调用产出的主路径。"""

    def setUp(self):
        """建临时工程与离线配置。"""
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ProjectStore(self.tmp.name)
        self.store.create("大纲角色对齐测试", author="ndw")
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

    def _chapter(self, index: int, rows: list[dict], beats: int = 2) -> dict:
        """构造一章用于直接调用角色对齐。

        参数:
            index: 章序号。
            rows: cast 行。
            beats: 节拍数。

        返回:
            章节 dict。
        """
        return {
            "index": index,
            "title": f"第{index}章",
            "summary": "",
            "beats": [{"summary": f"第 {pos} 拍"} for pos in range(1, beats + 1)],
        }

    def test_reuses_by_alias_and_supplements_aliases(self):
        """已有角色按别名命中：复用不建卡，并补上本章新称呼。"""
        ids = self._add_characters([("沈砚", "protagonist")])
        table = self.store.load_characters()
        for item in table["characters"]:
            if item["id"] == ids["沈砚"]:
                item["aliases"] = ["阿砚"]
        self.store.save_characters(table)

        chapter = self._chapter(1, [])
        report = apply_chapter_cast(
            self.store,
            chapter,
            [{"name": "阿砚", "aliases": ["小砚"], "beats": [1, 2]}],
            characters=[
                item for item in self.store.load_characters()["characters"] if item.get("role") != "narrator"
            ],
            pending={},
            persist=True,
            chapter_index=1,
        )
        self.assertEqual(report["created"], [])
        self.assertEqual(report["reused"], ["沈砚"])
        aliases = next(
            item["aliases"] for item in self.store.load_characters()["characters"] if item["id"] == ids["沈砚"]
        )
        self.assertIn("阿砚", aliases)
        self.assertIn("小砚", aliases)
        self.assertEqual([beat["character_ids"] for beat in chapter["beats"]], [[ids["沈砚"]], [ids["沈砚"]]])

    def test_descriptor_never_creates_card(self):
        """「女人」「对方」这类叙述代称不建卡，只按龙套记录。"""
        chapter = self._chapter(1, [])
        report = apply_chapter_cast(
            self.store,
            chapter,
            [
                {"name": "女人", "beats": [1]},
                {"name": "对方", "beats": [2]},
                {"name": "她", "beats": [1]},
            ],
            characters=[],
            pending={},
            persist=True,
            chapter_index=1,
        )
        self.assertEqual(report["created"], [])
        self.assertEqual(self._character_names(), [])
        self.assertEqual(chapter["beats"][0]["extra_characters"], ["女人", "她"])
        self.assertEqual(chapter["beats"][1]["extra_characters"], ["对方"])
        for beat in chapter["beats"]:
            self.assertEqual(beat["character_ids"], [NARRATOR_ID])

    def test_second_appearance_creates_card(self):
        """未命中候选第一次只登记，第二次出现（跨章）才建卡。"""
        pending: dict = {}
        first = self._chapter(1, [])
        report1 = apply_chapter_cast(
            self.store,
            first,
            [{"name": "顾三", "beats": [1]}],
            characters=[],
            pending=pending,
            persist=False,
            chapter_index=1,
        )
        self.assertEqual(report1["created"], [])
        self.assertIn("顾三", report1["extras"])
        self.assertIn("顾三", pending)
        self.assertEqual(first["beats"][0]["character_ids"], [NARRATOR_ID])

        second = self._chapter(2, [])
        report2 = apply_chapter_cast(
            self.store,
            second,
            [{"name": "顾三", "beats": [1, 2]}],
            characters=list(report1["characters"]),
            pending=pending,
            persist=False,
            chapter_index=2,
        )
        self.assertEqual(report2["created"], ["顾三"])
        self.assertNotIn("顾三", pending)
        created_id = report2["new_characters"][0]["id"]
        self.assertEqual(second["beats"][0]["character_ids"], [created_id])

    def test_narrator_fallback_when_no_character_named(self):
        """节拍没有点名任何角色时回退旁白，而不是硬绑主角。"""
        ids = self._add_characters([("沈砚", "protagonist")])
        chapter = self._chapter(1, [])
        report = apply_chapter_cast(
            self.store,
            chapter,
            [],
            characters=[
                item for item in self.store.load_characters()["characters"] if item.get("role") != "narrator"
            ],
            pending={},
            persist=True,
            chapter_index=1,
        )
        self.assertEqual(report["narrator_beats"], 2)
        for beat in chapter["beats"]:
            self.assertEqual(beat["character_ids"], [NARRATOR_ID])
            self.assertNotIn(ids["沈砚"], beat["character_ids"])

    def test_hier_outline_one_call_per_chapter(self):
        """分层大纲：每章只有一次节拍调用，同时产出 cast，不再单独请求角色。"""
        adapter = CountingAdapter()
        with patch("novel_drama.writing.outline_hier.build_writing_adapter", return_value=adapter):
            result = generate_hierarchical_outline(
                self.store,
                self.config,
                acts=1,
                chapters_per_act=2,
                beats_per_chapter=2,
                chapter_words=400,
                binding="auto",
            )
        self.assertEqual(adapter.calls.count("emit_beats"), 2, adapter.calls)
        self.assertNotIn("emit_chapter_cast", adapter.calls)
        # 幕 1 次 + 章节 1 次 + 节拍 2 次 = 4 次（旧实现每章还要多 1 次角色请求）
        self.assertEqual(len(adapter.calls), 4, adapter.calls)
        self.assertGreaterEqual(result["characters_created"], 1, result)

        valid_ids = {item["id"] for item in self.store.load_characters()["characters"]} | {NARRATOR_ID}
        work = load_work(self.store)
        for act in work["acts"]:
            for chapter in act["chapters"]:
                self.assertTrue(chapter.get("cast_synced"), chapter)
                for beat in chapter["beats"]:
                    self.assertTrue(beat.get("character_ids"), beat)
                    self.assertTrue(set(beat["character_ids"]) <= valid_ids, beat)

    def test_hier_outline_reuses_existing_cast(self):
        """分层大纲：已有同名角色一律复用，不重复建卡。"""
        ids = self._add_characters([("主人公", "protagonist")])
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
        self.assertGreaterEqual(result["characters_reused"], 1, result)
        self.assertEqual(len(self.store.load_characters()["characters"]), before)
        used = set()
        for chapter in result["acts"][0]["chapters"]:
            for beat in chapter["beats"]:
                used.update(beat["character_ids"])
        self.assertTrue(used <= set(ids.values()) | {NARRATOR_ID}, used)

    def test_hier_outline_resume_does_not_recreate(self):
        """续跑：节拍已齐的章节不再发请求，也不重复建卡。"""
        generate_hierarchical_outline(
            self.store,
            self.config,
            acts=1,
            chapters_per_act=2,
            beats_per_chapter=2,
            chapter_words=300,
            binding="auto",
        )
        before = sorted(self._character_names())
        adapter = CountingAdapter()
        with patch("novel_drama.writing.outline_hier.build_writing_adapter", return_value=adapter):
            again = generate_hierarchical_outline(
                self.store,
                self.config,
                acts=1,
                chapters_per_act=2,
                beats_per_chapter=2,
                chapter_words=300,
                binding="auto",
                resume=True,
            )
        self.assertEqual(adapter.calls, [], adapter.calls)
        self.assertEqual(again["characters_created"], 0, again)
        self.assertEqual(sorted(self._character_names()), before)

    def test_expand_outline_uses_same_logic(self):
        """扩充/续写：新章节的角色同样来自节拍那次调用。"""
        generate_outline(self.store, self.config, acts=1, chapters_per_act=1, beats_per_chapter=1, chapter_words=200)
        table = self.store.load_characters()
        table["characters"] = [item for item in table["characters"] if item.get("role") == "narrator"]
        self.store.save_characters(table)

        adapter = CountingAdapter()
        with patch("novel_drama.writing.expand.build_writing_adapter", return_value=adapter):
            result = expand_outline(
                self.store,
                self.config,
                mode="append",
                new_acts=1,
                chapters_per_act=2,
                beats_per_chapter=2,
                chapter_words=300,
                dry_run=False,
            )
        self.assertGreaterEqual(result["characters_created"], 1, result)
        self.assertEqual(adapter.calls.count("emit_beats"), 2, adapter.calls)
        self.assertNotIn("emit_chapter_cast", adapter.calls)

        valid_ids = {item["id"] for item in self.store.load_characters()["characters"]} | {NARRATOR_ID}
        inserted = {item["chapter_id"] for item in result["inserted_chapters"]}
        bible = SettingsStore(self.tmp.name).load()
        for act in bible["outline"]["acts"]:
            for chapter in act["chapters"]:
                if chapter.get("id") not in inserted:
                    continue
                for beat in chapter["beats"]:
                    self.assertTrue(beat.get("character_ids"), beat)
                    self.assertTrue(set(beat["character_ids"]) <= valid_ids, beat)

    def test_continue_outline_uses_same_logic(self):
        """CLI 续写：新增章节的角色同样来自同一次节拍调用。"""
        adapter = CountingAdapter()
        with patch("novel_drama.writing.continuation.build_writing_adapter", return_value=adapter):
            result = continue_outline(
                self.store,
                self.config,
                chapters=2,
                chapters_per_act=2,
                beats_per_chapter=2,
                chapter_words=300,
                dry_run=False,
            )
        self.assertNotIn("error", result, result)
        self.assertGreaterEqual(result["characters_created"], 1, result)
        # 一次 emit_act 出 2 章：cast 不额外发请求
        self.assertEqual(adapter.calls.count("emit_act"), 1, adapter.calls)
        self.assertNotIn("emit_chapter_cast", adapter.calls)

        valid_ids = {item["id"] for item in self.store.load_characters()["characters"]} | {NARRATOR_ID}
        bible = SettingsStore(self.tmp.name).load()
        for act in bible["outline"]["acts"]:
            for chapter in act["chapters"]:
                for beat in chapter["beats"]:
                    self.assertTrue(beat.get("character_ids"), beat)
                    self.assertTrue(set(beat["character_ids"]) <= valid_ids, beat)

    def test_preview_does_not_persist(self):
        """预览（dry_run）：角色对齐照做，但不写角色卡与登记文件。"""
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
            chapters_per_act=2,
            beats_per_chapter=2,
            chapter_words=300,
            dry_run=True,
        )
        self.assertGreaterEqual(result["characters_created"], 1, result)
        self.assertEqual(self._character_names(), [])
        self.assertEqual(len(SettingsStore(self.tmp.name).load()["outline"]["acts"]), before_acts)


if __name__ == "__main__":
    unittest.main()
