"""中间产物目录清理。

统一约定：清理只针对「本次已成功落盘」的中间目录；删除失败只写告警日志，
不抛异常、不阻断主流程，方便用户事后手动处理。
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)


def remove_dir_quietly(path: Path, label: str) -> bool:
    """删除一个目录；失败只告警，不影响调用方主流程。

    参数:
        path: 要删除的目录。
        label: 中文名称（写入告警日志，便于定位）。

    返回:
        True 表示目录已删除或本来不存在；False 表示删除失败。
    """
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        return True
    except OSError as exc:
        logger.warning("清理%s失败：%s（%s），可手动删除", label, path, exc)
        return False
    return True


def remove_stale_temp_dirs(parent: Path) -> int:
    """清理目录下上一次生成中断残留的 `.ndw-tmp-*` 临时目录。

    说明:
        仅在 GPU 锁内调用，此时不会有其它生成流程正在写这些目录。

    参数:
        parent: 媒体产物目录（如 `assets/images`、`assets/video`）。

    返回:
        实际清理的目录数量。
    """
    if not parent.is_dir():
        return 0
    removed = 0
    for child in sorted(parent.glob(".ndw-tmp-*")):
        if child.is_dir() and remove_dir_quietly(child, "ComfyUI 残留临时目录"):
            removed += 1
    return removed
