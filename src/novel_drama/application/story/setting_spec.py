"""设定生成用的字段表与取值白名单（纯数据）。"""

from __future__ import annotations

#: 设定生成步骤（世界观归世界库，写作设定只管故事层）
SETTING_STEPS = ("premise", "style")

#: 梗概分区字段（与 setting-bible.schema.json 对齐）
PREMISE_FIELDS = ("logline", "theme", "core_conflict", "ending_intent", "target_audience", "target_length")

#: 文风分区字段
STYLE_FIELDS = ("tone", "pov", "tense", "prose_style", "forbidden", "visual_style")

#: 标量字段长度上限（来自 schema）
SCALAR_LIMITS = {
    "logline": 500,
    "theme": 500,
    "core_conflict": 1000,
    "ending_intent": 1000,
    "target_audience": 200,
    "tone": 300,
    "prose_style": 1000,
    "visual_style": 500,
    "character.appearance_prompt": 2000,
    "character.negative_prompt": 1000,
}

#: 枚举取值
POV_OPTIONS = ("first", "third_limited", "third_omniscient", "second")

TENSE_OPTIONS = ("past", "present")

LENGTH_OPTIONS = ("short", "novella", "novel", "series")

ROLE_OPTIONS = ("protagonist", "antagonist", "supporting", "minor")

GENDER_OPTIONS = ("male", "female", "other", "unknown")

#: 模型偶尔把枚举写成中文（含括号说明），校验前先按前缀归一；长别名放前面避免误匹配
CHOICE_ALIASES = {
    "短篇": "short",
    "中篇": "novella",
    "长篇连载": "series",
    "系列": "series",
    "中长篇": "novella",
    "长篇": "novel",
    "第三人称有限": "third_limited",
    "第三人称全知": "third_omniscient",
    "第三人称": "third_limited",
    "第一人称": "first",
    "第二人称": "second",
    "过去时": "past",
    "现在时": "present",
    "主角": "protagonist",
    "男主": "protagonist",
    "女主": "protagonist",
    "反派": "antagonist",
    "配角": "supporting",
    "男配": "supporting",
    "女配": "supporting",
    "龙套": "minor",
    "男性": "male",
    "女性": "female",
    "男": "male",
    "女": "female",
    "其他": "other",
    "未知": "unknown",
}

#: 单字段润色白名单（世界观文本由世界库维护，不在写作设定里润色）
POLISH_FIELDS = (
    "logline",
    "theme",
    "core_conflict",
    "ending_intent",
    "target_audience",
    "tone",
    "prose_style",
    "visual_style",
    "character.background",
    "character.personality",
    "character.appearance_prompt",
    "character.negative_prompt",
    "work.genre",
)

#: 黄金三章检查用的冲突语义关键词
CONFLICT_KEYWORDS = ("冲突", "对抗", "矛盾", "争执", "对峙", "摊牌", "威胁", "追杀", "决裂", "争夺", "冲突升级")
