"""LLM 取消链路。

覆盖：
1. 取消表语义：登记、重复登记返回同一令牌、取消幂等、释放后不再命中；
2. 总线在分片之间检查取消：命中后立即抛 `LlmCancelled`，不再返回完整结果；
3. 取消发生在调用开始前时，适配器完全不会被调用（不写任何产物）；
4. `POST /api/ui/llm/cancel` 幂等，未知 call_id 也不报错。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from novel_drama.adapters.llm.contracts import (
    HealthStatus,
    Message,
    TextGenRequest,
    TextGenResult,
)
from novel_drama.application.support import llm_cancel
from novel_drama.application.support.llm_bus import LlmBus, LlmCancelled


class ChunkedAdapter:
    """按分片返回文本的假适配器。

    参数:
        chunks: 依次交给 `on_delta` 的文本分片。
        cancel_at: 第几个分片前触发取消；None 表示不取消。
        call_id: 要取消的调用标识。
    """

    def __init__(self, chunks: list[str], cancel_at: int | None = None, call_id: str = ""):
        """保存分片与取消时机。"""
        self.chunks = chunks
        self.cancel_at = cancel_at
        self.call_id = call_id
        self.calls = 0

    def generate_stream(self, req: TextGenRequest, on_delta=None) -> TextGenResult:
        """逐片回调；到点后触发取消，验证下个分片被总线拦住。"""
        self.calls += 1
        delivered: list[str] = []
        for index, chunk in enumerate(self.chunks):
            if self.cancel_at is not None and index == self.cancel_at:
                llm_cancel.cancel(self.call_id)
            if on_delta is not None:
                on_delta(chunk)
            delivered.append(chunk)
        return TextGenResult(text="".join(delivered), finish_reason="stop")

    def health(self, timeout_s: float | None = None) -> HealthStatus:
        """假适配器始终可用。"""
        return HealthStatus(ok=True, detail="ok")


class NonStreamAdapter:
    """只实现 `generate` 的假适配器，用于验证调用前取消。"""

    def __init__(self) -> None:
        """记录是否被调用。"""
        self.calls = 0

    def generate(self, req: TextGenRequest) -> TextGenResult:
        """返回固定文本并计数。"""
        self.calls += 1
        return TextGenResult(text="完整结果", finish_reason="stop")

    def health(self, timeout_s: float | None = None) -> HealthStatus:
        """假适配器始终可用。"""
        return HealthStatus(ok=True, detail="ok")


def _request() -> TextGenRequest:
    """构造最小文本请求。"""
    return TextGenRequest(task="freeform", system="", messages=[Message(role="user", content="写一句")])


@pytest.fixture(autouse=True)
def _clean_tokens():
    """每个用例前后清理取消表，避免 call_id 串味。"""
    llm_cancel._tokens.clear()
    yield
    llm_cancel._tokens.clear()


def test_register_and_cancel_are_idempotent():
    """同一 call_id 重复登记返回同一令牌；重复取消保持已取消。"""
    call_id = llm_cancel.new_call_id()
    first = llm_cancel.register(call_id)
    second = llm_cancel.register(call_id)
    assert first is second

    assert llm_cancel.is_cancelled(call_id) is False
    assert llm_cancel.cancel(call_id) is True
    assert llm_cancel.cancel(call_id) is True
    assert llm_cancel.is_cancelled(call_id) is True

    llm_cancel.release(call_id)
    assert llm_cancel.cancel(call_id) is False
    assert llm_cancel.is_cancelled(call_id) is False


def test_unknown_or_empty_call_id_is_ignored():
    """未登记 / 空 call_id 的取消不会命中，也不会报错。"""
    assert llm_cancel.cancel("") is False
    assert llm_cancel.cancel("llm_not_registered") is False
    llm_cancel.ensure_active("")


def test_bus_stops_between_chunks():
    """取消后总线在下一个分片前终止，已收到的分片保留但不再返回结果。"""
    call_id = llm_cancel.new_call_id()
    adapter = ChunkedAdapter(["第一段", "第二段", "第三段"], cancel_at=1, call_id=call_id)
    bus = LlmBus(config=None, call_id=call_id)
    bus._adapter = adapter

    received: list[str] = []
    with pytest.raises(LlmCancelled):
        bus.text("character.updates", _request(), on_delta=received.append)

    assert adapter.calls == 1
    assert received == ["第一段"]


def test_cancel_before_call_skips_adapter():
    """调用开始前已取消：适配器完全不被调用，相当于没有产物写入。"""
    call_id = llm_cancel.new_call_id()
    adapter = NonStreamAdapter()
    bus = LlmBus(config=None, call_id=call_id)
    bus._adapter = adapter
    llm_cancel.cancel(call_id)

    with pytest.raises(LlmCancelled):
        bus.text("character.updates", _request())

    assert adapter.calls == 0


def test_bus_rejects_unregistered_action():
    """未登记动作直接拒绝，避免出现「能调但页面没有进度卡」的入口。"""
    from novel_drama.adapters.errors import AdapterError

    bus = LlmBus(config=None)
    with pytest.raises(AdapterError):
        bus.text("not.registered", _request())


def test_cancel_endpoint_is_idempotent(tmp_path: Path):
    """取消接口：重复调用都返回 ok，未知 call_id 只是 cancelled=False。"""
    from fastapi.testclient import TestClient

    from novel_drama.interface.http.app import create_app

    config_path = tmp_path / "offline.yaml"
    config_path.write_text("adapters:\n  writing: offline\n", encoding="utf-8")
    client = TestClient(create_app(project_root=str(tmp_path), config_path=str(config_path)))

    call_id = llm_cancel.new_call_id()
    llm_cancel.register(call_id)
    first = client.post("/api/ui/llm/cancel", json={"call_id": call_id})
    second = client.post("/api/ui/llm/cancel", json={"call_id": call_id})
    unknown = client.post("/api/ui/llm/cancel", json={"call_id": "llm_missing"})

    assert first.status_code == 200
    assert first.json() == {"ok": True, "call_id": call_id, "cancelled": True}
    assert second.json()["cancelled"] is True
    assert unknown.json() == {"ok": True, "call_id": "llm_missing", "cancelled": False}
