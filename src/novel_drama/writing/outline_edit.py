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
from ..text.titles import strip_index_prefix

#: 章节删除影响预览的默认结构
_EMPTY_IMPACT = {"chars": 0, "lines": 0, "audio": 0, "image": 0, "video": 0}

#: 可编辑文本字段与长度上限（与 setting-bible.schema.json 保持一致）
_TEXT_LIMITS = {
    "act": {"title": 200, "summary": 2000},
    "chapter": {"title": 200, "summary": 2000},
    "beat": {"summary": 1000},
}

#: 字段的中文名，用于错误提示里指明是哪个字段
_TEXT_LABELS = {
    "act": {"title": "幕标题", "summary": "幕概要"},
    "chapter": {"title": "章标题", "summary": "章概要"},
    "beat": {"summary": "节拍文本"},
}


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


def _impact_for_entry(store: ProjectStore, entry: dict) -> dict:
    """统计单个大纲章节条目被删除时的影响。

    参数:
        store: 工程。
        entry: 大纲章节条目。

    返回:
        `{chapter_id, index, title, landed, chars, lines, audio, image, video}`。
    """
    chapter_id = str(entry.get("id") or "")
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
    return _impact_for_entry(store, entry)


def _find_act(bible: dict, act_id: str) -> dict | None:
    """在设定库大纲里按幕 ID 查找。

    参数:
        bible: 设定库 dict。
        act_id: 幕 ID。

    返回:
        幕条目；找不到返回 None。
    """
    for act in (bible.get("outline") or {}).get("acts") or []:
        if str(act.get("id") or "") == str(act_id):
            return act
    return None


def act_delete_impact(store: ProjectStore, act_id: str) -> dict:
    """预览删除整幕会影响的内容（按幕汇总，前端据此二次确认）。

    参数:
        store: 工程。
        act_id: 幕 ID。

    返回:
        `{act_id, index, title, chapters: [...], landed_count, totals}`；
        totals 为正文合计 / 台词合计 / 音频 / 图片 / 视频条目合计。

    异常:
        ValueError: 设定库不存在或大纲里没有这一幕。
    """
    settings_store = SettingsStore(store.root)
    if not settings_store.exists():
        raise ValueError("设定库还没有大纲：先执行 outline generate")
    bible = settings_store.load()
    act = _find_act(bible, act_id)
    if act is None:
        raise ValueError(f"大纲里没有这一幕：{act_id}")
    # 幕没有 schema 序号字段，展示用的序号取数组位置（从 1 开始）
    acts = (bible.get("outline") or {}).get("acts") or []
    display_index = next((i + 1 for i, item in enumerate(acts) if item is act), 0)
    chapters = [_impact_for_entry(store, entry) for entry in act.get("chapters") or []]
    totals = {key: sum(item[key] for item in chapters) for key in _EMPTY_IMPACT}
    return {
        "act_id": str(act.get("id") or ""),
        "index": display_index,
        "title": act.get("title") or "未命名幕",
        "chapters": chapters,
        "landed_count": sum(1 for item in chapters if item["landed"]),
        "totals": totals,
    }



def _renumber_outline(settings_store: SettingsStore, bible: dict) -> list[dict]:
    """只在内存里把大纲章节序号重排为 1..N。

    幕按数组顺序表达，schema 不允许幕上出现 `index` 字段，因此这里不碰幕；
    章的 `index` 是 schema 必填字段，需要一起重排。

    参数:
        settings_store: 设定库读写。
        bible: 设定库 dict（就地修改）。

    返回:
        按幕顺序展开的全部章节条目（已重排序号）。
    """
    entries = settings_store.all_outline_chapters(bible)
    for order, entry in enumerate(entries, start=1):
        entry["index"] = order
    return entries


