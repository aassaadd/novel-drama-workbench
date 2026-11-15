"""章节场景拆分与影响面预览。"""

from __future__ import annotations

from novel_drama.application.story.scene_split import scene_split_impact, split_scenes
from novel_drama.interface.http import use_cases
from novel_drama.interface.http.deps import store_for


def register_story_scene_routes(app, project_root: str | None = None, config_path: str | None = None) -> None:
    """注册「章节场景拆分与影响面预览」相关路由。

    参数:
        app: FastAPI 实例。
        project_root: 默认工程目录（请求未带 path 时使用）。
        config_path: 本机配置文件路径（与 serve 同源）。

    返回:
        无。
    """

    _store = store_for(project_root)

    @app.post("/api/ui/scene/split")
    def ui_scene_split(body: dict) -> dict:
        """切分场景（`scene split`）；force 覆盖，keep_lines 保留未变化场景的台词。"""
        store = _store(body["path"])
        # 缺 chapter 属于请求体错误：与旧实现一致，直接抛 KeyError（500），不走用例翻译
        chapter_id = body["chapter"]
        scenes = use_cases.call(
            lambda: split_scenes(
                store,
                chapter_id,
                force=bool(body.get("force")),
                keep_lines=bool(body.get("keep_lines")),
            )
        )
        return {"dry_run": False, "scenes": scenes}

    @app.post("/api/ui/scene/split/impact")
    def ui_scene_split_impact(body: dict) -> dict:
        """重切场景的影响清单（`scene split --impact`，只读）。"""
        store = _store(body["path"])
        chapter_id = body["chapter"]
        return use_cases.call(
            lambda: scene_split_impact(
                store, chapter_id, keep_lines=bool(body.get("keep_lines"))
            )
        )
