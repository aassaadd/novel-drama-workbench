"""角色字段清洗与龙套槽位规则（生成结果落到角色表前的最后一道清洗）。"""

from __future__ import annotations

import re

from ...models.enums import AgeRange, SpeechPitch, SpeechTempo
from ...models.ids import now_iso
from ..story.setting_spec import GENDER_OPTIONS, ROLE_OPTIONS
from ..support.llm_calls import _check_text, _clean_str_list, _normalize_choice


def _clean_age_range(value, owner: str) -> str | None:
    """清洗年龄段：只接受分档枚举，模型给出的数字区间按中点归入分档。

    参数:
        value: 模型返回的年龄段（可能是「18-24」「30s」「青年」等写法）。
        owner: 角色名的中文描述，用于报错，如「人物 张三」。

    返回:
        合法分档值；未填写返回 None。

    异常:
        ValueError: 无法归入任何分档（消息含可选值）。
    """
    raw = _check_text(value, 40, f"{owner} 的年龄段")
    if not raw:
        return None
    age = AgeRange.normalize(raw)
    if age is None:
        raise ValueError(f"{owner} 的年龄段「{raw}」无法归入分档；可选值：{'/'.join(AgeRange.values())}")
    return age

def _clean_speech_style(value) -> dict | None:
    """清洗人物说话方式（写作时直接注入提示词）。

    参数:
        value: 模型返回的 speech_style 对象，或旧数据。

    返回:
        `{tempo,pitch,accent,catchphrases}`；四项全空时返回 None。
    """
    if not isinstance(value, dict):
        return None
    raw_tempo = _check_text(value.get("tempo"), 40, "说话方式的语速")
    raw_pitch = _check_text(value.get("pitch"), 40, "说话方式的音高")
    tempo = SpeechTempo.normalize(raw_tempo) if raw_tempo else None
    pitch = SpeechPitch.normalize(raw_pitch) if raw_pitch else None
    if raw_tempo and tempo is None:
        raise ValueError(f"说话方式的语速「{raw_tempo}」不在可选范围；只能取 { '/'.join(SpeechTempo.values()) }")
    if raw_pitch and pitch is None:
        raise ValueError(f"说话方式的音高「{raw_pitch}」不在可选范围；只能取 { '/'.join(SpeechPitch.values()) }")
    clean = {
        "tempo": tempo,
        "pitch": pitch,
        "accent": _check_text(value.get("accent"), 40, "说话方式的口音"),
        "catchphrases": _clean_str_list(value.get("catchphrases"), 60, "说话方式的口头禅")[:3],
    }
    if not any([clean["tempo"], clean["pitch"], clean["accent"], clean["catchphrases"]]):
        return None
    return clean

