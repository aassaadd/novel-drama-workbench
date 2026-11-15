"""设定库的人工编辑用例：标量字段写入、分区合并、字段润色回写。

CLI（`setting set/apply/polish`）与 Web 共用本模块，写入口径只有一份。
"""

from __future__ import annotations

import re

from ...domain.shared.errors import PreconditionError
from ...store import ProjectStore
from ...store.settings_store import SettingsStore

#: `setting set` 支持标量字段：解析参数名 → (设定库路径, 字段名)
SCALAR_FIELDS = {
    "logline": ("premise", "logline"),
    "theme": ("premise", "theme"),
    "core_conflict": ("premise", "core_conflict"),
    "ending_intent": ("premise", "ending_intent"),
    "target_audience": ("premise", "target_audience"),
    "target_length": ("premise", "target_length"),
    "tone": ("style", "tone"),
    "pov": ("style", "pov"),
    "tense": ("style", "tense"),
    "prose_style": ("style", "prose_style"),
    "visual_style": ("style", "visual_style"),
}

#: `setting apply` / `setting generate` 各分区允许写入的字段
SECTION_FIELDS = {
    "premise": ("logline", "theme", "core_conflict", "ending_intent", "target_audience", "target_length", "target_words"),
    "style": ("tone", "pov", "tense", "prose_style", "forbidden", "visual_style"),
}
#: 整数型分区字段：写入前转 int，避免存成字符串
INT_FIELDS = {"target_words"}


def as_str_list(rows) -> list[str]:
    """把字符串或数组统一成去空字符串列表。

    参数:
        rows: 字符串、数组或 None。

    返回:
        去空白后的字符串列表。

    异常:
        ValueError: 既不是字符串也不是数组。
    """
    if rows is None:
        return []
    if isinstance(rows, str):
        rows = [rows]
    if isinstance(rows, dict):
        # 模型有时把规则/势力返回成单个对象或「名称→说明」映射
        if any(key in rows for key in ("name", "title", "description", "desc", "summary")):
            rows = [rows]
        else:
            rows = [
                f"{key}：{value}" if not isinstance(value, (dict, list)) and str(value).strip() else str(key)
                for key, value in rows.items()
            ]
    if not isinstance(rows, list):
        raise ValueError(f"该字段必须是字符串数组（实际收到 {type(rows).__name__}）")
    result = []
    for item in rows:
        if isinstance(item, dict):
            name = str(item.get("name") or item.get("title") or "").strip()
            note = str(item.get("description") or item.get("desc") or item.get("summary") or "").strip()
            item = f"{name}：{note}" if name and note else (name or note or "")
        text = str(item).strip()
        if text:
            result.append(text)
    return result


def split_items(text: str) -> list[str]:
    """按常见分隔符把一段文本拆成列表。

    参数:
        text: 如「冷静、克制，敏锐」的文本。

    返回:
        去空后的条目列表。
    """
    return [part.strip() for part in re.split(r"[、,，;；\n]+", text or "") if part.strip()]


def merge_section(bible: dict, section: str, fields: dict) -> list[str]:
    """把字段合并进设定库对应分区（词条自动补 aliases）。

    参数:
        bible: 设定库 dict（就地修改）。
        section: premise / style。
        fields: 待写入字段。

    返回:
        实际写入的字段名列表。

    异常:
        ValueError: 分区非法、fields 不是对象或没有可写入字段。
    """
    if section not in SECTION_FIELDS:
        raise ValueError(f"未知设定分区: {section}（可选 premise / style）")
    if not isinstance(fields, dict):
        raise ValueError("fields 必须是 JSON 对象")
    target = bible.setdefault(section, {})
    changed: list[str] = []

    for key in SECTION_FIELDS[section]:
        if key not in fields:
            continue
        value = fields.get(key)
        if key in INT_FIELDS:
            # 目标总字数：空值或 0 都表示不约束，统一存 int
            try:
                number = int(value or 0)
            except (TypeError, ValueError) as error:
                raise ValueError(f"{key} 必须是整数") from error
            target[key] = max(0, number)
        else:
            target[key] = as_str_list(value) if key == "forbidden" else str(value or "").strip()
        changed.append(key)

    if not changed:
        raise ValueError(f"fields 里没有可写入 {section} 分区的字段")
    return changed


