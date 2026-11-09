"""时长对齐：以目标时长为准，规则化决定「接受 / 微变速 / 重生成 / 换 provider」。

策略（阈值全部可配，见 `tts.alignment`）：
1. 误差在 `max_error_ms` 内 → 不动；
2. 需要的变速比在 `max_speed_change` 内 → `atempo` 微变速（优先）；
3. 超出且 `on_exceed=regenerate` → 同参数重生成，最多 `max_regenerate` 次；
4. 超出且 `on_exceed=provider_switch` → 换下一个可用 provider；
5. 用尽次数仍偏差过大 → `accept`（记录超差，交由时间轴吸收）。
"""

from __future__ import annotations

import time
from pathlib import Path

from ..media.ffmpeg import probe_duration_ms, run

DEFAULTS = {
    "enabled": True,
    "max_error_ms": 250,
    "max_speed_change": 0.05,
    "on_exceed": "regenerate",
    "max_regenerate": 1,
}


def resolve_config(raw: dict | None) -> dict:
    """合并默认值与用户配置，并做基本钳制。

    参数:
        raw: 配置里的 `tts.alignment`（可为空）。

    返回:
        完整配置 dict。
    """
    cfg = dict(DEFAULTS)
    for key, value in (raw or {}).items():
        if value is not None:
            cfg[key] = value
    cfg["enabled"] = bool(cfg["enabled"])
    cfg["max_error_ms"] = max(0, int(cfg["max_error_ms"]))
    cfg["max_speed_change"] = min(0.5, max(0.0, float(cfg["max_speed_change"])))
    cfg["max_regenerate"] = max(0, int(cfg["max_regenerate"]))
    if cfg["on_exceed"] not in ("regenerate", "provider_switch", "accept"):
        cfg["on_exceed"] = "regenerate"
    return cfg


def evaluate(actual_ms: int, target_ms: int | None, cfg: dict, attempt: int = 0) -> dict:
    """判断一条音频该怎么对齐。

    参数:
        actual_ms: 实际时长。
        target_ms: 目标时长；缺省或非正数时直接返回 none。
        cfg: `resolve_config` 的结果。
        attempt: 已重生成/换 provider 的次数。

    返回:
        {action, ratio, error_ms, speed_change, reason}；
        action ∈ none / tempo / regenerate / provider_switch / accept。
    """
    result = {
        "action": "none",
        "ratio": 1.0,
        "error_ms": 0,
        "speed_change": 0.0,
        "reason": "",
    }
    if not cfg.get("enabled") or not target_ms or target_ms <= 0 or actual_ms <= 0:
        result["reason"] = "未启用或缺少目标时长"
        return result

    error_ms = int(actual_ms) - int(target_ms)
    result["error_ms"] = error_ms
    # atempo 的 ratio = 目标/实际：音频变短用 >1，变长用 <1
    ratio = round(int(target_ms) / int(actual_ms), 4)
    speed_change = round(ratio - 1.0, 4)
    result["ratio"] = ratio
    result["speed_change"] = speed_change

    if abs(error_ms) <= cfg["max_error_ms"]:
        result["reason"] = f"误差 {error_ms}ms 在阈值 {cfg['max_error_ms']}ms 内"
        return result

    if abs(speed_change) <= cfg["max_speed_change"]:
        result["action"] = "tempo"
        result["reason"] = f"微变速 {speed_change:+.2%} 可命中目标"
        return result

    if attempt < cfg["max_regenerate"]:
        result["action"] = cfg["on_exceed"]
        result["reason"] = f"需变速 {speed_change:+.2%} 超出 ±{cfg['max_speed_change']:.0%}，按 {cfg['on_exceed']} 处理"
        return result

    result["action"] = "accept"
    result["reason"] = f"重试 {attempt} 次仍偏差 {error_ms}ms，接受并记录"
    return result


def apply_tempo(path: str | Path, ratio: float, binary: str = "ffmpeg", probe_binary: str = "ffprobe") -> dict:
    """用 ffmpeg atempo 原地微调语速（覆盖原文件）。

    参数:
        path: wav 路径。
        ratio: 速度倍率（>1 变快）。
        binary: ffmpeg 可执行文件。
        probe_binary: ffprobe 可执行文件。

    返回:
        {applied, ratio, before_ms, after_ms, elapsed_ms}。
    """
    target = Path(path)
    before_ms = probe_duration_ms(target, probe_binary)
    ratio = round(float(ratio), 4)
    if not target.exists() or abs(ratio - 1.0) < 1e-4:
        return {"applied": False, "ratio": 1.0, "before_ms": before_ms, "after_ms": before_ms, "elapsed_ms": 0}

    tmp = target.with_name(target.stem + ".tempo.wav")
    started = time.perf_counter()
    run(
        [
            binary,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-i",
            str(target),
            "-filter:a",
            f"atempo={ratio:.4f}",
            "-c:a",
            "pcm_s16le",
            str(tmp),
        ],
        timeout_s=600,
    )
    tmp.replace(target)
    return {
        "applied": True,
        "ratio": ratio,
        "before_ms": before_ms,
        "after_ms": probe_duration_ms(target, probe_binary),
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
    }
