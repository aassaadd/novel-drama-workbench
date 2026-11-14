"""智能对轨接口：上传整段播音，按静音切分后依次写入所选章节的台词行。

不引入新的转写模型：对轨以「静音切分 + 顺序对应」实现，切分数量与台词行数
不一致时不写入，只把任务标记为失败并给出原因，避免错位污染已有录音。
产物写入 `assets/voice/lines/<line>_r<revision>.wav`，与真人录音、TTS take
共用同一套版本与时间轴结构。
"""

from __future__ import annotations

import json
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException, Request

from ..models.ids import now_iso
from ..store import ProjectStore

#: 允许上传的音频后缀
ALLOWED_SUFFIX = {".mp3", ".wav", ".m4a"}

#: 单个文件上限（100MB，与参考站一致）
MAX_BYTES = 100 * 1024 * 1024

#: 静音判定：-35dB 以下持续 0.35s 视为间隔
SILENCE_DB = -35
SILENCE_SECONDS = 0.35

#: 任务记录文件（工程内 .ndw/align_tasks.json）
TASKS_NAME = "align_tasks.json"


def _now() -> str:
    """当前 UTC 时间（ISO 8601）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _store(path: str | None) -> ProjectStore:
    """按路径取工程，未初始化时返回 404。

    参数:
        path: 工程根目录。

    返回:
        ProjectStore。

    异常:
        HTTPException: 路径缺失或工程不存在。
    """
    if not path:
        raise HTTPException(status_code=400, detail="缺少工程路径")
    store = ProjectStore(path)
    if not store.exists():
        raise HTTPException(status_code=404, detail="工程不存在")
    return store


def _tasks_path(store: ProjectStore) -> Path:
    """任务记录文件路径。"""
    return store.root / ".ndw" / TASKS_NAME


def _load_tasks(store: ProjectStore) -> list[dict]:
    """读取对轨任务（文件缺失或损坏时返回空列表）。

    参数:
        store: 工程。

    返回:
        任务列表。
    """
    path = _tasks_path(store)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    tasks = data.get("tasks") if isinstance(data, dict) else None
    return tasks if isinstance(tasks, list) else []


def _save_tasks(store: ProjectStore, tasks: list[dict]) -> None:
    """原子写回任务列表。

    参数:
        store: 工程。
        tasks: 完整任务列表。
    """
    path = _tasks_path(store)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"schema_version": 1, "tasks": tasks}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _chapter_range(store: ProjectStore, start: str, end: str) -> list[dict]:
    """取所选范围内的章节（按工程内章节顺序，含首尾）。

    参数:
        store: 工程。
        start: 起始章节 ID。
        end: 结束章节 ID。

    返回:
        章节 dict 列表。

    异常:
        HTTPException: 章节不存在或起止顺序颠倒。
    """
    chapters = store.list_chapters()
    ids = [item["id"] for item in chapters]
    if start not in ids or end not in ids:
        raise HTTPException(status_code=404, detail="选择的章节不存在")
    head, tail = ids.index(start), ids.index(end)
    if head > tail:
        raise HTTPException(status_code=400, detail="起始章节不能晚于结束章节")
    return [store.load_chapter(chapter_id) for chapter_id in ids[head : tail + 1]]


def _ordered_lines(chapters: list[dict]) -> list[tuple[str, dict]]:
    """按章节 → 场景 → 行顺序展开台词行。

    参数:
        chapters: 章节 dict 列表。

    返回:
        `(章节 ID, 台词行)` 列表。
    """
    rows: list[tuple[str, dict]] = []
    for chapter in chapters:
        for scene in sorted(chapter.get("scenes") or [], key=lambda item: item.get("index") or 0):
            for line in sorted(scene.get("lines") or [], key=lambda item: item.get("index") or 0):
                if (line.get("text") or "").strip():
                    rows.append((chapter["id"], line))
    return rows


def _speech_intervals(path: Path) -> list[tuple[float, float]]:
    """用 ffmpeg silencedetect 求有声区间。

    参数:
        path: 已转成 wav 的音频。

    返回:
        `[(start_s, end_s)]`，按时间升序。

    异常:
        HTTPException: ffmpeg 缺失或执行失败。
    """
    from ..media.ffmpeg import run

    try:
        result = run(
            [
                "ffmpeg", "-hide_banner", "-nostdin", "-i", str(path),
                "-af", f"silencedetect=noise={SILENCE_DB}dB:d={SILENCE_SECONDS}",
                "-f", "null", "-",
            ],
            timeout_s=600,
        )
    except Exception as exc:  # noqa: BLE001 - 统一转成可操作提示
        raise HTTPException(status_code=500, detail=f"读取音频失败：{exc}") from exc
    stderr = result.stderr or ""
    starts = [float(match) for match in re.findall(r"silence_start:\s*([0-9.]+)", stderr)]
    ends = [float(match) for match in re.findall(r"silence_end:\s*([0-9.]+)", stderr)]
    total = 0.0
    duration_match = re.search(r"Duration:\s*(\d+):(\d+):([0-9.]+)", stderr)
    if duration_match:
        hours, minutes, seconds = duration_match.groups()
        total = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    if not total:
        from ..media.ffmpeg import probe_duration_ms

        total = probe_duration_ms(path) / 1000
    intervals: list[tuple[float, float]] = []
    cursor = 0.0
    for start, end in zip(starts, ends):
        if start - cursor > 0.05:
            intervals.append((cursor, start))
        cursor = max(cursor, end)
    if total - cursor > 0.05:
        intervals.append((cursor, total))
    return intervals


def _convert_upload(raw: Path, target: Path) -> None:
    """把上传文件转成 48kHz 单声道 wav。

    参数:
        raw: 上传的临时文件。
        target: 目标 wav。

    异常:
        HTTPException: 转码失败。
    """
    from ..media.ffmpeg import run

    try:
        run(
            [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
                "-i", str(raw), "-ac", "1", "-ar", "48000", "-c:a", "pcm_s16le", str(target),
            ],
            timeout_s=600,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"音频转码失败：{exc}") from exc


def _cut_segment(source: Path, target: Path, start_s: float, end_s: float) -> int:
    """按时间区间切出一段 wav。

    参数:
        source: 完整 wav。
        target: 目标片段。
        start_s: 起始秒。
        end_s: 结束秒。

    返回:
        片段时长（毫秒）。
    """
    from ..media.ffmpeg import probe_duration_ms, run

    run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
            "-ss", f"{max(0.0, start_s):.3f}", "-to", f"{max(start_s + 0.05, end_s):.3f}",
            "-i", str(source), "-ac", "1", "-ar", "48000", "-c:a", "pcm_s16le", str(target),
        ],
        timeout_s=120,
    )
    return int(probe_duration_ms(target) or 0)


def _apply_take(store: ProjectStore, line: dict, rel: str, duration_ms: int, task_id: str) -> None:
    """把切出的音频写成该行的新生效版本（保留旧版本）。

    只修改传入的行 dict 与版本索引，章节落盘由调用方统一保存。

    参数:
        store: 工程。
        line: 台词行 dict（会原地修改）。
        rel: 音频相对工程根目录的路径。
        duration_ms: 音频时长。
        task_id: 对轨任务 ID，记进版本元数据。
    """
    from .voice_tools_api import _load_takes, _save_takes

    revision = int(line.get("revision") or 1) + 1
    line["revision"] = revision
    line["tts"] = {
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
                "text": line.get("text") or "",
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
        "updated_at": now_iso(),
    }
    takes = _load_takes(store)
    takes[f"{line.get('id')}_r{revision}"] = {
        "source": "align",
        "duration_ms": duration_ms,
        "created_at": _now(),
        "task_id": task_id,
    }
    _save_takes(store, takes)


def register_align_routes(app, config_path: str | None = None) -> None:
    """注册智能对轨相关接口。

    参数:
        app: FastAPI 应用。
        config_path: 本机配置路径；重建时间轴时透传给 CLI。
    """

    @app.get("/api/ui/align/tasks")
    def ui_align_tasks(path: str) -> dict:
        """读取对轨任务列表。

        参数:
            path: 工程根目录。

        返回:
            `{tasks: [...]}`。
        """
        store = _store(path)
        return {"tasks": _load_tasks(store)}

    @app.post("/api/ui/align/tasks/delete")
    def ui_align_tasks_delete(body: dict) -> dict:
        """删除一条任务记录（不删除已写入的台词音频）。

        参数:
            body: `{path, id}`。

        返回:
            `{ok: true}`。
        """
        store = _store(body.get("path"))
        task_id = str(body.get("id") or "")
        tasks = [item for item in _load_tasks(store) if item.get("id") != task_id]
        _save_tasks(store, tasks)
        return {"ok": True}

    @app.post("/api/ui/align/upload")
    async def ui_align_upload(
        request: Request,
        path: str,
        start_chapter: str,
        end_chapter: str,
        filename: str = "upload.mp3",
    ) -> dict:
        """上传播音并按静音切分对轨。

        参数:
            request: FastAPI 请求（原始音频字节放在 body）。
            path: 工程根目录。
            start_chapter: 起始章节 ID。
            end_chapter: 结束章节 ID。
            filename: 原始文件名，只用于后缀与展示。

        返回:
            `{ok, task}`。

        异常:
            HTTPException 400: 格式 / 大小不合法或起止章节颠倒。
            HTTPException 404: 工程或章节不存在。
            HTTPException 500: ffmpeg 缺失或转码 / 切分失败。
        """
        suffix = Path(filename or "").suffix.lower()
        if suffix not in ALLOWED_SUFFIX:
            raise HTTPException(status_code=400, detail="只支持 MP3 / WAV / M4A 格式")
        payload = await request.body()
        if not payload:
            raise HTTPException(status_code=400, detail="上传内容为空")
        if len(payload) > MAX_BYTES:
            raise HTTPException(status_code=400, detail="音频超过 100MB，请压缩或分段上传")

        store = _store(path)
        chapters = _chapter_range(store, start_chapter, end_chapter)
        rows = _ordered_lines(chapters)
        if not rows:
            raise HTTPException(status_code=400, detail="所选章节还没有台词行，先在画本编辑里切分台词")

        task_id = f"aln_{now_iso().replace(':', '').replace('-', '')[:15]}"
        align_dir = store.root / ".ndw" / "align"
        align_dir.mkdir(parents=True, exist_ok=True)
        source_path = align_dir / f"{task_id}{suffix}"
        wav_path = align_dir / f"{task_id}.wav"
        source_path.write_bytes(payload)

        task = {
            "id": task_id,
            "name": Path(filename or source_path.name).name,
            "range": f"{chapters[0].get('title') or '起始章'} → {chapters[-1].get('title') or '结束章'}",
            "status": "ready",
            "message": "",
            "lines": 0,
            "segments": 0,
            "created_at": _now(),
        }
        try:
            _convert_upload(source_path, wav_path)
            intervals = _speech_intervals(wav_path)
            task["segments"] = len(intervals)
            if len(intervals) != len(rows):
                task["status"] = "failed"
                task["message"] = (
                    f"切分出 {len(intervals)} 段，台词 {len(rows)} 行，数量不一致未写入；"
                    "可调整章节范围或降低录音中的停顿后重试"
                )
            else:
                folder = store.root / "assets" / "voice" / "lines"
                folder.mkdir(parents=True, exist_ok=True)
                # 逐章重新读取并保存，避免跨章行写入时互相覆盖
                current_chapter = None
                chapter_data: dict | None = None
                for (chapter_id, line), (start_s, end_s) in zip(rows, intervals):
                    if chapter_id != current_chapter:
                        if chapter_data is not None:
                            store.save_chapter(chapter_data)
                        chapter_data = store.load_chapter(chapter_id)
                        current_chapter = chapter_id
                    revision = int(line.get("revision") or 1) + 1
                    out_path = folder / f"{line['id']}_r{revision}.wav"
                    duration_ms = _cut_segment(wav_path, out_path, start_s, end_s)
                    rel = str(out_path.relative_to(store.root))
                    # 写回章节里对应的行
                    for scene in chapter_data.get("scenes") or []:
                        for target in scene.get("lines") or []:
                            if target.get("id") == line.get("id"):
                                _apply_take(store, target, rel, duration_ms, task_id)
                if chapter_data is not None:
                    store.save_chapter(chapter_data)
                task["lines"] = len(rows)
                task["status"] = "ready"
                task["message"] = f"已写入 {len(rows)} 行真人录音版本"
        finally:
            wav_path.unlink(missing_ok=True)

        tasks = _load_tasks(store)
        tasks.insert(0, task)
        _save_tasks(store, tasks)

        if task["status"] == "ready" and config_path is not None:
            # 已有时间轴时重建，让新写入的录音立刻进入时间轴
            from .actions import run

            for chapter in chapters:
                if store.timeline_path(chapter["id"]).exists():
                    run(
                        "--config", config_path, "--project", str(store.root),
                        "--json", "timeline", "build", "--chapter", chapter["id"],
                    )
        return {"ok": True, "task": task}
