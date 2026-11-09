"""TTS provider 的数据结构定义。

模型差异（参数名、后端、错误文本）全部收敛在适配器内，这里只放
上层需要看到的中立结构：能力矩阵、合成请求、合成结果、路由需求。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 能力矩阵默认值：缺失字段一律按「不支持」处理，避免误用
_CAP_DEFAULTS: dict[str, Any] = {
    "emotion_control": "none",
    "emotion_instruction": False,
    "emotion_reference_variants": False,
    "emotion_intensity_levels": 0,
    "voice_clone": False,
    "clone_min_ref_ms": 0,
    "clone_recommended_ref_ms": 0,
    "clone_prompt_cache": False,
    "max_variants_per_voice": 1,
    "max_text_chars": 200,
    "max_text_ms": 30000,
    "nonverbal": [],
    "nonverbal_control": "none",
    "native_duration_control": False,
    "sample_rates": [24000],
    "output_formats": ["wav"],
    "ssml": False,
    "pause_control": "silence_event",
    "streaming": False,
    "native_speed": False,
    "native_pitch": False,
    "languages": ["zh-CN"],
    "license": "unknown",
    "license_gate": "non_commercial",
    "commercial_use": False,
    "license_note": None,
    "quality_tier": "standard",
    "latency_tier": "standard",
    "consistency": {"stability": "medium", "pin_per_character": False},
    "allow_auto_select": True,
}

# 质量档位 → 分数
QUALITY_SCORE = {"premium": 25.0, "standard": 18.0, "draft": 8.0}
# 耗时档位 → 分数
LATENCY_SCORE = {"fast": 15.0, "standard": 10.0, "slow": 3.0}


@dataclass
class Capabilities:
    """某个 provider 的能力声明。"""

    data: dict = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        """读能力字段，缺失时回退默认值。

        参数:
            key: 能力字段名。
            default: 显式默认值；为 None 时使用能力矩阵默认值。
        """
        if key in self.data:
            return self.data[key]
        if default is not None:
            return default
        return _CAP_DEFAULTS.get(key)

    def supports_paralinguistic(self, tag: str) -> bool:
        """是否支持某个副语言标记。

        参数:
            tag: laugh/sigh/breath/cry/whisper。
        """
        return tag in (self.get("nonverbal") or [])

    def to_dict(self) -> dict:
        """导出完整能力矩阵（补全默认值）。"""
        merged = dict(_CAP_DEFAULTS)
        merged.update(self.data or {})
        return merged


@dataclass
class ProviderSpec:
    """provider 配置项（来自 config/config.yaml）。"""

    provider_id: str
    model: str
    backend: str = "mlx"
    model_version: str | None = None
    display_name: str | None = None
    weights_path: str | None = None
    endpoint: str | None = None
    api_key_env: str | None = None
    health_path: str | None = None
    vram_estimate_gb: float | None = None
    max_concurrent: int = 1
    supports_batch: bool = False
    enabled: bool = False
    unload: dict = field(default_factory=dict)
    capabilities: Capabilities = field(default_factory=Capabilities)
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict) -> "ProviderSpec":
        """从配置字典构造。

        参数:
            raw: 配置里的单个 provider 对象。
        """
        known = {
            "provider_id",
            "model",
            "backend",
            "model_version",
            "display_name",
            "weights_path",
            "endpoint",
            "api_key_env",
            "health_path",
            "vram_estimate_gb",
            "max_concurrent",
            "supports_batch",
            "enabled",
            "unload",
            "capabilities",
        }
        extra = {k: v for k, v in raw.items() if k not in known}
        return cls(
            provider_id=raw["provider_id"],
            model=raw["model"],
            backend=raw.get("backend") or "mlx",
            model_version=raw.get("model_version"),
            display_name=raw.get("display_name"),
            weights_path=raw.get("weights_path"),
            endpoint=raw.get("endpoint"),
            api_key_env=raw.get("api_key_env"),
            health_path=raw.get("health_path"),
            vram_estimate_gb=raw.get("vram_estimate_gb"),
            max_concurrent=int(raw.get("max_concurrent") or 1),
            supports_batch=bool(raw.get("supports_batch")),
            enabled=bool(raw.get("enabled")),
            unload=raw.get("unload") or {},
            capabilities=Capabilities(raw.get("capabilities") or {}),
            extra=extra,
        )


@dataclass
class VoiceRef:
    """音色变体的参考素材（provider 中立）。"""

    variant_id: str | None = None
    ref_audio: str | None = None
    ref_text: str | None = None
    instruct: str | None = None
    emotion: str | None = None
    # 情绪参考音频（IndexTTS 的 emo_audio_prompt 通路），与音色参考音频独立
    emotion_audio: str | None = None
    source: str = "preset"


@dataclass
class Requirement:
    """自动路由的输入：这一句需要什么。"""

    text: str
    emotion: str | None = None
    emotion_intensity: int = 0  # 0~3
    paralinguistic: list[str] = field(default_factory=list)
    clone_required: bool = False
    language: str = "zh-CN"
    duration_mode: str = "none"  # none|max|target
    target_duration_ms: int | None = None
    streaming_required: bool = False
    usage: str = "personal"  # commercial|personal
    character_id: str | None = None
    line_id: str | None = None

    @property
    def text_chars(self) -> int:
        """文本字符数。"""
        return len(self.text)

    def to_dict(self) -> dict:
        """用于计算 input_hash 的规范化结构。"""
        return {
            "text_chars": self.text_chars,
            "emotion": self.emotion,
            "emotion_intensity": self.emotion_intensity,
            "paralinguistic": sorted(self.paralinguistic),
            "clone_required": self.clone_required,
            "language": self.language,
            "duration_mode": self.duration_mode,
            "target_duration_ms": self.target_duration_ms,
            "streaming_required": self.streaming_required,
            "usage": self.usage,
        }


@dataclass
class SynthesisRequest:
    """一次合成请求（provider 中立）。"""

    text: str
    output_path: str
    voice: VoiceRef = field(default_factory=VoiceRef)
    emotion: str | None = None
    emotion_intensity: int = 0  # 0~3，与 Requirement 对齐；provider 用来推导情绪强度
    instruct: str | None = None
    target_duration_ms: int | None = None
    sample_rate: int | None = None
    language: str | None = None  # 来自 Requirement.language，供 provider 选语言分支


@dataclass
class SynthesisResult:
    """一次合成结果。"""

    audio_path: str
    sample_rate: int
    duration_ms: int
    provider_id: str
    model: str
    model_version: str | None
    latency_ms: int
    variant_id: str | None = None

    def to_dict(self) -> dict:
        """转成可落盘的结构。"""
        return {
            "audio_path": self.audio_path,
            "sample_rate": self.sample_rate,
            "duration_ms": self.duration_ms,
            "provider_id": self.provider_id,
            "model": self.model,
            "model_version": self.model_version,
            "variant_id": self.variant_id,
            "latency_ms": self.latency_ms,
        }


@dataclass
class HealthStatus:
    """provider 健康检查结果。"""

    provider_id: str
    ok: bool
    detail: str = ""
    code: str | None = None
    loaded: bool = False

    def to_dict(self) -> dict:
        """转成可打印结构。"""
        return {
            "provider_id": self.provider_id,
            "ok": self.ok,
            "detail": self.detail,
            "code": self.code,
            "loaded": self.loaded,
        }
