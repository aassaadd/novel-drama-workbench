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
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import HTTPException

from ..adapters.comfyui import LocalComfyGateway
from ..config import load_config
from ..media import comfy_media, comfy_models
from ..tts import model_store

#: 后台下载任务表：`job_id -> {status, group, key, started_at, finished_at, result, logs}`
_JOBS: dict[str, dict] = {}

#: 任务表并发写锁（FastAPI 同步路由跑在线程池里）
_JOBS_LOCK = threading.Lock()

#: 每个任务最多保留的日志行数
_MAX_LOG_LINES = 200

#: 视为成功的安装状态
_OK_STATUS = ("installed", "already")


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
        `{"image": {模板ID...}, "video": {模板ID...}}`；清单缺失时回落到内置模板 ID。
    """
    groups: dict[str, set[str]] = {"image": set(), "video": set()}
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
        rows.append(
            {
                "key": key,
                "kind": item.get("kind"),
                "target_path": str(root / key),
                "bytes": int(item.get("bytes") or 0),
                "present": bool(item.get("present")),
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
            }
        )
    return str(root), rows, None


def _split_groups(rows: list[dict], groups: dict[str, set[str]]) -> tuple[list[dict], list[dict]]:
    """把图像/视频模型行按模板归属拆成两组（同一行只进一组）。

    参数:
        rows: 扫描出的模型行。
        groups: `{"image": {模板ID}, "video": {模板ID}}`。

    返回:
        `(图像模型行, 视频模型行)`。
    """
    images: list[dict] = []
    videos: list[dict] = []
    for row in rows:
        templates = {str(item) for item in row.get("templates") or []}
        if templates & groups["image"]:
            images.append(row)
        elif templates & groups["video"]:
            videos.append(row)
    return images, videos


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
        """模型清单：音频 / 图像 / 视频三组 + 目录与镜像链（只读、不联网、不写盘）。"""
        config = load_config(config_path)
        groups = _template_kinds(config)
        comfy_dir, comfy_rows, comfy_error = _comfy_rows(config)
        images, videos = _split_groups(comfy_rows, groups)
        audio_root, audio_rows, audio_error = _audio_rows(config)
        return {
            "dirs": {"models_root": audio_root, "comfy_models_dir": comfy_dir},
            "mirrors": model_store.download_mirrors(config),
            "audio": {"root": audio_root, "models": audio_rows, "message": audio_error},
            "image": {"models": images, "message": comfy_error},
            "video": {"models": videos, "message": comfy_error},
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
        if group not in ("audio", "image", "video") or not key:
            raise HTTPException(status_code=400, detail={"code": 2, "message": "需要 group（audio/image/video）与 key"})
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
        if group not in ("audio", "image", "video") or not key:
            raise HTTPException(status_code=400, detail={"code": 2, "message": "需要 group（audio/image/video）与 key"})
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
        if group not in ("image", "video") or not key:
            raise HTTPException(status_code=400, detail={"code": 2, "message": "需要 group（image/video）与 key"})
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
            error = None
            try:
                missing = comfy_models.missing_for_template(config, [str(item) for item in entry.get("models") or []])
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
            message = f"{message}；模板校验未通过：{'、'.join(bad)}"
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
