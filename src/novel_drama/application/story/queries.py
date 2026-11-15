"""故事侧只读查询：章节、设定库、大纲、世界列表、黄金三章。

CLI 与 Web 共用同一份读模型，保证终端与页面看到的字段完全一致。
"""

from __future__ import annotations

from ...store import ProjectStore
from ...store.settings_store import SettingsStore
from ...store.world_store import WorldStore
from .outline_validate import check_golden_three


def chapter_list(store: ProjectStore) -> list[dict]:
    """章节元信息列表（按 index 升序）。

    参数:
        store: 工程。

    返回:
        章节元信息数组（`chapter list --json` 的负载）。
    """
    return store.list_chapters()


def chapter_detail(store: ProjectStore, chapter_id: str) -> dict:
    """读取章节全文。

    参数:
        store: 工程。
        chapter_id: 章节 ID。

    返回:
        章节 dict（`chapter show --json` 的负载）。

    异常:
        FileNotFoundError: 章节不存在。
    """
    return store.load_chapter(chapter_id)


def settings_view(store: ProjectStore) -> dict:
    """设定库全文（缺失时按模板补齐）。

    参数:
        store: 工程。

    返回:
        设定库 dict（`setting show --json` 的负载）。
    """
    return SettingsStore(store.root).ensure()


def outline_view(store: ProjectStore) -> dict:
    """大纲视图：幕/章/节拍与设定版本号。

    参数:
        store: 工程。

    返回:
        `{"acts": [...], "settings_revision": r}`。
    """
    bible = SettingsStore(store.root).ensure()
    acts = (bible.get("outline") or {}).get("acts") or []
    return {"acts": acts, "settings_revision": bible.get("settings_revision")}


def golden_check(store: ProjectStore) -> dict:
    """黄金三章检查结果（不阻断流程）。

    参数:
        store: 工程。

    返回:
        `{"passed", "issues", "settings_revision"}`。
    """
    bible = SettingsStore(store.root).ensure()
    return {**check_golden_three(bible), "settings_revision": bible.get("settings_revision")}


def world_list(store: ProjectStore) -> dict:
    """世界条目列表。

    参数:
        store: 工程。

    返回:
        `{"worlds": [...], "count": n}`。
    """
    worlds = WorldStore(store.root).ensure().get("worlds") or []
    return {"worlds": worlds, "count": len(worlds)}
