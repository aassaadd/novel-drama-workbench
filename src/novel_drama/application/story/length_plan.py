"""正文长度提示词口径：目标区间 + 分段要求。

只影响提示词怎么写，**不是输出上限**：生成过程仍不限长，写完后按目标字数
判偏差并走超纲 / 不足的动作。

阈值集中在本模块：目标区间上限倍率、各档每段字数、档位分界。
`length_plan()` 返回的档位会写进日志，便于排查「这章为什么按这个区间写」。
"""

from __future__ import annotations

#: 目标区间上限 = 目标字数 × 该倍率（下限就是目标字数）
TARGET_RANGE_HIGH_RATIO = 1.15
#: 档位分界（目标字数）
TIER_SHORT_MAX = 1500
TIER_MEDIUM_MAX = 4000
#: 各档每段字数区间（低, 高）
PARAGRAPH_CHARS = {
    "short": (300, 500),
    "medium": (600, 800),
    "long": (600, 800),
}
TIER_LABELS = {"short": "短篇", "medium": "中篇", "long": "长篇"}


def length_plan(target_words: int) -> dict:
    """按章节目标字数推导目标区间、段数与每段字数。

    参数:
        target_words: 本章目标字数（节拍目标之和）。

    返回:
        `{tier, tier_label, low, high, min_paragraphs, max_paragraphs,
        paragraph_low, paragraph_high}`。
    """
    target = max(200, int(target_words or 0))
    if target < TIER_SHORT_MAX:
        tier = "short"
    elif target < TIER_MEDIUM_MAX:
        tier = "medium"
    else:
        tier = "long"
    paragraph_low, paragraph_high = PARAGRAPH_CHARS[tier]
    low = target
    high = max(low, int(round(target * TARGET_RANGE_HIGH_RATIO / 50.0) * 50))
    # 段数按「目标下限 / 每段上限」到「目标上限 / 每段下限」推导，至少 1 段
    min_paragraphs = max(1, int(round(low / paragraph_high)))
    max_paragraphs = max(min_paragraphs, int(round(high / paragraph_low)))
    return {
        "tier": tier,
        "tier_label": TIER_LABELS[tier],
        "low": low,
        "high": high,
        "min_paragraphs": min_paragraphs,
        "max_paragraphs": max_paragraphs,
        "paragraph_low": paragraph_low,
        "paragraph_high": paragraph_high,
    }


def length_instruction(target_words: int) -> str:
    """生成「目标区间 + 分段 + 禁止重复」的中文提示词片段。

    参数:
        target_words: 本章（或本轮）目标字数。

    返回:
        可直接拼进提示词的说明。
    """
    plan = length_plan(target_words)
    return (
        f"篇幅参考约 {plan['low']}–{plan['high']} 字（{plan['tier_label']}档），"
        f"分 {plan['min_paragraphs']}–{plan['max_paragraphs']} 段，"
        f"每段约 {plan['paragraph_low']}–{plan['paragraph_high']} 字；"
        "只作篇幅参考，不因字数提前结束。"
        "禁止复述同一句对白、同一句式或同一段落结构；每次对话都要推进新信息。"
    )
