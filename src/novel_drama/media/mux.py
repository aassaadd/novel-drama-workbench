"""混流（音画合成）：把混音轨与时间轴上的视频段/关键图合成 MP4。

执行策略（不依赖任何模型）：
1. 按时间轴把「短视频段 + 关键图」排成一条视觉轨，空档用最后一张画面或纯色补齐；
2. 每个片段单独渲染成同参数的 H.264 片段（图像走 loop，视频不足则循环）；
3. concat 拼接视觉轨，再与混音 wav 合流成最终 MP4（AAC 音轨、`-shortest` 对齐）。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from ..cleanup import remove_dir_quietly
from .comfy_media import resolve_aspect

# 默认编码参数（画布宽高由工程画幅推导，不在此留默认值）
CODEC = {"video": "libx264", "audio": "aac", "fps": 24, "pix_fmt": "yuv420p"}
# 无画面时的兜底色
FALLBACK_COLOR = "0x101318"


def plan_mux(
    timeline: dict,
    project_root: str | Path,
    mix_plan: dict | None = None,
    *,
    settings: dict | None = None,
    manifest: dict | None = None,
) -> dict:
    """生成混流计划（不执行）。

    参数:
        timeline: 时间轴 dict。
        project_root: 工程根目录。
        mix_plan: `plan_mix` 的结果；为空时只描述视频/画面输入。
        settings: 工程设置；画布宽高由 `output.aspect_ratio` 推导。
        manifest: ComfyUI 模板清单；用于取工程画幅对应的像素尺寸。

    返回:
        {status, audio, video_segments, images, visual_items, output, codec, aspect_ratio, blockers}。
    """
    aspect = resolve_aspect(settings or {}, "image", manifest=manifest)
    production_audio = ((settings or {}).get("production") or {}).get("audio") or {}
    codec = {
        **CODEC,
        "width": aspect["width"],
        "height": aspect["height"],
        "audio_bitrate": f"{int(production_audio.get('bitrate_kbps') or 192)}k",
    }
    root = Path(project_root)
    videos: list[dict] = []
    images: list[dict] = []
    for track in timeline.get("tracks") or []:
        if track.get("kind") == "video":
            videos = [item for item in track.get("items") or [] if item.get("asset_id")]
        elif track.get("kind") == "image":
            images = [item for item in track.get("items") or [] if item.get("asset_id")]

    visual_items = [
        {
            "source_kind": item.get("source_kind") or "video_segment",
            "source_id": item.get("source_id"),
            "path": str(root / item["asset_id"]),
            "start_ms": int(item.get("start_ms") or 0),
            "end_ms": int(item.get("end_ms") or 0),
        }
        for item in videos + images
        if (root / item["asset_id"]).exists()
    ]
    visual_items.sort(key=lambda item: (item["start_ms"], item["source_kind"] != "video_segment"))

    # 混音计划里优先用归一化后的成品；没有归一化时退回原始 mix
    outputs = (mix_plan or {}).get("outputs") or {}
    audio = None
    if outputs.get("normalized") and Path(outputs["normalized"]).exists():
        audio = outputs["normalized"]
    elif outputs.get("mix"):
        audio = outputs["mix"]
    else:
        audio = (mix_plan or {}).get("output")
    if audio and not Path(audio).exists():
        # 允许调用方只给了未归一化的路径：同名 .norm.wav 存在就用它
        normalized = Path(audio).with_name(Path(audio).stem + ".norm.wav")
        audio = str(normalized) if normalized.exists() else audio

    blockers = []
    if not audio:
        blockers.append("缺少混音输出：先执行 ndw mix --render")
    elif not Path(audio).exists():
        blockers.append(f"混音文件不存在：{audio}（先执行 ndw mix --render）")

    return {
        "status": "planned",
        "reason": "按时间轴合成视觉轨并与混音合流",
        "audio": audio,
        "video_segments": videos,
        "images": images,
        "visual_items": visual_items,
        "duration_ms": int(timeline.get("duration_ms") or 0),
        "output": str(root / "renders" / f"{timeline.get('chapter_id')}.mp4"),
        "codec": codec,
        "aspect_ratio": aspect["ratio"],
        "blockers": blockers,
    }


def _run(cmd: list[str], timeout: int = 1800) -> subprocess.CompletedProcess:
    """执行 ffmpeg/ffprobe 命令。

    参数:
        cmd: 命令与参数。
        timeout: 超时秒数。

    返回:
        CompletedProcess。

    异常:
        RuntimeError: 退出码非 0 或超时。
    """
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"命令超时（{timeout}s）：{' '.join(cmd)}") from exc
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-8:]
        raise RuntimeError(f"命令失败（退出码 {proc.returncode}）：{' '.join(cmd)}\n" + "\n".join(tail))
    return proc


def _probe_ms(path: Path, binary: str) -> int:
    """读取媒体真实时长。

    参数:
        path: 媒体文件。
        binary: ffprobe。

    返回:
        毫秒；失败返回 0。
    """
    try:
        proc = subprocess.run(
            [binary, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if proc.returncode != 0:
            return 0
        return int(round(float(json.loads(proc.stdout)["format"]["duration"]) * 1000))
    except (subprocess.SubprocessError, KeyError, ValueError, json.JSONDecodeError):
        return 0


def _render_clip(
    out_path: Path,
    duration_ms: int,
    *,
    image: Path | None = None,
    video: Path | None = None,
    width: int,
    height: int,
    fps: int,
    binary: str,
) -> None:
    """渲染一个等长等参数的视频片段。

    参数:
        out_path: 输出片段路径。
        duration_ms: 片段时长。
        image: 静帧来源（与 video 二选一）。
        video: 视频来源。
        width: 画布宽。
        height: 画布高。
        fps: 帧率。
        binary: ffmpeg。
    """
    seconds = max(0.2, duration_ms / 1000.0)
    scale = f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,format=yuv420p"
    if video is not None:
        inputs = ["-stream_loop", "-1", "-i", str(video)]
    elif image is not None:
        inputs = ["-loop", "1", "-i", str(image)]
    else:
        inputs = ["-f", "lavfi", "-i", f"color=c={FALLBACK_COLOR}:s={width}x{height}:d={seconds:.3f}"]
    cmd = [
        binary,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        *inputs,
        "-t",
        f"{seconds:.3f}",
        "-vf",
        scale,
        "-r",
        str(fps),
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "26",
        "-pix_fmt",
        "yuv420p",
        "-an",
        str(out_path),
    ]
    _run(cmd)


def mux(plan: dict) -> dict:
    """执行混流。

    参数:
        plan: `plan_mux` 的返回（需含 audio/output/visual_items/codec）。
        plan["binary"]: 可选 ffmpeg 路径；`plan["ffprobe"]` 可选。

    返回:
        {"path", "duration_ms", "clips", "audio", "elapsed_ms"}。
        成片成功后中间片段目录 `_<章名>_mux/` 会被删除；失败时保留用于排查。

    异常:
        ValueError: 缺少音轨或参数非法。
        RuntimeError: 编码失败。
    """
    import time

    started = time.perf_counter()
    audio = plan.get("audio")
    if not audio or not Path(audio).exists():
        raise ValueError("混流缺少可用音轨：请先执行 ndw mix --render 生成混音文件")
    codec = {**CODEC, **(plan.get("codec") or {})}
    if not codec.get("width") or not codec.get("height"):
        raise ValueError("混流计划缺少画布尺寸：请用 plan_mux() 按工程画幅生成计划")
    binary = plan.get("binary") or "ffmpeg"
    ffprobe = plan.get("ffprobe") or "ffprobe"
    output = Path(plan["output"])
    output.parent.mkdir(parents=True, exist_ok=True)

    duration_ms = int(plan.get("duration_ms") or _probe_ms(Path(audio), ffprobe) or 0)
    if duration_ms <= 0:
        raise ValueError("时间轴时长为 0：先执行 ndw timeline build")

    work_dir = output.parent / f"_{output.stem}_mux"
    work_dir.mkdir(parents=True, exist_ok=True)
    clips: list[Path] = []
    poster: Path | None = None

    items = [item for item in plan.get("visual_items") or [] if Path(item.get("path", "")).exists()]
    items.sort(key=lambda item: int(item.get("start_ms") or 0))

    cursor = 0
    for index, item in enumerate(items):
        start = max(cursor, int(item.get("start_ms") or 0))
        end = min(duration_ms, int(item.get("end_ms") or 0))
        if end <= start:
            continue
        if start > cursor:
            clip = work_dir / f"clip_{len(clips):03d}.mp4"
            _render_clip(
                clip,
                start - cursor,
                image=poster,
                width=codec["width"],
                height=codec["height"],
                fps=codec["fps"],
                binary=binary,
            )
            clips.append(clip)
        clip = work_dir / f"clip_{len(clips):03d}.mp4"
        path = Path(item["path"])
        _render_clip(
            clip,
            end - start,
            video=path if item.get("source_kind") == "video_segment" else None,
            image=path if item.get("source_kind") != "video_segment" else None,
            width=codec["width"],
            height=codec["height"],
            fps=codec["fps"],
            binary=binary,
        )
        clips.append(clip)
        if item.get("source_kind") != "video_segment":
            poster = path
        cursor = end

    if cursor < duration_ms:
        clip = work_dir / f"clip_{len(clips):03d}.mp4"
        _render_clip(
            clip,
            duration_ms - cursor,
            image=poster,
            width=codec["width"],
            height=codec["height"],
            fps=codec["fps"],
            binary=binary,
        )
        clips.append(clip)

    list_file = work_dir / "clips.txt"
    list_file.write_text("".join(f"file '{clip.name}'\n" for clip in clips), encoding="utf-8")
    silent_video = work_dir / "visual.mp4"
    _run([binary, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(list_file), "-c", "copy", str(silent_video)])

    _run(
        [
            binary,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(silent_video),
            "-i",
            str(audio),
            "-c:v",
            "copy",
            "-c:a",
            codec["audio"],
            "-b:a",
            codec.get("audio_bitrate") or "192k",
            "-shortest",
            "-movflags",
            "+faststart",
            str(output),
        ]
    )
    actual = _probe_ms(output, ffprobe)
    # 成片已生成，删除中间片段目录；清理失败只告警，不阻断返回结果
    remove_dir_quietly(work_dir, "混流中间片段目录")
    return {
        "path": str(output),
        "duration_ms": actual or duration_ms,
        "clips": len(clips),
        "audio": str(audio),
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
    }
