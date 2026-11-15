"""场景切分与重切影响面：页面与 CLI 共用同一份实现。"""

from __future__ import annotations

from ...adapters.persistence import ProjectStore
from ...orchestration.pipeline import split_chapter_scenes
from ...orchestration.resplit import analyze_scene_split


def split_scenes(
    store: ProjectStore,
    chapter_id: str,
    max_chars: int = 1200,
    force: bool = False,
    dry_run: bool = False,
    keep_lines: bool = False,
) -> list[dict]:
    """把章节正文拆成场景。

    参数:
        store: 工程。
        chapter_id: 章节 ID。
        max_chars: 单场景软上限。
        force: 已有场景时是否覆盖。
        dry_run: 只返回草案，不写盘。
        keep_lines: 保留模式；正文未变化的场景沿用原场景与台词。

    返回:
        场景草案列表或写入后的场景列表。

    异常:
        FileNotFoundError: 章节不存在。
        ValueError: 已有场景且未指定 force。
    """
    chapter = store.load_chapter(chapter_id)
    return split_chapter_scenes(
        store,
        chapter,
        max_chars=max_chars,
        force=force,
        dry_run=dry_run,
        keep_lines=keep_lines,
    )


def scene_split_impact(
    store: ProjectStore,
    chapter_id: str,
    max_chars: int = 1200,
    keep_lines: bool = False,
) -> dict:
    """计算重切场景的影响清单（不写盘）。

    参数:
        store: 工程。
        chapter_id: 章节 ID。
        max_chars: 单场景软上限。
        keep_lines: 保留模式。

    返回:
        `analyze_scene_split` 的报告（含 `text` 与影响明细）。

    异常:
        FileNotFoundError: 章节不存在。
    """
    chapter = store.load_chapter(chapter_id)
    characters = store.load_characters()["characters"]
    return analyze_scene_split(
        store, chapter, characters, max_chars=max_chars, keep_lines=keep_lines
    )
