"""按业务域拆分的 CLI 命令实现。

每个模块对外提供 `cmd_*` 处理函数，并在阶段 5-6 完成后提供 `add_parser(sub)`，
由 `interface/cli/main.py` 统一组装命令行树。
"""

from __future__ import annotations

__all__: list[str] = []
