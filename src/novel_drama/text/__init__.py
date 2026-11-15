"""文本处理：规则优先的分章/分场/台词切分，以及长上下文装配。"""

from .context import ContextAssembler
from .splitter import split_lines, split_scenes

__all__ = ["ContextAssembler", "split_lines", "split_scenes"]
