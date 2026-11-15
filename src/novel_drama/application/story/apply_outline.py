"""用例：把设定库大纲落地成章节文件。"""

from __future__ import annotations

from ...adapters.persistence import ProjectStore
from ...adapters.persistence.settings_store import SettingsStore
from ...domain.story.rules.chapter_rules import bump_revision
from ...models import entities
from ...models.ids import now_iso


def apply_outline(store: ProjectStore, dry_run: bool = False) -> dict:
    """把设定库大纲落地成章节文件。

    已存在的章节只同步标题与大纲文本，不覆盖正文；缺失的按序创建。

    参数:
        store: 工程。
        dry_run: 只统计不落盘。

    返回:
        {"created": [...], "updated": [...], "chapters": N}。

    异常:
        ValueError: 设定库里没有大纲。
    """
    settings_store = SettingsStore(store.root)
    bible = settings_store.ensure()
    entries = settings_store.all_outline_chapters(bible)
    if not entries:
        raise ValueError("设定库还没有大纲：先执行 ndw outline generate")

    project = store.load_project()
    work_id = project["work"]["id"]
    existing_ids = {item["id"] for item in store.list_chapters()}
    created: list[str] = []
    updated: list[str] = []

    for entry in sorted(entries, key=lambda item: int(item.get("index") or 0)):
        outline_text = "\n".join(
            f"{index}. {beat.get('summary')}" for index, beat in enumerate(entry.get("beats") or [], start=1)
        ) or (entry.get("summary") or "")
        if entry["id"] not in existing_ids:
            if dry_run:
                created.append(f"{entry.get('index')} {entry.get('title')}（待创建）")
                continue
            chapter = entities.new_chapter(work_id, int(entry.get("index") or 1), entry.get("title") or "未命名", outline=outline_text)
            chapter["id"] = entry["id"]
            # POV 以设定库为主源，章节文件只是副本：落地时一并写通
            chapter["pov_character_id"] = entry.get("pov_character_id")
            chapter["pov_mode"] = entry.get("pov_mode")
            store.save_chapter(chapter)
            created.append(chapter["id"])
            existing_ids.add(chapter["id"])
        else:
            # 列表接口只返回轻量元信息，写回前必须读完整章节，否则会丢掉 work_id 等必填字段
            chapter = store.load_chapter(entry["id"])
            changed = False
            for key, value in (
                ("title", entry.get("title")),
                ("outline", outline_text),
                ("pov_character_id", entry.get("pov_character_id")),
                ("pov_mode", entry.get("pov_mode")),
            ):
                if key in ("pov_character_id", "pov_mode"):
                    # POV 副本允许为空（全知视角），不能因为空值跳过写通
                    if chapter.get(key) != value:
                        chapter[key] = value
                        changed = True
                    continue
                if value and chapter.get(key) != value:
                    chapter[key] = value
                    changed = True
            if changed:
                bump_revision(chapter)
                chapter["updated_at"] = now_iso()
                if not dry_run:
                    store.save_chapter(chapter)
                updated.append(chapter["id"])
    return {"created": created, "updated": updated, "chapters": len(entries), "dry_run": dry_run}

def _chapter_beats(bible: dict, chapter: dict, settings_store: SettingsStore) -> list[dict]:
    """取章节对应的节拍；大纲中没有时从 chapter.outline 文本回退拆解。

    参数:
        bible: 设定库。
        chapter: 章节。
        settings_store: 设定库读写。

    返回:
        节拍列表（至少一项）。
    """
    entry = settings_store.find_outline_chapter(bible, chapter["id"], index=int(chapter.get("index") or 0))
    if entry and entry.get("beats"):
        return entry["beats"]
    outline = (chapter.get("outline") or "").strip()
    if outline:
        lines = [line.strip(" .、0123456789") for line in outline.splitlines() if line.strip()]
        if lines:
            return [{"summary": line, "character_ids": [], "turning_point": index == len(lines) - 1, "target_words": 0} for index, line in enumerate(lines)]
    return [{"summary": chapter.get("title") or "推进主线", "character_ids": [], "turning_point": False, "target_words": 0}]
