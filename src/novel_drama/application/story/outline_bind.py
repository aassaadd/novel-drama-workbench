"""大纲与章节的角色/世界绑定：POV、节拍角色、绑定统计与完整性校验。"""

from __future__ import annotations

from ...adapters.persistence import ProjectStore
from ...adapters.persistence.settings_store import SettingsStore
from ...adapters.persistence.world_store import WorldStore
from ...models import entities


def bind_outline_context(bible: dict, worlds: list[dict], characters: list[dict]) -> dict:
    """把大纲章节与节拍绑定到世界条目与角色（就地修改 bible）。

    参数:
        bible: 设定库 dict。
        worlds: 世界条目列表；为空时把每章 world_ids 写成空数组。
        characters: 角色表（含旁白，内部会过滤）。

    返回:
        {"chapters": 绑定章节数, "beats": 绑定节拍数, "worlds": 可用世界数}。
    """
    cast = [item for item in characters or [] if item.get("id") and item.get("role") != "narrator" and item.get("id") != entities.NARRATOR_ID]
    # 节拍可以用旁白兜底：旁白是保留实体，允许出现在 character_ids 里
    cast_ids = {item["id"] for item in cast} | {entities.NARRATOR_ID}
    protagonist = next((item for item in cast if item.get("role") == "protagonist"), cast[0] if cast else None)
    order = ([protagonist] + [item for item in cast if item is not protagonist]) if protagonist else []
    world_ids = [item["id"] for item in worlds or [] if item.get("id")]

    chapters_bound = 0
    beats_bound = 0
    index = 0
    for act in (bible.get("outline") or {}).get("acts") or []:
        for chapter in act.get("chapters") or []:
            if world_ids:
                chapter["world_ids"] = [world_ids[index % len(world_ids)]]
            else:
                chapter["world_ids"] = []
            existing_pov = chapter.get("pov_character_id")
            if existing_pov in cast_ids:
                pov = existing_pov
            elif order:
                pov = order[index % len(order)]["id"]
            else:
                pov = None
            chapter["pov_character_id"] = pov
            chapters_bound += 1
            for beat in chapter.get("beats") or []:
                valid = [cid for cid in (beat.get("character_ids") or []) if cid in cast_ids]
                if not valid:
                    # 节拍没有点名任何角色时回退旁白，不再硬绑主角
                    valid = [entities.NARRATOR_ID]
                beat["character_ids"] = valid
                beats_bound += 1
            index += 1
    return {"chapters": chapters_bound, "beats": beats_bound, "worlds": len(world_ids)}

def sanitize_outline_character_ids(bible: dict, characters: list[dict]) -> dict:
    """过滤大纲节拍里的非法角色引用（`binding=none` 专用）。

    与 `bind_outline_context()` 的差异（两者都会丢弃非法引用，但后续处理不同）：
    - `auto`：过滤后若为空，回落到本章 POV，并按幕轮转绑定世界；
    - `none`：只做清洗，**不回落到 POV、不改世界绑定**，保持空数组。

    判定规则：只保留角色表里真实存在的非旁白 ID；人名、空串、重复项一律丢弃。

    参数:
        bible: 设定库 dict（就地修改大纲里的节拍）。
        characters: 角色表（含旁白也可，内部会过滤）。

    返回:
        `{"chapters": 处理章节数, "kept": 保留引用数, "dropped": 丢弃引用数}`。
    """
    cast_ids = {
        str(item["id"])
        for item in characters or []
        if item.get("id") and item.get("role") != "narrator" and item.get("id") != entities.NARRATOR_ID
    }
    # 旁白是保留实体，节拍里的旁白兜底不算非法引用
    cast_ids.add(entities.NARRATOR_ID)
    chapters = 0
    kept = 0
    dropped = 0
    for act in (bible.get("outline") or {}).get("acts") or []:
        for chapter in act.get("chapters") or []:
            chapters += 1
            for beat in chapter.get("beats") or []:
                clean: list[str] = []
                seen: set[str] = set()
                for raw in beat.get("character_ids") or []:
                    text = str(raw or "").strip()
                    # 非 ID（人名/空串）与重复项都丢弃，但计数用于提示
                    if not text or text in seen or text not in cast_ids:
                        dropped += 1
                        continue
                    seen.add(text)
                    clean.append(text)
                kept += len(clean)
                beat["character_ids"] = clean
    return {"chapters": chapters, "kept": kept, "dropped": dropped}

