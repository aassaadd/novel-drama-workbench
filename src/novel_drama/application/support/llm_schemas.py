"""结构化生成用的 JSON Schema 与步骤映射（纯数据，不含业务判定）。"""

from __future__ import annotations

import copy

from ...models.enums import AgeRange, SpeechPitch, SpeechTempo
from ..story.setting_spec import (
    GENDER_OPTIONS,
    LENGTH_OPTIONS,
    POV_OPTIONS,
    ROLE_OPTIONS,
    TENSE_OPTIONS,
)


def _gen_tool(step: str | None) -> dict | None:
    """按生成步骤返回对应的工具定义（OpenAI tools 单项）。

    参数:
        step: outline|act|premise|style|world|worlds|character|characters。

    返回:
        工具定义 dict；未知步骤返回 None。

    异常:
        无。
    """
    if step == "outline_acts":
        return {
            "type": "function",
            "function": {"name": "emit_acts", "description": "输出幕列表（标题、目标/概要、预计章数）", "parameters": _HIER_ACTS_TOOL_SCHEMA},
        }
    if step == "outline_chapters":
        return {
            "type": "function",
            "function": {"name": "emit_chapters", "description": "输出某一幕的章节列表（标题与概要）", "parameters": _HIER_CHAPTERS_TOOL_SCHEMA},
        }
    if step == "outline_beats":
        return {
            "type": "function",
            "function": {"name": "emit_beats", "description": "输出某一章的全部节拍", "parameters": _HIER_BEATS_TOOL_SCHEMA},
        }
    if step == "outline":
        return {
            "type": "function",
            "function": {"name": "emit_outline", "description": "输出幕/章/节拍大纲", "parameters": _OUTLINE_TOOL_SCHEMA},
        }
    if step == "act":
        return {
            "type": "function",
            "function": {"name": "emit_act", "description": "输出单幕大纲（含本幕章节与节拍）", "parameters": _ACT_TOOL_SCHEMA},
        }
    if step == "premise":
        return {
            "type": "function",
            "function": {"name": "emit_premise", "description": "输出故事梗概设定", "parameters": _PREMISE_TOOL_SCHEMA},
        }
    if step == "style":
        return {
            "type": "function",
            "function": {"name": "emit_style", "description": "输出文风设定", "parameters": _STYLE_TOOL_SCHEMA},
        }
    if step == "worlds":
        return {
            "type": "function",
            "function": {
                "name": "emit_worlds",
                "description": "输出多条世界设定",
                "parameters": {
                    "type": "object",
                    "properties": {"worlds": {"type": "array", "items": _WORLD_TOOL_SCHEMA}},
                    "required": ["worlds"],
                },
            },
        }
    if step == "character":
        return {
            "type": "function",
            "function": {"name": "emit_character", "description": "输出一个完整人物设定", "parameters": _CHARACTER_TOOL_SCHEMA},
        }
    if step == "character_plan":
        return {
            "type": "function",
            "function": {
                "name": "emit_character_plan",
                "description": "输出人物名单：每人姓名、定位与一句话简介，不写详细设定",
                "parameters": _CHARACTER_PLAN_TOOL_SCHEMA,
            },
        }
    if step == "characters":
        return {
            "type": "function",
            "function": {
                "name": "emit_characters",
                "description": "输出人物设定：数量按需求自行判断（至少 1 名主角），不要凑数",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "characters": {"type": "array", "items": _CHARACTER_TOOL_SCHEMA},
                        "stock_characters": {
                            "type": "array",
                            "items": _STOCK_CHARACTER_TOOL_SCHEMA,
                            "description": "常用路人/龙套池，固定 6-10 个职业，供跨章复用音色",
                        },
                        "narrator_profile": {
                            "type": "object",
                            "properties": {"voice_profile": _VOICE_PROFILE_TOOL_SCHEMA},
                            "required": ["voice_profile"],
                            "description": "旁白音色描述（旁白是既有实体，这里只产出描述）",
                        },
                    },
                    "required": ["characters"],
                },
            },
        }
    return None

#: 世界条目参数 schema（单条与批量共用；世界库负责总述与分类条目）
_WORLD_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "世界/时代名称，80 字以内"},
        "era": {"type": "string", "description": "时代背景，200 字以内"},
        "summary": {"type": "string", "description": "世界观总述，500 字以内"},
        "rules": {"type": "array", "items": {"type": "string"}, "description": "世界规则/力量体系，每条 120 字以内"},
        "factions": {"type": "array", "items": {"type": "string"}, "description": "主要势力，每条 120 字以内"},
        "locations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "atmosphere": {"type": "string"},
                },
                "required": ["name"],
            },
            "description": "关键地点：JSON 数组，每个元素必须是对象（不要写成字符串），含 name/description/atmosphere",
        },
        "others": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["name"],
            },
            "description": "其它条目（关键物件、习俗、生物等未归类内容）：对象数组，含 name/description",
        },
        "glossary": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "term": {"type": "string"},
                    "meaning": {"type": "string"},
                    "aliases": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["term", "meaning"],
            },
            "description": "专有名词表：JSON 数组，每个元素必须是对象（不要写成字符串），含 term/meaning",
        },
        "visual_style": {"type": "string", "description": "画面风格关键词，200 字以内"},
    },
    "required": ["name", "era"],
}

