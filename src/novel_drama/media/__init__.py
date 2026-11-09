"""媒体合成层：时间轴构建、拼接、混音、音频规整、播放清单导出、ffmpeg 封装。"""

from .bgm import add_cue, import_asset, load_cues, save_cues
from .concat import concat_voice, plan_concat
from .export import build_manifest, build_srt, write_export
from .mixer import plan_mix, render_mix
from .mux import mux, plan_mux
from .normalize import measure_all, measure_loudness, measure_peak, normalize_audio
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
    "render_mix",
    "save_cues",
    "summarize_timeline",
    "write_export",
]
