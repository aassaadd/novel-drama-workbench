"""混音骨架：把时间轴的 voice / sfx / music 三轨分别渲染成分轨，再合成整章混音。

规则：
- 每个 item 独立增益、独立淡入淡出；`loop` 的音乐 cue 会循环铺到指定结束时间；
- 没有素材的轨道输出等长静音轨，保证「无音效也能导出」；
- 分轨（stem）与混音（raw / normalized）全部留档；
- 混音原始产物保留，规整只在最终产物上做一次，避免二次转换损质。
"""

from __future__ import annotations

import resource
import time
from pathlib import Path

from . import normalize
from .ffmpeg import binary_available, probe_duration_ms, run

KINDS = ("voice", "sfx", "music")


def peak_rss_mb() -> float:
    """当前进程峰值内存（MB）。"""
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 / 1024, 1)


def _asset_path(root: Path, asset_id: str) -> Path:
    """把 asset_id 解析成绝对路径。"""
    path = Path(asset_id)
    return path if path.is_absolute() else root / asset_id


def plan_mix(
    timeline: dict,
    project_root: str | Path,
    bgm_cues: list[dict] | None = None,
    output_name: str | None = None,
) -> dict:
    """由时间轴与 BGM cue 生成混音计划（不落盘、不执行）。

    参数:
        timeline: 时间轴 dict。
        project_root: 工程根目录。
        bgm_cues: 章节 BGM cue 列表；会并入 music 轨。
        output_name: 混音输出文件名；缺省 `<chapter>.mix.wav`。

    返回:
        {chapter_id, duration_ms, sample_rate, channels, tracks, outputs, warnings}。
    """
    root = Path(project_root)
    chapter_id = timeline.get("chapter_id") or "chapter"
    duration_ms = int(timeline.get("duration_ms") or 0)
    sample_rate = int(timeline.get("sample_rate") or 48000)
    channels = int(timeline.get("channels") or 2)
    warnings = list(timeline.get("warnings") or [])

    tracks: dict[str, list[dict]] = {kind: [] for kind in KINDS}
    for track in timeline.get("tracks") or []:
        kind = track.get("kind")
        if kind not in tracks:
            continue
        for item in track.get("items") or []:
            asset = item.get("asset_id")
            if not asset:
                continue
            path = _asset_path(root, asset)
            if not path.exists():
                warnings.append(f"{kind} 轨素材不存在，已跳过: {asset}")
                continue
            tracks[kind].append(
                {
                    "source_id": item.get("source_id"),
                    "path": str(path),
                    "start_ms": int(item.get("start_ms") or 0),
                    "end_ms": int(item.get("end_ms") or 0),
                    "gain_db": float(item.get("gain_db") or 0.0),
                    "fade_in_ms": int(item.get("fade_in_ms") or 0),
                    "fade_out_ms": int(item.get("fade_out_ms") or 0),
                    "loop": False,
                    "loop_until_ms": None,
                }
            )

    for cue in bgm_cues or []:
        asset = cue.get("asset_id")
        if not asset:
            continue
        path = _asset_path(root, asset)
        if not path.exists():
            warnings.append(f"BGM 素材不存在，已跳过: {asset}")
            continue
        start = int(cue.get("start_ms") or 0)
        until = cue.get("loop_until_ms")
        tracks["music"].append(
            {
                "source_id": cue.get("id"),
                "path": str(path),
                "start_ms": start,
                "end_ms": int(until) if until else duration_ms,
                "gain_db": float(cue.get("gain_db") or -18.0),
                "fade_in_ms": int(cue.get("fade_in_ms") or 0),
                "fade_out_ms": int(cue.get("fade_out_ms") or 0),
                "loop": bool(cue.get("loop")),
                "loop_until_ms": int(until) if until else None,
            }
        )

    if duration_ms <= 0:
        duration_ms = max([item["end_ms"] for items in tracks.values() for item in items] or [0])
    if not any(tracks[kind] for kind in KINDS):
        warnings.append("三条轨道都没有素材，将输出等长静音混音")

    renders = root / "renders"
    outputs = {
        "voice": str(renders / f"{chapter_id}.stem.voice.wav"),
        "sfx": str(renders / f"{chapter_id}.stem.sfx.wav"),
        "music": str(renders / f"{chapter_id}.stem.music.wav"),
        "mix": str(renders / (output_name or f"{chapter_id}.mix.wav")),
        "normalized": str(renders / (output_name.replace(".wav", ".norm.wav") if output_name else f"{chapter_id}.mix.norm.wav")),
    }
    return {
        "chapter_id": chapter_id,
        "duration_ms": duration_ms,
        "sample_rate": sample_rate,
        "channels": channels,
        "tracks": tracks,
        "outputs": outputs,
        "warnings": warnings,
    }


