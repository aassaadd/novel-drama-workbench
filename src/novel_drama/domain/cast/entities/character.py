"""角色实体（Character）。

字段与 `character.schema.json#$defs/character` 一一对应，26 个键全部建模，保证
`to_dict()` 不丢字段。非法历史枚举值由 `enum_bridge.coerce_enum` 原样保留。

本阶段只落地「实体 + 往返 + 基础不变式」，音色解析、合并等跨聚合规则在阶段 3 接入。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from novel_drama.domain.cast.value_objects.state import CharacterState
from novel_drama.domain.cast.value_objects.style import SpeechStyle
from novel_drama.domain.cast.value_objects.voice import (
    Stock,
    VoiceBinding,
    VoiceProfile,
)
from novel_drama.domain.shared.enum_bridge import coerce_enum, enum_value
from novel_drama.domain.shared.errors import require, require_len
from novel_drama.domain.shared.ids import has_prefix
from novel_drama.domain.shared.text import normalize_name
from novel_drama.domain.shared.timestamps import now_iso
from novel_drama.models.enums import AgeRange, CharacterGender, CharacterRole

#: 旁白的保留 ID
NARRATOR_ID = "chr_narrator"

#: 角色图像素材的三个固定槽位
IMAGE_SLOTS = ("fullbody", "closeup", "turnaround")

#: schema 明确允许 null 的字段；其余可选字段为空时省略键（schema 不允许 null）
NULLABLE_KEYS = frozenset({"gender", "age_range", "portrait_asset", "voice_binding", "state"})


@dataclass
class Character:
    """角色。

    属性:
        id: 角色 ID，`chr_` 开头；旁白固定 `chr_narrator`。
        name: 姓名，1~60 字，全书唯一（由 `Cast` 聚合校验）。
        role: 角色定位枚举。
        created_at: 创建时间（UTC ISO8601）。
        updated_at: 最近更新时间。
        aliases: 别名列表，单项 ≤60 字。
        gender: 性别枚举。
        age_range: 年龄段档位；旧工程的自由文本原样保留。
        personality: 性格标签，单项 ≤40 字。
        background: 背景，≤2000 字。
        speech_style: 说话方式。
        voice_binding: 音色绑定。
        voice_profile: 结构化音色描述。
        stock: 龙套槽位（仅 `role=minor`）。
        appearance_prompt / negative_prompt: 出图提示词。
        voice_description_zh/en: 中英文音色描述。
        appearance_desc_zh/en: 中英文外貌描述。
        appearance_negative_zh/en: 中英文负向说明。
        portrait_asset: 头像素材相对路径。
        image_assets: 全身/特写/三视图相对路径。
        state: 状态卡。
    """

    id: str
    name: str
    role: CharacterRole | str
    created_at: str
    updated_at: str
    aliases: list[str] = field(default_factory=list)
    gender: CharacterGender | str | None = None
    age_range: AgeRange | str | None = None
    personality: list[str] = field(default_factory=list)
    background: str = ""
    speech_style: SpeechStyle | None = None
    voice_binding: VoiceBinding | None = None
    voice_profile: VoiceProfile | None = None
    stock: Stock | None = None
    appearance_prompt: str = ""
    negative_prompt: str = ""
    voice_description_zh: str | None = None
    voice_description_en: str | None = None
    appearance_desc_zh: str | None = None
    appearance_desc_en: str | None = None
    appearance_negative_zh: str | None = None
    appearance_negative_en: str | None = None
    portrait_asset: str | None = None
    image_assets: dict[str, str | None] = field(default_factory=dict)
    state: CharacterState | None = None
    #: 载入时源文件里出现过的可选键；用于回写时保持与源文件同样的键集合
    present: frozenset[str] = field(default_factory=frozenset, repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: dict, strict: bool = False) -> Character:
        """从落盘字典构造并校验。

        参数:
            data: 单个角色字典。
            strict: 枚举非法时是否抛异常（载入历史数据时用 False）。

        返回:
            Character。
        """
        character = cls(
            id=str(data.get("id") or ""),
            name=str(data.get("name") or ""),
            role=coerce_enum(CharacterRole, data.get("role"), strict=strict, field="role"),
            created_at=str(data.get("created_at") or ""),
            updated_at=str(data.get("updated_at") or ""),
            aliases=list(data.get("aliases") or []),
            gender=coerce_enum(CharacterGender, data.get("gender"), strict=strict, field="gender"),
            age_range=coerce_enum(AgeRange, data.get("age_range"), strict=strict, field="age_range"),
            personality=list(data.get("personality") or []),
            background=str(data.get("background") or ""),
            speech_style=SpeechStyle.from_dict(data.get("speech_style"), strict=strict),
            voice_binding=VoiceBinding.from_dict(data.get("voice_binding"), strict=strict),
            voice_profile=VoiceProfile.from_dict(data.get("voice_profile"), strict=strict),
            stock=Stock.from_dict(data.get("stock"), strict=strict),
            appearance_prompt=str(data.get("appearance_prompt") or ""),
            negative_prompt=str(data.get("negative_prompt") or ""),
            voice_description_zh=data.get("voice_description_zh"),
            voice_description_en=data.get("voice_description_en"),
            appearance_desc_zh=data.get("appearance_desc_zh"),
            appearance_desc_en=data.get("appearance_desc_en"),
            appearance_negative_zh=data.get("appearance_negative_zh"),
            appearance_negative_en=data.get("appearance_negative_en"),
            portrait_asset=data.get("portrait_asset"),
            image_assets={
                slot: (data.get("image_assets") or {}).get(slot) for slot in IMAGE_SLOTS
            },
            state=CharacterState.from_dict(data.get("state"), strict=strict),
            present=frozenset(data.keys()),
        )
        character.validate(strict=strict)
        return character

    def validate(self, strict: bool = False) -> None:
        """校验角色不变式。

        参数:
            strict: 为 True 时把「非 minor 带 stock」这类历史宽松项也判为违规。

        异常:
            InvariantViolation: ID/姓名非法、旁白 ID 不符或字段超长。
        """
        require(has_prefix(self.id, "chr"), "id", f"角色 ID 必须以 chr_ 开头：{self.id}")
        require_len(self.name, "name", 60, 1)
        require(self.role is not None, "role", "必须填写角色定位")
        if enum_value(self.role) == "narrator":
            require(self.id == NARRATOR_ID, "id", "旁白角色的 ID 必须是 chr_narrator")
        if strict and self.stock is not None and enum_value(self.role) != "minor":
            require(False, "stock", "只有 role=minor 的角色才能配置龙套槽位")
        for index, alias in enumerate(self.aliases):
            require_len(alias, f"aliases[{index}]", 60)
        for index, item in enumerate(self.personality):
            require_len(item, f"personality[{index}]", 40)
        require_len(self.background, "background", 2000)
        require_len(self.appearance_prompt, "appearance_prompt", 2000)
        require_len(self.negative_prompt, "negative_prompt", 1000)
        for name, maximum in (
            ("voice_description_zh", 1000),
            ("voice_description_en", 1000),
            ("appearance_desc_zh", 1200),
            ("appearance_desc_en", 1200),
            ("appearance_negative_zh", 800),
            ("appearance_negative_en", 800),
        ):
            require_len(getattr(self, name), name, maximum)
        for slot in self.image_assets:
            require(slot in IMAGE_SLOTS, "image_assets", f"未知素材槽位：{slot}")
        if self.state is not None:
            for relation in self.state.relations:
                require(
                    relation.target_id != self.id,
                    "state.relations",
                    "关系不能指向角色自己",
                )

    def normalized_names(self) -> list[str]:
        """返回用于查重的归一化姓名与别名。

        返回:
            姓名在前、别名在后，已归一化且去重。
        """
        seen: list[str] = []
        for raw in [self.name, *self.aliases]:
            key = normalize_name(raw)
            if key and key not in seen:
                seen.append(key)
        return seen

    def rename(self, name: str) -> None:
        """改名并刷新更新时间。

        参数:
            name: 新姓名。

        异常:
            InvariantViolation: 姓名为空或超长。
        """
        require_len(name, "name", 60, 1)
        self.name = name.strip()
        self.updated_at = now_iso()

    def add_alias(self, alias: str) -> None:
        """追加别名（去重）。

        参数:
            alias: 新别名。

        异常:
            InvariantViolation: 别名超长。
        """
        text = (alias or "").strip()
        require_len(text, "aliases", 60, 1)
        if normalize_name(text) not in {normalize_name(item) for item in self.aliases}:
            self.aliases.append(text)
            self.updated_at = now_iso()

    def to_dict(self) -> dict[str, Any]:
        """转回落盘字典。

        schema 里部分可选字段不允许 null（如 `appearance_desc_en`、`stock`），因此这些字段
        为空时**省略键**；而 `gender`/`age_range`/`portrait_asset`/`voice_binding`/`state`
        显式允许 null，保持原有的 `null` 语义，避免往返时把「显式为空的绑定」变成「键不存在」。

        返回:
            角色字典。
        """
        data: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "role": enum_value(self.role),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        optional = {
            "aliases": list(self.aliases),
            "gender": enum_value(self.gender),
            "age_range": enum_value(self.age_range),
            "personality": list(self.personality),
            "background": self.background,
            "speech_style": self.speech_style.to_dict() if self.speech_style else None,
            "voice_binding": self.voice_binding.to_dict() if self.voice_binding else None,
            "voice_profile": self.voice_profile.to_dict() if self.voice_profile else None,
            "stock": self.stock.to_dict() if self.stock else None,
            "appearance_prompt": self.appearance_prompt,
            "negative_prompt": self.negative_prompt,
            "voice_description_zh": self.voice_description_zh,
            "voice_description_en": self.voice_description_en,
            "appearance_desc_zh": self.appearance_desc_zh,
            "appearance_desc_en": self.appearance_desc_en,
            "appearance_negative_zh": self.appearance_negative_zh,
            "appearance_negative_en": self.appearance_negative_en,
            "portrait_asset": self.portrait_asset,
            "image_assets": {
                slot: self.image_assets.get(slot)
                for slot in IMAGE_SLOTS
                if any(self.image_assets.get(s) for s in IMAGE_SLOTS)
            },
            "state": self.state.to_dict() if self.state else None,
        }
        for key, value in optional.items():
            empty = value is None or value == [] or value == {}
            # 源文件里本来就没有、现在也没有值：不凭空补键
            if empty and key not in self.present:
                continue
            # schema 不允许 null 的字段，即使源里显式为 null 也不能写回 null
            if value is None and key not in NULLABLE_KEYS:
                continue
            data[key] = value
        return data
