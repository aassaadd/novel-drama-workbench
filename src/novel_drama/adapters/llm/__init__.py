"""写作（LLM）适配器：契约、omlx 真实实现、离线实现与不可用兜底。"""

from __future__ import annotations

from .contracts import (
    Message,
    TextGenChunk,
    TextGenRequest,
    TextGenResult,
    Usage,
    WritingAdapter,
)
from .offline import OfflineWritingAdapter
from .omlx import OmlxWritingAdapter
from .unavailable import UnavailableWritingAdapter

__all__ = [
    "Message",
    "OfflineWritingAdapter",
    "OmlxWritingAdapter",
    "TextGenChunk",
    "TextGenRequest",
    "TextGenResult",
    "UnavailableWritingAdapter",
    "Usage",
    "WritingAdapter",
]
