"""ffmpeg 封装：只做命令拼装与子进程调用，不承载业务规则。"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from ..adapters.base import AdapterError, ErrorCode


def binary_available(name: str = "ffmpeg") -> bool:
    """检查可执行文件是否在 PATH 中。

    参数:
        name: 可执行文件名。

    返回:
        是否存在。
    """
    return shutil.which(name) is not None


def run(args: list[str], timeout_s: float = 3600.0) -> subprocess.CompletedProcess:
    """执行 ffmpeg/ffprobe 命令。

    参数:
        args: 完整命令行参数（第一个元素为可执行文件）。
        timeout_s: 超时秒数。

    返回:
        CompletedProcess。

    异常:
        AdapterError(UNAVAILABLE): 可执行文件缺失。
        AdapterError(TIMEOUT): 执行超时。
        AdapterError(UPSTREAM_ERROR): 返回码非 0。
    """
    if not binary_available(args[0]):
        raise AdapterError(ErrorCode.UNAVAILABLE, f"未找到 {args[0]}，请先安装 ffmpeg（macOS 可用 brew install ffmpeg）")
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout_s, check=False)
    except subprocess.TimeoutExpired as exc:
        raise AdapterError(ErrorCode.TIMEOUT, f"ffmpeg 执行超时（{timeout_s}s）", retryable=True) from exc
    if result.returncode != 0:
        raise AdapterError(ErrorCode.UPSTREAM_ERROR, f"{args[0]} 执行失败: {result.stderr[-500:]}", detail={"stderr": result.stderr[-2000:]})
    return result


def probe_duration_ms(path: str | Path, probe_binary: str = "ffprobe") -> int:
    """读取媒体文件时长（毫秒）。

    参数:
        path: 媒体文件路径。
        probe_binary: ffprobe 可执行文件名。

    返回:
        时长毫秒（无法解析时返回 0）。
    """
    if not Path(path).exists():
        return 0
    result = run([probe_binary, "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)], timeout_s=60)
    try:
        return int(float(result.stdout.strip()) * 1000)
    except ValueError:
        return 0
