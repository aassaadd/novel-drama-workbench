"""续写 CLI：`ndw outline continue` / `ndw chapter continue`。

正文续写默认只预览并把结果存为「未应用草稿」；`--apply` 应用草稿（无草稿时才现生成并写入），
`--retry` 重抽覆盖草稿。已有正文与原大纲逐字不动，只在末尾追加。
"""

from __future__ import annotations

from .cli_common import EXIT_OK, PreconditionError, TaskFailedError, _emit, _resolve_store
from .config import AppConfig
from .writing.continuation import continue_chapter, continue_outline


def cmd_chapter_continue(args, config: AppConfig) -> int:
    """在已有正文末尾续写（默认预览存草稿，`--apply` 才写正文）。

    参数:
        args: 参数（chapter 必填；words / rounds / intent / from-beat；retry；dry-run/apply）。
        config: 本机配置。

    返回:
        退出码。

    异常:
        PreconditionError: 正文为空、接近长度上限或草稿已失效。
        TaskFailedError: 模型调用失败且没有可用的新内容。
    """
    store = _resolve_store(args, config)
    before_text = str(store.load_chapter(args.chapter).get("content") or "").rstrip()
    try:
        result = continue_chapter(
            store,
            config,
            args.chapter,
            words=getattr(args, "words", None),
            rounds=getattr(args, "rounds", None),
            intent=getattr(args, "intent", None),
            from_beat=getattr(args, "from_beat", None),
            dry_run=not args.apply,
            retry=bool(getattr(args, "retry", False)),
        )
    except ValueError as exc:
        raise PreconditionError(str(exc)) from exc

    if result.get("applied_from_draft"):
        head = "已应用续写草稿"
    elif args.apply:
        head = "续写完成"
    else:
        head = "续写预览（未写盘，加 --apply 才保存）"
    covered = result.get("covered_beats") or []
    beat_note = ""
    if covered:
        beat_note = f"，从第 {result['from_beat']} 个节拍继续（覆盖第 {covered[0]}~{covered[-1]} 拍）"
    lines = [
        (
            f"章节 {result['chapter_id']} {head}：{result['before_words']} 字 → {result['after_words']} 字"
            f"（+{result['added_words']}）{beat_note}"
        ),
        (
            f"{result.get('rounds') or 0} 轮，stop_reason={result.get('stop_reason')}"
            f"，provider={result.get('provider') or 'offline-writing'}"
        ),
    ]
    if args.apply:
        lines.append(f"status={result.get('status')}，revision={result.get('revision')}，记录 {result.get('continuation_id')}")
    else:
        if before_text:
            lines.append(f"衔接锚点：……{before_text[-60:]}")
        lines.append(f"追加位置：正文末尾（第 {result['append_start']} 字之后）")
    lines.extend(f"  ! {item}" for item in result.get("warnings") or [])
    if result.get("error"):
        lines.append(f"  x {result['error']}")
    if result.get("draft_path"):
        lines.append(f"未应用草稿：{result['draft_path']}（--apply 应用，--retry 重抽）")
    if result.get("added_words", 0) <= 0:
        lines.append("  ! 本次没有产生可追加的新内容（模型可能复述或返回空），可补充 intent 后 --retry")
    if not args.apply and result.get("preview"):
        lines.append("--- 新增正文预览（前 400 字）---")
        lines.append(result["preview"][:400])

    _emit(args, "\n".join(lines), result)
    if result.get("error") and not result.get("added_words"):
        raise TaskFailedError(result["error"])
    return EXIT_OK


def cmd_outline_continue(args, config: AppConfig) -> int:
    """在已有大纲之后追加新章（默认预览，`--apply` 才写设定库）。

    参数:
        args: 参数（chapters / acts / chapters_per_act / beats_per_chapter / words / brief / intent / dry-run / apply）。
        config: 本机配置。

    返回:
        退出码。

    异常:
        PreconditionError: 大纲章节序号存在空洞或与章节文件冲突。
        TaskFailedError: 一幕都没生成成功。
    """
    store = _resolve_store(args, config)
    try:
        result = continue_outline(
            store,
            config,
            chapters=getattr(args, "chapters", None),
            acts=getattr(args, "acts", None),
            chapters_per_act=int(getattr(args, "chapters_per_act", 3) or 3),
            beats_per_chapter=int(getattr(args, "beats_per_chapter", 3) or 3),
            chapter_words=int(getattr(args, "words", 1200) or 1200),
            brief=getattr(args, "brief", None),
            intent=getattr(args, "intent", None),
            dry_run=not args.apply,
        )
    except ValueError as exc:
        raise PreconditionError(str(exc)) from exc

    head = "大纲续写已写入" if args.apply else "大纲续写预览（未写盘，加 --apply 才保存）"
    lines = [
        (
            f"{head}：新增 {result['acts_added']} 幕 / {result['chapters_added']} 章"
            f"（第 {result['start_index']} 章起，provider={result.get('provider') or 'offline-writing'}，"
            f"settings_revision={result.get('settings_revision')}）"
        )
    ]
    for act in result.get("new_acts") or []:
        lines.append(f"  {act.get('title')}（{act.get('chapters')} 章）")
    for chapter in result.get("new_chapters") or []:
        lines.append(f"    {chapter.get('index'):>3}. {chapter.get('title')}")
    if not args.apply:
        lines.append("（写入后仍需 outline apply 才会创建章节文件）")
    lines.extend(f"  ! {item}" for item in result.get("warnings") or [])
    if result.get("error"):
        lines.append(f"  x {result['error']}")

    _emit(args, "\n".join(lines), result)
    if result.get("error") and not result.get("acts_added"):
        raise TaskFailedError(result["error"])
    return EXIT_OK
