"""音色相关值对象：绑定（VoiceBinding）、音色描述（VoiceProfile）、龙套槽位（Stock）。

字段与 `character.schema.json` 的 `$defs/voice_binding`、`$defs/voice_profile`、
`$defs/stock` 一一对应。这里只做「形状与区间」校验；「变体是否属于该音色」「音色是否
可用」需要读音色库，属跨聚合规则，放在领域服务 `cast/rules/voice_resolution.py`（阶段 3）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from novel_drama.domain.shared.enum_bridge import coerce_enum, enum_value
from novel_drama.domain.shared.errors import require, require_len
from novel_drama.domain.shared.ids import has_prefix
from novel_drama.models.enums import (
    AgeFeel,
    CharacterGender,
    GenderSource,
    StockReusePolicy,
    VoiceProfileStatus,
)

#: 槽位稳定键：小写字母开头，只含小写字母、数字、下划线，2~40 位
SLOT_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")


@dataclass
class VoiceBinding:
    """角色与音色的绑定关系。

    属性:
        adapter: TTS 适配器名，必填。
        voice_id: v1 供应商侧音色 ID（≤120 字，非 `voc_` 形态）。
        voice_library_id: v2 音色库 ID，优先于 voice_id。
        default_variant_id: 默认变体 ID，情绪未命中映射时使用。
        emotion_variant_map: 情绪 → 变体 ID。
        fallback_voice_id: 首选不可用时的替代音色。
        provider_id: 角色级 provider 覆盖。
        fallback_provider_id: 角色级兜底 provider。
        reference_audio: 克隆参考音路径。
        seed: 合成种子。
        speed: 语速倍率，0.5~2.0。
        emotion_map: 统一情绪 → 引擎情绪标签。
    """

    adapter: str = ""
    voice_id: str | None = None
    voice_library_id: str | None = None
    default_variant_id: str | None = None
    emotion_variant_map: dict[str, str] = field(default_factory=dict)
    fallback_voice_id: str | None = None
    provider_id: str | None = None
    fallback_provider_id: str | None = None
    reference_audio: str | None = None
    seed: int | None = None
    speed: float = 1.0
    emotion_map: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict | None, strict: bool = False) -> VoiceBinding | None:
        """从落盘字典构造。

        参数:
            data: `voice_binding` 字典；None 返回 None。
            strict: 保留参数，当前无枚举字段。

        返回:
            VoiceBinding 或 None。
        """
        if data is None:
            return None
        binding = cls(
            adapter=str(data.get("adapter") or ""),
            voice_id=data.get("voice_id"),
            voice_library_id=data.get("voice_library_id"),
            default_variant_id=data.get("default_variant_id"),
            emotion_variant_map=dict(data.get("emotion_variant_map") or {}),
            fallback_voice_id=data.get("fallback_voice_id"),
            provider_id=data.get("provider_id"),
            fallback_provider_id=data.get("fallback_provider_id"),
            reference_audio=data.get("reference_audio"),
            seed=data.get("seed"),
            speed=float(data["speed"]) if data.get("speed") is not None else 1.0,
            emotion_map=dict(data.get("emotion_map") or {}),
        )
        binding.validate()
        return binding

    def validate(self) -> None:
        """校验绑定不变式。

        异常:
            InvariantViolation: 缺 adapter、既无音色 ID、速度越界、ID 形态不符或超长。
        """
        require(bool(self.adapter.strip()), "voice_binding.adapter", "必须填写 TTS 适配器")
        require_len(self.adapter, "voice_binding.adapter", 40, 1)
        require(
            bool(self.voice_id) or bool(self.voice_library_id),
            "voice_binding",
            "voice_id 与 voice_library_id 至少填写一个",
        )
        require(0.5 <= float(self.speed) <= 2.0, "voice_binding.speed", "语速倍率必须在 0.5~2.0")
        require_len(self.voice_id, "voice_binding.voice_id", 120)
        require_len(self.provider_id, "voice_binding.provider_id", 64)
        require_len(self.fallback_provider_id, "voice_binding.fallback_provider_id", 64)
        if self.voice_library_id:
            require(
                has_prefix(self.voice_library_id, "voc"),
                "voice_binding.voice_library_id",
                "必须是 voc_ 开头的音色库 ID",
            )
        if self.fallback_voice_id:
            require(
                has_prefix(self.fallback_voice_id, "voc"),
                "voice_binding.fallback_voice_id",
                "必须是 voc_ 开头的音色库 ID",
            )
        if self.default_variant_id:
            require(
                has_prefix(self.default_variant_id, "vav"),
                "voice_binding.default_variant_id",
                "必须是 vav_ 开头的变体 ID",
            )
        for emotion, variant_id in self.emotion_variant_map.items():
            require(
                has_prefix(variant_id, "vav"),
                f"voice_binding.emotion_variant_map.{emotion}",
                "必须是 vav_ 开头的变体 ID",
            )
        for emotion, tag in self.emotion_map.items():
            require_len(tag, f"voice_binding.emotion_map.{emotion}", 60)

    def to_dict(self) -> dict:
        """转回落盘字典。

        返回:
            `voice_binding` 字典。
        """
        return {
            "adapter": self.adapter,
            "voice_id": self.voice_id,
            "voice_library_id": self.voice_library_id,
            "default_variant_id": self.default_variant_id,
            "emotion_variant_map": dict(self.emotion_variant_map),
            "fallback_voice_id": self.fallback_voice_id,
            "provider_id": self.provider_id,
            "fallback_provider_id": self.fallback_provider_id,
            "reference_audio": self.reference_audio,
            "seed": self.seed,
            "speed": self.speed,
            "emotion_map": dict(self.emotion_map),
        }


@dataclass
class VoiceProfile:
    """结构化音色描述。

    schema 允许扩展字段（`additionalProperties: true`），因此未知键收进 `extra` 并在
    `to_dict()` 时原样写回，保证旧数据（R17 输出）在保存后不丢字段。
    """

    schema: str | None = None
    summary: str | None = None
    voice_summary: str | None = None
    gender: CharacterGender | str | None = None
    gender_source: GenderSource | str | None = None
    age_feel: AgeFeel | str | None = None
    age_range_text: str | None = None
    age_range: str | None = None
    pitch: str | None = None
    timbre: list[str] = field(default_factory=list)
    pace: str | None = None
    volume: str | None = None
    energy: str | None = None
    warmth: str | None = None
    articulation: str | None = None
    breathiness: float | None = None
    raspiness: float | None = None
    temperament: list[str] = field(default_factory=list)
    accent: str | None = None
    region_hint: str | None = None
    language: str | None = None
    role_distinctiveness: float | None = None
    design_prompt: str | None = None
    negative_prompt: list[str] = field(default_factory=list)
    reference_text: str | None = None
    confidence: float | None = None
    evidence: list[str] = field(default_factory=list)
    source: dict | None = None
    status: VoiceProfileStatus | str | None = None
    stale_reason: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict | None, strict: bool = False) -> VoiceProfile | None:
        """从落盘字典构造，未知键进 `extra`。

        参数:
            data: `voice_profile` 字典；None 返回 None。
            strict: 枚举非法时是否抛异常。

        返回:
            VoiceProfile 或 None。
        """
        if data is None:
            return None
        known = {
            "schema", "summary", "voice_summary", "gender", "gender_source", "age_feel",
            "age_range_text", "age_range", "pitch", "timbre", "pace", "volume", "energy",
            "warmth", "articulation", "breathiness", "raspiness", "temperament", "accent",
            "region_hint", "language", "role_distinctiveness", "design_prompt",
            "negative_prompt", "reference_text", "confidence", "evidence", "source",
            "status", "stale_reason",
        }
        profile = cls(
            schema=data.get("schema"),
            summary=data.get("summary"),
            voice_summary=data.get("voice_summary"),
            gender=coerce_enum(CharacterGender, data.get("gender"), strict=strict, field="voice_profile.gender"),
            gender_source=coerce_enum(GenderSource, data.get("gender_source"), strict=strict, field="voice_profile.gender_source"),
            age_feel=coerce_enum(AgeFeel, data.get("age_feel"), strict=strict, field="voice_profile.age_feel"),
            age_range_text=data.get("age_range_text"),
            age_range=data.get("age_range"),
            pitch=data.get("pitch"),
            timbre=list(data.get("timbre") or []),
            pace=data.get("pace"),
            volume=data.get("volume"),
            energy=data.get("energy"),
            warmth=data.get("warmth"),
            articulation=data.get("articulation"),
            breathiness=data.get("breathiness"),
            raspiness=data.get("raspiness"),
            temperament=list(data.get("temperament") or []),
            accent=data.get("accent"),
            region_hint=data.get("region_hint"),
            language=data.get("language"),
            role_distinctiveness=data.get("role_distinctiveness"),
            design_prompt=data.get("design_prompt"),
            negative_prompt=list(data.get("negative_prompt") or []),
            reference_text=data.get("reference_text"),
            confidence=data.get("confidence"),
            evidence=list(data.get("evidence") or []),
            source=data.get("source"),
            status=coerce_enum(VoiceProfileStatus, data.get("status"), strict=strict, field="voice_profile.status"),
            stale_reason=data.get("stale_reason"),
            extra={key: value for key, value in data.items() if key not in known},
        )
        profile.validate()
        return profile

    def validate(self) -> None:
        """校验长度与 0~1 区间。

        异常:
            InvariantViolation: 字段超长或比例越界。
        """
        limits = {
            "schema": 40, "summary": 300, "voice_summary": 300, "age_range_text": 40,
            "age_range": 40, "pitch": 20, "pace": 20, "volume": 20, "energy": 30,
            "warmth": 30, "articulation": 30, "accent": 60, "region_hint": 60,
            "language": 20, "design_prompt": 1200, "reference_text": 500, "stale_reason": 200,
        }
        for name, maximum in limits.items():
            require_len(getattr(self, name), f"voice_profile.{name}", maximum)
        for name in ("breathiness", "raspiness", "role_distinctiveness", "confidence"):
            value = getattr(self, name)
            if value is not None:
                require(
                    0.0 <= float(value) <= 1.0,
                    f"voice_profile.{name}",
                    "必须在 0~1 之间",
                )

    def to_dict(self) -> dict:
        """转回落盘字典，未知扩展字段原样写回。

        返回:
            `voice_profile` 字典。
        """
        data: dict[str, Any] = dict(self.extra)
        data.update(
            {
                "schema": self.schema,
                "summary": self.summary,
                "voice_summary": self.voice_summary,
                "gender": enum_value(self.gender),
                "gender_source": enum_value(self.gender_source),
                "age_feel": enum_value(self.age_feel),
                "age_range_text": self.age_range_text,
                "age_range": self.age_range,
                "pitch": self.pitch,
                "timbre": list(self.timbre),
                "pace": self.pace,
                "volume": self.volume,
                "energy": self.energy,
                "warmth": self.warmth,
                "articulation": self.articulation,
                "breathiness": self.breathiness,
                "raspiness": self.raspiness,
                "temperament": list(self.temperament),
                "accent": self.accent,
                "region_hint": self.region_hint,
                "language": self.language,
                "role_distinctiveness": self.role_distinctiveness,
                "design_prompt": self.design_prompt,
                "negative_prompt": list(self.negative_prompt),
                "reference_text": self.reference_text,
                "confidence": self.confidence,
                "evidence": list(self.evidence),
                "source": dict(self.source) if self.source is not None else None,
                "status": enum_value(self.status),
                "stale_reason": self.stale_reason,
            }
        )
        return data


@dataclass
class Stock:
    """龙套槽位标记：只有 `role=minor` 的类型角色使用。

    属性:
        slot: 职业槽位稳定键，落盘后不可改。
        voice_slot: 音色池槽位，多个职业槽位可共用。
        label: 展示名。
        archetype_tags: 原型标签，单项 ≤20 字。
        reuse_policy: 复用策略。
    """

    slot: str = ""
    voice_slot: str | None = None
    label: str | None = None
    archetype_tags: list[str] = field(default_factory=list)
    reuse_policy: StockReusePolicy | str | None = None

    @classmethod
    def from_dict(cls, data: dict | None, strict: bool = False) -> Stock | None:
        """从落盘字典构造。

        参数:
            data: `stock` 字典；None 返回 None。
            strict: 枚举非法时是否抛异常。

        返回:
            Stock 或 None。
        """
        if data is None:
            return None
        stock = cls(
            slot=str(data.get("slot") or ""),
            voice_slot=data.get("voice_slot"),
            label=data.get("label"),
            archetype_tags=list(data.get("archetype_tags") or []),
            reuse_policy=coerce_enum(StockReusePolicy, data.get("reuse_policy"), strict=strict, field="stock.reuse_policy"),
        )
        stock.validate()
        return stock

    def validate(self) -> None:
        """校验槽位键与长度。

        异常:
            InvariantViolation: 槽位键不合法或字段超长。
        """
        require(bool(SLOT_RE.match(self.slot)), "stock.slot", f"槽位键不合法：{self.slot}")
        if self.voice_slot:
            require(bool(SLOT_RE.match(self.voice_slot)), "stock.voice_slot", f"音色槽位键不合法：{self.voice_slot}")
        require_len(self.label, "stock.label", 40)
        for index, tag in enumerate(self.archetype_tags):
            require_len(tag, f"stock.archetype_tags[{index}]", 20)

    def to_dict(self) -> dict:
        """转回落盘字典。

        返回:
            `stock` 字典。
        """
        return {
            "slot": self.slot,
            "voice_slot": self.voice_slot,
            "label": self.label,
            "archetype_tags": list(self.archetype_tags),
            "reuse_policy": enum_value(self.reuse_policy),
        }
