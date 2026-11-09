"""适配器装配：按配置决定用哪个实现；未知名称直接报错，不做静默回退。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..config import REPO_ROOT, AppConfig
from .base import AdapterError, ComfyGateway, ErrorCode, SfxAdapter, TtsAdapter, WritingAdapter
from .comfyui import LocalComfyGateway
from .sfx import PlaceholderSfxAdapter
from .tts import PlaceholderTtsAdapter, ProviderTtsAdapter
from .writing import OmlxWritingAdapter

__all__ = [
    "AdapterError",
    "Adapters",
    "ComfyGateway",
    "ErrorCode",
    "SfxAdapter",
    "TtsAdapter",
    "WritingAdapter",
    "build_adapters",
]


@dataclass
class Adapters:
    """四类外部能力的装配结果。"""

    writing: WritingAdapter
    tts: TtsAdapter
    sfx: SfxAdapter
    comfy: ComfyGateway


def build_adapters(config: AppConfig, project_settings: dict | None = None) -> Adapters:
    """按配置装配适配器。

    参数:
        config: 本机配置。
        project_settings: 工程级设置（可选，覆盖本机 omlx 参数）。

    返回:
        Adapters 实例。

    异常:
        AdapterError(INVALID_INPUT): 出现未知适配器名称。
    """
    names = config.adapters
    project_settings = project_settings or {}

    writing_name = names.get("writing", "omlx")
    if writing_name != "omlx":
        raise AdapterError(ErrorCode.INVALID_INPUT, f"未知写作适配器: {writing_name}（当前仅支持 omlx）")
    omlx_settings = dict(config.get("omlx", {}) or {})
    omlx_settings.update(project_settings.get("writing") or {})
    writing = OmlxWritingAdapter(omlx_settings)

    # 默认走真实 provider：只有显式写 placeholder 才会落到占位实现，避免静默降级
    tts_name = names.get("tts", "auto")
    if tts_name == "placeholder":
        tts = PlaceholderTtsAdapter(project_settings.get("tts") or config.get("tts", {}))
    else:
        configured = {str(raw.get("provider_id")) for raw in (config.get("tts.providers") or [])}
        if tts_name != "auto" and tts_name not in configured:
            raise AdapterError(
                ErrorCode.INVALID_INPUT,
                f"未知 TTS 适配器: {tts_name}（可选 placeholder/auto 或 tts.providers 里的 provider_id）",
            )
        tts = ProviderTtsAdapter(config, tts_name)

    sfx_name = names.get("sfx", "placeholder")
    if sfx_name != "placeholder":
        raise AdapterError(ErrorCode.INVALID_INPUT, f"未知音效适配器: {sfx_name}（本轮仅提供 placeholder）")
    sfx = PlaceholderSfxAdapter(project_settings.get("sfx") or config.get("sfx", {}))

    comfy_name = names.get("comfyui", "local")
    if comfy_name != "local":
        raise AdapterError(ErrorCode.INVALID_INPUT, f"未知 ComfyUI 适配器: {comfy_name}（当前仅支持 local）")
    templates_dir = Path(config.get("comfyui.templates_dir", "config/comfy")).expanduser()
    if not templates_dir.is_absolute():
        templates_dir = REPO_ROOT / templates_dir
    comfy = LocalComfyGateway(config.get("comfyui", {}) or {}, templates_dir)

    return Adapters(writing=writing, tts=tts, sfx=sfx, comfy=comfy)
