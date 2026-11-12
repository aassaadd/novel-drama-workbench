"""CLI：生成背景音乐（默认项目内 Stable Audio Open，可切 ACE-Step），两档（叙事 / 铺底）。"""

from __future__ import annotations

from .cli_common import EXIT_OK, _emit, _resolve_store
from .config import AppConfig
from .media.music import MODE_LABELS, render_bgm


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
