"""角色表仓储：对外只暴露 `Cast` 聚合。

与章节仓储一致：复用 `ProjectStore` 的校验与原子写入，本层不复制 IO。
"""

from __future__ import annotations

from dataclasses import dataclass

from novel_drama.domain.cast.entities.cast import Cast
from novel_drama.store.project_store import ProjectStore


@dataclass
class CastRepository:
    """角色表聚合的仓储。

    属性:
        store: 现有工程仓储。
    """

    store: ProjectStore

    def load(self) -> Cast:
        """读取角色表并转成聚合。

        返回:
            Cast；文件缺失时返回只含旁白的空表（与 `load_characters` 语义一致）。
        """
        return Cast.from_dict(self.store.load_characters())

    def save(self, cast: Cast) -> Cast:
        """保存角色表聚合（先校验枚举与 schema）。

        参数:
            cast: 角色表聚合。

        返回:
            同一个聚合实例。

        异常:
            InvariantViolation: 聚合不满足领域不变式（如姓名重复）。
            EnumValueError: 写入未定义的枚举值。
        """
        self.store.save_characters(cast.to_dict())
        return cast
