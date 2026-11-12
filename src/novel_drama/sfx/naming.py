"""音效产物命名：CLI 与页面共用同一套规则。

命名形如 `sfx_<时间戳>_<标签>_<请求哈希8>.wav`，侧车 JSON 同名。
哈希覆盖提示词/时长/采样参数，保证「同描述同参数」可追溯、不同参数不撞名。
"""

from __future__ import annotations

import hashlib
import re
import time


def slugify(text: str, fallback: str = "sfx") -> str:
    """把描述压成文件名友好的短标签。

    参数:
        text: 英文描述或自定义名称。
        fallback: 无可用字符时的兜底。

    返回:
        仅含小写字母/数字/下划线、长度不超过 24 的标签。
    """
    words = re.findall(r"[a-zA-Z0-9]+", text or "")[:4]
    slug = "_".join(word.lower() for word in words)[:24].strip("_")
    return slug or fallback


def build_stem(prompt: str, duration_s: float, name: str | None = None, extra: str = "") -> str:
    """生成音效文件主名（不含扩展名）。

    参数:
        prompt: 最终英文提示词。
        duration_s: 时长（秒）。
        name: 自定义标签；缺省取提示词前几个词。
        extra: 追加进哈希的采样参数（如步数/cfg/seed）。

    返回:
        `sfx_<YYYYmmddTHHMMSS>_<标签>_<哈希8>`。
    """
    digest = hashlib.sha1(f"{prompt}|{duration_s}|{extra}".encode("utf-8")).hexdigest()[:8]
    stamp = time.strftime("%Y%m%dT%H%M%S")
    return f"sfx_{stamp}_{slugify(name or prompt)}_{digest}"
