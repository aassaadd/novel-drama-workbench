"""章节目录、搜索、筛选与角色捕获。"""

from __future__ import annotations

from fastapi import HTTPException, Query

from novel_drama.application.production.line_query import chapter_lines
from novel_drama.application.production.lines_source import chapter_view
from novel_drama.application.story.queries import (
    chapter_list,
)
from novel_drama.composition import QueueStore
from novel_drama.infrastructure.config import load_config
from novel_drama.interface.http import use_cases
from novel_drama.interface.http.deps import store_for


def register_story_chapters_routes(app, project_root: str | None = None, config_path: str | None = None) -> None:
    """注册「章节目录、搜索、筛选与角色捕获」相关路由。

    参数:
        app: FastAPI 实例。
        project_root: 默认工程目录（请求未带 path 时使用）。
        config_path: 本机配置文件路径（与 serve 同源）。

    返回:
        无。
    """
    _store = store_for(project_root)

    def _outline_chapters(store) -> list[dict]:
        """读取设定库里的全部大纲章节（捕获角色时查找节拍用）。

        参数:
            store: 工程。

        返回:
            大纲章节列表；没有设定库时返回空列表。
        """
        from novel_drama.composition import SettingsStore

        settings = SettingsStore(store.root)
        if not settings.exists():
            return []
        bible = settings.ensure()
        return [
            chapter
            for act in (bible.get("outline") or {}).get("acts") or []
            for chapter in act.get("chapters") or []
        ]

    @app.get("/api/ui/chapters")
    def ui_chapters(path: str = Query(...)):
        """章节目录（应用层读模型，与 `chapter list --json` 同源）。"""
        return chapter_list(_store(path))

    @app.get("/api/ui/chapters/search")
    def ui_chapters_search(path: str = Query(...), q: str = Query("")) -> dict:
        """章节搜索：标题 + 正文（含命中次数与首处片段）。

        参数:
            path: 工程目录。
            q: 关键词（去首尾空格后至少 1 个字符）。

        返回:
            `{q, hits: [{id, title, count, snippet}]}`，按章节 index 升序。
        """
        keyword = (q or "").strip()
        if not keyword:
            return {"q": "", "hits": []}
        store = _store(path)
        lowered = keyword.lower()
        hits = []
        for meta in store.list_chapters():
            chapter = store.load_chapter(meta["id"])
            content = str(chapter.get("content") or "")
            count = content.lower().count(lowered)
            title = str(chapter.get("title") or "")
            in_title = lowered in title.lower()
            if not count and not in_title:
                continue
            position = content.lower().find(lowered)
            snippet = ""
            if position >= 0:
                start = max(0, position - 18)
                snippet = content[start : position + len(keyword) + 22].replace("\n", " ")
            hits.append(
                {
                    "id": meta["id"],
                    "title": title,
                    "count": count,
                    "snippet": snippet,
                }
            )
        return {"q": keyword, "hits": hits}

    @app.get("/api/ui/chapters/stale")
    def ui_chapters_stale(path: str = Query(...), chapter: str = Query(...)) -> dict:
        """章节级下游过期汇总：列出过期/失败的台词行与其所在场景。

        参数:
            path: 工程目录。
            chapter: 章节 ID。

        返回:
            `{chapter, title, counts, items: [{line_id, scene_id, scene_title, index, speaker, text, state}]}`。
        """
        store = _store(path)
        payload = use_cases.call(
            lambda: chapter_lines(store, chapter, load_config(config_path))
        )
        data = chapter_view(store, store.load_chapter(chapter))
        scene_titles = {
            str(scene.get("id")): str(scene.get("title") or f"场景 {int(scene.get('index') or 0) + 1}")
            for scene in data.get("scenes") or []
        }
        items = [
            {
                "line_id": row.get("line_id"),
                "scene_id": row.get("scene_id"),
                "scene_title": scene_titles.get(str(row.get("scene_id")), ""),
                "index": row.get("index"),
                "speaker": row.get("speaker"),
                "text": row.get("text"),
                "state": row.get("state"),
            }
            for row in payload.get("lines") or []
            if row.get("state") in ("stale", "failed")
        ]
        return {
            "chapter": chapter,
            "title": data.get("title"),
            "counts": payload.get("counts") or {},
            "items": items,
        }

    @app.get("/api/ui/chapters/facets")
    def ui_chapters_facets(path: str = Query(...)) -> dict:
        """章节目录三组筛选维度：全部场景 / 全部角色 / 全部状态。

        全部按真实数据派生，不新增落盘字段：
        - 角色：本章台词里出现过的说话人（旁白置顶）；
        - 创作状态：无台词=画本中，有未就绪行=配音中，全部就绪未混音=后期中，
          已混音未导出=审听中，已导出=创作完成；
        - 成品音频：`renders/<chapter>.mix(.norm).wav` 是否存在；
        - 工单：队列 `.ndw/queue.json` 是否有指向本章的任务。

        参数:
            path: 工程目录。

        返回:
            `{characters, scenes, chapters}`；chapters 为章节 ID → 维度值。
        """
        from novel_drama.tts import line_state

        store = _store(path)
        characters = store.load_characters().get("characters") or []
        by_id = {item.get("id"): item for item in characters}
        cfg = load_config(config_path) if config_path else load_config()
        engine = line_state.engine_params(cfg)
        live_ids = line_state.live_line_ids(store)
        job_chapters = {
            str((job.get("target") or {}).get("chapter_id"))
            for job in QueueStore(store).load()
            if (job.get("target") or {}).get("chapter_id")
        }

        char_seen: dict[str, dict] = {}
        scene_seen: list[str] = []
        per_chapter: dict[str, dict] = {}
        for meta in store.list_chapters():
            chapter = chapter_view(store, store.load_chapter(meta["id"]))
            chapter_id = meta["id"]
            lines = [line for scene in chapter.get("scenes") or [] for line in scene.get("lines") or []]
            snapshots = line_state.load_snapshots(store.root, chapter)
            states = line_state.chapter_states(
                chapter, by_id, snapshots, root=store.root, live_ids=live_ids, engine=engine
            )
            counts = line_state.state_counts(states)
            total = len(lines)
            ready = int(counts.get("ready") or 0)
            pending = total - ready

            renders = store.root / "renders"
            has_audio = (renders / f"{chapter_id}.mix.norm.wav").exists() or (renders / f"{chapter_id}.mix.wav").exists()
            has_export = (store.root / "exports" / f"{chapter_id}.manifest.json").exists() or (
                renders / f"{chapter_id}.mp4"
            ).exists()
            if not total:
                creative = "script"
            elif pending:
                creative = "voice"
            elif not has_audio:
                creative = "post"
            elif not has_export:
                creative = "review"
            else:
                creative = "done"

            chapter_chars: list[str] = []
            chapter_scenes: list[str] = []
            for scene in chapter.get("scenes") or []:
                title = str(scene.get("title") or "").strip()
                if title:
                    chapter_scenes.append(title)
                    if title not in scene_seen:
                        scene_seen.append(title)
                for line in scene.get("lines") or []:
                    speaker = line.get("speaker_id")
                    if speaker and speaker not in chapter_chars:
                        chapter_chars.append(speaker)
            for speaker in chapter_chars:
                if speaker not in char_seen:
                    item = by_id.get(speaker) or {}
                    name = item.get("name") or ("旁白" if speaker == "chr_narrator" else "未知角色")
                    char_seen[speaker] = {"id": speaker, "name": name, "role": item.get("role")}

            per_chapter[chapter_id] = {
                "character_ids": chapter_chars,
                "scene_titles": chapter_scenes,
                "creative": creative,
                "has_audio": has_audio,
                "has_job": chapter_id in job_chapters,
                "stale": int(counts.get("stale") or 0),
                "failed": int(counts.get("failed") or 0),
            }

        ordered_chars = sorted(
            char_seen.values(),
            key=lambda item: (item["id"] != "chr_narrator", item.get("role") or "zz", item["id"]),
        )
        return {
            "characters": ordered_chars,
            "scenes": scene_seen,
            "chapters": per_chapter,
        }

    @app.get("/api/ui/chapters/character-updates")
    def ui_character_updates_get(path: str = Query(...), chapter: str = Query(...)) -> dict:
        """读取本章角色状态回流提案（只读，不自动改角色卡）。

        参数:
            path: 工程目录。
            chapter: 章节 ID。

        返回:
            `{chapter_id, exists, applied, updates: [{character_id, situation, knowledge_add, relations}]}`。
        """
        from novel_drama.application.cast.character_updates import (
            load_character_updates,
        )

        return load_character_updates(_store(path), chapter)

    @app.post("/api/ui/chapters/character-updates")
    def ui_character_updates_create(body: dict) -> dict:
        """根据本章正文生成角色状态更新提案（不写回）。

        参数:
            body: `{path, chapter}`。

        返回:
            提案记录。

        异常:
            HTTPException 400: 正文为空或生成失败。
        """
        from novel_drama.application.cast.character_updates import (
            propose_character_updates,
        )

        store = _store(body.get("path"))
        chapter = str(body.get("chapter") or "")
        if not chapter:
            raise HTTPException(status_code=400, detail={"code": 2, "message": "缺少 chapter 参数"})
        try:
            return propose_character_updates(store, load_config(config_path), chapter)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"code": 2, "message": str(exc)}) from exc

    @app.post("/api/ui/chapters/character-updates/apply")
    def ui_character_updates_apply(body: dict) -> dict:
        """按作者确认的选择写回角色状态（只改被确认的字段）。

        参数:
            body: `{path, chapter, selections?: [{character_id, situation?, knowledge_add?, relations?}]}`；
            selections 缺省表示全部确认。

        返回:
            `{chapter_id, applied, skipped}`。

        异常:
            HTTPException 400: 没有可应用的提案。
        """
        from novel_drama.application.cast.character_updates import (
            apply_character_updates,
        )

        store = _store(body.get("path"))
        chapter = str(body.get("chapter") or "")
        if not chapter:
            raise HTTPException(status_code=400, detail={"code": 2, "message": "缺少 chapter 参数"})
        try:
            return apply_character_updates(store, chapter, body.get("selections"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"code": 2, "message": str(exc)}) from exc

    @app.get("/api/ui/chapters/consistency")
    def ui_consistency_get(path: str = Query(...), chapter: str = Query(...)) -> dict:
        """读取本章最近一次角色一致性检查结果。

        参数:
            path: 工程目录。
            chapter: 章节 ID。

        返回:
            `{chapter_id, exists, issues, counts}`。
        """
        from novel_drama.application.story.consistency import load_consistency

        return load_consistency(_store(path), chapter)

    @app.post("/api/ui/chapters/consistency")
    def ui_consistency_check(body: dict) -> dict:
        """执行本章角色一致性检查（只产出问题清单）。

        参数:
            body: `{path, chapter}`。

        返回:
            `{chapter_id, issues, counts}`。

        异常:
            HTTPException 400: 正文为空或生成失败。
        """
        from novel_drama.application.story.consistency import check_chapter_consistency

        store = _store(body.get("path"))
        chapter = str(body.get("chapter") or "")
        if not chapter:
            raise HTTPException(status_code=400, detail={"code": 2, "message": "缺少 chapter 参数"})
        try:
            return check_chapter_consistency(store, load_config(config_path), chapter)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"code": 2, "message": str(exc)}) from exc

    @app.post("/api/ui/chapters/capture-character")
    def ui_capture_character(body: dict) -> dict:
        """捕获未建卡/计划外角色：建最小角色卡并补进指定节拍。

        参数:
            body: `{path, chapter, beat?, character_id?, name?, sample?, role?}`。

        返回:
            `{character, binding}`。

        异常:
            HTTPException 400: 缺少参数或节拍不存在。
        """
        from novel_drama.application.story.outline_bind import (
            bind_beat_characters,
            chapter_binding,
        )
        from novel_drama.models import entities as model_entities

        store = _store(body.get("path"))
        chapter_id = str(body.get("chapter") or "")
        if not chapter_id:
            raise HTTPException(status_code=400, detail={"code": 2, "message": "缺少 chapter 参数"})
        chapter = store.load_chapter(chapter_id)
        table = store.load_characters()
        cast = [item for item in table["characters"] if item.get("role") != "narrator"]
        character_id = str(body.get("character_id") or "").strip()
        character = next((item for item in cast if item.get("id") == character_id), None) if character_id else None
        name = str(body.get("name") or "").strip()
        if character is None:
            if not name:
                raise HTTPException(status_code=400, detail={"code": 2, "message": "缺少 name：无法建卡"})
            character = next((item for item in cast if item.get("name") == name), None)
            if character is None:
                role = str(body.get("role") or "minor")
                if role not in ("protagonist", "antagonist", "supporting", "minor"):
                    role = "minor"
                character = model_entities.new_character(name, role=role)
                sample = str(body.get("sample") or "").strip()[:200]
                character["state"] = {
                    "situation": f"首次出现在第 {chapter.get('index') or '?'} 章" + (f"：{sample}" if sample else ""),
                    "knowledge": None,
                    "relations": None,
                    "as_of_chapter_id": chapter_id,
                }
                table["characters"].append(character)
                store.save_characters(table)
        beat_id = str(body.get("beat") or "").strip()
        if beat_id:
            current = next(
                (
                    [str(cid) for cid in (beat.get("character_ids") or [])]
                    for beats in [
                        (chapter_entry or {}).get("beats") or []
                        for chapter_entry in _outline_chapters(store)
                    ]
                    for beat in beats
                    if beat.get("id") == beat_id
                ),
                [],
            )
            if character["id"] not in current:
                current.append(character["id"])
            try:
                binding = bind_beat_characters(store, chapter_id, beat_id, current)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail={"code": 2, "message": str(exc)}) from exc
        else:
            binding = chapter_binding(store, chapter_id)
        return {"character": character, "binding": binding}

    @app.get("/api/ui/chapters/binding")
    def ui_chapter_binding(path: str = Query(...), chapter: str = Query(...)) -> dict:
        """本章角色绑定与对账（视角人物、节拍出场、计划 vs 实际）。

        参数:
            path: 工程目录。
            chapter: 章节 ID。

        返回:
            `chapter_binding` 的完整结果；页面据此显示绑定条与生成阻断提示。
        """
        from novel_drama.application.story.outline_bind import chapter_binding

        return chapter_binding(_store(path), chapter)
