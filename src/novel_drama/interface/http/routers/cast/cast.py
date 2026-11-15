"""配音表、逐章配音、自动填表与试听。"""

from __future__ import annotations

from fastapi import HTTPException, Query

from novel_drama.infrastructure.config import load_config
from novel_drama.interface.http.deps import run_cli, store_for
from novel_drama.interface.http.routers.common import (
    _resolve_voice_binding,
    _respond,
    _voice_binding_index,
    _watch_background_job,
)


def register_cast_routes(app, project_root: str | None = None, config_path: str | None = None) -> None:
    """注册「配音表、逐章配音、自动填表与试听」相关路由。

    参数:
        app: FastAPI 实例。
        project_root: 默认工程目录（请求未带 path 时使用）。
        config_path: 本机配置文件路径（与 serve 同源）。

    返回:
        无。
    """
    _store = store_for(project_root)

    def _cli(*argv: str):
        """按 `serve --config` 的同一份配置执行 CLI。

        参数:
            argv: 不含 `--config` 的参数串。

        返回:
            `actions.CliResult`。
        """
        return run_cli(config_path, *argv)

    def _tts_config():
        """读取本机配置（与 serve 同源），供音色相关接口使用。"""
        return load_config(config_path) if config_path else load_config()

    @app.get("/api/ui/cast/check")
    def ui_cast_check(path: str = Query(...), chapter: str | None = None) -> dict:
        """全剧音色与 BGM 一致性校验（R18）。

        参数:
            path: 工程根目录。
            chapter: 只校验该章；为空校验全剧。

        返回:
            `{level, findings: [...], stats: {...}}`。
        """
        from novel_drama.composition import check_cast

        store = _store(path)
        return check_cast(store, chapter)

    @app.get("/api/ui/cast")
    def ui_cast_get(path: str = Query(...)) -> dict:
        """读取音色编排：音色池、旁白人格、切换点、每章旁白展开表。

        参数:
            path: 工程根目录。

        返回:
            `{cast, chapters, voices, stock_slots, errors, plan}`。
        """
        from novel_drama.composition import narrator_plan, validate_cast_config

        store = _store(path)
        project = store.load_project()
        cast = ((project.get("settings") or {}).get("tts") or {}).get("cast") or {}
        chapters = [
            {"id": item.get("id"), "index": item.get("index"), "title": item.get("title")}
            for item in store.list_chapters()
        ]
        voices = [
            {"id": item.get("id"), "name": item.get("name"), "status": item.get("status")}
            for item in store.load_voices().get("voices") or []
        ]
        binding_index = _voice_binding_index(store.load_voices().get("voices") or [])
        characters = [
            {
                "id": item.get("id"),
                "name": item.get("name"),
                "role": item.get("role"),
                "voice_id": _resolve_voice_binding(item.get("voice_binding"), binding_index),
            }
            for item in store.load_characters().get("characters") or []
        ]
        stock: dict[str, dict] = {}
        for character in store.load_characters().get("characters") or []:
            slot = (character.get("stock") or {}).get("slot")
            if not slot:
                continue
            stock.setdefault(
                slot,
                {
                    "slot": slot,
                    "label": (character.get("stock") or {}).get("label"),
                    "voice_slot": (character.get("stock") or {}).get("voice_slot"),
                    "count": 0,
                },
            )
            stock[slot]["count"] += 1
        voice_ids = {item["id"] for item in voices if item.get("id")}
        return {
            "cast": cast,
            "chapters": chapters,
            "voices": voices,
            "characters": characters,
            "stock_slots": list(stock.values()),
            "errors": validate_cast_config(cast, chapters, voice_ids, set(stock)),
            "plan": narrator_plan(cast, chapters),
        }

    @app.get("/api/ui/cast/chapter")
    def ui_cast_chapter_get(path: str = Query(...), chapter: str = Query(...)) -> dict:
        """读取某章的章节音色覆盖表（每行默认给出跟随全局时的生效音色）。

        参数:
            path: 工程根目录。
            chapter: 章节 ID。

        返回:
            `{chapter, rows, voices}`；rows 含 mode / inherited / override / effective。
        """
        from novel_drama.composition import chapter_override_rows

        try:
            return chapter_override_rows(_store(path), chapter)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail={"code": 4, "message": str(exc)})

    @app.post("/api/ui/cast/chapter")
    def ui_cast_chapter_save(body: dict) -> dict:
        """写入/清除章节覆盖。

        参数:
            body: `{path, chapter, character_id?, voice_id?, all?}`；
                  `all=true` 清空整章，`voice_id` 为空清除单行。

        返回:
            `{ok, chapter, rows, voices}`。
        """
        from novel_drama.composition import (
            clear_chapter_overrides,
            set_chapter_override,
        )

        chapter_id = str(body.get("chapter") or "").strip()
        if not chapter_id:
            raise HTTPException(status_code=400, detail={"code": 2, "message": "缺少章节 ID"})
        store = _store(body.get("path"))
        try:
            if body.get("all"):
                data = clear_chapter_overrides(store, chapter_id)
            else:
                data = set_chapter_override(
                    store,
                    chapter_id,
                    str(body.get("character_id") or "").strip(),
                    body.get("voice_id") or None,
                )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail={"code": 4, "message": str(exc)})
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"code": 2, "message": str(exc)})
        return {"ok": True, **data}

    @app.get("/api/ui/cast/character")
    def ui_cast_character(path: str = Query(...), character: str = Query(...)) -> dict:
        """某个角色在各章的生效音色（角色默认 + 章节覆盖）。

        参数:
            path: 工程根目录。
            character: 角色 ID。

        返回:
            `{character, chapters: [{id, index, title, overridable, mode, override, inherited, effective}], voices}`。
        """
        from novel_drama.composition import chapter_override_rows

        store = _store(path)
        rows: list[dict] = []
        voices: list[dict] = []
        for meta in store.list_chapters():
            try:
                data = chapter_override_rows(store, meta["id"])
            except FileNotFoundError:
                continue
            row = next((item for item in data.get("rows") or [] if item.get("character_id") == character), None)
            if row is None:
                continue
            voices = data.get("voices") or voices
            rows.append(
                {
                    "id": meta["id"],
                    "index": meta.get("index"),
                    "title": meta.get("title"),
                    "overridable": bool(row.get("overridable")),
                    "mode": row.get("mode"),
                    "override": row.get("override"),
                    "inherited": row.get("inherited"),
                    "effective": row.get("effective"),
                }
            )
        return {"character": character, "chapters": rows, "voices": voices}

    @app.get("/api/ui/cast/capture-plan")
    def ui_cast_capture_plan(
        path: str = Query(...),
        chapter: str = "",
        act: str = "",
        act_index: int = 0,
    ) -> dict:
        """列出仍需补跑角色识别的章节（生成弹窗提前如实提示，不写盘）。

        参数:
            path: 工程根目录。
            chapter: 只检查这一章（单章弹窗）。
            act: 只检查这一幕（整幕弹窗）。
            act_index: 幕序号兜底（幕 ID 缺失时使用）。

        返回:
            `{chapters, count, seconds, seconds_per_chapter}`。
        """
        from novel_drama.application.cast.capture_cast import cast_capture_plan

        return cast_capture_plan(
            _store(path),
            chapter_id=chapter or None,
            act_id=act or None,
            act_index=act_index or None,
        )

    @app.get("/api/ui/cast/pending")
    def ui_cast_pending(path: str = Query(...)) -> dict:
        """读取待定角色队列（含出现次数与归属线索，只读）。

        参数:
            path: 工程根目录。

        返回:
            `{items, count}`。
        """
        from novel_drama.application.cast.capture_cast import pending_overview

        return pending_overview(_store(path))

    @app.post("/api/ui/cast/pending/promote")
    def ui_cast_pending_promote(body: dict) -> dict:
        """把待定角色立即建卡（沿用角色对齐的建卡字段）。

        参数:
            body: `{path, key, role?}`。

        返回:
            `{ok, created, character, pending}`。
        """
        from novel_drama.application.cast.sync_cast import promote_pending

        key = str(body.get("key") or "").strip()
        if not key:
            raise HTTPException(status_code=400, detail={"code": 2, "message": "缺少待定角色标识"})
        try:
            result = promote_pending(_store(body.get("path")), key, role=str(body.get("role") or "supporting"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"code": 2, "message": str(exc)})
        return {"ok": True, **result}

    @app.post("/api/ui/cast/pending/discard")
    def ui_cast_pending_discard(body: dict) -> dict:
        """从待定队列丢弃候选人（不影响已建角色）。

        参数:
            body: `{path, key}`。

        返回:
            `{ok, discarded, pending}`。
        """
        from novel_drama.application.cast.sync_cast import discard_pending

        key = str(body.get("key") or "").strip()
        if not key:
            raise HTTPException(status_code=400, detail={"code": 2, "message": "缺少待定角色标识"})
        try:
            result = discard_pending(_store(body.get("path")), key)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"code": 2, "message": str(exc)})
        return {"ok": True, **result}

    @app.get("/api/ui/cast/status")
    def ui_cast_status(path: str = Query(...), act: str = "", act_index: int = 0) -> dict:
        """逐章返回角色识别状态（与补跑计划同一判定口径）。

        参数:
            path: 工程根目录。
            act: 只看这一幕（可选）。
            act_index: 幕序号兜底（可选）。

        返回:
            `{chapters, counts, job}`。
        """
        from novel_drama.application.cast.capture_cast import cast_status_overview

        return cast_status_overview(
            _store(path),
            act_id=act or None,
            act_index=act_index or None,
        )

    @app.post("/api/ui/cast/capture/start")
    def ui_cast_capture_start(body: dict) -> dict:
        """启动手动补跑：只做角色识别，不写正文。

        参数:
            body: `{path, chapter?, act?, act_index?}`。

        返回:
            `{job}`。
        """
        from novel_drama.application.cast.capture_jobs import start_capture_job

        store = _store(body.get("path"))
        try:
            job = start_capture_job(
                store,
                load_config(config_path),
                chapter_id=str(body.get("chapter") or ""),
                act_id=str(body.get("act") or ""),
                act_index=int(body.get("act_index") or 0) or None,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"code": 2, "message": str(exc)})
        return {"job": job}

    @app.get("/api/ui/cast/capture/job")
    def ui_cast_capture_job(path: str = Query(...)) -> dict:
        """读取最近一次手动补跑任务（页面轮询进度与失败原因）。

        参数:
            path: 工程根目录。

        返回:
            `{job}`；没有任务时 job 为空对象。
        """
        from novel_drama.application.cast.capture_jobs import latest_capture_job

        return {"job": latest_capture_job(_store(path))}

    @app.post("/api/ui/cast/capture/cancel")
    def ui_cast_capture_cancel(body: dict) -> dict:
        """停止手动补跑：当前章节识别结束后生效。

        参数:
            body: `{path, job_id?}`。

        返回:
            `{job}`。
        """
        from novel_drama.application.cast.capture_jobs import cancel_capture_job

        try:
            job = cancel_capture_job(_store(body.get("path")), str(body.get("job_id") or ""))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"code": 2, "message": str(exc)})
        return {"job": job}

    @app.get("/api/ui/cast/autofill")
    def ui_cast_autofill_preview(path: str = Query(...)) -> dict:
        """预览「按角色自动补齐」计划（只读，不落盘）。

        参数:
            path: 工程根目录。

        返回:
            `{plan, undo}`；plan.items 每项含 action / proposed / reason。
        """
        from novel_drama.tts.cast_autofill import build_plan, undo_state

        store = _store(path)
        return {"plan": build_plan(store), "undo": undo_state(store)}

    @app.post("/api/ui/cast/autofill")
    def ui_cast_autofill_apply(body: dict) -> dict:
        """应用自动补齐，并保留一份补齐前的快照供撤回。

        参数:
            body: `{path}`。

        返回:
            `{applied, changed, plan, undo}`。
        """
        from novel_drama.tts.cast_autofill import apply

        store = _store(body.get("path"))
        try:
            return apply(store)
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=400, detail={"code": 2, "message": str(exc)})

    @app.post("/api/ui/cast/autofill/undo")
    def ui_cast_autofill_undo(body: dict) -> dict:
        """撤回上次自动补齐，恢复补齐前的 cast。

        参数:
            body: `{path}`。

        返回:
            `{restored, undo}`。
        """
        from novel_drama.tts.cast_autofill import undo_last

        store = _store(body.get("path"))
        try:
            return undo_last(store)
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=400, detail={"code": 2, "message": str(exc)})

    @app.get("/api/ui/cast/fill")
    def ui_cast_fill_state(path: str = Query(...)) -> dict:
        """一键补齐音色的当前状态：任务快照与是否还有剩余角色。

        参数:
            path: 工程根目录。

        返回:
            `{job, has_remaining}`。
        """
        from novel_drama.tts import voice_fill

        return voice_fill.state(_store(path))

    @app.post("/api/ui/cast/fill/start")
    def ui_cast_fill_start(body: dict) -> dict:
        """启动一键补齐全部音色（扫描 → 复用绑定 → 缺失生成）。

        参数:
            body: `{path, count?, overwrite?}`；overwrite=true 时已有音色也重新生成并覆盖。

        返回:
            `{job}`。
        """
        from novel_drama.tts import voice_fill

        store = _store(body.get("path"))
        try:
            job = voice_fill.start_job(
                store,
                _tts_config(),
                body.get("count"),
                overwrite=bool(body.get("overwrite")),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"code": 2, "message": str(exc)}) from exc
        _watch_background_job(
            store,
            "voice.fill",
            "一键补齐音色",
            lambda: voice_fill.read_job(store),
        )
        return {"job": job}

    @app.post("/api/ui/cast/fill/resume")
    def ui_cast_fill_resume(body: dict) -> dict:
        """继续补齐剩余角色，或重试指定角色 / 龙套音色池。

        参数:
            body: `{path, character_ids?, pool_slots?}`；两者都为空时补齐全部未完成与失败项。

        返回:
            `{job}`。
        """
        from novel_drama.tts import voice_fill

        store = _store(body.get("path"))
        try:
            job = voice_fill.resume_job(
                store,
                _tts_config(),
                body.get("character_ids"),
                body.get("pool_slots"),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"code": 2, "message": str(exc)}) from exc
        _watch_background_job(
            store,
            "voice.fill",
            "继续补齐音色",
            lambda: voice_fill.read_job(store),
        )
        return {"job": job}

    @app.post("/api/ui/cast/fill/stop")
    def ui_cast_fill_stop(body: dict) -> dict:
        """停止补齐：已绑定/已生成的结果保留，未完成的可续跑。

        参数:
            body: `{path}`。

        返回:
            `{job}`。
        """
        from novel_drama.tts import voice_fill

        job = voice_fill.cancel_job(_store(body.get("path")))
        if not job:
            raise HTTPException(status_code=404, detail={"code": 3, "message": "补齐任务不存在"})
        return {"job": job}

    @app.post("/api/ui/cast")
    def ui_cast_save(body: dict) -> dict:
        """保存音色池与旁白切换点；校验不通过时返回错误列表且不落盘。

        参数:
            body: `{path, cast}`。

        返回:
            `{ok, errors, check?, plan?}`。
        """
        from novel_drama.composition import (
            check_cast,
            narrator_plan,
            normalize_cast,
            validate_cast_config,
        )

        store = _store(body.get("path"))
        project = store.load_project()
        chapters = [
            {"id": item.get("id"), "index": item.get("index"), "title": item.get("title")}
            for item in store.list_chapters()
        ]
        voices = store.load_voices().get("voices") or []
        voice_ids = {item.get("id") for item in voices if item.get("id")}
        stock_slots = {
            (character.get("stock") or {}).get("slot")
            for character in store.load_characters().get("characters") or []
            if (character.get("stock") or {}).get("slot")
        }
        cast = normalize_cast(body.get("cast") or {})
        errors = validate_cast_config(cast, chapters, voice_ids, stock_slots)
        if errors:
            return {"ok": False, "errors": errors}
        settings = project.setdefault("settings", {})
        tts = settings.setdefault("tts", {})
        tts["cast"] = cast
        try:
            store.save_project(project)
        except Exception as exc:  # noqa: BLE001 - schema/IO 失败统一回给页面
            return {"ok": False, "errors": [f"保存失败：{exc}"]}
        return {
            "ok": True,
            "errors": [],
            "check": check_cast(store),
            "plan": narrator_plan(cast, chapters),
        }

    @app.post("/api/ui/cast/backfill")
    def ui_cast_backfill(body: dict) -> dict:
        """给缺失音色描述的角色补生成（`cast backfill --profiles --apply`）。

        参数:
            body: `{path, all?, force?}`。

        返回:
            `{changed, applied}`。
        """
        path = body.get("path")
        _store(path)
        argv = ["--project", path, "--json", "cast", "backfill", "--profiles", "--apply"]
        if body.get("all"):
            argv.append("--all")
        if body.get("force"):
            argv.append("--force")
        return _respond(_cli(*argv))
