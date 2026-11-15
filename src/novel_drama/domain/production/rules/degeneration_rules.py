"""重复退化判定规则（纯计算，无 I/O）。

正文侧（`application/story/degeneration.py`）与思考通道
（`adapters/llm/reasoning_guard.py`）共用同一套阈值与判定，避免两处标准漂移：

- S1 后缀周期重复：长度 6–120 字的片段连续出现 3 次；
- S2 n-gram 覆盖率：12 字 shingle 出现 ≥3 次，覆盖字符占比 ≥30%
  **且最长连续重复 ≥60 字**（tt1 真实正文实测标定）；
- S3 句级重复：去标点后同一句（≥6 字）在相邻几句里出现 ≥3 次；
- 判定窗口：`max(1.5 × 目标字数, 400)` 字，上限 2000。

检查点固定在 `CHECK_EVERY_CHARS` 的整数倍上，并只对「文本前缀」判定，
因此结论与上游流式分片的粗细无关（同一文本重复跑、不同分片跑结果一致）。

调参原则：宁可漏判，也不误杀正常文本（误杀会导致截断、续写甚至整节重写）。
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
#: S2：重复 shingle 覆盖字符占比的硬止损阈值
SHINGLE_COVERAGE_STOP = 0.30
#: S2：重复片段必须连续覆盖的最短长度（字）
SHINGLE_MIN_SPAN = 60
#: S3：参与句级重复判定的最短句长（去标点后）
SENTENCE_MIN_CHARS = 6
#: S3：同一句出现次数达到该值即止损
SENTENCE_STOP_COUNT = 3
#: S3：两次出现之间最多隔几句
SENTENCE_MAX_GAP = 2

#: 信号清单（顺序即判定顺序）
ALL_SIGNALS: tuple[str, ...] = ("suffix", "shingle", "sentence")

_SENTENCE_PATTERN = re.compile(r"[^。！？!?…\n]+[。！？!?…]*|[。！？!?…]+|\n+")
_STRIP_CHARS = " \u3000\t\r\n「」『』“”\"'‘’（）()《》【】[]…—"


def resolve_window(target_chars: int = 0) -> int:
    """按目标字数推导判定窗口大小。

    参数:
        target_chars: 本轮目标字数；0 或未知时只用下限。

    返回:
        窗口字数：`max(1.5 × 目标字数, 400)`，上限 2000。
    """
    basis = int(WINDOW_TARGET_RATIO * max(0, int(target_chars or 0)))
    return max(WINDOW_MIN_CHARS, min(WINDOW_MAX_CHARS, basis))


def sentence_key(text: str) -> str:
    """把句子归一化成可比较的键（去首尾引号、空格与常见标点）。

    参数:
        text: 原始句子片段。

    返回:
        归一化后的句子；过短时返回空串。
    """
    key = (text or "").strip().strip(_STRIP_CHARS).replace(" ", "")
    return key if len(key) >= 2 else ""


def sentences_with_offsets(text: str) -> list[tuple[str, int, int]]:
    """按句末标点与换行切句，并保留每句在原文本里的起止下标。

    参数:
        text: 待切分的文本。

    返回:
        `[(归一化句子, 起始下标, 结束下标)]`，只含非空句。
    """
    rows: list[tuple[str, int, int]] = []
    for match in _SENTENCE_PATTERN.finditer(text or ""):
        key = sentence_key(match.group(0))
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
    return [key for key, _start, _end in sentences_with_offsets(text)]


def keep_before_loop(text: str, loop_start: int) -> int:
    """计算「截到循环起点前一句」应保留的长度。

    参数:
        text: 本轮已接收的完整文本。
        loop_start: 循环片段起点下标。

    返回:
        保留长度（按字符）；找不到前一句时为 0。
    """
    keep = 0
    for _key, _start, end in sentences_with_offsets(text or ""):
        if end <= loop_start:
            keep = end
        else:
            break
    return keep


def suffix_loop_start(text: str) -> tuple[int, int] | None:
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


def shingle_loop_start(text: str) -> tuple[int, float, int] | None:
    """S2：检测窗口内 12 字 shingle 的重复覆盖率与最长连续重复长度。

    两个条件同时满足才算退化：覆盖率 ≥ `SHINGLE_COVERAGE_STOP`，
    且连续被覆盖的长度 ≥ `SHINGLE_MIN_SPAN`。

    参数:
        text: 判定窗口内的文本。

    返回:
        `(循环起点下标, 覆盖率, 最长连续重复字数)`；未达硬止损阈值时 None。
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
    longest = 0
    current = 0
    for flag in covered:
        current = current + 1 if flag else 0
        if current > longest:
            longest = current
    if longest < SHINGLE_MIN_SPAN:
        return None
    return loop_start, coverage, longest


