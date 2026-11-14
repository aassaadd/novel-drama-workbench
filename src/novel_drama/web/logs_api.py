"""全平台操作日志的 HTTP 层：查询 / 详情 / 统计 / 清空 + 操作日志中间件。

职责边界：
- 记录哪些请求：所有带副作用的接口（POST / PATCH / PUT / DELETE），
  以及它们内部的 CLI 调用与 LLM 调用（由 `oplog` 的钩子按 trace 串起来）；
- 记录哪些字段：时间、工程、动作、目标、参数摘要、结果、耗时、失败原因，
  以及出错时的异常堆栈；
- 不记录：只读 GET 接口与日志接口自身，避免刷屏。

日志写入在 `oplog` 里异步完成，本模块只负责组装记录与提供查询接口。
"""

from __future__ import annotations

import json
import time
import traceback

from fastapi import HTTPException, Query
from starlette.responses import Response

from .. import oplog
from ..config import load_config

#: 需要记录日志的 HTTP 方法（只读 GET 不记）
_MUTATING = {"POST", "PATCH", "PUT", "DELETE"}

#: 写接口 → (机器动作名, 中文动作名)
_ACTIONS: dict[str, tuple[str, str]] = {
    "/api/config": ("config.save", "修改本机配置"),
    "/api/ui/config/connection": ("config.connection", "保存连接配置"),
    "/api/project/delete": ("project.delete", "删除工程"),
    "/api/project/reveal": ("project.reveal", "在访达中打开工程"),
    "/api/ui/project/init": ("project.init", "新建工程"),
    "/api/ui/project/duplicate": ("project.duplicate", "复制工程"),
    "/api/ui/project/pick-dir": ("project.pick", "选择工程目录"),
    "/api/ui/project/work": ("project.work", "保存作品信息"),
    "/api/ui/project/production": ("project.production", "保存制作要求"),
    "/api/ui/projects/reindex": ("project.reindex", "重建工程索引"),
    "/api/ui/chapter/add": ("chapter.add", "新建章节"),
    "/api/ui/chapter/import": ("chapter.import", "导入正文"),
    "/api/ui/chapter/rename": ("chapter.rename", "重命名章节"),
    "/api/ui/chapter/delete": ("chapter.delete", "删除章节"),
    "/api/ui/chapter/mix": ("chapter.mix", "合并章节"),
    "/api/ui/chapter/save": ("chapter.save", "保存章节正文"),
    "/api/ui/chapter/edit": ("chapter.edit", "编辑章节正文"),
    "/api/ui/chapter/generate": ("chapter.generate", "生成章节正文"),
    "/api/ui/chapter/generate/stream": ("chapter.generate", "生成章节正文（流式）"),
    "/api/ui/chapter/continue": ("chapter.continue", "续写章节正文"),
    "/api/ui/chapter/continue/stream": ("chapter.continue", "续写章节正文（流式）"),
    "/api/ui/chapter/summarize": ("chapter.summarize", "反向总结章节"),
    "/api/ui/lines/replace": ("line.replace", "批量替换台词"),
    "/api/ui/line/set": ("line.set", "设置台词行"),
    "/api/ui/line/set-batch": ("line.set_batch", "批量设置台词行"),
    "/api/ui/line/edit": ("line.edit", "编辑台词行"),
    "/api/ui/line/revert": ("line.revert", "回滚台词行"),
    "/api/ui/line/merge": ("line.merge", "合并台词行"),
    "/api/ui/line/merge-narration": ("line.merge_narration", "合并旁白"),
    "/api/ui/line/split": ("line.split", "切分台词"),
    "/api/ui/line/split-at": ("line.split_at", "按位置切分台词"),
    "/api/ui/line/rerun": ("line.rerun", "重跑台词配音"),
    "/api/ui/line/audio/upload": ("line.audio_upload", "上传播音"),
    "/api/ui/line/audio/delete": ("line.audio_delete", "删除录音"),
    "/api/ui/line/audio/activate": ("line.audio_activate", "切换录音版本"),
    "/api/ui/line/audio/version/delete": ("line.audio_version_delete", "删除录音版本"),
    "/api/ui/scene/split": ("scene.split", "切分场景"),
    "/api/ui/script/suggest": ("script.suggest", "生成导演标注"),
    "/api/ui/timeline/save": ("timeline.save", "保存时间轴"),
    "/api/ui/timeline/build": ("timeline.build", "构建时间轴"),
    "/api/ui/setting/set": ("setting.set", "修改写作设定"),
    "/api/ui/setting/world": ("setting.world", "修改世界观"),
    "/api/ui/setting/generate": ("setting.generate", "生成写作设定"),
    "/api/ui/setting/apply": ("setting.apply", "应用写作设定"),
    "/api/ui/setting/polish": ("setting.polish", "润色写作设定"),
    "/api/ui/outline/generate": ("outline.generate", "生成大纲"),
    "/api/ui/outline/generate/stream": ("outline.generate", "生成大纲（流式）"),
    "/api/ui/outline/hierarchical/stream": ("outline.generate_hier", "分层生成大纲（流式）"),
    "/api/ui/outline/apply": ("outline.apply", "应用大纲"),
    "/api/ui/outline/bind": ("outline.bind", "绑定大纲"),
    "/api/ui/outline/text": ("outline.text", "保存大纲文本"),
    "/api/ui/outline/act/delete": ("outline.act_delete", "删除幕"),
    "/api/ui/outline/beat/delete": ("outline.beat_delete", "删除节拍"),
    "/api/ui/outline/chapter/delete": ("outline.chapter_delete", "删除大纲章节"),
    "/api/ui/outline/continue": ("outline.continue", "续写大纲"),
    "/api/ui/cast": ("cast.save", "保存台词角色"),
    "/api/ui/cast/chapter": ("cast.chapter", "保存本章角色"),
    "/api/ui/cast/autofill": ("cast.autofill", "自动填充角色"),
    "/api/ui/cast/autofill/undo": ("cast.autofill_undo", "撤销自动填充"),
    "/api/ui/cast/backfill": ("cast.backfill", "回填角色"),
    "/api/ui/cast/fill/start": ("cast.fill_start", "补齐缺少的角色"),
    "/api/ui/cast/fill/resume": ("cast.fill_resume", "继续补齐角色"),
    "/api/ui/cast/fill/stop": ("cast.fill_stop", "停止补齐角色"),
    "/api/ui/cast/preview": ("cast.preview", "试听角色音色"),
    "/api/ui/cast/preview/clear": ("cast.preview_clear", "清理试听缓存"),
    "/api/ui/characters": ("character.save", "保存角色"),
    "/api/ui/characters/generate": ("character.generate", "生成角色"),
    "/api/ui/characters/generate/stream": ("character.generate", "生成角色（流式）"),
    "/api/ui/characters/image": ("character.image", "生成角色立绘"),
    "/api/ui/characters/delete": ("character.delete", "删除角色"),
    "/api/ui/characters/descriptions/fill": ("character.desc_fill", "补全角色描述"),
    "/api/ui/characters/descriptions/fill/stream": ("character.desc_fill", "补全角色描述（流式）"),
    "/api/ui/characters/plan/stream": ("character.plan", "规划角色"),
    "/api/ui/worlds": ("world.save", "保存世界模块"),
    "/api/ui/worlds/generate": ("world.generate", "生成世界条目"),
    "/api/ui/worlds/generate/stream": ("world.generate", "生成世界条目（流式）"),
    "/api/ui/worlds/image": ("world.image", "生成世界封面"),
    "/api/ui/worlds/delete": ("world.delete", "删除世界"),
    "/api/ui/voices": ("voice.save", "保存音色"),
    "/api/ui/voices/bind": ("voice.bind", "绑定音色"),
    "/api/ui/voices/delete": ("voice.delete", "删除音色"),
    "/api/ui/voices/builtin/install": ("voice.builtin_install", "安装内置音色"),
    "/api/ui/voices/builtin/import": ("voice.builtin_import", "导入内置音色"),
    "/api/ui/voices/design/preview": ("voice.design_preview", "试听设计音色"),
    "/api/ui/voices/design/batch": ("voice.design_batch", "批量设计音色"),
    "/api/ui/voices/design/batches": ("voice.design_batch", "批量设计音色"),
    "/api/ui/voices/design/select": ("voice.design_select", "选定设计音色"),
    "/api/ui/voices/casting/preview": ("voice.casting_preview", "试听选角"),
    "/api/ui/voices/casting/apply": ("voice.casting_apply", "应用选角"),
    "/api/ui/voices/casting/bind": ("voice.casting_bind", "绑定选角"),
    "/api/ui/voices/undo/last": ("voice.undo", "撤销音色操作"),
    "/api/ui/tts/batch-all": ("tts.batch", "整批配音"),
    "/api/ui/tts/pause": ("tts.pause", "暂停 / 继续配音"),
    "/api/ui/align/upload": ("align.upload", "上传智能对轨音频"),
    "/api/ui/align/tasks/delete": ("align.delete", "删除对轨任务"),
    "/api/ui/bgm/generate": ("bgm.generate", "生成配乐"),
    "/api/ui/sfx/generate": ("sfx.generate", "生成音效"),
    "/api/ui/media/run": ("media.run", "生成素材"),
    "/api/ui/export/jianying": ("export.jianying", "导出剪映草稿"),
    "/api/ui/export/bundle": ("export.bundle", "导出成片包"),
    "/api/ui/produce": ("produce.start", "启动全链路编排"),
    "/api/ui/produce/cancel": ("produce.cancel", "取消编排"),
    "/api/ui/generate": ("generate.start", "启动生成任务"),
    "/api/ui/stop": ("generate.stop", "停止生成任务"),
    "/api/ui/jobs/clear": ("job.clear", "清理任务记录"),
    "/api/ui/jobs/cancel": ("job.cancel", "停止任务"),
    "/api/ui/gpu/lock/release": ("gpu.release", "释放 GPU 锁"),
    "/api/ui/models/install": ("model.install", "下载模型"),
    "/api/ui/models/manual": ("model.manual", "补录模型来源"),
    "/api/ui/models/resolve": ("model.resolve", "反查模型来源"),
    "/api/ui/settings/models": ("settings.models", "保存模型设置"),
    "/api/ui/settings/model-limits": ("settings.model_limits", "保存模型上限"),
    "/api/ui/llm/status": ("llm.status", "检测写作模型"),
    "/api/ui/comfy/health": ("comfy.health", "检测 ComfyUI"),
}


