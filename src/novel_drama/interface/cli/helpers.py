"""CLI 公共装配：范围参数与配音队列用例的转发。

队列装配、任务排序与消费已下沉到 `application/production/tts_run.py`，
CLI 只保留参数注册；业务规则仍在 application，适配器实例统一经组合根取。
"""

from __future__ import annotations

from novel_drama.application.production.tts_run import (
    build_adapters_for as _build_adapters,
)
from novel_drama.application.production.tts_run import (
    drain as _drain,
)
from novel_drama.application.production.tts_run import (
    make_queue as _make_queue,
)
from novel_drama.application.production.tts_run import (
    queue_service as _queue_service,
)
from novel_drama.application.production.tts_run import (
    sort_jobs_by_group as _sort_jobs_by_group,
)
from novel_drama.tts import line_state

#: 供各命令模块按原私有名导入（队列装配已下沉 application）
__all__ = ["_add_scope_args", "_build_adapters", "_drain", "_make_queue", "_queue_service", "_sort_jobs_by_group"]


def _add_scope_args(q) -> None:
    """给 `tts plan` / `tts run` 挂上生成范围选择参数。

    参数:
        q: 子命令 parser。
    """
    q.add_argument(
        "--scope",
        choices=list(line_state.SCOPES),
        help="selected=指定行 / pending=待处理（默认）/ scene=指定场景 / chapter=整章",
    )
    q.add_argument("--line", action="append", help="选中的台词行 ID，可重复（--scope selected 必填）")
    q.add_argument("--scene", action="append", help="选中的场景 ID，可重复（--scope scene 必填）")
    q.add_argument("--status", help="逗号分隔的派生状态：pending,stale,failed,ready；与 --scope pending 互斥")
