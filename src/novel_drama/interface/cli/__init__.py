"""CLI 接口层：命令解析与各域命令实现。

实现集中在 `main.py`（入口与参数装配）与 `commands/`（按业务域拆分）。
顶层 `novel_drama.cli` 仅保留兼容入口，供 `ndw` 与 `python -m novel_drama.cli` 使用。
"""

from __future__ import annotations

__all__: list[str] = []
