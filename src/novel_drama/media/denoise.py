"""参考音频降噪：用 ffmpeg afftdn 在 48kHz 上轻度去底噪，再降到 24kHz。

只服务音色库的录制/上传链路。任何失败（ffmpeg 缺失、命令失败、过度抑制）都返回
`enabled=False`，由调用方保留原音，绝不阻断保存。
"""

from __future__ import annotations

import math
import os
from pathlib import Path

import numpy as np
import soundfile as sf

from ..adapters.base import AdapterError
from .ffmpeg import binary_available, run

DEFAULT_ENGINE = "ffmpeg_afftdn"
DEFAULT_NOISE_REDUCTION_DB = 12.0
DEFAULT_NOISE_FLOOR_DB = -50.0
DEFAULT_MAX_ATTENUATION_DB = 20.0


def _rms_db(path: Path) -> float | None:
    """读取 wav 的整体 RMS（dBFS）。

    参数:
        path: wav 路径。

    返回:
        RMS 分贝值；读取失败或空音频返回 None。
    """
    try:
        data, _sample_rate = sf.read(str(path), dtype="float32", always_2d=False)
    except Exception:  # noqa: BLE001 - 读取失败只用于保护判断，不影响主流程
        return None
    if data is None or len(data) == 0:
        return None
    samples = data.astype(np.float64, copy=False)
    rms = float(np.sqrt(np.mean(samples * samples)))
    if rms <= 0:
        return -120.0
    return 20 * math.log10(rms)


def _measure_floor_db(path: Path) -> float | None:
    """测量安静段底噪：取 50ms 帧 RMS 的第 10 百分位。

    参数:
        path: wav 路径。

    返回:
        底噪（dBFS）；读取失败或片段过短返回 None。
    """
    try:
        data, sample_rate = sf.read(str(path), dtype="float32", always_2d=False)
    except Exception:  # noqa: BLE001 - 测量失败由调用方兜底
        return None
    if data is None or len(data) == 0 or sample_rate <= 0:
        return None
    samples = data.astype(np.float64, copy=False)
    frame = max(1, int(sample_rate * 0.05))
    usable = len(samples) // frame * frame
    if usable < frame * 4:
        return None
    frames = samples[:usable].reshape(-1, frame)
    frame_rms = np.sqrt(np.mean(frames * frames, axis=1))
    quiet = float(np.percentile(frame_rms, 10))
    if quiet <= 0:
        return -120.0
    return 20 * math.log10(quiet)


def _estimate_noise_floor_db(path: Path, fallback: float) -> float:
    """估计底噪门限：优先按音频自身的安静段估，读不到时用配置值兜底。

    固定门限（如 -50dB）对不同录音的底噪差异没有适应性：底噪高于门限时几乎不降，
    低于门限时又容易过度抑制。

    参数:
        path: 已转码的原始 wav。
        fallback: 配置里的 `noise_floor_db`。

    返回:
        门限（dBFS），限制在 -80 ~ -20 之间。
    """
    measured = _measure_floor_db(path)
    if measured is None:
        return fallback
    return max(-80.0, min(-20.0, measured))


def denoise_wav(src: Path, dst: Path, cfg: dict | None = None, ffmpeg_binary: str = "ffmpeg") -> dict:
    """把 src 轻度降噪后写到 dst（24k 单声道 s16le）。

    降噪链路固定为 `aresample=48000 → afftdn → aresample=24000`：
    先在 48kHz 上做频谱减法，再降到 IndexTTS 需要的 24kHz，避免在错误采样率上处理。

    参数:
        src: 已转码的原始 wav（24k 单声道）。
        dst: 降噪后的目标 wav；失败时不留下半成品。
        cfg: `voice.denoise` 配置段（缺省用内置默认值）。
        ffmpeg_binary: ffmpeg 可执行文件名。

    返回:
        `{enabled, engine, note, attenuation_db}`；`enabled=False` 表示未降噪，
        调用方应继续使用原音。
    """
    conf = cfg or {}
    engine = str(conf.get("engine") or DEFAULT_ENGINE)
    noise_reduction = float(conf.get("noise_reduction_db") or DEFAULT_NOISE_REDUCTION_DB)
    configured_floor = float(conf.get("noise_floor_db") or DEFAULT_NOISE_FLOOR_DB)
    max_attenuation = abs(float(conf.get("max_attenuation_db") or DEFAULT_MAX_ATTENUATION_DB))

    if not binary_available(ffmpeg_binary):
        return {"enabled": False, "engine": engine, "note": f"未找到 {ffmpeg_binary}，已保留原音", "attenuation_db": None}

    noise_floor = _estimate_noise_floor_db(src, configured_floor)

    tmp = dst.with_name(f"{dst.stem}.denoise.tmp.wav")
    tmp.unlink(missing_ok=True)
    chain = f"aresample=48000,afftdn=nr={noise_reduction:g}:nf={noise_floor:g},aresample=24000"
    try:
        run(
            [
                ffmpeg_binary,
                "-v",
                "error",
                "-y",
                "-i",
                str(src),
                "-af",
                chain,
                "-ac",
                "1",
                "-ar",
                "24000",
                "-c:a",
                "pcm_s16le",
                str(tmp),
            ],
            timeout_s=120,
        )
    except (AdapterError, OSError) as exc:
        tmp.unlink(missing_ok=True)
        return {"enabled": False, "engine": engine, "note": f"降噪失败，已保留原音：{exc}", "attenuation_db": None}

    before = _rms_db(src)
    after = _rms_db(tmp)
    if before is None or after is None:
        tmp.unlink(missing_ok=True)
        return {"enabled": False, "engine": engine, "note": "降噪结果无法读取，已保留原音", "attenuation_db": None}
    attenuation = round(before - after, 1)
    if attenuation > max_attenuation:
        tmp.unlink(missing_ok=True)
        return {
            "enabled": False,
            "engine": engine,
            "note": f"降噪过强（音量下降 {attenuation}dB，阈值 {max_attenuation:g}dB），已保留原音",
            "attenuation_db": attenuation,
        }

    os.replace(tmp, dst)
    floor_before = _measure_floor_db(src)
    floor_after = _measure_floor_db(dst)
    drop = None if floor_before is None or floor_after is None else round(floor_before - floor_after, 1)
    note = f"已降噪（安静段 {floor_before:.1f} → {floor_after:.1f} dBFS）" if drop is not None else "已降噪"
    return {
        "enabled": True,
        "engine": engine,
        "note": note,
        "attenuation_db": attenuation,
        "noise_floor_drop_db": drop,
    }
