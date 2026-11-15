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
- 开启思考时给思考留出下限：`THINKING_ALLOWANCE_TOKENS`，保证思考再长也
  留得下写正文的空间（止损线允许思考占一部分，但不是创作限制）；
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
#: 开启思考时给思考预留的 token 下限。
#: 依据：tt1 真实日志里正常带思考的调用 reasoning 在 600–900 token 量级，
#: 失控的会到 2900+ token（把 731 token 的节拍轮额度全部吃光、正文为空）。
#: 取 1024 覆盖正常思考，并保证正文至少拿到「目标字数 × 0.886」的空间；
#: 再长的思考由思考通道的重复检测早停、或空正文重试兜底。
THINKING_ALLOWANCE_TOKENS = 1024


def watchdog_tokens(target_chars: int, with_summary: bool = False, thinking: bool = False) -> int:
    """按本轮目标字数计算看门狗 token 上限。

    参数:
        target_chars: 本轮目标汉字数（与章节提示词同源，按节拍占比分配）。
        with_summary: 本轮是否同时产出本章摘要（末轮为 True）。
        thinking: 本轮是否开启思考；开启时保证正文与思考都留得下额度。

    返回:
        看门狗 token 上限（整数，不小于 `MIN_WATCHDOG_TOKENS`）。
    """
    chars = max(0, int(target_chars or 0)) * WATCHDOG_RATIO
    if with_summary:
        chars += SUMMARY_EXTRA_CHARS
    tokens = int(math.ceil(chars * TOKENS_PER_CHAR))
    if thinking:
        # 思考再长也要给正文留够「目标字数」所需的 token
        body_tokens = int(math.ceil(max(0, int(target_chars or 0)) * TOKENS_PER_CHAR))
        tokens = max(tokens, body_tokens + THINKING_ALLOWANCE_TOKENS)
    return max(MIN_WATCHDOG_TOKENS, tokens)


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
    thinking: bool = False,
) -> str:
    """生成一句可读的看门狗计算说明（日志与界面共用，口径同源）。

    参数:
        target_chars: 本轮目标汉字数。
        with_summary: 本轮是否同时产出摘要。
        remaining: 上下文剩余可用 token；None 表示未知。
        actual: 实际下发的 max_tokens；None 时按未受上下文限制计算。
        thinking: 本轮是否开启思考。

    返回:
        中文说明，写清系数、换算与是否被上下文压住。
    """
    desired = watchdog_tokens(target_chars, with_summary, thinking)
    parts = [f"目标 {int(target_chars or 0)} 字 × {WATCHDOG_RATIO}"]
    if with_summary:
        parts.append(f"+ 摘要额度 {SUMMARY_EXTRA_CHARS} 字")
    parts.append(f"× {TOKENS_PER_CHAR} token/字 = {desired} tokens")
    if thinking:
        floor = int(math.ceil(max(0, int(target_chars or 0)) * TOKENS_PER_CHAR)) + THINKING_ALLOWANCE_TOKENS
        parts.append(f"开思考时下限 {floor} tokens（正文额度 + 思考预留 {THINKING_ALLOWANCE_TOKENS}）")
    if remaining is not None:
        value = desired if actual is None else int(actual)
        text = f"上下文剩余 {int(remaining)} tokens，实际下发 {value} tokens"
        if value < desired:
            text += "（被上下文压住）"
        parts.append(text)
    parts.append("这是止损线，不是创作限制")
    return "；".join(parts)
