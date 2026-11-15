"""章节正文、版本、一致性、续写与时间轴。"""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path

from fastapi import HTTPException, Query

from novel_drama.application.production.line_edit import chapter_consistency
from novel_drama.application.story.chapter_import import (
    import_chapters as import_chapter_text,
)
from novel_drama.application.story.edit_chapter import (
    edit_chapter,
    restore_chapter_version,
    save_chapter_content,
)
from novel_drama.application.story.generate_chapter import generate_chapter_checked
from novel_drama.application.story.queries import (
    chapter_detail,
)
from novel_drama.application.story.summarize_chapter import (
    apply_summary,
    summarize_chapter,
)
from novel_drama.composition import AdapterError, ProjectStore
from novel_drama.infrastructure.config import load_config
from novel_drama.interface.http import use_cases
from novel_drama.interface.http.deps import store_for
from novel_drama.interface.http.routers.common import _require

_SUMMARY_PREVIEWS: dict[str, dict] = {}

_SUMMARY_PREVIEW_LIMIT = 64

def _summary_cache_key(store: ProjectStore, chapter_id: str) -> str:
    """生成重总结预览缓存键。

    参数:
        store: 工程。
        chapter_id: 章节 ID。

    返回:
        「工程绝对路径::章节 ID」字符串，保证不同工程互不覆盖。
    """
    return f"{store.root.resolve()}::{chapter_id}"

def _summary_content_hash(content: str) -> str:
    """计算正文哈希，用于判断预览生成后正文是否被改动。

    参数:
        content: 章节正文。

    返回:
        sha1 十六进制字符串。
    """
    return hashlib.sha1(content.encode("utf-8")).hexdigest()

def _remember_summary(store: ProjectStore, chapter_id: str, content: str, result: dict) -> None:
    """缓存一份重总结预览，供「应用」时直接复用，避免重复跑模型。

    参数:
        store: 工程。
        chapter_id: 章节 ID。
        content: 生成预览时的正文（用于校验是否过期）。
        result: `summarize_chapter(apply=False)` 的返回。
    """
    _SUMMARY_PREVIEWS[_summary_cache_key(store, chapter_id)] = {
        "content_hash": _summary_content_hash(content),
        "preview": result["preview"],
        "rounds": int(result.get("rounds") or 1),
    }
    while len(_SUMMARY_PREVIEWS) > _SUMMARY_PREVIEW_LIMIT:
        _SUMMARY_PREVIEWS.pop(next(iter(_SUMMARY_PREVIEWS)), None)

def _cached_summary(store: ProjectStore, chapter_id: str, content: str) -> dict | None:
    """读取未过期的重总结预览。

    参数:
        store: 工程。
        chapter_id: 章节 ID。
        content: 当前正文。

    返回:
        命中且正文未变时返回缓存项，否则 None。
    """
    cached = _SUMMARY_PREVIEWS.get(_summary_cache_key(store, chapter_id))
    if not cached or cached["content_hash"] != _summary_content_hash(content):
        return None
    return cached

