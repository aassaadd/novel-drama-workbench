"""整章语音拼接：按时间轴把已就绪的配音片段拼成一个音频文件。

本轮只拼语音轨（不混音效）：
- 每段按 `start_ms` 延迟后叠加，空缺由静音自动补齐；
- 输出时长严格等于时间轴总时长；
- 缺失/未就绪的行记入 `missing`，不阻断拼接。
"""

from __future__ import annotations

import resource
import time
from pathlib import Path

from .ffmpeg import binary_available, probe_duration_ms, run


def peak_rss_mb() -> float:
    """当前进程峰值内存（MB）。"""
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 / 1024, 1)


def plan_concat(timeline: dict, project_root: str | Path) -> dict:
    """由时间轴生成拼接计划。

    参数:
        timeline: 时间轴 dict。
        project_root: 工程根目录。

    返回:
        {output, sample_rate, channels, duration_ms, segments, missing}。
    """
    root = Path(project_root)
    voice = next((track for track in timeline.get("tracks") or [] if track.get("kind") == "voice"), None)
    segments: list[dict] = []
    missing: list[str] = []
    for item in sorted((voice or {}).get("items") or [], key=lambda entry: entry.get("start_ms") or 0):
        asset = item.get("asset_id")
        if not asset:
            missing.append(item.get("source_id"))
            continue
        path = Path(asset)
        if not path.is_absolute():
            path = root / asset
        if not path.exists():
            missing.append(item.get("source_id"))
            continue
        segments.append(
            {
                "line_id": item.get("source_id"),
                "path": str(path),
                "start_ms": int(item.get("start_ms") or 0),
                "end_ms": int(item.get("end_ms") or 0),
            }
        )
    return {
        "output": str(root / "renders" / f"{timeline.get('chapter_id')}.voice.wav"),
        "sample_rate": int(timeline.get("sample_rate") or 48000),
        "channels": int(timeline.get("channels") or 1),
        "duration_ms": int(timeline.get("duration_ms") or 0),
        "segments": segments,
        "missing": missing,
    }


def concat_voice(plan: dict, binary: str = "ffmpeg", probe_binary: str = "ffprobe") -> dict:
    """执行拼接（ffmpeg）。

    参数:
        plan: `plan_concat` 的返回。
        binary: ffmpeg 可执行文件。
        probe_binary: ffprobe 可执行文件。

    返回:
        {path, duration_ms, size_bytes, elapsed_ms, peak_rss_mb, inputs, missing}。

    异常:
        AdapterError(UNAVAILABLE): ffmpeg 缺失或输入全部不可用。
    """
    if not binary_available(binary):
        from ..adapters.base import AdapterError, ErrorCode

        raise AdapterError(ErrorCode.UNAVAILABLE, f"未找到 {binary}，请先安装 ffmpeg（macOS: brew install ffmpeg）")

    output = Path(plan["output"])
    output.parent.mkdir(parents=True, exist_ok=True)
    sample_rate = int(plan.get("sample_rate") or 48000)
    channels = int(plan.get("channels") or 1)
    layout = "mono" if channels == 1 else "stereo"
    duration_s = max(0.05, int(plan.get("duration_ms") or 0) / 1000)
    segments = plan.get("segments") or []

    started = time.perf_counter()
    if not segments:
        args = [
            binary,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-f",
            "lavfi",
            "-i",
            f"anullsrc=r={sample_rate}:cl={layout}",
            "-t",
            f"{duration_s:.3f}",
            "-c:a",
            "pcm_s16le",
            "-ar",
            str(sample_rate),
            "-ac",
            str(channels),
            str(output),
        ]
    else:
        args = [binary, "-y", "-hide_banner", "-loglevel", "error", "-nostdin"]
        for segment in segments:
            args.extend(["-i", segment["path"]])
        filters = []
        labels = []
        for index, segment in enumerate(segments):
            delay = max(0, int(segment.get("start_ms") or 0))
            filters.append(
                f"[{index}:a]aresample={sample_rate},aformat=channel_layouts={layout},adelay={delay}:all=1[a{index}]"
            )
            labels.append(f"[a{index}]")
        if len(labels) == 1:
            filters.append(f"{labels[0]}apad[outa]")
        else:
            filters.append(f"{''.join(labels)}amix=inputs={len(labels)}:normalize=0:duration=longest,apad[outa]")
        args.extend(
            [
                "-filter_complex",
                ";".join(filters),
                "-map",
                "[outa]",
                "-t",
                f"{duration_s:.3f}",
                "-c:a",
                "pcm_s16le",
                "-ar",
                str(sample_rate),
                "-ac",
                str(channels),
                str(output),
            ]
        )
    run(args, timeout_s=3600)
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return {
        "path": str(output),
        "duration_ms": probe_duration_ms(output, probe_binary) or int(duration_s * 1000),
        "size_bytes": output.stat().st_size if output.exists() else 0,
        "elapsed_ms": elapsed_ms,
        "peak_rss_mb": peak_rss_mb(),
        "inputs": len(segments),
        "missing": plan.get("missing") or [],
    }
