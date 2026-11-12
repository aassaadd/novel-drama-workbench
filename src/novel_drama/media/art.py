"""世界封面与角色设定图生成。

角色设定图分三档：
- `fullbody`（全身像）→ 母图，走文生图模板；
- `closeup`（头像）/ `turnaround`（三视图）→ 都以全身像为参考图生成，
  三者共用同一 seed 与同一风格词，保证「同一张脸、同一套风格」。

产物统一落 `assets/image/worlds/<wld>.png` 与
`assets/image/characters/<chr>.<kind>.png`，并把工程内相对路径写回
世界条目（cover_asset）与角色（image_assets）。
"""

from __future__ import annotations

from pathlib import Path

from ..config import AppConfig
from ..models.ids import now_iso
from ..store import ProjectStore
from ..store.settings_store import SettingsStore
from ..store.world_store import WorldStore
from ..writing.service import build_art_prompt
from .comfy_media import resolve_aspect, seed_for
from .generate import render_image

#: 角色设定图支持的档位（fullbody 是头像与三视图的母图）
CHARACTER_KINDS = ("fullbody", "closeup", "turnaround")


def _style(store: ProjectStore, visual_style: str) -> dict:
    """拼装配图风格参数（全局视觉风格 + 工程画幅尺寸）。

    参数:
        store: 工程。
        visual_style: 世界或全书统一画面风格。

    返回:
        {"visual_style", "width", "height"}；尺寸由工程画幅解析，不再读独立宽高配置。
    """
    settings = store.settings() or {}
    aspect = resolve_aspect(settings, "image")
    return {
        "visual_style": str(visual_style or "").strip(),
        "width": int(aspect["width"]),
        "height": int(aspect["height"]),
    }


def _render(
    store: ProjectStore,
    config: AppConfig,
    prompt_row: dict,
    target: Path,
    label: str,
    item_id: str,
    *,
    reference: Path | None = None,
    aspect_ratio: str | None = None,
    seed: int | None = None,
) -> dict:
    """渲染并确保文件真实落盘：comfyui 走真实模型，其它走离线 Pillow。

    参数:
        store: 工程（取画幅设置）。
        config: 本机配置（决定 provider 与 ComfyUI 地址）。
        prompt_row: build_art_prompt 的返回值。
        target: 输出 PNG 绝对路径。
        label: 中文产物名，用于错误提示。
        item_id: 稳定 ID（ComfyUI 的 seed 与临时产物命名）。
        reference: 参考图绝对路径（头像 / 三视图传母图）。
        aspect_ratio: 强制画幅（三视图传 `3:1`）。
        seed: 指定 seed；为空时按「条目 ID + revision」派生。

    返回:
        渲染结果 dict（path/width/height，ComfyUI 时额外含 job_id）。

    异常:
        RuntimeError: 渲染不可用、未连上 ComfyUI 或写盘失败。
    """
    from .runner import _provider

    if _provider(config, "image") == "comfyui":
        from .comfy_render import render_image_via_comfy

        try:
            return render_image_via_comfy(
                store,
                config,
                prompt_row,
                target,
                item_id,
                reference=reference,
                aspect_ratio=aspect_ratio,
                seed=seed,
            )
        except RuntimeError as exc:
            raise RuntimeError(f"{label}生成失败：{exc}") from exc

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
    result = _render(store, config, prompt_row, target, "世界封面", world_id)
    world_store.update(world_id, {"cover_asset": rel_path})
    return {"world_id": world_id, "cover_asset": rel_path, "created": True, "width": result["width"], "height": result["height"]}


def _fullbody_asset(store: ProjectStore, config: AppConfig, character: dict, character_id: str, visual: str) -> str:
    """确保角色全身像（母图）存在，返回其在工程内的相对路径。

    母图是头像与三视图的参考图来源：不存在时先按文生图模板生成一张，
    并立刻写回角色的 image_assets.fullbody。

    参数:
        store: 工程。
        config: 本机配置。
        character: 角色字典（就地更新 image_assets）。
        character_id: 角色 ID。
        visual: 全局视觉风格词。

    返回:
        母图相对路径（`assets/image/characters/<chr>.fullbody.png`）。

    异常:
        RuntimeError: 母图生成失败。
    """
    rel_path = f"assets/image/characters/{character_id}.fullbody.png"
    existing = (character.get("image_assets") or {}).get("fullbody")
    for candidate in (existing, rel_path):
        if candidate and (store.root / str(candidate)).exists():
            if existing != candidate:
                character["image_assets"] = {**(character.get("image_assets") or {}), "fullbody": candidate}
            return candidate

    prompt_row = build_art_prompt("fullbody", character, _style(store, visual))
    target = store.root / rel_path
    _render(
        store,
        config,
        prompt_row,
        target,
        "角色全身像（母图）",
        character_id,
        seed=seed_for(character_id, 1),
    )
    character["image_assets"] = {**(character.get("image_assets") or {}), "fullbody": rel_path}
    return rel_path


def render_character_sheet(store: ProjectStore, config: AppConfig, character_id: str, kind: str, force: bool = False) -> dict:
    """生成角色设定图（全身像 / 头像 / 三视图）并写回 image_assets（幂等）。

    参数的 kind 三档：
    - fullbody：全身像母图，文生图；
    - closeup / turnaround：以全身像为参考图生成（母图缺失时先自动补一张），
      与母图共用 seed 与风格词，保证同一张脸、同一套风格。

    参数:
        store: 工程。
        config: 本机配置（决定 provider 与 ComfyUI 地址）。
        character_id: 角色 ID。
        kind: fullbody（全身像）| closeup（头像）| turnaround（三视图）。
        force: True 时强制重绘（母图已存在则继续复用）。

    返回:
        {"character_id", "kind", "image_asset", "created", "width"?, "height"?, "base_asset"?}。

    异常:
        ValueError: kind 非法、角色不存在或缺少外貌提示词。
        RuntimeError: 渲染不可用或写盘失败。
    """
    if kind not in CHARACTER_KINDS:
        raise ValueError(f"不支持的角色设定图类型: {kind}（可选 {' / '.join(CHARACTER_KINDS)}）")
    table = store.load_characters()
    character = next((item for item in table.get("characters") or [] if item.get("id") == character_id), None)
    if character is None:
        raise ValueError(f"角色不存在: {character_id}")
    appearance = str(character.get("appearance_prompt") or "").strip()
    if not appearance:
        raise ValueError("角色缺少外貌提示词，无法生成设定图：请先在人物卡片填写「外貌提示词」并点保存")

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

    base_rel = None
    reference = None
    if kind in ("closeup", "turnaround"):
        base_rel = _fullbody_asset(store, config, character, character_id, visual)
        reference = store.root / base_rel
        # 母图可能是刚补生成的，先落盘角色表，避免后续失败丢掉母图登记
        character["updated_at"] = now_iso()
        store.save_characters(table)

    prompt_row = build_art_prompt(kind, character, _style(store, visual))
    target = store.root / rel_path
    result = _render(
        store,
        config,
        prompt_row,
        target,
        "角色设定图",
        f"{character_id}.{kind}",
        reference=reference,
        aspect_ratio=None,
        seed=seed_for(character_id, 1),
    )

    character["image_assets"] = {**(character.get("image_assets") or {}), kind: rel_path}
    character["updated_at"] = now_iso()
    store.save_characters(table)
    result_payload = {
        "character_id": character_id,
        "kind": kind,
        "image_asset": rel_path,
        "created": True,
        "width": result["width"],
        "height": result["height"],
    }
    if base_rel:
        result_payload["base_asset"] = base_rel
    return result_payload

