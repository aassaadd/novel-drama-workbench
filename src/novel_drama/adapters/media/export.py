"""导出：SRT 字幕 + 时间轴清单（JSON/TSV）。

只使用时间轴与章节数据，不依赖任何模型；字幕与清单都是派生文件。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .timeline_builder import build_schedule, format_schedule_tsv


def _now() -> str:
    """当前 UTC 时间（ISO 8601）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _timestamp(ms: int) -> str:
    """毫秒转 SRT 时间码 HH:MM:SS,mmm。"""
    ms = max(0, int(ms))
    hours, rest = divmod(ms, 3600_000)
    minutes, rest = divmod(rest, 60_000)
    seconds, millis = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def build_srt(timeline: dict, chapter: dict, characters: dict[str, dict] | None = None) -> str:
    """由时间轴生成 SRT 字幕。

    参数:
        timeline: 时间轴 dict。
        chapter: 章节 dict（取台词文本与说话人）。
        characters: {角色 ID: 角色}。

    返回:
        SRT 文本。
    """
    characters = characters or {}
    line_index = {}
    for scene in chapter.get("scenes") or []:
        for line in scene.get("lines") or []:
            line_index[line.get("id")] = line
    voice = next((track for track in timeline.get("tracks") or [] if track.get("kind") == "voice"), None)
    items = sorted((voice or {}).get("items") or [], key=lambda item: item.get("start_ms") or 0)

    blocks = []
    for item in items:
        line = line_index.get(item.get("source_id")) or {}
        text = (line.get("text") or "").strip()
        if not text:
            continue
        speaker = (characters.get(line.get("speaker_id")) or {}).get("name") or ""
        prefix = f"{speaker}：" if speaker and line.get("kind") == "dialogue" else ""
        blocks.append(
            f"{len(blocks) + 1}\n{_timestamp(item.get('start_ms') or 0)} --> {_timestamp(item.get('end_ms') or 0)}\n{prefix}{text}\n"
        )
    return "\n".join(blocks)


def build_manifest(timeline: dict, chapter: dict, characters: dict[str, dict] | None = None) -> dict:
    """生成时间轴清单（逐条时间码）。

    参数:
        timeline: 时间轴 dict。
        chapter: 章节 dict。
        characters: {角色 ID: 角色}。
    """
    characters = characters or {}
    voice = next((track for track in timeline.get("tracks") or [] if track.get("kind") == "voice"), None)
    line_index = {}
    for scene in chapter.get("scenes") or []:
        for line in scene.get("lines") or []:
            line_index[line.get("id")] = line
    entries = []
    for index, item in enumerate(sorted((voice or {}).get("items") or [], key=lambda entry: entry.get("start_ms") or 0), start=1):
        line = line_index.get(item.get("source_id")) or {}
        entries.append(
            {
                "index": index,
                "line_id": item.get("source_id"),
                "scene_id": line.get("scene_id"),
                "speaker_id": line.get("speaker_id"),
                "speaker": (characters.get(line.get("speaker_id")) or {}).get("name"),
                "kind": line.get("kind"),
                "start_ms": int(item.get("start_ms") or 0),
                "end_ms": int(item.get("end_ms") or 0),
                "duration_ms": int((item.get("end_ms") or 0) - (item.get("start_ms") or 0)),
                "estimated": bool(item.get("estimated")),
                "asset_id": item.get("asset_id"),
                "text": line.get("text"),
            }
        )
    return {
        "chapter_id": chapter.get("id"),
        "chapter_title": chapter.get("title"),
        "timeline_id": timeline.get("id"),
        "timeline_revision": timeline.get("revision"),
        "duration_ms": int(timeline.get("duration_ms") or 0),
        "sample_rate": timeline.get("sample_rate"),
        "channels": timeline.get("channels"),
        "entries": entries,
        "built_at": _now(),
    }


def write_export(
    timeline: dict,
    chapter: dict,
    characters: dict[str, dict] | None,
    output_dir: str | Path,
    silence_events: list[dict] | None = None,
) -> dict:
    """写出 SRT、清单 JSON 与时间表 TSV。

    参数:
        timeline: 时间轴 dict。
        chapter: 章节 dict。
        characters: {角色 ID: 角色}。
        output_dir: 输出目录（通常为工程 exports/）。
        silence_events: 导演脚本静音事件（给时间表补说明）。

    返回:
        {srt, manifest, schedule, entries}。
    """
    folder = Path(output_dir)
    folder.mkdir(parents=True, exist_ok=True)
    stem = chapter.get("id") or "chapter"
    srt_path = folder / f"{stem}.srt"
    manifest_path = folder / f"{stem}.manifest.json"
    schedule_path = folder / f"{stem}.schedule.tsv"

    srt_path.write_text(build_srt(timeline, chapter, characters), encoding="utf-8")
    manifest = build_manifest(timeline, chapter, characters)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    rows = build_schedule(timeline, chapter, characters, silence_events)
    schedule_path.write_text(format_schedule_tsv(rows), encoding="utf-8")
    return {
        "srt": str(srt_path),
        "manifest": str(manifest_path),
        "schedule": str(schedule_path),
        "entries": len(manifest["entries"]),
    }
