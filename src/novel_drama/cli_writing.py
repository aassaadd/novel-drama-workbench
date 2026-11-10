"""写作侧 CLI：`ndw outline ...` / `ndw chapter generate` / `ndw script suggest ...`。

模型调用由 `writing/service.py` 统一装配：默认离线实现，零依赖可跑通。
"""

from __future__ import annotations

import json
from pathlib import Path

from .cli_common import EXIT_OK, PreconditionError, _emit, _resolve_store
from .config import AppConfig
from .store import validate
from .store.settings_store import SettingsStore
from .writing import apply_outline, generate_chapter, generate_outline, suggest_script


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
        )
        head = f"[dry-run] " if args.dry_run else ""
        golden = result.get("golden_three") or {"passed": True, "issues": []}
        hints = ""
        if not golden["passed"]:
            hints = "\n黄金三章提示（不阻断）：\n" + "\n".join(
                f"  第 {issue['chapter']} 章：{issue['problem']}——{issue['hint']}" for issue in golden["issues"]
            )
        _emit(
            args,
            f"{head}大纲已生成：{result['acts'].__len__()} 幕 / {result['chapters']} 章 / {result['beats']} 个节拍"
            f"（provider={result['provider']}，settings_revision={result['settings_revision']}，binding={result.get('binding')}）\n"
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
        args: 参数（chapter/regen/dry-run）。
        config: 本机配置。

    返回:
        退出码。
    """
    store = _resolve_store(args, config)
    result = generate_chapter(store, config, args.chapter, regen=args.regen, dry_run=args.dry_run)
    head = "[dry-run] " if args.dry_run else ""
    text = f"{head}章节 {result['chapter_id']} 正文生成完成：{result['words']} 字（provider={result['provider']}）"
    if args.dry_run and result.get("content"):
        preview = result["content"][:400]
        text += f"\n--- 预览（前 400 字）---\n{preview}"
    _emit(args, text, {key: value for key, value in result.items() if key != "content"} | {"preview": (result.get("content") or "")[:400]})
    return EXIT_OK


def cmd_script_suggest(args, config: AppConfig) -> int:
    """生成导演标注建议，可选直接落盘并同步到章节。

    参数:
        args: 参数（chapter/apply/dry-run/out）。
        config: 本机配置。

    返回:
        退出码。
    """
    store = _resolve_store(args, config)
    result = suggest_script(store, config, args.chapter, dry_run=args.dry_run)
    markup = result["markup"]
    chapter_id = args.chapter

    out_path = Path(args.out) if getattr(args, "out", None) else store.root / "scripts" / f"{chapter_id}.markup.txt"
    if not args.dry_run:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(markup + "\n", encoding="utf-8")

    lines = [
        f"标注建议：镜头 {result['shots']} 个，台词 {result['lines']} 行，告警 {len(result['warnings'])} 条（provider={result['provider']}）",
        f"标注文本: {out_path}",
    ]
    lines.extend(f"  ! {item}" for item in result["warnings"])

    if args.apply and not args.dry_run:
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
