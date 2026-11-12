"""提示词适配层：运行时只补时长后缀，不再做中→英翻译。

Stable Audio 3 的文本编码器（t5gemma）以英文语料为主，官方示例与
ComfyUI 蓝图都要求英文描述，并在末尾显式写 `Length: N seconds`。

双语在**生成阶段**完成：标注链路（`!sfx ... 中文 || English prompt`）同时产出
中文描述与英文提示词，分别存进 `prompt` / `prompt_en`。运行时只读取
`prompt_en` 并追加时长后缀（`finalize_prompt`），缺英文直接报错。

`rewrite_prompt` 与字典仅保留为显式调试选项（CLI `--rewrite-prompt`），
不参与页面与常规生成链路。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .types import BAD_REQUEST, SfxError

#: 含中日韩统一表意文字即视为中文输入
_CJK = re.compile(r"[\u4e00-\u9fff]")

#: LLM 改写用的系统提示（要求只输出英文提示词，并写清时长）
LLM_SYSTEM = (
    "You rewrite Chinese sound-effect descriptions into ONE concise English prompt "
    "for a text-to-audio model. Output only the English prompt, no quotes, no explanation. "
    "Name the sound sources, materials, actions and acoustics; keep it under 40 words. "
    "Always end with 'Length: N seconds' matching the requested duration."
)
DEFAULT_LLM_TIMEOUT_S = 20.0


@dataclass
class RewriteOutcome:
    """一次改写的最终结果。"""

    prompt: str
    changed: bool
    mode: str
    note: str | None = None


#: 中文音效描述 → 英文短语。匹配时按长度从长到短，避免「关门」吃掉「猛地关门」
LEXICON: dict[str, str] = {
    # 材质 / 物体
    "木门": "wooden door",
    "铁门": "metal door",
    "车门": "car door",
    "玻璃": "glass",
    "陶瓷": "ceramic",
    "金属": "metal",
    "铁皮": "tin",
    "屋顶": "roof",
    "纸张": "paper",
    "布料": "cloth",
    "丝绸": "silk",
    "木头": "wood",
    "石块": "stone",
    "骨头": "bone",
    "硬币": "coins",
    "齿轮": "gears",
    "弹簧": "spring",
    "键盘": "keyboard",
    "相机": "camera",
    "快门": "shutter",
    "电话": "telephone",
    "钟表": "clock",
    "秒针": "clock ticking",
    "拉链": "zipper",
    "开关": "switch",
    "引擎": "engine",
    "刹车": "brakes",
    "轮胎": "tires",
    "飞船": "spaceship",
    "无人机": "drone",
    # 动作 / 事件
    "猛地关上": "slams shut",
    "重重关上": "slams shut heavily",
    "关上": "closes",
    "开门": "door opens",
    "关门": "door closes",
    "锁门": "lock clicks shut",
    "敲击": "knocking",
    "敲打": "knocking",
    "碎裂": "shatters",
    "破碎": "breaking",
    "倒塌": "collapses",
    "爆炸": "explosion",
    "枪声": "gunshot",
    "开枪": "gunshot",
    "枪": "gun",
    "撞击": "impact",
    "碰撞": "collision",
    "摩擦": "friction",
    "刮擦": "scraping",
    "旋转": "spinning",
    "启动": "starting up",
    "关闭": "shutting down",
    "警报": "alarm",
    "铃声": "ringing",
    "响起": "sounds",
    "发出": "producing",
    "打在": "hitting",
    "打": "hitting",
    # 自然 / 环境
    "雨点": "raindrops",
    "大雨": "heavy rain",
    "小雨": "light rain",
    "雨": "rain",
    "雷声": "thunder",
    "雷": "thunder",
    "风声": "wind",
    "风": "wind",
    "海浪": "ocean waves",
    "波浪": "waves",
    "水滴": "water drops",
    "滴水": "dripping water",
    "水流": "running water",
    "火": "fire",
    "燃烧": "burning",
    "森林": "forest",
    "树叶": "leaves",
    "鸟鸣": "birds chirping",
    "虫鸣": "insects chirping",
    "雪": "snow",
    "洞穴": "cave",
    "沙滩": "beach",
    # 人类 / 生物
    "脚步声": "footsteps",
    "脚步": "footsteps",
    "心跳": "heartbeat",
    "呼吸": "breathing",
    "笑声": "laughter",
    "哭声": "crying",
    "尖叫": "scream",
    "鼓掌": "applause",
    "人群": "crowd",
    "马蹄": "hooves",
    "拳击": "punch",
    "打击": "strike",
    # 城市 / 机械 / 科幻
    "城市": "city",
    "街道": "street",
    "汽车": "car",
    "电流": "electricity",
    "机械": "machinery",
    "激光": "laser",
    "能量": "energy",
    "魔法": "magic",
    "霓虹": "neon hum",
    "日光灯": "fluorescent light hum",
    # 修饰语
    "沉重的": "heavy",
    "沉重": "heavy",
    "低沉的": "deep low-frequency",
    "低沉": "deep low-frequency",
    "密集地": "densely",
    "密集": "dense",
    "连续": "continuous",
    "突然": "sudden",
    "远处": "distant",
    "缓慢": "slow",
    "快速": "fast",
    "逐渐": "gradually",
    "猛烈": "violent",
    "轻微": "subtle",
    "清脆": "crisp",
    "尖锐": "sharp",
    "温暖": "warm",
    "阴森": "eerie",
    # 量词 / 结构助词（丢弃，不产生英文）
    "一阵": "",
    "一声": "",
    "一个": "",
    "的": "",
    "地": "",
    "被": "",
    "在": "",
    "上": "",
    "下": "",
    "中": "",
    "里": "",
    "和": "",
    "与": "",
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


def rewrite(prompt: str, duration_s: float) -> tuple[str, bool]:
    """把描述改写成英文并追加时长后缀。

    参数:
        prompt: 原始描述（中文或英文）。
        duration_s: 目标时长（秒），写进 `Length: N seconds`。

    返回:
        (最终提示词, 是否做过中文改写)。

    异常:
        SfxError(BAD_REQUEST): 输入为中文但一个词都没命中（无法可靠改写）。
    """
    text = (prompt or "").strip()
    if not text:
        raise SfxError(BAD_REQUEST, "音效描述不能为空")
    text = text.rstrip(" .;,，。；")
    suffix = f"Length: {duration_s:g} seconds"
    if not has_cjk(text):
        return f"{text}. {suffix}", False

    words: list[str] = []
    matched = 0
    i = 0
    while i < len(text):
        hit = None
        for key in _KEYS:
            if text.startswith(key, i):
                hit = key
                break
        if hit is None:
            # 未命中的字符（中文漏词或标点）跳过，不打断整句
            i += 1
            continue
        phrase = LEXICON[hit]
        if phrase and (not words or words[-1] != phrase):
            words.append(phrase)
        matched += 1
        i += len(hit)

    if matched == 0:
        raise SfxError(
            BAD_REQUEST,
            f"中文描述没有命中任何音效词条，无法改写：{text!r}",
            hint="换用更通用的描述（如「木门 关上」），或直接用 --prompt-en 给英文",
        )
    return f"{', '.join(words)}. {suffix}", True


def _ensure_length(prompt: str, duration_s: float) -> str:
    """去掉模型自带的时长尾巴，再追加项目统一的 `Length: N seconds`。

    参数:
        prompt: 待处理的英文提示词。
        duration_s: 目标时长（秒）。

    返回:
        以时长后缀结尾的提示词。
    """
    cleaned = re.sub(r"(?i)[.。,]?\s*length\s*[:：]\s*[\d.]+\s*(?:seconds?|s)\b\.?\s*$", "", prompt).strip()
    cleaned = cleaned.rstrip(" .;,，。；")
    return f"{cleaned}. Length: {duration_s:g} seconds"


def _llm_settings(config, timeout_s: float | None = None) -> dict | None:
    """从配置里取出可用的 LLM 设置（未配置端点/模型时返回 None）。

    参数:
        config: AppConfig。
        timeout_s: 覆盖单次请求超时；None 用 `sfx.prompt.llm_timeout_s`。

    返回:
        omlx 设置字典（已覆盖短超时与低温度）；不可用时返回 None。
    """
    settings = dict(config.get("omlx") or {})
    if not str(settings.get("endpoint") or "").strip() or not str(settings.get("model") or "").strip():
        return None
    settings["request_timeout_s"] = float(
        timeout_s if timeout_s is not None else (config.get("sfx.prompt.llm_timeout_s") or DEFAULT_LLM_TIMEOUT_S)
    )
    # 改写是短任务：低温度 + 关思考，避免几十秒的推理
    settings["temperature"] = 0.2
    settings["max_tokens"] = 220
    settings["enable_thinking"] = False
    return settings


def _llm_rewrite(prompt: str, duration_s: float, settings: dict) -> str:
    """调用本地 LLM 把中文描述改写成英文提示词。

    参数:
        prompt: 中文（或中英混合）描述。
        duration_s: 目标时长（秒）。
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
        task="sfx_prompt",
        system=LLM_SYSTEM,
        messages=[Message(role="user", content=f"中文描述：{prompt}\n目标时长：{duration_s:g} 秒")],
        max_tokens=220,
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
    if len(text) > 400:
        text = text[:400]
    return _ensure_length(text, duration_s)


