"""TTS 契约：音色描述、单行配音请求 / 结果与 `TtsAdapter` 协议。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ..errors import HealthStatus


@dataclass
class Voice:
    """音色描述，供角色音色库选择。"""

    voice_id: str
    name: str
    language: str = "zh-CN"
    gender: str = "unknown"
    adapter: str = ""
    extra: dict = field(default_factory=dict)


@dataclass
class TtsRequest:
    """单行配音请求（以台词行为单位，不做整章拼接）。"""

    text: str
    voice_id: str
    emotion: str = "neutral"
    emotion_intensity: float = 0.5
    speed: float = 1.0
    pause_before_ms: int = 0
    pause_after_ms: int = 0
    sample_rate: int = 48000
    format: str = "wav"
    reference_audio: str | None = None
    seed: int | None = None
    delivery_note: str | None = None
    output_path: str | None = None
    metadata: dict = field(default_factory=dict)


@dataclass
class TtsResult:
    """配音产物；`duration_ms` 必须为实测值。"""

    audio_path: str
    duration_ms: int
    sample_rate: int
    channels: int
    voice_id: str
    model: str = ""
    raw: dict | None = None


class TtsAdapter(Protocol):
    """多角色 TTS 适配接口。"""

    def list_voices(self) -> list[Voice]:
        """列出可用音色。"""
        ...

    def synthesize(self, req: TtsRequest) -> TtsResult:
        """合成单行音频。"""
        ...

    def health(self) -> HealthStatus:
        """检查 TTS 服务是否可用。"""
        ...
