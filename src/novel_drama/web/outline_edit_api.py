"""大纲编辑接口：删除节拍 / 删除整章（含已落地章节的影响预览）。

删除整章分两步：先 `dry_run=true` 取影响（正文字数、台词、音频/图片/视频条目数），
页面确认后再正式删除；删节拍直接改大纲，不动正文。
"""

from __future__ import annotations

from fastapi import HTTPException

from ..store import ProjectStore
from ..writing.outline_edit import (
    act_delete_impact,
    chapter_delete_impact,
    delete_beat,
    delete_outline_act,
    delete_outline_chapter,
)


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


def register_outline_edit_routes(app) -> None:
    """挂载大纲编辑接口。

    参数:
        app: FastAPI 应用。

    返回:
        无。
    """

    @app.post("/api/ui/outline/beat/delete")
    def ui_outline_beat_delete(body: dict) -> dict:
        """删除大纲里的一个节拍（只改大纲数据，不改正文）。

        参数:
            body: `{path, chapter, beat}`；beat 为节拍序号（从 1 开始）。

        返回:
            `{chapter_id, index, title, beat_index, removed, remaining_beats, landed, warnings}`。

        异常:
            HTTPException 400: 缺少 chapter / beat 或序号非法。
            HTTPException 409: 大纲不存在或节拍越界。
        """
        store = _store(body.get("path"))
        chapter_id = str(body.get("chapter") or "")
        if not chapter_id:
            raise HTTPException(status_code=400, detail="缺少 chapter 参数（大纲章节 ID）")
        try:
            beat_index = int(body.get("beat"))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="缺少 beat 参数（节拍序号，从 1 开始）") from exc
        try:
            return delete_beat(store, chapter_id, beat_index)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/ui/outline/act/delete")
    def ui_outline_act_delete(body: dict) -> dict:
        """预览或执行整幕删除（含幕下所有已落地章节）。

        参数:
            body: `{path, act, dry_run?}`；dry_run=true 只返回按幕汇总的影响。

        返回:
            dry_run 时返回 `{act_id, index, title, chapters, landed_count, totals}`；
            正式删除返回 `{deleted, remaining_chapters, remaining_acts, warnings}`。

        异常:
            HTTPException 400: 缺少 act。
            HTTPException 409: 大纲不存在或找不到该幕。
        """
        store = _store(body.get("path"))
        act_id = str(body.get("act") or "")
        if not act_id:
            raise HTTPException(status_code=400, detail="缺少 act 参数（幕 ID）")
        try:
            if body.get("dry_run"):
                return act_delete_impact(store, act_id)
            return delete_outline_act(store, act_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/ui/outline/chapter/delete")
    def ui_outline_chapter_delete(body: dict) -> dict:
        """预览或执行整章删除（含已落地章节文件）。

        参数:
            body: `{path, chapter, dry_run?}`；dry_run=true 只返回影响，不删除。

        返回:
            dry_run 时返回 `{landed, index, title, chars, lines, audio, image, video}`；
            正式删除返回 `{deleted, remaining, acts, warnings}`。

        异常:
            HTTPException 400: 缺少 chapter。
            HTTPException 409: 大纲不存在或找不到该章。
        """
        store = _store(body.get("path"))
        chapter_id = str(body.get("chapter") or "")
        if not chapter_id:
            raise HTTPException(status_code=400, detail="缺少 chapter 参数（大纲章节 ID）")
        try:
            if body.get("dry_run"):
                return chapter_delete_impact(store, chapter_id)
            return delete_outline_chapter(store, chapter_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
