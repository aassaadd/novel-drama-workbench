"""归因 A/B 单组实验：加载指定精度的 CosyVoice3，按给定参考音频合成一句，输出 JSON 指标。

用法示例：
    .venv/bin/python var/ab_test/ab_run.py \
        --model /Volumes/other/models/Fun-CosyVoice3-0.5B-2512-fp16 \
        --ref /Volumes/other/models/voice_refs/cosyvoice_zeroshot_human.wav \
        --ref-text "收到好友从远方寄来的生日礼物，那份意外的惊喜让我开心得不得了。" \
        --text "别回头。现在，把门关上。" --out var/ab_test/out/fp16_human.wav
"""

from __future__ import annotations

import argparse
import json
import resource
import time
from pathlib import Path

import mlx.core as mx
import numpy as np
import soundfile as sf


def _rss_mb() -> float:
    """进程峰值 RSS（MB）；macOS 的 ru_maxrss 单位为字节。"""
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 / 1024, 1)


def _reset_peak() -> None:
    """重置 MLX 峰值显存统计（不同版本 API 名称不同）。"""
    for name in ("reset_peak_memory", "metal.reset_peak_memory"):
        obj = mx
        for part in name.split("."):
            obj = getattr(obj, part, None)
            if obj is None:
                break
        if callable(obj):
            obj()
            return


def _peak_gb() -> float:
    """MLX 峰值显存（GB）。"""
    try:
        return round(mx.get_peak_memory() / 1e9, 3)
    except Exception:
        return -1.0


def main() -> int:
    """执行一组合成并打印 JSON 结果。"""
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--ref", default=None)
    ap.add_argument("--ref-text", default=None)
    ap.add_argument("--instruct", default=None)
    ap.add_argument("--text", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--max-tokens", type=int, default=1200)
    args = ap.parse_args()

    from mlx_audio.tts.generate import load_audio
    from mlx_audio.tts.utils import load_model

    _reset_peak()
    t0 = time.perf_counter()
    model = load_model(Path(args.model))
    load_s = round(time.perf_counter() - t0, 2)
    load_peak_gb = _peak_gb()

    ref_audio = None
    ref_s = 0.0
    if args.ref:
        t1 = time.perf_counter()
        ref_audio = load_audio(args.ref, sample_rate=model.sample_rate)
        ref_s = round(time.perf_counter() - t1, 2)

    kwargs = {
        "text": args.text,
        "ref_audio": ref_audio,
        "ref_text": args.ref_text,
        "stt_model": None,
        "verbose": False,
        "max_tokens": args.max_tokens,
    }
    if args.instruct:
        kwargs["instruct_text"] = args.instruct

    _reset_peak()
    t2 = time.perf_counter()
    error = None
    result = None
    try:
        for item in model.generate(**kwargs):
            result = item
    except Exception as exc:  # 无参考等结构性错误要如实记录
        error = f"{type(exc).__name__}: {exc}"
    synth_s = round(time.perf_counter() - t2, 2)
    synth_peak_gb = _peak_gb()

    payload = {
        "model": args.model,
        "ref": args.ref,
        "ref_text": args.ref_text,
        "instruct": args.instruct,
        "text": args.text,
        "load_s": load_s,
        "load_peak_gb": load_peak_gb,
        "synth_s": synth_s,
        "synth_peak_gb": synth_peak_gb,
        "rss_mb": _rss_mb(),
        "error": error,
        "out": None,
        "sample_rate": None,
        "duration_s": None,
    }
    if result is not None:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        audio = np.asarray(result.audio)
        sf.write(str(out), audio, int(result.sample_rate))
        payload.update(
            out=str(out),
            sample_rate=int(result.sample_rate),
            duration_s=round(len(audio) / int(result.sample_rate), 3),
            rt_ratio=round(synth_s / max(len(audio) / int(result.sample_rate), 1e-6), 3),
        )
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if result is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
