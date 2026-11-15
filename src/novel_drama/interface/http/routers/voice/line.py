"""台词行编辑、拆分合并、音频变体与重配。"""

from __future__ import annotations

import logging

from fastapi import HTTPException, Query

from novel_drama.interface.cli.commands.line import chapter_consistency

logger = logging.getLogger(__name__)

from novel_drama.interface.http.deps import run_cli, store_for
from novel_drama.interface.http.routers.common import (
    _respond,
)


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

    def _cli(*argv: str):
        """按 `serve --config` 的同一份配置执行 CLI。

        参数:
            argv: 不含 `--config` 的参数串。

        返回:
            `actions.CliResult`。
        """
        return run_cli(config_path, *argv)

    @app.post("/api/ui/lines/replace")
    def replace_line_text(body: dict) -> dict:
        """在本章台词里做查找替换（逐行走 `line edit`，保证正文同步）。

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
        payload = store.load_chapter(chapter_id)
        replaced = 0
        failed = 0
        matched = 0
        for scene in payload.get("scenes") or []:
            for line in scene.get("lines") or []:
                text = str(line.get("text") or "")
                if find not in text:
                    continue
                matched += 1
                new_text = text.replace(find, replacement)
                result = _cli(
                    "--project", str(store.root), "--json",
                    "line", "edit", "--chapter", chapter_id, "--line", line.get("id"), "--text", new_text,
                )
                if result.code == 0:
                    replaced += 1
                else:
                    failed += 1
        return {"replaced": replaced, "failed": failed, "total_matched": matched}

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
        payload = _respond(_cli(*argv))
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
        return _respond(_cli("--project", path, "--json", "line", "diff", "--chapter", chapter, "--line", line))

    @app.get("/api/ui/line")
    def ui_line(path: str = Query(...), chapter: str = Query(...), line: str = Query(...)):
        """单行详情（`line list --line --json`），供队列完成后按行回填。"""
        return _respond(_cli("--project", path, "--json", "line", "list", "--chapter", chapter, "--line", line))

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
        lines = [str(item).strip() for item in (body.get("lines") or []) if str(item).strip()]
        if not lines:
            raise HTTPException(status_code=400, detail={"code": 2, "message": "没有选中任何行"})
        argv = [
            "--project",
            body["path"],
            "--json",
            "line",
            "set",
            "--chapter",
            body["chapter"],
            "--lines",
            ",".join(lines),
        ]
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
        lines = [str(item).strip() for item in (body.get("lines") or []) if str(item).strip()]
        if len(lines) < 2:
            raise HTTPException(status_code=400, detail={"code": 2, "message": "至少选择两行才能合并"})
        return _respond(
            _cli(
                "--project",
                body["path"],
                "--json",
                "line",
                "merge",
                "--chapter",
                body["chapter"],
                "--lines",
                ",".join(lines),
            )
        )

    @app.post("/api/ui/line/merge-narration")
    def ui_line_merge_narration(body: dict) -> dict:
        """按期望字数批量合并相邻旁白（`line merge-narration --json`）。

        参数:
            body: `{path, chapter, scene?|scenes?: [...], max_chars?, dry_run?}`；
                  `dry_run` 缺省为 true，只返回预览分组。

        返回:
            `{mode, dry_run, groups, counts, changed?}`。
        """
        argv = ["--project", body["path"], "--json", "line", "merge-narration", "--chapter", body["chapter"]]
        scenes = body.get("scenes") or ([body["scene"]] if body.get("scene") else [])
        for scene in scenes:
            argv += ["--scene", str(scene)]
        if body.get("max_chars") is not None:
            argv += ["--max-chars", str(int(body["max_chars"]))]
        if body.get("dry_run", True):
            argv.append("--dry-run")
        return _respond(_cli(*argv))

    @app.post("/api/ui/line/edit")
    def ui_line_edit(body: dict) -> dict:
        """就地编辑台词文本，并同步回章节正文（`line edit --json`）。

        正文对不上时 CLI 返回退出码 2，界面会收到「正文已变更，请先重新切分」。
        """
        return _respond(
            _cli(
                "--project",
                body["path"],
                "--json",
                "line",
                "edit",
                "--chapter",
                body["chapter"],
                "--line",
                body["line"],
                "--text",
                body["text"],
            )
        )

    @app.post("/api/ui/line/revert")
    def ui_line_revert(body: dict) -> dict:
        """回滚到上次生成时的参数（`line revert`）。"""
        return _respond(
            _cli("--project", body["path"], "--json", "line", "revert", "--chapter", body["chapter"], "--line", body["line"])
        )

    @app.post("/api/ui/line/split")
    def ui_line_split(body: dict) -> dict:
        """切分台词（`line split`）；force 覆盖，keep_manual 保留人工修改的台词行。"""
        argv = ["--project", body["path"], "--json", "line", "split", "--chapter", body["chapter"]]
        if body.get("force"):
            argv.append("--force")
        if body.get("keep_manual"):
            argv.append("--keep-manual")
        return _respond(_cli(*argv))

    @app.post("/api/ui/line/split-at")
    def ui_line_split_at(body: dict) -> dict:
        """在光标处拆分一条台词（`line split-at --offset`）。

        参数:
            body: `{path, chapter, line, offset}`；offset 为 0 基字符位置。

        返回:
            `{changed, line_id, new_line_id, first_text, second_text}`。
        """
        return _respond(
            _cli(
                "--project",
                body["path"],
                "--json",
                "line",
                "split-at",
                "--chapter",
                body["chapter"],
                "--line",
                body["line"],
                "--offset",
                str(int(body.get("offset") or 0)),
            )
        )

    @app.post("/api/ui/line/split/impact")
    def ui_line_split_impact(body: dict) -> dict:
        """重切台词的影响清单（`line split --impact`，只读）。"""
        argv = ["--project", body["path"], "--json", "line", "split", "--chapter", body["chapter"], "--impact"]
        if body.get("keep_manual"):
            argv.append("--keep-manual")
        return _respond(_cli(*argv))
