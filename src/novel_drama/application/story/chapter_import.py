"""章节导入：按标题切章、重复导入策略、原文快照与替换保护。

页面（上传纯文本）与 CLI（`chapter import`）共用同一份实现。
"""

from __future__ import annotations

import re

from ...adapters.persistence import ProjectStore
from ...composition import ImportsStore, digest
from ...domain.story.rules.chapter_rules import bump_revision, count_words
from ...tts import line_state


def save_source_snapshot(
    store: ProjectStore, chapter: dict, raw_text: str, source_path: str | None, tool: str
) -> dict:
    """把清洗前的原文存进 `.ndw/imports/`，并把引用与哈希写回章节 JSON。

    参数:
        store: 工程。
        chapter: 章节 dict。
        raw_text: 清洗前的原文（不要传 strip 之后的正文）。
        source_path: 来源文件路径。
        tool: 触发导入的命令名。

    返回:
        快照元信息（含 `deduplicated`）。
    """
    snapshot = ImportsStore(store.root).save(chapter["id"], raw_text, source_path=source_path, tool=tool)
    chapter["source"] = {
        key: snapshot.get(key)
        for key in ("snapshot_id", "file", "sha256", "chars", "imported_at", "source_path", "tool")
    }
    store.save_chapter(chapter)
    return snapshot


def _chapter_work_stats(store: ProjectStore, chapter: dict) -> dict:
    """统计章节里已有的用户工作量（替换前用来判断能不能直接覆盖）。

    参数:
        store: 工程。
        chapter: 章节 dict。

    返回:
        `{"lines": 台词行数, "assets": 已生成音频数}`。
    """
    lines = [line for scene in chapter.get("scenes") or [] for line in scene.get("lines") or []]
    assets = 0
    for line in lines:
        path = line_state._asset_path(store.root, (line.get("tts") or {}).get("asset_id"))
        if path is not None and path.exists():
            assets += 1
    return {"lines": len(lines), "assets": assets}


def find_duplicate(
    chapters: list[dict],
    title: str,
    index: int,
    sha256_value: str,
    source_path: str | None = None,
    imports_index: dict | None = None,
) -> dict | None:
    """找重复章节：哈希 → 同源同标题 → 同序号同标题，依次退让。

    前两层走 `.ndw/imports/index.json`（只有它记着 sha256 与来源路径），
    避免为了比对把几百个章节 JSON 全读一遍；第三层兜住 `chapter add` 建的、没有快照的章节。

    同一文件改过内容后再导入，哈希变了、序号也往后移，只剩「来源文件 + 标题」还认得出是同一章，
    否则会又建一份重复章节——这正是要堵的代价不对称问题。

    参数:
        chapters: 现有章节列表（`list_chapters` 的元信息）。
        title: 本次要落的标题。
        index: 本次要落的序号。
        sha256_value: 本次原文切片的哈希。
        source_path: 本次导入的来源文件绝对路径。
        imports_index: `.ndw/imports/index.json` 的内容（`{chapter_id: 元信息}`）。

    返回:
        命中的已有章节；没有命中返回 None。
    """
    by_id = {item.get("id"): item for item in chapters}
    for chapter_id, meta in (imports_index or {}).items():
        if meta.get("sha256") == sha256_value and chapter_id in by_id:
            return by_id[chapter_id]
    if source_path:
        for chapter_id, meta in (imports_index or {}).items():
            item = by_id.get(chapter_id)
            if item and meta.get("source_path") == source_path and (item.get("title") or "").strip() == title.strip():
                return item
    for item in chapters:
        if item.get("index") == index and (item.get("title") or "").strip() == title.strip():
            return item
    return None


