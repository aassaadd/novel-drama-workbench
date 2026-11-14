"""全链路编排 CLI：`ndw produce` 与 `ndw run show`。"""

from __future__ import annotations

from .cli_common import EXIT_FAILED, EXIT_OK, _emit, _resolve_store
from .config import AppConfig
from .orchestration.produce import STAGE_LABELS, latest_run, load_run, produce


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
