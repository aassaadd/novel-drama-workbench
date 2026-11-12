"""CLI：本地生成音效（Stable Audio 3 small-sfx）。"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

from .cli_common import EXIT_OK, _emit, _resolve_store
from .config import AppConfig
from .sfx.prompting import rewrite_prompt
from .sfx.registry import build_providers, pick_provider
from .sfx.types import SfxRequest

#: 音效文件落盘的工程内目录
SFX_DIR = Path("assets/audio/sfx")
#: Stable Audio 3 单条最长 120 秒；实测 0.5 秒会退化成削波噪声，1 秒是可用下限
MIN_SECONDS = 1.0
MAX_SECONDS = 120.0


def _slug(text: str, fallback: str = "sfx") -> str:
    """把描述压成文件名友好的短标签。

    参数:
        text: 英文描述或自定义名称。
        fallback: 无可用字符时的兜底。

    返回:
        仅含小写字母/数字/下划线、长度不超过 24 的标签。
    """
    words = re.findall(r"[a-zA-Z0-9]+", text or "")[:4]
    slug = "_".join(word.lower() for word in words)[:24].strip("_")
    return slug or fallback


def cmd_sfx_generate(args, config: AppConfig) -> int:
    """按中文/英文描述生成一条音效，落盘到工程 `assets/audio/sfx/`。

    参数:
        args: 已解析参数（prompt / prompt_en / duration / steps / cfg / seed / provider / name）。
        config: 本机配置。

    返回:
        退出码。
    """
    store = _resolve_store(args, config)
    if not (args.prompt or args.prompt_en):
        raise ValueError("必须提供 --prompt（中文描述）或 --prompt-en（英文描述）")
    duration = float(args.duration)
    if not (MIN_SECONDS <= duration <= MAX_SECONDS):
        raise ValueError(f"时长必须在 {MIN_SECONDS}~{MAX_SECONDS} 秒之间：{duration}")

    if args.prompt_en:
        outcome = rewrite_prompt(args.prompt_en, duration)
    else:
        outcome = rewrite_prompt(args.prompt, duration, config=config, allow_llm=not args.no_llm)
    final_prompt = outcome.prompt

    providers = build_providers(config)
    pid, provider = pick_provider(config, providers, args.provider)

    seed = args.seed
    digest = hashlib.sha1(
        f"{final_prompt}|{duration}|{args.steps}|{args.cfg}|{seed}".encode("utf-8")
    ).hexdigest()[:8]
    stamp = time.strftime("%Y%m%dT%H%M%S")
    stem = f"sfx_{stamp}_{_slug(args.name or final_prompt)}_{digest}"
    out_dir = store.root / SFX_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    audio_path = out_dir / f"{stem}.wav"

    request = SfxRequest(
        prompt=final_prompt,
        output_path=str(audio_path),
        duration_s=duration,
        negative_prompt=args.negative,
        steps=args.steps,
        cfg=args.cfg,
        seed=seed,
    )
    try:
        result = provider.generate(request)
    finally:
        provider.close()

    # 侧车元数据：把「用的什么描述、什么参数、实测指标」和音频绑在一起，便于追溯
    meta = {
        **result.to_dict(),
        "provider_id": pid,
        "raw_prompt": args.prompt or args.prompt_en,
        "prompt": final_prompt,
        "rewritten": outcome.changed,
        "rewrite_mode": outcome.mode,
        "rewrite_note": outcome.note,
        "requested_duration_s": duration,
        "negative_prompt": args.negative,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    meta_path = audio_path.with_suffix(".json")
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    mode_text = {"llm": "LLM 改写", "dict": "词典改写", "passthrough": "原样透传"}.get(outcome.mode, outcome.mode)
    lines = [
        f"音效已生成：{audio_path.name}",
        f"提示词（{mode_text}）：{final_prompt}",
    ]
    if outcome.note:
        lines.append(f"提示：{outcome.note}")
    lines += [
        f"参数：{result.steps} 步 / cfg {result.cfg} / seed {result.seed} / device {provider.spec.defaults.get('device', 'mps')}",
        f"实测：{result.duration_s:.2f}s / {result.sample_rate}Hz / {result.channels}ch",
        f"耗时：推理 {result.latency_ms / 1000:.2f}s（模型加载 {result.load_s:.2f}s，进程峰值 {result.peak_mib:.0f} MiB）",
        f"目录：{out_dir}",
        f"元数据：{meta_path.name}",
    ]
    _emit(args, "\n".join(lines), {**meta, "audio_path": str(audio_path), "meta_path": str(meta_path)})
    return EXIT_OK


def cmd_sfx_providers(args, config: AppConfig) -> int:
    """列出音效 provider 及其可用性。

    参数:
        args: 已解析参数。
        config: 本机配置。

    返回:
        退出码。
    """
    providers = build_providers(config)
    rows = []
    for pid, provider in providers.items():
        health = provider.health()
        rows.append(
            {
                "provider_id": pid,
                "model": provider.spec.model,
                "enabled": provider.spec.enabled,
                "ok": health.ok,
                "detail": health.detail,
                "code": health.code,
                "license": provider.spec.license,
                "license_gate": provider.spec.license_gate,
            }
        )
    lines = [
        f"{row['provider_id']}（{row['model']}）：{'可用' if row['ok'] else '不可用'}"
        f"{'' if row['enabled'] else '（未启用）'}\n  {row['detail']}"
        for row in rows
    ]
    _emit(args, "\n".join(lines) or "未配置任何音效 provider", rows)
    return EXIT_OK
