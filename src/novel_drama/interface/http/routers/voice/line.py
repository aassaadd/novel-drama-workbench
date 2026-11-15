"""台词行编辑、拆分合并、音频变体与重配。"""

from __future__ import annotations

import json
import logging

from fastapi import HTTPException, Query

from novel_drama.application.production.line_edit import (
    chapter_consistency,
    edit_line_text,
    line_split_impact,
    merge_lines,
    merge_narrations,
    replace_line_texts,
    revert_line_params,
    set_lines,
    split_line_at,
    split_lines,
)
from novel_drama.application.production.line_query import chapter_lines, line_diff
from novel_drama.infrastructure.config import load_config
from novel_drama.interface.http import use_cases
from novel_drama.interface.http.deps import store_for
from novel_drama.interface.http.routers.common import _require

logger = logging.getLogger(__name__)

#: 请求体字段 → (台词行字段, 类型转换)；与 `line set` 的参数类型保持一致
_LINE_FIELD_MAP: dict[str, tuple[str, type]] = {
    "speaker": ("speaker_id", str),
    "kind": ("kind", str),
    "emotion": ("emotion", str),
    "emotion_intensity": ("emotion_intensity", float),
    "speed": ("speed", float),
    "pause_before_ms": ("pause_before_ms", int),
    "pause_after_ms": ("pause_after_ms", int),
    "text": ("text", str),
    "delivery_note": ("delivery_note", str),
}


def _line_updates(body: dict) -> dict:
    """从请求体提取实际给出的行字段。

    参数:
        body: 请求体，只传实际改动的字段。

    返回:
        `{台词行字段: 值}`；类型与 `line set` 的命令行参数一致（float/int/str）。
    """
    return {
        field: convert(body[key])
        for key, (field, convert) in _LINE_FIELD_MAP.items()
        if body.get(key) is not None
    }


