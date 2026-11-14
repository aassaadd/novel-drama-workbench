"""配音段落级缓存与复用计划测试。

覆盖：
- 段落签名对文本 / 说话人 / 音色 / 合成参数 / 参考音频 / 目标时长 / 对齐配置的敏感性；
- 缓存写入、命中、产物被删后失效、provider 回落后的键替换；
- 批量重跑时「未变行复用、改文本行重合成」，以及复用计划给出的 keep / regenerate 与变更原因。

运行：`.venv/bin/python -m unittest tests.test_segment_cache -v`。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novel_drama.tts import cache, line_state, reuse, sidecar  # noqa: E402
from novel_drama.tts.batch import plan_batch, run_batch  # noqa: E402


def base_params(**overrides) -> dict:
    """构造一份完整的合成参数，可按字段覆盖。

    参数:
        **overrides: 要覆盖的字段。

    返回:
        参数字典。
    """
    params = {
        "text": "第一句台词",
        "speaker_id": "chr_1",
        "emotion": "neutral",
        "emotion_intensity": 0.5,
        "delivery_note": "",
        "speed": 1.0,
        "target_duration_ms": None,
        "voice_id": "voc_a",
        "variant_id": "vav_a1",
        "instruct": "",
        "language": "zh-CN",
        "provider_id": "fake",
        "model_version": "m1",
        "chunk_limit": 60,
        "chunk_silence_ms": 80,
        "edge_trim_ms": 30,
        "edge_trim_max_ms": 120,
        "keep_min_ms": 60,
        "silence_threshold_dbfs": -45.0,
        "hard_split_ratio": 0.5,
    }
    params.update(overrides)
    return params


class SignatureTest(unittest.TestCase):
    """段落签名与变更说明。"""

    def signature(self, **overrides) -> dict:
        """按覆盖后的参数生成签名。"""
        return cache.build_signature(
            base_params(**overrides),
            ref_audio=None,
            ref_text="参考",
            target_duration_ms=overrides.get("target_duration_ms"),
            alignment={"enabled": True, "max_error_ms": 250, "max_speed_change": 0.05},
        )

    def test_stable_for_same_params(self):
        """同参数必须得到同一个键。"""
        self.assertEqual(cache.signature_key(self.signature()), cache.signature_key(self.signature()))

    def test_sensitive_fields(self):
        """文本、说话人、音色、合成参数变化都必须改变缓存键。"""
        baseline = cache.signature_key(self.signature())
        for overrides in (
            {"text": "第一句台词改了"},
            {"speaker_id": "chr_2"},
            {"voice_id": "voc_b"},
            {"variant_id": "vav_a2"},
            {"emotion": "happy"},
            {"emotion_intensity": 0.9},
            {"speed": 1.1},
            {"target_duration_ms": 3000},
            {"provider_id": "fake2"},
            {"model_version": "m2"},
            {"chunk_limit": 40},
            {"chunk_silence_ms": 120},
            {"edge_trim_ms": 0},
            {"hard_split_ratio": 0.8},
        ):
            with self.subTest(overrides=overrides):
                self.assertNotEqual(baseline, cache.signature_key(self.signature(**overrides)))

    def test_ref_audio_content_changes_key(self):
        """参考音频内容变化（即使路径不变）必须改变缓存键。"""
        with tempfile.TemporaryDirectory() as tmp:
            ref = Path(tmp) / "ref.wav"
            ref.write_bytes(b"aaaa")
            first = cache.build_signature(base_params(), ref_audio=str(ref))
            ref.write_bytes(b"bbbb")
            cache._REF_HASH_CACHE.clear()
            second = cache.build_signature(base_params(), ref_audio=str(ref))
            self.assertNotEqual(cache.signature_key(first), cache.signature_key(second))

    def test_alignment_changes_key(self):
        """对齐配置影响成片，必须进键。"""
        params = base_params()
        loose = cache.build_signature(params, alignment={"enabled": True, "max_speed_change": 0.05})
        tight = cache.build_signature(params, alignment={"enabled": True, "max_speed_change": 0.02})
        self.assertNotEqual(cache.signature_key(loose), cache.signature_key(tight))

    def test_describe_changes(self):
        """变更说明要能读出文本增删与强度变化。"""
        before = self.signature()
        after = self.signature(text="第一句台词", emotion_intensity=0.9)
        changes = cache.describe_changes(before, after)
        fields = {item["field"]: item for item in changes}
        self.assertIn("emotion_intensity", fields)
        self.assertIn("0.5", fields["emotion_intensity"]["text"])
        self.assertIn("0.9", fields["emotion_intensity"]["text"])
        text_changes = cache.describe_changes(before, self.signature(text="第一句台词多两个字"))
        self.assertTrue(any(item["field"] == "text" for item in text_changes))

    def test_with_provider(self):
        """provider / 模型版本替换后键必须变化。"""
        signature = self.signature()
        switched = cache.with_provider(signature, "backup", "m9")
        self.assertNotEqual(cache.signature_key(signature), cache.signature_key(switched))


class CacheStoreTest(unittest.TestCase):
    """缓存落盘、命中与产物缺失。"""

    def test_put_lookup_and_missing_asset(self):
        """写入后可命中；音频被删后视为未命中。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio = root / "assets" / "voice" / "lines" / "lin_a_r1.wav"
            audio.parent.mkdir(parents=True)
            audio.write_bytes(b"RIFF")
            signature = cache.build_signature(base_params())
            key = cache.signature_key(signature)
            cache.put_many(
                root,
                {
                    key: {
                        "line_id": "lin_a",
                        "chapter_id": "chp_1",
                        "signature": signature,
                        "asset_id": "assets/voice/lines/lin_a_r1.wav",
                        "duration_ms": 1200,
                        "provider_id": "fake",
                    }
                },
            )
            table = cache.load(root)
            self.assertIsNotNone(cache.lookup(root, key, table))
            self.assertEqual(cache.entry_for_line(table, "lin_a")["duration_ms"], 1200)
            audio.unlink()
            self.assertIsNone(cache.lookup(root, key, table))

    def test_legacy_schema_ignored(self):
        """旧 schema 的缓存按空处理。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = cache.cache_path(root)
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"schema_version": 1, "entries": {"k": {}}, "lines": {}}), encoding="utf-8")
            table = cache.load(root)
            self.assertEqual(table["entries"], {})
            self.assertEqual(table["lines"], {})


class _Store:
    """最小工程替身：只提供配音链路会用到的方法。"""

    def __init__(self, root: Path):
        self.root = root

    def settings(self) -> dict:
        """工程设置为空。"""
        return {}

    def list_chapters(self) -> list[dict]:
        """没有章节索引需求。"""
        return []

    def load_characters(self) -> dict:
        """单角色表。"""
        return {"characters": [{"id": "chr_1", "name": "甲", "voice_binding": {}}]}

    def load_voices(self) -> dict:
        """空音色库，走 service.default_voice 兜底。"""
        return {"voices": []}

    def save_chapter(self, chapter: dict) -> None:
        """替身不落盘。"""
        return None


class _FakeService:
    """最小合成替身：写一个占位音频与侧车，并统计真实合成次数。"""

    def __init__(self, ref_audio: str):
        self.default_voice = SimpleNamespace(
            variant_id="vav_default", ref_audio=ref_audio, ref_text="参考文本", source="preset"
        )
        self.providers = {
            "fake": SimpleNamespace(spec=SimpleNamespace(model_version="m1")),
            "fake2": SimpleNamespace(spec=SimpleNamespace(model_version="m2")),
        }
        self.chunk_limit = 60
        self.chunk_silence_ms = 80
        self.edge_trim_ms = 30
        self.edge_trim_max_ms = 120
        self.keep_min_ms = 60
        self.silence_threshold_dbfs = -45.0
        self.hard_split_ratio = 0.5
        self.alignment_cfg = None
        self.synth_calls = 0

    def decide(self, requirement, line_provider, character_provider) -> dict:
        """固定选 fake；行级锁定优先。"""
        provider = line_provider or character_provider or "fake"
        return {"selected": {"provider_id": provider}}

    def synthesize(
        self,
        text,
        out_path,
        requirement=None,
        voice=None,
        instruct=None,
        line_provider_id=None,
        character_provider_id=None,
        params_extra=None,
    ) -> dict:
        """写占位产物与侧车，返回最小 payload。"""
        self.synth_calls += 1
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"RIFF")
        provider = line_provider_id or character_provider_id or "fake"
        params = dict(params_extra or {})
        params.update(
            {
                "provider_id": provider,
                "model_version": "m1" if provider == "fake" else "m2",
                "chunk_limit": self.chunk_limit,
                "chunk_silence_ms": self.chunk_silence_ms,
                "edge_trim_ms": self.edge_trim_ms,
                "edge_trim_max_ms": self.edge_trim_max_ms,
                "keep_min_ms": self.keep_min_ms,
                "silence_threshold_dbfs": self.silence_threshold_dbfs,
                "hard_split_ratio": self.hard_split_ratio,
            }
        )
        sidecar.dump(
            out_path,
            {
                "text": text,
                "provider_id": provider,
                "model_version": params["model_version"],
                "variant_id": (voice.variant_id if voice else None),
                "duration_ms": 1000,
                "params": params,
            },
        )
        return {
            "provider_id": provider,
            "model_version": params["model_version"],
            "duration_ms": 1000,
            "chunks": 1,
            "chunk_limit": self.chunk_limit,
            "routing": {},
            "latency_ms": 10,
        }


class BatchReuseTest(unittest.TestCase):
    """批量合成的复用行为。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "assets" / "voice" / "lines").mkdir(parents=True)
        self.ref = self.root / "ref.wav"
        self.ref.write_bytes(b"ref")
        self.store = _Store(self.root)
        self.service = _FakeService(str(self.ref))
        self.line_a = {
            "id": "lin_a",
            "revision": 1,
            "index": 1,
            "text": "第一句台词",
            "speaker_id": "chr_1",
            "emotion": "neutral",
            "emotion_intensity": 0.5,
            "speed": 1.0,
        }
        self.line_b = {
            "id": "lin_b",
            "revision": 1,
            "index": 2,
            "text": "第二句台词",
            "speaker_id": "chr_1",
            "emotion": "neutral",
            "emotion_intensity": 0.5,
            "speed": 1.0,
        }
        self.chapter = {
            "id": "chp_1",
            "title": "测试章",
            "scenes": [{"id": "scn_1", "index": 1, "lines": [self.line_a, self.line_b]}],
        }

    def tearDown(self):
        self.tmp.cleanup()

    def rows(self, include_ready=False) -> list[dict]:
        """按当前章节状态规划本次要处理的行。"""
        characters = self.store.load_characters()["characters"]
        snapshots = line_state.load_snapshots(self.root, self.chapter)
        return plan_batch(
            self.chapter,
            characters,
            include_ready=include_ready,
            snapshots=snapshots,
            root=self.root,
            engine={},
        )

    def test_second_run_reuses_unchanged_lines(self):
        """首轮全部真实合成，二次运行全部命中缓存。"""
        first = run_batch(self.service, self.store, self.chapter, self.rows(), batch_size=5)
        self.assertEqual(first["stats"]["regenerated"], 2)
        self.assertEqual(self.service.synth_calls, 2)
        self.assertTrue((self.root / "assets/voice/lines/lin_a_r1.wav").exists())

        second = run_batch(
            self.service,
            self.store,
            self.chapter,
            self.rows(include_ready=True),
            batch_size=5,
            avg_ms_per_line=800,
        )
        self.assertEqual(second["stats"]["reused"], 2)
        self.assertEqual(second["stats"]["regenerated"], 0)
        self.assertEqual(second["stats"]["saved_ms_estimate"], 1600)
        self.assertEqual(self.service.synth_calls, 2)
        self.assertTrue(all(row["decision"] == "reuse" for row in second["rows"]))

    def test_text_change_only_resynths_that_line(self):
        """改一句文本后，只重合成这一行，另一行保持不动。"""
        run_batch(self.service, self.store, self.chapter, self.rows(), batch_size=5)
        self.line_a["text"] = "第一句台词改过了"
        self.line_a["revision"] = 2
        rows = self.rows()
        self.assertEqual([row["line"]["id"] for row in rows], ["lin_a"])
        result = run_batch(self.service, self.store, self.chapter, rows, batch_size=5)
        self.assertEqual(result["stats"]["regenerated"], 1)
        self.assertEqual(result["stats"]["reused"], 0)
        self.assertEqual(self.service.synth_calls, 3)
        self.assertTrue(any(change["field"] == "text" for change in result["rows"][0]["changes"]))

        # 未变行仍是老的 r1，改文本行走的是新的 r2
        self.assertTrue((self.root / "assets/voice/lines/lin_a_r2.wav").exists())
        self.assertTrue((self.root / "assets/voice/lines/lin_b_r1.wav").exists())

    def test_provider_change_invalidates_cache(self):
        """参数里的 provider 变化必须让缓存失效，而不是错误复用。"""
        run_batch(self.service, self.store, self.chapter, self.rows(), batch_size=5)
        self.line_b["voice"] = {"provider_id": "fake2"}
        result = run_batch(
            self.service,
            self.store,
            self.chapter,
            self.rows(include_ready=True),
            batch_size=5,
        )
        row_b = next(row for row in result["rows"] if row["line_id"] == "lin_b")
        self.assertEqual(row_b["decision"], "synth")
        self.assertEqual(row_b["provider_id"], "fake2")

    def test_voice_ref_change_invalidates_ready_lines(self):
        """音色参考音频变了，即使行状态 ready 也要重新纳入并重合成。"""
        run_batch(self.service, self.store, self.chapter, self.rows(), batch_size=5)
        self.ref.write_bytes(b"ref-changed")
        cache._REF_HASH_CACHE.clear()
        characters_table = self.store.load_characters()
        checker = reuse.changed_checker(self.service, self.store, characters_table, self.chapter)
        snapshots = line_state.load_snapshots(self.root, self.chapter)
        rows = plan_batch(
            self.chapter,
            characters_table["characters"],
            snapshots=snapshots,
            root=self.root,
            engine={},
            changed_checker=checker,
        )
        self.assertEqual(len(rows), 2)
        result = run_batch(self.service, self.store, self.chapter, rows, batch_size=5)
        self.assertEqual(result["stats"]["regenerated"], 2)
        self.assertEqual(self.service.synth_calls, 4)

    def test_provider_change_replans_ready_line(self):
        """行状态 ready 但换了 provider，也要被重新纳入并重合成。"""
        run_batch(self.service, self.store, self.chapter, self.rows(), batch_size=5)
        self.line_b["voice"] = {"provider_id": "fake2"}
        characters_table = self.store.load_characters()
        checker = reuse.changed_checker(self.service, self.store, characters_table, self.chapter)
        snapshots = line_state.load_snapshots(self.root, self.chapter)
        rows = plan_batch(
            self.chapter,
            characters_table["characters"],
            snapshots=snapshots,
            root=self.root,
            engine={},
            changed_checker=checker,
        )
        self.assertEqual([row["line"]["id"] for row in rows], ["lin_b"])
        result = run_batch(self.service, self.store, self.chapter, rows, batch_size=5)
        self.assertEqual(result["stats"]["regenerated"], 1)
        self.assertEqual(result["rows"][0]["provider_id"], "fake2")

    def test_plan_reuse_marks_keep_and_regenerate(self):
        """复用计划：就绪行记沿用，改文本行记重合成并给原因。"""
        run_batch(self.service, self.store, self.chapter, self.rows(), batch_size=5)
        self.line_a["text"] = "第一句台词改过了"
        self.line_a["revision"] = 2
        rows = self.rows()
        characters_table = self.store.load_characters()
        snapshots = line_state.load_snapshots(self.root, self.chapter)
        states = line_state.chapter_states(
            self.chapter,
            {item["id"]: item for item in characters_table["characters"]},
            snapshots,
            root=self.root,
            engine={},
        )
        plan = reuse.plan_reuse(
            self.service,
            self.store,
            self.chapter,
            rows,
            characters_table=characters_table,
            snapshots=snapshots,
            engine={},
            states=states,
            avg_ms_per_line=700,
        )
        self.assertEqual(plan["keep"], 1)
        self.assertEqual(plan["regenerate"], 1)
        self.assertEqual(plan["reuse"], 0)
        decisions = {item["line_id"]: item for item in plan["items"]}
        self.assertEqual(decisions["lin_b"]["decision"], "keep")
        self.assertEqual(decisions["lin_a"]["decision"], "synth")
        self.assertTrue(any(change["field"] == "text" for change in decisions["lin_a"]["changes"]))


if __name__ == "__main__":
    unittest.main()
