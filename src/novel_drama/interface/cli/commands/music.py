"""CLI：生成背景音乐（默认项目内 Stable Audio Open，可切 ACE-Step），两档（叙事 / 铺底）。"""

from __future__ import annotations

import argparse

from novel_drama.composition import (
    MODE_LABELS,
    add_cue,
    build_timeline,
    import_asset,
    load_cues,
    render_bgm,
    sync_cues_from_plans,
)
from novel_drama.infrastructure.config import AppConfig
from novel_drama.interface.cli.common import (
    EXIT_OK,
    PreconditionError,
    _emit,
    _resolve_store,
)


def cmd_bgm_generate(args, config: AppConfig) -> int:
    """生成一段配乐并按档位处理后写回章节 cue。

    参数:
        args: 已解析参数（chapter / prompt / prompt_en / rewrite_prompt / provider /
            duration / mode / plan / seed / steps / cfg / no_llm / name / start_ms / no_cue）。
        config: 本机配置。

    返回:
        退出码。
    """
    store = _resolve_store(args, config)
    if not (getattr(args, "prompt", None) or getattr(args, "prompt_en", None) or getattr(args, "plan", None)):
        raise ValueError("必须提供 --prompt-en（英文提示词）、--plan（读取计划英文）或调试用 --rewrite-prompt + --prompt")
    result = render_bgm(
        store,
        config,
        args.chapter,
        args.prompt or "",
        duration_s=args.duration,
        requested_mode=args.mode,
        requested_normalize=args.normalize,
        requested_trim=args.trim_edges,
        plan_id=args.plan,
        seed=args.seed,
        name=args.name,
        start_ms=args.start_ms,
        add_to_cue=not args.no_cue,
        provider=args.provider,
        prompt_en=args.prompt_en,
        allow_llm=not getattr(args, "no_llm", False),
        allow_rewrite=bool(getattr(args, "rewrite_prompt", False)),
        steps=getattr(args, "steps", None),
        cfg=getattr(args, "cfg", None),
    )
    peak = result.get("true_peak")
    lufs = result.get("lufs")
    loudness = (
        f"响度 {lufs:.2f} LUFS / 真峰值 {peak:.2f} dBTP"
        if isinstance(lufs, (int, float)) and isinstance(peak, (int, float))
        else "响度未测得"
    )
    provider_label = "本地 Stable Audio Open" if result.get("provider") == "local" else "ComfyUI / ACE-Step"
    lines = [
        f"配乐已生成：{result['asset_id']}",
        f"后端：{provider_label}（{result.get('model') or '—'}）",
        f"档位：{result['mode_label']}（{MODE_LABELS[result['mode']]}，{result['mode_reason']}）",
        f"耗时：生成 {result['generation_s']:.1f}s + 后处理 {result['postprocess_s']:.1f}s",
        f"成品：{result.get('duration_s')}s · {loudness}",
    ]
    if result.get("prompt_en"):
        lines.append(f"英文提示词：{result['prompt_en']}")
    if result.get("peak_mib"):
        lines.append(f"推理：{result.get('steps')} 步 / cfg {result.get('cfg')} / 加载 {result.get('load_s')}s / 进程峰值 {result.get('peak_mib'):.0f} MiB")
    if result.get("cue"):
        lines.append(f"cue：{result['cue']['id']}  start={result['cue']['start_ms']}ms")
    lines.append(f"记录：{result['record_path']}")
    lines.append(f"侧车：{result['sidecar']}")
    _emit(args, "\n".join(lines), result)
    return EXIT_OK


def cmd_bgm(args, config: AppConfig) -> int:
    """BGM 轨管理：导入现成音频、按时间轴放置、设置增益与淡入淡出。"""
    store = _resolve_store(args, config)
    if args.action == "add":
        if not args.file:
            raise PreconditionError("请用 --file 指定要导入的音频文件")
        asset_id = import_asset(store.root, args.file)
        cue = add_cue(
            store.root,
            args.chapter,
            asset_id,
            start_ms=args.start_ms,
            gain_db=args.gain_db,
            fade_in_ms=args.fade_in_ms,
            fade_out_ms=args.fade_out_ms,
            loop=not args.no_loop,
            loop_until_ms=args.loop_until_ms,
        )
        _emit(args, f"已加入 BGM cue: {cue['id']}  {cue['asset_id']}  start={cue['start_ms']}ms  gain={cue['gain_db']}dB", cue)
        return EXIT_OK
    if args.action == "clear":
        from novel_drama.composition import save_cues

        save_cues(store.root, args.chapter, [])
        _emit(args, f"已清空 {args.chapter} 的 BGM cue", {"chapter": args.chapter})
        return EXIT_OK
    if args.action == "sync":
        chapter = store.load_chapter(args.chapter)
        try:
            script = store.load_script(args.chapter)
        except FileNotFoundError:
            raise PreconditionError("导演脚本不存在：先执行 ndw script parse / generate") from None
        timeline = build_timeline(chapter, store.settings())
        result = sync_cues_from_plans(store.root, chapter, script, timeline, dry_run=bool(args.dry_run))
        _emit(
            args,
            (
                f"{'[dry-run] ' if args.dry_run else ''}BGM cue：由描述派生 {result['planned']} 条，"
                f"保留手工 {result['manual']} 条"
            ),
            {"chapter": args.chapter, "planned": result["planned"], "manual": result["manual"], "cues": result["cues"]},
        )
        return EXIT_OK

    cues = load_cues(store.root, args.chapter)
    if getattr(args, "json", False):
        _emit(args, payload={"chapter": args.chapter, "cues": cues})
        return EXIT_OK
    if not cues:
        print(f"{args.chapter} 还没有 BGM cue")
        return EXIT_OK
    for cue in cues:
        print(
            f"{cue['id']}  {cue.get('name') or ''}  {cue['asset_id']}  "
            f"start={cue['start_ms']}ms  gain={cue.get('gain_db')}dB  "
            f"fade={cue.get('fade_in_ms')}/{cue.get('fade_out_ms')}ms  loop={cue.get('loop')}"
        )
    return EXIT_OK


