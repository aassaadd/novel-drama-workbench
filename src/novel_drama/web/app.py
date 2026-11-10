"""展示层 HTTP 骨架（FastAPI）。

页面只消费 CLI 的输出：所有读写都经 `web/actions.run` 调 `cli.main`，
不在这里重算行状态，也不复制业务规则。
接口统一以 `path` 查询参数指定工程目录，避免引入工程注册服务。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from fastapi import HTTPException

from .. import __version__
from ..adapters.base import AdapterError
from ..config import DEFAULT_UI_CONFIG_PATH, load_config
from ..media import comfy_media
from ..models.ids import now_iso
from ..store import ProjectStore, list_workspaces
from ..writing.editing import edit_chapter, save_chapter_content
from ..writing.summarize import summarize_chapter
from .actions import fail_body, run
from .models_api import register_models_routes

STATIC_DIR = Path(__file__).with_name("static")

#: 「设置」页允许改写的配置键（点号路径）；其余键一律拒绝，避免页面改坏配置
#: 设置页「全部配置」分组：分组名 → (键, 中文标签, 类型, 选项, 最小值, 最大值)
#: 已有专属控件的键（写作地址/模型、模板选择、两个目录等）不在这里重复列出，避免同键两个控件。
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
)

#: 可编辑键：分组字段 + 设置页已有专属控件的键（同键不复用两次）
CONFIG_EDITABLE = tuple(
    item[0] for _group, items in CONFIG_FIELD_GROUPS for item in items
) + (
    "omlx.endpoint",
    "omlx.model",
    "omlx.api_key",
    "omlx.temperature",
    "omlx.max_tokens",
    "tts.default_provider_id",
    "tts.provider_order",
    "comfyui.base_url",
    "comfyui.models_dir",
    "comfyui.templates.image",
    "comfyui.templates.image_ref",
    "comfyui.templates.video_t2v",
    "comfyui.templates.video_i2v",
    "paths.models_root",
)

#: 键 → (类型, 选项, 最小值, 最大值)，供统一校验
CONFIG_FIELD_SPEC = {
    item[0]: (item[2], item[3], item[4], item[5]) for _group, items in CONFIG_FIELD_GROUPS for item in items
}
CONFIG_FIELD_SPEC.update(
    {
        "omlx.endpoint": ("url", None, None, None),
        "omlx.model": ("str", None, None, None),
        "omlx.api_key": ("password", None, None, None),
        "omlx.temperature": ("float", None, 0, 2),
        "omlx.max_tokens": ("int", None, 1, None),
        "tts.default_provider_id": ("str", None, None, None),
        "tts.provider_order": ("strlist", None, None, None),
        "comfyui.base_url": ("url", None, None, None),
        "comfyui.models_dir": ("path", None, None, None),
        "comfyui.templates.image": ("str", None, None, None),
        "comfyui.templates.image_ref": ("str", None, None, None),
        "comfyui.templates.video_t2v": ("str", None, None, None),
        "comfyui.templates.video_i2v": ("str", None, None, None),
        "paths.models_root": ("path", None, None, None),
    }
)

#: 缺失标记：区分「键不存在」与「值为 None」
_MISSING = object()

#: 设定库可在界面上编辑的标量字段（与 `setting set` 的参数一一对应）
SETTING_SCALARS = (
    "logline",
    "theme",
    "core_conflict",
    "ending_intent",
    "target_audience",
    "target_length",
    "era",
    "tone",
    "pov",
    "tense",
    "prose_style",
    "visual_style",
)


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
            cleaned[key] = str(raw).strip()
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
            "max_tokens": omlx.get("max_tokens"),
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


def create_app(project_root: str | None = None, config_path: str | None = None):
    """创建 FastAPI 应用。

    参数:
        project_root: 默认工程目录（可被请求参数覆盖）。
        config_path: 本机配置文件路径；只用于给后台生成进程透传，保证与 `serve` 同源。

    返回:
        FastAPI 实例。

    异常:
        RuntimeError: 未安装 fastapi。
    """
    try:
        from fastapi import FastAPI, HTTPException, Query
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("未安装 Web 依赖：pip install fastapi uvicorn（或 pip install -e '.[web]'）") from exc

    app = FastAPI(title="novel-drama-workbench", version=__version__)

    def _store(path: str | None) -> ProjectStore:
        """按请求参数定位工程。

        参数:
            path: 工程目录。

        返回:
            ProjectStore。

        异常:
            HTTPException: 未指定或未初始化。
        """
        root = path or project_root
        if not root:
            raise HTTPException(status_code=400, detail="缺少 path 参数（工程目录）")
        store = ProjectStore(root)
        if not store.exists():
            raise HTTPException(status_code=404, detail=f"工程不存在: {store.root}")
        return store

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

    @app.get("/api/project/info")
    def project_info(path: str | None = Query(default=None)) -> dict:
        """工程概览（作品、章节数、角色数）。"""
        store = _store(path)
        project = store.load_project()
        return {"root": str(store.root), "work": project["work"], "chapters": store.list_chapters(), "characters": len(store.load_characters()["characters"])}

    @app.get("/api/project/chapters")
    def chapters(path: str | None = Query(default=None)) -> list[dict]:
        """章节列表。"""
        return _store(path).list_chapters()

    @app.get("/api/project/chapter/{chapter_id}")
    def chapter(chapter_id: str, path: str | None = Query(default=None)) -> dict:
        """章节详情（含场景与台词）。"""
        try:
            return _store(path).load_chapter(chapter_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/project/timeline/{chapter_id}")
    def timeline(chapter_id: str, path: str | None = Query(default=None)) -> dict:
        """时间轴详情（未构建时 404）。"""
        store = _store(path)
        timeline_path = store.timeline_path(chapter_id)
        if not timeline_path.exists():
            raise HTTPException(status_code=404, detail="时间轴尚未构建")
        return __import__("json").loads(timeline_path.read_text(encoding="utf-8"))

    # ---------------- 台词校对页（P4）----------------
    # 读接口一律把 CLI 的 --json 原样透传，前端不做状态推导。

    def _respond(result):
        """把 CLI 结果转成 HTTP 响应。

        参数:
            result: `actions.CliResult`。

        返回:
            CLI 的 JSON 负载。

        异常:
            HTTPException: CLI 退出码非 0 时按映射返回状态码与中文说明。
        """
        if result.code != 0:
            raise HTTPException(status_code=result.status, detail=fail_body(result))
        return result.payload if result.payload is not None else {"text": result.text}

    def _cli(*argv: str):
        """按 `serve --config` 的同一份配置执行 CLI，避免界面与终端读不同配置。

        参数:
            argv: 不含 `--config` 的参数串。

        返回:
            `actions.CliResult`。
        """
        if config_path and "--config" not in argv:
            return run("--config", config_path, *argv)
        return run(*argv)

    @app.get("/api/ui/chapters")
    def ui_chapters(path: str = Query(...)):
        """章节目录（`chapter list --json`）。"""
        return _respond(_cli("--project", path, "--json", "chapter", "list"))

    @app.get("/api/ui/chapter")
    def ui_chapter(path: str = Query(...), chapter: str = Query(...)):
        """章节详情（含场景，`chapter show <id> --json`）。"""
        return _respond(_cli("--project", path, "--json", "chapter", "show", chapter))

    @app.get("/api/ui/lines")
    def ui_lines(
        path: str = Query(...),
        chapter: str = Query(...),
        status: str | None = Query(default=None),
    ) -> dict:
        """台词表（`line list --json`，状态与筛选全部由 CLI 计算）。

        `status` 支持逗号分隔的 `pending,stale,ready,failed,unknown`，与终端用法一致。
        """
        argv = ["--project", path, "--json", "line", "list", "--chapter", chapter]
        if status:
            argv += ["--status", status]
        return _respond(_cli(*argv))

    @app.get("/api/ui/line/diff")
    def ui_line_diff(path: str = Query(...), chapter: str = Query(...), line: str = Query(...)) -> dict:
        """单行参数差异（`line diff --json`）。"""
        return _respond(_cli("--project", path, "--json", "line", "diff", "--chapter", chapter, "--line", line))

    @app.get("/api/ui/line")
    def ui_line(path: str = Query(...), chapter: str = Query(...), line: str = Query(...)):
        """单行详情（`line list --line --json`），供队列完成后按行回填。"""
        return _respond(_cli("--project", path, "--json", "line", "list", "--chapter", chapter, "--line", line))

    @app.get("/api/ui/plan")
    def ui_plan(path: str = Query(...), chapter: str = Query(...)):
        """本次将生成多少行（`tts plan --json`）。"""
        return _respond(_cli("--project", path, "--json", "tts", "plan", "--chapter", chapter))

    @app.get("/api/ui/queue")
    def ui_queue(path: str = Query(...)):
        """队列快照（`queue list --json`）：轮询只打这个接口。"""
        return _respond(_cli("--project", path, "--json", "queue", "list"))

    @app.get("/api/ui/readiness")
    def ui_readiness() -> dict:
        """配音链是否可开工：任一已启用 provider 可用才算就绪。

        模型缺失时不静默降级，直接把原因回给界面，由界面禁用「开始」。
        """
        result = _cli("--json", "tts", "providers")
        if result.code != 0:
            return {"ready": False, "reason": f"读取 provider 状态失败：{result.message}", "providers": []}
        rows = (result.payload or {}).get("providers") or []
        usable = [row for row in rows if row.get("enabled") and row.get("available")]
        if usable:
            return {"ready": True, "reason": "", "providers": rows}
        blocked = [row for row in rows if row.get("enabled")]
        if blocked:
            reason = "；".join(
                f"{row['provider_id']} 不可用（{row.get('code') or '无原因'}：{row.get('detail')}）" for row in blocked
            )
        else:
            reason = "配置里没有启用的 TTS provider（检查 config 的 tts.providers）"
        return {"ready": False, "reason": f"{reason}。先运行 ndw models list / verify 确认权重就位", "providers": rows}

    @app.post("/api/ui/generate")
    def ui_generate(body: dict) -> dict:
        """入队并启动后台消费进程；请求立即返回，进度一律靠 `queue list` 反映。

        消费放在独立进程里（`tts run --wait`），避免阻塞请求线程，
        也避免在请求线程里重定向 stdout 与其它请求相互干扰。
        """
        path = body["path"]
        chapter = body["chapter"]
        # 前置检查：权重缺失/不可用时直接拒绝，不静默降级，也不启动一个必然失败的后台进程
        ready = ui_readiness()
        if not ready["ready"]:
            raise HTTPException(status_code=412, detail={"code": 4, "message": ready["reason"]})
        jobs = _respond(_cli("--project", path, "--json", "queue", "list")) or []
        active = [job for job in jobs if job.get("status") in ("queued", "running")]
        if active:
            # 接管已在执行的一批：since 取这批任务里最早的创建时间，避免把历史任务算进本次进度
            since = min((job.get("created_at") or "") for job in active) or now_iso()
            return {"started": False, "active": len(active), "since": since}

        log_dir = ProjectStore(path).root / ".ndw" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        since = now_iso()
        log_path = log_dir / f"web-tts-{since.replace(':', '').replace('-', '')}.log"
        src_dir = Path(__file__).resolve().parents[2]
        env = dict(os.environ)
        env["PYTHONPATH"] = str(src_dir) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        argv = [sys.executable, "-m", "novel_drama.cli", "--project", path]
        if config_path:
            argv += ["--config", config_path]
        argv += ["tts", "run", "--chapter", chapter, "--wait"]
        with log_path.open("wb") as log:
            process = subprocess.Popen(
                argv,
                cwd=str(src_dir),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                env=env,
                start_new_session=True,
            )
        return {"started": True, "pid": process.pid, "log": str(log_path), "since": since}

    @app.post("/api/ui/line/set")
    def ui_line_set(body: dict) -> dict:
        """改行参数（`line set`）；只传实际改动的字段。"""
        argv = ["--project", body["path"], "line", "set", "--chapter", body["chapter"], "--line", body["line"]]
        mapping = {
            "speaker": "--speaker",
            "kind": "--kind",
            "emotion": "--emotion",
            "emotion_intensity": "--emotion-intensity",
            "speed": "--speed",
            "pause_before_ms": "--pause-before",
            "pause_after_ms": "--pause-after",
            "text": "--text",
            "delivery_note": "--delivery-note",
        }
        for key, flag in mapping.items():
            if body.get(key) is not None:
                argv += [flag, str(body[key])]
        return _respond(_cli(*argv))

    @app.post("/api/ui/line/revert")
    def ui_line_revert(body: dict) -> dict:
        """回滚到上次生成时的参数（`line revert`）。"""
        return _respond(
            _cli("--project", body["path"], "--json", "line", "revert", "--chapter", body["chapter"], "--line", body["line"])
        )

    # ---------------- 全链路工作台（R14）----------------
    # 与语音页同一套纪律：界面不重算业务，全部转调 CLI --json。

    @app.get("/api/ui/setting")
    def ui_setting(path: str = Query(...)):
        """设定库全文（`setting show --json`）。"""
        return _respond(_cli("--project", path, "--json", "setting", "show"))

    @app.post("/api/ui/setting/set")
    def ui_setting_set(body: dict) -> dict:
        """修改设定库标量字段（`setting set`）。"""
        argv = ["--project", body["path"], "--json", "setting", "set"]
        fields = body.get("fields") or {}
        for key in SETTING_SCALARS:
            value = fields.get(key)
            if value is None or value == "":
                continue
            argv += ["--" + key.replace("_", "-"), str(value)]
        if len(argv) == 5:
            raise HTTPException(status_code=400, detail={"code": 2, "message": "没有要修改的字段"})
        return _respond(_cli(*argv))

    @app.post("/api/ui/setting/world")
    def ui_setting_world(body: dict) -> dict:
        """增补世界设定（`setting world`）。"""
        argv = ["--project", body["path"], "--json", "setting", "world"]
        for rule in body.get("rules") or []:
            if rule:
                argv += ["--add-rule", rule]
        for faction in body.get("factions") or []:
            if faction:
                argv += ["--add-faction", faction]
        if body.get("location"):
            argv += ["--add-location", body["location"]]
        if body.get("glossary"):
            argv += ["--add-glossary", body["glossary"]]
        return _respond(_cli(*argv))

    @app.get("/api/ui/outline")
    def ui_outline(path: str = Query(...)):
        """大纲（`outline show --json`）。"""
        return _respond(_cli("--project", path, "--json", "outline", "show"))

    @app.post("/api/ui/outline/generate")
    def ui_outline_generate(body: dict) -> dict:
        """生成大纲（`outline generate`）。"""
        argv = ["--project", body["path"], "--json", "outline", "generate"]
        for key, flag in (("acts", "--acts"), ("chapters_per_act", "--chapters-per-act"), ("beats_per_chapter", "--beats-per-chapter"), ("words", "--words")):
            if body.get(key) is not None:
                argv += [flag, str(body[key])]
        if body.get("dry_run"):
            argv.append("--dry-run")
        return _respond(_cli(*argv))

    @app.post("/api/ui/outline/apply")
    def ui_outline_apply(body: dict) -> dict:
        """大纲落地成章节（`outline apply`）。"""
        argv = ["--project", body["path"], "--json", "outline", "apply"]
        if body.get("dry_run"):
            argv.append("--dry-run")
        return _respond(_cli(*argv))

    @app.post("/api/ui/chapter/generate")
    def ui_chapter_generate(body: dict) -> dict:
        """生成章节正文（`chapter generate`）。"""
        argv = ["--project", body["path"], "--json", "chapter", "generate", "--chapter", body["chapter"]]
        if body.get("regen"):
            argv.append("--regen")
        if body.get("dry_run"):
            argv.append("--dry-run")
        return _respond(_cli(*argv))

    @app.post("/api/ui/chapter/save")
    def ui_chapter_save(body: dict) -> dict:
        """保存手工编辑后的章节正文。

        正文可能有上万字：拼进 argv 会撞命令行长度上限，还会出现在进程列表里，
        因此这里不转发 CLI，直接调用 `writing.editing.save_chapter_content`
        （与上方直接读 store 的只读接口是同一取舍）。

        参数:
            body: {path 工程目录, chapter 章节 ID, content 新正文}。

        返回:
            {chapter_id, word_count, revision, status, warnings}。

        异常:
            HTTPException: 正文为空返回 400，章节不存在返回 404。
        """
        store = _store(body.get("path"))
        try:
            return save_chapter_content(store, body["chapter"], body.get("content") or "")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/ui/chapter/edit")
    def ui_chapter_edit(body: dict) -> dict:
        """按提示词让 LLM 改写正文（默认预览，dry_run=false 才落盘）。

        参数:
            body: {path 工程目录, chapter 章节 ID, instruction 提示词,
                   range? {char_start, char_end}, dry_run? 默认 true}。

        返回:
            {chapter_id, mode, before, after, changed, word_count, revision,
             status, warnings, continuation_id?}。

        异常:
            HTTPException: 参数/正文/范围错误 400，章节不存在 404，模型异常 502。
        """
        store = _store(body.get("path"))
        raw_range = body.get("range")
        dry_run = body.get("dry_run")
        char_range = None
        if raw_range:
            if not isinstance(raw_range, dict):
                raise HTTPException(status_code=400, detail="range 需为 {char_start, char_end}")
            char_range = (raw_range.get("char_start"), raw_range.get("char_end"))
        try:
            return edit_chapter(
                store,
                load_config(config_path),
                body.get("chapter") or "",
                body.get("instruction") or "",
                char_range=char_range,
                dry_run=True if dry_run is None else bool(dry_run),
            )
        except AdapterError as exc:
            raise HTTPException(status_code=502, detail=exc.message) from exc
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/ui/chapter/summarize")
    def ui_chapter_summarize(body: dict) -> dict:
        """根据正文反向总结标题/摘要/大纲/节拍（默认预览，apply=true 才落盘）。

        参数:
            body: {path 工程目录, chapter 章节 ID, apply? 默认 false}。

        返回:
            预览: {chapter_id, preview, changes, replaced_beats}；
            应用: {applied, chapter_id, old_title, new_title, changed_fields,
                   replaced_beats, warnings}。

        异常:
            HTTPException: 正文为空 400，章节不存在 404，模型异常 502。
        """
        store = _store(body.get("path"))
        try:
            return summarize_chapter(
                store,
                load_config(config_path),
                body.get("chapter") or "",
                apply=bool(body.get("apply")),
            )
        except AdapterError as exc:
            raise HTTPException(status_code=502, detail=exc.message) from exc
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/ui/scene/split")
    def ui_scene_split(body: dict) -> dict:
        """切分场景（`scene split`）。"""
        argv = ["--project", body["path"], "--json", "scene", "split", "--chapter", body["chapter"]]
        if body.get("force"):
            argv.append("--force")
        return _respond(_cli(*argv))

    @app.post("/api/ui/line/split")
    def ui_line_split(body: dict) -> dict:
        """切分台词（`line split`）。"""
        argv = ["--project", body["path"], "--json", "line", "split", "--chapter", body["chapter"]]
        if body.get("force"):
            argv.append("--force")
        return _respond(_cli(*argv))

    @app.get("/api/ui/script")
    def ui_script(path: str = Query(...), chapter: str = Query(...)) -> dict:
        """导演标注文本与脚本文件状态（读落盘产物，不重算）。"""
        store = _store(path)
        markup_path = store.root / "scripts" / f"{chapter}.markup.txt"
        script_path = store.script_path(chapter)
        return {
            "markup_path": str(markup_path),
            "markup": markup_path.read_text(encoding="utf-8") if markup_path.exists() else "",
            "script_path": str(script_path),
            "has_script": script_path.exists(),
        }

    @app.post("/api/ui/script/suggest")
    def ui_script_suggest(body: dict) -> dict:
        """生成标注建议（`script suggest`），可同时应用。"""
        argv = ["--project", body["path"], "--json", "script", "suggest", "--chapter", body["chapter"]]
        if body.get("apply"):
            argv.append("--apply")
        if body.get("dry_run"):
            argv.append("--dry-run")
        return _respond(_cli(*argv))

    @app.get("/api/ui/media")
    def ui_media(path: str = Query(...), chapter: str = Query(...)) -> dict:
        """章节内三类媒体条目（从场景内嵌数据汇总，状态以 asset 为准）。

        图像/视频额外补只读的模板与画幅字段；单项解析失败只留空值，不让接口 500。
        """
        store = _store(path)
        chapter_data = store.load_chapter(chapter)
        config = load_config(config_path)
        project_settings = store.settings()
        try:
            manifest = comfy_media.load_manifest(comfy_media.templates_dir(config))
        except Exception:  # 清单缺失只影响展示，不影响其它字段
            manifest = {}

        def _template_for(item: dict, kind: str) -> str:
            """解析单个条目实际使用的模板显示名。

            参数:
                item: 关键图或视频条目。
                kind: image / video。

            返回:
                模板显示名（缺省回落到模板 ID）；解析失败返回空串。
            """
            try:
                if kind == "video":
                    i2v = item.get("mode") == "i2v"
                    template_kind = "video_i2v" if i2v else "video_t2v"
                    setting_key = "comfyui.templates.video_i2v" if i2v else "comfyui.templates.video_t2v"
                else:
                    template_kind = "image"
                    setting_key = "comfyui.templates.image"
                resolved = comfy_media.resolve_template(item.get("workflow_template_id") or config.get(setting_key), template_kind, manifest)
                entry = resolved.get("template") or {}
                return str(entry.get("display_name") or resolved.get("template_id") or "")
            except Exception:
                return ""

        def _aspect_for(item: dict, kind: str) -> dict:
            """解析单个条目的画幅与宽高。

            参数:
                item: 关键图或视频条目。
                kind: image / video。

            返回:
                `{"ratio","width","height"}`；解析失败时各字段为 None。
            """
            try:
                return comfy_media.resolve_aspect(item, project_settings, kind)
            except Exception:
                return {"ratio": None, "width": None, "height": None}

        rows = {"sfx": [], "image": [], "video": []}
        keys = {"sfx": "sfx_events", "image": "key_images", "video": "video_segments"}
        for scene in chapter_data.get("scenes") or []:
            for kind, key in keys.items():
                for item in scene.get(key) or []:
                    asset = item.get("asset") or {}
                    template = ""
                    aspect = {"ratio": None, "width": None, "height": None}
                    if kind in ("image", "video"):
                        template = _template_for(item, kind)
                        aspect = _aspect_for(item, kind)
                    rows[kind].append(
                        {
                            "id": item.get("id"),
                            "scene_id": scene.get("id"),
                            "index": item.get("index"),
                            "prompt": item.get("prompt") or "",
                            "kind": item.get("kind"),
                            "mode": item.get("mode"),
                            "duration_ms": item.get("duration_ms") or asset.get("actual_duration_ms"),
                            "status": asset.get("status") or item.get("status") or "pending",
                            "asset_id": asset.get("asset_id"),
                            "error": asset.get("error"),
                            "template": template or None,
                            "aspect_ratio": aspect.get("ratio"),
                            "width": aspect.get("width"),
                            "height": aspect.get("height"),
                        }
                    )
        return rows

    @app.post("/api/ui/media/run")
    def ui_media_run(body: dict) -> dict:
        """执行一类媒体生成（`media sfx|image|video run`）。"""
        kind = body["kind"]
        if kind not in ("sfx", "image", "video"):
            raise HTTPException(status_code=400, detail={"code": 2, "message": f"未知媒体类型: {kind}"})
        argv = ["--project", body["path"], "--json", kind, "run", "--chapter", body["chapter"]]
        if body.get("force"):
            argv.append("--force")
        if body.get("limit") is not None:
            argv += ["--limit", str(body["limit"])]
        return _respond(_cli(*argv))

    @app.post("/api/ui/produce")
    def ui_produce(body: dict) -> dict:
        """启动全链路编排（后台进程，进度靠 `run show` 反映）。"""
        path, chapter = body["path"], body["chapter"]
        argv = ["--project", path]
        if config_path:
            argv += ["--config", config_path]
        argv += ["produce", "--chapter", chapter]
        if body.get("from"):
            argv += ["--from", body["from"]]
        if body.get("to"):
            argv += ["--to", body["to"]]
        if body.get("force"):
            argv.append("--force")
        if body.get("keep_going"):
            argv.append("--keep-going")
        if body.get("no_resume"):
            argv.append("--no-resume")

        log_dir = ProjectStore(path).root / ".ndw" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"web-produce-{now_iso().replace(':', '').replace('-', '')}.log"
        src_dir = Path(__file__).resolve().parents[2]
        env = dict(os.environ)
        env["PYTHONPATH"] = str(src_dir) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        with log_path.open("wb") as log:
            process = subprocess.Popen(
                [sys.executable, "-u", "-m", "novel_drama.cli", *argv],
                cwd=str(src_dir),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                env=env,
                start_new_session=True,
            )
        return {"started": True, "pid": process.pid, "log": str(log_path)}

    @app.get("/api/ui/produce/latest")
    def ui_produce_latest(path: str = Query(...), run_id: str | None = Query(default=None)) -> dict:
        """最近一次（或指定）编排记录（`run show --json`）。"""
        argv = ["--project", path, "--json", "run", "show"]
        if run_id:
            argv += ["--run", run_id]
        result = _cli(*argv)
        if result.code != 0:
            return {"run": None, "message": result.message}
        return {"run": result.payload}

    @app.get("/api/ui/exports")
    def ui_exports(path: str = Query(...)) -> dict:
        """列成片产物：`renders/` 与 `exports/` 下的文件。"""
        from datetime import datetime

        root = _store(path).root
        files = []
        for sub in ("renders", "exports"):
            folder = root / sub
            if not folder.exists():
                continue
            for item in sorted(folder.rglob("*")):
                if not item.is_file():
                    continue
                stat = item.stat()
                files.append(
                    {
                        "rel": str(item.relative_to(root)),
                        "path": str(item),
                        "size": stat.st_size,
                        "mtime": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                    }
                )
        return {"root": str(root), "files": files}

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

    # ---------------- AI 生书创作向导（设定 / 人物 / 大纲检查）----------------

    def _temp_json(payload: dict) -> str:
        """把 JSON 负载写入临时文件，供 CLI 的 `--file` 参数使用。

        参数:
            payload: 待写入的 JSON 对象。

        返回:
            临时文件的绝对路径（调用方负责删除）。
        """
        import json as _json
        import tempfile

        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        with handle:
            _json.dump(payload, handle, ensure_ascii=False)
        return handle.name

    def _business_error(result):
        """把新接口的 CLI 失败结果转成中文说明 + 分级状态码。

        参数:
            result: `actions.CliResult`。

        返回:
            带 detail 的 HTTPException（409 引用冲突 / 404 不存在 / 422 校验失败）。

        异常:
            无。
        """
        body = fail_body(result)
        payload = result.payload if isinstance(result.payload, dict) else {}
        raw_message = str(payload.get("message") or body.get("message") or "")
        message = re.sub(r"^\[[A-Z_]+\]\s*", "", raw_message)
        body = {**body, "message": message}
        if result.code == 1:
            detail = dict(body)
            if payload.get("chapters"):
                detail["chapters"] = payload["chapters"]
            return HTTPException(status_code=409, detail=detail)
        if result.code == 4 and "不存在" in message:
            return HTTPException(status_code=404, detail=body)
        if result.code in (5, 6):
            return HTTPException(status_code=422, detail=body)
        return HTTPException(status_code=result.status, detail=body)

    def _respond_business(result):
        """成功返回 JSON 负载，失败按业务语义映射状态码。

        参数:
            result: `actions.CliResult`。

        返回:
            CLI 的 JSON 负载。

        异常:
            HTTPException: 退出码非 0。
        """
        if result.code != 0:
            raise _business_error(result)
        return result.payload if result.payload is not None else {"text": result.text}

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

    @app.post("/api/ui/setting/generate")
    def ui_setting_generate(body: dict) -> dict:
        """预览 AI 生成的设定（`setting generate --dry-run`，不落盘）。

        参数:
            body: `{path, section?, brief?, target_id?, step?, overwrite?}`；
            section 缺省时保持旧的 step 行为。

        异常:
            HTTPException 400: section / step 缺失或非法。
        """
        path = body.get("path")
        _store(path)
        section = str(body.get("section") or "").strip()
        if section:
            if section not in ("premise", "style", "world", "character"):
                raise HTTPException(
                    status_code=400,
                    detail={"code": 2, "message": "section 必须是 premise / style / world / character"},
                )
            argv = ["--project", path, "--json", "setting", "generate", "--section", section, "--dry-run"]
            if body.get("brief"):
                argv += ["--brief", str(body["brief"])]
            if body.get("target_id"):
                argv += ["--target-id", str(body["target_id"])]
            return _respond_business(_cli(*argv))
        step = str(body.get("step") or "").strip()
        if step not in ("world", "premise", "style"):
            raise HTTPException(status_code=400, detail={"code": 2, "message": "step 必须是 world / premise / style"})
        argv = ["--project", path, "--json", "setting", "generate", "--step", step, "--dry-run"]
        if body.get("overwrite"):
            argv.append("--overwrite")
        return _respond(_cli(*argv))

    @app.post("/api/ui/setting/apply")
    def ui_setting_apply(body: dict) -> dict:
        """把前端确认后的字段合并写入设定库分区（`setting apply --file`）。

        参数:
            body: `{path, section: world|premise|style, fields: {...}}`。

        异常:
            HTTPException 400: section / fields 不合法。
        """
        path = body.get("path")
        _store(path)
        section = str(body.get("section") or "").strip()
        if section not in ("world", "premise", "style"):
            raise HTTPException(status_code=400, detail={"code": 2, "message": "section 必须是 world / premise / style"})
        fields = body.get("fields")
        if not isinstance(fields, dict) or not fields:
            raise HTTPException(status_code=400, detail={"code": 2, "message": "fields 必须是非空对象"})
        tmp = _temp_json({"fields": fields})
        try:
            return _respond(_cli("--project", path, "--json", "setting", "apply", "--section", section, "--file", tmp))
        finally:
            Path(tmp).unlink(missing_ok=True)

    @app.post("/api/ui/setting/polish")
    def ui_setting_polish(body: dict) -> dict:
        """润色单个字段（`setting polish`，不写盘）。

        参数:
            body: `{path, field, text?, context?, character?}`；text 缺省读取当前值。

        异常:
            HTTPException 400: field 缺失或不受支持。
        """
        path = body.get("path")
        _store(path)
        field = str(body.get("field") or "").strip()
        if not field:
            raise HTTPException(status_code=400, detail={"code": 2, "message": "缺少 field 参数"})
        argv = ["--project", path, "--json", "setting", "polish", "--field", field]
        if body.get("text") is not None:
            argv += ["--text", str(body["text"])]
        if body.get("context"):
            argv += ["--context", str(body["context"])]
        if body.get("character"):
            argv += ["--character", str(body["character"])]
        return _respond(_cli(*argv))

    @app.get("/api/ui/characters")
    def ui_characters(path: str = Query(...)) -> dict:
        """人物表（`character list --json`）。"""
        _store(path)
        payload = _respond(_cli("--project", path, "--json", "character", "list"))
        return {"characters": payload or []}

    @app.post("/api/ui/characters")
    def ui_characters_add(body: dict) -> dict:
        """批量追加人物（`character apply --file`）。

        参数:
            body: `{path, characters: [...]}`。

        异常:
            HTTPException 400: characters 不是非空数组。
        """
        path = body.get("path")
        _store(path)
        rows = body.get("characters")
        if not isinstance(rows, list) or not rows:
            raise HTTPException(status_code=400, detail={"code": 2, "message": "characters 必须是非空数组"})
        tmp = _temp_json({"characters": rows})
        try:
            return _respond(_cli("--project", path, "--json", "character", "apply", "--file", tmp))
        finally:
            Path(tmp).unlink(missing_ok=True)

    @app.post("/api/ui/characters/generate")
    def ui_characters_generate(body: dict) -> dict:
        """生成主角与主要配角（`character generate --dry-run`，预览不落盘）。

        参数:
            body: `{path, count?, brief?}`；count 为期望生成的总人数，brief 为补充要求。

        返回:
            `{characters: [...], applied: false}`，由前端回填后用户确认再 POST 追加。
        """
        path = body.get("path")
        _store(path)
        argv = ["--project", path, "--json", "character", "generate", "--dry-run"]
        if body.get("brief"):
            argv += ["--brief", str(body["brief"])]
        if body.get("count") is not None:
            argv += ["--count", str(body["count"])]
        return _respond(_cli(*argv))

    @app.post("/api/ui/characters/image")
    def ui_characters_image(body: dict) -> dict:
        """生成角色特写 / 三视图（`character image`）。

        参数:
            body: `{path, id, kind: closeup|turnaround, force?}`。

        返回:
            `{character_id, kind, image_asset, created}`。

        异常:
            HTTPException 400: 缺少 id 或 kind 非法。
        """
        path = body.get("path")
        _store(path)
        character_id = body.get("id") or body.get("character")
        if not character_id:
            raise HTTPException(status_code=400, detail={"code": 2, "message": "缺少 id 参数"})
        kind = str(body.get("kind") or "").strip()
        if kind not in ("fullbody", "closeup", "turnaround"):
            raise HTTPException(status_code=400, detail={"code": 2, "message": "kind 必须是 fullbody / closeup / turnaround"})
        argv = ["--project", path, "--json", "character", "image", "--id", str(character_id), "--kind", kind]
        if body.get("force"):
            argv.append("--force")
        return _respond_business(_cli(*argv))

    @app.patch("/api/ui/characters")
    def ui_characters_update(body: dict) -> dict:
        """按 ID 更新人物字段（`character update`）。

        参数:
            body: `{path, id, name?, role?, gender?, age_range?, personality?, background?,
            aliases?, appearance_prompt?, negative_prompt?, situation?, relationships?}`。

        异常:
            HTTPException 400: 缺少 id。
        """
        path = body.get("path")
        _store(path)
        character_id = body.get("id") or body.get("character")
        if not character_id:
            raise HTTPException(status_code=400, detail={"code": 2, "message": "缺少 id 参数"})
        argv = ["--project", path, "--json", "character", "update", "--id", str(character_id)]
        for key, flag in (
            ("name", "--name"),
            ("role", "--role"),
            ("gender", "--gender"),
            ("age_range", "--age-range"),
            ("personality", "--personality"),
            ("background", "--background"),
            ("aliases", "--aliases"),
            ("appearance_prompt", "--appearance-prompt"),
            ("negative_prompt", "--negative-prompt"),
            ("situation", "--situation"),
        ):
            value = body.get(key)
            if value is None:
                continue
            if isinstance(value, list):
                value = ",".join(str(item) for item in value)
            argv += [flag, str(value)]
        relationships = body.get("relationships") or {}
        if isinstance(relationships, dict):
            relationships = [f"{key}={value}" for key, value in relationships.items()]
        for item in relationships:
            argv += ["--relationship", str(item)]
        return _respond(_cli(*argv))

    @app.post("/api/ui/characters/delete")
    def ui_characters_delete(body: dict) -> dict:
        """按 ID 删除人物（`character remove`，旁白会被拒绝）。

        参数:
            body: `{path, id}`。

        异常:
            HTTPException 400: 缺少 id。
        """
        path = body.get("path")
        _store(path)
        character_id = body.get("id") or body.get("character")
        if not character_id:
            raise HTTPException(status_code=400, detail={"code": 2, "message": "缺少 id 参数"})
        return _respond(_cli("--project", path, "--json", "character", "remove", "--id", str(character_id)))

    @app.get("/api/ui/outline/check")
    def ui_outline_check(path: str = Query(...)) -> dict:
        """黄金三章检查（`outline check --json`），不阻断流程。"""
        _store(path)
        return _respond(_cli("--project", path, "--json", "outline", "check"))

    @app.post("/api/ui/outline/bind")
    def ui_outline_bind(body: dict) -> dict:
        """更新章节的世界 / POV 绑定（`outline bind`）。

        参数:
            body: `{path, chapter, world_ids?: [...], pov_character_id?}`。

        异常:
            HTTPException 400: 缺少 chapter。
            HTTPException 422: 世界或角色 ID 不存在。
        """
        path = body.get("path")
        _store(path)
        chapter = body.get("chapter")
        if not chapter:
            raise HTTPException(status_code=400, detail={"code": 2, "message": "缺少 chapter 参数"})
        argv = ["--project", path, "--json", "outline", "bind", "--chapter", str(chapter)]
        if body.get("world_ids") is not None:
            world_ids = body["world_ids"]
            if isinstance(world_ids, list):
                world_ids = ",".join(str(item) for item in world_ids)
            argv += ["--worlds", str(world_ids)]
        if body.get("pov_character_id") is not None:
            argv += ["--pov", str(body["pov_character_id"])]
        return _respond_business(_cli(*argv))

    # ---------------- 世界条目（多世界 / 封面图）----------------

    @app.get("/api/ui/worlds")
    def ui_worlds(path: str = Query(...)) -> dict:
        """世界条目列表与人物数量（`world list` / `character list`）。

        参数:
            path: 工程目录。

        返回:
            `{worlds, count, characters}`；characters 供前端流程条展示人物数量。
        """
        _store(path)
        payload = _respond_business(_cli("--project", path, "--json", "world", "list")) or {}
        characters = _respond_business(_cli("--project", path, "--json", "character", "list")) or []
        return {
            "worlds": payload.get("worlds") or [],
            "count": payload.get("count") or 0,
            "characters": len(characters) if isinstance(characters, list) else 0,
        }

    @app.post("/api/ui/worlds")
    def ui_worlds_apply(body: dict) -> dict:
        """写入世界条目（`world apply --file`，带 id 且存在则更新）。

        参数:
            body: `{path, world?: {...}, worlds?: [...]}`。

        异常:
            HTTPException 400: 既没有 world 也没有 worlds。
        """
        path = body.get("path")
        _store(path)
        payload: dict = {}
        if isinstance(body.get("world"), dict):
            payload["world"] = body["world"]
        if isinstance(body.get("worlds"), list):
            payload["worlds"] = body["worlds"]
        if not payload:
            raise HTTPException(status_code=400, detail={"code": 2, "message": "world 或 worlds 至少给一个"})
        tmp = _temp_json(payload)
        try:
            return _respond_business(_cli("--project", path, "--json", "world", "apply", "--file", tmp))
        finally:
            Path(tmp).unlink(missing_ok=True)

    @app.post("/api/ui/worlds/delete")
    def ui_worlds_delete(body: dict) -> dict:
        """删除世界条目（`world remove`）；被引用时返回 409 与引用章节列表。

        参数:
            body: `{path, id}`。

        异常:
            HTTPException 400: 缺少 id。
            HTTPException 409: 仍被大纲章节引用。
        """
        path = body.get("path")
        _store(path)
        world_id = body.get("id")
        if not world_id:
            raise HTTPException(status_code=400, detail={"code": 2, "message": "缺少 id 参数"})
        return _respond_business(_cli("--project", path, "--json", "world", "remove", "--id", str(world_id)))

    @app.post("/api/ui/worlds/generate")
    def ui_worlds_generate(body: dict) -> dict:
        """预览 AI 生成的世界条目（`world generate --dry-run`，不落盘）。

        参数:
            body: `{path, brief?, count?}`。

        返回:
            `{worlds, count, saved: false}`。
        """
        path = body.get("path")
        _store(path)
        argv = ["--project", path, "--json", "world", "generate", "--dry-run"]
        if body.get("brief"):
            argv += ["--brief", str(body["brief"])]
        if body.get("count") is not None:
            argv += ["--count", str(body["count"])]
        return _respond_business(_cli(*argv))

    @app.post("/api/ui/worlds/image")
    def ui_worlds_image(body: dict) -> dict:
        """生成世界封面（`world image`）。

        参数:
            body: `{path, id, force?}`。

        异常:
            HTTPException 400: 缺少 id。
        """
        path = body.get("path")
        _store(path)
        world_id = body.get("id")
        if not world_id:
            raise HTTPException(status_code=400, detail={"code": 2, "message": "缺少 id 参数"})
        argv = ["--project", path, "--json", "world", "image", "--id", str(world_id)]
        if body.get("force"):
            argv.append("--force")
        return _respond_business(_cli(*argv))

    # 模型清单 / 下载 / ComfyUI 自检（独立模块，避免本文件继续膨胀）
    register_models_routes(app, config_path)

    if STATIC_DIR.exists():
        from fastapi.responses import FileResponse
        from fastapi.staticfiles import StaticFiles

        class NoCacheStaticFiles(StaticFiles):
            """静态资源禁用强缓存。

            本地工作台改完前端只需刷新页面即可生效，避免浏览器按启发式缓存
            继续使用旧 JS，导致改了代码但界面没变化。
            """

            def file_response(self, *args, **kwargs):
                """在原始静态响应上补一条 no-cache。"""
                response = super().file_response(*args, **kwargs)
                response.headers["Cache-Control"] = "no-cache"
                return response

        app.mount("/static", NoCacheStaticFiles(directory=STATIC_DIR), name="static")

        @app.get("/")
        def index() -> FileResponse:
            """台词校对页。"""
            return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    return app


def serve(host: str = "127.0.0.1", port: int = 8765, project_root: str | None = None, config_path: str | None = None) -> None:
    """启动本地 Web 服务。

    参数:
        host: 监听地址（默认仅本机）。
        port: 端口。
        project_root: 默认工程目录。
        config_path: 本机配置文件路径（透传给后台生成进程）。
    """
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("未安装 uvicorn：pip install uvicorn") from exc
    uvicorn.run(create_app(project_root, config_path), host=host, port=port)


def default_project_path(value: str | None) -> Path | None:
    """把可选的路径字符串转为 Path（供测试与外部调用）。"""
    return Path(value).expanduser() if value else None
