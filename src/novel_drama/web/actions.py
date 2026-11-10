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
import threading
from typing import NamedTuple

#: CLI 执行串行锁。CLI 用全局 stdout/stderr 重定向捕获输出，
#: FastAPI 同步接口跑在线程池里，并发调用会互相抢缓冲导致输出丢失（表现为空负载）。
_RUN_LOCK = threading.Lock()

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
    from ..cli import main

    out, err = io.StringIO(), io.StringIO()
    with _RUN_LOCK:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main([str(item) for item in argv if item is not None])
    text = out.getvalue().strip()
    payload = None
    if text.startswith(("{", "[")):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None
    return CliResult(code=code, payload=payload, text=text, stderr=err.getvalue().strip())


def fail_body(result: CliResult) -> dict:
    """把失败结果整理成前端统一的错误体。

    参数:
        result: CLI 结果。

    返回:
        `{"code": 退出码, "message": 中文说明}`。
    """
    return {"code": result.code, "message": result.message}
