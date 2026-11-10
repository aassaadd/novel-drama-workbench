"""世界封面与角色设定图生成。

复用 `media/generate.py` 的 `render_image`（离线 Pillow 占位渲染 / 后续可替换为真实模型），
产物统一落 `assets/image/worlds/<wld>.png` 与 `assets/image/characters/<chr>.<kind>.png`，
并把工程内相对路径写回世界条目（cover_asset）与角色（image_assets）。
"""

from __future__ import annotations

from pathlib import Path

from ..config import AppConfig
from ..models.ids import now_iso
from ..store import ProjectStore
from ..store.settings_store import SettingsStore
from ..store.world_store import WorldStore
from ..writing.service import build_art_prompt
from .generate import render_image


def _style(store: ProjectStore, visual_style: str) -> dict:
    """拼装配图风格参数（全局视觉风格 + 工程输出尺寸）。

    参数:
        store: 工程。
        visual_style: 世界或全书统一画面风格。

    返回:
        {"visual_style", "width", "height"}；尺寸取工程 output 默认，缺失时 1024。
    """
    output = store.settings().get("output") or {}
    try:
        width = int(output.get("image_width") or 1024)
        height = int(output.get("image_height") or 1024)
    except (TypeError, ValueError):
        width, height = 1024, 1024
    return {"visual_style": str(visual_style or "").strip(), "width": width, "height": height}


def _render(prompt_row: dict, target: Path, label: str) -> dict:
    """调用 `render_image` 渲染并确保文件真实落盘。

    参数:
        prompt_row: build_art_prompt 的返回值。
        target: 输出 PNG 绝对路径。
        label: 中文产物名，用于错误提示。

    返回:
        render_image 的返回 dict（path/width/height/sha256/prompt）。

    异常:
        RuntimeError: Pillow 不可用、写盘失败或未产出文件。
    """
    item = {
        "id": target.stem,
        "prompt": prompt_row["prompt"],
        "negative_prompt": prompt_row["negative_prompt"],
        "width": prompt_row["width"],
        "height": prompt_row["height"],
    }
    try:
        result = render_image(item, target)
    except RuntimeError as exc:
        raise RuntimeError(f"{label}生成失败：{exc}") from exc
    except OSError as exc:
        raise RuntimeError(f"{label}写盘失败：{exc}") from exc
    if not target.exists():
        raise RuntimeError(f"{label}生成失败：渲染未产出文件 {target}")
    return result


def render_world_cover(store: ProjectStore, config: AppConfig, world_id: str, force: bool = False) -> dict:
    """生成一张世界封面并写回 cover_asset（幂等）。

    参数:
        store: 工程。
        config: 本机配置（保留给后续真实生图路由使用）。
        world_id: 世界 ID。
        force: True 时强制重绘。

    返回:
        {"world_id", "cover_asset", "created", "width"?, "height"?}；
        已存在且未 force 时 created=False，直接复用旧路径。

    异常:
        ValueError: 世界条目不存在。
        RuntimeError: 渲染不可用或写盘失败。
    """
    world_store = WorldStore(store.root)
    world_store.ensure()
    world = world_store.get(world_id)
    if world is None:
        raise ValueError(f"世界条目不存在: {world_id}")

    rel_path = f"assets/image/worlds/{world_id}.png"
    if not force:
        for candidate in (world.get("cover_asset"), rel_path):
            if candidate and (store.root / str(candidate)).exists():
                if world.get("cover_asset") != candidate:
                    world_store.update(world_id, {"cover_asset": candidate})
                return {"world_id": world_id, "cover_asset": candidate, "created": False}

    bible = SettingsStore(store.root).load_or_none() or {}
    visual = str(world.get("visual_style") or "").strip() or str((bible.get("style") or {}).get("visual_style") or "").strip()
    prompt_row = build_art_prompt("world", world, _style(store, visual))
    target = store.root / rel_path
    result = _render(prompt_row, target, "世界封面")
    world_store.update(world_id, {"cover_asset": rel_path})
    return {"world_id": world_id, "cover_asset": rel_path, "created": True, "width": result["width"], "height": result["height"]}


def render_character_sheet(store: ProjectStore, config: AppConfig, character_id: str, kind: str, force: bool = False) -> dict:
    """生成角色特写 / 三视图并写回 image_assets（幂等）。

    参数:
        store: 工程。
        config: 本机配置（保留给后续真实生图路由使用）。
        character_id: 角色 ID。
        kind: closeup（脸部特写）| turnaround（三视图）。
        force: True 时强制重绘。

    返回:
        {"character_id", "kind", "image_asset", "created", "width"?, "height"?}。

    异常:
        ValueError: kind 非法、角色不存在或缺少外貌提示词。
        RuntimeError: 渲染不可用或写盘失败。
    """
    if kind not in ("closeup", "turnaround"):
        raise ValueError(f"不支持的角色设定图类型: {kind}（可选 closeup / turnaround）")
    table = store.load_characters()
    character = next((item for item in table.get("characters") or [] if item.get("id") == character_id), None)
    if character is None:
        raise ValueError(f"角色不存在: {character_id}")
    appearance = str(character.get("appearance_prompt") or "").strip()
    if not appearance:
        raise ValueError("角色缺少外貌提示词，无法生成设定图")

    rel_path = f"assets/image/characters/{character_id}.{kind}.png"
    if not force:
        existing = (character.get("image_assets") or {}).get(kind)
        for candidate in (existing, rel_path):
            if candidate and (store.root / str(candidate)).exists():
                if existing != candidate:
                    character["image_assets"] = {**(character.get("image_assets") or {}), kind: candidate}
                    character["updated_at"] = now_iso()
                    store.save_characters(table)
                return {"character_id": character_id, "kind": kind, "image_asset": candidate, "created": False}

    bible = SettingsStore(store.root).load_or_none() or {}
    visual = str((bible.get("style") or {}).get("visual_style") or "").strip()
    prompt_row = build_art_prompt(kind, character, _style(store, visual))
    target = store.root / rel_path
    result = _render(prompt_row, target, "角色设定图")

    character["image_assets"] = {**(character.get("image_assets") or {}), kind: rel_path}
    character["updated_at"] = now_iso()
    store.save_characters(table)
    return {
        "character_id": character_id,
        "kind": kind,
        "image_asset": rel_path,
        "created": True,
        "width": result["width"],
        "height": result["height"],
    }
