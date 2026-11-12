"""媒体音效生成：provider（Stable Audio 3）优先，ffmpeg 合成兜底。

- `sfx.engine=model`（默认）：调用本项目 sfx provider，产物写 `assets/audio/sfx/`，
  与 CLI `sfx generate` 同命名；
- `sfx.engine=offline`：保留旧的 `render_sfx` ffmpeg 合成，路径仍是 `assets/sfx/<id>.wav`。

两种方式都会把状态写回条目 `asset`，页面与时间轴都只看这份状态。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from ..sfx import SfxError, SfxRequest
from ..sfx.naming import build_stem
from ..sfx.prompting import finalize_prompt
from ..sfx.registry import build_providers, pick_provider
from ..sfx.types import BAD_REQUEST
from .generate import render_sfx

#: 音效产物目录（provider 路线，与 CLI 一致）
SFX_REL_DIR = Path("assets/audio/sfx")
#: 模型最短可用时长（秒）；0.5s 会退化成噪声
MIN_SECONDS = 1.0
#: 低于该时长提示冲击类可能削波
CLIP_WARN_SECONDS = 2.0

#: Web 长驻进程复用同一 provider（模型只加载一次，空闲由 worker 自己退出）
_PROVIDERS: dict = {}
_PROVIDER_LOCK = threading.Lock()


def sfx_engine(config) -> str:
    """读取音效生成引擎。

    参数:
        config: AppConfig。

    返回:
        "model"（默认）或 "offline"。
    """
    value = str(config.get("sfx.engine") or "model").strip().lower()
    return value if value in ("model", "offline") else "model"


def get_provider(config, provider_id: str | None = None, cached: bool = True):
    """按配置取一个音效 provider。

    参数:
        config: AppConfig。
        provider_id: 指定 provider；None 取默认。
        cached: True 时进程内复用（Web 场景），False 时新建（CLI 批量结束后需 close）。

    返回:
        (provider_id, provider 实例)。

    异常:
        SfxError: provider 不可用或未知。
    """
    with _PROVIDER_LOCK:
        if cached:
            key = provider_id or str(config.get("sfx.default_provider_id") or "")
            if key in _PROVIDERS:
                return key, _PROVIDERS[key]
        try:
            providers = build_providers(config)
            pid, provider = pick_provider(config, providers, provider_id)
        except ValueError as exc:
            raise SfxError(BAD_REQUEST, str(exc)) from exc
        if cached:
            _PROVIDERS[pid] = provider
        return pid, provider


def validate_duration(duration_ms: int | float) -> float:
    """校验并归一化时长。

    参数:
        duration_ms: 条目时长（毫秒）。

    返回:
        秒数（保留 3 位小数）。

    异常:
        SfxError(BAD_REQUEST): 低于 1 秒，附中文原因。
    """
    seconds = round(float(duration_ms or 0) / 1000.0, 3)
    if seconds < MIN_SECONDS:
        raise SfxError(
            BAD_REQUEST,
            f"时长 {seconds:g}s 低于模型下限 1 秒：0.5 秒会退化成噪声，请把条目标注时长改到 ≥1 秒",
        )
    return seconds


def generate_sfx_item(
    store,
    config,
    item: dict,
    prompt_en: str | None = None,
    provider=None,
    provider_id: str | None = None,
    name: str | None = None,
) -> dict:
    """按条目生成一条音效，并把结果写回 `item["asset"]`。

    参数:
        store: ProjectStore。
        config: AppConfig。
        item: 音效条目（含 prompt/prompt_en/duration_ms/id）。
        prompt_en: 页面手填的英文提示词，优先于条目已存的 prompt_en。
        provider: 已建好的 provider；None 时按配置新建（用完不关闭，交给调用方）。
        provider_id: provider 标识（用于回报）。
        name: 文件名标签；缺省取提示词前几个词。

    返回:
        {"status", "asset_id", "duration_ms", "sample_rate", "channels", "engine",
         "prompt", "prompt_source", "warning", "latency_ms", "load_s", "peak_mib"}。

    异常:
        SfxError: 校验失败或 provider 不可用；ffmpeg 失败抛 RuntimeError。
    """
    seconds = validate_duration(item.get("duration_ms"))
    warning = (
        f"{seconds:g}s 属于极短时长，冲击类可能有削波，建议 ≥{CLIP_WARN_SECONDS:g}s"
        if seconds < CLIP_WARN_SECONDS
        else None
    )
    asset = item.setdefault("asset", {})
    engine = sfx_engine(config)

    if engine == "offline":
        out_path = store.root / f"assets/sfx/{item['id']}.wav"
        result = render_sfx(item, out_path, binary=config.get("ffmpeg.binary", "ffmpeg"), sample_rate=int(config.get("output.sample_rate", 48000)))
        asset.update(
            {
                "status": "ready",
                "asset_id": _rel(store, out_path),
                "actual_duration_ms": result["duration_ms"],
                "adapter": "offline-sfx",
                "cache_key": result["sha256"],
                "error": None,
            }
        )
        return {
            "status": "ready",
            "asset_id": asset["asset_id"],
            "duration_ms": result["duration_ms"],
            "sample_rate": result["sample_rate"],
            "channels": result["channels"],
            "engine": "offline",
            "prompt": item.get("prompt") or "",
            "rewrite_mode": None,
            "note": None,
            "warning": warning,
            "latency_ms": None,
            "load_s": None,
            "peak_mib": None,
        }

    pid = provider_id
    if provider is None:
        pid, provider = get_provider(config, pid)
    pid = pid or provider.spec.provider_id
    health = provider.health()
    if not health.ok:
        raise SfxError(health.code or BAD_REQUEST, health.detail or "音效 provider 不可用")

    raw_prompt = (prompt_en or "").strip() or (item.get("prompt_en") or "").strip()
    if not raw_prompt:
        raise SfxError(
            BAD_REQUEST,
            "条目缺少英文提示词（prompt_en）：请在媒体页手动填写英文，或重新生成标注描述",
        )
    # 运行时只补时长后缀，不再做中→英翻译（英文在标注阶段与中文同源落盘）
    final_prompt = finalize_prompt(raw_prompt, seconds)
    prompt_source = "manual" if (prompt_en or "").strip() else "stored"

    out_dir = store.root / SFX_REL_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    defaults = provider.spec.defaults or {}
    stem = build_stem(
        final_prompt,
        seconds,
        name=name,
        extra=f"{defaults.get('steps')}|{defaults.get('cfg')}",
    )
    out_path = out_dir / f"{stem}.wav"
    request = SfxRequest(
        prompt=final_prompt,
        output_path=str(out_path),
        duration_s=seconds,
        steps=defaults.get("steps"),
        cfg=defaults.get("cfg"),
    )
    t0 = time.time()
    result = provider.generate(request)
    latency_ms = int((time.time() - t0) * 1000)
    meta = {
        **result.to_dict(),
        "provider_id": pid,
        "raw_prompt": raw_prompt,
        "prompt": final_prompt,
        "prompt_source": prompt_source,
        "item_id": item.get("id"),
        "source": "media-page",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    out_path.with_suffix(".json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    asset.update(
        {
            "status": "ready",
            "asset_id": _rel(store, out_path),
            "actual_duration_ms": round(result.duration_s * 1000),
            "adapter": "stable-audio-3-small-sfx",
            "cache_key": stem,
            "error": None,
        }
    )
    return {
        "status": "ready",
        "asset_id": asset["asset_id"],
        "duration_ms": asset["actual_duration_ms"],
        "sample_rate": result.sample_rate,
        "channels": result.channels,
        "engine": "model",
        "prompt": final_prompt,
        "prompt_source": prompt_source,
        "warning": warning,
        "latency_ms": latency_ms,
        "load_s": result.load_s,
        "peak_mib": result.peak_mib,
    }


def _rel(store, path: Path) -> str:
    """把绝对路径转成工程内相对路径（正斜杠）。"""
    try:
        return str(Path(path).resolve().relative_to(store.root.resolve())).replace("\\", "/")
    except ValueError:
        return str(path)
