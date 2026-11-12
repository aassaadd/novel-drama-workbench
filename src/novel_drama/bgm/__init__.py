"""配乐生成：项目内 provider（Stable Audio Open 1.0）与 ComfyUI（ACE-Step）两条路径。

上游 `media/music.py` 只关心「拿到一条原始 wav」，两档（叙事 / 铺底）的
响度归一、首尾裁剪、循环填充与记录格式都由上层统一处理，provider 不重复实现。

默认走本地 provider（`bgm.provider: local`），配置可切回 `comfyui`。
"""

from __future__ import annotations

from .base import BaseBgmProvider, BgmProvider
from .prompting import RewriteOutcome, rewrite_prompt
from .registry import build_providers, pick_provider
from .types import BgmError, BgmHealth, BgmRequest, BgmResult, BgmSpec

__all__ = [
    "BaseBgmProvider",
    "BgmProvider",
    "BgmError",
    "BgmHealth",
    "BgmRequest",
    "BgmResult",
    "BgmSpec",
    "RewriteOutcome",
    "build_providers",
    "pick_provider",
    "rewrite_prompt",
]
