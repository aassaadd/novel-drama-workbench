"""台本读取与标注建议。"""

from __future__ import annotations

from fastapi import Query

from novel_drama.application.script.suggest_apply import suggest_and_apply
from novel_drama.infrastructure.config import load_config
from novel_drama.interface.http import use_cases
from novel_drama.interface.http.deps import store_for
from novel_drama.interface.http.routers.common import _require


def register_script_routes(app, project_root: str | None = None, config_path: str | None = None) -> None:
    """注册「台本读取与标注建议」相关路由。

    参数:
        app: FastAPI 实例。
        project_root: 默认工程目录（请求未带 path 时使用）。
        config_path: 本机配置文件路径（与 serve 同源）。

    返回:
        无。
    """
    _store = store_for(project_root)

    @app.get("/api/ui/script")
    def ui_script(path: str = Query(...), chapter: str = Query(...)) -> dict:
        """导演标注文本与脚本文件状态（读落盘产物，不重算）。

        额外解析落盘标注，返回 `warnings` 与结构化 `warning_items`，
        让页面重进后仍能看到告警并点击定位；解析失败只影响告警展示，不影响标注回显。
        """
        store = _store(path)
        markup_path = store.root / "scripts" / f"{chapter}.markup.txt"
        script_path = store.script_path(chapter)
        markup = markup_path.read_text(encoding="utf-8") if markup_path.exists() else ""
        warnings: list[str] = []
        warning_items: list[dict] = []
        if markup.strip():
            try:
                from novel_drama.director.markup import parse_markup

                warning_items = []
                script = parse_markup(
                    markup,
                    store.load_chapter(chapter),
                    store.load_characters()["characters"],
                    store.settings(),
                    diagnostics=warning_items,
                )
                warnings = script.get("warnings") or []
            except Exception:  # noqa: BLE001 - 告警展示失败不阻断标注回显
                warnings = []
                warning_items = []
        return {
            "markup_path": str(markup_path),
            "markup": markup,
            "script_path": str(script_path),
            "has_script": script_path.exists(),
            "warnings": warnings,
            "warning_items": warning_items,
        }

    @app.post("/api/ui/script/suggest")
    def ui_script_suggest(body: dict) -> dict:
        """生成标注建议（`script suggest`），可同时应用。"""
        _require(
            body,
            "本接口需要 path（工程目录）与 chapter（章节 ID）",
            path="工程目录",
            chapter="章节 ID",
        )
        store = _store(body["path"])
        outcome = use_cases.call(
            lambda: suggest_and_apply(
                store,
                load_config(config_path),
                body["chapter"],
                apply=bool(body.get("apply")),
                dry_run=bool(body.get("dry_run")),
                resume=bool(body.get("resume")),
            )
        )
        return outcome["payload"]