def set_scalars(store: ProjectStore, values: dict) -> dict:
    """写入标量字段（空值跳过）。

    参数:
        store: 工程。
        values: 字段名 → 值（字段名同 `SCALAR_FIELDS` 的键）。

    返回:
        `{"changed": {section.field: value}, "settings_revision": r}`。

    异常:
        ValueError: 没有任何可写入字段。
    """
    settings = SettingsStore(store.root)
    bible = settings.ensure()
    changed: dict[str, str] = {}
    for flag, (section, field) in SCALAR_FIELDS.items():
        value = values.get(flag)
        if value is None or value == "":
            continue
        bible.setdefault(section, {})[field] = value
        changed[f"{section}.{field}"] = value
    if not changed:
        raise ValueError("没有要修改的字段：至少给一个（如 --logline / --era / --tone / --visual-style）")
    bible = settings.save(bible)
    return {"changed": changed, "settings_revision": bible["settings_revision"]}


def apply_section(store: ProjectStore, section: str, fields: dict) -> dict:
    """合并一个分区的字段并保存。

    参数:
        store: 工程。
        section: premise / style。
        fields: 待写入字段。

    返回:
        `{"section", "changed", "settings_revision", "fields"}`。
    """
    settings = SettingsStore(store.root)
    bible = settings.ensure()
    changed = merge_section(bible, section, fields)
    bible = settings.save(bible)
    return {
        "section": section,
        "changed": changed,
        "settings_revision": bible["settings_revision"],
        "fields": bible.get(section) or {},
    }


def read_field_value(store: ProjectStore, bible: dict, field: str, character_id: str | None = None) -> str:
    """读取待润色字段的当前值。

    参数:
        store: 工程。
        bible: 设定库 dict。
        field: 润色字段名。
        character_id: character.* 字段所属角色 ID。

    返回:
        字段当前文本（数组用「、」连接）。

    异常:
        ValueError: 列表型字段未指定具体条目，或缺少 character_id。
        PreconditionError: 角色不存在。
    """
    if field in SCALAR_FIELDS:
        section, key = SCALAR_FIELDS[field]
        return str((bible.get(section) or {}).get(key) or "")
    if field == "work.genre":
        work = store.load_project().get("work") or {}
        return "、".join(work.get("genre") or [])
    if field.startswith("character."):
        if not character_id:
            raise ValueError("润色人物字段需要用 --character 指定角色 ID")
        table = store.load_characters()
        target = next((item for item in table["characters"] if item.get("id") == character_id), None)
        if target is None:
            raise PreconditionError(f"角色不存在: {character_id}")
        if field == "character.personality":
            return "、".join(target.get("personality") or [])
        return str(target.get("background") or "")
    raise ValueError(f"不支持的润色字段: {field}")


def apply_polished(
    store: ProjectStore,
    settings: SettingsStore,
    bible: dict,
    field: str,
    source: str,
    polished: str,
    character_id: str | None = None,
) -> dict:
    """把润色结果写回设定库 / 人物表 / 工程元信息。

    参数:
        store: 工程。
        settings: 设定库读写入口。
        bible: 设定库 dict（就地修改）。
        field: 润色字段名。
        source: 润色前的原始文本（用于定位列表条目）。
        polished: 润色后的文本。
        character_id: character.* 字段所属角色 ID。

    返回:
        `{"field", "applied": True, 附加的 settings_revision / character_id / genres}`。

    异常:
        ValueError: 列表字段找不到对应条目。
        PreconditionError: 角色不存在。
    """
    if field == "work.genre":
        project = store.load_project()
        project["work"]["genre"] = split_items(polished)
        store.save_project(project)
        return {"field": field, "applied": True, "genres": project["work"]["genre"]}

    if field.startswith("character."):
        if not character_id:
            raise ValueError("润色人物字段需要用 --character 指定角色 ID")
        table = store.load_characters()
        target = next((item for item in table["characters"] if item.get("id") == character_id), None)
        if target is None:
            raise PreconditionError(f"角色不存在: {character_id}")
        if field == "character.personality":
            target["personality"] = split_items(polished)
        else:
            target["background"] = polished
        store.save_characters(table)
        return {"field": field, "applied": True, "character_id": character_id}

    if field in SCALAR_FIELDS:
        section, key = SCALAR_FIELDS[field]
        bible.setdefault(section, {})[key] = polished
        saved = settings.save(bible)
        return {"field": field, "applied": True, "settings_revision": saved["settings_revision"]}

    raise ValueError(f"不支持的润色字段: {field}")
