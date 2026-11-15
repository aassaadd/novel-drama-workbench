"""TTS 暂停状态与整批配音。"""

from __future__ import annotations

from fastapi import Query

from novel_drama.composition import QueueStore
from novel_drama.interface.http.deps import store_for
from novel_drama.models.ids import now_iso


def register_voice_tts_routes(app, project_root: str | None = None, config_path: str | None = None) -> None:
    """注册「TTS 暂停状态与整批配音」相关路由。

    参数:
        app: FastAPI 实例。
        project_root: 默认工程目录（请求未带 path 时使用）。
        config_path: 本机配置文件路径（与 serve 同源）。

    返回:
        无。
    """
    _store = store_for(project_root)

    @app.get("/api/ui/tts/pause")
    def ui_tts_pause_state(path: str = Query(...)) -> dict:
        """读取配音批次与媒体队列的暂停状态。

        参数:
            path: 工程目录。

        返回:
            `{paused 配音批次暂停, queue_paused 队列暂停}`。
        """
        store = _store(path)
        return {
            "paused": (store.root / ".ndw" / "tts.paused").exists(),
            "queue_paused": QueueStore(store).paused(),
        }

    @app.post("/api/ui/tts/pause")
    def ui_tts_pause(body: dict) -> dict:
        """暂停或继续配音批次（同时暂停媒体队列消费）。

        暂停只阻止「下一行开始」，正在合成的那一行会跑完；删除标记后自动续跑。

        参数:
            body: `{path, paused}`。

        返回:
            `{paused}`。
        """
        store = _store(body["path"])
        paused = bool(body.get("paused"))
        marker = store.root / ".ndw" / "tts.paused"
        if paused:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(now_iso(), encoding="utf-8")
        else:
            marker.unlink(missing_ok=True)
        QueueStore(store).set_paused(paused)
        return {"paused": paused}
