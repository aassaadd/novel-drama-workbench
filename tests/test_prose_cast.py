"""正文角色抽取：确定性扫描、预览不建卡、确认建卡幂等、代称过滤与缓存复用。

全部离线运行：临时工程 + offline 配置；需要「新人物」时用固定返回的桩适配器。
运行：`.venv/bin/python -m pytest -q tests/test_prose_cast.py`。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novel_drama.adapters.llm.contracts import TextGenResult
from novel_drama.adapters.persistence import ProjectStore
from novel_drama.application.cast import prose_cast
from novel_drama.application.cast.prose_cast import (
    apply_prose_cast,
    ensure_prose_cast,
    extract_prose_cast,
    load_prose_cast,
    scan_mentions,
    scope_character_ids,
)
from novel_drama.application.cast.sync_cast import created_in_chapter
from novel_drama.infrastructure.config import load_config
from novel_drama.models import entities


class _StubAdapter:
    """返回固定 JSON 的写作适配器（记录调用次数）。"""

    def __init__(self, payload: dict) -> None:
        """绑定要返回的 JSON。

        参数:
            payload: 模型输出结构（会被序列化成文本）。
        """
        self.payload = payload
        self.calls = 0

    def generate(self, req) -> TextGenResult:
        """返回固定 JSON 并记录调用。

        参数:
            req: 文本请求（忽略）。

        返回:
            TextGenResult。
        """
        self.calls += 1
        return TextGenResult(
            text=json.dumps(self.payload, ensure_ascii=False),
            finish_reason="stop",
            model="stub",
        )


class ProseCastTest(unittest.TestCase):
    """覆盖 Task 1：正文角色抽取与确认建卡。"""

    def setUp(self):
        """建临时工程与一章正文，并放一个带别名的现有角色。"""
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ProjectStore(self.tmp.name)
        self.store.create("正文角色抽取测试", author="ndw")
        self.config = load_config(str(self._runtime_config()))
        table = self.store.load_characters()
        self.shen = entities.new_character("沈砚", role="protagonist", aliases=["老沈"])
        table["characters"].append(self.shen)
        self.store.save_characters(table)
        self.chapter = entities.new_chapter(
            self.store.load_project()["work"]["id"],
            1,
            "第一章",
            content="沈砚推门进来，老沈没有回头。\n",
        )
        self.store.save_chapter(self.chapter)
        self.chapter_id = self.chapter["id"]

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

    def _set_content(self, content: str) -> None:
        """改写本章正文并保存。

        参数:
            content: 新正文。

        返回:
            无。
        """
        chapter = self.store.load_chapter(self.chapter_id)
        chapter["content"] = content
        self.store.save_chapter(chapter)

    def _extract_with(self, payload: dict) -> dict:
        """用桩适配器执行一次抽取。

        参数:
            payload: 模型返回结构。

        返回:
            抽取记录。
        """
        adapter = _StubAdapter(payload)
        with patch.object(prose_cast, "build_writing_adapter", return_value=adapter):
            return extract_prose_cast(self.store, self.config, self.chapter_id)

    def test_scan_mentions_is_longest_first_and_non_overlapping(self):
        """长名优先、非重叠计数，单字名不参与扫描。"""
        characters = [
            entities.new_character("林夏", role="supporting"),
            entities.new_character("林夏青", role="supporting"),
            entities.new_character("李", role="minor"),
        ]
        rows = scan_mentions("林夏青来了。林夏看着林夏青。李没说话。\n", characters)
        mentions = {row["name"]: row["mentions"] for row in rows}
        self.assertEqual(mentions.get("林夏青"), 2)
        self.assertEqual(mentions.get("林夏"), 1)
        self.assertNotIn("李", mentions)
        row = next(item for item in rows if item["name"] == "林夏青")
        self.assertEqual(row["evidence"], "林夏青来了。")

    def test_extract_links_existing_without_creating_cards(self):
        """正文命中现有角色姓名/别名时标记 linked，且不写角色库。"""
        record = extract_prose_cast(self.store, self.config, self.chapter_id)
        self.assertEqual(record["version"], "prose_cast/1")
        self.assertTrue(record["content_hash"].startswith("sha256:"))
        self.assertFalse(record["applied"])
        row = next(item for item in record["characters"] if item["character_id"] == self.shen["id"])
        self.assertEqual(row["status"], "linked")
        self.assertGreaterEqual(row["mentions"], 2)
        self.assertIn("沈砚", row["evidence"])
        names = [item["name"] for item in self.store.load_characters()["characters"]]
        self.assertEqual(names.count("沈砚"), 1)
        path = self.store.root / ".ndw" / "prose_cast" / f"{self.chapter_id}.json"
        self.assertTrue(path.exists())

    def test_new_candidate_preview_only(self):
        """模型给出新人物时只进待新建候选，预览阶段不建卡。"""
        self._set_content("周也听见了。周也回头。\n")
        record = self._extract_with(
            {
                "characters": [
                    {
                        "name": "周也",
                        "aliases": ["小周"],
                        "role": "supporting",
                        "gender": "female",
                        "age_range": "18-25",
                        "personality": ["冷静", "冷静", "敏锐"],
                        "background": "",
                        "speech_style": {"tempo": "slow", "pitch": "low", "accent": "", "catchphrases": []},
                        "mentions": 2,
                        "evidence": "周也听见了。",
                    }
                ]
            }
        )
        row = next(item for item in record["characters"] if item["name"] == "周也")
        self.assertEqual(row["status"], "new")
        self.assertIsNone(row["character_id"])
        self.assertEqual(row["draft"]["personality"], ["冷静", "敏锐"])
        self.assertEqual(row["draft"]["role"], "supporting")
        names = [item["name"] for item in self.store.load_characters()["characters"]]
        self.assertNotIn("周也", names)

    def test_apply_creates_card_and_is_idempotent(self):
        """确认后建卡并关联本章，重复应用不产生重复角色。"""
        self._set_content("周也听见了。周也回头。\n")
        self._extract_with(
            {
                "characters": [
                    {
                        "name": "周也",
                        "aliases": ["小周"],
                        "role": "supporting",
                        "gender": "female",
                        "age_range": "18-25",
                        "personality": ["冷静", "敏锐"],
                        "background": "",
                        "speech_style": {"tempo": "slow", "pitch": "low", "accent": "", "catchphrases": []},
                        "mentions": 2,
                        "evidence": "周也听见了。",
                    }
                ]
            }
        )
        first = apply_prose_cast(self.store, self.chapter_id, None)
        self.assertEqual(first["created"], ["周也"])
        rows = [item for item in self.store.load_characters()["characters"] if item.get("name") == "周也"]
        self.assertEqual(len(rows), 1)
        zhou = rows[0]
        self.assertEqual(zhou["role"], "supporting")
        self.assertEqual(zhou["gender"], "female")
        self.assertEqual(zhou["age_range"], "18-25")
        self.assertEqual(zhou["personality"], ["冷静", "敏锐"])
        self.assertEqual(zhou["aliases"], ["小周"])
        self.assertIn("正文抽取于第 1 章", zhou["background"])
        self.assertTrue(zhou["state"]["situation"].startswith("首次出现在第 1 章正文："))
        self.assertEqual(zhou["state"]["as_of_chapter_id"], self.chapter_id)
        record = load_prose_cast(self.store, self.chapter_id)
        self.assertTrue(record["applied"])
        self.assertIn(zhou["id"], record["linked_ids"])
        self.assertTrue(any(item["name"] == "周也" for item in created_in_chapter(self.store, 1)))

        second = apply_prose_cast(self.store, self.chapter_id, None)
        self.assertEqual(second["created"], [])
        names = [item["name"] for item in self.store.load_characters()["characters"]]
        self.assertEqual(names.count("周也"), 1)

    def test_descriptor_is_not_listed_as_new(self):
        """「女人」这类泛称不会成为待新建候选。"""
        self._set_content("女人站在门口。周也回头。\n")
        record = self._extract_with(
            {
                "characters": [
                    {"name": "女人", "mentions": 1, "evidence": "女人站在门口。"},
                    {"name": "周也", "mentions": 1, "evidence": "周也回头。"},
                ]
            }
        )
        names = [item["name"] for item in record["characters"]]
        self.assertNotIn("女人", names)
        self.assertIn("周也", names)

    def test_cache_reuse_by_content_hash(self):
        """正文未改时复用缓存，正文改动后标记 stale。"""
        first = ensure_prose_cast(self.store, self.config, self.chapter_id)
        self.assertEqual(first["source"], "model")
        second = ensure_prose_cast(self.store, self.config, self.chapter_id)
        self.assertEqual(second["source"], "cache")
        self.assertEqual(second["created_at"], first["created_at"])
        self._set_content("老沈看着窗外。\n")
        loaded = load_prose_cast(self.store, self.chapter_id)
        self.assertTrue(loaded["stale"])

    def test_repeat_extract_reuses_cache(self):
        """同一正文连续抽取两次：第二次直接复用缓存，不重新调用模型。"""
        first = extract_prose_cast(self.store, self.config, self.chapter_id)
        second = extract_prose_cast(self.store, self.config, self.chapter_id)
        self.assertEqual(second["source"], "cache")
        self.assertEqual(second["created_at"], first["created_at"])

    def test_scope_falls_back_to_binding_for_scan_fallback(self):
        """降级记录（scan-fallback）不参与范围计算，只返回大纲绑定角色。"""
        binding = {
            "characters": [
                {"id": "chr_a", "name": "甲", "planned": True},
                {"id": "chr_b", "name": "乙", "planned": False},
            ]
        }
        record = {
            "model": "scan-fallback",
            "characters": [{"character_id": "chr_b", "name": "乙", "status": "linked"}],
        }
        ids, source = scope_character_ids(binding, record)
        self.assertEqual(ids, {"chr_a"})
        self.assertEqual(source, "binding")

    def test_load_missing_returns_empty_skeleton(self):
        """没有记录时返回空骨架，避免调用方读不到字段。"""
        record = load_prose_cast(self.store, "chp_000000000999")
        self.assertFalse(record["exists"])
        self.assertTrue(record["stale"])
        self.assertEqual(record["characters"], [])

    def test_http_routes_read_extract_and_apply(self):
        """接口层：读取空记录、抽取关联现有角色、确认应用三条路径可用。"""
        from fastapi.testclient import TestClient

        from novel_drama.interface.http.app import create_app

        client = TestClient(
            create_app(
                project_root=str(self.store.root),
                config_path=str(self._runtime_config()),
            )
        )
        params = {"path": str(self.store.root), "chapter": self.chapter_id}

        empty = client.get("/api/ui/chapters/prose-cast", params=params)
        self.assertEqual(empty.status_code, 200)
        self.assertFalse(empty.json()["exists"])

        missing = client.post("/api/ui/chapters/prose-cast", json={"path": str(self.store.root)})
        self.assertEqual(missing.status_code, 400)
        self.assertIn("chapter", missing.json()["detail"]["message"])

        extracted = client.post("/api/ui/chapters/prose-cast", json=params)
        self.assertEqual(extracted.status_code, 200)
        ids = [row["character_id"] for row in extracted.json()["characters"]]
        self.assertIn(self.shen["id"], ids)

        applied = client.post(
            "/api/ui/chapters/prose-cast/apply",
            json={**params, "selections": []},
        )
        self.assertEqual(applied.status_code, 200)
        self.assertEqual(applied.json()["created"], [])

        no_record = client.post(
            "/api/ui/chapters/prose-cast/apply",
            json={"path": str(self.store.root), "chapter": "chp_000000000999"},
        )
        self.assertEqual(no_record.status_code, 400)


if __name__ == "__main__":
    unittest.main()
