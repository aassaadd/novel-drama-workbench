"""操作日志核心：记录写入、脱敏、按天分卷与查询（给用户自查问题用）。

设计要点：
- 记录两类：`op`（有副作用的操作）与 `llm`（每次大模型调用）；
- 落盘为 JSONL，按天滚动，单文件超限再分卷；总量超限删除最旧分卷；
- 写入走后台线程 + 跨进程文件锁，写日志失败绝不影响业务请求；
- 所有记录与文本统一脱敏（api_key / token / bearer 等），密钥不落盘；
- 查询按「工程 / 类型 / 时间范围 / 关键字 / trace」过滤，详情按文件+行号回读。

LLM 调用的采集与钩子见同包的 `model_call_recorder`；本模块不依赖 FastAPI，
Web 路由见 `web/logs_api.py`。
"""

from __future__ import annotations

import atexit
import contextlib
import contextvars
import fcntl
import json
import os
import queue
import re
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from ...infrastructure.config import REPO_ROOT

#: 记录类型：操作 / LLM 调用
KIND_OP = "op"
KIND_LLM = "llm"

#: 默认日志目录（相对仓库根）；可在配置里用 `logs.dir` 覆盖
_DEFAULT_DIR = REPO_ROOT / "var" / "logs"
#: 单个日志文件上限（MB）与全部日志总量上限（MB）
_DEFAULT_MAX_FILE_MB = 32
_DEFAULT_MAX_TOTAL_MB = 512
#: 单条记录上限（MB）：超出后压缩长文本字段并标记 truncated，保证不撑爆单文件
_DEFAULT_MAX_RECORD_MB = 4
#: 列表检索时单条记录比较关键字的 JSON 文本上限（字符）
_MAX_SEARCH_CHARS = 2_000_000
#: 写入队列上限；写日志不能阻塞业务，队列满时直接丢弃
_QUEUE_MAX = 5_000

