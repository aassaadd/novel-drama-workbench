"""写作模型契约：请求 / 响应结构、用量统计与 `WritingAdapter` 协议。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator, Protocol

from ..errors import HealthStatus


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
    #: 前缀缓存命中的 token 数；上游不返回缓存统计时为 None
    cached_tokens: int | None = None


@dataclass
class TextGenRequest:
    """统一文本生成请求；所有文本任务共用此结构。"""

    task: str  # outline|chapter|scene|dialogue_split|emotion_tag|image_prompt|freeform
    system: str
    messages: list[Message]
    #: None 表示用设置页的温度（写作分组）；调用点需要特殊温度时显式传值
    temperature: float | None = None
    stop: list[str] = field(default_factory=list)
    stream: bool = True
    context_budget_tokens: int = 32768
    metadata: dict = field(default_factory=dict)
    #: 前缀缓存键（工程 id + 章节 id 等稳定值）；为 None 时不透传缓存开关
    prompt_cache_key: str | None = None


@dataclass
class TextGenChunk:
    """流式生成的一个增量块。"""

    delta: str
    done: bool = False
    usage: Usage | None = None
    #: 上游结束原因（stop/length 等）；只有 done 块上带值
    finish_reason: str = ""


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

    def generate_tool(self, req: TextGenRequest, tool: dict, timeout_s: float | None = None) -> dict:
        """以工具调用方式返回结构化参数（模型参数即结果）。"""
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
