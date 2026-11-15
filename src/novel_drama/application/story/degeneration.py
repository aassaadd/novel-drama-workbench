"""流式正文的退化熔断与局部回退。

线上出现的退化形态是模型在流式输出里反复来回同一段对白（「你有。」「我没有！」…），
直到上下文被填满才停。本模块只在**文本层**做检测并给出「从哪里截断」，不做任何
长度上限；截断后的续写编排在 `application.story.generate_chapter`。

阈值集中在本模块，出处见《docs/research-degeneration-and-length-control.md》
第 2.2 / 6.3 节（除注明外均为工程建议值，需按线上样本标定）：

- 判定窗口：`max(1.5 × 目标字数, 400)` 字，上限 `WINDOW_MAX_CHARS`（调研 2.2 / 6.3）；
- S1 后缀周期重复：长度 6–120 字的片段连续出现 3 次（调研 2.2；vLLM 官方
  `min_count≥2` 机制 + 社区检测器用 3 次）；
- S2 n-gram 覆盖率：12 字 shingle 出现 ≥3 次，覆盖字符占比 ≥30%
  **且最长连续重复 ≥60 字**（tt1 实测标定，见下方常量注释）；
- S3 句级重复：去标点后同一句（≥6 字）在相邻几句里出现 ≥3 次
  （tt1 实测标定：不加「紧凑」约束会把正常口头禅误判）；
- 压缩率（S4）调研建议先在中文小说域标定，默认关闭，本模块不实现。

检查点固定在 `CHECK_EVERY_CHARS` 的整数倍上，并只对「文本前缀」判定，
因此结论与上游流式分片的粗细无关（同一文本重复跑、不同分片跑结果一致）。

调参原则：宁可漏判，也不误杀正常正文（误杀会导致截断、续写甚至整节重写）。

任一信号命中即止损；`DegenerationDetected.keep_index` 给出「截到循环起点前一句」
的保留长度，调用方据此做局部续写，而不是整轮丢弃。
"""

from __future__ import annotations

from ...domain.production.rules.degeneration_rules import (
    ALL_SIGNALS,
    CHECK_EVERY_CHARS,
    SENTENCE_MAX_GAP,
    SENTENCE_MIN_CHARS,
    SENTENCE_STOP_COUNT,
    SHINGLE_COVERAGE_STOP,
    SHINGLE_MIN_COUNT,
    SHINGLE_MIN_SPAN,
    SHINGLE_SIZE,
    SUFFIX_MAX_PERIOD,
    SUFFIX_MIN_PERIOD,
    SUFFIX_REPEATS,
    WINDOW_MAX_CHARS,
    WINDOW_MIN_CHARS,
    WINDOW_TARGET_RATIO,
)
from ...domain.production.rules.degeneration_rules import detect_loop as _detect_loop
from ...domain.production.rules.degeneration_rules import (
    keep_before_loop as keep_before_loop,
)
from ...domain.production.rules.degeneration_rules import (
    resolve_window as resolve_window,
)
from ...domain.production.rules.degeneration_rules import (
    sentences_with_offsets as _sentences_with_offsets,
)
from ...domain.production.rules.degeneration_rules import (
    split_sentences as split_sentences,
)

#: 对外暴露的阈值与工具（阈值唯一来源在 domain 规则模块，这里只做转出）
__all__ = [
    "ALL_SIGNALS",
    "CHECK_EVERY_CHARS",
    "DegenerationDetected",
    "DegenerationGuard",
    "SENTENCE_MAX_GAP",
    "SENTENCE_MIN_CHARS",
    "SENTENCE_STOP_COUNT",
    "SHINGLE_COVERAGE_STOP",
    "SHINGLE_MIN_COUNT",
    "SHINGLE_MIN_SPAN",
    "SHINGLE_SIZE",
    "SUFFIX_MAX_PERIOD",
    "SUFFIX_MIN_PERIOD",
    "SUFFIX_REPEATS",
    "WINDOW_MAX_CHARS",
    "WINDOW_MIN_CHARS",
    "WINDOW_TARGET_RATIO",
    "continue_instruction",
    "keep_at_sentence_end",
    "keep_before_loop",
    "resolve_window",
    "retry_instruction",
    "split_sentences",
]



class DegenerationDetected(Exception):
    """命中退化规则时抛出，携带可读原因、已接收字数与可保留的截断位置。"""

    def __init__(self, reason: str, chars: int = 0, loop_start: int = 0, keep_index: int | None = None):
        """保存命中信息。

        参数:
            reason: 可读原因（写进结果 / 日志）。
            chars: 命中时本轮已接收的字数。
            loop_start: 循环片段在本轮文本里的起始下标。
            keep_index: 建议保留的长度（截到循环起点前一句）；None 时用 loop_start。
        """
        super().__init__(reason)
        self.reason = reason
        self.chars = int(chars)
        self.loop_start = max(0, int(loop_start))
        self.keep_index = self.loop_start if keep_index is None else max(0, int(keep_index))


