"""导演脚本：人工标注解析（规则，不用 LLM）。"""

from .markup import ASPECT_SIZES, parse_markup

__all__ = ["ASPECT_SIZES", "parse_markup"]
