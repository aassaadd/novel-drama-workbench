"""章节媒体批量执行：音效 / 关键图 / 短视频位。

逐条执行、逐条回写状态：单条失败不中断整批，失败信息写进 `asset.error`，
供 `ndw media` 与 Web 页面直接展示。产物统一落 `assets/{sfx,images,video}`。

生成路由（Task 7.1）：
- `providers.image / providers.video = comfyui` 时走本机 ComfyUI 真实生成：
  模板填参 → 提交 → 轮询 → 取回产物，整段在 GPU 锁内执行；
- 其它 / 缺省走 `media/generate.py` 的离线占位逻辑（行为与本轮之前完全一致）；
- ComfyUI 失败只写 `asset.status=failed` + 中文错误，**不静默回退离线占位图**。
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

from ..adapters.base import AdapterError, ErrorCode
from ..adapters.comfyui import LocalComfyGateway
from ..config import AppConfig
from ..models.ids import now_iso
from ..orchestration.gpu_queue import GpuLock
from ..store import ProjectStore
from . import comfy_models
from .comfy_media import (
    build_prompt,
    load_manifest,
    manifest_entries,
    resolve_aspect,
    resolve_template,
    seed_for,
    templates_dir,
)
from .generate import probe_duration_ms, render_image, render_sfx, render_video
from .references import appearance_digest, collect_references, reference_options, scene_characters, slot_hint
from .styles import with_style


def _visual_style(store: ProjectStore) -> str:
    """读取设定库里的统一画面风格词（图片与视频共用同一份）。

    参数:
        store: 工程。

    返回:
        风格词文本；未设置时返回空字符串。
    """
    from ..store.settings_store import SettingsStore

    bible = SettingsStore(store.root).load_or_none() or {}
    return str((bible.get("style") or {}).get("visual_style") or "").strip()

# 每类媒体的产物目录与扩展名
MEDIA_PATHS = {"sfx": ("assets/sfx", ".wav"), "image": ("assets/images", ".png"), "video": ("assets/video", ".mp4")}

#: ComfyUI 产物类型 → 默认扩展名
_FALLBACK_EXT = {"image": ".png", "video": ".mp4"}


def _iter_items(chapter: dict, kind: str):
    """遍历章节内某类媒体条目。

    参数:
        chapter: 章节 dict。
        kind: sfx/image/video。

    产出:
        (scene, item) 二元组。
    """
    key = {"sfx": "sfx_events", "image": "key_images", "video": "video_segments"}[kind]
    for scene in chapter.get("scenes") or []:
        for item in scene.get(key) or []:
            yield scene, item


def _rel(store: ProjectStore, path: Path) -> str:
    """把绝对路径转成工程内相对路径。

    参数:
        store: 工程。
        path: 绝对路径。

    返回:
        相对路径字符串（不在工程内时返回绝对路径）。
    """
    try:
        return str(path.relative_to(store.root))
    except ValueError:
        return str(path)


def _source_image(store: ProjectStore, chapter: dict, segment: dict) -> Path | None:
    """查找视频位的首帧关键图路径。

    参数:
        store: 工程。
        chapter: 章节。
        segment: 视频段。

    返回:
        已就绪关键图路径；没有则 None。
    """
    source_id = segment.get("source_image_id")
    if not source_id:
        return None
    for _, item in _iter_items(chapter, "image"):
        if item.get("id") != source_id:
            continue
        asset = item.get("asset") or {}
        asset_id = asset.get("asset_id")
        if asset.get("status") == "ready" and asset_id:
            candidate = store.root / asset_id
            if candidate.exists():
                return candidate
    return None


def _provider(config: AppConfig, kind: str) -> str:
    """读取某类媒体的生成提供方。

    参数:
        config: 本机配置。
        kind: image / video。

    返回:
        `comfyui` 或 `offline` 等配置值（缺省 offline）。
    """
    value = config.get(f"providers.{kind}")
    text = str(value).strip() if value else ""
    return text or "offline"


def _comfy_gateway(config: AppConfig) -> LocalComfyGateway:
    """按配置装配本机 ComfyUI 网关。

    参数:
        config: 本机配置。

    返回:
        LocalComfyGateway。
    """
    return LocalComfyGateway(config.get("comfyui", {}) or {}, templates_dir(config))


def _next_revision(item: dict) -> int:
    """计算本次生成使用的 revision（失败重试/重跑会 +1）。

    参数:
        item: 关键图或视频条目。

    返回:
        本次 revision（首次为条目原值）。
    """
    revision = int(item.get("revision") or 1)
    asset = item.get("asset") or {}
    if asset.get("status") in ("ready", "failed", "running", "queued") or asset.get("asset_id") or asset.get("job_id"):
        revision += 1
    return max(1, revision)


def _existing_asset(store: ProjectStore, item: dict, kind: str) -> Path | None:
    """判断条目是否已有可直接跳过的就绪产物。

    参数:
        store: 工程。
        item: 媒体条目。
        kind: image / video。

    返回:
        存在的产物路径；未就绪或文件缺失时返回 None。
    """
    asset = item.get("asset") or {}
    if asset.get("status") != "ready":
        return None
    candidates: list[Path] = []
    if asset.get("asset_id"):
        candidates.append(store.root / str(asset["asset_id"]))
    rel_dir, ext = MEDIA_PATHS[kind]
    candidates.append(store.root / f"{rel_dir}/{item['id']}{ext}")
    for path in candidates:
        if path.exists():
            return path
    return None


def _job_timeout_s(config: AppConfig, kind: str) -> float:
    """读取单条任务的轮询总超时（秒）。

    参数:
        config: 本机配置。
        kind: image / video。

    返回:
        超时秒数；优先 `comfyui.<kind>_job_timeout_seconds`，否则用 `comfyui.job_timeout_ms`。
    """
    key = "comfyui.video_job_timeout_seconds" if kind == "video" else "comfyui.image_job_timeout_seconds"
    try:
        value = config.get(key)
        if value not in (None, ""):
            return max(1.0, float(value))
    except (TypeError, ValueError):
        pass
    try:
        return max(1.0, float(config.get("comfyui.job_timeout_ms", 1800000)) / 1000)
    except (TypeError, ValueError):
        return 1800.0


def _wait_for_job(gateway: LocalComfyGateway, config: AppConfig, prompt_id: str, timeout_s: float, kind: str) -> None:
    """轮询任务直到 success；error/超时抛 AdapterError。

    参数:
        gateway: ComfyUI 网关。
        config: 本机配置（读 `comfyui.poll_interval_ms`）。
        prompt_id: ComfyUI 任务 ID。
        timeout_s: 总超时秒数。
        kind: image / video（只用于超时提示里的配置键名）。

    异常:
        AdapterError: 任务失败（UPSTREAM_ERROR）或超时（TIMEOUT）。
    """
    try:
        interval = max(0.2, float(config.get("comfyui.poll_interval_ms", 1000)) / 1000)
    except (TypeError, ValueError):
        interval = 1.0
    deadline = time.monotonic() + timeout_s
    while True:
        status = gateway.status(prompt_id)
        if status.state == "success":
            return
        if status.state == "error":
            raise AdapterError(
                ErrorCode.UPSTREAM_ERROR,
                status.message or "ComfyUI 执行失败（无详细节点信息）",
                detail=status.error or {},
            )
        if status.state == "canceled":
            raise AdapterError(ErrorCode.UPSTREAM_ERROR, "ComfyUI 任务已取消")
        if time.monotonic() >= deadline:
            raise AdapterError(
                ErrorCode.TIMEOUT,
                f"等待 ComfyUI 任务超时（{timeout_s:.0f}s）：{prompt_id}；可在配置里调大 "
                f"comfyui.{kind}_job_timeout_seconds",
                retryable=True,
            )
        time.sleep(interval)


def _submit_and_fetch(
    gateway: LocalComfyGateway,
    config: AppConfig,
    kind: str,
    prompt: dict,
    item_id: str,
    revision: int,
    out_dir: Path,
) -> tuple[Path, str]:
    """在 GPU 锁内完成「提交 → 轮询 → 取回」，返回最终产物路径与 prompt_id。

    参数:
        gateway: ComfyUI 网关。
        config: 本机配置。
        kind: image / video。
        prompt: 已填参的 API 格式工作流。
        item_id: 条目 ID。
        revision: 条目版本号。
        out_dir: 最终产物目录（`assets/images` 或 `assets/video`）。

    返回:
        `(产物路径, ComfyUI prompt_id)`。

    异常:
        AdapterError: 锁超时、任务失败、超时、产物缺失。
    """
    timeout_s = _job_timeout_s(config, kind)
    lock = GpuLock(
        config.get("gpu.lock_path", "/Volumes/other/gpu.lock"),
        poll_interval_s=float(config.get("gpu.poll_interval_s", 2) or 2),
        timeout_s=timeout_s,
    )
    temp_dir = out_dir / f".ndw-tmp-{item_id}-r{revision}"
    with lock.hold(f"comfy-{item_id}-r{revision}"):
        submitted = gateway.submit_prompt(prompt, client_id=f"ndw-{item_id}")
        prompt_id = submitted.prompt_id
        _wait_for_job(gateway, config, prompt_id, timeout_s, kind)
        try:
            artifacts = gateway.fetch(prompt_id, str(temp_dir))
            artifact = next((item for item in artifacts if item.kind == kind), None)
            if artifact is None or not artifact.local_path:
                names = "、".join(item.filename for item in artifacts) or "无"
                raise AdapterError(
                    ErrorCode.UPSTREAM_ERROR,
                    f"ComfyUI 未返回{('图片' if kind == 'image' else '视频')}产物（实际：{names}）",
                )
            ext = Path(artifact.filename).suffix.lower() or _FALLBACK_EXT[kind]
            target = out_dir / f"{item_id}_r{revision}{ext}"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(artifact.local_path, target)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
    return target, prompt_id


def _check_models(config: AppConfig, entry: dict, template_id: str) -> None:
    """检查模板依赖模型是否就位。

    参数:
        config: 本机配置。
        entry: 清单登记。
        template_id: 模板 ID（用于报错）。

    异常:
        AdapterError(INVALID_INPUT): 有模型文件缺失。
    """
    keys = [str(item) for item in (entry.get("models") or [])]
    if not keys:
        return
    try:
        missing = comfy_models.missing_for_template(config, keys)
    except ValueError as exc:
        raise AdapterError(ErrorCode.INVALID_INPUT, f"无法定位 ComfyUI 模型目录：{exc}") from exc
    if missing:
        raise AdapterError(
            ErrorCode.INVALID_INPUT,
            f"模板 {template_id} 缺少依赖模型文件：{', '.join(missing)}；可在设置页模型清单一键下载",
        )


def _auto_fill_characters(
    store: ProjectStore,
    config: AppConfig,
    missing: list[dict],
    limit: int = 3,
) -> tuple[int, list[str]]:
    """为缺设定图的角色自动补全身像（单次最多 limit 个）。

    参数:
        store: 工程。
        config: 本机配置。
        missing: `collect_references` 返回的缺图角色列表 `[{"id","name"}]`。
        limit: 单次最多补全的角色数量。

    返回:
        `(成功补全数量, 中文告警列表)`；成功的角色其 `image_assets.fullbody` 会写回角色表。
    """
    from .art import _fullbody_asset

    if not missing:
        return 0, []
    try:
        count = max(0, int(limit))
    except (TypeError, ValueError):
        count = 3
    table = store.load_characters()
    by_id = {}
    for character in table.get("characters") or []:
        if isinstance(character, dict) and character.get("id"):
            by_id[character["id"]] = character

    visual = _visual_style(store)
    filled = 0
    warnings: list[str] = []
    for entry in missing[:count]:
        character_id = str(entry.get("id") or "")
        name = str(entry.get("name") or character_id)
        character = by_id.get(character_id)
        if character is None:
            warnings.append(f"角色「{name}」不在角色表中，跳过自动补全身像")
            continue
        try:
            _fullbody_asset(store, config, character, character_id, visual)
        except Exception as exc:  # 单个角色失败不影响其它角色与整条生成
            warnings.append(f"角色「{name}」自动补全身像失败：{exc}")
            continue
        filled += 1
    if filled:
        store.save_characters(table)
    return filled, warnings


def _image_template_id(config: AppConfig, manifest: dict, explicit, has_refs: bool) -> tuple[str, str]:
    """选出关键图要用的模板 ID 与用途类型。

    参数:
        config: 本机配置（读 `comfyui.templates.image` / `comfyui.templates.image_ref`）。
        manifest: 模板清单。
        explicit: 条目自带的 `workflow_template_id`（可为空）。
        has_refs: 本次是否有参考图。

    返回:
        `(模板 ID, 用途类型)`；用途类型为 `image_ref`（有参考图）或 `image`。
        只有 explicit 在清单里且其 kind 与目标类型一致时才采用 explicit。
    """
    kind = "image_ref" if has_refs else "image"
    key = "comfyui.templates.image_ref" if has_refs else "comfyui.templates.image"
    if explicit:
        wanted = str(explicit)
        for entry in manifest_entries(manifest or {}):
            if str(entry.get("id") or entry.get("template_id")) != wanted:
                continue
            if str(entry.get("kind") or "") == kind:
                return wanted, kind
            break
    return str(config.get(key) or ""), kind


def _generate_image(
    store: ProjectStore,
    config: AppConfig,
    gateway: LocalComfyGateway,
    chapter: dict,
    scene: dict,
    item: dict,
    revision: int,
    settings: dict,
    out_dir: Path,
) -> dict:
    """用 ComfyUI 生成一张关键图并回写条目与产物信息。

    参数:
        store: 工程。
        config: 本机配置。
        gateway: ComfyUI 网关。
        chapter: 所在章节（解析世界封面）。
        scene: 所在场景（解析出场角色设定图）。
        item: 关键图条目。
        revision: 本次版本号。
        settings: 工程设置（画幅回落用）。
        out_dir: 产物目录。

    返回:
        `{"path","asset_id","job_id","width","height","template_id","aspect",
        "warning","references","reference_warnings"}`。
    """
    # 参考图：世界封面 + 出场角色设定图；按配置自动补全身像
    options = reference_options(config)
    refs = collect_references(store, config, chapter, scene, item)
    ref_warnings = list(refs.get("warnings") or [])
    if options["auto_fill"] and refs.get("missing"):
        filled, fill_warnings = _auto_fill_characters(store, config, refs["missing"], limit=3)
        ref_warnings.extend(fill_warnings)
        if filled:
            refs = collect_references(store, config, chapter, scene, item)
            # 保留自动补全过程中的告警（如个别角色补全失败），再合并最新缺图告警
            ref_warnings = [*fill_warnings, *(refs.get("warnings") or [])]
    ref_items = refs.get("items") or []

    aspect = resolve_aspect(item, settings, "image")
    item["aspect_ratio"] = aspect["ratio"]
    item["width"], item["height"] = aspect["width"], aspect["height"]
    seed = int(item["seed"]) if item.get("seed") is not None else seed_for(item["id"], revision)
    item["seed"] = seed

    manifest = load_manifest(templates_dir(config))
    template_id, template_kind = _image_template_id(
        config, manifest, item.get("workflow_template_id"), bool(ref_items)
    )
    resolved = resolve_template(template_id, template_kind, manifest)
    _check_models(config, resolved["template"], resolved["template_id"])

    base = str(item.get("prompt") or "")
    if ref_items:
        hint = slot_hint(ref_items)
        if hint:
            base = f"{base}\n{hint}" if base else hint
    prompt = with_style(base, {"style": {"visual_style": _visual_style(store)}})

    # 参考图上传到 ComfyUI input/ndw，按顺序占用模板槽位
    input_images: list[str] = []
    for ref in ref_items:
        path = Path(str(ref.get("path") or ""))
        try:
            uploaded = gateway.upload_image(path, subfolder="ndw")
        except Exception as exc:
            name = ref.get("name") or path.name
            raise AdapterError(
                ErrorCode.UPSTREAM_ERROR,
                f"参考图上传失败（{name}）：{exc}",
            ) from exc
        name = str(uploaded.get("name") or path.name)
        subfolder = str(uploaded.get("subfolder") or "").strip("/")
        input_images.append(f"{subfolder}/{name}" if subfolder and not name.startswith(f"{subfolder}/") else name)

    payload = build_prompt(
        config,
        resolved["template_id"],
        template_kind,
        prompt=prompt,
        negative_prompt=item.get("negative_prompt") or "",
        aspect_ratio=aspect["ratio"],
        item_id=item["id"],
        revision=revision,
        input_images=input_images or None,
        seed=seed,
    )
    warnings = [*ref_warnings]
    if payload["warning"]:
        warnings.append(str(payload["warning"]))
    target, prompt_id = _submit_and_fetch(gateway, config, "image", payload["prompt"], item["id"], revision, out_dir)
    return {
        "path": target,
        "asset_id": _rel(store, target),
        "job_id": f"job_{prompt_id.replace('-', '')}",
        "width": int(payload["width"]),
        "height": int(payload["height"]),
        "template_id": payload["template_id"],
        "aspect": payload["aspect_ratio"],
        "warning": "；".join(value for value in warnings if value) or None,
        "references": [
            {"kind": ref.get("kind"), "id": ref.get("id"), "name": ref.get("name"), "rel": ref.get("rel")}
            for ref in ref_items
        ],
        "reference_warnings": ref_warnings,
    }


def _generate_video(
    store: ProjectStore,
    config: AppConfig,
    gateway: LocalComfyGateway,
    chapter: dict,
    scene: dict,
    item: dict,
    revision: int,
    settings: dict,
    out_dir: Path,
) -> dict:
    """用 ComfyUI 生成一条短视频；有首帧走 i2v，否则降级 t2v。

    参数:
        store: 工程。
        config: 本机配置。
        gateway: ComfyUI 网关。
        chapter: 章节（查找首帧关键图）。
        scene: 所在场景（t2v 时补充出场角色外貌）。
        item: 视频条目。
        revision: 本次版本号。
        settings: 工程设置（画幅回落用）。
        out_dir: 产物目录。

    返回:
        `{"path","asset_id","job_id","width","height","duration_s","template_id",
        "aspect","source_mode","warning","references","reference_warnings"}`。
    """
    source = _source_image(store, chapter, item)
    source_mode = "i2v" if source else "t2v"
    warnings: list[str] = []
    if item.get("mode") == "i2v" and not source:
        warnings.append("首帧关键图未就绪，已降级为文生视频（t2v）")

    aspect = resolve_aspect(item, settings, "video")
    item["aspect_ratio"] = aspect["ratio"]
    item["width"], item["height"] = aspect["width"], aspect["height"]
    seed = int(item["seed"]) if item.get("seed") is not None else seed_for(item["id"], revision)
    item["seed"] = seed
    if aspect.get("note"):
        warnings.append(str(aspect["note"]))

    manifest = load_manifest(templates_dir(config))
    template_key = "comfyui.templates.video_i2v" if source else "comfyui.templates.video_t2v"
    template_id = config.get(template_key)
    resolved = resolve_template(str(template_id or ""), "video_i2v" if source else "video_t2v", manifest)
    _check_models(config, resolved["template"], resolved["template_id"])

    input_image = None
    if source:
        uploaded = gateway.upload_image(source, subfolder="ndw")
        name = str(uploaded.get("name") or source.name)
        subfolder = str(uploaded.get("subfolder") or "").strip("/")
        input_image = f"{subfolder}/{name}" if subfolder and not name.startswith(f"{subfolder}/") else name

    # 无首帧（t2v / 首帧缺失）时把出场角色外貌摘要拼进提示词，降低换脸；
    # 总开关关闭时不做任何一致性增强，保持纯文生视频
    base = str(item.get("prompt") or "")
    if source is None and reference_options(config)["enabled"]:
        digest = appearance_digest(scene_characters(store, scene, item))
        if digest:
            base = f"{base}\n人物：{digest}" if base else f"人物：{digest}"
            warnings.append("无首帧，已补充出场角色外貌摘要（t2v）")

    payload = build_prompt(
        config,
        resolved["template_id"],
        "video_i2v" if source else "video_t2v",
        prompt=with_style(base, {"style": {"visual_style": _visual_style(store)}}),
        negative_prompt=item.get("negative_prompt") or "",
        aspect_ratio=aspect["ratio"],
        item_id=item["id"],
        revision=revision,
        input_image=input_image,
        seed=seed,
    )
    if payload["warning"]:
        warnings.append(str(payload["warning"]))
    target, prompt_id = _submit_and_fetch(gateway, config, "video", payload["prompt"], item["id"], revision, out_dir)
    duration_ms = probe_duration_ms(target, config.get("ffmpeg.probe_binary", "ffprobe"))
    return {
        "path": target,
        "asset_id": _rel(store, target),
        "job_id": f"job_{prompt_id.replace('-', '')}",
        "width": int(payload["width"]),
        "height": int(payload["height"]),
        "duration_s": round(duration_ms / 1000, 3) if duration_ms else 5.0,
        "duration_ms": duration_ms or 5000,
        "template_id": payload["template_id"],
        "aspect": payload["aspect_ratio"],
        "source_mode": source_mode,
        "warning": "；".join(warnings) or None,
        "references": [],
        "reference_warnings": [],
    }


def _run_comfy(
    store: ProjectStore,
    config: AppConfig,
    chapter: dict,
    kind: str,
    force: bool,
    limit: int | None,
    stats: dict,
) -> dict:
    """用真实 ComfyUI 生成一章内的关键图或视频。

    参数:
        store: 工程。
        config: 本机配置。
        chapter: 章节 dict（执行后保存）。
        kind: image / video。
        force: 已就绪项是否重跑。
        limit: 单次最多处理条数。
        stats: 统计结构（就地填充）。

    返回:
        统计结构。
    """
    rel_dir, ext = MEDIA_PATHS[kind]
    out_dir = store.root / rel_dir
    settings = store.settings()
    gateway = _comfy_gateway(config)
    health = gateway.health()
    processed = 0

    for scene, item in _iter_items(chapter, kind):
        stats["total"] += 1
        asset = item.setdefault("asset", {"status": "pending"})
        if _existing_asset(store, item, kind) and not force:
            stats["skipped"] += 1
            continue
        if limit is not None and processed >= limit:
            stats["skipped"] += 1
            continue
        processed += 1
        if not health.ok:
            message = f"ComfyUI 不可用：{health.detail}"
            asset.update({"status": "failed", "error": {"code": ErrorCode.UNAVAILABLE.value, "message": message}, "updated_at": now_iso()})
            stats["failed"] += 1
            stats["items"].append({"id": item["id"], "status": "failed", "path": None, "error": message})
            continue

        revision = _next_revision(item)
        item["revision"] = revision
        started = time.perf_counter()
        try:
            if kind == "image":
                result = _generate_image(store, config, gateway, chapter, scene, item, revision, settings, out_dir)
                asset.update(
                    {
                        "status": "ready",
                        "asset_id": result["asset_id"],
                        "job_id": result["job_id"],
                        "width": result["width"],
                        "height": result["height"],
                        "error": None,
                        "updated_at": now_iso(),
                    }
                )
            else:
                result = _generate_video(store, config, gateway, chapter, scene, item, revision, settings, out_dir)
                asset.update(
                    {
                        "status": "ready",
                        "asset_id": result["asset_id"],
                        "job_id": result["job_id"],
                        "actual_duration_ms": result["duration_ms"],
                        "width": result["width"],
                        "height": result["height"],
                        "duration_s": result["duration_s"],
                        "error": None,
                        "updated_at": now_iso(),
                    }
                )
            stats["ready"] += 1
            stats["items"].append(
                {
                    "id": item["id"],
                    "status": "ready",
                    "path": result["asset_id"],
                    "template_id": result["template_id"],
                    "aspect_ratio": result["aspect"],
                    "width": result["width"],
                    "height": result["height"],
                    "source_mode": result.get("source_mode"),
                    "warning": result.get("warning"),
                    "references": result.get("references") or [],
                    "reference_warnings": result.get("reference_warnings") or [],
                    "elapsed_ms": int((time.perf_counter() - started) * 1000),
                    "error": None,
                }
            )
        except AdapterError as exc:
            asset.update(
                {
                    "status": "failed",
                    "error": {"code": exc.code.value, "message": exc.message},
                    "updated_at": now_iso(),
                }
            )
            stats["failed"] += 1
            stats["items"].append(
                {
                    "id": item["id"],
                    "status": "failed",
                    "path": None,
                    "elapsed_ms": int((time.perf_counter() - started) * 1000),
                    "error": exc.message,
                }
            )
        except Exception as exc:  # 单条失败不影响整批，但必须落盘原因
            message = f"生成异常：{exc}"
            asset.update(
                {
                    "status": "failed",
                    "error": {"code": "RENDER_FAILED", "message": message},
                    "updated_at": now_iso(),
                }
            )
            stats["failed"] += 1
            stats["items"].append(
                {
                    "id": item["id"],
                    "status": "failed",
                    "path": None,
                    "elapsed_ms": int((time.perf_counter() - started) * 1000),
                    "error": message,
                }
            )

    if processed:
        chapter["revision"] = int(chapter.get("revision") or 1) + 1
        chapter["updated_at"] = now_iso()
        store.save_chapter(chapter)
    return stats


def plan_media(store: ProjectStore, config: AppConfig, chapter_id: str, kind: str) -> list[dict]:
    """Dry-run：列出每条媒体将使用的模板、画幅、宽高与 seed，不提交。

    参数:
        store: 工程。
        config: 本机配置。
        chapter_id: 章节 ID。
        kind: image / video。

    返回:
        每条 `{item_id, template_id, aspect_ratio, width, height, seed, source_mode,
        warning, references, reference_warnings}`。

    异常:
        ValueError: kind 非法。
    """
    if kind not in ("image", "video"):
        raise ValueError(f"plan_media 只支持 image/video，收到 {kind}")
    chapter = store.load_chapter(chapter_id)
    settings = store.settings()
    provider = _provider(config, kind)
    manifest = None
    rows: list[dict] = []

    if provider == "comfyui":
        manifest = load_manifest(templates_dir(config))

    for scene, item in _iter_items(chapter, kind):
        revision = _next_revision(item)
        warnings: list[str] = []
        references: list[dict] = []
        reference_warnings: list[str] = []
        if provider != "comfyui":
            rel = item.get("aspect_ratio") or ((settings.get("output") or {}).get("aspect_ratio")) or "1:1"
            rows.append(
                {
                    "item_id": item["id"],
                    "template_id": item.get("workflow_template_id"),
                    "aspect_ratio": rel,
                    "width": item.get("width"),
                    "height": item.get("height"),
                    "seed": item.get("seed"),
                    "source_mode": "offline",
                    "warning": f"providers.{kind} 未设为 comfyui，将走离线占位渲染",
                    "references": [],
                    "reference_warnings": [],
                }
            )
            continue

        if kind == "image":
            refs = collect_references(store, config, chapter, scene, item)
            ref_items = refs.get("items") or []
            reference_warnings = list(refs.get("warnings") or [])
            if reference_options(config)["auto_fill"] and refs.get("missing"):
                reference_warnings.append("缺图角色将在生成时自动补全身像（单次最多 3 个）")
            references = [
                {"kind": ref.get("kind"), "id": ref.get("id"), "name": ref.get("name"), "rel": ref.get("rel")}
                for ref in ref_items
            ]
            template_id, template_kind = _image_template_id(
                config, manifest, item.get("workflow_template_id"), bool(ref_items)
            )
            resolved = resolve_template(template_id, template_kind, manifest)
            source_mode = "comfy"
            aspect = resolve_aspect(item, settings, "image")
        else:
            source = _source_image(store, chapter, item)
            source_mode = "i2v" if source else "t2v"
            if item.get("mode") == "i2v" and not source:
                warnings.append("首帧关键图未就绪，将降级为文生视频（t2v）")
            if source is None and reference_options(config)["enabled"] and appearance_digest(
                scene_characters(store, scene, item)
            ):
                reference_warnings.append("无首帧，将补充出场角色外貌摘要（t2v）")
            key = "comfyui.templates.video_i2v" if source else "comfyui.templates.video_t2v"
            resolved = resolve_template(str(config.get(key) or ""), "video_i2v" if source else "video_t2v", manifest)
            aspect = resolve_aspect(item, settings, "video")

        if resolved.get("warning"):
            warnings.append(str(resolved["warning"]))
        if aspect.get("note"):
            warnings.append(str(aspect["note"]))
        seed = int(item["seed"]) if item.get("seed") is not None else seed_for(item["id"], revision)
        rows.append(
            {
                "item_id": item["id"],
                "template_id": resolved["template_id"],
                "aspect_ratio": aspect["ratio"],
                "width": aspect["width"],
                "height": aspect["height"],
                "seed": seed,
                "source_mode": source_mode,
                "warning": "；".join(warnings) or None,
                "references": references,
                "reference_warnings": reference_warnings,
            }
        )
    return rows


def run_media(
    store: ProjectStore,
    config: AppConfig,
    chapter: dict,
    kind: str,
    force: bool = False,
    limit: int | None = None,
    dry_run: bool = False,
) -> dict:
    """执行一章内某类媒体的生成。

    参数:
        store: 工程。
        config: 本机配置。
        chapter: 章节 dict（执行后保存）。
        kind: sfx/image/video。
        force: 已就绪项是否重跑。
        limit: 单次最多处理条数。
        dry_run: 只统计不执行。

    返回:
        {"kind", "total", "ready", "skipped", "failed", "items": [{id, status, path, error}]}。

    异常:
        ValueError: kind 非法。
    """
    if kind not in MEDIA_PATHS:
        raise ValueError(f"未知媒体类型: {kind}（可选 sfx/image/video）")
    rel_dir, ext = MEDIA_PATHS[kind]
    out_dir = store.root / rel_dir
    ffmpeg = config.get("ffmpeg.binary", "ffmpeg")
    sample_rate = int(config.get("output.sample_rate", 48000))

    stats = {"kind": kind, "total": 0, "ready": 0, "skipped": 0, "failed": 0, "items": [], "dry_run": dry_run}
    if kind in ("image", "video") and not dry_run and _provider(config, kind) == "comfyui":
        return _run_comfy(store, config, chapter, kind, force, limit, stats)

    processed = 0
    for scene, item in _iter_items(chapter, kind):
        stats["total"] += 1
        asset = item.setdefault("asset", {"status": "pending"})
        asset_path = store.root / f"{rel_dir}/{item['id']}{ext}"
        already = asset.get("status") == "ready" and asset_path.exists()
        if already and not force:
            stats["skipped"] += 1
            continue
        if limit is not None and processed >= limit:
            stats["skipped"] += 1
            continue
        processed += 1
        if dry_run:
            stats["items"].append({"id": item["id"], "status": "pending", "path": _rel(store, asset_path), "error": None})
            continue
        try:
            if kind == "sfx":
                result = render_sfx(item, asset_path, binary=ffmpeg, sample_rate=sample_rate)
                asset.update(
                    {
                        "status": "ready",
                        "asset_id": _rel(store, asset_path),
                        "actual_duration_ms": result["duration_ms"],
                        "adapter": "offline-sfx",
                        "cache_key": result["sha256"],
                        "error": None,
                    }
                )
            elif kind == "image":
                result = render_image(item, asset_path)
                asset.update(
                    {
                        "status": "ready",
                        "asset_id": _rel(store, asset_path),
                        "job_id": None,
                        "width": result["width"],
                        "height": result["height"],
                        "error": None,
                    }
                )
                item["width"], item["height"] = result["width"], result["height"]
            else:
                source = _source_image(store, chapter, item)
                result = render_video(item, asset_path, source, binary=ffmpeg)
                asset.update(
                    {
                        "status": "ready",
                        "asset_id": _rel(store, asset_path),
                        "job_id": None,
                        "actual_duration_ms": probe_duration_ms(asset_path) or result["duration_ms"],
                        "error": None,
                    }
                )
            stats["ready"] += 1
            stats["items"].append({"id": item["id"], "status": "ready", "path": _rel(store, asset_path), "error": None})
        except Exception as exc:  # 单条失败不影响整批
            asset.update({"status": "failed", "error": {"code": "RENDER_FAILED", "message": str(exc)}})
            stats["failed"] += 1
            stats["items"].append({"id": item["id"], "status": "failed", "path": None, "error": str(exc)})

    if not dry_run and processed:
        chapter["revision"] = int(chapter.get("revision") or 1) + 1
        chapter["updated_at"] = now_iso()
        store.save_chapter(chapter)
    return stats
