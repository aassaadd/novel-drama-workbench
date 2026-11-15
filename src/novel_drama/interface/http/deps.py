"""HTTP 接口层公共依赖：按请求定位工程。"""

from __future__ import annotations

from fastapi import HTTPException

from novel_drama.composition import ProjectStore


def store_for(project_root: str | None):
    """生成按请求参数定位工程的函数（请求未带 path 时回落到服务默认工程）。

    参数:
        project_root: 服务启动时的默认工程目录。

    返回:
        `_store(path) -> ProjectStore`。
    """

    def _store(path: str | None) -> ProjectStore:
        """按请求参数定位工程。

        参数:
            path: 工程目录。

        返回:
            ProjectStore。

        异常:
            HTTPException: 未指定或未初始化。
        """
        root = path or project_root
        if not root:
            raise HTTPException(status_code=400, detail="缺少 path 参数（工程目录）")
        store = ProjectStore(root)
        if not store.exists():
            raise HTTPException(status_code=404, detail=f"工程不存在: {store.root}")
        return store

    return _store
