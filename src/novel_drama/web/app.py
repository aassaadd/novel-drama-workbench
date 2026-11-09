"""展示层 HTTP 骨架（FastAPI）。

页面只消费 CLI 的输出：所有读写都经 `web/actions.run` 调 `cli.main`，
不在这里重算行状态，也不复制业务规则。
接口统一以 `path` 查询参数指定工程目录，避免引入工程注册服务。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .. import __version__
from ..models.ids import now_iso
from ..store import ProjectStore, list_workspaces
from .actions import fail_body, run

STATIC_DIR = Path(__file__).with_name("static")


def create_app(project_root: str | None = None, config_path: str | None = None):
    """创建 FastAPI 应用。

    参数:
        project_root: 默认工程目录（可被请求参数覆盖）。
        config_path: 本机配置文件路径；只用于给后台生成进程透传，保证与 `serve` 同源。

    返回:
        FastAPI 实例。

    异常:
        RuntimeError: 未安装 fastapi。
    """
    try:
        from fastapi import FastAPI, HTTPException, Query
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("未安装 Web 依赖：pip install fastapi uvicorn（或 pip install -e '.[web]'）") from exc

    app = FastAPI(title="novel-drama-workbench", version=__version__)

    def _store(path: str | None) -> ProjectStore:
        """按请求参数定位工程。

        参数:
            path: 工程目录。

        返回:
            ProjectStore。

        异常:
            HTTPException: 未指定或未初始化。
        """
        root = path or project_root
        if not root:
            raise HTTPException(status_code=400, detail="缺少 path 参数（工程目录）")
        store = ProjectStore(root)
        if not store.exists():
            raise HTTPException(status_code=404, detail=f"工程不存在: {store.root}")
        return store

    @app.get("/api/health")
    def health() -> dict:
        """健康检查。"""
        return {"ok": True, "version": __version__}

    @app.get("/api/projects")
    def projects() -> list[dict]:
        """列出本机工程。"""
        return list_workspaces()

    @app.get("/api/project/info")
    def project_info(path: str | None = Query(default=None)) -> dict:
        """工程概览（作品、章节数、角色数）。"""
        store = _store(path)
        project = store.load_project()
        return {"root": str(store.root), "work": project["work"], "chapters": store.list_chapters(), "characters": len(store.load_characters()["characters"])}

    @app.get("/api/project/chapters")
    def chapters(path: str | None = Query(default=None)) -> list[dict]:
        """章节列表。"""
        return _store(path).list_chapters()

    @app.get("/api/project/chapter/{chapter_id}")
    def chapter(chapter_id: str, path: str | None = Query(default=None)) -> dict:
        """章节详情（含场景与台词）。"""
        try:
            return _store(path).load_chapter(chapter_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/project/timeline/{chapter_id}")
    def timeline(chapter_id: str, path: str | None = Query(default=None)) -> dict:
        """时间轴详情（未构建时 404）。"""
        store = _store(path)
        timeline_path = store.timeline_path(chapter_id)
        if not timeline_path.exists():
            raise HTTPException(status_code=404, detail="时间轴尚未构建")
        return __import__("json").loads(timeline_path.read_text(encoding="utf-8"))

    # ---------------- 台词校对页（P4）----------------
    # 读接口一律把 CLI 的 --json 原样透传，前端不做状态推导。

    def _respond(result):
        """把 CLI 结果转成 HTTP 响应。

        参数:
            result: `actions.CliResult`。

        返回:
            CLI 的 JSON 负载。

        异常:
            HTTPException: CLI 退出码非 0 时按映射返回状态码与中文说明。
        """
        if result.code != 0:
            raise HTTPException(status_code=result.status, detail=fail_body(result))
        return result.payload if result.payload is not None else {"text": result.text}

    def _cli(*argv: str):
        """按 `serve --config` 的同一份配置执行 CLI，避免界面与终端读不同配置。

        参数:
            argv: 不含 `--config` 的参数串。

        返回:
            `actions.CliResult`。
        """
        if config_path and "--config" not in argv:
            return run("--config", config_path, *argv)
        return run(*argv)

    @app.get("/api/ui/chapters")
    def ui_chapters(path: str = Query(...)):
        """章节目录（`chapter list --json`）。"""
        return _respond(_cli("--project", path, "--json", "chapter", "list"))

    @app.get("/api/ui/chapter")
    def ui_chapter(path: str = Query(...), chapter: str = Query(...)):
        """章节详情（含场景，`chapter show <id> --json`）。"""
        return _respond(_cli("--project", path, "--json", "chapter", "show", chapter))

    @app.get("/api/ui/lines")
    def ui_lines(
        path: str = Query(...),
        chapter: str = Query(...),
        status: str | None = Query(default=None),
    ) -> dict:
        """台词表（`line list --json`，状态与筛选全部由 CLI 计算）。

        `status` 支持逗号分隔的 `pending,stale,ready,failed,unknown`，与终端用法一致。
        """
        argv = ["--project", path, "--json", "line", "list", "--chapter", chapter]
        if status:
            argv += ["--status", status]
        return _respond(_cli(*argv))

    @app.get("/api/ui/line/diff")
    def ui_line_diff(path: str = Query(...), chapter: str = Query(...), line: str = Query(...)) -> dict:
        """单行参数差异（`line diff --json`）。"""
        return _respond(_cli("--project", path, "--json", "line", "diff", "--chapter", chapter, "--line", line))

    @app.get("/api/ui/line")
    def ui_line(path: str = Query(...), chapter: str = Query(...), line: str = Query(...)):
        """单行详情（`line list --line --json`），供队列完成后按行回填。"""
        return _respond(_cli("--project", path, "--json", "line", "list", "--chapter", chapter, "--line", line))

    @app.get("/api/ui/plan")
    def ui_plan(path: str = Query(...), chapter: str = Query(...)):
        """本次将生成多少行（`tts plan --json`）。"""
        return _respond(_cli("--project", path, "--json", "tts", "plan", "--chapter", chapter))

    @app.get("/api/ui/queue")
    def ui_queue(path: str = Query(...)):
        """队列快照（`queue list --json`）：轮询只打这个接口。"""
        return _respond(_cli("--project", path, "--json", "queue", "list"))

    @app.get("/api/ui/readiness")
    def ui_readiness() -> dict:
        """配音链是否可开工：任一已启用 provider 可用才算就绪。

        模型缺失时不静默降级，直接把原因回给界面，由界面禁用「开始」。
        """
        result = _cli("--json", "tts", "providers")
        if result.code != 0:
            return {"ready": False, "reason": f"读取 provider 状态失败：{result.message}", "providers": []}
        rows = (result.payload or {}).get("providers") or []
        usable = [row for row in rows if row.get("enabled") and row.get("available")]
        if usable:
            return {"ready": True, "reason": "", "providers": rows}
        blocked = [row for row in rows if row.get("enabled")]
        if blocked:
            reason = "；".join(
                f"{row['provider_id']} 不可用（{row.get('code') or '无原因'}：{row.get('detail')}）" for row in blocked
            )
        else:
            reason = "配置里没有启用的 TTS provider（检查 config 的 tts.providers）"
        return {"ready": False, "reason": f"{reason}。先运行 ndw models list / verify 确认权重就位", "providers": rows}

    @app.post("/api/ui/generate")
    def ui_generate(body: dict) -> dict:
        """入队并启动后台消费进程；请求立即返回，进度一律靠 `queue list` 反映。

        消费放在独立进程里（`tts run --wait`），避免阻塞请求线程，
        也避免在请求线程里重定向 stdout 与其它请求相互干扰。
        """
        path = body["path"]
        chapter = body["chapter"]
        # 前置检查：权重缺失/不可用时直接拒绝，不静默降级，也不启动一个必然失败的后台进程
        ready = ui_readiness()
        if not ready["ready"]:
            raise HTTPException(status_code=412, detail={"code": 4, "message": ready["reason"]})
        jobs = _respond(_cli("--project", path, "--json", "queue", "list")) or []
        active = [job for job in jobs if job.get("status") in ("queued", "running")]
        if active:
            # 接管已在执行的一批：since 取这批任务里最早的创建时间，避免把历史任务算进本次进度
            since = min((job.get("created_at") or "") for job in active) or now_iso()
            return {"started": False, "active": len(active), "since": since}

        log_dir = ProjectStore(path).root / ".ndw" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        since = now_iso()
        log_path = log_dir / f"web-tts-{since.replace(':', '').replace('-', '')}.log"
        src_dir = Path(__file__).resolve().parents[2]
        env = dict(os.environ)
        env["PYTHONPATH"] = str(src_dir) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        argv = [sys.executable, "-m", "novel_drama.cli", "--project", path]
        if config_path:
            argv += ["--config", config_path]
        argv += ["tts", "run", "--chapter", chapter, "--wait"]
        with log_path.open("wb") as log:
            process = subprocess.Popen(
                argv,
                cwd=str(src_dir),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                env=env,
                start_new_session=True,
            )
        return {"started": True, "pid": process.pid, "log": str(log_path), "since": since}

    @app.post("/api/ui/line/set")
    def ui_line_set(body: dict) -> dict:
        """改行参数（`line set`）；只传实际改动的字段。"""
        argv = ["--project", body["path"], "line", "set", "--chapter", body["chapter"], "--line", body["line"]]
        mapping = {
            "speaker": "--speaker",
            "kind": "--kind",
            "emotion": "--emotion",
            "emotion_intensity": "--emotion-intensity",
            "speed": "--speed",
            "pause_before_ms": "--pause-before",
            "pause_after_ms": "--pause-after",
            "text": "--text",
            "delivery_note": "--delivery-note",
        }
        for key, flag in mapping.items():
            if body.get(key) is not None:
                argv += [flag, str(body[key])]
        return _respond(_cli(*argv))

    @app.post("/api/ui/line/revert")
    def ui_line_revert(body: dict) -> dict:
        """回滚到上次生成时的参数（`line revert`）。"""
        return _respond(
            _cli("--project", body["path"], "--json", "line", "revert", "--chapter", body["chapter"], "--line", body["line"])
        )

    if STATIC_DIR.exists():
        from fastapi.responses import FileResponse
        from fastapi.staticfiles import StaticFiles

        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

        @app.get("/")
        def index() -> FileResponse:
            """台词校对页。"""
            return FileResponse(STATIC_DIR / "index.html")

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
