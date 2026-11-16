"""配音页的补充能力：版本对比 / 试听清理 / 补齐缺失预演。

这些接口不重写业务规则：
- 版本列表、上传、激活、删除都只操作已落盘的音频与章节 `tts` 记录；
- 补齐缺失的预演仍然走应用层 `tts_plan.plan_batch_preview`；
- Casting 试听的**生成**已迁到媒体总线（`voice.preview`），这里只保留清理临时目录。

生成类接口（单行重录 / 批量配音 / 试听）统一由 `POST /api/ui/media/run` 启动。
"""

from __future__ import annotations

import json
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException, Request

from novel_drama.application.production.lines_source import chapter_view, chapter_views
from novel_drama.application.production.timeline import build_chapter_timeline
from novel_drama.application.production.tts_plan import plan_batch_preview
from novel_drama.composition import ProjectStore, check_cast, unresolved_speaker_lines
from novel_drama.infrastructure.config import load_config
from novel_drama.interface.http import use_cases
from novel_drama.tts import TtsService

#: 上传播音串行锁：同一时间只允许一个录音写入，避免两个请求同时改同一章
_RERUN_LOCK = threading.Lock()


def _now() -> str:
    """当前 UTC 时间（ISO 8601），落进录音元数据用。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rebuild_timeline(store: ProjectStore, chapter_id: str) -> bool:
    """录音 / 版本变化后重建章节时间轴。

    参数:
        store: 工程。
        chapter_id: 章节 ID。

    返回:
        True 表示重建成功；工程还没有时间轴或重建失败时为 False（不影响本次操作结果）。
    """
    if not store.timeline_path(chapter_id).exists():
        return False
    try:
        build_chapter_timeline(store, chapter_id)
        return True
    except Exception:  # noqa: BLE001 - 重建失败不阻断正在完成的录音/版本操作
        return False


def _store(path: str | None) -> ProjectStore:
    """定位工程，未初始化时返回 404。

    参数:
        path: 工程根目录。

    返回:
        ProjectStore。
    """
    if not path:
        raise HTTPException(status_code=400, detail="缺少 path 参数（工程目录）")
    store = ProjectStore(path)
    if not store.exists():
        raise HTTPException(status_code=404, detail=f"工程不存在: {store.root}")
    return store


def _find_line(store: ProjectStore, chapter_id: str, line_id: str) -> dict:
    """按章节 + 行 ID 取台词行。

    参数:
        store: 工程。
        chapter_id: 章节 ID。
        line_id: 台词行 ID。

    返回:
        台词行 dict。

    异常:
        HTTPException 404: 章节或台词行不存在。
    """
    try:
        chapter = chapter_view(store, store.load_chapter(chapter_id))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"章节不存在: {chapter_id}") from exc
    for scene in chapter.get("scenes") or []:
        for line in scene.get("lines") or []:
            if line.get("id") == line_id:
                return line
    raise HTTPException(status_code=404, detail=f"台词行不存在: {line_id}")


def _takes_index_path(store: ProjectStore) -> Path:
    """真人录音的元数据文件路径（记录哪一版是实录、时长等）。

    参数:
        store: 工程。

    返回:
        `.ndw/line_takes.json` 的绝对路径。
    """
    return store.root / ".ndw" / "line_takes.json"


def _load_takes(store: ProjectStore) -> dict:
    """读取真人录音元数据，文件缺失或损坏时返回空字典。

    参数:
        store: 工程。

    返回:
        `{行ID_r版本: {source, duration_ms, created_at}}`。
    """
    path = _takes_index_path(store)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_takes(store: ProjectStore, data: dict) -> None:
    """写回真人录音元数据（失败不影响主流程）。

    参数:
        store: 工程。
        data: 完整元数据字典。
    """
    path = _takes_index_path(store)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def _safe_duration(path: Path) -> int:
    """读取音频时长（毫秒），ffprobe 缺失或解析失败时返回 0。

    参数:
        path: 音频文件。

    返回:
        时长毫秒。
    """
    try:
        from novel_drama.composition import probe_duration_ms

        return int(probe_duration_ms(path) or 0)
    except Exception:  # noqa: BLE001 - 探测失败不应阻断版本列表
        return 0


def _take_params(path: Path) -> dict:
    """读取某一版音频的侧车参数（TTS 产物才有）。

    参数:
        path: 音频文件。

    返回:
        侧车里的 `params` 字典；没有侧车或解析失败时返回空字典。
    """
    sidecar = Path(str(path) + ".json")
    if not sidecar.is_file():
        return {}
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    params = data.get("params") if isinstance(data, dict) else None
    return params if isinstance(params, dict) else {}


def _line_versions(store: ProjectStore, line_id: str, active: str | None) -> list[dict]:
    """扫描某行已存在的所有音频版本。

    参数:
        store: 工程。
        line_id: 台词行 ID。
        active: 当前生效的相对路径（`line.tts.asset_id`）。

    返回:
        按 revision 倒序排列的版本列表，含来源（tts / human）与时长。
    """
    folder = store.root / "assets" / "voice" / "lines"
    if not folder.is_dir():
        return []
    takes = _load_takes(store)
    pattern = re.compile(rf"^{re.escape(line_id)}_r(\d+)\.wav$")
    rows: list[dict] = []
    for item in folder.iterdir():
        match = pattern.match(item.name)
        if not match or not item.is_file():
            continue
        revision = int(match.group(1))
        rel = str(item.relative_to(store.root))
        meta = takes.get(f"{line_id}_r{revision}") or {}
        rows.append(
            {
                "revision": revision,
                "path": rel,
                "size": item.stat().st_size,
                "active": bool(active) and rel == active,
                "source": str(meta.get("source") or "tts"),
                "duration_ms": int(meta.get("duration_ms") or _safe_duration(item)),
            }
        )
    rows.sort(key=lambda row: row["revision"], reverse=True)
    return rows


def _find_scene_line(chapter: dict, line_id: str) -> dict | None:
    """在章节里按 ID 找台词行。

    参数:
        chapter: 章节 dict。
        line_id: 台词行 ID。

    返回:
        台词行；找不到时返回 None。
    """
    for scene in chapter.get("scenes") or []:
        for item in scene.get("lines") or []:
            if item.get("id") == line_id:
                return item
    return None


#: 音色校验错误码 → 页面上给用户看的直白说法（不出现错误码等内部术语）
from novel_drama.interface.http.routers.voice.blockers import (
    _avg_ms_per_line,
    _blocker,
    _cast_blockers,
    _chapter_labels,
    _split_blockers,
    _unknown_speaker_blocker,
)


def register_voice_tools_routes(app, config_path: str | None = None) -> None:
    """注册版本 / 试听清理 / 补齐缺失预演接口。

    参数:
        app: FastAPI 实例。
        config_path: 本机配置文件路径（与 serve 同源）。

    返回:
        无。
    """

    @app.get("/api/ui/line/versions")
    def ui_line_versions(path: str, chapter: str, line: str) -> dict:
        """列出一行的全部录音版本，并标出当前生效版本。

        参数:
            path: 工程根目录。
            chapter: 章节 ID。
            line: 台词行 ID。

        返回:
            `{line_id, revision, active, versions: [...]}`
        """
        store = _store(path)
        row = _find_line(store, chapter, line)
        active = (row.get("tts") or {}).get("asset_id")
        versions = _line_versions(store, line, active)
        return {
            "line_id": line,
            "revision": int(row.get("revision") or 1),
            "active": active,
            "versions": versions,
        }

    @app.post("/api/ui/line/audio/delete")
    def ui_line_audio_delete(body: dict) -> dict:
        """删除某一行的生效音频，让该行退回「待配」状态。

        参数:
            body: `{path, chapter, line}`。

        返回:
            `{ok, line_id, changed, timeline_rebuilt}`；该行本来没有音频时 changed=False。

        说明:
            只清空章节里的 `tts` 记录，不删磁盘上的历史 wav（版本记录仍可试听/找回）；
            已有时间轴时同步重建，保证底栏时长与行状态一致。
        """
        path = body.get("path")
        chapter_id = body.get("chapter")
        line_id = body.get("line")
        if not line_id:
            raise HTTPException(status_code=400, detail="缺少 line 参数（台词行 ID）")
        store = _store(path)
        chapter = store.load_chapter(chapter_id)
        target = _find_scene_line(chapter, line_id)
        if target is None:
            raise HTTPException(status_code=404, detail=f"台词行不存在: {line_id}")
        if not target.get("tts"):
            return {"ok": True, "line_id": line_id, "changed": False, "timeline_rebuilt": False}
        target["tts"] = {"status": "pending"}
        store.save_chapter(chapter)
        timeline_rebuilt = _rebuild_timeline(store, chapter_id)
        return {"ok": True, "line_id": line_id, "changed": True, "timeline_rebuilt": timeline_rebuilt}

    @app.post("/api/ui/line/audio/upload")
    async def ui_line_audio_upload(
        request: Request,
        path: str,
        chapter: str,
        line: str,
        ext: str = "webm",
    ) -> dict:
        """上传一段真人录音，作为该行的新 take 写入并生效。

        参数:
            request: FastAPI 请求（原始音频字节放在 body）。
            path: 工程根目录。
            chapter: 章节 ID。
            line: 台词行 ID。
            ext: 上传容器格式（webm / mp4 / wav / m4a），只用于临时文件后缀。

        返回:
            `{ok, line_id, revision, active, duration_ms, versions, timeline_rebuilt}`

        异常:
            HTTPException 400: 请求体为空或过大。
            HTTPException 409: 已有重录任务在进行。
            HTTPException 500: ffmpeg 转码失败（中文原因）。
        """
        payload = await request.body()
        if not payload:
            raise HTTPException(status_code=400, detail="上传内容为空")
        if len(payload) > 64 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="录音文件超过 64MB，请分段录制")
        suffix = "." + re.sub(r"[^a-zA-Z0-9]", "", ext or "webm")[:8]
        store = _store(path)
        if not _RERUN_LOCK.acquire(blocking=False):
            raise HTTPException(status_code=409, detail={"code": 1, "message": "已有重录任务在进行，请等它结束"})
        try:
            chapter_data = store.load_chapter(chapter)
            target = _find_scene_line(chapter_data, line)
            if target is None:
                raise HTTPException(status_code=404, detail=f"台词行不存在: {line}")
            revision = int(target.get("revision") or 1) + 1
            folder = store.root / "assets" / "voice" / "lines"
            folder.mkdir(parents=True, exist_ok=True)
            out_path = folder / f"{line}_r{revision}.wav"

            tmp_dir = store.root / ".ndw" / "tmp"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(suffix=suffix, dir=tmp_dir, delete=False) as handle:
                handle.write(payload)
                raw_path = Path(handle.name)
            try:
                from novel_drama.composition import binary_available
                from novel_drama.composition import run as ffmpeg_run

                if not binary_available("ffmpeg"):
                    raise HTTPException(status_code=500, detail="未找到 ffmpeg，无法转码录音（macOS 可用 brew install ffmpeg）")
                try:
                    ffmpeg_run(
                        [
                            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
                            "-i", str(raw_path), "-ac", "1", "-ar", "48000", "-c:a", "pcm_s16le", str(out_path),
                        ],
                        timeout_s=120,
                    )
                except Exception as exc:  # 转码失败统一转成中文可操作提示
                    raise HTTPException(status_code=500, detail=f"录音转码失败：{exc}") from exc
            finally:
                raw_path.unlink(missing_ok=True)

            rel = str(out_path.relative_to(store.root))
            duration_ms = _safe_duration(out_path)
            target["revision"] = revision
            target["tts"] = {
                "status": "ready",
                "asset_id": rel,
                "duration_ms": duration_ms,
                "adapter": "human",
                "provider_id": "human",
                "voice_id": None,
                "variant_id": None,
                "chunks": [
                    {
                        "index": 0,
                        "text": target.get("text") or "",
                        "asset_id": rel,
                        "duration_ms": duration_ms,
                        "provider_id": "human",
                        "variant_id": None,
                        "status": "ready",
                    }
                ],
                "synthesized_revision": revision,
                "actual_speed": 1.0,
                "error": None,
                "updated_at": _now(),
            }
            store.save_chapter(chapter_data)
            takes = _load_takes(store)
            takes[f"{line}_r{revision}"] = {
                "source": "human",
                "duration_ms": duration_ms,
                "created_at": _now(),
            }
            _save_takes(store, takes)
            timeline_rebuilt = _rebuild_timeline(store, chapter)
            return {
                "ok": True,
                "line_id": line,
                "revision": revision,
                "active": rel,
                "duration_ms": duration_ms,
                "timeline_rebuilt": timeline_rebuilt,
                "versions": _line_versions(store, line, rel),
            }
        finally:
            _RERUN_LOCK.release()

    @app.post("/api/ui/line/audio/activate")
    def ui_line_audio_activate(body: dict) -> dict:
        """把某一版历史录音切回生效版本（不重新合成）。

        参数:
            body: `{path, chapter, line, revision}`。

        返回:
            `{ok, line_id, active, duration_ms, versions, timeline_rebuilt}`

        异常:
            HTTPException 404: 版本文件不存在。
        """
        path = body.get("path")
        chapter_id = body.get("chapter")
        line_id = body.get("line")
        revision = int(body.get("revision") or 0)
        if not line_id or revision <= 0:
            raise HTTPException(status_code=400, detail="缺少 line / revision 参数")
        store = _store(path)
        chapter_data = store.load_chapter(chapter_id)
        target = _find_scene_line(chapter_data, line_id)
        if target is None:
            raise HTTPException(status_code=404, detail=f"台词行不存在: {line_id}")
        rel = f"assets/voice/lines/{line_id}_r{revision}.wav"
        source_path = store.root / rel
        if not source_path.is_file():
            raise HTTPException(status_code=404, detail=f"录音版本不存在: r{revision}")
        duration_ms = _safe_duration(source_path)
        takes = _load_takes(store)
        meta = takes.get(f"{line_id}_r{revision}") or {}
        params = _take_params(source_path)
        tts = dict(target.get("tts") or {})
        tts.update(
            {
                "status": "ready",
                "asset_id": rel,
                "duration_ms": duration_ms,
                "synthesized_revision": revision,
                "error": None,
                "updated_at": _now(),
            }
        )
        if params:
            # TTS take：恢复该版侧车里的音色/供应商，避免切回后被判成「音色已更换」
            tts.update(
                {
                    "adapter": params.get("provider_id") or tts.get("adapter"),
                    "provider_id": params.get("provider_id") or tts.get("provider_id"),
                    "voice_id": params.get("voice_id"),
                    "variant_id": params.get("variant_id"),
                    "model_version": params.get("model_version"),
                }
            )
        if str(meta.get("source") or "") == "human":
            tts.update({"adapter": "human", "provider_id": "human", "voice_id": None, "variant_id": None})
        target["tts"] = tts
        store.save_chapter(chapter_data)
        timeline_rebuilt = _rebuild_timeline(store, chapter_id)
        return {
            "ok": True,
            "line_id": line_id,
            "active": rel,
            "duration_ms": duration_ms,
            "timeline_rebuilt": timeline_rebuilt,
            "versions": _line_versions(store, line_id, rel),
        }

    @app.post("/api/ui/line/audio/version/delete")
    def ui_line_audio_version_delete(body: dict) -> dict:
        """删除某一版录音文件；若删的是生效版本，该行退回「待配」。

        参数:
            body: `{path, chapter, line, revision}`。

        返回:
            `{ok, line_id, deleted, was_active, timeline_rebuilt, versions}`

        异常:
            HTTPException 404: 版本文件不存在。
        """
        path = body.get("path")
        chapter_id = body.get("chapter")
        line_id = body.get("line")
        revision = int(body.get("revision") or 0)
        if not line_id or revision <= 0:
            raise HTTPException(status_code=400, detail="缺少 line / revision 参数")
        store = _store(path)
        rel = f"assets/voice/lines/{line_id}_r{revision}.wav"
        source_path = store.root / rel
        if not source_path.is_file():
            raise HTTPException(status_code=404, detail=f"录音版本不存在: r{revision}")
        chapter_data = store.load_chapter(chapter_id)
        target = _find_scene_line(chapter_data, line_id)
        if target is None:
            raise HTTPException(status_code=404, detail=f"台词行不存在: {line_id}")
        tts = target.get("tts") or {}
        was_active = tts.get("asset_id") == rel
        try:
            source_path.unlink()
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"删除失败：{exc}") from exc
        takes = _load_takes(store)
        if takes.pop(f"{line_id}_r{revision}", None) is not None:
            _save_takes(store, takes)
        if was_active:
            target["tts"] = {"status": "pending"}
            store.save_chapter(chapter_data)
        timeline_rebuilt = was_active and _rebuild_timeline(store, chapter_id)
        active = (target.get("tts") or {}).get("asset_id")
        return {
            "ok": True,
            "line_id": line_id,
            "deleted": rel,
            "was_active": was_active,
            "timeline_rebuilt": timeline_rebuilt,
            "versions": _line_versions(store, line_id, active),
        }

    @app.post("/api/ui/cast/preview/clear")
    def ui_cast_preview_clear(body: dict) -> dict:
        """清空 Casting 试听临时目录。"""
        store = _store(body.get("path"))
        folder = store.root / ".ndw" / "cast_previews"
        removed = 0
        if folder.is_dir():
            for item in folder.iterdir():
                if item.is_file():
                    try:
                        item.unlink()
                        removed += 1
                    except OSError:
                        pass
        return {"removed": removed}

    @app.get("/api/ui/tts/batch-all/plan")
    def ui_batch_all_plan(path: str, scope: str = "chapter", chapter: str | None = None) -> dict:
        """补齐缺失的预计条数、跳过数与前置检查。

        说明:
            只跑缺失/失败/过期行，已有音频一律跳过；scope=chapter 时只看本章，
            scope=all 才跨章（底层仍是 `tts batch --all-chapters`，页面不叫全书重跑）。

        参数:
            path: 工程根目录。
            scope: `chapter`（默认）或 `all`。
            chapter: scope=chapter 时的章节 ID。

        返回:
            `{scope, chapter, total, skipped, lines_total, eta_ms, avg_ms_per_line,
              chapters, per_chapter, blockers, unknown_total, unknown_rows, ready, ready_reason,
              reuse}`；`reuse` 给出沿用/缓存命中/重新合成的行数与预计节省时间。
        """
        store = _store(path)
        if scope == "all":
            chapter_ids = [item["id"] for item in store.list_chapters()]
            if not chapter_ids:
                raise HTTPException(
                    status_code=412,
                    detail={"code": 4, "message": "[PRECONDITION] 工程里还没有章节"},
                )
        else:
            if not chapter:
                raise HTTPException(status_code=400, detail="补齐本章需要 chapter 参数")
            chapter_ids = [chapter]
        config = load_config(config_path)
        # 说话人无法归属的行不参与预演，与「补齐」正式执行时跳过的口径一致
        skip_ids = {
            row["line_id"] for row in unresolved_speaker_lines(store, chapter_views(store, chapter_ids))
        }
        plan = use_cases.call(
            lambda: plan_batch_preview(store, config, chapter_ids, skip_ids=skip_ids)["payload"]
        )
        total = int(plan.get("rows") or 0)
        unknown_rows = plan.get("unknown_rows") or []
        unknown_total = int(plan.get("skipped_unknown") or len(unknown_rows))
        unknown_ids = set(plan.get("unknown_line_ids") or [row.get("line_id") for row in unknown_rows])
        avg = _avg_ms_per_line(store)
        service = TtsService(config, config.project_root or None)
        try:
            rows = service.doctor()
        finally:
            service.close()
        usable = [item for item in rows if item.get("enabled") and item.get("available")]
        ready = bool(usable)
        ready_reason = ""
        if not ready:
            ready_reason = "还没有可用的配音模型：请到「设置 → 本机环境与模型」确认配音模型已下载并就绪。"

        # 音色检查按同一范围做：只看本章时不要把别的章节的缺失算成本章阻断项
        try:
            gate = check_cast(
                store,
                chapter_views(store, [chapter] if scope != "all" and chapter else None),
            )
            findings: list[dict] = list(gate.get("findings") or [])
        except Exception:  # noqa: BLE001 - 检查失败只表示「查不出问题」，与原先透传空结果一致
            findings = []
        labels = _chapter_labels(store)
        lines_total = int(plan.get("lines_total") or 0)
        # 阻断项逐条给出「名称 / 涉及章节或数量 / 原因 / 下一步」，页面不再只显示一句结论
        blockers: list[dict] = []
        if not ready:
            blockers.append(
                _blocker(
                    "PROVIDER_NOT_READY",
                    "没有可用的配音模型",
                    "配音前至少要有一个已就绪的配音模型，否则所有行都无法合成。",
                    {"label": "去设置检查模型", "kind": "models"},
                )
            )
        blockers.extend(_split_blockers(store, scope, chapter, lines_total))
        blockers.extend(
            _cast_blockers(
                store,
                findings,
                unknown_ids,
                labels,
                fallback_label=labels.get(chapter, "") if scope != "all" and chapter else "",
            )
        )
        unknown_blocker = _unknown_speaker_blocker(unknown_rows, unknown_total)
        if unknown_blocker:
            blockers.append(unknown_blocker)
        reuse = plan.get("reuse") or {}
        # 缓存命中的行不会真正调用模型，预计耗时只按需重合成的行数算，避免把 ETA 抬高一倍
        need_synth = int(reuse.get("regenerate") or total)
        return {
            "scope": scope,
            "chapter": chapter,
            "total": total,
            "skipped": int(plan.get("skipped") or 0),
            "lines_total": int(plan.get("lines_total") or 0),
            "eta_ms": need_synth * avg if avg and need_synth else 0,
            "avg_ms_per_line": avg,
            "chapters": plan.get("chapters") or [],
            "per_chapter": plan.get("per_chapter") or [],
            "blockers": blockers,
            "unknown_total": unknown_total,
            "unknown_rows": unknown_rows,
            "items": plan.get("items") or [],
            "items_truncated": bool(plan.get("items_truncated")),
            "assets": int(plan.get("assets") or 0),
            "reuse": reuse,
            "ready": ready,
            "ready_reason": ready_reason,
        }
