"""模型与下载接口（设置页「模型与下载」卡片专用）。

职责边界：
- 只读接口展示模型清单与线上地址缓存，扫描时不写盘（`scan(config, write=False)`），
  也不在只读接口里发起网络请求；
- 下载放到后台线程执行，接口立即返回 `job_id`，页面按固定间隔轮询进度；
- ComfyUI 健康检查把「连不上」转成 `ok=false` + 中文说明，HTTP 仍返回 200。

与 `app.py` 的分工：配置读写仍留在 app.py，本模块只做清单 / 下载 / 自检，
避免 app.py 继续膨胀。
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import HTTPException

from ..adapters.comfyui import LocalComfyGateway
from ..adapters.writing import OmlxWritingAdapter, last_llm_call
from ..config import DEFAULT_UI_CONFIG_PATH, load_config, save_ui_config
from ..media import comfy_media, comfy_models
from ..tts import model_store
from ..writing.service import _tool_max_tokens

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


def register_models_routes(app, config_path: str | None = None) -> None:
    """把模型清单 / 下载 / ComfyUI 自检接口注册到应用上。

    参数:
        app: FastAPI 实例。
        config_path: 本机配置文件路径（与 app.py 其它路由同源）。

    返回:
        无。
    """

    @app.get("/api/ui/models")
    def ui_models() -> dict:
        """模型清单：音频 / 图像 / 视频 / 音乐四组 + 目录与镜像链（只读、不联网、不写盘）。"""
        config = load_config(config_path)
        groups = _template_kinds(config)
        comfy_dir, comfy_rows, comfy_error = _comfy_rows(config)
        images, videos, music = _split_groups(comfy_rows, groups)
        audio_root, audio_rows, audio_error = _audio_rows(config)
        return {
            "dirs": {"models_root": audio_root, "comfy_models_dir": comfy_dir},
            "mirrors": model_store.download_mirrors(config),
            "audio": {"root": audio_root, "models": audio_rows, "message": audio_error},
            "image": {"models": images, "message": comfy_error},
            "video": {"models": videos, "message": comfy_error},
            "music": {"models": music, "message": comfy_error},
        }

    @app.post("/api/ui/models/install")
    def ui_models_install(body: dict) -> dict:
        """启动一次模型下载（后台线程），立即返回 job_id。

        参数:
            body: `{group: audio|image|video, key, accept_license, yes}`。

        返回:
            `{"job_id": "..."}`。

        异常:
            HTTPException 400: 参数不合法、模型未知，或需确认许可却没勾选。
        """
        group = str(body.get("group") or "")
        key = str(body.get("key") or "").strip()
        if group not in ("audio", "image", "video", "music") or not key:
            raise HTTPException(status_code=400, detail={"code": 2, "message": "需要 group（audio/image/video/music）与 key"})
        gate, entry = _license_gate(group, key)
        if entry is None:
            raise HTTPException(status_code=400, detail={"code": 2, "message": f"未知模型：{group}:{key}"})
        accept_license = bool(body.get("accept_license"))
        if gate not in ("", "none") and not accept_license:
            raise HTTPException(
                status_code=400,
                detail={"code": 2, "message": "该模型许可需确认，请先勾选已理解许可限制"},
            )

        job_id = uuid.uuid4().hex[:12]
        with _JOBS_LOCK:
            _JOBS[job_id] = {
                "job_id": job_id,
                "status": "running",
                "group": group,
                "key": key,
                "started_at": _now(),
                "finished_at": None,
                "result": None,
                "logs": [],
                "progress": {"stage": "preparing", "done_bytes": 0, "total_bytes": 0, "percent": None},
            }
        thread = threading.Thread(
            target=_run_job,
            args=(job_id, config_path, group, key, accept_license, bool(body.get("yes"))),
            daemon=True,
            name=f"ndw-model-install-{job_id}",
        )
        thread.start()
        return {"job_id": job_id}

    @app.get("/api/ui/models/install/{job_id}")
    def ui_models_install_status(job_id: str) -> dict:
        """查询下载任务状态（日志只回尾部 40 行）。

        参数:
            job_id: `POST /api/ui/models/install` 返回的任务 ID。

        返回:
            `{job_id, status, group, key, started_at, finished_at, logs, result}`。

        异常:
            HTTPException 404: 任务不存在。
        """
        job = _JOBS.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail={"code": 3, "message": f"下载任务不存在：{job_id}"})
        return {
            "job_id": job["job_id"],
            "status": job["status"],
            "group": job["group"],
            "key": job["key"],
            "started_at": job["started_at"],
            "finished_at": job["finished_at"],
            "logs": list(job["logs"][-40:]),
            "result": job["result"],
            "progress": job.get("progress"),
        }

    @app.post("/api/ui/models/resolve")
    def ui_models_resolve(body: dict) -> dict:
        """重新反查线上地址（图像/视频联网；音频直接回仓库名）。

        参数:
            body: `{group, key}`。

        返回:
            `{ok, group, key, repo, hf_filename, url, error}`。

        异常:
            HTTPException 400: 参数不合法、模型未知，或解析失败。
        """
        group = str(body.get("group") or "")
        key = str(body.get("key") or "").strip()
        if group not in ("audio", "image", "video", "music") or not key:
            raise HTTPException(status_code=400, detail={"code": 2, "message": "需要 group（audio/image/video/music）与 key"})
        config = load_config(config_path)
        if group == "audio":
            entry = model_store.catalog_entry(key)
            if entry is None:
                raise HTTPException(status_code=400, detail={"code": 2, "message": f"未知音频模型：{key}"})
            return {
                "ok": True,
                "group": "audio",
                "key": key,
                "repo": entry.get("hf_repo"),
                "hf_filename": None,
                "url": None,
                "error": None,
                "message": "音频模型按整仓库下载，无需解析",
            }

        if _find_comfy_entry(key) is None:
            raise HTTPException(status_code=400, detail={"code": 2, "message": f"未知图像/视频模型：{key}"})
        try:
            result = comfy_models.resolve(config, keys=[key], refresh=True)
        except Exception as exc:
            raise HTTPException(status_code=400, detail={"code": 2, "message": f"解析失败：{_err_message(exc)}"}) from exc
        item = (result.get("items") or {}).get(key) or {}
        if item.get("error") and not item.get("repo"):
            raise HTTPException(status_code=400, detail={"code": 2, "message": f"解析失败：{item['error']}"})
        return {
            "ok": True,
            "group": group,
            "key": key,
            "repo": item.get("repo"),
            "hf_filename": item.get("hf_filename"),
            "url": item.get("url"),
            "error": item.get("error"),
        }

    @app.post("/api/ui/models/manual")
    def ui_models_manual(body: dict) -> dict:
        """手工补录图像/视频模型的线上来源（仓库 + 文件名，或直接下载地址）。

        参数:
            body: `{group, key, repo, hf_filename, url}`；group 只能是 image / video。

        返回:
            `{ok, group, key, item}`。

        异常:
            HTTPException 400: 参数不合法、模型未知，或补录内容不完整。
        """
        group = str(body.get("group") or "")
        key = str(body.get("key") or "").strip()
        if group not in ("image", "video", "music") or not key:
            raise HTTPException(status_code=400, detail={"code": 2, "message": "需要 group（image/video/music）与 key"})
        config = load_config(config_path)
        try:
            result = comfy_models.save_manual(
                config,
                key,
                repo=body.get("repo"),
                hf_filename=body.get("hf_filename"),
                url=body.get("url"),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"code": 2, "message": str(exc)}) from exc
        return {"group": group, **result}

    #: 仓库根目录：配置里的相对路径统一按它解析
    REPO_ROOT = Path(__file__).resolve().parents[3]

    def _resolve_config_path(value: str) -> Path:
        """把配置里的路径解析成绝对路径（相对路径按仓库根目录）。

        参数:
            value: 配置中的路径文本，可为空。

        返回:
            解析后的绝对路径。
        """
        raw = Path(value or ".").expanduser()
        return raw if raw.is_absolute() else (REPO_ROOT / raw)

    @app.get("/api/ui/comfy/workflows")
    def ui_comfy_workflows(templates_dir: str = "") -> dict:
        """列出工作流目录里实际存在的工作流，供设置页做下拉选择。

        参数:
            templates_dir: 工作流目录；留空表示使用当前生效配置。

        返回:
            `{ok, dir, resolved_dir, workflows, message}`，读取失败时 ok=false 并给中文原因。
        """
        target = (templates_dir or "").strip()
        if not target:
            configured = (load_config(config_path).get("comfyui", {}) or {}).get("templates_dir") or ""
            target = str(configured)
        resolved = _resolve_config_path(target)
        if not resolved.is_dir():
            return {
                "ok": False,
                "dir": target,
                "resolved_dir": str(resolved),
                "workflows": [],
                "message": f"工作流目录不存在：{resolved}",
            }
        workflows: list[dict] = []
        manifest = resolved / "manifest.json"
        if manifest.is_file():
            try:
                payload = json.loads(manifest.read_text(encoding="utf-8"))
                for item in payload.get("templates", []):
                    if not isinstance(item, dict):
                        continue
                    workflows.append(
                        {
                            "id": item.get("id") or Path(str(item.get("file", ""))).stem,
                            "file": item.get("file") or "",
                            "kind": item.get("kind") or "",
                            "display_name": item.get("display_name") or item.get("id") or "",
                        }
                    )
            except (OSError, ValueError):
                workflows = []
        if not workflows:
            skip = {"manifest.json", "models.json", "models.local.json", "styles.json"}
            for file in sorted(resolved.glob("*.json")):
                if file.name in skip:
                    continue
                workflows.append({"id": file.stem, "file": file.name, "kind": "", "display_name": file.stem})
        if not workflows:
            return {
                "ok": False,
                "dir": target,
                "resolved_dir": str(resolved),
                "workflows": [],
                "message": f"目录里没有找到工作流文件：{resolved}",
            }
        return {
            "ok": True,
            "dir": target,
            "resolved_dir": str(resolved),
            "workflows": workflows,
            "message": f"已读取 {len(workflows)} 个工作流",
        }

    @app.get("/api/ui/fs/dirs")
    def ui_fs_dirs(path: str = "") -> dict:
        """列出目录下的子目录，供「模型目录 / 工作流目录」等路径项做下拉建议。

        参数:
            path: 目录路径；空串表示仓库根目录，相对路径按仓库根目录解析。

        返回:
            `{ok, path, resolved_dir, exists, dirs, message}`；不存在时 ok=false 并给中文原因。
        """
        resolved = _resolve_config_path((path or "").strip())
        if not resolved.exists():
            return {
                "ok": False,
                "path": path,
                "resolved_dir": str(resolved),
                "exists": False,
                "dirs": [],
                "message": f"目录不存在：{resolved}",
            }
        if not resolved.is_dir():
            resolved = resolved.parent
        try:
            dirs = sorted(
                item.name
                for item in resolved.iterdir()
                if item.is_dir() and not item.name.startswith(".")
            )
        except OSError as exc:
            return {
                "ok": False,
                "path": path,
                "resolved_dir": str(resolved),
                "exists": True,
                "dirs": [],
                "message": f"目录不可读：{exc.strerror or exc}",
            }
        return {
            "ok": True,
            "path": path,
            "resolved_dir": str(resolved),
            "exists": True,
            "dirs": dirs,
            "message": f"已读取 {len(dirs)} 个子目录",
        }

    #: 设置页可写的连接配置白名单：点分键 -> (中文名, 值类型)
    CONNECTION_FIELDS = {
        "comfyui.base_url": ("ComfyUI 服务地址", str),
        "comfyui.templates_dir": ("ComfyUI 工作流目录", str),
        "comfyui.models_dir": ("ComfyUI 模型目录", str),
        "comfyui.templates.image": ("图像工作流", str),
        "comfyui.templates.video_t2v": ("文生视频工作流", str),
        "comfyui.templates.video_i2v": ("图生视频工作流", str),
        "omlx.endpoint": ("写作模型接口地址", str),
        "omlx.api_key": ("写作模型 API Key", str),
        "omlx.model": ("写作模型名称", str),
        "omlx.request_timeout_s": ("请求超时（秒）", (int, float)),
        "omlx.connect_timeout_s": ("连接超时（秒）", (int, float)),
        "omlx.max_concurrency": ("最大并发", (int, float)),
        "omlx.first_token_timeout_s": ("首字超时（秒）", (int, float)),
        "omlx.context_window": ("上下文窗口（tokens）", int),
        "omlx.max_tokens": ("最大输出（tokens）", int),
        "omlx.enable_thinking": ("开启思考", bool),
        "omlx.reasoning_effort": ("思考强度", str),
    }

    #: 思考强度合法档位（与设置页下拉一致）
    THINKING_EFFORTS = ("low", "medium", "high")

    def _connection_state(config) -> dict:
        """把当前生效的连接配置整理成页面可编辑的结构（不回显密钥原文）。"""
        comfy = dict(config.get("comfyui", {}) or {})
        llm = dict(config.get("omlx", {}) or {})
        return {
            "config_path": str(config.path or config_path or ""),
            "ui_config_path": str(config.ui_path or DEFAULT_UI_CONFIG_PATH),
            "comfyui": {
                "base_url": comfy.get("base_url") or "",
                "templates_dir": comfy.get("templates_dir") or "",
                "models_dir": comfy.get("models_dir") or "",
                "templates": dict(comfy.get("templates") or {}),
            },
            "llm": {
                "endpoint": llm.get("endpoint") or "",
                "model": llm.get("model") or "",
                "api_key_set": bool(llm.get("api_key")),
                "request_timeout_s": llm.get("request_timeout_s"),
                "connect_timeout_s": llm.get("connect_timeout_s"),
                "max_concurrency": llm.get("max_concurrency"),
                "first_token_timeout_s": llm.get("first_token_timeout_s"),
                # 上下文 / 最大输出由「测试连通」的服务端返回值回填，也可手改后保存
                "context_window": llm.get("context_window"),
                "max_output_tokens": llm.get("max_tokens"),
                "thinking_enabled": bool(llm.get("enable_thinking")),
                "thinking_effort": str(llm.get("reasoning_effort") or "medium"),
            },
        }

    @app.get("/api/ui/config/connection")
    def ui_config_connection() -> dict:
        """读取全局连接配置（ComfyUI / 写作模型）与配置存放位置。"""
        return _connection_state(load_config(config_path))

    @app.post("/api/ui/config/connection")
    def ui_save_connection(body: dict | None = None) -> dict:
        """保存全局连接配置到页面覆盖层，返回保存后的生效值。

        参数:
            body: `{comfyui: {...}, llm: {...}}`；只接受白名单字段。
                `llm.api_key` 留空表示沿用已保存密钥，不会被清空。

        异常:
            400：字段类型不合法或值超出范围（中文说明）。
        """
        body = body or {}
        updates: dict = {}
        for section, payload in (("comfyui", body.get("comfyui") or {}), ("omlx", body.get("llm") or {})):
            for key, value in dict(payload).items():
                dotted = f"{section}.{key}"
                if dotted not in CONNECTION_FIELDS:
                    continue
                label, types = CONNECTION_FIELDS[dotted]
                if dotted == "omlx.api_key" and not str(value or "").strip():
                    continue
                if isinstance(value, str):
                    value = value.strip()
                # 数字项留空表示「沿用已保存值」，不因空串报错
                if types is not str and types is not bool and value in ("", None):
                    continue
                if types is bool:
                    # 开关允许布尔值或 "true"/"false" 字符串（表单序列化差异都能接受）
                    if isinstance(value, str):
                        value = value.lower() in ("1", "true", "on", "yes")
                    else:
                        value = bool(value)
                elif types is int:
                    try:
                        value = int(float(value))
                    except (TypeError, ValueError) as exc:
                        raise HTTPException(
                            status_code=400,
                            detail={"code": 2, "message": f"{label}必须是整数"},
                        ) from exc
                    if value <= 0:
                        raise HTTPException(
                            status_code=400,
                            detail={"code": 2, "message": f"{label}必须大于 0"},
                        )
                elif types is not str:
                    try:
                        value = float(value)
                    except (TypeError, ValueError) as exc:
                        raise HTTPException(
                            status_code=400,
                            detail={"code": 2, "message": f"{label}必须是数字"},
                        ) from exc
                    if value <= 0:
                        raise HTTPException(
                            status_code=400,
                            detail={"code": 2, "message": f"{label}必须大于 0"},
                        )
                    if isinstance(types, tuple) and int in types and key == "max_concurrency":
                        value = int(value)
                if dotted == "omlx.reasoning_effort" and value not in THINKING_EFFORTS:
                    raise HTTPException(
                        status_code=400,
                        detail={"code": 2, "message": f"{label}只能是 low / medium / high"},
                    )
                updates.setdefault(section, {})[key] = value
        # 覆盖层固定写 config/config.ui.yaml：所有 load_config 调用都会合并它，保存即生效
        try:
            target = save_ui_config(updates)
        except OSError as exc:
            raise HTTPException(
                status_code=500,
                detail={"code": 3, "message": f"配置写入失败：{exc.strerror or exc}"},
            ) from exc
        state = _connection_state(load_config(config_path))
        state["saved_to"] = str(target)
        return state

    @app.post("/api/ui/comfy/health")
    def ui_comfy_health(body: dict | None = None) -> dict:
        """检查 ComfyUI 连接并校验模板依赖是否就位。

        参数:
            body: 可选 `{base_url}`；传了就先按这个地址探测，
                这样页面里还没保存的新地址也能直接做联通检测。

        返回:
            `{ok, version, queue, templates, models_dir, base_url, latency_ms, message}`；
            连不上时 `ok=false`，HTTP 仍为 200，由页面显示红色状态。
        """
        config = load_config(config_path)
        comfy = dict(config.get("comfyui", {}) or {})
        override = str((body or {}).get("base_url") or "").strip()
        if override:
            comfy["base_url"] = override
        manifest = {}
        manifest_error = None
        try:
            manifest = comfy_media.load_manifest(comfy_media.templates_dir(config))
        except Exception as exc:
            manifest_error = _err_message(exc)

        models_dir = None
        try:
            models_dir = str(comfy_models.resolve_models_dir(config))
        except Exception:
            models_dir = None

        gateway = LocalComfyGateway(comfy, comfy_media.templates_dir(config))
        status = gateway.health()
        extra = status.extra or {}

        # 连接成功时再拉一次节点定义，校验模板里的节点类型与模型下拉项
        node_types: set[str] = set()
        model_options: set[str] = set()
        info_error = None
        if status.ok:
            try:
                node_types, model_options = _object_info_index(gateway.object_info())
            except Exception as exc:
                info_error = _err_message(exc)

        directory = comfy_media.templates_dir(config)
        templates: list[dict] = []
        for entry in comfy_media.manifest_entries(manifest):
            template_id = str(entry.get("id") or entry.get("template_id") or "")
            if not template_id:
                continue
            missing: list[str] = []
            missing_files: list[dict] = []
            error = None
            try:
                missing = comfy_models.missing_for_template(config, [str(item) for item in entry.get("models") or []])
                for key in missing:
                    model_entry = _find_comfy_entry(key) or {}
                    missing_files.append(
                        {
                            "key": key,
                            "path": str(Path(models_dir) / key) if models_dir else None,
                            "downloadable": bool(model_entry),
                            "license": model_entry.get("license"),
                            "license_gate": model_entry.get("license_gate") or "none",
                            "expected_bytes": int(model_entry.get("bytes") or 0),
                        }
                    )
            except Exception as exc:
                error = _err_message(exc)
            missing_nodes: list[str] = []
            missing_options: list[str] = []
            if status.ok and not info_error:
                class_types = _template_class_types(directory, entry)
                missing_nodes = sorted(node for node in class_types if node not in node_types)
                for key in entry.get("models") or []:
                    name = str(key).rsplit("/", 1)[-1]
                    if name and name not in model_options:
                        missing_options.append(name)
            templates.append(
                {
                    "id": template_id,
                    "kind": entry.get("kind"),
                    "display_name": entry.get("display_name") or template_id,
                    "ok": not missing and not error and not missing_nodes and not missing_options,
                    "missing": missing,
                    "missing_files": missing_files,
                    "missing_nodes": missing_nodes,
                    "missing_model_options": missing_options,
                    "error": error,
                }
            )

        message = status.detail or ""
        if not status.ok and not message:
            message = "ComfyUI 未连接：检查是否启动、地址是否正确"
        if info_error:
            message = f"{message}；节点定义读取失败：{info_error}" if message else f"节点定义读取失败：{info_error}"
        bad = [item["id"] for item in templates if not item["ok"]]
        if status.ok and bad:
            details: list[str] = []
            for item in templates:
                if item["ok"]:
                    continue
                if item["missing"]:
                    files = "、".join(str(name) for name in item["missing"])
                    where = f"（应放到 {models_dir}）" if models_dir else ""
                    details.append(f"{item['id']} 缺少模型 {files}{where}")
                elif item["missing_nodes"]:
                    details.append(f"{item['id']} 缺少节点 {'、'.join(item['missing_nodes'])}")
                elif item["missing_model_options"]:
                    details.append(f"{item['id']} 模型未在 ComfyUI 下拉项中 {'、'.join(item['missing_model_options'])}")
                else:
                    details.append(f"{item['id']} 校验失败")
            message = f"{message}；模板校验未通过：{'；'.join(details)}" if message else f"模板校验未通过：{'；'.join(details)}"
        if manifest_error:
            message = f"{message}；模板清单读取失败：{manifest_error}" if message else f"模板清单读取失败：{manifest_error}"
        return {
            "ok": bool(status.ok),
            "version": extra.get("version"),
            "queue": extra.get("queue"),
            "templates": templates,
            "models_dir": models_dir,
            "base_url": gateway.base_url,
            "latency_ms": status.latency_ms,
            "message": message,
        }

    def _business_budget(config) -> int:
        """返回当前业务侧最常用的工具调用预算（用于提示「上限小于估算会截断」）。

        参数:
            config: 本机配置。

        返回:
            人物 8 人与整本大纲两种估算中的较大值。
        """
        try:
            return max(_tool_max_tokens(config, "characters", 8), _tool_max_tokens(config, "outline", 27))
        except Exception:
            return 16384

    @app.post("/api/ui/llm/status")
    def ui_llm_status(body: dict | None = None) -> dict:
        """检测写作 LLM（OpenAI 兼容服务）并返回可用模型与上下限。

        参数:
            body: 可选 `{endpoint, api_key, model}`；传了就先按表单值探测，
                未保存的新地址 / 新模型也能直接检测，缺省回落到已保存配置。

        返回:
            `{ok, connected, endpoint, model, model_found, models, context_window,
            context_source, max_output_tokens, max_output_source, business_budget,
            probes, latency_ms, message}`；上限只来自服务端接口字段（server）或
            未取到（unknown），不读本机文件、不伪造默认值。
        """
        config = load_config(config_path)
        settings, model = resolve_llm_settings(config, body, allow_empty_model=True)
        adapter = OmlxWritingAdapter(settings)
        started = time.monotonic()
        limits = adapter.fetch_model_limits(model or None, timeout_s=2.0)
        latency = int((time.monotonic() - started) * 1000)
        models = limits.get("models") or []
        ids = [item["id"] for item in models]
        probes = limits.get("probes") or []
        list_probe = next((item for item in probes if item.get("path") == "/v1/models"), {})
        list_status = list_probe.get("status")
        # 只要能收到任何 HTTP 响应就算「服务可达」；401/403 仍算可达，只是鉴权失败
        connected = list_status is not None
        list_ok = list_status == 200 and bool(ids)
        selected = next((item for item in models if item["id"] == model), None)
        if not connected:
            stage = "connect"
            message = f"无法连接服务（{adapter.endpoint}）：{list_probe.get('error') or '无响应'}"
        elif list_status in (401, 403):
            stage = "auth"
            message = f"连接成功，鉴权失败：密钥无效或无权限（HTTP {list_status}）"
        elif not models:
            stage = "models"
            message = "连接成功，但服务端未返回模型列表"
        elif not model:
            stage = "model"
            message = f"未选择模型，请在设置页检测连接后选择（可用：{'、'.join(ids)}）"
        elif selected is None:
            stage = "model"
            message = f"已保存的模型 {model} 不在当前服务列表中，请重新选择（可用：{'、'.join(ids)}）"
        elif not (selected.get("context_length") and selected.get("max_output_tokens")):
            stage = "limits"
            message = "连接成功、已取列表，但未返回上下限字段"
        else:
            stage = "ok"
            message = (
                f"已按 {model} 检测：上下文 {selected.get('context_length')} tokens / "
                f"最大输出 {selected.get('max_output_tokens')} tokens（来源：服务端字段）"
            )
        return {
            "ok": stage == "ok",
            "connected": connected,
            "stage": stage,
            "list_status": list_status,
            "list_ok": list_ok,
            "endpoint": adapter.endpoint,
            "model": model,
            "model_found": selected is not None,
            "models": models,
            "context_window": (selected or {}).get("context_length"),
            "context_source": (selected or {}).get("context_source") or "unknown",
            "max_output_tokens": (selected or {}).get("max_output_tokens"),
            "max_output_source": (selected or {}).get("max_output_source") or "unknown",
            "business_budget": _business_budget(config),
            "probes": probes,
            "latency_ms": latency,
            "message": message,
            "provider": str(config.get("adapters.writing") or ""),
            "last_call": last_llm_call(),
        }

    @app.post("/api/ui/settings/models")
    def ui_model_catalog(body: dict | None = None) -> dict:
        """实时读取上游模型列表（设置页「测试连接」与模型下拉的唯一来源）。

        参数:
            body: 可选 `{endpoint, api_key}`；用于测试尚未保存的地址与密钥，
                不传则使用已保存配置。不会回显 api_key。

        返回:
            `{ok, connected, status, endpoint, models, count, latency_ms, message}`；
            `models` 条目含 `id/owned_by/created/max_model_len`。服务不可达或
            鉴权失败时 HTTP 仍为 200，由页面展示中文原因。
        """
        config = load_config(config_path)
        settings, _model = resolve_llm_settings(config, body)
        adapter = OmlxWritingAdapter(settings)
        started = time.monotonic()
        try:
            catalog = adapter.fetch_model_catalog(timeout_s=2.0)
            error = None
        except Exception as exc:  # 探测内部已兜底，这里再保一层，保证接口不 500
            catalog = {
                "ok": False,
                "connected": False,
                "status": None,
                "models": [],
                "count": 0,
                "message": f"读取模型列表失败：{_err_message(exc)}",
            }
            error = _err_message(exc)
        latency = int((time.monotonic() - started) * 1000)
        message = catalog.get("message") or (f"读取模型列表失败：{error}" if error else "未获取到模型列表")
        return {
            "ok": bool(catalog.get("ok")),
            "connected": bool(catalog.get("connected")),
            "status": catalog.get("status"),
            "endpoint": adapter.endpoint,
            "models": catalog.get("models") or [],
            "count": int(catalog.get("count") or 0),
            "latency_ms": latency,
            "message": message,
        }

    @app.post("/api/ui/settings/model-limits")
    def ui_model_limits(body: dict | None = None) -> dict:
        """探测当前模型的上下文与最大输出上限（只读 OpenAI 兼容接口）。

        参数:
            body: 可选 `{endpoint, api_key, model}` 覆盖值；不传则用已保存配置。

        返回:
            `{ok, connected, model, context_length, context_source,
            max_output_tokens, max_output_source, models, probes, latency_ms, message}`；
            单个端点超时 2 秒，失败快速返回，不影响页面其他操作。
        """
        config = load_config(config_path)
        settings, model = resolve_llm_settings(config, body)
        adapter = OmlxWritingAdapter(settings)
        started = time.monotonic()
        try:
            limits = adapter.fetch_model_limits(model or None, timeout_s=2.0)
            error = None
        except Exception as exc:  # 探测内部已兜底，这里再保一层，保证接口不 500
            limits = {
                "model": model,
                "context_length": None,
                "context_source": "unknown",
                "max_output_tokens": None,
                "max_output_source": "unknown",
                "models": [],
                "probes": [],
            }
            error = _err_message(exc)
        latency = int((time.monotonic() - started) * 1000)
        probes = limits.get("probes") or []
        return {
            "ok": bool(limits.get("context_length") or limits.get("max_output_tokens")),
            "connected": any(item.get("status") == 200 for item in probes),
            "endpoint": adapter.endpoint,
            "model": limits.get("model") or model,
            "context_length": limits.get("context_length"),
            "context_source": limits.get("context_source") or "unknown",
            "max_output_tokens": limits.get("max_output_tokens"),
            "max_output_source": limits.get("max_output_source") or "unknown",
            "business_budget": _business_budget(config),
            "models": limits.get("models") or [],
            "probes": probes,
            "latency_ms": latency,
            "message": limits.get("message") or (f"检测失败：{error}" if error else "未获取到上下限"),
        }