class DegenerationGuard:
    """维护最近文本窗口，按增量检测重复退化。

    只在 `feed` 累积到一定字数后检查，流式与非流式共用同一套规则。
    """

    def __init__(
        self,
        target_chars: int = 0,
        window_chars: int | None = None,
        check_every: int = CHECK_EVERY_CHARS,
        signals: tuple[str, ...] = ("suffix", "shingle", "sentence"),
    ):
        """初始化窗口与检查步长。

        参数:
            target_chars: 本轮目标字数，用于推导窗口大小。
            window_chars: 显式指定窗口大小（优先于 target_chars）。
            check_every: 每累积多少字检查一次。
            signals: 启用的检测信号；压缩这类「有界改写」可只留 `suffix`
                （原文本身可能就带重复结构，S2/S3 会误杀）。
        """
        self.target_chars = int(target_chars or 0)
        self.window_chars = int(window_chars or resolve_window(target_chars))
        self.check_every = int(check_every)
        self.signals = tuple(signals)
        self.text = ""
        self.reason = ""
        #: 循环片段在本轮文本里的起点（0 表示未知 / 从开头开始）
        self.loop_start = 0
        #: 已检查到的固定位置（始终是 check_every 的整数倍）
        self._checked = 0

    def feed(self, delta: str) -> str | None:
        """追加一段增量，在固定的检查位置上检测。

        检查点固定在 `check_every` 的整数倍，且对「文本前缀」判定，因此结果只取
        决于文本内容与该步长，与上游分片的粗细无关（不会因为一次到 37 字、
        一次到 400 字而算出不同的覆盖率）。

        参数:
            delta: 新到的文本增量。

        返回:
            命中退化时返回可读原因；否则 None（同一轮内只返回一次非空原因）。
        """
        if delta:
            self.text += delta
        if self.reason:
            return self.reason
        return self._check_until(len(self.text), include_tail=False)

    def check_all(self) -> str | None:
        """对当前全部文本做完整检测（非流式生成与流结束时用）。

        先补齐所有固定检查点，再对文本末尾补一次；因此流式与非流式对同一段
        文本给出一致结论。

        返回:
            可读原因；正常时 None。
        """
        if self.reason:
            return self.reason
        reason = self._check_until(len(self.text), include_tail=True)
        self._checked = len(self.text)
        return reason

    def _check_until(self, end: int, include_tail: bool) -> str | None:
        """按固定网格检查到 end，可选再检查一次末尾。

        参数:
            end: 本次要检查到的文本长度（未对齐部分不参与网格检查）。
            include_tail: 是否对 end 处的内容额外检查一次（流结束 / 非流式）。

        返回:
            可读原因；正常时 None。
        """
        while self._checked + self.check_every <= end:
            self._checked += self.check_every
            self.reason = self.detect_at(self._checked)
            if self.reason:
                return self.reason
        if include_tail and end > self._checked:
            self.reason = self.detect_at(end)
            if self.reason:
                return self.reason
        return None

    def detect(self) -> str:
        """对当前全部文本检测一次（兼容直接调用）。

        返回:
            可读原因；正常时返回空串。
        """
        return self.detect_at(len(self.text))

    def detect_at(self, end: int) -> str:
        """在指定的文本前缀上执行 S1 / S2 / S3。

        判定规则与阈值统一由 `domain.production.rules.degeneration_rules` 提供，
        思考通道用的是同一套标准。

        参数:
            end: 只看 `self.text[:end]`，保证不同分片下检查点一致。

        返回:
            可读原因；正常时返回空串。命中时同时更新 `loop_start`。
        """
        reason, loop_start = _detect_loop(self.text, end, self.window_chars, self.signals)
        if reason:
            self.loop_start = loop_start
        return reason

    def exception(self, chars: int | None = None) -> DegenerationDetected:
        """按当前命中状态构造异常，附带建议的保留长度。

        参数:
            chars: 本轮已接收字数；None 时取当前文本长度。

        返回:
            DegenerationDetected。
        """
        total = len(self.text) if chars is None else int(chars)
        keep = keep_before_loop(self.text, self.loop_start)
        return DegenerationDetected(self.reason or "检测到重复退化", total, self.loop_start, keep)


def keep_at_sentence_end(text: str) -> int:
    """计算「按句末收尾」应保留的长度（看门狗命中时用）。

    参数:
        text: 已接收文本。

    返回:
        最后一个完整句子的结束下标；没有任何句子时返回原文本长度。
    """
    keep = 0
    for _key, _start, end in _sentences_with_offsets(text or ""):
        keep = end
    return keep or len(text or "")


def continue_instruction(reason: str, remaining_words: int = 0, keep_note: str = "") -> str:
    """局部续写指令：从截断处继续写，并推进新情节。

    参数:
        reason: 上一次命中的可读原因。
        remaining_words: 剩余目标字数；0 表示未知。
        keep_note: 额外说明（如「已保留前 1200 字」）。

    返回:
        中文续写要求。
    """
    budget = f"剩余约 {remaining_words} 字。" if remaining_words > 0 else ""
    extra = f"{keep_note}。" if keep_note else ""
    return (
        f"上一次输出在（{reason}）处被检测到重复退化，重复部分已截除，{extra}"
        f"请紧接上文继续写，{budget}"
        "硬约束：必须推进上文没有的新信息——让某个角色做出新动作、拿出新物件或改变立场；"
        "禁止反复使用同一句对白、同一句式或同一段落结构，禁止原地来回的重复对白；"
        "只输出新增正文。"
    )


def retry_instruction(reason: str) -> str:
    """整节重写指令：命中退化且局部续写未收敛后追加的硬约束。

    参数:
        reason: 上一次命中的可读原因。

    返回:
        中文重写要求。
    """
    return (
        f"上一次输出被检测到重复退化（{reason}），本次改为整节重写。硬约束："
        "禁止反复使用同一句对白、同一句式或同一段落结构；"
        "每次对话必须推进新信息，相邻句子不得高度雷同；"
        "不要复述已写内容，按本轮节拍重新组织这一段。"
    )
