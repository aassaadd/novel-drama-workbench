"""音效契约：音效生成 / 素材检索的请求与结果、`SfxAdapter` 协议。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ..errors import HealthStatus


@dataclass
class SfxRequest:
    """音效生成请求。"""

    prompt: str
    kind: str  # ambience|foley|impact|transition|music_sting
    duration_ms: int
    loop: bool = False
    seed: int | None = None
    output_path: str | None = None
    metadata: dict = field(default_factory=dict)


@dataclass
class SfxResult:
    """音效产物。"""

    audio_path: str
    duration_ms: int
    sample_rate: int
    channels: int
    provider: str = ""
    cache_key: str | None = None
    raw: dict | None = None


@dataclass
class SfxCandidate:
    """素材库检索候选。"""

    path: str
    name: str
    duration_ms: int
    cache_key: str
    score: float = 0.0
    extra: dict = field(default_factory=dict)


class SfxAdapter(Protocol):
    """音效适配接口：生成式与素材库检索统一为「描述进、音频出」。"""

    def generate(self, req: SfxRequest) -> SfxResult:
        """生成音效。"""
        ...

    def search(self, query: str, kind: str, limit: int = 10) -> list[SfxCandidate]:
        """检索素材库；不支持时抛 NOT_IMPLEMENTED。"""
        ...

    def health(self) -> HealthStatus:
        """检查音效服务是否可用。"""
        ...
