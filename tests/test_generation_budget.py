"""上下文/输出口径的离线用例。

口径：输出侧不再设置任何人为上限（`max_tokens` 取「上下文窗口 − 已用提示词」），
输入侧仍按模型上下文窗口限制装配；finish_reason=length 按异常提示，不静默放过。
"""

from __future__ import annotations

import json
from pathlib import Path
from queue import Queue

import pytest

from novel_drama.adapters.base import (
    AdapterError,
    ErrorCode,
    Message,
    TextGenRequest,
    TextGenResult,
)
from novel_drama.adapters.writing import OmlxWritingAdapter
from novel_drama.config import load_config
from novel_drama.writing import budgets, service


def _write(path: Path, text: str) -> Path:
    """写入临时配置文本并返回路径。

    参数:
        path: 目标文件。
        text: YAML 文本。

    返回:
        文件路径。
    """
    # GPU 锁文件指向临时目录，避免测试读取真实外置盘路径
    header = f"gpu:\n  lock_path: {path.with_suffix('.lock')}\n"
    path.write_text(header + text, encoding="utf-8")
    return path


def _adapter(settings: dict) -> OmlxWritingAdapter:
    """构造不发起 HTTP 的写作适配器（只用于请求体与思考解析）。

    参数:
        settings: omlx 配置片段。

    返回:
        OmlxWritingAdapter 实例。
    """
    base = {"endpoint": "http://127.0.0.1:1", "model": "demo", "temperature": 0.8}
    base.update(settings)
    return OmlxWritingAdapter(base)


def test_context_status_uses_window_and_fallback(tmp_path: Path) -> None:
    """输入侧预算优先用模型上下文窗口；未知时按 32768 兜底。"""
    base = _write(tmp_path / "config.yaml", "omlx:\n  context_window: 100000\n")
    config = load_config(base, tmp_path / "missing.ui.yaml")

    assert budgets.context_window(config) == 100000
    assert budgets.context_budget_tokens(config) == 100000
    status = budgets.context_status(config)
    assert status["window"] == 100000
    assert status["budget"] == 100000
    assert status["detected"] is True and status["fallback"] is False
    assert "输出侧不设置人为上限" in status["note"]

    empty = _write(tmp_path / "empty.yaml", "")
    fallback = budgets.context_status(load_config(empty, tmp_path / "missing2.ui.yaml"))
    assert fallback["window"] is None
    assert fallback["budget"] == budgets.DEFAULT_CONTEXT_BUDGET_TOKENS
    assert fallback["fallback"] is True


def test_adapter_max_tokens_is_remaining_context() -> None:
    """请求 max_tokens = 上下文窗口 − 已用提示词，不受旧设置项影响。"""
    adapter = _adapter({"model": "remaining-demo", "context_window": 10000, "max_tokens": 512})
    request = TextGenRequest(task="chapter", system="", messages=[Message("user", "字" * 100)])

    payload = adapter._build_payload(request, stream=False)

    # 提示词估算：100 字 + 256 固定开销 = 356；剩余空间 9644
    assert payload["max_tokens"] == 10000 - 356
    assert payload["max_tokens"] != 512


def test_adapter_omits_max_tokens_without_window() -> None:
    """上下文窗口未知时不传 max_tokens，由模型自身决定何时停止。"""
    adapter = _adapter({"model": "no-window-demo"})
    request = TextGenRequest(task="chapter", system="", messages=[Message("user", "x" * 10)])

    payload = adapter._build_payload(request, stream=False)

    assert "max_tokens" not in payload


def test_adapter_rejects_prompt_over_window() -> None:
    """提示词占满上下文窗口时给出中文错误，不做静默截断。"""
    adapter = _adapter({"model": "over-window-demo", "context_window": 300})
    request = TextGenRequest(task="chapter", system="", messages=[Message("user", "x" * 100)])

    with pytest.raises(AdapterError) as error:
        adapter._build_payload(request, stream=False)

    assert error.value.code == ErrorCode.INVALID_INPUT
    assert "上下文窗口" in error.value.message


