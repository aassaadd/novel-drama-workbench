"""TTS 多 provider 层。

对外只暴露服务与类型；路由、适配器、错误码是内部实现。
"""

from .service import TtsService, split_text
from .types import Requirement, SynthesisRequest, SynthesisResult, VoiceRef

__all__ = ["TtsService", "Requirement", "SynthesisRequest", "SynthesisResult", "VoiceRef", "split_text"]
