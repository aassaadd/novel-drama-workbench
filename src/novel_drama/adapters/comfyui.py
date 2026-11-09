"""ComfyUI 网关（本轮只做本地模板检查，不发起网络请求）。

对接约定（详见 docs/architecture.md §3.5）：
- 模板为 ComfyUI API 格式 JSON，放在 config/comfy/<template_id>.json；
- 模板中用 `{{param}}` 占位，工作台只做字符串替换，不生成/修改节点；
- 提交前必须 health()，连不上报 UNAVAILABLE 并提示检查地址与进程；
- 状态查询使用短超时（默认 10s），避免 ComfyUI 无响应时长时间阻塞；
- 取回的产物写入工程 assets/，不直接引用 ComfyUI 的 output 目录。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .base import (
    ComfyArtifact,
    ComfyJobStatus,
    ComfySubmitResult,
    HealthStatus,
    WorkflowTemplate,
    not_implemented,
)

_PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")


class LocalComfyGateway:
    """本机 ComfyUI 网关（占位实现 + 模板检查）。"""

    def __init__(self, settings: dict, templates_dir: str | Path):
        """绑定 ComfyUI 配置与模板目录。

        参数:
            settings: {base_url, poll_interval_ms, job_timeout_ms, status_timeout_ms, templates}。
            templates_dir: 本地工作流模板目录。
        """
        self.settings = settings or {}
        self.templates_dir = Path(templates_dir).expanduser()

    def health(self) -> HealthStatus:
        """检查 ComfyUI 可用性（本轮不发网络请求）。

        返回:
            HealthStatus，说明本轮未启用网络调用与待实现的接口。
        """
        return HealthStatus(ok=False, detail="本轮未启用 ComfyUI 网络调用；对接后实现 GET /system_stats 健康检查")

    def list_templates(self) -> list[WorkflowTemplate]:
        """扫描模板目录并校验占位符。

        返回:
            模板元信息列表；文件缺失或 JSON 非法时 `valid=false` 并给出错误。
        """
        templates: list[WorkflowTemplate] = []
        if not self.templates_dir.exists():
            return templates
        for path in sorted(self.templates_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                placeholders = sorted(set(_PLACEHOLDER.findall(path.read_text(encoding="utf-8"))))
                valid = isinstance(data, dict) and bool(data)
                templates.append(
                    WorkflowTemplate(
                        template_id=path.stem,
                        path=str(path),
                        placeholders=placeholders,
                        valid=valid,
                        error=None if valid else "模板必须是 API 格式的非空 JSON 对象",
                    )
                )
            except (json.JSONDecodeError, OSError) as exc:
                templates.append(WorkflowTemplate(template_id=path.stem, path=str(path), valid=False, error=str(exc)))
        return templates

    def submit(self, template_id: str, params: dict, client_id: str) -> ComfySubmitResult:
        """提交任务到 ComfyUI。

        参数:
            template_id: 模板 ID。
            params: 占位符参数。
            client_id: 客户端标识。

        返回:
            ComfySubmitResult。

        异常:
            AdapterError(NOT_IMPLEMENTED): 本轮未实现。
        """
        raise not_implemented(
            "ComfyUI 提交",
            "实现：读取 config/comfy/<template_id>.json，替换 {{param}} 后 POST /prompt，返回 prompt_id/client_id",
        )

    def status(self, prompt_id: str) -> ComfyJobStatus:
        """查询任务状态。"""
        raise not_implemented("ComfyUI 状态查询", "实现：结合 GET /history/{prompt_id} 与 GET /queue 映射为 pending/running/success/error/canceled")

    def fetch(self, prompt_id: str, dest_dir: str) -> list[ComfyArtifact]:
        """取回产物。"""
        raise not_implemented("ComfyUI 产物下载", "实现：按 /history 输出信息用 GET /view 下载到 dest_dir，再复制/移动到工程 assets/")

    def cancel(self, prompt_id: str) -> None:
        """取消任务。"""
        raise not_implemented("ComfyUI 取消", '实现：POST /queue {"delete": [prompt_id]}')

    def interrupt(self) -> None:
        """中断当前执行。"""
        raise not_implemented("ComfyUI 中断", "实现：POST /interrupt")

    def queue_snapshot(self) -> dict:
        """返回队列快照。"""
        raise not_implemented("ComfyUI 队列查询", "实现：GET /queue，返回 running/pending 列表")
