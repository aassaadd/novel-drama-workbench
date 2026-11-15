"""角色合并的纯数据迁移：字段并入、关系改指、大纲与章节引用改指。

`character_merge` 只保留「读工程 → 调这里 → 写工程」的编排与登记清理；
本模块的函数全部是就地修改 dict，不做 IO，便于用真实工程数据并跑比对。
"""

from __future__ import annotations


def merge_fields(target: dict, source: dict) -> None:
    """把源角色的设定补进目标角色的空缺字段（不覆盖已有内容）。

    参数:
        target: 目标角色（就地修改）。
        source: 源角色。

    返回:
        无。
    """
    for key in (
        "voice_binding",
        "voice_profile",
        "appearance_prompt",
        "negative_prompt",
        "portrait_asset",
        "image_assets",
        "voice_description_zh",
        "voice_description_en",
        "appearance_desc_zh",
        "appearance_desc_en",
        "appearance_negative_zh",
        "appearance_negative_en",
    ):
        if not target.get(key) and source.get(key):
            target[key] = source[key]
    for key in ("gender", "age_range"):
        if (not target.get(key) or target.get(key) == "unknown") and source.get(key):
            target[key] = source[key]
    if not target.get("background") and source.get("background"):
        target["background"] = source["background"]
    personality = list(target.get("personality") or [])
    for word in source.get("personality") or []:
        if word not in personality:
            personality.append(word)
    if personality:
        target["personality"] = personality
    style = dict(target.get("speech_style") or {})
    for key, value in (source.get("speech_style") or {}).items():
        if key == "catchphrases":
            rows = list(style.get(key) or [])
            for phrase in value or []:
                if phrase not in rows:
                    rows.append(phrase)
            if rows:
                style[key] = rows
        elif not style.get(key) and value:
            style[key] = value
    if style:
        target["speech_style"] = style
    # 源角色的关系与备注并入目标角色
    source_state = source.get("state") or {}
    if source_state:
        state = dict(target.get("state") or {})
        notes = dict(state.get("relationship_notes") or {})
        for key, value in (source_state.get("relationship_notes") or {}).items():
            if key == source.get("id"):
                continue
            if key not in notes:
                notes[key] = value
        relations = list(state.get("relations") or [])
        known = {str(row.get("target_id")) for row in relations}
        for row in source_state.get("relations") or []:
            target_key = str(row.get("target_id") or "")
            if not target_key or target_key in (source.get("id"), target.get("id")) or target_key in known:
                continue
            relations.append(row)
            known.add(target_key)
        if notes:
            state["relationship_notes"] = notes
        if relations:
            state["relations"] = relations
        if state:
            target["state"] = state


def merge_relation_refs(table: dict, source_id: str, target_id: str) -> None:
    """把其它角色指向源角色的关系与备注改到目标角色。

    参数:
        table: 角色表（就地修改）。
        source_id: 源角色 ID。
        target_id: 目标角色 ID。

    返回:
        无。
    """
    for item in table.get("characters") or []:
        character_id = str(item.get("id"))
        if character_id == source_id:
            continue
        state = item.get("state") or {}
        changed = False
        notes = state.get("relationship_notes") or {}
        if source_id in notes:
            note = notes.pop(source_id)
            notes.setdefault(target_id, note)
            changed = True
        if character_id in notes:
            notes.pop(character_id, None)
            changed = True
        if changed:
            state["relationship_notes"] = notes
        relations = list(state.get("relations") or [])
        seen: set[str] = set()
        merged: list[dict] = []
        for row in relations:
            target_key = str(row.get("target_id") or "")
            key = target_id if target_key == source_id else target_key
            # 合并后不能留下指向自己的关系
            if key and (key == character_id or key in seen):
                changed = True
                continue
            if target_key == source_id:
                row = dict(row, target_id=target_id)
                changed = True
            if key:
                seen.add(key)
            merged.append(row)
        if changed:
            state["relations"] = merged
            item["state"] = state


def merge_outline(bible: dict, source_id: str, target_id: str) -> None:
    """迁移设定库里的节拍选角、章节视角与角色弧线。

    参数:
        bible: 设定库（就地修改）。
        source_id: 源角色 ID。
        target_id: 目标角色 ID。

    返回:
        无。
    """
    for act in (bible.get("outline") or {}).get("acts") or []:
        for chapter in act.get("chapters") or []:
            if chapter.get("pov_character_id") == source_id:
                chapter["pov_character_id"] = target_id
            for arc in chapter.get("character_arcs") or []:
                if arc.get("character_id") == source_id:
                    arc["character_id"] = target_id
            # 合并同章重复弧线，避免同一角色出现两次
            arcs = chapter.get("character_arcs")
            if isinstance(arcs, list):
                seen: set[str] = set()
                kept: list[dict] = []
                for arc in arcs:
                    key = str(arc.get("character_id") or "")
                    if key and key in seen:
                        continue
                    if key:
                        seen.add(key)
                    kept.append(arc)
                if kept:
                    chapter["character_arcs"] = kept
            for beat in chapter.get("beats") or []:
                ids: list[str] = []
                for raw in beat.get("character_ids") or []:
                    key = target_id if str(raw) == source_id else str(raw)
                    if key not in ids:
                        ids.append(key)
                beat["character_ids"] = ids


def merge_chapter_file(chapter: dict, source_id: str, target_id: str) -> bool:
    """迁移单个章节文件里的角色引用。

    参数:
        chapter: 章节 dict（就地修改）。
        source_id: 源角色 ID。
        target_id: 目标角色 ID。

    返回:
        True 表示有改动。
    """
    changed = False
    if chapter.get("pov_character_id") == source_id:
        chapter["pov_character_id"] = target_id
        changed = True
    for scene in chapter.get("scenes") or []:
        ids = scene.get("character_ids") or []
        if source_id in ids:
            merged: list[str] = []
            for raw in ids:
                key = target_id if str(raw) == source_id else str(raw)
                if key not in merged:
                    merged.append(key)
            scene["character_ids"] = merged
            changed = True
        for line in scene.get("lines") or []:
            if line.get("speaker_id") == source_id:
                line["speaker_id"] = target_id
                changed = True
    overrides = chapter.get("voice_overrides") or {}
    if source_id in overrides:
        override = overrides.pop(source_id)
        overrides.setdefault(target_id, override)
        chapter["voice_overrides"] = overrides
        changed = True
    for record in chapter.get("continuations") or []:
        context = record.get("context") or {}
        cards = context.get("include_character_cards")
        if isinstance(cards, list) and source_id in cards:
            merged = []
            for raw in cards:
                key = target_id if str(raw) == source_id else str(raw)
                if key not in merged:
                    merged.append(key)
            context["include_character_cards"] = merged
            changed = True
    return changed
