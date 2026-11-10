"""ComfyUI 真实网关：健康检查、提交、轮询、产物下载与队列管理。

对接约定（详见 docs/architecture.md §3.5）：
- 模板为 ComfyUI API 格式 JSON，登记在 `config/comfy/manifest.json`，文件放在 `config/comfy/`；
- 模板中用 `{{param}}` 占位，本网关只做类型安全替换，不生成/修改节点；
- 所有 HTTP 使用标准库 `urllib.request`，不引入第三方依赖；
- 错误统一映射为 AdapterError：连不上 UNAVAILABLE、超时 TIMEOUT、节点校验失败 UPSTREAM_ERROR、参数问题 INVALID_INPUT；
- 产物下载到调用方目录，不读取 ComfyUI 的 `output/` 目录。
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import mimetypes
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from .base import (
    AdapterError,
    ComfyArtifact,
    ComfyJobStatus,
    ComfySubmitResult,
    ErrorCode,
    HealthStatus,
    WorkflowTemplate,
)

logger = logging.getLogger(__name__)

_PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")
# /history 的 outputs 里可能承载产物的键
_OUTPUT_KEYS = ("images", "gifs", "videos", "audio")
# 扩展名 → 产物类型
_KIND_BY_SUFFIX = {
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".webp": "image",
    ".mp4": "video", ".webm": "video",
    ".wav": "audio", ".mp3": "audio", ".flac": "audio",
}
_UNREACHABLE = "无法连接 ComfyUI（{url}）：检查是否启动、地址是否 http://127.0.0.1:8188"


def _kind_of(filename: str) -> str:
    """按扩展名判断产物类型：参数 filename 为服务端文件名，返回 image|video|audio|other。"""
    return _KIND_BY_SUFFIX.get(Path(filename).suffix.lower(), "other")


class LocalComfyGateway:
    """本机 ComfyUI HTTP 网关。"""

    def __init__(self, settings: dict, templates_dir: str | Path):
        """绑定配置与模板目录：settings 为 comfyui.* 配置，templates_dir 为含 manifest.json 的模板目录。"""
        self.settings = settings or {}
        self.templates_dir = Path(templates_dir).expanduser()
        self.base_url = str(self.settings.get("base_url") or "http://127.0.0.1:8188").rstrip("/")
        # 最近一次 list_templates 的整体性错误（如清单缺失），供 CLI/页面提示
        self.last_error: str | None = None

    # ---------------- 内部工具 ----------------

    def _timeout_ms(self, key: str, default: int) -> int:
        """读取超时配置：参数 key 为配置键、default 为缺省毫秒，返回毫秒整数（最小 500）。"""
        try:
            return max(500, int(self.settings.get(key, default)))
        except (TypeError, ValueError):
            return default

    def _net_error(self, url: str, timeout_s: float, exc: BaseException) -> AdapterError:
        """把网络异常映射为 AdapterError：参数 url/timeout_s/exc，返回超时或不可达错误。"""
        reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
        if isinstance(reason, (socket.timeout, TimeoutError)):
            return AdapterError(ErrorCode.TIMEOUT, f"ComfyUI 请求超时（{timeout_s:.0f}s）：{url}，可调大 comfyui.request_timeout_ms",
                                retryable=True, detail={"url": url})
        return AdapterError(ErrorCode.UNAVAILABLE, _UNREACHABLE.format(url=self.base_url), retryable=True,
                            detail={"url": url, "reason": str(reason)})

    def _http(self, method: str, path: str, *, payload: dict | None = None, raw_body: bytes | None = None,
              content_type: str | None = None, timeout_ms: int | None = None) -> tuple[int, bytes]:
        """发送 HTTP 请求：参数 method/path/payload(JSON 体)/raw_body(原始体)/content_type/timeout_ms，返回 (状态码, 响应体)；连接失败抛 UNAVAILABLE、超时抛 TIMEOUT。"""
        url = f"{self.base_url}{path}"
        timeout_s = self._timeout_ms("request_timeout_ms", 30000) / 1000 if timeout_ms is None else max(0.5, timeout_ms / 1000)
        headers = {"Accept": "application/json"}
        data = raw_body
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if content_type:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()
        except (socket.timeout, TimeoutError, urllib.error.URLError, OSError) as exc:
            raise self._net_error(url, timeout_s, exc) from exc

    def _parse_json(self, body: bytes, what: str) -> dict:
        """解析响应体为 JSON 对象：参数 body/what=接口名，返回 dict；非法结构抛 UPSTREAM_ERROR。"""
        text = body.decode("utf-8", "replace")
        try:
            data = json.loads(text) if text.strip() else {}
        except json.JSONDecodeError as exc:
            raise AdapterError(ErrorCode.UPSTREAM_ERROR, f"ComfyUI {what}返回的内容不是合法 JSON：{text[:200]}", detail={"response": text[:2000]}) from exc
        if not isinstance(data, dict):
            raise AdapterError(ErrorCode.UPSTREAM_ERROR, f"ComfyUI {what}返回结构异常（期望 JSON 对象）", detail={"response": text[:2000]})
        return data

    def _check_http(self, code: int, body: bytes, what: str) -> None:
        """非 2xx 统一抛错：参数 code/body/what（接口名），2xx 直接返回，其它抛 UPSTREAM_ERROR（5xx 可重试）。"""
        if 200 <= code < 300:
            return
        text = body.decode("utf-8", "replace")
        raise AdapterError(ErrorCode.UPSTREAM_ERROR, f"ComfyUI {what}失败（HTTP {code}）：{text[:200] or '无响应内容'}",
                           retryable=code >= 500, detail={"status": code, "response": text[:2000]})

    def _manifest(self) -> dict:
        """读取 manifest.json：返回清单 dict；不存在/非法时返回空 dict 并写入 last_error。"""
        path = self.templates_dir / "manifest.json"
        if not path.exists():
            self.last_error = f"未找到模板清单：{path}"
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            self.last_error = f"模板清单解析失败：{path}（{exc}）"
            return {}
        if not isinstance(data, dict):
            self.last_error = f"模板清单格式错误：{path}（必须是 JSON 对象）"
            return {}
        return data

    @staticmethod
    def _manifest_entries(manifest: dict) -> list[dict]:
        """把清单登记统一成 dict 列表：参数 manifest 为清单内容，兼容 templates 为对象或数组。"""
        raw = manifest.get("templates") if manifest.get("templates") is not None else manifest.get("workflows")
        if isinstance(raw, dict):
            return [{**dict(value), "template_id": str(key)} if isinstance(value, dict) else {"template_id": str(key)} for key, value in raw.items()]
        if isinstance(raw, list):
            return [dict(value) for value in raw if isinstance(value, dict)]
        return []

    def _template_path(self, template_id: str, manifest: dict | None = None) -> Path:
        """解析模板文件路径：优先清单的 file/path，缺省 templates_dir/<id>.json；返回 Path。"""
        manifest = self._manifest() if manifest is None else manifest
        for entry in self._manifest_entries(manifest):
            entry_id = entry.get("template_id") or entry.get("id") or entry.get("template")
            if str(entry_id) != template_id:
                continue
            candidate = Path(str(entry.get("file") or entry.get("path") or f"{template_id}.json")).expanduser()
            if candidate.is_absolute():
                return candidate
            local = self.templates_dir / candidate
            return local if local.exists() or not candidate.exists() else candidate
        return self.templates_dir / f"{template_id}.json"

    # ---------------- 健康检查与模板 ----------------

    def health(self) -> HealthStatus:
        """检查 ComfyUI 可用性：GET /system_stats + /queue，返回 HealthStatus（成功 detail 含版本/设备，extra={version, queue}；失败不抛异常）。"""
        started = time.perf_counter()
        try:
            code, body = self._http("GET", "/system_stats", timeout_ms=self._timeout_ms("request_timeout_ms", 30000))
        except AdapterError as exc:
            return HealthStatus(ok=False, detail=f"{exc.message}（{exc.code.value}）",
                                latency_ms=int((time.perf_counter() - started) * 1000), extra={"error": exc.code.value})
        latency_ms = int((time.perf_counter() - started) * 1000)
        if not 200 <= code < 300:
            return HealthStatus(ok=False, detail=f"ComfyUI /system_stats 返回 HTTP {code}：检查服务日志", latency_ms=latency_ms, extra={"status": code})
        try:
            data = self._parse_json(body, "/system_stats ")
        except AdapterError as exc:
            return HealthStatus(ok=False, detail=exc.message, latency_ms=latency_ms, extra={"error": exc.code.value})

        system = data.get("system") if isinstance(data.get("system"), dict) else {}
        version = str(system.get("comfyui_version") or "未知")
        device_parts: list[str] = []
        for device in (data.get("devices") if isinstance(data.get("devices"), list) else [])[:2]:
            if not isinstance(device, dict):
                continue
            name = str(device.get("name") or device.get("type") or "device")
            total = device.get("vram_total") or 0
            free = device.get("vram_free") or 0
            device_parts.append(f"{name} 显存 {free / (1024 ** 3):.1f}/{total / (1024 ** 3):.1f}GB" if total else name)
        device_text = "、".join(device_parts) or "未知设备"

        queue_extra: dict = {"running": None, "pending": None}
        queue_note = ""
        try:
            snapshot = self.queue_snapshot()
            running = len(snapshot.get("queue_running") or [])
            pending = len(snapshot.get("queue_pending") or [])
            queue_extra = {"running": running, "pending": pending}
            queue_note = f"；队列 运行 {running} / 等待 {pending}"
        except AdapterError as exc:
            queue_note = f"；队列读取失败（{exc.code.value}）"
        return HealthStatus(
            ok=True,
            detail=f"已连接 ComfyUI {version}（{device_text}）{queue_note}",
            latency_ms=latency_ms,
            extra={"version": version, "queue": queue_extra},
        )

    def object_info(self) -> dict:
        """拉取 ComfyUI 节点定义（GET /object_info）。

        供健康检查校验模板里的节点类型与模型下拉项是否存在于当前实例。

        返回:
            节点定义字典（class_type → 输入定义）。

        异常:
            AdapterError: 服务不可达，或返回内容不是 JSON。
        """
        code, body = self._http("GET", "/object_info", timeout_ms=self._timeout_ms("request_timeout_ms", 30000))
        if not 200 <= code < 300:
            raise AdapterError(ErrorCode.UPSTREAM_ERROR, f"ComfyUI /object_info 返回 HTTP {code}")
        return self._parse_json(body, "/object_info ")

    def list_templates(self) -> list[WorkflowTemplate]:
        """按清单校验本地模板：只含清单登记且文件存在的模板，校验 JSON 可解析与必需占位符，返回 WorkflowTemplate 列表。"""
        self.last_error = None
        manifest = self._manifest()
        if not manifest:
            logger.warning(self.last_error or "模板清单缺失")
            return []
        templates: list[WorkflowTemplate] = []
        for entry in self._manifest_entries(manifest):
            template_id = entry.get("template_id") or entry.get("id") or entry.get("template")
            if not template_id:
                continue
            template_id = str(template_id)
            path = self._template_path(template_id, manifest)
            if not path.exists():
                continue
            try:
                text = path.read_text(encoding="utf-8")
                data = json.loads(text)
            except (json.JSONDecodeError, OSError) as exc:
                templates.append(WorkflowTemplate(template_id=template_id, path=str(path), valid=False, error=f"模板读取/解析失败：{exc}"))
                continue
            placeholders = sorted(set(_PLACEHOLDER.findall(text)))
            required_raw = entry.get("required_placeholders") or entry.get("placeholders") or []
            required = sorted(required_raw.keys()) if isinstance(required_raw, dict) else [str(item) for item in required_raw]
            errors: list[str] = []
            if not isinstance(data, dict) or not data:
                errors.append("模板必须是 API 格式的非空 JSON 对象")
            missing = [name for name in required if name not in placeholders]
            if missing:
                errors.append(f"缺少必需占位符：{', '.join('{{' + name + '}}' for name in missing)}")
            templates.append(
                WorkflowTemplate(
                    template_id=template_id,
                    path=str(path),
                    placeholders=placeholders,
                    valid=not errors,
                    error="；".join(errors) or None,
                )
            )
        return templates

    # ---------------- 提交 ----------------

    @staticmethod
    def _render_value(value: object, params: dict) -> object:
        """递归替换占位符：参数 value 为模板节点值、params 为参数；整值占位符保留原类型，内嵌占位符按 JSON 字符串转义。"""
        if isinstance(value, dict):
            return {key: LocalComfyGateway._render_value(item, params) for key, item in value.items()}
        if isinstance(value, list):
            return [LocalComfyGateway._render_value(item, params) for item in value]
        if not isinstance(value, str):
            return value
        match = _PLACEHOLDER.fullmatch(value)
        if match:
            name = match.group(1)
            return copy.deepcopy(params[name]) if params.get(name) is not None else value

        def replace(found: re.Match[str]) -> str:
            """替换单个内嵌占位符，按 JSON 字符串内容转义。"""
            name = found.group(1)
            return found.group(0) if params.get(name) is None else json.dumps(str(params[name]), ensure_ascii=False)[1:-1]

        return _PLACEHOLDER.sub(replace, value)

    def submit_prompt(self, prompt: dict, client_id: str) -> ComfySubmitResult:
        """提交 API 格式工作流：参数 prompt 为节点字典、client_id 为客户端标识，返回提交结果；空工作流抛 INVALID_INPUT，节点校验失败（400）抛 UPSTREAM_ERROR。"""
        if not isinstance(prompt, dict) or not prompt:
            raise AdapterError(ErrorCode.INVALID_INPUT, "提示词工作流为空：需为 ComfyUI API 格式的非空 JSON 对象")
        code, body = self._http(
            "POST",
            "/prompt",
            payload={"prompt": prompt, "client_id": client_id},
            timeout_ms=self._timeout_ms("request_timeout_ms", 30000),
        )
        text = body.decode("utf-8", "replace")
        if code == 400:
            raise AdapterError(
                ErrorCode.UPSTREAM_ERROR,
                f"ComfyUI 节点校验失败：{self._summarize_node_errors(text)}",
                detail={"status": code, "response": text[:4000]},
            )
        self._check_http(code, body, "提交任务")
        data = self._parse_json(body, "提交任务")
        prompt_id = data.get("prompt_id")
        if not prompt_id:
            raise AdapterError(ErrorCode.UPSTREAM_ERROR, f"ComfyUI 提交成功但未返回 prompt_id：{text[:200]}", detail={"response": text[:2000]})
        number = data.get("number")
        return ComfySubmitResult(prompt_id=str(prompt_id), client_id=client_id, number=int(number) if isinstance(number, (int, float)) else None)

    @staticmethod
    def _summarize_node_errors(text: str) -> str:
        """把 /prompt 的 node_errors 摘要为中文：参数 text 为 400 响应体，返回最多 5 个节点的错误描述。"""
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return text[:300] or "节点校验失败（无详细信息）"
        error = data.get("error") if isinstance(data, dict) else None
        node_errors = data.get("node_errors") if isinstance(data, dict) else None
        parts: list[str] = []
        if isinstance(error, dict) and error.get("type") == "prompt_outputs_failed_validation":
            parts.append("工作流校验未通过")
        elif isinstance(error, dict) and (error.get("message") or error.get("type")):
            parts.append(str(error.get("message") or error.get("type")))
        elif isinstance(error, str) and error:
            parts.append(error)
        if isinstance(node_errors, dict):
            for node_id, info in list(node_errors.items())[:5]:
                if not isinstance(info, dict):
                    continue
                details = info.get("errors") if isinstance(info.get("errors"), list) else []
                reasons = [str(item.get("message") or item.get("type")) for item in details
                           if isinstance(item, dict) and (item.get("message") or item.get("type"))]
                parts.append(f"节点 {node_id}（{info.get('class_type') or '未知节点'}）：{'；'.join(reasons) or '校验失败'}")
            if len(node_errors) > 5:
                parts.append(f"等共 {len(node_errors)} 个节点出错")
        return "；".join(parts) or "节点校验失败（无详细信息）"

    def submit(self, template_id: str, params: dict, client_id: str) -> ComfySubmitResult:
        """读取模板并填参后提交：参数 template_id/params/client_id，返回提交结果；模板缺失或占位符未解析抛 INVALID_INPUT（不发请求）。"""
        path = self._template_path(template_id)
        if not path.exists():
            raise AdapterError(ErrorCode.INVALID_INPUT, f"未找到模板 {template_id}：{path}（执行 ndw comfy import 生成模板）")
        try:
            text = path.read_text(encoding="utf-8")
            template = json.loads(text)
        except (json.JSONDecodeError, OSError) as exc:
            raise AdapterError(ErrorCode.INVALID_INPUT, f"模板 {template_id} 读取/解析失败：{exc}") from exc
        if not isinstance(template, dict) or not template:
            raise AdapterError(ErrorCode.INVALID_INPUT, f"模板 {template_id} 必须是 API 格式的非空 JSON 对象")
        params = params or {}
        unresolved = sorted(name for name in set(_PLACEHOLDER.findall(text)) if params.get(name) is None)
        if unresolved:
            raise AdapterError(ErrorCode.INVALID_INPUT,
                               f"模板 {template_id} 存在未解析占位符：{', '.join('{{' + name + '}}' for name in unresolved)}")
        return self.submit_prompt(self._render_value(template, params), client_id)

    # ---------------- 状态、产物与队列 ----------------

    def _history(self, prompt_id: str) -> dict:
        """查询任务历史：参数 prompt_id，返回该任务记录（不存在时为空 dict）；超时/服务异常抛 AdapterError。"""
        code, body = self._http("GET", f"/history/{urllib.parse.quote(prompt_id)}", timeout_ms=self._timeout_ms("status_timeout_ms", 10000))
        if code == 404:
            return {}
        self._check_http(code, body, "查询历史")
        entry = self._parse_json(body, "查询历史").get(prompt_id)
        return entry if isinstance(entry, dict) else {}

    def status(self, prompt_id: str) -> ComfyJobStatus:
        """查询任务状态：参数 prompt_id，返回 pending/running/success/error（error 带节点与原因摘要）。"""
        entry = self._history(prompt_id)
        if entry:
            status_obj = entry.get("status") if isinstance(entry.get("status"), dict) else {}
            if status_obj.get("status_str") == "error":
                message, error = self._history_error(status_obj)
                return ComfyJobStatus(state="error", message=message, error=error)
            if status_obj.get("completed") is True or status_obj.get("status_str") == "success":
                return ComfyJobStatus(state="success", progress=1.0)
            return ComfyJobStatus(state="running")
        snapshot = self.queue_snapshot()
        if prompt_id in self._queue_ids(snapshot.get("queue_running")):
            return ComfyJobStatus(state="running")
        if prompt_id in self._queue_ids(snapshot.get("queue_pending")):
            return ComfyJobStatus(state="pending")
        # 未入队也未完成：可能尚未被工作流消费，按 pending 交回轮询方继续等待
        return ComfyJobStatus(state="pending")

    @staticmethod
    def _queue_ids(items: object) -> set[str]:
        """提取队列项里的 prompt_id：参数 items 为 /queue 列表，返回字符串集合。"""
        ids: set[str] = set()
        if not isinstance(items, list):
            return ids
        for item in items:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                ids.add(str(item[1]))
            elif isinstance(item, dict) and item.get("prompt_id"):
                ids.add(str(item["prompt_id"]))
        return ids

    @staticmethod
    def _history_error(status_obj: dict) -> tuple[str, dict]:
        """解析 history.status.messages：参数 status_obj，返回 (中文摘要, 结构化错误 dict)。"""
        messages = status_obj.get("messages") if isinstance(status_obj.get("messages"), list) else []
        for message in messages:
            if not isinstance(message, (list, tuple)) or len(message) < 2:
                continue
            kind, payload = message[0], message[1]
            if kind not in ("execution_error", "execution_interrupted"):
                continue
            payload = payload if isinstance(payload, dict) else {}
            node = payload.get("node_type") or payload.get("node_id") or "未知节点"
            reason = payload.get("exception_message") or payload.get("exception_type") or "执行中断"
            return f"ComfyUI 执行失败：{node} → {reason}", {"node": node, "message": str(reason), "type": str(kind)}
        return "ComfyUI 执行失败（无详细节点信息）", {}

    def fetch(self, prompt_id: str, dest_dir: str) -> list[ComfyArtifact]:
        """把任务产物下载到 dest_dir：参数 prompt_id/dest_dir，返回 ComfyArtifact 列表（含本地绝对路径）；历史无记录抛 INVALID_INPUT。"""
        entry = self._history(prompt_id)
        if not entry:
            raise AdapterError(ErrorCode.INVALID_INPUT, f"ComfyUI 历史中没有任务 {prompt_id}：任务可能未开始、已完成被清理或尚未完成")
        outputs = entry.get("outputs") if isinstance(entry.get("outputs"), dict) else {}
        destination = Path(dest_dir).expanduser()
        destination.mkdir(parents=True, exist_ok=True)
        artifacts: list[ComfyArtifact] = []
        seen: set[tuple[str, str, str]] = set()
        for node_output in outputs.values():
            if not isinstance(node_output, dict):
                continue
            for key in _OUTPUT_KEYS:
                for item in node_output.get(key) if isinstance(node_output.get(key), list) else []:
                    if not isinstance(item, dict) or not item.get("filename"):
                        continue
                    filename = str(item["filename"])
                    subfolder = str(item.get("subfolder") or "")
                    file_type = str(item.get("type") or "output")
                    marker = (filename, subfolder, file_type)
                    if marker in seen:
                        continue
                    seen.add(marker)
                    query = urllib.parse.urlencode({"filename": filename, "subfolder": subfolder, "type": file_type})
                    code, body = self._http("GET", f"/view?{query}", timeout_ms=self._timeout_ms("request_timeout_ms", 30000))
                    self._check_http(code, body, "下载产物")
                    local_path = destination / Path(filename).name
                    local_path.write_bytes(body)
                    logger.debug("ComfyUI 产物 %s 已下载到 %s（sha256=%s）", filename, local_path, hashlib.sha256(body).hexdigest())
                    artifacts.append(
                        ComfyArtifact(
                            kind=_kind_of(filename),
                            filename=filename,
                            subfolder=subfolder,
                            server_path=f"{subfolder}/{filename}" if subfolder else filename,
                            local_path=str(local_path),
                        )
                    )
        return artifacts

    def upload_image(self, path: str | Path, subfolder: str = "ndw") -> dict:
        """上传图片到 ComfyUI input（i2v 首帧）：参数 path 本地路径、subfolder 子目录，返回 {"name","subfolder","type"}；文件不存在抛 INVALID_INPUT。"""
        source = Path(path).expanduser()
        if not source.exists() or not source.is_file():
            raise AdapterError(ErrorCode.INVALID_INPUT, f"首帧图片不存在：{source}")
        try:
            content = source.read_bytes()
        except OSError as exc:
            raise AdapterError(ErrorCode.INVALID_INPUT, f"首帧图片读取失败：{source}（{exc}）") from exc
        boundary = f"----ndw{uuid.uuid4().hex}"
        content_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
        parts: list[bytes] = []
        for name, value in (("overwrite", "true"), ("subfolder", subfolder), ("type", "input")):
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode("utf-8"))
        parts.append(
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="{source.name}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n"
            ).encode("utf-8")
            + content
            + b"\r\n"
        )
        parts.append(f"--{boundary}--\r\n".encode("utf-8"))
        code, body = self._http(
            "POST",
            "/upload/image",
            raw_body=b"".join(parts),
            content_type=f"multipart/form-data; boundary={boundary}",
            timeout_ms=self._timeout_ms("request_timeout_ms", 30000),
        )
        self._check_http(code, body, "上传首帧图片")
        data = self._parse_json(body, "上传首帧图片")
        return {"name": data.get("name") or source.name, "subfolder": data.get("subfolder", subfolder), "type": data.get("type", "input")}

    def cancel(self, prompt_id: str) -> None:
        """从队列删除任务：参数 prompt_id；失败抛 AdapterError(UPSTREAM_ERROR)。"""
        code, body = self._http("POST", "/queue", payload={"delete": [prompt_id]}, timeout_ms=self._timeout_ms("status_timeout_ms", 10000))
        self._check_http(code, body, "取消任务")

    def interrupt(self) -> None:
        """中断当前执行（POST /interrupt）；失败抛 AdapterError(UPSTREAM_ERROR)。"""
        code, body = self._http("POST", "/interrupt", payload={}, timeout_ms=self._timeout_ms("status_timeout_ms", 10000))
        self._check_http(code, body, "中断任务")

    def queue_snapshot(self) -> dict:
        """返回队列原始快照：GET /queue，成功返回 dict（queue_running/queue_pending），失败抛 AdapterError。"""
        code, body = self._http("GET", "/queue", timeout_ms=self._timeout_ms("status_timeout_ms", 10000))
        self._check_http(code, body, "查询队列")
        return self._parse_json(body, "查询队列")
