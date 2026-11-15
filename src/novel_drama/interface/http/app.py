"""FastAPI 应用装配：注册各域路由 + 挂载前端静态目录。

页面只消费应用层读模型与用例：所有读写都在 `application/*`，这里不重算状态、
不复制业务规则。接口统一以 `path` 查询参数指定工程目录，避免引入工程注册服务。
"""

from __future__ import annotations

import logging
from pathlib import Path

from novel_drama import __version__
from novel_drama.interface.http.routers.align import register_align_routes
from novel_drama.interface.http.routers.bgm import register_bgm_routes
from novel_drama.interface.http.routers.cast.cast import register_cast_routes
from novel_drama.interface.http.routers.cast.character_desc import (
    register_character_desc_routes,
)
from novel_drama.interface.http.routers.cast.characters import (
    register_cast_characters_routes,
)
from novel_drama.interface.http.routers.common import (
    _ACTIVE_TTS_PROJECTS,
    _stop_tts_project,
)
from novel_drama.interface.http.routers.config import register_config_routes
from novel_drama.interface.http.routers.jobs import register_jobs_routes
from novel_drama.interface.http.routers.logs import register_logs_routes
from novel_drama.interface.http.routers.media import register_media_routes
from novel_drama.interface.http.routers.models import register_models_http_routes
from novel_drama.interface.http.routers.models_admin import register_models_routes
from novel_drama.interface.http.routers.script import register_script_routes
from novel_drama.interface.http.routers.sfx import register_sfx_routes
from novel_drama.interface.http.routers.story.chapter import (
    register_story_chapter_routes,
)
from novel_drama.interface.http.routers.story.chapters import (
    register_story_chapters_routes,
)
from novel_drama.interface.http.routers.story.continue_chapter import (
    register_continue_routes,
)
from novel_drama.interface.http.routers.story.outline import (
    register_story_outline_routes,
)
from novel_drama.interface.http.routers.story.outline_edit import (
    register_outline_edit_routes,
)
from novel_drama.interface.http.routers.story.scene import register_story_scene_routes
from novel_drama.interface.http.routers.story.setting import (
    register_story_setting_routes,
)
from novel_drama.interface.http.routers.story.worlds import register_story_worlds_routes
from novel_drama.interface.http.routers.stream import register_stream_routes
from novel_drama.interface.http.routers.voice.line import register_voice_line_routes
from novel_drama.interface.http.routers.voice.tools import register_voice_tools_routes
from novel_drama.interface.http.routers.voice.tts import register_voice_tts_routes
from novel_drama.interface.http.routers.voice.voices import register_voice_voices_routes

logger = logging.getLogger(__name__)

#: 唯一前端目录（仓库根 web-next/）；直接挂到根路径，旧 static 界面已废弃
WEB_NEXT_DIR = Path(__file__).resolve().parents[4] / "web-next"


def create_app(project_root: str | None = None, config_path: str | None = None):
    """创建 FastAPI 应用。

    参数:
        project_root: 默认工程目录（可被请求参数覆盖）。
        config_path: 本机配置文件路径；只用于给后台生成进程透传，保证与 `serve` 同源。

    返回:
        FastAPI 实例。

    异常:
        RuntimeError: 未安装 fastapi，或缺少前端目录。
    """
    try:
        from fastapi import FastAPI
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("未安装 Web 依赖：pip install fastapi uvicorn（或 pip install -e '.[web]'）") from exc

    app = FastAPI(title="novel-drama-workbench", version=__version__)

    @app.on_event("shutdown")
    def _shutdown_tts_batches() -> None:
        """Web 服务退出时回收本次启动的配音批次，避免留下无主 CLI/worker 进程。"""
        for root in list(_ACTIVE_TTS_PROJECTS):
            try:
                _stop_tts_project(root)
            except Exception:  # noqa: BLE001, S110 - 退出钩子不能抛错
                pass
            _ACTIVE_TTS_PROJECTS.discard(root)

    register_config_routes(app, project_root, config_path)
    register_story_chapters_routes(app, project_root, config_path)
    register_story_chapter_routes(app, project_root, config_path)
    register_story_outline_routes(app, project_root, config_path)
    register_outline_edit_routes(app)
    register_continue_routes(app, config_path)
    register_story_scene_routes(app, project_root, config_path)
    register_story_worlds_routes(app, project_root, config_path)
    register_story_setting_routes(app, project_root, config_path)
    register_cast_characters_routes(app, project_root, config_path)
    register_character_desc_routes(app, config_path)
    register_cast_routes(app, project_root, config_path)
    register_voice_voices_routes(app, project_root, config_path)
    register_voice_line_routes(app, project_root, config_path)
    register_voice_tts_routes(app, project_root, config_path)
    register_voice_tools_routes(app, config_path)
    register_script_routes(app, project_root, config_path)
    register_media_routes(app, project_root, config_path)
    register_align_routes(app)
    register_bgm_routes(app, config_path)
    register_sfx_routes(app, config_path)
    register_models_http_routes(app, project_root, config_path)
    register_models_routes(app, config_path)
    register_jobs_routes(app, project_root, config_path)
    register_logs_routes(app, config_path)
    register_stream_routes(app, config_path)

    from fastapi.staticfiles import StaticFiles

    class NoCacheStaticFiles(StaticFiles):
        """静态资源禁用强缓存。

        本地工作台改完前端只需刷新页面即可生效，避免浏览器按启发式缓存
        继续使用旧 JS，导致改了代码但界面没变化。
        """

        def file_response(self, *args, **kwargs):
            """在原始静态响应上补一条 no-cache。"""
            response = super().file_response(*args, **kwargs)
            response.headers["Cache-Control"] = "no-cache"
            return response

    # 前端只有 web-next 一套：直接挂在根路径。hash 路由不需要服务端回退。
    # 缺少目录时直接报错，不回退到旧的 static 界面。
    if not WEB_NEXT_DIR.exists():
        raise RuntimeError(f"缺少前端目录：{WEB_NEXT_DIR}；请从仓库根启动，旧 static 界面已废弃")
    app.mount("/", NoCacheStaticFiles(directory=WEB_NEXT_DIR, html=True), name="web")

    return app


def serve(host: str = "127.0.0.1", port: int = 8765, project_root: str | None = None, config_path: str | None = None) -> None:
    """启动本地 Web 服务。

    参数:
        host: 监听地址（默认仅本机）。
        port: 端口。
        project_root: 默认工程目录。
        config_path: 本机配置文件路径（透传给后台生成进程）。
    """
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("未安装 uvicorn：pip install uvicorn") from exc
    uvicorn.run(create_app(project_root, config_path), host=host, port=port)


def default_project_path(value: str | None) -> Path | None:
    """把可选的路径字符串转为 Path（供测试与外部调用）。"""
    return Path(value).expanduser() if value else None
