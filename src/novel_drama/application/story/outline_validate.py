"""大纲校验：黄金三章检查与大纲章节遍历。"""

from __future__ import annotations

from .setting_spec import CONFLICT_KEYWORDS


def check_golden_three(bible: dict) -> dict:
    """检查前三章是否满足「黄金三章」约束（不阻断落盘）。

    规则:
        第 1 章首个节拍摘要非空（钩子）；
        第 2 章节拍 ≥ 2，或章节摘要含冲突/对抗语义；
        第 3 章存在 turning_point=true 的节拍。

    参数:
        bible: 设定库 dict。

    返回:
        {"passed": bool, "issues": [{"chapter_index", "chapter_id", "chapter_title", "problem",
        "hint", "focus"}]}；focus 供界面定位（如 beat_turning_point 表示该章需要标转折）。
    """
    chapters = _outline_chapters(bible)[:3]
    issues: list[dict] = []

    if not chapters:
        issues.append(
            _golden_issue(1, None, "还没有大纲", "设定库里没有任何幕/章；先执行生成大纲（ndw outline generate）")
        )
        return {"passed": False, "issues": issues}

    def index_of(position: int, chapter: dict | None = None) -> int:
        """取章节在整本书里的实际序号，缺失章节按已有最大序号顺延。"""
        if chapter is not None:
            return int(chapter.get("index") or position)
        tail = chapters[-1] if chapters else {}
        return int(tail.get("index") or len(chapters)) + (1 if tail else 0)

    first = chapters[0]
    first_index = index_of(1, first)
    first_beats = first.get("beats") or []
    if not (first_beats and str((first_beats[0] or {}).get("summary") or "").strip()):
        issues.append(
            _golden_issue(
                first_index,
                first,
                "开篇钩子缺失",
                f"第 {first_index} 章首个节拍的摘要是空的；要写清悬念/冲突/能力亮相，前 300 字抓住读者",
            )
        )

    if len(chapters) < 2:
        missing_index = index_of(2)
        issues.append(
            _golden_issue(missing_index, None, f"缺少第 {missing_index} 章", "第 2 章需要确立核心矛盾与对抗关系")
        )
    else:
        second = chapters[1]
        second_index = index_of(2, second)
        second_beats = second.get("beats") or []
        summary = str(second.get("summary") or "")
        has_conflict = len(second_beats) >= 2 or any(word in summary for word in CONFLICT_KEYWORDS)
        if not has_conflict:
            issues.append(
                _golden_issue(
                    second_index,
                    second,
                    "没有确立核心矛盾",
                    f"第 {second_index} 章只有 {len(second_beats)} 个节拍，摘要里也没有冲突/对抗语义；"
                    "把冲突写进章节摘要，或拆成至少 2 个节拍呈现对抗",
                )
            )

    if len(chapters) < 3:
        missing_index = index_of(3)
        issues.append(
            _golden_issue(
                missing_index, None, f"缺少第 {missing_index} 章", "第 3 章需要出现第一次小高潮，让读者确认「这本书能看」"
            )
        )
    else:
        third = chapters[2]
        third_index = index_of(3, third)
        turned = any(bool((beat or {}).get("turning_point")) for beat in (third.get("beats") or []))
        if not turned:
            issues.append(
                _golden_issue(
                    third_index,
                    third,
                    "缺少第一次小高潮",
                    f"第 {third_index} 章没有任何节拍标记 turning_point=true；把其中一个节拍标记为转折，形成阶段性反转",
                    focus="beat_turning_point",
                )
            )

    return {"passed": not issues, "issues": issues}

def _golden_issue(
    chapter_index: int, chapter: dict | None, problem: str, hint: str, focus: str | None = None
) -> dict:
    """构造一条黄金三章问题（带可定位的章节信息）。

    参数:
        chapter_index: 章节序号（从 1 起，用于缺章时的兜底展示）。
        chapter: 对应的大纲章节 dict；章节不存在时为 None。
        problem: 未通过的具体项（一句话，不含章节号）。
        hint: 为什么没过 + 怎么改。
        focus: 界面定位用的落点标记；目前仅 `beat_turning_point`（需标记转折的节拍）。

    返回:
        {"chapter_index", "chapter_id", "chapter_title", "problem", "hint", "focus"}。
    """
    chapter = chapter or {}
    return {
        "chapter_index": int(chapter_index),
        "chapter_id": chapter.get("id") or None,
        "chapter_title": str(chapter.get("title") or ""),
        "problem": problem,
        "hint": hint,
        "focus": focus,
    }

def _outline_chapters(bible: dict) -> list[dict]:
    """按序号展开设定库里的全部大纲章节。

    参数:
        bible: 设定库 dict。

    返回:
        大纲章节列表（按 index 升序）。
    """
    rows: list[dict] = []
    for act in (bible.get("outline") or {}).get("acts") or []:
        rows.extend(act.get("chapters") or [])
    return sorted(rows, key=lambda item: int(item.get("index") or 0))
