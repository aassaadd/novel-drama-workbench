"""`ndw queue`：串行队列的查看、消费、重试与取消。"""

from __future__ import annotations

from novel_drama.composition import QueueStore
from novel_drama.infrastructure.config import AppConfig
from novel_drama.interface.cli.common import (
    EXIT_OK,
    PreconditionError,
    _emit,
    _resolve_store,
)
from novel_drama.interface.cli.helpers import _make_queue, _queue_service


def cmd_queue(args, config: AppConfig) -> int:
    """队列查看、消费、重试、取消。"""
    store = _resolve_store(args, config)
    queue_store = QueueStore(store)
    if args.action == "list":
        jobs = queue_store.load()
        if args.status:
            jobs = [job for job in jobs if job.get("status") == args.status]
        if getattr(args, "json", False):
            _emit(args, payload=jobs)
            return EXIT_OK
        if not jobs:
            print("队列为空")
        for job in jobs:
            error = (job.get("error") or {}).get("message", "")
            print(f"{job['id']}  {job['kind']:<8} {job['status']:<12} attempts={job['attempts']}  {error}")
        return EXIT_OK
    if args.action == "run":
        queue = _make_queue(store, config)
        service = _queue_service(queue)
        try:
            processed = queue.run(once=args.once)
        finally:
            if service is not None:
                service.close()
        print(f"已处理 {processed} 条任务")
        return EXIT_OK
    if args.action == "retry":
        target = queue_store.find(args.job)
        if target is None:
            raise PreconditionError(f"任务不存在: {args.job}")
        target["status"] = "queued"
        target["attempts"] = 0
        target["error"] = None
        queue_store.update(target)
        _emit(args, f"已重新入队 {target['id']}", target)
        return EXIT_OK
    target = queue_store.find(args.job)
    if target is None:
        raise PreconditionError(f"任务不存在: {args.job}")
    target["status"] = "canceled"
    queue_store.update(target)
    _emit(args, f"已取消 {target['id']}", target)
    return EXIT_OK


def add_parser(sub) -> None:
    """注册 `queue` 命令组。"""
    p = sub.add_parser("queue", help="串行队列")
    p_sub = p.add_subparsers(dest="action", required=True)
    q = p_sub.add_parser("list")
    q.add_argument("--status")
    q.set_defaults(func=cmd_queue)
    q = p_sub.add_parser("run")
    q.add_argument("--once", action="store_true")
    q.set_defaults(func=cmd_queue)
    q = p_sub.add_parser("retry")
    q.add_argument("--job", required=True)
    q.set_defaults(func=cmd_queue)
    q = p_sub.add_parser("cancel")
    q.add_argument("--job", required=True)
    q.set_defaults(func=cmd_queue)
