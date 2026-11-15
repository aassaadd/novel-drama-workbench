"""角色增删改、合并、引用影响面与出图。"""

from __future__ import annotations

import json

from fastapi import HTTPException, Query

from novel_drama.application.cast.character_crud import (
    UPDATE_FIELDS,
    append_characters,
    character_refs,
    character_sheet,
    remove_character,
    update_character,
)
from novel_drama.application.cast.generate_characters import preview_characters
from novel_drama.application.cast.queries import character_list
from novel_drama.application.production.lines_source import chapter_view
from novel_drama.infrastructure.config import load_config
from novel_drama.interface.http import use_cases
from novel_drama.interface.http.deps import store_for


def register_cast_characters_routes(app, project_root: str | None = None, config_path: str | None = None) -> None:
    """注册「角色增删改、合并、引用影响面与出图」相关路由。

    参数:
        app: FastAPI 实例。
        project_root: 默认工程目录（请求未带 path 时使用）。
        config_path: 本机配置文件路径（与 serve 同源）。

    返回:
        无。
    """
    _store = store_for(project_root)

    @app.get("/api/ui/characters")
    def ui_characters(path: str = Query(...)) -> dict:
        """人物表（`character list --json`）。

        参数:
            path: 工程根目录。

        返回:
            `{characters, enum_issues}`；`enum_issues` 是历史数据里无法识别的枚举值，
            每个条目带字段名、原值与可选值，供界面给出修复提示（不会被静默吞掉）。
        """
        from novel_drama.models.enum_check import check_character_table

        store = _store(path)
        payload = character_list(store)
        table = store.load_characters()
        try:
            issues = check_character_table(table, baseline=table)
        except ValueError:
            issues = []
        rows = table.get("characters") or []
        for item in issues:
            parts = str(item.get("path") or "").split("/")
            if len(parts) >= 2 and parts[0] == "characters" and parts[1].isdigit():
                index = int(parts[1])
                if 0 <= index < len(rows):
                    item["character_id"] = rows[index].get("id")
                    item["character_name"] = rows[index].get("name")
        return {"characters": payload or [], "enum_issues": issues}

    @app.post("/api/ui/characters")
    def ui_characters_add(body: dict) -> dict:
        """批量追加人物（应用层用例 `append_characters`，与 `character apply` 同源）。

        参数:
            body: `{path, characters: [...]}`。

        异常:
            HTTPException 400: characters 不是非空数组。
        """
        store = _store(body.get("path"))
        rows = body.get("characters")
        if not isinstance(rows, list) or not rows:
            raise HTTPException(status_code=400, detail={"code": 2, "message": "characters 必须是非空数组"})
        applied = use_cases.call(lambda: append_characters(store, rows))
        return {"characters": applied["characters"], "applied": True, "skipped": applied["skipped"]}

    @app.post("/api/ui/characters/generate")
    def ui_characters_generate(body: dict) -> dict:
        """生成主角与主要配角（`character generate --dry-run`，预览不落盘）。

        参数:
            body: `{path, count?, brief?}`；count 为期望生成的总人数，brief 为补充要求。

        返回:
            `{characters: [...], applied: false}`，由前端回填后用户确认再 POST 追加。
        """
        path = body.get("path")
        store = _store(path)
        count = int(body["count"]) if body.get("count") is not None else None
        return use_cases.call(
            lambda: preview_characters(
                store,
                load_config(config_path),
                count=count,
                brief=str(body["brief"]) if body.get("brief") else None,
            )
        )

    @app.post("/api/ui/characters/image")
    def ui_characters_image(body: dict) -> dict:
        """生成角色特写 / 三视图（`character image`）。

        参数:
            body: `{path, id, kind: closeup|turnaround, force?}`。

        返回:
            `{character_id, kind, image_asset, created}`。

        异常:
            HTTPException 400: 缺少 id 或 kind 非法。
        """
        path = body.get("path")
        store = _store(path)
        character_id = body.get("id") or body.get("character")
        if not character_id:
            raise HTTPException(status_code=400, detail={"code": 2, "message": "缺少 id 参数"})
        kind = str(body.get("kind") or "").strip()
        if kind not in ("fullbody", "closeup", "turnaround"):
            raise HTTPException(status_code=400, detail={"code": 2, "message": "kind 必须是 fullbody / closeup / turnaround"})
        return use_cases.call(
            lambda: character_sheet(
                store,
                load_config(config_path),
                str(character_id),
                kind,
                force=bool(body.get("force")),
            ),
            business=True,
        )

    @app.patch("/api/ui/characters")
    def ui_characters_update(body: dict) -> dict:
        """按 ID 更新人物字段（应用层用例 `update_character`，与 `character update` 同源）。

        参数:
            body: `{path, id, name?, role?, gender?, age_range?, personality?, background?,
            aliases?, appearance_prompt?, negative_prompt?, situation?, relationships?,
            removed_relationships?}`；removed_relationships 为要删除的关系目标 ID 列表。

        异常:
            HTTPException 400: 缺少 id。
        """
        store = _store(body.get("path"))
        character_id = body.get("id") or body.get("character")
        if not character_id:
            raise HTTPException(status_code=400, detail={"code": 2, "message": "缺少 id 参数"})
        changes: dict = {}
        for key in UPDATE_FIELDS:
            value = body.get(key)
            if value is None:
                continue
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False)
            changes[key] = str(value)
        relationships = body.get("relationships") or {}
        if isinstance(relationships, dict):
            relationships = [f"{key}={value}" for key, value in relationships.items()]
        # 关系键统一按 CLI 的 `目标ID=备注` 形式传入；被删掉的关系显式通知用例移除
        changes["relationship"] = [str(item) for item in relationships]
        changes["relationship_remove"] = [
            str(target_id).strip() for target_id in (body.get("removed_relationships") or []) if str(target_id).strip()
        ]
        payload = use_cases.call(lambda: update_character(store, str(character_id), changes))
        return payload["character"]

    @app.get("/api/ui/characters/refs")
    def ui_character_refs(path: str = Query(...), character: str = Query(...)) -> dict:
        """角色引用位置：谁会受影响、各多少处、来自哪里（删除前提示用）。

        参数:
            path: 工程目录。
            character: 角色 ID。

        返回:
            `character_refs` 的完整清单：`{total, sources, chapters, relations, outline, chapter_refs}`。

        异常:
            HTTPException 412: 角色不存在（可能已在别处被删除）。
        """
        return use_cases.call(lambda: character_refs(_store(path), character))

    @app.post("/api/ui/characters/delete")
    def ui_characters_delete(body: dict) -> dict:
        """按 ID 删除人物（应用层用例 `remove_character`，旁白会被拒绝）。

        默认拦截仍被引用的角色并返回影响面摘要；`force=true` 表示用户已在确认框里
        明确选择「删除并清理引用」，此时先清理大纲/章节/关系里的引用再删除。

        参数:
            body: `{path, id, force?}`。

        异常:
            HTTPException 400: 缺少 id。
            HTTPException 412: 角色不存在，或仍被引用且未确认。
        """
        store = _store(body.get("path"))
        character_id = body.get("id") or body.get("character")
        if not character_id:
            raise HTTPException(status_code=400, detail={"code": 2, "message": "缺少 id 参数"})
        return use_cases.call(
            lambda: remove_character(store, str(character_id), force=bool(body.get("force"))),
        )

    @app.get("/api/ui/characters/merge/impact")
    def ui_character_merge_impact(path: str = Query(...), source: str = Query(...), target: str = Query(...)) -> dict:
        """预览「合并到已有角色」会改动多少处引用（只读，供二次确认）。

        参数:
            path: 工程目录。
            source: 要合并掉的角色 ID。
            target: 合并到的角色 ID。

        返回:
            `{source, target, outline: {beats, pov, arcs}, chapters: {scenes, lines, overrides},
            relations, total}`。

        异常:
            HTTPException 400: 参数不合法或角色不存在。
        """
        from novel_drama.character_merge import merge_impact

        try:
            return merge_impact(_store(path), source, target)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"code": 2, "message": str(exc)})

    @app.post("/api/ui/characters/merge")
    def ui_characters_merge(body: dict) -> dict:
        """把新建角色合并到已有角色，并迁移大纲绑定、节拍选角与台词行。

        参数:
            body: `{path, source, target}`。

        返回:
            `{merged, source, target, impact, settings_revision}`。

        异常:
            HTTPException 400: 参数不合法、角色不存在或试图合并旁白。
        """
        from novel_drama.character_merge import merge_characters

        source = str(body.get("source") or "").strip()
        target = str(body.get("target") or "").strip()
        try:
            return merge_characters(_store(body.get("path")), source, target)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"code": 2, "message": str(exc)})

    @app.get("/api/ui/characters/new-this-chapter")
    def ui_characters_created(path: str = Query(...), chapter: int | None = None) -> dict:
        """查看「本章新建」角色（大纲生成时按章登记）。

        参数:
            path: 工程目录。
            chapter: 只取该章序号；为空时按章倒序返回全部章节。

        返回:
            `{chapters: [{index, title, items: [{id, name, created_at}]}]}`。
        """
        from novel_drama.application.cast.sync_cast import (
            created_in_chapter,
            list_created_chapters,
        )

        store = _store(path)
        if chapter is not None:
            rows = [{"index": int(chapter), "title": "", "items": created_in_chapter(store, int(chapter))}]
            return {"chapters": [row for row in rows if row["items"]]}
        return {"chapters": list_created_chapters(store)}

    @app.get("/api/ui/characters/stats")
    def ui_character_stats(path: str = Query(...)) -> dict:
        """按角色汇总全书出场情况（实际：场景 + 台词；计划：大纲节拍）。

        参数:
            path: 工程目录。

        返回:
            `{characters: {角色ID: {lines, scenes, chapters: [...], planned_chapters: [...]}}}`；
            chapters 每项含 `{id, title, index, lines, scenes}`，planned_chapters 每项含
            `{id, title, index, beats}`（该角色在本章被排进几个节拍）。
        """
        store = _store(path)
        from novel_drama.models import entities

        stats: dict[str, dict] = {}

        def row_of(character_id: str) -> dict:
            """取或创建角色统计行。"""
            return stats.setdefault(
                character_id,
                {"lines": 0, "scenes": 0, "chapters": [], "planned_chapters": []},
            )

        for chapter in store.list_chapters():
            chapter_id = chapter.get("id")
            if not chapter_id:
                continue
            data = chapter_view(store, store.load_chapter(chapter_id))
            chapter_lines: dict[str, int] = {}
            chapter_scenes: dict[str, int] = {}
            for scene in data.get("scenes") or []:
                for cid in scene.get("character_ids") or []:
                    key = str(cid or "").strip()
                    if key:
                        chapter_scenes[key] = chapter_scenes.get(key, 0) + 1
                for line in scene.get("lines") or []:
                    speaker = str(line.get("speaker_id") or "").strip()
                    if speaker:
                        chapter_lines[speaker] = chapter_lines.get(speaker, 0) + 1
            for character_id in set(chapter_lines) | set(chapter_scenes):
                if character_id in (entities.NARRATOR_ID, entities.UNKNOWN_SPEAKER_ID):
                    continue
                row = row_of(character_id)
                lines = chapter_lines.get(character_id, 0)
                scenes = chapter_scenes.get(character_id, 0)
                row["lines"] += lines
                row["scenes"] += scenes
                row["chapters"].append(
                    {
                        "id": chapter_id,
                        "title": chapter.get("title") or "",
                        "index": chapter.get("index") or 0,
                        "lines": lines,
                        "scenes": scenes,
                    }
                )
        # 计划出场：只读设定库节拍，不落盘
        from novel_drama.composition import SettingsStore

        bible = SettingsStore(store.root).ensure()
        title_by_id = {item.get("id"): item.get("title") for item in store.list_chapters()}
        for act in (bible.get("outline") or {}).get("acts") or []:
            for chapter in act.get("chapters") or []:
                chapter_id = chapter.get("id")
                if not chapter_id:
                    continue
                counts: dict[str, int] = {}
                for beat in chapter.get("beats") or []:
                    for cid in beat.get("character_ids") or []:
                        key = str(cid or "").strip()
                        if key:
                            counts[key] = counts.get(key, 0) + 1
                for character_id, beats in counts.items():
                    row_of(character_id)["planned_chapters"].append(
                        {
                            "id": chapter_id,
                            "title": title_by_id.get(chapter_id) or chapter.get("title") or "",
                            "index": chapter.get("index") or 0,
                            "beats": beats,
                        }
                    )
        for row in stats.values():
            row["chapters"].sort(key=lambda item: int(item.get("index") or 0))
            row["planned_chapters"].sort(key=lambda item: int(item.get("index") or 0))
        return {"characters": stats}

    @app.get("/api/ui/characters/graph")
    def ui_character_graph(path: str = Query(...)) -> dict:
        """角色关系图数据：结构化 relations + 旧关系备注，统一解析成姓名。

        参数:
            path: 工程目录。

        返回:
            `{nodes: [{id,name,role,pov?}], edges: [{source,target,label,sentiment,note,kind}]}`。
        """
        store = _store(path)
        characters = [
            item for item in store.load_characters()["characters"] if item.get("role") != "narrator"
        ]
        by_id = {str(item.get("id")): item for item in characters if item.get("id")}
        name_by_id = {cid: str(item.get("name") or "") for cid, item in by_id.items()}
        nodes = [
            {"id": cid, "name": item.get("name") or "", "role": item.get("role") or "minor"}
            for cid, item in by_id.items()
        ]
        edges: list[dict] = []
        seen: set[tuple] = set()
        for cid, item in by_id.items():
            state = item.get("state") or {}
            for relation in state.get("relations") or []:
                target = str(relation.get("target_id") or "").strip()
                if not target or target not in by_id:
                    continue
                key = (cid, target, str(relation.get("label") or ""))
                if key in seen:
                    continue
                seen.add(key)
                edges.append(
                    {
                        "source": cid,
                        "source_name": name_by_id.get(cid, ""),
                        "target": target,
                        "target_name": name_by_id.get(target, ""),
                        "label": str(relation.get("label") or ""),
                        "sentiment": relation.get("sentiment"),
                        "note": str(relation.get("note") or ""),
                        "kind": "relation",
                    }
                )
            for key_name, note in (state.get("relationship_notes") or {}).items():
                target = str(key_name)
                if target not in by_id:
                    continue
                key = (cid, target, "note")
                if key in seen:
                    continue
                seen.add(key)
                edges.append(
                    {
                        "source": cid,
                        "source_name": name_by_id.get(cid, ""),
                        "target": target,
                        "target_name": name_by_id.get(target, ""),
                        "label": "关系备注",
                        "sentiment": None,
                        "note": str(note or ""),
                        "kind": "note",
                    }
                )
        return {"nodes": nodes, "edges": edges}

    @app.get("/api/ui/characters/arcs")
    def ui_character_arcs(path: str = Query(...), character: str = Query("")) -> dict:
        """角色弧线：按章节列出该角色在大纲里的 from → to 变化。

        参数:
            path: 工程目录。
            character: 角色 ID；为空时返回全部角色的弧线。

        返回:
            `{arcs: [{chapter_id, index, title, character_id, name, from_state, to_state, note}]}`。
        """
        store = _store(path)
        from novel_drama.composition import SettingsStore

        bible = SettingsStore(store.root).ensure()
        name_by_id = {
            str(item.get("id")): item.get("name")
            for item in store.load_characters()["characters"]
            if item.get("id")
        }
        rows: list[dict] = []
        for act in (bible.get("outline") or {}).get("acts") or []:
            for chapter in act.get("chapters") or []:
                for arc in chapter.get("character_arcs") or []:
                    cid = str(arc.get("character_id") or "").strip()
                    if not cid or (character and cid != character):
                        continue
                    rows.append(
                        {
                            "chapter_id": chapter.get("id"),
                            "index": chapter.get("index"),
                            "title": chapter.get("title"),
                            "character_id": cid,
                            "name": name_by_id.get(cid) or "已删除角色",
                            "from_state": arc.get("from_state") or "",
                            "to_state": arc.get("to_state") or "",
                            "note": arc.get("note") or "",
                        }
                    )
        rows.sort(key=lambda item: int(item.get("index") or 0))
        return {"arcs": rows}
