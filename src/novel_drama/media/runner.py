"""章节媒体批量执行：音效 / 关键图 / 短视频位。

逐条执行、逐条回写状态：单条失败不中断整批，失败信息写进 `asset.error`，
供 `ndw media` 与 Web 页面直接展示。产物统一落 `assets/{sfx,images,video}`。
"""

from __future__ import annotations

from pathlib import Path

from ..config import AppConfig
from ..models.ids import now_iso
from ..store import ProjectStore
from .generate import probe_duration_ms, render_image, render_sfx, render_video

# 每类媒体的产物目录与扩展名
MEDIA_PATHS = {"sfx": ("assets/sfx", ".wav"), "image": ("assets/images", ".png"), "video": ("assets/video", ".mp4")}


def _iter_items(chapter: dict, kind: str):
    """遍历章节内某类媒体条目。

    参数:
        chapter: 章节 dict。
        kind: sfx/image/video。

    产出:
        (scene, item) 二元组。
    """
    key = {"sfx": "sfx_events", "image": "key_images", "video": "video_segments"}[kind]
    for scene in chapter.get("scenes") or []:
        for item in scene.get(key) or []:
            yield scene, item


def _rel(store: ProjectStore, path: Path) -> str:
    """把绝对路径转成工程内相对路径。

    参数:
        store: 工程。
        path: 绝对路径。

    返回:
        相对路径字符串（不在工程内时返回绝对路径）。
    """
    try:
        return str(path.relative_to(store.root))
    except ValueError:
        return str(path)


def _source_image(store: ProjectStore, chapter: dict, segment: dict) -> Path | None:
    """查找视频位的首帧关键图路径。

    参数:
        store: 工程。
        chapter: 章节。
        segment: 视频段。

    返回:
        已就绪关键图路径；没有则 None。
    """
    source_id = segment.get("source_image_id")
    if not source_id:
        return None
    for _, item in _iter_items(chapter, "image"):
        if item.get("id") != source_id:
            continue
        asset = item.get("asset") or {}
        asset_id = asset.get("asset_id")
        if asset.get("status") == "ready" and asset_id:
            candidate = store.root / asset_id
            if candidate.exists():
                return candidate
    return None


def run_media(
    store: ProjectStore,
    config: AppConfig,
    chapter: dict,
    kind: str,
    force: bool = False,
    limit: int | None = None,
    dry_run: bool = False,
) -> dict:
    """执行一章内某类媒体的生成。

    参数:
        store: 工程。
        config: 本机配置。
        chapter: 章节 dict（执行后保存）。
        kind: sfx/image/video。
        force: 已就绪项是否重跑。
        limit: 单次最多处理条数。
        dry_run: 只统计不执行。

    返回:
        {"kind", "total", "ready", "skipped", "failed", "items": [{id, status, path, error}]}。

    异常:
        ValueError: kind 非法。
    """
    if kind not in MEDIA_PATHS:
        raise ValueError(f"未知媒体类型: {kind}（可选 sfx/image/video）")
    rel_dir, ext = MEDIA_PATHS[kind]
    out_dir = store.root / rel_dir
    ffmpeg = config.get("ffmpeg.binary", "ffmpeg")
    sample_rate = int(config.get("output.sample_rate", 48000))

    stats = {"kind": kind, "total": 0, "ready": 0, "skipped": 0, "failed": 0, "items": [], "dry_run": dry_run}
    processed = 0
    for scene, item in _iter_items(chapter, kind):
        stats["total"] += 1
        asset = item.setdefault("asset", {"status": "pending"})
        asset_path = store.root / f"{rel_dir}/{item['id']}{ext}"
        already = asset.get("status") == "ready" and asset_path.exists()
        if already and not force:
            stats["skipped"] += 1
            continue
        if limit is not None and processed >= limit:
            stats["skipped"] += 1
            continue
        processed += 1
        if dry_run:
            stats["items"].append({"id": item["id"], "status": "pending", "path": _rel(store, asset_path), "error": None})
            continue
        try:
            if kind == "sfx":
                result = render_sfx(item, asset_path, binary=ffmpeg, sample_rate=sample_rate)
                asset.update(
                    {
                        "status": "ready",
                        "asset_id": _rel(store, asset_path),
                        "actual_duration_ms": result["duration_ms"],
                        "adapter": "offline-sfx",
                        "cache_key": result["sha256"],
                        "error": None,
                    }
                )
            elif kind == "image":
                result = render_image(item, asset_path)
                asset.update(
                    {
                        "status": "ready",
                        "asset_id": _rel(store, asset_path),
                        "job_id": None,
                        "width": result["width"],
                        "height": result["height"],
                        "error": None,
                    }
                )
                item["width"], item["height"] = result["width"], result["height"]
            else:
                source = _source_image(store, chapter, item)
                result = render_video(item, asset_path, source, binary=ffmpeg)
                asset.update(
                    {
                        "status": "ready",
                        "asset_id": _rel(store, asset_path),
                        "job_id": None,
                        "actual_duration_ms": probe_duration_ms(asset_path) or result["duration_ms"],
                        "error": None,
                    }
                )
            stats["ready"] += 1
            stats["items"].append({"id": item["id"], "status": "ready", "path": _rel(store, asset_path), "error": None})
        except Exception as exc:  # 单条失败不影响整批
            asset.update({"status": "failed", "error": {"code": "RENDER_FAILED", "message": str(exc)}})
            stats["failed"] += 1
            stats["items"].append({"id": item["id"], "status": "failed", "path": None, "error": str(exc)})

    if not dry_run and processed:
        chapter["revision"] = int(chapter.get("revision") or 1) + 1
        chapter["updated_at"] = now_iso()
        store.save_chapter(chapter)
    return stats
