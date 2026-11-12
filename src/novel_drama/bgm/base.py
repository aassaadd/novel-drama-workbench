"""配乐 provider 统一接口。

新增一个配乐模型只要实现 `BgmProvider`：声明可用性、把请求翻译成自家调用。
上层（CLI / 渲染）不感知模型名，也不做静默回退。
"""

from __future__ import annotations

from typing import Protocol

from .types import BgmHealth, BgmRequest, BgmResult, BgmSpec


class BgmProvider(Protocol):
    """所有配乐适配器必须满足的接口。"""

    spec: BgmSpec

    def health(self) -> BgmHealth:
        """检查这个 provider 当前是否可用。"""
        ...

    def generate(self, request: BgmRequest) -> BgmResult:
        """生成一段配乐并返回落盘结果。"""
        ...

    def close(self) -> None:
        """关闭常驻子进程；无状态 provider 可为空实现。"""
        ...


class BaseBgmProvider:
    """适配器公共实现：保存配置，默认关闭为空操作。"""

    def __init__(self, spec: BgmSpec) -> None:
        """初始化。

        参数:
            spec: provider 配置。
        """
        self.spec = spec

    def health(self) -> BgmHealth:  # pragma: no cover - 由子类实现
        """默认不可用，子类必须覆盖。"""
        return BgmHealth(self.spec.provider_id, False, "未实现健康检查", code="NOT_IMPLEMENTED")

    def generate(self, request: BgmRequest) -> BgmResult:  # pragma: no cover
        """默认不可用，子类必须覆盖。"""
        raise NotImplementedError(self.spec.provider_id)

    def close(self) -> None:
        """无状态 provider 无需关闭。"""
