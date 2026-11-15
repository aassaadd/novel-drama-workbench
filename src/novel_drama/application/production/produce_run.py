"""全链路编排的后台进程入口：按工程与阶段区间执行一次 `produce`。

路由只负责创建任务与启动进程，编排逻辑仍由 `orchestration.produce` 承接；
这里不拼 CLI 命令串，入参是结构化字段。
"""

from __future__ import annotations

from ...composition import ProjectStore
from ...infrastructure.config import load_config
from ...orchestration.produce import STAGE_LABELS, produce


def _format_stages(stages: list[dict]) -> list[str]:
    """把阶段记录渲染成可读文本行。

    参数:
        stages: 阶段记录列表。

    返回:
        多行文本（每阶段一行）。
    """
    marks = {"succeeded": "✓", "failed": "✗", "skipped": "–", "pending": "·", "running": "…"}
    rows: list[str] = []
    for item in stages:
        mark = marks.get(item.get("status"), "?")
        label = item.get("label") or STAGE_LABELS.get(item.get("stage"), item.get("stage"))
        note = item.get("note") or item.get("error") or ""
        rows.append(f"  {mark} {label:<8} {item.get('status'):<10} {note}")
    return rows


def run_produce_entry(
    project_root: str,
    config_path: str | None = None,
    chapter_id: str = "",
    from_stage: str = "setting",
    to_stage: str = "export",
    resume: bool = True,
    force: bool = False,
    keep_going: bool = False,
) -> dict:
    """编排后台进程入口。

    参数:
        project_root: 工程目录。
        config_path: 本机配置文件路径（可空）。
        chapter_id: 只跑该章；为空时章节级阶段按全书逐章执行。
        from_stage: 起始阶段。
        to_stage: 结束阶段。
        resume: 是否跳过上次已成功的阶段。
        force: 强制重跑正文/脚本/媒体。
        keep_going: 阶段失败后继续后续阶段。

    返回:
        `{"run_id", "status", "path", "exit_code", "failed"}`；有阶段失败时退出码为 5。
    """
    config = load_config(config_path) if config_path else load_config()
    store = ProjectStore(project_root)
    result = produce(
        store,
        config,
        chapter_id,
        from_stage=from_stage,
        to_stage=to_stage,
        resume=resume,
        dry_run=False,
        force=force,
        keep_going=keep_going,
    )
    failed = [item for item in result["stages"] if item["status"] == "failed"]
    head = "全链路中断" if failed else "全链路完成"
    print(f"{head}：run_id={result['run_id']}")
    for row in _format_stages(result["stages"]):
        print(row)
    print(f"记录: {result['path']}")
    if failed:
        print("失败阶段：" + "、".join(item["stage"] for item in failed))
    return {
        "run_id": result["run_id"],
        "status": result["status"],
        "path": result["path"],
        "failed": [item["stage"] for item in failed],
        "exit_code": 5 if failed else 0,
    }
