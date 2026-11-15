"""导出包、媒体渲染、时间轴与成片检查。"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import HTTPException, Query

from novel_drama.composition import ProjectStore, collect_references, comfy_media
from novel_drama.infrastructure.config import load_config

logger = logging.getLogger(__name__)

from novel_drama.interface.http.deps import run_cli, store_for
from novel_drama.interface.http.routers.common import (
    _respond,
)


def _export_preflight(store: ProjectStore, chapter_id: str) -> dict:
    """导出前检查（只读）：缺配音、音色编排提示、时间轴状态。

    参数:
        store: 工程读写入口。
        chapter_id: 章节 ID。

    返回:
        `{ok, issues, lines_total, ready, missing, missing_total, timeline_exists}`；
        issues 为 `{code, level, message, line_id?}`，level=error 表示建议先修。
    """
    from novel_drama.composition import check_cast

    chapter = store.load_chapter(chapter_id)
    characters = {item["id"]: item for item in store.load_characters().get("characters") or []}
    lines = [line for scene in chapter.get("scenes") or [] for line in scene.get("lines") or []]
    missing = [
        {
            "line_id": line.get("id"),
            "speaker": (characters.get(line.get("speaker_id")) or {}).get("name") or line.get("speaker_id"),
            "text": line.get("text"),
        }
        for line in lines
        if not (line.get("tts") or {}).get("asset_id")
    ]
    timeline_exists = store.timeline_path(chapter_id).exists()
    issues: list[dict] = []
    if not timeline_exists:
        issues.append({"code": "timeline-missing", "level": "warn", "message": "时间轴尚未构建（导出时会自动重建）"})
    if missing:
        issues.append(
            {
                "code": "missing-audio",
                "level": "warn",
                "message": f"{len(missing)} 行还没有配音，导出包里对应行没有音频",
            }
        )
    try:
        findings = check_cast(store, chapter_id).get("findings") or []
    except Exception as exc:  # 检查失败必须让用户看见，而不是当成「无问题」
        logger.warning("导出前音色检查失败（chapter=%s）：%s", chapter_id, exc, exc_info=True)
        findings = []
        issues.append(
            {
                "code": "cast-check-failed",
                "level": "error",
                "message": (
                    f"音色编排检查失败：{exc}；本次无法确认哪些行音色有问题。"
                    "确认忽略后仍可导出，但成品可能有音色错配"
                ),
            }
        )
    for item in findings[:20]:
        issues.append(
            {
                "code": item.get("code"),
                "level": item.get("level") or "warn",
                "message": item.get("message"),
                "line_id": item.get("line_id"),
            }
        )
    return {
        "ok": not any(item["level"] == "error" for item in issues),
        "issues": issues,
        "lines_total": len(lines),
        "ready": len(lines) - len(missing),
        "missing": missing[:100],
        "missing_total": len(missing),
        "timeline_exists": timeline_exists,
    }

def register_media_routes(app, project_root: str | None = None, config_path: str | None = None) -> None:
    """注册「导出包、媒体渲染、时间轴与成片检查」相关路由。

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

    @app.post("/api/ui/timeline/save")
    def ui_timeline_save(body: dict) -> dict:
        """保存页面上调整过的时间轴（片段平移 / 淡入淡出）。

        写回 `timelines/<chapter>.timeline.json`；后续混音与导出直接消费该文件。

        参数:
            body: `{path, chapter, timeline}`，timeline 需含 duration_ms 与 tracks。

        返回:
            `{saved, duration_ms, tracks}`。

        异常:
            HTTPException 400: 时间轴结构不合法。
        """
        store = _store(body["path"])
        chapter_id = body["chapter"]
        data = body.get("timeline") or {}
        tracks = data.get("tracks")
        if not isinstance(tracks, list):
            raise HTTPException(status_code=400, detail={"code": 4, "message": "时间轴缺少 tracks"})
        max_end = 0
        for track in tracks:
            for item in track.get("items") or []:
                try:
                    start = int(item.get("start_ms"))
                    end = int(item.get("end_ms"))
                    fade_in = max(0, int(item.get("fade_in_ms") or 0))
                    fade_out = max(0, int(item.get("fade_out_ms") or 0))
                except (TypeError, ValueError):
                    raise HTTPException(
                        status_code=400,
                        detail={"code": 4, "message": "片段起止或淡入淡出必须是整数毫秒"},
                    )
                if start < 0 or end <= start:
                    raise HTTPException(status_code=400, detail={"code": 4, "message": "片段结束时间必须大于开始时间"})
                # 淡入淡出超过片段时长时按片段长度截断（构建器会给很短的音效配长淡入淡出，
                # 混音侧同样按片段长度收敛，这里不因此拒绝整份时间轴）
                length = end - start
                if fade_in > length:
                    fade_in = length
                if fade_in + fade_out > length:
                    fade_out = max(0, length - fade_in)
                item["start_ms"] = start
                item["end_ms"] = end
                item["fade_in_ms"] = fade_in
                item["fade_out_ms"] = fade_out
                max_end = max(max_end, end)
        data["duration_ms"] = max(int(data.get("duration_ms") or 0), max_end)
        path = store.timeline_path(chapter_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"saved": True, "duration_ms": data["duration_ms"], "tracks": len(tracks)}

    @app.post("/api/ui/timeline/build")
    def build_timeline(body: dict) -> dict:
        """构建（或重建）某章时间轴并返回结果。

        参数:
            body: `{path, chapter}`。

        返回:
            时间轴 JSON（与 `GET /api/project/timeline/{chapter}` 相同结构）。
        """
        store = _store(body.get("path"))
        chapter_id = body.get("chapter")
        result = _cli("--project", str(store.root), "--json", "timeline", "build", "--chapter", chapter_id)
        if result.code != 0:
            raise HTTPException(status_code=500, detail={"code": result.code, "message": result.text.strip() or "时间轴构建失败"})
        timeline_path = store.timeline_path(chapter_id)
        if not timeline_path.exists():
            raise HTTPException(status_code=500, detail={"code": 3, "message": "时间轴构建后没有产物"})
        return json.loads(timeline_path.read_text(encoding="utf-8"))

    @app.get("/api/ui/media")
    def ui_media(path: str = Query(...), chapter: str = Query(...)) -> dict:
        """章节内三类媒体条目（从场景内嵌数据汇总，状态以 asset 为准）。

        图像/视频额外补只读的模板与画幅字段；单项解析失败只留空值，不让接口 500。
        """
        store = _store(path)
        chapter_data = store.load_chapter(chapter)
        config = load_config(config_path)
        project_settings = store.settings()
        try:
            manifest = comfy_media.load_manifest(comfy_media.templates_dir(config))
        except Exception:  # 清单缺失只影响展示，不影响其它字段
            manifest = {}

        def _template_for(item: dict, kind: str, has_refs: bool = False) -> str:
            """解析单个条目实际使用的模板显示名。

            参数:
                item: 关键图或视频条目。
                kind: image / video。
                has_refs: 关键图是否带参考图（决定展示 image_ref 模板）。

            返回:
                模板显示名（缺省回落到模板 ID）；解析失败返回空串。
            """
            try:
                if kind == "video":
                    i2v = item.get("mode") == "i2v"
                    template_kind = "video_i2v" if i2v else "video_t2v"
                    setting_key = "comfyui.templates.video_i2v" if i2v else "comfyui.templates.video_t2v"
                else:
                    template_kind = "image_ref" if has_refs else "image"
                    setting_key = "comfyui.templates.image_ref" if has_refs else "comfyui.templates.image"
                template_id = config.get(setting_key)
                explicit = item.get("workflow_template_id")
                if explicit:
                    # 只有条目自带模板的用途与本次一致时才采用，避免纯文生图模板盖掉参考图模板
                    entry = comfy_media.template_by_id(manifest, str(explicit))
                    if str(entry.get("kind") or "") == template_kind:
                        template_id = explicit
                resolved = comfy_media.resolve_template(template_id, template_kind, manifest)
                entry = resolved.get("template") or {}
                return str(entry.get("display_name") or resolved.get("template_id") or "")
            except Exception:
                return ""

        def _aspect_for(kind: str) -> dict:
            """按工程画幅解析尺寸。

            参数:
                kind: image / video。

            返回:
                `{"ratio","width","height"}`；解析失败时各字段为 None。
            """
            try:
                return comfy_media.resolve_aspect(project_settings, kind)
            except Exception:
                return {"ratio": None, "width": None, "height": None}

        def _size_mismatch(asset: dict, aspect: dict) -> bool:
            """判断已生成产物尺寸是否与工程画幅一致。

            参数:
                asset: 条目资产状态（含 width/height）。
                aspect: 工程画幅解析结果。

            返回:
                双方尺寸都存在且不一致时返回 True；缺数据时按一致处理。
            """
            try:
                actual_width, actual_height = int(asset.get("width")), int(asset.get("height"))
                expected_width, expected_height = int(aspect.get("width")), int(aspect.get("height"))
            except (TypeError, ValueError):
                return False
            return (actual_width, actual_height) != (expected_width, expected_height)

        rows = {"sfx": [], "image": [], "video": []}
        keys = {"sfx": "sfx_events", "image": "key_images", "video": "video_segments"}
        for scene in chapter_data.get("scenes") or []:
            for kind, key in keys.items():
                for item in scene.get(key) or []:
                    asset = item.get("asset") or {}
                    template = ""
                    aspect = {"ratio": None, "width": None, "height": None}
                    # 参考图只给关键图；解析失败降级为空列表 + 一条告警，绝不让接口 500
                    references = []
                    reference_warnings = []
                    if kind == "image":
                        try:
                            resolved_refs = collect_references(store, config, chapter_data, scene, item)
                            references = [
                                {field: ref.get(field) for field in ("kind", "id", "name", "rel")}
                                for ref in resolved_refs.get("items") or []
                            ]
                            reference_warnings = list(resolved_refs.get("warnings") or [])
                        except Exception as exc:
                            references = []
                            reference_warnings = [f"参考图解析失败：{exc}"]
                    if kind in ("image", "video"):
                        template = _template_for(item, kind, bool(references))
                        aspect = _aspect_for(kind)
                    rows[kind].append(
                        {
                            "id": item.get("id"),
                            "scene_id": scene.get("id"),
                            "index": item.get("index"),
                            "prompt": item.get("prompt") or "",
                            "prompt_en": item.get("prompt_en") or "",
                            "kind": item.get("kind"),
                            "mode": item.get("mode"),
                            "duration_ms": item.get("duration_ms") or asset.get("actual_duration_ms"),
                            "status": asset.get("status") or item.get("status") or "pending",
                            "asset_id": asset.get("asset_id"),
                            "error": asset.get("error"),
                            "template": template or None,
                            "aspect_ratio": aspect.get("ratio"),
                            "width": aspect.get("width"),
                            "height": aspect.get("height"),
                            "actual_width": asset.get("width"),
                            "actual_height": asset.get("height"),
                            "size_mismatch": _size_mismatch(asset, aspect) if kind in ("image", "video") else False,
                            "references": references,
                            "reference_warnings": reference_warnings,
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

    @app.post("/api/ui/export/jianying")
    def ui_export_jianying(body: dict) -> dict:
        """导出剪映草稿 zip（`export --format jianying`）。"""
        argv = [
            "--project",
            body["path"],
            "--json",
            "export",
            "--chapter",
            body["chapter"],
            "--format",
            "jianying",
        ]
        if body.get("out_dir"):
            argv += ["--out-dir", body["out_dir"]]
        return _respond(_cli(*argv))

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
                rel = item.relative_to(root)
                # 剪映草稿的 media 目录是素材副本，逐条列出会刷屏，只展示 zip 与草稿元数据
                if (
                    len(rel.parts) > 3
                    and rel.parts[0] == "exports"
                    and rel.parts[1] == "jianying"
                    and rel.parts[3] == "media"
                ):
                    continue
                stat = item.stat()
                files.append(
                    {
                        "rel": str(rel),
                        "path": str(item),
                        "size": stat.st_size,
                        "mtime": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                    }
                )
        return {"root": str(root), "files": files}

    @app.get("/api/ui/export/preflight")
    def ui_export_preflight(path: str = Query(...), chapter: str = Query(...)) -> dict:
        """导出前检查：缺配音行、音色编排提示、时间轴状态。

        参数:
            path: 工程目录。
            chapter: 章节 ID。

        返回:
            见 `_export_preflight`。
        """
        return _export_preflight(_store(path), chapter)

    @app.post("/api/ui/export/bundle")
    def ui_export_bundle(body: dict) -> dict:
        """导出本章交付包并打成 zip（新版前端「导出」页使用）。

        流程：先构建时间轴（幂等派生物），再走 `export --format bundle` 产出
        SRT / 清单 / MP3 / MP4，最后把成品与逐行配音音频、台词脚本一起打包。

        参数:
            body: `{path, chapter}`。

        返回:
            `{ok, zip, rel, name, missing, entries}`；`rel` 可直接交给
            `/api/ui/file?path=...&project=...` 下载。

        异常:
            HTTPException: 时间轴构建或导出失败时透传 CLI 的中文说明。
        """
        import zipfile
        from datetime import datetime

        path = body["path"]
        chapter_id = body["chapter"]
        store = _store(path)
        # 导出前检查：有缺音频/编排问题时必须显式忽略，避免「静默交付半成品」
        check = _export_preflight(store, chapter_id)
        blocking = check["missing_total"] or any(item["level"] == "error" for item in check["issues"])
        if blocking and not body.get("ignore_issues"):
            raise HTTPException(
                status_code=412,
                detail={
                    "code": 4,
                    "message": (
                        f"导出前检查未通过：{check['missing_total']} 行缺音频，"
                        f"{len(check['issues'])} 条问题；确认忽略后再导出"
                    ),
                    "preflight": check,
                },
            )
        # 时间轴是导出前置；重建是幂等的，避免用户改词后导出到旧时间轴
        _respond(_cli("--project", path, "--json", "timeline", "build", "--chapter", chapter_id))
        result = _respond(_cli("--project", path, "--json", "export", "--chapter", chapter_id, "--format", "bundle"))
        if not isinstance(result, dict):
            raise HTTPException(status_code=500, detail={"code": 3, "message": "导出结果格式异常"})

        lines_payload = _respond(_cli("--project", path, "--json", "line", "list", "--chapter", chapter_id))
        rows = (lines_payload or {}).get("lines") or []
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        export_dir = store.root / "exports"
        export_dir.mkdir(parents=True, exist_ok=True)
        zip_path = export_dir / f"{chapter_id}.bundle-{stamp}.zip"

        entries: list[str] = []
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
            # 1) 交付包成品（srt / manifest / mp3 / mp4 里有就收）
            for item in result.get("artifacts") or []:
                target = Path(str(item.get("path") or ""))
                if target.is_file():
                    archive.write(target, target.name)
                    entries.append(target.name)
            for key in ("readme", "bundle"):
                target = Path(str(result.get(key) or ""))
                if target.is_file():
                    archive.write(target, target.name)
                    entries.append(target.name)
            # 2) 台词脚本：没有成片时也能直接读稿
            script = "\n".join(
                f"[{int(row.get('ordinal') or 0):03d}] {row.get('speaker') or ''}：{row.get('text') or ''}"
                for row in rows
            )
            archive.writestr("台词脚本.txt", script + "\n")
            entries.append("台词脚本.txt")
            # 3) 已配好的逐行音频：交付包里最实用的部分
            audio_count = 0
            for row in rows:
                rel = row.get("audio")
                if not rel:
                    continue
                source = store.root / str(rel)
                if not source.is_file():
                    continue
                archive.write(source, f"audio/{source.name}")
                audio_count += 1
            if audio_count:
                entries.append(f"audio/（{audio_count} 个文件）")

        return {
            "ok": True,
            "zip": str(zip_path),
            "rel": str(zip_path.relative_to(store.root)),
            "name": zip_path.name,
            "missing": result.get("missing") or [],
            "entries": entries,
        }
