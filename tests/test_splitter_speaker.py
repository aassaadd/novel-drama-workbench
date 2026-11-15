"""说话人归属 v3 单元测试：三位置 cue、段内合并、轮次先验、嵌套引号与龙套回退。

运行：`.venv/bin/python -m unittest tests.test_splitter_speaker -v`
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novel_drama.adapters.persistence.voice_cast import unresolved_speaker_lines  # noqa: E402
from novel_drama.models.entities import UNKNOWN_SPEAKER_ID  # noqa: E402
from novel_drama.orchestration.resplit import merge_manual_lines  # noqa: E402
from novel_drama.text.splitter import _scene_candidates, split_lines  # noqa: E402

CHARS = [
    {"id": "chr_1", "name": "林九斤", "aliases": ["九斤"], "role": "lead", "gender": "female"},
    {"id": "chr_2", "name": "周也", "aliases": [], "role": "lead", "gender": "male"},
    {"id": "chr_3", "name": "苏梅", "aliases": [], "role": "support", "gender": "female"},
    {"id": "chr_4", "name": "陈默", "aliases": [], "role": "support", "gender": "male"},
]
NAME = {character["id"]: character["name"] for character in CHARS}


def dialogue_lines(text: str, characters: list[dict] | None = None) -> list[dict]:
    """只取对白/独白行，忽略旁白。"""
    return [line for line in split_lines(text, characters or CHARS) if line["kind"] != "narration"]


class PostCueTest(unittest.TestCase):
    """引号后置 cue（「……」X 说）。"""

    def test_name_after_quote(self) -> None:
        lines = dialogue_lines("“我知道了。”九斤说。")
        self.assertEqual(len(lines), 1)
        self.assertEqual(NAME[lines[0]["speaker_id"]], "林九斤")
        self.assertGreaterEqual(lines[0]["confidence"], 0.9)

    def test_name_after_quote_with_narration(self) -> None:
        lines = dialogue_lines("“信号没了。”周也坐在副驾驶，手机屏幕亮着。")
        self.assertEqual(len(lines), 1)
        self.assertEqual(NAME[lines[0]["speaker_id"]], "周也")

    def test_second_quote_in_same_paragraph_follows_first(self) -> None:
        lines = dialogue_lines("“信号没了。”周也坐在副驾驶，手机屏幕亮着。“从进山开始就没了。”")
        self.assertEqual([NAME[line["speaker_id"]] for line in lines], ["周也", "周也"])


class PreCueTest(unittest.TestCase):
    """引号前置 cue（X 说：「……」）。"""

    def test_name_before_quote(self) -> None:
        lines = dialogue_lines("周也开口：“一拜天地。”")
        self.assertEqual(len(lines), 1)
        self.assertEqual(NAME[lines[0]["speaker_id"]], "周也")
        self.assertGreaterEqual(lines[0]["confidence"], 0.9)

    def test_alias_before_quote(self) -> None:
        lines = dialogue_lines("九斤说：“走吧。”")
        self.assertEqual(NAME[lines[0]["speaker_id"]], "林九斤")


class MidCueTest(unittest.TestCase):
    """引号中 cue（「……」X 说，「……」）。"""

    def test_mid_cue_applies_to_both_quotes(self) -> None:
        lines = dialogue_lines("“先走。”九斤说，“我随后就来。”")
        self.assertEqual([NAME[line["speaker_id"]] for line in lines], ["林九斤", "林九斤"])
        self.assertTrue(all(line["confidence"] >= 0.9 for line in lines))


class PronounCueTest(unittest.TestCase):
    """代词 cue 按性别解析。"""

    def test_he_said(self) -> None:
        lines = dialogue_lines("九斤把手机放下。周也看着她。“怎么说？”他问。")
        self.assertEqual(NAME[lines[0]["speaker_id"]], "周也")

    def test_gender_mismatch_returns_unknown(self) -> None:
        lines = dialogue_lines("九斤把手机放下。“怎么说？”他说。")
        self.assertEqual(lines[0]["speaker_id"], UNKNOWN_SPEAKER_ID)


class RotationTest(unittest.TestCase):
    """无标记连续台词在最近两个说话人之间交替。"""

    def test_alternates_between_two_speakers(self) -> None:
        text = "九斤站在门口。\n“左边。”周也说。\n“右边。”\n“中间。”\n九斤没动。"
        lines = dialogue_lines(text)
        self.assertEqual([NAME[line["speaker_id"]] for line in lines], ["周也", "林九斤", "周也"])
        self.assertAlmostEqual(lines[1]["confidence"], 0.45)

    def test_narration_breaks_rotation(self) -> None:
        text = "九斤站在门口。\n“左边。”周也说。\n九斤低头看地。\n“右边。”"
        lines = dialogue_lines(text)
        self.assertEqual(lines[-1]["speaker_id"], UNKNOWN_SPEAKER_ID)


class NestedQuoteTest(unittest.TestCase):
    """嵌套引号并入外层，不拆成两个说话人。"""

    def test_nested_corner_quotes(self) -> None:
        lines = dialogue_lines("“他说「我来了」，然后就走了。”九斤说。")
        self.assertEqual(len(lines), 1)
        self.assertIn("我来了", lines[0]["text"])
        self.assertEqual(NAME[lines[0]["speaker_id"]], "林九斤")

    def test_nested_same_kind_quotes(self) -> None:
        lines = dialogue_lines("“他说“我来了”，然后走了。”九斤说。")
        self.assertEqual(len(lines), 1)
        self.assertIn("我来了", lines[0]["text"])


class OffscreenTest(unittest.TestCase):
    """角色卡外的龙套回退未知，不强行归属。"""

    def test_emcee_after_quote(self) -> None:
        lines = dialogue_lines("九斤站在旁边。\n“一拜天地。”司仪说。")
        self.assertEqual(lines[0]["speaker_id"], UNKNOWN_SPEAKER_ID)

    def test_student_after_quote(self) -> None:
        lines = dialogue_lines("“别管她！”大学生喊道。")
        self.assertEqual(lines[0]["speaker_id"], UNKNOWN_SPEAKER_ID)

    def test_emphasis_quote_folded_into_narration(self) -> None:
        text = "九斤看着那堆纸钱。上面写着“柳庄”，她不认识。"
        lines = split_lines(text, CHARS)
        self.assertEqual([line for line in lines if line["kind"] != "narration"], [])
        self.assertTrue(any("“柳庄”" in line["text"] for line in lines if line["kind"] == "narration"))

    def test_mixed_paragraph_keeps_real_dialogue(self) -> None:
        text = "周也开口：“走吧。”九斤看着纸钱，上面写着“柳庄”，她不认识。"
        lines = split_lines(text, CHARS)
        dialogues = [line for line in lines if line["kind"] != "narration"]
        self.assertEqual(len(dialogues), 1)
        self.assertEqual(dialogues[0]["text"], "走吧。")
        self.assertEqual(NAME[dialogues[0]["speaker_id"]], "周也")
        self.assertTrue(any("“柳庄”" in line["text"] for line in lines if line["kind"] == "narration"))

    def test_folded_sink_collects_emphasis_text(self) -> None:
        folded: set[str] = set()
        split_lines("九斤看着纸钱。上面写着“柳庄”，她不认识。", CHARS, folded_sink=folded)
        self.assertEqual(folded, {"柳庄"})


class CandidateTest(unittest.TestCase):
    """候选集限定本场景实际出场角色，且不超过 10 个。"""

    def test_candidates_capped(self) -> None:
        characters = [
            {"id": f"chr_{index}", "name": f"角色{index}", "aliases": [], "gender": "male"} for index in range(12)
        ]
        scene = "。".join(f"角色{index}站着" for index in range(12)) + "。"
        self.assertEqual(len(_scene_candidates(characters, scene)), 10)

    def test_character_not_in_scene_is_not_used(self) -> None:
        # 周也是男性但不在本场景，只有九斤在场 → 「他说」无法解析
        lines = dialogue_lines("九斤坐在桌前。“怎么说？”他说。")
        self.assertEqual(lines[0]["speaker_id"], UNKNOWN_SPEAKER_ID)


class ResplitKeepManualTest(unittest.TestCase):
    """重切时只补空，不覆盖人工已改的说话人。"""

    def test_known_speaker_kept(self) -> None:
        existing = [{"text": "“对。”", "kind": "dialogue", "speaker_id": "chr_3", "confidence": 0.1, "index": 0}]
        drafts = [{"text": "“对。”", "kind": "dialogue", "speaker_id": "chr_1", "confidence": 0.9}]
        merged = merge_manual_lines(existing, drafts, "sc_1")
        self.assertEqual(merged[0]["speaker_id"], "chr_3")

    def test_unknown_speaker_filled(self) -> None:
        existing = [
            {"text": "“对。”", "kind": "dialogue", "speaker_id": UNKNOWN_SPEAKER_ID, "confidence": 0.0, "index": 0}
        ]
        drafts = [{"text": "“对。”", "kind": "dialogue", "speaker_id": "chr_1", "confidence": 0.9}]
        merged = merge_manual_lines(existing, drafts, "sc_1")
        self.assertEqual(merged[0]["speaker_id"], "chr_1")
        self.assertAlmostEqual(merged[0]["confidence"], 0.9)

    def test_folded_unknown_row_dropped(self) -> None:
        existing = [
            {"text": "柳庄", "kind": "dialogue", "speaker_id": UNKNOWN_SPEAKER_ID, "confidence": 0.0, "index": 0}
        ]
        drafts = [{"text": "她走进村口。", "kind": "narration", "speaker_id": "chr_narrator", "confidence": 1.0}]
        merged = merge_manual_lines(existing, drafts, "sc_1", folded_texts={"柳庄"})
        self.assertEqual([row["text"] for row in merged], ["她走进村口。"])

    def test_folded_known_speaker_kept(self) -> None:
        existing = [
            {"text": "柳庄", "kind": "dialogue", "speaker_id": "chr_1", "confidence": 0.9, "index": 0}
        ]
        drafts = [{"text": "她走进村口。", "kind": "narration", "speaker_id": "chr_narrator", "confidence": 1.0}]
        merged = merge_manual_lines(existing, drafts, "sc_1", folded_texts={"柳庄"})
        self.assertEqual(merged[0]["speaker_id"], "chr_1")

    def test_folded_row_with_audio_kept(self) -> None:
        existing = [
            {
                "text": "柳庄",
                "kind": "dialogue",
                "speaker_id": UNKNOWN_SPEAKER_ID,
                "confidence": 0.0,
                "index": 0,
                "tts": {"asset_id": "asset_1", "status": "ready"},
            }
        ]
        drafts = [{"text": "她走进村口。", "kind": "narration", "speaker_id": "chr_narrator", "confidence": 1.0}]
        merged = merge_manual_lines(existing, drafts, "sc_1", folded_texts={"柳庄"})
        self.assertEqual(merged[0]["text"], "柳庄")


class UnresolvedSpeakerTest(unittest.TestCase):
    """配音阻断清单只统计台词，旁白即使 speaker 脏了也不算未知台词。"""

    def test_narration_is_skipped(self) -> None:
        chapter = {
            "id": "chp_1",
            "title": "第一章",
            "scenes": [
                {
                    "id": "sc_1",
                    "index": 0,
                    "lines": [
                        {"id": "lin_1", "index": 0, "kind": "narration", "speaker_id": UNKNOWN_SPEAKER_ID, "text": "旁白。"},
                        {"id": "lin_2", "index": 1, "kind": "dialogue", "speaker_id": UNKNOWN_SPEAKER_ID, "text": "台词。"},
                    ],
                }
            ],
        }

        class Store:
            """最小工程读取器。"""

            def load_characters(self):
                """返回角色卡。"""
                return {"characters": CHARS}

        rows = unresolved_speaker_lines(Store(), [chapter])
        self.assertEqual([row["text"] for row in rows], ["台词。"])


WEAK_CHARS = [
    {"id": "chr_1", "name": "林九斤", "aliases": ["九斤"], "role": "lead", "gender": "female"},
    {"id": "chr_2", "name": "苏梅", "aliases": ["苏姐", "女人"], "role": "support", "gender": "female"},
    {"id": "chr_3", "name": "傩王", "aliases": ["它"], "role": "boss", "gender": "unknown"},
    {"id": "chr_4", "name": "纸奶奶", "aliases": ["老板娘", "纸人"], "role": "boss", "gender": "female"},
]


class WeakAliasTest(unittest.TestCase):
    """弱代称（女人 / 纸人 / 它）：场景唯一且有支持线索时才采信，置信度低。"""

    def test_nickname_in_narration(self) -> None:
        lines = dialogue_lines("苏梅站在门口。“别怕，”女人说，“我们是刚出来的。”", WEAK_CHARS)
        self.assertEqual([line["speaker_id"] for line in lines], ["chr_2", "chr_2"])
        self.assertTrue(all(line["confidence"] <= 0.5 for line in lines))

    def test_nickname_with_action_in_narration(self) -> None:
        lines = dialogue_lines(
            "苏梅也来了。\n“傩面村。”女人站起身，拍了拍裙子上的灰。“我们四个进去的，就剩我和他。”",
            WEAK_CHARS,
        )
        self.assertEqual([line["speaker_id"] for line in lines], ["chr_2", "chr_2"])

    def test_nickname_without_scene_support_stays_unknown(self) -> None:
        lines = dialogue_lines("“别怕，”女人说，“我们是刚出来的。”", WEAK_CHARS)
        self.assertTrue(all(line["speaker_id"] == UNKNOWN_SPEAKER_ID for line in lines))

    def test_ambiguous_nickname_stays_unknown(self) -> None:
        characters = WEAK_CHARS + [{"id": "chr_5", "name": "李姐", "aliases": ["女人"], "gender": "female"}]
        lines = dialogue_lines("苏梅和李姐都到了。女人说：“走吧。”", characters)
        self.assertEqual([line["speaker_id"] for line in lines], [UNKNOWN_SPEAKER_ID])

    def test_it_requires_speech_verb(self) -> None:
        lines = dialogue_lines("傩王没有动。\n“不，”它说，“不开脸。”", WEAK_CHARS)
        self.assertEqual([line["speaker_id"] for line in lines], ["chr_3", "chr_3"])
        self.assertTrue(all(line["confidence"] <= 0.5 for line in lines))

    def test_it_action_only_is_not_attributed(self) -> None:
        lines = dialogue_lines("傩王在场。\n它拿起了铜钱。“换脸。”", WEAK_CHARS)
        self.assertEqual([line["speaker_id"] for line in lines], [UNKNOWN_SPEAKER_ID])

    def test_zhiren_with_speech_verb(self) -> None:
        lines = dialogue_lines("纸奶奶站在柜台后。\n“欢迎来到纸扎世界。”纸人说。“我是这里的主人。”", WEAK_CHARS)
        self.assertEqual([line["speaker_id"] for line in lines], ["chr_4", "chr_4"])


if __name__ == "__main__":
    unittest.main()
