"""把导演脚本同步进章节：镜头 → 场景、台词 → 台词行，并物化音效/画面/动画。"""

from __future__ import annotations

import re

from ...adapters.persistence import ProjectStore
from ...director.materialize import materialize_media
from ...domain.story.rules.chapter_rules import bump_revision
from ...models import entities
from ...models.ids import now_iso


def _text_key(text: str) -> str:
    """台词比较键：忽略空白与首尾引号差异，用于复用旧行。"""
    value = re.sub(r"\s+", "", str(text or ""))
    return value.strip("“”\"'「」『』")


def apply_script_to_chapter(store: ProjectStore, chapter: dict, script: dict) -> dict:
    """把导演脚本的镜头/台词同步进章节（只增改台词，不动物理产物）。

    参数:
        store: 工程。
        chapter: 章节 dict（就地修改并保存）。
        script: 导演脚本文档。

    返回:
        `{scenes_created, lines_added, lines_updated, sfx_added, images_added, videos_added}`。
    """
    scenes = sorted(chapter.get("scenes") or [], key=lambda item: item.get("index") or 0)
    by_index = {scene.get("index"): scene for scene in scenes}
    line_lookup = {}
    for scene in scenes:
        for line in scene.get("lines") or []:
            line_lookup[line["id"]] = line
    added = updated = created = 0
    for shot in sorted(script.get("shots") or [], key=lambda item: item.get("index") or 0):
        scene_index = int(shot.get("index") or 1) - 1
        scene = by_index.get(scene_index)
        if scene is None:
            scene = entities.new_scene(chapter["id"], scene_index, shot.get("scene_label") or "")
            scene["location"] = shot.get("location")
            scene["time_of_day"] = shot.get("time_of_day") or "unknown"
            scene["mood"] = shot.get("mood")
            scene["goal"] = shot.get("goal")
            chapter.setdefault("scenes", []).append(scene)
            by_index[scene_index] = scene
            created += 1
        for line in shot.get("lines") or []:
            existing = line_lookup.get(line["id"])
            if existing is not None:
                changed = False
                for key in ("speaker_id", "emotion", "kind", "text"):
                    if line.get(key) is not None and existing.get(key) != line[key]:
                        existing[key] = line[key]
                        changed = True
                if changed:
                    existing["revision"] = int(existing.get("revision") or 1) + 1
                    if (existing.get("tts") or {}).get("status") == "ready":
                        existing["tts"]["status"] = "stale"
                    updated += 1
                continue
            # 旧行是 chr_unknown、文本只是空白/引号差异时，按新映射结果更新旧行，避免叠加新行
            key = _text_key(line.get("text"))
            fallback = None
            if key and line.get("speaker_id") not in (None, "", entities.UNKNOWN_SPEAKER_ID):
                for candidate in scene.get("lines") or []:
                    if candidate.get("speaker_id") != entities.UNKNOWN_SPEAKER_ID:
                        continue
                    if _text_key(candidate.get("text")) == key:
                        fallback = candidate
                        break
            if fallback is not None:
                changed = False
                for key_name in ("speaker_id", "emotion", "kind", "text"):
                    if line.get(key_name) is not None and fallback.get(key_name) != line[key_name]:
                        fallback[key_name] = line[key_name]
                        changed = True
                if changed:
                    fallback["revision"] = int(fallback.get("revision") or 1) + 1
                    if (fallback.get("tts") or {}).get("status") == "ready":
                        fallback["tts"]["status"] = "stale"
                    fallback["inferred"] = False
                    updated += 1
                continue
            new_line = entities.new_line(
                scene["id"],
                len(scene.get("lines") or []),
                line["text"],
                kind=line.get("kind") or "dialogue",
                speaker_id=line.get("speaker_id") or entities.UNKNOWN_SPEAKER_ID,
                emotion=line.get("emotion") or "neutral",
            )
            new_line["id"] = line["id"]
            if line.get("anchor"):
                new_line["anchor"] = line["anchor"]
            new_line["inferred"] = False
            scene.setdefault("lines", []).append(new_line)
            line_lookup[new_line["id"]] = new_line
            added += 1
    bump_revision(chapter)
    chapter["updated_at"] = now_iso()
    # 把镜头里的音效/画面/动画物化进场景，供时间轴与渲染使用
    media = materialize_media(chapter, script, store.settings())
    store.save_chapter(chapter)
    return {
        "scenes_created": created,
        "lines_added": added,
        "lines_updated": updated,
        "sfx_added": media["sfx_added"],
        "images_added": media["image_added"],
        "videos_added": media["video_added"],
    }