def _clean_character_row(row, index: int) -> dict:
    """清洗单个 LLM 人物条目（重名由调用方判断）。

    参数:
        row: 模型返回的单个角色对象。
        index: 该角色在本次结果中的序号（从 0 开始，用于错误提示）。

    返回:
        清洗后的角色 dict（字段与 characters.json 对齐）。

    异常:
        ValueError: 条目不是对象或必填字段缺失。
    """
    if not isinstance(row, dict):
        raise ValueError(f"第 {index + 1} 个人物必须是对象")
    name = _check_text(row.get("name"), 60, f"第 {index + 1} 个人物姓名", required=True)
    role = _normalize_choice(row.get("role"), ROLE_OPTIONS) or "supporting"
    gender = _normalize_choice(row.get("gender"), GENDER_OPTIONS) or "unknown"
    relationships = row.get("relationships") or {}
    if not isinstance(relationships, dict):
        relationships = {}
    entry = {
        "name": name,
        "role": role,
        "gender": gender,
        "age_range": _clean_age_range(row.get("age_range"), f"人物 {name}"),
        "personality": _clean_str_list(row.get("personality"), 120, f"人物 {name} 的性格"),
        "background": _check_text(row.get("background"), 2000, f"人物 {name} 的背景"),
        "speech_style": _clean_speech_style(row.get("speech_style")),
        "appearance_prompt": _check_text(row.get("appearance_prompt"), 2000, f"人物 {name} 的外貌提示词"),
        "negative_prompt": _check_text(row.get("negative_prompt"), 1000, f"人物 {name} 的负向提示词"),
        "voice_description_zh": _check_text(row.get("voice_description_zh"), 1000, f"人物 {name} 的中文音色描述"),
        "voice_description_en": _check_text(row.get("voice_description_en"), 1000, f"人物 {name} 的英文音色描述"),
        "appearance_desc_zh": _check_text(row.get("appearance_desc_zh"), 1200, f"人物 {name} 的中文外貌描述"),
        "appearance_desc_en": _check_text(row.get("appearance_desc_en"), 1200, f"人物 {name} 的英文外貌描述"),
        "appearance_negative_zh": _check_text(row.get("appearance_negative_zh"), 800, f"人物 {name} 的中文负向说明"),
        "appearance_negative_en": _check_text(row.get("appearance_negative_en"), 800, f"人物 {name} 的英文负向提示词"),
        "relationships": {
            _check_text(key, 60, f"人物 {name} 的关系对象", required=True): _check_text(note, 500, f"人物 {name} 的关系备注")
            for key, note in relationships.items()
        },
    }
    profile = _clean_voice_profile(row.get("voice_profile"))
    if profile:
        entry["voice_profile"] = _attach_profile_source(entry, profile)
    stock = _clean_stock(row.get("stock")) if role == "minor" else None
    if stock:
        entry["stock"] = stock
        entry["name"] = _stock_display_name(entry["name"], stock["label"])
    # 新旧字段兼容：已有消费端只看 appearance_prompt / negative_prompt
    if not entry["appearance_prompt"] and entry["appearance_desc_en"]:
        entry["appearance_prompt"] = entry["appearance_desc_en"]
    if not entry["negative_prompt"] and entry["appearance_negative_en"]:
        entry["negative_prompt"] = entry["appearance_negative_en"]
    return entry

def _clean_voice_profile(row) -> dict | None:
    """清洗 LLM 产出的音色描述（只保留已知字段，长度受限）。

    参数:
        row: 模型返回的 voice_profile 对象。

    返回:
        清洗后的 dict；空对象返回 None。
    """
    if not isinstance(row, dict):
        return None
    profile: dict = {}
    for key, limit in _VOICE_PROFILE_TEXT_LIMITS.items():
        value = _check_text(row.get(key), limit, f"音色描述 {key}")
        if value:
            profile[key] = value
    for key in ("timbre", "temperament", "negative_prompt", "evidence"):
        items = _clean_str_list(row.get(key), 60, f"音色描述 {key}")
        if items:
            profile[key] = items
    gender = _normalize_choice(row.get("gender"), GENDER_OPTIONS)
    if gender:
        profile["gender"] = gender
    confidence = row.get("confidence")
    if isinstance(confidence, (int, float)) and 0 <= confidence <= 1:
        profile["confidence"] = float(confidence)
    return profile or None

def _attach_profile_source(character: dict, profile: dict) -> dict:
    """补齐音色描述的元数据（schema/summary/来源哈希/状态）。"""
    from ...store.voice_cast import profile_source_hash

    profile["schema"] = "voice_profile/1"
    profile.setdefault("summary", profile.get("voice_summary"))
    profile["source"] = {"kind": "llm", "at": now_iso(), "from_hash": profile_source_hash(character)}
    profile["status"] = "ready"
    return profile

def _stock_voice_slot(gender: str | None, age_range: str | None) -> str:
    """按性别与年龄感给出音色池槽位。"""
    age = str(age_range or "")
    elder = any(word in age for word in ("60", "70", "80", "老"))
    young = any(word in age for word in ("16", "17", "18", "19", "20", "21", "22", "少年", "少女"))
    if gender == "female":
        return "female_elder" if elder else ("female_young" if young else "female_mid")
    if gender == "male":
        return "male_elder" if elder else ("male_young" if young else "male_mid")
    return "neutral_mid"

def _clean_stock(row) -> dict | None:
    """清洗龙套槽位；非法槽位返回 None（由调用方决定是否降级）。"""
    if not isinstance(row, dict):
        return None
    label = _check_text(row.get("label"), 40, "龙套职业名", required=True)
    slot = str(row.get("slot") or "").strip().lower()
    if not re.match(r"^[a-z][a-z0-9_]{1,39}$", slot):
        return None
    return {
        "slot": slot,
        "voice_slot": str(row.get("voice_slot") or "").strip() or None,
        "label": label,
        "archetype_tags": _clean_str_list(row.get("archetype_tags"), 20, f"龙套 {label} 的标签"),
        "reuse_policy": "reuse",
    }