def _sync_chapter_indexes(store: ProjectStore, entries: list[dict], ignore: set[str] | None = None) -> list[str]:
    """把大纲序号同步到已落地章节文件，并扫描不在大纲中的孤儿章节。

    只在设定库校验通过、落盘之后调用，避免校验失败时章节文件已被改写。

    参数:
        store: 工程。
        entries: 重排后的大纲章节条目。
        ignore: 孤儿扫描时忽略的章节 ID（正在删除的章节）。

    返回:
        告警列表（例如存在不在大纲中的章节文件）。
    """
    warnings: list[str] = []
    ignored = ignore or set()
    tracked: set[str] = set()
    for order, entry in enumerate(entries, start=1):
        chapter_id = str(entry.get("id") or "")
        tracked.add(chapter_id)
        if not store.chapter_path(chapter_id).exists():
            continue
        chapter = store.load_chapter(chapter_id)
        if int(chapter.get("index") or 0) == order:
            continue
        chapter["index"] = order
        chapter["revision"] = int(chapter.get("revision") or 1) + 1
        store.save_chapter(chapter)

    # 孤儿章节用容错扫描：单个文件损坏或缺失不应打断整批删除
    orphans: list[str] = []
    chapter_dir = store.root / "chapters"
    if chapter_dir.exists():
        for path in sorted(chapter_dir.glob("*.json")):
            chapter_id = path.stem
            if chapter_id in tracked or chapter_id in ignored:
                continue
            try:
                orphans.append(str(store.load_chapter(chapter_id).get("index")))
            except Exception:
                orphans.append("?")

    if orphans:
        indexes = "、".join(orphans)
        warnings.append(f"有 {len(orphans)} 个已落地章节不在大纲中（第 {indexes} 章），序号未调整")
    return warnings


def _delete_files(store: ProjectStore, chapter_id: str, was_landed: bool) -> list[str]:
    """删除章节文件与派生物（脚本、时间轴）；缺失或失败只告警，不中断。

    参数:
        store: 工程。
        chapter_id: 章节 ID。
        was_landed: 预览时章节文件是否存在。

    返回:
        告警列表。
    """
    warnings: list[str] = []
    chapter_path = store.chapter_path(chapter_id)
    if was_landed and not chapter_path.exists():
        warnings.append(f"第 {chapter_id} 章的章节文件已不存在（可能已被手动删除）")
    targets = [(chapter_path, "章节文件"), (store.script_path(chapter_id), "导演脚本"), (store.timeline_path(chapter_id), "时间轴")]
    for path, label in targets:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            warnings.append(f"第 {chapter_id} 章的{label}删除失败：{exc}")
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
    warnings: list[str] = []
    acts = []
    for act in outline.get("acts") or []:
        chapters = [item for item in act.get("chapters") or [] if item.get("id") != chapter_id]
        if not chapters:
            continue  # 幕被删空时一并移除
        act["chapters"] = chapters
        acts.append(act)
    outline["acts"] = acts
    entries = _renumber_outline(settings_store, bible)
    # 先校验并原子落盘：校验失败时章节文件尚未改动，不会留下半写状态
    settings_store.save(bible)
    warnings.extend(_sync_chapter_indexes(store, entries, ignore={chapter_id}))

    if deleted["landed"]:
        warnings.extend(_delete_files(store, chapter_id, deleted["landed"]))
    return {
        "deleted": deleted,
        "remaining": len(settings_store.all_outline_chapters(bible)),
        "acts": len(acts),
        "warnings": warnings,
    }


