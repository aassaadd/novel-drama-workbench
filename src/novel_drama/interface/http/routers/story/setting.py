"""写作设定分区的读取、改写、生成与润色。"""

from __future__ import annotations

from fastapi import HTTPException

from novel_drama.application.production.job_tracker import (
    UiJob,
)
from novel_drama.application.story.settings_edit import (
    apply_section,
    current_field_text,
    set_scalars,
    settings_revision,
)
from novel_drama.application.story.settings_generate import (
    generate_section,
    generate_settings,
    polish_field,
)
from novel_drama.infrastructure.config import load_config
from novel_drama.interface.http import use_cases
from novel_drama.interface.http.deps import store_for

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
            seed = None
            # 显式固定种子才传；缺省不固定，保证同一设定重复生成结果不同
            if body.get("seed") is not None and str(body.get("seed")).strip() != "":
                try:
                    seed = int(body["seed"])
                except (TypeError, ValueError):
                    raise HTTPException(status_code=400, detail={"code": 2, "message": "seed 必须是整数"})
            overwrite = body.get("overwrite") is not False
            job = UiJob(store, "setting", {"label": SECTION_LABEL.get(section, section)}, f"生成{SECTION_LABEL.get(section, section)}")
            job.start("已提交生成请求")
            try:
                result = use_cases.call(
                    lambda: generate_section(
                        store,
                        load_config(config_path),
                        section,
                        brief=str(body["brief"]) if body.get("brief") else None,
                        target_id=str(body["target_id"]) if body.get("target_id") else None,
                        seed=seed,
                        overwrite=overwrite,
                    ),
                    business=True,
                )
            except Exception as exc:  # noqa: BLE001 - 失败原因写进任务队列后再抛出
                job.fail(str(getattr(exc, "detail", exc)))
                raise
            job.finish("生成完成")
            return {
                "section": result["section"],
                "fields": result["fields"] or {},
                "skipped": result.get("skipped") or [],
                "overwrite": overwrite,
            }
        step = str(body.get("step") or "").strip()
        if step not in ("premise", "style"):
            raise HTTPException(status_code=400, detail={"code": 2, "message": "step 必须是 premise / style（世界观请用世界模块）"})
        overwrite_step = bool(body.get("overwrite"))
        job = UiJob(store, "setting", {"label": SECTION_LABEL.get(step, step)}, f"生成{SECTION_LABEL.get(step, step)}")
        job.start("已提交生成请求")
        try:
            result = use_cases.call(
                lambda: generate_settings(
                    store, load_config(config_path), step, overwrite=overwrite_step
                )
            )
        except Exception as exc:  # noqa: BLE001
            job.fail(str(getattr(exc, "detail", exc)))
            raise
        job.finish("生成完成")
        return {**result, "applied": False, "settings_revision": settings_revision(store)}

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
        store = _store(path)
        field = str(body.get("field") or "").strip()
        if not field:
            raise HTTPException(status_code=400, detail={"code": 2, "message": "缺少 field 参数"})
        character_id = str(body["character"]) if body.get("character") else None

        def _polish() -> dict:
            """读取待润色文本（未传时取当前值）并调用润色用例。"""
            source = body.get("text")
            if source is None:
                source = current_field_text(store, field, character_id)
            return polish_field(
                store,
                load_config(config_path),
                field,
                str(source),
                context=str(body["context"]) if body.get("context") else None,
            )

        return {**use_cases.call(_polish), "applied": False}