#: 音色描述参数 schema（正式角色 / 龙套 / 旁白共用）
_VOICE_PROFILE_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "voice_summary": {"type": "string", "description": "一句话音色概括，80 字以内"},
        "gender": {"type": "string", "enum": list(GENDER_OPTIONS)},
        "age_feel": {
            "type": "string",
            "enum": ["child", "teen", "young_adult", "adult", "middle_aged", "elderly"],
        },
        "age_range": {"type": "string", "description": "年龄区间，如「30-39」"},
        "pitch": {
            "type": "string",
            "enum": ["very_low", "low", "mid_low", "mid", "mid_high", "high", "very_high"],
        },
        "timbre": {"type": "array", "items": {"type": "string"}, "description": "2-4 个音色质感词"},
        "pace": {"type": "string", "enum": ["very_slow", "slow", "normal", "fast", "very_fast"]},
        "volume": {"type": "string", "enum": ["soft", "normal", "loud"]},
        "energy": {"type": "string", "enum": ["calm", "conversational", "dramatic", "authoritative"]},
        "warmth": {"type": "string", "enum": ["warm", "neutral", "cold", "distant"]},
        "articulation": {"type": "string", "enum": ["clear", "mumbled", "clipped", "drawl"]},
        "temperament": {"type": "array", "items": {"type": "string"}, "description": "2-4 个气质词"},
        "accent": {"type": "string", "description": "口音/地域腔调；标准普通话写「标准普通话」"},
        "design_prompt": {"type": "string", "description": "可直接用于文字造音色的提示词，120 字以内"},
        "negative_prompt": {"type": "array", "items": {"type": "string"}, "description": "应避免的音色特征"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "evidence": {"type": "array", "items": {"type": "string"}, "description": "推导依据，引用人物档案里的字段"},
    },
    "required": ["voice_summary", "design_prompt"],
}

#: 龙套槽位参数 schema
_STOCK_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "slot": {
            "type": "string",
            "description": "职业槽位稳定英文键，小写+下划线，如 waiter/guard/runner",
        },
        "voice_slot": {
            "type": "string",
            "description": "音色池槽位：male_young/male_mid/male_elder/female_young/female_mid/female_elder",
        },
        "label": {"type": "string", "description": "中文职业名，如「店小二」"},
        "archetype_tags": {"type": "array", "items": {"type": "string"}, "description": "2-3 个语义标签"},
    },
    "required": ["slot", "label"],
}

#: 人物条目参数 schema（单个与批量共用）
_CHARACTER_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "姓名，60 字以内"},
        "role": {
            "type": "string",
            "enum": list(ROLE_OPTIONS),
            "description": "protagonist=主角，antagonist=反派，supporting=配角，minor=龙套",
        },
        "gender": {"type": "string", "enum": list(GENDER_OPTIONS)},
        "age_range": {
            "type": "string",
            "enum": list(AgeRange.values()),
            "description": "年龄段分档，只能从枚举里选：10-14 儿童 / 18-25 青年 / 26-39 成年 / 40-54 中年 / 55-70 老年",
        },
        "personality": {
            "type": "array",
            "items": {"type": "string"},
            "description": "3-5 个性格短标签，每个不超过 20 字",
        },
        "background": {"type": "string", "description": "人物背景，500 字以内"},
        "speech_style": {
            "type": "object",
            "description": "写作时直接注入提示词的口吻约束：语速、音高、口音/用词习惯、口头禅",
            "properties": {
                "tempo": {
                    "type": "string",
                    "enum": list(SpeechTempo.values()),
                    "description": "语速档位：slow=慢，medium=中速，fast=快（只选枚举，不要写「偏慢，常停顿」这类长句）",
                },
                "pitch": {
                    "type": "string",
                    "enum": list(SpeechPitch.values()),
                    "description": "音高档位：low=偏低，medium=中等，high=偏高（只选枚举）",
                },
                "accent": {"type": "string", "description": "口音或用词习惯，如「带江南口音，爱用反问」"},
                "catchphrases": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "1-3 句标志性口头禅，写作时可直接复用",
                },
            },
        },
        "appearance_prompt": {"type": "string", "description": "外貌提示词，用于配图（可与 appearance_desc_en 一致）"},
        "negative_prompt": {"type": "string", "description": "配图负向提示词（可与 appearance_negative_en 一致）"},
        "voice_description_zh": {
            "type": "string",
            "description": "中文音色描述：音色质感、语速、语气、年龄感、口音，80-160 字，供 TTS 参考与人工校对",
        },
        "voice_description_en": {
            "type": "string",
            "description": "英文音色描述：可直接用于语音模型的 prompt，纯英文，覆盖 timbre/pace/tone/age feel/accent",
        },
        "appearance_desc_zh": {
            "type": "string",
            "description": "中文外貌描述（正面）：脸型、五官、发型、身形、惯常着装与气质，具体不空泛",
        },
        "appearance_desc_en": {
            "type": "string",
            "description": "英文正面出图 prompt：纯英文、具体可见特征，避免 young woman 这类泛描述",
        },
        "appearance_negative_zh": {
            "type": "string",
            "description": "中文负向说明：出图应排除的元素（如眼镜、胡须、伤疤），用名词短语，不要堆否定句",
        },
        "appearance_negative_en": {
            "type": "string",
            "description": "英文 negative prompt：逗号分隔的排除项，写 glasses, beard 这类名词，不要 not/no 句式",
        },
        "relationships": {
            "type": "object",
            "additionalProperties": {"type": "string"},
            "description": "与其他人物/势力的关系（名字→备注）",
        },
        "state": {
            "type": "object",
            "properties": {
                "relationship_notes": {"type": "object", "additionalProperties": {"type": "string"}}
            },
            "description": "人物状态；单人物生成时关系备注写在 relationship_notes",
        },
        "voice_profile": _VOICE_PROFILE_TOOL_SCHEMA,
        "stock": _STOCK_TOOL_SCHEMA,
    },
    "required": ["name"],
}

