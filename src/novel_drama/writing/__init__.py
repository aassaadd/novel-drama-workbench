"""写作编排包：大纲、章节正文、导演标注建议、设定与人物生成。"""

from .continuation import continue_chapter, continue_outline
from .editing import edit_chapter, save_chapter_content
from .service import (
    apply_outline,
    bind_outline_characters,
    bind_outline_context,
    build_art_prompt,
    build_writing_adapter,
    check_golden_three,
    generate_chapter,
    generate_characters,
    generate_outline,
    generate_section,
    generate_settings,
    generate_worlds,
    polish_field,
    suggest_script,
    validate_chapter_binding,
)
from .summarize import summarize_chapter

__all__ = [
    "apply_outline",
    "bind_outline_characters",
    "bind_outline_context",
    "build_art_prompt",
    "build_writing_adapter",
    "check_golden_three",
    "continue_chapter",
    "continue_outline",
    "edit_chapter",
    "generate_chapter",
    "generate_characters",
    "generate_outline",
    "generate_section",
    "generate_settings",
    "generate_worlds",
    "polish_field",
    "save_chapter_content",
    "suggest_script",
    "summarize_chapter",
    "validate_chapter_binding",
]
