"""未装配写作适配器时的兜底实现：任何调用都明确报错，避免静默失败。"""

from __future__ import annotations

from typing import Iterator

from ..errors import AdapterError, ErrorCode, HealthStatus
from .contracts import TextGenChunk, TextGenRequest, TextGenResult


class UnavailableWritingAdapter:
    """未装配写作适配器时的兜底实现，所有调用直接报错，避免静默失败。"""

    def __init__(self, reason: str = "未配置写作适配器"):
        """记录不可用原因。

        参数:
            reason: 面向用户的原因。
        """
        self.reason = reason

    def generate(self, req: TextGenRequest) -> TextGenResult:
        """始终抛出 NOT_IMPLEMENTED。"""
        raise AdapterError(ErrorCode.NOT_IMPLEMENTED, f"写作能力不可用：{self.reason}")

    def stream(self, req: TextGenRequest) -> Iterator[TextGenChunk]:
        """始终抛出 NOT_IMPLEMENTED。"""
        raise AdapterError(ErrorCode.NOT_IMPLEMENTED, f"写作能力不可用：{self.reason}")

    def health(self, timeout_s: float | None = None) -> HealthStatus:
        """返回不可用状态。"""
        return HealthStatus(ok=False, detail=self.reason)

    def unload(self, model: str | None = None) -> dict:
        """未装配时无法卸载。"""
        raise AdapterError(ErrorCode.NOT_IMPLEMENTED, f"写作能力不可用：{self.reason}")

    def generate_tool(self, req: TextGenRequest, tool: dict, timeout_s: float | None = None) -> dict:
        """未装配时无法做工具调用。"""
        raise AdapterError(ErrorCode.NOT_IMPLEMENTED, f"写作能力不可用：{self.reason}")

    def list_models(self) -> list[dict]:
        """未装配时没有模型列表。"""
        raise AdapterError(ErrorCode.NOT_IMPLEMENTED, f"写作能力不可用：{self.reason}")
