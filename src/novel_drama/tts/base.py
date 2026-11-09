"""TTS provider 统一接口。

新增一个模型只要实现 `TtsProvider`：声明能力、做健康检查、把合成请求
翻译成自家调用。上层（路由、CLI、任务）不感知模型名。
"""

from __future__ import annotations

from typing import Protocol

from .types import HealthStatus, ProviderSpec, SynthesisRequest, SynthesisResult


class TtsProvider(Protocol):
    """所有 TTS 适配器必须满足的接口。"""

    spec: ProviderSpec

    def health(self) -> HealthStatus:
        """检查这个 provider 当前是否可用。"""
        ...

    def synthesize(self, request: SynthesisRequest) -> SynthesisResult:
        """合成一段文本并返回落盘结果。"""
        ...

    def unload(self) -> None:
        """释放常驻内存/显存；无状态 provider 可为空实现。"""
        ...


class BaseProvider:
    """适配器公共实现：保存配置、默认卸载。"""

    def __init__(self, spec: ProviderSpec) -> None:
        """初始化。

        参数:
            spec: provider 配置。
        """
        self.spec = spec

    def health(self) -> HealthStatus:  # pragma: no cover - 由子类实现
        """默认不可用，子类必须覆盖。"""
        return HealthStatus(self.spec.provider_id, False, "未实现健康检查", code="NOT_IMPLEMENTED")

    def synthesize(self, request: SynthesisRequest) -> SynthesisResult:  # pragma: no cover
        """默认不可用，子类必须覆盖。"""
        raise NotImplementedError(self.spec.provider_id)

    def unload(self) -> None:
        """无状态 provider 无需卸载。"""
        return None