def test_output_budget_is_not_a_setting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """设置页与接口不再暴露任何输出预算设置；状态接口只读。"""
    from fastapi.testclient import TestClient

    from novel_drama.web import models_api
    from novel_drama.web.app import create_app

    base = _write(tmp_path / "config.yaml", "omlx:\n  context_window: 65536\n")
    # 隔离真实页面覆盖层，避免本机 config.ui.yaml 里的上下文窗口干扰断言
    ui = tmp_path / "config.ui.yaml"
    monkeypatch.setattr("novel_drama.config.DEFAULT_UI_CONFIG_PATH", ui)
    monkeypatch.setattr(models_api, "DEFAULT_UI_CONFIG_PATH", ui)
    client = TestClient(create_app(config_path=str(base)))

    config = client.get("/api/config").json()
    assert "omlx.max_tokens" not in config["editable"]
    assert "max_tokens" not in (config.get("writing") or {})

    connection = client.get("/api/ui/config/connection").json()
    assert "max_output_tokens" not in connection["llm"]
    assert "round_max_tokens" not in connection["generation"]

    status = client.get("/api/ui/config/generation").json()
    assert status["context"]["window"] == 65536
    assert status["output"]["artificial_limit"] is False
    assert "人为上限" in status["output"]["note"]
    assert isinstance(status["last_call"], dict)
    # 正文生成只恢复两个可改项，且不带任何输出预算字段
    gen = status["generation"]
    assert set(gen["values"]) == {"beats_per_round", "max_rounds"}
    assert "max_tokens" not in gen["values"]


def _generation_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """构造带独立覆盖层的设置客户端。

    参数:
        tmp_path: 临时目录。
        monkeypatch: pytest 补丁工具。

    返回:
        (TestClient, 覆盖层路径)。
    """
    from fastapi.testclient import TestClient

    from novel_drama.web import models_api
    from novel_drama.web.app import create_app

    base = _write(tmp_path / "config.yaml", "")
    ui = tmp_path / "config.ui.yaml"
    monkeypatch.setattr("novel_drama.config.DEFAULT_UI_CONFIG_PATH", ui)
    monkeypatch.setattr(models_api, "DEFAULT_UI_CONFIG_PATH", ui)
    return TestClient(create_app(config_path=str(base))), ui


def test_generation_settings_save_and_reset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """保存接口只写每轮节拍数与最大轮数，并支持恢复默认。"""
    client, ui = _generation_client(tmp_path, monkeypatch)

    saved = client.post(
        "/api/ui/config/generation",
        json={
            "values": {
                "writing.multi_turn.beats_per_round": 3,
                "writing.multi_turn.max_rounds": 6,
            }
        },
    )
    assert saved.status_code == 200
    gen = saved.json()["generation"]
    assert gen["values"] == {"beats_per_round": 3, "max_rounds": 6}
    assert gen["sources"]["beats_per_round"] == "page"
    assert gen["sources"]["max_rounds"] == "page"
    assert "beats_per_round" in ui.read_text(encoding="utf-8")

    # 自动分配用 0 表示；最大轮数下限 1
    reset = client.post(
        "/api/ui/config/generation",
        json={
            "values": {
                "writing.multi_turn.beats_per_round": None,
                "writing.multi_turn.max_rounds": None,
            }
        },
    )
    assert reset.status_code == 200
    gen = reset.json()["generation"]
    assert gen["values"] == {"beats_per_round": 0, "max_rounds": 8}
    assert gen["sources"]["beats_per_round"] == "builtin"
    assert gen["sources"]["max_rounds"] == "builtin"


def test_generation_settings_reject_budget_and_bad_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """输出预算类键与非整数/越界值都拒绝，不允许借这个接口加回预算。"""
    client, _ui = _generation_client(tmp_path, monkeypatch)

    budget = client.post(
        "/api/ui/config/generation",
        json={"values": {"omlx.max_tokens": 16384}},
    )
    assert budget.status_code == 400
    assert "不支持修改" in budget.json()["detail"]["message"]

    for value in (0, 21, 2.5, "abc"):
        response = client.post(
            "/api/ui/config/generation",
            json={"values": {"writing.multi_turn.max_rounds": value}},
        )
        assert response.status_code == 400


