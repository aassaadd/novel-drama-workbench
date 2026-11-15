"""场景与台词行 CLI：场景切分、台词切分、状态列表、diff / revert / set / 合并。

命令名、参数、输出格式、退出码一律未改动；判定与写盘已下沉到应用层用例。
"""

from __future__ import annotations

from novel_drama.application.production.line_edit import (
    edit_line_text,
    line_split_impact,
    merge_lines,
    merge_narrations,
    revert_line_params,
    set_lines,
    split_line_at,
    split_lines,
)
from novel_drama.application.production.line_query import (
    chapter_lines,
    line_diff,
)
from novel_drama.application.story.scene_split import scene_split_impact, split_scenes
from novel_drama.infrastructure.config import AppConfig
from novel_drama.models import entities

from ..common import EXIT_OK, EXIT_REJECTED, _emit, _resolve_store, cast_hint


def cmd_scene_split(args, config: AppConfig) -> int:
    """拆分场景；`--impact` 时只输出影响清单（不写盘）。

    参数:
        args: 参数（chapter/max-chars/force/dry-run/impact/keep-lines）。
        config: 本机配置。

    返回:
        退出码。
    """
    store = _resolve_store(args, config)
    keep_lines = bool(getattr(args, "keep_lines", False))
    if getattr(args, "impact", False):
        report = scene_split_impact(
            store, args.chapter, max_chars=args.max_chars, keep_lines=keep_lines
        )
        _emit(args, report["text"], report)
        return EXIT_OK
    scenes = split_scenes(
        store,
        args.chapter,
        max_chars=args.max_chars,
        force=args.force,
        dry_run=args.dry_run,
        keep_lines=keep_lines,
    )
    payload = {"dry_run": args.dry_run, "scenes": scenes}
    if args.dry_run:
        text = "\n".join(f"{index + 1}. {item['title']}（{len(item['content'])} 字）" for index, item in enumerate(scenes)) or "正文为空，未产生场景"
    else:
        text = f"已拆分 {len(scenes)} 个场景（章节已保存）"
    _emit(args, text, payload)
    if not args.dry_run:
        # 切分完成后提示音色/BGM 编排问题，但不阻断切分结果落盘
        cast_hint(store)
    return EXIT_OK


def cmd_line_split(args, config: AppConfig) -> int:
    """切分台词行；`--impact` 时只输出影响清单（不写盘）。

    参数:
        args: 参数（chapter/scene/force/dry-run/impact/keep-manual）。
        config: 本机配置。

    返回:
        退出码。
    """
    store = _resolve_store(args, config)
    keep_manual = bool(getattr(args, "keep_manual", False))
    if getattr(args, "impact", False):
        report = line_split_impact(
            store, args.chapter, scene_ids=args.scene, keep_manual=keep_manual
        )
        _emit(args, report["text"], report["payload"])
        return EXIT_OK
    result = split_lines(
        store,
        args.chapter,
        scene_ids=args.scene,
        force=args.force,
        dry_run=args.dry_run,
        keep_manual=keep_manual,
    )
    _emit(args, result["text"], result["payload"])
    if not args.dry_run:
        # 切分完成后提示音色/BGM 编排问题，但不阻断切分结果落盘
        cast_hint(store)
    return EXIT_OK


def cmd_line_list(args, config: AppConfig) -> int:
    """列出台词行（可按派生状态与行 ID 筛选）。

    状态一律按「产物 + 侧车快照」现算（七态模型），不使用落盘的 `tts.status` 直接展示，
    因此过期（stale）不需要写盘，僵尸 running 也会在这里降级成 pending。
    筛选与 `tts plan` 共用 `line_state` 的同一套判定，计数天然一致。
    """
    store = _resolve_store(args, config)
    payload = chapter_lines(
        store, args.chapter, config, statuses_raw=args.status, line_ids=args.line
    )
    rows = payload["lines"]
    if getattr(args, "json", False):
        _emit(args, payload=payload)
        return EXIT_OK
    if not rows:
        print("没有匹配的台词行")
    for row in rows:
        summary = "；".join(item["text"] for item in row["changes"][:5])
        if len(row["changes"]) > 5:
            summary += f"；等 {len(row['changes'])} 项"
        tail = f"  {summary}" if summary else ""
        print(
            f"{row['line_id']}  {row['state']:<8} {row['kind']:<15} {row['speaker']:<8} "
            f"{row['emotion']:<8} {(row['text'] or '')[:24]}{tail}"
        )
    return EXIT_OK


def cmd_line_split_at(args, config: AppConfig) -> int:
    """在指定字符位置把一条台词拆成两行（对应参考的「光标处拆分」）。

    参数:
        args: 含 chapter、line、offset（0 基字符位置）。
        config: 本机配置。

    返回:
        退出码。

    异常:
        ValueError: 行不存在、位置不在台词中间、正文对不上。
    """
    store = _resolve_store(args, config)
    result = split_line_at(store, args.chapter, args.line, int(args.offset))
    _emit(args, result["text"], result["payload"])
    return EXIT_OK


