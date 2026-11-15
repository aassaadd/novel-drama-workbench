"""思考通道的重复检测、空正文重试与看门狗思考下限（离线用例）。

覆盖：
- 思考内容命中重复（S1/S2/S3 同一套阈值）时提前中止，带 `reasoning_loop` 原因码；
- 正文为空且 `finish_reason=length` 时关掉思考重试一次，成功即返回正文；
- 重试仍为空时报明确中文错误，章节正文与 revision 都不变；
- 看门狗开思考时给正文留出额度下限，并说明这不是创作限制。

运行：`.venv/bin/python -m pytest tests/test_thinking_budget.py -v`。
"""

from __future__ import annotations

import pytest
from test_chapter_resume import _patch_adapter, _project

from novel_drama.adapters.errors import AdapterError
from novel_drama.adapters.llm.contracts import (
    Message,
    TextGenChunk,
    TextGenRequest,
    TextGenResult,
)
from novel_drama.adapters.llm.omlx import OmlxWritingAdapter
from novel_drama.adapters.llm.reasoning_guard import ReasoningLoopGuard
from novel_drama.application.story.chapter_context import stream_text
from novel_drama.application.story.generate_chapter import (
    generate_chapter as generate_chapter,
)
from novel_drama.domain.production.rules.watchdog import (
    THINKING_ALLOWANCE_TOKENS,
    TOKENS_PER_CHAR,
    describe,
    watchdog_tokens,
)

#: 思考里原地绕圈的典型句子
LOOP_SENTENCE = "让我再想想这个问题。"
NORMAL_TEXT = "他把窗推开一条缝，湿冷的风挤进来，桌上的纸角被掀起又落下。"


def _request(metadata: dict | None = None) -> TextGenRequest:
    """构造一个正文请求。"""
    return TextGenRequest(
        task="chapter",
        system="",
        messages=[Message("user", "写一节")],
        metadata=metadata or {"thinking_stage": "chapter_round", "target_words": 500},
    )


def test_reasoning_loop_guard_detects_repetition() -> None:
    """思考内容里整段重复 3 次即命中，原因与正文侧同一套口径。"""
    guard = ReasoningLoopGuard(1200)
    reason = guard.feed(LOOP_SENTENCE * 20) or guard.check_all()
    assert reason
    assert "连续重复 3 次" in reason


def test_reasoning_loop_guard_ignores_normal_thinking() -> None:
    """正常思考（有推进、不重复）不命中。"""
    guard = ReasoningLoopGuard(1200)
    text = (
        "先确认场景：喜堂、纸扎宾客、周也站在盖头前。"
        "再看约束：九斤手腕被红绸缠住，动不了。"
        "这一拍要推进的是纸人开始收吹打声，空气变冷，所以她先听到声音变化。"
    )
    assert guard.feed(text) == ""
    assert guard.check_all() == ""


def test_reasoning_loop_abort_carries_reason_code() -> None:
    """命中后抛 AdapterError，`detail.code` 为 reasoning_loop，文案说明已中止。"""
    adapter = OmlxWritingAdapter({})
    with pytest.raises(AdapterError) as exc:
        adapter._abort_on_reasoning_loop(_request(), "同一段连续重复 3 次（「让我再想想」）")
    assert exc.value.detail.get("code") == "reasoning_loop"
    assert "思考" in exc.value.message
    # 日志钩子据此写入 reason.code，便于统计「谁把调用停掉的」
    from novel_drama.adapters.observability.model_call_recorder import (
        _error_reason_code,
    )

    assert _error_reason_code(exc.value) == "reasoning_loop"


class EmptyThenTextAdapter:
    """第一次只出思考并以 length 结束；关掉思考后第二次返回正文。"""

    def __init__(self) -> None:
        """记录每次请求，便于核对第二次是否关了思考。"""
        self.calls: list[TextGenRequest] = []
        self.model = "fake"

    def generate(self, request: TextGenRequest) -> TextGenResult:
        """非流式：一律返回空正文 + length。"""
        self.calls.append(request)
        return TextGenResult(text="", finish_reason="length", model="fake")

    def stream(self, request: TextGenRequest):
        """流式：思考耗尽额度时正文为空，关思考后正常输出。"""
        self.calls.append(request)
        if (request.metadata or {}).get("enable_thinking") is False:
            yield TextGenChunk(delta=NORMAL_TEXT, done=False)
            yield TextGenChunk(delta="", done=True, finish_reason="stop")
        else:
            yield TextGenChunk(delta="", done=False)
            yield TextGenChunk(delta="", done=True, finish_reason="length")


class AlwaysEmptyAdapter:
    """正文始终为空、结束原因始终是 length。"""

    def __init__(self) -> None:
        """记录调用次数。"""
        self.calls = 0
        self.model = "fake"

    def generate(self, request: TextGenRequest) -> TextGenResult:
        """非流式：空正文 + length。"""
        self.calls += 1
        return TextGenResult(text="", finish_reason="length", model="fake")

    def stream(self, request: TextGenRequest):
        """流式：空正文 + length。"""
        self.calls += 1
        yield TextGenChunk(delta="", done=True, finish_reason="length")


def test_empty_body_retries_with_thinking_off() -> None:
    """空正文且额度用尽时关掉思考重试一次，成功即返回正文并留通知。"""
    adapter = EmptyThenTextAdapter()
    notices: list[str] = []
    result = stream_text(adapter, _request(), lambda _text: None, on_notice=notices.append)

    assert result.text == NORMAL_TEXT
    assert len(adapter.calls) == 2
    assert (adapter.calls[1].metadata or {}).get("enable_thinking") is False
    assert any("思考占满额度" in text for text in notices)


def test_empty_body_twice_raises() -> None:
    """重试仍为空时报明确中文错误，原因码为 thinking_exhausted。"""
    with pytest.raises(AdapterError) as exc:
        stream_text(AlwaysEmptyAdapter(), _request(), lambda _text: None)
    assert exc.value.detail.get("code") == "thinking_exhausted"
    assert "思考占满输出额度" in exc.value.message


def test_empty_body_does_not_touch_chapter(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """整章生成遇到空正文时报错，正文与 revision 都不变。"""
    store, config, chapter = _project(tmp_path)
    _patch_adapter(monkeypatch, AlwaysEmptyAdapter())
    before = store.load_chapter(chapter["id"])

    with pytest.raises(AdapterError):
        generate_chapter(store, config, chapter["id"])

    after = store.load_chapter(chapter["id"])
    assert after.get("content") == before.get("content")
    assert after.get("revision") == before.get("revision")


def test_watchdog_reserves_body_space_when_thinking() -> None:
    """开思考时看门狗不低于「正文额度 + 思考预留」，且说明里写明不是创作限制。"""
    target = 500
    plain = watchdog_tokens(target)
    thinking = watchdog_tokens(target, False, True)
    assert thinking > plain
    assert thinking >= int(target * TOKENS_PER_CHAR) + THINKING_ALLOWANCE_TOKENS

    text = describe(target, False, None, thinking, True)
    assert "思考预留" in text
    assert "止损线" in text
