"""CLI 共享：退出码、前置条件异常、工程定位、输出与配音服务构造。"""

from __future__ import annotations

import json

from .config import AppConfig
from .store import ProjectStore

EXIT_OK = 0
EXIT_REJECTED = 1
EXIT_USAGE = 2
EXIT_NOT_IMPLEMENTED = 3
EXIT_PRECONDITION = 4
EXIT_FAILED = 5
EXIT_SCHEMA = 6


class PreconditionError(RuntimeError):
    """前置条件不满足（未建工程、未拆场景、未绑音色等）。"""


class TaskFailedError(RuntimeError):
    """任务校验失败（章节绑定不完整、引用的条目不存在等），映射退出码 5。"""


def _emit(args, text: str = "", payload=None) -> None:
    """按 `--json` 输出结构化结果或普通文本。

    参数:
        args: 已解析参数。
        text: 普通文本输出。
        payload: 结构化结果（--json 时输出）。
    """
    if getattr(args, "json", False) and payload is not None:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif text:
        print(text)


def _resolve_store(args, config: AppConfig) -> ProjectStore:
    """定位当前工程目录。

    参数:
        args: 已解析参数。
        config: 本机配置。

    返回:
        ProjectStore。

    异常:
        PreconditionError: 未指定或未初始化工程。
    """
    root = getattr(args, "project", None) or config.project_root
    if not root:
        raise PreconditionError("未指定工程：请用 --project <目录> 或设置环境变量 NDW_PROJECT")
    store = ProjectStore(root)
    if not store.exists():
        raise PreconditionError(f"工程不存在或未初始化: {store.root}（先执行 ndw init <dir> --title 书名）")
    return store


def _tts_service(args, config: AppConfig):
    """构造配音服务；有工程时带上工程根目录以便记录同角色历史。

    参数:
        args: 已解析参数（读取 --project）。
        config: 本机配置。

    返回:
        TtsService 实例。
    """
    from .tts import TtsService

    root = getattr(args, "project", None) or config.project_root
    project_root = None
    if root:
        store = ProjectStore(root)
        if store.exists():
            project_root = store.root
    return TtsService(config, project_root)