def delete_outline_act(store: ProjectStore, act_id: str) -> dict:
    """删除整幕及其下所有章节（已落地章节连同文件一起删）。

    参数:
        store: 工程。
        act_id: 幕 ID。

    返回:
        `{deleted: {id, index, title, chapters, landed_count}, remaining_chapters,
        remaining_acts, warnings}`。

    异常:
        ValueError: 设定库不存在或大纲里没有这一幕。
    """
    settings_store = SettingsStore(store.root)
    if not settings_store.exists():
        raise ValueError("设定库还没有大纲：先执行 outline generate")
    bible = settings_store.load()
    act = _find_act(bible, act_id)
    if act is None:
        raise ValueError(f"大纲里没有这一幕：{act_id}")
    # 幕没有 schema 序号字段，展示用的序号取数组位置（从 1 开始）
    acts_now = (bible.get("outline") or {}).get("acts") or []
    display_index = next((i + 1 for i, item in enumerate(acts_now) if item is act), 0)
    items = list(act.get("chapters") or [])
    warnings: list[str] = []
    impacts: list[dict] = []
    broken: list[str] = []
    for entry in items:
        try:
            impacts.append(_impact_for_entry(store, entry))
        except Exception as exc:  # 章节文件缺失/损坏：只告警，继续删其余章节
            chapter_id = str(entry.get("id") or "")
            broken.append(chapter_id)
            warnings.append(f"第 {entry.get('index') or '?'} 章的章节文件读取失败（可能已缺失）：{exc}")
    deleted = {
        "id": str(act.get("id") or ""),
        "index": display_index,
        "title": act.get("title") or "未命名幕",
        "chapters": [str(entry.get("id") or "") for entry in items],
        "landed_count": sum(1 for item in impacts if item["landed"]),
    }

    outline = bible.setdefault("outline", {})
    outline["acts"] = [
        item for item in outline.get("acts") or [] if str(item.get("id") or "") != str(act_id)
    ]
    entries = _renumber_outline(settings_store, bible)
    # 先校验并原子落盘：校验失败时章节文件尚未改动，不会留下半写状态
    settings_store.save(bible)
    warnings.extend(_sync_chapter_indexes(store, entries, ignore=set(deleted["chapters"])))

    # 逐个删文件：缺失或失败只告警，不影响其余章节
    for impact in impacts:
        if impact["landed"]:
            warnings.extend(_delete_files(store, impact["chapter_id"], True))
    for chapter_id in broken:
        warnings.extend(_delete_files(store, chapter_id, False))
    return {
        "deleted": deleted,
        "remaining_chapters": len(settings_store.all_outline_chapters(bible)),
        "remaining_acts": len(outline["acts"]),
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


def _clean_text(scope: str, field: str, raw) -> str:
    """清洗并校验一个可编辑文本字段。

    参数:
        scope: 编辑对象（act / chapter / beat）。
        field: 字段名（title / summary）。
        raw: 原始输入。

    返回:
        去掉首尾空白后的文本；标题会顺便剥掉模型序号前缀。

    异常:
        ValueError: 标题为空或文本超长，消息里带字段名与原因。
    """
    label = _TEXT_LABELS[scope][field]
    value = str(raw or "").strip()
    if field == "title":
        value = strip_index_prefix(value)
        if not value:
            raise ValueError(f"{label}不能为空（不能只写序号）")
    limit = _TEXT_LIMITS[scope][field]
    if len(value) > limit:
        raise ValueError(f"{label}过长：最多 {limit} 字，当前 {len(value)} 字")
    return value


def update_outline_text(
    store: ProjectStore,
    act: str | None = None,
    chapter: str | None = None,
    beat: int | str | None = None,
    fields: dict | None = None,
) -> dict:
    """就地修改大纲文本（幕/章标题与概要、单个节拍文本）。

    只改设定库大纲里的文本：不动章节序号、节拍顺序，不写章节文件，也不碰正文与标注。
    保存沿用 `SettingsStore.save` 的 schema 校验 + 原子写，失败时不会留下半写状态。

    参数:
        store: 工程。
        act: 幕 ID（编辑幕时传）。
        chapter: 大纲章节 ID（编辑章或节拍时传）。
        beat: 节拍序号（从 1 开始，编辑节拍时传）。
        fields: 要更新的字段，例如 `{"title": "...", "summary": "..."}`。

    返回:
        `{scope, act_id?, chapter_id?, index?, beat?, title?, updated, settings_revision}`。

    异常:
        ValueError: 设定库不存在、对象找不到、字段不支持、标题为空或文本超长。
    """
    settings_store = SettingsStore(store.root)
    if not settings_store.exists():
        raise ValueError("设定库还没有大纲：先执行 outline generate")
    bible = settings_store.load()

    values = {str(key): value for key, value in (fields or {}).items()}
    if not values:
        raise ValueError("没有需要保存的字段")

    act_id = str(act or "").strip()
    chapter_id = str(chapter or "").strip()
    try:
        beat_index = None if beat in (None, "") else int(beat)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"节拍序号必须是整数：{beat}") from exc

    if beat_index is not None:
        scope = "beat"
        if not chapter_id:
            raise ValueError("编辑节拍需要同时提供 chapter（大纲章节 ID）")
    elif chapter_id:
        scope = "chapter"
    elif act_id:
        scope = "act"
    else:
        raise ValueError("缺少要编辑的对象：act / chapter / beat 至少给一个")

    allowed = _TEXT_LIMITS[scope]
    unknown = [key for key in values if key not in allowed]
    if unknown:
        raise ValueError(
            f"不支持的字段：{'、'.join(unknown)}（{scope} 只支持 {'、'.join(allowed)}）"
        )

    if scope == "act":
        entry = _find_act(bible, act_id)
        if entry is None:
            raise ValueError(f"大纲里没有这一幕：{act_id}")
    else:
        entry = settings_store.find_outline_chapter(bible, chapter_id)
        if entry is None:
            raise ValueError(f"大纲里没有这一章：{chapter_id}")

    cleaned = {key: _clean_text(scope, key, value) for key, value in values.items()}
    if scope == "beat":
        beats = entry.get("beats") or []
        if not 1 <= beat_index <= len(beats):
            raise ValueError(f"节拍序号越界：{beat_index}（本章共 {len(beats)} 个节拍）")
        beats[beat_index - 1]["summary"] = cleaned["summary"]
    else:
        entry.update(cleaned)

    saved = settings_store.save(bible)
    result = {
        "scope": scope,
        "updated": sorted(cleaned),
        "settings_revision": saved.get("settings_revision"),
    }
    if scope == "act":
        acts = (bible.get("outline") or {}).get("acts") or []
        result["act_id"] = act_id
        result["index"] = next((i + 1 for i, item in enumerate(acts) if item is entry), 0)
        result["title"] = entry.get("title") or ""
    else:
        result["chapter_id"] = chapter_id
        result["index"] = int(entry.get("index") or 0)
        result["title"] = entry.get("title") or ""
    if scope == "beat":
        result["beat"] = beat_index
        result["summary"] = cleaned["summary"]
    return result


