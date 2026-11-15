"""角色合并：把新建的角色卡并到已有角色，并迁移全部引用。

合并是「会改动引用」的操作，页面必须先看影响面再二次确认：
`merge_impact()` 只统计不写盘，`merge_characters()` 才真正迁移并删除源角色。

迁移范围：
- 设定库：大纲节拍选角、章节视角、角色弧线；
- 章节文件：场景出场、台词说话人、章节音色覆盖、续写上下文里的角色卡；
- 角色表：其它角色指向源角色的关系与关系备注、源角色的别名/设定并入目标角色；
- 角色登记：待定登记里同名候选清掉，「本章新建」里的源角色改成目标角色。
"""

from __future__ import annotations

import json
import re

from .models import entities
from .store import ProjectStore
from .store.settings_store import SettingsStore
from .writing.cast_sync import load_pending, save_pending

#: 归一化姓名用的空白
_SPACE_RE = re.compile(r"\s+")


def _norm(name: str) -> str:
    """姓名归一化（去空白、小写），用于别名去重。

    参数:
        name: 姓名或别名。

    返回:
        归一化字符串。
    """
    return _SPACE_RE.sub("", str(name or "")).casefold()


def _find(table: dict, character_id: str) -> dict:
    """在角色表里按 ID 取角色。

    参数:
        table: characters.json 内容。
        character_id: 角色 ID。

    返回:
        角色 dict。

    异常:
        ValueError: 角色不存在。
    """
    for item in table.get("characters") or []:
        if str(item.get("id")) == character_id:
            return item
    raise ValueError(f"角色不存在：{character_id}")


def _pair(store: ProjectStore, source_id: str, target_id: str) -> tuple[dict, dict]:
    """校验并取出源角色与目标角色。

    参数:
        store: 工程。
        source_id: 要合并掉的角色 ID。
        target_id: 合并到的角色 ID。

    返回:
        `(source, target)`。

    异常:
        ValueError: 缺少 ID、同一个角色、旁白或角色不存在。
    """
    if not source_id or not target_id:
        raise ValueError("合并需要同时给出源角色与目标角色")
    if source_id == target_id:
        raise ValueError("不能把角色合并到自己")
    if entities.NARRATOR_ID in (source_id, target_id):
        raise ValueError("旁白是保留角色，不能参与合并")
    table = store.load_characters()
    source = _find(table, source_id)
    target = _find(table, target_id)
    if source.get("role") == "narrator" or target.get("role") == "narrator":
        raise ValueError("旁白是保留角色，不能参与合并")
    return source, target


def _outline_refs(bible: dict, source_id: str) -> dict:
    """统计设定库里的源角色引用。

    参数:
        bible: 设定库。
        source_id: 角色 ID。

    返回:
        `{beats, pov, arcs}`。
    """
    beats = 0
    pov = 0
    arcs = 0
    for act in (bible.get("outline") or {}).get("acts") or []:
        for chapter in act.get("chapters") or []:
            if chapter.get("pov_character_id") == source_id:
                pov += 1
            arcs += sum(1 for row in chapter.get("character_arcs") or [] if row.get("character_id") == source_id)
            for beat in chapter.get("beats") or []:
                if source_id in (beat.get("character_ids") or []):
                    beats += 1
    return {"beats": beats, "pov": pov, "arcs": arcs}


def _chapter_refs(store: ProjectStore, source_id: str) -> dict:
    """统计章节文件里的源角色引用。

    参数:
        store: 工程。
        source_id: 角色 ID。

    返回:
        `{scenes, lines, overrides, poc}`。
    """
    scenes = 0
    lines = 0
    overrides = 0
    for meta in store.list_chapters():
        try:
            chapter = store.load_chapter(meta["id"])
        except (OSError, ValueError):
            continue
        for scene in chapter.get("scenes") or []:
            if source_id in (scene.get("character_ids") or []):
                scenes += 1
            for line in scene.get("lines") or []:
                if line.get("speaker_id") == source_id:
                    lines += 1
        if source_id in (chapter.get("voice_overrides") or {}):
            overrides += 1
    return {"scenes": scenes, "lines": lines, "overrides": overrides}


def _relation_refs(table: dict, source_id: str) -> int:
    """统计其它角色指向源角色的关系条数。

    参数:
        table: 角色表。
        source_id: 角色 ID。

    返回:
        关系条数（关系行与关系备注各算一处）。
    """
    total = 0
    for item in table.get("characters") or []:
        if str(item.get("id")) == source_id:
            continue
        state = item.get("state") or {}
        notes = state.get("relationship_notes") or {}
        if source_id in notes:
            total += 1
        total += sum(1 for row in state.get("relations") or [] if str(row.get("target_id")) == source_id)
    return total


