"""把导演脚本里的音效/画面/动画落地成场景实体。

导演脚本（`scripts/<chapter>.json`）是标注结果的规范形态，但时间轴、音效渲染、
配图与视频渲染都只认场景内的 `sfx_events` / `key_images` / `video_segments`。
本模块负责这层「物化」，并保证重复执行幂等：同 ID 条目保留已有产物的状态。
"""

from __future__ import annotations

from ..models import entities
from ..models.ids import now_iso


def _merge_items(scene: dict, key: str, incoming: list[dict]) -> tuple[int, int]:
    """把脚本条目录入场景数组，保留同 ID 条目的已有产物字段。

    参数:
        scene: 场景 dict（就地修改）。
        key: `sfx_events` / `key_images` / `video_segments`。
        incoming: 脚本解析出的条目列表。

    返回:
        (新增数, 更新数)。
    """
    existing = {item.get("id"): item for item in scene.get(key) or []}
    merged = []
    added = updated = 0
    for item in incoming:
        old = existing.get(item.get("id"))
        if old is None:
            merged.append(item)
            added += 1
            continue
        # 保留资产状态与 revisions，仅同步提示词/位置等描述字段
        for field in ("prompt", "kind", "duration_ms", "position", "loop", "anchor", "width", "height", "aspect_ratio", "mode", "source_image_id", "audio_policy"):
            if field in item:
                old[field] = item[field]
        if old.get("revision") is None:
            old["revision"] = item.get("revision") or 1
        merged.append(old)
        updated += 1
    scene[key] = merged
    return added, updated


def materialize_media(chapter: dict, script: dict) -> dict:
    """把导演脚本物化进章节场景。

    参数:
        chapter: 章节 dict（就地修改，由调用方负责保存）。
        script: 导演脚本 dict。

    返回:
        {"sfx_added", "sfx_updated", "image_added", "image_updated",
         "video_added", "video_updated", "scenes"}。
    """
    scenes = {int(scene.get("index") or 0): scene for scene in chapter.get("scenes") or []}
    stats = {
        "sfx_added": 0,
        "sfx_updated": 0,
        "image_added": 0,
        "image_updated": 0,
        "video_added": 0,
        "video_updated": 0,
        "scenes": 0,
    }
    for shot in sorted(script.get("shots") or [], key=lambda item: item.get("index") or 0):
        scene_index = int(shot.get("index") or 1) - 1
        scene = scenes.get(scene_index)
        if scene is None:
            continue
        stats["scenes"] += 1
        anchor_line_id = (shot.get("lines") or [{}])[0].get("id")

        sfx_items = []
        for event in shot.get("sfx_events") or []:
            built = entities.new_sfx_event(
                scene["id"],
                int(event.get("index") or len(sfx_items) + 1),
                event.get("kind") or "ambience",
                event.get("prompt") or "",
                int(event.get("duration_ms") or 3000),
                line_id=(event.get("position") or {}).get("line_id") or anchor_line_id,
            )
            built["id"] = event["id"]
            built["position"] = event.get("position") or built["position"]
            built["anchor"] = event.get("anchor") or built.get("anchor")
            sfx_items.append(built)

        image_items = []
        visual_ids: dict[str, str] = {}
        for visual in shot.get("visuals") or []:
            built = entities.new_key_image(
                scene["id"],
                int(visual.get("index") or len(image_items) + 1),
                anchor_line_id,
                visual.get("prompt") or "",
                int(visual.get("width") or 1152),
                int(visual.get("height") or 1536),
            )
            built["id"] = visual["id"]
            built["aspect_ratio"] = visual.get("aspect_ratio") or built["aspect_ratio"]
            built["workflow_template_id"] = visual.get("workflow_template_id") or built["workflow_template_id"]
            image_items.append(built)
            visual_ids[visual["id"]] = built["id"]

        video_items = []
        for animation in shot.get("animations") or []:
            source = animation.get("visual_id")
            built = entities.new_video_segment(
                scene["id"],
                anchor_line_id,
                animation.get("prompt") or "",
                mode=animation.get("mode") or "t2v",
                source_image_id=visual_ids.get(source) or source,
                duration_ms=int(animation.get("duration_ms") or 5000),
            )
            built["id"] = animation["id"]
            built["camera_move"] = animation.get("camera_move")
            built["workflow_template_id"] = animation.get("workflow_template_id") or built["workflow_template_id"]
            video_items.append(built)

        added, updated = _merge_items(scene, "sfx_events", sfx_items)
        stats["sfx_added"] += added
        stats["sfx_updated"] += updated
        added, updated = _merge_items(scene, "key_images", image_items)
        stats["image_added"] += added
        stats["image_updated"] += updated
        added, updated = _merge_items(scene, "video_segments", video_items)
        stats["video_added"] += added
        stats["video_updated"] += updated
        scene["revision"] = int(scene.get("revision") or 1) + 1

    chapter["updated_at"] = now_iso()
    return stats
