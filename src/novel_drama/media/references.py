"""参考图解析层：按「章节所属世界 + 场景出场角色」汇总关键图参考图清单。

本层只读取工程内的既有设定图（世界封面 / 角色全身像 / 头像）并给出告警，
不生成任何图片；缺图是否自动补全由上层（媒体执行器）按配置决定。
"""

from __future__ import annotations

from pathlib import Path

from ..models.entities import NARRATOR_ID, UNKNOWN_SPEAKER_ID
from ..store import ProjectStore, WorldStore

#: 参考图默认配置（`comfyui.references` 缺失时逐项回退）
DEFAULT_OPTIONS = {"enabled": True, "max_count": 4, "auto_fill": True, "use_world_cover": True}

#: 角色设定图取值优先级：全身像 → 头像 → 旧版头像字段
_CHARACTER_ASSET_KEYS = ("fullbody", "closeup")


def _as_bool(value, default: bool) -> bool:
    """把配置值宽松地解释为布尔。

    参数:
        value: 原始配置值（可能是 bool / 字符串 / None）。
        default: 值缺失（None）时的回退值。

    返回:
        布尔值；字符串 "false"/"0"/"no"/"off"/空串视为 False。
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in ("", "false", "0", "no", "off")
    return bool(value)


def _raw_options(config) -> dict:
    """从配置对象中取出 `comfyui.references` 原始字典。

    参数:
        config: AppConfig（带 `get(dotted, default)`）或普通 dict。

    返回:
        参考图配置字典；缺失或类型异常时返回空字典。
    """
    if config is None:
        return {}
    try:
        if isinstance(config, dict):
            value = ((config.get("comfyui") or {}) if isinstance(config.get("comfyui"), dict) else {}).get("references")
        elif hasattr(config, "get"):
            value = config.get("comfyui.references", {})
        else:
            value = None
    except Exception:  # 配置对象异常不应影响媒体流程
        value = None
    return value if isinstance(value, dict) else {}


def reference_options(config) -> dict:
    """读取参考图相关配置（缺失项回退默认值）。

    参数:
        config: AppConfig 或配置字典。

    返回:
        `{"enabled": bool, "max_count": int, "auto_fill": bool, "use_world_cover": bool}`，
        其中 max_count 被夹到 1..4，异常值回退 4。
    """
    raw = _raw_options(config)
    try:
        max_count = int(raw.get("max_count", DEFAULT_OPTIONS["max_count"]))
    except (TypeError, ValueError):
        max_count = DEFAULT_OPTIONS["max_count"]
    max_count = max(1, min(4, max_count))
    return {
        "enabled": _as_bool(raw.get("enabled"), DEFAULT_OPTIONS["enabled"]),
        "max_count": max_count,
        "auto_fill": _as_bool(raw.get("auto_fill"), DEFAULT_OPTIONS["auto_fill"]),
        "use_world_cover": _as_bool(raw.get("use_world_cover"), DEFAULT_OPTIONS["use_world_cover"]),
    }


def scene_characters(store: ProjectStore, scene: dict, item: dict | None = None) -> list[dict]:
    """按出场顺序解析场景角色（剔除旁白 / 未识别 / 未知 / 重复 ID）。

    参数:
        store: 工程。
        scene: 场景 dict。
        item: 关键图或视频条目（可选，其 `character_ids` 优先于场景）。

    返回:
        角色 dict 列表，顺序与 `character_ids` 一致；角色表损坏时返回空列表。
    """
    ids = (item or {}).get("character_ids") or (scene or {}).get("character_ids") or []
    if not isinstance(ids, (list, tuple)):
        return []
    try:
        table = store.load_characters()
    except Exception:  # characters.json 缺失或损坏时按「没有角色」处理
        return []
    by_id = {}
    for character in table.get("characters") or []:
        if isinstance(character, dict) and character.get("id"):
            by_id[character["id"]] = character

    result: list[dict] = []
    seen: set[str] = set()
    for character_id in ids:
        if not character_id or character_id in (NARRATOR_ID, UNKNOWN_SPEAKER_ID) or character_id in seen:
            continue
        character = by_id.get(character_id)
        if character is None:
            continue
        seen.add(character_id)
        result.append(character)
    return result


def _project_path(store: ProjectStore, rel: str) -> Path:
    """把工程内相对路径转成绝对路径（绝对路径原样返回）。

    参数:
        store: 工程。
        rel: 工程内相对路径或绝对路径。

    返回:
        绝对路径对象。
    """
    path = Path(str(rel)).expanduser()
    return path if path.is_absolute() else (store.root / path)


def _chapter_world_ids(store: ProjectStore, chapter: dict) -> list[str]:
    """取章节绑定的世界 ID：章节字段优先，其次设定库大纲章节，最后回落全部世界。

    参数:
        store: 工程。
        chapter: 章节 dict。

    返回:
        世界 ID 列表，用于按顺序寻找带封面的世界。
    """
    bound = [str(value) for value in (chapter.get("world_ids") or []) if value]
    if bound:
        return bound
    try:
        settings_store = SettingsStore(store.root)
        bible = settings_store.load_or_none() or {}
        entry = settings_store.find_outline_chapter(bible, chapter.get("id"))
        bound = [str(value) for value in ((entry or {}).get("world_ids") or []) if value]
        if bound:
            return bound
    except Exception:  # 设定库缺失/损坏时继续尝试全部世界
        pass
    try:
        world_store = WorldStore(store.root)
        if world_store.exists():
            return [str(world.get("id")) for world in world_store.list_worlds() if world.get("id")]
    except Exception:
        pass
    return []


def _pick_world_cover(store: ProjectStore, chapter: dict, scene: dict) -> tuple[dict, str] | None:
    """按「场景世界 → 章节绑定世界 → 全部世界」顺序找第一个带真实封面文件的世界。

    参数:
        store: 工程。
        chapter: 章节 dict。
        scene: 场景 dict。

    返回:
        `(世界条目, 封面相对路径)`；没有可用封面时返回 None。
    """
    candidates = [scene.get("world_id")] + _chapter_world_ids(store, chapter or {})
    world_store = WorldStore(store.root)
    if not world_store.exists():
        return None
    for world_id in candidates:
        if not world_id:
            continue
        try:
            world = world_store.get(world_id)
        except Exception:  # worlds.json 损坏等异常不影响关键图流程
            continue
        if not world:
            continue
        rel = world.get("cover_asset")
        if rel and _project_path(store, rel).is_file():
            return world, str(rel)
    return None


def collect_references(store: ProjectStore, config, chapter: dict, scene: dict, item: dict | None = None) -> dict:
    """汇总某个关键图条目可用的参考图（世界封面在前，角色按出场顺序）。

    参数:
        store: 工程。
        config: AppConfig 或配置字典（读 `comfyui.references`）。
        chapter: 章节 dict。
        scene: 场景 dict。
        item: 关键图或视频条目（可选，其 `character_ids` 优先）。

    返回:
        `{"items": [{kind,id,name,rel,path}], "warnings": [中文提示], "missing": [{id,name}]}`；
        总开关关闭或全部缺图时各列表为空。
    """
    options = reference_options(config)
    empty = {"items": [], "warnings": [], "missing": []}
    if not options["enabled"]:
        return empty

    items: list[dict] = []
    warnings: list[str] = []
    missing: list[dict] = []
    seen: set[str] = set()

    # 世界封面：仅取一个（场景世界优先，其次章节绑定的世界）
    if options["use_world_cover"]:
        try:
            picked = _pick_world_cover(store, chapter or {}, scene or {})
        except Exception:  # 世界数据异常只跳过世界参考图
            picked = None
        if picked:
            world, rel = picked
            path = _project_path(store, rel)
            pitch = str(path)
            if pitch not in seen:
                seen.add(pitch)
                items.append(
                    {
                        "kind": "world",
                        "id": world.get("id") or "",
                        "name": f"世界·{world.get('name') or '世界'}",
                        "rel": rel,
                        "path": pitch,
                    }
                )

    # 角色设定图：fullbody → closeup → portrait_asset
    try:
        characters = scene_characters(store, scene or {}, item)
    except Exception:
        characters = []
    for character in characters:
        character_id = str(character.get("id") or "")
        name = str(character.get("name") or character_id)
        assets = character.get("image_assets")
        assets = assets if isinstance(assets, dict) else {}
        candidates = [assets.get(key) for key in _CHARACTER_ASSET_KEYS]
        candidates.append(character.get("portrait_asset"))

        found = None
        for candidate in candidates:
            if not candidate:
                continue
            path = _project_path(store, candidate)
            if path.is_file():
                found = (str(candidate), path)
                break
        if found is None:
            missing.append({"id": character_id, "name": name})
            warnings.append(f"角色「{name}」缺少设定图（全身像/头像），本次不参与参考")
            continue
        rel, path = found
        absolute = str(path)
        if absolute in seen:
            continue
        seen.add(absolute)
        items.append(
            {
                "kind": "character",
                "id": character_id,
                "name": name,
                "rel": rel,
                "path": absolute,
            }
        )

    return {"items": items[: options["max_count"]], "warnings": warnings, "missing": missing}


def slot_hint(items: list[dict]) -> str:
    """生成提示词里的参考图槽位绑定说明。

    参数:
        items: `collect_references` 返回的参考图条目列表。

    返回:
        中文绑定说明；items 为空时返回空字符串。
    """
    if not items:
        return ""
    parts = []
    for index, item in enumerate(items, 1):
        if item.get("kind") == "world":
            parts.append(f"参考图{index}=世界场景（世界观与场景风格参考）")
        else:
            parts.append(f"参考图{index}={item.get('name') or item.get('id') or ''}")
    return (
        "人物一致性参考：" + "；".join(parts) + "。"
        "画面中同一人物必须与对应参考图完全一致（脸型、发型、服装、画风），不要串脸。"
    )


def appearance_digest(characters: list[dict], limit: int = 120) -> str:
    """把角色外貌提示词拼成一段截断后的摘要（供视频提示词复用）。

    参数:
        characters: 角色 dict 列表。
        limit: 单个角色外貌保留的最大字符数。

    返回:
        `角色A（外貌…）；角色B（…）`；无可用外貌时返回空字符串。
    """
    try:
        max_chars = max(1, int(limit))
    except (TypeError, ValueError):
        max_chars = 120
    parts = []
    for character in characters or []:
        if not isinstance(character, dict):
            continue
        appearance = str(character.get("appearance_prompt") or "").strip()
        if not appearance:
            continue
        name = str(character.get("name") or character.get("id") or "角色")
        parts.append(f"{name}（{appearance[:max_chars]}）")
    return "；".join(parts)