def _describe(method: str, path: str) -> tuple[str, str]:
    """给出接口对应的动作名。

    参数:
        method: HTTP 方法。
        path: 接口路径。

    返回:
        `(机器动作名, 中文动作名)`；未登记时用路径兜底。
    """
    hit = _ACTIONS.get(path)
    if hit:
        return hit
    # 批量子路径（如 /api/ui/voices/design/batches/{id}/cancel）按前缀兜底
    for prefix in sorted(_ACTIONS, key=len, reverse=True):
        if path.startswith(prefix + "/"):
            action, label = _ACTIONS[prefix]
            tail = path[len(prefix) + 1 :].replace("/", " ")
            return action, f"{label}（{tail}）"
    return f"http.{method.lower()}", f"{method} {path}"


def _clip(value: str, limit: int) -> str:
    """截断长文本并注明省略长度。

    参数:
        value: 原文。
        limit: 最大字符数。

    返回:
        截断后的文本。
    """
    text = "" if value is None else str(value)
    if len(text) <= limit:
        return text
    return text[:limit] + f"…（已省略 {len(text) - limit} 字）"


def _clean_params(body: dict, query: dict) -> dict:
    """整理请求参数：去掉路径参数、长文本只留摘要。

    参数:
        body: 请求体 JSON。
        query: 查询参数。

    返回:
        可落盘的参数 dict。
    """
    params: dict = {}
    for key, value in (query or {}).items():
        if key == "path":
            continue
        params[f"query.{key}"] = _clip(str(value), 600)
    for key, value in (body or {}).items():
        if key == "path":
            continue
        if isinstance(value, str):
            params[key] = _clip(value, 3000)
        elif isinstance(value, list):
            params[key] = value[:60] if len(value) > 60 else value
        else:
            params[key] = value
    return params


