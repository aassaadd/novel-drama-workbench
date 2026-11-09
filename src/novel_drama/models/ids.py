"""ID 与时间工具：全局唯一的实体 ID 与统一时间格式。"""

from __future__ import annotations

import secrets
import time
from datetime import datetime, timezone

# Crockford Base32，避免易混淆字符（I/L/O/U）
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _encode(value: int, length: int) -> str:
    """把整数编码为指定长度的 Base32 字符串（高位在前）。

    参数:
        value: 待编码的非负整数。
        length: 输出字符数。

    返回:
        Base32 编码字符串。
    """
    chars = []
    for _ in range(length):
        chars.append(_ALPHABET[value & 31])
        value >>= 5
    return "".join(reversed(chars))


def new_id(prefix: str) -> str:
    """生成 `<前缀>_<ULID>` 形式的实体 ID。

    参数:
        prefix: 实体前缀，如 wrk/chp/scn/chr/lin/sfx/img/vid/tml/exp/job。

    返回:
        26 位 ULID（毫秒时间戳 + 80 位随机数）拼成的 ID，可按时间大致排序。
    """
    timestamp_ms = int(time.time() * 1000)
    randomness = secrets.randbits(80)
    return f"{prefix}_{_encode(timestamp_ms, 10)}{_encode(randomness, 16)}"


def now_iso() -> str:
    """返回当前时间的 ISO 8601 UTC 字符串（秒级，带 Z）。"""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
