"""持久化适配器：工程文件读写、schema 校验与实体映射。

本层是落盘契约的唯一入口：
- `ProjectStore` 负责工程目录与各类 JSON 的原子读写（原 `store/project_store.py`）；
- 其余 `*_store` 模块按聚合拆分读写职责；
- `chapter_store` / `cast_store` 只做「实体 ⇄ 字典」转换，IO 仍复用 `ProjectStore`。

包级导出保持与拆分前一致，调用方按需从具体模块导入，避免多一跳。
"""

from .groups_store import GroupsStore
from .imports_store import ImportsStore
from .project_store import ProjectStore, list_workspaces
from .queue_store import QueueStore
from .schema_validator import SchemaValidationError, validate
from .settings_store import SettingsStore
from .voice_cast import (
    cast_config,
    chapter_override_rows,
    check_cast,
    clear_chapter_overrides,
    narrator_persona,
    narrator_plan,
    normalize_cast,
    profile_source_hash,
    resolve_cast_voice,
    set_chapter_override,
    unresolved_speaker_lines,
    validate_cast_config,
)
from .voice_store import (
    find_variant,
    find_voice,
    pick_variant,
    resolve_for_character,
    variant_ref,
    voice_binding_summary,
)
from .world_store import WorldStore

__all__ = [
    "GroupsStore",
    "ImportsStore",
    "ProjectStore",
    "QueueStore",
    "SchemaValidationError",
    "SettingsStore",
    "WorldStore",
    "cast_config",
    "chapter_override_rows",
    "check_cast",
    "clear_chapter_overrides",
    "find_variant",
    "find_voice",
    "list_workspaces",
    "narrator_persona",
    "narrator_plan",
    "normalize_cast",
    "pick_variant",
    "profile_source_hash",
    "resolve_cast_voice",
    "resolve_for_character",
    "set_chapter_override",
    "unresolved_speaker_lines",
    "validate",
    "validate_cast_config",
    "variant_ref",
    "voice_binding_summary",
]