def register_voice_line_routes(app, project_root: str | None = None, config_path: str | None = None) -> None:
    """注册「台词行编辑、拆分合并、音频变体与重配」相关路由。

    参数:
        app: FastAPI 实例。
        project_root: 默认工程目录（请求未带 path 时使用）。
        config_path: 本机配置文件路径（与 serve 同源）。

    返回:
        无。
    """
    _store = store_for(project_root)

    @app.post("/api/ui/lines/replace")
    def replace_line_text(body: dict) -> dict:
        """在本章台词里做查找替换（逐行同步正文）。

        参数:
            body: `{path, chapter, find, replace, whole?, case_sensitive?}`。

        返回:
            `{replaced, failed, total_matched}`。

        异常:
            HTTPException 400: find 为空。
        """
        path = body.get("path")
        chapter_id = body.get("chapter")
        find = str(body.get("find") or "")
        if not find:
            raise HTTPException(status_code=400, detail={"code": 2, "message": "查找内容不能为空"})
        replacement = str(body.get("replace") or "")
        store = _store(path)
        return use_cases.call(
            lambda: replace_line_texts(store, chapter_id, find, replacement)
        )

    @app.get("/api/ui/lines")
    def ui_lines(
        path: str = Query(...),
        chapter: str = Query(...),
        status: str | None = Query(default=None),
    ) -> dict:
        """台词表（`line list --json`，状态与筛选全部由 CLI 计算）。

        `status` 支持逗号分隔的 `pending,stale,ready,failed,unknown`，与终端用法一致。
        """
        store = _store(path)
        payload = use_cases.call(
            lambda: chapter_lines(
                store, chapter, load_config(config_path), statuses_raw=status
            )
        )
        # 正文与切分是否还对得上：只读检查，让编辑区能直接给出「重新切分」出口
        try:
            payload["consistency"] = chapter_consistency(_store(path).load_chapter(chapter))
        except Exception as exc:  # 检查失败不影响台词展示，但必须留下原因
            logger.warning("正文一致性检查失败（chapter=%s）：%s", chapter, exc, exc_info=True)
            payload["consistency"] = {
                "ok": None,
                "chapter": chapter,
                "chapter_title": "",
                "total": 0,
                "missing_total": 0,
                "missing": [],
                "error": f"正文一致性检查失败：{exc}",
            }
        return payload

    @app.get("/api/ui/line/diff")
    def ui_line_diff(path: str = Query(...), chapter: str = Query(...), line: str = Query(...)) -> dict:
        """单行参数差异（`line diff --json`）。"""
        store = _store(path)
        payload = use_cases.call(
            lambda: line_diff(store, chapter, line, load_config(config_path))
        )
        if not payload.get("snapshot_available"):
            # 沿用原有语义：没有参数快照属于「拒绝执行」，返回 409 且不改盘
            raise HTTPException(
                status_code=409,
                detail={
                    "code": 1,
                    # 与原实现一致：拒绝时把 payload 原文写进 message，界面按原样展示
                    "message": json.dumps(payload, ensure_ascii=False, indent=2),
                },
            )
        return payload

    @app.get("/api/ui/line")
    def ui_line(path: str = Query(...), chapter: str = Query(...), line: str = Query(...)):
        """单行详情（`line list --line --json`），供队列完成后按行回填。"""
        store = _store(path)
        return use_cases.call(
            lambda: chapter_lines(
                store, chapter, load_config(config_path), line_ids=[line]
            )
        )

    @app.post("/api/ui/line/set")
    def ui_line_set(body: dict) -> dict:
        """改行参数（`line set`）；只传实际改动的字段。"""
        _require(
            body,
            "本接口需要 path（工程目录）、chapter（章节 ID）与 line（行 ID）",
            path="工程目录",
            chapter="章节 ID",
            line="行 ID",
        )
        store = _store(body["path"])
        result = use_cases.call(
            lambda: set_lines(
                store, body["chapter"], [body["line"]], _line_updates(body), batch=False
            )
        )
        # 与旧接口一致：单行修改返回终端文案（该路由原本不带 --json）
        return {"text": result["text"]}

    @app.post("/api/ui/line/set-batch")
    def ui_line_set_batch(body: dict) -> dict:
        """批量改行参数（`line set --lines`）：返回成功/失败计数与失败原因。

        参数:
            body: `{path, chapter, lines: [行 ID], speaker?, kind?, ...}`。

        返回:
            `{changed, updated, not_found, counts}`。

        异常:
            HTTPException 400: 没有选中行。
        """
        _require(
            body,
            "本接口需要 path（工程目录）、chapter（章节 ID）与 lines（行 ID 列表）",
            path="工程目录",
            chapter="章节 ID",
        )
        lines = [str(item).strip() for item in (body.get("lines") or []) if str(item).strip()]
        if not lines:
            raise HTTPException(status_code=400, detail={"code": 2, "message": "没有选中任何行"})
        store = _store(body["path"])
        result = use_cases.call(
            lambda: set_lines(
                store, body["chapter"], lines, _line_updates(body), batch=True
            )
        )
        return result["payload"]

    @app.post("/api/ui/line/merge")
    def ui_line_merge(body: dict) -> dict:
        """合并同一场景内相邻的台词行（`line merge --json`）。

        参数:
            body: `{path, chapter, lines: [行 ID]}`，必须同场景且 index 连续。

        返回:
            `{changed, merged: {line_id, text, chars, removed, lost_audio}, revision}`。

        异常:
            HTTPException 400: 少于两行、跨场景、不相邻或正文对不上。
        """
        _require(
            body,
            "本接口需要 path（工程目录）、chapter（章节 ID）与 lines（行 ID 列表）",
            path="工程目录",
            chapter="章节 ID",
        )
        lines = [str(item).strip() for item in (body.get("lines") or []) if str(item).strip()]
        if len(lines) < 2:
            raise HTTPException(status_code=400, detail={"code": 2, "message": "至少选择两行才能合并"})
        store = _store(body["path"])
        result = use_cases.call(lambda: merge_lines(store, body["chapter"], lines))
        return result["payload"]

    @app.post("/api/ui/line/merge-narration")
    def ui_line_merge_narration(body: dict) -> dict:
        """按期望字数批量合并相邻旁白（`line merge-narration --json`）。

        参数:
            body: `{path, chapter, scene?|scenes?: [...], max_chars?, dry_run?}`；
                  `dry_run` 缺省为 true，只返回预览分组。

        返回:
            `{mode, dry_run, groups, counts, changed?}`。
        """
        _require(
            body,
            "本接口需要 path（工程目录）与 chapter（章节 ID）",
            path="工程目录",
            chapter="章节 ID",
        )
        scenes = body.get("scenes") or ([body["scene"]] if body.get("scene") else [])
        max_chars = int(body["max_chars"]) if body.get("max_chars") is not None else 120
        store = _store(body["path"])
        result = use_cases.call(
            lambda: merge_narrations(
                store,
                body["chapter"],
                scene_ids=[str(item) for item in scenes],
                max_chars=max_chars,
                dry_run=bool(body.get("dry_run", True)),
            )
        )
        return result["payload"]

    @app.post("/api/ui/line/edit")
    def ui_line_edit(body: dict) -> dict:
        """就地编辑台词文本，并同步回章节正文（`line edit --json`）。

        正文对不上时 CLI 返回退出码 2，界面会收到「正文已变更，请先重新切分」。
        """
        _require(
            body,
            "本接口需要 path（工程目录）、chapter（章节 ID）、line（行 ID）与 text（新文本）",
            path="工程目录",
            chapter="章节 ID",
            line="行 ID",
            text="新文本",
        )
        store = _store(body["path"])
        result = use_cases.call(
            lambda: edit_line_text(store, body["chapter"], body["line"], body["text"])
        )
        return result["payload"]

    @app.post("/api/ui/line/revert")
    def ui_line_revert(body: dict) -> dict:
        """回滚到上次生成时的参数（`line revert`）。"""
        _require(
            body,
            "本接口需要 path（工程目录）、chapter（章节 ID）与 line（行 ID）",
            path="工程目录",
            chapter="章节 ID",
            line="行 ID",
        )
        store = _store(body["path"])
        result = use_cases.call(
            lambda: revert_line_params(
                store, body["chapter"], body["line"], load_config(config_path)
            )
        )
        if not result["snapshot_available"]:
            # 沿用旧语义（CLI 退出码 1）：无参数快照属于「拒绝执行」，409 且不改盘
            raise HTTPException(
                status_code=409,
                detail={
                    "code": 1,
                    "message": json.dumps(result["payload"], ensure_ascii=False, indent=2),
                },
            )
        return result["payload"]

    @app.post("/api/ui/line/split")
    def ui_line_split(body: dict) -> dict:
        """切分台词（`line split`）；force 覆盖，keep_manual 保留人工修改的台词行。"""
        _require(
            body,
            "本接口需要 path（工程目录）与 chapter（章节 ID）",
            path="工程目录",
            chapter="章节 ID",
        )
        store = _store(body["path"])
        result = use_cases.call(
            lambda: split_lines(
                store,
                body["chapter"],
                force=bool(body.get("force")),
                keep_manual=bool(body.get("keep_manual")),
            )
        )
        return result["payload"]

    @app.post("/api/ui/line/split-at")
    def ui_line_split_at(body: dict) -> dict:
        """在光标处拆分一条台词（`line split-at --offset`）。

        参数:
            body: `{path, chapter, line, offset}`；offset 为 0 基字符位置。

        返回:
            `{changed, line_id, new_line_id, first_text, second_text}`。
        """
        _require(
            body,
            "本接口需要 path（工程目录）、chapter（章节 ID）与 line（行 ID）",
            path="工程目录",
            chapter="章节 ID",
            line="行 ID",
        )
        store = _store(body["path"])
        result = use_cases.call(
            lambda: split_line_at(
                store, body["chapter"], body["line"], int(body.get("offset") or 0)
            )
        )
        return result["payload"]

    @app.post("/api/ui/line/split/impact")
    def ui_line_split_impact(body: dict) -> dict:
        """重切台词的影响清单（`line split --impact`，只读）。"""
        _require(
            body,
            "本接口需要 path（工程目录）与 chapter（章节 ID）",
            path="工程目录",
            chapter="章节 ID",
        )
        store = _store(body["path"])
        result = use_cases.call(
            lambda: line_split_impact(
                store, body["chapter"], keep_manual=bool(body.get("keep_manual"))
            )
        )
        return result["payload"]
