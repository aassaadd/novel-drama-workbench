"""正文生成的看门狗上限（止损线，不是创作限制）。

背景：输出侧不设「按目标字数推导的正文预算」，但退化或跑飞时不能靠上下文被填满
才停。看门狗按**本轮目标字数**动态算一个止损线，命中后保留已写正文并按句末收尾，
再走局部续写；它不是篇幅控制手段。

口径统一在本模块（调研《research-degeneration-and-length-control.md》第 6.1 / 6.4
节 + 本项目实测）：

- 系数：`WATCHDOG_RATIO = 1.65`（调研 6.1 / 6.4 的看门狗建议值）；
- 汉字折 token：`TOKENS_PER_CHAR = 0.886`（本项目 42 条真实请求实测，
  取代厂商文档的「1.5–1.8 汉字/token」说法，以实测为准）；
- 末轮同时产出本章摘要时，额外加 `SUMMARY_EXTRA_CHARS` 字对应额度；
- 与「上下文窗口 − 提示词占用量」取小，被上下文压住时在日志与结果里说明。
"""

from __future__ import annotations

import math

#: 看门狗系数：本轮目标字数 × 该系数（调研 6.1 / 6.4）
WATCHDOG_RATIO = 1.65
#: 汉字折 token 的实测口径（0.886 token/汉字，42 条真实请求校验）
TOKENS_PER_CHAR = 0.886
#: 末轮内联摘要的额外字符额度（本章摘要 + 更新后的全书摘要 JSON）
SUMMARY_EXTRA_CHARS = 600
#: 看门狗下限（token），避免极短目标算出不可用的上限
MIN_WATCHDOG_TOKENS = 64


def watchdog_tokens(target_chars: int, with_summary: bool = False) -> int:
    """按本轮目标字数计算看门狗 token 上限。

    参数:
        target_chars: 本轮目标汉字数（与章节提示词同源，按节拍占比分配）。
        with_summary: 本轮是否同时产出本章摘要（末轮为 True）。

    返回:
        看门狗 token 上限（整数，不小于 `MIN_WATCHDOG_TOKENS`）。
    """
    chars = max(0, int(target_chars or 0)) * WATCHDOG_RATIO
    if with_summary:
        chars += SUMMARY_EXTRA_CHARS
    return max(MIN_WATCHDOG_TOKENS, int(math.ceil(chars * TOKENS_PER_CHAR)))


def apply_context_limit(desired: int, remaining: int | None) -> tuple[int, bool]:
    """把看门狗值与剩余上下文空间取小。

    参数:
        desired: 按目标字数算出的看门狗 token 上限。
        remaining: 「上下文窗口 − 提示词占用量」；None 表示窗口未知。

    返回:
        `(实际使用的 max_tokens, 是否被上下文压住)`。
    """
    if remaining is None:
        return desired, False
    if desired <= remaining:
        return desired, False
    return max(1, int(remaining)), True


def describe(
    target_chars: int,
    with_summary: bool = False,
    remaining: int | None = None,
    actual: int | None = None,
) -> str:
    """生成一句可读的看门狗计算说明（日志与界面共用，口径同源）。

    参数:
        target_chars: 本轮目标汉字数。
        with_summary: 本轮是否同时产出摘要。
        remaining: 上下文剩余可用 token；None 表示未知。
        actual: 实际下发的 max_tokens；None 时按未受上下文限制计算。

    返回:
        中文说明，写清系数、换算与是否被上下文压住。
    """
    desired = watchdog_tokens(target_chars, with_summary)
    parts = [f"目标 {int(target_chars or 0)} 字 × {WATCHDOG_RATIO}"]
    if with_summary:
        parts.append(f"+ 摘要额度 {SUMMARY_EXTRA_CHARS} 字")
    parts.append(f"× {TOKENS_PER_CHAR} token/字 = {desired} tokens")
    if remaining is not None:
        value = desired if actual is None else int(actual)
        text = f"上下文剩余 {int(remaining)} tokens，实际下发 {value} tokens"
        if value < desired:
            text += "（被上下文压住）"
        parts.append(text)
    parts.append("这是止损线，不是创作限制")
    return "；".join(parts)
