"""角色状态卡（CharacterState）与结构化关系（CharacterRelation）。

字段与 `character.schema.json` 的 `state`、`state.relations` 一一对应。
关系只存目标角色 ID，不持有 `Character` 对象——跨角色一致性由 `Cast` 聚合保证。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from novel_drama.domain.shared.enum_bridge import coerce_enum, enum_value
from novel_drama.domain.shared.errors import require, require_len
from novel_drama.domain.shared.ids import has_prefix
from novel_drama.models.enums import RelationSentiment


@dataclass
class CharacterRelation:
    """一条结构化关系。

    属性:
        target_id: 目标角色 ID。
        label: 关系称谓，如「师父」「旧识」。
        sentiment: 关系性质档位。
        since_chapter_id: 该关系开始的章节 ID。
        note: 备注。
    """

    target_id: str = ""
    label: str | None = None
    sentiment: RelationSentiment | str | None = None
    since_chapter_id: str | None = None
    note: str | None = None

    @classmethod
    def from_dict(cls, data: dict, strict: bool = False) -> CharacterRelation:
        """从落盘字典构造。

        参数:
            data: 单条关系字典。
            strict: 枚举非法时是否抛异常。

        返回:
            CharacterRelation。
        """
        relation = cls(
            target_id=str(data.get("target_id") or ""),
            label=data.get("label"),
            sentiment=coerce_enum(RelationSentiment, data.get("sentiment"), strict=strict, field="relations.sentiment"),
            since_chapter_id=data.get("since_chapter_id"),
            note=data.get("note"),
        )
        relation.validate()
        return relation

    def validate(self) -> None:
        """校验目标 ID 形态与长度。

        异常:
            InvariantViolation: 目标 ID 缺失/形态不符或字段超长。
        """
        require(has_prefix(self.target_id, "chr"), "relations.target_id", "必须是 chr_ 开头的角色 ID")
        if self.since_chapter_id:
            require(
                has_prefix(self.since_chapter_id, "chp"),
                "relations.since_chapter_id",
                "必须是 chp_ 开头的章节 ID",
            )
        require_len(self.label, "relations.label", 40)
        require_len(self.note, "relations.note", 500)

    def to_dict(self) -> dict:
        """转回落盘字典。

        返回:
            关系字典。
        """
        return {
            "target_id": self.target_id,
            "label": self.label,
            "sentiment": enum_value(self.sentiment),
            "since_chapter_id": self.since_chapter_id,
            "note": self.note,
        }


@dataclass
class CharacterState:
    """角色当前状态卡。

    属性:
        situation: 现状描述，≤1000 字。
        relationship_notes: 其他角色 ID → 关系备注（自由文本）。
        knowledge: 角色已知信息。
        relations: 结构化关系。
        as_of_chapter_id: 状态截止章节。
    """

    situation: str | None = None
    relationship_notes: dict[str, str] = field(default_factory=dict)
    knowledge: list[str] = field(default_factory=list)
    relations: list[CharacterRelation] = field(default_factory=list)
    as_of_chapter_id: str | None = None

    @classmethod
    def from_dict(cls, data: dict | None, strict: bool = False) -> CharacterState | None:
        """从落盘字典构造。

        参数:
            data: `state` 字典；None 返回 None。
            strict: 枚举非法时是否抛异常。

        返回:
            CharacterState 或 None。
        """
        if data is None:
            return None
        state = cls(
            situation=data.get("situation"),
            relationship_notes=dict(data.get("relationship_notes") or {}),
            knowledge=list(data.get("knowledge") or []),
            relations=[
                CharacterRelation.from_dict(item, strict=strict)
                for item in (data.get("relations") or [])
            ],
            as_of_chapter_id=data.get("as_of_chapter_id"),
        )
        state.validate()
        return state

    def validate(self) -> None:
        """校验长度与 ID 形态。

        异常:
            InvariantViolation: 字段超长或 ID 形态不符。
        """
        require_len(self.situation, "state.situation", 1000)
        for character_id, note in self.relationship_notes.items():
            require(has_prefix(character_id, "chr"), "state.relationship_notes", f"非法角色 ID：{character_id}")
            require_len(note, f"state.relationship_notes.{character_id}", 500)
        for index, item in enumerate(self.knowledge):
            require_len(item, f"state.knowledge[{index}]", 200)
        if self.as_of_chapter_id:
            require(
                has_prefix(self.as_of_chapter_id, "chp"),
                "state.as_of_chapter_id",
                "必须是 chp_ 开头的章节 ID",
            )

    def to_dict(self) -> dict:
        """转回落盘字典。

        返回:
            `state` 字典。
        """
        return {
            "situation": self.situation,
            "relationship_notes": dict(self.relationship_notes),
            "knowledge": list(self.knowledge),
            "relations": [relation.to_dict() for relation in self.relations],
            "as_of_chapter_id": self.as_of_chapter_id,
        }
