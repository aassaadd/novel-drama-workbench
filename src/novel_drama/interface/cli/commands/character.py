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
    character_sheet,
    remove_character,
    update_character,
)
from novel_drama.application.cast.generate_characters import (
    generate_characters,
    preview_characters,
)
from novel_drama.application.cast.queries import character_list
from novel_drama.infrastructure.config import AppConfig
from novel_drama.interface.cli.common import EXIT_OK
from novel_drama.models import entities
from novel_drama.models.ids import now_iso

from ..common import PreconditionError, _emit, _resolve_store, cast_hint


def cmd_character_add(args, config: AppConfig) -> int:
    """新增角色。"""
    store = _resolve_store(args, config)
    table = store.load_characters()
    if any(item["name"] == args.name for item in table["characters"]):
        raise PreconditionError(f"角色已存在: {args.name}")
    character = entities.new_character(args.name, role=args.role, aliases=args.alias or [])
    if args.appearance:
        character["appearance_prompt"] = args.appearance
    table["characters"].append(character)
    store.save_characters(table)
    _emit(args, f"角色已创建: {character['id']} {character['name']}（{character['role']}）", character)
    return EXIT_OK


def cmd_character_list(args, config: AppConfig) -> int:
    """列出角色与音色绑定状态（JSON 里补齐 image_assets 字段）。

    参数:
        args: 已解析参数（json）。
        config: 本机配置。

    返回:
        进程退出码。
    """
    store = _resolve_store(args, config)
    rows = character_list(store)
    characters = store.load_characters()["characters"]
    if getattr(args, "json", False):
        _emit(args, payload=rows)
        return EXIT_OK
    for item in characters:
        voice = (item.get("voice_binding") or {}).get("voice_id") or "未绑定音色"
        print(f"{item['id']}  {item['name']:<8} {item['role']:<12} {voice}")
    return EXIT_OK


def cmd_character_bind_voice(args, config: AppConfig) -> int:
    """绑定角色音色（变更后相关台词需重跑）。"""
    store = _resolve_store(args, config)
    table = store.load_characters()
    target = next((item for item in table["characters"] if item["id"] == args.character or item["name"] == args.character), None)
    if target is None:
        raise PreconditionError(f"角色不存在: {args.character}")
    target["voice_binding"] = {"adapter": args.adapter, "voice_id": args.voice, "reference_audio": None, "seed": None, "speed": args.speed, "emotion_map": {}}
    target["updated_at"] = now_iso()
    store.save_characters(table)
    _emit(args, f"已绑定 {target['name']} → {args.voice}（该角色台词需重跑配音）", target)
    return EXIT_OK


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
    if getattr(args, "dry_run", False):
        # 预览不落盘：与页面「生成角色」走同一个用例
        result = preview_characters(
            store,
            config,
            count=getattr(args, "count", None),
            brief=getattr(args, "brief", None),
        )
        _emit(args, f"[dry-run] 已生成 {len(result['characters'])} 个人物", result)
        return 0
    result = generate_characters(
        store,
        config,
        count=getattr(args, "count", None),
        brief=getattr(args, "brief", None),
    )
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
    result = character_sheet(
        store, config, args.id, args.kind, force=bool(getattr(args, "force", False))
    )
    state = "已生成" if result.get("created") else "已存在，复用"
    _emit(args, f"角色设定图{state}: {result.get('image_asset')}", result)
    return 0


