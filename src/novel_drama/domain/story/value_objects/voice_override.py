"""章节级音色覆盖（VoiceOverride）。

只允许旁白或龙套使用，且必须写明原因——否则后期排查「这章为什么换音色」只能靠猜。
"""

from __future__ import annotations

from dataclasses import dataclass

from novel_drama.domain.shared.errors import require, require_len
from novel_drama.domain.shared.ids import has_prefix


@dataclass
class VoiceOverride:
    """章节内对某个角色的音色覆盖。

    属性:
        reason: 覆盖原因，1~200 字，必填。
        voice_id: 覆盖到的音色库 ID。
        variant_id: 覆盖到的变体 ID。
        persona_id: 旁白人格 ID，仅旁白使用。
    """

    reason: str
    voice_id: str | None = None
    variant_id: str | None = None
    persona_id: str | None = None

    @classmethod
    def from_dict(cls, data: dict) -> VoiceOverride:
        """从落盘字典构造。

        参数:
            data: 覆盖项字典。

        返回:
            VoiceOverride。
        """
        override = cls(
            reason=str(data.get("reason") or ""),
            voice_id=data.get("voice_id"),
            variant_id=data.get("variant_id"),
            persona_id=data.get("persona_id"),
        )
        override.validate()
        return override

    def validate(self) -> None:
        """校验原因与 ID 形态。

        异常:
            InvariantViolation: 缺原因、原因超长或 ID 前缀不符。
        """
        require_len(self.reason, "voice_overrides.reason", 200, 1)
        if self.voice_id:
            require(has_prefix(self.voice_id, "voc"), "voice_overrides.voice_id", "必须是 voc_ 开头的音色 ID")
        if self.variant_id:
            require(has_prefix(self.variant_id, "vav"), "voice_overrides.variant_id", "必须是 vav_ 开头的变体 ID")
        if self.persona_id:
            require(has_prefix(self.persona_id, "npr"), "voice_overrides.persona_id", "必须是 npr_ 开头的旁白人格 ID")

    def to_dict(self) -> dict:
        """转回落盘字典。

        返回:
            覆盖项字典。
        """
        return {
            "voice_id": self.voice_id,
            "variant_id": self.variant_id,
            "persona_id": self.persona_id,
            "reason": self.reason,
        }
