"""可观测性适配器：操作日志与 LLM 调用采集（统一对外导出）。

- `oplog`：记录写入、脱敏、按天分卷与查询；
- `model_call_recorder`：LLM 调用采集钩子、中断/来因码与最近一次调用摘要。
"""

from __future__ import annotations

from .model_call_recorder import (
    LLM_REASON_LABELS,
    clear_llm_abort_reason,
    install_llm_hooks,
    last_llm_call,
    record_budget_stats,
    record_model_call,
    set_llm_abort_reason,
    set_llm_call_reason,
)
from .oplog import (
    KIND_LLM,
    KIND_OP,
    bind_request,
    clear_logs,
    collect_steps,
    configure,
    current_trace,
    facets,
    flush,
    get_record,
    is_enabled,
    limits,
    log_dir,
    new_trace_id,
    query,
    record_cli,
    record_op,
    redact,
    stats,
)

__all__ = [
    "KIND_LLM",
    "KIND_OP",
    "LLM_REASON_LABELS",
    "bind_request",
    "clear_llm_abort_reason",
    "clear_logs",
    "collect_steps",
    "configure",
    "current_trace",
    "facets",
    "flush",
    "get_record",
    "install_llm_hooks",
    "is_enabled",
    "last_llm_call",
    "limits",
    "log_dir",
    "new_trace_id",
    "query",
    "record_budget_stats",
    "record_cli",
    "record_model_call",
    "record_op",
    "redact",
    "set_llm_abort_reason",
    "set_llm_call_reason",
    "stats",
]
