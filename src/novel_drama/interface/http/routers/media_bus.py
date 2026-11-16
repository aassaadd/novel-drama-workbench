"""统一媒体生成总线路由：启动、计划、查询与取消。

所有声音/图片/视频/音效/BGM 生成都走 `POST /api/ui/media/run`，
只通过 `action + params` 区分；进度与取消按 `job_id` 统一处理。
"""

from __future__ import annotations

from fastapi import HTTPException, Query, Request

from novel_drama.application.support import media_bus, media_plan
from novel_drama.infrastructure.config import load_config
from novel_drama.interface.http.deps import store_for


def register_media_bus_routes(
    app,
    project_root: str | None = None,
    config_path: str | None = None,
) -> None:
    """注册统一媒体生成总线路由。

    参数:
        app: FastAPI 实例。
        project_root: 默认工程目录（请求未带 path 时使用）。
        config_path: 本机配置文件路径（与 serve 同源）。

    返回:
        无。
    """
    _store = store_for(project_root)

    def _spec(action: str):
        """校验并返回动作规格。

        参数:
            action: 动作键。

        返回:
            `MediaActionSpec`。

        异常:
            HTTPException 400: 动作未登记。
        """
        try:
            return media_plan.action_spec(action)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"code": 2, "message": str(exc)}) from exc

    @app.post("/api/ui/media/run")
    def ui_media_bus_run(body: dict) -> dict:
        """启动一次媒体生成并立即返回统一任务快照。

        参数:
            body: `{action, params}`；`params` 必须含 `path`。

        返回:
            新建任务的统一快照。

        异常:
            HTTPException 400: action 未登记、params 不合法或执行器未注册。
        """
        action = str(body.get("action") or "").strip()
        if not action:
            raise HTTPException(
                status_code=400,
                detail={"code": 2, "message": "缺少必填参数：action（媒体动作）"},
            )
        _spec(action)
        params = body.get("params")
        if not isinstance(params, dict):
            raise HTTPException(
                status_code=400,
                detail={"code": 2, "message": "params 必须是对象，且包含 path（工程目录）等参数"},
            )
        path = str(params.get("path") or "").strip()
        if not path:
            raise HTTPException(
                status_code=400,
                detail={"code": 2, "message": "缺少必填参数：path（工程目录）"},
            )
        store = _store(path)
        try:
            return media_bus.run(store, load_config(config_path), action, params)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"code": 2, "message": str(exc)}) from exc

    @app.get("/api/ui/media/plan")
    def ui_media_bus_plan(request: Request) -> dict:
        """返回一次媒体生成的事实清单（供统一确认弹窗）。

        参数:
            request: 请求对象；全部 query 参数转成 dict，`action` 必填，`path` 可空。

        返回:
            `media_bus.plan` 的结果。

        异常:
            HTTPException 400: 缺少 action 或动作未登记。
        """
        params = dict(request.query_params)
        action = str(params.pop("action", "") or "").strip()
        if not action:
            raise HTTPException(
                status_code=400,
                detail={"code": 2, "message": "缺少必填参数：action（媒体动作）"},
            )
        _spec(action)
        path = str(params.get("path") or "").strip()
        store = _store(path or None) if (path or project_root) else None
        return media_bus.plan(store, load_config(config_path), action, params)

    @app.get("/api/ui/media/job/{job_id}")
    def ui_media_bus_job(job_id: str, path: str = Query(...)) -> dict:
        """读取一条媒体任务的统一快照。

        参数:
            job_id: 任务 ID。
            path: 工程目录。

        返回:
            统一任务快照。

        异常:
            HTTPException 404: 任务不存在或已被清理。
        """
        store = _store(path)
        snap = media_bus.snapshot(store, job_id)
        if snap is None:
            raise HTTPException(
                status_code=404,
                detail={"code": 4, "message": "任务不存在，可能已被清理"},
            )
        return snap

    @app.post("/api/ui/media/cancel")
    def ui_media_bus_cancel(body: dict) -> dict:
        """按 job_id 协作式取消一条媒体任务（幂等）。

        参数:
            body: `{path, job_id}`。

        返回:
            `{canceled, status, forced, message}`。

        异常:
            HTTPException 400: 缺少参数。
            HTTPException 404: 任务不存在。
        """
        path = str(body.get("path") or "").strip()
        job_id = str(body.get("job_id") or "").strip()
        missing = [
            name
            for name, value in (("path（工程目录）", path), ("job_id（任务 ID）", job_id))
            if not value
        ]
        if missing:
            raise HTTPException(
                status_code=400,
                detail={"code": 2, "message": f"缺少必填参数：{'、'.join(missing)}"},
            )
        store = _store(path)
        try:
            return media_bus.cancel(store, job_id)
        except KeyError as exc:
            raise HTTPException(
                status_code=404,
                detail={"code": 4, "message": "任务不存在，可能已被清理"},
            ) from exc
