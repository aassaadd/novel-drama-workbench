"""全链路编排 CLI：`ndw produce` 与 `ndw run show`。"""

from __future__ import annotations

from novel_drama.infrastructure.config import AppConfig
from novel_drama.orchestration.produce import (
    STAGE_LABELS,
    latest_run,
    load_run,
    produce,
)
from novel_drama.orchestration.produce import (
    STAGES as PRODUCE_STAGES,
)

from ..common import EXIT_FAILED, EXIT_OK, _emit, _resolve_store


def _fmt_stages(stages: list[dict]) -> str:
    """把阶段记录渲染成表格文本。

    参数:
        stages: 阶段记录列表。

    返回:
        多行文本。
    """
    marks = {"succeeded": "✓", "failed": "✗", "skipped": "–", "pending": "·", "running": "…"}
    lines = []
    for item in stages:
        mark = marks.get(item.get("status"), "?")
        label = item.get("label") or STAGE_LABELS.get(item.get("stage"), item.get("stage"))
        note = item.get("note") or item.get("error") or ""
        lines.append(f"  {mark} {label:<8} {item.get('status'):<10} {note}")
    return "\n".join(lines)


def cmd_produce(args, config: AppConfig) -> int:
    """执行全链路编排。

    参数:
        args: 参数（chapter/from/to/no-resume/dry-run/force/keep-going）。
        config: 本机配置。

    返回:
        退出码（0 成功 / 5 有阶段失败）。
    """
    store = _resolve_store(args, config)
    result = produce(
        store,
        config,
        args.chapter or "",
        from_stage=args.from_stage or "setting",
        to_stage=args.to_stage or "export",
        resume=not args.no_resume,
        dry_run=args.dry_run,
        force=args.force,
        keep_going=args.keep_going,
    )
    failed = [item for item in result["stages"] if item["status"] == "failed"]
    head = {"succeeded": "全链路完成", "failed": "全链路中断", "dry_run": "全链路体检（dry-run）"}[result["status"]]
    text = f"{head}：run_id={result['run_id']}\n{_fmt_stages(result['stages'])}\n记录: {result['path']}"
    if failed:
        text += "\n失败阶段：" + "、".join(item["stage"] for item in failed)
    _emit(args, text, result)
    return EXIT_FAILED if failed else EXIT_OK


def cmd_run_show(args, config: AppConfig) -> int:
    """查看编排记录。

    参数:
        args: 参数（run/latest）。
        config: 本机配置。

    返回:
        退出码。
    """
    store = _resolve_store(args, config)
    if args.run:
        record = load_run(store, args.run)
    else:
        record = latest_run(store)
        if record is None:
            raise FileNotFoundError("还没有任何编排记录：先执行 ndw produce")
    text = (
        f"run {record['run_id']}（{record.get('status')}）{_scope_text(record)}\n"
        f"开始 {record.get('started_at')}　结束 {record.get('finished_at')}\n" + _fmt_stages(record.get("stages") or [])
    )
    _emit(args, text, record)
    return EXIT_OK


def _scope_text(record: dict) -> str:
    """编排记录的适用范围中文描述。

    参数:
        record: 编排记录。

    返回:
        如「章节 chp_x」「全书（8 章）」「工程级（不涉及章节）」。
    """
    scope = record.get("scope")
    if scope == "book":
        return f"全书（{len(record.get('chapters') or [])} 章）"
    if record.get("chapter_id"):
        return f"章节 {record['chapter_id']}"
    return "工程级（不涉及章节）"


def add_parser(sub) -> None:
    """注册 `produce` 命令。"""
    p = sub.add_parser("produce", help="全链路编排：设定 → 大纲 → 正文 → 标注 → 配音 → 音效/配图/视频 → 混流 → 导出")
    p.add_argument(
        "--chapter",
        help="目标章节；缺省时工程级阶段跑一次、章节级阶段按全书逐章执行",
    )
    p.add_argument("--from", dest="from_stage", choices=list(PRODUCE_STAGES))
    p.add_argument("--to", dest="to_stage", choices=list(PRODUCE_STAGES))
    p.add_argument("--no-resume", dest="no_resume", action="store_true", help="不跳过上次已成功的阶段")
    p.add_argument("--force", action="store_true", help="强制重跑正文/脚本/媒体")
    p.add_argument("--keep-going", dest="keep_going", action="store_true", help="阶段失败后继续后续阶段")
    p.add_argument("--dry-run", action="store_true", help="只体检前置条件，不写盘")
    p.set_defaults(func=cmd_produce)


def add_run_parser(sub) -> None:
    """注册 `run show` 命令。"""
    p = sub.add_parser("run", help="查看编排记录")
    p_sub = p.add_subparsers(dest="action", required=True)
    q = p_sub.add_parser("show", help="查看某次或最近一次编排")
    q.add_argument("--run", help="run_id；缺省取最近一次")
    q.set_defaults(func=cmd_run_show)
