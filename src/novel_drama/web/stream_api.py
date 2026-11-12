"""页面实时流（SSE）：章节正文生成 / 正文续写。

只**新增**流式接口，原有非流式接口保持不变；业务逻辑仍由
`writing.service` / `writing.continuation` 承担，这里只把模型增量
透给浏览器：

- `event: start` 开始；
- `event: delta` 增量文本 `{"text": "..."}`；
- `event: done` 最终结果 `{"result": {...}}`（字数、轮次、预览等）；
- `event: error` 中文原因 `{"message": "..."}`。

生成在后台线程执行，前端断开（AbortController）后会在下一个增量处停止，
并随上游生成器的关闭一起释放 HTTP 连接。
"""

from __future__ import annotations

import json
import queue
import threading
import time

from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from ..adapters.base import AdapterError
from ..config import load_config
from ..store import ProjectStore
from ..writing.character_plan import generate_from_plan, load_plan, plan_characters
from ..writing.continuation import continue_chapter
from ..writing.outline_hier import generate_hierarchical_outline
from ..writing.service import (
    DEFAULT_CHAPTER_WORDS,
    generate_chapter,
    generate_characters,
    generate_outline,
    generate_worlds,
)

#: SSE 响应头：禁用缓存与反向代理缓冲，保证逐条到达浏览器
_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


class _Cancelled(Exception):
    """客户端断开后用于中断生成流程。"""


def _sse(event: str, data: dict) -> str:
    """拼一条 SSE 事件。

    参数:
        event: 事件名（start/delta/done/error）。
        data: 可 JSON 序列化的负载。

    返回:
        `event: ...\\ndata: ...\\n\\n` 字符串。
    """
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _store(path: str | None) -> ProjectStore:
    """定位工程，未初始化时返回 404。

    参数:
        path: 工程根目录。

    返回:
        ProjectStore。

    异常:
        HTTPException 400/404: 缺少 path 或工程不存在。
    """
    if not path:
        raise HTTPException(status_code=400, detail="缺少 path 参数（工程目录）")
    store = ProjectStore(path)
    if not store.exists():
        raise HTTPException(status_code=404, detail=f"工程不存在: {store.root}")
    return store


def _error_message(exc: Exception) -> str:
    """把异常转成可读中文。

    参数:
        exc: 捕获到的异常。

    返回:
        AdapterError 取 message，其它取 str 或类名。
    """
    if isinstance(exc, AdapterError):
        return exc.message
    return str(exc) or exc.__class__.__name__


def _stream_response(task) -> StreamingResponse:
    """在后台线程执行生成任务，把增量与最终结果转成 SSE。

    参数:
        task: `task(on_delta) -> dict`，内部应把模型增量交给 on_delta。

    返回:
        StreamingResponse（text/event-stream）。
    """
    events: queue.Queue = queue.Queue()
    stop = threading.Event()

    def on_delta(text: str) -> None:
        """接收模型增量；客户端已断开时抛异常中断生成。"""
        if stop.is_set():
            raise _Cancelled()
        events.put(("delta", {"text": text}))

    def worker() -> None:
        """后台生成线程。"""
        try:
            result = task(on_delta)
            events.put(("done", {"result": result}))
        except _Cancelled:
            pass
        except AdapterError as exc:
            events.put(("error", {"message": f"生成失败：{exc.message}"}))
        except Exception as exc:  # 其它异常统一转成中文可读错误
            events.put(("error", {"message": f"生成失败：{_error_message(exc)}"}))
        finally:
            events.put(None)

    thread = threading.Thread(target=worker, name="ndw-sse", daemon=True)
    thread.start()

    def events_iter():
        """按队列顺序产出 SSE 事件。"""
        try:
            yield _sse("start", {"ok": True})
            while True:
                item = events.get()
                if item is None:
                    return
                event, data = item
                yield _sse(event, data)
        finally:
            # 浏览器断开或正常结束：通知后台在下一个增量处停止
            stop.set()

    return StreamingResponse(events_iter(), media_type="text/event-stream", headers=_SSE_HEADERS)


