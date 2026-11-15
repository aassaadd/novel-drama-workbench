"""章节场景拆分与影响面预览。"""

from __future__ import annotations

from novel_drama.interface.http.deps import run_cli
from novel_drama.interface.http.routers.common import (
    _respond,
)


def register_story_scene_routes(app, project_root: str | None = None, config_path: str | None = None) -> None:
    """注册「章节场景拆分与影响面预览」相关路由。

    参数:
        app: FastAPI 实例。
        project_root: 默认工程目录（请求未带 path 时使用）。
        config_path: 本机配置文件路径（与 serve 同源）。

    返回:
        无。
    """

    def _cli(*argv: str):
        """按 `serve --config` 的同一份配置执行 CLI。

        参数:
            argv: 不含 `--config` 的参数串。

        返回:
            `actions.CliResult`。
        """
        return run_cli(config_path, *argv)

    @app.post("/api/ui/scene/split")
    def ui_scene_split(body: dict) -> dict:
        """切分场景（`scene split`）；force 覆盖，keep_lines 保留未变化场景的台词。"""
        argv = ["--project", body["path"], "--json", "scene", "split", "--chapter", body["chapter"]]
        if body.get("force"):
            argv.append("--force")
        if body.get("keep_lines"):
            argv.append("--keep-lines")
        return _respond(_cli(*argv))

    @app.post("/api/ui/scene/split/impact")
    def ui_scene_split_impact(body: dict) -> dict:
        """重切场景的影响清单（`scene split --impact`，只读）。"""
        argv = ["--project", body["path"], "--json", "scene", "split", "--chapter", body["chapter"], "--impact"]
        if body.get("keep_lines"):
            argv.append("--keep-lines")
        return _respond(_cli(*argv))