def finalize_prompt(prompt: str, duration_s: float) -> str:
    """给已存英文提示词补上项目统一的时长后缀（不做翻译或改写）。

    参数:
        prompt: 标注阶段已落盘的英文提示词。
        duration_s: 目标时长（秒）。

    返回:
        以 `Length: N seconds` 结尾的提示词。

    异常:
        SfxError(BAD_REQUEST): 提示词为空。
    """
    text = (prompt or "").strip()
    if not text:
        raise SfxError(BAD_REQUEST, "英文提示词为空：请在标注里重新生成描述，或在媒体页手动填写英文")
    return _ensure_length(text, duration_s)


def rewrite_prompt(
    prompt: str,
    duration_s: float,
    config=None,
    allow_llm: bool = True,
    timeout_s: float | None = None,
) -> RewriteOutcome:
    """把描述改写成英文并追加时长后缀（LLM 优先、词典兜底）。

    参数:
        prompt: 原始描述（中文或英文）。
        duration_s: 目标时长（秒）。
        config: AppConfig；None 时视为未配置 LLM，直接走词典。
        allow_llm: False 时强制走词典（对应 CLI 的 `--no-llm`）。
        timeout_s: LLM 单次请求超时覆盖值。

    返回:
        RewriteOutcome：最终提示词、是否改写、走的哪条路线、降级提示。

    异常:
        SfxError(BAD_REQUEST): `llm` 模式失败，或中文描述完全无法改写。
    """
    text = (prompt or "").strip()
    if not text:
        raise SfxError(BAD_REQUEST, "音效描述不能为空")
    if not has_cjk(text):
        # 英文输入原样透传，只补时长后缀
        return RewriteOutcome(_ensure_length(text, duration_s), False, "passthrough")

    mode = str((config.get("sfx.prompt.rewrite") if config is not None else "dict") or "auto").lower()
    settings = _llm_settings(config, timeout_s) if (config is not None and allow_llm) else None

    if mode == "dict":
        final, _ = rewrite(text, duration_s)
        return RewriteOutcome(final, True, "dict", "按配置使用词典改写（未调用 LLM）")

    if mode == "llm" and settings is None:
        raise SfxError(BAD_REQUEST, "sfx.prompt.rewrite=llm，但 omlx 未配置地址或模型")

    if settings is not None:
        try:
            return RewriteOutcome(_llm_rewrite(text, duration_s, settings), True, "llm")
        except SfxError:
            raise
        except Exception as exc:  # noqa: BLE001 - 网络/协议/内容问题统一转降级或报错
            if mode == "llm":
                raise SfxError(BAD_REQUEST, f"LLM 改写失败：{exc}", hint="检查 omlx 服务，或改用 --no-llm 走词典") from exc
            note = f"LLM 改写不可用（{type(exc).__name__}: {exc}），已降级为词典改写"
            final, _ = rewrite(text, duration_s)
            return RewriteOutcome(final, True, "dict", note)

    final, _ = rewrite(text, duration_s)
    return RewriteOutcome(final, True, "dict")