def outline_binding_stats(bible: dict) -> dict:
    """统计大纲里实际生效的角色绑定情况（供完成摘要展示）。

    参数:
        bible: 设定库 dict。

    返回:
        `{"characters": 去重角色数, "refs": 节拍角色引用总数, "chapters": 有角色引用的章节数}`。
    """
    character_ids: set[str] = set()
    refs = 0
    chapters = 0
    for act in (bible.get("outline") or {}).get("acts") or []:
        for chapter in act.get("chapters") or []:
            touched = False
            for beat in chapter.get("beats") or []:
                ids = [str(item) for item in (beat.get("character_ids") or []) if str(item).strip()]
                if not ids:
                    continue
                touched = True
                refs += len(ids)
                character_ids.update(ids)
            if touched:
                chapters += 1
    return {"characters": len(character_ids), "refs": refs, "chapters": chapters}

def bind_outline_characters(store: ProjectStore, bible: dict) -> dict:
    """把大纲章节与节拍绑定到已有世界与人物（兼容旧函数名）。

    读取工程内的世界列表后交给 `bind_outline_context`，每章按幕轮转绑定 1 个世界；
    工程里还没有世界条目时，每章 `world_ids` 写成空数组。

    参数:
        store: 工程（读取 worlds.json 与 characters.json）。
        bible: 设定库 dict。

    返回:
        {"chapters": 绑定章节数, "beats": 绑定节拍数, "worlds": 可用世界数}。
    """
    world_store = WorldStore(store.root)
    worlds = world_store.list_worlds() if world_store.exists() else []
    return bind_outline_context(bible, worlds, store.load_characters()["characters"])

def _effective_pov_mode(entry: dict | None, chapter: dict, style: dict) -> str:
    """计算本章实际生效的视角写法。

    优先级：设定库章节的 pov_mode → 章节副本 pov_mode → 全书 style.pov → 第三人称有限。

    参数:
        entry: 设定库大纲章节；没有绑定时为 None。
        chapter: 章节文件。
        style: 设定库 style。

    返回:
        视角枚举值（first/second/third_limited/third_omniscient）。
    """
    for value in ((entry or {}).get("pov_mode"), chapter.get("pov_mode"), style.get("pov")):
        text = str(value or "").strip()
        if text:
            return text
    return "third_limited"

def sync_chapter_pov(
    store: ProjectStore,
    chapter_id: str,
    pov_character_id: str | None,
    pov_mode: str | None = None,
    update_mode: bool = False,
) -> dict:
    """把本章 POV 同步写进设定库与章节文件（消灭两份死数据）。

    设定库是主源，章节文件只作为副本保存；两处同时写入，页面与生成读取的结果一致。

    参数:
        store: 工程。
        chapter_id: 章节 ID。
        pov_character_id: 视角人物 ID；None 表示不绑定（全知视角常用）。
        pov_mode: 视角写法覆盖；None 表示跟随全书。
        update_mode: 是否改写 pov_mode（False 时只改人物，不动视角写法）。

    返回:
        {"chapter": 章节ID, "pov_character_id", "pov_mode", "settings_revision"}。

    异常:
        ValueError: 角色不存在，或视角写法非法。
    """
    allowed_modes = {"first", "second", "third_limited", "third_omniscient", None, ""}
    if pov_mode not in allowed_modes:
        raise ValueError(f"视角写法非法：{pov_mode}（可选 first/second/third_limited/third_omniscient）")
    settings = SettingsStore(store.root)
    bible = settings.ensure()
    entry = settings.find_outline_chapter(bible, chapter_id)
    cast_ids = {
        str(item.get("id"))
        for item in store.load_characters()["characters"]
        if item.get("id") and item.get("role") != "narrator"
    }
    if pov_character_id and pov_character_id not in cast_ids:
        raise ValueError(f"角色不存在：{pov_character_id}；请先在人物页生成人物")
    if entry is not None:
        entry["pov_character_id"] = pov_character_id or None
        if update_mode:
            entry["pov_mode"] = pov_mode or None
        bible = settings.save(bible)
    chapter = None
    try:
        chapter = store.load_chapter(chapter_id)
    except FileNotFoundError:
        chapter = None
    if chapter is not None:
        chapter["pov_character_id"] = pov_character_id or None
        if update_mode:
            chapter["pov_mode"] = pov_mode or None
        store.save_chapter(chapter)
    return {
        "chapter": chapter_id,
        "pov_character_id": pov_character_id or None,
        "pov_mode": (entry or {}).get("pov_mode") if entry is not None else (chapter or {}).get("pov_mode"),
        "settings_revision": bible.get("settings_revision"),
    }

