"""队列持久化：`.ndw/queue.json`，进程重启后据此恢复与标记中断任务。"""

from __future__ import annotations

import json

from ..models.ids import now_iso
from .project_store import ProjectStore


class QueueStore:
    """串行队列的落盘读写，只做数据存取，不做调度决策。"""

    def __init__(self, store: ProjectStore):
        """绑定工程。

        参数:
            store: 工程读写入口。
        """
        self.store = store

    def load(self) -> list[dict]:
        """读取全部任务（按优先级与创建时间排序）。

        返回:
            任务列表。
        """
        path = self.store.queue_file
        if not path.exists():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        jobs = data.get("jobs", [])
        return sorted(jobs, key=lambda job: (job.get("priority", 100), job.get("created_at") or ""))

    def save(self, jobs: list[dict]) -> None:
        """覆盖写入队列快照。

        参数:
            jobs: 任务列表。
        """
        self.store._write_json(self.store.queue_file, {"schema_version": 1, "jobs": jobs})

    def append(self, job: dict) -> tuple[dict, bool]:
        """追加任务；同幂等键且未失败时复用既有任务。

        参数:
            job: 待入队任务。

        返回:
            (任务, 是否新增)。已存在同键的 queued/running/succeeded 任务时返回既有任务。
        """
        jobs = self.load()
        for existing in jobs:
            if existing.get("idempotency_key") != job.get("idempotency_key"):
                continue
            if existing.get("status") in ("queued", "running", "succeeded"):
                return existing, False
        jobs.append(job)
        self.save(jobs)
        return job, True

    def update(self, job: dict) -> None:
        """按 ID 更新任务。

        参数:
            job: 已修改的任务 dict。
        """
        jobs = self.load()
        self.save([job if item.get("id") == job.get("id") else item for item in jobs])

    def find(self, job_id: str) -> dict | None:
        """按 ID 查找任务，未找到返回 None。"""
        for job in self.load():
            if job.get("id") == job_id:
                return job
        return None

    def mark_interrupted(self) -> int:
        """把上次退出时仍为 running 的任务标记为 interrupted。

        返回:
            被标记的任务数量。
        """
        jobs = self.load()
        count = 0
        for job in jobs:
            if job.get("status") == "running":
                job["status"] = "interrupted"
                job.setdefault("timings", {})["finished_at"] = now_iso()
                count += 1
        if count:
            self.save(jobs)
        return count
