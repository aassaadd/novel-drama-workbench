"""角色侧只读查询：角色表（含图片资源字段）。

CLI 与 Web 共用同一份读模型，避免两边补字段的口径不一致。
"""

from __future__ import annotations

from ...store import ProjectStore


def character_list(store: ProjectStore) -> list[dict]:
    """角色表（JSON 里显式补齐 image_assets 字段）。

    参数:
        store: 工程。

    返回:
        角色数组（`character list --json` 的负载）。
    """
    characters = store.load_characters()["characters"]
    return [{**item, "image_assets": item.get("image_assets")} for item in characters]