def bind_beat_characters(store: ProjectStore, chapter_id: str, beat_id: str, character_ids: list[str]) -> dict:
    """把某个节拍的出场角色写入设定库（页面「节拍选角」）。

    只改设定库里的计划选角，不触碰正文与章节文件；返回最新绑定供页面立即刷新。

    参数:
        store: 工程。
        chapter_id: 章节 ID。
        beat_id: 节拍 ID。
        character_ids: 角色 ID 列表（去重，按传入顺序）。

    返回:
        `chapter_binding` 的返回值。

    异常:
        ValueError: 大纲章节/节拍不存在，或角色 ID 非法。
    """
    settings = SettingsStore(store.root)
    bible = settings.ensure()
    entry = settings.find_outline_chapter(bible, chapter_id)
    if entry is None:
        raise ValueError(f"本章还没有大纲条目：{chapter_id}；请先生成或落地大纲")
    cast_ids = {
        str(item.get("id"))
        for item in store.load_characters()["characters"]
        if item.get("id") and item.get("role") != "narrator" and item.get("id") != entities.NARRATOR_ID
    }
    beat = next((item for item in (entry.get("beats") or []) if item.get("id") == beat_id), None)
    if beat is None:
        raise ValueError(f"节拍不存在：{beat_id}")
    clean: list[str] = []
    for raw in character_ids or []:
        value = str(raw or "").strip()
        if not value:
            continue
        if value not in cast_ids:
            raise ValueError(f"角色不存在或不可出场：{value}")
        if value not in clean:
            clean.append(value)
    beat["character_ids"] = clean
    settings.save(bible)
    return chapter_binding(store, chapter_id)

