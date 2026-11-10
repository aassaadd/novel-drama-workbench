"""适配层公共契约：统一错误码、健康状态与四类能力的请求/响应结构。

本轮不接真实模型：具体实现要么是 `NotImplemented` 占位，要么只做本地文件检查。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Iterator, Protocol


class ErrorCode(str, Enum):
    """统一错误码；编排层据此决定是否重试与如何提示。"""

    NOT_IMPLEMENTED = "NOT_IMPLEMENTED"
    UNAVAILABLE = "UNAVAILABLE"
    TIMEOUT = "TIMEOUT"
    RATE_LIMITED = "RATE_LIMITED"
    INVALID_INPUT = "INVALID_INPUT"
    UPSTREAM_ERROR = "UPSTREAM_ERROR"
    CONTENT_BLOCKED = "CONTENT_BLOCKED"


class AdapterError(Exception):
    """适配器唯一允许抛出的异常类型。"""

    def __init__(self, code: ErrorCode, message: str, retryable: bool = False, detail: dict | None = None):
        """构造适配器错误。

        参数:
            code: 统一错误码。
            message: 面向用户的中文提示，必须可操作。
            retryable: 是否允许编排层自动重试。
            detail: 原始响应/日志片段，仅落日志。
        """
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.detail = detail or {}

    def to_dict(self) -> dict:
        """转为可写入任务 `error` 字段的 dict。"""
        return {"code": self.code.value, "message": self.message, "retryable": self.retryable, "detail": self.detail}


def not_implemented(what: str, contract: str) -> AdapterError:
    """构造「未对接」错误，并附带对接约定提示。

    参数:
        what: 能力名称，如「omlx 写作适配器」。
        contract: 对接约定摘要。

    返回:
        AdapterError（NOT_IMPLEMENTED，不可重试）。
    """
    return AdapterError(ErrorCode.NOT_IMPLEMENTED, f"{what}尚未对接：{contract}", retryable=False)


@dataclass
class HealthStatus:
    """适配器/服务健康状态。"""

    ok: bool
    detail: str = ""
    latency_ms: int | None = None
    extra: dict = field(default_factory=dict)


# ---------- 写作模型（omlx） ----------


@dataclass
class Message:
    """对话消息。"""

    role: str  # system|user|assistant
    content: str


@dataclass
class Usage:
    """token 用量。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass
class TextGenRequest:
    """统一文本生成请求；所有文本任务共用此结构。"""

    task: str  # outline|chapter|scene|dialogue_split|emotion_tag|image_prompt|freeform
    system: str
    messages: list[Message]
    max_tokens: int = 2048
    temperature: float = 0.8
    stop: list[str] = field(default_factory=list)
    stream: bool = True
    context_budget_tokens: int = 32768
    metadata: dict = field(default_factory=dict)


@dataclass
class TextGenChunk:
    """流式生成的一个增量块。"""

    delta: str
    done: bool = False
    usage: Usage | None = None


@dataclass
class TextGenResult:
    """非流式/聚合后的生成结果。"""

    text: str
    finish_reason: str  # stop|length|error
    model: str = ""
    usage: Usage | None = None
    raw: dict | None = None


class WritingAdapter(Protocol):
    """写作模型适配接口；omlx 通过 OpenAI 兼容 HTTP 实现本接口。"""

    def generate(self, req: TextGenRequest) -> TextGenResult:
        """一次性返回完整文本。"""
        ...

    def stream(self, req: TextGenRequest) -> Iterator[TextGenChunk]:
        """流式返回增量文本。"""
        ...

    def health(self, timeout_s: float | None = None) -> HealthStatus:
        """检查写作服务是否可用。"""
        ...

    def list_models(self) -> list[dict]:
        """列出服务端可用模型。"""
        ...

    def unload(self, model: str | None = None) -> dict:
        """显式卸载模型，释放显存（omlx 不会自动卸载）。"""
        ...


# ---------- TTS ----------


@dataclass
class Voice:
    """音色描述，供角色音色库选择。"""

    voice_id: str
    name: str
    language: str = "zh-CN"
    gender: str = "unknown"
    adapter: str = ""
    extra: dict = field(default_factory=dict)


@dataclass
class TtsRequest:
    """单行配音请求（以台词行为单位，不做整章拼接）。"""

    text: str
    voice_id: str
    emotion: str = "neutral"
    emotion_intensity: float = 0.5
    speed: float = 1.0
    pause_before_ms: int = 0
    pause_after_ms: int = 0
    sample_rate: int = 48000
    format: str = "wav"
    reference_audio: str | None = None
    seed: int | None = None
    delivery_note: str | None = None
    output_path: str | None = None
    metadata: dict = field(default_factory=dict)


@dataclass
class TtsResult:
    """配音产物；`duration_ms` 必须为实测值。"""

    audio_path: str
    duration_ms: int
    sample_rate: int
    channels: int
    voice_id: str
    model: str = ""
    raw: dict | None = None


class TtsAdapter(Protocol):
    """多角色 TTS 适配接口。"""

    def list_voices(self) -> list[Voice]:
        """列出可用音色。"""
        ...

    def synthesize(self, req: TtsRequest) -> TtsResult:
        """合成单行音频。"""
        ...

    def health(self) -> HealthStatus:
        """检查 TTS 服务是否可用。"""
        ...


# ---------- 音效 ----------


@dataclass
class SfxRequest:
    """音效生成请求。"""

    prompt: str
    kind: str  # ambience|foley|impact|transition|music_sting
    duration_ms: int
    loop: bool = False
    seed: int | None = None
    output_path: str | None = None
    metadata: dict = field(default_factory=dict)


@dataclass
class SfxResult:
    """音效产物。"""

    audio_path: str
    duration_ms: int
    sample_rate: int
    channels: int
    provider: str = ""
    cache_key: str | None = None
    raw: dict | None = None


@dataclass
class SfxCandidate:
    """素材库检索候选。"""

    path: str
    name: str
    duration_ms: int
    cache_key: str
    score: float = 0.0
    extra: dict = field(default_factory=dict)


class SfxAdapter(Protocol):
    """音效适配接口：生成式与素材库检索统一为「描述进、音频出」。"""

    def generate(self, req: SfxRequest) -> SfxResult:
        """生成音效。"""
        ...

    def search(self, query: str, kind: str, limit: int = 10) -> list[SfxCandidate]:
        """检索素材库；不支持时抛 NOT_IMPLEMENTED。"""
        ...

    def health(self) -> HealthStatus:
        """检查音效服务是否可用。"""
        ...


# ---------- ComfyUI ----------


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
