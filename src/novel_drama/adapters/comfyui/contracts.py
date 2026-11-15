"""ComfyUI 契约：模板、提交 / 状态 / 产物结构与 `ComfyGateway` 协议。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ..errors import HealthStatus


@dataclass
class WorkflowTemplate:
    """本地工作流模板元信息。"""

    template_id: str
    path: str
    placeholders: list[str] = field(default_factory=list)
    valid: bool = True
    error: str | None = None


@dataclass
class ComfySubmitResult:
    """提交结果。"""

    prompt_id: str
    client_id: str
    number: int | None = None


@dataclass
class ComfyJobStatus:
    """任务状态。"""

    state: str  # pending|running|success|error|canceled
    progress: float | None = None
    current_node: str | None = None
    message: str | None = None
    error: dict | None = None


@dataclass
class ComfyArtifact:
    """ComfyUI 取回的产物。"""

    kind: str  # image|video|audio|other
    filename: str
    subfolder: str
    server_path: str
    local_path: str | None = None


class ComfyGateway(Protocol):
    """ComfyUI 网关：只做填参、提交、轮询、取产物。"""

    def health(self) -> HealthStatus:
        """检查 ComfyUI 是否可用。"""
        ...

    def list_templates(self) -> list[WorkflowTemplate]:
        """列出本地模板并校验占位符。"""
        ...

    def submit(self, template_id: str, params: dict, client_id: str) -> ComfySubmitResult:
        """按模板填参并提交任务。"""
        ...

    def submit_prompt(self, prompt: dict, client_id: str) -> ComfySubmitResult:
        """提交已渲染的 API 格式工作流。"""
        ...

    def upload_image(self, path: str, subfolder: str = "ndw") -> dict:
        """上传图片（i2v 首帧），返回 ComfyUI 的 name/subfolder/type。"""
        ...

    def status(self, prompt_id: str) -> ComfyJobStatus:
        """查询任务状态（必须使用短超时）。"""
        ...

    def fetch(self, prompt_id: str, dest_dir: str) -> list[ComfyArtifact]:
        """取回产物到工程目录。"""
        ...

    def cancel(self, prompt_id: str) -> None:
        """从队列删除任务。"""
        ...

    def interrupt(self) -> None:
        """中断当前执行。"""
        ...

    def queue_snapshot(self) -> dict:
        """返回 ComfyUI 队列快照。"""
        ...
