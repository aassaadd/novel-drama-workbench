"""CLI 共享：退出码、前置条件异常、工程定位、输出与配音服务构造。"""

from __future__ import annotations

import json
import sys

from novel_drama.application.production.lines_source import upgrade_project
from novel_drama.composition import ProjectStore
from novel_drama.domain.shared.errors import PreconditionError, TaskFailedError
from novel_drama.infrastructure.config import AppConfig

EXIT_OK = 0
EXIT_REJECTED = 1
EXIT_USAGE = 2
EXIT_NOT_IMPLEMENTED = 3
EXIT_PRECONDITION = 4
EXIT_FAILED = 5
EXIT_SCHEMA = 6

#: 接口层仍从本模块取这两个异常，保持原有导入路径不变（定义已下沉到 domain）
__all__ = ["PreconditionError", "TaskFailedError"]


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
    """定位当前工程目录，并执行打开工程自检。

    自检动作（迁移旧内嵌场景、补台本、清点孤儿行）幂等且只写工程内文件；
    有改动或孤儿行时在 **stderr** 打一行中文提示，避免污染 `--json` 的 stdout。

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
    _report_upgrade(store)
    return store


def _report_upgrade(store: ProjectStore) -> None:
    """执行打开工程自检并把结果提示到 stderr。

    参数:
        store: 工程。

    返回:
        无。
    """
    try:
        report = upgrade_project(store)
    except Exception as error:  # 自检失败不影响主命令
        print(f"提示：打开工程自检失败（{error}），已跳过", file=sys.stderr)
        return
    if report.get("skipped"):
        return
    parts = []
    if report["migrated"]:
        parts.append(f"迁移旧场景 {report['migrated']} 章")
    if report["scripts_created"]:
        parts.append(f"补出台本 {report['scripts_created']} 章")
    if report["orphan_count"]:
        parts.append(f"孤儿台词行 {report['orphan_count']} 条（已不参与渲染）")
    if parts:
        print("提示：" + "，".join(parts), file=sys.stderr)


def cast_hint(store: ProjectStore, chapter_id: str | None = None) -> None:
    """跑一次音色/BGM 编排校验，并把问题以中文提示打到 stderr（只提示，不阻断）。

    参数:
        store: ProjectStore。
        chapter_id: 只校验该章；为空校验全剧。

    返回:
        无。
    """
    from novel_drama.application.production.lines_source import chapter_views
    from novel_drama.composition import check_cast

    try:
        result = check_cast(store, chapter_views(store, [chapter_id] if chapter_id else None))
    except Exception as exc:  # 提示性校验不能反过来中断主流程
        print(f"[提示] 音色/BGM 编排检查未完成：{exc}", file=sys.stderr)
        return
    findings = result.get("findings") or []
    if not findings:
        return
    labels = {"error": "错误", "warn": "告警"}
    print(f"[提示] 音色/BGM 编排有 {len(findings)} 项待处理（本次操作已完成，不阻断）：", file=sys.stderr)
    for item in findings[:8]:
        print(f"  - [{labels.get(item.get('level'), item.get('level'))}] {item.get('code')}: {item.get('message')}", file=sys.stderr)
    if len(findings) > 8:
        print(f"  … 其余 {len(findings) - 8} 项请执行 ndw cast check 查看", file=sys.stderr)


def _tts_service(args, config: AppConfig):
    """构造配音服务；有工程时带上工程根目录以便记录同角色历史。

    参数:
        args: 已解析参数（读取 --project）。
        config: 本机配置。

    返回:
        TtsService 实例。
    """
    from novel_drama.tts import TtsService

    root = getattr(args, "project", None) or config.project_root
    project_root = None
    if root:
        store = ProjectStore(root)
        if store.exists():
            project_root = store.root
    return TtsService(config, project_root)
