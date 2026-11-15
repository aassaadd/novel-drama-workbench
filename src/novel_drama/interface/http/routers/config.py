"""配置读写、设置页字段、工程生命周期与全局检索。"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

from fastapi import HTTPException, Query

from novel_drama import __version__
from novel_drama.application.story.queries import (
    settings_view,
)
from novel_drama.composition import (
    WORKSPACE_INDEX,
    OmlxWritingAdapter,
    ProjectStore,
    comfy_media,
    list_workspaces,
)
from novel_drama.infrastructure.config import DEFAULT_UI_CONFIG_PATH, load_config
from novel_drama.interface.http import use_cases
from novel_drama.interface.http.cli_bridge import fail_body
from novel_drama.interface.http.deps import run_cli, store_for
from novel_drama.interface.http.routers.common import (
    _respond,
)
from novel_drama.interface.http.routers.models_helpers import resolve_llm_settings
from novel_drama.models.ids import now_iso

CONFIG_FIELD_GROUPS = (
    (
        "运行与路径",
        (
            ("project_root", "默认工程目录", "path", None, None, None),
            ("tts.hf_home", "HF 缓存目录", "path", None, None, None),
            ("gpu.lock_path", "GPU 锁文件", "path", None, None, None),
            ("ffmpeg.binary", "ffmpeg 可执行文件", "str", None, None, None),
            ("ffmpeg.probe_binary", "ffprobe 可执行文件", "str", None, None, None),
        ),
    ),
    (
        "适配器与提供方",
        (
            ("adapters.writing", "写作适配器", "select", ("omlx",), None, None),
            ("adapters.tts", "TTS 适配器", "select", ("auto",), None, None),
            ("adapters.sfx", "音效适配器", "select", ("placeholder",), None, None),
            ("adapters.comfyui", "ComfyUI 适配器", "select", ("local",), None, None),
            ("providers.image", "图像 provider", "select", ("comfyui", "offline"), None, None),
            ("providers.video", "视频 provider", "select", ("comfyui", "offline"), None, None),
            ("models.download_mirrors", "模型下载镜像链", "strlist", None, None, None),
        ),
    ),
    (
        "写作（omlx）",
        (
            ("omlx.protocol", "接口协议", "select", ("openai_compatible",), None, None),
            ("omlx.available_models", "可选模型列表", "strlist", None, None, None),
            ("omlx.context_window", "上下文窗口", "int", None, 1, None),
            ("omlx.memory_ceiling_gb", "内存上限（GB）", "float", None, 1, None),
            ("omlx.idle_timeout_s", "空闲卸载（秒）", "int", None, 1, None),
            ("omlx.max_concurrency", "服务端并发", "int", None, 1, None),
            ("omlx.request_timeout_s", "请求超时（秒）", "int", None, 1, None),
            ("omlx.connect_timeout_s", "连接超时（秒）", "int", None, 1, None),
            ("omlx.max_retries", "最大重试次数", "int", None, 0, None),
            ("omlx.stop", "停止词", "strlist", None, None, None),
            ("omlx.enable_thinking", "启用 thinking", "bool", None, None, None),
            ("omlx.reasoning_effort", "思考强度", "select", ("low", "medium", "high"), None, None),
            ("omlx.unload.enabled", "文本任务后卸载", "bool", None, None, None),
            ("omlx.unload.path", "卸载接口路径", "str", None, None, None),
            ("omlx.unload.policy", "卸载策略", "select", ("after_text_task", "never"), None, None),
        ),
    ),
    (
        "配音（TTS）",
        (
            ("tts.comparison_keep", "对比保留候选数", "int", None, 0, None),
            ("tts.segmentation.max_chars", "单块最大字数", "int", None, 1, None),
            ("tts.segmentation.chunk_silence_ms", "块间静音（毫秒）", "int", None, 0, None),
            ("tts.segmentation.edge_trim_ms", "首尾裁剪（毫秒）", "int", None, 0, None),
            ("tts.segmentation.edge_trim_max_ms", "首尾最大裁剪（毫秒）", "int", None, 0, None),
            ("tts.segmentation.silence_threshold_dbfs", "静音阈值（dBFS）", "float", None, -120, 0),
            ("tts.segmentation.keep_min_ms", "最短保留（毫秒）", "int", None, 0, None),
            ("tts.segmentation.hard_split_ratio", "硬切比例", "float", None, 0, 1),
            ("tts.segmentation.trim_single_chunk", "单块也裁剪", "bool", None, None, None),
            ("tts.alignment.enabled", "启用时长对齐", "bool", None, None, None),
            ("tts.alignment.max_error_ms", "允许误差（毫秒）", "int", None, 0, None),
            ("tts.alignment.max_speed_change", "最大变速比例", "float", None, 0, 1),
            ("tts.alignment.on_exceed", "超差处理", "select", ("regenerate", "provider_switch", "accept"), None, None),
            ("tts.alignment.max_regenerate", "最多重生成次数", "int", None, 0, None),
            ("tts.default_voice.ref_audio", "默认参考音频", "path", None, None, None),
            ("tts.default_voice.ref_text", "参考音频文本", "str", None, None, None),
            ("tts.routing.mode", "路由模式", "select", ("auto", "manual"), None, None),
            ("tts.routing.lock_provider_id", "锁定 provider", "str", None, None, None),
            ("tts.routing.weights.capability", "路由权重·能力", "int", None, 0, None),
            ("tts.routing.weights.quality", "路由权重·质量", "int", None, 0, None),
            ("tts.routing.weights.consistency", "路由权重·一致性", "int", None, 0, None),
            ("tts.routing.weights.latency", "路由权重·延迟", "int", None, 0, None),
            ("tts.routing.weights.resource", "路由权重·资源", "int", None, 0, None),
            ("tts.routing.retry_original_first", "先重试原 provider", "bool", None, None, None),
            ("tts.routing.allow_noncommercial", "允许非商用 provider", "bool", None, None, None),
            ("tts.routing.policy_revision", "路由策略版本", "int", None, 0, None),
        ),
    ),
    (
        "音色库",
        (
            ("voice.denoise.default_enabled", "上传/录音默认降噪", "bool", None, None, None),
            ("voice.denoise.engine", "降噪引擎", "select", ("ffmpeg_afftdn",), None, None),
            ("voice.denoise.noise_reduction_db", "降噪强度（dB）", "float", None, 1, 40),
            ("voice.denoise.noise_floor_db", "底噪门限（dB）", "float", None, -80, -20),
            ("voice.denoise.max_attenuation_db", "过度抑制阈值（dB）", "float", None, 1, 60),
        ),
    ),
    (
        "ComfyUI",
        (
            ("comfyui.templates_dir", "模板目录", "path", None, None, None),
            ("comfyui.poll_interval_ms", "轮询间隔（毫秒）", "int", None, 100, None),
            ("comfyui.request_timeout_ms", "请求超时（毫秒）", "int", None, 1000, None),
            ("comfyui.job_timeout_ms", "任务超时（毫秒）", "int", None, 1000, None),
            ("comfyui.status_timeout_ms", "状态超时（毫秒）", "int", None, 1000, None),
        ),
    ),
    (
        "参考图一致性",
        (
            ("comfyui.references.enabled", "启用参考图一致性", "bool", None, None, None),
            ("comfyui.references.max_count", "参考图数量上限（1-4）", "int", None, 1, 4),
            ("comfyui.references.auto_fill", "缺图自动补全身像", "bool", None, None, None),
            ("comfyui.references.use_world_cover", "世界封面作为场景参考", "bool", None, None, None),
        ),
    ),
    (
        "GPU 与输出",
        (
            ("gpu.max_concurrent", "GPU 并发", "int", None, 1, None),
            ("gpu.poll_interval_s", "队列轮询（秒）", "int", None, 1, None),
            ("output.sample_rate", "混音采样率", "int", None, 8000, None),
            ("output.channels", "声道数", "int", None, 1, 8),
            ("output.loudness_target_lufs", "响度目标（LUFS）", "float", None, -60, 0),
            ("output.video_max_ms", "视频最长时长（毫秒）", "int", None, 1000, None),
            ("output.image_width", "默认图宽", "int", None, 64, None),
            ("output.image_height", "默认图高", "int", None, 64, None),
            ("audio.highpass_hz", "高通截止（Hz）", "int", None, 0, None),
            ("audio.high_shelf_gain_db", "高频增益（dB）", "float", None, -24, 24),
            ("audio.high_shelf_hz", "高频起始（Hz）", "int", None, 0, None),
            ("audio.true_peak_db", "真峰值（dB）", "float", None, -24, 0),
            ("audio.lra", "响度范围（LU）", "int", None, 0, 50),
        ),
    ),
    (
        "导出",
        (
            ("export.jianying.drafts_root", "剪映草稿根目录", "path", None, None, None),
        ),
    ),
)

CONFIG_EDITABLE = tuple(
    item[0] for _group, items in CONFIG_FIELD_GROUPS for item in items
) + (
    "omlx.endpoint",
    "omlx.model",
    "omlx.api_key",
    "omlx.temperature",
    "omlx.first_token_timeout_s",
    "omlx.stream_idle_timeout_s",
    "omlx.total_timeout_s",
    "tts.default_provider_id",
    "tts.provider_order",
    "comfyui.base_url",
    "comfyui.models_dir",
    "comfyui.templates.image",
    "comfyui.templates.image_ref",
    "comfyui.templates.video_t2v",
    "comfyui.templates.video_i2v",
    "comfyui.templates.music",
    "paths.models_root",
)

CONFIG_FIELD_SPEC = {
    item[0]: (item[2], item[3], item[4], item[5]) for _group, items in CONFIG_FIELD_GROUPS for item in items
}
CONFIG_FIELD_SPEC.update(
    {
        "omlx.endpoint": ("url", None, None, None),
        "omlx.model": ("str", None, None, None),
        "omlx.api_key": ("password", None, None, None),
        "omlx.temperature": ("float", None, 0, 2),
        "omlx.first_token_timeout_s": ("int", None, 1, None),
        "omlx.stream_idle_timeout_s": ("int", None, 1, None),
        "omlx.total_timeout_s": ("int", None, 1, None),
        "tts.default_provider_id": ("str", None, None, None),
        "tts.provider_order": ("strlist", None, None, None),
        "comfyui.base_url": ("url", None, None, None),
        "comfyui.models_dir": ("path", None, None, None),
        "comfyui.templates.image": ("str", None, None, None),
        "comfyui.templates.image_ref": ("str", None, None, None),
        "comfyui.templates.video_t2v": ("str", None, None, None),
        "comfyui.templates.video_i2v": ("str", None, None, None),
        "comfyui.templates.music": ("str", None, None, None),
        "paths.models_root": ("path", None, None, None),
    }
)

_MISSING = object()

def _dig(data: dict, dotted: str):
    """按点号路径取值。

    参数:
        data: 配置字典。
        dotted: 点号路径，如 `omlx.model`。

    返回:
        取到的值；路径不存在时返回 `_MISSING`。
    """
    node = data
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return _MISSING
        node = node[part]
    return node

def _put_dotted(data: dict, dotted: str, value) -> None:
    """按点号路径写入，中间层不存在时自动补空字典。

    参数:
        data: 目标字典（就地修改）。
        dotted: 点号路径。
        value: 要写入的值。

    返回:
        无。
    """
    parts = dotted.split(".")
    node = data
    for part in parts[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            node[part] = nxt
        node = nxt
    node[parts[-1]] = value

def _del_dotted(data: dict, dotted: str) -> None:
    """按点号路径删除，并顺手清掉因此变空的父级字典。

    参数:
        data: 目标字典（就地修改）。
        dotted: 点号路径。

    返回:
        无。
    """
    parts = dotted.split(".")
    stack: list[tuple[dict, str]] = []
    node = data
    for part in parts[:-1]:
        if not isinstance(node, dict) or part not in node:
            return
        stack.append((node, part))
        node = node[part]
    if isinstance(node, dict):
        node.pop(parts[-1], None)
    for parent, key in reversed(stack):
        if isinstance(parent.get(key), dict) and not parent[key]:
            parent.pop(key)

def _validate_config_patch(values: dict) -> dict:
    """校验页面提交的配置改动。

    参数:
        values: `{点号路径: 新值}`；值为 None / "" / [] 表示删除该覆盖项。

    返回:
        校验通过后的值字典（类型已归一）。

    异常:
        ValueError: 出现白名单外的键或类型/取值不合法。
    """
    unknown = [key for key in values if key not in CONFIG_EDITABLE]
    if unknown:
        raise ValueError(f"不支持在页面修改：{'、'.join(unknown)}")

    cleaned: dict = {}
    for key, raw in values.items():
        if raw is None or raw == "" or raw == []:
            cleaned[key] = None
            continue
        kind, options, minimum, maximum = CONFIG_FIELD_SPEC[key]
        if kind == "bool":
            cleaned[key] = bool(raw)
        elif kind == "int":
            try:
                number = int(raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{key} 必须是整数") from exc
            if minimum is not None and number < minimum:
                raise ValueError(f"{key} 不能小于 {minimum}")
            if maximum is not None and number > maximum:
                raise ValueError(f"{key} 不能大于 {maximum}")
            cleaned[key] = number
        elif kind == "float":
            try:
                number = float(raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{key} 必须是数字") from exc
            if key == "omlx.temperature" and not 0 <= number <= 2:
                raise ValueError("temperature 必须在 0~2 之间")
            if minimum is not None and number < minimum:
                raise ValueError(f"{key} 不能小于 {minimum}")
            if maximum is not None and number > maximum:
                raise ValueError(f"{key} 不能大于 {maximum}")
            cleaned[key] = number
        elif kind == "strlist":
            if isinstance(raw, str):
                raw = [item.strip() for item in raw.replace("，", ",").replace("\n", ",").split(",")]
            if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
                raise ValueError(f"{key} 必须是字符串数组")
            cleaned[key] = [item.strip() for item in raw if item.strip()]
        elif kind == "url":
            text = str(raw).strip()
            if not text.startswith(("http://", "https://")):
                raise ValueError(f"{key} 必须是 http:// 或 https:// 开头的地址")
            cleaned[key] = text
        elif kind == "select":
            text = str(raw).strip()
            if options and text not in options:
                raise ValueError(f"{key} 只能是 {'、'.join(options)}")
            cleaned[key] = text
        else:  # str / path / password
            text = str(raw)
            # 模型 id 原样存储：保留 models/ 前缀与 .gguf 后缀，不做 trim/规范化
            cleaned[key] = text if key == "omlx.model" else text.strip()
    return cleaned

def _templates_manifest(cfg) -> list[dict]:
    """读取 ComfyUI 模板清单，整理成设置页需要的只读结构。

    参数:
        cfg: AppConfig（读 `comfyui.templates_dir`）。

    返回:
        `[{id, kind, display_name, fixed, size_map, models, placeholders}]`；
        清单缺失或损坏时返回空列表（页面据此只保留「未识别」选项）。
    """
    try:
        manifest = comfy_media.load_manifest(comfy_media.templates_dir(cfg))
    except Exception:  # 清单缺失不能让设置页整体读取失败
        return []
    rows: list[dict] = []
    for entry in comfy_media.manifest_entries(manifest):
        template_id = str(entry.get("id") or entry.get("template_id") or "")
        if not template_id:
            continue
        rows.append(
            {
                "id": template_id,
                "kind": entry.get("kind"),
                "display_name": entry.get("display_name") or template_id,
                "fixed": entry.get("fixed") or {},
                "size_map": entry.get("size_map") or {},
                "models": list(entry.get("models") or []),
                "placeholders": list(entry.get("placeholders") or entry.get("required_placeholders") or []),
            }
        )
    return rows

def _advanced_payload(cfg) -> list[dict]:
    """把 config.yaml 的其余可调项整理成分组表单。

    参数:
        cfg: AppConfig。

    返回:
        `[{group, fields: [{key, label, type, options, value}]}]`；
        value 为当前生效值（缺失时 None），strlist 统一成数组。
    """
    groups: list[dict] = []
    for group, items in CONFIG_FIELD_GROUPS:
        fields: list[dict] = []
        for key, label, kind, options, _minimum, _maximum in items:
            value = _dig(cfg.raw, key)
            if value is _MISSING:
                value = None
            if kind == "strlist":
                value = list(value) if isinstance(value, (list, tuple)) else ([] if value is None else [str(value)])
            fields.append(
                {
                    "key": key,
                    "label": label,
                    "type": kind,
                    "options": list(options) if options else [],
                    "value": value,
                }
            )
        groups.append({"group": group, "fields": fields})
    return groups

def _config_payload(cfg) -> dict:
    """把生效配置整理成「设置」页需要的结构。

    参数:
        cfg: AppConfig。

    返回:
        {writing, tts, image, paths, editable, overrides}。
    """
    omlx = cfg.get("omlx", {}) or {}
    tts = cfg.get("tts", {}) or {}
    comfy = cfg.get("comfyui", {}) or {}
    api_key = omlx.get("api_key") or ""

    providers = [
        {
            "provider_id": item.get("provider_id"),
            "display_name": item.get("display_name"),
            "enabled": bool(item.get("enabled")),
            "backend": item.get("backend"),
            "weights_path": item.get("weights_path"),
            "quality_tier": (item.get("capabilities") or {}).get("quality_tier"),
            "commercial_use": (item.get("capabilities") or {}).get("commercial_use"),
        }
        for item in (tts.get("providers") or [])
    ]

    overrides = {
        key: _dig(cfg.ui_values or {}, key) for key in CONFIG_EDITABLE if _dig(cfg.ui_values or {}, key) is not _MISSING
    }

    return {
        "writing": {
            "endpoint": omlx.get("endpoint"),
            "model": omlx.get("model"),
            "has_api_key": bool(api_key),
            "api_key_hint": f"已配置（{len(api_key)} 位）" if api_key else "未配置",
            "temperature": omlx.get("temperature"),
            "first_token_timeout_s": omlx.get("first_token_timeout_s"),
            "stream_idle_timeout_s": omlx.get("stream_idle_timeout_s"),
            "total_timeout_s": omlx.get("total_timeout_s"),
            "available_models": omlx.get("available_models") or [],
            "context_window": omlx.get("context_window"),
        },
        "tts": {
            "default_provider_id": tts.get("default_provider_id"),
            "provider_order": tts.get("provider_order") or [],
            "providers": providers,
        },
        "image": {
            "base_url": comfy.get("base_url"),
            "templates_dir": comfy.get("templates_dir"),
            "templates_dir_exists": (Path(comfy.get("templates_dir") or "")).is_dir() if comfy.get("templates_dir") else False,
            "models_dir": comfy.get("models_dir"),
            "models_dir_exists": (Path(comfy.get("models_dir") or "")).is_dir() if comfy.get("models_dir") else False,
            "templates": comfy.get("templates") or {},
            "templates_manifest": _templates_manifest(cfg),
        },
        "paths": {
            "models_root": cfg.get("paths.models_root"),
            "models_root_exists": (Path(cfg.get("paths.models_root") or "")).is_dir() if cfg.get("paths.models_root") else False,
        },
        "editable": list(CONFIG_EDITABLE),
        "advanced": _advanced_payload(cfg),
        "overrides": overrides,
        "ui_path": str(cfg.ui_path) if cfg.ui_path else None,
    }

def _project_dir(raw) -> Path:
    """解析并校验前端传来的工程目录。

    参数:
        raw: 前端传来的路径字符串。

    返回:
        规范化后的绝对路径。

    异常:
        HTTPException 400: 路径为空。
        HTTPException 404: 目录里没有 project.json，不是工程目录。
    """
    text = str(raw or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail={"code": 2, "message": "缺少工程路径"})
    root = Path(text).expanduser().resolve()
    if not (root / "project.json").is_file():
        raise HTTPException(status_code=404, detail={"code": 4, "message": f"不是工程目录（缺少 project.json）：{root}"})
    return root

def register_config_routes(app, project_root: str | None = None, config_path: str | None = None) -> None:
    """注册「配置读写、设置页字段、工程生命周期与全局检索」相关路由。

    参数:
        app: FastAPI 实例。
        project_root: 默认工程目录（请求未带 path 时使用）。
        config_path: 本机配置文件路径（与 serve 同源）。

    返回:
        无。
    """
    _store = store_for(project_root)

    def _cli(*argv: str):
        """按 `serve --config` 的同一份配置执行 CLI。

        参数:
            argv: 不含 `--config` 的参数串。

        返回:
            `actions.CliResult`。
        """
        return run_cli(config_path, *argv)

    @app.get("/api/health")
    def health() -> dict:
        """健康检查。"""
        return {"ok": True, "version": __version__}

    @app.get("/api/config")
    def read_config() -> dict:
        """「设置」页回填：当前生效的写作 / TTS / 图像视频配置（api_key 只给掩码提示）。"""
        return _config_payload(load_config(config_path))

    @app.post("/api/config")
    def write_config(payload: dict) -> dict:
        """「设置」页保存：把白名单内的改动写进覆盖层 config/config.ui.yaml。

        参数:
            payload: `{"values": {点号路径: 新值}}`；值为 None / "" / [] 表示删除该覆盖项。

        返回:
            写入后重新计算出的生效配置（结构同 GET /api/config）。

        异常:
            HTTPException 400: 出现白名单外的键，或类型/取值不合法。
        """
        raw_values = (payload or {}).get("values") or {}
        if not isinstance(raw_values, dict):
            raise HTTPException(status_code=400, detail={"code": 2, "message": "values 必须是对象"})
        try:
            values = _validate_config_patch(raw_values)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"code": 2, "message": str(exc)}) from exc

        # 模型名必须来自服务端实时列表：保存前用「本次提交的地址 + 密钥」再校验一次
        model_value = values.get("omlx.model")
        if isinstance(model_value, str) and model_value:
            body = {
                "endpoint": values.get("omlx.endpoint") or "",
                "api_key": values.get("omlx.api_key") or "",
                "model": model_value,
            }
            settings, submitted_model = resolve_llm_settings(
                load_config(config_path), body, allow_empty_model=True
            )
            adapter = OmlxWritingAdapter(settings)
            catalog = adapter.fetch_model_catalog(timeout_s=2.0)
            ids = [str(item.get("id")) for item in catalog.get("models") or []]
            if not catalog.get("ok"):
                raise HTTPException(
                    status_code=400,
                    detail={
                        "code": 2,
                        "message": f"无法在 {adapter.endpoint} 校验模型：{catalog.get('message') or '服务未返回模型列表'}",
                    },
                )
            if submitted_model not in ids:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "code": 2,
                        "message": f"在 {adapter.endpoint} 的模型列表中未找到模型 {submitted_model}，"
                        f"当前可用：{'、'.join(ids)}",
                    },
                )

        # 目录类配置在保存时先创建，创建失败直接拦下，避免写入不可用路径
        for key in ("paths.models_root", "comfyui.models_dir"):
            target_dir = values.get(key)
            if not target_dir:
                continue
            try:
                Path(str(target_dir)).expanduser().mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise HTTPException(
                    status_code=400,
                    detail={"code": 2, "message": f"{key} 目录不可用（无法创建或不可写）：{exc}"},
                ) from exc

        try:
            import yaml
        except ImportError as exc:  # pragma: no cover
            raise HTTPException(status_code=500, detail={"code": 3, "message": "缺少 PyYAML，无法写入配置"}) from exc

        target = DEFAULT_UI_CONFIG_PATH
        current = {}
        if target.exists():
            try:
                current = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
            except Exception:  # 覆盖层写坏了就以空层重建，原文件已备份
                current = {}
            if not isinstance(current, dict):
                current = {}
            target.with_suffix(".yaml.bak").write_text(target.read_text(encoding="utf-8"), encoding="utf-8")

        for key, value in values.items():
            if value is None:
                _del_dotted(current, key)
            else:
                _put_dotted(current, key, value)

        target.parent.mkdir(parents=True, exist_ok=True)
        header = (
            "# 由 Web「设置」页写入，只覆盖 config.yaml 里的同名键。\n"
            "# 删掉本文件（或删掉某个键）即回落到 config.yaml 的取值。\n"
        )
        if current:
            target.write_text(header + yaml.safe_dump(current, allow_unicode=True, sort_keys=False), encoding="utf-8")
        elif target.exists():
            target.unlink()

        return _config_payload(load_config(config_path))

    @app.get("/api/projects")
    def projects() -> list[dict]:
        """列出本机工程。"""
        return list_workspaces()

    @app.post("/api/project/reveal")
    def reveal_project(body: dict) -> dict:
        """在访达里打开工程目录。

        参数:
            body: `{"path": 工程目录}`。

        返回:
            `{"ok": true, "path": 绝对路径}`。

        异常:
            HTTPException 404: 目录不是工程（没有 project.json）。
            HTTPException 500: 调用 open 失败（非 macOS 或系统拒绝）。
        """
        root = _project_dir(body.get("path"))
        if sys.platform != "darwin":
            raise HTTPException(status_code=500, detail={"code": 3, "message": "当前系统不支持用访达打开"})
        result = subprocess.run(["open", str(root)], capture_output=True, text=True)
        if result.returncode != 0:
            raise HTTPException(status_code=500, detail={"code": 3, "message": f"打开失败：{result.stderr.strip() or result.returncode}"})
        return {"ok": True, "path": str(root)}

    @app.post("/api/project/delete")
    def delete_project(body: dict) -> dict:
        """把整个工程目录移入废纸篓（可从废纸篓恢复，不做 rm -rf）。

        参数:
            body: `{"path": 工程目录, "confirm": true}`，confirm 必须是布尔 true。

        返回:
            `{"ok": true, "path": 原路径}`。

        异常:
            HTTPException 400: 没有显式确认。
            HTTPException 404: 目录不是工程（没有 project.json）。
            HTTPException 500: 非 macOS，或 osascript 调用失败。
        """
        root = _project_dir(body.get("path"))
        if body.get("confirm") is not True:
            raise HTTPException(status_code=400, detail={"code": 2, "message": "删除工程需要显式确认"})
        if sys.platform != "darwin":
            raise HTTPException(status_code=500, detail={"code": 3, "message": "当前系统不支持移入废纸篓"})
        # 走 Finder 删除才会进废纸篓；直接 unlink 无法恢复
        script = f'tell application "Finder" to delete POSIX file "{root}"'
        result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
        if result.returncode != 0:
            raise HTTPException(status_code=500, detail={"code": 3, "message": f"移入废纸篓失败：{result.stderr.strip() or result.returncode}"})
        return {"ok": True, "path": str(root)}

    @app.get("/api/ui/projects/overview")
    def projects_overview() -> list[dict]:
        """工程列表扩展信息：章节数、总字数、磁盘占用。

        返回:
            `[{path, title, last_opened_at, chapters, words, size_bytes}]`；
            单个工程读取失败时该项带 `error`，不影响其它工程。
        """
        rows: list[dict] = []
        for item in list_workspaces():
            row = dict(item)
            try:
                store = ProjectStore(item["path"])
                chapters = store.list_chapters()
                row["chapters"] = len(chapters)
                row["words"] = sum(int(chapter.get("word_count") or 0) for chapter in chapters)
                total = 0
                newest = 0.0
                for file in Path(item["path"]).rglob("*"):
                    if file.is_file():
                        try:
                            stat = file.stat()
                        except OSError:
                            continue
                        total += stat.st_size
                        newest = max(newest, stat.st_mtime)
                # 配音完成度：逐章统计行数，供首页「状态」筛选使用
                ready_lines = 0
                total_lines = 0
                for chapter in chapters:
                    try:
                        data = store.load_chapter(chapter["id"])
                    except Exception:  # noqa: BLE001 - 单章损坏不影响其它统计
                        continue
                    for scene in data.get("scenes") or []:
                        for line in scene.get("lines") or []:
                            total_lines += 1
                            if (line.get("tts") or {}).get("status") == "ready":
                                ready_lines += 1
                row["ready_lines"] = ready_lines
                row["total_lines"] = total_lines
                row["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(newest)) if newest else None
                row["size_bytes"] = total
            except Exception as exc:  # noqa: BLE001 - 单个工程损坏不影响列表
                row["error"] = str(exc)
            rows.append(row)
        return rows

    @app.post("/api/ui/projects/reindex")
    def projects_reindex(body: dict | None = None) -> dict:
        """重建本机工程索引：扫描默认工程目录并写回 ~/.ndw/workspaces.json。

        参考产品在「项目」页提供刷新索引；本地没有服务端索引缓存，但仍会维护
        `~/.ndw/workspaces.json` 作为工程列表数据源。本接口负责重新扫描磁盘，
        把手动移动过目录、或从其它位置拷入的工程重新登记进来。

        参数:
            body: 可选 `{root?: 额外扫描目录}`；不传时使用配置里的 `project_root`。

        返回:
            `{ok: true, count: 扫描到的工程数, index: 索引文件路径}`。
        """
        candidates: list[Path] = []
        raw = str((body or {}).get("root") or load_config(config_path).get("project_root") or "").strip()
        if raw:
            candidates.append(Path(raw).expanduser())
        # 现有索引里工程的父目录也补扫，避免工程被整体移动后从列表消失
        for item in list_workspaces():
            parent = Path(item["path"]).parent
            if parent == Path.home() or len(parent.parts) < 4:
                continue
            if parent not in candidates:
                candidates.append(parent)

        found: dict[str, Path] = {}

        def scan(base: Path, depth: int) -> None:
            """递归扫描 base 下 depth 层内的工程目录。"""
            try:
                if (base / "project.json").is_file():
                    found[str(base.resolve())] = base.resolve()
                if depth <= 0:
                    return
                for child in sorted(base.iterdir()):
                    if child.name.startswith(".") or not child.is_dir():
                        continue
                    scan(child, depth - 1)
            except OSError:
                return

        for base in candidates:
            scan(base, 2)

        # 保留已有工程的最近打开时间，新发现的工程按 project.json 修改时间排序
        previous = {item["path"]: item.get("last_opened_at") for item in list_workspaces()}
        projects = []
        for path, root in found.items():
            stamp = previous.get(path)
            if not stamp:
                try:
                    stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime((root / "project.json").stat().st_mtime))
                except OSError:
                    stamp = now_iso()
            projects.append({"path": path, "last_opened_at": stamp})
        projects.sort(key=lambda item: item.get("last_opened_at") or "", reverse=True)
        try:
            WORKSPACE_INDEX.parent.mkdir(parents=True, exist_ok=True)
            WORKSPACE_INDEX.write_text(
                json.dumps({"projects": projects}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"索引文件不可写：{exc}") from exc
        return {"ok": True, "count": len(projects), "index": str(WORKSPACE_INDEX)}

    @app.post("/api/ui/project/duplicate")
    def duplicate_project(body: dict) -> dict:
        """复制整个工程目录，并把副本登记到工程列表。

        参数:
            body: `{"path": 源工程, "name"?: 副本目录名}`。

        返回:
            `{"ok": true, "path": 副本路径}`。

        异常:
            HTTPException 400: 副本目录已存在。
            HTTPException 404: 源目录不是工程。
            HTTPException 500: 复制失败（磁盘或权限）。
        """
        source = _project_dir(body.get("path"))
        name = str(body.get("name") or "").strip() or f"{source.name}-copy"
        # 只允许目录名，禁止把副本写到其它目录
        if "/" in name or name in {".", ".."}:
            raise HTTPException(status_code=400, detail={"code": 2, "message": "副本名称只能是目录名，不能带路径"})
        target = source.parent / name
        if target.exists():
            raise HTTPException(status_code=400, detail={"code": 2, "message": f"目标目录已存在：{target}"})
        try:
            shutil.copytree(source, target)
        except OSError as exc:
            raise HTTPException(status_code=500, detail={"code": 3, "message": f"复制失败：{exc}"}) from exc
        ProjectStore(str(target)).register_workspace()
        return {"ok": True, "path": str(target)}

    @app.get("/api/project/info")
    def project_info(path: str | None = Query(default=None)) -> dict:
        """工程概览（作品、章节数、角色数）。"""
        store = _store(path)
        project = store.load_project()
        return {"root": str(store.root), "work": project["work"], "chapters": store.list_chapters(), "characters": len(store.load_characters()["characters"])}

    @app.get("/api/ui/project/production")
    def project_production(path: str | None = Query(default=None)) -> dict:
        """读取项目级制作要求（静音间隔 + 音频导出参数）。

        参数:
            path: 工程目录。

        返回:
            `{production: {silences_ms, audio}}`。
        """
        return {"production": _store(path).production()}

    @app.post("/api/ui/project/production")
    def save_project_production(body: dict) -> dict:
        """保存项目级制作要求，并同步输出采样率。

        参数:
            body: `{path, production: {silences_ms?, audio?}}`，允许只带其中一段。

        返回:
            `{production: 保存后的完整值}`。

        异常:
            HTTPException 400: 字段类型或取值范围不合法。
        """
        store = _store(body.get("path"))
        incoming = body.get("production") or {}
        clean: dict = {}
        silences = incoming.get("silences_ms")
        if isinstance(silences, dict):
            clean["silences_ms"] = {}
            for key, value in silences.items():
                try:
                    number = int(value)
                except (TypeError, ValueError) as exc:
                    raise HTTPException(status_code=400, detail=f"静音间隔 `{key}` 必须是整数毫秒") from exc
                if number < 0 or number > 10000:
                    raise HTTPException(status_code=400, detail=f"静音间隔 `{key}` 应在 0–10000 毫秒之间")
                clean["silences_ms"][key] = number
        audio = incoming.get("audio")
        if isinstance(audio, dict):
            fmt = str(audio.get("format") or "").lower()
            if fmt not in {"mp3", "wav", "m4a"}:
                raise HTTPException(status_code=400, detail="音频格式只支持 mp3 / wav / m4a")
            try:
                sample_rate = int(audio.get("sample_rate"))
                bitrate = int(audio.get("bitrate_kbps"))
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=400, detail="采样率与码率必须是整数") from exc
            if sample_rate not in {44100, 48000}:
                raise HTTPException(status_code=400, detail="采样率只支持 44100 / 48000")
            if bitrate not in {192, 256}:
                raise HTTPException(status_code=400, detail="目标码率只支持 192 / 256 kbps")
            clean["audio"] = {"format": fmt, "sample_rate": sample_rate, "bitrate_kbps": bitrate}
        return {"production": store.save_production(clean)}

    @app.get("/api/ui/search")
    def ui_search(
        q: str = Query(""),
        path: str | None = Query(default=None),
        limit: int = Query(default=40),
    ) -> dict:
        """全局搜索：跨工程、跨章节正文与台词行。

        参数:
            q: 关键词（去首尾空格后至少 2 个字符，否则直接返回空结果）。
            path: 限定工程目录；为空时搜索最近打开的多个工程（最多 12 个）。
            limit: 章节 / 台词两组各自最多返回条数（1~100）。

        返回:
            `{q, scope, counts:{projects,chapters,lines}, chapters:[...], lines:[...], truncated}`。
            章节命中带 `count` 与首处 `snippet`；台词命中带 `line_id`、说话人、状态与场景名，
            供前端直达具体台词行。
        """
        keyword = (q or "").strip()
        scope = "current" if path else "all"
        payload = {
            "q": keyword,
            "scope": scope,
            "counts": {"projects": 0, "chapters": 0, "lines": 0},
            "chapters": [],
            "lines": [],
            "truncated": False,
        }
        if len(keyword) < 2:
            return payload
        lowered = keyword.lower()
        cap = max(1, min(int(limit or 40), 100))
        if path:
            roots = [str(Path(path).expanduser())]
        else:
            roots = [
                str(item.get("path"))
                for item in list_workspaces()[:12]
                if item.get("path")
            ]
        hit_projects: set[str] = set()
        for root in roots:
            if len(payload["chapters"]) >= cap and len(payload["lines"]) >= cap:
                break
            try:
                store = ProjectStore(root)
                project = store.load_project()
            except Exception:  # noqa: BLE001 - 单个工程不可读不影响其它工程
                continue
            project_title = str((project.get("work") or {}).get("title") or Path(root).name)
            try:
                names = {
                    str(item.get("id")): str(item.get("name") or "")
                    for item in (store.load_characters().get("characters") or [])
                }
            except Exception:  # noqa: BLE001 - 角色表缺失时用 ID 兜底
                names = {}
            for meta in store.list_chapters():
                try:
                    chapter = store.load_chapter(meta.get("id"))
                except Exception:  # noqa: BLE001 - 损坏章节跳过
                    continue
                chapter_id = str(chapter.get("id") or meta.get("id"))
                chapter_title = str(chapter.get("title") or "")
                chapter_index = chapter.get("index") or meta.get("index")
                content = str(chapter.get("content") or "")
                haystack = content.lower()
                count = haystack.count(lowered)
                if count or lowered in chapter_title.lower():
                    if len(payload["chapters"]) < cap:
                        offset = haystack.find(lowered)
                        snippet = ""
                        if offset >= 0:
                            start = max(0, offset - 18)
                            snippet = content[start : offset + len(keyword) + 22].replace("\n", " ")
                        payload["chapters"].append(
                            {
                                "project": root,
                                "project_title": project_title,
                                "chapter_id": chapter_id,
                                "chapter_title": chapter_title,
                                "chapter_index": chapter_index,
                                "count": count,
                                "snippet": snippet,
                            }
                        )
                        hit_projects.add(root)
                for scene in chapter.get("scenes") or []:
                    scene_title = str(scene.get("title") or "")
                    for line in scene.get("lines") or []:
                        text = str(line.get("text") or "")
                        if lowered not in text.lower():
                            continue
                        if len(payload["lines"]) >= cap:
                            break
                        speaker_id = str(line.get("speaker_id") or "")
                        payload["lines"].append(
                            {
                                "project": root,
                                "project_title": project_title,
                                "chapter_id": chapter_id,
                                "chapter_title": chapter_title,
                                "chapter_index": chapter_index,
                                "line_id": str(line.get("id") or ""),
                                "speaker": names.get(speaker_id)
                                or ("旁白" if speaker_id == "chr_narrator" else speaker_id),
                                "text": text,
                                "state": str((line.get("tts") or {}).get("status") or "pending"),
                                "scene_title": scene_title,
                            }
                        )
                        hit_projects.add(root)
                    if len(payload["lines"]) >= cap:
                        break
                if len(payload["lines"]) >= cap and len(payload["chapters"]) >= cap:
                    break
        payload["counts"] = {
            "projects": len(hit_projects),
            "chapters": len(payload["chapters"]),
            "lines": len(payload["lines"]),
        }
        payload["truncated"] = (
            len(payload["chapters"]) >= cap or len(payload["lines"]) >= cap
        )
        return payload

    @app.get("/api/ui/setting")
    def ui_setting(path: str = Query(...)):
        """设定库全文（应用层读模型，与 `setting show --json` 同源）。"""
        store = _store(path)
        return use_cases.call(lambda: settings_view(store))

    @app.post("/api/ui/project/init")
    def ui_project_init(body: dict) -> dict:
        """新建工程（`init`），返回可直接用于后续请求的绝对路径。"""
        argv = ["--json", "init", body["dir"], "--title", body["title"]]
        if body.get("author"):
            argv += ["--author", body["author"]]
        for genre in body.get("genres") or []:
            if genre:
                argv += ["--genre", genre]
        result = _cli(*argv)
        if result.code != 0:
            raise HTTPException(status_code=result.status, detail=fail_body(result))
        payload = result.payload if isinstance(result.payload, dict) else {}
        payload["root"] = str(ProjectStore(body["dir"]).root)
        return payload

    @app.post("/api/ui/project/pick-dir")
    def ui_project_pick_dir() -> dict:
        """弹出 macOS 原生「选择文件夹」面板，返回选中的绝对路径。

        返回:
            `{"path": 绝对路径}`；用户点取消时 `{"path": ""}`。

        异常:
            HTTPException 500: 非 macOS，或 osascript 调用失败 / 超时。
        """
        if sys.platform != "darwin":
            raise HTTPException(status_code=500, detail={"code": 3, "message": "当前系统不支持选择文件夹"})
        # 直接由 osascript 弹原生面板，不经过 System Events，免掉额外的自动化授权
        script = 'POSIX path of (choose folder with prompt "选择工程目录")'
        try:
            result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=300)
        except subprocess.TimeoutExpired as exc:
            raise HTTPException(status_code=500, detail={"code": 3, "message": "选择目录超时"}) from exc
        if result.returncode != 0:
            stderr = result.stderr.strip()
            # 用户点「取消」时 osascript 返回 -128，不当作错误打扰
            if "-128" in stderr or "User canceled" in stderr:
                return {"path": ""}
            raise HTTPException(status_code=500, detail={"code": 3, "message": f"选择目录失败：{stderr or result.returncode}"})
        # choose folder 的 POSIX path 末尾带斜杠，去掉后与手输路径保持一致
        picked = result.stdout.strip().rstrip("/") or "/"
        return {"path": picked}

    @app.get("/api/ui/file")
    def ui_file(path: str = Query(...), project: str = Query(...)):
        """只读返回工程内单个文件，供页面内联预览图片 / 音频 / 视频。

        参数:
            path: 相对工程根目录的文件路径（如 `assets/voice/lines/xxx.wav`）。
            project: 工程根目录，用于把相对路径钉死在工程内。

        返回:
            FileResponse；路径越界或文件不存在时返回 403 / 404。
        """
        from fastapi.responses import FileResponse

        root = _store(project).root.resolve()
        target = (root / path).resolve()
        if target != root and root not in target.parents:
            raise HTTPException(status_code=403, detail={"code": 2, "message": "路径越界"})
        if not target.is_file():
            raise HTTPException(status_code=404, detail={"code": 3, "message": "文件不存在", "path": path})
        return FileResponse(target)

    @app.get("/api/ui/project/work")
    def ui_project_work(path: str = Query(...)) -> dict:
        """作品元信息（书名 / 作者 / 类型），供创作向导回填。"""
        _store(path)
        payload = _respond(_cli("--project", path, "--json", "project", "info"))
        work = (payload or {}).get("work") or {}
        return {"title": work.get("title") or "", "author": work.get("author") or "", "genres": work.get("genre") or []}

    @app.post("/api/ui/project/work")
    def ui_project_work_set(body: dict) -> dict:
        """写回作品元信息（`project set`）。

        参数:
            body: `{path, title?, author?, genres?[]}`；genres 为空数组表示清空。

        异常:
            HTTPException 400: 没有任何要修改的字段，或 genres 不是数组。
        """
        path = body.get("path")
        _store(path)
        argv = ["--project", path, "--json", "project", "set"]
        if body.get("title") is not None:
            argv += ["--title", str(body["title"])]
        if body.get("author") is not None:
            argv += ["--author", str(body["author"])]
        genres = body.get("genres")
        if genres is not None:
            if not isinstance(genres, list):
                raise HTTPException(status_code=400, detail={"code": 2, "message": "genres 必须是数组"})
            if genres:
                for genre in genres:
                    if str(genre).strip():
                        argv += ["--genre", str(genre).strip()]
            else:
                argv.append("--clear-genres")
        if len(argv) == 5:
            raise HTTPException(status_code=400, detail={"code": 2, "message": "没有要修改的字段"})
        return _respond(_cli(*argv))