#: 敏感字段名关键字（小写包含匹配）
_SENSITIVE_KEYS = ("api_key", "apikey", "api-key", "token", "secret", "password", "authorization", "cookie")
#: 文本里常见的密钥形态
_BEARER_RE = re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]{4,}", re.IGNORECASE)
_SK_RE = re.compile(r"(sk-)[A-Za-z0-9_\-]{6,}")
#: `api_key=xxx` / `api-key: xxx` 这类键值文本
_KV_RE = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|secret|password)(\s*[=:]\s*)([^\s,;&\"'}]{4,})"
)

#: 当前请求上下文：trace_id / 工程路径 / 操作名 / CLI 步骤
_CTX: contextvars.ContextVar[dict] = contextvars.ContextVar("ndw_log_ctx", default={})
#: CLI 步骤收集器（同一请求内多次 CLI 调用按顺序聚合）
_STEPS: contextvars.ContextVar[list | None] = contextvars.ContextVar("ndw_log_steps", default=None)

#: 运行期配置（由 `configure` 写入）
_CFG = {
    "enabled": True,
    "dir": str(_DEFAULT_DIR),
    "max_file_bytes": _DEFAULT_MAX_FILE_MB * 1024 * 1024,
    "max_total_bytes": _DEFAULT_MAX_TOTAL_MB * 1024 * 1024,
    "max_record_bytes": _DEFAULT_MAX_RECORD_MB * 1024 * 1024,
}

#: 写队列与后台线程
_QUEUE: "queue.Queue[str]" = queue.Queue(maxsize=_QUEUE_MAX)
_WORKER: threading.Thread | None = None
_WORKER_LOCK = threading.Lock()
#: 每个日期当前写入的分卷号与已知大小
_PART_CACHE: dict[str, tuple[int, int]] = {}
_FILE_LOCK = threading.Lock()
_APPEND_COUNT = 0
#: 自上次总量检查以来已写入的字节数（按阈值触发清理，避免每次写入都扫目录）
_PENDING_BYTES = 0


# ---------------------------------------------------------------- 基础工具


def _now() -> str:
    """当前本地时间（带时区偏移的 ISO 字符串，毫秒精度）。

    返回:
        形如 `2026-11-14T16:20:31.123+08:00` 的字符串。
    """
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _new_id(prefix: str) -> str:
    """生成短 ID。

    参数:
        prefix: 前缀，如 `lg` / `tr`。

    返回:
        `前缀_12位十六进制`。
    """
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def new_trace_id() -> str:
    """生成一次操作的 trace ID（同一次操作的多条记录共用）。

    返回:
        trace ID 字符串。
    """
    return _new_id("tr")


def current_trace() -> dict:
    """读取当前上下文中的 trace 信息（子进程回落到环境变量）。

    返回:
        `{trace_id, project, action}`，缺失字段为空串。
    """
    data = dict(_CTX.get() or {})
    if not data.get("trace_id"):
        data["trace_id"] = os.environ.get("NDW_LOG_TRACE") or _new_id("tr")
    if not data.get("project"):
        data["project"] = os.environ.get("NDW_LOG_PROJECT") or ""
    if not data.get("action"):
        data["action"] = os.environ.get("NDW_LOG_ACTION") or ""
    return data


@contextlib.contextmanager
def bind_request(trace_id: str, project: str = "", action: str = "") -> Iterator[None]:
    """在请求处理期间绑定 trace / 工程 / 操作名，供 LLM 钩子与 CLI 记录复用。

    参数:
        trace_id: 本次请求的 trace ID。
        project: 工程目录（可为空）。
        action: 操作中文名（可为空）。

    返回:
        上下文管理器（退出时恢复原上下文）。
    """
    token = _CTX.set({"trace_id": trace_id, "project": project, "action": action})
    steps_token = _STEPS.set([])
    try:
        yield
    finally:
        try:
            _CTX.reset(token)
            _STEPS.reset(steps_token)
        except ValueError:  # 跨线程恢复时忽略
            pass


def _is_sensitive_key(key: str) -> bool:
    """判断字段名是否属于敏感字段。

    参数:
        key: 字段名。

    返回:
        命中敏感关键字时为 True。
    """
    lowered = str(key or "").lower()
    return any(word in lowered for word in _SENSITIVE_KEYS)


def _redact_text(text: str) -> str:
    """脱敏文本里的密钥形态。

    参数:
        text: 原始文本。

    返回:
        脱敏后的文本。
    """
    if not text:
        return text
    text = _BEARER_RE.sub(r"\1***", text)
    text = _SK_RE.sub(r"\1***", text)
    text = _KV_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}***", text)
    return text


def redact(value: Any) -> Any:
    """递归脱敏任意可 JSON 化结构。

    参数:
        value: 原始值（dict / list / str / 其它）。

    返回:
        脱敏后的新值（不修改入参）。
    """
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            if _is_sensitive_key(str(key)) and not isinstance(item, (dict, list)):
                cleaned[key] = "***" if item not in (None, "") else item
            else:
                cleaned[key] = redact(item)
        return cleaned
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _dumps(data: Any) -> str:
    """把记录序列化成单行 JSON（不可序列化对象转字符串）。

    参数:
        data: 任意数据。

    返回:
        单行 JSON 文本。
    """
    return json.dumps(data, ensure_ascii=False, default=str, separators=(",", ":"))


def _clip(text: str, limit: int) -> str:
    """截断文本并标注省略长度。

    参数:
        text: 原文。
        limit: 最大字符数。

    返回:
        截断后的文本。
    """
    text = "" if text is None else str(text)
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…（已省略 {len(text) - limit} 字）"


# ---------------------------------------------------------------- 配置与目录