def chapter_binding(store: ProjectStore, chapter_id: str) -> dict:
    """汇总本章的角色绑定、实际出场与对账结果（只读派生，不落盘）。

    实际出场从 `scenes[].character_ids` 与 `lines[].speaker_id` 现算；计划出场只读大纲节拍。

    参数:
        store: 工程。
        chapter_id: 章节 ID。

    返回:
        {
          chapter_id, pov: {id,name}|null, pov_mode, blocking: [...], warnings: [...],
          beats: [...], characters: [...],
          audit: {planned_not_used, unplanned_appeared, pov_not_in_prose, missing_refs, unknown_speakers}
        }。
    """
    settings = SettingsStore(store.root)
    bible = settings.ensure()
    entry = settings.find_outline_chapter(bible, chapter_id)
    try:
        chapter = store.load_chapter(chapter_id)
    except FileNotFoundError:
        # 大纲章还没落地：绑定信息仍要能看，实际出场视为空
        chapter = {"pov_character_id": None, "scenes": []}
    style = bible.get("style") or {}
    characters = [
        item
        for item in store.load_characters()["characters"]
        if item.get("id") and item.get("role") != "narrator"
    ]
    cast_by_id = {str(item["id"]): item for item in characters}
    pov_id = (entry or {}).get("pov_character_id") or chapter.get("pov_character_id")
    if pov_id == entities.NARRATOR_ID:
        pov_id = None
    pov_mode = _effective_pov_mode(entry, chapter, style)
    beats = (entry or {}).get("beats") or []
    planned_counts: dict[str, int] = {}
    beat_rows: list[dict] = []
    for position, beat in enumerate(beats, start=1):
        ids = [str(cid) for cid in (beat.get("character_ids") or []) if str(cid).strip()]
        for cid in ids:
            planned_counts[cid] = planned_counts.get(cid, 0) + 1
        beat_rows.append(
            {
                "beat_id": beat.get("id"),
                "index": position,
                "summary": beat.get("summary") or "",
                "character_ids": ids,
                "characters": [
                    {
                        "id": cid,
                        "name": (cast_by_id.get(cid) or {}).get("name") or "已删除角色",
                        "role": (cast_by_id.get(cid) or {}).get("role"),
                    }
                    for cid in ids
                ],
            }
        )
    # 实际出场：场景标签 + 台词说话人
    lines_by_char: dict[str, int] = {}
    scenes_by_char: dict[str, int] = {}
    unknown: dict[str, dict] = {}
    for scene in chapter.get("scenes") or []:
        for cid in scene.get("character_ids") or []:
            key = str(cid or "").strip()
            if key and key not in (entities.NARRATOR_ID, entities.UNKNOWN_SPEAKER_ID):
                scenes_by_char[key] = scenes_by_char.get(key, 0) + 1
        for line in scene.get("lines") or []:
            key = str(line.get("speaker_id") or "").strip()
            if not key or key == entities.NARRATOR_ID:
                continue
            lines_by_char[key] = lines_by_char.get(key, 0) + 1
            if key == entities.UNKNOWN_SPEAKER_ID or key not in cast_by_id:
                row = unknown.setdefault(key, {"speaker_id": key, "lines": 0, "sample": ""})
                row["lines"] += 1
                if not row["sample"]:
                    row["sample"] = str(line.get("text") or "")[:60]
    actual_ids = {cid for cid, count in lines_by_char.items() if count} | {
        cid for cid, count in scenes_by_char.items() if count
    }
    actual_ids = {cid for cid in actual_ids if cid not in (entities.UNKNOWN_SPEAKER_ID,)}
    rows: list[dict] = []
    for cid in sorted(set(cast_by_id) | set(planned_counts) | actual_ids | ({pov_id} if pov_id else set())):
        if cid in (entities.NARRATOR_ID, entities.UNKNOWN_SPEAKER_ID):
            continue
        item = cast_by_id.get(cid) or {}
        rows.append(
            {
                "id": cid,
                "name": item.get("name") or "已删除角色",
                "role": item.get("role") or "minor",
                "planned": cid in planned_counts,
                "planned_beats": planned_counts.get(cid, 0),
                "lines": lines_by_char.get(cid, 0),
                "scenes": scenes_by_char.get(cid, 0),
                "is_pov": cid == pov_id,
            }
        )
    missing_refs: list[dict] = []
    for cid in planned_counts:
        if cid not in cast_by_id:
            missing_refs.append({"kind": "beat", "id": cid})
    if pov_id and pov_id not in cast_by_id:
        missing_refs.append({"kind": "pov", "id": pov_id})
    blocking: list[dict] = []
    warnings: list[dict] = []
    if pov_mode != "third_omniscient" and not pov_id:
        blocking.append(
            {
                "code": "BINDING_POV_MISSING",
                "message": "本章还没有指定视角人物：请选择本章视角人物，或显式切换为全知视角",
            }
        )
    if not planned_counts and not pov_id:
        blocking.append(
            {
                "code": "BINDING_CHARACTER_MISSING",
                "message": "本章还没有绑定任何角色：请先在大纲里给节拍选择出场角色",
            }
        )
    empty_beats = [row["index"] for row in beat_rows if not row["character_ids"]]
    if empty_beats:
        warnings.append(
            {
                "code": "BINDING_BEAT_EMPTY",
                "message": "以下节拍还没有指定出场角色：第 " + "、".join(str(i) for i in empty_beats) + " 个",
            }
        )
    planned_not_used = [
        {"id": row["id"], "name": row["name"]}
        for row in rows
        if row["planned"] and row["lines"] == 0 and row["scenes"] == 0
    ]
    if planned_not_used:
        warnings.append(
            {
                "code": "PLAN_UNUSED_CHARACTER",
                "message": "计划出场但正文里没有出现：" + "、".join(item["name"] for item in planned_not_used),
            }
        )
    unplanned = [
        {"id": row["id"], "name": row["name"]}
        for row in rows
        if not row["planned"] and (row["lines"] or row["scenes"]) and row["id"] in cast_by_id
    ]
    if unplanned:
        warnings.append(
            {
                "code": "PLAN_UNPLANNED_CHARACTER",
                "message": "正文里出现了计划外角色：" + "、".join(item["name"] for item in unplanned),
            }
        )
    pov_row = next((row for row in rows if row["id"] == pov_id), None) if pov_id else None
    pov_not_in_prose = bool(pov_id and pov_row and pov_row["lines"] == 0 and pov_row["scenes"] == 0)
    if pov_not_in_prose:
        warnings.append(
            {"code": "POV_NOT_IN_PROSE", "message": f"视角人物「{pov_row['name']}」没有在正文场景或台词里出现"}
        )
    if unknown:
        warnings.append(
            {
                "code": "UNKNOWN_SPEAKER",
                "message": f"有 {sum(item['lines'] for item in unknown.values())} 行台词无法对应到角色卡",
            }
        )
    pov_name = (cast_by_id.get(pov_id) or {}).get("name") if pov_id else None
    return {
        "chapter_id": chapter_id,
        "pov": {"id": pov_id, "name": pov_name or ("已删除角色" if pov_id else None)} if pov_id else None,
        "pov_mode": pov_mode,
        "blocking": blocking,
        "warnings": warnings,
        "beats": beat_rows,
        "characters": rows,
        "audit": {
            "planned_not_used": planned_not_used,
            "unplanned_appeared": unplanned,
            "pov_not_in_prose": pov_not_in_prose,
            "missing_refs": missing_refs,
            "unknown_speakers": list(unknown.values()),
        },
    }

