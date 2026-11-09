"""音效适配（本轮占位）。

对接约定：
- 统一为「描述进、音频出」，生成式模型与本地素材库都实现同一接口；
- 允许返回短于请求时长的音频（由时间轴决定循环或补静音）；
- 返回 `cache_key` 时，编排层可直接复用既有产物，不重复生成。
"""

from __future__ import annotations

from .base import HealthStatus, SfxCandidate, SfxRequest, SfxResult, not_implemented


class PlaceholderSfxAdapter:
    """未对接的音效实现。"""

    def __init__(self, settings: dict | None = None):
        """绑定音效配置。

        参数:
            settings: {adapter, default_gain_db}。
        """
        self.settings = settings or {}

    def generate(self, req: SfxRequest) -> SfxResult:
        """生成音效。

        参数:
            req: 音效请求（prompt/kind/duration_ms/loop）。

        返回:
            SfxResult（audio_path 为工程内相对路径）。

        异常:
            AdapterError(NOT_IMPLEMENTED): 始终抛出。
        """
        raise not_implemented(
            "音效适配器",
            "实现 SfxAdapter.generate：按 output_path 写音频并返回实测 duration_ms/sample_rate/channels；支持素材库时返回稳定 cache_key",
        )

    def search(self, query: str, kind: str, limit: int = 10) -> list[SfxCandidate]:
        """检索素材库。

        参数:
            query: 检索词。
            kind: 音效类型。
            limit: 返回上限。

        返回:
            候选列表。

        异常:
            AdapterError(NOT_IMPLEMENTED): 始终抛出。
        """
        raise not_implemented("音效素材库检索", "若音效实现为素材库，请实现 search 并返回按 score 降序的候选列表")

    def health(self) -> HealthStatus:
        """检查音效服务可用性。"""
        return HealthStatus(ok=False, detail="音效适配器未对接（本轮为骨架）")
