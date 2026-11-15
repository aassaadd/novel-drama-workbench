"""说话方式（SpeechStyle）。

字段与 `character.schema.json#$defs/speech_style` 一一对应。语速与音高只允许档位枚举，
口音与口头禅属于自由文本（schema 也这么约定）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from novel_drama.domain.shared.enum_bridge import coerce_enum, enum_value
from novel_drama.domain.shared.errors import require_len
from novel_drama.models.enums import SpeechPitch, SpeechTempo


@dataclass
class SpeechStyle:
    """角色的说话方式。

    属性:
        tempo: 语速档位；非法历史值原样保留为字符串。
        pitch: 音高档位；同上。
        accent: 口音与用词习惯，自由文本，≤40 字。
        catchphrases: 口头禅，单项 ≤60 字。
    """

    tempo: SpeechTempo | str | None = None
    pitch: SpeechPitch | str | None = None
    accent: str | None = None
    catchphrases: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict | None, strict: bool = False) -> SpeechStyle | None:
        """从落盘字典构造。

        参数:
            data: `speech_style` 字典；None 返回 None。
            strict: 枚举非法时是否抛异常。

        返回:
            SpeechStyle 或 None。
        """
        if data is None:
            return None
        style = cls(
            tempo=coerce_enum(SpeechTempo, data.get("tempo"), strict=strict, field="speech_style.tempo"),
            pitch=coerce_enum(SpeechPitch, data.get("pitch"), strict=strict, field="speech_style.pitch"),
            accent=data.get("accent"),
            catchphrases=list(data.get("catchphrases") or []),
        )
        style.validate()
        return style

    def validate(self) -> None:
        """校验长度约束。

        异常:
            InvariantViolation: 字段超长。
        """
        require_len(self.accent, "speech_style.accent", 40)
        for index, phrase in enumerate(self.catchphrases):
            require_len(phrase, f"speech_style.catchphrases[{index}]", 60)

    def to_dict(self) -> dict:
        """转回落盘字典（键序与 schema 一致）。

        返回:
            `speech_style` 字典。
        """
        return {
            "tempo": enum_value(self.tempo),
            "pitch": enum_value(self.pitch),
            "accent": self.accent,
            "catchphrases": list(self.catchphrases),
        }
