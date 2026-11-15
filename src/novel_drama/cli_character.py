"""人物表 CLI：`ndw character generate / apply / update / remove`。

负责 AI 生成人物、批量追加、单字段维护与删除；旁白 `chr_narrator` 不可删除。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .cli_common import PreconditionError, _emit, _resolve_store, cast_hint
from .config import AppConfig
from .media.art import render_character_sheet
from .models import entities
from .models.enums import AgeRange, SpeechPitch, SpeechTempo, choices_text
from .models.ids import now_iso
from .writing import generate_characters

#: 允许写入的角色定位（narrator 由系统保留）
ROLE_CHOICES = ("protagonist", "antagonist", "supporting", "minor")
#: 允许写入的性别枚举
GENDER_CHOICES = ("male", "female", "other", "unknown")


def _split_items(text: str) -> list[str]:
    """把逗号/顿号分隔的文本拆成去空列表。

    参数:
        text: 命令行原始文本。

    返回:
        条目列表。
    """
    import re

    return [part.strip() for part in re.split(r"[、,，;；]+", text or "") if part.strip()]


def _clean_speech_style(style: dict, current: dict | None = None) -> dict:
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


def append_characters(store, rows: list[dict]) -> dict:
    """把生成/导入的人物追加到 characters.json。

    参数:
        store: 工程。
        rows: [{name, role, gender, age_range, personality, background, appearance_prompt,
        negative_prompt, relationships（或 state.relationship_notes）}]。

    返回:
        {"characters": 新建人物列表, "skipped": 重名被跳过的人物名列表}。

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


def _load_rows(args) -> list[dict]:
    """从 `--file` 读取待追加的人物 JSON。

    参数:
        args: 已解析参数（含 file）。

    返回:
        人物列表。

    异常:
        ValueError: JSON 结构不合法。
    """
    raw = sys.stdin.read() if args.file == "-" else Path(args.file).expanduser().read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON 解析失败: {exc}") from exc
    rows = data.get("characters") if isinstance(data, dict) else data
    if not isinstance(rows, list) or not rows:
        raise ValueError("JSON 里没有 characters 数组")
    return rows


def cmd_character_generate(args, config: AppConfig) -> int:
    """AI 生成人物并（默认）落盘。

    参数:
        args: 已解析参数（count / dry_run / json）。
        config: 本机配置。

    返回:
        进程退出码。
    """
    store = _resolve_store(args, config)
    result = generate_characters(
        store,
        config,
        count=getattr(args, "count", None),
        brief=getattr(args, "brief", None),
    )
    if getattr(args, "dry_run", False):
        _emit(args, f"[dry-run] 已生成 {len(result['characters'])} 个人物", {**result, "applied": False})
        return 0
    rows = list(result["characters"]) + list(result.get("stock_characters") or [])
    applied = append_characters(store, rows)
    narrator_profile = result.get("narrator_profile") or {}
    if narrator_profile.get("voice_profile"):
        table = store.load_characters()
        narrator = next((item for item in table["characters"] if item.get("id") == entities.NARRATOR_ID), None)
        if narrator is not None and not narrator.get("voice_profile"):
            narrator["voice_profile"] = narrator_profile["voice_profile"]
            store.save_characters(table)
    payload = {
        "characters": applied["characters"],
        "stock_characters": len(result.get("stock_characters") or []),
        "narrator_profile": bool(narrator_profile.get("voice_profile")),
        "applied": True,
        "skipped": applied["skipped"],
    }
    text = f"已写入 {len(applied['characters'])} 个人物"
    if result.get("stock_characters"):
        text += f"（含龙套 {len(result['stock_characters'])} 个）"
    if applied["skipped"]:
        text += f"（重名跳过：{'、'.join(applied['skipped'])}）"
    _emit(args, text, payload)
    return 0