def _progress_response(task) -> StreamingResponse:
    """在后台线程执行结构化生成，把 batch/chars 进度与最终结果转成 SSE。

    与 `_stream_response` 的区别：任务通过 `on_progress(event)` 上报批次与
    已接收字数，页面据此显示「第 k/N 批 · 已接收 X 字」。

    参数:
        task: `task(on_progress) -> dict`。

    返回:
        StreamingResponse（text/event-stream）。
    """
    events: queue.Queue = queue.Queue()
    stop = threading.Event()
    throttle = {"chars": 0, "at": 0.0}

    def on_progress(event: dict) -> None:
        """接收进度事件；chars 做轻量合并，避免刷屏。"""
        if stop.is_set():
            raise _Cancelled()
        if not isinstance(event, dict):
            return
        kind = str(event.get("type") or "progress")
        if kind == "chars":
            chars = int(event.get("chars") or 0)
            now = time.monotonic()
            # 字数增长不足 32 且距上次不足 0.2 秒时丢弃，页面仍能保持 5 次/秒左右
            if chars - throttle["chars"] < 32 and now - throttle["at"] < 0.2:
                return
            throttle["chars"] = chars
            throttle["at"] = now
        elif kind == "batch":
            # 新批次 / 新角色的字数从头累计，重置节流基线，保证首个 chars 事件不被吞
            throttle["chars"] = 0
            throttle["at"] = 0.0
        events.put((kind, event))

    def worker() -> None:
        """后台生成线程。"""
        try:
            result = task(on_progress)
            events.put(("done", {"result": result}))
        except _Cancelled:
            pass
        except AdapterError as exc:
            events.put(("error", {"message": f"生成失败：{exc.message}"}))
        except Exception as exc:  # 其它异常统一转成中文可读错误
            events.put(("error", {"message": f"生成失败：{_error_message(exc)}"}))
        finally:
            events.put(None)

    thread = threading.Thread(target=worker, name="ndw-sse-progress", daemon=True)
    thread.start()

    def events_iter():
        """按队列顺序产出 SSE 事件。"""
        try:
            yield _sse("start", {"ok": True})
            while True:
                item = events.get()
                if item is None:
                    return
                event, data = item
                yield _sse(event, data)
        finally:
            stop.set()

    return StreamingResponse(events_iter(), media_type="text/event-stream", headers=_SSE_HEADERS)


