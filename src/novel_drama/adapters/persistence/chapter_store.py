"""章节仓储：对外只暴露 `Chapter` 实体。

设计取舍（阶段 2）：
- 不改变 `ProjectStore.load_chapter/save_chapter` 的 dict 契约，避免一次性改动 160+ 个调用点；
- 仓储复用 `ProjectStore` 的校验、原子写入与版本归档，保证行为与现有系统完全一致；
- 保存后把 `updated_at` 与键集合同步回实体，避免调用方拿着过期实体继续写。
"""

from __future__ import annotations

from dataclasses import dataclass

from novel_drama.adapters.persistence.project_store import ProjectStore
from novel_drama.domain.story.entities.chapter import Chapter


@dataclass
class ChapterRepository:
    """章节聚合的仓储。

    属性:
        store: 现有工程仓储（负责路径、schema 校验、原子写入与版本归档）。
    """

    store: ProjectStore

    def load(self, chapter_id: str) -> Chapter:
        """读取一章并转成实体。

        参数:
            chapter_id: 章节 ID。

        返回:
            Chapter。

        异常:
            FileNotFoundError: 章节文件不存在（与现有仓储语义一致）。
        """
        return Chapter.from_dict(self.store.load_chapter(chapter_id))

    def load_all(self) -> list[Chapter]:
        """读取全部章节，按 index 升序。

        返回:
            章节实体列表。
        """
        chapters = [self.load(path.stem) for path in (self.store.root / "chapters").glob("*.json")]
        return sorted(chapters, key=lambda item: item.index)

    def exists(self, chapter_id: str) -> bool:
        """判断章节文件是否存在。

        参数:
            chapter_id: 章节 ID。

        返回:
            存在返回 True。
        """
        return self.store.chapter_path(chapter_id).exists()

    def save(self, chapter: Chapter) -> Chapter:
        """保存章节实体。

        复用 `ProjectStore.save_chapter`：正文变化时先归档旧版本，再原子写入并做 schema 校验。
        保存后把最新的 `updated_at` 与「源文件键集合」同步回实体。

        参数:
            chapter: 章节实体。

        返回:
            同一个实体实例（已同步 updated_at / present）。

        异常:
            InvariantViolation: 实体不满足领域不变式。
            EnumValueError: 写入未定义的枚举值。
        """
        payload = chapter.to_dict()
        self.store.save_chapter(payload)
        chapter.updated_at = str(payload["updated_at"])
        chapter.present = frozenset(payload)
        return chapter