#: 龙套条目 schema：在人物 schema 上要求 stock
_STOCK_CHARACTER_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        **_CHARACTER_TOOL_SCHEMA["properties"],
        "stock": _STOCK_TOOL_SCHEMA,
        "role": {"type": "string", "enum": ["minor"], "description": "龙套固定为 minor"},
    },
    "required": ["name", "stock"],
}

#: 人物名单参数 schema（两阶段生成的第一阶段：只出名单，不写详情）
_CHARACTER_PLAN_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "characters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "姓名，60 字以内；不要与已有角色重名"},
                    "role": {
                        "type": "string",
                        "enum": list(ROLE_OPTIONS),
                        "description": "protagonist=主角，antagonist=反派，supporting=配角，minor=龙套",
                    },
                    "one_line": {"type": "string", "description": "一句话定位（身份 + 在故事里的作用），40 字以内"},
                    "main": {"type": "boolean", "description": "是否主要角色（主要角色会进入候选列表）"},
                },
                "required": ["name", "role", "one_line"],
            },
        },
        "basis": {"type": "string", "description": "名单依据：篇幅、章节量、已有角色与题材需要"},
    },
    "required": ["characters"],
}

#: 梗概参数 schema
_PREMISE_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "logline": {"type": "string", "description": "一句话故事核心，120 字以内"},
        "theme": {"type": "string", "description": "主题，200 字以内"},
        "core_conflict": {"type": "string", "description": "核心冲突，300 字以内"},
        "ending_intent": {"type": "string", "description": "结局意图，200 字以内"},
        "target_audience": {"type": "string", "description": "目标读者，200 字以内"},
        "target_length": {
            "type": "string",
            "enum": list(LENGTH_OPTIONS),
            "description": "short=短篇，novella=中篇，novel=长篇，series=长篇连载",
        },
    },
    "required": ["logline"],
}

#: 文风参数 schema
_STYLE_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "tone": {"type": "string", "description": "整体基调，200 字以内"},
        "pov": {"type": "string", "enum": list(POV_OPTIONS), "description": "叙事视角"},
        "tense": {"type": "string", "enum": list(TENSE_OPTIONS), "description": "时态"},
        "prose_style": {"type": "string", "description": "文笔风格描述，500 字以内"},
        "forbidden": {
            "type": "array",
            "items": {"type": "string"},
            "description": "需要避免的写法，每条 40 字以内",
        },
        "visual_style": {
            "type": "string",
            "description": "画面风格：具体的场景、光线、色彩、材质与镜头质感，120-300 字，不能为空",
        },
    },
    "required": ["tone", "visual_style"],
}