def _stock_display_name(name: str, label: str) -> str:
    """统一龙套展示名：`龙套·<职业>`。"""
    if name.startswith("龙套"):
        return name
    return f"龙套·{label}"

def _build_stock_characters(rows: list[dict], seen: set[str]) -> list[dict]:
    """把 LLM 的 stock_characters 清洗成角色实体（同槽位只保留第一条）。

    参数:
        rows: 模型返回的龙套条目。
        seen: 已占用的人物姓名集合（会被更新）。

    返回:
        角色 dict 列表。
    """
    built: list[dict] = []
    used_slots: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        stock = _clean_stock(row.get("stock"))
        if not stock or stock["slot"] in used_slots:
            continue
        used_slots.add(stock["slot"])
        name = _stock_display_name(str(row.get("name") or "").strip(), stock["label"])
        if name in seen:
            continue
        seen.add(name)
        gender = _normalize_choice(row.get("gender"), GENDER_OPTIONS) or "unknown"
        stock["voice_slot"] = stock.get("voice_slot") or _stock_voice_slot(gender, row.get("age_range"))
        character = {
            "name": name,
            "role": "minor",
            "gender": gender,
            "age_range": _clean_age_range(row.get("age_range"), f"龙套 {name}"),
            "personality": _clean_str_list(row.get("personality"), 120, f"龙套 {name} 的性格"),
            "background": _check_text(row.get("background"), 1200, f"龙套 {name} 的背景"),
            "appearance_prompt": _check_text(row.get("appearance_prompt"), 1200, f"龙套 {name} 的外貌提示词"),
            "negative_prompt": _check_text(row.get("negative_prompt"), 800, f"龙套 {name} 的负向提示词"),
            "relationships": {},
            "stock": stock,
        }
        profile = _clean_voice_profile(row.get("voice_profile"))
        if profile:
            character["voice_profile"] = _attach_profile_source(character, profile)
        built.append(character)
    return built

def _default_stock_characters() -> list[dict]:
    """LLM 未产出龙套时的默认 8 个职业槽位（无音色描述，由校验提醒补齐）。"""
    return [
        {
            "name": f"龙套·{label}",
            "role": "minor",
            "gender": "female" if voice_slot.startswith("female") else ("male" if voice_slot.startswith("male") else "unknown"),
            "age_range": None,
            "personality": [],
            "background": None,
            "appearance_prompt": None,
            "negative_prompt": None,
            "relationships": {},
            "stock": {
                "slot": slot,
                "voice_slot": voice_slot,
                "label": label,
                "archetype_tags": [],
                "reuse_policy": "reuse",
            },
        }
        for slot, label, voice_slot in _DEFAULT_STOCK_SLOTS
    ]

