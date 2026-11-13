"""写作侧 CLI：`ndw outline ...` / `ndw chapter generate` / `ndw script suggest ...`。

模型调用由 `writing/service.py` 统一装配：默认离线实现，零依赖可跑通。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .cli_common import (
    EXIT_OK,
    PreconditionError,
    TaskFailedError,
    _emit,
    _resolve_store,
)
from .config import AppConfig
from .store import validate
from .store.settings_store import SettingsStore
from .writing import (
    apply_outline,
    edit_chapter,
    generate_chapter,
    generate_outline,
    save_chapter_content,
    suggest_script,
    summarize_chapter,
    validate_chapter_binding,
)


def _fmt_outline(acts: list[dict]) -> str:
    """把大纲渲染成缩进文本。

    参数:
        acts: 幕列表。

    返回:
        多行文本。
    """
    lines = []
    for act in acts:
        lines.append(f"{act.get('title')}　{act.get('summary') or ''}".rstrip())
        for chapter in act.get("chapters") or []:
            lines.append(f"  {chapter.get('index'):>2}. {chapter.get('title')}（{len(chapter.get('beats') or [])} 拍）")
            for index, beat in enumerate(chapter.get("beats") or [], start=1):
                flag = "★" if beat.get("turning_point") else " ·"
                lines.append(f"      {flag} {index}. {beat.get('summary')}（{beat.get('target_words')} 字）")
    return "\n".join(lines) or "（大纲为空）"


def cmd_outline(args, config: AppConfig) -> int:
    """大纲：查看、生成、落地成章节。

    参数:
        args: 参数（action=show/generate/apply）。
        config: 本机配置。

    返回:
        退出码。
    """
    store = _resolve_store(args, config)
    settings = SettingsStore(store.root)

    if args.action == "show":
        bible = settings.ensure()
        acts = (bible.get("outline") or {}).get("acts") or []
        _emit(args, _fmt_outline(acts), {"acts": acts, "settings_revision": bible.get("settings_revision")})
        return EXIT_OK

    if args.action == "generate":
        result = generate_outline(
            store,
            config,
            acts=args.acts,
            chapters_per_act=args.chapters_per_act,
            beats_per_chapter=args.beats_per_chapter,
            chapter_words=args.words,
            dry_run=args.dry_run,
            binding=getattr(args, "binding", "auto"),
            multi_turn=bool(getattr(args, "multi_turn", False)),
            acts_per_round=int(getattr(args, "acts_per_round", 1) or 1),
        )
        head = f"[dry-run] " if args.dry_run else ""
        golden = result.get("golden_three") or {"passed": True, "issues": []}
        hints = ""
        if not golden["passed"]:
            hints = "\n黄金三章提示（不阻断）：\n" + "\n".join(
                f"  第 {issue['chapter']} 章：{issue['problem']}——{issue['hint']}" for issue in golden["issues"]
            )
        rounds_note = f"，multi-turn，{result.get('rounds') or 1} 轮" if result.get("multi_turn") else ""
        _emit(
            args,
            f"{head}大纲已生成：{result['acts'].__len__()} 幕 / {result['chapters']} 章 / {result['beats']} 个节拍"
            f"（provider={result['provider']}，settings_revision={result['settings_revision']}，binding={result.get('binding')}{rounds_note}）\n"
            + _fmt_outline(result["acts"])
            + hints,
            result,
        )
        return EXIT_OK

    if args.action == "apply":
        result = apply_outline(store, dry_run=args.dry_run)
        head = "[dry-run] " if args.dry_run else ""
        _emit(
            args,
            f"{head}大纲已落地：新建 {len(result['created'])} 章，更新 {len(result['updated'])} 章"
            + (f"\n  新建: {', '.join(result['created'])}" if result["created"] else "")
            + (f"\n  更新: {', '.join(result['updated'])}" if result["updated"] else ""),
            result,
        )
        return EXIT_OK

    raise PreconditionError(f"未知 outline 操作: {args.action}")


def cmd_chapter_generate(args, config: AppConfig) -> int:
    """按大纲生成章节正文。

    参数:
        args: 参数（chapter/regen/dry-run/multi_turn/segment_words/max_rounds）。
        config: 本机配置。

    返回:
        退出码。

    异常:
        TaskFailedError: 章节世界/角色绑定不完整。
    """
    store = _resolve_store(args, config)
    try:
        validate_chapter_binding(store, args.chapter)
    except ValueError as exc:
        raise TaskFailedError(str(exc)) from exc
    result = generate_chapter(
        store,
        config,
        args.chapter,
        regen=args.regen,
        dry_run=args.dry_run,
        multi_turn=getattr(args, "multi_turn", None),
        segment_words=getattr(args, "segment_words", None),
        max_rounds=getattr(args, "max_rounds", None),
    )
    head = "[dry-run] " if args.dry_run else ""
    text = (
        f"{head}章节 {result['chapter_id']} 正文生成完成：{result['words']} 字"
        f"（{result.get('rounds') or 1} 轮，provider={result['provider']}）"
    )
    if args.dry_run and result.get("content"):
        preview = result["content"][:400]
        text += f"\n--- 预览（前 400 字）---\n{preview}"
    _emit(args, text, {key: value for key, value in result.items() if key != "content"} | {"preview": (result.get("content") or "")[:400]})
    return EXIT_OK


def _read_save_content(args) -> str:
    """读取 `chapter save` 的正文来源（--file / --stdin / --text 三选一，统一 UTF-8）。

    参数:
        args: 含 file（文件路径）、stdin（是否读标准输入）、text（直接给出的正文）。

    返回:
        正文文本；`--text` 未给值时返回空串，由 `save_chapter_content` 统一拒绝。
    """
    if args.file:
        return Path(args.file).read_text(encoding="utf-8")
    if args.stdin:
        return sys.stdin.buffer.read().decode("utf-8")
    return args.text or ""


def cmd_chapter_save(args, config: AppConfig) -> int:
    """用手写正文覆盖章节正文，并同步字数、版本与状态。

    参数:
        args: 参数（chapter 必填；file / stdin / text 三选一）。
        config: 本机配置。

    返回:
        退出码。

    异常:
        ValueError: 正文为空（由 `save_chapter_content` 抛出）。
        FileNotFoundError: 章节不存在。
    """
    store = _resolve_store(args, config)
    result = save_chapter_content(store, args.chapter, _read_save_content(args))
    text = (
        f"章节 {result['chapter_id']} 正文已保存：{result['word_count']} 字"
        f"（revision={result['revision']}，status={result['status']}）"
    )
    lines = [text]
    lines.extend(f"  ! {item}" for item in result["warnings"])
    _emit(args, "\n".join(lines), result)
    return EXIT_OK


def _parse_char_range(raw: str | None) -> tuple[int, int] | None:
    """解析 `--range start:end` 选区参数。

    参数:
        raw: 形如 `120:260` 的字符串；None 或空串表示整章。

    返回:
        (start, end) 或 None。

    异常:
        ValueError: 格式不是两个整数。
    """
    if not raw:
        return None
    parts = str(raw).split(":")
    if len(parts) != 2:
        raise ValueError("选区格式应为 start:end，例如 --range 120:260")
    try:
        return int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise ValueError("选区格式应为 start:end（两个整数偏移）") from exc


def cmd_chapter_edit(args, config: AppConfig) -> int:
    """按提示词让 LLM 改写章节正文（默认预览，`--apply` 才落盘）。

    参数:
        args: 参数（chapter/instruction 必填；range / chunk_words 可选；dry-run / apply 二选一）。
        config: 本机配置。

    返回:
        退出码。

    异常:
        ValueError: 提示词为空、正文为空或选区越界（由 `edit_chapter` 抛出）。
        AdapterError: 模型调用失败或返回空内容。
    """
    store = _resolve_store(args, config)
    result = edit_chapter(
        store,
        config,
        args.chapter,
        args.instruction,
        char_range=_parse_char_range(getattr(args, "char_range", None)),
        dry_run=not args.apply,
        chunk_words=getattr(args, "chunk_words", None),
    )
    head = "改写完成" if args.apply else "改写预览（未写盘，加 --apply 才保存）"
    lines = [
        (
            f"章节 {result['chapter_id']} {head}：{result['word_count']} 字"
            f"（status={result['status']}，revision={result['revision']}，{result.get('rounds') or 1} 轮）"
        )
    ]
    lines.extend(f"  ! {item}" for item in result["warnings"])
    if not args.apply:
        lines.append("--- 预览（前 400 字）---")
        lines.append(result["after"][:400])
    _emit(args, "\n".join(lines), result)
    return EXIT_OK


def _fmt_summarize_preview(old_title: str, result: dict) -> str:
    """把反向总结预览渲染成中文可读文本。

    参数:
        old_title: 当前章节标题。
        result: `summarize_chapter(apply=False)` 的返回。

    返回:
        多行文本。
    """
    preview = result["preview"]
    lines = [
        f"章节 {result['chapter_id']} 重总结预览（未写盘，加 --apply 才保存）",
        f"旧标题：{old_title}",
        f"新标题：{preview['title']}",
        f"摘要：{preview['synopsis'] or '（空）'}",
        "大纲：",
        preview["outline"] or "（空）",
    ]
    changes = result["changes"]
    if not changes:
        lines.append("（与当前内容一致，应用不会改动任何字段）")
        return "\n".join(lines)
    lines.append(f"节拍（{len(preview['beats'])} 条，替换旧节拍 {result['replaced_beats']} 条）：")
    for index, beat in enumerate(preview["beats"], start=1):
        flag = "★" if beat["turning_point"] else " ·"
        lines.append(f"  {flag} {index}. {beat['summary']}")
    lines.append(f"变更字段：{', '.join(changes)}")
    return "\n".join(lines)


def cmd_chapter_summarize(args, config: AppConfig) -> int:
    """根据正文反向总结标题、摘要、大纲与节拍（默认预览，`--apply` 才落盘）。

    参数:
        args: 参数（chapter 必填；apply 是否写盘；chunk_words 分块阈值）。
        config: 本机配置。

    返回:
        退出码。

    异常:
        ValueError: 正文为空（由 `summarize_chapter` 抛出）。
        AdapterError: 模型调用失败或输出不合法。
    """
    store = _resolve_store(args, config)
    chapter = store.load_chapter(args.chapter)
    result = summarize_chapter(
        store,
        config,
        args.chapter,
        apply=args.apply,
        chunk_words=getattr(args, "chunk_words", None),
    )
    if args.apply:
        lines = [
            (
                f"章节 {result['chapter_id']} 重总结已应用："
                f"{result['old_title']} → {result['new_title']}"
            ),
            (
                f"变更字段：{', '.join(result['changed_fields']) or '（无）'}"
                f"；替换旧节拍 {result['replaced_beats']} 条"
                f"；{result.get('rounds') or 1} 轮"
            ),
        ]
        lines.extend(f"  ! {item}" for item in result["warnings"])
    else:
        lines = [_fmt_summarize_preview(str(chapter.get("title") or ""), result)]
        lines.append(f"轮次：{result.get('rounds') or 1} 轮")
    _emit(args, "\n".join(lines), result)
    return EXIT_OK


def cmd_script_suggest(args, config: AppConfig) -> int:
    """生成导演标注建议，可选直接落盘并同步到章节。

    参数:
        args: 参数（chapter/apply/dry-run/out/scenes_per_round）。
        config: 本机配置。

    返回:
        退出码。
    """
    store = _resolve_store(args, config)
    result = suggest_script(
        store,
        config,
        args.chapter,
        dry_run=args.dry_run,
        scenes_per_round=getattr(args, "scenes_per_round", None),
        resume=bool(getattr(args, "resume", False)),
    )
    markup = result["markup"]
    chapter_id = args.chapter

    out_path = Path(args.out) if getattr(args, "out", None) else store.root / "scripts" / f"{chapter_id}.markup.txt"
    if not args.dry_run:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(markup + "\n", encoding="utf-8")

    from .director.markup import INFO_PREFIX

    warnings = result["warnings"]
    hard = [item for item in warnings if not item.startswith(INFO_PREFIX)]
    info = [item for item in warnings if item.startswith(INFO_PREFIX)]
    lines = [
        (
            f"标注建议：镜头 {result['shots']} 个，台词 {result['lines']} 行，告警 {len(hard)} 条"
            f"，已自动修正 {len(info)} 项（provider={result['provider']}，{result.get('rounds') or 1} 轮）"
        ),
        f"标注文本: {out_path}",
    ]
    lines.extend(f"  ! {item}" for item in warnings)
    failures = result.get("failures") or []
    for item in failures:
        lines.append(
            f"  x 第 {item['start_scene']}–{item['end_scene']} 个镜头（第 {item['batch']} 批）失败：{item['message']}"
        )
    if failures:
        lines.append("  已保留成功批次；加 --resume 可只重试失败批次")

    if args.apply and not args.dry_run:
        if failures:
            lines.append("（有失败批次，本次未同步到章节）")
            payload = {key: value for key, value in result.items() if key != "markup"} | {"markup_path": str(out_path)}
        else:
            script = result["script"]
            validate(script, "director-script.schema.json")
            store.save_script(script)
            from .cli_voice import _apply_script_to_chapter

            chapter = store.load_chapter(chapter_id)
            summary = _apply_script_to_chapter(store, chapter, script)
            lines.append(
                f"已同步到章节：新建场景 {summary['scenes_created']}，新增台词 {summary['lines_added']}，"
                f"更新台词 {summary['lines_updated']}，音效 {summary['sfx_added']}，画面 {summary['images_added']}，视频位 {summary['videos_added']}"
            )
            payload = {**{key: value for key, value in result.items() if key != "markup"}, "apply": summary, "markup_path": str(out_path)}
    else:
        payload = {key: value for key, value in result.items() if key != "markup"} | {"markup_path": str(out_path)}
        if args.dry_run:
            lines.append("（dry-run，未写盘）")
    _emit(args, "\n".join(lines), payload)
    return EXIT_OK


def cmd_setting_dispatch(args, config: AppConfig) -> int:
    """设定库命令的统一入口（转发到 `cli_setting`）。

    参数:
        args: 参数。
        config: 本机配置。

    返回:
        退出码。
    """
    from .cli_setting import cmd_setting

    return cmd_setting(args, config)


def _dumps(payload: dict) -> str:
    """调试用 JSON 序列化。

    参数:
        payload: 任意 dict。

    返回:
        JSON 文本。
    """
    return json.dumps(payload, ensure_ascii=False, indent=2)
