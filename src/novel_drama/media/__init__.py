"""媒体合成层：时间轴构建、拼接、混音、音频规整、播放清单导出、ffmpeg 封装。"""

from .art import render_character_sheet, render_world_cover
from .bgm import add_cue, import_asset, load_cues, save_cues
from .bundle import export_bundle
from .concat import concat_voice, plan_concat
from .export import build_manifest, build_srt, write_export
from .generate import probe_duration_ms, render_image, render_sfx, render_video
from .mixer import plan_mix, render_mix
from .mux import mux, plan_mux
from .normalize import measure_all, measure_loudness, measure_peak, normalize_audio
from .runner import run_media
from .timeline_builder import (
    apply_silences,
    build_schedule,
    build_timeline,
    compute_source_hash,
    format_schedule_tsv,
    summarize_timeline,
)

__all__ = [
    "add_cue",
    "apply_silences",
    "build_manifest",
    "build_schedule",
    "build_srt",
    "build_timeline",
    "compute_source_hash",
    "concat_voice",
    "export_bundle",
    "format_schedule_tsv",
    "import_asset",
    "load_cues",
    "measure_all",
    "measure_loudness",
    "measure_peak",
    "mux",
    "normalize_audio",
    "plan_concat",
    "plan_mix",
    "plan_mux",
    "probe_duration_ms",
    "render_character_sheet",
    "render_image",
    "render_mix",
    "render_sfx",
    "render_video",
    "run_media",
    "save_cues",
    "summarize_timeline",
    "write_export",
]
