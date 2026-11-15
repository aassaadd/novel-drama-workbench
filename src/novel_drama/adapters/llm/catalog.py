"""omlx 模型目录能力：模型列表、上下文上限探测、加载状态与健康检查。

以 mixin 形式与 `OmlxWritingAdapter` 组合，让传输细节与目录查询各自成文件。
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request

from ..errors import AdapterError, ErrorCode, HealthStatus
from .helpers import (
    _CONTEXT_FIELDS,
    _LIMIT_CACHE,
    _LIMIT_PROBE_PATHS,
    _MODEL_CACHE,
    _MODEL_CACHE_TTL_S,
    _OUTPUT_FIELDS,
)


class ModelCatalogMixin:
    def list_models(self, timeout_s: float | None = None) -> list[dict]:
        """获取服务端模型列表（GET /v1/models）。

        参数:
            timeout_s: 可选，覆盖默认超时（设置页自检时用较短超时）。

        返回:
            模型 dict 列表（含 id、max_model_len 等）。

        异常:
            AdapterError(INVALID_INPUT): 未配置地址。
        """
        # 列表接口不依赖 model，未选模型时也允许拉取（设置页「检测」要先看到可选项）
        if not self.endpoint:
            raise AdapterError(
                ErrorCode.INVALID_INPUT,
                "omlx 未配置地址：请在设置页填写服务地址（或在 config/config.yaml 的 omlx.endpoint 配置）",
            )
        data = self._request_json("GET", "/v1/models", timeout_s=timeout_s)
        models = data.get("data") if isinstance(data, dict) else None
        return models if isinstance(models, list) else []

    def model_ids(self) -> list[str]:
        """返回模型 ID 列表。"""
        return [str(item.get("id")) for item in self.list_models() if item.get("id")]

    def fetch_model_catalog(self, timeout_s: float = 2.0) -> dict:
        """实时读取服务端模型列表（设置页「测试连接」与模型下拉的唯一来源）。

        只访问 OpenAI 兼容的 `GET /v1/models`，不读本机配置文件；单次请求短超时，
        失败不抛异常，统一转成中文说明，便于页面直接展示。

        参数:
            timeout_s: 单次请求超时秒数（默认 2 秒）。

        返回:
            dict：`{ok, connected, status, models, count, message}`；
            `models` 条目含 `id/owned_by/created/max_model_len`，字段存在上限时
            额外带 `context_length/max_output_tokens`（来源固定为 server）。
        """
        if not self.endpoint:
            return {
                "ok": False,
                "connected": False,
                "status": None,
                "models": [],
                "count": 0,
                "message": "未配置接口地址：请先在设置页填写 omlx.endpoint",
            }
        status, data, error = self._probe("/v1/models", timeout_s)
        if status in (401, 403):
            return {
                "ok": False,
                "connected": True,
                "status": status,
                "models": [],
                "count": 0,
                "message": f"密钥无效或无权限（HTTP {status}）：请检查 omlx.api_key 是否与服务端一致",
            }
        if status is None:
            reason = str(error or "无响应")
            return {
                "ok": False,
                "connected": False,
                "status": None,
                "models": [],
                "count": 0,
                "message": f"无法连接服务（{self.endpoint}）：{reason}；请确认服务已启动、地址正确",
            }
        if status != 200 or not isinstance(data, dict):
            snippet = str(data)[:200] if data else "响应不是 JSON"
            return {
                "ok": False,
                "connected": True,
                "status": status,
                "models": [],
                "count": 0,
                "message": f"服务返回 HTTP {status}：{snippet}",
            }
        rows = data.get("data")
        if not isinstance(rows, list):
            rows = data.get("models") if isinstance(data.get("models"), list) else []
        models: list[dict] = []
        for row in rows:
            if not isinstance(row, dict) or not row.get("id"):
                continue
            context, output, _hits = self._pick_limits(row)
            entry = {
                "id": str(row.get("id")),
                "owned_by": row.get("owned_by"),
                "created": row.get("created"),
                "max_model_len": row.get("max_model_len"),
            }
            if context:
                entry["context_length"] = context
                entry["context_source"] = "server"
            if output:
                entry["max_output_tokens"] = output
                entry["max_output_source"] = "server"
            models.append(entry)
        if not models:
            return {
                "ok": False,
                "connected": True,
                "status": status,
                "models": [],
                "count": 0,
                "message": "服务可达但未返回任何模型",
            }
        _MODEL_CACHE[(self.endpoint, self.api_key)] = (time.monotonic(), [item["id"] for item in models])
        return {
            "ok": True,
            "connected": True,
            "status": status,
            "models": models,
            "count": len(models),
            "message": f"连接成功，已获取 {len(models)} 个模型",
        }

    def invalidate_model_cache(self) -> None:
        """清掉本适配器对应的模型列表缓存（设置页刷新后调用）。"""
        _MODEL_CACHE.pop((self.endpoint, self.api_key), None)

    def _check_model_available(self) -> None:
        """调用前轻量校验配置的模型仍在服务端列表中。

        缓存 30 秒，避免每次生成都多打一次 `/v1/models`；列表接口本身不可用时
        不阻断调用（由真实请求给出可读错误），只在确认列表为空或缺少目标模型时报错。

        异常:
            AdapterError(INVALID_INPUT): 配置模型不在服务端返回的模型列表里。
        """
        if not self.endpoint or not self.model:
            return
        key = (self.endpoint, self.api_key)
        cached = _MODEL_CACHE.get(key)
        if cached and time.monotonic() - cached[0] < _MODEL_CACHE_TTL_S:
            ids = cached[1]
        else:
            status, data, _error = self._probe("/v1/models", 2.0)
            # 列表接口失败（401/超时/非标准实现）交给真实请求处理，避免误伤可用服务
            if status != 200 or not isinstance(data, dict):
                return
            rows = data.get("data") if isinstance(data.get("data"), list) else []
            ids = [str(row.get("id")) for row in rows if isinstance(row, dict) and row.get("id")]
            if not ids:
                return
            _MODEL_CACHE[key] = (time.monotonic(), ids)
        if self.model not in ids:
            raise AdapterError(
                ErrorCode.INVALID_INPUT,
                f"模型 {self.model} 不在服务端模型列表中，当前可用：{'、'.join(ids)}。"
                "请到「设置 → 写作模型」重新选择模型后再试",
                detail={"model": self.model, "available": ids, "endpoint": self.endpoint},
            )

    def _probe(self, path: str, timeout_s: float) -> tuple[int | None, dict | None, str | None]:
        """快速 GET 一个端点，用于上限探测。

        参数:
            path: 以 / 开头的接口路径。
            timeout_s: 超时秒数（设置页检测用 2 秒级）。

        返回:
            `(HTTP 状态, JSON dict 或 None, 错误文本)`；404、非 JSON、网络错误都不抛异常。
        """
        request = urllib.request.Request(self._url(path), headers=self._headers(), method="GET")
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                try:
                    data = json.loads(raw) if raw.strip() else {}
                except json.JSONDecodeError:
                    data = None
                return int(getattr(resp, "status", 200)), data if isinstance(data, dict) else None, None
        except urllib.error.HTTPError as exc:
            return int(exc.code), None, None
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
            return None, None, str(getattr(exc, "reason", exc) or exc)

    @staticmethod
    def _pick_limits(data: dict) -> tuple[int | None, int | None, list[str]]:
        """从任意字典里挑选上下文/输出上限字段（兼容不同实现的命名）。

        参数:
            data: 接口返回的模型对象或详情 dict。

        返回:
            `(上下文上限, 最大输出, 命中字段名列表)`；取不到时为 None。
        """

        def first(fields: tuple[str, ...]) -> int | None:
            for key in fields:
                value = data.get(key)
                if isinstance(value, (int, float)) and int(value) > 0:
                    return int(value)
            return None

        context = first(_CONTEXT_FIELDS)
        output = first(_OUTPUT_FIELDS)
        hits = [
            key
            for key in _CONTEXT_FIELDS + _OUTPUT_FIELDS
            if isinstance(data.get(key), (int, float)) and int(data[key]) > 0
        ]
        return context, output, hits

    @staticmethod
    def _merge_limits(entry: dict, context: int | None, output: int | None) -> None:
        """把探测到的上限合并进模型条目，只补缺失值，来源记为 server。

        参数:
            entry: 模型条目 dict（会被原地修改）。
            context: 探测到的上下文上限。
            output: 探测到的最大输出。
        """
        if context and not entry.get("context_length"):
            entry["context_length"] = context
            entry["context_source"] = "server"
        if output and not entry.get("max_output_tokens"):
            entry["max_output_tokens"] = output
            entry["max_output_source"] = "server"

    def fetch_model_limits(self, model: str | None = None, timeout_s: float = 2.0) -> dict:
        """探测模型的上下文与最大输出上限（只读 OpenAI 兼容接口，不读本机文件）。

        探测顺序：`/v1/models` 列表项候选字段 → `/v1/models/{id}` 详情 →
        `/v1/models/status` 等服务特有端点 → `/props` 等通用候选端点。

        参数:
            model: 目标模型；缺省用配置里的 model。
            timeout_s: 单个端点的超时秒数（设置页检测用 2 秒级，失败快速返回）。

        返回:
            dict：`{model, context_length, context_source, max_output_tokens,
            max_output_source, models, probes, message}`；
            来源只有 `server`（接口字段）与 `unknown`（未取到）两种。
        """
        target = str(model or self.model or "").strip()
        probes: list[dict] = []
        result = {
            "model": target,
            "context_length": None,
            "context_source": "unknown",
            "max_output_tokens": None,
            "max_output_source": "unknown",
            "models": [],
            "probes": probes,
        }
        if not self.endpoint:
            result["message"] = "未配置接口地址：请先填写 omlx.endpoint 后再检测"
            return result

        # 1) 列表接口：不同实现的候选字段都试一遍
        status, data, error = self._probe("/v1/models", timeout_s)
        fields: list[str] = []
        models: list[dict] = []
        rows = (data or {}).get("data") if isinstance(data, dict) else None
        if isinstance(rows, list):
            for item in rows:
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                context, output, hits = self._pick_limits(item)
                fields.extend(hits)
                models.append(
                    {
                        "id": str(item.get("id")),
                        "owned_by": item.get("owned_by"),
                        "context_length": context,
                        "context_source": "server" if context else "unknown",
                        "max_output_tokens": output,
                        "max_output_source": "server" if output else "unknown",
                    }
                )
        probes.append({"path": "/v1/models", "status": status, "fields": sorted(set(fields)), "error": error})
        if not isinstance(rows, list):
            result["message"] = (
                f"读取 /v1/models 失败（{error or status or '无响应'}）："
                "接口不可达或返回非 JSON；可稍后重试，或手动填写最大输出"
            )
            return result

        selected = next((item for item in models if item["id"] == target), None)
        if not target:
            # 未选模型：只返回列表供页面下拉使用，绝不自动挑一个模型去探测/请求
            result["models"] = models
            result["message"] = "未选择模型，请在设置页检测连接后选择"
            return result
        if selected is None:
            # 已保存的模型不在当前列表：明确报错，不静默换成其他模型
            result["models"] = models
            result["message"] = f"已保存的模型 {target} 不在当前服务列表中，请重新选择"
            return result
        # 2) 单模型详情（有的实现支持）
        if target:
            path = f"/v1/models/{urllib.parse.quote(target, safe='')}"
            status, data, error = self._probe(path, timeout_s)
            fields = []
            context = output = None
            if isinstance(data, dict):
                context, output, fields = self._pick_limits(data)
                for key in ("model", "data"):
                    nested = data.get(key)
                    if isinstance(nested, dict):
                        nested_context, nested_output, nested_fields = self._pick_limits(nested)
                        context = context or nested_context
                        output = output or nested_output
                        fields.extend(nested_fields)
            probes.append({"path": path, "status": status, "fields": sorted(set(fields)), "error": error})
            if selected is not None:
                self._merge_limits(selected, context, output)
        # 3) 服务特有端点（omlx 有 /v1/models/status；换服务时可能 404，忽略即可）
        status, data, error = self._probe("/v1/models/status", timeout_s)
        fields = []
        if isinstance(data, dict):
            # omlx 实测形态：上限字段直接放在顶层（model_context_length / max_context_window / max_tokens）
            top_context, top_output, top_hits = self._pick_limits(data)
            if top_hits:
                fields.extend(top_hits)
                # 顶层字段可能带模型标识；只有未标注或标注与目标一致时才并入当前目标模型
                # （不用裸 id：部分实现会把它当作请求标识，容易误判）
                owner = ""
                for key in ("model", "model_id", "current_model", "loaded_model"):
                    if data.get(key):
                        owner = str(data[key])
                        break
                if selected is not None and (not owner or owner == target):
                    self._merge_limits(selected, top_context, top_output)
        if isinstance(data, dict) and isinstance(data.get("models"), list):
            for item in data["models"]:
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                context, output, hits = self._pick_limits(item)
                fields.extend(hits)
                entry = next((row for row in models if row["id"] == str(item["id"])), None)
                if entry is None:
                    entry = {
                        "id": str(item["id"]),
                        "owned_by": None,
                        "context_length": None,
                        "context_source": "unknown",
                        "max_output_tokens": None,
                        "max_output_source": "unknown",
                    }
                    models.append(entry)
                self._merge_limits(entry, context, output)
                if target and entry["id"] == target:
                    selected = entry
        probes.append({"path": "/v1/models/status", "status": status, "fields": sorted(set(fields)), "error": error})
        # 4) 通用候选端点：能识别就识别，识别不到只记录 404
        for path in _LIMIT_PROBE_PATHS:
            status, data, error = self._probe(path, timeout_s)
            fields = []
            context = output = None
            if isinstance(data, dict):
                context, output, fields = self._pick_limits(data)
                for key in ("model", "data"):
                    nested = data.get(key)
                    if isinstance(nested, dict):
                        nested_context, nested_output, nested_fields = self._pick_limits(nested)
                        context = context or nested_context
                        output = output or nested_output
                        fields.extend(nested_fields)
            probes.append({"path": path, "status": status, "fields": sorted(set(fields)), "error": error})
            if selected is not None:
                self._merge_limits(selected, context, output)

        if selected is not None:
            result["context_length"] = selected["context_length"]
            result["context_source"] = selected["context_source"]
            result["max_output_tokens"] = selected["max_output_tokens"]
            result["max_output_source"] = selected["max_output_source"]
        if result["context_length"]:
            # 缓存供请求体预算校验使用（不落盘，进程内共享）
            _LIMIT_CACHE[(self.endpoint, target)] = int(result["context_length"])
        result["models"] = models
        if result["context_length"] and result["max_output_tokens"]:
            result["message"] = (
                f"已按 {target} 检测：上下文 {result['context_length']} tokens / "
                f"最大输出 {result['max_output_tokens']} tokens（来源：服务端字段）"
            )
        elif result["context_length"]:
            result["message"] = (
                f"已按 {target} 检测到上下文 {result['context_length']} tokens（来源：服务端字段）；"
                "服务端未返回最大输出，可手动填写"
            )
        elif result["max_output_tokens"]:
            result["message"] = (
                f"已按 {target} 检测到最大输出 {result['max_output_tokens']} tokens（来源：服务端字段）；"
                "服务端未返回上下文上限"
            )
        elif not result.get("message"):
            result["message"] = (
                f"服务端未返回 {target or '所选模型'} 的上下文/输出上限"
                "（已尝试 /v1/models、/v1/models/{id}、/v1/models/status 等）：可手动填写最大输出"
            )
        return result

    def model_status(self) -> dict:
        """获取模型加载状态（GET /v1/models/status）。

        返回:
            含 final_ceiling、current_model_memory、models 的 dict；接口不可用时返回空 dict。
        """
        try:
            return self._request_json("GET", "/v1/models/status")
        except AdapterError:
            return {}

    def loaded_models(self) -> list[str]:
        """返回当前已加载的模型 ID 列表。"""
        status = self.model_status()
        return [str(item.get("id")) for item in status.get("models") or [] if item.get("loaded")]

    def health(self, timeout_s: float | None = None) -> HealthStatus:
        """检查服务可达性与配置模型是否存在。

        参数:
            timeout_s: 覆盖默认超时（自检时用较短超时）。

        返回:
            HealthStatus；ok=False 时 detail 给出可操作原因。
        """
        if not self.endpoint:
            return HealthStatus(ok=False, detail="omlx 未配置 endpoint：请在设置页填写服务地址（或配置 omlx.endpoint）")
        started = time.monotonic()
        try:
            models = self.list_models() if timeout_s is None else self.list_models(timeout_s)
        except AdapterError as exc:
            return HealthStatus(ok=False, detail=exc.message, extra={"code": exc.code.value})
        latency = int((time.monotonic() - started) * 1000)
        ids = [str(item.get("id")) for item in models if item.get("id")]
        extra = {"models": ids, "endpoint": self.endpoint}
        if self.model and self.model not in ids:
            return HealthStatus(
                ok=False,
                detail=f"模型 {self.model} 不在服务列表中：可用模型 {ids}；请在设置页重新检测并选择模型",
                latency_ms=latency,
                extra=extra,
            )
        status = {}
        try:
            status = self._request_json("GET", "/v1/models/status", timeout_s=timeout_s) if timeout_s else self.model_status()
        except AdapterError:
            status = {}
        if status:
            extra.update(
                {
                    "loaded": [str(item.get("id")) for item in status.get("models") or [] if item.get("loaded")],
                    "memory_bytes": status.get("current_model_memory"),
                    "ceiling_bytes": status.get("final_ceiling"),
                }
            )
        return HealthStatus(ok=True, detail=f"omlx 可达，模型 {self.model} 存在（共 {len(ids)} 个）", latency_ms=latency, extra=extra)
