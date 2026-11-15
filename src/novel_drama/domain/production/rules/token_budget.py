"""输入侧上下文上限。

输出侧不再设置人为预算：`max_tokens` 由适配器按「上下文窗口 − 已用提示词」计算，
不额外留余量；模型自己输出结束符停止。这里只负责上下文窗口与装配预算。
"""

from __future__ import annotations

#: 未检测到模型上下文窗口时的装配兜底（仅输入侧，不是输出预算）
DEFAULT_CONTEXT_BUDGET_TOKENS = 32768


def _get(config, dotted: str, default=None):
    """读取点号配置；兼容 AppConfig 与普通 dict。

    参数:
        config: AppConfig（带 get(dotted)）或嵌套 dict。
        dotted: 点号路径。
        default: 缺失时的返回值。

    返回:
        配置值或 default。
    """
    if config is None:
        return default
    if isinstance(config, dict):
        node = config
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node
    getter = getattr(config, "get", None)
    if getter is None:
        return default
    return getter(dotted, default)


def _as_int(value, default: int = 0) -> int:
    """把配置值转成正整数；非法值用默认值。

    参数:
        value: 原始配置值。
        default: 兜底值。

    返回:
        正整数或 0。
    """
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def context_window(config) -> int:
    """返回模型上下文窗口；未配置时为 0。

    参数:
        config: AppConfig 或 dict。

    返回:
        上下文窗口 tokens，或 0。
    """
    return _as_int(_get(config, "omlx.context_window"), 0)


def context_budget_tokens(config) -> int:
    """返回上下文装配预算：优先用模型上下文窗口，未知时用 32768 兜底。

    参数:
        config: AppConfig 或 dict。

    返回:
        装配预算 tokens。
    """
    return context_window(config) or DEFAULT_CONTEXT_BUDGET_TOKENS


def context_status(config) -> dict:
    """返回设置页展示的上下文现状。

    参数:
        config: AppConfig 或 dict。

    返回:
        `{window, budget, detected, fallback, note}`。
    """
    window = context_window(config)
    return {
        "window": window or None,
        "budget": context_budget_tokens(config),
        "detected": bool(window),
        "fallback": not window,
        "note": (
            "按模型上下文窗口限制输入装配；输出侧不设置人为上限，"
            "请求的 max_tokens 使用「上下文窗口 − 已用提示词」的剩余空间"
            if window
            else "未检测到模型上下文窗口，输入装配按 32768 tokens 兜底；"
            "输出侧仍不设置人为上限，建议先在连接配置里检测一次上下文窗口"
        ),
    }
