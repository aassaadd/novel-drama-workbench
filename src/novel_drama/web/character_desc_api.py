"""人物描述补全接口：给已有人物补生成音色 / 外貌的中英文描述。

只补缺失字段，不覆盖已有内容；单个角色失败不影响其他角色。
业务实现仍在 `writing.character_plan.fill_character_descriptions`，
这里只做工程校验、分角色过滤与结构化返回，不复制清洗与校验规则。
"""

from __future__ import annotations

from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from ..config import load_config
from ..store import ProjectStore
from ..writing.character_plan import fill_character_descriptions
from .stream_api import _progress_response


def register_character_desc_routes(app, config_path: str | None = None) -> None:
    """注册人物描述补全接口。

    参数:
        app: FastAPI 实例。
        config_path: 本机配置文件路径（与 serve 同源）。

    返回:
        无。
    """

    @app.post("/api/ui/characters/descriptions/fill")
    def ui_character_descriptions_fill(body: dict) -> dict:
        """补全缺失的音色 / 外貌中英文描述（只补缺失）。

        参数:
            body: `{path, names?: string[], overwrite?: boolean}`；names 为空表示全部非旁白角色，
            overwrite=true 时重新生成全部描述字段并覆盖已有内容。
        返回:
            `{total, updated, skipped, failures, summary}`；
            failures 每项 `{name, message}`，有失败时仍返回 200，页面据此提示重试。

        异常:
            HTTPException 400: 缺少 path。
            HTTPException 404: 工程不存在。
            HTTPException 500: 补全流程本身抛错（非单个角色失败）。
        """
        path = (body or {}).get("path")
        if not path:
            raise HTTPException(status_code=400, detail={"code": 2, "message": "缺少 path 参数"})
        store = ProjectStore(path)
        if not store.exists():
            raise HTTPException(status_code=404, detail={"code": 3, "message": f"工程不存在: {store.root}"})
        names = [str(item) for item in ((body or {}).get("names") or []) if str(item).strip()]
        overwrite = bool((body or {}).get("overwrite"))
        try:
            result = fill_character_descriptions(
                store,
                load_config(config_path) if config_path else load_config(),
                names=names or None,
                overwrite=overwrite,
            )
        except Exception as exc:  # noqa: BLE001 - 统一转中文错误，避免前端拿到 500 堆栈
            raise HTTPException(status_code=500, detail={"code": 5, "message": f"补全描述失败：{exc}"}) from exc
        return {
            "total": result.get("total", 0),
            "updated": result.get("updated", []),
            "skipped": result.get("skipped", []),
            "failures": result.get("failures", []),
            "summary": result.get("summary", ""),
            "overwrite": result.get("overwrite", overwrite),
        }

    @app.post("/api/ui/characters/descriptions/fill/stream")
    def ui_character_descriptions_fill_stream(body: dict) -> StreamingResponse:
        """补全缺失描述（流式进度）：逐角色推进，可中途停止。

        事件：start → total{total,pending,skipped,narrator} →
        (batch{index,total,name} → chars* → item{status})* → done(result) / error(message)。
        item.status 取 done / skipped / failed；skipped 的 reason 为「已齐全」或「旁白」。
        停止后已完成的角色已逐个落盘，重跑只会处理仍缺失的角色。

        参数:
            body: `{path, names?: string[], overwrite?: boolean}`；names 为空表示全部非旁白角色，
            overwrite=true 时重新生成全部描述字段并覆盖已有内容。
        返回:
            StreamingResponse（text/event-stream）。

        异常:
            HTTPException 400: 缺少 path。
            HTTPException 404: 工程不存在。
        """
        path = (body or {}).get("path")
        if not path:
            raise HTTPException(status_code=400, detail={"code": 2, "message": "缺少 path 参数"})
        store = ProjectStore(path)
        if not store.exists():
            raise HTTPException(status_code=404, detail={"code": 3, "message": f"工程不存在: {store.root}"})
        names = [str(item) for item in ((body or {}).get("names") or []) if str(item).strip()]
        overwrite = bool((body or {}).get("overwrite"))
        config = load_config(config_path) if config_path else load_config()

        def task(on_progress):
            """逐个角色补全并透出进度。"""
            return fill_character_descriptions(
                store,
                config,
                names=names or None,
                on_progress=on_progress,
                overwrite=overwrite,
            )

        return _progress_response(task)
