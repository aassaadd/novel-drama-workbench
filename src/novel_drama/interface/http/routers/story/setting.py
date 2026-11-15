"""写作设定分区的读取、改写、生成与润色。"""

from __future__ import annotations

from fastapi import HTTPException

from novel_drama.application.production.job_tracker import (
    UiJob,
)
from novel_drama.application.story.settings_edit import apply_section, set_scalars
from novel_drama.interface.http import use_cases
from novel_drama.interface.http.deps import run_cli, store_for
from novel_drama.interface.http.routers.common import (
    _respond,
    _respond_business,
)

SECTION_LABEL = {
    "premise": "故事内核",
    "style": "文风",
    "character": "人物设定",
}

SETTING_SCALARS = (
    "logline",
    "theme",
    "core_conflict",
    "ending_intent",
    "target_audience",
    "target_length",
    "tone",
    "pov",
    "tense",
    "prose_style",
    "visual_style",
)

def register_story_setting_routes(app, project_root: str | None = None, config_path: str | None = None) -> None:
    """注册「写作设定分区的读取、改写、生成与润色」相关路由。

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

    @app.post("/api/ui/setting/set")
    def ui_setting_set(body: dict) -> dict:
        """修改设定库标量字段（应用层用例 `set_scalars`，与 `setting set` 同源）。"""
        store = _store(body["path"])
        fields = body.get("fields") or {}
        values = {key: fields.get(key) for key in SETTING_SCALARS if fields.get(key) not in (None, "")}
        if not values:
            raise HTTPException(status_code=400, detail={"code": 2, "message": "没有要修改的字段"})
        return use_cases.call(lambda: set_scalars(store, values))

    @app.post("/api/ui/setting/generate")
    def ui_setting_generate(body: dict) -> dict:
        """预览 AI 生成的设定（`setting generate --dry-run`，不落盘）。

        参数:
            body: `{path, section?, brief?, target_id?, step?, overwrite?}`；
            section 缺省时保持旧的 step 行为。

        异常:
            HTTPException 400: section / step 缺失或非法。
        """
        path = body.get("path")
        store = _store(path)
        section = str(body.get("section") or "").strip()
        if section:
            if section not in ("premise", "style", "character"):
                raise HTTPException(
                    status_code=400,
                    detail={"code": 2, "message": "section 必须是 premise / style / character（世界观请用世界模块）"},
                )
            argv = ["--project", path, "--json", "setting", "generate", "--section", section, "--dry-run"]
            if body.get("brief"):
                argv += ["--brief", str(body["brief"])]
            if body.get("target_id"):
                argv += ["--target-id", str(body["target_id"])]
            # overwrite=false 表示「只补缺失」：保留已有字段，只生成空缺
            if body.get("overwrite") is False:
                argv.append("--keep-existing")
            # 显式固定种子才传；缺省不固定，保证同一设定重复生成结果不同
            if body.get("seed") is not None and str(body.get("seed")).strip() != "":
                try:
                    argv += ["--seed", str(int(body["seed"]))]
                except (TypeError, ValueError):
                    raise HTTPException(status_code=400, detail={"code": 2, "message": "seed 必须是整数"})
            job = UiJob(store, "setting", {"label": SECTION_LABEL.get(section, section)}, f"生成{SECTION_LABEL.get(section, section)}")
            job.start("已提交生成请求")
            try:
                payload = _respond_business(_cli(*argv))
            except Exception as exc:  # noqa: BLE001 - 失败原因写进任务队列后再抛出
                job.fail(str(getattr(exc, "detail", exc)))
                raise
            job.finish("生成完成")
            return payload
        step = str(body.get("step") or "").strip()
        if step not in ("premise", "style"):
            raise HTTPException(status_code=400, detail={"code": 2, "message": "step 必须是 premise / style（世界观请用世界模块）"})
        argv = ["--project", path, "--json", "setting", "generate", "--step", step, "--dry-run"]
        if body.get("overwrite"):
            argv.append("--overwrite")
        job = UiJob(store, "setting", {"label": SECTION_LABEL.get(step, step)}, f"生成{SECTION_LABEL.get(step, step)}")
        job.start("已提交生成请求")
        try:
            payload = _respond(_cli(*argv))
        except Exception as exc:  # noqa: BLE001
            job.fail(str(getattr(exc, "detail", exc)))
            raise
        job.finish("生成完成")
        return payload

    @app.post("/api/ui/setting/apply")
    def ui_setting_apply(body: dict) -> dict:
        """把前端确认后的字段合并写入设定库分区（`setting apply --file`）。

        参数:
            body: `{path, section: premise|style, fields: {...}}`。

        异常:
            HTTPException 400: section / fields 不合法。
        """
        path = body.get("path")
        _store(path)
        section = str(body.get("section") or "").strip()
        if section not in ("premise", "style"):
            raise HTTPException(status_code=400, detail={"code": 2, "message": "section 必须是 premise / style（世界观请用世界模块）"})
        fields = body.get("fields")
        if not isinstance(fields, dict) or not fields:
            raise HTTPException(status_code=400, detail={"code": 2, "message": "fields 必须是非空对象"})
        store = _store(path)
        payload = use_cases.call(lambda: apply_section(store, section, fields))
        return {key: payload[key] for key in ("section", "changed", "settings_revision")}

    @app.post("/api/ui/setting/polish")
    def ui_setting_polish(body: dict) -> dict:
        """润色单个字段（`setting polish`，不写盘）。

        参数:
            body: `{path, field, text?, context?, character?}`；text 缺省读取当前值。

        异常:
            HTTPException 400: field 缺失或不受支持。
        """
        path = body.get("path")
        _store(path)
        field = str(body.get("field") or "").strip()
        if not field:
            raise HTTPException(status_code=400, detail={"code": 2, "message": "缺少 field 参数"})
        argv = ["--project", path, "--json", "setting", "polish", "--field", field]
        if body.get("text") is not None:
            argv += ["--text", str(body["text"])]
        if body.get("context"):
            argv += ["--context", str(body["context"])]
        if body.get("character"):
            argv += ["--character", str(body["character"])]
        return _respond(_cli(*argv))
