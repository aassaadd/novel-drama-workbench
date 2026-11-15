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

from .adapters.persistence import ProjectStore
from .adapters.persistence.settings_store import SettingsStore
from .application.cast.character_crud import (
    chapter_references,
    outline_references,
    relation_references,
)
from .application.cast.sync_cast import load_pending, save_pending
from .domain.cast.rules.merge import (
    merge_chapter_file as _merge_chapter_file,
)
from .domain.cast.rules.merge import (
    merge_fields as _merge_fields,
)
from .domain.cast.rules.merge import (
    merge_outline as _merge_outline,
)
from .domain.cast.rules.merge import (
    merge_relation_refs as _merge_relation_refs,
)
from .domain.cast.rules.naming import alias_key, merge_aliases
from .models import entities


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
    """统计设定库里的源角色引用（口径与 `character_refs` 共用一份实现）。

    参数:
        bible: 设定库。
        source_id: 角色 ID。

    返回:
        `{beats, pov, arcs}`。
    """
    refs = outline_references(bible, source_id)
    return {"beats": refs["beats"], "pov": refs["pov"], "arcs": refs["arcs"]}


def _chapter_refs(store: ProjectStore, source_id: str) -> dict:
    """统计章节文件里的源角色引用（口径与 `character_refs` 共用一份实现）。

    参数:
        store: 工程。
        source_id: 角色 ID。

    返回:
        `{scenes, lines, overrides}`。
    """
    refs = chapter_references(store, source_id)
    return {"scenes": refs["scenes"], "lines": refs["lines"], "overrides": refs["overrides"]}


def _relation_refs(table: dict, source_id: str) -> int:
    """统计其它角色指向源角色的关系条数（口径与 `character_refs` 共用一份实现）。

    参数:
        table: 角色表。
        source_id: 角色 ID。

    返回:
        关系条数（关系行与关系备注各算一处）。
    """
    refs = relation_references(table, source_id)
    return refs["notes"] + refs["relations"]


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
    names = {alias_key(source.get("name")), *(alias_key(alias) for alias in source.get("aliases") or [])}
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

    merge_aliases(target, source)
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
