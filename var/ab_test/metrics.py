"""音频客观指标：频谱上限、高频能量比、基频抖动、削波比例、积分响度。

用于对比「同一句话」在不同量化/音色/风格下的差异。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import librosa
import numpy as np
import soundfile as sf


def _hnr_db(data: np.ndarray, sr: int) -> float:
    """估算谐噪比（dB）：逐帧自相关峰值，衡量嗓音「毛刺/电流感」。

    参数:
        data: 单声道波形。
        sr: 采样率。

    返回:
        有声帧的平均 HNR（dB）；越高越干净，越低越沙哑/电子。
    """
    frame = int(0.04 * sr)
    hop = int(0.02 * sr)
    values = []
    for start in range(0, len(data) - frame, hop):
        chunk = data[start : start + frame].astype(np.float64)
        if np.sqrt(np.mean(chunk**2)) < 1e-3:
            continue
        chunk = chunk - chunk.mean()
        corr = np.correlate(chunk, chunk, mode="full")[frame - 1 :]
        corr /= corr[0] + 1e-12
        lo, hi = int(sr / 500), int(sr / 70)
        if hi >= len(corr):
            continue
        r = float(np.max(corr[lo:hi]))
        r = min(max(r, 1e-4), 0.9999)
        values.append(10 * np.log10(r / (1 - r)))
    return round(float(np.mean(values)), 2) if values else -1.0


def analyze(path: str) -> dict:
    """计算单个 wav 的客观指标。

    参数:
        path: wav 路径。

    返回:
        指标字典（采样率、时长、频谱上限、高频能量比、基频抖动、削波、响度）。
    """
    data, sr = sf.read(path, always_2d=False)
    if data.ndim > 1:
        data = data.mean(axis=1)
    data = data.astype(np.float32)

    # 削波：达到满刻度的样本占比
    clip = float(np.mean(np.abs(data) >= 0.999))

    # 频谱：平均幅度谱，取相对峰值 -60 dB 的最高频率作为实际上限
    n_fft = 2048
    hop = 512
    spec = np.abs(librosa.stft(data, n_fft=n_fft, hop_length=hop))
    avg = spec.mean(axis=1)
    freqs = librosa.fft_frequencies(sr=sr, n_fft=n_fft)
    db = 20 * np.log10(np.maximum(avg, 1e-12))
    peak_db = db.max()
    above = freqs[db >= peak_db - 60]
    ceiling = float(above.max()) if above.size else 0.0
    # 高频能量比：>8 kHz 与 >12 kHz 占总能量（功率）比例
    power = avg**2
    total = float(power.sum()) or 1e-12
    hf8 = float(power[freqs >= 8000].sum() / total)
    hf12 = float(power[freqs >= 12000].sum() / total)
    centroid = float(librosa.feature.spectral_centroid(S=spec, sr=sr).mean())

    # 基频：pyin 提取有声帧，算局部抖动与音分标准差
    f0, voiced, _ = librosa.pyin(
        data.astype(np.float64), fmin=70, fmax=500, sr=sr, frame_length=2048
    )
    f0v = f0[np.isfinite(f0)]
    if f0v.size > 3:
        jitter = float(np.mean(np.abs(np.diff(f0v))) / np.mean(f0v))
        cents = float(np.std(1200 * np.log2(f0v / np.median(f0v))))
        f0_median = float(np.median(f0v))
    else:
        jitter = cents = f0_median = -1.0

    # 响度：ITU-R BS.1770 积分响度
    try:
        import pyloudnorm as pyln

        lufs = float(pyln.Meter(sr).integrated_loudness(data.astype(np.float64)))
    except Exception:
        lufs = float("nan")

    return {
        "path": path,
        "sr": int(sr),
        "duration_s": round(len(data) / sr, 3),
        "peak_dbfs": round(float(np.max(np.abs(data))), 4),
        "clipping_ratio": round(clip, 6),
        "lufs": round(lufs, 2) if lufs == lufs else None,
        "hnr_db": _hnr_db(data, sr),
        "spectral_flatness": round(float(librosa.feature.spectral_flatness(y=data).mean()), 5),
        "spectral_ceiling_hz": round(ceiling, 1),
        "hf8_ratio": round(hf8, 5),
        "hf12_ratio": round(hf12, 6),
        "centroid_hz": round(centroid, 1),
        "f0_median_hz": round(f0_median, 2),
        "f0_jitter": round(jitter, 5),
        "f0_cents_std": round(cents, 1),
        "voiced_frames": int(np.isfinite(f0).sum()),
    }


def main() -> int:
    """命令行入口：对多个 wav 依次算指标并打印 JSON 数组。"""
    rows = [analyze(p) for p in sys.argv[1:]]
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