def register_stream_routes(app, config_path: str | None = None) -> None:
    """挂载页面实时流接口。

    参数:
        app: FastAPI 应用。
        config_path: `serve --config` 传入的配置文件路径。

    返回:
        无。
    """

    @app.post("/api/ui/chapter/generate/stream")
    def ui_chapter_generate_stream(body: dict) -> StreamingResponse:
        """章节正文生成（流式）：边生成边把增量推给页面。

        参数:
            body: `{path, chapter, regen?, dry_run?, multi_turn?, segment_words?, max_rounds?}`。

        返回:
            SSE：start → delta* → done(result) / error(message)。
        """
        store = _store(body.get("path"))
        chapter_id = str(body.get("chapter") or "")
        if not chapter_id:
            raise HTTPException(status_code=400, detail="缺少 chapter 参数（章节 ID）")
        config = load_config(config_path)

        def task(on_delta):
            """调用 service 生成正文，增量交给 SSE。"""
            return generate_chapter(
                store,
                config,
                chapter_id,
                regen=bool(body.get("regen")),
                dry_run=bool(body.get("dry_run")),
                multi_turn=body.get("multi_turn"),
                segment_words=body.get("segment_words"),
                max_rounds=body.get("max_rounds"),
                on_delta=on_delta,
            )

        return _stream_response(task)

    @app.post("/api/ui/chapter/continue/stream")
    def ui_chapter_continue_stream(body: dict) -> StreamingResponse:
        """章节正文续写预览（流式）：边生成边推增量，完成后仍存为未应用草稿。

        参数:
            body: `{path, chapter, words?, intent?, from_beat?, apply?, retry?}`。

        返回:
            SSE：start → delta* → done(result) / error(message)。
        """
        store = _store(body.get("path"))
        chapter_id = str(body.get("chapter") or "")
        if not chapter_id:
            raise HTTPException(status_code=400, detail="缺少 chapter 参数（章节 ID）")
        config = load_config(config_path)

        def task(on_delta):
            """调用 continuation 续写，增量交给 SSE。"""
            return continue_chapter(
                store,
                config,
                chapter_id,
                words=body.get("words"),
                intent=body.get("intent"),
                from_beat=body.get("from_beat"),
                dry_run=not bool(body.get("apply")),
                retry=bool(body.get("retry")),
                on_delta=on_delta,
            )

        return _stream_response(task)

    @app.post("/api/ui/characters/generate/stream")
    def ui_characters_generate_stream(body: dict) -> StreamingResponse:
        """人物生成（流式进度）：batch/chars/done，不改动原有批量与合并逻辑。

        参数:
            body: `{path, count?, brief?}`。

        返回:
            SSE：start → batch/chars* → done(result) / error(message)。
        """
        store = _store(body.get("path"))
        config = load_config(config_path)

        def task(on_progress):
            """调用 generate_characters 并透出批次与接收字数。"""
            return generate_characters(
                store,
                config,
                count=body.get("count"),
                brief=body.get("brief"),
                on_progress=on_progress,
            )

        return _progress_response(task)

    @app.post("/api/ui/characters/plan/stream")
    def ui_characters_plan_stream(body: dict) -> StreamingResponse:
        """人物两阶段生成（流式进度）：先规划名单，再逐个生成详情。

        阶段 1 产出名单并落盘到 `<工程>/.ndw/character_plan.json`；
        阶段 2 逐个调用 `emit_character`，单个失败不影响其他角色。

        参数:
            body: `{path, count?, brief?, retry_failed?, resume?}`；
                retry_failed=True 只重试上次失败的角色，resume=True 继续未完成的角色。

        返回:
            SSE：start → plan → (batch/chars)* → done(result) / error(message)。
        """
        store = _store(body.get("path"))
        config = load_config(config_path)
        retry_failed = bool(body.get("retry_failed"))
        resume = bool(body.get("resume"))

        def task(on_progress):
            """规划（或读取已有名单）后逐个生成。"""
            if retry_failed or resume:
                plan = load_plan(store)
                if plan is None:
                    raise ValueError("没有可继续的人物名单：先执行一次批量生成")
                statuses = ("failed",) if retry_failed else ("pending", "failed")
            else:
                plan = plan_characters(store, config, count=body.get("count"), brief=body.get("brief"))
                statuses = ("pending",)
            on_progress({"type": "plan", "total": len(plan.get("items") or []), "basis": plan.get("basis")})
            return generate_from_plan(store, config, plan, statuses=statuses, on_progress=on_progress)

        return _progress_response(task)

    @app.post("/api/ui/worlds/generate/stream")
    def ui_worlds_generate_stream(body: dict) -> StreamingResponse:
        """世界设定生成（流式进度）：batch/chars/done。

        参数:
            body: `{path, count?, brief?, overwrite?}`。

        返回:
            SSE：start → batch/chars* → done(result) / error(message)。
        """
        store = _store(body.get("path"))
        config = load_config(config_path)

        def task(on_progress):
            """调用 generate_worlds 并透出批次与接收字数。"""
            return generate_worlds(
                store,
                config,
                count=body.get("count"),
                overwrite=bool(body.get("overwrite")),
                brief=body.get("brief"),
                on_progress=on_progress,
            )

        return _progress_response(task)

    @app.post("/api/ui/outline/generate/stream")
    def ui_outline_generate_stream(body: dict) -> StreamingResponse:
        """大纲生成（流式进度）：batch/chars/done。

        参数:
            body: `{path, acts?, chapters_per_act?, beats_per_chapter?, words?, binding?,
                multi_turn?, acts_per_round?, dry_run?}`。

        返回:
            SSE：start → batch/chars* → done(result) / error(message)。
        """
        store = _store(body.get("path"))
        config = load_config(config_path)

        def task(on_progress):
            """调用 generate_outline 并透出批次与接收字数。"""
            return generate_outline(
                store,
                config,
                acts=int(body.get("acts") or 3),
                chapters_per_act=int(body.get("chapters_per_act") or 3),
                beats_per_chapter=int(body.get("beats_per_chapter") or 3),
                chapter_words=int(body.get("words") or DEFAULT_CHAPTER_WORDS),
                dry_run=bool(body.get("dry_run")),
                binding=str(body.get("binding") or "auto"),
                multi_turn=bool(body.get("multi_turn")),
                acts_per_round=int(body.get("acts_per_round") or 1),
                on_progress=on_progress,
            )

        return _progress_response(task)

    @app.post("/api/ui/outline/hierarchical/stream")
    def ui_outline_hierarchical_stream(body: dict) -> StreamingResponse:
        """大纲三级分层生成（流式进度）：幕 → 章 → 节拍。

        参数:
            body: `{path, acts?, chapters_per_act?, beats_per_chapter?, words?, binding?,
                dry_run?, resume?}`；resume=true 时只补做失败/缺失的幕与章。

        返回:
            SSE：start → stage/batch/chars* → done(result) / error(message)；
            result.failures 里是单幕/单章的失败项，可再次以 resume=true 重试。
        """
        store = _store(body.get("path"))
        config = load_config(config_path)

        def task(on_progress):
            """调用三级分层生成并透出层级进度。"""
            return generate_hierarchical_outline(
                store,
                config,
                acts=int(body.get("acts") or 3),
                chapters_per_act=int(body.get("chapters_per_act") or 3),
                beats_per_chapter=int(body.get("beats_per_chapter") or 3),
                chapter_words=int(body.get("words") or DEFAULT_CHAPTER_WORDS),
                dry_run=bool(body.get("dry_run")),
                binding=str(body.get("binding") or "auto"),
                resume=bool(body.get("resume")),
                on_progress=on_progress,
            )

        return _progress_response(task)
