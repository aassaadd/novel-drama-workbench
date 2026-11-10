"""写作编排包：大纲、章节正文、导演标注建议、设定与人物生成。"""

from .service import (
    apply_outline,
    bind_outline_characters,
    build_writing_adapter,
    check_golden_three,
    generate_chapter,
    generate_characters,
    generate_outline,
    generate_settings,
    polish_field,
    suggest_script,
)

__all__ = [
    "apply_outline",
    "bind_outline_characters",
    "build_writing_adapter",
    "check_golden_three",
    "generate_chapter",
    "generate_characters",
    "generate_outline",
    "generate_settings",
    "polish_field",
    "suggest_script",
]
