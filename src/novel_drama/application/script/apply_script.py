"""把导演脚本同步进章节：台本落盘 → 派生场景 → 物化音效/画面/动画。

步骤 3 起台本是内容唯一可写源：本用例先保存台本，再由台本整章派生场景，
不再向场景增量写台词，避免同一行在台本与场景各存一份。
"""

from __future__ import annotations

import re

from ...adapters.persistence import ProjectStore
from ...director.materialize import materialize_media
from ...domain.story.rules.chapter_rules import bump_revision
from ...models import entities
from ...models.ids import now_iso
from ..production.lines_source import read_scenes, scenes_from_script

#: 统计「更新」时比较的内容字段（与改造前的增量同步口径一致）
_COMPARE_FIELDS = ("speaker_id", "emotion", "kind", "text")


def _text_key(text: str) -> str:
    """台词比较键：忽略空白与首尾引号差异，用于复用旧行。"""
    value = re.sub(r"\s+", "", str(text or ""))
    return value.strip("“”\"'「」『』")


def _reuse_fallback_ids(script: dict, scenes: list[dict]) -> None:
    """把标注新生成的行 ID 挂回同场景里「同文本 + 未识别说话人」的旧行。

    标注解析只对完全相同文本复用行 ID；这里补上「空白/引号差异」的兜底，
    让旧行的配音（`tts`）在派生时按行 ID 继续保留。就地修改 `script`。

    参数:
        script: 导演脚本 dict。
        scenes: 改造前的场景视图（用于取候选旧行）。
    """
    by_index = {int(scene.get("index") or 0): scene for scene in scenes}
    used = {line.get("id") for shot in script.get("shots") or [] for line in shot.get("lines") or []}
    for shot in script.get("shots") or []:
        scene = by_index.get(int(shot.get("index") or 1) - 1)
        if scene is None:
            continue
        scene_lines = scene.get("lines") or []
        scene_ids = {item.get("id") for item in scene_lines}
        for line in shot.get("lines") or []:
            if line.get("id") in scene_ids:
                continue
            key = _text_key(line.get("text"))
            if not key or line.get("speaker_id") in (None, "", entities.UNKNOWN_SPEAKER_ID):
                continue
            for candidate in scene_lines:
                candidate_id = candidate.get("id")
                if candidate_id in used:
                    continue
                if candidate.get("speaker_id") != entities.UNKNOWN_SPEAKER_ID:
                    continue
                if _text_key(candidate.get("text")) == key:
                    line["id"] = candidate_id
                    line["inferred"] = False
                    used.add(candidate_id)
                    break


def apply_script_to_chapter(store: ProjectStore, chapter: dict, script: dict) -> dict:
    """把导演脚本落盘并从台本整章派生场景（含媒体物化）。

    参数:
        store: 工程。
        chapter: 章节 dict（就地修改并保存）。
        script: 导演脚本文档（会先校验并写入 `scripts/<chapter>.json`）。

    返回:
        `{scenes_created, lines_added, lines_updated, sfx_added, images_added, videos_added}`。
    """
    before = read_scenes(store, chapter)
    chapter["scenes"] = before
    old_lines = {line["id"]: line for scene in before for line in scene.get("lines") or []}
    old_indexes = {int(scene.get("index") or 0) for scene in before}
    _reuse_fallback_ids(script, before)

    created = len({int(shot.get("index") or 1) - 1 for shot in script.get("shots") or []} - old_indexes)
    added = updated = 0
    for shot in script.get("shots") or []:
        for line in shot.get("lines") or []:
            old = old_lines.get(line.get("id"))
            if old is None:
                added += 1
            elif any(
                line.get(field) is not None and old.get(field) != line[field] for field in _COMPARE_FIELDS
            ):
                updated += 1

    # 台本是唯一可写源：先落盘，再派生场景；场景里按行 ID / 条目 ID 保留生产态与媒体
    store.save_script(script)
    chapter["scenes"] = scenes_from_script(chapter, script)
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
