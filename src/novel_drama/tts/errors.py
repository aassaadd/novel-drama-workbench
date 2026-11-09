"""TTS 统一错误码。

所有 provider 适配器必须把底层异常映射到这里的错误码，上层只认码，
不解析各家模型的报错文本。
"""

from __future__ import annotations

# 可用性
PROVIDER_UNREACHABLE = "PROVIDER_UNREACHABLE"
PROVIDER_UNAUTHORIZED = "PROVIDER_UNAUTHORIZED"
MODEL_MISSING = "MODEL_MISSING"
PROVIDER_OOM = "PROVIDER_OOM"
PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
PROVIDER_DISABLED = "PROVIDER_DISABLED"
NOT_AUTO_SELECTABLE = "NOT_AUTO_SELECTABLE"
# 能力与许可
UNSUPPORTED_VOICE = "UNSUPPORTED_VOICE"
UNSUPPORTED_EMOTION = "UNSUPPORTED_EMOTION"
UNSUPPORTED_NONVERBAL = "UNSUPPORTED_NONVERBAL"
UNSUPPORTED_LANGUAGE = "UNSUPPORTED_LANGUAGE"
UNSUPPORTED_DURATION = "UNSUPPORTED_DURATION"
UNSUPPORTED_STREAMING = "UNSUPPORTED_STREAMING"
LICENSE_BLOCKED = "LICENSE_BLOCKED"
# 产物与路由
BAD_OUTPUT = "BAD_OUTPUT"
NOT_IMPLEMENTED = "NOT_IMPLEMENTED"
ROUTING_NO_CANDIDATE = "ROUTING_NO_CANDIDATE"
ROUTING_LOCKED_MISSING = "ROUTING_LOCKED_MISSING"
VOICE_MISSING = "VOICE_MISSING"

# 错误码默认是否可重试
RETRYABLE = {
    PROVIDER_UNREACHABLE: True,
    PROVIDER_UNAUTHORIZED: False,
    MODEL_MISSING: False,
    PROVIDER_OOM: True,
    PROVIDER_TIMEOUT: True,
    PROVIDER_DISABLED: False,
    NOT_AUTO_SELECTABLE: False,
    UNSUPPORTED_VOICE: False,
    UNSUPPORTED_EMOTION: False,
    UNSUPPORTED_NONVERBAL: False,
    UNSUPPORTED_LANGUAGE: False,
    UNSUPPORTED_DURATION: False,
    UNSUPPORTED_STREAMING: False,
    LICENSE_BLOCKED: False,
    BAD_OUTPUT: True,
    NOT_IMPLEMENTED: False,
    ROUTING_NO_CANDIDATE: False,
    ROUTING_LOCKED_MISSING: False,
    VOICE_MISSING: False,
}

# 错误码对应的可操作提示
HINTS = {
    PROVIDER_UNREACHABLE: "检查服务是否启动、endpoint 是否正确",
    PROVIDER_UNAUTHORIZED: "检查 api_key_env 指向的环境变量是否已设置",
    MODEL_MISSING: "检查 weights_path 下是否有 config.json 与 model.safetensors",
    PROVIDER_OOM: "关闭其他占用显存的进程，或换成显存更小的 provider",
    PROVIDER_TIMEOUT: "缩短文本或调大 request_timeout_s",
    PROVIDER_DISABLED: "在 config/config.yaml 把该 provider 的 enabled 设为 true",
    NOT_AUTO_SELECTABLE: "该 provider 声明不允许自动选中，需手动锁定",
    UNSUPPORTED_VOICE: "换一个支持该音色变体的 provider，或为该 provider 准备参考音频",
    UNSUPPORTED_EMOTION: "为该 provider 增加对应情绪/强度的音色变体，或改选其他 provider",
    UNSUPPORTED_NONVERBAL: "去掉副语言标记，或改选支持该副语言的 provider",
    UNSUPPORTED_LANGUAGE: "改选支持该语言的 provider，或关闭语言硬过滤",
    UNSUPPORTED_DURATION: "换用支持原生时长控制的 provider，或接受切分+静音补偿",
    UNSUPPORTED_STREAMING: "该 provider 不支持流式，改为整块合成或改选其他 provider",
    LICENSE_BLOCKED: "工程用途为商用，需换用许可干净的 provider 或取得授权",
    BAD_OUTPUT: "重试；若持续失败检查磁盘空间与输出路径权限",
    NOT_IMPLEMENTED: "该 provider 本轮只留接口，未接入权重",
    ROUTING_NO_CANDIDATE: "检查 provider 是否启用、许可与能力是否满足；或手动锁定一个 provider",
    ROUTING_LOCKED_MISSING: "锁定的 provider 未在配置中启用，改锁其他 provider 或改为自动",
    VOICE_MISSING: "为角色准备音色变体与参考音频",
}


class TtsError(Exception):
    """TTS 统一异常。

    参数:
        code: 统一错误码（见本模块常量）。
        message: 人读消息。
        provider_id: 出错的 provider。
        retryable: 是否可重试；缺省按错误码默认值。
        hint: 可操作提示；缺省按错误码默认值。
    """

    def __init__(
        self,
        code: str,
        message: str,
        provider_id: str | None = None,
        retryable: bool | None = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.provider_id = provider_id
        self.retryable = RETRYABLE.get(code, False) if retryable is None else retryable
        self.hint = HINTS.get(code, "") if hint is None else hint

    def to_dict(self) -> dict:
        """转成可落盘的错误结构。"""
        return {
            "code": self.code,
            "message": self.message,
            "provider_id": self.provider_id,
            "retryable": self.retryable,
            "hint": self.hint,
        }
