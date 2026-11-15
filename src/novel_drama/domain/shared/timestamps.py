"""时间与数量基础类型。

约定：时间一律存 UTC ISO8601 秒级字符串（`Z` 结尾），与现有落盘数据保持一致；
版本号与时长用品牌类型区分语义，运行时仍是 int。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import NewType

#: UTC 秒级 ISO8601 时间戳，如 `2026-11-15T11:36:44Z`
Timestamp = NewType("Timestamp", str)

#: 版本号，≥1，内容或结构变更时自增
Revision = NewType("Revision", int)

#: 时长（毫秒），≥0
DurationMs = NewType("DurationMs", int)


def now_iso() -> str:
    """返回当前时间的 UTC ISO8601 字符串（秒级，带 Z）。

    返回:
        形如 `2026-11-15T11:36:44Z` 的字符串。
    """
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(value: str | None) -> datetime | None:
    """解析 ISO8601 时间字符串。

    参数:
        value: 时间字符串，支持结尾 Z 与偏移量两种写法。

    返回:
        带时区的 datetime；空值或格式非法时返回 None。
    """
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
