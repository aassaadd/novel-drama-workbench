"""大纲查看、生成、应用与视角/节拍编辑。"""

from __future__ import annotations

from fastapi import HTTPException, Query

from novel_drama.application.story.apply_outline import apply_outline
from novel_drama.application.story.queries import (
    golden_check,
    outline_view,
)
from novel_drama.interface.http.deps import run_cli, store_for
from novel_drama.interface.http.routers.common import (
    _respond,
    _respond_business,
)
from novel_drama.web import use_cases


def register_story_outline_routes(app, project_root: str | None = None, config_path: str | None = None) -> None:
    """注册「大纲查看、生成、应用与视角/节拍编辑」相关路由。

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

    @app.get("/api/ui/outline")
    def ui_outline(path: str = Query(...)):
        """大纲（应用层读模型，与 `outline show --json` 同源）。"""
        store = _store(path)
        return use_cases.call(lambda: outline_view(store))

    @app.post("/api/ui/outline/generate")
    def ui_outline_generate(body: dict) -> dict:
        """生成大纲（`outline generate`）。

        参数:
            body: {path 工程目录, acts, chapters_per_act, beats_per_chapter, words, dry_run}。

        返回:
            CLI 的 JSON 负载（含 validation / summary）。
        """
        argv = ["--project", body["path"], "--json", "outline", "generate"]
        for key, flag in (("acts", "--acts"), ("chapters_per_act", "--chapters-per-act"), ("beats_per_chapter", "--beats-per-chapter"), ("words", "--words")):
            if body.get(key) is not None:
                argv += [flag, str(body[key])]
        if body.get("dry_run"):
            argv.append("--dry-run")
        return _respond(_cli(*argv))

    @app.post("/api/ui/outline/apply")
    def ui_outline_apply(body: dict) -> dict:
        """大纲落地成章节（应用层用例 `apply_outline`，不调模型）。"""
        store = _store(body["path"])
        dry_run = bool(body.get("dry_run"))
        return use_cases.call(lambda: apply_outline(store, dry_run=dry_run))

    @app.get("/api/ui/outline/check")
    def ui_outline_check(path: str = Query(...)) -> dict:
        """黄金三章检查（应用层读模型，与 `outline check --json` 同源），不阻断流程。"""
        store = _store(path)
        return use_cases.call(lambda: golden_check(store))

    @app.post("/api/ui/outline/bind")
    def ui_outline_bind(body: dict) -> dict:
        """更新章节的世界 / POV 绑定（`outline bind`）。

        参数:
            body: `{path, chapter, world_ids?: [...], pov_character_id?}`。

        异常:
            HTTPException 400: 缺少 chapter。
            HTTPException 422: 世界或角色 ID 不存在。
        """
        path = body.get("path")
        _store(path)
        chapter = body.get("chapter")
        if not chapter:
            raise HTTPException(status_code=400, detail={"code": 2, "message": "缺少 chapter 参数"})
        argv = ["--project", path, "--json", "outline", "bind", "--chapter", str(chapter)]
        if body.get("world_ids") is not None:
            world_ids = body["world_ids"]
            if isinstance(world_ids, list):
                world_ids = ",".join(str(item) for item in world_ids)
            argv += ["--worlds", str(world_ids)]
        if body.get("pov_character_id") is not None:
            argv += ["--pov", str(body["pov_character_id"])]
        if body.get("pov_mode") is not None:
            argv += ["--pov-mode", str(body["pov_mode"])]
        return _respond_business(_cli(*argv))

    @app.post("/api/ui/outline/pov")
    def ui_outline_pov(body: dict) -> dict:
        """设置本章视角人物或显式切换全知视角（写通设定库与章节副本）。

        参数:
            body: `{path, chapter, pov_character_id?: string|null, pov_mode?: string|null}`；
                pov_mode=third_omniscient 时可不绑定具体角色。

        返回:
            最新 `chapter_binding`。

        异常:
            HTTPException 400: 角色不存在或视角写法非法。
        """
        from novel_drama.application.story.outline_bind import (
            chapter_binding,
            sync_chapter_pov,
        )

        store = _store(body.get("path"))
        chapter_id = str(body.get("chapter") or "")
        if not chapter_id:
            raise HTTPException(status_code=400, detail={"code": 2, "message": "缺少 chapter 参数"})
        pov_id = body.get("pov_character_id")
        pov_mode = body.get("pov_mode")
        if pov_mode not in (None, "", "first", "second", "third_limited", "third_omniscient"):
            raise HTTPException(status_code=400, detail={"code": 2, "message": f"视角写法非法：{pov_mode}"})
        pov_id = str(pov_id).strip() if pov_id else ""
        try:
            sync_chapter_pov(
                store,
                chapter_id,
                pov_id or None,
                pov_mode=pov_mode or None,
                update_mode=True,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"code": 2, "message": str(exc)}) from exc
        return chapter_binding(store, chapter_id)

    @app.post("/api/ui/outline/beat/characters")
    def ui_outline_beat_characters(body: dict) -> dict:
        """设置某个节拍的出场角色（只改大纲计划选角）。

        参数:
            body: `{path, chapter, beat, character_ids: ["chr_..."]}`。

        返回:
            最新 `chapter_binding`。

        异常:
            HTTPException 400: 缺少参数或角色 ID 非法。
        """
        from novel_drama.application.story.outline_bind import bind_beat_characters

        path = body.get("path")
        store = _store(path)
        chapter = body.get("chapter")
        beat = body.get("beat")
        if not chapter or not beat:
            raise HTTPException(status_code=400, detail={"code": 2, "message": "缺少 chapter 或 beat 参数"})
        character_ids = body.get("character_ids")
        if character_ids is None:
            character_ids = []
        if not isinstance(character_ids, list):
            raise HTTPException(status_code=400, detail={"code": 2, "message": "character_ids 必须是数组"})
        try:
            return bind_beat_characters(store, str(chapter), str(beat), [str(item) for item in character_ids])
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"code": 2, "message": str(exc)}) from exc

    @app.post("/api/ui/outline/arcs")
    def ui_outline_arcs(body: dict) -> dict:
        """设置本章角色弧线（角色在本章从什么状态走到什么状态）。

        参数:
            body: `{path, chapter, arcs: [{character_id, from_state?, to_state?, note?}]}`。

        返回:
            `{chapter, arcs}`。

        异常:
            HTTPException 400: 章节/角色不存在或字段非法。
        """
        path = body.get("path")
        store = _store(path)
        chapter_id = str(body.get("chapter") or "")
        arcs = body.get("arcs")
        if not chapter_id or not isinstance(arcs, list):
            raise HTTPException(status_code=400, detail={"code": 2, "message": "缺少 chapter 或 arcs 参数"})
        from novel_drama.composition import SettingsStore

        settings = SettingsStore(store.root)
        bible = settings.ensure()
        entry = settings.find_outline_chapter(bible, chapter_id)
        if entry is None:
            raise HTTPException(status_code=404, detail={"code": 4, "message": f"本章还没有大纲条目：{chapter_id}"})
        cast_ids = {
            str(item.get("id"))
            for item in store.load_characters()["characters"]
            if item.get("id") and item.get("role") != "narrator"
        }
        clean: list[dict] = []
        for arc in arcs:
            if not isinstance(arc, dict):
                continue
            character_id = str(arc.get("character_id") or "").strip()
            if not character_id:
                continue
            if character_id not in cast_ids:
                raise HTTPException(status_code=400, detail={"code": 2, "message": f"角色不存在：{character_id}"})
            clean.append(
                {
                    "character_id": character_id,
                    "from_state": str(arc.get("from_state") or "").strip()[:500],
                    "to_state": str(arc.get("to_state") or "").strip()[:500],
                    "note": str(arc.get("note") or "").strip()[:500],
                }
            )
        entry["character_arcs"] = clean
        settings.save(bible)
        return {"chapter": chapter_id, "arcs": clean}
