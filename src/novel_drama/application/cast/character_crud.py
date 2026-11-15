"""角色表的写用例：批量追加、单字段更新、删除与引用统计。

CLI（`character apply/update/remove`）与 Web 共用本模块，写入规则只有一份。
"""

from __future__ import annotations

import json

from ...domain.shared.errors import PreconditionError
from ...models import entities
from ...models.enums import AgeRange, SpeechPitch, SpeechTempo, choices_text
from ...models.ids import now_iso
from ...store import ProjectStore

#: 允许写入的角色定位（narrator 由系统保留）
ROLE_CHOICES = ("protagonist", "antagonist", "supporting", "minor")
#: 允许写入的性别枚举
GENDER_CHOICES = ("male", "female", "other", "unknown")

#: `update_character` 接受的标量字段（与 CLI 参数名一致）
UPDATE_FIELDS = (
    "name",
    "role",
    "gender",
    "age_range",
    "personality",
    "background",
    "aliases",
    "appearance_prompt",
    "negative_prompt",
    "voice_description_zh",
    "voice_description_en",
    "appearance_desc_zh",
    "appearance_desc_en",
    "appearance_negative_zh",
    "appearance_negative_en",
    "situation",
    "knowledge",
    "relations",
    "speech_style",
    "voice_profile",
    "stock",
)


def split_items(text: str) -> list[str]:
    """把逗号/顿号分隔的文本拆成去空列表。

    参数:
        text: 原始文本。

    返回:
        条目列表。
    """
    import re

    return [part.strip() for part in re.split(r"[、,，;；]+", text or "") if part.strip()]


def clean_speech_style(style: dict, current: dict | None = None) -> dict:
    """清洗说话方式：语速 / 音高只接受枚举值，口音与口头禅保留自由文本。

    参数:
        style: 界面或命令行传入的说话方式对象。
        current: 磁盘上现有的说话方式；传入时，与现有值完全相同的旧非法值原样保留，
            这样只修一个字段（如语速）不会因为另一个历史字段（如音高「高」）被连带拒绝。

    返回:
        `{tempo, pitch, accent, catchphrases}`，未填写项为 None / 空数组。

    异常:
        ValueError: 语速或音高不在可选范围（中文消息含可选值）。
    """
    raw_tempo = str(style.get("tempo") or "").strip()
    raw_pitch = str(style.get("pitch") or "").strip()
    old_tempo = str((current or {}).get("tempo") or "").strip()
    old_pitch = str((current or {}).get("pitch") or "").strip()
    tempo = SpeechTempo.coerce(raw_tempo) if raw_tempo else None
    pitch = SpeechPitch.coerce(raw_pitch) if raw_pitch else None
    if raw_tempo and tempo is None:
        if raw_tempo != old_tempo:
            raise ValueError(f"语速「{raw_tempo}」不在可选范围；可选值：{choices_text(SpeechTempo)}")
        tempo = raw_tempo
    if raw_pitch and pitch is None:
        if raw_pitch != old_pitch:
            raise ValueError(f"音高「{raw_pitch}」不在可选范围；可选值：{choices_text(SpeechPitch)}")
        pitch = raw_pitch
    return {
        "tempo": tempo,
        "pitch": pitch,
        "accent": str(style.get("accent") or "").strip()[:40] or None,
        "catchphrases": [str(item).strip()[:60] for item in (style.get("catchphrases") or []) if str(item).strip()],
    }


