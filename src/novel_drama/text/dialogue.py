"""对白引号规范化：把各类引号统一成中文全角双引号 “”。

只改引号字符，不动其它标点、空格与换行；对「看起来是对白却没有引号」的行
只记提示，不擅自补引号，避免改写正文语义。

用于正文生成、续写正文与批量生成（编排）后的统一收口。
"""

from __future__ import annotations

import re

#: 含中文的最小对白片段（避免误伤英文缩写、代码、计量单位）
_CJK = r"\u3400-\u9fff\uf900-\ufaff"
_STRAIGHT_DOUBLE = re.compile(rf'"([^"\n]*[{_CJK}][^"\n]*)"')
_STRAIGHT_SINGLE = re.compile(rf"'([^'\n]*[{_CJK}][^'\n]*)'")
#: 整行只有一个【...】或 [前面只有少量空白] 的方头括号内容，按对白处理
_WHOLE_BRACKET = re.compile(r"^\s*【([^【】\n]+)】\s*[，。！？!?…]*\s*$")


def _convert_pairs(line: str, left: str, right: str) -> str:
    """把成对的左右引号替换成全角双引号（数量不平衡时原样返回）。

    参数:
        line: 单行文本。
        left: 左引号字符。
        right: 右引号字符。

    返回:
        替换后的行。
    """
    if line.count(left) != line.count(right) or not line.count(left):
        return line
    return line.replace(left, "“").replace(right, "”")


def _convert_square_brackets(line: str) -> str:
    """只把「整行就是【…】」的方头括号对白换成 “…”（保留行尾标点）。

    参数:
        line: 单行文本。

    返回:
        替换后的行。
    """
    match = _WHOLE_BRACKET.match(line)
    if not match:
        return line
    body = match.group(1).strip()
    if not body:
        return line
    return line[: match.start(1) - 1] + f"“{body}”" + line[match.end(1) + 1 :]


def normalize_dialogue(text: str, speaker_names: list[str] | None = None) -> tuple[str, list[str]]:
    """规范化对白引号并检查疑似漏引号的对白行。

    参数:
        text: 待处理正文（可含多行）。
        speaker_names: 已知角色名列表，用于识别「姓名：台词」这类漏引号对白。

    返回:
        (规范化后的正文, 提示列表)。提示不会改动正文。
    """
    names = {str(name).strip() for name in (speaker_names or []) if str(name).strip()}
    warnings: list[str] = []
    out_lines: list[str] = []
    for lineno, line in enumerate((text or "").split("\n"), start=1):
        original = line
        # 直角引号 / 双层直角引号 → 全角双引号
        line = _convert_pairs(line, "「", "」")
        line = _convert_pairs(line, "『", "』")
        # 整行方头括号（截图里的对白写法）→ 全角双引号
        line = _convert_square_brackets(line)
        # 英文双引号 / 单引号（仅处理包住中文的成对引号）
        while True:
            replaced = _STRAIGHT_DOUBLE.sub(r"“\1”", line)
            if replaced == line:
                break
            line = replaced
        while True:
            replaced = _STRAIGHT_SINGLE.sub(r"“\1”", line)
            if replaced == line:
                break
            line = replaced
        # 残留的英文引号提示（不猜配对，避免改坏内容）
        if line != original and ('"' in line or "'" in line):
            warnings.append(f"第 {lineno} 行仍有未配对的英文引号，请人工确认：{line.strip()[:40]}")
        # 疑似漏引号对白：已知角色名 + 冒号，且整行没有中文引号
        if names and "“" not in line and "”" not in line:
            match = re.match(r"^\s*([^\s：:]{1,12})[：:]\s*(.+)$", line)
            if match and match.group(1).strip() in names:
                warnings.append(f"第 {lineno} 行疑似对白未加引号，请人工确认：{line.strip()[:40]}")
        out_lines.append(line)
    return "\n".join(out_lines), warnings
