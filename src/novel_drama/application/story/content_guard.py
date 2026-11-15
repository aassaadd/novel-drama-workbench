"""章节正文落盘前的有效性校验。

只做一件事：判断模型输出是不是「小说叙事正文」，避免把思考过程、任务复述、
写作准则清单、人物设定罗列当成正文写进 `chapter.content`。

判定口径（命中任一即视为无效）：
- 开头命中指令复述 / 思考口吻的特征短语（中英文都覆盖）；
- 全文多次出现「写作准则」「人物设定」「节拍 N 内容」等提示词结构词；
- 列表行（`- ` / `* ` / `1. ` 等）占比过高，说明是准则清单而不是叙事；
- 开头几乎没有中文（英文思考），或文本过短。

本模块不修改文本，也不做「截掉前半段」这类静默修补；调用方命中后应重试或明确报错。
"""

from __future__ import annotations

import re

#: 开头若干字符内出现即判为复述任务 / 思考
_HEAD_MARKERS = (
    "用户要求",
    "用户希望",
    "用户想要",
    "用户让我",
    "任务指令",
    "写作要求",
    "写作准则",
    "写法要点",
    "写作要点",
    "需要遵循的写作准则",
    "人物设定",
    "节拍内容",
    "让我开始写",
    "我准备这样写",
    "写作计划",
    "The user wants",
    "The user asked",
    "The user is asking",
    "I need to write",
    "Context Analysis",
)

#: 提示词结构词：出现在正文里说明模型在复述指令
_STRUCTURE_PATTERN = re.compile(
    r"(写作准则|写作要求|写作要点|人物设定|让我开始写|写作计划|节拍\s*\d+\s*内容|Context Analysis)"
)

#: 列表行：`- xxx` / `* xxx` / `1. xxx` / `1、xxx`
_LIST_LINE_PATTERN = re.compile(r"^\s*(?:[-*•]|\d+[.、])\s*\S")

#: 开头检查长度与英文占比阈值
_HEAD_CHARS = 200
_MIN_CHARS = 50
_MIN_CJK_RATIO = 0.2
#: 列表行占比阈值（列表行 / 非空行），以及参与判断的最少行数
_MAX_LIST_RATIO = 0.4
_MIN_LINES_FOR_RATIO = 8

_CJK_PATTERN = re.compile(r"[\u4e00-\u9fff]")


def check_chapter_content(text: str) -> tuple[bool, str]:
    """校验章节正文是否为小说叙事文本。

    参数:
        text: 模型输出（落盘前的正文）。

    返回:
        `(ok, reason)`；ok 为 False 时 reason 是中文的判定原因，用于日志与报错。
    """
    body = (text or "").strip()
    if not body:
        return False, "正文为空"
    if len(body) < _MIN_CHARS:
        return False, f"正文过短（{len(body)} 字）"

    head = body[:_HEAD_CHARS]
    lowered = head.lower()
    for marker in _HEAD_MARKERS:
        if marker.lower() in lowered:
            return False, f"开头在复述任务或写作要求（命中「{marker}」）"

    # 开头基本没有中文：典型英文思考输出
    cjk = len(_CJK_PATTERN.findall(head))
    if cjk / max(1, len(head)) < _MIN_CJK_RATIO:
        return False, "开头几乎没有中文，疑似英文思考内容"

    structure_hits = _STRUCTURE_PATTERN.findall(body)
    if len(structure_hits) >= 2:
        return False, f"正文中多次出现写作准则/人物设定等指令结构词（{len(structure_hits)} 处）"

    lines = [line for line in body.splitlines() if line.strip()]
    if len(lines) >= _MIN_LINES_FOR_RATIO:
        list_lines = sum(1 for line in lines if _LIST_LINE_PATTERN.match(line))
        if list_lines / len(lines) > _MAX_LIST_RATIO:
            return False, f"列表行占比过高（{list_lines}/{len(lines)}），疑似写作准则清单"

    return True, ""


def strict_retry_instruction() -> str:
    """返回命中校验后追加的严格约束，用于重试同一轮。

    返回:
        一段中文指令，明确禁止复述任务、罗列准则与输出思考过程。
    """
    return (
        "上一次输出不是小说正文（疑似复述任务或罗列写作准则），已作废。"
        "请重新输出：只写小说叙事正文，直接以场景、动作或对白开头；"
        "禁止复述任务要求，禁止列出写作准则、人物设定或节拍清单，"
        "禁止输出思考过程、写作计划，禁止出现「用户要求」「让我开始写」这类字样。"
    )