def configure(config: Any = None) -> None:
    """从本机配置读取日志参数（目录、上限、开关）。

    参数:
        config: AppConfig 或 dict；为空时使用默认值。

    返回:
        无。
    """
    def _get(key: str, default: Any = None) -> Any:
        try:
            if config is None:
                return default
            return config.get(key, default) if hasattr(config, "get") else default
        except Exception:
            return default

    raw_dir = str(_get("logs.dir") or os.environ.get("NDW_LOG_DIR") or "").strip()
    log_dir = Path(raw_dir).expanduser() if raw_dir else _DEFAULT_DIR
    if not log_dir.is_absolute():
        log_dir = REPO_ROOT / log_dir
    try:
        max_file = int(float(_get("logs.max_file_mb") or _DEFAULT_MAX_FILE_MB))
    except (TypeError, ValueError):
        max_file = _DEFAULT_MAX_FILE_MB
    try:
        max_total = int(float(_get("logs.max_total_mb") or _DEFAULT_MAX_TOTAL_MB))
    except (TypeError, ValueError):
        max_total = _DEFAULT_MAX_TOTAL_MB
    try:
        max_record = int(float(_get("logs.max_record_mb") or _DEFAULT_MAX_RECORD_MB))
    except (TypeError, ValueError):
        max_record = _DEFAULT_MAX_RECORD_MB

    _CFG["enabled"] = bool(_get("logs.enabled", True))
    _CFG["dir"] = str(log_dir)
    _CFG["max_file_bytes"] = max(1, max_file) * 1024 * 1024
    _CFG["max_total_bytes"] = max(1, max_total) * 1024 * 1024
    _CFG["max_record_bytes"] = max(1, max_record) * 1024 * 1024
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        # 目录不可写时只关闭日志能力，不影响工作台本身
        _CFG["enabled"] = False


def log_dir() -> Path:
    """当前日志目录。

    返回:
        Path 对象。
    """
    return Path(_CFG["dir"])


def is_enabled() -> bool:
    """日志是否启用。

    返回:
        启用为 True。
    """
    return bool(_CFG["enabled"])


def limits() -> dict:
    """当前磁盘保护参数（供界面展示）。

    返回:
        `{max_file_bytes, max_total_bytes, max_record_bytes, dir, enabled}`。
    """
    return dict(_CFG)


# ---------------------------------------------------------------- 异步落盘


def _ensure_worker() -> None:
    """按需启动后台写线程（守护线程，进程退出前尽力排空队列）。"""
    global _WORKER
    with _WORKER_LOCK:
        if _WORKER is not None and _WORKER.is_alive():
            return
        _WORKER = threading.Thread(target=_writer_loop, name="ndw-oplog-writer", daemon=True)
        _WORKER.start()
        # CLI / 子进程往往是「跑完即退」，守护线程来不及写；退出钩子同步补写
        atexit.register(_drain)


def _drain() -> None:
    """进程退出前把队列里剩余的记录同步写完（尽力而为）。"""
    lines: list[str] = []
    while True:
        try:
            lines.append(_QUEUE.get_nowait())
        except queue.Empty:
            break
        except Exception:  # noqa: BLE001
            break
    if not lines:
        return
    try:
        _append_batch(lines)
    except Exception:  # noqa: BLE001 - 退出阶段不能抛错
        pass


def _writer_loop() -> None:
    """后台写线程：批量取出记录并追加到当天分卷。"""
    while True:
        try:
            first = _QUEUE.get(timeout=1.0)
        except queue.Empty:
            continue
        batch = [first]
        while len(batch) < 200:
            try:
                batch.append(_QUEUE.get_nowait())
            except queue.Empty:
                break
        try:
            _append_batch(batch)
        except Exception:  # noqa: BLE001 - 写日志失败绝不影响业务
            pass


def _enqueue(record: dict) -> None:
    """把记录放进写队列；队列满或未启用时直接丢弃。

    参数:
        record: 记录 dict。

    返回:
        无。
    """
    if not _CFG["enabled"]:
        return
    try:
        line = _dumps(_fit_record(record))
    except Exception:  # noqa: BLE001
        return
    _ensure_worker()
    try:
        _QUEUE.put_nowait(line)
    except queue.Full:
        pass


def flush(timeout_s: float = 2.0) -> None:
    """等待写队列排空（测试与退出前使用）。

    参数:
        timeout_s: 最长等待秒数。

    返回:
        无。
    """
    deadline = time.monotonic() + max(0.0, timeout_s)
    while time.monotonic() < deadline:
        if _QUEUE.empty():
            time.sleep(0.05)
            if _QUEUE.empty():
                return
        time.sleep(0.05)


