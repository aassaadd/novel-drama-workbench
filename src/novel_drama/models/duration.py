"""时长估算：未合成时用字数与语速粗估，合成后必须替换为实测值。"""

from __future__ import annotations

# 中文有声书常见语速：约 5 字/秒（含自然停顿）
DEFAULT_CHARS_PER_SECOND = 5.0

# 句读带来的额外停顿（毫秒）
_PAUSE_RULES = {"。": 180, "！": 200, "？": 200, "；": 120, "，": 60, "、": 40}


def estimate_duration_ms(text: str, speed: float = 1.0, chars_per_second: float = DEFAULT_CHARS_PER_SECOND) -> int:
    """估算一段文本的朗读时长。

    参数:
        text: 待朗读文本。
        speed: 语速倍率（>1 更快，时长更短）。
        chars_per_second: 基准语速，默认 5 字/秒。

    返回:
        估算时长（毫秒，最小 200ms）。

    说明:
        该值只用于未合成时的时间轴占位，产物必须带 `estimated=true` 标记。
    """
    if speed <= 0:
        speed = 1.0
    char_count = len("".join(text.split()))
    base_ms = char_count / max(chars_per_second, 0.1) * 1000
    pause_ms = sum(text.count(char) * weight for char, weight in _PAUSE_RULES.items())
    return max(200, int((base_ms + pause_ms) / speed))
