"""世界条目查看、生成、出图与删除。"""

from __future__ import annotations

from fastapi import HTTPException, Query

from novel_drama.application.cast.queries import character_list
from novel_drama.application.story.queries import (
    world_list,
)
from novel_drama.application.story.settings_generate import generate_worlds
from novel_drama.application.story.world_crud import (
    remove_world,
    render_cover,
    upsert_worlds,
)
from novel_drama.infrastructure.config import load_config
from novel_drama.interface.http import use_cases
from novel_drama.interface.http.deps import store_for


def register_story_worlds_routes(app, project_root: str | None = None, config_path: str | None = None) -> None:
    """注册「世界条目查看、生成、出图与删除」相关路由。

    参数:
        app: FastAPI 实例。
        project_root: 默认工程目录（请求未带 path 时使用）。
        config_path: 本机配置文件路径（与 serve 同源）。

    返回:
        无。
    """
    _store = store_for(project_root)

    @app.get("/api/ui/worlds")
    def ui_worlds(path: str = Query(...)) -> dict:
        """世界模块（`world list`）与人物数量。

        参数:
            path: 工程目录。

        返回:
            `{worlds, count, characters}`；worlds 每项含世界观总述与地点/势力/术语/其它条目。
        """
        store = _store(path)
        payload = world_list(store)
        characters = character_list(store)
        return {
            "worlds": payload.get("worlds") or [],
            "count": payload.get("count") or 0,
            "characters": len(characters) if isinstance(characters, list) else 0,
        }

    @app.post("/api/ui/worlds")
    def ui_worlds_apply(body: dict) -> dict:
        """写入世界条目（应用层用例 `upsert_worlds`，带 id 且存在则更新）。

        参数:
            body: `{path, world?: {...}, worlds?: [...]}`。

        异常:
            HTTPException 400: 既没有 world 也没有 worlds。
        """
        store = _store(body.get("path"))
        rows: list[dict] = []
        if isinstance(body.get("world"), dict):
            rows.append(body["world"])
        if isinstance(body.get("worlds"), list):
            rows.extend(body["worlds"])
        if not rows:
            raise HTTPException(status_code=400, detail={"code": 2, "message": "world 或 worlds 至少给一个"})
        return use_cases.call(lambda: upsert_worlds(store, rows), business=True)

    @app.post("/api/ui/worlds/delete")
    def ui_worlds_delete(body: dict) -> dict:
        """删除整套世界（应用层用例 `remove_world`）：总述 + 分类条目，并解除章节/场景绑定。

        参数:
            body: `{path, id}`；id 为世界 ID。

        异常:
            HTTPException 400: 缺少 id。
            HTTPException 404: 世界不存在。
        """
        store = _store(body.get("path"))
        world_id = body.get("id")
        if not world_id:
            raise HTTPException(status_code=400, detail={"code": 2, "message": "缺少世界 id 参数"})
        return use_cases.call(lambda: remove_world(store, str(world_id)), business=True)

    @app.post("/api/ui/worlds/generate")
    def ui_worlds_generate(body: dict) -> dict:
        """预览 AI 生成的世界条目（`world generate --dry-run`，不落盘）。

        参数:
            body: `{path, brief?, count?}`。

        返回:
            `{worlds, count, saved: false}`。
        """
        store = _store(body.get("path"))
        count = body.get("count")
        if count is not None and str(count).strip() != "":
            try:
                count = int(count)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail={"code": 2, "message": "count 必须是整数"})
        else:
            count = None
        result = use_cases.call(
            lambda: generate_worlds(
                store,
                load_config(config_path),
                count=count,
                brief=str(body["brief"]) if body.get("brief") else None,
            ),
            business=True,
        )
        generated = result["worlds"]
        return {"worlds": generated, "count": len(generated), "saved": False}

    @app.post("/api/ui/worlds/image")
    def ui_worlds_image(body: dict) -> dict:
        """生成世界封面（`world image`）。

        参数:
            body: `{path, id, force?}`。

        异常:
            HTTPException 400: 缺少 id。
        """
        store = _store(body.get("path"))
        world_id = body.get("id")
        if not world_id:
            raise HTTPException(status_code=400, detail={"code": 2, "message": "缺少 id 参数"})
        force = bool(body.get("force"))

        return use_cases.call(
            lambda: render_cover(store, load_config(config_path), str(world_id), force=force),
            business=True,
        )
