"""枚举桥接：在落盘值（英文稳定键）与枚举成员之间转换。

本模块**不定义枚举**。枚举的唯一来源是 `novel_drama.models.enums`（前端
`web-next/js/enums.js` 与之一一对应），这里只负责两件事：

1. 载入时把合法落盘值转成枚举成员；
2. 遇到历史遗留的非法值（例如旧工程里的 `age_range="30-40"`、`tempo="measured"`），
   **原样保留字符串**而不是丢弃或猜测改写——丢弃会让用户数据在保存时被静默改掉，
   猜测改写会把「待人工修正」变成「系统已修好」。

`strict=True` 时改为直接抛 `InvariantViolation`，供新增数据与测试使用。
"""

from __future__ import annotations

from enum import Enum
from typing import Any, TypeVar

from .errors import InvariantViolation

T = TypeVar("T", bound=Enum)


def coerce_enum(enum_cls: type[T], raw: Any, strict: bool = False, field: str = "") -> T | str | None:
    """把落盘值转成枚举成员。

    参数:
        enum_cls: 枚举类，带 `coerce` 类方法（值或中文标签）。
        raw: 原始落盘值，可为 None、枚举成员、字符串。
        strict: 为 True 时非法值抛异常；否则原样保留字符串。
        field: 严格模式下报错用的字段名。

    返回:
        枚举成员 / 原始字符串（非法值）/ None（空值）。

    异常:
        InvariantViolation: strict=True 且值非法。
    """
    if raw is None:
        return None
    if isinstance(raw, enum_cls):
        return raw
    text = str(raw).strip()
    if not text:
        return None
    # 载入只认「落盘值」本身。中文标签虽然能通过 `ZhEnum.coerce`，但那是给用户输入用的，
    # 载入时若把 `"快"` 悄悄改成 `"fast"`，会让用户在下次保存时发现数据被系统改过。
    values = enum_cls.values()  # type: ignore[attr-defined]
    if text not in values:
        if strict:
            raise InvariantViolation(field or enum_cls.__name__, f"不是合法取值：{text}")
        return text
    return enum_cls(text)


def enum_value(value: Any) -> Any:
    """把枚举成员转回落盘值；非枚举原样返回。

    参数:
        value: 枚举成员、字符串或 None。

    返回:
        落盘值（字符串）或原值。
    """
    if isinstance(value, Enum):
        return value.value
    return value


def enum_set(enum_cls: type[T], raw: Any, strict: bool = False, field: str = "") -> T | None:
    """严格版：返回枚举成员或 None，非法值按 strict 决定抛错或返回 None。

    参数:
        enum_cls: 枚举类。
        raw: 原始值。
        strict: 为 True 时非法值抛异常。
        field: 报错字段名。

    返回:
        枚举成员或 None。

    异常:
        InvariantViolation: strict=True 且值非法。
    """
    result = coerce_enum(enum_cls, raw, strict=strict, field=field)
    if isinstance(result, enum_cls):
        return result
    return None
