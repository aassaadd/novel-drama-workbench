"""章节落盘规则：版本号、字数与历史版本存档判定。

从 `store/project_store.py` 与 `writing/*` 收敛出来的三条规则：

1. 版本号：正文或结构变更后 `revision = 当前值 + 1`，当前值缺失或为 0 时按 1 计；
2. 字数：`word_count` 由正文派生（非空白字符数），不接受外部赋值；
3. 存档：只有「磁盘上已有正文」且「正文确实变了」才把旧版存进
   `.ndw/versions/<章节ID>/<旧 revision>.json`；只改标题/摘要不产生版本。

`updated_at` 不在这里判断：它由落盘层每次保存时刷新（既有语义，阶段 3 明确不改），
所以本模块不提供「内容无变化就不刷新」的分支。
"""

from __future__ import annotations

from novel_drama.domain.shared.text import count_words

__all__ = [
    "bump_revision",
    "count_words",
    "next_revision",
    "should_archive_version",
    "version_id_of",
]


def next_revision(chapter: dict) -> int:
    """算出下一个版本号。

    参数:
        chapter: 章节 dict（读取 `revision`）。

    返回:
        当前版本号 + 1；当前值缺失或为 0 时返回 1 + 1。
    """
    return int(chapter.get("revision") or 1) + 1


def bump_revision(chapter: dict) -> int:
    """就地自增章节版本号。

    参数:
        chapter: 章节 dict（会被就地修改）。

    返回:
        自增后的版本号。
    """
    chapter["revision"] = next_revision(chapter)
    return chapter["revision"]


def should_archive_version(current: dict, incoming: dict) -> bool:
    """判断保存章节时是否需要把磁盘上的旧版本存档。

    参数:
        current: 磁盘上现有的章节 dict；文件不存在时传空 dict。
        incoming: 即将写入的章节 dict。

    返回:
        True 表示需要存档：已有非空正文，且正文与写入内容不同。
        新建章节（旧正文为空）与仅改标题/摘要不存档，避免版本列表被刷屏。
    """
    if not current:
        return False
    if not str(current.get("content") or "").strip():
        return False
    return current.get("content") != incoming.get("content")


def version_id_of(snapshot: dict) -> str:
    """返回存档文件名（不含扩展名）。

    参数:
        snapshot: 待存档的章节快照。

    返回:
        版本号字符串；`revision` 缺失或为 0 时按 1 计。
    """
    return str(int(snapshot.get("revision") or 1))
