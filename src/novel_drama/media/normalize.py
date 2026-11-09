"""音频规整：整章渲染阶段统一采样率、响度、低频清理与轻微高频提升。

只作用于「整章拼接 / 混音」这类成片产物；TTS 原始 24 kHz 片段直出不重采样，
避免多次转换损质。所有阈值来自 `output.*` 与 `audio.*` 配置。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from .ffmpeg import probe_duration_ms, run

DEFAULT_AUDIO = {
    "highpass_hz": 80,
    "high_shelf_gain_db": 1.5,
    "high_shelf_hz": 9000,
    "true_peak_db": -1.5,
    "lra": 11,
}


def resolve_config(audio_cfg: dict | None, loudness_target: float | None = None) -> dict:
    """合并规整参数默认值。

    参数:
        audio_cfg: 配置里的 `audio` 段。
        loudness_target: 目标响度（LUFS），缺省 -16。

    返回:
        完整参数字典。
    """
    cfg = dict(DEFAULT_AUDIO)
    cfg.update({key: value for key, value in (audio_cfg or {}).items() if value is not None})
    cfg["target_lufs"] = float(loudness_target if loudness_target is not None else -16)
    return cfg


def _parse_json_block(text: str) -> dict:
    """从 ffmpeg stderr 中截取最后一段 JSON（loudnorm 首遍输出）。"""
    start = text.rfind("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return {}


def measure_loudness(path: str | Path, binary: str = "ffmpeg") -> dict:
    """测量响度（LUFS / 真峰值 / LRA）。

    参数:
        path: 音频路径。
        binary: ffmpeg 可执行文件。

    返回:
        {lufs, true_peak, lra}；解析失败时各项为 None。
    """
    result = run(
        [
            binary,
            "-hide_banner",
            "-nostats",
            "-i",
            str(path),
            "-af",
            "loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json",
            "-f",
            "null",
            "-",
        ],
        timeout_s=900,
    )
    data = _parse_json_block(result.stderr)
    def _num(key):
        try:
            return float(data[key])
        except (KeyError, TypeError, ValueError):
            return None

    return {"lufs": _num("input_i"), "true_peak": _num("input_tp"), "lra": _num("input_lra")}


_ASTATS_RMS = re.compile(r"RMS level dB:\s*(-?[\d.]+|-inf)")
_ASTATS_PEAK = re.compile(r"Peak level dB:\s*(-?[\d.]+|-inf)")


def _astats(path: Path, af: str, binary: str) -> dict:
    """跑一遍 astats，返回该滤镜链下的 RMS/峰值（dB）。"""
    result = run(
        [binary, "-hide_banner", "-nostats", "-i", str(path), "-af", f"{af},astats=measure_perchannel=none", "-f", "null", "-"],
        timeout_s=900,
    )
    rms = _ASTATS_RMS.findall(result.stderr)
    peak = _ASTATS_PEAK.findall(result.stderr)

    def _last(values):
        if not values:
            return None
        raw = values[-1]
        if raw == "-inf":
            return -120.0
        try:
            return float(raw)
        except ValueError:
            return None

    return {"rms_db": _last(rms), "peak_db": _last(peak)}


def measure_bands(path: str | Path, split_hz: int = 8000, binary: str = "ffmpeg") -> dict:
    """测量高频段占比（用于核对 high-shelf 是否生效）。

    参数:
        path: 音频路径。
        split_hz: 分频点。
        binary: ffmpeg 可执行文件。

    返回:
        {low_rms_db, high_rms_db, high_ratio_db}。
    """
    target = Path(path)
    low = _astats(target, f"lowpass=f={split_hz}", binary)
    high = _astats(target, f"highpass=f={split_hz}", binary)
    ratio = None
    if low.get("rms_db") is not None and high.get("rms_db") is not None:
        ratio = round(high["rms_db"] - low["rms_db"], 2)
    return {"low_rms_db": low.get("rms_db"), "high_rms_db": high.get("rms_db"), "high_ratio_db": ratio}


def measure_all(path: str | Path, binary: str = "ffmpeg") -> dict:
    """综合测量：时长 + 响度 + 频段。

    参数:
        path: 音频路径。
        binary: ffmpeg 可执行文件。

    返回:
        {duration_ms, ...响度字段, ...频段字段}。
    """
    loudness = measure_loudness(path, binary)
    bands = measure_bands(path, binary=binary)
    return {"duration_ms": probe_duration_ms(path), **loudness, **bands}


def measure_peak(path: str | Path, binary: str = "ffmpeg") -> float | None:
    """测量峰值电平（dBFS）。

    参数:
        path: 音频路径。
        binary: ffmpeg 可执行文件。

    返回:
        峰值 dBFS；解析失败返回 None。
    """
    return _astats(Path(path), "anull", binary).get("peak_db")


def normalize_audio(
    src: str | Path,
    dst: str | Path | None = None,
    audio_cfg: dict | None = None,
    loudness_target: float | None = None,
    sample_rate: int = 48000,
    channels: int = 2,
    binary: str = "ffmpeg",
    measure: bool = True,
) -> dict:
    """两遍法响度归一化 + 高通 + 轻 high-shelf，并重采样到目标规格。

    参数:
        src: 输入音频。
        dst: 输出音频；缺省为 `<src>.norm.wav`。
        audio_cfg: `audio` 配置段。
        loudness_target: 目标 LUFS。
        sample_rate: 目标采样率。
        channels: 目标声道数。
        binary: ffmpeg 可执行文件。
        measure: 是否测量前后差异（会多跑几遍 ffmpeg）。

    返回:
        {path, before, after, params, elapsed_ms}。
    """
    cfg = resolve_config(audio_cfg, loudness_target)
    source = Path(src)
    target = Path(dst) if dst else source.with_name(source.stem + ".norm.wav")
    target.parent.mkdir(parents=True, exist_ok=True)

    started = time.perf_counter()
    before = measure_all(source, binary) if measure else None

    # 第一遍：测量
    first = run(
        [
            binary,
            "-hide_banner",
            "-nostats",
            "-i",
            str(source),
            "-af",
            f"loudnorm=I={cfg['target_lufs']}:TP={cfg['true_peak_db']}:LRA={cfg['lra']}:print_format=json",
            "-f",
            "null",
            "-",
        ],
        timeout_s=1800,
    )
    measured = _parse_json_block(first.stderr)

    # 第二遍：高通 + high-shelf + 重采样 + 线性归一化
    chain = (
        f"highpass=f={cfg['highpass_hz']},"
        f"treble=g={cfg['high_shelf_gain_db']}:f={cfg['high_shelf_hz']},"
        f"aresample={sample_rate},"
        f"loudnorm=I={cfg['target_lufs']}:TP={cfg['true_peak_db']}:LRA={cfg['lra']}"
    )
    if measured.get("input_i"):
        chain += (
            f":measured_I={measured.get('input_i')}:measured_TP={measured.get('input_tp')}"
            f":measured_LRA={measured.get('input_lra')}:measured_thresh={measured.get('input_thresh')}"
            f":offset={measured.get('target_offset')}:linear=true"
        )
    run(
        [
            binary,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-i",
            str(source),
            "-af",
            chain,
            "-ar",
            str(sample_rate),
            "-ac",
            str(channels),
            "-c:a",
            "pcm_s16le",
            str(target),
        ],
        timeout_s=1800,
    )
    after = measure_all(target, binary) if measure else None
    return {
        "path": str(target),
        "before": before,
        "after": after,
        "params": {**cfg, "sample_rate": sample_rate, "channels": channels},
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
    }
