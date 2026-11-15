"""角色表聚合根（Cast）。

为什么整张表是一个聚合：姓名/别名唯一、关系双向一致、合并角色要迁移全部引用，这三条都是
表级规则，单看一个 `Character` 无法自洽（见 `docs/architecture-domain-model.md` §3.3.1）。
单本工程角色数量在几十到一百量级，整表加载与现有 `load_characters()` 行为一致。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from novel_drama.domain.cast.entities.character import NARRATOR_ID, Character
from novel_drama.domain.shared.errors import require
from novel_drama.domain.shared.text import normalize_name


@dataclass
class Cast:
    """一张角色表。

    属性:
        schema_version: 落盘版本，固定 1。
        characters: 角色实体列表，顺序即落盘顺序。
    """

    schema_version: int = 1
    characters: list[Character] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict, strict: bool = False) -> Cast:
        """从 `characters.json` 字典构造并校验唯一性。

        参数:
            data: 角色表字典。
            strict: 枚举非法时是否抛异常。

        返回:
            Cast。
        """
        cast = cls(
            schema_version=int(data.get("schema_version") or 1),
            characters=[Character.from_dict(item, strict=strict) for item in (data.get("characters") or [])],
        )
        cast.assert_unique_names()
        return cast

    def to_dict(self) -> dict:
        """转回落盘字典。

        返回:
            `characters.json` 形状的字典。
        """
        return {
            "schema_version": self.schema_version,
            "characters": [character.to_dict() for character in self.characters],
        }

    def get(self, character_id: str) -> Character | None:
        """按 ID 取角色。

        参数:
            character_id: 角色 ID。

        返回:
            角色；不存在返回 None。
        """
        for character in self.characters:
            if character.id == character_id:
                return character
        return None

    def find_by_name(self, name: str) -> Character | None:
        """按姓名或别名查角色（归一化后精确匹配）。

        参数:
            name: 姓名或别名。

        返回:
            命中的角色；未命中返回 None。
        """
        key = normalize_name(name)
        if not key:
            return None
        for character in self.characters:
            if key in character.normalized_names():
                return character
        return None

    def add(self, character: Character) -> None:
        """新增角色，先校验 ID 与姓名不冲突。

        参数:
            character: 待加入的角色。

        异常:
            InvariantViolation: ID 重复或姓名/别名与既有角色冲突。
        """
        require(self.get(character.id) is None, "id", f"角色 ID 已存在：{character.id}")
        clash = self._name_clash(character)
        require(clash is None, "name", f"姓名或别名与角色 {clash} 冲突")
        self.characters.append(character)

    def remove(self, character_id: str) -> Character:
        """删除角色（旁白不可删除）。

        参数:
            character_id: 角色 ID。

        返回:
            被删除的角色。

        异常:
            InvariantViolation: 目标不存在或试图删除旁白。
        """
        require(character_id != NARRATOR_ID, "id", "旁白角色不可删除")
        character = self.get(character_id)
        require(character is not None, "id", f"角色不存在：{character_id}")
        self.characters.remove(character)
        return character

    def assert_unique_names(self) -> None:
        """校验姓名与别名在全书内唯一（归一化比较）。

        异常:
            InvariantViolation: 存在冲突，消息里给出冲突双方 ID。
        """
        owners: dict[str, str] = {}
        for character in self.characters:
            for key in character.normalized_names():
                owner = owners.get(key)
                require(
                    owner is None or owner == character.id,
                    "name",
                    f"姓名或别名重复：{key} 同时属于 {owner} 与 {character.id}",
                )
                owners[key] = character.id

    def _name_clash(self, character: Character) -> str | None:
        """检查新角色与既有角色的姓名/别名冲突。

        参数:
            character: 待检查角色。

        返回:
            冲突方的角色 ID；无冲突返回 None。
        """
        mine = set(character.normalized_names())
        for other in self.characters:
            if other.id == character.id:
                continue
            if mine & set(other.normalized_names()):
                return other.id
        return None
