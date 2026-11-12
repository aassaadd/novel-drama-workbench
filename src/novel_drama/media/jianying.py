"""剪映草稿导出。

把工程时间轴铺成「分镜级」剪映草稿目录，并把所有引用到的素材复制进草稿内，
最后打包成 zip。产物完全自包含，解压后整个文件夹拷进剪映草稿根目录即可。

设计依据见 `docs/spec-r16-jianying-draft-export.md`。
本模块不做「直接写入草稿目录」、不做成片级布局，只输出 zip。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from .comfy_media import project_ratio

# 剪映 5.9 基线字段：低版本草稿可被高版本剪映打开并自动升级
JY_VERSION = 360000
JY_NEW_VERSION = "110.0.0"
JY_APP_VERSION = "5.9.0"
JY_FPS = 30.0

# 未配置草稿根目录时的默认值（macOS 剪映专业版）
DEFAULT_DRAFTS_ROOT = "~/Movies/JianyingPro/User Data/Projects/com.lveditor.draft"

# 图片素材在剪映里的素材时长固定为 3 小时，实际时长由片段 target_timerange 决定
IMAGE_MATERIAL_DURATION_US = 10_800_000_000

# 工程画幅 -> 草稿画布（宽高必须为偶数）
CANVAS_BY_RATIO = {
    "16:9": (1920, 1080),
    "9:16": (1080, 1920),
    "1:1": (1080, 1080),
    "4:3": (1440, 1080),
    "3:4": (1080, 1440),
}

# 字幕固定样式：白字、黑边、8.0 号、底部居中
SUBTITLE_SIZE = 8.0
SUBTITLE_COLOR = [1.0, 1.0, 1.0]
SUBTITLE_STROKE_COLOR = [0.0, 0.0, 0.0]
SUBTITLE_STROKE_WIDTH = 0.08
SUBTITLE_TRANSFORM_Y = -0.8

# 兜底字幕：文本长度估算耗时（毫秒/字）与最短时长
FALLBACK_MS_PER_CHAR = 220
FALLBACK_MIN_DURATION_MS = 800
SUBTITLE_MIN_DURATION_MS = 300


class JianyingExportError(RuntimeError):
    """剪映草稿导出失败（素材缺失、无可铺内容等）。"""


def export_jianying(
    store,
    chapter: dict,
    timeline: dict,
    drafts_root: str | None = None,
    ffprobe: str = "ffprobe",
    out_dir: str | Path | None = None,
) -> dict:
    """导出剪映草稿目录并打包 zip。

    参数:
        store: 工程存储对象（需要有 root 属性）。
        chapter: 章节数据。
        timeline: 时间轴数据。
        drafts_root: 剪映草稿根目录（仅用于导入说明，不写盘）。
        ffprobe: ffprobe 可执行文件。
        out_dir: 产物根目录，默认 `<工程>/exports/jianying`。

    返回:
        导出结果，包含草稿目录、zip 路径、字幕/素材对账统计与未验证项。
    """
    root = Path(store.root)
    project = store.load_project()
    settings = store.settings()
    ratio = project_ratio(settings)
    width, height = CANVAS_BY_RATIO.get(ratio, CANVAS_BY_RATIO["1:1"])

    draft_name = _draft_name(project, chapter)
    base_dir = Path(out_dir) if out_dir else root / "exports" / "jianying"
    draft_dir = base_dir / draft_name
    media_dir = draft_dir / "media"
    # 素材与草稿元数据里的绝对路径按「用户复制到的最终位置」写，
    # 这样解压后整份拷进草稿根目录即可直接打开（文档 §6.3 方案 B）。
    install_dir = _install_dir(drafts_root, draft_name)

    # 重跑时清空旧草稿目录，避免残留素材影响导入
    if draft_dir.exists():
        shutil.rmtree(draft_dir)
    media_dir.mkdir(parents=True, exist_ok=True)

    plan = _build_plan(root, chapter, timeline)
    assets = _copy_assets(
        root,
        media_dir,
        plan["asset_ids"],
        plan["usage"],
        install_dir=install_dir,
        ffprobe=ffprobe,
    )
    if plan["missing_assets"]:
        raise JianyingExportError(
            "以下素材在工程内不存在，已中止导出（避免生成残缺草稿）：\n  - "
            + "\n  - ".join(plan["missing_assets"])
        )
    if not plan["screen_items"]:
        raise JianyingExportError("工程内没有可用的分镜视频或关键图，无法铺设视频轨")

    content = _build_draft_content(
        chapter=chapter,
        timeline=timeline,
        plan=plan,
        assets=assets,
        draft_name=draft_name,
        width=width,
        height=height,
        ratio=ratio,
    )

    manifest = _build_manifest(
        draft_name=draft_name,
        chapter=chapter,
        ratio=ratio,
        width=width,
        height=height,
        plan=plan,
        assets=assets,
        draft_dir=draft_dir,
        install_dir=install_dir,
        content=content,
        drafts_root=drafts_root,
    )

    content_text = json.dumps(content, ensure_ascii=False, indent=4)
    # 双文件名：11.x 目录式草稿可能优先识别 draft_info.json
    (draft_dir / "draft_content.json").write_text(content_text + "\n", encoding="utf-8")
    (draft_dir / "draft_info.json").write_text(content_text + "\n", encoding="utf-8")
    (draft_dir / "draft_meta_info.json").write_text(
        json.dumps(_build_meta_info(draft_name, draft_dir, install_dir, chapter, timeline, content),
                   ensure_ascii=False, indent=4) + "\n",
        encoding="utf-8",
    )
    (draft_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (draft_dir / "导入说明.txt").write_text(
        _import_readme(draft_name, drafts_root, install_dir), encoding="utf-8"
    )

    zip_path = base_dir / f"{draft_name}.zip"
    _write_zip(draft_dir, zip_path)

    return {
        "draft_name": draft_name,
        "draft_dir": str(draft_dir),
        "install_dir": str(install_dir),
        "zip": str(zip_path),
        "manifest": str(draft_dir / "manifest.json"),
        "canvas": {"width": width, "height": height, "ratio": ratio},
        "counts": manifest["counts"],
        "reconcile": manifest["reconcile"],
        "subtitle_gaps": manifest["subtitle_gaps"],
        "warnings": manifest["warnings"],
        "unverified": manifest["unverified"],
    }


# ---------------------------------------------------------------------------
# 计划：轨道内容、素材引用、字幕兜底
# ---------------------------------------------------------------------------


def _build_plan(root: Path, chapter: dict, timeline: dict) -> dict:
    """把时间轴整理成「画面 / 配音 / 音效 / BGM / 字幕」五类计划项。

    参数:
        root: 工程根目录。
        chapter: 章节数据。
        timeline: 时间轴数据。

    返回:
        包含各类片段计划、素材引用、缺失清单和字幕兜底记录的字典。
    """
    tracks = {track.get("kind"): track for track in timeline.get("tracks") or []}
    screen_items = _screen_layout(
        _items(tracks.get("video"), "video") + _items(tracks.get("image"), "image")
    )
    voice_items = _items(tracks.get("voice"), "voice")
    sfx_items = _items(tracks.get("sfx"), "sfx")
    music_items = _items(tracks.get("music"), "music")

    usage: dict[str, list[dict]] = {}
    for item in screen_items + voice_items + sfx_items:
        asset_id = item.get("asset_id")
        if asset_id:
            usage.setdefault(asset_id, []).append(
                {"track": item.get("_track"), "source_id": item.get("source_id"), "kind": item.get("source_kind")}
            )

    bgm_segments = _bgm_segments(root, chapter, timeline, music_items)
    for cue in bgm_segments:
        if cue.get("asset_id"):
            usage.setdefault(cue["asset_id"], []).append(
                {"track": "music", "source_id": cue.get("cue_id"), "kind": "bgm"}
            )

    subtitles, subtitle_gaps = _subtitles(chapter, voice_items, timeline)
    missing = sorted(asset_id for asset_id in usage if not _resolve(root, asset_id).exists())

    return {
        "screen_items": screen_items,
        "voice_items": voice_items,
        "sfx_items": sfx_items,
        "bgm_segments": bgm_segments,
        "subtitles": subtitles,
        "subtitle_gaps": subtitle_gaps,
        "usage": usage,
        "asset_ids": sorted(usage),
        "missing_assets": missing,
    }


def _items(track: dict | None, track_name: str) -> list[dict]:
    """取出轨道条目，打上轨道标记并过滤掉没有起止时间的异常项。

    参数:
        track: 时间轴轨道。
        track_name: 轨道名（video/image/voice/sfx/music），用于清单标注。

    返回:
        条目列表（每项附 `_track`）。
    """
    if not track:
        return []
    result = []
    for raw in track.get("items") or []:
        item = dict(raw)
        if item.get("end_ms", 0) <= item.get("start_ms", 0):
            continue
        item["_track"] = track_name
        result.append(item)
    return result


def _screen_layout(items: list[dict]) -> list[dict]:
    """把视频段与关键图合并成互不重叠的画面项，重叠项自动分配到新的叠加轨。

    剪映同一条轨道内不允许片段重叠，因此按开始时间贪心分配到最早的可用轨道；
    这里不裁剪任何片段，保证工程里的每个画面素材都进草稿。

    参数:
        items: 视频轨与图片轨条目（带 _track 标记）。

    返回:
        画面项列表，`_layer` 表示落在第几条视频轨（0 为主轨）。
    """
    ordered = []
    for item in items:
        entry = dict(item)
        entry["_layer"] = 0
        ordered.append(entry)
    ordered.sort(key=lambda item: (item.get("start_ms", 0), item.get("end_ms", 0)))

    lane_ends: list[int] = []
    for item in ordered:
        start = int(item.get("start_ms") or 0)
        end = int(item.get("end_ms") or start)
        layer = next((index for index, lane_end in enumerate(lane_ends) if start >= lane_end), None)
        if layer is None:
            layer = len(lane_ends)
            lane_ends.append(0)
        item["_layer"] = layer
        lane_ends[layer] = end
    return ordered


def _assign_layers(items: list[dict]) -> list[tuple[int, dict]]:
    """把可能重叠的片段分配到多条互不重叠的轨道层。

    剪映同一条轨道内不允许片段重叠，因此按开始时间贪心选择「已结束的最早层」；
    本函数不裁剪、不丢弃任何片段，只决定它落在第几层。

    参数:
        items: 含 `start_ms` / `end_ms` 的片段计划列表。

    返回:
        (层号, 条目) 列表，保持入参顺序。
    """
    lane_ends: list[int] = []
    result: list[tuple[int, dict]] = []
    for item in items:
        start = int(item.get("start_ms") or 0)
        end = int(item.get("end_ms") or start)
        lane = next((index for index, lane_end in enumerate(lane_ends) if start >= lane_end), None)
        if lane is None:
            lane = len(lane_ends)
            lane_ends.append(0)
        result.append((lane, item))
        lane_ends[lane] = end
    return result


def _segment_speed(target_us: int, source_us: int) -> float:
    """按 target / source 计算播放倍速，满足 target.duration × speed == source.duration。

    参数:
        target_us: 片段在轨道上的目标时长（微秒）。
        source_us: 实际截取的素材时长（微秒）。

    返回:
        播放倍速；无法计算时返回 1.0。
    """
    if target_us <= 0 or source_us <= 0:
        return 1.0
    return round(source_us / target_us, 6)


def _bgm_segments(
    root: Path, chapter: dict, timeline: dict, music_items: list[dict] | None = None
) -> list[dict]:
    """读取章节 BGM cue 并展开成实际音频片段（循环 cue 自动平铺）。

    参数:
        root: 工程根目录。
        chapter: 章节数据。
        timeline: 时间轴数据。
        music_items: 时间轴音乐轨条目（人工加的音频也要铺进草稿）。

    返回:
        BGM 片段计划列表，每项含 asset_id / start_ms / end_ms / gain_db / fade。
    """
    from .bgm import load_cues

    total_ms = int(timeline.get("duration_ms") or 0)
    segments: list[dict] = []

    # 时间轴音乐轨：按原样铺，不做循环
    for item in music_items or []:
        if not item.get("asset_id"):
            continue
        segments.append(
            {
                "asset_id": item["asset_id"],
                "cue_id": item.get("source_id"),
                "start_ms": int(item.get("start_ms") or 0),
                "end_ms": int(item.get("end_ms") or 0),
                "loop": False,
                "gain_db": float(item.get("gain_db") or 0.0),
                "fade_in_ms": max(0, int(item.get("fade_in_ms") or 0)),
                "fade_out_ms": max(0, int(item.get("fade_out_ms") or 0)),
                "total_ms": total_ms,
            }
        )

    for cue in load_cues(root, chapter.get("id") or ""):
        asset_id = cue.get("asset_id")
        if not asset_id:
            continue
        start = max(0, int(cue.get("start_ms") or 0))
        until = cue.get("loop_until_ms") or total_ms
        until = max(start, int(until))
        segments.append(
            {
                "asset_id": asset_id,
                "cue_id": cue.get("id"),
                "start_ms": start,
                "end_ms": until,
                "loop": bool(cue.get("loop", True)),
                "gain_db": float(cue.get("gain_db", -18.0)),
                "fade_in_ms": max(0, int(cue.get("fade_in_ms") or 0)),
                "fade_out_ms": max(0, int(cue.get("fade_out_ms") or 0)),
                "total_ms": total_ms,
            }
        )
    return segments


def _subtitles(chapter: dict, voice_items: list[dict], timeline: dict) -> tuple[list[dict], list[dict]]:
    """生成覆盖全部有文本台词的字幕计划，缺失时间的行用兜底时间补齐。

    参数:
        chapter: 章节数据。
        voice_items: 时间轴配音条目。
        timeline: 时间轴数据。

    返回:
        (字幕计划, 兜底记录)。兜底记录会写进报告，不会静默丢弃。
    """
    timing = {}
    for item in voice_items:
        source_id = item.get("source_id")
        if source_id:
            timing[source_id] = (int(item.get("start_ms") or 0), int(item.get("end_ms") or 0))

    total_ms = int(timeline.get("duration_ms") or 0)
    subtitles: list[dict] = []
    gaps: list[dict] = []
    cursor = 0

    for scene in chapter.get("scenes") or []:
        for line in scene.get("lines") or []:
            text = (line.get("text") or "").strip()
            if not text:
                continue
            line_id = line.get("id")
            start, end = timing.get(line_id, (None, None))
            reason = None
            if start is None or end is None or end <= start:
                duration = int((line.get("tts") or {}).get("duration_ms") or 0)
                if duration <= 0:
                    duration = max(FALLBACK_MIN_DURATION_MS, len(text) * FALLBACK_MS_PER_CHAR)
                start = cursor
                end = start + duration
                reason = "时间轴缺少该行有效起止时间，已按上一句结束时间顺延估算"
            if end - start < SUBTITLE_MIN_DURATION_MS:
                end = start + SUBTITLE_MIN_DURATION_MS
                reason = reason or "台词时长过短，已补足最短字幕时长"
            subtitles.append(
                {
                    "line_id": line_id,
                    "text": text,
                    "start_ms": int(start),
                    "end_ms": int(end),
                    "speaker_id": line.get("speaker_id"),
                }
            )
            if reason:
                gaps.append({"line_id": line_id, "text": text, "reason": reason, "start_ms": int(start), "end_ms": int(end)})
            cursor = max(cursor, int(end))

    total_ms = max(total_ms, cursor)
    return subtitles, gaps


# ---------------------------------------------------------------------------
# 素材复制与探测
# ---------------------------------------------------------------------------


def _resolve(root: Path, asset_id: str) -> Path:
    """把工程内相对素材路径解析成绝对路径。"""
    path = Path(asset_id)
    return path if path.is_absolute() else root / path


def _copy_assets(
    root: Path,
    media_dir: Path,
    asset_ids: list[str],
    usage: dict[str, list[dict]],
    *,
    install_dir: Path,
    ffprobe: str,
) -> dict:
    """把素材复制进草稿 media 目录并探测媒体信息。

    参数:
        root: 工程根目录。
        media_dir: 草稿 media 目录。
        asset_ids: 需要复制的素材相对路径。
        usage: 素材使用位置，用于清单标注。
        install_dir: 用户最终复制草稿的位置（写入草稿的绝对路径以此为根）。
        ffprobe: ffprobe 可执行文件。

    返回:
        素材信息映射：asset_id -> {path, target_path, rel, duration_ms, width, height, size, usage}。
    """
    assets: dict[str, dict] = {}
    counters: dict[str, int] = {}
    for asset_id in asset_ids:
        source = _resolve(root, asset_id)
        category = _category(asset_id)
        counters[category] = counters.get(category, 0) + 1
        name = f"{counters[category]:03d}_{_safe_filename(Path(asset_id).stem)}{Path(asset_id).suffix}"
        target = media_dir / category / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        info = _probe_media(target, ffprobe)
        rel = target.relative_to(media_dir.parent)
        assets[asset_id] = {
            "path": str(target),
            "target_path": str(install_dir / rel),
            "rel": str(rel),
            "duration_ms": info["duration_ms"],
            "width": info["width"],
            "height": info["height"],
            "size": target.stat().st_size,
            "usage": usage.get(asset_id, []),
        }
    return assets


def _category(asset_id: str) -> str:
    """按素材路径推断草稿内子目录，便于人工检查。"""
    lowered = asset_id.replace("\\", "/").lower()
    if "/voice/" in lowered:
        return "voice"
    if "/sfx/" in lowered:
        return "sfx"
    if "/bgm/" in lowered or "/audio/" in lowered:
        return "music"
    if "/video/" in lowered:
        return "video"
    if "/image" in lowered:
        return "image"
    return "other"


def _safe_filename(name: str) -> str:
    """把文件名里的非法字符替换成下划线。"""
    return re.sub(r'[\\/:*?"<>|\s]+', "_", name).strip("_") or "asset"


def _probe_media(path: Path, ffprobe: str = "ffprobe") -> dict:
    """用 ffprobe 读取媒体时长与尺寸，失败时回落到安全默认值。

    参数:
        path: 媒体文件路径。
        ffprobe: ffprobe 可执行文件。

    返回:
        {duration_ms, width, height}。
    """
    result = {"duration_ms": 0, "width": 0, "height": 0}
    try:
        proc = subprocess.run(
            [
                ffprobe or "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration:stream=width,height,duration",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
        data = json.loads(proc.stdout or "{}")
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return result

    duration = 0.0
    try:
        duration = float((data.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    for stream in data.get("streams") or []:
        if stream.get("width"):
            result["width"] = int(stream["width"])
        if stream.get("height"):
            result["height"] = int(stream["height"])
        if not duration and stream.get("duration"):
            try:
                duration = float(stream["duration"])
            except (TypeError, ValueError):
                pass
    result["duration_ms"] = max(0, int(round(duration * 1000)))
    return result


# ---------------------------------------------------------------------------
# draft_content.json 构建
# ---------------------------------------------------------------------------


def _build_draft_content(
    *,
    chapter: dict,
    timeline: dict,
    plan: dict,
    assets: dict,
    draft_name: str,
    width: int,
    height: int,
    ratio: str,
) -> dict:
    """构建完整的 draft_content.json 内容。

    参数:
        chapter: 章节数据。
        timeline: 时间轴数据。
        plan: 片段计划。
        assets: 已复制素材的信息（含写入草稿的目标绝对路径）。
        draft_name: 草稿名。
        width: 画布宽。
        height: 画布高。
        ratio: 工程画幅。

    返回:
        可直接写盘的草稿内容字典。
    """
    materials = _empty_materials()
    materials["videos"] = []
    materials["audios"] = []
    materials["texts"] = []
    materials["speeds"] = []
    materials["audio_fades"] = []

    material_by_asset: dict[str, str] = {}

    def register(asset_id: str) -> str:
        """为素材登记唯一 material，重复引用时复用。"""
        if asset_id in material_by_asset:
            return material_by_asset[asset_id]
        info = assets[asset_id]
        mid = _uuid()
        if _is_image(asset_id):
            materials["videos"].append(_video_material(mid, asset_id, info, "photo"))
        elif _is_audio(asset_id):
            materials["audios"].append(_audio_material(mid, asset_id, info))
        else:
            materials["videos"].append(_video_material(mid, asset_id, info, "video"))
        material_by_asset[asset_id] = mid
        return mid

    tracks: list[dict] = []
    video_tracks: dict[int, dict] = {}
    audio_tracks: dict[tuple[str, int], dict] = {}
    text_tracks: dict[int, dict] = {}

    def video_track(layer: int) -> dict:
        """按层级创建/复用视频轨。"""
        if layer not in video_tracks:
            video_tracks[layer] = _new_track("video", flag=0 if layer == 0 else 3)
            tracks.append(video_tracks[layer])
        return video_tracks[layer]

    def audio_track(kind: str, lane: int) -> dict:
        """按「类型 + 层」创建/复用音频轨，保证同轨片段不重叠。"""
        key = (kind, lane)
        if key not in audio_tracks:
            track = _new_track("audio", flag=3)
            track["_kind"] = kind
            audio_tracks[key] = track
            tracks.append(track)
        return audio_tracks[key]

    def text_track(lane: int) -> dict:
        """按层创建/复用字幕轨，保证同轨片段不重叠。"""
        if lane not in text_tracks:
            text_tracks[lane] = _new_track("text", flag=3)
            tracks.append(text_tracks[lane])
        return text_tracks[lane]

    # 视频 / 图片
    for item in plan["screen_items"]:
        asset_id = item.get("asset_id")
        if not asset_id:
            continue
        info = assets[asset_id]
        start_us = _us(item.get("start_ms"))
        end_us = _us(item.get("end_ms"))
        duration_us = max(1, end_us - start_us)
        mid = register(asset_id)
        available_us = max(1, info["duration_ms"] * 1000) if info["duration_ms"] else duration_us
        source_us = min(duration_us, available_us)
        speed = _segment_speed(duration_us, source_us)
        speed_id = _add_speed(materials, speed)
        segment = _visual_segment(
            mid,
            start_us,
            duration_us,
            source_us,
            speed_id,
            speed=speed,
            track_index=len(tracks) - 1,
        )
        video_track(int(item.get("_layer") or 0))["segments"].append(segment)

    # 配音：同一时刻可能有多行，按层铺到多条轨道避免同轨重叠
    for lane, item in _assign_layers(plan["voice_items"]):
        asset_id = item.get("asset_id")
        if not asset_id:
            continue
        info = assets[asset_id]
        start_us = _us(item.get("start_ms"))
        end_us = _us(item.get("end_ms"))
        duration_us = max(1, end_us - start_us)
        mid = register(asset_id)
        fade_refs, fade = _fade_material(materials, item)
        available_us = max(1, info["duration_ms"] * 1000) if info["duration_ms"] else duration_us
        source_us = min(duration_us, available_us)
        speed = _segment_speed(duration_us, source_us)
        speed_id = _add_speed(materials, speed)
        segment = _audio_segment(
            mid,
            start_us,
            duration_us,
            source_us,
            speed_id,
            _gain(item),
            len(tracks),
            speed=speed,
            extra_refs=fade_refs,
        )
        audio_track("voice", lane)["segments"].append(segment)

    # 音效：多个音效可能同时出现，同样按层拆分
    for lane, item in _assign_layers(plan["sfx_items"]):
        asset_id = item.get("asset_id")
        if not asset_id:
            continue
        info = assets[asset_id]
        start_us = _us(item.get("start_ms"))
        end_us = _us(item.get("end_ms"))
        duration_us = max(1, end_us - start_us)
        mid = register(asset_id)
        fade_refs, fade = _fade_material(materials, item)
        available_us = max(1, info["duration_ms"] * 1000) if info["duration_ms"] else duration_us
        source_us = min(duration_us, available_us)
        speed = _segment_speed(duration_us, source_us)
        speed_id = _add_speed(materials, speed)
        segment = _audio_segment(
            mid,
            start_us,
            duration_us,
            source_us,
            speed_id,
            _gain(item),
            len(tracks),
            speed=speed,
            extra_refs=fade_refs,
        )
        audio_track("sfx", lane)["segments"].append(segment)

    # BGM（循环 cue 平铺；多条 cue 或音乐轨条目可能交叠，按层拆分）
    for lane, cue in _assign_layers(plan["bgm_segments"]):
        asset_id = cue.get("asset_id")
        if not asset_id:
            continue
        info = assets[asset_id]
        mid = register(asset_id)
        asset_us = max(1, info["duration_ms"] * 1000) if info["duration_ms"] else _us(cue["end_ms"] - cue["start_ms"])
        cursor = _us(cue["start_ms"])
        limit = _us(cue["end_ms"])
        first = True
        while cursor < limit:
            duration_us = min(asset_us, limit - cursor)
            speed_id = _add_speed(materials)
            fade_refs: list[str] = []
            if first:
                fade_refs, _ = _fade_material(materials, cue)
                first = False
            segment = _audio_segment(
                mid,
                cursor,
                duration_us,
                duration_us,
                speed_id,
                _gain(cue),
                len(tracks),
                extra_refs=fade_refs,
            )
            audio_track("music", lane)["segments"].append(segment)
            if not cue.get("loop"):
                break
            cursor += duration_us

    # 字幕：全部有文本的台词都要铺，重叠时拆到另一条字幕轨
    for lane, subtitle in _assign_layers(plan["subtitles"]):
        start_us = _us(subtitle["start_ms"])
        duration_us = max(_us(SUBTITLE_MIN_DURATION_MS), _us(subtitle["end_ms"]) - start_us)
        speed_id = _add_speed(materials)
        material_id = _uuid()
        materials["texts"].append(_text_material(material_id, subtitle["text"]))
        text_track(lane)["segments"].append(
            _text_segment(
                material_id,
                start_us,
                duration_us,
                speed_id,
                len(tracks),
            )
        )

    total_us = max(
        _us(timeline.get("duration_ms")),
        max((seg["target_timerange"]["start"] + seg["target_timerange"]["duration"] for t in tracks for seg in t["segments"]), default=0),
    )

    for index, track in enumerate(tracks):
        track.pop("_kind", None)
        for segment in track["segments"]:
            segment["render_index"] = index

    now = int(datetime.now().timestamp())
    return {
        "canvas_config": {"width": width, "height": height, "ratio": "original"},
        "color_space": 0,
        "config": _default_config(),
        "cover": None,
        "create_time": now,
        "duration": total_us,
        "extra_info": None,
        "fps": JY_FPS,
        "free_render_index_mode_on": False,
        "group_container": None,
        "id": _uuid(),
        "keyframe_graph_list": [],
        "keyframes": {
            "adjusts": [],
            "audios": [],
            "effects": [],
            "filters": [],
            "handwrites": [],
            "stickers": [],
            "texts": [],
            "videos": [],
        },
        "last_modified_platform": _platform(),
        "platform": _platform(),
        "materials": materials,
        "mutable_config": None,
        "name": draft_name,
        "new_version": JY_NEW_VERSION,
        "relationships": [],
        "render_index_track_mode_on": False,
        "retouch_cover": None,
        "source": "default",
        "static_cover_image_path": "",
        "time_marks": None,
        "tracks": tracks,
        "update_time": now,
        "version": JY_VERSION,
    }


def _new_track(track_type: str, flag: int) -> dict:
    """创建一条剪映轨道。"""
    return {
        "id": _uuid(),
        "type": track_type,
        "flag": flag,
        "attribute": 0,
        "name": "",
        "is_default_name": False,
        "segments": [],
    }


def _base_segment(segment_id: str, material_id: str, start_us: int, duration_us: int) -> dict:
    """片段的公共字段。"""
    return {
        "enable_adjust": True,
        "enable_color_correct_adjust": False,
        "enable_color_curves": True,
        "enable_color_match_adjust": False,
        "enable_color_wheels": True,
        "enable_lut": True,
        "enable_smart_color_adjust": False,
        "last_nonzero_volume": 1.0,
        "reverse": False,
        "track_attribute": 0,
        "track_render_index": 0,
        "visible": True,
        "id": segment_id,
        "material_id": material_id,
        "target_timerange": {"start": start_us, "duration": duration_us},
        "common_keyframes": [],
        "keyframe_refs": [],
        "_duration_us": duration_us,
    }


def _visual_segment(
    material_id: str,
    start_us: int,
    duration_us: int,
    source_duration_us: int,
    speed_id: str,
    track_index: int,
    *,
    speed: float = 1.0,
) -> dict:
    """视频/图片片段。"""
    segment = _base_segment(_uuid(), material_id, start_us, duration_us)
    segment.pop("_duration_us", None)
    segment.update(
        {
            "source_timerange": {"start": 0, "duration": source_duration_us},
            "speed": speed,
            "volume": 1.0,
            "extra_material_refs": [speed_id],
            "is_tone_modify": False,
            "clip": _clip(),
            "uniform_scale": {"on": True, "value": 1.0},
            "hdr_settings": {"intensity": 1.0, "mode": 1, "nits": 1000},
            "render_index": track_index,
        }
    )
    return segment


def _audio_segment(
    material_id: str,
    start_us: int,
    duration_us: int,
    source_duration_us: int,
    speed_id: str,
    volume: float,
    track_index: int,
    *,
    speed: float = 1.0,
    extra_refs: list[str] | None = None,
) -> dict:
    """音频片段。"""
    segment = _base_segment(_uuid(), material_id, start_us, duration_us)
    segment.pop("_duration_us", None)
    segment.update(
        {
            "source_timerange": {"start": 0, "duration": source_duration_us},
            "speed": speed,
            "volume": volume,
            "extra_material_refs": [speed_id, *(extra_refs or [])],
            "is_tone_modify": False,
            "clip": None,
            "hdr_settings": None,
            "render_index": track_index,
        }
    )
    return segment


def _text_segment(material_id: str, start_us: int, duration_us: int, speed_id: str, track_index: int) -> dict:
    """字幕片段（无 source_timerange，无 hdr_settings）。"""
    segment = _base_segment(_uuid(), material_id, start_us, duration_us)
    segment.pop("_duration_us", None)
    segment.update(
        {
            "source_timerange": None,
            "speed": 1.0,
            "volume": 1.0,
            "extra_material_refs": [speed_id],
            "is_tone_modify": False,
            "clip": _clip(transform_y=SUBTITLE_TRANSFORM_Y),
            "uniform_scale": {"on": True, "value": 1.0},
            "render_index": track_index,
        }
    )
    return segment


def _clip(transform_y: float = 0.0) -> dict:
    """片段变换设置。"""
    return {
        "alpha": 1.0,
        "flip": {"horizontal": False, "vertical": False},
        "rotation": 0.0,
        "scale": {"x": 1.0, "y": 1.0},
        "transform": {"x": 0.0, "y": transform_y},
    }


def _add_speed(materials: dict, speed: float = 1.0) -> str:
    """新增一个倍速素材并返回 id（剪映要求片段引用 speeds）。

    参数:
        materials: 草稿素材集合。
        speed: 播放倍速，必须与片段 target/source 的比值一致。

    返回:
        倍速素材 id。
    """
    speed_id = _uuid()
    materials["speeds"].append(
        {"curve_speed": None, "id": speed_id, "mode": 0, "speed": speed, "type": "speed"}
    )
    return speed_id


def _fade_material(materials: dict, item: dict) -> tuple[list[str], dict | None]:
    """按条目里的淡入淡出毫秒数生成音频淡化素材。

    参数:
        materials: 草稿素材集合。
        item: 含 fade_in_ms / fade_out_ms 的条目。

    返回:
        (需要挂到片段上的素材 id 列表, 素材对象或 None)。
    """
    fade_in = max(0, int(item.get("fade_in_ms") or 0))
    fade_out = max(0, int(item.get("fade_out_ms") or 0))
    if not fade_in and not fade_out:
        return [], None
    fade_id = _uuid()
    fade = {
        "id": fade_id,
        "fade_in_duration": _us(fade_in),
        "fade_out_duration": _us(fade_out),
        "fade_type": 0,
        "type": "audio_fade",
    }
    materials["audio_fades"].append(fade)
    return [fade_id], fade


def _gain(item: dict) -> float:
    """分贝转线性音量，并限制在 0~4 之间。"""
    try:
        db = float(item.get("gain_db") or 0)
    except (TypeError, ValueError):
        db = 0.0
    return min(4.0, max(0.0, 10 ** (db / 20)))


def _empty_materials() -> dict:
    """剪映素材集合的空骨架。"""
    return {
        "ai_translates": [],
        "audio_balances": [],
        "audio_effects": [],
        "audio_fades": [],
        "audio_track_indexes": [],
        "audios": [],
        "beats": [],
        "canvases": [],
        "chromas": [],
        "color_curves": [],
        "digital_humans": [],
        "drafts": [],
        "effects": [],
        "flowers": [],
        "green_screens": [],
        "handwrites": [],
        "hsl": [],
        "images": [],
        "log_color_wheels": [],
        "loudnesses": [],
        "manual_deformations": [],
        "masks": [],
        "material_animations": [],
        "material_colors": [],
        "multi_language_refs": [],
        "placeholders": [],
        "plugin_effects": [],
        "primary_color_wheels": [],
        "realtime_denoises": [],
        "shapes": [],
        "smart_crops": [],
        "smart_relights": [],
        "sound_channel_mappings": [],
        "speeds": [],
        "stickers": [],
        "tail_leaders": [],
        "text_templates": [],
        "texts": [],
        "time_marks": [],
        "transitions": [],
        "video_effects": [],
        "video_trackings": [],
        "videos": [],
        "vocal_beautifys": [],
        "vocal_separations": [],
    }


def _default_config() -> dict:
    """草稿级配置默认值。"""
    return {
        "adjust_max_index": 1,
        "attachment_info": [],
        "combination_max_index": 1,
        "export_range": None,
        "extract_audio_last_index": 1,
        "lyrics_recognition_id": "",
        "lyrics_sync": True,
        "lyrics_taskinfo": [],
        "maintrack_adsorb": True,
        "material_save_mode": 0,
        "multi_language_current": "none",
        "multi_language_list": [],
        "multi_language_main": "none",
        "multi_language_mode": "none",
        "original_sound_last_index": 1,
        "record_audio_last_index": 1,
        "sticker_max_index": 1,
        "subtitle_keywords_config": None,
        "subtitle_recognition_id": "",
        "subtitle_sync": True,
        "subtitle_taskinfo": [],
        "system_font_list": [],
        "video_mute": False,
        "zoom_info_params": None,
    }


def _platform() -> dict:
    """写入方平台信息（钉 5.9 基线，便于高版本剪映自动升级）。"""
    return {"app_id": 3704, "app_source": "lv", "app_version": JY_APP_VERSION, "os": "mac"}


def _video_material(material_id: str, asset_id: str, info: dict, kind: str) -> dict:
    """视频/图片素材。"""
    return {
        "audio_fade": None,
        "category_id": "",
        "category_name": "local",
        "check_flag": 63487,
        "crop": {
            "upper_left_x": 0.0,
            "upper_left_y": 0.0,
            "upper_right_x": 1.0,
            "upper_right_y": 0.0,
            "lower_left_x": 0.0,
            "lower_left_y": 1.0,
            "lower_right_x": 1.0,
            "lower_right_y": 1.0,
        },
        "crop_ratio": "free",
        "crop_scale": 1.0,
        "duration": IMAGE_MATERIAL_DURATION_US if kind == "photo" else max(1, info["duration_ms"] * 1000),
        "height": info["height"] or 1080,
        "id": material_id,
        "local_material_id": "",
        "material_id": material_id,
        "material_name": Path(asset_id).name,
        "media_path": "",
        "path": info["target_path"],
        "type": kind,
        "width": info["width"] or 1920,
    }


def _audio_material(material_id: str, asset_id: str, info: dict) -> dict:
    """本地音频素材。"""
    return {
        "app_id": 0,
        "category_id": "",
        "category_name": "local",
        "check_flag": 3,
        "copyright_limit_type": "none",
        "duration": max(1, info["duration_ms"] * 1000),
        "effect_id": "",
        "formula_id": "",
        "id": material_id,
        "local_material_id": material_id,
        "music_id": material_id,
        "name": Path(asset_id).name,
        "path": info["target_path"],
        "source_platform": 0,
        "type": "extract_music",
        "wave_points": [],
    }


def _text_material(material_id: str, text: str) -> dict:
    """字幕素材：白字 + 黑边 + 8.0 号 + 居中对齐。"""
    content = {
        "styles": [
            {
                "fill": {
                    "alpha": 1.0,
                    "content": {
                        "render_type": "solid",
                        "solid": {"alpha": 1.0, "color": SUBTITLE_COLOR},
                    },
                },
                "range": [0, len(text)],
                "size": SUBTITLE_SIZE,
                "bold": False,
                "italic": False,
                "underline": False,
                "strokes": [
                    {
                        "content": {"solid": {"alpha": 1.0, "color": SUBTITLE_STROKE_COLOR}},
                        "width": SUBTITLE_STROKE_WIDTH,
                    }
                ],
            }
        ],
        "text": text,
    }
    return {
        "id": material_id,
        "content": json.dumps(content, ensure_ascii=False),
        "typesetting": 0,
        "alignment": 1,
        "letter_spacing": 0.0,
        "line_spacing": 0.02,
        "line_feed": 1,
        "line_max_width": 0.82,
        "force_apply_line_max_width": False,
        "check_flag": 15,
        "type": "text",
        "global_alpha": 1.0,
    }


# ---------------------------------------------------------------------------
# draft_meta_info / 清单 / 说明 / zip
# ---------------------------------------------------------------------------


def _build_meta_info(
    draft_name: str, draft_dir: Path, install_dir: Path, chapter: dict, timeline: dict, content: dict
) -> dict:
    """构建 draft_meta_info.json（路径按用户最终复制位置写）。"""
    return {
        "cloud_package_completed_time": "",
        "draft_cloud_capcut_purchase_info": "",
        "draft_cloud_last_action_download": False,
        "draft_cloud_materials": [],
        "draft_cloud_purchase_info": "",
        "draft_cloud_template_id": "",
        "draft_cloud_tutorial_info": "",
        "draft_cloud_videocut_purchase_info": "",
        "draft_cover": "",
        "draft_deeplink_url": "",
        "draft_enterprise_info": {
            "draft_enterprise_extra": "",
            "draft_enterprise_id": "",
            "draft_enterprise_name": "",
            "enterprise_material": [],
        },
        "draft_fold_path": str(install_dir),
        "draft_id": _uuid(),
        "draft_is_ai_packaging_used": False,
        "draft_is_ai_shorts": False,
        "draft_is_ai_translate": False,
        "draft_is_article_video_draft": False,
        "draft_is_from_deeplink": "false",
        "draft_is_invisible": False,
        "draft_materials": [{"type": item, "value": []} for item in (0, 1, 2, 3, 6, 7, 8)],
        "draft_materials_copied_info": [],
        "draft_name": draft_name,
        "draft_new_version": "",
        "draft_removable_storage_device": "",
        "draft_root_path": str(install_dir.parent),
        "draft_segment_extra_info": [],
        "draft_type": "",
        "tm_draft_cloud_completed": "",
        "tm_draft_cloud_modified": 0,
        "tm_draft_removed": 0,
        "tm_duration": int(content.get("duration") or 0),
    }


def _build_manifest(
    *,
    draft_name: str,
    chapter: dict,
    ratio: str,
    width: int,
    height: int,
    plan: dict,
    assets: dict,
    draft_dir: Path,
    install_dir: Path,
    content: dict,
    drafts_root: str | None,
) -> dict:
    """构建 zip 内的清单，含文件大小与轨道对应关系。"""
    files = []
    path_map = []
    for asset_id, info in assets.items():
        files.append(
            {
                "path": info["rel"],
                "size": info["size"],
                "source": asset_id,
                "target_path": info["target_path"],
                "usage": info["usage"],
            }
        )
        path_map.append(
            {
                "asset_id": asset_id,
                "in_package": info["rel"],
                "at_import": info["target_path"],
            }
        )
    files.sort(key=lambda item: item["path"])
    path_map.sort(key=lambda item: item["in_package"])

    counts = {
        "dialogue_lines": sum(
            len([line for line in scene.get("lines") or [] if (line.get("text") or "").strip()])
            for scene in chapter.get("scenes") or []
        ),
        "subtitles": len(plan["subtitles"]),
        "video_segments": sum(1 for item in plan["screen_items"] if not _is_image(item.get("asset_id") or "")),
        "image_segments": sum(1 for item in plan["screen_items"] if _is_image(item.get("asset_id") or "")),
        "voice_segments": len(plan["voice_items"]),
        "sfx_segments": len(plan["sfx_items"]),
        "bgm_segments": len(plan["bgm_segments"]),
        "assets": len(assets),
        "tracks": len(content.get("tracks") or []),
    }
    return {
        "schema_version": 1,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "draft_name": draft_name,
        "chapter_id": chapter.get("id"),
        "chapter_index": chapter.get("index"),
        "chapter_title": chapter.get("title"),
        "canvas": {"width": width, "height": height, "ratio": ratio, "fps": JY_FPS},
        "export_dir": str(draft_dir),
        "install_dir": str(install_dir),
        "drafts_root": drafts_root or str(install_dir.parent),
        "counts": counts,
        "reconcile": {
            "referenced": len(plan["asset_ids"]),
            "copied": len(assets),
            "missing": plan["missing_assets"],
            "subtitle_expected": counts["dialogue_lines"],
            "subtitle_written": counts["subtitles"],
        },
        "subtitle_gaps": plan["subtitle_gaps"],
        "unvoiced_lines": [
            item.get("source_id")
            for item in plan["voice_items"]
            if not item.get("asset_id")
        ],
        "files": files,
        "path_map": path_map,
        "warnings": plan.get("warnings") or [],
        "unverified": [
            "未在剪映中实测：草稿字段按社区验证过的 5.9 基线生成，剪映 10.8/11.x 打开效果待验证",
            "剪映 6.0+ 加密仅影响读取应用生成的草稿，本方案不读取用户已有草稿",
            "11.x 目录式草稿可能优先读取 draft_info.json，因此双文件名同写（未在 11.x 实测）",
            "素材使用绝对路径指向 zip 解压后的草稿目录，复制到草稿根目录后若改路径会显示媒体丢失",
            "字幕字体使用剪映默认字体，未设置字体文件；未缓存的字体需二次打开草稿",
            "未铺转场/特效/滤镜/关键帧/蒙版（工程无对应数据来源）",
        ],
    }


def _import_readme(draft_name: str, drafts_root: str | None, install_dir: Path) -> str:
    """生成用户导入说明。"""
    root = drafts_root or DEFAULT_DRAFTS_ROOT
    return f"""剪映草稿导入说明

