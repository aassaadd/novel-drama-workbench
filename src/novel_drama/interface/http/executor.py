"""长任务子进程执行器：传的是应用入口（模块:函数），不是 CLI 命令串。

耗时以分钟计的合成放在独立子进程里跑：既不占 HTTP 线程池、也不持有进程内锁，
因此合成期间页面其它接口照常响应；ffmpeg / 模型进程崩溃也不会拖垮服务进程。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

#: 子进程引导：从 stdin 读参数、按「模块:函数」取用例、把 JSON 结果写回 stdout。
#: 用例里的 print 统一改到 stderr，避免污染结果通道。
_BOOTSTRAP = r"""
import json
import sys
from importlib import import_module

entry = sys.argv[1]
payload = json.load(sys.stdin)
module_name, _, func_name = entry.partition(":")
real_stdout = sys.stdout
try:
    func = getattr(import_module(module_name), func_name)
    sys.stdout = sys.stderr
    result = func(**payload)
except BaseException as exc:  # noqa: BLE001 - 所有失败都要带回父进程
    real_stdout.write(json.dumps({"ok": False, "message": str(exc)}, ensure_ascii=False))
    real_stdout.flush()
    raise SystemExit(1)
real_stdout.write(json.dumps({"ok": True, "result": result}, ensure_ascii=False))
real_stdout.flush()
"""


class ChildFailed(RuntimeError):
    """子进程执行失败：消息是可直接展示给用户的中文原因。"""


def run_application(entry: str, payload: dict) -> object:
    """在独立子进程里执行一个应用用例并取回结果。

    参数:
        entry: 应用入口，形如 `novel_drama.application.production.mix:mix_chapter`。
        payload: 传给该函数的具名参数，必须可 JSON 序列化。

    返回:
        用例返回值（经 JSON 往返，只适合可序列化的数据）。

    异常:
        ChildFailed: 子进程无法启动，或用例抛出异常。
    """
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    # 子进程未必继承服务的 PYTHONPATH（例如通过控制台脚本启动），显式补上包根目录
    source_root = str(Path(__file__).resolve().parents[3])
    env["PYTHONPATH"] = os.pathsep.join(
        [source_root, env.get("PYTHONPATH", "")]
    ).strip(os.pathsep)
    # 把 trace / 工程 / 操作名透传给子进程：子进程里的模型与媒体调用归到同一次操作
    try:
        from novel_drama.composition import observability

        ctx = observability.current_trace()
        env["NDW_LOG_TRACE"] = str(ctx.get("trace_id") or "")
        if ctx.get("project"):
            env["NDW_LOG_PROJECT"] = str(ctx["project"])
        if ctx.get("action"):
            env["NDW_LOG_ACTION"] = str(ctx["action"])
        env["NDW_LOG_DIR"] = str(observability.log_dir())
    except Exception:  # noqa: BLE001 - 日志上下文缺失不影响任务执行
        pass
    try:
        completed = subprocess.run(
            [sys.executable, "-c", _BOOTSTRAP, entry],
            input=json.dumps(payload, ensure_ascii=False),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
    except OSError as exc:
        raise ChildFailed(f"无法启动生成进程：{exc}") from exc
    try:
        data = json.loads((completed.stdout or "").strip())
    except json.JSONDecodeError:
        reason = (completed.stderr or completed.stdout or "").strip()
        raise ChildFailed(
            f"生成进程异常退出（{completed.returncode}）：{reason[-800:]}"
        ) from None
    if not data.get("ok"):
        raise ChildFailed(str(data.get("message") or "生成失败"))
    return data.get("result")