def cmd_line_diff(args, config: AppConfig) -> int:
    """查看一条台词相对上次生成参数改了哪些项。

    没有侧车快照时拒绝输出差异（不猜），返回退出码 1。
    """
    store = _resolve_store(args, config)
    payload = line_diff(store, args.chapter, args.line, config)
    if not payload["snapshot_available"]:
        _emit(
            args,
            f"{args.line} 没有参数快照（未生成过或侧车是旧版），无法比对差异；先执行 tts run 生成一次",
            payload,
        )
        return EXIT_REJECTED
    if not payload["changes"]:
        _emit(args, f"{payload['line_id']} 与上次生成参数一致（{payload['state']}）", payload)
        return EXIT_OK
    text = f"{payload['line_id']}  state={payload['state']}  共 {len(payload['changes'])} 项变更"
    for item in payload["changes"]:
        text += f"\n  [{item['group']}] {item['text']}"
    _emit(args, text, payload)
    return EXIT_OK


def cmd_line_revert(args, config: AppConfig) -> int:
    """把一条台词的情绪/强度/语速/交付说明回滚到上次生成时的值（文本不回滚）。

    无侧车快照时拒绝撤回并返回退出码 1：不基于猜测回滚。
    """
    store = _resolve_store(args, config)
    result = revert_line_params(store, args.chapter, args.line, config)
    _emit(args, result["text"], result["payload"])
    if not result["snapshot_available"]:
        return EXIT_REJECTED
    return EXIT_OK


def cmd_line_set(args, config: AppConfig) -> int:
    """修改台词行的说话人/情绪/强度/交付说明/语速/停顿/文本。

    支持 `--line` 单行与 `--lines a,b,c` 批量；批量时说话人变更会同步台词类型
    （旁白 ↔ 对白，内心独白保持不变），与章节页行内编辑保持一致。

    参数:
        args: 参数（chapter/line/lines/speaker/emotion/...）。
        config: 本机配置。

    返回:
        退出码。
    """
    store = _resolve_store(args, config)
    raw_lines = getattr(args, "lines", None)
    if raw_lines:
        targets = [item.strip() for item in str(raw_lines).split(",") if item.strip()]
    elif getattr(args, "line", None):
        targets = [args.line]
    else:
        raise ValueError("请用 --line 指定一行，或用 --lines 批量指定（逗号分隔）")
    if not targets:
        raise ValueError("--lines 里没有有效的行 ID")

    updates: dict = {}
    for field_name, attr in (
        ("speaker_id", "speaker"),
        ("emotion", "emotion"),
        ("text", "text"),
        ("kind", "kind"),
        ("delivery_note", "delivery_note"),
        ("speed", "speed"),
        ("pause_before_ms", "pause_before"),
        ("pause_after_ms", "pause_after"),
        ("emotion_intensity", "emotion_intensity"),
    ):
        value = getattr(args, attr, None)
        if value is not None:
            updates[field_name] = value

    result = set_lines(store, args.chapter, targets, updates, batch=bool(raw_lines))
    _emit(args, result["text"], result["payload"])
    return EXIT_OK


def cmd_line_edit(args, config: AppConfig) -> int:
    """就地编辑一条台词文本，并把同一处文本同步回章节正文。

    参数:
        args: 含 chapter、line、text。
        config: 本机配置。

    返回:
        退出码。

    异常:
        ValueError: 文本为空/含换行/超长，或正文与切分结果已不一致。
        KeyError: 台词行不存在。
    """
    store = _resolve_store(args, config)
    result = edit_line_text(store, args.chapter, args.line, args.text)
    _emit(args, result["text"], result["payload"])
    return EXIT_OK


def cmd_line_merge(args, config: AppConfig) -> int:
    """合并同一场景内相邻的台词行（选中行 → 合并）。

    参数:
        args: 含 chapter、lines（逗号分隔的行 ID）。
        config: 本机配置。

    返回:
        退出码。

    异常:
        ValueError: 行不足两行、跨场景、不相邻或正文对不上。
    """
    store = _resolve_store(args, config)
    line_ids = [item.strip() for item in str(args.lines or "").split(",") if item.strip()]
    result = merge_lines(store, args.chapter, line_ids)
    _emit(args, result["text"], result["payload"])
    return EXIT_OK


def cmd_line_merge_narration(args, config: AppConfig) -> int:
    """按期望字数批量合并相邻旁白；`--dry-run` 只输出预览。

    参数:
        args: 含 chapter、scene（可重复）、max_chars、dry_run。
        config: 本机配置。

    返回:
        退出码。
    """
    store = _resolve_store(args, config)
    result = merge_narrations(
        store,
        args.chapter,
        scene_ids=args.scene,
        max_chars=args.max_chars,
        dry_run=args.dry_run,
    )
    _emit(args, result["text"], result["payload"])
    return EXIT_OK


