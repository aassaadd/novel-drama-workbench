"""思考通道的重复检测：命中即提前中止本次调用。

思考内容（`reasoning_content`）不进正文，但和正文共用同一个输出额度：模型一旦在
思考里来回绕同一句话，就会把额度耗光，正文变成空串。这里复用正文侧
`domain.production.rules.degeneration_rules` 的同一套阈值（S1 后缀周期 / S2 长段
重复 / S3 句级紧凑重复），逐段增量判定，命中就止损，不再等它把额度耗完。
"""

from __future__ import annotations

from ...domain.production.rules.degeneration_rules import (
    ALL_SIGNALS,
    CHECK_EVERY_CHARS,
    detect_loop,
    resolve_window,
)


def reasoning_target_chars(metadata: dict | None) -> int:
    """取本轮目标字数，用于推导思考内容的判定窗口。

    与正文侧同源：优先 `watchdog_target_words`，其次 `target_words`。

    参数:
        metadata: 请求元数据。

    返回:
        目标字数；未知时 0（窗口取下限）。
    """
    meta = metadata or {}
    raw = meta.get("watchdog_target_words", meta.get("target_words"))
    try:
        return max(0, int(raw or 0))
    except (TypeError, ValueError):
        return 0


class ReasoningLoopGuard:
    """维护思考内容的最近窗口，按增量检测重复退化。

    检查点与正文侧一致，固定在 `check_every` 的整数倍上并只对前缀判定，
    因此结论与流式分片粗细无关。
    """

    def __init__(
        self,
        target_chars: int = 0,
        window_chars: int | None = None,
        check_every: int = CHECK_EVERY_CHARS,
        signals: tuple[str, ...] = ALL_SIGNALS,
    ):
        """初始化思考检测窗口。

        参数:
            target_chars: 本轮目标字数，用于推导窗口大小。
            window_chars: 显式窗口大小（优先于 target_chars）。
            check_every: 每累积多少字检查一次。
            signals: 启用的检测信号，默认与正文侧相同。
        """
        self.window_chars = int(window_chars or resolve_window(target_chars))
        self.check_every = max(1, int(check_every))
        self.signals = tuple(signals)
        self.text = ""
        self.reason = ""
        self._checked = 0

    def feed(self, delta: str) -> str:
        """追加一段思考增量，命中退化时返回可读原因。

        参数:
            delta: 新到的思考文本。

        返回:
            可读原因；正常时返回空串（同一轮内只返回第一次命中的原因）。
        """
        if delta:
            self.text += delta
        if self.reason:
            return self.reason
        while self._checked + self.check_every <= len(self.text):
            self._checked += self.check_every
            self.reason = detect_loop(self.text, self._checked, self.window_chars, self.signals)[0]
            if self.reason:
                return self.reason
        return ""

    def check_all(self) -> str:
        """对当前全部思考文本检测一次（非流式响应用）。

        返回:
            可读原因；正常时返回空串。
        """
        if self.reason:
            return self.reason
        if self.text:
            self.reason = detect_loop(self.text, len(self.text), self.window_chars, self.signals)[0]
        return self.reason