def append_characters(store: ProjectStore, rows: list[dict]) -> dict:
    """把生成/导入的人物追加到 characters.json。

    参数:
        store: 工程。
        rows: [{name, role, gender, age_range, personality, background, appearance_prompt,
        negative_prompt, relationships（或 state.relationship_notes）}]。

    返回:
        `{"characters": 新建人物列表, "skipped": 重名被跳过的人物名列表}`。

    异常:
        ValueError: 字段不合法。
    """
    table = store.load_characters()
    existing = {item.get("name") for item in table["characters"]}
    id_by_name = {item.get("name"): item.get("id") for item in table["characters"] if item.get("name")}
    created: list[dict] = []
    skipped: list[str] = []
    pending: list[tuple[dict, dict]] = []

    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"第 {index + 1} 个人物必须是对象")
        name = str(row.get("name") or "").strip()
        if not name:
            raise ValueError("人物姓名不能为空")
        if len(name) > 60:
            raise ValueError(f"人物姓名超过 60 字上限: {name}")
        if name in existing:
            skipped.append(name)
            continue
        role = str(row.get("role") or "supporting").strip()
        if role not in ROLE_CHOICES:
            raise ValueError(f"人物 {name} 的 role 非法: {role}（可选 {'/'.join(ROLE_CHOICES)}）")
        gender = str(row.get("gender") or "unknown").strip()
        character = entities.new_character(name, role=role)
        character["gender"] = gender if gender in GENDER_CHOICES else "unknown"
        character["age_range"] = AgeRange.normalize(row.get("age_range")) if str(row.get("age_range") or "").strip() else None
        character["personality"] = [str(item).strip() for item in (row.get("personality") or []) if str(item).strip()]
        character["background"] = str(row.get("background") or "").strip()
        character["appearance_prompt"] = str(row.get("appearance_prompt") or "").strip()
        character["negative_prompt"] = str(row.get("negative_prompt") or "").strip()
        relationships = row.get("relationships") or {}
        state = row.get("state") if isinstance(row.get("state"), dict) else {}
        if not relationships and isinstance(state.get("relationship_notes"), dict):
            relationships = state["relationship_notes"]
        character["state"] = {"situation": None, "relationship_notes": None, "knowledge": None, "as_of_chapter_id": None}
        if isinstance(row.get("voice_profile"), dict):
            character["voice_profile"] = row["voice_profile"]
        if isinstance(row.get("stock"), dict):
            character["stock"] = row["stock"]
        if isinstance(row.get("voice_binding"), dict):
            character["voice_binding"] = row["voice_binding"]
        table["characters"].append(character)
        existing.add(name)
        id_by_name[name] = character["id"]
        created.append(character)
        if isinstance(relationships, dict):
            pending.append((character, relationships))

    # 全部角色建好后再解析关系目标，保证同批生成的人物也能映射到真实 ID
    for character, relationships in pending:
        notes: dict[str, str] = {}
        for target, note in relationships.items():
            key = str(target or "").strip()
            if not key:
                continue
            notes[id_by_name.get(key, key)] = str(note or "").strip()[:500]
        character["state"]["relationship_notes"] = notes or None

    if created:
        store.save_characters(table)
    return {"characters": created, "skipped": skipped}


