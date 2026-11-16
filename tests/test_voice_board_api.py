"""音色面板数据：角色音色描述需要随人物返回，供音色设计弹窗默认带入。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fastapi.testclient import TestClient

from novel_drama.adapters.persistence import ProjectStore
from novel_drama.interface.http.app import create_app
from novel_drama.models import entities


class VoiceBoardDescriptionTest(unittest.TestCase):
    """`GET /api/ui/voices` 的人物字段。"""

    def setUp(self):
        """建临时工程与一个带音色描述的角色。"""
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        ProjectStore(str(self.root)).create("音色描述回归", author="ndw")
        store = ProjectStore(str(self.root))
        characters = store.load_characters()
        character = entities.new_character("沈砚", role="protagonist")
        character["voice_description_zh"] = "三十岁男性，低沉，语速偏慢"
        character["voice_description_en"] = "A low, slow male voice"
        character["voice_profile"] = {
            "design_prompt_zh": "三十岁男性，低沉，语速偏慢",
            "design_prompt": "A low, slow male voice",
        }
        characters["characters"].append(character)
        store.save_characters(characters)
        self.character_id = character["id"]

        config_path = self.root / "offline.yaml"
        config_path.write_text(
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
        self.client = TestClient(
            create_app(project_root=str(self.root), config_path=str(config_path))
        )

    def tearDown(self):
        """清理临时工程。"""
        self.tmp.cleanup()

    def test_board_returns_character_voice_description(self):
        """面板人物数据带中英描述与 voice_profile。"""
        response = self.client.get("/api/ui/voices", params={"path": str(self.root)})
        self.assertEqual(response.status_code, 200, response.text)
        character = next(
            item for item in response.json()["characters"] if item["id"] == self.character_id
        )
        self.assertEqual(character["voice_description_zh"], "三十岁男性，低沉，语速偏慢")
        self.assertEqual(character["voice_description_en"], "A low, slow male voice")
        self.assertEqual(
            character["voice_profile"]["design_prompt"], "A low, slow male voice"
        )


if __name__ == "__main__":
    unittest.main()
