"""BGM 轨道：章节级背景音乐 cue 表（独立文件，不塞进 timeline schema）。

每个 cue 描述「哪个音频文件、从第几毫秒开始、增益多少、淡入淡出、是否循环」。
音频文件本身存放在 `assets/audio/bgm/`，cue 表在 `bgm/<chapter_id>.json`。
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from ..models import new_id

_TEMPLATE = {"schema_version": 1, "chapter_id": None, "cues": []}


def cue_path(root: str | Path, chapter_id: str) -> Path:
    """cue 表路径。"""
    return Path(root) / "bgm" / f"{chapter_id}.json"


def load_cues(root: str | Path, chapter_id: str) -> list[dict]:
    """读取章节 BGM cue；缺失时返回空列表。

    参数:
        root: 工程根目录。
        chapter_id: 章节 ID。

    返回:
        cue 列表。
    """
    path = cue_path(root, chapter_id)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return data.get("cues") or []


def save_cues(root: str | Path, chapter_id: str, cues: list[dict]) -> Path:
    """原子写入章节 BGM cue 表。

    参数:
        root: 工程根目录。
        chapter_id: 章节 ID。
        cues: cue 列表。

    返回:
        写入路径。
    """
    path = cue_path(root, chapter_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {**_TEMPLATE, "chapter_id": chapter_id, "cues": cues}
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def import_asset(root: str | Path, source: str | Path) -> str:
    """把外部音频复制进工程 `assets/audio/bgm/`，返回相对路径。

    参数:
        root: 工程根目录。
        source: 外部音频文件。

    返回:
        相对工程根目录的路径。
    """
    src = Path(source).expanduser()
    if not src.exists():
        raise FileNotFoundError(f"BGM 文件不存在: {src}")
    folder = Path(root) / "assets" / "audio" / "bgm"
    folder.mkdir(parents=True, exist_ok=True)
    dst = folder / src.name
    if src.resolve() != dst.resolve():
        shutil.copy2(src, dst)
    return str(dst.relative_to(root))


def sync_cues_from_plans(
    root: str | Path,
    chapter: dict,
    script: dict,
    timeline: dict,
    dry_run: bool = False,
) -> dict:
    """按导演脚本里的 BGM 描述重建 cue，保留手工 cue 与已有选曲。

    参数:
        root: 工程根目录。
        chapter: 章节 dict（取 chapter_id）。
        script: 导演脚本 dict（读取 shots[].bgm）。
        timeline: 时间轴 dict（用 voice 轨道计算行起止毫秒）。
        dry_run: 只计算不写盘。

    返回:
        {"cues": 新 cue 列表, "manual": 保留的手工 cue 数, "planned": 由描述派生的 cue 数}。
    """
    chapter_id = chapter.get("id")
    existing = load_cues(root, chapter_id)
    manual = [cue for cue in existing if not ((cue.get("plan_ref") or {}).get("plan_id"))]
    previous = {
        ((cue.get("plan_ref") or {}).get("plan_id"), int((cue.get("plan_ref") or {}).get("segment_index") or 0)): cue
        for cue in existing
        if (cue.get("plan_ref") or {}).get("plan_id")
    }
    windows = {}
    for track in timeline.get("tracks") or []:
        if track.get("kind") != "voice":
            continue
        for item in track.get("items") or []:
            windows[item.get("source_id")] = (int(item.get("start_ms") or 0), int(item.get("end_ms") or 0))
    chapter_end = int(timeline.get("duration_ms") or 0)

    derived: list[dict] = []
    for shot in sorted(script.get("shots") or [], key=lambda item: item.get("index") or 0):
        plan = shot.get("bgm")
        if not plan:
            continue
        line_ids = [item.get("id") for item in shot.get("lines") or []]
        starts = [windows[line_id][0] for line_id in line_ids if line_id in windows]
        ends = [windows[line_id][1] for line_id in line_ids if line_id in windows]
        shot_start = min(starts) if starts else 0
        shot_end = max(ends) if ends else shot_start
        base_segment = {
            "index": 0,
            "anchor_line_id": None,
            "mood_family": plan.get("mood_family"),
            "narrative_function": plan.get("narrative_function"),
            "energy": plan.get("energy"),
        }
        segments = [base_segment] + list(plan.get("segments") or [])
        boundaries = [
            windows.get(segment.get("anchor_line_id"), (shot_start, shot_end))[0] if segment.get("anchor_line_id") else shot_start
            for segment in segments
        ]
        for position, segment in enumerate(segments):
            key = (plan.get("id"), int(segment.get("index") or 0))
            old = previous.get(key) or {}
            start = max(0, boundaries[position])
            end = boundaries[position + 1] if position + 1 < len(boundaries) else shot_end
            if end <= start:
                end = min(chapter_end, start + int(plan.get("duration_hint_ms") or 30000)) or start + 1000
            asset_id = old.get("asset_id")
            derived.append(
                {
                    "id": old.get("id") or new_id("bgm"),
                    "name": plan.get("name") or shot.get("scene_label") or plan.get("id"),
                    "asset_id": asset_id,
                    "start_ms": start,
                    "gain_db": old.get("gain_db", plan.get("gain_db_hint", -20.0)),
                    "fade_in_ms": old.get("fade_in_ms", plan.get("fade_in_ms", 1500)),
                    "fade_out_ms": old.get("fade_out_ms", plan.get("fade_out_ms", 2000)),
                    "loop": bool(plan.get("loop_required")),
                    "loop_until_ms": end if plan.get("loop_required") else None,
                    "plan_ref": {
                        "plan_id": plan.get("id"),
                        "shot_id": shot.get("id"),
                        "segment_index": int(segment.get("index") or 0) or None,
                    },
                    "mood_family": segment.get("mood_family") or plan.get("mood_family"),
                    "prompt_en": plan.get("prompt_en"),
                    "narrative_function": segment.get("narrative_function") or plan.get("narrative_function"),
                    "status": "asset_ready" if asset_id else "planned",
                }
            )
    merged = sorted(manual + derived, key=lambda item: int(item.get("start_ms") or 0))
    if not dry_run:
        save_cues(root, chapter_id, merged)
    return {"cues": merged, "manual": len(manual), "planned": len(derived)}


def add_cue(
    root: str | Path,
    chapter_id: str,
    asset_id: str,
    start_ms: int = 0,
    gain_db: float = -18.0,
    fade_in_ms: int = 1500,
    fade_out_ms: int = 2000,
    loop: bool = True,
    loop_until_ms: int | None = None,
    mode: str | None = None,
    name: str | None = None,
) -> dict:
    """向章节追加一条 BGM cue。

    参数:
        root: 工程根目录。
        chapter_id: 章节 ID。
        asset_id: 音频相对路径。
        start_ms: 起始时间。
        gain_db: 增益（通常压低到 -18 dB 左右）。
        fade_in_ms: 淡入时长。
        fade_out_ms: 淡出时长。
        loop: 是否循环铺满。
        loop_until_ms: 循环结束时间；为空表示铺到章末。
        mode: 用途档位（narrative / bed），为空时不写该字段。
        name: 展示名称；为空时取文件名。

    返回:
        新 cue。
    """
    cues = load_cues(root, chapter_id)
    cue = {
        "id": new_id("bgm"),
        "name": name or Path(asset_id).stem,
        "asset_id": asset_id,
        "start_ms": max(0, int(start_ms)),
        "gain_db": float(gain_db),
        "fade_in_ms": max(0, int(fade_in_ms)),
        "fade_out_ms": max(0, int(fade_out_ms)),
        "loop": bool(loop),
        "loop_until_ms": int(loop_until_ms) if loop_until_ms else None,
    }
    if mode:
        cue["mode"] = mode
    cues.append(cue)
    save_cues(root, chapter_id, cues)
    return cue
