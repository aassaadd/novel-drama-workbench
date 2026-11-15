"""人物表 CLI：`ndw character generate / apply / update / remove`。

负责 AI 生成人物、批量追加、单字段维护与删除；旁白 `chr_narrator` 不可删除。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from novel_drama.application.cast.character_crud import (
    UPDATE_FIELDS,
    append_characters,
    remove_character,
    update_character,
)
from novel_drama.application.cast.generate_characters import generate_characters
from novel_drama.composition import render_character_sheet
from novel_drama.infrastructure.config import AppConfig
from novel_drama.models import entities

from ..common import PreconditionError, _emit, _resolve_store, cast_hint


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
    """按 ID 更新单个人物的字段（用例在 application.cast.character_crud）。

    参数:
        args: 已解析参数（id 及可选字段）。
        config: 本机配置。

    返回:
        进程退出码。
    """
    store = _resolve_store(args, config)
    changes = {field: getattr(args, field, None) for field in UPDATE_FIELDS}
    changes["relationship"] = getattr(args, "relationship", None)
    changes["relationship_remove"] = getattr(args, "relationship_remove", None)
    payload = update_character(store, args.id, changes)
    target = payload["character"]
    _emit(args, f"已更新角色 {target.get('name')}（{args.id}）：" + "、".join(payload["changed"]), target)
    cast_hint(store)
    return 0


def cmd_character_remove(args, config: AppConfig) -> int:
    """按 ID 删除角色（旁白不可删除；被引用时默认拦截，`--force` 才清理引用后删除）。

    参数:
        args: 已解析参数（id / force / json）。
        config: 本机配置。

    返回:
        进程退出码。
    """
    store = _resolve_store(args, config)
    payload = remove_character(store, args.id, force=bool(getattr(args, "force", False)))
    text = f"已删除角色 {args.id}"
    if payload["cleaned"] and any(payload["cleaned"].values()):
        detail = "、".join(f"{key} {value}" for key, value in payload["cleaned"].items() if value)
        text += f"，并清理 {payload['references']} 处引用（{detail}）"
    _emit(args, text, payload)
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
