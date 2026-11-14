"""Web 层调用 CLI 的唯一通道。

界面不重写任何业务判定：每一个读写动作都拼成一条 CLI 命令，走 `cli.main`，
因此「状态怎么算、什么时候算过期、退出码是什么」在界面与终端里天然一致。

退出码映射成 HTTP 状态码，保证「拒绝执行」与「参数错」「前置条件缺失」在界面上可区分，
不会把失败伪装成成功。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import threading
import time
from typing import NamedTuple

#: CLI 执行串行锁。CLI 用全局 stdout/stderr 重定向捕获输出，
#: FastAPI 同步接口跑在线程池里，并发调用会互相抢缓冲导致输出丢失（表现为空负载）。
#: 只用于「秒级」的读写命令；生成类长命令走子进程，见 `_LONG_COMMANDS`。
_RUN_LOCK = threading.Lock()

#: 会调用模型 / 媒体生成、耗时可能以分钟计的命令。
#: 这些命令改在独立子进程执行：既不占 `_RUN_LOCK`，也不会因为等待模型
#: 让章节、角色等页面数据接口排队（否则切视图会一直停在「正在加载」）。
_LONG_COMMANDS: set[tuple[str, str]] = {
    ("setting", "generate"),
    ("setting", "polish"),
    ("outline", "generate"),
    ("chapter", "generate"),
    ("character", "generate"),
    ("script", "suggest"),
    ("world", "generate"),
    ("sfx", "run"),
    ("image", "run"),
    ("video", "run"),
}

#: CLI 退出码 → HTTP 状态码
EXIT_TO_STATUS: dict[int, int] = {
    0: 200,
    1: 409,  # 拒绝执行且未改盘（如无参数快照、重复导入命中）
    2: 400,  # 参数错误
    3: 501,  # 适配器未对接
    4: 412,  # 前置条件缺失
    5: 500,  # 任务失败
    6: 422,  # schema 校验失败
}


class CliResult(NamedTuple):
    """一次 CLI 调用的结果。

    属性:
        code: CLI 退出码。
        payload: `--json` 输出解析后的对象（非 JSON 输出时为 None）。
        text: 标准输出原文。
        stderr: 标准错误原文（错误码与中文说明）。
    """

    code: int
    payload: object | None
    text: str
    stderr: str

    @property
    def status(self) -> int:
        """对应的 HTTP 状态码。"""
        return EXIT_TO_STATUS.get(self.code, 500)

    @property
    def message(self) -> str:
        """给界面展示的错误说明（优先 stderr）。"""
        return self.stderr or self.text or f"命令失败（退出码 {self.code}）"


def run(*argv: str) -> CliResult:
    """按 CLI 语法执行一次命令并捕获输出。

    参数:
        argv: 不含程序名的参数，如 `("--project", path, "--json", "line", "list", "--chapter", cid)`。

    返回:
        CliResult。
    """
    args = [str(item) for item in argv if item is not None]
    started = time.monotonic()
    # 生成类长命令走独立子进程：不占全局锁，页面数据请求可与生成并发
    if _is_long_command(args):
        result = _run_process(args)
    else:
        result = _run_inline(args)
    _record_cli(args, result, started)
    return result


def _run_inline(args: list[str]) -> CliResult:
    """在当前进程执行 CLI（秒级读写命令）。

    参数:
        args: CLI 参数列表。

    返回:
        CliResult。
    """
    from ..cli import main

    out, err = io.StringIO(), io.StringIO()
    with _RUN_LOCK:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = main(args)
            except SystemExit as error:
                # argparse 参数错误会直接 SystemExit；不接住会把 500 抛给页面。
                # 参数说明已写进 stderr（上面已重定向），这里只转成退出码。
                code = error.code if isinstance(error.code, int) else 2
    return _parse_result(code, out.getvalue(), err.getvalue())


def _record_cli(args: list[str], result: CliResult, started: float) -> None:
    """把一次 CLI 调用记进日志（实际参数、退出码、stderr、耗时）。

    写日志失败静默忽略，绝不影响业务请求。

    参数:
        args: CLI 参数列表。
        result: 执行结果。
        started: 开始时间（time.monotonic）。

    返回:
        无。
    """
    try:
        from .. import oplog

        oplog.record_cli(
            argv=args,
            code=result.code,
            duration_ms=int((time.monotonic() - started) * 1000),
            stderr=result.stderr,
            stdout=result.text,
        )
    except Exception:  # noqa: BLE001 - 日志异常不得冒泡到业务
        pass


def _is_long_command(args: list[str]) -> bool:
    """判断是否属于「会等模型 / 媒体生成」的长命令。

    参数:
        args: CLI 参数列表（如 `["--project", p, "--json", "setting", "generate"]`）。

    返回:
        命中 `_LONG_COMMANDS` 时为 True。
    """
    for index in range(len(args) - 1):
        if (args[index], args[index + 1]) in _LONG_COMMANDS:
            return True
    return False


def _parse_result(code: int, text: str, stderr: str) -> CliResult:
    """把退出码与输出统一整理成 `CliResult`。

    参数:
        code: 退出码。
        text: 标准输出原文。
        stderr: 标准错误原文。

    返回:
        CliResult；标准输出是 JSON 时解析为 payload。
    """
    text = text.strip()
    payload = None
    if text.startswith(("{", "[")):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None
    return CliResult(code=code, payload=payload, text=text, stderr=stderr.strip())


def _run_process(args: list[str]) -> CliResult:
    """在独立子进程执行一条 CLI 命令（长任务专用）。

    与 `run` 的区别：不触碰 `_RUN_LOCK`，因此生成期间其它接口不会被排队；
    生成进程独立持有自己的模型连接与内存，请求线程只等待结果。

    参数:
        args: CLI 参数列表。

    返回:
        CliResult；子进程无法启动时按任务失败返回中文原因。
    """
    command = [sys.executable, "-m", "novel_drama.cli", *args]
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    # 把 trace / 工程 / 操作名透传给子进程：子进程里的 LLM 调用日志归到同一次操作
    try:
        from .. import oplog

        ctx = oplog.current_trace()
        env["NDW_LOG_TRACE"] = str(ctx.get("trace_id") or "")
        if ctx.get("project"):
            env["NDW_LOG_PROJECT"] = str(ctx["project"])
        if ctx.get("action"):
            env["NDW_LOG_ACTION"] = str(ctx["action"])
        env["NDW_LOG_DIR"] = str(oplog.log_dir())
    except Exception:  # noqa: BLE001 - 日志上下文缺失不影响命令执行
        pass
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
    except OSError as exc:
        return CliResult(code=5, payload=None, text="", stderr=f"无法启动生成进程：{exc}")
    return _parse_result(completed.returncode, completed.stdout, completed.stderr)


def fail_body(result: CliResult) -> dict:
    """把失败结果整理成前端统一的错误体。

    参数:
        result: CLI 结果。

    返回:
        `{"code": 退出码, "message": 中文说明}`。
    """
    return {"code": result.code, "message": result.message}
