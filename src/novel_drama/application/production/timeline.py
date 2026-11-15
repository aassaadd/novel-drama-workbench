"""章节时间轴：由章节行数据与导演脚本里的静音事件派生并落盘。

时间轴是派生物，页面、录音对轨与 CLI 都走这同一个用例，避免各自重算口径不同。
"""

from __future__ import annotations

import json

from ...adapters.media.timeline_builder import build_timeline
from ...adapters.persistence import ProjectStore, validate
from ...models.ids import now_iso


def silence_events(store: ProjectStore, chapter_id: str) -> list[dict]:
    """从导演脚本里拍平静音事件（缺失脚本时返回空列表）。

    参数:
        store: 工程。
        chapter_id: 章节 ID。

    返回:
        静音事件列表（已按 shot 展开）。
    """
    path = store.script_path(chapter_id)
    if not path.exists():
        return []
    script = json.loads(path.read_text(encoding="utf-8"))
    events = []
    for shot in script.get("shots") or []:
        events.extend(shot.get("silence_events") or [])
    return events


def build_chapter_timeline(store: ProjectStore, chapter_id: str, dry_run: bool = False) -> dict:
    """构建指定章节的时间轴；已有时间轴时按其口径增量重算。

    参数:
        store: 工程。
        chapter_id: 章节 ID。
        dry_run: True 时只返回计算结果，不写盘、不刷新 built_at。

    返回:
        时间轴 dict（含 `warnings`）。

    异常:
        FileNotFoundError: 章节不存在。
        SchemaValidationError: 时间轴不符合 schema（仅写盘前校验）。
    """
    chapter = store.load_chapter(chapter_id)
    path = store.timeline_path(chapter_id)
    existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    timeline = build_timeline(
        chapter,
        store.settings(),
        existing=existing,
        silence_events=silence_events(store, chapter_id),
    )
    if dry_run:
        return timeline
    timeline["built_at"] = now_iso()
    validate(timeline, "timeline.schema.json")
    store.save_timeline(chapter_id, timeline)
    return timeline
