"""续写页接口（R19）：章节末尾续写（预览 / 应用 / 重抽）与大纲追加章节。

业务规则全部在 `writing.continuation`，这里只做 HTTP 包装：
- 预览（不传 apply）会生成并把结果存为工程内 `.ndw/drafts/` 的未应用草稿；
- 应用（apply=true）优先消费该草稿，不重复调用模型；`retry=true` 才重抽覆盖草稿；
- 大纲续写预览不写盘，`apply=true` 逐幕写入设定库（仍需 outline apply 才建章节文件）。
"""

from __future__ import annotations

from fastapi import HTTPException

from ..adapters.base import AdapterError
from ..config import load_config
from ..store import ProjectStore
from ..writing.continuation import continue_chapter, continue_outline, load_continue_draft


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


def register_continue_routes(app, config_path: str | None = None) -> None:
    """挂载续写相关接口。

    参数:
        app: FastAPI 应用。
        config_path: `serve --config` 传入的配置文件路径。

    返回:
        无。
    """

    @app.post("/api/ui/chapter/continue")
    def ui_chapter_continue(body: dict) -> dict:
        """章节正文续写：预览存草稿 / 应用草稿 / 重抽。

        参数:
            body: `{path, chapter, words?, intent?, from_beat?, apply?, retry?}`。

        返回:
            `continue_chapter` 的结果（含 added_words / preview / draft_path / error 等）。

        异常:
            HTTPException 409: 正文为空、接近上限或草稿失效。
            HTTPException 502: 模型调用失败且没有可写入内容。
        """
        store = _store(body.get("path"))
        chapter_id = body.get("chapter")
        if not chapter_id:
            raise HTTPException(status_code=400, detail="缺少 chapter 参数（章节 ID）")
        config = load_config(config_path)
        try:
            result = continue_chapter(
                store,
                config,
                chapter_id,
                words=body.get("words"),
                intent=body.get("intent"),
                from_beat=body.get("from_beat"),
                dry_run=not bool(body.get("apply")),
                retry=bool(body.get("retry")),
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except AdapterError as exc:
            raise HTTPException(status_code=502, detail=f"续写失败：{exc.message}") from exc
        if result.get("error") and not result.get("added_words"):
            raise HTTPException(status_code=502, detail=result["error"])
        return result

    @app.get("/api/ui/chapter/continue/draft")
    def ui_chapter_continue_draft(path: str, chapter: str) -> dict:
        """查看某章是否留有未应用续写草稿（刷新页面后仍能看到草稿提示）。

        参数:
            path: 工程根目录。
            chapter: 章节 ID。

        返回:
            `{exists, draft}`；没有草稿时 draft 为 null。
        """
        store = _store(path)
        draft = load_continue_draft(store, chapter)
        return {"exists": bool(draft), "draft": draft}

    @app.post("/api/ui/outline/continue")
    def ui_outline_continue(body: dict) -> dict:
        """大纲续写：在已有章节之后追加新章（跨幕自动建幕）。

        参数:
            body: `{path, chapters?, acts?, chapters_per_act?, beats_per_chapter?, words?,
                brief?, intent?, apply?}`；intent 为本次续写的明确需求（最高优先级，可留空）。

        返回:
            `continue_outline` 的结果（含 new_chapters / start_index / intent / error 等）。

        异常:
            HTTPException 409: 章节序号空洞或大纲与章节文件冲突。
            HTTPException 502: 一幕都没生成成功。
        """
        store = _store(body.get("path"))
        config = load_config(config_path)
        try:
            result = continue_outline(
                store,
                config,
                chapters=body.get("chapters"),
                acts=body.get("acts"),
                chapters_per_act=int(body.get("chapters_per_act") or 3),
                beats_per_chapter=int(body.get("beats_per_chapter") or 3),
                chapter_words=int(body.get("words") or 1200),
                brief=body.get("brief"),
                intent=body.get("intent"),
                dry_run=not bool(body.get("apply")),
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except AdapterError as exc:
            raise HTTPException(status_code=502, detail=f"大纲续写失败：{exc.message}") from exc
        if result.get("error") and not result.get("acts_added"):
            raise HTTPException(status_code=502, detail=result["error"])
        return result