def validate_chapter_binding(store: ProjectStore, chapter_id: str) -> dict:
    """校验章节是否已绑定可用的世界、视角人物与角色。

    POV 缺失时阻断生成（除非本章显式设为全知视角）；不再静默套用默认主角。

    参数:
        store: 工程。
        chapter_id: 章节 ID。

    返回:
        {"chapter_id", "source", "world_ids", "pov_character_id", "pov_mode", "character_ids"}。

    异常:
        ValueError: 绑定的世界条目不存在、缺少视角人物或缺少非旁白角色。
    """
    binding = chapter_binding(store, chapter_id)
    characters = [
        item
        for item in store.load_characters()["characters"]
        if item.get("id") and item.get("role") != "narrator" and item.get("id") != entities.NARRATOR_ID
    ]
    cast_ids = {item["id"] for item in characters}
    settings_store = SettingsStore(store.root)
    bible = settings_store.ensure()
    entry = settings_store.find_outline_chapter(bible, chapter_id)
    world_ids = list((entry or {}).get("world_ids") or [])
    if world_ids:
        world_store = WorldStore(store.root)
        known = {item.get("id") for item in (world_store.list_worlds() if world_store.exists() else [])}
        missing = [world_id for world_id in world_ids if world_id not in known]
        if missing:
            raise ValueError(f"本章绑定的世界条目不存在：{'、'.join(missing)}；请先在世界页生成并绑定世界")
    blocking = binding.get("blocking") or []
    if blocking:
        raise ValueError(blocking[0]["message"])
    linked = {row["id"] for row in binding["characters"] if row["planned"] or row["is_pov"]}
    valid = linked & cast_ids
    if not valid:
        raise ValueError("本章还没有绑定角色：请先在人物页生成主要人物并在大纲页绑定")
    return {
        "chapter_id": chapter_id,
        "source": "outline" if entry else "bible",
        "world_ids": world_ids,
        "pov_character_id": binding.get("pov", {}).get("id") if binding.get("pov") else None,
        "pov_mode": binding.get("pov_mode"),
        "character_ids": sorted(valid),
    }


def update_chapter_binding(
    store: ProjectStore,
    chapter_id: str,
    world_ids: list[str] | None = None,
    pov_character_id: str | None = None,
    pov_mode: str | None = None,
) -> dict:
    """更新大纲某一章的世界 / POV 绑定（页面与 CLI 共用）。

    参数:
        store: 工程。
        chapter_id: 大纲章节 ID（`chp_...`）。
        world_ids: 新的世界 ID 列表；None 表示不改。
        pov_character_id: 新的 POV 角色 ID；空串表示清空；None 表示不改。
        pov_mode: 视角写法；None 表示不改。

    返回:
        `{chapter, world_ids, pov_character_id, pov_mode, settings_revision}`。

    异常:
        TaskFailedError: 大纲章节 / 世界条目 / 角色不存在。
        ValueError: 三个绑定参数都没给。
    """
    from ...domain.shared.errors import TaskFailedError

    settings = SettingsStore(store.root)
    bible = settings.ensure()
    chapter = settings.find_outline_chapter(bible, chapter_id)
    if chapter is None:
        raise TaskFailedError("大纲章节不存在")
    if world_ids is None and pov_character_id is None and pov_mode is None:
        raise ValueError("缺少绑定参数")

    if world_ids is not None:
        if world_ids:
            known = {item.get("id") for item in WorldStore(store.root).ensure().get("worlds") or []}
            missing = [world_id for world_id in world_ids if world_id not in known]
            if missing:
                raise TaskFailedError(f"世界条目不存在：{'、'.join(missing)}；请先在世界页生成世界")
        chapter["world_ids"] = list(world_ids)

    if pov_character_id is not None:
        pov = pov_character_id.strip() or None
        if pov is not None:
            cast_ids = {item.get("id") for item in store.load_characters()["characters"]}
            if pov not in cast_ids:
                raise TaskFailedError(f"角色不存在：{pov}；请先在人物页生成人物")
        chapter["pov_character_id"] = pov

    bible = settings.save(bible)
    # POV 以设定库为主源，同时写通章节副本，避免画本/大纲两处看到不同的值
    sync_result = sync_chapter_pov(
        store,
        chapter_id,
        chapter.get("pov_character_id"),
        pov_mode=pov_mode or None,
        update_mode=pov_mode is not None,
    )
    return {
        "chapter": chapter_id,
        "world_ids": chapter.get("world_ids") or [],
        "pov_character_id": chapter.get("pov_character_id"),
        "pov_mode": sync_result.get("pov_mode"),
        "settings_revision": bible.get("settings_revision"),
    }