def test_thinking_is_single_global_switch() -> None:
    """思考只有全局开关与强度，不再按正文/摘要/审校/抽取分阶段。"""
    adapter = _adapter({"model": "thinking-demo", "enable_thinking": True, "reasoning_effort": "high"})
    chapter = TextGenRequest(task="chapter", system="", messages=[Message("user", "x")])
    recap = TextGenRequest(task="recap", system="", messages=[Message("user", "x")])

    assert adapter._thinking_for(chapter) == (True, "high")
    # 摘要不再有阶段覆盖：仍跟随全局开关
    assert adapter._thinking_for(recap) == (True, "high")

    override = TextGenRequest(
        task="recap",
        system="",
        messages=[Message("user", "x")],
        metadata={"enable_thinking": False},
    )
    assert adapter._thinking_for(override) == (False, "high")


def test_finish_reason_length_is_reported() -> None:
    """finish_reason=length 按异常提示，且不触发内容重试。"""
    body = (
        "雨从傍晚就开始下，屋檐下的水声一直没有停。"
        "他把外衣挂在门后，走到桌边坐下，手指在杯沿上停了很久。"
        "窗外的街灯被水汽泡得发白，远处传来一阵很轻的车声。"
        "她推门进来时带进一股凉气，头发上还沾着细小的水珠。"
        "两人都没有先开口，屋子里只剩钟摆来回走动的声音。"
        "他终于抬起头，问了一句路上顺不顺利。"
    )

    class FakeAdapter:
        """只返回一次结果、记录调用次数的假适配器。"""

        def __init__(self) -> None:
            """初始化调用计数。"""
            self.calls = 0

        def generate(self, request: TextGenRequest) -> TextGenResult:
            """返回 finish_reason=length 的正文。

            参数:
                request: 统一文本请求。

            返回:
                生成结果。
            """
            self.calls += 1
            return TextGenResult(text=body, finish_reason="length")

    adapter = FakeAdapter()
    notices: list[str] = []
    request = TextGenRequest(task="chapter", system="", messages=[Message("user", "写一章")])

    result, text, _, _ = service._generate_validated(
        adapter, request, None, "第 1 轮正文", on_notice=notices.append
    )

    assert result.finish_reason == "length"
    assert text == body
    assert adapter.calls == 1
    assert any("finish_reason=length" in item and "异常" in item for item in notices)


def test_stream_notices_cover_chapter_results() -> None:
    """整幕结果里的逐章提醒也会进任务通知，完成文案带上第一条与剩余条数。"""
    from novel_drama.web.stream_api import (
        _emit_notices,
        _finish_message,
        _result_notices,
    )

    class FakeJob:
        """只记录 event 调用的假任务。"""

        def __init__(self) -> None:
            """初始化记录列表。"""
            self.events: list[tuple[str, dict]] = []

        def event(self, kind: str, data: dict | None = None) -> None:
            """记录一次任务事件。

            参数:
                kind: 事件名。
                data: 事件负载。

            返回:
                无。
            """
            self.events.append((kind, data or {}))

    result = {
        "notices": ["本章摘要回退到单独请求"],
        "chapters": [{"notices": ["第 1 章以 finish_reason=length 结束"]}, {"notices": []}],
    }
    notices = _result_notices(result)
    assert notices == ["本章摘要回退到单独请求", "第 1 章以 finish_reason=length 结束"]

    job = FakeJob()
    events: Queue = Queue()
    _emit_notices(notices, job, events)
    assert [kind for kind, _ in job.events] == ["notice", "notice"]
    event, data = events.get()
    assert event == "notice" and data["message"] == "本章摘要回退到单独请求"
    assert _finish_message(notices).startswith("生成完成 · ⚠ 本章摘要回退到单独请求")
    assert "另有 1 条提醒" in _finish_message(notices)


def test_legacy_project_max_tokens_is_rejected(tmp_path: Path) -> None:
    """旧工程带 settings.omlx.max_tokens 时直接报可读错误，不静默忽略。"""
    from novel_drama.store.project_store import ProjectStore
    from novel_drama.store.schema_validator import SchemaValidationError

    project = {
        "schema_version": 1,
        "work": {"id": "wrk_legacy", "title": "旧工程"},
        "settings": {"omlx": {"max_tokens": 2048}},
    }
    (tmp_path / "project.json").write_text(json.dumps(project, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(SchemaValidationError) as error:
        ProjectStore(tmp_path).load_project()

    message = str(error.value)
    assert "settings/omlx/max_tokens" in message
    assert "已删除" in message

