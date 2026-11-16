"""音色抽卡种子回归：每次重新抽卡必须换随机种子，不能重复上一轮结果。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novel_drama.adapters.persistence import ProjectStore
from novel_drama.tts import voice_design


class _Cfg:
    """最小配置桩：支持 `voice.design.*` 点号读取。"""

    def __init__(self, data: dict):
        """保存配置字典。"""
        self.data = data

    def get(self, key: str, default=None):
        """按点号路径读取配置。"""
        current = self.data
        for part in key.split("."):
            if not isinstance(current, dict) or part not in current:
                return default
            current = current[part]
        return current


class VoiceDesignSeedTest(unittest.TestCase):
    """抽卡批次的随机种子。"""

    def test_each_batch_uses_new_seed_base(self):
        """连续两次抽卡应得到不同的 seed_base，避免候选完全重复。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ProjectStore(str(root)).create("抽卡种子回归", author="ndw")
            store = ProjectStore(str(root))
            cfg = _Cfg({"voice": {"design": {"enabled": True, "max_keep_days": 7}}})
            character = {"id": "char_seed", "name": "旁白"}
            with mock.patch.object(voice_design, "_run_batch"):
                first = voice_design.create_batch(store, cfg, character, "A calm voice.", 4)
                second = voice_design.create_batch(store, cfg, character, "A calm voice.", 4)
            self.assertNotEqual(first["seed_base"], second["seed_base"])
            self.assertGreaterEqual(first["seed_base"], 1)
            self.assertLess(first["seed_base"], 2**31)


if __name__ == "__main__":
    unittest.main()