def _stem_filter(items: list[dict], sample_rate: int, layout: str) -> tuple[list[str], str]:
    """拼一条轨道的 ffmpeg 参数（-i 与 filter_complex 片段）。

    参数:
        items: 该轨的素材条目。
        sample_rate: 输出采样率。
        layout: 声道布局（mono/stereo）。

    返回:
        (输入参数列表, filter 表达式)；无素材时返回空列表与空串。
    """
    args: list[str] = []
    filters: list[str] = []
    labels: list[str] = []
    for index, item in enumerate(items):
        if item.get("loop"):
            args.extend(["-stream_loop", "-1"])
        args.extend(["-i", item["path"]])
        length_ms = max(100, int(item.get("end_ms") or 0) - int(item.get("start_ms") or 0))
        length_s = length_ms / 1000
        chain = (
            f"[{index}:a]aresample={sample_rate},aformat=channel_layouts={layout},"
            f"volume={item.get('gain_db', 0.0)}dB,atrim=end={length_s:.3f},asetpts=N/SR/TB"
        )
        fade_in = int(item.get("fade_in_ms") or 0)
        if fade_in > 0:
            chain += f",afade=t=in:st=0:d={fade_in / 1000:.3f}"
        start_ms = int(item.get("start_ms") or 0)
        delay = max(0, start_ms)
        chain += f",adelay={delay}|{delay}"
        fade_out = int(item.get("fade_out_ms") or 0)
        if fade_out > 0:
            out_at = max(0.0, start_ms / 1000 + length_s - fade_out / 1000)
            chain += f",afade=t=out:st={out_at:.3f}:d={fade_out / 1000:.3f}"
        filters.append(chain + f"[s{index}]")
        labels.append(f"[s{index}]")
    if not labels:
        return [], ""
    if len(labels) == 1:
        filters.append(f"{labels[0]}anull[out]")
    else:
        filters.append(f"{''.join(labels)}amix=inputs={len(labels)}:normalize=0:duration=longest[out]")
    return args, ";".join(filters)


def _render_stem(plan: dict, kind: str, output: Path, binary: str) -> dict:
    """渲染单条分轨；无素材时输出等长静音。"""
    items = plan["tracks"].get(kind) or []
    sample_rate = plan["sample_rate"]
    channels = plan["channels"]
    layout = "mono" if channels == 1 else "stereo"
    duration_s = max(0.05, plan["duration_ms"] / 1000)
    output.parent.mkdir(parents=True, exist_ok=True)

    started = time.perf_counter()
    if not items:
        run(
            [
                binary, "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
                "-f", "lavfi", "-i", f"anullsrc=r={sample_rate}:cl={layout}",
                "-t", f"{duration_s:.3f}", "-c:a", "pcm_s16le",
                "-ar", str(sample_rate), "-ac", str(channels), str(output),
            ],
            timeout_s=1800,
        )
    else:
        args, filters = _stem_filter(items, sample_rate, layout)
        args = [binary, "-y", "-hide_banner", "-loglevel", "error", "-nostdin", *args]
        args.extend(
            [
                "-filter_complex", filters, "-map", "[out]", "-t", f"{duration_s:.3f}",
                "-c:a", "pcm_s16le", "-ar", str(sample_rate), "-ac", str(channels), str(output),
            ]
        )
        run(args, timeout_s=1800)
    return {
        "kind": kind,
        "path": str(output),
        "items": len(items),
        "duration_ms": probe_duration_ms(output),
        "peak_db": normalize.measure_peak(output),
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
    }


def render_mix(
    plan: dict,
    binary: str = "ffmpeg",
    audio_cfg: dict | None = None,
    loudness_target: float | None = None,
) -> dict:
    """执行混音：三条分轨 → 原始混音 → 规整产物。

    参数:
        plan: `plan_mix` 的返回。
        binary: ffmpeg 可执行文件。
        audio_cfg: `audio` 规整配置。
        loudness_target: 目标 LUFS。

    返回:
        {outputs, stems, mix, normalized, peak_rss_mb, elapsed_ms}。
    """
    if not binary_available(binary):
        from ..adapters.base import AdapterError, ErrorCode

        raise AdapterError(ErrorCode.UNAVAILABLE, f"未找到 {binary}，请先安装 ffmpeg（macOS: brew install ffmpeg）")

    started = time.perf_counter()
    outputs = plan["outputs"]
    stems = []
    for kind in KINDS:
        stems.append(_render_stem(plan, kind, Path(outputs[kind]), binary))

    sample_rate = int(plan["sample_rate"])
    channels = int(plan["channels"])
    duration_s = max(0.05, int(plan["duration_ms"]) / 1000)
    mix_path = Path(outputs["mix"])
    mix_path.parent.mkdir(parents=True, exist_ok=True)

    args = [binary, "-y", "-hide_banner", "-loglevel", "error", "-nostdin"]
    for stem in stems:
        args.extend(["-i", stem["path"]])
    labels = "".join(f"[{index}:a]" for index in range(len(stems)))
    filters = (
        f"{labels}amix=inputs={len(stems)}:normalize=0:duration=longest,"
        f"atrim=end={duration_s:.3f}[out]"
    )
    args.extend(
        [
            "-filter_complex", filters, "-map", "[out]", "-ar", str(sample_rate), "-ac", str(channels),
            "-c:a", "pcm_s16le", str(mix_path),
        ]
    )
    run(args, timeout_s=1800)

    normalized = normalize.normalize_audio(
        mix_path,
        outputs["normalized"],
        audio_cfg=audio_cfg,
        loudness_target=loudness_target,
        sample_rate=sample_rate,
        channels=channels,
        binary=binary,
    )
    return {
        "chapter_id": plan["chapter_id"],
        "duration_ms": probe_duration_ms(mix_path),
        "stems": stems,
        "mix": {
            "path": str(mix_path),
            "peak_db": normalize.measure_peak(mix_path),
            "loudness": normalize.measure_loudness(mix_path, binary),
        },
        "normalized": normalized,
        "warnings": plan.get("warnings") or [],
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
        "peak_rss_mb": peak_rss_mb(),
    }