def cmd_character_apply(args, config: AppConfig) -> int:
    """从 JSON 文件/stdin 批量追加人物。

    参数:
        args: 已解析参数（file / json）。
        config: 本机配置。

    返回:
        进程退出码。
    """
    store = _resolve_store(args, config)
    applied = append_characters(store, _load_rows(args))
    payload = {"characters": applied["characters"], "applied": True, "skipped": applied["skipped"]}
    text = f"已写入 {len(applied['characters'])} 个人物"
    if applied["skipped"]:
        text += f"（重名跳过：{'、'.join(applied['skipped'])}）"
    _emit(args, text, payload)
    if not getattr(args, "dry_run", False):
        # 保存人物后提示音色/BGM 编排问题，不阻断写入
        cast_hint(store)
    return 0


def cmd_character_update(args, config: AppConfig) -> int:
    """按 ID 更新单个人物的字段。

    参数:
        args: 已解析参数（id 及可选字段）。
        config: 本机配置。

    返回:
        进程退出码。

    异常:
        PreconditionError: 角色不存在或新名字已被占用。
    """
    store = _resolve_store(args, config)
    table = store.load_characters()
    target = next((item for item in table["characters"] if item.get("id") == args.id), None)
    if target is None:
        raise PreconditionError(f"角色不存在: {args.id}")
    # 关系键统一为角色 ID：姓名只用于展示，写入前先映射成 ID
    id_by_name = {str(item.get("name") or ""): str(item.get("id") or "") for item in table["characters"]}

    changed: list[str] = []
    if getattr(args, "name", None) is not None:
        name = args.name.strip()
        if not name:
            raise ValueError("角色名不能为空")
        if any(item.get("name") == name and item.get("id") != args.id for item in table["characters"]):
            raise PreconditionError(f"角色名已存在: {name}")
        target["name"] = name
        changed.append(f"name={name}")
    if getattr(args, "role", None):
        target["role"] = args.role
        changed.append(f"role={args.role}")
    if getattr(args, "gender", None):
        target["gender"] = args.gender
        changed.append(f"gender={args.gender}")
    if getattr(args, "age_range", None) is not None:
        raw_age = args.age_range.strip()
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
    if getattr(args, "personality", None):
        target["personality"] = _split_items(args.personality)
        changed.append("personality")
    if getattr(args, "background", None) is not None:
        target["background"] = args.background
        changed.append("background")
    if getattr(args, "aliases", None) is not None:
        target["aliases"] = _split_items(args.aliases)
        changed.append("aliases")
    if getattr(args, "appearance_prompt", None) is not None:
        target["appearance_prompt"] = args.appearance_prompt.strip()
        changed.append("appearance_prompt")
    if getattr(args, "negative_prompt", None) is not None:
        target["negative_prompt"] = args.negative_prompt.strip()
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
        value = getattr(args, field, None)
        if value is not None:
            target[field] = value.strip()
            changed.append(field)
    if getattr(args, "voice_profile", None) is not None:
        raw = args.voice_profile.strip()
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
    if getattr(args, "stock", None) is not None:
        raw = args.stock.strip()
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

    if getattr(args, "speech_style", None) is not None:
        raw = str(args.speech_style or "").strip()
        if raw:
            try:
                style = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(f"speech_style 不是合法 JSON: {exc}") from exc
            if not isinstance(style, dict):
                raise ValueError("speech_style 必须是 JSON 对象")
            target["speech_style"] = _clean_speech_style(style, target.get("speech_style"))
        else:
            target["speech_style"] = {"tempo": None, "pitch": None, "accent": None, "catchphrases": []}
        changed.append("speech_style")

    state = target.get("state") or {}
    state_changed = False
    if getattr(args, "situation", None) is not None:
        state["situation"] = args.situation.strip() or None
        state_changed = True
        changed.append("situation")
    if getattr(args, "knowledge", None) is not None:
        raw = str(args.knowledge or "").strip()
        if raw.startswith("["):
            try:
                items = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(f"knowledge 不是合法 JSON: {exc}") from exc
        else:
            items = _split_items(raw)
        state["knowledge"] = [str(item).strip()[:200] for item in items if str(item).strip()] or None
        state_changed = True
        changed.append("knowledge")
    if getattr(args, "relations", None) is not None:
        raw = str(args.relations or "").strip()
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
    for item in getattr(args, "relationship", None) or []:
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
    for item in getattr(args, "relationship_remove", None) or []:
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
    _emit(args, f"已更新角色 {target.get('name')}（{args.id}）：" + "、".join(changed), target)
    if not getattr(args, "dry_run", False):
        # 保存人物后提示音色/BGM 编排问题，不阻断写入
        cast_hint(store)
    return 0


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
    settings_available = False
    try:
        from ..store.settings_store import SettingsStore

        settings = SettingsStore(store.root)
        if settings.exists():
            settings_available = True
            bible = settings.ensure()
            by_chapter = {item.get("id"): item for item in chapters}
            for act in (bible.get("outline") or {}).get("acts") or []:
                for outline_chapter in act.get("chapters") or []:
                    chapter_id = outline_chapter.get("id")
                    if not chapter_id:
                        continue
                    pov = outline_chapter.get("pov_character_id") == character_id
                    beats = sum(
                        1
                        for beat in outline_chapter.get("beats") or []
                        if character_id in (beat.get("character_ids") or [])
                    )
                    if not pov and not beats:
                        continue
                    row = by_chapter.get(chapter_id)
                    if row is None:
                        row = {
                            "id": chapter_id,
                            "index": outline_chapter.get("index"),
                            "title": outline_chapter.get("title"),
                            "lines": [],
                            "scenes": 0,
                            "pov": False,
                            "beats": 0,
                        }
                        chapters.append(row)
                        by_chapter[chapter_id] = row
                    row["pov"] = bool(row.get("pov") or pov)
                    row["beats"] = int(row.get("beats") or 0) + beats
                    total += (1 if pov else 0) + beats
    except Exception:  # 设定库损坏不应阻断删除保护之外的正常查询
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