def update_character(store: ProjectStore, character_id: str, changes: dict) -> dict:
    """按 ID 更新单个人物的字段。

    参数:
        store: 工程。
        character_id: 角色 ID。
        changes: 字段名 → 原值（与 CLI 参数同类型；JSON 型字段传字符串）。
            `relationship` 为 `目标ID=备注` 列表，`relationship_remove` 为要删除的关系目标。

    返回:
        `{"character": 更新后的角色, "changed": ["name=新名", ...]}`。

    异常:
        PreconditionError: 角色不存在或新名字已被占用。
        ValueError: 字段值不合法或没有任何可修改字段。
    """
    table = store.load_characters()
    target = next((item for item in table["characters"] if item.get("id") == character_id), None)
    if target is None:
        raise PreconditionError(f"角色不存在: {character_id}")
    # 关系键统一为角色 ID：姓名只用于展示，写入前先映射成 ID
    id_by_name = {str(item.get("name") or ""): str(item.get("id") or "") for item in table["characters"]}

    def value(key: str):
        return changes.get(key)

    changed: list[str] = []
    if value("name") is not None:
        name = str(value("name")).strip()
        if not name:
            raise ValueError("角色名不能为空")
        if any(item.get("name") == name and item.get("id") != character_id for item in table["characters"]):
            raise PreconditionError(f"角色名已存在: {name}")
        target["name"] = name
        changed.append(f"name={name}")
    if value("role"):
        target["role"] = value("role")
        changed.append(f"role={value('role')}")
    if value("gender"):
        target["gender"] = value("gender")
        changed.append(f"gender={value('gender')}")
    if value("age_range") is not None:
        raw_age = str(value("age_range")).strip()
        if not raw_age:
            target["age_range"] = None
        else:
            age = AgeRange.coerce(raw_age)
            if age is None:
                # 历史非法值原样提交时放行（由界面提示修正），改动成新的非法值仍然拒绝
                if raw_age != str(target.get("age_range") or "").strip():
                    raise ValueError(f"年龄段「{raw_age}」不在可选范围；可选值：{choices_text(AgeRange)}")
                age = raw_age
            target["age_range"] = age
        changed.append("age_range")
    if value("personality"):
        target["personality"] = split_items(value("personality"))
        changed.append("personality")
    if value("background") is not None:
        target["background"] = value("background")
        changed.append("background")
    if value("aliases") is not None:
        target["aliases"] = split_items(value("aliases"))
        changed.append("aliases")
    if value("appearance_prompt") is not None:
        target["appearance_prompt"] = str(value("appearance_prompt")).strip()
        changed.append("appearance_prompt")
    if value("negative_prompt") is not None:
        target["negative_prompt"] = str(value("negative_prompt")).strip()
        changed.append("negative_prompt")
    # 六项双语描述（音色 / 外貌 / 负向）：后续配音与出图直接取用，允许手改与清空
    for field in (
        "voice_description_zh",
        "voice_description_en",
        "appearance_desc_zh",
        "appearance_desc_en",
        "appearance_negative_zh",
        "appearance_negative_en",
    ):
        if value(field) is not None:
            target[field] = str(value(field)).strip()
            changed.append(field)
    if value("voice_profile") is not None:
        raw = str(value("voice_profile")).strip()
        if raw:
            try:
                profile = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(f"voice_profile 不是合法 JSON: {exc}") from exc
            if not isinstance(profile, dict):
                raise ValueError("voice_profile 必须是 JSON 对象")
            target["voice_profile"] = profile
        else:
            target.pop("voice_profile", None)
        changed.append("voice_profile")
    if value("stock") is not None:
        raw = str(value("stock")).strip()
        if raw:
            try:
                stock = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(f"stock 不是合法 JSON: {exc}") from exc
            if not isinstance(stock, dict) or not stock.get("slot"):
                raise ValueError("stock 必须是含 slot 的 JSON 对象")
            target["stock"] = stock
        else:
            target.pop("stock", None)
        changed.append("stock")

    if value("speech_style") is not None:
        raw = str(value("speech_style") or "").strip()
        if raw:
            try:
                style = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(f"speech_style 不是合法 JSON: {exc}") from exc
            if not isinstance(style, dict):
                raise ValueError("speech_style 必须是 JSON 对象")
            target["speech_style"] = clean_speech_style(style, target.get("speech_style"))
        else:
            target["speech_style"] = {"tempo": None, "pitch": None, "accent": None, "catchphrases": []}
        changed.append("speech_style")

    state = target.get("state") or {}
    state_changed = False
    if value("situation") is not None:
        state["situation"] = str(value("situation")).strip() or None
        state_changed = True
        changed.append("situation")
    if value("knowledge") is not None:
        raw = str(value("knowledge") or "").strip()
        if raw.startswith("["):
            try:
                items = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(f"knowledge 不是合法 JSON: {exc}") from exc
        else:
            items = split_items(raw)
        state["knowledge"] = [str(item).strip()[:200] for item in items if str(item).strip()] or None
        state_changed = True
        changed.append("knowledge")
    if value("relations") is not None:
        raw = str(value("relations") or "").strip()
        rows = json.loads(raw) if raw else []
        if not isinstance(rows, list):
            raise ValueError("relations 必须是 JSON 数组")
        clean_rows: list[dict] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            target_id = str(row.get("target_id") or "").strip()
            if not target_id:
                continue
            if target_id not in id_by_name.values():
                raise ValueError(f"关系目标角色不存在：{target_id}")
            clean_rows.append(
                {
                    "target_id": target_id,
                    "label": str(row.get("label") or "").strip()[:40],
                    "sentiment": row.get("sentiment") if row.get("sentiment") in ("ally", "enemy", "family", "romance", "neutral") else None,
                    "since_chapter_id": str(row.get("since_chapter_id") or "").strip() or None,
                    "note": str(row.get("note") or "").strip()[:500],
                }
            )
        state["relations"] = clean_rows or None
        state_changed = True
        changed.append("relations")
    for item in changes.get("relationship") or []:
        if "=" not in item:
            raise ValueError(f"--relationship 需要 `目标ID=备注` 格式: {item}")
        key, note = item.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError("--relationship 的目标角色 ID 不能为空")
        # 允许命令行传姓名，落盘前统一换成角色 ID
        key = id_by_name.get(key, key)
        if key not in id_by_name.values():
            raise ValueError(f"关系目标角色不存在：{key}；请使用角色 ID（人物页可复制）")
        notes = state.get("relationship_notes") or {}
        notes[key] = note.strip()[:500]
        state["relationship_notes"] = notes
        state_changed = True
        changed.append(f"relationship:{key}")
    for item in changes.get("relationship_remove") or []:
        key = str(item or "").strip()
        if not key:
            continue
        key = id_by_name.get(key, key)
        notes = state.get("relationship_notes") or {}
        if key in notes:
            del notes[key]
            state["relationship_notes"] = notes
            state_changed = True
            changed.append(f"relationship-remove:{key}")
        # 结构化关系同步删除，避免关系图残留悬空边
        relations = state.get("relations") or []
        kept = [row for row in relations if str(row.get("target_id") or "") != key]
        if len(kept) != len(relations):
            state["relations"] = kept or None
            state_changed = True
    if state_changed:
        target["state"] = state

    if not changed:
        raise ValueError("没有要修改的字段：至少给一个（如 --name / --role / --background）")
    target["updated_at"] = now_iso()
    store.save_characters(table)
    return {"character": target, "changed": changed}


