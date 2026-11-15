"""组合根：集中导入适配器实现，供接口层装配使用。

接口层（`interface/`）只依赖 application 与 domain；需要仓储、模型调用、媒体处理等
适配器时，统一从这里取，避免路由/命令模块直接 import `adapters.*`（分层守卫会报违规）。
真实实例仍由适配器自己构造，这里只做「从哪拿」的收口，不缓存、不改行为。
"""

from __future__ import annotations

from novel_drama.adapters import build_adapters, observability
from novel_drama.adapters.comfyui import LocalComfyGateway
from novel_drama.adapters.comfyui import media as comfy_media
from novel_drama.adapters.comfyui import models as comfy_models
from novel_drama.adapters.comfyui.media import load_manifest, templates_dir
from novel_drama.adapters.comfyui.workflow import (
    ComfyWorkflowError,
    convert_ui_workflow,
)
from novel_drama.adapters.errors import AdapterError, ErrorCode
from novel_drama.adapters.llm import OmlxWritingAdapter
from novel_drama.adapters.llm.factory import build_omlx_writing_adapter
from novel_drama.adapters.media import (
    JianyingExportError,
    add_cue,
    build_schedule,
    build_timeline,
    concat_voice,
    export_bundle,
    export_jianying,
    format_schedule_tsv,
    import_asset,
    mux,
    normalize_audio,
    plan_concat,
    plan_mix,
    plan_mux,
    render_character_sheet,
    render_mix,
    run_media,
    save_cues,
    summarize_timeline,
    sync_cues_from_plans,
    write_export,
)
from novel_drama.adapters.media.art import render_world_cover
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
from novel_drama.adapters.media.runner import plan_media
from novel_drama.adapters.media.sfx import generate_sfx_item, get_provider, sfx_engine
from novel_drama.adapters.media.styles import load_styles
from novel_drama.adapters.observability import last_llm_call, set_llm_abort_reason
from novel_drama.adapters.persistence import (
    GroupsStore,
    ImportsStore,
    ProjectStore,
    QueueStore,
    SchemaValidationError,
    SettingsStore,
    WorldStore,
    check_cast,
    find_variant,
    find_voice,
    list_workspaces,
    profile_source_hash,
    unresolved_speaker_lines,
    validate,
    variant_ref,
    voice_binding_summary,
)
from novel_drama.adapters.persistence.groups_store import (
    apply_group,
    build_diff,
    covering_groups,
    detect,
)
from novel_drama.adapters.persistence.imports_store import (
    compare_texts,
    digest,
    preview,
    split_paragraphs,
)
from novel_drama.adapters.persistence.project_store import WORKSPACE_INDEX
from novel_drama.adapters.persistence.voice_cast import (
    chapter_override_rows,
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
    "JianyingExportError",
    "LONG_BED_SECONDS",
    "MODE_BED",
    "MODE_LABELS",
    "MODE_NARRATIVE",
    "WORKSPACE_INDEX",
    "AdapterError",
    "ComfyWorkflowError",
    "ErrorCode",
    "GroupsStore",
    "ImportsStore",
    "LocalComfyGateway",
    "OmlxWritingAdapter",
    "build_omlx_writing_adapter",
    "ProjectStore",
    "QueueStore",
    "SchemaValidationError",
    "SettingsStore",
    "WorldStore",
    "add_cue",
    "apply_group",
    "binary_available",
    "build_adapters",
    "build_diff",
    "build_schedule",
    "build_timeline",
    "chapter_override_rows",
    "check_cast",
    "clear_chapter_overrides",
    "collect_references",
    "comfy_media",
    "comfy_models",
    "compare_texts",
    "concat_voice",
    "convert_ui_workflow",
    "covering_groups",
    "denoise_wav",
    "detect",
    "digest",
    "export_bundle",
    "export_jianying",
    "ffmpeg_run",
    "find_variant",
    "find_voice",
    "format_schedule_tsv",
    "generate_sfx_item",
    "get_provider",
    "import_asset",
    "last_llm_call",
    "list_workspaces",
    "load_cues",
    "load_generation_records",
    "load_manifest",
    "load_styles",
    "mux",
    "narrator_plan",
    "normalize_audio",
    "normalize_cast",
    "observability",
    "pick_variant",
    "plan_concat",
    "plan_media",
    "plan_mix",
    "plan_mux",
    "preview",
    "probe_duration_ms",
    "profile_source_hash",
    "render_bgm",
    "render_character_sheet",
    "render_mix",
    "render_world_cover",
    "resolve_for_character",
    "resolve_mode",
    "run",
    "run_media",
    "save_cues",
    "set_chapter_override",
    "set_llm_abort_reason",
    "sfx_engine",
    "split_paragraphs",
    "summarize_timeline",
    "sync_cues_from_plans",
    "templates_dir",
    "unresolved_speaker_lines",
    "validate",
    "validate_cast_config",
    "variant_ref",
    "voice_binding_summary",
    "write_export",
]
