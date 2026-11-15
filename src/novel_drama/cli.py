"""命令行兼容入口：实现已迁至 `novel_drama.interface.cli`。

保留本模块只为 `ndw` 入口点与 `python -m novel_drama.cli`（页面触发的长命令子进程）
继续可用，行为与拆分前完全一致。
"""

from __future__ import annotations

from novel_drama.interface.cli.main import build_parser, main

__all__ = ["build_parser", "main"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