def character_refs(store, character_id: str) -> dict:
    """汇总某个角色被哪些章节/台词行/大纲/关系引用（删除前保护与页面提示用）。

    四类引用都算数：台词说话人、场景出场标签、大纲 POV 与节拍选角、其它角色的关系目标。

    参数:
        store: 工程读写入口。
        character_id: 角色 ID。

    返回:
        `{total, lines_total, chapters: [{id, index, title, lines, scenes, pov, beats}],
        relations: [{id, name, note}], settings_available}`。
    """
    chapters: list[dict] = []
    total = 0
    lines_total = 0
    for meta in store.list_chapters():
        chapter = store.load_chapter(meta["id"])
        rows = [
            {"line_id": line.get("id"), "text": line.get("text")}
            for scene in chapter.get("scenes") or []
            for line in scene.get("lines") or []
            if line.get("speaker_id") == character_id
        ]
        scenes = sum(
            1
            for scene in chapter.get("scenes") or []
            if character_id in (scene.get("character_ids") or [])
        )
        if not rows and not scenes:
            continue
        total += len(rows) + (1 if scenes else 0)
        lines_total += len(rows)
        chapters.append(
            {
                "id": meta["id"],
                "index": meta.get("index"),
                "title": meta.get("title"),
                "lines": rows[:50],
                "scenes": scenes,
                "pov": False,
                "beats": 0,
            }
        )
    # 现状对齐（本次重构不顺手改行为）：旧实现的设定库读取写成了 `from ..store...`，
    # 而 `cli_character` 位于 `novel_drama` 根包，`..` 越界必抛 ImportError 并被
    # `except Exception` 吞掉，所以删除保护实际只统计台词/场景/关系，不看大纲 POV/节拍。
    # 迁移到 application 后 `...store` 是正确的，但计入大纲引用会改变“能否删除”的判定，
    # 需产品确认后单独修（已在阶段汇报中列出复现步骤）。
    settings_available = False

    relations: list[dict] = []
    table = store.load_characters()
    for item in table.get("characters") or []:
        if item.get("id") == character_id:
            continue
        notes = (item.get("state") or {}).get("relationship_notes") or {}
        relations_rows = (item.get("state") or {}).get("relations") or []
        note = notes.get(character_id)
        has_relation = any(str(row.get("target_id") or "") == character_id for row in relations_rows)
        if note or has_relation:
            relations.append({"id": item.get("id"), "name": item.get("name"), "note": note or ""})
    total += len(relations)
    chapters.sort(key=lambda item: int(item.get("index") or 0))
    return {
        "total": total,
        "lines_total": lines_total,
        "chapters": chapters,
        "relations": relations,
        "settings_available": settings_available,
    }


def remove_character(store: ProjectStore, character_id: str) -> dict:
    """按 ID 删除角色（旁白不可删除；仍被引用时拒绝并列出位置）。

    参数:
        store: 工程。
        character_id: 角色 ID。

    返回:
        `{"removed": id, "characters": 剩余角色数}`。

    异常:
        ValueError: 试图删除旁白，或角色仍被引用。
        PreconditionError: 角色不存在。
    """
    if character_id == entities.NARRATOR_ID:
        raise ValueError("旁白是保留角色，不能删除")
    table = store.load_characters()
    if not any(item.get("id") == character_id for item in table["characters"]):
        raise PreconditionError(f"角色不存在: {character_id}")
    refs = character_refs(store, character_id)
    if refs["total"]:
        chapters = "、".join(str(item.get("title") or item.get("id")) for item in refs["chapters"][:6])
        parts = []
        if refs.get("lines_total"):
            parts.append(f"{refs['lines_total']} 行台词")
        if refs.get("chapters"):
            parts.append(f"{len(refs['chapters'])} 章出场/大纲引用")
        if refs.get("relations"):
            parts.append(f"{len(refs['relations'])} 个角色的关系指向")
        raise ValueError(
            f"角色仍被引用（{'，'.join(parts) or '未知引用'}；章节：{chapters}），"
            "请先改派台词、调整大纲选角/视角，并清理其它角色的关系后再删除"
        )
    table["characters"] = [item for item in table["characters"] if item.get("id") != character_id]
    store.save_characters(table)
    return {"removed": character_id, "characters": len(table["characters"])}
