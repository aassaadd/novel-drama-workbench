"""模型管理接口的辅助函数：作业进度、资产清单与模板匹配。"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from pathlib import Path

from novel_drama.composition import comfy_media, comfy_models
from novel_drama.infrastructure.config import (
    _get_dotted,
    load_config,
)
from novel_drama.tts import model_store

#: 本模块日志：配置键等排查信息只写日志，不回显到界面
logger = logging.getLogger(__name__)

#: 后台下载任务表：`job_id -> {status, group, key, started_at, finished_at, result, logs}`
_JOBS: dict[str, dict] = {}

#: 任务表并发写锁（FastAPI 同步路由跑在线程池里）


_JOBS_LOCK = threading.Lock()

#: 每个任务最多保留的日志行数
_MAX_LOG_LINES = 200

#: 视为成功的安装状态
_OK_STATUS = ("installed", "already")


def resolve_llm_settings(
    config: dict, body: dict | None, allow_empty_model: bool = False
) -> tuple[dict, str]:
    """按本次提交的参数装配 omlx 设置，供「检测连接」与「保存校验」共用。

    参数:
        config: 本机配置（`load_config` 的结果）。
        body: 本次提交的 `{endpoint, api_key, model}`；留空表示沿用已保存值。
        allow_empty_model: 为 True 时，body 显式传 `model: ""` 表示「未选模型」，
            不再回落到已保存的模型名。

    返回:
        `(settings, model)`；endpoint / api_key / model 均保持原样，不做路径规范化。
    """
    settings = dict(config.get("omlx", {}) or {})
    for key in ("endpoint", "api_key"):
        value = str((body or {}).get(key) or "").strip()
        if value:
            settings[key] = value
    if allow_empty_model and isinstance(body, dict) and "model" in body:
        settings["model"] = str(body.get("model") or "")
    else:
        value = str((body or {}).get("model") or "")
        if value:
            settings["model"] = value
    return settings, str(settings.get("model") or "")


def _now() -> str:
    """当前本地时间字符串。

    返回:
        形如 `2026-11-10T22:30:00` 的本地时间。
    """
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _human_bytes(size: int) -> str:
    """把字节数转成人读单位。

    参数:
        size: 字节数。

    返回:
        形如 `11.3 GB` 的字符串；0 返回 `—`。
    """
    value = float(size or 0)
    if value <= 0:
        return "—"
    units = ("B", "KB", "MB", "GB", "TB")
    index = 0
    while value >= 1024 and index < len(units) - 1:
        value /= 1024
        index += 1
    return f"{value:.1f} {units[index]}" if index else f"{int(value)} B"


def _job_log(job: dict, line: str) -> None:
    """往任务日志追加一行（最多保留 200 行）。

    参数:
        job: 任务字典。
        line: 日志文本。

    返回:
        无。
    """
    job["logs"].append(line)
    if len(job["logs"]) > _MAX_LOG_LINES:
        del job["logs"][: len(job["logs"]) - _MAX_LOG_LINES]


def _err_message(exc: BaseException) -> str:
    """取异常的中文消息（AdapterError 用 message，其它用 str）。

    参数:
        exc: 异常对象。

    返回:
        可读的错误文本。
    """
    message = getattr(exc, "message", None)
    return str(message or exc)


def _config_source(config, dotted: str) -> str:
    """判定配置项的真实来源，供页面标注默认值的出处。

    参数:
        config: AppConfig（含 `ui_values` 页面覆盖层与 `base_values` 基础配置）。
        dotted: 点号分隔的配置键。

    返回:
        `page` 页面覆盖层 / `file` 基础配置文件 / `builtin` 代码内置默认。
    """
    if _get_dotted(config.ui_values or {}, dotted) is not None:
        return "page"
    if _get_dotted(config.base_values or {}, dotted) is not None:
        return "file"
    return "builtin"


def _template_kinds(config) -> dict[str, set[str]]:
    """按用途归类清单里的模板 ID。

    参数:
        config: AppConfig。

    返回:
        `{"image": {模板ID...}, "video": {模板ID...}, "music": {模板ID...}}`；
        清单缺失时图像/视频回落到内置模板 ID。
    """
    groups: dict[str, set[str]] = {"image": set(), "video": set(), "music": set()}
    try:
        manifest = comfy_media.load_manifest(comfy_media.templates_dir(config))
    except Exception:  # 清单缺失/损坏只影响分组，不影响其它接口
        manifest = {}
    for entry in comfy_media.manifest_entries(manifest):
        template_id = str(entry.get("id") or entry.get("template_id") or "")
        kind = str(entry.get("kind") or "")
        if not template_id:
            continue
        if kind == "image":
            groups["image"].add(template_id)
        elif kind == "video":
            groups["video"].add(template_id)
        elif kind == "music":
            groups["music"].add(template_id)
    for kind in ("image", "video"):
        if groups[kind]:
            continue
        try:
            groups[kind].update(comfy_media.builtin_template_ids(kind))
        except Exception:
            continue
    return groups


def _comfy_rows(config) -> tuple[str | None, list[dict], str | None]:
    """扫描 ComfyUI 模板依赖的模型（不写盘）。

    参数:
        config: AppConfig。

    返回:
        `(models_dir, 模型行列表, 错误消息)`；目录无法定位或扫描失败时行列表为空。
    """
    try:
        root = comfy_models.resolve_models_dir(config)
    except Exception as exc:
        return None, [], _err_message(exc)
    try:
        scanned = comfy_models.scan(config, write=False)
    except Exception as exc:
        return str(root), [], _err_message(exc)

    cache = (comfy_models.cached_resolution(config) or {}).get("items") or {}
    rows: list[dict] = []
    for item in scanned.get("models") or []:
        key = str(item.get("key") or "")
        if not key:
            continue
        resolved = cache.get(key) or {}
        repo = resolved.get("repo") or item.get("repo")
        hf_filename = resolved.get("hf_filename") or item.get("hf_filename")
        url = resolved.get("url")
        expected = int(item.get("expected_bytes") or 0)
        actual = int(item.get("bytes") or 0)
        present = bool(item.get("present"))
        # 完整 = 文件存在且（没有登记期望体积 或 实际体积与期望一致）
        complete = present and (not expected or actual == expected)
        reasons: list[str] = []
        if present and not complete:
            if actual <= 0:
                reasons.append("文件为空")
            elif actual < expected:
                reasons.append(f"文件未下全：已下 {_human_bytes(actual)} / 期望 {_human_bytes(expected)}")
            else:
                reasons.append(f"文件大小不符：已下 {_human_bytes(actual)} / 期望 {_human_bytes(expected)}")
        rows.append(
            {
                "key": key,
                "kind": item.get("kind"),
                "target_path": str(root / key),
                "bytes": int(item.get("bytes") or 0),
                "expected_bytes": int(item.get("expected_bytes") or 0),
                "present": present,
                "complete": complete,
                "reasons": reasons,
                "missing_files": [],
                "templates": list(item.get("templates") or []),
                "license": item.get("license"),
                "license_gate": item.get("license_gate") or "none",
                "commercial_use": bool(item.get("commercial_use")),
                "note": item.get("note"),
                "repo": repo,
                "hf_filename": hf_filename,
                "url": url,
                "resolved": bool(url or (repo and hf_filename)),
                "error": resolved.get("error"),
            }
        )
    return str(root), rows, None


def _audio_rows(config) -> tuple[str, list[dict], str | None]:
    """扫描音频模型目录，并补齐页面需要的字段。

    参数:
        config: AppConfig。

    返回:
        `(models_root, 模型行列表, 错误消息)`。
    """
    root = model_store.models_root(config)
    try:
        scanned = model_store.scan(config)
    except Exception as exc:
        return str(root), [], _err_message(exc)

    rows: list[dict] = []
    for item in scanned:
        key = str(item.get("key") or "")
        catalog = model_store.catalog_entry(key) or {}
        gate = catalog.get("license_gate") or item.get("license_gate") or "none"
        rows.append(
            {
                **item,
                "hf_repo": catalog.get("hf_repo"),
                "repo": catalog.get("hf_repo"),
                "hf_filename": None,
                "url": None,
                "target_path": str(root / key),
                "license_gate": gate,
                "commercial_use": bool(item.get("commercial_use")),
                "downloadable": gate != "retired",
                "resolved": bool(catalog.get("hf_repo")),
                "error": None,
                # 音频的 missing 里既有缺失文件名，也可能带「大小不符：a/b」说明，原样交给页面展示
                "missing_files": list(item.get("missing") or []),
                "reasons": [],
            }
        )
    return str(root), rows, None


def _split_groups(
    rows: list[dict], groups: dict[str, set[str]]
) -> tuple[list[dict], list[dict], list[dict]]:
    """把图像/视频/音乐模型行按模板归属拆成三组（同一行只进一组）。

    参数:
        rows: 扫描出的模型行。
        groups: `{"image": {模板ID}, "video": {模板ID}, "music": {模板ID}}`。

    返回:
        `(图像模型行, 视频模型行, 音乐模型行)`。
    """
    images: list[dict] = []
    videos: list[dict] = []
    music: list[dict] = []
    for row in rows:
        templates = {str(item) for item in row.get("templates") or []}
        if templates & groups["image"]:
            images.append(row)
        elif templates & groups["video"]:
            videos.append(row)
        elif templates & groups["music"]:
            music.append(row)
    return images, videos, music


def _find_comfy_entry(key: str) -> dict | None:
    """按相对路径查 ComfyUI 模板依赖清单项。

    参数:
        key: 相对 models_dir 的路径。

    返回:
        清单项或 None。
    """
    for item in comfy_models.REQUIRED_MODELS:
        if item["key"] == key:
            return item
    return None


def _license_gate(group: str, key: str) -> tuple[str, dict | None]:
    """取某个模型行的许可门禁。

    参数:
        group: audio / image / video。
        key: 音频目录键或 ComfyUI 相对路径。

    返回:
        `(门禁取值, 清单项)`；未知模型返回 `("", None)`。
    """
    entry = model_store.catalog_entry(key) if group == "audio" else _find_comfy_entry(key)
    if entry is None:
        return "", None
    return str(entry.get("license_gate") or "none"), entry


def _result_lines(result: dict) -> list[str]:
    """把安装结果整理成几条关键日志。

    参数:
        result: `model_store.install` 或 `comfy_models.install` 的返回值。

    返回:
        日志行列表。
    """
    lines = [f"结果状态：{result.get('status') or '未知'}"]
    if result.get("mirrors"):
        lines.append("镜像链：" + " → ".join(str(item) for item in result["mirrors"]))
    if result.get("mirror_used"):
        lines.append(f"实际镜像：{result['mirror_used']}")
    size = result.get("bytes") or result.get("total_bytes")
    if size:
        lines.append(f"体积：{_human_bytes(int(size))}")
    if result.get("error"):
        lines.append(f"错误：{result['error']}")
    if result.get("message"):
        lines.append(f"说明：{result['message']}")
    return lines


def _progress_loop(job: dict, stop: threading.Event) -> None:
    """后台轮询暂存目录体积，把下载百分比写回任务进度。

    参数:
        job: 任务字典（含 `progress`）。
        stop: 停止信号。

    返回:
        无。
    """
    while not stop.wait(1.0):
        progress = job.get("progress") or {}
        staging = progress.get("staging")
        total = int(progress.get("total_bytes") or 0)
        if not staging or total <= 0:
            continue
        done = model_store.dir_size(Path(staging))
        progress["done_bytes"] = done
        progress["percent"] = round(min(99.9, done * 100.0 / total), 1)


def _run_job(job_id: str, config_path: str | None, group: str, key: str,
             accept_license: bool, yes: bool) -> None:
    """后台线程入口：执行一次模型下载并把结果写回任务表。

    参数:
        job_id: 任务 ID。
        config_path: 本机配置文件路径（线程内重新加载，避免复用过期配置）。
        group: audio / image / video。
        key: 音频目录键或 ComfyUI 相对路径。
        accept_license: 是否已勾选理解许可限制。
        yes: 是否已确认体积。

    返回:
        无（结果与日志写进 `_JOBS[job_id]`）。
    """
    job = _JOBS[job_id]
    stop = threading.Event()
    watcher = threading.Thread(target=_progress_loop, args=(job, stop), daemon=True, name=f"ndw-model-progress-{job_id}")
    watcher.start()
    try:
        config = load_config(config_path)
        mirrors = model_store.download_mirrors(config)
        _job_log(job, f"[{_now()}] 开始下载 {group}:{key}")
        _job_log(job, "镜像链：" + " → ".join(mirrors))
        if group == "audio":
            result = model_store.install(
                config,
                key,
                yes=yes,
                accept_license=accept_license,
                mirrors=mirrors,
                progress=job.get("progress"),
            )
        else:
            result = comfy_models.install(
                config,
                key,
                yes=yes,
                accept_license=accept_license,
                mirrors=mirrors,
            )
        for line in _result_lines(result):
            _job_log(job, line)
        job["result"] = result
        job["status"] = "done" if result.get("status") in _OK_STATUS else "failed"
    except Exception as exc:  # 线程内异常必须落到日志里，不能让线程静默退出
        _job_log(job, f"异常：{type(exc).__name__}: {_err_message(exc)}")
        job["result"] = {"status": "error", "key": key, "error": _err_message(exc)}
        job["status"] = "failed"
    finally:
        stop.set()
        watcher.join(timeout=2)
        progress = job.get("progress") or {}
        if job.get("status") == "done":
            progress.update({"stage": "done", "percent": 100.0})
        elif job.get("status") == "failed":
            progress["stage"] = "failed"
        job["finished_at"] = _now()


def _object_info_index(info: dict) -> tuple[set[str], set[str]]:
    """从 ComfyUI `/object_info` 提取节点类型与所有下拉选项字符串。

    参数:
        info: `/object_info` 返回的字典（class_type → 节点定义）。

    返回:
        `(节点类型集合, 下拉选项集合)`；下拉选项用于校验模型文件名是否存在。
    """
    nodes: set[str] = set()
    options: set[str] = set()
    for class_type, spec in (info or {}).items():
        if not isinstance(spec, dict):
            continue
        nodes.add(str(class_type))
        inputs = spec.get("input") if isinstance(spec.get("input"), dict) else {}
        for section in ("required", "optional"):
            group = inputs.get(section) if isinstance(inputs.get(section), dict) else {}
            for value in group.values():
                # 下拉项形如 [[选项1, 选项2], {...}]，第一项才是候选列表
                if isinstance(value, list) and value and isinstance(value[0], list):
                    options.update(str(item) for item in value[0] if isinstance(item, str))
    return nodes, options


def _template_class_types(directory, entry: dict) -> set[str]:
    """读取模板文件，返回其中出现的节点 class_type 集合。

    参数:
        directory: 模板目录。
        entry: manifest 里的模板项。

    返回:
        class_type 集合；文件缺失或损坏时返回空集合。
    """
    file_name = str(entry.get("file") or f"{entry.get('id') or ''}.json")
    path = Path(file_name).expanduser()
    if not path.is_absolute():
        path = Path(directory) / path
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    types: set[str] = set()
    if isinstance(data, dict):
        for node in data.values():
            if isinstance(node, dict) and node.get("class_type"):
                types.add(str(node["class_type"]))
    return types
