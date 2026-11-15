"""世界库的写用例：导入/更新条目、删除整套世界并解除章节绑定。

CLI（`world apply/remove`）与 Web 共用本模块。
"""

from __future__ import annotations

from ...adapters.persistence import ProjectStore
from ...adapters.persistence.settings_store import SettingsStore
from ...adapters.persistence.world_store import WorldStore
from ...domain.shared.errors import PreconditionError
from .world import clean_world_entry


def chapter_refs(store: ProjectStore, world_id: str) -> list[dict]:
    """查找大纲中引用了指定世界的章节。

    参数:
        store: 工程。
        world_id: 世界 ID。

    返回:
        `[{"id": 章节 ID, "title": 章节标题}]`；没有引用时返回空列表。
    """
    bible = SettingsStore(store.root).load_or_none() or {}
    refs: list[dict] = []
    for act in (bible.get("outline") or {}).get("acts") or []:
        for chapter in act.get("chapters") or []:
            if world_id in (chapter.get("world_ids") or []):
                refs.append({"id": chapter.get("id"), "title": chapter.get("title")})
    return refs


def unbind_world(store: ProjectStore, world_id: str) -> dict:
    """删除世界前清理相关引用：大纲章节 `world_ids` 中的该世界。

    说明：世界 = 世界观总述 + 分类条目；删除是整模块删除，因此章节绑定一并解除。
    章节文件与场景 schema 不允许世界绑定字段，这里只处理大纲。

    参数:
        store: 工程。
        world_id: 被删除的世界 ID。

    返回:
        `{"outline_chapters": n}` 统计。
    """
    stats = {"outline_chapters": 0}
    settings = SettingsStore(store.root)
    bible = settings.load_or_none()
    if not bible:
        return stats
    changed = 0
    for act in (bible.get("outline") or {}).get("acts") or []:
        for chapter in act.get("chapters") or []:
            bound = [str(value) for value in (chapter.get("world_ids") or []) if value]
            if world_id not in bound:
                continue
            chapter["world_ids"] = [value for value in bound if value != world_id]
            changed += 1
    if changed:
        settings.save(bible)
    stats["outline_chapters"] = changed
    return stats


def upsert_worlds(store: ProjectStore, rows: list[dict]) -> dict:
    """导入世界条目：带 id 且已存在则更新，否则新增。

    参数:
        store: 工程。
        rows: 世界条目数组。

    返回:
        `{"worlds": [...], "count": n}`。

    异常:
        ValueError: 条目不是对象。
    """
    worlds = WorldStore(store.root)
    worlds.ensure()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("世界条目必须是 JSON 对象")
        world_id = str(row.get("id") or "").strip()
        if world_id and worlds.get(world_id):
            worlds.update(world_id, row)
            continue
        entry = dict(row) if world_id else clean_world_entry(row)
        worlds.add(entry)
    table = worlds.list_worlds()
    return {"worlds": table, "count": len(table)}


def remove_world(store: ProjectStore, world_id: str) -> dict:
    """删除整套世界（总述 + 地点/势力/术语/其它条目）并解除相关引用。

    参数:
        store: 工程。
        world_id: 世界 ID。

    返回:
        `{"removed", "id", "name", "chapters", "unbound"}`。

    异常:
        PreconditionError: 世界不存在。
    """
    worlds = WorldStore(store.root)
    worlds.ensure()
    existing = worlds.get(world_id)
    if existing is None:
        raise PreconditionError(f"世界不存在: {world_id}")
    refs = chapter_refs(store, world_id)
    # 先解绑再删除：清理失败时世界仍然存在，不会留下悬空引用
    unbound = unbind_world(store, world_id)
    if not worlds.remove(world_id):
        raise PreconditionError(f"世界不存在: {world_id}")
    return {"removed": True, "id": world_id, "name": existing.get("name"), "chapters": refs, "unbound": unbound}
