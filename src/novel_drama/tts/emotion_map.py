"""项目 10 情绪 → IndexTTS 2.5 的 8 维情绪向量映射。

项目侧情绪在 `schema/line.schema.json` 固定为 10 个：
`neutral / joy / sad / angry / fear / surprise / tender / excited / whisper / serious`。

IndexTTS 2.5 官方只认 8 维，顺序固定为
`[happy, angry, sad, afraid, disgusted, melancholic, surprised, calm]`
（官方 README.md:87-95）。两边不是同一套枚举，必须显式翻译。

本模块只做「确定的单向映射」，不做加权猜测：
- `neutral` 一律全 0，等于不做情绪控制，也不传 `emo_alpha`；
- 官方没有的 `tender / whisper / serious` 落到 `calm`（最接近），原始名保留在日志里；
- `excited` 落 `happy`，但把 `emo_alpha` 抬到 0.9 以上，避免和 `joy` 听起来一样。
"""

from __future__ import annotations

# 官方 8 维顺序（不可改，改了向量就错位）
EMO_ORDER = ["happy", "angry", "sad", "afraid", "disgusted", "melancholic", "surprised", "calm"]

# 项目情绪 → 8 维里的目标维度；None 表示不做情绪控制
PROJECT_TO_EMO: dict[str, str | None] = {
    "neutral": None,
    "joy": "happy",
    "excited": "happy",
    "sad": "sad",
    "angry": "angry",
    "fear": "afraid",
    "surprise": "surprised",
    "tender": "calm",
    "whisper": "calm",
    "serious": "calm",
    # 允许直接写官方 8 维名（CLI 联调时方便，不需要再套一层项目枚举）
    "happy": "happy",
    "afraid": "afraid",
    "disgusted": "disgusted",
    "melancholic": "melancholic",
    "surprised": "surprised",
    "calm": "calm",
}

# 需要把强度拉到接近满格的情绪
SATURATED_EMOTIONS = {"excited"}

# 中文别名（CLI/手工标注里常用），统一折到项目情绪名
CHINESE_ALIASES: dict[str, str] = {
    "中性": "neutral",
    "开心": "joy",
    "高兴": "joy",
    "兴奋": "excited",
    "悲伤": "sad",
    "难过": "sad",
    "愤怒": "angry",
    "生气": "angry",
    "恐惧": "fear",
    "害怕": "fear",
    "惊讶": "surprise",
    "吃惊": "surprise",
    "温柔": "tender",
    "低语": "whisper",
    "耳语": "whisper",
    "严肃": "serious",
}

# 强度上限，与 Requirement.emotion_intensity 的 0~3 对齐
INTENSITY_MAX = 3


def normalize(emotion: str | None) -> str:
    """把情绪名归一化成小写英文名（支持中文别名）。

    参数:
        emotion: 项目情绪名、官方 8 维名或中文别名。

    返回:
        归一化后的名字；空值返回空串。
    """
    name = (emotion or "").strip()
    if not name:
        return ""
    if name in CHINESE_ALIASES:
        return CHINESE_ALIASES[name]
    return name.lower()


def to_index_emotion(emotion: str | None) -> str | None:
    """把项目情绪名翻成 8 维里的维度名。

    参数:
        emotion: 项目情绪名或官方 8 维名，大小写不敏感。

    返回:
        目标维度名；`neutral`/空值/未知情绪返回 None。

    说明:
        未知情绪不抛错，由调用方决定是否报错；provider 侧会先用 `is_supported`
        判断是否需要给出可用清单。
    """
    name = normalize(emotion)
    if not name:
        return None
    return PROJECT_TO_EMO.get(name)


def is_supported(emotion: str | None) -> bool:
    """判断情绪名是否能被映射（含 `neutral`）。

    参数:
        emotion: 项目情绪名或官方 8 维名。

    返回:
        能识别返回 True；空值与未知名返回 False。
    """
    name = normalize(emotion)
    return bool(name) and name in PROJECT_TO_EMO


def build_vector(emotion: str | None, intensity: int = 0) -> list[float] | None:
    """构造 8 维 one-hot 情绪向量。

    参数:
        emotion: 项目情绪名或官方 8 维名。
        intensity: 情绪强度 0~3，目前不影响向量形状，只影响 `emo_alpha`。

    返回:
        长度 8 的浮点列表；`neutral` 返回 None（表示不传 `emo_vector`）。
    """
    target = to_index_emotion(emotion)
    if target is None:
        return None
    vec = [0.0] * len(EMO_ORDER)
    vec[EMO_ORDER.index(target)] = 1.0
    return vec


def build_alpha(emotion: str | None, intensity: int = 0) -> float | None:
    """由强度档位推导官方 `emo_alpha`。

    参数:
        emotion: 项目情绪名或官方 8 维名。
        intensity: 情绪强度 0~3；0 表示「按默认半强度」。

    返回:
        0.5~1.0 的浮点；`neutral` 返回 None（不传该参数）。
    """
    target = to_index_emotion(emotion)
    if target is None:
        return None
    level = max(0, min(int(intensity or 0), INTENSITY_MAX))
    alpha = 0.5 + 0.5 * (level / INTENSITY_MAX)
    name = normalize(emotion)
    if name in SATURATED_EMOTIONS:
        alpha = max(alpha, 0.9)
    return round(alpha, 3)


def describe(emotion: str | None, intensity: int = 0) -> str:
    """生成一行可读说明，用于日志与侧车留痕。

    参数:
        emotion: 项目情绪名或官方 8 维名。
        intensity: 情绪强度 0~3。

    返回:
        形如 `excited→happy(alpha=0.900)` 或 `neutral(无情绪控制)` 的字符串。
    """
    target = to_index_emotion(emotion)
    if target is None:
        return "neutral(无情绪控制)"
    name = normalize(emotion)
    alpha = build_alpha(name, intensity)
    if name == target:
        return f"{target}(alpha={alpha:.3f})"
    return f"{name}→{target}(alpha={alpha:.3f})"


def available_emotions() -> list[str]:
    """返回项目侧可用情绪清单（按映射表顺序）。

    返回:
        情绪名列表，供错误提示与文档使用。
    """
    return list(PROJECT_TO_EMO.keys())
