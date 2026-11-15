"""章节长度阈值与页面动作（全工程唯一出处，页面文案与此同源）。"""

from __future__ import annotations

# 默认每章目标字数
DEFAULT_CHAPTER_WORDS = 1200

#: 超纲阈值（全工程唯一出处，页面文案与此同源）：
#: > 1.5 倍只在结果里标字数；>= 2 倍才提供「按目标字数压缩」「重写」两个动作。
OVERSHOOT_NOTICE_RATIO = 1.5

OVERSHOOT_ACTION_RATIO = 2.0

#: 不足阈值：低于目标 0.7 倍时提供「补写」动作
UNDERFILL_ACTION_RATIO = 0.7

#: 页面上展示的动作（id 与 generate_chapter 的 mode 对应）
OVERSHOOT_ACTIONS = (
    {"id": "compress", "label": "按目标字数压缩"},
    {"id": "rewrite", "label": "重写"},
)

UNDERFILL_ACTIONS = ({"id": "supplement", "label": "补写"},)

#: 补写时的要点覆盖判定：节拍摘要关键词在正文里的命中率低于此值，视为「尚未覆盖」。
#: 只用于挑出要补的情节要点，不参与长度判断。
SUPPLEMENT_COVERAGE_RATIO = 0.34

#: 退化后的局部续写次数上限（调研 2.3 / 6.1：同一节最多回退 2 次，再失败整节重写）
MAX_LOCAL_CONTINUATIONS = 2
