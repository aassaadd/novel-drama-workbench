"""媒体任务与内嵌 LLM 调用的父任务关联与级联取消。

覆盖：
1. `begin_media_job` + `register_child` + `cancel_children` 的语义（含退出清理）；
2. 取消父媒体任务后，内嵌的 LLM 调用在下一个分片检查时收到 `LlmCancelled`，
   任务落 `canceled`，`meta.llm_calls` 记录登记次数，且不写入半截结果；
3. 成功完成时同样记录 `meta.llm_calls`。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from novel_drama.adapters.llm.contracts import (
    HealthStatus,
    Message,
    TextGenRequest,
    TextGenResult,
)
from novel_drama.adapters.persistence import ProjectStore
from novel_drama.application.production import job_tracker
from novel_drama.application.support import llm_cancel, media_bus
from novel_drama.application.support.llm_bus import LlmBus, LlmCancelled

#: 轮询终态的最长等待（秒）
_WAIT_S = 8.0


class ChunkedAdapter:
    """按分片返回文本的假适配器（每个分片前留出取消检查窗口）。

    参数:
        chunks: 分片数量；每片间隔 20ms。
    """

    def __init__(self, chunks: int = 500):
        """保存分片数量并初始化计数。"""
        self.chunks = chunks
        self.sent = 0
        self.finished = False

    def generate_stream(self, req: TextGenRequest, on_delta=None) -> TextGenResult:
        """逐片回调；上游被取消时由总线在分片前抛 `LlmCancelled`。"""
        for index in range(self.chunks):
            time.sleep(0.02)
            self.sent += 1
            if on_delta is not None:
                on_delta(f"片段{index}")
        self.finished = True
        return TextGenResult(text="完整结果", finish_reason="stop")

    def health(self, timeout_s: float | None = None) -> HealthStatus:
        """假适配器始终可用。"""
        return HealthStatus(ok=True, detail="ok")


def _request() -> TextGenRequest:
    """构造最小文本请求。"""
    return TextGenRequest(task="freeform", system="", messages=[Message(role="user", content="写一句")])


@pytest.fixture
def store(tmp_path: Path) -> ProjectStore:
    """建一个临时工程并返回可复用的 ProjectStore。"""
    ProjectStore(str(tmp_path)).create("媒体 LLM 级联测试")
    return ProjectStore(str(tmp_path))


@pytest.fixture(autouse=True)
def _clean_state():
    """每个用例前后清理执行器与取消/子调用登记，避免串味。"""
    media_bus._HANDLERS.clear()
    llm_cancel._tokens.clear()
    llm_cancel._children.clear()
    llm_cancel._counters.clear()
    yield
    media_bus._HANDLERS.clear()
    llm_cancel._tokens.clear()
    llm_cancel._children.clear()
    llm_cancel._counters.clear()


def _wait_terminal(store: ProjectStore, job_id: str) -> dict:
    """轮询任务直到进入终态并返回快照。"""
    deadline = time.monotonic() + _WAIT_S
    latest: dict | None = None
    while time.monotonic() < deadline:
        latest = media_bus.snapshot(store, job_id)
        if latest and latest.get("status") in job_tracker.TERMINAL_STATUSES:
            return latest
        time.sleep(0.02)
    raise AssertionError(f"任务未在 {_WAIT_S} 秒内结束：{latest}")


def test_begin_media_job_cancels_children_and_cleans_up():
    """父任务绑定期间登记子调用；取消命中；退出后登记与父任务 ID 都清理。"""
    call_id = llm_cancel.new_call_id()
    llm_cancel.register(call_id)

    with llm_cancel.begin_media_job("job_parent") as counter:
        assert llm_cancel.current_parent_job() == "job_parent"
        llm_cancel.register_child("job_parent", call_id)
        assert counter.count == 1
        assert llm_cancel.cancel_children("job_parent") == 1
        with pytest.raises(LlmCancelled):
            llm_cancel.ensure_active(call_id)

    assert llm_cancel.current_parent_job() == ""
    assert llm_cancel.cancel_children("job_parent") == 0


def test_cancel_parent_media_job_cascades_to_llm(store: ProjectStore):
    """取消父媒体任务：LLM 在下一个分片前退出，任务 canceled 且不写半截结果。"""
    adapter = ChunkedAdapter()
    call_id = llm_cancel.new_call_id()

    def handler(handle, _store, _config, _params):
        """在媒体任务里发起一次 LLM 调用，只有完整返回才写结果。"""
        bus = LlmBus(config=None, call_id=call_id)
        bus._adapter = adapter
        handle.progress("等待模型输出", 10, force=True)
        result = bus.text("character.updates", _request())
        handle.set_meta(answer=result.text)
        return {"message": f"生成 {len(result.text)} 字"}

    media_bus.register_handler("voice.design", handler)
    job = media_bus.run(
        store,
        None,
        "voice.design",
        {"path": str(store.root), "character_id": "c1"},
    )

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and adapter.sent == 0:
        time.sleep(0.02)
    assert adapter.sent > 0, "LLM 调用没有开始"

    canceled = media_bus.cancel(store, job["job_id"])
    assert canceled["canceled"] is True
    assert canceled["status"] == "canceled"

    snap = _wait_terminal(store, job["job_id"])
    assert snap["status"] == "canceled"
    assert snap["artifacts"] == []
    assert snap["meta"]["llm_calls"] == 1
    # 取消不写半截产物：既没有结果摘要，也没有提交任何文本
    assert "result" not in snap["meta"]
    assert "answer" not in snap["meta"]
    assert adapter.finished is False

    # 重复取消幂等
    again = media_bus.cancel(store, job["job_id"])
    assert again["canceled"] is False


def test_success_records_llm_call_count(store: ProjectStore):
    """正常完成时也记录本次嵌套的 LLM 调用次数。"""
    adapter = ChunkedAdapter(chunks=2)
    call_id = llm_cancel.new_call_id()

    def handler(handle, _store, _config, _params):
        """跑完一次 LLM 调用并返回结果摘要。"""
        bus = LlmBus(config=None, call_id=call_id)
        bus._adapter = adapter
        result = bus.text("character.updates", _request())
        return {"message": result.text}

    media_bus.register_handler("voice.preview", handler)
    job = media_bus.run(
        store,
        None,
        "voice.preview",
        {"path": str(store.root), "character_id": "c1"},
    )

    snap = _wait_terminal(store, job["job_id"])
    assert snap["status"] == "succeeded"
    assert snap["meta"]["llm_calls"] == 1
    assert snap["meta"]["result"]["message"] == "完整结果"
