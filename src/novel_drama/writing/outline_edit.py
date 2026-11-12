"""大纲编辑：删除单个节拍 / 删除整章（含已落地章节），并保持章节序号连续。

只动大纲数据与章节元信息：

- 删节拍：从设定库大纲里移除该条，重建已落地章节的 `outline` 文本，正文不动；
- 删整章：移除大纲条目，删除已落地章节文件与其派生物（导演脚本、时间轴），
  `assets/` 下的成片素材保留不删；随后把剩余章节重新编号为 1..N。

序号规则与 `outline apply` / `outline continue` 的校验保持一致：不留空洞，
大纲序号与章节文件序号同步前移。
"""

from __future__ import annotations

import re

from ..store import ProjectStore
from ..store.settings_store import SettingsStore

#: 章节删除影响预览的默认结构
_EMPTY_IMPACT = {"chars": 0, "lines": 0, "audio": 0, "image": 0, "video": 0}


def _outline_text(chapter: dict) -> str:
    """按节拍重建章节大纲文本（与 `outline apply` 的写法一致）。

    参数:
        chapter: 大纲章节条目。

    返回:
        多行大纲文本；没有节拍时回退到章节摘要。
    """
    beats = chapter.get("beats") or []
    text = "\n".join(f"{index}. {beat.get('summary') or ''}" for index, beat in enumerate(beats, start=1))
    return text or (chapter.get("summary") or "")


def _media_counts(script: dict) -> dict:
    """统计导演脚本里的台词与音频 / 图片 / 视频条目数。

    兼容镜头（shots）与场景（scenes）两种存储形态；只数条目，不数实际文件。

    参数:
        script: 导演脚本 dict。

    返回:
        `{lines, audio, image, video}`。
    """
    counts = {"lines": 0, "audio": 0, "image": 0, "video": 0}
    for block in script.get("shots") or script.get("scenes") or []:
        if not isinstance(block, dict):
            continue
        counts["lines"] += len(block.get("lines") or [])
        counts["audio"] += len(block.get("sfx_events") or [])
        bgm = block.get("bgm") or {}
        if isinstance(bgm, dict):
            counts["audio"] += len(bgm.get("segments") or [])
        counts["image"] += len(block.get("visuals") or []) + len(block.get("key_images") or [])
        counts["video"] += len(block.get("animations") or []) + len(block.get("video_segments") or [])
    return counts


def chapter_delete_impact(store: ProjectStore, chapter_id: str) -> dict:
    """预览删除某章会影响的内容（用于前端二次确认）。

    参数:
        store: 工程。
        chapter_id: 大纲章节 ID。

    返回:
        `{chapter_id, index, title, landed, chars, lines, audio, image, video}`；
        landed=False 表示尚未落地，只有大纲条目会消失。

    异常:
        ValueError: 设定库不存在或大纲里没有这一章。
    """
    settings_store = SettingsStore(store.root)
    if not settings_store.exists():
        raise ValueError("设定库还没有大纲：先执行 outline generate")
    bible = settings_store.load()
    entry = settings_store.find_outline_chapter(bible, chapter_id)
    if entry is None:
        raise ValueError(f"大纲里没有这一章：{chapter_id}")

    impact = {
        "chapter_id": chapter_id,
        "index": int(entry.get("index") or 0),
        "title": entry.get("title") or "未命名",
        "landed": store.chapter_path(chapter_id).exists(),
        **_EMPTY_IMPACT,
    }
    if not impact["landed"]:
        return impact

    chapter = store.load_chapter(chapter_id)
    impact["chars"] = len(re.sub(r"\s", "", str(chapter.get("content") or "")))
    if store.script_path(chapter_id).exists():
        impact.update(_media_counts(store.load_script(chapter_id)))
    return impact