草稿名称：{draft_name}

导入步骤：
1. 把同名 zip 解压到剪映草稿目录下，使草稿文件夹的最终路径正好是：
   {install_dir}
   （macOS 剪映专业版默认草稿目录：{root}）
2. 打开剪映「设置」→「全局设置」，确认「草稿位置」就是上面这个目录。
   不一致时，先在设置页修改「导出 → 剪映草稿根目录」，再重新生成一次。
3. 完全退出剪映（避免运行中写入缓存导致覆盖）。
4. 重新打开剪映，草稿列表中会出现「{draft_name}」，打开即可继续编辑。

注意：
- 请解压出完整文件夹：json 与 media 目录必须一起放在 {install_dir}，只拷 json 会显示「媒体丢失」。
- 解压后不要移动、改名这个草稿文件夹：草稿内的素材路径是绝对路径，直接指向
  {install_dir}；一旦移动或改名，剪映就找不到素材，同样会显示「媒体丢失」。
- 本产物未在剪映中实测，首次打开如遇版本提示，允许剪映升级草稿即可。
"""


def _install_dir(drafts_root: str | None, draft_name: str) -> Path:
    """计算用户最终复制草稿的绝对路径（写入草稿的素材路径以此为根）。

    参数:
        drafts_root: 剪映草稿根目录（可为 ~ 开头的路径）。
        draft_name: 草稿文件夹名。

    返回:
        草稿最终所在目录的绝对路径。
    """
    raw = (drafts_root or DEFAULT_DRAFTS_ROOT).strip() or DEFAULT_DRAFTS_ROOT
    return Path(os.path.abspath(os.path.expanduser(raw))) / draft_name


def _write_zip(draft_dir: Path, zip_path: Path) -> None:
    """把整个草稿目录打包成 zip（zip 内保留顶层草稿文件夹）。"""
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(draft_dir.rglob("*")):
            if path.is_file():
                zf.write(path, f"{draft_dir.name}/{path.relative_to(draft_dir)}")


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _draft_name(project: dict, chapter: dict) -> str:
    """生成草稿文件夹名：书名_章节_短 ID。"""
    title = (((project.get("work") or {}).get("title")) or "未命名").strip()
    chapter_title = (chapter.get("title") or f"第{chapter.get('index') or 0}章").strip()
    short_id = str(chapter.get("id") or "")[-6:] or "000000"
    return _sanitize(f"{title}_{chapter_title}_{short_id}")


def _sanitize(name: str) -> str:
    """清理草稿名里的非法字符与首尾空格。"""
    cleaned = re.sub(r'[\\/:*?"<>|]+', "_", name)
    return re.sub(r"\s+", " ", cleaned).strip()[:80] or "jianying_draft"


def _us(value_ms: Any) -> int:
    """毫秒转微秒。"""
    try:
        return max(0, int(round(float(value_ms or 0) * 1000)))
    except (TypeError, ValueError):
        return 0


def _uuid() -> str:
    """生成剪映风格的 32 位大写十六进制 ID。"""
    return uuid.uuid4().hex.upper()


def _is_image(asset_id: str) -> bool:
    """判断素材是否为图片。"""
    return Path(asset_id).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".bmp"}


def _is_audio(asset_id: str) -> bool:
    """判断素材是否为音频。"""
    return Path(asset_id).suffix.lower() in {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg"}
