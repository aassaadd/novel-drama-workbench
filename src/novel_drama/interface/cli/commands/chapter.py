"""章节 CLI：增删查、按标题导入（重复可控）、原文快照与对照。

从 `cli.py` 拆出，纯搬迁：命令名、参数、输出格式、退出码一律未改动。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from novel_drama.application.story.chapter_import import (
    import_chapters,
    save_source_snapshot,
)
from novel_drama.application.story.queries import chapter_detail, chapter_list
from novel_drama.composition import (
    ImportsStore,
    compare_texts,
    preview,
    split_paragraphs,
)
from novel_drama.infrastructure.config import AppConfig
from novel_drama.interface.cli.commands.continuation import cmd_chapter_continue
from novel_drama.interface.cli.commands.writing import (
    cmd_chapter_edit,
    cmd_chapter_generate,
    cmd_chapter_save,
    cmd_chapter_summarize,
)

from ..common import EXIT_OK, EXIT_REJECTED, _emit, _resolve_store

# ---------------- chapter ----------------


def cmd_chapter_add(args, config: AppConfig) -> int:
    """新增章节；给了 `--content-file` 时同时落一份原文快照。"""
    store = _resolve_store(args, config)
    raw = Path(args.content_file).read_text(encoding="utf-8") if args.content_file else ""
    chapter = store.create_chapter(args.title, content=raw, outline=args.outline or "", index=args.index)
    note = ""
    if raw:
        snapshot = save_source_snapshot(store, chapter, raw, str(Path(args.content_file).resolve()), "novel add")
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
    chapters = chapter_list(store)
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
    chapter = chapter_detail(store, args.chapter)
    print(json.dumps(chapter, ensure_ascii=False, indent=2))
    return EXIT_OK


def cmd_chapter_import(args, config: AppConfig) -> int:
    """导入 txt/markdown 并按标题切章（重复导入可控）。

    参数:
        args: 含 file / pattern / on_duplicate / force / force_replace。

    返回:
        退出码：有章节被跳过或被拒绝替换时返回 1（未改动的那部分），全部新建/替换成功返回 0。
    """
    store = _resolve_store(args, config)
    text = Path(args.file).read_text(encoding="utf-8")
    result = import_chapters(
        store,
        text,
        str(Path(args.file).resolve()),
        Path(args.file).stem,
        pattern=args.pattern,
        on_duplicate=args.on_duplicate,
        force=bool(args.force),
        force_replace=bool(args.force_replace),
    )
    _emit(args, result["text"], result["payload"])
    return EXIT_REJECTED if result["code"] else EXIT_OK


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


def add_parser(sub) -> None:
    """注册 `chapter` 命令组。"""
    p = sub.add_parser("chapter", help="章节")
    p_sub = p.add_subparsers(dest="action", required=True)
    q = p_sub.add_parser("add")
    q.add_argument("--title", required=True)
    q.add_argument("--index", type=int)
    q.add_argument("--outline", default="")
    q.add_argument("--content-file")
    q.set_defaults(func=cmd_chapter_add)
    p_sub.add_parser("list").set_defaults(func=cmd_chapter_list)
    q = p_sub.add_parser("show")
    q.add_argument("chapter")
    q.set_defaults(func=cmd_chapter_show)
    q = p_sub.add_parser("import", help="按标题切章导入，重复导入可控")
    q.add_argument("file")
    q.add_argument("--pattern", default=r"^#{1,3}\s+(.+)$")
    q.add_argument("--max-chars", type=int, default=5000)
    q.add_argument(
        "--on-duplicate",
        choices=["skip", "replace", "new"],
        default="skip",
        help="命中已有章节时：skip 跳过（默认）/ replace 覆盖正文 / new 再建一份",
    )
    q.add_argument("--force", action="store_true", help="等价于 --on-duplicate new")
    q.add_argument("--force-replace", action="store_true", help="目标章节已有台词行或音频时仍强制覆盖")
    q.set_defaults(func=cmd_chapter_import)
    q = p_sub.add_parser("source", help="查看原文快照 / 与当前正文对照 / 定位某段原文")
    q.add_argument("--chapter", required=True)
    q.add_argument("--diff", action="store_true", help="按段落对齐，报 missing/added/modified")
    q.add_argument("--at", type=int, help="原文偏移（0 起算）")
    q.add_argument("--paragraph", type=int, help="原文段落序号（1 起算）")
    q.set_defaults(func=cmd_chapter_source)
    q = p_sub.add_parser("generate", help="按大纲/节拍生成正文（离线写作或 LLM）")
    q.add_argument("--chapter", required=True)
    q.add_argument("--regen", action="store_true", help="已有正文时覆盖重写")
    q.add_argument("--dry-run", action="store_true", help="只预览，不落盘")
    q.add_argument(
        "--multi-turn",
        dest="multi_turn",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="强制开启/关闭按节拍分轮；缺省按 writing.multi_turn.enabled 自动判断",
    )
    q.add_argument("--beats-per-round", dest="beats_per_round", type=int, help="每轮写几个节拍（默认按节拍数与最大轮数自动分配）")
    q.add_argument("--max-rounds", dest="max_rounds", type=int, help="最大续写轮数（保险上限，默认取配置 max_rounds）")
    q.set_defaults(func=cmd_chapter_generate)
    q = p_sub.add_parser("save", help="用手写正文覆盖章节正文（UTF-8）")
    q.add_argument("--chapter", required=True)
    save_source = q.add_mutually_exclusive_group(required=True)
    save_source.add_argument("--file", help="从 UTF-8 文本文件读取正文")
    save_source.add_argument("--stdin", action="store_true", help="从标准输入读取正文")
    save_source.add_argument("--text", help="直接给出正文文本")
    q.set_defaults(func=cmd_chapter_save)
    q = p_sub.add_parser("edit", help="按提示词让 LLM 改写正文（默认预览，--apply 才落盘）")
    q.add_argument("--chapter", required=True)
    q.add_argument("--instruction", required=True, help="改写提示词")
    q.add_argument("--range", dest="char_range", help="选区 start:end（0 起算，左闭右开）；缺省改写整章")
    q.add_argument("--chunk-words", dest="chunk_words", type=int, help="分块阈值（默认取配置 edit_chunk_words）")
    edit_mode = q.add_mutually_exclusive_group()
    edit_mode.add_argument("--dry-run", action="store_true", help="只预览（默认行为）")
    edit_mode.add_argument("--apply", action="store_true", help="写盘并追加 continuations 记录")
    q.set_defaults(func=cmd_chapter_edit)
    q = p_sub.add_parser("summarize", help="根据正文反向总结标题/摘要/大纲/节拍（默认预览，--apply 才落盘）")
    q.add_argument("--chapter", required=True)
    q.add_argument("--apply", action="store_true", help="写回章节文件与设定库大纲")
    q.add_argument("--chunk-words", dest="chunk_words", type=int, help="分块阈值（默认取配置 summary_chunk_words）")
    q.set_defaults(func=cmd_chapter_summarize)
    q = p_sub.add_parser("continue", help="在已有正文末尾续写（默认预览，--apply 才落盘）")
    q.add_argument("--chapter", required=True)
    q.add_argument("--words", type=int, help="每轮目标字数（默认取配置 segment_words=800；页面用 500/800/1200 档位）")
    q.add_argument("--rounds", type=int, help="最多轮数（内部参数，页面不暴露；默认取配置 max_rounds=3）")
    q.add_argument("--intent", help="自由意图：接下来发生什么（最高优先级，可为空）")
    q.add_argument("--instruction", help="续写思路（兼容旧参数，优先级低于 --intent）")
    q.add_argument("--from-beat", dest="from_beat", type=int, help="从第几个节拍开始写（1 起）；缺省按已写字数估算")
    q.add_argument("--retry", action="store_true", help="重抽：忽略已有草稿重新生成（覆盖草稿）")
    continue_mode = q.add_mutually_exclusive_group()
    continue_mode.add_argument("--dry-run", action="store_true", help="只预览（默认行为）")
    continue_mode.add_argument("--apply", action="store_true", help="追加进正文并记录 continuations")
    q.set_defaults(func=cmd_chapter_continue)
