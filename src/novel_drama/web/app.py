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

#: 设定库可在界面上编辑的标量字段（与 `setting set` 的参数一一对应）
SETTING_SCALARS = (
    "logline",
    "theme",
    "core_conflict",
    "ending_intent",
    "target_audience",
    "target_length",
    "era",
    "tone",
    "pov",
    "tense",
    "prose_style",
    "visual_style",
)


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

    # ---------------- 全链路工作台（R14）----------------
    # 与语音页同一套纪律：界面不重算业务，全部转调 CLI --json。

    @app.get("/api/ui/setting")
    def ui_setting(path: str = Query(...)):
        """设定库全文（`setting show --json`）。"""
        return _respond(_cli("--project", path, "--json", "setting", "show"))

    @app.post("/api/ui/setting/set")
    def ui_setting_set(body: dict) -> dict:
        """修改设定库标量字段（`setting set`）。"""
        argv = ["--project", body["path"], "--json", "setting", "set"]
        fields = body.get("fields") or {}
        for key in SETTING_SCALARS:
            value = fields.get(key)
            if value is None or value == "":
                continue
            argv += ["--" + key.replace("_", "-"), str(value)]
        if len(argv) == 5:
            raise HTTPException(status_code=400, detail={"code": 2, "message": "没有要修改的字段"})
        return _respond(_cli(*argv))

    @app.post("/api/ui/setting/world")
    def ui_setting_world(body: dict) -> dict:
        """增补世界设定（`setting world`）。"""
        argv = ["--project", body["path"], "--json", "setting", "world"]
        for rule in body.get("rules") or []:
            if rule:
                argv += ["--add-rule", rule]
        for faction in body.get("factions") or []:
            if faction:
                argv += ["--add-faction", faction]
        if body.get("location"):
            argv += ["--add-location", body["location"]]
        if body.get("glossary"):
            argv += ["--add-glossary", body["glossary"]]
        return _respond(_cli(*argv))

    @app.get("/api/ui/outline")
    def ui_outline(path: str = Query(...)):
        """大纲（`outline show --json`）。"""
        return _respond(_cli("--project", path, "--json", "outline", "show"))

    @app.post("/api/ui/outline/generate")
    def ui_outline_generate(body: dict) -> dict:
        """生成大纲（`outline generate`）。"""
        argv = ["--project", body["path"], "--json", "outline", "generate"]
        for key, flag in (("acts", "--acts"), ("chapters_per_act", "--chapters-per-act"), ("beats_per_chapter", "--beats-per-chapter"), ("words", "--words")):
            if body.get(key) is not None:
                argv += [flag, str(body[key])]
        if body.get("dry_run"):
            argv.append("--dry-run")
        return _respond(_cli(*argv))

    @app.post("/api/ui/outline/apply")
    def ui_outline_apply(body: dict) -> dict:
        """大纲落地成章节（`outline apply`）。"""
        argv = ["--project", body["path"], "--json", "outline", "apply"]
        if body.get("dry_run"):
            argv.append("--dry-run")
        return _respond(_cli(*argv))

    @app.post("/api/ui/chapter/generate")
    def ui_chapter_generate(body: dict) -> dict:
        """生成章节正文（`chapter generate`）。"""
        argv = ["--project", body["path"], "--json", "chapter", "generate", "--chapter", body["chapter"]]
        if body.get("regen"):
            argv.append("--regen")
        if body.get("dry_run"):
            argv.append("--dry-run")
        return _respond(_cli(*argv))

    @app.post("/api/ui/scene/split")
    def ui_scene_split(body: dict) -> dict:
        """切分场景（`scene split`）。"""
        argv = ["--project", body["path"], "--json", "scene", "split", "--chapter", body["chapter"]]
        if body.get("force"):
            argv.append("--force")
        return _respond(_cli(*argv))

    @app.post("/api/ui/line/split")
    def ui_line_split(body: dict) -> dict:
        """切分台词（`line split`）。"""
        argv = ["--project", body["path"], "--json", "line", "split", "--chapter", body["chapter"]]
        if body.get("force"):
            argv.append("--force")
        return _respond(_cli(*argv))

    @app.get("/api/ui/script")
    def ui_script(path: str = Query(...), chapter: str = Query(...)) -> dict:
        """导演标注文本与脚本文件状态（读落盘产物，不重算）。"""
        store = _store(path)
        markup_path = store.root / "scripts" / f"{chapter}.markup.txt"
        script_path = store.script_path(chapter)
        return {
            "markup_path": str(markup_path),
            "markup": markup_path.read_text(encoding="utf-8") if markup_path.exists() else "",
            "script_path": str(script_path),
            "has_script": script_path.exists(),
        }

    @app.post("/api/ui/script/suggest")
    def ui_script_suggest(body: dict) -> dict:
        """生成标注建议（`script suggest`），可同时应用。"""
        argv = ["--project", body["path"], "--json", "script", "suggest", "--chapter", body["chapter"]]
        if body.get("apply"):
            argv.append("--apply")
        if body.get("dry_run"):
            argv.append("--dry-run")
        return _respond(_cli(*argv))

    @app.get("/api/ui/media")
    def ui_media(path: str = Query(...), chapter: str = Query(...)) -> dict:
        """章节内三类媒体条目（从场景内嵌数据汇总，状态以 asset 为准）。"""
        chapter_data = _store(path).load_chapter(chapter)
        rows = {"sfx": [], "image": [], "video": []}
        keys = {"sfx": "sfx_events", "image": "key_images", "video": "video_segments"}
        for scene in chapter_data.get("scenes") or []:
            for kind, key in keys.items():
                for item in scene.get(key) or []:
                    asset = item.get("asset") or {}
                    rows[kind].append(
                        {
                            "id": item.get("id"),
                            "scene_id": scene.get("id"),
                            "index": item.get("index"),
                            "prompt": item.get("prompt") or "",
                            "kind": item.get("kind"),
                            "mode": item.get("mode"),
                            "duration_ms": item.get("duration_ms") or asset.get("actual_duration_ms"),
                            "status": asset.get("status") or item.get("status") or "pending",
                            "asset_id": asset.get("asset_id"),
                            "error": asset.get("error"),
                        }
                    )
        return rows

    @app.post("/api/ui/media/run")
    def ui_media_run(body: dict) -> dict:
        """执行一类媒体生成（`media sfx|image|video run`）。"""
        kind = body["kind"]
        if kind not in ("sfx", "image", "video"):
            raise HTTPException(status_code=400, detail={"code": 2, "message": f"未知媒体类型: {kind}"})
        argv = ["--project", body["path"], "--json", kind, "run", "--chapter", body["chapter"]]
        if body.get("force"):
            argv.append("--force")
        if body.get("limit") is not None:
            argv += ["--limit", str(body["limit"])]
        return _respond(_cli(*argv))

    @app.post("/api/ui/produce")
    def ui_produce(body: dict) -> dict:
        """启动全链路编排（后台进程，进度靠 `run show` 反映）。"""
        path, chapter = body["path"], body["chapter"]
        argv = ["--project", path]
        if config_path:
            argv += ["--config", config_path]
        argv += ["produce", "--chapter", chapter]
        if body.get("from"):
            argv += ["--from", body["from"]]
        if body.get("to"):
            argv += ["--to", body["to"]]
        if body.get("force"):
            argv.append("--force")
        if body.get("keep_going"):
            argv.append("--keep-going")
        if body.get("no_resume"):
            argv.append("--no-resume")

        log_dir = ProjectStore(path).root / ".ndw" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"web-produce-{now_iso().replace(':', '').replace('-', '')}.log"
        src_dir = Path(__file__).resolve().parents[2]
        env = dict(os.environ)
        env["PYTHONPATH"] = str(src_dir) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        with log_path.open("wb") as log:
            process = subprocess.Popen(
                [sys.executable, "-u", "-m", "novel_drama.cli", *argv],
                cwd=str(src_dir),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                env=env,
                start_new_session=True,
            )
        return {"started": True, "pid": process.pid, "log": str(log_path)}

    @app.get("/api/ui/produce/latest")
    def ui_produce_latest(path: str = Query(...), run_id: str | None = Query(default=None)) -> dict:
        """最近一次（或指定）编排记录（`run show --json`）。"""
        argv = ["--project", path, "--json", "run", "show"]
        if run_id:
            argv += ["--run", run_id]
        result = _cli(*argv)
        if result.code != 0:
            return {"run": None, "message": result.message}
        return {"run": result.payload}

    @app.get("/api/ui/exports")
    def ui_exports(path: str = Query(...)) -> dict:
        """列成片产物：`renders/` 与 `exports/` 下的文件。"""
        from datetime import datetime

        root = _store(path).root
        files = []
        for sub in ("renders", "exports"):
            folder = root / sub
            if not folder.exists():
                continue
            for item in sorted(folder.rglob("*")):
                if not item.is_file():
                    continue
                stat = item.stat()
                files.append(
                    {
                        "rel": str(item.relative_to(root)),
                        "path": str(item),
                        "size": stat.st_size,
                        "mtime": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                    }
                )
        return {"root": str(root), "files": files}

    @app.post("/api/ui/project/init")
    def ui_project_init(body: dict) -> dict:
        """新建工程（`init`），返回可直接用于后续请求的绝对路径。"""
        argv = ["--json", "init", body["dir"], "--title", body["title"]]
        if body.get("author"):
            argv += ["--author", body["author"]]
        for genre in body.get("genres") or []:
            if genre:
                argv += ["--genre", genre]
        result = _cli(*argv)
        if result.code != 0:
            raise HTTPException(status_code=result.status, detail=fail_body(result))
        payload = result.payload if isinstance(result.payload, dict) else {}
        payload["root"] = str(ProjectStore(body["dir"]).root)
        return payload

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