def _fit_record(record: dict) -> dict:
    """把单条记录压到上限以内：优先压缩长文本，再丢弃流式原始事件。

    参数:
        record: 原始记录。

    返回:
        可安全落盘的记录。
    """
    budget = int(_CFG["max_record_bytes"])
    try:
        size = len(_dumps(record).encode("utf-8"))
    except Exception:  # noqa: BLE001
        return record
    if size <= budget:
        return record
    detail = record.get("detail")
    if not isinstance(detail, dict):
        return record
    detail["truncated"] = True
    # 逐步压缩：先压缩最长文本，再丢弃原始事件，最后兜底截断整段
    for limit in (200_000, 60_000, 20_000, 6_000):
        _shrink_texts(detail, limit)
        if len(_dumps(record).encode("utf-8")) <= budget:
            return record
    raw = detail.get("raw")
    if isinstance(raw, dict):
        raw.pop("events", None)
        raw["events_omitted"] = "记录过大，原始事件已丢弃"
    if len(_dumps(record).encode("utf-8")) <= budget:
        return record
    record["detail"] = {"truncated": True, "note": "记录体积超限，详情已省略"}
    return record


def _shrink_texts(value: Any, limit: int) -> None:
    """递归把超长字符串截断到 limit 字符（原地修改）。

    参数:
        value: dict / list / str。
        limit: 最大字符数。

    返回:
        无。
    """
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, str):
                value[key] = _clip(item, limit)
            else:
                _shrink_texts(item, limit)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            if isinstance(item, str):
                value[index] = _clip(item, limit)
            else:
                _shrink_texts(item, limit)


def _day_of(ts: str) -> str:
    """从记录时间戳取日期部分。

    参数:
        ts: ISO 时间字符串。

    返回:
        `YYYY-MM-DD`；解析失败时用今天。
    """
    try:
        return str(ts)[:10]
    except Exception:  # noqa: BLE001
        return datetime.now().strftime("%Y-%m-%d")


def _file_key(path: Path) -> tuple[str, int]:
    """分卷排序键：`(日期, 分卷号)`；旧命名（YYYY-MM-DD.jsonl）视为第 0 卷。

    参数:
        path: 分卷路径。

    返回:
        可比较的排序键（升序 = 旧 → 新）。
    """
    stem = path.name[:-6] if path.name.endswith(".jsonl") else path.name
    day, _, suffix = stem.partition(".")
    return (day, int(suffix) if suffix.isdigit() else 0)


def _files() -> list[Path]:
    """列出所有日志分卷（按「日期 + 分卷号」升序，即旧→新）。

    返回:
        Path 列表。
    """
    try:
        return sorted(log_dir().glob("*.jsonl"), key=_file_key)
    except OSError:
        return []


def _candidate_path(day: str) -> Path:
    """取当天应写入的分卷路径（超过单文件上限时自动换下一卷）。

    参数:
        day: `YYYY-MM-DD`。

    返回:
        分卷路径。
    """
    folder = log_dir()
    part, size = _PART_CACHE.get(day, (0, 0))
    while True:
        name = f"{day}.jsonl" if part == 0 else f"{day}.{part:03d}.jsonl"
        path = folder / name
        if not path.exists():
            _PART_CACHE[day] = (part, 0)
            return path
        if size == 0:
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
        if size < min(int(_CFG["max_file_bytes"]), int(_CFG["max_total_bytes"])):
            _PART_CACHE[day] = (part, size)
            return path
        part += 1
        size = 0