def _clean_character_entry(row: dict) -> dict:
    """清洗单个完整人物（含配图提示词与关系备注）。

    参数:
        row: 模型返回的人物对象。

    返回:
        {name, role, gender, age_range, personality, background, appearance_prompt,
        negative_prompt, state.relationship_notes}。

    异常:
        ValueError: 类型或字段长度不合法。
    """
    if not isinstance(row, dict):
        raise ValueError("人物必须是 JSON 对象")
    name = _check_text(row.get("name"), 60, "人物姓名", required=True)
    role = _normalize_choice(row.get("role"), ROLE_OPTIONS) or "supporting"
    gender = _normalize_choice(row.get("gender"), GENDER_OPTIONS) or "unknown"
    state = row.get("state") if isinstance(row.get("state"), dict) else {}
    notes = state.get("relationship_notes")
    if not isinstance(notes, dict):
        notes = row.get("relationships") if isinstance(row.get("relationships"), dict) else {}
    entry = {
        "name": name,
        "role": role,
        "gender": gender,
        "age_range": _clean_age_range(row.get("age_range"), f"人物 {name}"),
        "personality": _clean_str_list(row.get("personality"), 120, f"人物 {name} 的性格"),
        "background": _check_text(row.get("background"), 2000, f"人物 {name} 的背景"),
        "speech_style": _clean_speech_style(row.get("speech_style")),
        "appearance_prompt": _check_text(row.get("appearance_prompt"), 2000, f"人物 {name} 的外貌提示词"),
        "negative_prompt": _check_text(row.get("negative_prompt"), 1000, f"人物 {name} 的负向提示词"),
        "voice_description_zh": _check_text(row.get("voice_description_zh"), 1000, f"人物 {name} 的中文音色描述"),
        "voice_description_en": _check_text(row.get("voice_description_en"), 1000, f"人物 {name} 的英文音色描述"),
        "appearance_desc_zh": _check_text(row.get("appearance_desc_zh"), 1200, f"人物 {name} 的中文外貌描述"),
        "appearance_desc_en": _check_text(row.get("appearance_desc_en"), 1200, f"人物 {name} 的英文外貌描述"),
        "appearance_negative_zh": _check_text(row.get("appearance_negative_zh"), 800, f"人物 {name} 的中文负向说明"),
        "appearance_negative_en": _check_text(row.get("appearance_negative_en"), 800, f"人物 {name} 的英文负向提示词"),
        "state": {
            "relationship_notes": {
                _check_text(key, 60, f"人物 {name} 的关系对象", required=True): _check_text(note, 500, f"人物 {name} 的关系备注")
                for key, note in notes.items()
            }
        },
    }
    # 新旧字段兼容：已有消费端只看 appearance_prompt / negative_prompt
    if not entry["appearance_prompt"] and entry["appearance_desc_en"]:
        entry["appearance_prompt"] = entry["appearance_desc_en"]
    if not entry["negative_prompt"] and entry["appearance_negative_en"]:
        entry["negative_prompt"] = entry["appearance_negative_en"]
    return entry

_VOICE_PROFILE_TEXT_LIMITS = {
    "voice_summary": 300,
    "age_range": 40,
    "age_feel": 20,
    "pitch": 20,
    "pace": 20,
    "volume": 20,
    "energy": 30,
    "warmth": 30,
    "articulation": 30,
    "accent": 60,
    "design_prompt": 1200,
}

#: 龙套默认槽位（LLM 未产出时的兜底；职业槽位与音色池分离）
_DEFAULT_STOCK_SLOTS = (
    ("waiter", "店小二", "male_mid"),
    ("guard", "衙役", "male_mid"),
    ("runner", "小厮", "male_young"),
    ("passerby_m", "路人男", "male_mid"),
    ("passerby_f", "路人女", "female_mid"),
    ("elder_m", "老者", "male_elder"),
    ("elder_f", "老妇", "female_elder"),
    ("girl", "少女", "female_young"),
)

#: 人物描述性字段的统一要求（人物生成 / 逐人生成 / 单人物编辑共用）
_CHARACTER_DESC_RULES = (
    "除基础设定外，必须一次给出后续可直接使用的描述性字段（缺一不可）：\n"
    "1) voice_description_zh：中文音色描述，覆盖音色质感、语速、语气、年龄感、口音，80-160 字，能直接指导 TTS；\n"
    "2) voice_description_en：同一音色的英文描述，必须是可直接用于语音模型 prompt 的英文，不得夹杂中文，"
    "覆盖 timbre、pace、tone、age feel、accent；\n"
    "3) appearance_desc_zh：中文外貌描述（脸型、五官、发型、身形、惯常着装与气质），供界面展示与人工校对；\n"
    "4) appearance_desc_en：英文正面出图 prompt，纯英文，写具体可见特征（发型长度、眉眼、肤色、服装材质等），"
    "禁止只写 young woman、beautiful 这类空泛描述；\n"
    "5) appearance_negative_zh：中文负向说明，用名词短语列出出图应排除的元素（如眼镜、胡须、伤疤），"
    "不要写成否定句堆砌；\n"
    "6) appearance_negative_en：英文 negative prompt，逗号分隔的排除项（如 glasses, beard, scar），"
    "不要写 not/no 句式；\n"
    "7) speech_style：写作时直接进提示词的口吻约束，必须给 tempo（语速，只能取 slow/medium/fast）、"
    "pitch（音高，只能取 low/medium/high）、accent（口音或用词习惯）、catchphrases（1-3 句口头禅）；"
    "四个人物之间的语速、用词和口头禅要能明显区分，不能千篇一律。"
)