def _target_of(body: dict) -> str:
    """从请求体里提取目标对象（章节 / 角色 / 音色 / 台词行等）。

    参数:
        body: 请求体 JSON。

    返回:
        目标描述文本（取不到时为空串）。
    """
    if not isinstance(body, dict):
        return ""
    pairs = (
        ("chapter_title", "章节"),
        ("chapter_id", "章节"),
        ("chapter", "章节"),
        ("character_name", "角色"),
        ("character_id", "角色"),
        ("voice_name", "音色"),
        ("voice_id", "音色"),
        ("world_id", "世界"),
        ("line_id", "台词行"),
        ("scene_id", "场景"),
        ("chapter_ref", "章节"),
        ("name", ""),
        ("title", ""),
        ("id", ""),
    )
    for key, prefix in pairs:
        value = body.get(key)
        if value in (None, "", [], {}):
            continue
        text = str(value)
        return f"{prefix} {text}".strip()
    return ""


def _summary_of(body: dict) -> str:
    """生成关键参数摘要（字数 / 条数 / 参数字段名）。

    参数:
        body: 请求体 JSON。

    返回:
        摘要文本。
    """
    if not isinstance(body, dict):
        return ""
    parts: list[str] = []
    for key in ("text", "content", "prompt"):
        value = body.get(key)
        if isinstance(value, str) and value:
            parts.append(f"{key} {len(value)} 字")
    for key in ("ids", "items", "lines", "chapters", "characters"):
        value = body.get(key)
        if isinstance(value, list) and value:
            parts.append(f"{key} {len(value)} 项")
    flags = [key for key in ("force", "overwrite", "dry_run", "yes") if body.get(key)]
    if flags:
        parts.append("选项 " + "/".join(flags))
    keys = [str(key) for key in body if key != "path" and not isinstance(body.get(key), (dict, list))]
    if keys:
        parts.append("字段 " + "、".join(keys[:8]) + ("…" if len(keys) > 8 else ""))
    return " · ".join(parts)


