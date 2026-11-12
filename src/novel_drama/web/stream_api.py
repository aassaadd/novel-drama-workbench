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

from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from ..adapters.base import AdapterError
from ..config import load_config
from ..store import ProjectStore
from ..writing.continuation import continue_chapter
from ..writing.service import generate_chapter

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
