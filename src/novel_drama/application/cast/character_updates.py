"""生成后的角色状态回流：提案、预览、确认应用。

原则：
- 正文生成完成后不让模型直接改角色卡，只产出「更新提案」落到 `.ndw/character_updates/`；
- 作者在界面上逐条确认后，才写回 `characters.json`；
- 应用只改提案里出现的字段（处境/已知信息/关系），其它字段与其它角色一律不动。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ...adapters.llm.contracts import Message, TextGenRequest
from ...adapters.persistence import ProjectStore
from ...adapters.persistence.settings_store import SettingsStore
from ...infrastructure.config import AppConfig, resolve_temperature
from ...models.ids import now_iso
from ..story.outline_bind import chapter_binding
from ..support.llm_calls import build_writing_adapter

#: 提案中允许写回的角色状态字段
UPDATE_FIELDS = ("situation", "knowledge_add", "relations")

#: 关系倾向枚举（与 character.schema.json 对齐）
SENTIMENTS = ("ally", "enemy", "family", "romance", "neutral")


def updates_dir(store: ProjectStore) -> Path:
    """返回角色更新提案目录（不存在时创建）。

    参数:
        store: 工程。

    返回:
        `.ndw/character_updates` 目录。
    """
    path = store.root / ".ndw" / "character_updates"
    path.mkdir(parents=True, exist_ok=True)
    return path


def updates_file(store: ProjectStore, chapter_id: str) -> Path:
    """返回某章的角色更新提案文件。

    参数:
        store: 工程。
        chapter_id: 章节 ID。

    返回:
        提案 JSON 路径。
    """
    return updates_dir(store) / f"{chapter_id}.json"


def _parse_json(text: str) -> dict:
    """从模型输出里解析 JSON（容忍 ```json 代码块与前后说明）。

    参数:
        text: 模型原始输出。

    返回:
        解析后的 dict；失败时返回空 dict。
    """
    raw = str(text or "").strip()
    match = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw)
    if match:
        raw = match.group(1).strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        value = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _clean_text(value, limit: int) -> str:
    """把任意值转为去空白字符串并截断。

    参数:
        value: 原始值。
        limit: 最大长度。

    返回:
        清理后的字符串。
    """
    return str(value or "").strip()[:limit]


def _clean_update(raw: dict, cast_ids: set[str]) -> dict | None:
    """清洗单条更新提案；非法角色/关系目标直接丢弃。

    参数:
        raw: 模型返回的单条提案。
        cast_ids: 当前角色 ID 集合。

    返回:
        清洗后的提案；无效时返回 None。
    """
    character_id = _clean_text(raw.get("character_id"), 40)
    if not character_id or character_id not in cast_ids:
        return None
    relations: list[dict] = []
    for relation in raw.get("relations") or []:
        if not isinstance(relation, dict):
            continue
        target = _clean_text(relation.get("target_id"), 40)
        if not target or target not in cast_ids or target == character_id:
            continue
        sentiment = relation.get("sentiment")
        relations.append(
            {
                "target_id": target,
                "label": _clean_text(relation.get("label"), 40),
                "sentiment": sentiment if sentiment in SENTIMENTS else None,
                "note": _clean_text(relation.get("note"), 500),
            }
        )
    knowledge = [
        _clean_text(item, 200) for item in (raw.get("knowledge_add") or []) if _clean_text(item, 200)
    ]
    update = {
        "character_id": character_id,
        "situation": _clean_text(raw.get("situation"), 500),
        "knowledge_add": knowledge,
        "relations": relations,
        "arc_note": _clean_text(raw.get("arc_note"), 300),
    }
    if not any([update["situation"], update["knowledge_add"], update["relations"]]):
        return None
    return update


def propose_character_updates(
    store: ProjectStore,
    config: AppConfig,
    chapter_id: str,
    excerpt_chars: int = 3000,
) -> dict:
    """读取本章正文，生成角色状态更新提案（不写回角色卡）。

    参数:
        store: 工程。
        config: 本机配置（用于装配写作适配器）。
        chapter_id: 章节 ID。
        excerpt_chars: 正文最多读取的字符数。

    返回:
        `{chapter_id, created_at, updates, applied: false}`，并已写入提案文件。

    异常:
        ValueError: 本章正文为空（没有可回流的内容）。
    """
    chapter = store.load_chapter(chapter_id)
    content = str(chapter.get("content") or "").strip()
    if not content:
        raise ValueError("本章还没有正文：无法生成角色状态回流提案")
    characters = [
        item for item in store.load_characters()["characters"] if item.get("role") != "narrator"
    ]
    cast_ids = {str(item.get("id")) for item in characters if item.get("id")}
    binding = chapter_binding(store, chapter_id)
    focus_ids = {
        row["id"] for row in binding.get("characters") or [] if row.get("planned") or row.get("is_pov")
    }
    focus = [item for item in characters if str(item.get("id")) in focus_ids] or characters
    settings = SettingsStore(store.root)
    bible = settings.ensure()
    adapter = build_writing_adapter(config, bible, action="character.updates")
    state_brief = [
        {
            "character_id": item.get("id"),
            "name": item.get("name"),
            "role": item.get("role"),
            "situation": (item.get("state") or {}).get("situation"),
            "knowledge": (item.get("state") or {}).get("knowledge"),
            "relationships": (item.get("state") or {}).get("relationship_notes") or {},
            "speech_style": item.get("speech_style") or {},
        }
        for item in focus
    ]
    prompt = (
        "下面是本章正文与出场角色当前状态。请判断本章写完后，哪些角色的处境、已知信息、关系发生了变化。\n"
        "只输出 JSON，不要解释：\n"
        '{"updates":[{"character_id":"chr_xxx","situation":"本章结束时的处境（没变就留空）",'
        '"knowledge_add":["本章新知道的关键事实，最多3条"],'
        '"relations":[{"target_id":"chr_yyy","label":"关系名","sentiment":"ally|enemy|family|romance|neutral",'
        '"note":"变化说明"}],"arc_note":"本章的角色弧线变化"}]}\n'
        "约束：只使用下面给出的 character_id；没有变化的角色不要输出；不要新增角色；"
        "没有把握的内容不要编造。\n\n"
        f"角色当前状态：{json.dumps(state_brief, ensure_ascii=False)}\n\n"
        f"本章正文：\n{content[:excerpt_chars]}"
    )
    req = TextGenRequest(
        task="character_updates",
        system="你是小说编辑，只输出角色状态更新 JSON，不写正文、不解释。",
        messages=[Message("user", prompt)],
        stream=False,
        temperature=resolve_temperature(config, "writing"),
        metadata={
            "chapter_id": chapter_id,
            "characters": state_brief,
            "content": content[:excerpt_chars],
            "summary": chapter.get("synopsis"),
            "thinking_stage": "character_updates",
        },
    )
    result = adapter.generate(req)
    payload = _parse_json(result.text)
    updates = []
    for raw in payload.get("updates") or []:
        if isinstance(raw, dict):
            clean = _clean_update(raw, cast_ids)
            if clean:
                updates.append(clean)
    record = {
        "chapter_id": chapter_id,
        "created_at": now_iso(),
        "applied": False,
        "model": result.model,
        "updates": updates,
    }
    updates_file(store, chapter_id).write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return record


def load_character_updates(store: ProjectStore, chapter_id: str) -> dict:
    """读取某章的角色更新提案。

    参数:
        store: 工程。
        chapter_id: 章节 ID。

    返回:
        提案记录；没有提案时返回 `{chapter_id, updates: [], applied: false, exists: false}`。
    """
    path = updates_file(store, chapter_id)
    if not path.exists():
        return {"chapter_id": chapter_id, "updates": [], "applied": False, "exists": False}
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"chapter_id": chapter_id, "updates": [], "applied": False, "exists": False}
    record["exists"] = True
    return record


def apply_character_updates(
    store: ProjectStore,
    chapter_id: str,
    selections: list[dict] | None = None,
) -> dict:
    """按作者确认的选择写回角色状态（只改被确认的字段）。

    参数:
        store: 工程。
        chapter_id: 章节 ID。
        selections: 确认项；None 表示应用全部。每项可含
            `{character_id, situation?: bool, knowledge_add?: bool, relations?: bool}`，
            缺省字段视为确认。

    返回:
        `{chapter_id, applied: [{character_id, name, fields}], skipped: [...]}`。

    异常:
        ValueError: 没有可应用的提案。
    """
    record = load_character_updates(store, chapter_id)
    if not record.get("updates"):
        raise ValueError("本章没有可应用的角色状态提案：请先生成正文并检测回流")
    table = store.load_characters()
    by_id = {str(item.get("id")): item for item in table["characters"] if item.get("id")}
    cast_ids = {cid for cid, item in by_id.items() if item.get("role") != "narrator"}
    choice_by_id: dict[str, dict] = {}
    for item in selections or []:
        if isinstance(item, dict) and item.get("character_id"):
            choice_by_id[str(item["character_id"])] = item
    applied: list[dict] = []
    skipped: list[str] = []
    for update in record["updates"]:
        character_id = str(update.get("character_id") or "")
        character = by_id.get(character_id)
        if character is None or character_id not in cast_ids:
            skipped.append(character_id or "未知角色")
            continue
        choice = choice_by_id.get(character_id)
        if selections is not None and choice is None:
            skipped.append(character_id)
            continue
        choice = choice or {}
        state = character.get("state") or {}
        fields: list[str] = []
        if choice.get("situation", True) and update.get("situation"):
            state["situation"] = update["situation"]
            fields.append("situation")
        if choice.get("knowledge_add", True) and update.get("knowledge_add"):
            knowledge = [str(item) for item in (state.get("knowledge") or []) if str(item).strip()]
            for item in update["knowledge_add"]:
                if item not in knowledge:
                    knowledge.append(item)
            state["knowledge"] = knowledge[-20:] or None
            fields.append("knowledge")
        if choice.get("relations", True) and update.get("relations"):
            relations = [dict(row) for row in (state.get("relations") or []) if isinstance(row, dict)]
            notes = dict(state.get("relationship_notes") or {})
            for relation in update["relations"]:
                target = relation["target_id"]
                relations = [row for row in relations if str(row.get("target_id") or "") != target]
                relations.append(
                    {
                        "target_id": target,
                        "label": relation.get("label") or "",
                        "sentiment": relation.get("sentiment"),
                        "note": relation.get("note") or "",
                    }
                )
                if relation.get("note"):
                    notes[target] = relation["note"]
            state["relations"] = relations or None
            state["relationship_notes"] = notes or None
            fields.append("relations")
        if not fields:
            skipped.append(character_id)
            continue
        state["as_of_chapter_id"] = chapter_id
        character["state"] = state
        character["updated_at"] = now_iso()
        applied.append({"character_id": character_id, "name": character.get("name"), "fields": fields})
    if not applied:
        raise ValueError("没有任何提案被确认：角色卡未改动")
    store.save_characters(table)
    record["applied"] = True
    record["applied_at"] = now_iso()
    updates_file(store, chapter_id).write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {"chapter_id": chapter_id, "applied": applied, "skipped": skipped}
