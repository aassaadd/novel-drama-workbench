"""HTTP 用例适配层的异常翻译：状态码与中文消息必须与终端一致。"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from novel_drama.composition import AdapterError, ErrorCode, SchemaValidationError
from novel_drama.domain.shared.errors import (
    PreconditionError,
    RefusedError,
    TaskFailedError,
)
from novel_drama.interface.http import use_cases


def _raise(error: Exception):
    """构造一个「立即抛出指定异常」的用例。

    参数:
        error: 待抛出的异常实例。

    返回:
        无参可调用对象。
    """

    def _action():
        raise error

    return _action


def _caught(error: Exception, *, business: bool = False) -> HTTPException:
    """执行用例并取回被翻译出来的 HTTPException。

    参数:
        error: 用例要抛出的异常。
        business: 是否按业务接口口径映射。

    返回:
        HTTPException。
    """
    with pytest.raises(HTTPException) as caught:
        use_cases.call(_raise(error), business=business)
    return caught.value


def test_success_returns_value():
    """成功时原样返回用例结果。"""
    assert use_cases.call(lambda: {"ok": True}) == {"ok": True}


def test_refused_maps_to_412():
    """拒绝执行（exit 1）映射 412，消息带可读原因码。"""
    error = _caught(RefusedError("EXISTS", "工程已存在: /tmp/a"))
    assert error.status_code == 412
    assert error.detail == {"code": 1, "message": "[EXISTS] 工程已存在: /tmp/a"}


def test_refused_business_strips_tag():
    """业务口径剥掉标签前缀，状态码仍是 412。"""
    error = _caught(RefusedError("IN_USE", "世界仍被 2 章引用"), business=True)
    assert error.status_code == 412
    assert error.detail == {"code": 1, "message": "世界仍被 2 章引用"}


def test_file_exists_maps_to_412():
    """既有的 FileExistsError 同属「拒绝执行」，与 RefusedError 同码。"""
    error = _caught(FileExistsError("工程已存在"))
    assert error.status_code == 412
    assert error.detail == {"code": 1, "message": "[EXISTS] 工程已存在"}


def test_precondition_maps_to_412():
    """前置条件缺失映射 412。"""
    error = _caught(PreconditionError("未指定工程"))
    assert error.status_code == 412
    assert error.detail == {"code": 4, "message": "[PRECONDITION] 未指定工程"}


def test_precondition_not_found_business_maps_to_404():
    """业务口径里「不存在」降为 404。"""
    error = _caught(PreconditionError("世界不存在: w_1"), business=True)
    assert error.status_code == 404
    assert error.detail == {"code": 4, "message": "世界不存在: w_1"}


def test_usage_maps_to_400():
    """参数错误映射 400，不复用 412。"""
    error = _caught(ValueError("缺少 --title"))
    assert error.status_code == 400
    assert error.detail == {"code": 2, "message": "[USAGE] 缺少 --title"}


def test_task_failed_status_follows_business_flag():
    """任务失败默认 500；业务口径按 422 返回。"""
    assert _caught(TaskFailedError("章节绑定不完整")).status_code == 500
    business = _caught(TaskFailedError("章节绑定不完整"), business=True)
    assert business.status_code == 422
    assert business.detail == {"code": 5, "message": "章节绑定不完整"}


def test_schema_error_maps_to_422():
    """schema 校验失败映射 422（先于 ValueError 分支被捕获）。"""
    error = _caught(SchemaValidationError("line.text 不能为空"))
    assert error.status_code == 422
    assert error.detail == {"code": 6, "message": "[SCHEMA] line.text 不能为空"}


def test_adapter_not_implemented_maps_to_501():
    """适配器未对接映射 501。"""
    error = _caught(AdapterError(ErrorCode.NOT_IMPLEMENTED, "未接入该模型"))
    assert error.status_code == 501
    assert error.detail == {"code": 3, "message": "[NOT_IMPLEMENTED] 未接入该模型"}


def test_adapter_failure_maps_to_500():
    """适配器其它失败按任务失败映射 500。"""
    error = _caught(AdapterError(ErrorCode.UPSTREAM_ERROR, "上游 502"))
    assert error.status_code == 500
    assert error.detail == {"code": 5, "message": "[UPSTREAM_ERROR] 上游 502"}


def test_status_table_is_complete():
    """退出码表覆盖 CLI 全部退出码，且 1/4 同为 412。"""
    assert use_cases.EXIT_TO_STATUS == {0: 200, 1: 412, 2: 400, 3: 501, 4: 412, 5: 500, 6: 422}