def inspect_outline(store: ProjectStore) -> dict:
    """只读检查设定库：schema 是否合法、章序号是否连续、幕上是否混入 index。

    不写盘，可用于排查删除失败后数据是否被改坏。

    参数:
        store: 工程。

    返回:
        `{exists, valid, errors, acts, chapters, chapter_indexes, continuous,
        act_extra_index, needs_fix}`；needs_fix 为 True 时需要人工修复。
    """
    settings_store = SettingsStore(store.root)
    if not settings_store.exists():
        return {
            "exists": False,
            "valid": False,
            "errors": ["设定库不存在（settings/bible.json）"],
            "acts": 0,
            "chapters": 0,
            "chapter_indexes": [],
            "continuous": True,
            "act_extra_index": [],
            "needs_fix": True,
        }

    bible = settings_store.load()
    errors: list[str] = []
    try:
        settings_store.validate(bible)
    except Exception as exc:
        errors.append(str(exc))
    acts = (bible.get("outline") or {}).get("acts") or []
    chapters = [item for act in acts for item in act.get("chapters") or []]
    indexes = [int(item.get("index") or 0) for item in chapters]
    expected = list(range(1, len(chapters) + 1))
    # 幕按数组顺序表达，schema 不允许幕上出现 index
    act_extra = [index for index, act in enumerate(acts) if "index" in act]
    return {
        "exists": True,
        "valid": not errors,
        "errors": errors,
        "acts": len(acts),
        "chapters": len(chapters),
        "chapter_indexes": indexes,
        "continuous": indexes == expected,
        "act_extra_index": act_extra,
        "needs_fix": bool(errors) or indexes != expected or bool(act_extra),
    }
