"""离线媒体渲染器：音效 / 关键图 / 短视频位。

没有模型也能产出**真实文件**：
- 音效：ffmpeg 合成（粉噪、脉冲、低频冲击、扫频、和弦），确定性可复现；
- 关键图：Pillow 绘制的占位画面（色块 + 标题 + 提示词），真实 PNG；
- 短视频位：ffmpeg 把关键图渲染成 H.264 的 ≤5s 静音片段。

三者都返回实测元数据（时长/尺寸/sha256），供上层回写 asset 状态。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path


def _sha256(path: Path) -> str:
    """计算文件 sha256。

    参数:
        path: 文件路径。

    返回:
        `sha256:<hex>` 字符串。
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def _run(cmd: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
    """执行外部命令并返回结果（失败时抛 RuntimeError 带原始 stderr）。

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
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-6:]
        raise RuntimeError(f"命令失败（退出码 {proc.returncode}）：{' '.join(cmd)}\n" + "\n".join(tail))
    return proc


def probe_duration_ms(path: Path, binary: str = "ffprobe") -> int:
    """用 ffprobe 读取媒体文件真实时长。

    参数:
        path: 媒体文件。
        binary: ffprobe 可执行文件。

    返回:
        时长毫秒；读取失败返回 0。
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


# ---------------- 音效 ----------------


def _sfx_filter(kind: str, duration: float, seed: int) -> str:
    """按音效类型生成 ffmpeg lavfi 音频表达式。

    参数:
        kind: ambience/foley/impact/transition/music_sting。
        duration: 时长（秒）。
        seed: 确定性种子（用于噪声源）。

    返回:
        lavfi 输入描述字符串。
    """
    d = f"{duration:.3f}"
    if kind == "ambience":
        return f"anoisesrc=color=pink:amplitude=0.35:duration={d}:seed={seed},lowpass=f=1400,volume=0.55"
    if kind == "foley":
        return f"anoisesrc=color=white:amplitude=0.55:duration={d}:seed={seed},bandpass=f=1800:width_type=h:w=900,afade=t=out:st=0:d={d}:curve=exp"
    if kind == "impact":
        return f"aevalsrc=0.85*sin(2*PI*(120-95*t/{d})*t):d={d},afade=t=out:st=0:d={d}:curve=exp"
    if kind == "transition":
        return f"aevalsrc=0.4*sin(2*PI*(200+1800*t/{d})*t):d={d},afade=t=in:st=0:d=0.15,afade=t=out:st={max(0.0, duration - 0.3):.3f}:d=0.3"
    if kind == "music_sting":
        return (
            f"aevalsrc=0.22*sin(2*PI*261.63*t)+0.22*sin(2*PI*329.63*t)+0.22*sin(2*PI*392*t):d={d},"
            f"afade=t=in:st=0:d=0.1,afade=t=out:st=0:d={d}:curve=exp"
        )
    raise ValueError(f"未知音效类型: {kind}（可选 ambience/foley/impact/transition/music_sting）")


def render_sfx(event: dict, out_path: str | Path, binary: str = "ffmpeg", sample_rate: int = 48000) -> dict:
    """合成一条音效并写盘。

    参数:
        event: 音效事件（kind/prompt/duration_ms）。
        out_path: 输出 wav 路径。
        binary: ffmpeg 可执行文件。
        sample_rate: 采样率。

    返回:
        {"path", "duration_ms", "sample_rate", "channels", "sha256"}。

    异常:
        RuntimeError: ffmpeg 失败。
        ValueError: kind 非法或时长越界。
    """
    kind = event.get("kind") or "ambience"
    duration_ms = int(event.get("duration_ms") or 3000)
    if duration_ms < 500 or duration_ms > 60000:
        raise ValueError(f"音效时长必须在 500~60000ms：{duration_ms}")
    duration = duration_ms / 1000.0
    seed = int(hashlib.sha256(f"{kind}:{event.get('prompt') or ''}:{duration_ms}".encode("utf-8")).hexdigest()[:6], 16)
    target = Path(out_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        binary,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        _sfx_filter(kind, duration, seed),
        "-ac",
        "2",
        "-ar",
        str(sample_rate),
        "-c:a",
        "pcm_s16le",
        str(target),
    ]
    _run(cmd)
    actual = probe_duration_ms(target) or duration_ms
    return {
        "path": str(target),
        "duration_ms": actual,
        "sample_rate": sample_rate,
        "channels": 2,
        "sha256": _sha256(target),
        "kind": kind,
        "prompt": event.get("prompt") or "",
    }


# ---------------- 关键图 ----------------


def _pick_font(size: int):
    """挑选可用的中文字体。

    参数:
        size: 字号。

    返回:
        PIL.ImageFont 实例。
    """
    from PIL import ImageFont

    for candidate in (
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/Library/Fonts/Arial Unicode.ttf",
    ):
        if Path(candidate).exists():
            try:
                return ImageFont.truetype(candidate, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _wrap(text: str, width: int) -> list[str]:
    """按每行字数折行（中文按字符切）。

    参数:
        text: 原文。
        width: 每行字符数。

    返回:
        行列表。
    """
    text = " ".join((text or "").split())
    return [text[index : index + width] for index in range(0, len(text), width)] or [""]


def render_image(item: dict, out_path: str | Path) -> dict:
    """渲染一张占位关键图（真实 PNG）。

    参数:
        item: 关键图条目（prompt/width/height/aspect_ratio）。
        out_path: 输出 png 路径。

    返回:
        {"path", "width", "height", "sha256", "prompt"}。

    异常:
        RuntimeError: Pillow 不可用或写盘失败。
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError as exc:  # pragma: no cover - 依赖缺失时给出可操作提示
        raise RuntimeError("缺少 Pillow：请在项目 venv 中安装 pillow 后重试") from exc

    prompt = (item.get("prompt") or "").strip()
    width = int(item.get("width") or 1152)
    height = int(item.get("height") or 1536)
    # 控制占位图体积：长边不超过 1536
    scale = min(1.0, 1536 / max(width, height))
    width, height = max(64, int(width * scale)), max(64, int(height * scale))

    digest = hashlib.sha256(prompt.encode("utf-8")).digest()
    base = (digest[0] // 2 + 20, digest[1] // 2 + 20, digest[2] // 2 + 20)
    accent = (min(255, base[0] + 70), min(255, base[1] + 50), min(255, base[2] + 40))

    image = Image.new("RGB", (width, height), base)
    draw = ImageDraw.Draw(image)
    # 顶部渐变色带（用多条矩形模拟）
    band = max(8, height // 90)
    for index in range(band):
        ratio = index / band
        color = tuple(int(base[channel] + (accent[channel] - base[channel]) * ratio) for channel in range(3))
        draw.rectangle([0, index * 3, width, index * 3 + 3], fill=color)

    title_font = _pick_font(max(20, width // 20))
    body_font = _pick_font(max(14, width // 34))
    margin = max(16, width // 20)
    draw.text((margin, margin), f"占位画面 · {item.get('id') or 'key_image'}", font=title_font, fill=(255, 255, 255))
    lines = _wrap(prompt or "（无提示词）", max(8, (width - margin * 2) // max(10, width // 34)))
    y = margin + width // 16
    for line in lines[:12]:
        draw.text((margin, y), line, font=body_font, fill=(235, 235, 235))
        y += max(20, width // 30)
    draw.text(
        (margin, height - margin - 8),
        f"{width}×{height} · offline-render",
        font=body_font,
        fill=(200, 200, 200),
    )

    target = Path(out_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    image.save(target, format="PNG")
    return {"path": str(target), "width": width, "height": height, "sha256": _sha256(target), "prompt": prompt}


# ---------------- 短视频位 ----------------


def render_video(segment: dict, out_path: str | Path, source_image: str | Path | None, binary: str = "ffmpeg") -> dict:
    """把关键图渲染成一段 H.264 静音短视频。

    参数:
        segment: 视频段（duration_ms/fps/prompt）。
        out_path: 输出 mp4 路径。
        source_image: 首帧图片；为空时用纯色兜底。
        binary: ffmpeg 可执行文件。

    返回:
        {"path", "duration_ms", "fps", "sha256"}。

    异常:
        RuntimeError: ffmpeg 失败。
        ValueError: 时长越界。
    """
    duration_ms = int(segment.get("duration_ms") or 5000)
    if duration_ms < 500 or duration_ms > 5000:
        raise ValueError(f"视频位时长必须在 500~5000ms：{duration_ms}")
    fps = int(segment.get("fps") or 24)
    target = Path(out_path)
    target.parent.mkdir(parents=True, exist_ok=True)

    if source_image and Path(source_image).exists():
        inputs = ["-loop", "1", "-i", str(source_image)]
    else:
        # 没有源图时生成纯色输入，保证视频位永远可出片
        inputs = ["-f", "lavfi", "-i", f"color=c=0x1f2430:s=1280x720:d={duration_ms / 1000.0:.2f}"]

    cmd = [
        binary,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        *inputs,
        "-t",
        f"{duration_ms / 1000.0:.3f}",
        "-vf",
        "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,format=yuv420p",
        "-r",
        str(fps),
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "28",
        "-pix_fmt",
        "yuv420p",
        "-an",
        str(target),
    ]
    _run(cmd)
    actual = probe_duration_ms(target) or duration_ms
    return {"path": str(target), "duration_ms": actual, "fps": fps, "sha256": _sha256(target)}