def add_parser(sub) -> None:
    """注册 `character` 命令组。"""
    p = sub.add_parser("character", help="角色与音色")
    p_sub = p.add_subparsers(dest="action", required=True)
    q = p_sub.add_parser("add")
    q.add_argument("--name", required=True)
    q.add_argument("--role", default="supporting", choices=["protagonist", "antagonist", "supporting", "minor"])
    q.add_argument("--alias", action="append")
    q.add_argument("--appearance", default="")
    q.set_defaults(func=cmd_character_add)
    p_sub.add_parser("list").set_defaults(func=cmd_character_list)
    q = p_sub.add_parser("bind-voice")
    q.add_argument("--character", required=True)
    q.add_argument("--voice", required=True)
    q.add_argument("--adapter", default="placeholder")
    q.add_argument("--speed", type=float, default=1.0)
    q.set_defaults(func=cmd_character_bind_voice)
    q = p_sub.add_parser("generate", help="AI 生成人物（数量按需求自行判断）")
    q.add_argument("--count", type=int, help="人物总数；缺省时由模型按需求决定生成几个角色")
    q.add_argument("--brief", help="补充要求，如「一个主角和一个反派」")
    q.add_argument("--dry-run", action="store_true", help="只预览，不写 characters.json")
    q.set_defaults(func=cmd_character_generate)
    q = p_sub.add_parser("image", help="生成角色全身像（母图）/ 头像 / 三视图并写回 image_assets")
    q.add_argument("--id", required=True)
    q.add_argument("--kind", required=True, choices=["fullbody", "closeup", "turnaround"])
    q.add_argument("--force", action="store_true", help="强制重绘")
    q.set_defaults(func=cmd_character_image)
    q = p_sub.add_parser("apply", help="从 JSON 批量追加人物")
    q.add_argument("--file", required=True, help="JSON 文件路径，- 表示从 stdin 读取")
    q.set_defaults(func=cmd_character_apply)
    q = p_sub.add_parser("update", help="按 ID 更新角色字段")
    q.add_argument("--id", required=True)
    q.add_argument("--name")
    q.add_argument("--role", choices=["protagonist", "antagonist", "supporting", "minor"])
    q.add_argument("--gender", choices=["male", "female", "other", "unknown"])
    q.add_argument("--age-range", dest="age_range")
    q.add_argument("--personality", help="性格标签，逗号/顿号分隔")
    q.add_argument("--background")
    q.add_argument("--aliases", help="别名，逗号/顿号分隔")
    q.add_argument("--appearance-prompt", dest="appearance_prompt", help="角色外貌提示词（配图用，空串表示清空）")
    q.add_argument("--negative-prompt", dest="negative_prompt", help="角色负向提示词（配图用，空串表示清空）")
    q.add_argument("--voice-description-zh", dest="voice_description_zh", help="中文音色描述（空串表示清空）")
    q.add_argument("--voice-description-en", dest="voice_description_en", help="英文音色描述（空串表示清空）")
    q.add_argument("--appearance-desc-zh", dest="appearance_desc_zh", help="中文外貌描述（空串表示清空）")
    q.add_argument("--appearance-desc-en", dest="appearance_desc_en", help="英文外貌描述（空串表示清空）")
    q.add_argument("--appearance-negative-zh", dest="appearance_negative_zh", help="中文负向说明（空串表示清空）")
    q.add_argument("--appearance-negative-en", dest="appearance_negative_en", help="英文负向提示词（空串表示清空）")
    q.add_argument("--situation", help="当前处境")
    q.add_argument("--knowledge", help="已知信息，逗号分隔或 JSON 数组（空串表示清空）")
    q.add_argument("--relations", help="结构化关系 JSON 数组：[{target_id,label,sentiment,note}]")
    q.add_argument("--speech-style", dest="speech_style", help="说话方式 JSON：{tempo,pitch,accent,catchphrases}")
    q.add_argument("--voice-profile", dest="voice_profile", help="音色描述 JSON（空串表示清空）")
    q.add_argument("--stock", help="龙套槽位 JSON（空串表示清空）")
    q.add_argument("--relationship", action="append", help="`目标ID=备注`，可重复")
    q.add_argument("--relationship-remove", dest="relationship_remove", action="append", help="要移除的关系目标ID，可重复")
    q.set_defaults(func=cmd_character_update)
    q = p_sub.add_parser("remove", help="按 ID 删除角色（旁白不可删；被引用时默认拦截）")
    q.add_argument("--id", required=True)
    q.add_argument("--force", action="store_true", help="确认删除并清理该角色的全部引用")
    q.set_defaults(func=cmd_character_remove)