def import_chapters(
    store: ProjectStore,
    text: str,
    source_path: str | None,
    fallback_title: str,
    pattern: str | None = None,
    on_duplicate: str = "skip",
    force: bool = False,
    force_replace: bool = False,
) -> dict:
    """按标题切章导入；重复章节按 `on_duplicate` 处理。

    参数:
        store: 工程。
        text: 待导入全文。
        source_path: 来源文件绝对路径（写进快照与索引，用于二次导入去重）。
        fallback_title: 无法按标题切分时使用的章节标题。
        pattern: 章节标题正则；为空时用默认规则。
        on_duplicate: skip / replace / new。
        force: 等价于 `on_duplicate=new`（保留旧脚本习惯）。
        force_replace: 替换时允许清空已有台词行与音频。

    返回:
        `{"text": 终端文案, "payload": 结构化结果, "code": 0|1}`；
        有章节被跳过或被拒绝替换时 code 为 1。
    """
    pattern_text = pattern or r"^#{1,3}\s+(.+)$"
    matcher = re.compile(pattern_text, re.MULTILINE)
    matches = list(matcher.finditer(text))
    # --force 等价于显式选择 new：保留旧脚本习惯，同时让 `--on-duplicate new` 本身也够明确
    mode = "new" if force else on_duplicate
    made: list[tuple[dict, dict]] = []
    skipped: list[dict] = []
    replaced: list[dict] = []
    refused: list[dict] = []
    slices: list[tuple[str, int, str]] = []
    if not matches:
        slices.append((fallback_title, store.next_chapter_index(), text))
    else:
        # 序号在一次导入内递增，否则一个文件切多章会共用同一个 index（重复判定也会跟着错）
        base = store.next_chapter_index()
        for position, match in enumerate(matches):
            start = match.end()
            end = matches[position + 1].start() if position + 1 < len(matches) else len(text)
            title = match.group(1).strip() if match.groups() else f"第{position + 1}章"
            slices.append((title, base + position, text[start:end]))

    imports_index = ImportsStore(store.root).index()
    for title, index, raw in slices:
        sha = digest(raw)
        duplicate = find_duplicate(store.list_chapters(), title, index, sha, source_path, imports_index)
        if duplicate is None:
            chapter = store.create_chapter(title, content=raw.strip(), index=index)
            made.append((chapter, save_source_snapshot(store, chapter, raw, source_path, "novel import")))
            continue
        if mode == "skip":
            skipped.append({"chapter_id": duplicate["id"], "title": duplicate.get("title")})
            continue
        if mode == "replace":
            # 摘要里没有 scenes，必须读全文才能判断「有没有台词行/音频」，否则会把用户工作静默冲掉
            target = store.load_chapter(duplicate["id"])
            stats = _chapter_work_stats(store, target)
            if (stats["lines"] or stats["assets"]) and not force_replace:
                refused.append(
                    {
                        "chapter_id": duplicate["id"],
                        "title": duplicate.get("title"),
                        "lines": stats["lines"],
                        "assets": stats["assets"],
                    }
                )
                continue
            before = target.get("word_count") or 0
            target["content"] = raw.strip()
            target["word_count"] = count_words(target["content"])
            target["scenes"] = []
            bump_revision(target)
            store.save_chapter(target)
            snap = save_source_snapshot(store, target, raw, source_path, "novel import")
            replaced.append(
                {
                    "chapter_id": target["id"],
                    "title": target.get("title"),
                    "words_before": before,
                    "words_after": target["word_count"],
                    "cleared_lines": stats["lines"],
                    "snapshot": snap,
                }
            )
            continue
        # mode == "new"：显式要求再建一份
        chapter = store.create_chapter(title, content=raw.strip(), index=index)
        made.append((chapter, save_source_snapshot(store, chapter, raw, source_path, "novel import")))

    lines = [
        f"{item['id']}  {item['title']}  {item['word_count']} 字  快照 {snapshot['chars']} 字"
        for item, snapshot in made
    ]
    lines += [f"已存在章节 {item['chapter_id']}（{item['title']}），跳过" for item in skipped]
    lines += [
        f"已替换章节 {item['chapter_id']}（{item['title']}）：{item['words_before']} → {item['words_after']} 字，"
        f"清空 {item['cleared_lines']} 条台词行"
        for item in replaced
    ]
    lines += [
        f"拒绝替换 {item['chapter_id']}（{item['title']}）：已有 {item['lines']} 条台词行 / "
        f"{item['assets']} 个已生成音频；确认覆盖请加 --force-replace"
        for item in refused
    ]
    payload = {
        "mode": mode,
        "chapters": [item for item, _ in made],
        "snapshots": [snapshot for _, snapshot in made],
        "skipped": skipped,
        "replaced": replaced,
        "refused": refused,
    }
    return {
        "text": "\n".join(lines) or "没有可导入的内容",
        "payload": payload,
        "code": 1 if (skipped or refused) else 0,
    }
