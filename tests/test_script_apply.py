"""导演脚本标注：`@shot` 行解析与「写入本集」回归。

覆盖两个线上问题：
1. `@shot` 的 `location/time/mood` 不得混进镜头名，中文 mood 要归一到枚举；
2. 写入接口遇到模型产出的中文 mood 不能 500，且标题/情绪要正确落到场景。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fastapi.testclient import TestClient

from novel_drama.adapters.persistence import ProjectStore
from novel_drama.application.production.lines_source import chapter_view
from novel_drama.director.markup import parse_markup
from novel_drama.interface.http.app import create_app
from novel_drama.models import entities

_MARKUP = (
    "@shot 1 车内颠簸 location=碎石路 time=night mood=压抑\n"
    "旁白：林九斤觉得这趟远门走得不对劲。\n"
    "!bgm mood=tense energy=0.4 intensity=0.6 loop=1 name=荒路 "
    "function=铺垫不安 en=low+drone+wind+sparse+percussion+dark+minor\n"
)


class ScriptApplyTest(unittest.TestCase):
    """标注解析与写入接口的行为约束。"""

    def setUp(self):
        """建临时工程、一章正文与一个旁白场景。"""
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        store = ProjectStore(str(self.root))
        store.create("标注写入回归", author="ndw")
        self.store = ProjectStore(str(self.root))
        chapter = entities.new_chapter(
            self.store.load_project()["work"]["id"],
            1,
            "第一章",
            content="林九斤觉得这趟远门走得不对劲。\n",
        )
        scene = entities.new_scene(chapter["id"], 0, "场景 1")
        scene["lines"] = [
            entities.new_line(
                scene["id"],
                0,
                "林九斤觉得这趟远门走得不对劲。",
                kind="narration",
                speaker_id=entities.NARRATOR_ID,
            )
        ]
        chapter["scenes"] = [scene]
        self.store.save_chapter(chapter)
        self.chapter_id = chapter["id"]
        self.config_path = self.root / "offline.yaml"
        self.config_path.write_text(
            "\n".join(
                [
                    "adapters:",
                    "  writing: offline",
                    "  sfx: offline",
                    "gpu:",
                    f"  lock_path: {self.root / 'gpu.lock'}",
                    "tts:",
                    f"  hf_home: {self.root / 'hf'}",
                    "  builtin_voices:",
                    f"    root: {self.root / 'voices'}",
                    "",
                ]
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        """清理临时工程。"""
        self.tmp.cleanup()

    def _view(self) -> dict:
        """读取章节的台本派生视图。"""
        return chapter_view(self.store, self.store.load_chapter(self.chapter_id))

    def test_shot_label_excludes_extras_and_normalizes_mood(self):
        """镜头名只取名称部分；中文 mood 收敛为枚举值。"""
        script = parse_markup(
            _MARKUP,
            self._view(),
            self.store.load_characters()["characters"],
            self.store.settings(),
        )
        shot = script["shots"][0]
        self.assertEqual(shot["scene_label"], "车内颠簸")
        self.assertEqual(shot["location"], "碎石路")
        self.assertEqual(shot["mood"], "tense")
        self.assertEqual(shot["bgm"]["prompt_en"], "low drone wind sparse percussion dark minor")

    def test_apply_endpoint_accepts_chinese_mood(self):
        """写入接口不应因中文 mood 报 500，且场景标题与情绪正确。"""
        client = TestClient(
            create_app(project_root=str(self.root), config_path=str(self.config_path))
        )
        response = client.post(
            "/api/ui/script/apply",
            json={"path": str(self.root), "chapter": self.chapter_id, "markup": _MARKUP},
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["shots"], 1)
        self.assertEqual(body["lines"], 1)

        scene = self._view()["scenes"][0]
        self.assertEqual(scene["title"], "车内颠簸")
        self.assertEqual(scene["mood"], "tense")
        self.assertEqual(scene["bgm"]["prompt_en"], "low drone wind sparse percussion dark minor")
        self.assertTrue((self.root / "scripts" / f"{self.chapter_id}.markup.txt").exists())


if __name__ == "__main__":
    unittest.main()