def add_parser(sub) -> None:
    """注册 `scene` 与 `line` 两个命令组。"""
    p = sub.add_parser("scene", help="场景")
    p_sub = p.add_subparsers(dest="action", required=True)
    q = p_sub.add_parser("split")
    q.add_argument("--chapter", required=True)
    q.add_argument("--max-chars", type=int, default=1200)
    q.add_argument("--force", action="store_true")
    q.add_argument("--dry-run", action="store_true")
    q.add_argument("--impact", action="store_true", help="只输出重切影响清单，不写盘")
    q.add_argument("--keep-lines", action="store_true", dest="keep_lines",
                   help="保留模式：正文未变化的场景沿用原场景与台词（含配音/媒体引用）")
    q.set_defaults(func=cmd_scene_split)

    p = sub.add_parser("line", help="台词行")
    p_sub = p.add_subparsers(dest="action", required=True)
    q = p_sub.add_parser("split")
    q.add_argument("--chapter", required=True)
    q.add_argument("--scene", action="append")
    q.add_argument("--force", action="store_true")
    q.add_argument("--dry-run", action="store_true")
    q.add_argument("--impact", action="store_true", help="只输出重切影响清单，不写盘")
    q.add_argument("--keep-manual", action="store_true", dest="keep_manual",
                   help="保留模式：人工改过/新增的台词行原样保留并按原位置插回")
    q.set_defaults(func=cmd_line_split)
    q = p_sub.add_parser("list", help="列出派生状态，可按状态/行 ID 筛选")
    q.add_argument("--chapter", required=True)
    q.add_argument("--line", action="append", help="只看这些行，可重复")
    q.add_argument(
        "--status",
        help="逗号分隔的派生状态：pending,stale,failed,ready（历史值 unknown=未识别说话人）",
    )
    q.set_defaults(func=cmd_line_list)
    q = p_sub.add_parser("diff", help="查看一条台词相对上次生成改了哪些参数")
    q.add_argument("--chapter", required=True)
    q.add_argument("--line", required=True)
    q.set_defaults(func=cmd_line_diff)
    q = p_sub.add_parser("revert", help="把情绪/强度/语速/交付说明回滚到上次生成时的值（文本不回滚）")
    q.add_argument("--chapter", required=True)
    q.add_argument("--line", required=True)
    q.set_defaults(func=cmd_line_revert)
    q = p_sub.add_parser("set")
    q.add_argument("--chapter", required=True)
    q.add_argument("--line", help="单行 ID；与 --lines 二选一")
    q.add_argument("--lines", help="批量：逗号分隔的行 ID；与 --line 二选一")
    q.add_argument("--speaker")
    q.add_argument("--emotion", choices=list(entities.EMOTIONS))
    q.add_argument("--emotion-intensity", type=float, dest="emotion_intensity")
    q.add_argument("--delivery-note", dest="delivery_note")
    q.add_argument("--speed", type=float)
    q.add_argument("--pause-before", type=int, dest="pause_before")
    q.add_argument("--pause-after", type=int, dest="pause_after")
    q.add_argument("--text")
    q.add_argument("--kind", choices=["dialogue", "narration", "inner_monologue"])
    q.set_defaults(func=cmd_line_set)
    q = p_sub.add_parser("edit", help="就地编辑台词文本，并同步回章节正文对应位置")
    q.add_argument("--chapter", required=True)
    q.add_argument("--line", required=True)
    q.add_argument("--text", required=True)
    q.set_defaults(func=cmd_line_edit)
    q = p_sub.add_parser("merge", help="合并同一场景内相邻的台词行")
    q.add_argument("--chapter", required=True)
    q.add_argument("--lines", required=True, help="逗号分隔的行 ID（必须同场景且相邻）")
    q.set_defaults(func=cmd_line_merge)
    q = p_sub.add_parser("merge-narration", help="按期望字数批量合并相邻旁白")
    q.add_argument("--chapter", required=True)
    q.add_argument("--scene", action="append", help="只处理这些场景，可重复")
    q.add_argument("--max-chars", type=int, default=120, dest="max_chars", help="每组目标字数上限")
    q.add_argument("--dry-run", action="store_true", dest="dry_run", help="只输出预览，不写盘")
    q.set_defaults(func=cmd_line_merge_narration)
    q = p_sub.add_parser("split-at", help="在指定字符位置把一条台词拆成两行")
    q.add_argument("--chapter", required=True)
    q.add_argument("--line", required=True)
    q.add_argument("--offset", type=int, required=True, help="0 基字符位置，必须落在台词中间")
    q.set_defaults(func=cmd_line_split_at)
