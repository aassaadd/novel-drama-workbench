"""音色池删除回归：删除音色后从池中移除，并自动解绑正在使用它的角色。"""

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
from novel_drama.models.ids import now_iso


class VoiceDeleteTest(unittest.TestCase):
    """`POST /api/ui/voices/delete` 的删除与解绑。"""

    def setUp(self):
        """建临时工程、一个音色和一个已绑定该音色的角色。"""
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        ProjectStore(str(self.root)).create("音色删除回归", author="ndw")
        self.store = ProjectStore(str(self.root))
        self.voice_id = entities.new_id("voc")
        self.variant_id = entities.new_id("vav")
        table = self.store.load_voices()
        table["voices"].append(
            {
                "id": self.voice_id,
                "name": "待删除音色",
                "gender": "female",
                "tags": ["测试"],
                "default_variant_id": self.variant_id,
                "variants": [
                    {
                        "id": self.variant_id,
                        "name": "默认",
                        "source": "designed",
                        "emotion_tags": ["default"],
                        "status": "ready",
                        "reference": {"audio_path": "assets/voice/ref/test.wav", "duration_ms": 3000},
                        "consent": {"confirmed": True, "note": "测试音色"},
                        "created_at": now_iso(),
                    }
                ],
                "status": "ready",
                "revision": 1,
                "created_at": now_iso(),
                "updated_at": now_iso(),
            }
        )
        self.store.save_voices(table)

        characters = self.store.load_characters()
        character = entities.new_character("沈砚", role="protagonist")
        character["voice_binding"] = {"adapter": "auto", "voice_library_id": self.voice_id}
        characters["characters"].append(character)
        self.store.save_characters(characters)
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

    def test_delete_removes_voice_and_unbinds_character(self):
        """删除后音色不在池中，绑定它的角色自动解绑，参考音频文件一并删除。"""
        ref = self.root / "assets" / "voice" / "ref" / "test.wav"
        ref.parent.mkdir(parents=True, exist_ok=True)
        ref.write_bytes(b"RIFF")
        response = self.client.post(
            "/api/ui/voices/delete",
            json={"path": str(self.root), "voice_id": self.voice_id},
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertNotIn(self.voice_id, [item["id"] for item in body["voices"]])
        character = next(
            item for item in body["characters"] if item["id"] == self.character_id
        )
        self.assertIsNone(character.get("voice_binding"))
        self.assertFalse(ref.exists(), "参考音频应随音色删除")
        self.assertIn("assets/voice/ref/test.wav", body["deleted_files"])

    def test_delete_keeps_reference_shared_by_other_voice(self):
        """参考音频仍被其它音色引用时不能误删。"""
        import copy

        ref = self.root / "assets" / "voice" / "ref" / "test.wav"
        ref.parent.mkdir(parents=True, exist_ok=True)
        ref.write_bytes(b"RIFF")
        table = self.store.load_voices()
        shared = copy.deepcopy(next(item for item in table["voices"] if item["id"] == self.voice_id))
        shared["id"] = entities.new_id("voc")
        shared["name"] = "共享参考音色"
        table["voices"].append(shared)
        self.store.save_voices(table)

        response = self.client.post(
            "/api/ui/voices/delete",
            json={"path": str(self.root), "voice_id": self.voice_id},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(ref.exists(), "仍有音色引用该参考音频，不应删除")
        self.assertEqual(response.json()["deleted_files"], [])

    def test_delete_missing_voice_returns_404(self):
        """重复删除返回 404，页面可提示「音色不存在」。"""
        payload = {"path": str(self.root), "voice_id": self.voice_id}
        self.assertEqual(self.client.post("/api/ui/voices/delete", json=payload).status_code, 200)
        response = self.client.post("/api/ui/voices/delete", json=payload)
        self.assertEqual(response.status_code, 404, response.text)
        self.assertIn("音色不存在", response.text)


if __name__ == "__main__":
    unittest.main()
