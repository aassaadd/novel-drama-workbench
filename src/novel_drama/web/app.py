"""展示层 HTTP 骨架（FastAPI）。

页面只消费 CLI 的输出：所有读写都经 `web/actions.run` 调 `cli.main`，
不在这里重算行状态，也不复制业务规则。
接口统一以 `path` 查询参数指定工程目录，避免引入工程注册服务。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from fastapi import HTTPException

from .. import __version__
from ..adapters.base import AdapterError
from ..adapters.writing import OmlxWritingAdapter
from ..cli_line import chapter_consistency
from ..config import DEFAULT_UI_CONFIG_PATH, load_config
from ..media import comfy_media
from ..media.references import collect_references
from ..models.ids import now_iso
from ..store import ProjectStore, list_workspaces
from ..store.project_store import WORKSPACE_INDEX
from ..store.queue_store import QueueStore
from ..writing.editing import edit_chapter, save_chapter_content
from ..writing.summarize import apply_summary, summarize_chapter
from .actions import fail_body, run
from .models_api import register_models_routes, resolve_llm_settings
from .voice_tools_api import register_voice_tools_routes
from .align_api import register_align_routes
from .character_desc_api import register_character_desc_routes
from .continue_api import register_continue_routes
from .outline_edit_api import register_outline_edit_routes
from .stream_api import register_stream_routes
from .bgm_api import register_bgm_routes
from .sfx_api import register_sfx_routes

#: 唯一前端目录（仓库根 web-next/）；直接挂到根路径，旧 static 界面已废弃
WEB_NEXT_DIR = Path(__file__).resolve().parents[3] / "web-next"

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

#: 可编辑键：分组字段 + 设置页已有专属控件的键（同键不复用两次）
CONFIG_EDITABLE = tuple(
    item[0] for _group, items in CONFIG_FIELD_GROUPS for item in items
) + (
    "omlx.endpoint",
    "omlx.model",
    "omlx.api_key",
    "omlx.temperature",
    "omlx.max_tokens",
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


def _is_tts_process(pid: int) -> bool:
    """判断 pid 是否为本工程启动的配音消费进程。

    说明:
        pid 文件可能在进程自然退出后残留；系统复用 pid 时直接 killpg 会误杀无关进程，
        因此先用 ps 确认命令行里确实是 `novel_drama.cli ... tts run` 或 `tts batch`。

    参数:
        pid: 待确认的进程号。

    返回:
        True 表示可以安全终止。
    """
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    command = (result.stdout or "").strip()
    return "novel_drama.cli" in command and ("tts run" in command or "tts batch" in command)


#: 本次 Web 服务启动过的配音批次所属工程；服务退出时逐个回收
_ACTIVE_TTS_PROJECTS: set[str] = set()


def _stop_tts_project(path: str) -> dict:
    """终止某工程的后台配音批次，并把未完成任务标记为已取消。

    参数:
        path: 工程目录。

    返回:
        {stopped 是否确实停下, killed 是否终止了进程, canceled 被取消的任务数}。
    """
    store = ProjectStore(path)
    killed = False
    # 兼容两种入口：逐行/队列合成写 web-tts.pid，整本批量写 web-tts-batch.pid
    for name in ("web-tts.pid", "web-tts-batch.pid"):
        pid_file = store.root / ".ndw" / name
        if not pid_file.is_file():
            continue
        pid = 0
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
        except ValueError:
            pid = 0
        if pid and _is_tts_process(pid):
            try:
                # 配音 CLI 用 start_new_session 独立进程组，killpg 会同时打到常驻 worker
                os.killpg(os.getpgid(pid), signal.SIGTERM)
                killed = True
            except (ProcessLookupError, PermissionError):
                killed = False
            # 进程收到 SIGTERM 立刻退出；稍等再改队列，避免和它的写入互相覆盖
            for _ in range(20):
                try:
                    os.kill(pid, 0)
                except (ProcessLookupError, PermissionError):
                    break
                time.sleep(0.1)
        pid_file.unlink(missing_ok=True)
    queue = QueueStore(store)
    jobs = queue.load()
    canceled = 0
    for job in jobs:
        if job.get("status") in ("queued", "running"):
            job["status"] = "canceled"
            job["finished_at"] = now_iso()
            job["error"] = {"code": "CANCELED", "message": "用户停止生成"}
            canceled += 1
    if canceled:
        queue.save(jobs)
    return {"stopped": killed or canceled > 0, "killed": killed, "canceled": canceled}


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
            "max_tokens": omlx.get("max_tokens"),
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


#: 重总结预览缓存：key → {content_hash, preview, rounds}；只在内存里，进程重启即失效
_SUMMARY_PREVIEWS: dict[str, dict] = {}
#: 缓存条数上限，超出按写入顺序淘汰最早的一条
_SUMMARY_PREVIEW_LIMIT = 64


def _summary_cache_key(store: ProjectStore, chapter_id: str) -> str:
    """生成重总结预览缓存键。

    参数:
        store: 工程。
        chapter_id: 章节 ID。

    返回:
        「工程绝对路径::章节 ID」字符串，保证不同工程互不覆盖。
    """
    return f"{store.root.resolve()}::{chapter_id}"


def _summary_content_hash(content: str) -> str:
    """计算正文哈希，用于判断预览生成后正文是否被改动。

    参数:
        content: 章节正文。

    返回:
        sha1 十六进制字符串。
    """
    return hashlib.sha1(content.encode("utf-8")).hexdigest()


def _remember_summary(store: ProjectStore, chapter_id: str, content: str, result: dict) -> None:
    """缓存一份重总结预览，供「应用」时直接复用，避免重复跑模型。

    参数:
        store: 工程。
        chapter_id: 章节 ID。
        content: 生成预览时的正文（用于校验是否过期）。
        result: `summarize_chapter(apply=False)` 的返回。
    """
    _SUMMARY_PREVIEWS[_summary_cache_key(store, chapter_id)] = {
        "content_hash": _summary_content_hash(content),
        "preview": result["preview"],
        "rounds": int(result.get("rounds") or 1),
    }
    while len(_SUMMARY_PREVIEWS) > _SUMMARY_PREVIEW_LIMIT:
        _SUMMARY_PREVIEWS.pop(next(iter(_SUMMARY_PREVIEWS)), None)


def _cached_summary(store: ProjectStore, chapter_id: str, content: str) -> dict | None:
    """读取未过期的重总结预览。

    参数:
        store: 工程。
        chapter_id: 章节 ID。
        content: 当前正文。

    返回:
        命中且正文未变时返回缓存项，否则 None。
    """
    cached = _SUMMARY_PREVIEWS.get(_summary_cache_key(store, chapter_id))
    if not cached or cached["content_hash"] != _summary_content_hash(content):
        return None
    return cached


def _voice_binding_index(voices: list[dict]) -> dict:
    """建立「绑定值 → 音色 ID」索引，兼容旧字段、变体 ID 与旧版内置名称。

    参数:
        voices: 工程 `voices.json` 里的音色列表。

    返回:
        字典：音色 ID / 变体 ID / 音色名称 / 旧版内置名称 都映射到音色 ID。
    """
    from ..tts.builtin_voices import LEGACY_NAME_MAP

    index: dict[str, str] = {}
    names: dict[str, str] = {}
    for voice in voices:
        voice_id = str(voice.get("id") or "")
        if not voice_id:
            continue
        index[voice_id] = voice_id
        name = str(voice.get("name") or "")
        if name:
            names[name] = voice_id
            index[name] = voice_id
        for variant in voice.get("variants") or []:
            variant_id = str(variant.get("id") or "")
            if variant_id:
                index[variant_id] = voice_id
    # 旧版内置音色重命名后，旧名称仍可能留在 characters.json 里
    for old_name, new_name in LEGACY_NAME_MAP.items():
        target = names.get(new_name)
        if target:
            index.setdefault(old_name, target)
    return index


def _resolve_voice_binding(binding: dict | None, index: dict) -> str | None:
    """把人物绑定解析成音色库里的音色 ID。

    参数:
        binding: `character.voice_binding`。
        index: `_voice_binding_index` 生成的索引。

    返回:
        匹配到的音色 ID；无法解析时返回 None。
    """
    if not binding:
        return None
    for key in ("voice_library_id", "voice_id", "default_variant_id"):
        raw = binding.get(key)
        if not raw:
            continue
        resolved = index.get(str(raw))
        if resolved:
            return resolved
    return None


def _export_preflight(store: ProjectStore, chapter_id: str) -> dict:
    """导出前检查（只读）：缺配音、音色编排提示、时间轴状态。

    参数:
        store: 工程读写入口。
        chapter_id: 章节 ID。

    返回:
        `{ok, issues, lines_total, ready, missing, missing_total, timeline_exists}`；
        issues 为 `{code, level, message, line_id?}`，level=error 表示建议先修。
    """
    from ..store.voice_cast import check_cast

    chapter = store.load_chapter(chapter_id)
    characters = {item["id"]: item for item in store.load_characters().get("characters") or []}
    lines = [line for scene in chapter.get("scenes") or [] for line in scene.get("lines") or []]
    missing = [
        {
            "line_id": line.get("id"),
            "speaker": (characters.get(line.get("speaker_id")) or {}).get("name") or line.get("speaker_id"),
            "text": line.get("text"),
        }
        for line in lines
        if not (line.get("tts") or {}).get("asset_id")
    ]
    timeline_exists = store.timeline_path(chapter_id).exists()
    issues: list[dict] = []
    if not timeline_exists:
        issues.append({"code": "timeline-missing", "level": "warn", "message": "时间轴尚未构建（导出时会自动重建）"})
    if missing:
        issues.append(
            {
                "code": "missing-audio",
                "level": "warn",
                "message": f"{len(missing)} 行还没有配音，导出包里对应行没有音频",
            }
        )
    try:
        findings = check_cast(store, chapter_id).get("findings") or []
    except Exception:  # 音色检查失败不应阻断导出前的其他信息
        findings = []
    for item in findings[:20]:
        issues.append(
            {
                "code": item.get("code"),
                "level": item.get("level") or "warn",
                "message": item.get("message"),
                "line_id": item.get("line_id"),
            }
        )
    return {
        "ok": not any(item["level"] == "error" for item in issues),
        "issues": issues,
        "lines_total": len(lines),
        "ready": len(lines) - len(missing),
        "missing": missing[:100],
        "missing_total": len(missing),
        "timeline_exists": timeline_exists,
    }


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

    @app.on_event("shutdown")
    def _shutdown_tts_batches() -> None:
        """Web 服务退出时回收本次启动的配音批次，避免留下无主 CLI/worker 进程。"""
        for root in list(_ACTIVE_TTS_PROJECTS):
            try:
                _stop_tts_project(root)
            except Exception:  # noqa: BLE001, S110 - 退出钩子不能抛错
                pass
            _ACTIVE_TTS_PROJECTS.discard(root)

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

    @app.post("/api/ui/chapter/import")
    def import_chapters(body: dict) -> dict:
        """把前端上传的纯文本按标题切章导入。

        参数:
            body: `{path, text, pattern?, on_duplicate?}`；`on_duplicate` 取值 skip/replace/new。

        返回:
            CLI `chapter import` 的 JSON 结果。

        异常:
            HTTPException 400: 正文为空。
        """
        path = body.get("path")
        text = str(body.get("text") or "")
        if not text.strip():
            raise HTTPException(status_code=400, detail={"code": 2, "message": "导入内容为空"})
        store = _store(path)
        pattern = str(body.get("pattern") or "").strip()
        on_duplicate = str(body.get("on_duplicate") or "skip")
        if on_duplicate not in {"skip", "replace", "new"}:
            raise HTTPException(status_code=400, detail={"code": 2, "message": "on_duplicate 只能是 skip/replace/new"})
        with tempfile.NamedTemporaryFile("w", suffix=".md", encoding="utf-8", delete=False) as handle:
            handle.write(text)
            temp_path = handle.name
        try:
            argv = ["--project", str(store.root), "--json", "chapter", "import", temp_path, "--on-duplicate", on_duplicate]
            if pattern:
                argv += ["--pattern", pattern]
            return _respond(_cli(*argv))
        finally:
            Path(temp_path).unlink(missing_ok=True)

    @app.post("/api/ui/lines/replace")
    def replace_line_text(body: dict) -> dict:
        """在本章台词里做查找替换（逐行走 `line edit`，保证正文同步）。

        参数:
            body: `{path, chapter, find, replace, whole?, case_sensitive?}`。

        返回:
            `{replaced, failed, total_matched}`。

        异常:
            HTTPException 400: find 为空。
        """
        path = body.get("path")
        chapter_id = body.get("chapter")
        find = str(body.get("find") or "")
        if not find:
            raise HTTPException(status_code=400, detail={"code": 2, "message": "查找内容不能为空"})
        replacement = str(body.get("replace") or "")
        store = _store(path)
        payload = store.load_chapter(chapter_id)
        replaced = 0
        failed = 0
        matched = 0
        for scene in payload.get("scenes") or []:
            for line in scene.get("lines") or []:
                text = str(line.get("text") or "")
                if find not in text:
                    continue
                matched += 1
                new_text = text.replace(find, replacement)
                result = _cli(
                    "--project", str(store.root), "--json",
                    "line", "edit", "--chapter", chapter_id, "--line", line.get("id"), "--text", new_text,
                )
                if result.code == 0:
                    replaced += 1
                else:
                    failed += 1
        return {"replaced": replaced, "failed": failed, "total_matched": matched}

    @app.post("/api/ui/timeline/save")
    def ui_timeline_save(body: dict) -> dict:
        """保存页面上调整过的时间轴（片段平移 / 淡入淡出）。

        写回 `timelines/<chapter>.timeline.json`；后续混音与导出直接消费该文件。

        参数:
            body: `{path, chapter, timeline}`，timeline 需含 duration_ms 与 tracks。

        返回:
            `{saved, duration_ms, tracks}`。

        异常:
            HTTPException 400: 时间轴结构不合法。
        """
        store = _store(body["path"])
        chapter_id = body["chapter"]
        data = body.get("timeline") or {}
        tracks = data.get("tracks")
        if not isinstance(tracks, list):
            raise HTTPException(status_code=400, detail={"code": 4, "message": "时间轴缺少 tracks"})
        max_end = 0
        for track in tracks:
            for item in track.get("items") or []:
                try:
                    start = int(item.get("start_ms"))
                    end = int(item.get("end_ms"))
                    fade_in = max(0, int(item.get("fade_in_ms") or 0))
                    fade_out = max(0, int(item.get("fade_out_ms") or 0))
                except (TypeError, ValueError):
                    raise HTTPException(
                        status_code=400,
                        detail={"code": 4, "message": "片段起止或淡入淡出必须是整数毫秒"},
                    )
                if start < 0 or end <= start:
                    raise HTTPException(status_code=400, detail={"code": 4, "message": "片段结束时间必须大于开始时间"})
                # 淡入淡出超过片段时长时按片段长度截断（构建器会给很短的音效配长淡入淡出，
                # 混音侧同样按片段长度收敛，这里不因此拒绝整份时间轴）
                length = end - start
                if fade_in > length:
                    fade_in = length
                if fade_in + fade_out > length:
                    fade_out = max(0, length - fade_in)
                item["start_ms"] = start
                item["end_ms"] = end
                item["fade_in_ms"] = fade_in
                item["fade_out_ms"] = fade_out
                max_end = max(max_end, end)
        data["duration_ms"] = max(int(data.get("duration_ms") or 0), max_end)
        path = store.timeline_path(chapter_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"saved": True, "duration_ms": data["duration_ms"], "tracks": len(tracks)}

    @app.post("/api/ui/timeline/build")
    def build_timeline(body: dict) -> dict:
        """构建（或重建）某章时间轴并返回结果。

        参数:
            body: `{path, chapter}`。

        返回:
            时间轴 JSON（与 `GET /api/project/timeline/{chapter}` 相同结构）。
        """
        store = _store(body.get("path"))
        chapter_id = body.get("chapter")
        result = _cli("--project", str(store.root), "--json", "timeline", "build", "--chapter", chapter_id)
        if result.code != 0:
            raise HTTPException(status_code=500, detail={"code": result.code, "message": result.text.strip() or "时间轴构建失败"})
        timeline_path = store.timeline_path(chapter_id)
        if not timeline_path.exists():
            raise HTTPException(status_code=500, detail={"code": 3, "message": "时间轴构建后没有产物"})
        return json.loads(timeline_path.read_text(encoding="utf-8"))

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

    @app.post("/api/ui/chapter/rename")
    def ui_chapter_rename(body: dict) -> dict:
        """重命名章节。

        参数:
            body: `{path, chapter, title}`。

        返回:
            `{chapter_id, title}`。

        异常:
            HTTPException 400: 标题为空；404: 章节不存在。
        """
        title = str(body.get("title") or "").strip()
        if not title:
            raise HTTPException(status_code=400, detail="章节标题不能为空")
        store = _store(body.get("path"))
        chapter_id = body.get("chapter") or ""
        try:
            chapter = store.load_chapter(chapter_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        chapter["title"] = title
        store.save_chapter(chapter)
        return {"chapter_id": chapter_id, "title": title}

    @app.post("/api/ui/chapter/delete")
    def ui_chapter_delete(body: dict) -> dict:
        """删除章节文件与其时间轴/脚本（保留已生成的音频资产）。

        参数:
            body: `{path, chapter}`。

        返回:
            `{ok, chapter_id}`。

        异常:
            HTTPException 400: 只剩一个章节；404: 章节不存在。
        """
        store = _store(body.get("path"))
        chapter_id = body.get("chapter") or ""
        if len(store.list_chapters()) <= 1:
            raise HTTPException(status_code=400, detail="至少保留一个章节")
        path = store.chapter_path(chapter_id)
        if not path.exists():
            raise HTTPException(status_code=404, detail="章节不存在")
        path.unlink()
        store.timeline_path(chapter_id).unlink(missing_ok=True)
        store.script_path(chapter_id).unlink(missing_ok=True)
        return {"ok": True, "chapter_id": chapter_id}

    @app.post("/api/ui/chapter/mix")
    def ui_chapter_mix(body: dict) -> dict:
        """按时间轴执行本章混音（分轨 + 混音 + 响度规整）。

        参数:
            body: `{path, chapter}`。

        返回:
            CLI 的 JSON 结果（含 mix 路径与时长）。

        异常:
            HTTPException 500: 混音失败。
        """
        from .actions import run

        store = _store(body.get("path"))
        chapter_id = body.get("chapter") or ""
        try:
            result = run(
                "--config", config_path, "--project", str(store.root),
                "--json", "mix", "--chapter", chapter_id, "--render",
            )
        except Exception as exc:  # noqa: BLE001 - 统一转成 500
            raise HTTPException(status_code=500, detail=f"混音失败：{exc}") from exc
        if result.code != 0:
            raise HTTPException(status_code=500, detail=f"混音失败：{result.stderr or result.text}")
        return result.payload or {}

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

    @app.get("/api/ui/styles")
    def ui_styles():
        """固定画面风格预设（写实 / 动漫 / 古风等），图片与视频共用。"""
        from ..media.styles import load_styles

        cfg = load_config(config_path) if config_path else load_config()
        return {"styles": load_styles(comfy_media.templates_dir(cfg))}

    @app.get("/api/ui/chapters")
    def ui_chapters(path: str = Query(...)):
        """章节目录（`chapter list --json`）。"""
        return _respond(_cli("--project", path, "--json", "chapter", "list"))

    @app.get("/api/ui/chapters/search")
    def ui_chapters_search(path: str = Query(...), q: str = Query("")) -> dict:
        """章节搜索：标题 + 正文（含命中次数与首处片段）。

        参数:
            path: 工程目录。
            q: 关键词（去首尾空格后至少 1 个字符）。

        返回:
            `{q, hits: [{id, title, count, snippet}]}`，按章节 index 升序。
        """
        keyword = (q or "").strip()
        if not keyword:
            return {"q": "", "hits": []}
        store = _store(path)
        lowered = keyword.lower()
        hits = []
        for meta in store.list_chapters():
            chapter = store.load_chapter(meta["id"])
            content = str(chapter.get("content") or "")
            count = content.lower().count(lowered)
            title = str(chapter.get("title") or "")
            in_title = lowered in title.lower()
            if not count and not in_title:
                continue
            position = content.lower().find(lowered)
            snippet = ""
            if position >= 0:
                start = max(0, position - 18)
                snippet = content[start : position + len(keyword) + 22].replace("\n", " ")
            hits.append(
                {
                    "id": meta["id"],
                    "title": title,
                    "count": count,
                    "snippet": snippet,
                }
            )
        return {"q": keyword, "hits": hits}

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

    @app.get("/api/ui/chapters/stale")
    def ui_chapters_stale(path: str = Query(...), chapter: str = Query(...)) -> dict:
        """章节级下游过期汇总：列出过期/失败的台词行与其所在场景。

        参数:
            path: 工程目录。
            chapter: 章节 ID。

        返回:
            `{chapter, title, counts, items: [{line_id, scene_id, scene_title, index, speaker, text, state}]}`。
        """
        store = _store(path)
        payload = _respond(_cli("--project", path, "--json", "line", "list", "--chapter", chapter)) or {}
        data = store.load_chapter(chapter)
        scene_titles = {
            str(scene.get("id")): str(scene.get("title") or f"场景 {int(scene.get('index') or 0) + 1}")
            for scene in data.get("scenes") or []
        }
        items = [
            {
                "line_id": row.get("line_id"),
                "scene_id": row.get("scene_id"),
                "scene_title": scene_titles.get(str(row.get("scene_id")), ""),
                "index": row.get("index"),
                "speaker": row.get("speaker"),
                "text": row.get("text"),
                "state": row.get("state"),
            }
            for row in payload.get("lines") or []
            if row.get("state") in ("stale", "failed")
        ]
        return {
            "chapter": chapter,
            "title": data.get("title"),
            "counts": payload.get("counts") or {},
            "items": items,
        }

    @app.get("/api/ui/chapters/facets")
    def ui_chapters_facets(path: str = Query(...)) -> dict:
        """章节目录三组筛选维度：全部场景 / 全部角色 / 全部状态。

        全部按真实数据派生，不新增落盘字段：
        - 角色：本章台词里出现过的说话人（旁白置顶）；
        - 创作状态：无台词=画本中，有未就绪行=配音中，全部就绪未混音=后期中，
          已混音未导出=审听中，已导出=创作完成；
        - 成品音频：`renders/<chapter>.mix(.norm).wav` 是否存在；
        - 工单：队列 `.ndw/queue.json` 是否有指向本章的任务。

        参数:
            path: 工程目录。

        返回:
            `{characters, scenes, chapters}`；chapters 为章节 ID → 维度值。
        """
        from ..tts import line_state

        store = _store(path)
        characters = store.load_characters().get("characters") or []
        by_id = {item.get("id"): item for item in characters}
        cfg = load_config(config_path) if config_path else load_config()
        engine = line_state.engine_params(cfg)
        live_ids = line_state.live_line_ids(store)
        job_chapters = {
            str((job.get("target") or {}).get("chapter_id"))
            for job in QueueStore(store).load()
            if (job.get("target") or {}).get("chapter_id")
        }

        char_seen: dict[str, dict] = {}
        scene_seen: list[str] = []
        per_chapter: dict[str, dict] = {}
        for meta in store.list_chapters():
            chapter = store.load_chapter(meta["id"])
            chapter_id = meta["id"]
            lines = [line for scene in chapter.get("scenes") or [] for line in scene.get("lines") or []]
            snapshots = line_state.load_snapshots(store.root, chapter)
            states = line_state.chapter_states(
                chapter, by_id, snapshots, root=store.root, live_ids=live_ids, engine=engine
            )
            counts = line_state.state_counts(states)
            total = len(lines)
            ready = int(counts.get("ready") or 0)
            pending = total - ready

            renders = store.root / "renders"
            has_audio = (renders / f"{chapter_id}.mix.norm.wav").exists() or (renders / f"{chapter_id}.mix.wav").exists()
            has_export = (store.root / "exports" / f"{chapter_id}.manifest.json").exists() or (
                renders / f"{chapter_id}.mp4"
            ).exists()
            if not total:
                creative = "script"
            elif pending:
                creative = "voice"
            elif not has_audio:
                creative = "post"
            elif not has_export:
                creative = "review"
            else:
                creative = "done"

            chapter_chars: list[str] = []
            chapter_scenes: list[str] = []
            for scene in chapter.get("scenes") or []:
                title = str(scene.get("title") or "").strip()
                if title:
                    chapter_scenes.append(title)
                    if title not in scene_seen:
                        scene_seen.append(title)
                for line in scene.get("lines") or []:
                    speaker = line.get("speaker_id")
                    if speaker and speaker not in chapter_chars:
                        chapter_chars.append(speaker)
            for speaker in chapter_chars:
                if speaker not in char_seen:
                    item = by_id.get(speaker) or {}
                    name = item.get("name") or ("旁白" if speaker == "chr_narrator" else "未知角色")
                    char_seen[speaker] = {"id": speaker, "name": name, "role": item.get("role")}

            per_chapter[chapter_id] = {
                "character_ids": chapter_chars,
                "scene_titles": chapter_scenes,
                "creative": creative,
                "has_audio": has_audio,
                "has_job": chapter_id in job_chapters,
                "stale": int(counts.get("stale") or 0),
                "failed": int(counts.get("failed") or 0),
            }

        ordered_chars = sorted(
            char_seen.values(),
            key=lambda item: (item["id"] != "chr_narrator", item.get("role") or "zz", item["id"]),
        )
        return {
            "characters": ordered_chars,
            "scenes": scene_seen,
            "chapters": per_chapter,
        }

    @app.get("/api/ui/chapter")
    def ui_chapter(path: str = Query(...), chapter: str = Query(...)):
        """章节详情（含场景，`chapter show <id> --json`）。"""
        return _respond(_cli("--project", path, "--json", "chapter", "show", chapter))

    @app.post("/api/ui/chapter/add")
    def ui_chapter_add(body: dict) -> dict:
        """新建章节（`chapter add --json`），返回新章节 ID 供前端直接跳转。

        参数:
            body: `{path, title, index?, outline?}`。

        异常:
            HTTPException 400: 标题为空。
        """
        title = str(body.get("title") or "").strip()
        if not title:
            raise HTTPException(status_code=400, detail={"code": 2, "message": "章节标题不能为空"})
        argv = ["--project", body["path"], "--json", "chapter", "add", "--title", title]
        if body.get("index") is not None:
            argv += ["--index", str(int(body["index"]))]
        if body.get("outline"):
            argv += ["--outline", str(body["outline"])]
        return _respond(_cli(*argv))

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
        payload = _respond(_cli(*argv))
        # 正文与切分是否还对得上：只读检查，让编辑区能直接给出「重新切分」出口
        try:
            payload["consistency"] = chapter_consistency(_store(path).load_chapter(chapter))
        except Exception:  # noqa: BLE001 - 检查失败不影响台词展示
            payload["consistency"] = None
        return payload

    @app.get("/api/ui/chapter/consistency")
    def ui_chapter_consistency(path: str = Query(...), chapter: str = Query(...)) -> dict:
        """正文与切分对位检查（只读）：任务页、配音页共用同一出口。"""
        return chapter_consistency(_store(path).load_chapter(chapter))

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

    @app.get("/api/ui/gpu/status")
    def ui_gpu_status(path: str = Query(...)) -> dict:
        """GPU 锁与并发明细：锁文件持有人 + 队列状态计数（任务页展示）。

        参数:
            path: 工程根目录。

        返回:
            `{lock, queue, paused, max_concurrent, poll_interval_s}`。
        """
        from ..orchestration.gpu_queue import GpuLock

        store = _store(path)
        cfg = load_config(config_path) if config_path else load_config()
        lock_path = str(cfg.get("gpu.lock_path") or "")
        lock = GpuLock(lock_path).inspect() if lock_path else {
            "path": "",
            "exists": False,
            "alive": False,
            "holder": None,
        }
        jobs = QueueStore(store).load()
        counts = {"total": len(jobs), "queued": 0, "running": 0, "failed": 0, "succeeded": 0}
        for job in jobs:
            status = str(job.get("status") or "")
            if status in counts:
                counts[status] += 1
        return {
            "lock": lock,
            "queue": counts,
            "paused": QueueStore(store).paused(),
            "max_concurrent": int(cfg.get("gpu.max_concurrent") or 1),
            "poll_interval_s": float(cfg.get("gpu.poll_interval_s") or 2),
        }

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
        # regenerate：已就绪行也重新合成（绕过同键去重与产物缓存）；continue：只补未生成/过期/失败
        mode = "regenerate" if str(body.get("mode") or "") == "regenerate" else "continue"
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

        store = ProjectStore(path)
        # 主动启动意味着「继续跑」：清掉暂停标记，避免新批次刚开始就卡住
        (store.root / ".ndw" / "tts.paused").unlink(missing_ok=True)
        QueueStore(store).set_paused(False)
        # pid 文件显示已有进程活着时直接拒绝，避免重复启动第二个批次
        pid_file = store.root / ".ndw" / "web-tts.pid"
        if pid_file.is_file():
            try:
                old_pid = int(pid_file.read_text(encoding="utf-8").strip())
            except ValueError:
                old_pid = 0
            if old_pid and _is_tts_process(old_pid):
                return {
                    "started": False,
                    "active": 0,
                    "since": now_iso(),
                    "pid": old_pid,
                    "reason": "已有配音进程在运行",
                }
        log_dir = store.root / ".ndw" / "logs"
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
        if mode == "regenerate":
            argv += ["--scope", "chapter", "--force"]
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
        # 记录后台进程 pid，供「停止生成」终止（进程组由 start_new_session 独立出来）
        pid_file = store.root / ".ndw" / "web-tts.pid"
        pid_file.write_text(str(process.pid), encoding="utf-8")
        _ACTIVE_TTS_PROJECTS.add(str(store.root))
        return {"started": True, "mode": mode, "pid": process.pid, "log": str(log_path), "since": since}

    @app.post("/api/ui/stop")
    def ui_stop(body: dict) -> dict:
        """停止配音生成：终止后台消费进程，并把未完成任务标记为已取消。

        参数:
            body: {path 工程目录}。

        返回:
            {stopped 是否确实停下, killed 是否终止了进程, canceled 被取消的任务数}。
        """
        store = ProjectStore(body["path"])
        result = _stop_tts_project(body["path"])
        _ACTIVE_TTS_PROJECTS.discard(str(store.root))
        # 停止优先于暂停：清掉两个标记，避免下次启动直接卡在暂停态
        (store.root / ".ndw" / "tts.paused").unlink(missing_ok=True)
        QueueStore(store).set_paused(False)
        return result

    @app.get("/api/ui/tts/pause")
    def ui_tts_pause_state(path: str = Query(...)) -> dict:
        """读取配音批次与媒体队列的暂停状态。

        参数:
            path: 工程目录。

        返回:
            `{paused 配音批次暂停, queue_paused 队列暂停}`。
        """
        store = _store(path)
        return {
            "paused": (store.root / ".ndw" / "tts.paused").exists(),
            "queue_paused": QueueStore(store).paused(),
        }

    @app.post("/api/ui/tts/pause")
    def ui_tts_pause(body: dict) -> dict:
        """暂停或继续配音批次（同时暂停媒体队列消费）。

        暂停只阻止「下一行开始」，正在合成的那一行会跑完；删除标记后自动续跑。

        参数:
            body: `{path, paused}`。

        返回:
            `{paused}`。
        """
        store = _store(body["path"])
        paused = bool(body.get("paused"))
        marker = store.root / ".ndw" / "tts.paused"
        if paused:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(now_iso(), encoding="utf-8")
        else:
            marker.unlink(missing_ok=True)
        QueueStore(store).set_paused(paused)
        return {"paused": paused}

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

    @app.post("/api/ui/line/set-batch")
    def ui_line_set_batch(body: dict) -> dict:
        """批量改行参数（`line set --lines`）：返回成功/失败计数与失败原因。

        参数:
            body: `{path, chapter, lines: [行 ID], speaker?, kind?, ...}`。

        返回:
            `{changed, updated, not_found, counts}`。

        异常:
            HTTPException 400: 没有选中行。
        """
        lines = [str(item).strip() for item in (body.get("lines") or []) if str(item).strip()]
        if not lines:
            raise HTTPException(status_code=400, detail={"code": 2, "message": "没有选中任何行"})
        argv = [
            "--project",
            body["path"],
            "--json",
            "line",
            "set",
            "--chapter",
            body["chapter"],
            "--lines",
            ",".join(lines),
        ]
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

    @app.post("/api/ui/line/merge")
    def ui_line_merge(body: dict) -> dict:
        """合并同一场景内相邻的台词行（`line merge --json`）。

        参数:
            body: `{path, chapter, lines: [行 ID]}`，必须同场景且 index 连续。

        返回:
            `{changed, merged: {line_id, text, chars, removed, lost_audio}, revision}`。

        异常:
            HTTPException 400: 少于两行、跨场景、不相邻或正文对不上。
        """
        lines = [str(item).strip() for item in (body.get("lines") or []) if str(item).strip()]
        if len(lines) < 2:
            raise HTTPException(status_code=400, detail={"code": 2, "message": "至少选择两行才能合并"})
        return _respond(
            _cli(
                "--project",
                body["path"],
                "--json",
                "line",
                "merge",
                "--chapter",
                body["chapter"],
                "--lines",
                ",".join(lines),
            )
        )

    @app.post("/api/ui/line/merge-narration")
    def ui_line_merge_narration(body: dict) -> dict:
        """按期望字数批量合并相邻旁白（`line merge-narration --json`）。

        参数:
            body: `{path, chapter, scene?|scenes?: [...], max_chars?, dry_run?}`；
                  `dry_run` 缺省为 true，只返回预览分组。

        返回:
            `{mode, dry_run, groups, counts, changed?}`。
        """
        argv = ["--project", body["path"], "--json", "line", "merge-narration", "--chapter", body["chapter"]]
        scenes = body.get("scenes") or ([body["scene"]] if body.get("scene") else [])
        for scene in scenes:
            argv += ["--scene", str(scene)]
        if body.get("max_chars") is not None:
            argv += ["--max-chars", str(int(body["max_chars"]))]
        if body.get("dry_run", True):
            argv.append("--dry-run")
        return _respond(_cli(*argv))

    @app.post("/api/ui/line/edit")
    def ui_line_edit(body: dict) -> dict:
        """就地编辑台词文本，并同步回章节正文（`line edit --json`）。

        正文对不上时 CLI 返回退出码 2，界面会收到「正文已变更，请先重新切分」。
        """
        return _respond(
            _cli(
                "--project",
                body["path"],
                "--json",
                "line",
                "edit",
                "--chapter",
                body["chapter"],
                "--line",
                body["line"],
                "--text",
                body["text"],
            )
        )

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
        """生成大纲（`outline generate`）。

        参数:
            body: {path 工程目录, acts, chapters_per_act, beats_per_chapter, words,
                   multi_turn 是否按幕多轮, acts_per_round 每轮幕数, dry_run}。

        返回:
            CLI 的 JSON 负载（含 rounds / multi_turn）。
        """
        argv = ["--project", body["path"], "--json", "outline", "generate"]
        for key, flag in (("acts", "--acts"), ("chapters_per_act", "--chapters-per-act"), ("beats_per_chapter", "--beats-per-chapter"), ("words", "--words")):
            if body.get(key) is not None:
                argv += [flag, str(body[key])]
        if body.get("multi_turn"):
            argv.append("--multi-turn")
        if body.get("acts_per_round") is not None:
            argv += ["--acts-per-round", str(body["acts_per_round"])]
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
        """生成章节正文（`chapter generate`）。

        参数:
            body: {path 工程目录, chapter 章节 ID, regen, dry_run,
                   multi_turn 是否多轮续写, segment_words 每段字数, max_rounds 最大轮数}。

        返回:
            CLI 的 JSON 负载（含 words / rounds / segments / multi_turn）。
        """
        argv = ["--project", body["path"], "--json", "chapter", "generate", "--chapter", body["chapter"]]
        if body.get("regen"):
            argv.append("--regen")
        if body.get("dry_run"):
            argv.append("--dry-run")
        # 显式开关才带参数；缺省交给配置里的 writing.multi_turn 判断
        if body.get("multi_turn") is not None:
            argv.append("--multi-turn" if body["multi_turn"] else "--no-multi-turn")
        if body.get("segment_words") is not None:
            argv += ["--segment-words", str(body["segment_words"])]
        if body.get("max_rounds") is not None:
            argv += ["--max-rounds", str(body["max_rounds"])]
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

        说明:
            预览会按「工程 + 章节 + 正文哈希」缓存；随后 apply 命中缓存时直接写盘，
            不再重跑多轮模型，避免「点应用」后长时间无响应。

        异常:
            HTTPException: 正文为空 400，章节不存在 404，模型异常 502。
        """
        store = _store(body.get("path"))
        chapter_id = body.get("chapter") or ""
        apply = bool(body.get("apply"))
        try:
            chapter = store.load_chapter(chapter_id)
            content = str(chapter.get("content") or "")
            if apply:
                cached = _cached_summary(store, chapter_id, content)
                if cached:
                    return apply_summary(
                        store,
                        load_config(config_path),
                        chapter_id,
                        cached["preview"],
                        rounds=cached["rounds"],
                    )
            result = summarize_chapter(store, load_config(config_path), chapter_id, apply=apply)
            if not apply:
                _remember_summary(store, chapter_id, content, result)
            return result
        except AdapterError as exc:
            raise HTTPException(status_code=502, detail=exc.message) from exc
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/ui/scene/split")
    def ui_scene_split(body: dict) -> dict:
        """切分场景（`scene split`）；force 覆盖，keep_lines 保留未变化场景的台词。"""
        argv = ["--project", body["path"], "--json", "scene", "split", "--chapter", body["chapter"]]
        if body.get("force"):
            argv.append("--force")
        if body.get("keep_lines"):
            argv.append("--keep-lines")
        return _respond(_cli(*argv))

    @app.post("/api/ui/scene/split/impact")
    def ui_scene_split_impact(body: dict) -> dict:
        """重切场景的影响清单（`scene split --impact`，只读）。"""
        argv = ["--project", body["path"], "--json", "scene", "split", "--chapter", body["chapter"], "--impact"]
        if body.get("keep_lines"):
            argv.append("--keep-lines")
        return _respond(_cli(*argv))

    @app.post("/api/ui/line/split")
    def ui_line_split(body: dict) -> dict:
        """切分台词（`line split`）；force 覆盖，keep_manual 保留人工修改的台词行。"""
        argv = ["--project", body["path"], "--json", "line", "split", "--chapter", body["chapter"]]
        if body.get("force"):
            argv.append("--force")
        if body.get("keep_manual"):
            argv.append("--keep-manual")
        return _respond(_cli(*argv))

    @app.post("/api/ui/line/split-at")
    def ui_line_split_at(body: dict) -> dict:
        """在光标处拆分一条台词（`line split-at --offset`）。

        参数:
            body: `{path, chapter, line, offset}`；offset 为 0 基字符位置。

        返回:
            `{changed, line_id, new_line_id, first_text, second_text}`。
        """
        return _respond(
            _cli(
                "--project",
                body["path"],
                "--json",
                "line",
                "split-at",
                "--chapter",
                body["chapter"],
                "--line",
                body["line"],
                "--offset",
                str(int(body.get("offset") or 0)),
            )
        )

    @app.post("/api/ui/line/split/impact")
    def ui_line_split_impact(body: dict) -> dict:
        """重切台词的影响清单（`line split --impact`，只读）。"""
        argv = ["--project", body["path"], "--json", "line", "split", "--chapter", body["chapter"], "--impact"]
        if body.get("keep_manual"):
            argv.append("--keep-manual")
        return _respond(_cli(*argv))

    @app.get("/api/ui/script")
    def ui_script(path: str = Query(...), chapter: str = Query(...)) -> dict:
        """导演标注文本与脚本文件状态（读落盘产物，不重算）。

        额外解析落盘标注，返回 `warnings` 与结构化 `warning_items`，
        让页面重进后仍能看到告警并点击定位；解析失败只影响告警展示，不影响标注回显。
        """
        store = _store(path)
        markup_path = store.root / "scripts" / f"{chapter}.markup.txt"
        script_path = store.script_path(chapter)
        markup = markup_path.read_text(encoding="utf-8") if markup_path.exists() else ""
        warnings: list[str] = []
        warning_items: list[dict] = []
        if markup.strip():
            try:
                from ..director.markup import parse_markup

                warning_items = []
                script = parse_markup(
                    markup,
                    store.load_chapter(chapter),
                    store.load_characters()["characters"],
                    store.settings(),
                    diagnostics=warning_items,
                )
                warnings = script.get("warnings") or []
            except Exception:  # noqa: BLE001 - 告警展示失败不阻断标注回显
                warnings = []
                warning_items = []
        return {
            "markup_path": str(markup_path),
            "markup": markup,
            "script_path": str(script_path),
            "has_script": script_path.exists(),
            "warnings": warnings,
            "warning_items": warning_items,
        }

    @app.post("/api/ui/script/suggest")
    def ui_script_suggest(body: dict) -> dict:
        """生成标注建议（`script suggest`），可同时应用。"""
        argv = ["--project", body["path"], "--json", "script", "suggest", "--chapter", body["chapter"]]
        if body.get("apply"):
            argv.append("--apply")
        if body.get("dry_run"):
            argv.append("--dry-run")
        if body.get("resume"):
            argv.append("--resume")
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

        def _template_for(item: dict, kind: str, has_refs: bool = False) -> str:
            """解析单个条目实际使用的模板显示名。

            参数:
                item: 关键图或视频条目。
                kind: image / video。
                has_refs: 关键图是否带参考图（决定展示 image_ref 模板）。

            返回:
                模板显示名（缺省回落到模板 ID）；解析失败返回空串。
            """
            try:
                if kind == "video":
                    i2v = item.get("mode") == "i2v"
                    template_kind = "video_i2v" if i2v else "video_t2v"
                    setting_key = "comfyui.templates.video_i2v" if i2v else "comfyui.templates.video_t2v"
                else:
                    template_kind = "image_ref" if has_refs else "image"
                    setting_key = "comfyui.templates.image_ref" if has_refs else "comfyui.templates.image"
                template_id = config.get(setting_key)
                explicit = item.get("workflow_template_id")
                if explicit:
                    # 只有条目自带模板的用途与本次一致时才采用，避免纯文生图模板盖掉参考图模板
                    entry = comfy_media.template_by_id(manifest, str(explicit))
                    if str(entry.get("kind") or "") == template_kind:
                        template_id = explicit
                resolved = comfy_media.resolve_template(template_id, template_kind, manifest)
                entry = resolved.get("template") or {}
                return str(entry.get("display_name") or resolved.get("template_id") or "")
            except Exception:
                return ""

        def _aspect_for(kind: str) -> dict:
            """按工程画幅解析尺寸。

            参数:
                kind: image / video。

            返回:
                `{"ratio","width","height"}`；解析失败时各字段为 None。
            """
            try:
                return comfy_media.resolve_aspect(project_settings, kind)
            except Exception:
                return {"ratio": None, "width": None, "height": None}

        def _size_mismatch(asset: dict, aspect: dict) -> bool:
            """判断已生成产物尺寸是否与工程画幅一致。

            参数:
                asset: 条目资产状态（含 width/height）。
                aspect: 工程画幅解析结果。

            返回:
                双方尺寸都存在且不一致时返回 True；缺数据时按一致处理。
            """
            try:
                actual_width, actual_height = int(asset.get("width")), int(asset.get("height"))
                expected_width, expected_height = int(aspect.get("width")), int(aspect.get("height"))
            except (TypeError, ValueError):
                return False
            return (actual_width, actual_height) != (expected_width, expected_height)

        rows = {"sfx": [], "image": [], "video": []}
        keys = {"sfx": "sfx_events", "image": "key_images", "video": "video_segments"}
        for scene in chapter_data.get("scenes") or []:
            for kind, key in keys.items():
                for item in scene.get(key) or []:
                    asset = item.get("asset") or {}
                    template = ""
                    aspect = {"ratio": None, "width": None, "height": None}
                    # 参考图只给关键图；解析失败降级为空列表 + 一条告警，绝不让接口 500
                    references = []
                    reference_warnings = []
                    if kind == "image":
                        try:
                            resolved_refs = collect_references(store, config, chapter_data, scene, item)
                            references = [
                                {field: ref.get(field) for field in ("kind", "id", "name", "rel")}
                                for ref in resolved_refs.get("items") or []
                            ]
                            reference_warnings = list(resolved_refs.get("warnings") or [])
                        except Exception as exc:
                            references = []
                            reference_warnings = [f"参考图解析失败：{exc}"]
                    if kind in ("image", "video"):
                        template = _template_for(item, kind, bool(references))
                        aspect = _aspect_for(kind)
                    rows[kind].append(
                        {
                            "id": item.get("id"),
                            "scene_id": scene.get("id"),
                            "index": item.get("index"),
                            "prompt": item.get("prompt") or "",
                            "prompt_en": item.get("prompt_en") or "",
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
                            "actual_width": asset.get("width"),
                            "actual_height": asset.get("height"),
                            "size_mismatch": _size_mismatch(asset, aspect) if kind in ("image", "video") else False,
                            "references": references,
                            "reference_warnings": reference_warnings,
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

    @app.post("/api/ui/export/jianying")
    def ui_export_jianying(body: dict) -> dict:
        """导出剪映草稿 zip（`export --format jianying`）。"""
        argv = [
            "--project",
            body["path"],
            "--json",
            "export",
            "--chapter",
            body["chapter"],
            "--format",
            "jianying",
        ]
        if body.get("out_dir"):
            argv += ["--out-dir", body["out_dir"]]
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
                rel = item.relative_to(root)
                # 剪映草稿的 media 目录是素材副本，逐条列出会刷屏，只展示 zip 与草稿元数据
                if (
                    len(rel.parts) > 3
                    and rel.parts[0] == "exports"
                    and rel.parts[1] == "jianying"
                    and rel.parts[3] == "media"
                ):
                    continue
                stat = item.stat()
                files.append(
                    {
                        "rel": str(rel),
                        "path": str(item),
                        "size": stat.st_size,
                        "mtime": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                    }
                )
        return {"root": str(root), "files": files}

    @app.get("/api/ui/export/preflight")
    def ui_export_preflight(path: str = Query(...), chapter: str = Query(...)) -> dict:
        """导出前检查：缺配音行、音色编排提示、时间轴状态。

        参数:
            path: 工程目录。
            chapter: 章节 ID。

        返回:
            见 `_export_preflight`。
        """
        return _export_preflight(_store(path), chapter)

    @app.post("/api/ui/export/bundle")
    def ui_export_bundle(body: dict) -> dict:
        """导出本章交付包并打成 zip（新版前端「导出」页使用）。

        流程：先构建时间轴（幂等派生物），再走 `export --format bundle` 产出
        SRT / 清单 / MP3 / MP4，最后把成品与逐行配音音频、台词脚本一起打包。

        参数:
            body: `{path, chapter}`。

        返回:
            `{ok, zip, rel, name, missing, entries}`；`rel` 可直接交给
            `/api/ui/file?path=...&project=...` 下载。

        异常:
            HTTPException: 时间轴构建或导出失败时透传 CLI 的中文说明。
        """
        import zipfile
        from datetime import datetime

        path = body["path"]
        chapter_id = body["chapter"]
        store = _store(path)
        # 导出前检查：有缺音频/编排问题时必须显式忽略，避免「静默交付半成品」
        check = _export_preflight(store, chapter_id)
        blocking = check["missing_total"] or any(item["level"] == "error" for item in check["issues"])
        if blocking and not body.get("ignore_issues"):
            raise HTTPException(
                status_code=412,
                detail={
                    "code": 4,
                    "message": (
                        f"导出前检查未通过：{check['missing_total']} 行缺音频，"
                        f"{len(check['issues'])} 条问题；确认忽略后再导出"
                    ),
                    "preflight": check,
                },
            )
        # 时间轴是导出前置；重建是幂等的，避免用户改词后导出到旧时间轴
        _respond(_cli("--project", path, "--json", "timeline", "build", "--chapter", chapter_id))
        result = _respond(_cli("--project", path, "--json", "export", "--chapter", chapter_id, "--format", "bundle"))
        if not isinstance(result, dict):
            raise HTTPException(status_code=500, detail={"code": 3, "message": "导出结果格式异常"})

        lines_payload = _respond(_cli("--project", path, "--json", "line", "list", "--chapter", chapter_id))
        rows = (lines_payload or {}).get("lines") or []
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        export_dir = store.root / "exports"
        export_dir.mkdir(parents=True, exist_ok=True)
        zip_path = export_dir / f"{chapter_id}.bundle-{stamp}.zip"

        entries: list[str] = []
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
            # 1) 交付包成品（srt / manifest / mp3 / mp4 里有就收）
            for item in result.get("artifacts") or []:
                target = Path(str(item.get("path") or ""))
                if target.is_file():
                    archive.write(target, target.name)
                    entries.append(target.name)
            for key in ("readme", "bundle"):
                target = Path(str(result.get(key) or ""))
                if target.is_file():
                    archive.write(target, target.name)
                    entries.append(target.name)
            # 2) 台词脚本：没有成片时也能直接读稿
            script = "\n".join(
                f"[{int(row.get('ordinal') or 0):03d}] {row.get('speaker') or ''}：{row.get('text') or ''}"
                for row in rows
            )
            archive.writestr("台词脚本.txt", script + "\n")
            entries.append("台词脚本.txt")
            # 3) 已配好的逐行音频：交付包里最实用的部分
            audio_count = 0
            for row in rows:
                rel = row.get("audio")
                if not rel:
                    continue
                source = store.root / str(rel)
                if not source.is_file():
                    continue
                archive.write(source, f"audio/{source.name}")
                audio_count += 1
            if audio_count:
                entries.append(f"audio/（{audio_count} 个文件）")

        return {
            "ok": True,
            "zip": str(zip_path),
            "rel": str(zip_path.relative_to(store.root)),
            "name": zip_path.name,
            "missing": result.get("missing") or [],
            "entries": entries,
        }

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
            # overwrite=false 表示「只补缺失」：保留已有字段，只生成空缺
            if body.get("overwrite") is False:
                argv.append("--keep-existing")
            # 显式固定种子才传；缺省不固定，保证同一设定重复生成结果不同
            if body.get("seed") is not None and str(body.get("seed")).strip() != "":
                try:
                    argv += ["--seed", str(int(body["seed"]))]
                except (TypeError, ValueError):
                    raise HTTPException(status_code=400, detail={"code": 2, "message": "seed 必须是整数"})
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

    @app.get("/api/ui/cast/check")
    def ui_cast_check(path: str = Query(...), chapter: str | None = None) -> dict:
        """全剧音色与 BGM 一致性校验（R18）。

        参数:
            path: 工程根目录。
            chapter: 只校验该章；为空校验全剧。

        返回:
            `{level, findings: [...], stats: {...}}`。
        """
        from ..store.voice_cast import check_cast

        store = _store(path)
        return check_cast(store, chapter)

    @app.get("/api/ui/cast")
    def ui_cast_get(path: str = Query(...)) -> dict:
        """读取音色编排：音色池、旁白人格、切换点、每章旁白展开表。

        参数:
            path: 工程根目录。

        返回:
            `{cast, chapters, voices, stock_slots, errors, plan}`。
        """
        from ..store.voice_cast import narrator_plan, validate_cast_config

        store = _store(path)
        project = store.load_project()
        cast = ((project.get("settings") or {}).get("tts") or {}).get("cast") or {}
        chapters = [
            {"id": item.get("id"), "index": item.get("index"), "title": item.get("title")}
            for item in store.list_chapters()
        ]
        voices = [
            {"id": item.get("id"), "name": item.get("name"), "status": item.get("status")}
            for item in store.load_voices().get("voices") or []
        ]
        binding_index = _voice_binding_index(store.load_voices().get("voices") or [])
        characters = [
            {
                "id": item.get("id"),
                "name": item.get("name"),
                "role": item.get("role"),
                "voice_id": _resolve_voice_binding(item.get("voice_binding"), binding_index),
            }
            for item in store.load_characters().get("characters") or []
        ]
        stock: dict[str, dict] = {}
        for character in store.load_characters().get("characters") or []:
            slot = (character.get("stock") or {}).get("slot")
            if not slot:
                continue
            stock.setdefault(
                slot,
                {
                    "slot": slot,
                    "label": (character.get("stock") or {}).get("label"),
                    "voice_slot": (character.get("stock") or {}).get("voice_slot"),
                    "count": 0,
                },
            )
            stock[slot]["count"] += 1
        voice_ids = {item["id"] for item in voices if item.get("id")}
        return {
            "cast": cast,
            "chapters": chapters,
            "voices": voices,
            "characters": characters,
            "stock_slots": list(stock.values()),
            "errors": validate_cast_config(cast, chapters, voice_ids, set(stock)),
            "plan": narrator_plan(cast, chapters),
        }

    @app.get("/api/ui/cast/chapter")
    def ui_cast_chapter_get(path: str = Query(...), chapter: str = Query(...)) -> dict:
        """读取某章的章节音色覆盖表（每行默认给出跟随全局时的生效音色）。

        参数:
            path: 工程根目录。
            chapter: 章节 ID。

        返回:
            `{chapter, rows, voices}`；rows 含 mode / inherited / override / effective。
        """
        from ..store.voice_cast import chapter_override_rows

        try:
            return chapter_override_rows(_store(path), chapter)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail={"code": 4, "message": str(exc)})

    @app.post("/api/ui/cast/chapter")
    def ui_cast_chapter_save(body: dict) -> dict:
        """写入/清除章节覆盖。

        参数:
            body: `{path, chapter, character_id?, voice_id?, all?}`；
                  `all=true` 清空整章，`voice_id` 为空清除单行。

        返回:
            `{ok, chapter, rows, voices}`。
        """
        from ..store.voice_cast import clear_chapter_overrides, set_chapter_override

        chapter_id = str(body.get("chapter") or "").strip()
        if not chapter_id:
            raise HTTPException(status_code=400, detail={"code": 2, "message": "缺少章节 ID"})
        store = _store(body.get("path"))
        try:
            if body.get("all"):
                data = clear_chapter_overrides(store, chapter_id)
            else:
                data = set_chapter_override(
                    store,
                    chapter_id,
                    str(body.get("character_id") or "").strip(),
                    body.get("voice_id") or None,
                )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail={"code": 4, "message": str(exc)})
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"code": 2, "message": str(exc)})
        return {"ok": True, **data}

    @app.get("/api/ui/cast/character")
    def ui_cast_character(path: str = Query(...), character: str = Query(...)) -> dict:
        """某个角色在各章的生效音色（角色默认 + 章节覆盖）。

        参数:
            path: 工程根目录。
            character: 角色 ID。

        返回:
            `{character, chapters: [{id, index, title, overridable, mode, override, inherited, effective}], voices}`。
        """
        from ..store.voice_cast import chapter_override_rows

        store = _store(path)
        rows: list[dict] = []
        voices: list[dict] = []
        for meta in store.list_chapters():
            try:
                data = chapter_override_rows(store, meta["id"])
            except FileNotFoundError:
                continue
            row = next((item for item in data.get("rows") or [] if item.get("character_id") == character), None)
            if row is None:
                continue
            voices = data.get("voices") or voices
            rows.append(
                {
                    "id": meta["id"],
                    "index": meta.get("index"),
                    "title": meta.get("title"),
                    "overridable": bool(row.get("overridable")),
                    "mode": row.get("mode"),
                    "override": row.get("override"),
                    "inherited": row.get("inherited"),
                    "effective": row.get("effective"),
                }
            )
        return {"character": character, "chapters": rows, "voices": voices}

    @app.get("/api/ui/cast/autofill")
    def ui_cast_autofill_preview(path: str = Query(...)) -> dict:
        """预览「按角色自动补齐」计划（只读，不落盘）。

        参数:
            path: 工程根目录。

        返回:
            `{plan, undo}`；plan.items 每项含 action / proposed / reason。
        """
        from ..tts.cast_autofill import build_plan, undo_state

        store = _store(path)
        return {"plan": build_plan(store), "undo": undo_state(store)}

    @app.post("/api/ui/cast/autofill")
    def ui_cast_autofill_apply(body: dict) -> dict:
        """应用自动补齐，并保留一份补齐前的快照供撤回。

        参数:
            body: `{path}`。

        返回:
            `{applied, changed, plan, undo}`。
        """
        from ..tts.cast_autofill import apply

        store = _store(body.get("path"))
        try:
            return apply(store)
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=400, detail={"code": 2, "message": str(exc)})

    @app.post("/api/ui/cast/autofill/undo")
    def ui_cast_autofill_undo(body: dict) -> dict:
        """撤回上次自动补齐，恢复补齐前的 cast。

        参数:
            body: `{path}`。

        返回:
            `{restored, undo}`。
        """
        from ..tts.cast_autofill import undo_last

        store = _store(body.get("path"))
        try:
            return undo_last(store)
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=400, detail={"code": 2, "message": str(exc)})

    @app.get("/api/ui/cast/fill")
    def ui_cast_fill_state(path: str = Query(...)) -> dict:
        """一键补齐音色的当前状态：任务快照与是否还有剩余角色。

        参数:
            path: 工程根目录。

        返回:
            `{job, has_remaining}`。
        """
        from ..tts import voice_fill

        return voice_fill.state(_store(path))

    @app.post("/api/ui/cast/fill/start")
    def ui_cast_fill_start(body: dict) -> dict:
        """启动一键补齐全部音色（扫描 → 复用绑定 → 缺失生成）。

        参数:
            body: `{path, count?, overwrite?}`；overwrite=true 时已有音色也重新生成并覆盖。

        返回:
            `{job}`。
        """
        from ..tts import voice_fill

        try:
            job = voice_fill.start_job(
                _store(body.get("path")),
                _tts_config(),
                body.get("count"),
                overwrite=bool(body.get("overwrite")),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"code": 2, "message": str(exc)}) from exc
        return {"job": job}

    @app.post("/api/ui/cast/fill/resume")
    def ui_cast_fill_resume(body: dict) -> dict:
        """继续补齐剩余角色，或重试指定角色 / 龙套音色池。

        参数:
            body: `{path, character_ids?, pool_slots?}`；两者都为空时补齐全部未完成与失败项。

        返回:
            `{job}`。
        """
        from ..tts import voice_fill

        try:
            job = voice_fill.resume_job(
                _store(body.get("path")),
                _tts_config(),
                body.get("character_ids"),
                body.get("pool_slots"),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"code": 2, "message": str(exc)}) from exc
        return {"job": job}

    @app.post("/api/ui/cast/fill/stop")
    def ui_cast_fill_stop(body: dict) -> dict:
        """停止补齐：已绑定/已生成的结果保留，未完成的可续跑。

        参数:
            body: `{path}`。

        返回:
            `{job}`。
        """
        from ..tts import voice_fill

        job = voice_fill.cancel_job(_store(body.get("path")))
        if not job:
            raise HTTPException(status_code=404, detail={"code": 3, "message": "补齐任务不存在"})
        return {"job": job}

    @app.post("/api/ui/cast")
    def ui_cast_save(body: dict) -> dict:
        """保存音色池与旁白切换点；校验不通过时返回错误列表且不落盘。

        参数:
            body: `{path, cast}`。

        返回:
            `{ok, errors, check?, plan?}`。
        """
        from ..store.voice_cast import check_cast, narrator_plan, normalize_cast, validate_cast_config

        store = _store(body.get("path"))
        project = store.load_project()
        chapters = [
            {"id": item.get("id"), "index": item.get("index"), "title": item.get("title")}
            for item in store.list_chapters()
        ]
        voices = store.load_voices().get("voices") or []
        voice_ids = {item.get("id") for item in voices if item.get("id")}
        stock_slots = {
            (character.get("stock") or {}).get("slot")
            for character in store.load_characters().get("characters") or []
            if (character.get("stock") or {}).get("slot")
        }
        cast = normalize_cast(body.get("cast") or {})
        errors = validate_cast_config(cast, chapters, voice_ids, stock_slots)
        if errors:
            return {"ok": False, "errors": errors}
        settings = project.setdefault("settings", {})
        tts = settings.setdefault("tts", {})
        tts["cast"] = cast
        try:
            store.save_project(project)
        except Exception as exc:  # noqa: BLE001 - schema/IO 失败统一回给页面
            return {"ok": False, "errors": [f"保存失败：{exc}"]}
        return {
            "ok": True,
            "errors": [],
            "check": check_cast(store),
            "plan": narrator_plan(cast, chapters),
        }

    @app.post("/api/ui/cast/backfill")
    def ui_cast_backfill(body: dict) -> dict:
        """给缺失音色描述的角色补生成（`cast backfill --profiles --apply`）。

        参数:
            body: `{path, all?, force?}`。

        返回:
            `{changed, applied}`。
        """
        path = body.get("path")
        _store(path)
        argv = ["--project", path, "--json", "cast", "backfill", "--profiles", "--apply"]
        if body.get("all"):
            argv.append("--all")
        if body.get("force"):
            argv.append("--force")
        return _respond(_cli(*argv))

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
            aliases?, appearance_prompt?, negative_prompt?, situation?, relationships?,
            removed_relationships?}`；removed_relationships 为要删除的关系目标 ID 列表。

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
            ("voice_description_zh", "--voice-description-zh"),
            ("voice_description_en", "--voice-description-en"),
            ("appearance_desc_zh", "--appearance-desc-zh"),
            ("appearance_desc_en", "--appearance-desc-en"),
            ("appearance_negative_zh", "--appearance-negative-zh"),
            ("appearance_negative_en", "--appearance-negative-en"),
            ("situation", "--situation"),
            ("voice_profile", "--voice-profile"),
            ("stock", "--stock"),
        ):
            value = body.get(key)
            if value is None:
                continue
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False)
            argv += [flag, str(value)]
        relationships = body.get("relationships") or {}
        if isinstance(relationships, dict):
            relationships = [f"{key}={value}" for key, value in relationships.items()]
        for item in relationships:
            argv += ["--relationship", str(item)]
        # 列表编辑里被删掉的关系行：显式通知 CLI 移除，避免旧关系残留
        for target_id in body.get("removed_relationships") or []:
            if str(target_id).strip():
                argv += ["--relationship-remove", str(target_id).strip()]
        return _respond(_cli(*argv))

    @app.get("/api/ui/characters/refs")
    def ui_character_refs(path: str = Query(...), character: str = Query(...)) -> dict:
        """角色引用位置：哪些章节/台词行仍在使用该角色（删除前提示）。

        参数:
            path: 工程目录。
            character: 角色 ID。

        返回:
            `{total, chapters: [{id, index, title, lines}]}`。
        """
        from ..cli_character import character_refs

        return character_refs(_store(path), character)

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

    @app.get("/api/ui/characters/stats")
    def ui_character_stats(path: str = Query(...)) -> dict:
        """按角色汇总全书出场行数与首次登场章节（角色档案「查看出场」用）。

        参数:
            path: 工程目录。

        返回:
            `{characters: {角色ID: {lines, chapters: [{id, title, index, lines}]}}}`。
        """
        store = _store(path)
        stats: dict[str, dict] = {}
        for chapter in store.list_chapters():
            chapter_id = chapter.get("id")
            if not chapter_id:
                continue
            data = store.load_chapter(chapter_id)
            for scene in data.get("scenes") or []:
                for line in scene.get("lines") or []:
                    speaker = line.get("speaker_id")
                    if not speaker:
                        continue
                    row = stats.setdefault(speaker, {"lines": 0, "chapters": []})
                    row["lines"] += 1
                    bucket = row["chapters"][-1] if row["chapters"] else None
                    if not bucket or bucket["id"] != chapter_id:
                        bucket = {
                            "id": chapter_id,
                            "title": chapter.get("title") or "",
                            "index": chapter.get("index") or 0,
                            "lines": 0,
                        }
                        row["chapters"].append(bucket)
                    bucket["lines"] += 1
        return {"characters": stats}

    # ---------------- 音色库（内置参考音色 / 录音上传 / 绑定角色）----------------

    def _tts_config():
        """读取本机配置（与 serve 同源），供内置音色目录使用。"""
        return load_config(config_path) if config_path else load_config()

    def _voice_board(path: str) -> dict:
        """汇总音色库、人物绑定与内置音色状态。

        参数:
            path: 工程目录。

        返回:
            `{voices, characters, builtin, denoise_config}`。
        """
        from ..tts import builtin_voices

        store = _store(path)
        cfg = _tts_config()
        denoise_conf = cfg.get("voice.denoise", {}) or {}
        characters = store.load_characters()["characters"]
        voices = store.load_voices()["voices"]
        binding_index = _voice_binding_index(voices)
        return {
            "voices": voices,
            "characters": [
                {
                    "id": item["id"],
                    "name": item["name"],
                    "role": item.get("role"),
                    "gender": item.get("gender"),
                    # 供「按角色设定生成音色」面板带入描述，只读展示，不参与写回
                    "age_range": item.get("age_range"),
                    "personality": item.get("personality") or [],
                    "background": item.get("background"),
                    "speech_style": item.get("speech_style") or {},
                    # 兼容 v2 voice_library_id、v1 voice_id、变体 ID 与旧版内置名称
                    "voice_id": _resolve_voice_binding(item.get("voice_binding"), binding_index),
                }
                for item in characters
            ],
            "builtin": {
                "root": str(builtin_voices.builtin_root(cfg)),
                "items": builtin_voices.rows(cfg),
            },
            "denoise_config": {
                "default_enabled": bool(denoise_conf.get("default_enabled", True)),
                "engine": denoise_conf.get("engine"),
            },
        }

    @app.get("/api/ui/voices")
    def ui_voices(path: str = Query(...)) -> dict:
        """返回工程音色库、人物绑定与内置音色清单。"""
        return _voice_board(path)

    @app.post("/api/ui/voices/builtin/install")
    def ui_voices_builtin_install(body: dict) -> dict:
        """下载内置音色到外置盘。

        参数:
            body: `{keys?: [...], force?: bool}`。

        返回:
            `{result, items}`。
        """
        from ..tts import builtin_voices

        cfg = _tts_config()
        keys = body.get("keys") or None
        result = builtin_voices.install(cfg, keys=keys, force=bool(body.get("force")))
        return {"result": result, "items": builtin_voices.rows(cfg)}

    @app.post("/api/ui/voices/builtin/import")
    def ui_voices_builtin_import(body: dict) -> dict:
        """把内置音色导入工程音色库（按名称/路径迁移旧条目，可重复执行）。

        参数:
            body: `{path, keys?: [...]}`。

        返回:
            `{added, updated, skipped, voices, items}`。
        """
        from ..tts import builtin_voices

        path = body.get("path")
        store = _store(path)
        cfg = _tts_config()
        result = builtin_voices.import_into_project(store, cfg, keys=body.get("keys") or None)
        return {
            "added": result["added"],
            "updated": result["updated"],
            "skipped": result["skipped"],
            "voices": result["voices"],
            "items": builtin_voices.rows(cfg),
        }

    @app.get("/api/ui/voices/builtin/audio")
    def ui_voices_builtin_audio(key: str = Query(...)):
        """返回内置音色的参考音频，供页面试听。

        参数:
            key: 内置音色键（如 `voice_05`）。

        异常:
            HTTPException 404: 未下载或键不存在。
        """
        from fastapi.responses import FileResponse

        from ..tts import builtin_voices

        target = builtin_voices.audio_path(_tts_config(), key)
        if target is None:
            raise HTTPException(status_code=404, detail={"code": 3, "message": "内置音色未下载", "key": key})
        return FileResponse(target)

    @app.get("/api/ui/voices/audio")
    def ui_voices_audio(path: str = Query(...), voice_id: str = Query(...), variant_id: str = Query("")):
        """返回工程音色的参考音频，供页面试听（支持内置音色的绝对路径）。

        参数:
            path: 工程目录。
            voice_id: 音色 ID。
            variant_id: 变体 ID；为空时用默认变体。

        异常:
            HTTPException 404: 音色/变体/参考音频不存在。
        """
        from fastapi.responses import FileResponse

        from ..store import find_variant, find_voice, variant_ref

        store = _store(path)
        voice = find_voice(store.load_voices(), voice_id)
        if voice is None:
            raise HTTPException(status_code=404, detail={"code": 3, "message": "音色不存在", "voice_id": voice_id})
        variant = find_variant(voice, variant_id) if variant_id else find_variant(voice, voice.get("default_variant_id") or "")
        if variant is None:
            raise HTTPException(status_code=404, detail={"code": 3, "message": "变体不存在", "variant_id": variant_id})
        ref, _ = variant_ref(store, variant)
        if not ref or not Path(ref).is_file():
            raise HTTPException(status_code=404, detail={"code": 3, "message": "参考音频不存在"})
        return FileResponse(ref)

    def _decode_audio(store: ProjectStore, body: dict, variant_id: str, cfg) -> tuple[Path, int, dict]:
        """把前端上传/录音的 base64 音频落盘，转成 24k 单声道 wav，并按需降噪。

        参数:
            store: 工程。
            body: 含 audio_base64 / audio_filename / denoise。
            variant_id: 变体 ID，用作文件名。
            cfg: 本机配置（读 `voice.denoise` 与 `ffmpeg.binary`）。

        返回:
            (wav 绝对路径, 时长毫秒, 降噪记录)。

        异常:
            HTTPException 400: 音频为空、过大、转码失败或时长越界。
        """
        import base64
        import binascii

        from ..media.ffmpeg import binary_available, probe_duration_ms, run

        raw = str(body.get("audio_base64") or "").strip()
        if "," in raw and raw.startswith("data:"):
            raw = raw.split(",", 1)[1]
        if not raw:
            raise HTTPException(status_code=400, detail={"code": 2, "message": "缺少音频数据"})
        try:
            data = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError):
            raise HTTPException(status_code=400, detail={"code": 2, "message": "音频数据不是合法 base64"})
        if len(data) < 2000:
            raise HTTPException(status_code=400, detail={"code": 2, "message": "录音太短，请至少录制 2 秒"})
        if len(data) > 20 * 1024 * 1024:
            raise HTTPException(status_code=400, detail={"code": 2, "message": "音频超过 20MB"})
        if not binary_available("ffmpeg"):
            raise HTTPException(status_code=500, detail={"code": 4, "message": "未找到 ffmpeg，无法转码录音"})

        suffix = Path(str(body.get("audio_filename") or "recording.webm")).suffix or ".webm"
        ref_dir = store.voice_asset_dir("ref")
        ref_dir.mkdir(parents=True, exist_ok=True)
        upload = ref_dir / f".upload-{variant_id}{suffix}"
        raw_wav = ref_dir / f".raw-{variant_id}.wav"
        out = ref_dir / f"{variant_id}.wav"
        upload.write_bytes(data)
        try:
            run(["ffmpeg", "-v", "error", "-y", "-i", str(upload), "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", str(raw_wav)])
        except Exception as exc:
            upload.unlink(missing_ok=True)
            raw_wav.unlink(missing_ok=True)
            out.unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail={"code": 2, "message": f"录音转码失败: {exc}"})
        upload.unlink(missing_ok=True)

        conf = cfg.get("voice.denoise", {}) or {}
        wanted = body.get("denoise")
        if wanted is None:
            wanted = bool(conf.get("default_enabled", True))
        denoise_info = {
            "enabled": False,
            "engine": conf.get("engine"),
            "original_audio_path": None,
            "attenuation_db": None,
            "applied_at": None,
            "note": "未勾选降噪",
        }
        original = ref_dir / f"{variant_id}.orig.wav"
        if wanted:
            from ..media.denoise import denoise_wav

            shutil.copyfile(raw_wav, original)
            result = denoise_wav(raw_wav, out, conf, cfg.get("ffmpeg.binary", "ffmpeg"))
            if result.get("enabled"):
                denoise_info = {
                    "enabled": True,
                    "engine": result.get("engine"),
                    "original_audio_path": str(original.relative_to(store.root)),
                    "attenuation_db": result.get("attenuation_db"),
                    "applied_at": now_iso(),
                    "note": result.get("note"),
                }
            else:
                # 降噪失败或过度抑制：丢弃副本，直接用原音作为正式参考音频
                original.unlink(missing_ok=True)
                denoise_info["note"] = result.get("note")
                os.replace(raw_wav, out)
        else:
            os.replace(raw_wav, out)
        raw_wav.unlink(missing_ok=True)

        duration = probe_duration_ms(out)
        if duration < 2000 or duration > 20000:
            out.unlink(missing_ok=True)
            original.unlink(missing_ok=True)
            raise HTTPException(
                status_code=400,
                detail={"code": 2, "message": f"参考音频需 2~20 秒，当前 {duration / 1000:.1f} 秒"},
            )
        return out, duration, denoise_info

    def _variant_from_upload(store: ProjectStore, body: dict, cfg) -> tuple[dict, Path]:
        """构造一个「真人/上传」音色变体并落盘。

        参数:
            store: 工程。
            body: 含 name / gender / ref_text / consent / recorded / denoise 等。
            cfg: 本机配置（透传给转码与降噪）。

        返回:
            (变体 dict, wav 路径)。

        异常:
            HTTPException 400: 未确认授权或参数非法。
        """
        from ..models import entities

        if not body.get("consent"):
            raise HTTPException(status_code=400, detail={"code": 2, "message": "请先确认你拥有这段录音的使用授权"})
        variant_id = entities.new_id("vav")
        out, duration, denoise_info = _decode_audio(store, body, variant_id, cfg)
        variant = {
            "id": variant_id,
            "name": str(body.get("variant_name") or "默认"),
            "source": "recorded" if body.get("recorded") else "cloned",
            "emotion_tags": ["default"],
            "reference": {
                "audio_path": str(out.relative_to(store.root)),
                "duration_ms": int(duration),
                "text": (body.get("ref_text") or "").strip() or None,
                "denoise": denoise_info,
            },
            "consent": {
                "confirmed": True,
                "note": str(body.get("consent_note") or "Web 音色库上传/录音，用户确认已获授权"),
            },
            "provider_availability": [
                {
                    "provider_id": "indextts_25",
                    "status": "ready",
                    "clone_supported": True,
                    "checked_at": now_iso(),
                }
            ],
            "status": "ready",
            "created_at": now_iso(),
        }
        return variant, out

    @app.post("/api/ui/voices")
    def ui_voices_add(body: dict) -> dict:
        """上传/录制参考音频，新建音色或给已有音色追加变体。

        参数:
            body: `{path, name?, gender?, voice_id?, audio_base64, audio_filename?,
                    ref_text?, consent, recorded?, variant_name?}`。

        返回:
            `{voice, variant_id, denoise, voices, characters, builtin}`。
        """
        from ..models import entities

        path = body.get("path")
        store = _store(path)
        table = store.load_voices()
        variant, out = _variant_from_upload(store, body, _tts_config())
        voice_id = body.get("voice_id")
        if voice_id:
            voice = next((item for item in table["voices"] if item["id"] == voice_id), None)
            if voice is None:
                out.unlink(missing_ok=True)
                raise HTTPException(status_code=404, detail={"code": 3, "message": "音色不存在", "voice_id": voice_id})
            voice["variants"].append(variant)
            if not voice.get("default_variant_id"):
                voice["default_variant_id"] = variant["id"]
            voice["updated_at"] = now_iso()
        else:
            name = str(body.get("name") or "").strip()
            if not name:
                out.unlink(missing_ok=True)
                raise HTTPException(status_code=400, detail={"code": 2, "message": "缺少音色名称"})
            gender = body.get("gender")
            voice = {
                "id": entities.new_id("voc"),
                "name": name[:120],
                "engine": None,
                "default_provider_id": None,
                "language": "zh-CN",
                "gender": gender if gender in {"male", "female", "other", "unknown"} else "unknown",
                "tags": ["自建", "浏览器录音" if body.get("recorded") else "上传"],
                "default_variant_id": variant["id"],
                "variants": [variant],
                "status": "ready",
                "revision": 1,
                "created_at": now_iso(),
                "updated_at": now_iso(),
            }
            table["voices"].append(voice)
        try:
            store.save_voices(table)
        except Exception:
            out.unlink(missing_ok=True)
            raise
        board = _voice_board(path)
        return {"voice": voice, "variant_id": variant["id"], "denoise": variant["reference"].get("denoise"), **board}

    @app.post("/api/ui/voices/bind")
    def ui_voices_bind(body: dict) -> dict:
        """把音色绑定到人物（写入 v2 voice_binding）。

        参数:
            body: `{path, character_id, voice_id}`；voice_id 为空表示解绑。

        异常:
            HTTPException 404: 人物或音色不存在。
        """
        path = body.get("path")
        store = _store(path)
        character_id = body.get("character_id")
        voice_id = body.get("voice_id") or None
        characters = store.load_characters()
        target = next((item for item in characters["characters"] if item["id"] == character_id), None)
        if target is None:
            raise HTTPException(status_code=404, detail={"code": 3, "message": "人物不存在", "character_id": character_id})
        if voice_id is None:
            target["voice_binding"] = None
        else:
            voice = next((item for item in store.load_voices()["voices"] if item["id"] == voice_id), None)
            if voice is None:
                raise HTTPException(status_code=404, detail={"code": 3, "message": "音色不存在", "voice_id": voice_id})
            binding = target.get("voice_binding") or {}
            target["voice_binding"] = {
                "adapter": binding.get("adapter") or "placeholder",
                "voice_id": voice["id"],
                "voice_library_id": voice["id"],
                "default_variant_id": voice["default_variant_id"],
                "emotion_variant_map": binding.get("emotion_variant_map") or {},
            }
        target["updated_at"] = now_iso()
        store.save_characters(characters)
        return {"character": target, "characters": _voice_board(path)["characters"]}

    @app.post("/api/ui/voices/design/preview")
    def ui_voices_design_preview(body: dict) -> dict:
        """根据人物档案生成音色描述，供用户确认后再抽卡。

        参数:
            body: `{path, character_id, prompt?}`；prompt 非空表示用户手动改写。

        返回:
            `{profile, source, note, prompt, script, candidates}`。

        异常:
            HTTPException 400/404: 功能未启用或人物不存在。
        """
        from ..adapters import build_adapters
        from ..tts import voice_design

        store = _store(body.get("path"))
        cfg = _tts_config()
        design = cfg.get("voice.design") or {}
        if not design.get("enabled", True):
            raise HTTPException(status_code=400, detail={"code": 2, "message": "音色设计未启用（voice.design.enabled=false）"})
        characters = store.load_characters()["characters"]
        character = next((item for item in characters if item["id"] == body.get("character_id")), None)
        if character is None:
            raise HTTPException(status_code=404, detail={"code": 3, "message": "人物不存在", "character_id": body.get("character_id")})
        manual = str(body.get("prompt") or "").strip()
        if manual:
            result = {"profile": None, "source": "manual", "note": None, "prompt": manual}
        else:
            adapters = build_adapters(cfg, store.settings())
            built = voice_design.build_profile(adapters.writing, character)
            result = {**built, "prompt": built["profile"]["design_prompt"]}
        return {
            **result,
            "script": design.get("script"),
            "candidates": int(design.get("candidates", 4)),
            "max_keep_days": int(design.get("max_keep_days", 7)),
        }

    @app.post("/api/ui/voices/design/batches")
    def ui_voices_design_start(body: dict) -> dict:
        """启动一次音色抽卡批次（后台线程运行，接口立即返回批次 ID）。

        参数:
            body: `{path, character_id, prompt, profile?, count?}`。

        返回:
            批次元数据。

        异常:
            HTTPException 400/404: 参数缺失、环境未就绪或人物不存在。
        """
        from ..tts import voice_design

        store = _store(body.get("path"))
        cfg = _tts_config()
        design = cfg.get("voice.design") or {}
        if not design.get("enabled", True):
            raise HTTPException(status_code=400, detail={"code": 2, "message": "音色设计未启用"})
        python = str(design.get("python") or "")
        model_dir = str(design.get("model_dir") or "")
        if not Path(python).is_file():
            raise HTTPException(status_code=400, detail={"code": 2, "message": f"VoxCPM2 运行环境不存在：{python}"})
        if not Path(model_dir).is_dir():
            raise HTTPException(status_code=400, detail={"code": 2, "message": f"VoxCPM2 权重目录不存在：{model_dir}"})
        characters = store.load_characters()["characters"]
        character = next((item for item in characters if item["id"] == body.get("character_id")), None)
        if character is None:
            raise HTTPException(status_code=404, detail={"code": 3, "message": "人物不存在", "character_id": body.get("character_id")})
        prompt = str(body.get("prompt") or "").strip()
        if not prompt:
            raise HTTPException(status_code=400, detail={"code": 2, "message": "缺少音色描述"})
        count = int(body.get("count") or design.get("candidates", 4))
        return voice_design.create_batch(store, cfg, character, prompt, count, body.get("profile"))

    @app.get("/api/ui/voices/design/batches/{batch_id}")
    def ui_voices_design_status(batch_id: str, path: str = Query(...)) -> dict:
        """轮询批次进度与候选清单。

        参数:
            batch_id: 批次 ID。
            path: 工程目录。

        异常:
            HTTPException 404: 批次不存在。
        """
        from ..tts import voice_design

        meta = voice_design.read_batch(_store(path), batch_id)
        if not meta:
            raise HTTPException(status_code=404, detail={"code": 3, "message": "批次不存在", "batch_id": batch_id})
        return meta

    @app.post("/api/ui/voices/design/batches/{batch_id}/cancel")
    def ui_voices_design_cancel(batch_id: str, body: dict) -> dict:
        """停止批次后续候选：保留已完成候选，返回 stopped 状态。

        参数:
            batch_id: 批次 ID。
            body: `{path}`。

        异常:
            HTTPException 404: 批次不存在。
        """
        from ..tts import voice_design

        meta = voice_design.cancel_batch(_store(body.get("path")), batch_id)
        if not meta:
            raise HTTPException(status_code=404, detail={"code": 3, "message": "批次不存在", "batch_id": batch_id})
        return {"batch": meta}

    @app.post("/api/ui/voices/design/batches/{batch_id}/retry")
    def ui_voices_design_retry(batch_id: str, body: dict) -> dict:
        """单条重试：重新生成指定序号的失败/缺失候选。

        参数:
            batch_id: 批次 ID。
            body: `{path, index}`。

        异常:
            HTTPException 400: 批次仍在生成或该候选不需要重试。
        """
        from ..tts import voice_design

        store = _store(body.get("path"))
        try:
            return voice_design.retry_candidate(store, _tts_config(), batch_id, int(body.get("index") or 0))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"code": 2, "message": str(exc)}) from exc

    @app.post("/api/ui/voices/design/batch/plan")
    def ui_voices_batch_plan(body: dict) -> dict:
        """预览批量生成范围：待生成 N 个、跳过 M 个及原因，附带可选章节。

        参数:
            body: `{path, scope, chapter_id?}`。
        """
        from ..tts import voice_batch

        store = _store(body.get("path"))
        try:
            return voice_batch.plan(store, str(body.get("scope") or "unbound"), body.get("chapter_id"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"code": 2, "message": str(exc)}) from exc

    @app.post("/api/ui/voices/design/batch")
    def ui_voices_batch_start(body: dict) -> dict:
        """启动一键批量生成音色（逐角色串行）。

        参数:
            body: `{path, scope, chapter_id?, count?}`。
        """
        from ..tts import voice_batch

        store = _store(body.get("path"))
        try:
            return voice_batch.start_job(
                store,
                _tts_config(),
                str(body.get("scope") or "unbound"),
                body.get("chapter_id"),
                body.get("count"),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"code": 2, "message": str(exc)}) from exc

    @app.get("/api/ui/voices/design/batch/latest")
    def ui_voices_batch_latest(path: str | None = None) -> dict:
        """最近一次批量任务：页面刷新后据状态展示或续跑。"""
        from ..tts import voice_batch

        return {"job": voice_batch.latest_job(_store(path))}

    @app.post("/api/ui/voices/design/batch/{job_id}/cancel")
    def ui_voices_batch_cancel(job_id: str, body: dict) -> dict:
        """停止批量任务：当前角色停止，已完成角色保留。"""
        from ..tts import voice_batch

        job = voice_batch.cancel_job(_store(body.get("path")), job_id)
        if not job:
            raise HTTPException(status_code=404, detail={"code": 3, "message": "批量任务不存在"})
        return {"job": job}

    @app.post("/api/ui/voices/design/batch/{job_id}/resume")
    def ui_voices_batch_resume(job_id: str, body: dict) -> dict:
        """续跑或重试批量任务：只处理未完成与失败的角色。

        参数:
            body: `{path, character_ids?}`；character_ids 为空时重试全部失败项。
        """
        from ..tts import voice_batch

        try:
            job = voice_batch.resume_job(
                _store(body.get("path")), _tts_config(), job_id, body.get("character_ids")
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"code": 2, "message": str(exc)}) from exc
        return {"job": job}

    @app.get("/api/ui/voices/casting/overview")
    def ui_voices_casting_overview(
        path: str = Query(...), q: str = "", filter_name: str = Query("need", alias="filter")
    ) -> dict:
        """音色编排总览：默认只返回需要处理的角色，正常角色只给摘要。

        参数:
            path: 工程目录。
            q: 搜索词（角色名或当前音色名）。
            filter_name: need/unbound/chapter/duplicate/all。
        """
        from ..tts import voice_casting

        store = _store(path)
        data = voice_casting.overview(store, q, filter_name)
        data["undo"] = voice_casting.undo_state(store)
        return data

    @app.post("/api/ui/voices/casting/preview")
    def ui_voices_casting_preview(body: dict) -> dict:
        """自动编排预览：每个角色匹配到哪个音色、为什么，以及需对齐的章节覆盖。"""
        from ..tts import voice_casting

        return voice_casting.preview(_store(body.get("path")))

    @app.post("/api/ui/voices/casting/apply")
    def ui_voices_casting_apply(body: dict) -> dict:
        """应用自动编排建议并写撤销日志。

        参数:
            body: `{path, character_ids?}`；为空时应用全部有变化的角色。
        """
        from ..tts import voice_casting

        store = _store(body.get("path"))
        result = voice_casting.apply(store, body.get("character_ids"))
        return {
            **result,
            "overview": voice_casting.overview(store, "", "need"),
            "undo": voice_casting.undo_state(store),
        }

    @app.post("/api/ui/voices/casting/bind")
    def ui_voices_casting_bind(body: dict) -> dict:
        """单行替换角色音色（voice_id 为空表示解绑），并写撤销日志。

        异常:
            HTTPException 400/404: 角色或音色不存在 / 没有可用变体。
        """
        from ..tts import voice_casting

        store = _store(body.get("path"))
        try:
            result = voice_casting.bind_one(store, body.get("character_id"), body.get("voice_id"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"code": 2, "message": str(exc)}) from exc
        return {
            **result,
            "overview": voice_casting.overview(store, "", "need"),
            "undo": voice_casting.undo_state(store),
        }

    @app.post("/api/ui/voices/undo/last")
    def ui_voices_undo_last(body: dict) -> dict:
        """撤销最近一次自动编排 / 自动生成 / 单行替换。

        参数:
            body: `{path, kind?}`；kind 为空时撤销最近一条记录。
        """
        from ..tts import voice_casting

        store = _store(body.get("path"))
        try:
            result = voice_casting.undo_last(store, body.get("kind") or None)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"code": 2, "message": str(exc)}) from exc
        return {
            **result,
            "overview": voice_casting.overview(store, "", "need"),
            "undo": voice_casting.undo_state(store),
        }

    @app.get("/api/ui/voices/design/history")
    def ui_voices_design_history(path: str = Query(...), character_id: str = Query(...)) -> dict:
        """从磁盘恢复某角色的历史候选批次（页面刷新后使用）。

        参数:
            path: 工程目录。
            character_id: 人物 ID。

        返回:
            `{batches: [...]}`，倒序；含批次 ID、生成时间、候选是否仍在、已采用序号。
        """
        from ..tts import voice_design

        return {"batches": voice_design.list_batches(_store(path), character_id)}

    @app.get("/api/ui/voices/design/audio")
    def ui_voices_design_audio(path: str = Query(...), batch_id: str = Query(...), index: int = Query(...)):
        """返回候选音频，供页面逐条试听。

        异常:
            HTTPException 404: 批次/候选/文件不存在。
        """
        from fastapi.responses import FileResponse

        from ..tts import voice_design

        store = _store(path)
        meta = voice_design.read_batch(store, batch_id)
        candidate = next((item for item in meta.get("candidates") or [] if int(item.get("index")) == index), None)
        if candidate is None:
            raise HTTPException(status_code=404, detail={"code": 3, "message": "候选不存在", "index": index})
        target = voice_design.batch_dir(store, batch_id) / candidate["file"]
        if not target.is_file():
            raise HTTPException(status_code=404, detail={"code": 3, "message": "候选音频已过期"})
        return FileResponse(target)

    @app.post("/api/ui/voices/design/select")
    def ui_voices_design_select(body: dict) -> dict:
        """采用某条候选：固化参考音并（可选）绑定人物。

        参数:
            body: `{path, batch_id, index, name, gender, gender_source, bind, voice_id?}`。

        异常:
            HTTPException 400/404: 批次未就绪或参数非法。
        """
        from ..tts import voice_design

        path = body.get("path")
        store = _store(path)
        try:
            result = voice_design.select_candidate(
                store,
                str(body.get("batch_id") or ""),
                int(body.get("index") or 0),
                str(body.get("name") or ""),
                str(body.get("gender") or "unknown"),
                str(body.get("gender_source") or "manual"),
                bool(body.get("bind")),
                body.get("voice_id"),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"code": 2, "message": str(exc)}) from exc
        return {**result, **_voice_board(path)}

    @app.delete("/api/ui/voices/design/batches/{batch_id}")
    def ui_voices_design_delete(batch_id: str, path: str = Query(...)) -> dict:
        """手动清理一个候选批次。"""
        from ..tts import voice_design

        return {"deleted": voice_design.delete_batch(_store(path), batch_id)}

    @app.post("/api/ui/voices/delete")
    def ui_voices_delete(body: dict) -> dict:
        """删除音色，并解绑引用它的角色。

        参数:
            body: `{path, voice_id}`。

        异常:
            HTTPException 404: 音色不存在。
        """
        path = body.get("path")
        store = _store(path)
        voice_id = body.get("voice_id")
        table = store.load_voices()
        before = len(table["voices"])
        table["voices"] = [item for item in table["voices"] if item["id"] != voice_id]
        if len(table["voices"]) == before:
            raise HTTPException(status_code=404, detail={"code": 3, "message": "音色不存在", "voice_id": voice_id})
        store.save_voices(table)
        characters = store.load_characters()
        changed = False
        for item in characters["characters"]:
            binding = item.get("voice_binding") or {}
            if voice_id in (binding.get("voice_library_id"), binding.get("voice_id")):
                item["voice_binding"] = None
                item["updated_at"] = now_iso()
                changed = True
        if changed:
            store.save_characters(characters)
        return _voice_board(path)

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
    # 单行重录 / 版本对比 / Casting 试听 / 整本批量（独立模块）
    register_voice_tools_routes(app, config_path)
    # 智能对轨：上传播音按静音切分写入台词行（独立模块）
    register_align_routes(app, config_path)
    # 人物音色 / 外貌中英文描述补全（独立模块）
    register_character_desc_routes(app, config_path)
    # 章节/大纲续写（独立模块）
    register_continue_routes(app, config_path)
    # 大纲编辑：删除节拍 / 删除整章（独立模块）
    register_outline_edit_routes(app)
    # 页面实时流：章节正文生成 / 续写（SSE，独立模块）
    register_stream_routes(app, config_path)
    # 配乐生成：叙事 / 铺底两档（独立模块）
    register_bgm_routes(app, config_path)
    # 媒体页音效生成：Stable Audio 3 provider + 进度轮询（独立模块）
    register_sfx_routes(app, config_path)

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

    # 前端只有 web-next 一套：直接挂在根路径。hash 路由不需要服务端回退。
    # 缺少目录时直接报错，不回退到旧的 static 界面。
    if not WEB_NEXT_DIR.exists():
        raise RuntimeError(f"缺少前端目录：{WEB_NEXT_DIR}；请从仓库根启动，旧 static 界面已废弃")
    app.mount("/", NoCacheStaticFiles(directory=WEB_NEXT_DIR, html=True), name="web")

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