def merge_impact(store: ProjectStore, source_id: str, target_id: str) -> dict:
    """统计把源角色并到目标角色会影响多少处引用（只读）。

    参数:
        store: 工程。
        source_id: 要合并掉的角色 ID。
        target_id: 合并到的角色 ID。

    返回:
        `{source, target, outline, chapters, relations, total}`。
    """
    source, target = _pair(store, source_id, target_id)
    table = store.load_characters()
    outline = {"beats": 0, "pov": 0, "arcs": 0}
    settings_store = SettingsStore(store.root)
    if settings_store.exists():
        outline = _outline_refs(settings_store.load(), source_id)
    chapters = _chapter_refs(store, source_id)
    relations = _relation_refs(table, source_id)
    total = sum(outline.values()) + sum(chapters.values()) + relations
    return {
        "source": {"id": source_id, "name": source.get("name") or ""},
        "target": {"id": target_id, "name": target.get("name") or ""},
        "outline": outline,
        "chapters": chapters,
        "relations": relations,
        "total": total,
    }


def _merge_aliases(target: dict, source: dict) -> None:
    """把源角色的姓名与别名并入目标角色。

    参数:
        target: 目标角色（就地修改）。
        source: 源角色。

    返回:
        无。
    """
    merged = list(target.get("aliases") or [])
    known = {_norm(item) for item in [target.get("name"), *merged] if _norm(item)}
    for alias in [source.get("name"), *(source.get("aliases") or [])]:
        norm = _norm(alias)
        if norm and norm not in known:
            merged.append(alias)
            known.add(norm)
    if merged:
        target["aliases"] = merged


def _merge_fields(target: dict, source: dict) -> None:
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


def _merge_relation_refs(table: dict, source_id: str, target_id: str) -> None:
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


def _merge_outline(bible: dict, source_id: str, target_id: str) -> None:
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


def _merge_chapter_file(chapter: dict, source_id: str, target_id: str) -> bool:
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


def _clean_registries(store: ProjectStore, source: dict, target_id: str, target_name: str) -> None:
    """清理角色登记：待定同名候选与「本章新建」里的源角色。

    参数:
        store: 工程。
        source: 源角色。
        target_id: 目标角色 ID。
        target_name: 目标角色姓名。

    返回:
        无。
    """
    names = {_norm(source.get("name")), *(_norm(alias) for alias in source.get("aliases") or [])}
    pending = load_pending(store)
    removed = [key for key in pending if key in names]
    for key in removed:
        pending.pop(key, None)
    if removed:
        save_pending(store, pending)

    path = store.root / ".ndw" / "cast_created.json"
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    changed = False
    for row in (data.get("chapters") or {}).values():
        items = list((row or {}).get("items") or [])
        kept: list[dict] = []
        known: set[str] = set()
        for item in items:
            character_id = str(item.get("id") or "")
            if character_id == source.get("id"):
                item = dict(item, id=target_id, name=target_name)
                character_id = target_id
                changed = True
            if character_id in known:
                changed = True
                continue
            known.add(character_id)
            kept.append(item)
        if kept:
            row["items"] = kept
    if changed:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def merge_characters(store: ProjectStore, source_id: str, target_id: str) -> dict:
    """把源角色合并到目标角色，迁移全部引用后删除源角色。

    参数:
        store: 工程。
        source_id: 要合并掉的角色 ID。
        target_id: 合并到的角色 ID。

    返回:
        `{merged: True, source, target, impact, revision}`。

    异常:
        ValueError: 参数不合法、角色不存在或试图合并旁白。
    """
    impact = merge_impact(store, source_id, target_id)
    table = store.load_characters()
    source = _find(table, source_id)
    target = _find(table, target_id)

    _merge_aliases(target, source)
    _merge_fields(target, source)
    _merge_relation_refs(table, source_id, target_id)
    table["characters"] = [item for item in table["characters"] if str(item.get("id")) != source_id]
    store.save_characters(table)

    settings_store = SettingsStore(store.root)
    revision = None
    if settings_store.exists():
        bible = settings_store.load()
        _merge_outline(bible, source_id, target_id)
        bible = settings_store.save(bible)
        revision = bible.get("settings_revision")

    changed_chapters = 0
    for meta in store.list_chapters():
        try:
            chapter = store.load_chapter(meta["id"])
        except (OSError, ValueError):
            continue
        if _merge_chapter_file(chapter, source_id, target_id):
            store.save_chapter(chapter)
            changed_chapters += 1

    _clean_registries(store, source, target_id, target.get("name") or "")

    return {
        "merged": True,
        "source": {"id": source_id, "name": source.get("name") or ""},
        "target": {"id": target_id, "name": target.get("name") or ""},
        "impact": {**impact, "changed_chapters": changed_chapters},
        "settings_revision": revision,
    }