def _renumber(store: ProjectStore, settings_store: SettingsStore, bible: dict, ignore: set[str] | None = None) -> list[str]:
    """把大纲章节与已落地章节文件重新编号为 1..N，并返回告警。

    参数:
        store: 工程。
        settings_store: 设定库读写。
        bible: 设定库 dict（就地修改）。
        ignore: 计算孤儿章节时要忽略的 ID（正在删除的章节）。

    返回:
        告警列表（例如存在不在大纲中的章节文件）。
    """
    warnings: list[str] = []
    ignored = ignore or set()
    entries = settings_store.all_outline_chapters(bible)
    tracked = set()
    for order, entry in enumerate(entries, start=1):
        entry["index"] = order
        chapter_id = entry.get("id")
        tracked.add(chapter_id)
        if not store.chapter_path(chapter_id).exists():
            continue
        chapter = store.load_chapter(chapter_id)
        if int(chapter.get("index") or 0) == order:
            continue
        chapter["index"] = order
        chapter["revision"] = int(chapter.get("revision") or 1) + 1
        store.save_chapter(chapter)
    orphans = [
        item for item in store.list_chapters() if item.get("id") not in tracked and item.get("id") not in ignored
    ]
    if orphans:
        indexes = "、".join(str(item.get("index")) for item in orphans)
        warnings.append(f"有 {len(orphans)} 个已落地章节不在大纲中（第 {indexes} 章），序号未调整")
    return warnings


def delete_outline_chapter(store: ProjectStore, chapter_id: str) -> dict:
    """删除一个大纲章节；已落地时同时删除章节文件与派生物。

    参数:
        store: 工程。
        chapter_id: 大纲章节 ID。

    返回:
        `{deleted: {id, index, title, landed}, remaining, acts, warnings}`。

    异常:
        ValueError: 设定库不存在或大纲里没有这一章。
    """
    settings_store = SettingsStore(store.root)
    if not settings_store.exists():
        raise ValueError("设定库还没有大纲：先执行 outline generate")
    bible = settings_store.load()
    entry = settings_store.find_outline_chapter(bible, chapter_id)
    if entry is None:
        raise ValueError(f"大纲里没有这一章：{chapter_id}")
    deleted = {
        "id": chapter_id,
        "index": int(entry.get("index") or 0),
        "title": entry.get("title") or "未命名",
        "landed": store.chapter_path(chapter_id).exists(),
    }

    outline = bible.setdefault("outline", {})
    acts = []
    for act in outline.get("acts") or []:
        chapters = [item for item in act.get("chapters") or [] if item.get("id") != chapter_id]
        if not chapters:
            continue  # 幕被删空时一并移除
        act["chapters"] = chapters
        acts.append(act)
    outline["acts"] = acts
    warnings = _renumber(store, settings_store, bible, ignore={chapter_id})
    settings_store.save(bible)

    if deleted["landed"]:
        for path in (
            store.chapter_path(chapter_id),
            store.script_path(chapter_id),
            store.timeline_path(chapter_id),
        ):
            path.unlink(missing_ok=True)
    return {
        "deleted": deleted,
        "remaining": len(settings_store.all_outline_chapters(bible)),
        "acts": len(acts),
        "warnings": warnings,
    }


def delete_beat(store: ProjectStore, chapter_id: str, beat_index: int) -> dict:
    """删除大纲章节里的一个节拍（不影响正文）。

    参数:
        store: 工程。
        chapter_id: 大纲章节 ID。
        beat_index: 节拍序号（从 1 开始）。

    返回:
        `{chapter_id, index, title, beat_index, remaining_beats, landed, warnings}`。

    异常:
        ValueError: 大纲章节不存在或节拍序号越界。
    """
    settings_store = SettingsStore(store.root)
    if not settings_store.exists():
        raise ValueError("设定库还没有大纲：先执行 outline generate")
    bible = settings_store.load()
    entry = settings_store.find_outline_chapter(bible, chapter_id)
    if entry is None:
        raise ValueError(f"大纲里没有这一章：{chapter_id}")
    beats = entry.get("beats") or []
    if not 1 <= int(beat_index) <= len(beats):
        raise ValueError(f"节拍序号越界：{beat_index}（本章共 {len(beats)} 个节拍）")

    removed = beats.pop(int(beat_index) - 1)
    settings_store.save(bible)

    warnings: list[str] = []
    landed = store.chapter_path(chapter_id).exists()
    if landed:
        # 已落地章节只同步大纲文本（chapter.outline），正文 content 不动
        chapter = store.load_chapter(chapter_id)
        chapter["outline"] = _outline_text(entry)
        chapter["revision"] = int(chapter.get("revision") or 1) + 1
        store.save_chapter(chapter)
        warnings.append("已落地章节的正文未改动，仅同步了大纲文本")
    return {
        "chapter_id": chapter_id,
        "index": int(entry.get("index") or 0),
        "title": entry.get("title") or "未命名",
        "beat_index": int(beat_index),
        "removed": str(removed.get("summary") or ""),
        "remaining_beats": len(entry.get("beats") or []),
        "landed": landed,
        "warnings": warnings,
    }