def sentence_loop_start(text: str) -> tuple[int, str, int] | None:
    """S3：检测同一句（去标点后 ≥6 字）在相邻几句里反复出现。

    只在「紧凑」时才算退化：连续 `SENTENCE_STOP_COUNT` 次出现之间最多隔
    `SENTENCE_MAX_GAP` 句。

    参数:
        text: 判定窗口内的文本。

    返回:
        `(循环起点下标, 句子片段, 连续出现次数)`；未命中时 None。
    """
    rows = sentences_with_offsets(text)
    positions: dict[str, list[tuple[int, int]]] = {}
    for index, (key, start, _end) in enumerate(rows):
        if len(key) < SENTENCE_MIN_CHARS:
            continue
        positions.setdefault(key, []).append((index, start))
    for key, rows_for_key in positions.items():
        if len(rows_for_key) < SENTENCE_STOP_COUNT:
            continue
        run = 1
        for (prev_index, _prev_start), (cur_index, cur_start) in zip(rows_for_key, rows_for_key[1:]):
            run = run + 1 if cur_index - prev_index <= SENTENCE_MAX_GAP + 1 else 1
            if run >= SENTENCE_STOP_COUNT:
                return cur_start, key[:20], run
    return None


def detect_loop(
    text: str,
    end: int,
    window_chars: int,
    signals: tuple[str, ...] = ALL_SIGNALS,
) -> tuple[str, int]:
    """在文本前缀的尾部窗口上判定重复，返回可读原因与循环起点。

    参数:
        text: 已接收的完整文本。
        end: 只看 `text[:end]`，保证不同分片下检查点一致。
        window_chars: 判定窗口大小（字）。
        signals: 启用的信号（suffix / shingle / sentence）。

    返回:
        `(可读原因, 循环起点下标)`；未命中返回 `("", 0)`。
    """
    prefix = text[: max(0, int(end))]
    tail = prefix[-window_chars:] if window_chars > 0 else prefix
    base = len(prefix) - len(tail)

    if "suffix" in signals:
        suffix = suffix_loop_start(tail)
        if suffix is not None:
            start, period = suffix
            loop_start = base + start
            # 在全文范围内继续往回走，定位整段循环真正的起点（窗口外的重复也要截掉）
            unit = prefix[-period:]
            while loop_start - period >= 0 and prefix[loop_start - period : loop_start] == unit:
                loop_start -= period
            snippet = prefix[loop_start : loop_start + 20].replace("\n", " ")
            return f"同一段连续重复 {SUFFIX_REPEATS} 次（「{snippet}」）", loop_start

    if "shingle" in signals:
        shingle = shingle_loop_start(tail)
        if shingle is not None:
            start, coverage, longest = shingle
            return (
                f"最近 {len(tail)} 字里 {SHINGLE_SIZE} 字片段重复覆盖率 "
                f"{int(coverage * 100)}%、最长连续重复 {longest} 字"
                f"（阈值：覆盖率 ≥{int(SHINGLE_COVERAGE_STOP * 100)}% 且连续 ≥{SHINGLE_MIN_SPAN} 字）",
                base + start,
            )

    if "sentence" in signals:
        sentence = sentence_loop_start(tail)
        if sentence is not None:
            start, snippet, count = sentence
            return f"同一句在相邻几句里连续出现 {count} 次（「{snippet}」）", base + start

    return "", 0