def add_parser(sub) -> None:
    """注册 `bgm` 命令组。"""
    p = sub.add_parser("bgm", help="背景音乐轨（生成 / 导入现成音频、按时间轴放置）")
    p_sub = p.add_subparsers(dest="action", required=True)
    q = p_sub.add_parser("generate", help="生成配乐（默认本地 Stable Audio Open，可切 ACE-Step）")
    q.add_argument("--chapter", required=True)
    q.add_argument("--prompt", help="中文音乐描述；仅与 --rewrite-prompt 搭配做临时调试")
    q.add_argument("--prompt-en", dest="prompt_en", help="英文提示词（标注阶段产出；本地 provider 常规路径用它）")
    q.add_argument("--rewrite-prompt", action="store_true", dest="rewrite_prompt",
                   help="调试选项：把 --prompt 中文临时改写成英文（不参与常规链路）")
    q.add_argument("--provider", choices=["local", "comfyui"],
                   help="生成后端：local=项目内 Stable Audio Open（≤47 秒），comfyui=ACE-Step（10~180 秒）；缺省读配置")
    q.add_argument("--duration", type=float, help="时长秒数（local 5~47；comfyui 10~180；缺省取计划时长，否则 40）")
    q.add_argument("--mode", choices=["auto", "narrative", "bed"], default="auto",
                   help="用途档位：auto=按场景推断，narrative=叙事，bed=铺底")
    q.add_argument("--normalize", action=argparse.BooleanOptionalAction, default=None,
                   help="叙事档是否做 loudnorm（默认跟随配置；铺底档始终归一）")
    q.add_argument("--trim-edges", action=argparse.BooleanOptionalAction, default=None, dest="trim_edges",
                   help="叙事档是否裁首尾近静音并做结尾淡出（默认跟随配置）")
    q.add_argument("--plan", help="关联导演脚本里的 BGM 计划 ID（用于自动推断档位）")
    q.add_argument("--seed", type=int, help="随机种子；为空时按章节 + 档位派生")
    q.add_argument("--steps", type=int, help="本地 provider 采样步数（缺省读配置，默认 100）")
    q.add_argument("--cfg", type=float, help="本地 provider 的 cfg（缺省读配置，默认 7.0）")
    q.add_argument("--no-llm", action="store_true", dest="no_llm", help="仅与 --rewrite-prompt 搭配：跳过 LLM，只用词典改写")
    q.add_argument("--name", help="cue 展示名称；为空时取计划名或描述前 16 字")
    q.add_argument("--start-ms", type=int, default=0, dest="start_ms")
    q.add_argument("--no-cue", action="store_true", dest="no_cue", help="只生成文件，不写 cue")
    q.set_defaults(func=cmd_bgm_generate)
    q = p_sub.add_parser("add", help="导入音频并放置一条 cue")
    q.add_argument("--chapter", required=True)
    q.add_argument("--file", required=True, help="外部音频文件（会复制进 assets/audio/bgm/）")
    q.add_argument("--start-ms", type=int, default=0, dest="start_ms")
    q.add_argument("--gain-db", type=float, default=-18.0, dest="gain_db")
    q.add_argument("--fade-in-ms", type=int, default=1500, dest="fade_in_ms")
    q.add_argument("--fade-out-ms", type=int, default=2000, dest="fade_out_ms")
    q.add_argument("--no-loop", action="store_true", dest="no_loop")
    q.add_argument("--loop-until-ms", type=int, dest="loop_until_ms")
    q.set_defaults(func=cmd_bgm)
    q = p_sub.add_parser("list", help="列出章节 BGM cue")
    q.add_argument("--chapter", required=True)
    q.set_defaults(func=cmd_bgm)
    q = p_sub.add_parser("clear", help="清空章节 BGM cue")
    q.add_argument("--chapter", required=True)
    q.set_defaults(func=cmd_bgm)
    q = p_sub.add_parser("sync", help="按导演脚本的 BGM 描述重建 cue（保留手工 cue 与选曲）")
    q.add_argument("--chapter", required=True)
    q.add_argument("--dry-run", action="store_true")
    q.set_defaults(func=cmd_bgm)
