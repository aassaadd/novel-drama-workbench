"""音效 provider 统一接口。

新增一个音效模型只要实现 `SfxProvider`：声明可用性、把请求翻译成自家调用。
上层（CLI / 编排）不感知模型名，也不做静默回退。
"""

from __future__ import annotations

from typing import Protocol

from .types import SfxHealth, SfxRequest, SfxResult, SfxSpec


class SfxProvider(Protocol):
    """所有音效适配器必须满足的接口。"""

    spec: SfxSpec

    def health(self) -> SfxHealth:
        """检查这个 provider 当前是否可用。"""
        ...

    def generate(self, request: SfxRequest) -> SfxResult:
        """生成一段音效并返回落盘结果。"""
        ...

    def close(self) -> None:
        """关闭常驻子进程；无状态 provider 可为空实现。"""
        ...


class BaseSfxProvider:
    """适配器公共实现：保存配置，默认关闭为空操作。"""

    def __init__(self, spec: SfxSpec) -> None:
        """初始化。

        参数:
            spec: provider 配置。
        """
        self.spec = spec

    def health(self) -> SfxHealth:  # pragma: no cover - 由子类实现
        """默认不可用，子类必须覆盖。"""
        return SfxHealth(self.spec.provider_id, False, "未实现健康检查", code="NOT_IMPLEMENTED")

    def generate(self, request: SfxRequest) -> SfxResult:  # pragma: no cover
        """默认不可用，子类必须覆盖。"""
        raise NotImplementedError(self.spec.provider_id)

    def close(self) -> None:
        """无状态 provider 无需关闭。"""
