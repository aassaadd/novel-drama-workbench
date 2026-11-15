"""领域异常与不变式断言。

约定：
- 领域层只抛 `DomainError` 子类，不抛 ValueError/KeyError 这类无上下文的内置异常；
- 异常消息必须带字段名与中文说明，接口层可直接展示给用户；
- 异常不携带堆栈细节，排查信息由调用方记日志。
"""

from __future__ import annotations


class DomainError(Exception):
    """领域异常基类：所有可预期的业务拒绝都从这里派生。"""


class PreconditionError(DomainError):
    """前置条件不满足（未建工程、未拆场景、未绑音色等），接口层映射为退出码 4。"""


class TaskFailedError(DomainError):
    """任务校验失败（章节绑定不完整、引用的条目不存在等），接口层映射为退出码 5。"""


class InvariantViolation(DomainError):
    """不变式被违反：数据或操作不符合该实体/值对象的既定规则。

    属性:
        field: 出问题的字段名，未知时用对象名。
        message: 中文说明，可直接展示。
    """

    def __init__(self, field: str, message: str):
        """构造异常。

        参数:
            field: 字段名或对象名。
            message: 中文说明。
        """
        self.field = str(field or "")
        self.message = str(message or "")
        super().__init__(f"{self.field}: {self.message}" if self.field else self.message)


def require(condition: bool, field: str, message: str) -> None:
    """断言一个不变式。

    参数:
        condition: 必须为真的条件。
        field: 出问题时提示的字段名。
        message: 中文说明。

    返回:
        无。

    异常:
        InvariantViolation: 条件为假。
    """
    if not condition:
        raise InvariantViolation(field, message)


def require_len(value: str | None, field: str, maximum: int, minimum: int = 0) -> None:
    """断言字符串长度在允许区间内（None 视为长度 0）。

    参数:
        value: 待检查文本。
        field: 字段名。
        maximum: 允许的最大字符数。
        minimum: 允许的最小字符数，默认 0。

    返回:
        无。

    异常:
        InvariantViolation: 长度越界，消息里给出实际长度与区间。
    """
    length = len(value or "")
    if length < minimum or length > maximum:
        raise InvariantViolation(field, f"长度 {length} 超出允许范围 {minimum}~{maximum}")
