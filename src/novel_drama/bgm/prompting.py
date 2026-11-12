"""中文配乐描述适配层：LLM 优先、词典兜底。

Stable Audio Open 1.0 的文本编码器是 t5-base（英文语料），官方示例都用
英文描述，因此本地 provider 需要先把中文音乐描述改写成英文短句。

改写策略（`bgm.prompt.rewrite`）：
- `auto`（默认）：LLM 可达就用 LLM，不可达/超时/内容不合格则降级词典；
- `llm`：只用 LLM，失败直接报错；
- `dict`：不调用 LLM，只用最长匹配词典。

词典只覆盖常用配乐词（风格 / 情绪 / 乐器 / 织体），长尾描述会漏词；
漏词不影响可读性但会丢细节，必要时用 `--prompt-en` 直接给英文。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .types import BAD_REQUEST, BgmError

#: 含中日韩统一表意文字即视为中文输入
_CJK = re.compile(r"[\u4e00-\u9fff]")

#: LLM 改写用的系统提示（只要英文提示词，不写时长后缀——时长由模型条件控制）
LLM_SYSTEM = (
    "You rewrite Chinese background-music descriptions into ONE concise English prompt "
    "for a text-to-music model. Output only the English prompt, no quotes, no explanation. "
    "Describe genre, mood, instrumentation, tempo and production; avoid lyrics and vocals "
    "unless explicitly requested; keep it under 45 words."
)
DEFAULT_LLM_TIMEOUT_S = 20.0


@dataclass
class RewriteOutcome:
    """一次改写的最终结果。"""

    prompt: str
    changed: bool
    mode: str
    note: str | None = None


#: 中文配乐描述 → 英文短语。匹配按长度从长到短，避免「弦乐」吃掉「弦乐铺底」
LEXICON: dict[str, str] = {
    # 风格 / 类型
    "史诗": "epic",
    "宏大": "grandiose",
    "电影感": "cinematic",
    "影视配乐": "cinematic score",
    "预告片": "trailer music",
    "交响": "orchestral",
    "管弦乐": "orchestral",
    "室内乐": "chamber music",
    "中国风": "Chinese traditional",
    "民乐": "traditional Chinese instruments",
    "古风": "ancient Chinese style",
    "爵士": "jazz",
    "轻爵士": "smooth jazz",
    "电子": "electronic",
    "氛围电子": "ambient electronic",
    "合成器": "synthesizer",
    "低保真": "lo-fi",
    "摇滚": "rock",
    "民谣": "folk",
    "钢琴曲": "solo piano",
    "摇篮曲": "lullaby",
    "圆舞曲": "waltz",
    # 情绪 / 氛围
    "悬疑": "suspenseful",
    "紧张": "tense",
    "压迫": "oppressive",
    "不安": "uneasy",
    "恐怖": "eerie",
    "阴森": "eerie",
    "悲伤": "melancholic",
    "忧伤": "sorrowful",
    "孤独": "lonely",
    "温柔": "gentle",
    "温暖": "warm",
    "治愈": "soothing",
    "浪漫": "romantic",
    "甜蜜": "sweet",
    "欢快": "upbeat",
    "明亮": "bright",
    "希望": "hopeful",
    "壮阔": "majestic",
    "神秘": "mysterious",
    "梦幻": "dreamy",
    "空灵": "ethereal",
    "宁静": "calm",
    "平静": "peaceful",
    "慵懒": "laid-back",
    "怀旧": "nostalgic",
    "肃杀": "grim",
    "激烈": "intense",
    "紧迫": "urgent",
    # 乐器
    "弦乐": "strings",
    "大提琴": "cello",
    "小提琴": "violin",
    "中提琴": "viola",
    "低音提琴": "double bass",
    "钢琴": "piano",
    "电钢琴": "electric piano",
    "吉他": "guitar",
    "原声吉他": "acoustic guitar",
    "电吉他": "electric guitar",
    "竖琴": "harp",
    "长笛": "flute",
    "单簧管": "clarinet",
    "双簧管": "oboe",
    "小号": "trumpet",
    "圆号": "french horn",
    "长号": "trombone",
    "铜管": "brass",
    "木管": "woodwinds",
    "鼓": "drums",
    "太鼓": "taiko drums",
    "定音鼓": "timpani",
    "打击乐": "percussion",
    "贝斯": "bass",
    "低音": "low bass",
    "二胡": "erhu",
    "古筝": "guzheng",
    "琵琶": "pipa",
    "笛子": "bamboo flute",
    "箫": "xiao flute",
    "人声吟唱": "wordless vocal",
    "合唱": "choir",
    # 织体 / 节奏 / 制作
    "铺底": "sustained bed",
    "长音": "sustained tones",
    "持续低音": "sustained drone",
    "渐强": "building up",
    "渐弱": "fading down",
    "缓慢": "slow",
    "中速": "mid-tempo",
    "快速": "fast",
    "节奏": "rhythmic",
    "律动": "groove",
    "循环": "looping",
    "简约": "minimal",
    "极简": "minimalist",
    "层次": "layered",
    "混响": "reverb",
    "宽声场": "wide stereo",
    "低调度": "understated",
    "无人声": "instrumental, no vocals",
    "纯音乐": "instrumental",
}

#: 预排序的词典键（长词优先）
_KEYS = sorted(LEXICON, key=len, reverse=True)


def has_cjk(text: str) -> bool:
    """判断文本是否含中文。

    参数:
        text: 待检测文本。

    返回:
        True 表示含中日韩统一表意文字。
    """
    return bool(_CJK.search(text or ""))


def rewrite(prompt: str) -> tuple[str, bool]:
    """用词典把中文描述改写成英文短语。

    参数:
        prompt: 原始描述（中文或英文）。

    返回:
        (最终提示词, 是否做过中文改写)。

    异常:
        BgmError(BAD_REQUEST): 输入为中文但一个词都没命中。
    """
    text = (prompt or "").strip()
    if not text:
        raise BgmError(BAD_REQUEST, "配乐描述不能为空")
    text = text.rstrip(" .;,，。；")
    if not has_cjk(text):
        return text, False

    words: list[str] = []
    matched = 0
    index = 0
    while index < len(text):
        hit = None
        for key in _KEYS:
            if text.startswith(key, index):
                hit = key
                break
        if hit is None:
            index += 1
            continue
        phrase = LEXICON[hit]
        if phrase and (not words or words[-1] != phrase):
            words.append(phrase)
        matched += 1
        index += len(hit)

    if matched == 0:
        raise BgmError(
            BAD_REQUEST,
            f"中文配乐描述没有命中任何词条，无法改写：{text!r}",
            hint="换用更通用的描述（如「钢琴 缓慢 悲伤」），或直接用 --prompt-en 给英文",
        )
    return ", ".join(words), True


def _llm_settings(config, timeout_s: float | None = None) -> dict | None:
    """从配置里取出可用的 LLM 设置（未配置端点/模型时返回 None）。

    参数:
        config: AppConfig。
        timeout_s: 覆盖单次请求超时；None 用 `bgm.prompt.llm_timeout_s`。

    返回:
        omlx 设置字典（已覆盖短超时与低温度）；不可用时返回 None。
    """
    settings = dict(config.get("omlx") or {})
    if not str(settings.get("endpoint") or "").strip() or not str(settings.get("model") or "").strip():
        return None
    settings["request_timeout_s"] = float(
        timeout_s if timeout_s is not None else (config.get("bgm.prompt.llm_timeout_s") or DEFAULT_LLM_TIMEOUT_S)
    )
    settings["temperature"] = 0.2
    settings["max_tokens"] = 200
    settings["enable_thinking"] = False
    return settings


def _llm_rewrite(prompt: str, settings: dict) -> str:
    """调用本地 LLM 把中文描述改写成英文配乐提示词。

    参数:
        prompt: 中文（或中英混合）描述。
        settings: `_llm_settings` 的返回值。

    返回:
        规范化后的英文提示词。

    异常:
        Exception: 网络/协议/内容不合格都直接抛出，由上层决定降级。
    """
    from ..adapters.base import Message, TextGenRequest
    from ..adapters.writing import OmlxWritingAdapter

    adapter = OmlxWritingAdapter(settings)
    request = TextGenRequest(
        task="bgm_prompt",
        system=LLM_SYSTEM,
        messages=[Message(role="user", content=f"中文配乐描述：{prompt}")],
        max_tokens=200,
        temperature=0.2,
        stream=False,
    )
    text = (adapter.generate(request).text or "").strip()
    # 去掉代码块围栏与首尾引号，只保留第一段有效文本
    text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text).strip().strip('"').strip("'").strip()
    text = next((line.strip() for line in text.splitlines() if line.strip()), "")
    if not text:
        raise ValueError("LLM 返回空内容")
    if len(_CJK.findall(text)) > 2:
        raise ValueError(f"LLM 返回内容仍是中文：{text[:80]!r}")
    return text[:400]


def rewrite_prompt(
    prompt: str,
    config=None,
    allow_llm: bool = True,
    timeout_s: float | None = None,
) -> RewriteOutcome:
    """把描述改写成英文（LLM 优先、词典兜底）。

    参数:
        prompt: 原始描述（中文或英文）。
        config: AppConfig；None 时视为未配置 LLM，直接走词典。
        allow_llm: False 时强制走词典（对应 CLI 的 `--no-llm`）。
        timeout_s: LLM 单次请求超时覆盖值。

    返回:
        RewriteOutcome：最终提示词、是否改写、走的哪条路线、降级提示。

    异常:
        BgmError(BAD_REQUEST): `llm` 模式失败，或中文描述完全无法改写。
    """
    text = (prompt or "").strip()
    if not text:
        raise BgmError(BAD_REQUEST, "配乐描述不能为空")
    if not has_cjk(text):
        # 英文输入原样透传
        return RewriteOutcome(text.rstrip(" .;,，。；"), False, "passthrough")

    mode = str((config.get("bgm.prompt.rewrite") if config is not None else "dict") or "auto").lower()
    settings = _llm_settings(config, timeout_s) if (config is not None and allow_llm) else None

    if mode == "dict":
        final, _ = rewrite(text)
        return RewriteOutcome(final, True, "dict", "按配置使用词典改写（未调用 LLM）")

    if mode == "llm" and settings is None:
        raise BgmError(BAD_REQUEST, "bgm.prompt.rewrite=llm，但 omlx 未配置地址或模型")

    if settings is not None:
        try:
            return RewriteOutcome(_llm_rewrite(text, settings), True, "llm")
        except BgmError:
            raise
        except Exception as exc:  # noqa: BLE001 - 网络/协议/内容问题统一转降级或报错
            if mode == "llm":
                raise BgmError(BAD_REQUEST, f"LLM 改写失败：{exc}", hint="检查 omlx 服务，或改用 --no-llm 走词典") from exc
            note = f"LLM 改写不可用（{type(exc).__name__}: {exc}），已降级为词典改写"
            final, _ = rewrite(text)
            return RewriteOutcome(final, True, "dict", note)

    final, _ = rewrite(text)
    return RewriteOutcome(final, True, "dict")
