"""实体工厂与工具（纯 dict 表示，配合 schema 校验）。"""

from .duration import estimate_duration_ms
from .entities import (
    EMOTIONS,
    NARRATOR_ID,
    UNKNOWN_SPEAKER_ID,
    new_chapter,
    new_character,
    new_job,
    new_key_image,
    new_line,
    new_narrator,
    new_scene,
    new_sfx_event,
    new_video_segment,
    new_work,
    provisional_line_duration_ms,
)
from .ids import new_id, now_iso

__all__ = [
    "EMOTIONS",
    "NARRATOR_ID",
    "UNKNOWN_SPEAKER_ID",
    "estimate_duration_ms",
    "new_chapter",
    "new_character",
    "new_id",
    "new_job",
    "new_key_image",
    "new_line",
    "new_narrator",
    "new_scene",
    "new_sfx_event",
    "new_video_segment",
    "new_work",
    "now_iso",
    "provisional_line_duration_ms",
]
