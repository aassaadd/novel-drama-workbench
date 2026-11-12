"""用 ComfyUI 模板渲染单张图片（世界封面 / 角色设定图）。

与 `media/runner.py` 共用同一套模板、画幅解析、GPU 锁与取回逻辑，
避免配图链路继续走离线 Pillow 占位渲染。

两种出图方式：
- 文生图（正片模板）：世界封面、角色全身像母图；
- 参考图（`image_ref` 模板）：头像 / 三视图都以母图为参考，保证同一张脸与同一风格。
"""

from __future__ import annotations

import shutil
from pathlib import Path

from ..config import AppConfig
from ..store import ProjectStore
from .comfy_media import build_prompt, load_manifest, resolve_aspect, resolve_template, seed_for, templates_dir
from .runner import _check_models, _comfy_gateway, _provider, _release_comfy, _submit_and_fetch


def render_image_via_comfy(
    store: ProjectStore,
    config: AppConfig,
    prompt_row: dict,
    target: Path,
    item_id: str,
    *,
    reference: Path | None = None,
    aspect_ratio: str | None = None,
    seed: int | None = None,
) -> dict:
    """按工程画幅用 ComfyUI 渲染一张图并落到指定路径。

    参数:
        store: 工程（取 `settings.output.aspect_ratio`）。
        config: 本机配置（ComfyUI 地址、模板 ID、GPU 锁）。
        prompt_row: `build_art_prompt` 的返回值（prompt / negative_prompt）。
        target: 最终产物绝对路径（如 `assets/image/characters/chr_x.closeup.png`）。
        item_id: 稳定 ID（用于 seed 与产物临时命名）。
        reference: 参考图绝对路径；给定时改用 `image_ref` 模板做参考图生图。
        aspect_ratio: 强制画幅（如三视图用 `3:1`）；为空时按工程画幅。
        seed: 指定 seed；为空时按「条目 ID + revision」派生。

    返回:
        `{"path", "width", "height", "template_id", "job_id"}`。

    异常:
        RuntimeError: ComfyUI 不可达、模型缺失、渲染失败或未产出文件。
    """
    if _provider(config, "image") != "comfyui":
        raise RuntimeError("providers.image 未设为 comfyui，无法走真实生图")

    gateway = _comfy_gateway(config)
    status = gateway.health()
    if not status.ok:
        raise RuntimeError(f"ComfyUI 未连接：{status.detail or '检查是否启动、地址是否正确'}")

    settings = store.settings() or {}
    manifest = load_manifest(templates_dir(config))
    aspect = resolve_aspect(settings, "image", manifest=manifest)
    if reference is not None:
        template_key = "comfyui.templates.image_ref"
        kind = "image_ref"
    else:
        template_key = "comfyui.templates.image"
        kind = "image"
    template_id = config.get(template_key)
    resolved = resolve_template(str(template_id or ""), kind, manifest)
    _check_models(config, resolved["template"], resolved["template_id"])

    revision = 1
    actual_seed = int(seed) if seed is not None else seed_for(item_id, revision)
    input_image = None
    if reference is not None:
        if not reference.is_file():
            raise RuntimeError(f"参考图不存在：{reference}（先重新生成全身像母图）")
        uploaded = gateway.upload_image(reference, subfolder="ndw")
        name = str(uploaded.get("name") or reference.name)
        subfolder = str(uploaded.get("subfolder") or "").strip("/")
        input_image = f"{subfolder}/{name}" if subfolder and not name.startswith(f"{subfolder}/") else name

    payload = build_prompt(
        config,
        resolved["template_id"],
        kind,
        prompt=str(prompt_row.get("prompt") or ""),
        negative_prompt=str(prompt_row.get("negative_prompt") or ""),
        aspect_ratio=str(aspect_ratio or aspect["ratio"]),
        item_id=item_id,
        revision=revision,
        input_image=input_image,
        seed=actual_seed,
    )

    # 先落到目标同目录的临时产物，再改名成调用方要求的文件名
    target.parent.mkdir(parents=True, exist_ok=True)
    produced, prompt_id = _submit_and_fetch(
        gateway, config, "image", payload["prompt"], item_id, revision, target.parent
    )
    if produced != target:
        shutil.move(str(produced), str(target))
    if not target.exists():
        raise RuntimeError(f"ComfyUI 渲染未产出图片：{target}")
    # 单张设定图生成结束即释放模型与内存，避免常驻占用；失败只记日志
    _release_comfy(gateway, config, f"art-{item_id}")
    return {
        "path": target,
        "width": int(payload["width"]),
        "height": int(payload["height"]),
        "template_id": payload["template_id"],
        "job_id": f"job_{prompt_id.replace('-', '')}",
    }
