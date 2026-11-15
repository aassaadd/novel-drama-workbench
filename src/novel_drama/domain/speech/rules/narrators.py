"""旁白人格与章节切换点解析（纯函数）。

`_effective_switches` 把切换点展开成带起止序号的区间（end 缺省时延续到下一个切换点之前），
`narrator_persona` 按章节位置取当前生效人格，`narrator_plan` 生成逐章展开表供页面预览与校验共用。
"""

from __future__ import annotations


def effective_switches(cast: dict, order: dict[str, int], max_index: int) -> list[dict]:
    """把切换点展开成带起止序号的区间。

    参数:
        cast: `tts.cast` 配置。
        order: 章节 ID → 序号映射。
        max_index: 全剧最大章节序号。

    返回:
        `[{start, end, persona_id, reason, only}]`，按 start 升序。
    """
    raw = []
    for switch in cast.get("narrator_switches") or []:
        start = int(order.get(switch.get("at_chapter_id")) or 0)
        if not start:
            continue
        raw.append(
            {
                "start": start,
                "end": int(order.get(switch.get("end_chapter_id")) or 0) if switch.get("end_chapter_id") else 0,
                "persona_id": switch.get("persona_id"),
                "reason": switch.get("reason"),
                "only": switch.get("scope") == "this_chapter_only",
            }
        )
    raw.sort(key=lambda item: item["start"])
    for index, item in enumerate(raw):
        if item["only"]:
            item["end"] = item["start"]
        elif not item["end"]:
            following = raw[index + 1]["start"] if index + 1 < len(raw) else 0
            item["end"] = (following - 1) if following else max_index
    return raw


def narrator_persona(cast: dict, chapter: dict, order: dict[str, int]) -> dict | None:
    """按章节位置解析当前生效的旁白人格（含区间结束章节）。

    参数:
        cast: `tts.cast` 配置。
        chapter: 当前章节 dict。
        order: 章节 ID → 序号映射。

    返回:
        人格 dict；未配置时返回 None。
    """
    personas = {item.get("id"): item for item in cast.get("narrators") or [] if item.get("id")}
    current = int(order.get(chapter.get("id")) or chapter.get("index") or 0)
    max_index = max(order.values()) if order else current
    chosen = None
    for item in effective_switches(cast, order, max_index):
        if item["start"] <= current <= item["end"]:
            chosen = item["persona_id"]
    if chosen is None:
        chosen = cast.get("default_narrator_id")
    return personas.get(chosen) if chosen else None


def narrator_plan(cast: dict, chapters: list[dict]) -> list[dict]:
    """生成「每一章用哪个旁白」的展开表，供页面预览与校验共用。

    参数:
        cast: `tts.cast` 配置。
        chapters: 章节列表（需含 id/index/title）。

    返回:
        `[{chapter_id, index, title, persona_id, label, source}]`，source=switch|default。
    """
    order = {item.get("id"): int(item.get("index") or 0) for item in chapters if item.get("id")}
    max_index = max(order.values()) if order else 0
    personas = {item.get("id"): item for item in cast.get("narrators") or [] if item.get("id")}
    ranges = effective_switches(cast, order, max_index)
    plan = []
    for chapter in sorted(chapters, key=lambda item: int(item.get("index") or 0)):
        index = int(chapter.get("index") or 0)
        persona_id = cast.get("default_narrator_id")
        source = "default"
        for item in ranges:
            if item["start"] <= index <= item["end"]:
                persona_id = item["persona_id"]
                source = "switch"
        persona = personas.get(persona_id) or {}
        plan.append(
            {
                "chapter_id": chapter.get("id"),
                "index": index,
                "title": chapter.get("title"),
                "persona_id": persona_id,
                "label": persona.get("label") or "（未配置旁白人格）",
                "source": source,
            }
        )
    return plan
