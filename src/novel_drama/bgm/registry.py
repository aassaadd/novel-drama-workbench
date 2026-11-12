"""配乐 provider 注册表：把配置翻译成适配器实例。"""

from __future__ import annotations

from .providers.stable_audio_open import StableAudioOpenProvider
from .types import BgmSpec

# provider_id / model 关键字 → 适配器类
_FACTORY = {
    "stable_audio_open_10": StableAudioOpenProvider,
}

_MODEL_HINTS = (("stable-audio-open", StableAudioOpenProvider),)


def _pick_class(spec: BgmSpec):
    """根据 provider_id 或 model 选择适配器类。

    参数:
        spec: provider 配置。

    返回:
        适配器类；无法识别时返回 None。
    """
    if spec.provider_id in _FACTORY:
        return _FACTORY[spec.provider_id]
    model = (spec.model or "").lower()
    for hint, cls in _MODEL_HINTS:
        if hint in model:
            return cls
    return None


def build_providers(config) -> dict:
    """从配置构建配乐 provider 实例表（含未启用的）。

    参数:
        config: AppConfig。

    返回:
        {provider_id: BgmProvider 实例}；未知模型抛 ValueError，避免静默回退。
    """
    from ..tts.model_store import resolve_weights

    items = config.get("bgm.providers") or []
    providers: dict = {}
    for item in items:
        raw = dict(item)
        # 空闲退出既可按 provider 单独配置，也可用 bgm.idle_timeout_s 统一覆盖（runtime 优先）
        runtime = dict(raw.get("runtime") or {})
        if runtime.get("idle_timeout_s") is None:
            common = config.get("bgm.idle_timeout_s")
            if common:
                runtime["idle_timeout_s"] = common
        raw["runtime"] = runtime
        spec = BgmSpec.from_dict(raw)
        # 权重只从 paths.models_root 解析：相对路径拼根目录，绝对路径越界直接报错
        spec.weights_path = resolve_weights(config, spec.provider_id, raw.get("weights_path"))
        cls = _pick_class(spec)
        if cls is None:
            raise ValueError(f"未知配乐 provider：{spec.provider_id}（model={spec.model}）")
        providers[spec.provider_id] = cls(spec)
    return providers


def pick_provider(config, providers: dict, provider_id: str | None = None):
    """选出本次要用的本地 provider。

    参数:
        config: AppConfig。
        providers: build_providers() 的结果。
        provider_id: 显式指定的 provider；为空时取 `bgm.default_provider_id`，
            再退到第一个启用的 provider。

    返回:
        (provider_id, provider 实例)。

    异常:
        ValueError: 指定的 provider 不存在，或没有任何启用的 provider。
    """
    if provider_id:
        if provider_id not in providers:
            raise ValueError(f"未配置配乐 provider：{provider_id}（可选：{', '.join(providers) or '无'}）")
        return provider_id, providers[provider_id]
    default = config.get("bgm.default_provider_id")
    if default and default in providers:
        return default, providers[default]
    for pid, provider in providers.items():
        if provider.spec.enabled:
            return pid, provider
    raise ValueError("没有启用的配乐 provider：请在 config 的 bgm.providers 里设置 enabled: true")
