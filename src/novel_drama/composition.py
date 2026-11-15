"""组合根：集中导入适配器实现，供接口层装配使用。

接口层（`interface/`）只依赖 application 与 domain；需要仓储、模型调用、媒体处理等
适配器时，统一从这里取，避免路由模块直接 import `adapters.*`（分层守卫会报违规）。
真实实例仍由适配器自己构造，这里只做「从哪拿」的收口，不缓存、不改行为。
"""

from __future__ import annotations

from novel_drama.adapters import build_adapters, observability
from novel_drama.adapters.comfyui import LocalComfyGateway
from novel_drama.adapters.comfyui import media as comfy_media
from novel_drama.adapters.comfyui import models as comfy_models
from novel_drama.adapters.errors import AdapterError, ErrorCode
from novel_drama.adapters.llm import OmlxWritingAdapter
from novel_drama.adapters.media.bgm import load_cues
from novel_drama.adapters.media.denoise import denoise_wav
from novel_drama.adapters.media.ffmpeg import binary_available, probe_duration_ms, run
from novel_drama.adapters.media.ffmpeg import run as ffmpeg_run
from novel_drama.adapters.media.music import (
    LONG_BED_SECONDS,
    MODE_BED,
    MODE_LABELS,
    MODE_NARRATIVE,
    load_generation_records,
    render_bgm,
    resolve_mode,
)
from novel_drama.adapters.media.references import collect_references
from novel_drama.adapters.media.sfx import generate_sfx_item, get_provider, sfx_engine
from novel_drama.adapters.media.styles import load_styles
from novel_drama.adapters.observability import last_llm_call, set_llm_abort_reason
from novel_drama.adapters.persistence import (
    ProjectStore,
    find_variant,
    find_voice,
    list_workspaces,
    variant_ref,
)
from novel_drama.adapters.persistence.project_store import WORKSPACE_INDEX
from novel_drama.adapters.persistence.queue_store import QueueStore
from novel_drama.adapters.persistence.schema_validator import validate
from novel_drama.adapters.persistence.settings_store import SettingsStore
from novel_drama.adapters.persistence.voice_cast import (
    chapter_override_rows,
    check_cast,
    clear_chapter_overrides,
    narrator_plan,
    normalize_cast,
    set_chapter_override,
    validate_cast_config,
)
from novel_drama.adapters.persistence.voice_store import (
    pick_variant,
    resolve_for_character,
)

__all__ = [
    "LONG_BED_SECONDS",
    "MODE_BED",
    "MODE_LABELS",
    "MODE_NARRATIVE",
    "WORKSPACE_INDEX",
    "AdapterError",
    "ErrorCode",
    "LocalComfyGateway",
    "OmlxWritingAdapter",
    "ProjectStore",
    "QueueStore",
    "SettingsStore",
    "binary_available",
    "build_adapters",
    "chapter_override_rows",
    "check_cast",
    "clear_chapter_overrides",
    "collect_references",
    "comfy_media",
    "comfy_models",
    "denoise_wav",
    "ffmpeg_run",
    "find_variant",
    "find_voice",
    "generate_sfx_item",
    "get_provider",
    "last_llm_call",
    "list_workspaces",
    "load_cues",
    "load_generation_records",
    "load_styles",
    "narrator_plan",
    "normalize_cast",
    "observability",
    "pick_variant",
    "probe_duration_ms",
    "render_bgm",
    "resolve_for_character",
    "resolve_mode",
    "run",
    "set_chapter_override",
    "set_llm_abort_reason",
    "sfx_engine",
    "validate",
    "validate_cast_config",
    "variant_ref",
]
