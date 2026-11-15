"""队列与编排记录的只读查询：页面与 CLI 共用。"""

from __future__ import annotations

from ...adapters.persistence import ProjectStore
from ...composition import QueueStore
from ...orchestration.produce import latest_run, load_run


def queue_jobs(store: ProjectStore, status: str | None = None) -> list[dict]:
    """读取队列快照。

    参数:
        store: 工程。
        status: 只看指定状态（queued/running/...）；为空表示全部。

    返回:
        队列任务列表（按落盘顺序）。
    """
    jobs = QueueStore(store).load()
    if status:
        jobs = [job for job in jobs if job.get("status") == status]
    return jobs


def run_record(store: ProjectStore, run_id: str | None = None) -> dict | None:
    """读取编排记录。

    参数:
        store: 工程。
        run_id: 记录 ID；为空时取最近一次。

    返回:
        编排记录；没有任何记录时返回 None。

    异常:
        FileNotFoundError: 指定 run_id 不存在。
    """
    if run_id:
        return load_run(store, run_id)
    return latest_run(store)
