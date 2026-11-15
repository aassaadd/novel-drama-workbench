"""模型列表、LLM 调用计划与画面风格。"""

from __future__ import annotations

from fastapi import HTTPException, Query

from novel_drama.composition import comfy_media
from novel_drama.infrastructure.config import load_config
from novel_drama.interface.http.deps import store_for


def register_models_http_routes(app, project_root: str | None = None, config_path: str | None = None) -> None:
    """注册「模型列表、LLM 调用计划与画面风格」相关路由。

    参数:
        app: FastAPI 实例。
        project_root: 默认工程目录（请求未带 path 时使用）。
        config_path: 本机配置文件路径（与 serve 同源）。

    返回:
        无。
    """
    _store = store_for(project_root)

    @app.get("/api/ui/styles")
    def ui_styles():
        """固定画面风格预设（写实 / 动漫 / 古风等），图片与视频共用。"""
        from novel_drama.composition import load_styles

        cfg = load_config(config_path) if config_path else load_config()
        return {"styles": load_styles(comfy_media.templates_dir(cfg))}

    @app.post("/api/ui/llm/cancel")
    def ui_llm_cancel(body: dict | None = None) -> dict:
        """取消一次正在进行的模型调用。

        参数:
            body: `{call_id}`。

        返回:
            `{ok, call_id, cancelled}`；未命中或调用已结束时 `cancelled=false`，
            接口本身仍返回成功，方便前端重试与并发点击。

        说明:
            取消是正常结果，不是错误；总线下个分片会抛出 `LlmCancelled`。
        """
        from novel_drama.application.support.llm_cancel import cancel as cancel_llm

        call_id = str((body or {}).get("call_id") or "").strip()
        return {"ok": True, "call_id": call_id, "cancelled": cancel_llm(call_id)}

    @app.get("/api/ui/llm/plan")
    def ui_llm_plan(
        path: str = Query(...),
        action: str = Query(...),
        chapter: str = "",
        act: str = "",
        act_index: int = 0,
        count: int = 0,
        regen: bool = False,
        multi_turn: bool = False,
        beats_per_round: int = 0,
        max_rounds: int = 0,
        mode: str = "",
        character: str = "",
        acts: int = 0,
        chapters_per_act: int = 0,
        beats_per_chapter: int = 0,
        new_acts: int = 0,
        new_chapters: int = 0,
        from_stage: str = "",
        to_stage: str = "",
    ) -> dict:
        """返回某个模型动作的事实清单（次数、阶段、思考、能否取消）。

        参数:
            path: 工程根目录。
            action: 动作键。
            chapter / act / act_index: 作用范围。
            count / regen / multi_turn / beats_per_round / max_rounds / mode: 生成参数。
            character: 单个角色 ID（角色档案补全）。
            acts / chapters_per_act / beats_per_chapter / new_acts / new_chapters: 大纲规模。

        返回:
            统一确认弹窗所需字段。

        异常:
            HTTPException 400: 动作未登记。
        """
        from novel_drama.application.support.llm_plan import llm_plan

        store = _store(path)
        ctx = {
            "chapter": chapter,
            "act": act,
            "act_index": act_index,
            "count": count,
            "regen": regen,
            "multi_turn": multi_turn,
            "beats_per_round": beats_per_round,
            "max_rounds": max_rounds,
            "mode": mode,
            "character": character,
            "acts": acts,
            "chapters_per_act": chapters_per_act,
            "beats_per_chapter": beats_per_chapter,
            "new_acts": new_acts,
            "new_chapters": new_chapters,
            "from_stage": from_stage,
            "to_stage": to_stage,
        }
        try:
            return llm_plan(store, load_config(config_path), action, ctx)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"code": 2, "message": str(exc)})
