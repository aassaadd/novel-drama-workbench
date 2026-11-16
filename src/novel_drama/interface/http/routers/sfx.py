"""媒体页音效接口：只保留 provider 健康状态。

音效生成已统一由 `POST /api/ui/media/run`（action=`sfx.item` / `sfx.chapter`）
启动，任务进度与取消走统一的 `/api/ui/media/job/{id}` 与 `/api/ui/media/cancel`。
"""

from __future__ import annotations

from novel_drama.composition import get_provider, sfx_engine
from novel_drama.infrastructure.config import load_config
from novel_drama.sfx import SfxError


def register_sfx_routes(app, config_path: str | None = None) -> None:
    """注册音效相关接口。

    参数:
        app: FastAPI 应用。
        config_path: 本机配置文件路径。
    """

    @app.get("/api/ui/sfx/health")
    def ui_sfx_health() -> dict:
        """返回音效引擎与 provider 可用性（页面据此显示下载/检查入口）。"""
        config = load_config(config_path)
        engine = sfx_engine(config)
        if engine == "offline":
            return {"engine": "offline", "ok": True, "detail": "当前为 ffmpeg 离线合成（sfx.engine=offline）", "provider_id": None}
        try:
            pid, provider = get_provider(config)
            health = provider.health()
            return {
                "engine": "model",
                "ok": health.ok,
                "detail": health.detail,
                "code": health.code,
                "provider_id": pid,
                "warm": bool(getattr(provider, "is_loaded", lambda: False)()),
                "idle_timeout_s": getattr(provider, "idle_timeout_s", None),
            }
        except SfxError as exc:
            return {"engine": "model", "ok": False, "detail": exc.message, "code": exc.code, "provider_id": None}
