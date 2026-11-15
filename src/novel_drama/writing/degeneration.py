"""流式正文的退化熔断与局部回退。

线上出现的退化形态是模型在流式输出里反复来回同一段对白（「你有。」「我没有！」…），
直到上下文被填满才停。本模块只在**文本层**做检测并给出「从哪里截断」，不做任何
长度上限；截断后的续写编排在 `writing.service`。

阈值集中在本模块，出处见《docs/research-degeneration-and-length-control.md》
第 2.2 / 6.3 节（除注明外均为工程建议值，需按线上样本标定）：

- 判定窗口：`max(1.5 × 目标字数, 400)` 字，上限 `WINDOW_MAX_CHARS`（调研 2.2 / 6.3）；
- S1 后缀周期重复：长度 6–120 字的片段连续出现 3 次（调研 2.2；vLLM 官方
  `min_count≥2` 机制 + 社区检测器用 3 次）；
- S2 n-gram 覆盖率：12 字 shingle 在窗口内出现 ≥3 次，覆盖字符占比 ≥30%
  （调研 2.2，推断值）；
- S3 句级重复：去标点后同一句（≥6 字）在窗口内出现 ≥3 次（调研 2.2，推断值）；
- 压缩率（S4）调研建议先在中文小说域标定，默认关闭，本模块不实现。

任一信号命中即止损；`DegenerationDetected.keep_index` 给出「截到循环起点前一句」
的保留长度，调用方据此做局部续写，而不是整轮丢弃。
"""

from __future__ import annotations

import re

#: 判定窗口上限（字）
WINDOW_MAX_CHARS = 2000
#: 判定窗口下限（字）
WINDOW_MIN_CHARS = 400
#: 窗口相对目标字数的倍数
WINDOW_TARGET_RATIO = 1.5
#: 每收到多少字检查一次，避免逐字做重复统计
CHECK_EVERY_CHARS = 120
#: S1：重复片段的最短长度（字）
SUFFIX_MIN_PERIOD = 6
#: S1：重复片段的最长长度（字）
SUFFIX_MAX_PERIOD = 120
#: S1：同一片段连续出现的次数
SUFFIX_REPEATS = 3
#: S2：shingle 长度（字）
SHINGLE_SIZE = 12
#: S2：shingle 判定为重复的最少出现次数
SHINGLE_MIN_COUNT = 3
#: S2：重复 shingle 覆盖字符占比的硬止损阈值（20%–30% 为软告警，本模块只做硬止损）
SHINGLE_COVERAGE_STOP = 0.30
#: S3：参与句级重复判定的最短句长（去标点后）
SENTENCE_MIN_CHARS = 6
#: S3：同一句出现次数达到该值即止损
SENTENCE_STOP_COUNT = 3

_SENTENCE_PATTERN = re.compile(r"[^。！？!?…\n]+[。！？!?…]*|[。！？!?…]+|\n+")
_STRIP_CHARS = " \u3000\t\r\n「」『』“”\"'‘’（）()《》【】[]…—"


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


def resolve_window(target_chars: int = 0) -> int:
    """按目标字数推导判定窗口大小。

    参数:
        target_chars: 本轮目标字数；0 或未知时只用下限。

    返回:
        窗口字数：`max(1.5 × 目标字数, 400)`，上限 2000。
    """
    basis = int(WINDOW_TARGET_RATIO * max(0, int(target_chars or 0)))
    return max(WINDOW_MIN_CHARS, min(WINDOW_MAX_CHARS, basis))


def _sentence_key(text: str) -> str:
    """把句子归一化成可比较的键（去首尾引号、空格与常见标点）。

    参数:
        text: 原始句子片段。

    返回:
        归一化后的句子；过短时返回空串。
    """
    key = (text or "").strip().strip(_STRIP_CHARS).replace(" ", "")
    return key if len(key) >= 2 else ""


def _sentences_with_offsets(text: str) -> list[tuple[str, int, int]]:
    """按句末标点与换行切句，并保留每句在原文本里的起止下标。

    参数:
        text: 待切分的文本。

    返回:
        `[(归一化句子, 起始下标, 结束下标)]`，只含非空句。
    """
    rows: list[tuple[str, int, int]] = []
    for match in _SENTENCE_PATTERN.finditer(text or ""):
        key = _sentence_key(match.group(0))
        if key:
            rows.append((key, match.start(), match.end()))
    return rows


def split_sentences(text: str) -> list[str]:
    """按句末标点与换行切句，返回归一化后的句子列表。

    参数:
        text: 待切分的文本。

    返回:
        非空句子键列表。
    """
    return [key for key, _start, _end in _sentences_with_offsets(text)]


def keep_before_loop(text: str, loop_start: int) -> int:
    """计算「截到循环起点前一句」应保留的长度。

    参数:
        text: 本轮已接收的完整文本。
        loop_start: 循环片段起点下标。

    返回:
        保留长度（字节无关，按字符）；找不到前一句时为 0。
    """
    keep = 0
    for _key, _start, end in _sentences_with_offsets(text or ""):
        if end <= loop_start:
            keep = end
        else:
            break
    return keep