#: 大纲参数 schema（幕→章→节拍三层结构）
_OUTLINE_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "acts": {
            "type": "array",
            "description": "幕列表，按顺序排列",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "幕标题，80 字以内"},
                    "summary": {"type": "string", "description": "本幕概要，300 字以内"},
                    "chapters": {
                        "type": "array",
                        "description": "本幕章节，按顺序排列",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string", "description": "章节标题，80 字以内"},
                                "summary": {"type": "string", "description": "本章概要，300 字以内"},
                                "beats": {
                                    "type": "array",
                                    "description": "本章节拍，每个元素必须是对象（不要写成字符串）",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "summary": {"type": "string", "description": "节拍内容，200 字以内"},
                                            "character_ids": {
                                                "type": "array",
                                                "items": {"type": "string"},
                                                "description": "参与人物 id，从给定角色表中选取，可为空",
                                            },
                                            "turning_point": {"type": "boolean", "description": "是否为本章转折点"},
                                            "target_words": {"type": "integer", "description": "该节拍目标字数"},
                                            "extra_characters": {
                                                "type": "array",
                                                "items": {"type": "string"},
                                                "description": "只在本章出现、未建卡的龙套姓名（按 minor 处理，不进入角色表）",
                                            },
                                        },
                                        "required": ["summary"],
                                    },
                                },
                            },
                            "required": ["title"],
                        },
                    },
                },
                "required": ["title"],
            },
        }
    },
    "required": ["acts"],
}

#: 本章出场角色（与节拍同一次调用产出，不再单独发角色请求）
_CAST_ITEM_SCHEMA = {
    "type": "object",
    "required": ["name", "beats"],
    "properties": {
        "existing_id": {"type": "string", "description": "复用现有角色时填其 ID；新建角色留空"},
        "name": {"type": "string", "description": "角色姓名"},
        "aliases": {
            "type": "array",
            "items": {"type": "string"},
            "description": "本章出现的称呼/别名",
        },
        "role": {"type": "string", "enum": ["protagonist", "antagonist", "supporting", "minor"]},
        "gender": {"type": "string", "enum": ["male", "female", "other", "unknown"]},
        "age_range": {"type": "string"},
        "personality": {"type": "array", "items": {"type": "string"}},
        "background": {"type": "string", "description": "新建角色必须给，已有角色可留空"},
        "speech_style": {
            "type": "object",
            "properties": {
                "tempo": {"type": "string"},
                "pitch": {"type": "string"},
                "accent": {"type": "string"},
                "catchphrases": {"type": "array", "items": {"type": "string"}},
            },
        },
        "beats": {
            "type": "array",
            "items": {"type": "integer"},
            "description": "该角色出场的节拍序号（从 1 开始），这是绑定依据",
        },
    },
}

#: 单幕参数 schema：复用大纲结构里的 act 对象，外面再包一层 act 键，
#: 让 `emit_act` 一轮只产出一幕（title/summary/chapters[title/summary/beats/cast]）。
_ACT_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "act": copy.deepcopy(_OUTLINE_TOOL_SCHEMA["properties"]["acts"]["items"]),
    },
    "required": ["act"],
}

#: 三级分层生成：阶段一幕列表（不塞章节与节拍）
_HIER_ACTS_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "acts": {
            "type": "array",
            "description": "幕列表，按顺序排列",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "幕标题，80 字以内"},
                    "summary": {"type": "string", "description": "本幕目标与概要，300 字以内"},
                    "chapters_count": {"type": "integer", "description": "本幕预计章数"},
                },
                "required": ["title"],
            },
        }
    },
    "required": ["acts"],
}

#: 三级分层生成：阶段二某幕的章节（只出标题与概要）
_HIER_CHAPTERS_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "chapters": {
            "type": "array",
            "description": "本幕章节，按顺序排列",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "章节标题，80 字以内"},
                    "summary": {"type": "string", "description": "本章概要，300 字以内"},
                },
                "required": ["title"],
            },
        }
    },
    "required": ["chapters"],
}

#: 复用大纲结构里的节拍项定义，避免重复维护
_BEAT_ITEM_SCHEMA = _OUTLINE_TOOL_SCHEMA["properties"]["acts"]["items"]["properties"]["chapters"]["items"]["properties"]["beats"]["items"]

#: 三级分层生成：阶段三某章的节拍（一次补齐）
_HIER_BEATS_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "beats": {"type": "array", "description": "本章全部节拍，按顺序排列", "items": _BEAT_ITEM_SCHEMA},
        "cast": {
            "type": "array",
            "description": "本章出场角色；与节拍同一次调用产出，每项必须给 beats 绑定依据",
            "items": _CAST_ITEM_SCHEMA,
        },
        "character_arcs": {
            "type": "array",
            "description": "本章主要角色弧线：角色在本章从什么状态走到什么状态（可选，最多 3 条）",
            "items": {
                "type": "object",
                "properties": {
                    "character_id": {"type": "string", "description": "角色 ID，必须来自给定角色表"},
                    "from_state": {"type": "string", "description": "本章开始时的状态"},
                    "to_state": {"type": "string", "description": "本章结束时的状态"},
                    "note": {"type": "string", "description": "变化原因（可选）"},
                },
                "required": ["character_id", "to_state"],
            },
        },
    },
    "required": ["beats"],
}
