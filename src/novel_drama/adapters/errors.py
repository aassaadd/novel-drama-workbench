"""适配器层公共错误与健康状态。

四类能力（写作 / TTS / 音效 / ComfyUI）共用同一套错误码与健康状态结构，
避免每个适配器各造一套；编排层只需判断 `ErrorCode`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


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
