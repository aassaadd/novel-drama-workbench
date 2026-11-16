"""音色设计预览接口回归：必须能正常构造写作适配器，不能因导入路径写错报 500。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fastapi.testclient import TestClient

from novel_drama.adapters.llm.contracts import TextGenResult
from novel_drama.adapters.persistence import ProjectStore
from novel_drama.interface.http.app import create_app
from novel_drama.models import entities


class _FakeAdapter:
    """翻译测试用的假写作适配器。"""

    def generate(self, req):
        """固定返回一条英文音色描述。"""
        return TextGenResult(
            text="A low, slightly hoarse middle-aged male voice, slow pace, weary but restrained.",
            finish_reason="stop",
        )


class VoiceDesignPreviewTest(unittest.TestCase):
    """`POST /api/ui/voices/design/preview` 的可用性。"""

    def setUp(self):
        """建临时工程、一个角色与离线配置。"""
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        store = ProjectStore(str(self.root))
        store.create("音色设计回归", author="ndw")
        self.store = ProjectStore(str(self.root))
        table = self.store.load_characters()
        character = entities.new_character("沈砚", role="protagonist")
        character["gender"] = "male"
        table["characters"].append(character)
        self.store.save_characters(table)
        self.character_id = character["id"]
        self.config_path = self.root / "offline.yaml"
        self.config_path.write_text(
            "\n".join(
                [
                    "adapters:",
                    "  writing: offline",
                    "  sfx: offline",
                    "voice:",
                    "  design:",
                    "    enabled: true",
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

    def test_preview_returns_profile_instead_of_500(self):
        """走模型描述分支时接口应返回 200，失败也只能回退规则模板。"""
        client = TestClient(
            create_app(project_root=str(self.root), config_path=str(self.config_path))
        )
        response = client.post(
            "/api/ui/voices/design/preview",
            json={"path": str(self.root), "character_id": self.character_id},
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertIn("prompt", body)
        self.assertTrue(body["prompt"])
        self.assertIn(body["source"], ("llm", "rule"))
        # 中英双语：中文描述与英文抽卡描述都要有
        self.assertTrue(body["prompt_zh"])
        self.assertTrue(body["profile"]["design_prompt_zh"])

    def test_translate_returns_english_prompt(self):
        """中文描述走 LLM 翻译接口，返回英文抽卡描述。"""
        client = TestClient(
            create_app(project_root=str(self.root), config_path=str(self.config_path))
        )
        with patch(
            "novel_drama.application.support.llm_calls.build_writing_adapter",
            return_value=_FakeAdapter(),
        ):
            response = client.post(
                "/api/ui/voices/design/translate",
                json={"path": str(self.root), "text": "中年男性，低沉沙哑，语速偏慢，带一点疲惫感"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["source"], "llm")
        self.assertTrue(body["english"])
        self.assertIn("voice", body["english"].lower())


if __name__ == "__main__":
    unittest.main()
