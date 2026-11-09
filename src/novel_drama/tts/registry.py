"""provider 注册表：把配置文件翻译成适配器实例。"""

from __future__ import annotations

from .model_store import resolve_weights
from .providers.cosyvoice3_mlx import CosyVoice3MlxProvider
from .providers.indextts25 import IndexTts25Provider
from .providers.stubs import GlmTtsProvider, StepAudioEditXProvider
from .types import ProviderSpec

# provider_id 前缀/型号 → 适配器类；新增模型时在这里或配置里显式指定
_FACTORY = {
    "cosyvoice3_mlx": CosyVoice3MlxProvider,
    "step_audio_editx": StepAudioEditXProvider,
    "glm_tts": GlmTtsProvider,
    "indextts_25": IndexTts25Provider,
}

# 按 model 关键字兜底识别
_MODEL_HINTS = (
    ("cosyvoice3", CosyVoice3MlxProvider),
    ("step-audio-editx", StepAudioEditXProvider),
    ("glm-tts", GlmTtsProvider),
    ("indextts", IndexTts25Provider),
)


def _pick_class(spec: ProviderSpec):
    """根据 provider_id 或 model 选择适配器类。

    参数:
        spec: provider 配置。
    """
    if spec.provider_id in _FACTORY:
        return _FACTORY[spec.provider_id]
    model = (spec.model or "").lower()
    for hint, cls in _MODEL_HINTS:
        if hint in model:
            return cls
    return None


def build_providers(config) -> dict:
    """从配置构建 provider 实例表（含未启用的，便于 doctor 报告）。

    参数:
        config: AppConfig。

    返回:
        {provider_id: TtsProvider 实例}；未知模型抛 ValueError，避免静默回退。
    """
    items = config.get("tts.providers") or []
    providers: dict = {}
    for raw in items:
        spec = ProviderSpec.from_dict(raw)
        # 把 hf_home 透传给适配器（只放 tokenizer 等缓存，权重必须走 models_root）
        spec.extra.setdefault("hf_home", config.get("tts.hf_home"))
        # 权重只从 paths.models_root 解析：相对路径拼根目录，绝对路径越界直接报错
        spec.weights_path = resolve_weights(config, spec.provider_id, raw.get("weights_path"))
        cls = _pick_class(spec)
        if cls is None:
            raise ValueError(f"未知 TTS provider：{spec.provider_id}（model={spec.model}）")
        providers[spec.provider_id] = cls(spec)
    return providers
