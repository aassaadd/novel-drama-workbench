"""上下文装配单元测试：完整装配、预算裁剪、摘要回退与世界回退。

运行：`.venv/bin/python -m unittest tests.test_context -v`，
或直接 `.venv/bin/python tests/test_context.py`。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novel_drama.models.entities import NARRATOR_ID  # noqa: E402
from novel_drama.text.context import ContextAssembler  # noqa: E402

WORK = {
    "title": "长夜将明",
    "logline": "一名电台主播收到自己在未来发出的求救电话。",
    "genre": ["悬疑", "都市"],
    "style_guide": "克制，不说教",
}
BIBLE = {
    "premise": {
        "logline": "旧案重启，真相与牵挂只能二选一。",
        "theme": "记忆与自我欺骗",
        "core_conflict": "主角要查清旧案，体制要它永远封存",
        "ending_intent": "主角放弃翻案，把证据留给后来者",
    },
    "style": {
        "tone": "冷峻克制",
        "pov": "third_limited",
        "tense": "past",
        "prose_style": "短句为主，留白推进",
        "forbidden": ["不要网络流行语", "不要作者旁白式议论"],
    },
}
WORLD_DIGEST = "\n".join(
    [
        "【雾港】时代：民国末年的南方港城",
        "概述：港口常年被雾笼罩，帮会与报刊同时兴盛。",
        "画面风格：低饱和冷调，潮湿质感",
        "硬规则：午夜后不能接听陌生来电",
        "势力：旧城行会；守夜人小队",
        "地点：雾港码头（堆满无人认领的货箱）",
        "术语：持灯人：在停电夜维持街巷秩序的人",
    ]
)
CHARACTERS = [
    {"id": NARRATOR_ID, "name": "旁白", "role": "narrator", "personality": ["冷静"], "background": "旁白背景"},
    {
        "id": "chr_shen",
        "name": "沈砚",
        "role": "protagonist",
        "aliases": ["沈队"],
        "personality": ["克制", "多疑"],
        "background": "旧案唯一的目击者",
        "speech_style": {"tempo": "缓慢", "pitch": "偏低"},
        "state": {"relationship_notes": {"林见夏": "互相试探，谁都不肯先低头"}},
    },
    {
        "id": "chr_lin",
        "name": "林见夏",
        "role": "supporting",
        "aliases": [],
        "personality": ["敏锐"],
        "background": "电台同事，隐瞒了一件事",
        "speech_style": {"tempo": "轻快", "pitch": "中性"},
        "state": {"relationship_notes": {}},
    },
]
CHAPTER = {
    "id": "chp_003",
    "index": 3,
    "title": "旧信",
    "outline": "沈砚在旧信里发现关键线索",
    "synopsis": "",
    "content": "",
}
OUTLINE_CHAPTER = {
    "id": "chp_003",
    "index": 3,
    "title": "旧信",
    "summary": "沈砚追查旧信来源，与林见夏正面冲突。",
    "beats": [
        {"id": "bet_1", "summary": "沈砚在档案楼找到一封未寄出的旧信", "character_ids": ["chr_shen"], "turning_point": False, "target_words": 600},
        {"id": "bet_2", "summary": "林见夏承认自己早看过这封信", "character_ids": ["chr_shen", "chr_lin"], "turning_point": True, "target_words": 400},
    ],
    "pov_character_id": "chr_shen",
    "world_ids": ["wld_fog"],
}
PREVIOUS = [
    {"index": 1, "title": "夜雨", "synopsis": "第一章摘要：沈砚收到匿名电话。", "content": "第一章正文。"},
    {"index": 2, "title": "空屋", "synopsis": "第二章摘要：空屋里只有一台开着的收音机。", "content": "第二章正文。"},
]
INSTRUCTION = "写《旧信》本章正文，目标字数约 1000 字。\n输出格式：只输出正文。"


def make_assembler(**overrides) -> ContextAssembler:
    """按默认素材构造装配器，允许按用例覆盖单个入参。

    参数:
        overrides: 需要覆盖的 ContextAssembler 关键字参数。

    返回:
        ContextAssembler 实例。
    """
    params = {
        "work": WORK,
        "bible": BIBLE,
        "world_digest": WORLD_DIGEST,
        "characters": CHARACTERS,
        "chapter": CHAPTER,
        "outline_chapter": OUTLINE_CHAPTER,
        "previous_chapters": PREVIOUS,
        "current_content": "",
        "budget_tokens": 32768,
    }
    params.update(overrides)
    return ContextAssembler(**params)


def total_chars(req) -> int:
    """统计请求 system 与全部消息的总字数（中文按 1 字 ≈ 1 token）。

    参数:
        req: TextGenRequest。

    返回:
        总字符数。
    """
    return len(req.system) + sum(len(message.content) for message in req.messages)


class ContextAssemblerTest(unittest.TestCase):
    """上下文装配的核心行为测试。"""

    def test_full_context_order_and_content(self) -> None:
        """完整装配应包含世界、角色、前情、节拍，且顺序固定。"""
        assembler = make_assembler()
        req = assembler.build("chapter", INSTRUCTION)
        system = req.system
        user = req.messages[0].content

        self.assertEqual(assembler.warnings, [])
        # 故事圣经 → 世界 → 角色卡
        self.assertLess(system.index("【故事圣经】"), system.index("世界设定："))
        self.assertLess(system.index("世界设定："), system.index("角色卡："))
        self.assertIn("雾港", system)
        self.assertIn("午夜后不能接听陌生来电", system)
        # 角色卡必须含性格、背景、说话方式、关系备注
        self.assertIn("性格：克制、多疑", system)
        self.assertIn("背景：旧案唯一的目击者", system)
        self.assertIn("说话方式：缓慢，偏低", system)
        self.assertIn("关系备注：林见夏：互相试探", system)
        self.assertNotIn("- 旁白", system)
        # 前情摘要 → 本章大纲与节拍 → 前文摘录 → 任务指令
        self.assertLess(user.index("前情摘要："), user.index("【本章大纲与节拍】"))
        self.assertLess(user.index("【本章大纲与节拍】"), user.index("任务指令："))
        self.assertIn("第 1 章《夜雨》：第一章摘要", user)
        self.assertIn("第 2 章《空屋》：第二章摘要", user)
        self.assertIn("1. 沈砚在档案楼找到一封未寄出的旧信（目标 600 字）", user)
        self.assertIn("2. 林见夏承认自己早看过这封信（目标 400 字，转折点）", user)
        self.assertIn(INSTRUCTION, user)
        self.assertEqual(req.task, "chapter")
        self.assertEqual(req.context_budget_tokens, 32768)

    def test_speaker_ids_filter_and_narrator_excluded(self) -> None:
        """给定 speaker_ids 时只输出这些角色，且始终排除旁白。"""
        req = make_assembler().build("chapter", INSTRUCTION, speaker_ids=["chr_shen", NARRATOR_ID])
        system = req.system
        self.assertIn("沈砚", system)
        self.assertNotIn("- 林见夏（", system)
        self.assertNotIn("- 旁白", system)

    def test_previous_summary_fallback_to_content_tail(self) -> None:
        """前章无 synopsis 时回退正文末尾，两者皆空时跳过并记 warning。"""
        long_content = "正文开头。" + "推进" * 600 + "正文末尾标记"
        previous = [
            {"index": 1, "title": "远章", "synopsis": "", "content": long_content},
            {"index": 2, "title": "空章", "synopsis": "", "content": ""},
        ]
        assembler = make_assembler(previous_chapters=previous)
        user = assembler.build("chapter", INSTRUCTION).messages[0].content
        self.assertIn("第 1 章《远章》：", user)
        self.assertIn("正文末尾标记", user)
        self.assertNotIn("第 2 章《空章》", user)
        self.assertTrue(any("第 2 章" in warning and "跳过前情" in warning for warning in assembler.warnings))

    def test_world_falls_back_to_bible_digest(self) -> None:
        """未绑定世界时用设定库摘要 dict 渲染世界段。"""
        digest = {
            "era": "民国末年的南方港城",
            "logline": "旧案重启",
            "tone": "冷峻克制",
            "rules": ["规则甲"],
            "factions": ["旧城行会"],
            "locations": ["旧电台"],
            "glossary": ["回响"],
        }
        req = make_assembler(world_digest=digest).build("chapter", INSTRUCTION)
        system = req.system
        self.assertIn("【设定库摘要】", system)
        self.assertIn("时代：民国末年的南方港城", system)
        self.assertIn("硬规则：规则甲", system)
        self.assertIn("势力：旧城行会", system)
        self.assertIn("地点：旧电台", system)
        self.assertIn("术语：回响", system)

    def test_budget_trims_excerpt_before_summary(self) -> None:
        """超预算时先裁前文摘录，再动前情摘要。"""
        previous_with_tail = [
            {"index": 1, "title": "夜雨", "synopsis": "第一章摘要标记", "content": "第一章正文标记" * 50},
            {"index": 2, "title": "空屋", "synopsis": "第二章摘要标记", "content": "上一章结尾标记" * 50},
        ]
        full = make_assembler(previous_chapters=previous_with_tail, current_content="本章已有正文标记" * 50, budget_tokens=10**9)
        full_total = total_chars(full.build("chapter", INSTRUCTION))
        # 预算 = 不含任何前文摘录时的总长：必须先把摘录裁空才可能装下
        no_excerpt = make_assembler(
            previous_chapters=[
                {"index": 1, "title": "夜雨", "synopsis": "第一章摘要标记", "content": ""},
                {"index": 2, "title": "空屋", "synopsis": "第二章摘要标记", "content": ""},
            ],
            current_content="",
            budget_tokens=10**9,
        )
        budget = total_chars(no_excerpt.build("chapter", INSTRUCTION))
        self.assertLess(budget, full_total)

        assembler = make_assembler(previous_chapters=previous_with_tail, current_content="本章已有正文标记" * 50, budget_tokens=budget)
        req = assembler.build("chapter", INSTRUCTION)
        user = req.messages[0].content
        self.assertNotIn("前文摘录", user)
        self.assertNotIn("本章已有正文标记", user)
        self.assertNotIn("上一章结尾标记", user)
        self.assertIn("第一章摘要标记", user)
        self.assertIn("第二章摘要标记", user)
        self.assertIn("雾港", req.system)

    def test_budget_deep_trim_keeps_bible_and_instruction(self) -> None:
        """深度裁剪会去掉前情/世界细节/角色细节，但保留故事圣经与任务指令。"""
        previous_with_tail = [
            {"index": 1, "title": "夜雨", "synopsis": "第一章摘要标记", "content": "上一章结尾标记" * 50},
        ]
        characters = [dict(CHARACTERS[1])]
        characters[0]["background"] = "背景标记"
        characters[0]["personality"] = ["性格标记" * 300]
        characters[0]["speech_style"] = {"tempo": "说话方式标记" * 300, "pitch": ""}
        # 参考总长：只保留故事圣经 + 大纲 + 任务指令（去掉角色卡里的细节行）
        reference = make_assembler(
            characters=[CHARACTERS[0]],
            world_digest=None,
            previous_chapters=[],
            current_content="",
            budget_tokens=10**9,
        )
        budget = total_chars(reference.build("chapter", INSTRUCTION)) + 200

        assembler = make_assembler(
            characters=characters,
            previous_chapters=previous_with_tail,
            current_content="本章已有正文标记" * 50,
            budget_tokens=budget,
        )
        req = assembler.build("chapter", INSTRUCTION)
        system = req.system
        user = req.messages[0].content

        self.assertIn("【故事圣经】", system)
        self.assertIn("长夜将明", system)
        self.assertIn(INSTRUCTION, user)
        self.assertNotIn("前文摘录", user)
        self.assertNotIn("第一章摘要标记", user)
        self.assertNotIn("地点：雾港码头", system)
        self.assertNotIn("术语：持灯人", system)
        self.assertNotIn("硬规则：午夜后不能接听陌生来电", system)
        self.assertNotIn("势力：旧城行会", system)
        self.assertNotIn("背景标记", system)
        self.assertNotIn("性格标记", system)
        self.assertNotIn("说话方式标记", system)


if __name__ == "__main__":
    unittest.main()
