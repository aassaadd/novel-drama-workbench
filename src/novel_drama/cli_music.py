"""CLI：用 ACE-Step 生成背景音乐，支持「叙事 / 铺底」两档。"""

from __future__ import annotations

from .cli_common import EXIT_OK, _emit, _resolve_store
from .config import AppConfig
from .media.music import MODE_LABELS, render_bgm


def cmd_bgm_generate(args, config: AppConfig) -> int:
    """生成一段配乐并按档位处理后写回章节 cue。

    参数:
        args: 已解析参数（chapter / prompt / duration / mode / plan / seed / name / start_ms / no_cue）。
        config: 本机配置。

    返回:
        退出码。
    """
    store = _resolve_store(args, config)
    result = render_bgm(
        store,
        config,
        args.chapter,
        args.prompt,
        duration_s=args.duration,
        requested_mode=args.mode,
        requested_normalize=args.normalize,
        plan_id=args.plan,
        seed=args.seed,
        name=args.name,
        start_ms=args.start_ms,
        add_to_cue=not args.no_cue,
    )
    peak = result.get("true_peak")
    lufs = result.get("lufs")
    loudness = (
        f"响度 {lufs:.2f} LUFS / 真峰值 {peak:.2f} dBTP"
        if isinstance(lufs, (int, float)) and isinstance(peak, (int, float))
        else "响度未测得"
    )
    lines = [
        f"配乐已生成：{result['asset_id']}",
        f"档位：{result['mode_label']}（{MODE_LABELS[result['mode']]}，{result['mode_reason']}）",
        f"耗时：生成 {result['generation_s']:.1f}s + 后处理 {result['postprocess_s']:.1f}s",
        loudness,
    ]
    if result.get("cue"):
        lines.append(f"cue：{result['cue']['id']}  start={result['cue']['start_ms']}ms")
    lines.append(f"记录：{result['record_path']}")
    _emit(args, "\n".join(lines), result)
    return EXIT_OK
