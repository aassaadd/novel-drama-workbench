"""导入快照引用（ImportSnapshot）。

正文存 `.ndw/imports/`，章节里只留引用与哈希，用于回应「这段文字从哪来」。
快照文件是否存在、哈希是否一致属于 I/O，由应用层校验，这里只做形状规则。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from novel_drama.domain.shared.errors import require, require_len

#: 哈希形态：`sha256:` + 64 位小写十六进制
HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass
class ImportSnapshot:
    """一次导入留下的原文快照引用。

    属性:
        snapshot_id: 快照 ID。
        file: 相对工程根目录的快照路径。
        sha256: 快照内容哈希。
        chars: 字符数。
        imported_at: 导入时间。
        source_path: 原始文件路径（用户机器上的来源）。
        tool: 触发导入的命令。
    """

    snapshot_id: str
    file: str
    sha256: str
    chars: int
    imported_at: str
    source_path: str | None = None
    tool: str | None = None

    @classmethod
    def from_dict(cls, data: dict | None) -> ImportSnapshot | None:
        """从落盘字典构造。

        参数:
            data: `source` 字典；None 返回 None。

        返回:
            ImportSnapshot 或 None。
        """
        if data is None:
            return None
        snapshot = cls(
            snapshot_id=str(data.get("snapshot_id") or ""),
            file=str(data.get("file") or ""),
            sha256=str(data.get("sha256") or ""),
            chars=int(data.get("chars") or 0),
            imported_at=str(data.get("imported_at") or ""),
            source_path=data.get("source_path"),
            tool=data.get("tool"),
        )
        snapshot.validate()
        return snapshot

    def validate(self) -> None:
        """校验必填项与哈希格式。

        异常:
            InvariantViolation: 必填缺失或哈希不合法。
        """
        require(bool(self.snapshot_id), "source.snapshot_id", "快照 ID 不能为空")
        require(bool(self.file), "source.file", "快照路径不能为空")
        require(bool(HASH_RE.match(self.sha256)), "source.sha256", "快照哈希格式不合法")
        require(self.chars >= 0, "source.chars", "字符数不能为负")
        require(bool(self.imported_at), "source.imported_at", "导入时间不能为空")
        require_len(self.source_path, "source.source_path", 500)
        require_len(self.tool, "source.tool", 200)

    def to_dict(self) -> dict:
        """转回落盘字典。

        返回:
            `source` 字典。
        """
        return {
            "snapshot_id": self.snapshot_id,
            "file": self.file,
            "sha256": self.sha256,
            "chars": self.chars,
            "imported_at": self.imported_at,
            "source_path": self.source_path,
            "tool": self.tool,
        }