def cmd_character_remove(args, config: AppConfig) -> int:
    """按 ID 删除角色（旁白不可删除；仍被台词引用时拒绝并列出位置）。

    参数:
        args: 已解析参数（id / json）。
        config: 本机配置。

    返回:
        进程退出码。

    异常:
        ValueError: 试图删除旁白，或角色仍被台词引用。
        PreconditionError: 角色不存在。
    """
    store = _resolve_store(args, config)
    if args.id == entities.NARRATOR_ID:
        raise ValueError("旁白是保留角色，不能删除")
    table = store.load_characters()
    if not any(item.get("id") == args.id for item in table["characters"]):
        raise PreconditionError(f"角色不存在: {args.id}")
    refs = character_refs(store, args.id)
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
    table["characters"] = [item for item in table["characters"] if item.get("id") != args.id]
    store.save_characters(table)
    _emit(args, f"已删除角色 {args.id}", {"removed": args.id, "characters": len(table["characters"])})
    return 0


def cmd_character_image(args, config: AppConfig) -> int:
    """生成角色特写 / 三视图并写回 image_assets。

    参数:
        args: 已解析参数（id / kind / force / json）。
        config: 本机配置。

    返回:
        进程退出码。

    异常:
        PreconditionError: 角色不存在。
    """
    store = _resolve_store(args, config)
    table = store.load_characters()
    if not any(item.get("id") == args.id for item in table["characters"]):
        raise PreconditionError(f"角色不存在: {args.id}")
    result = render_character_sheet(store, config, args.id, args.kind, force=bool(getattr(args, "force", False)))
    state = "已生成" if result.get("created") else "已存在，复用"
    _emit(args, f"角色设定图{state}: {result.get('image_asset')}", result)
    return 0
