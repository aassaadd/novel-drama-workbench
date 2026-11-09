"""尚未接入的 provider 占位适配器。

本轮只留接口与配置，不下载权重：Step-Audio-EditX、GLM-TTS。
IndexTTS 2.5 已接真实现，见 indextts25.py。
健康检查明确报「未接入」，避免路由把它们当成可用候选。
"""

from __future__ import annotations

from pathlib import Path

from ..base import BaseProvider
from ..errors import NOT_IMPLEMENTED, TtsError
from ..types import HealthStatus, SynthesisRequest, SynthesisResult


class _StubProvider(BaseProvider):
    """占位实现：健康检查看权重是否存在，合成一律拒绝。"""

    reason = "本轮只留接口，未接入权重"

    def health(self) -> HealthStatus:
        """权重缺失或未接入时统一报不可用。"""
        path = Path(self.spec.weights_path) if self.spec.weights_path else None
        exists = bool(path and (path / "model.safetensors").exists())
        detail = self.reason
        if not exists:
            detail = f"{self.reason}；权重不存在：{self.spec.weights_path}"
        return HealthStatus(self.spec.provider_id, False, detail, NOT_IMPLEMENTED)

    def synthesize(self, request: SynthesisRequest) -> SynthesisResult:
        """占位 provider 不参与合成。"""
        raise TtsError(NOT_IMPLEMENTED, f"{self.spec.provider_id} {self.reason}", self.spec.provider_id)


class StepAudioEditXProvider(_StubProvider):
    """Step-Audio-EditX 占位。"""

    reason = "Step-Audio-EditX 本轮只留接口，未下载权重"


class GlmTtsProvider(_StubProvider):
    """GLM-TTS 占位。"""

    reason = "GLM-TTS 本轮只留接口，未下载权重"
