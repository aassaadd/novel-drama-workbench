"""Web 直接调应用用例时的统一异常翻译。

页面接口不再经 `actions.run` 走 CLI，但「哪类失败返回哪个状态码、消息长什么样」
必须与终端一致：本模块与 `cli.main` 的异常分支一一对应，状态码表与
`actions.EXIT_TO_STATUS` 保持同源。
"""

from __future__ import annotations

import re
from typing import Callable, TypeVar

from fastapi import HTTPException

from ..adapters.base import AdapterError, ErrorCode
from ..adapters.persistence import SchemaValidationError
from ..cli_common import PreconditionError, TaskFailedError
from ..tts.errors import TtsError

T = TypeVar("T")

#: 退出码 → HTTP 状态码（与 `web/actions.py` 的 EXIT_TO_STATUS 同源）
EXIT_TO_STATUS: dict[int, int] = {
    0: 200,
    1: 409,
    2: 400,
    3: 501,
    4: 412,
    5: 500,
    6: 422,
}


def _raise(code: int, message: str, *, business: bool = False) -> None:
    """按 CLI 退出码抛 HTTPException。

    参数:
        code: CLI 退出码。
        message: 带标签的中文说明（与 CLI stderr 同格式）。
        business: 为 True 时沿用 `web/app._business_error` 的口径：
            剥掉 `[TAG]` 前缀，前置缺失且含「不存在」返回 404，任务/schema 失败返回 422。

    异常:
        HTTPException: 始终抛出。
    """
    if business:
        message = re.sub(r"^\[[A-Z_]+\]\s*", "", message)
        if code == 4 and "不存在" in message:
            raise HTTPException(status_code=404, detail={"code": code, "message": message})
        if code in (5, 6):
            raise HTTPException(status_code=422, detail={"code": code, "message": message})
    raise HTTPException(status_code=EXIT_TO_STATUS.get(code, 500), detail={"code": code, "message": message})


def call(action: Callable[[], T], *, business: bool = False) -> T:
    """执行一个应用用例，把领域/适配器异常翻译成与 CLI 相同的状态码与消息。

    参数:
        action: 无参可调用，内部完成一次用例。
        business: 失败口径是否按业务接口映射（见 `_raise`）。

    返回:
        action 的返回值。

    异常:
        HTTPException: 参数错 400、前置缺失 412、适配器未对接 501、任务失败 500、schema 失败 422。
    """
    try:
        return action()
    except AdapterError as error:
        _raise(
            3 if error.code == ErrorCode.NOT_IMPLEMENTED else 5,
            f"[{error.code.value}] {error.message}",
            business=business,
        )
    except TtsError as error:
        message = f"[{error.code}] {error.message}"
        if error.hint:
            message += f"\n  提示：{error.hint}"
        _raise(3 if error.code == "NOT_IMPLEMENTED" else 5, message, business=business)
    except SchemaValidationError as error:
        _raise(6, f"[SCHEMA] {error}", business=business)
    except TaskFailedError as error:
        _raise(5, f"[FAILED] {error}", business=business)
    except PreconditionError as error:
        _raise(4, f"[PRECONDITION] {error}", business=business)
    except FileExistsError as error:
        _raise(1, f"[EXISTS] {error}", business=business)
    except (FileNotFoundError, KeyError) as error:
        _raise(4, f"[NOT_FOUND] {error}", business=business)
    except ValueError as error:
        _raise(2, f"[USAGE] {error}", business=business)
    raise AssertionError("unreachable")
