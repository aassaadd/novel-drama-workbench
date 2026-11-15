"""正文段落块（ChapterBlock）。

块是导演脚本锚点的定位基础：锚点记录 `block_id + 字符区间 + 文本哈希`，块本身只由
`content` 派生。本阶段只做「载入/回写」，重建块的规则（含 dialogue/heading 判定）
依赖导演标注解析，随阶段 5 一起下沉，避免现在写一套与标注器不一致的切分。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from novel_drama.domain.shared.enum_bridge import coerce_enum, enum_value
from novel_drama.domain.shared.errors import require, require_len
from novel_drama.domain.shared.ids import has_prefix
from novel_drama.models.enums import BlockKind

#: 文本哈希形态：`sha256:` + 64 位小写十六进制
HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass
class ChapterBlock:
    """一个正文段落块。

    属性:
        id: 块 ID，`blk_` 开头。
        index: 块序号，从 0 开始，章节内连续。
        kind: 段落类型枚举。
        char_start: 在正文中的起始字符下标（含）。
        char_end: 结束下标（不含）。
        text_hash: 块文本的 sha256，用于锚点复核。
    """

    id: str
    index: int
    kind: BlockKind | str
    char_start: int
    char_end: int
    text_hash: str

    @classmethod
    def from_dict(cls, data: dict, strict: bool = False) -> ChapterBlock:
        """从落盘字典构造。

        参数:
            data: 块字典。
            strict: 枚举非法时是否抛异常。

        返回:
            ChapterBlock。
        """
        block = cls(
            id=str(data.get("id") or ""),
            index=int(data.get("index") or 0),
            kind=coerce_enum(BlockKind, data.get("kind"), strict=strict, field="blocks.kind"),
            char_start=int(data.get("char_start") or 0),
            char_end=int(data.get("char_end") or 0),
            text_hash=str(data.get("text_hash") or ""),
        )
        block.validate()
        return block

    def validate(self) -> None:
        """校验块不变式。

        异常:
            InvariantViolation: ID 形态错误、区间倒挂、哈希格式不符或枚举缺失。
        """
        require(has_prefix(self.id, "blk"), "blocks.id", f"块 ID 必须以 blk_ 开头：{self.id}")
        require(self.index >= 0, "blocks.index", "块序号不能为负")
        require(self.kind is not None, "blocks.kind", "块类型不能为空")
        require(self.char_start >= 0, "blocks.char_start", "起始下标不能为负")
        require(self.char_end >= self.char_start, "blocks.char_end", "结束下标不能小于起始下标")
        require(bool(HASH_RE.match(self.text_hash)), "blocks.text_hash", "文本哈希格式不合法")
        require_len(self.id, "blocks.id", 40)

    def to_dict(self) -> dict:
        """转回落盘字典。

        返回:
            块字典。
        """
        return {
            "id": self.id,
            "index": self.index,
            "kind": enum_value(self.kind),
            "char_start": self.char_start,
            "char_end": self.char_end,
            "text_hash": self.text_hash,
        }
