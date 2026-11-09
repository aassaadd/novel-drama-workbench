"""章节 CLI：增删查、按标题导入（重复可控）、原文快照与对照。

从 `cli.py` 拆出，纯搬迁：命令名、参数、输出格式、退出码一律未改动。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .cli_common import EXIT_OK, EXIT_REJECTED, _emit, _resolve_store
from .config import AppConfig
from .store.imports_store import ImportsStore, compare_texts, digest, preview, split_paragraphs
from .tts import line_state


# ---------------- chapter ----------------


def _save_source_snapshot(store, chapter: dict, raw_text: str, source_path: str | None, tool: str) -> dict:
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


def cmd_chapter_add(args, config: AppConfig) -> int:
    """新增章节；给了 `--content-file` 时同时落一份原文快照。"""
    store = _resolve_store(args, config)
    raw = Path(args.content_file).read_text(encoding="utf-8") if args.content_file else ""
    chapter = store.create_chapter(args.title, content=raw, outline=args.outline or "", index=args.index)
    note = ""
    if raw:
        snapshot = _save_source_snapshot(store, chapter, raw, str(Path(args.content_file).resolve()), "novel add")
        note = f"；原文快照 {snapshot['file']}（{snapshot['chars']} 字）"
    _emit(
        args,
        f"章节已创建: {chapter['id']}  第{chapter['index']}章 {chapter['title']}（{chapter['word_count']} 字）{note}",
        chapter,
    )
    return EXIT_OK


def cmd_chapter_list(args, config: AppConfig) -> int:
    """列出章节。"""
    store = _resolve_store(args, config)
    chapters = store.list_chapters()
    if getattr(args, "json", False):
        _emit(args, payload=chapters)
        return EXIT_OK
    if not chapters:
        print("暂无章节")
    for item in chapters:
        print(f"[{item['index']:>2}] {item['id']}  {item['title']}  状态={item['status']}  场景={item['scene_count']}  字数={item['word_count']}")
    return EXIT_OK


def cmd_chapter_show(args, config: AppConfig) -> int:
    """输出章节 JSON。"""
    store = _resolve_store(args, config)
    chapter = store.load_chapter(args.chapter)
    print(json.dumps(chapter, ensure_ascii=False, indent=2))
    return EXIT_OK


def _chapter_work_stats(store, chapter: dict) -> dict:
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


def _find_duplicate(
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


def cmd_chapter_import(args, config: AppConfig) -> int:
    """导入 txt/markdown 并按标题切章（重复导入可控）。

    参数:
        args: 含 file / pattern / on_duplicate / force / force_replace。
        config: 本机配置。

    返回:
        退出码：有章节被跳过或被拒绝替换时返回 1（未改动的那部分），全部新建/替换成功返回 0。
    """
    store = _resolve_store(args, config)
    text = Path(args.file).read_text(encoding="utf-8")
    source_path = str(Path(args.file).resolve())
    pattern = re.compile(args.pattern, re.MULTILINE)
    matches = list(pattern.finditer(text))
    # --force 等价于显式选择 new：保留旧脚本习惯，同时让 `--on-duplicate new` 本身也够明确
    mode = "new" if args.force else args.on_duplicate
    made: list[tuple[dict, dict]] = []
    skipped, replaced, refused = [], [], []
    slices: list[tuple[str, int, str]] = []
    if not matches:
        slices.append((Path(args.file).stem, store.next_chapter_index(), text))
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
        duplicate = _find_duplicate(store.list_chapters(), title, index, sha, source_path, imports_index)
        if duplicate is None:
            chapter = store.create_chapter(title, content=raw.strip(), index=index)
            made.append((chapter, _save_source_snapshot(store, chapter, raw, source_path, "novel import")))
            continue
        if mode == "skip":
            skipped.append({"chapter_id": duplicate["id"], "title": duplicate.get("title")})
            continue
        if mode == "replace":
            # 摘要里没有 scenes，必须读全文才能判断「有没有台词行/音频」，否则会把用户工作静默冲掉
            target = store.load_chapter(duplicate["id"])
            stats = _chapter_work_stats(store, target)
            if (stats["lines"] or stats["assets"]) and not args.force_replace:
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
            target["word_count"] = len("".join(target["content"].split()))
            target["scenes"] = []
            target["revision"] = int(target.get("revision") or 1) + 1
            store.save_chapter(target)
            snap = _save_source_snapshot(store, target, raw, source_path, "novel import")
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
        made.append((chapter, _save_source_snapshot(store, chapter, raw, source_path, "novel import")))

    lines = [f"{item['id']}  {item['title']}  {item['word_count']} 字  快照 {snapshot['chars']} 字"
             for item, snapshot in made]
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
    _emit(args, "\n".join(lines) or "没有可导入的内容", payload)
    return EXIT_REJECTED if (skipped or refused) else EXIT_OK


def cmd_chapter_source(args, config: AppConfig) -> int:
    """查看原文快照，或把它与当前章节正文对照。

    三种用法：
    - 默认：输出快照元信息与段落数；
    - `--diff`：按段落对齐，报 missing / added / modified；
    - `--at 偏移` 或 `--paragraph 序号`：回答「这一段原文去哪了」。
    """
    store = _resolve_store(args, config)
    chapter = store.load_chapter(args.chapter)
    text, meta = ImportsStore(store.root).load(chapter["id"])
    if text is None:
        payload = {
            "available": False,
            "chapter": chapter["id"],
            "hint": "该章节没有原文快照（功能上线前导入）。可重跑 novel import 补录。",
        }
        _emit(args, f"{chapter['id']} 没有原文快照；重新导入一次即可补上", payload)
        return EXIT_OK
    paragraphs = split_paragraphs(text)
    current_text = chapter.get("content") or ""
    comparison = compare_texts(text, current_text)

    if args.at is not None or args.paragraph is not None:
        return _emit_source_locate(args, chapter, meta, text, paragraphs, comparison)
    if args.diff:
        summary = comparison["summary"]
        payload = {
            "available": True,
            "chapter": chapter["id"],
            "snapshot": meta,
            "summary": summary,
            "items": comparison["items"],
        }
        head = (
            f"{chapter['id']} 原文 {comparison['paragraphs']} 段 / 当前 {comparison['current_paragraphs']} 段："
            f"missing {summary['missing']} / added {summary['added']} / modified {summary['modified']}"
            f" / 一致 {summary['matched']}"
        )
        lines = [head]
        for item in comparison["items"]:
            where = (
                f"原文第 {item['paragraph']} 段 @{item['char_start']}-{item['char_end']}"
                if item["status"] != "added"
                else f"当前位置第 {item['current_paragraph']} 段 @{item['current_char_start']}-{item['current_char_end']}"
            )
            if item["status"] == "modified":
                where += f" → 当前第 {item['current_paragraph']} 段 @{item['current_char_start']}-{item['current_char_end']}"
            lines.append(f"  [{item['status']}] {where} 「{item['preview']}」")
        _emit(args, "\n".join(lines), payload)
        return EXIT_OK
    payload = {"available": True, "chapter": chapter["id"], "snapshot": meta, "paragraphs": len(paragraphs)}
    _emit(
        args,
        f"{chapter['id']} 快照 {meta['chars']} 字 / {len(paragraphs)} 段  {meta['sha256'][:19]}…  "
        f"导入于 {meta['imported_at']}  来源 {meta.get('source_path') or '（未记录）'}",
        payload,
    )
    return EXIT_OK


def _emit_source_locate(
    args, chapter: dict, meta: dict, source_text: str, paragraphs: list[dict], comparison: dict
) -> int:
    """把「原文某段/某个偏移」定位到当前章节。

    参数:
        args: 含 `at`（原文偏移）或 `paragraph`（1 起段号）。
        chapter: 章节 dict。
        meta: 快照元信息。
        source_text: 快照原文（只为算长度做越界检查）。
        paragraphs: 原文段落列表。
        comparison: `compare_texts` 的结果。

    返回:
        退出码；定位成功 0，越界抛 ValueError（CLI 转退出码 2）。
    """
    if args.paragraph is not None:
        number = args.paragraph
        if number < 1 or number > len(paragraphs):
            raise ValueError(f"段落序号超出范围: {number}（该快照共 {len(paragraphs)} 段）")
        index = number - 1
    else:
        offset = args.at
        if offset < 0 or offset >= len(source_text):
            raise ValueError(f"偏移超出范围: {offset}（该快照共 {len(source_text)} 字，从 0 起算）")
        index = next((item["index"] for item in paragraphs if item["char_start"] <= offset < item["char_end"]), None)
        if index is None:
            raise ValueError(f"偏移 {offset} 不落在任何段落内（可能是空行）")
    row = paragraphs[index]
    current_index = comparison["pairs"].get(index)
    payload = {
        "available": True,
        "chapter": chapter["id"],
        "snapshot": meta,
        "source": {
            "paragraph": index + 1,
            "char_start": row["char_start"],
            "char_end": row["char_end"],
            "preview": preview(row["text"]),
        },
        "current": None,
        "status": "missing",
    }
    if current_index is None:
        _emit(
            args,
            f"原文第 {index + 1} 段 @{row['char_start']}-{row['char_end']} → missing（当前章节找不到对应段落）"
            f"\n  「{preview(row['text'])}」",
            payload,
        )
        return EXIT_OK
    current_paragraphs = split_paragraphs(chapter.get("content") or "")
    current = current_paragraphs[current_index]
    payload["current"] = {
        "paragraph": current_index + 1,
        "char_start": current["char_start"],
        "char_end": current["char_end"],
        "preview": preview(current["text"]),
    }
    payload["status"] = "matched" if row["key"] == current["key"] else "modified"
    _emit(
        args,
        f"原文第 {index + 1} 段 @{row['char_start']}-{row['char_end']} → 当前第 {current_index + 1} 段"
        f" @{current['char_start']}-{current['char_end']}（{payload['status']}）"
        f"\n  原文「{preview(row['text'])}」\n  当前「{preview(current['text'])}」",
        payload,
    )
    return EXIT_OK