def _append_batch(lines: list[str]) -> None:
    """把一批记录追加到对应日期的分卷（跨进程文件锁保护）。

    参数:
        lines: 已序列化的单行 JSON 文本。

    返回:
        无。
    """
    global _APPEND_COUNT, _PENDING_BYTES
    if not lines:
        return
    grouped: dict[str, list[str]] = {}
    for line in lines:
        try:
            day = _day_of(json.loads(line).get("ts") or "")
        except Exception:  # noqa: BLE001
            day = datetime.now().strftime("%Y-%m-%d")
        grouped.setdefault(day, []).append(line)
    log_dir().mkdir(parents=True, exist_ok=True)
    written = 0
    for day, group in grouped.items():
        path = _candidate_path(day)
        payload = "\n".join(group) + "\n"
        written += len(payload.encode("utf-8"))
        with _FILE_LOCK:
            with open(path, "a", encoding="utf-8") as handle:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                    handle.write(payload)
                    handle.flush()
                finally:
                    with contextlib.suppress(OSError):
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            part = _PART_CACHE.get(day, (0, 0))[0]
            _PART_CACHE[day] = (part, size)
        _APPEND_COUNT += 1
    _PENDING_BYTES += written
    # 每写满总量 10% 或每 20 批检查一次，避免每次写入都扫目录
    if _PENDING_BYTES >= max(1024 * 1024, int(_CFG["max_total_bytes"]) // 10) or _APPEND_COUNT % 20 == 0:
        _PENDING_BYTES = 0
        _enforce_total()


def _enforce_total() -> None:
    """总量超限时从最旧分卷开始删除，直到回到上限以内（按修改时间保留最新一卷）。"""
    limit = int(_CFG["max_total_bytes"])
    rows: list[tuple[float, Path, int]] = []
    for path in _files():
        try:
            info = path.stat()
            rows.append((info.st_mtime, path, info.st_size))
        except OSError:
            continue
    if not rows:
        return
    total = sum(size for _, _, size in rows)
    # 按修改时间从旧到新删；永不删除正在写入的最新分卷
    rows.sort(key=lambda item: item[0])
    for _, path, size in rows[:-1]:
        if total <= limit:
            break
        with contextlib.suppress(OSError):
            path.unlink()
            total -= size


# ---------------------------------------------------------------- 记录构造


def record_op(
    action: str,
    label: str,
    target: str = "",
    summary: str = "",
    status: str = "ok",
    duration_ms: int = 0,
    project: str = "",
    error: str = "",
    detail: dict | None = None,
    trace_id: str = "",
) -> None:
    """记录一条有副作用的操作日志。

    参数:
        action: 机器可读动作名（如 `chapter.save`）。
        label: 中文动作名（如「保存章节正文」）。
        target: 目标对象（章节 / 角色 / 音色等）。
        summary: 关键参数摘要。
        status: `ok` / `error` / `partial`。
        duration_ms: 耗时毫秒。
        project: 工程目录。
        error: 失败原因。
        detail: 结构化详情（请求参数 / CLI / 中间步骤等）。
        trace_id: 关联 trace；为空时取当前上下文。

    返回:
        无。
    """
    ctx = current_trace()
    record = {
        "id": _new_id("lg"),
        "ts": _now(),
        "kind": KIND_OP,
        "action": action,
        "label": label,
        "trace_id": trace_id or ctx.get("trace_id") or "",
        "project": project or ctx.get("project") or "",
        "target": redact(target or ""),
        "summary": redact(summary or ""),
        "status": status,
        "duration_ms": int(duration_ms or 0),
        "error": redact(_clip(error or "", 4000)),
        "detail": redact(detail or {}),
    }
    _enqueue(record)


def collect_steps() -> list:
    """取出当前请求已收集的中间步骤（不清理，由调用方决定何时重置）。

    返回:
        步骤列表副本。
    """
    steps = _STEPS.get()
    return list(steps or [])


def _clean_argv(argv: list[str]) -> list[str]:
    """脱敏命令行参数：敏感开关的后续值替换成 `***`。

    参数:
        argv: 参数列表。

    返回:
        脱敏后的参数列表。
    """
    cleaned: list[str] = []
    mask_next = False
    for item in argv:
        text = str(item)
        if mask_next:
            cleaned.append("***")
            mask_next = False
            continue
        if text.startswith("--"):
            name = text[2:].split("=", 1)[0]
            if _is_sensitive_key(name):
                if "=" in text:
                    cleaned.append(f"--{name}=***")
                else:
                    cleaned.append(text)
                    mask_next = True
                continue
        cleaned.append(_redact_text(text))
    return cleaned


def record_cli(
    argv: list[str],
    code: int,
    duration_ms: int,
    stderr: str = "",
    stdout: str = "",
) -> None:
    """记录一次 CLI 命令调用（实际参数、退出码、stderr、耗时）。

    同一 HTTP 请求内调用时并入该操作记录的 `detail.cli`；否则单独记一条。

    参数:
        argv: 实际执行的参数（不含程序名）。
        code: 退出码。
        duration_ms: 耗时毫秒。
        stderr: 标准错误原文。
        stdout: 标准输出原文（只保留尾部）。

    返回:
        无。
    """
    if not _CFG["enabled"]:
        return
    entry = {
        "argv": _clean_argv(list(argv)),
        "code": int(code),
        "duration_ms": int(duration_ms),
        "stderr": _clip(stderr or "", 8000),
        "stdout_tail": _clip((stdout or "")[-2000:], 2000),
    }
    ctx = current_trace()
    steps = _STEPS.get()
    if steps is not None:
        # 请求内：并入父操作记录，避免列表里出现成对的重复条目
        if len(steps) < 200:
            steps.append({"at": _now(), "label": f"CLI 退出码 {code}", "detail": _dumps(entry)})
        return
    record_op(
        action="cli.run",
        label="CLI 命令",
        target=" ".join(str(item) for item in argv[:3]),
        summary=f"退出码 {code}",
        status="ok" if code == 0 else "error",
        duration_ms=duration_ms,
        project=ctx.get("project") or "",
        error="" if code == 0 else _clip(stderr or "", 2000),
        detail={"cli": [entry]},
    )
# ---------------------------------------------------------------- 查询与维护


def _shallow(record: dict, file_name: str, line_no: int) -> dict:
    """取列表用的浅层字段（不含大段提示词与返回文本）。

    参数:
        record: 完整记录。
        file_name: 所在分卷文件名。
        line_no: 行号（0 基）。

    返回:
        浅层记录 dict。
    """
    detail = record.get("detail") or {}
    response = detail.get("response") if isinstance(detail, dict) else None
    preview = ""
    if isinstance(response, dict):
        preview = str(response.get("text") or "")[:120]
    return {
        "id": record.get("id"),
        "ts": record.get("ts"),
        "kind": record.get("kind"),
        "action": record.get("action"),
        "label": record.get("label"),
        "trace_id": record.get("trace_id"),
        "project": record.get("project"),
        "target": record.get("target"),
        "summary": record.get("summary"),
        "status": record.get("status"),
        "duration_ms": record.get("duration_ms"),
        "reason": record.get("reason") or {},
        "error": _clip(str(record.get("error") or ""), 300),
        "preview": preview,
        "file": file_name,
        "line": line_no,
    }


def _match(
    record: dict,
    kind: str,
    project: str,
    keyword: str,
    since: str,
    until: str,
    trace: str,
) -> bool:
    """判断记录是否命中筛选条件。

    参数:
        record: 记录 dict。
        kind: `op` / `llm` / 空（全部）。
        project: 工程路径（精确匹配）。
        keyword: 关键字（大小写不敏感的包含匹配）。
        since: 起始时间（ISO 前缀）。
        until: 结束时间（ISO 前缀）。
        trace: trace ID 精确匹配。

    返回:
        命中为 True。
    """
    if kind and record.get("kind") != kind:
        return False
    if project and str(record.get("project") or "") != project:
        return False
    if trace and str(record.get("trace_id") or "") != trace:
        return False
    ts = str(record.get("ts") or "")
    if since and ts < since:
        return False
    if until and ts > until:
        return False
    if keyword:
        haystack = _dumps(record)
        if len(haystack) > _MAX_SEARCH_CHARS:
            haystack = haystack[:_MAX_SEARCH_CHARS]
        if keyword.lower() not in haystack.lower():
            return False
    return True


def query(
    kind: str = "",
    project: str = "",
    keyword: str = "",
    since: str = "",
    until: str = "",
    trace: str = "",
    limit: int = 100,
    offset: int = 0,
) -> dict:
    """按条件查询日志（最新在前）。

    参数:
        kind: `op` / `llm` / 空（全部）。
        project: 工程路径。
        keyword: 关键字。
        since: 起始时间。
        until: 结束时间。
        trace: trace ID。
        limit: 本次返回条数（1~500）。
        offset: 跳过条数。

    返回:
        `{items, matched, has_more, scanned_files}`。
    """
    limit = max(1, min(500, int(limit or 100)))
    offset = max(0, int(offset or 0))
    keyword = (keyword or "").strip()
    items: list[dict] = []
    matched = 0
    files = list(reversed(_files()))
    for path in files:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                lines = handle.readlines()
            # 单文件内也要最新在前：按行号倒序遍历，保证整体顺序是「新 → 旧」
            for line_no in range(len(lines) - 1, -1, -1):
                line = lines[line_no]
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not _match(record, kind, project, keyword, since, until, trace):
                    continue
                matched += 1
                if matched <= offset:
                    continue
                items.append(_shallow(record, path.name, line_no))
                if len(items) > limit:
                    return {
                        "items": items[:limit],
                        "matched": matched,
                        "has_more": True,
                        "scanned_files": len(files),
                    }
        except OSError:
            continue
    return {"items": items, "matched": matched, "has_more": False, "scanned_files": len(files)}


def get_record(file_name: str, line_no: int) -> dict | None:
    """按「分卷 + 行号」回读一条完整记录。

    参数:
        file_name: 分卷文件名（只允许日志目录下的 .jsonl）。
        line_no: 行号（0 基）。

    返回:
        完整记录；文件或行号不存在时为 None。
    """
    name = Path(str(file_name or "")).name
    if not name.endswith(".jsonl") or name != file_name:
        return None
    path = log_dir() / name
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for index, line in enumerate(handle):
                if index == int(line_no):
                    return json.loads(line) if line.strip() else None
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return None


def stats() -> dict:
    """统计日志目录占用与分卷列表。

    返回:
        `{dir, enabled, limits, files, total_bytes, file_count}`。
    """
    rows = []
    total = 0
    for path in _files():
        try:
            info = path.stat()
            rows.append({"name": path.name, "bytes": info.st_size, "mtime": int(info.st_mtime)})
            total += info.st_size
        except OSError:
            continue
    return {
        "dir": str(log_dir()),
        "enabled": bool(_CFG["enabled"]),
        "limits": {
            "max_file_bytes": int(_CFG["max_file_bytes"]),
            "max_total_bytes": int(_CFG["max_total_bytes"]),
            "max_record_bytes": int(_CFG["max_record_bytes"]),
        },
        "files": rows,
        "file_count": len(rows),
        "total_bytes": total,
    }


def facets(days: int = 7) -> dict:
    """取最近若干天内出现过的工程路径（供筛选下拉）。

    参数:
        days: 扫描分卷天数（按文件名日期倒序取）。

    返回:
        `{projects: [{path, name, count}]}`。
    """
    files = list(reversed(_files()))[: max(1, int(days))]
    counter: dict[str, int] = {}
    for path in files:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    project = str(record.get("project") or "")
                    if project:
                        counter[project] = counter.get(project, 0) + 1
        except OSError:
            continue
    rows = [
        {"path": key, "name": Path(key).name or key, "count": value}
        for key, value in counter.items()
    ]
    rows.sort(key=lambda item: item["count"], reverse=True)
    return {"projects": rows}


def clear_logs() -> dict:
    """清空全部日志（用户主动操作）。

    返回:
        `{removed, freed_bytes}`。
    """
    removed = 0
    freed = 0
    for path in _files():
        try:
            freed += path.stat().st_size
            path.unlink()
            removed += 1
        except OSError:
            continue
    _PART_CACHE.clear()
    return {"removed": removed, "freed_bytes": freed}