def register_story_chapter_routes(app, project_root: str | None = None, config_path: str | None = None) -> None:
    """注册「章节正文、版本、一致性、续写与时间轴」相关路由。

    参数:
        app: FastAPI 实例。
        project_root: 默认工程目录（请求未带 path 时使用）。
        config_path: 本机配置文件路径（与 serve 同源）。

    返回:
        无。
    """
    _store = store_for(project_root)

    @app.post("/api/ui/chapter/import")
    def import_chapters(body: dict) -> dict:
        """把前端上传的纯文本按标题切章导入。

        参数:
            body: `{path, text, pattern?, on_duplicate?}`；`on_duplicate` 取值 skip/replace/new。

        返回:
            CLI `chapter import` 的 JSON 结果。

        异常:
            HTTPException 400: 正文为空。
        """
        path = body.get("path")
        text = str(body.get("text") or "")
        if not text.strip():
            raise HTTPException(status_code=400, detail={"code": 2, "message": "导入内容为空"})
        store = _store(path)
        pattern = str(body.get("pattern") or "").strip()
        on_duplicate = str(body.get("on_duplicate") or "skip")
        if on_duplicate not in {"skip", "replace", "new"}:
            raise HTTPException(status_code=400, detail={"code": 2, "message": "on_duplicate 只能是 skip/replace/new"})
        with tempfile.NamedTemporaryFile("w", suffix=".md", encoding="utf-8", delete=False) as handle:
            handle.write(text)
            temp_path = handle.name
        try:
            result = use_cases.call(
                lambda: import_chapter_text(
                    store,
                    text,
                    # 与 CLI 一致：快照里记绝对路径（macOS 上 /tmp 会解析成 /private/tmp）
                    str(Path(temp_path).resolve()),
                    Path(temp_path).stem,
                    pattern=pattern or None,
                    on_duplicate=on_duplicate,
                )
            )
            if result["code"]:
                # 沿用旧语义：有章节被跳过/拒绝替换时属于「拒绝执行」，409 并把结果原文放进 message
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": 1,
                        "message": json.dumps(result["payload"], ensure_ascii=False, indent=2),
                    },
                )
            return result["payload"]
        finally:
            Path(temp_path).unlink(missing_ok=True)

    @app.post("/api/ui/chapter/rename")
    def ui_chapter_rename(body: dict) -> dict:
        """重命名章节。

        参数:
            body: `{path, chapter, title}`。

        返回:
            `{chapter_id, title}`。

        异常:
            HTTPException 400: 标题为空；404: 章节不存在。
        """
        title = str(body.get("title") or "").strip()
        if not title:
            raise HTTPException(status_code=400, detail="章节标题不能为空")
        store = _store(body.get("path"))
        chapter_id = body.get("chapter") or ""
        try:
            chapter = store.load_chapter(chapter_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        chapter["title"] = title
        store.save_chapter(chapter)
        return {"chapter_id": chapter_id, "title": title}

    @app.post("/api/ui/chapter/delete")
    def ui_chapter_delete(body: dict) -> dict:
        """删除章节文件与其时间轴/脚本，并同步移除大纲里的对应条目。

        参数:
            body: `{path, chapter}`。

        返回:
            `{ok, chapter_id, outline_removed, warnings}`。

        异常:
            HTTPException 400: 只剩一个章节；404: 章节不存在。
        """
        store = _store(body.get("path"))
        chapter_id = body.get("chapter") or ""
        if len(store.list_chapters()) <= 1:
            raise HTTPException(status_code=400, detail="至少保留一个章节")
        path = store.chapter_path(chapter_id)
        if not path.exists():
            raise HTTPException(status_code=404, detail="章节不存在")
        result = {"ok": True, "chapter_id": chapter_id, "outline_removed": False, "warnings": []}
        # 已落章通常也在大纲里：一并删除，避免章节列表删掉后大纲还留着悬空引用
        try:
            from novel_drama.application.story.outline_edit import (
                delete_outline_chapter,
            )
            from novel_drama.composition import SettingsStore

            settings_store = SettingsStore(store.root)
            if settings_store.exists():
                bible = settings_store.load()
                if settings_store.find_outline_chapter(bible, chapter_id) is not None:
                    deleted = delete_outline_chapter(store, chapter_id)
                    result["outline_removed"] = True
                    result["warnings"] = deleted.get("warnings") or []
                    return result
        except (OSError, ValueError):
            # 大纲缺失或校验失败时降级为只删章节文件，不把删除请求卡死
            pass
        path.unlink(missing_ok=True)
        store.timeline_path(chapter_id).unlink(missing_ok=True)
        store.script_path(chapter_id).unlink(missing_ok=True)
        return result

    @app.post("/api/ui/chapter/mix")
    def ui_chapter_mix(body: dict) -> dict:
        """按时间轴执行本章混音（分轨 + 混音 + 响度规整）。

        参数:
            body: `{path, chapter}`。

        返回:
            CLI 的 JSON 结果（含 mix 路径与时长）。

        异常:
            HTTPException 500: 混音失败。
        """
        from novel_drama.interface.http.executor import ChildFailed, run_application

        store = _store(body.get("path"))
        chapter_id = body.get("chapter") or ""
        try:
            # 混音要跑 ffmpeg、耗时以分钟计：放独立子进程，页面其它接口不被阻塞
            result = run_application(
                "novel_drama.application.production.mix:mix_chapter",
                {
                    "root": str(store.root),
                    "chapter_id": chapter_id,
                    "config_path": config_path,
                    "render": True,
                },
            )
        except ChildFailed as exc:
            raise HTTPException(status_code=500, detail=f"混音失败：{exc}") from exc
        return dict(result or {})

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

    @app.get("/api/ui/chapter")
    def ui_chapter(path: str = Query(...), chapter: str = Query(...)):
        """章节详情（应用层读模型，与 `chapter show <id> --json` 同源）。"""
        store = _store(path)
        return use_cases.call(lambda: chapter_detail(store, chapter))

    @app.post("/api/ui/chapter/add")
    def ui_chapter_add(body: dict) -> dict:
        """新建章节（与 `chapter add --json` 同源），返回新章节 ID 供前端直接跳转。

        参数:
            body: `{path, title, index?, outline?}`。

        异常:
            HTTPException 400: 标题为空。
        """
        title = str(body.get("title") or "").strip()
        if not title:
            raise HTTPException(status_code=400, detail={"code": 2, "message": "章节标题不能为空"})
        _require(body, "本接口需要 path（工程目录）", path="工程目录")
        store = _store(body["path"])
        index = body.get("index")
        return store.create_chapter(
            title,
            content="",
            outline=str(body.get("outline") or ""),
            index=int(index) if index is not None else None,
        )

    @app.get("/api/ui/chapter/consistency")
    def ui_chapter_consistency(path: str = Query(...), chapter: str = Query(...)) -> dict:
        """正文与切分对位检查（只读）：任务页、配音页共用同一出口。"""
        return chapter_consistency(_store(path).load_chapter(chapter))

    @app.post("/api/ui/chapter/generate")
    def ui_chapter_generate(body: dict) -> dict:
        """生成章节正文（`chapter generate`）。

        参数:
            body: {path 工程目录, chapter 章节 ID, regen, dry_run,
                   multi_turn 是否按节拍分轮, beats_per_round 每轮节拍数, max_rounds 最大轮数}。

        返回:
            CLI 的 JSON 负载（含 words / rounds / beats_total / beats_done / stop_reason）。
        """
        _require(
            body,
            "本接口需要 path（工程目录）与 chapter（章节 ID）",
            path="工程目录",
            chapter="章节 ID",
        )
        store = _store(body["path"])
        return use_cases.call(
            lambda: generate_chapter_checked(
                store,
                load_config(config_path),
                body["chapter"],
                regen=bool(body.get("regen")),
                dry_run=bool(body.get("dry_run")),
                # 显式开关才带参数；缺省交给配置里的 writing.multi_turn 判断
                multi_turn=body.get("multi_turn"),
                beats_per_round=(
                    int(body["beats_per_round"]) if body.get("beats_per_round") is not None else None
                ),
                max_rounds=int(body["max_rounds"]) if body.get("max_rounds") is not None else None,
            )
        )

    @app.post("/api/ui/chapter/save")
    def ui_chapter_save(body: dict) -> dict:
        """保存手工编辑后的章节正文。

        正文可能有上万字：拼进 argv 会撞命令行长度上限，还会出现在进程列表里，
        因此这里不转发 CLI，直接调用 `application.story.edit_chapter.save_chapter_content`
        （与上方直接读 store 的只读接口是同一取舍）。

        参数:
            body: {path 工程目录, chapter 章节 ID, content 新正文}。

        返回:
            {chapter_id, word_count, revision, status, warnings}。

        异常:
            HTTPException: 正文为空返回 400，章节不存在返回 404。
        """
        store = _store(body.get("path"))
        try:
            return save_chapter_content(store, body["chapter"], body.get("content") or "")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/ui/chapter/versions")
    def ui_chapter_versions(path: str = Query(...), chapter: str = Query(...)) -> dict:
        """章节历史版本列表（新版本在前）。

        参数:
            path: 工程根目录。
            chapter: 章节 ID。

        返回:
            {chapter_id, versions: [{version_id, revision, title, word_count, updated_at, content_chars}]}。
        """
        store = _store(path)
        return {"chapter_id": chapter, "versions": store.list_chapter_versions(chapter)}

    @app.post("/api/ui/chapter/restore")
    def ui_chapter_restore(body: dict) -> dict:
        """把指定历史版本恢复为当前正文（revision 递增，必要时标记 stale）。

        参数:
            body: {path 工程目录, chapter 章节 ID, version_id 版本号}。

        返回:
            {chapter_id, version_id, revision, status, word_count, restored_from, warnings}。

        异常:
            HTTPException: 参数错误/该版本无正文 400，章节或版本不存在 404。
        """
        store = _store(body.get("path"))
        try:
            return restore_chapter_version(store, body.get("chapter") or "", body.get("version_id") or "")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/ui/chapter/edit")
    def ui_chapter_edit(body: dict) -> dict:
        """按提示词让 LLM 改写正文（默认预览，dry_run=false 才落盘）。

        参数:
            body: {path 工程目录, chapter 章节 ID, instruction 提示词,
                   range? {char_start, char_end}, dry_run? 默认 true}。

        返回:
            {chapter_id, mode, before, after, changed, word_count, revision,
             status, warnings, continuation_id?}。

        异常:
            HTTPException: 参数/正文/范围错误 400，章节不存在 404，模型异常 502。
        """
        store = _store(body.get("path"))
        raw_range = body.get("range")
        dry_run = body.get("dry_run")
        char_range = None
        if raw_range:
            if not isinstance(raw_range, dict):
                raise HTTPException(status_code=400, detail="range 需为 {char_start, char_end}")
            char_range = (raw_range.get("char_start"), raw_range.get("char_end"))
        try:
            return edit_chapter(
                store,
                load_config(config_path),
                body.get("chapter") or "",
                body.get("instruction") or "",
                char_range=char_range,
                dry_run=True if dry_run is None else bool(dry_run),
            )
        except AdapterError as exc:
            raise HTTPException(status_code=502, detail=exc.message) from exc
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/ui/chapter/summarize")
    def ui_chapter_summarize(body: dict) -> dict:
        """根据正文反向总结标题/摘要/大纲/节拍（默认预览，apply=true 才落盘）。

        参数:
            body: {path 工程目录, chapter 章节 ID, apply? 默认 false}。

        返回:
            预览: {chapter_id, preview, changes, replaced_beats}；
            应用: {applied, chapter_id, old_title, new_title, changed_fields,
                   replaced_beats, warnings}。

        说明:
            预览会按「工程 + 章节 + 正文哈希」缓存；随后 apply 命中缓存时直接写盘，
            不再重跑多轮模型，避免「点应用」后长时间无响应。

        异常:
            HTTPException: 正文为空 400，章节不存在 404，模型异常 502。
        """
        store = _store(body.get("path"))
        chapter_id = body.get("chapter") or ""
        apply = bool(body.get("apply"))
        try:
            chapter = store.load_chapter(chapter_id)
            content = str(chapter.get("content") or "")
            if apply:
                cached = _cached_summary(store, chapter_id, content)
                if cached:
                    return apply_summary(
                        store,
                        load_config(config_path),
                        chapter_id,
                        cached["preview"],
                        rounds=cached["rounds"],
                    )
            result = summarize_chapter(store, load_config(config_path), chapter_id, apply=apply)
            if not apply:
                _remember_summary(store, chapter_id, content, result)
            return result
        except AdapterError as exc:
            raise HTTPException(status_code=502, detail=exc.message) from exc
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