def _suffix_loop_start(text: str) -> tuple[int, int] | None:
    """S1：检测末尾是否存在长度 6–120 的片段连续出现 3 次。

    参数:
        text: 判定窗口内的文本。

    返回:
        `(循环片段起点下标, 周期)`；未命中时 None。
    """
    length = len(text)
    for period in range(SUFFIX_MIN_PERIOD, SUFFIX_MAX_PERIOD + 1):
        span = period * SUFFIX_REPEATS
        if length < span:
            break
        unit = text[-period:]
        if text[-2 * period : -period] == unit and text[-3 * period : -2 * period] == unit:
            return length - span, period
    return None


def _shingle_loop_start(text: str) -> tuple[int, float] | None:
    """S2：检测窗口内 12 字 shingle 的重复覆盖率。

    参数:
        text: 判定窗口内的文本。

    返回:
        `(循环起点下标, 覆盖率)`；未达硬止损阈值时 None。
    """
    length = len(text)
    if length < SHINGLE_SIZE * SHINGLE_MIN_COUNT:
        return None
    positions: dict[str, list[int]] = {}
    for start in range(length - SHINGLE_SIZE + 1):
        positions.setdefault(text[start : start + SHINGLE_SIZE], []).append(start)
    covered = bytearray(length)
    loop_start: int | None = None
    for starts in positions.values():
        if len(starts) < SHINGLE_MIN_COUNT:
            continue
        for start in starts:
            for index in range(start, min(start + SHINGLE_SIZE, length)):
                covered[index] = 1
        # 第二次出现的位置才是「重复开始」，第一次属于正常行文
        second = starts[1]
        if loop_start is None or second < loop_start:
            loop_start = second
    if loop_start is None:
        return None
    coverage = sum(covered) / length
    if coverage < SHINGLE_COVERAGE_STOP:
        return None
    return loop_start, coverage


def _sentence_loop_start(text: str) -> tuple[int, str, int] | None:
    """S3：检测同一句（去标点后 ≥6 字）在窗口内出现 ≥3 次。

    参数:
        text: 判定窗口内的文本。

    返回:
        `(循环起点下标, 句子片段, 出现次数)`；未命中时 None。
    """
    rows = _sentences_with_offsets(text)
    starts: dict[str, list[int]] = {}
    for key, start, _end in rows:
        if len(key) < SENTENCE_MIN_CHARS:
            continue
        starts.setdefault(key, []).append(start)
    for key, positions in starts.items():
        if len(positions) >= SENTENCE_STOP_COUNT:
            return positions[1], key[:20], len(positions)
    return None


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
        self._checked = 0

    def feed(self, delta: str) -> str | None:
        """追加一段增量，达到检查步长时检测。

        参数:
            delta: 新到的文本增量。

        返回:
            命中退化时返回可读原因；否则 None（同一轮内只返回一次非空原因）。
        """
        if delta:
            self.text += delta
        if self.reason:
            return self.reason
        if len(self.text) - self._checked < self.check_every:
            return None
        self._checked = len(self.text)
        self.reason = self.detect()
        return self.reason or None

    def check_all(self) -> str | None:
        """对当前全部文本立即检测一次（非流式生成用）。

        返回:
            可读原因；正常时 None。
        """
        self._checked = len(self.text)
        if self.reason:
            return self.reason
        self.reason = self.detect()
        return self.reason or None

    def detect(self) -> str:
        """依次执行 S1 / S2 / S3，命中即返回可读原因。

        返回:
            可读原因；正常时返回空串。命中时同时更新 `loop_start`。
        """
        tail = self.text[-self.window_chars :]
        base = len(self.text) - len(tail)
        if len(tail) < SUFFIX_MIN_PERIOD * SUFFIX_REPEATS:
            return ""

        # S1：后缀周期重复（最直观的「来回同一段」）
        if "suffix" in self.signals:
            suffix = _suffix_loop_start(tail)
            if suffix is not None:
                start, period = suffix
                loop_start = base + start
                # 在全文范围内继续往回走，定位整段循环真正的起点（窗口外的重复也要截掉）
                unit = self.text[-period:]
                while loop_start - period >= 0 and self.text[loop_start - period : loop_start] == unit:
                    loop_start -= period
                self.loop_start = loop_start
                snippet = self.text[loop_start : loop_start + 20].replace("\n", " ")
                return f"同一段连续重复 {SUFFIX_REPEATS} 次（「{snippet}」）"

        # S2：n-gram 覆盖率（长周期、换名字重演同一场也能抓到）
        if "shingle" in self.signals:
            shingle = _shingle_loop_start(tail)
            if shingle is not None:
                start, coverage = shingle
                self.loop_start = base + start
                return (
                    f"最近 {len(tail)} 字里 {SHINGLE_SIZE} 字片段重复覆盖率 "
                    f"{int(coverage * 100)}%（阈值 {int(SHINGLE_COVERAGE_STOP * 100)}%）"
                )

        # S3：句级重复（对话体最直观的信号）
        if "sentence" in self.signals:
            sentence = _sentence_loop_start(tail)
            if sentence is not None:
                start, snippet, count = sentence
                self.loop_start = base + start
                return f"同一句出现 {count} 次（「{snippet}」）"
        return ""

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
