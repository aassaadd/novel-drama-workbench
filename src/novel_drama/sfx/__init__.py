"""音效（SFX）provider 层：文本描述进、wav 出。

与 `tts/` 同构：provider 只负责「把请求翻译成自家调用」，模型常驻在独立
Python 环境里，由常驻 worker 进程持有；上层（CLI / 编排）不感知模型名。
"""

from .base import BaseSfxProvider, SfxProvider
from .types import SfxError, SfxHealth, SfxRequest, SfxResult, SfxSpec

__all__ = [
    "BaseSfxProvider",
    "SfxProvider",
    "SfxError",
    "SfxHealth",
    "SfxRequest",
    "SfxResult",
    "SfxSpec",
]
