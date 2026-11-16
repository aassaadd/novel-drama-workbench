"""媒体总线守卫测试。

覆盖两类回归：
1. 后端：媒体生成只有一个启动入口，路由层不得直接调用 provider / 批处理 / GPU
   执行函数，也不得新增分渠道的任务或进度接口；
2. 前端：媒体任务进度只能由 `media-bus.js` 统一轮询，页面不得自行轮询任务，
   也不得再引用已删除的旧接口。
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_JS = _ROOT / "web-next" / "js"
_PAGES = _JS / "pages"
_ROUTERS = _ROOT / "src" / "novel_drama" / "interface" / "http" / "routers"

#: 已删除的旧媒体接口（前端不得再出现）
_REMOVED_ENDPOINTS = (
    "/api/ui/sfx/generate",
    "/api/ui/sfx/job",
    "/api/ui/bgm/generate",
    "/api/ui/line/rerun",
    "/api/ui/tts/batch-all/progress",
    "/api/ui/voices/design/batches'",
    "/api/ui/voices/design/batches\"",
    "/api/ui/voices/design/batch'",
    "/api/ui/voices/design/batch\"",
    "/api/ui/cast/preview",
)

#: 路由层禁止直接调用的执行函数（只能出现在 media_handlers）
_FORBIDDEN_CALLS = (
    "run_media_kind(",
    "run_batch(",
    "run_chapter_tts(",
    "generate_sfx_item(",
    "render_bgm(",
    "create_batch(",
)

#: 禁止回流的旧路由：每个都是「分渠道启动/进度/批次轮询」
_FORBIDDEN_ROUTES = re.compile(
    r"^/api/ui/(?:"
    r"sfx/(?:generate|job)"
    r"|bgm/generate"
    r"|line/rerun"
    r"|tts/batch-all(?:/progress)?"
    r"|voices/design/batches"
    r"|voices/design/batch"
    r"|voices/design/batch/\{job_id\}/stream"
    r"|cast/preview"
    r")$"
)


def _router_sources() -> list[Path]:
    """列出后端路由源码（跳过缓存）。"""
    return sorted(path for path in _ROUTERS.rglob("*.py") if "__pycache__" not in path.parts)


def _page_sources() -> list[Path]:
    """列出前端页面源码。"""
    return sorted(_PAGES.glob("*.js"))


def test_single_media_start_entry():
    """只允许一个 `POST /api/ui/media/run` 启动入口（日志映射不算接口）。"""
    found: list[str] = []
    for path in _router_sources():
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r'@app\.\w+\("([^"]+)"', text):
            if match.group(1) == "/api/ui/media/run":
                found.append(path.name)
    assert found == ["media_bus.py"], f"媒体启动入口应只在 media_bus.py，实际：{found}"


def test_no_per_channel_media_routes():
    """路由里不得再出现分渠道启动/进度/批次轮询接口。"""
    offenders: list[str] = []
    for path in _router_sources():
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r'@app\.\w+\("([^"]+)"', text):
            route = match.group(1)
            if _FORBIDDEN_ROUTES.match(route):
                offenders.append(f"{path.name}: {route}")
    assert offenders == [], "发现旧的分渠道媒体接口：" + "；".join(offenders)


def test_media_routes_do_not_call_executors():
    """媒体路由只能调总线，不能直接调 provider / 批处理 / GPU 执行函数。"""
    offenders: list[str] = []
    for path in _router_sources():
        text = path.read_text(encoding="utf-8")
        for call in _FORBIDDEN_CALLS:
            if call in text:
                offenders.append(f"{path.name}: {call}")
    assert offenders == [], "路由层直接调用了媒体执行函数：" + "；".join(offenders)


def test_pages_do_not_use_removed_endpoints():
    """页面不得引用已删除的旧媒体接口。"""
    offenders: list[str] = []
    for path in _page_sources():
        text = path.read_text(encoding="utf-8")
        for endpoint in _REMOVED_ENDPOINTS:
            if endpoint in text:
                offenders.append(f"{path.name}: {endpoint}")
    assert offenders == [], "页面仍引用已删除接口：" + "；".join(offenders)


def test_pages_do_not_poll_media_jobs_directly():
    """页面不得自行轮询媒体任务，进度只能由 media-bus.js 提供。"""
    offenders: list[str] = []
    for path in _page_sources():
        text = path.read_text(encoding="utf-8")
        if "/api/ui/media/job" in text or "/api/ui/media/run" in text:
            offenders.append(path.name)
    assert offenders == [], "这些页面绕过 media-bus 直连媒体任务接口：" + "、".join(offenders)


def test_media_bus_owns_polling_and_exports():
    """统一模块必须自己负责轮询，并导出页面需要的入口。"""
    text = (_JS / "media-bus.js").read_text(encoding="utf-8")
    assert "/api/ui/media/job" in text
    assert "/api/ui/media/run" in text
    assert "/api/ui/media/cancel" in text
    for name in (
        "fetchMediaPlan",
        "confirmMedia",
        "runMedia",
        "watchMediaJob",
        "cancelMediaJob",
        "mediaJobCardHTML",
        "mediaJobRowsHTML",
    ):
        # 展示函数从 media-format.js 集中实现、由媒体总线统一导出
        assert f"export function {name}" in text or f"export async function {name}" in text or f"  {name}," in text


def test_progress_module_has_no_media_polling():
    """进度浮层只负责展示，不再内置任何后台任务轮询。"""
    text = (_JS / "progress.js").read_text(encoding="utf-8")
    assert "watchProgress" not in text
    assert "batch-all" not in text
    assert "getJSON" not in text