def _response_error(raw: bytes) -> str:
    """从 4xx / 5xx 响应体里取中文错误说明。

    参数:
        raw: 响应体字节。

    返回:
        错误文本；取不到时为空串。
    """
    if not raw:
        return ""
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _clip(raw.decode("utf-8", errors="replace"), 2000)
    detail = payload.get("detail") if isinstance(payload, dict) else payload
    if isinstance(detail, dict):
        return _clip(str(detail.get("message") or detail), 2000)
    if isinstance(detail, list):
        return _clip("；".join(str(item) for item in detail[:5]), 2000)
    return _clip(str(detail or payload), 2000)


def register_logs_routes(app, config_path: str | None = None) -> None:
    """注册日志接口与操作日志中间件。

    参数:
        app: FastAPI 实例。
        config_path: 本机配置文件路径（与 serve 同源，用于读取 logs.* 配置）。

    返回:
        无。
    """
    config = None
    try:
        config = load_config(config_path)
    except Exception:  # noqa: BLE001 - 配置异常不阻断服务启动
        config = None
    oplog.configure(config)
    oplog.install_llm_hooks()

    @app.middleware("http")
    async def _oplog_middleware(request, call_next):
        """记录所有写接口的操作日志；任何记录失败都不得影响业务响应。"""
        path = request.url.path
        method = request.method.upper()
        if method not in _MUTATING or not path.startswith("/api/") or path.startswith("/api/logs"):
            return await call_next(request)

        body: dict = {}
        content_type = request.headers.get("content-type", "")
        try:
            # 只读 JSON 请求体：文件上传等 multipart 不整体缓冲，避免额外内存占用
            if "json" in content_type.lower():
                raw = await request.body()
                if raw:
                    parsed = json.loads(raw.decode("utf-8"))
                    body = parsed if isinstance(parsed, dict) else {"payload": parsed}
        except Exception:  # noqa: BLE001 - 请求体不是 JSON 时按空处理
            body = {}

        action, label = _describe(method, path)
        project = str(
            body.get("path") or body.get("project") or body.get("dir") or request.query_params.get("path") or ""
        )
        trace_id = request.headers.get("X-Ndw-Trace") or oplog.new_trace_id()
        started = time.monotonic()

        with oplog.bind_request(trace_id, project, label):
            try:
                response = await call_next(request)
            except BaseException as exc:  # noqa: BLE001 - 记录后原样抛出
                oplog.record_op(
                    action=action,
                    label=label,
                    target=_target_of(body),
                    summary=_summary_of(body),
                    status="error",
                    duration_ms=int((time.monotonic() - started) * 1000),
                    project=project,
                    error=str(exc),
                    detail={
                        "http": {"method": method, "path": path, "status": 500, "content_type": content_type},
                        "params": oplog.redact(_clean_params(body, dict(request.query_params))),
                        "steps": oplog.collect_steps(),
                        "error": {"message": str(exc), "traceback": traceback.format_exc()},
                    },
                    trace_id=trace_id,
                )
                raise
            duration_ms = int((time.monotonic() - started) * 1000)
            status_code = int(getattr(response, "status_code", 200))
            error_text = ""
            try:
                # BaseHTTPMiddleware 返回的是流式包装，没有 .body；失败响应体积很小，
                # 这里读出后原样重建，保证错误原因能落进日志，不影响正常响应。
                raw_body = b""
                if status_code >= 400 and not hasattr(response, "body"):
                    chunks: list[bytes] = []
                    async for chunk in response.body_iterator:
                        chunks.append(chunk if isinstance(chunk, bytes) else str(chunk).encode("utf-8"))
                    raw_body = b"".join(chunks)
                    response = Response(
                        content=raw_body,
                        status_code=status_code,
                        headers=dict(response.headers),
                        media_type=getattr(response, "media_type", None),
                    )
                elif hasattr(response, "body"):
                    raw_body = bytes(response.body or b"")
                error_text = _response_error(raw_body)
            except Exception:  # noqa: BLE001 - 取错误文本失败不影响响应
                error_text = ""
            oplog.record_op(
                action=action,
                label=label,
                target=_target_of(body),
                summary=_summary_of(body),
                status="ok" if status_code < 400 else "error",
                duration_ms=duration_ms,
                project=project,
                error=error_text,
                detail={
                    "http": {"method": method, "path": path, "status": status_code, "content_type": content_type},
                    "params": oplog.redact(_clean_params(body, dict(request.query_params))),
                    "steps": oplog.collect_steps(),
                },
                trace_id=trace_id,
            )
            try:
                response.headers["X-Ndw-Trace"] = trace_id
            except Exception:  # noqa: BLE001
                pass
            return response

    @app.get("/api/logs")
    def list_logs(
        kind: str = Query("", description="op / llm / 空=全部"),
        project: str = Query("", description="工程路径"),
        q: str = Query("", description="关键字"),
        since: str = Query("", description="起始时间（ISO 前缀）"),
        until: str = Query("", description="结束时间（ISO 前缀）"),
        trace: str = Query("", description="trace ID"),
        limit: int = Query(100, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ) -> dict:
        """查询日志列表（最新在前，返回浅层字段）。"""
        return oplog.query(
            kind=(kind or "").strip(),
            project=(project or "").strip(),
            keyword=(q or "").strip(),
            since=(since or "").strip(),
            until=(until or "").strip(),
            trace=(trace or "").strip(),
            limit=limit,
            offset=offset,
        )

    @app.get("/api/logs/record")
    def read_log_record(
        file: str = Query(..., description="分卷文件名"),
        line: int = Query(..., ge=0, description="行号（0 基）"),
    ) -> dict:
        """按「分卷 + 行号」读取一条完整日志（含完整提示词与返回文本）。"""
        record = oplog.get_record(file, line)
        if record is None:
            raise HTTPException(status_code=404, detail="日志记录不存在（可能已被滚动清理）")
        return record

    @app.get("/api/logs/stats")
    def log_stats() -> dict:
        """日志目录占用、分卷列表与磁盘上限。"""
        return oplog.stats()

    @app.get("/api/logs/facets")
    def log_facets(days: int = Query(7, ge=1, le=90)) -> dict:
        """最近若干天出现过的工程（筛选下拉用）。"""
        return oplog.facets(days)

    @app.post("/api/logs/clear")
    def clear_logs() -> dict:
        """清空全部日志（用户主动操作）。"""
        removed = oplog.clear_logs()
        oplog.record_op(
            action="log.clear",
            label="清空日志",
            summary=f"删除 {removed.get('removed', 0)} 个文件",
            status="ok",
        )
        return removed
