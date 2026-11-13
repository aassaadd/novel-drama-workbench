"""人物描述补全接口：给已有人物补生成音色 / 外貌的中英文描述。

只补缺失字段，不覆盖已有内容；单个角色失败不影响其他角色。
业务实现仍在 `writing.character_plan.fill_character_descriptions`，
这里只做工程校验、分角色过滤与结构化返回，不复制清洗与校验规则。
"""

from __future__ import annotations

from fastapi import HTTPException

from ..config import load_config
from ..store import ProjectStore
from ..writing.character_plan import fill_character_descriptions


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
            body: `{path, names?: string[]}`；names 为空表示全部非旁白角色。

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
        try:
            result = fill_character_descriptions(
                store,
                load_config(config_path) if config_path else load_config(),
                names=names or None,
            )
        except Exception as exc:  # noqa: BLE001 - 统一转中文错误，避免前端拿到 500 堆栈
            raise HTTPException(status_code=500, detail={"code": 5, "message": f"补全描述失败：{exc}"}) from exc
        return {
            "total": result.get("total", 0),
            "updated": result.get("updated", []),
            "skipped": result.get("skipped", []),
            "failures": result.get("failures", []),
            "summary": result.get("summary", ""),
        }
