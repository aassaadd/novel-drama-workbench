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
        character["age_range"] = str(row.get("age_range") or "").strip() or None
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
        target["age_range"] = args.age_range.strip() or None
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

    state = target.get("state") or {}
    state_changed = False
    if getattr(args, "situation", None) is not None:
        state["situation"] = args.situation.strip() or None
        state_changed = True
        changed.append("situation")
    for item in getattr(args, "relationship", None) or []:
        if "=" not in item:
            raise ValueError(f"--relationship 需要 `目标ID=备注` 格式: {item}")
        key, note = item.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError("--relationship 的目标角色 ID 不能为空")
        notes = state.get("relationship_notes") or {}
        notes[key] = note.strip()[:500]
        state["relationship_notes"] = notes
        state_changed = True
        changed.append(f"relationship:{key}")
    for item in getattr(args, "relationship_remove", None) or []:
        key = str(item or "").strip()
        if not key:
            continue
        notes = state.get("relationship_notes") or {}
        if key in notes:
            del notes[key]
            state["relationship_notes"] = notes
            state_changed = True
            changed.append(f"relationship-remove:{key}")
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


def cmd_character_remove(args, config: AppConfig) -> int:
    """按 ID 删除角色（旁白不可删除）。

    参数:
        args: 已解析参数（id / json）。
        config: 本机配置。

    返回:
        进程退出码。

    异常:
        ValueError: 试图删除旁白。
        PreconditionError: 角色不存在。
    """
    store = _resolve_store(args, config)
    if args.id == entities.NARRATOR_ID:
        raise ValueError("旁白是保留角色，不能删除")
    table = store.load_characters()
    before = len(table["characters"])
    table["characters"] = [item for item in table["characters"] if item.get("id") != args.id]
    if len(table["characters"]) == before:
        raise PreconditionError(f"角色不存在: {args.id}")
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
