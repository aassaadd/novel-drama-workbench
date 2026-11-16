"""音色参考音回归：试听文案要短，采用候选时要压缩长气口。

背景：克隆模型会模仿参考音里的停顿。抽卡候选若来自一段多逗号长文案，
参考音里会留下 0.5~1.2 秒气口，成品配音也会带着同样的长气口。
"""

from __future__ import annotations

import json
import math
import sys
import tempfile
import unittest
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novel_drama.adapters.persistence import ProjectStore
from novel_drama.infrastructure.config import _FALLBACK
from novel_drama.models import entities
from novel_drama.tts import voice_design


def _write_tone_with_gap(path: Path, gap_s: float = 0.7) -> None:
    """写一段「1.1s 音 + 0.7s 静音 + 1.1s 音」的测试音频。

    参数:
        path: 输出 wav 路径。
        gap_s: 中间静音时长（秒）。
    """
    rate = 24000
    amplitude = 8000
    frames: list[bytes] = []
    for index in range(int(rate * 1.1)):
        value = int(amplitude * math.sin(2 * math.pi * 220 * index / rate))
        frames.append(value.to_bytes(2, "little", signed=True))
    frames.extend(b"\x00\x00" for _ in range(int(rate * gap_s)))
    for index in range(int(rate * 1.1)):
        value = int(amplitude * math.sin(2 * math.pi * 220 * index / rate))
        frames.append(value.to_bytes(2, "little", signed=True))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"".join(frames))


class ReferenceAudioTest(unittest.TestCase):
    """默认文案与参考音规整。"""

    def test_default_design_script_is_short(self):
        """默认试听文案应为一句短句，避免参考音天然带长停顿。"""
        script = _FALLBACK["voice"]["design"]["script"]
        self.assertLessEqual(len(script), 16, script)
        self.assertNotIn("，", script)
        self.assertNotIn(",", script)

    def test_normalize_compresses_long_gap(self):
        """规整后内部 0.7s 停顿应明显变短，且不会把参考音压得过短。"""
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "cand.wav"
            target = Path(tmp) / "ref.wav"
            _write_tone_with_gap(source)
            result = voice_design._normalize_reference_audio(source, target)
            self.assertTrue(result["normalized"], result)
            self.assertIn("长气口", result["note"])
            self.assertLess(result["duration_ms"], 2700)
            self.assertGreater(result["duration_ms"], 1200)

    def test_select_candidate_normalizes_and_records_text(self):
        """采用候选时写入规整后的参考音，并记录试听文案。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ProjectStore(str(root)).create("参考音回归", author="ndw")
            store = ProjectStore(str(root))
            batch_id = entities.new_id("vdb")
            target = voice_design.batch_dir(store, batch_id)
            target.mkdir(parents=True, exist_ok=True)
            _write_tone_with_gap(target / "cand-01.wav")
            (target / "batch.json").write_text(
                json.dumps(
                    {
                        "batch_id": batch_id,
                        "status": "ready",
                        "done": 1,
                        "total": 1,
                        "character_id": None,
                        "character_name": "旁白",
                        "prompt": "A calm narrator voice.",
                        "script": "我一直在等你回来。",
                        "candidates": [
                            {"index": 1, "file": "cand-01.wav", "seed": 7, "duration_ms": 2900}
                        ],
                        "failures": [],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            result = voice_design.select_candidate(
                store,
                batch_id,
                1,
                "旁白·设计音色",
                "male",
                "manual",
                False,
                record_undo=False,
            )
            variant = next(
                item
                for item in result["voice"]["variants"]
                if item["id"] == result["variant_id"]
            )
            reference = variant["reference"]
            self.assertEqual(reference["text"], "我一直在等你回来。")
            self.assertIn("长气口", reference["denoise"]["note"])
            self.assertLess(reference["duration_ms"], 2700)


if __name__ == "__main__":
    unittest.main()
