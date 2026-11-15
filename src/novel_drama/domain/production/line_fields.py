"""台词行与场景的字段分层清单（阶段 6-3 步骤 2 的唯一口径）。

背景：台本 `scripts/<chapter>.json` 与章节场景 `chapters/<chapter>.json` 曾各存一份完整台词行，
其中 `tts` 生产态双份保存，导致 stale 判定与告警定位混乱。这里把字段按「归属」列清楚：

- 内容层：唯一可写源是台本；台本行不再保存 `tts`；
- 生产态：留在场景投影，派生时按行 ID 原样保留；
- 场景身份/描述/媒体：scenes 自有，台本只覆盖描述字段。

本模块只放常量，不含逻辑；schema 与换算函数都以这里为准。
"""

from __future__ import annotations

#: 内容层字段：台本镜头行承接的内容，也是派生场景行时唯一由台本覆盖的部分
CONTENT_LINE_FIELDS = (
    "index",
    "kind",
    "speaker_id",
    "text",
    "emotion",
    "emotion_intensity",
    "speed",
    "pause_before_ms",
    "pause_after_ms",
    "delivery_note",
    "paralinguistic",
    "target_duration_ms",
    "voice",
    "inferred",
)

#: 生产态字段：只归场景投影，台本不再保存；派生时按行 ID 从旧场景保留
PRODUCTION_LINE_FIELDS = (
    "tts",
    "confidence",
)

#: 场景身份字段：派生时按 index 对齐旧场景沿用，不随台本变化
SCENE_IDENTITY_FIELDS = (
    "id",
    "chapter_id",
    "summary",
    "source_text",
    "status",
    "character_ids",
)

#: 场景描述字段：由镜头（台本）覆盖
SCENE_DESCRIPTIVE_FIELDS = (
    "location",
    "time_of_day",
    "mood",
    "goal",
)

#: 场景媒体字段：归 scenes，按条目 ID 保留，不迁进台本
SCENE_MEDIA_FIELDS = (
    "sfx_events",
    "key_images",
    "video_segments",
)
