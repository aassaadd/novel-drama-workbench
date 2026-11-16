"""统一媒体生成总线：登记表、任务结构与生命周期。

覆盖：
1. 动作登记表完整（12 个动作、通道与任务类型合法）；
2. 成功 / 失败 / 取消的统一任务字段与进度；
3. 取消幂等，以及取消已结束任务不报错；
4. HTTP 入口：未登记动作、未注册执行器都返回 400；
5. 任务文件版本：旧结构丢弃、遗留 running 标记 interrupted。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from novel_drama.adapters.persistence import ProjectStore
from novel_drama.application.production import job_tracker
from novel_drama.application.support import media_bus, media_plan
from novel_drama.interface.http.app import create_app
from novel_drama.models.enums import UiJobKind

#: 轮询终态的最长等待（秒）
_WAIT_S = 8.0


@pytest.fixture
def store(tmp_path: Path) -> ProjectStore:
    """建一个临时工程并返回可复用的 ProjectStore。"""
    ProjectStore(str(tmp_path)).create("媒体总线测试")
    return ProjectStore(str(tmp_path))


@pytest.fixture(autouse=True)
def _clean_handlers():
    """每个用例前后清空执行器登记，避免用例之间串味。"""
    media_bus._HANDLERS.clear()
    yield
    media_bus._HANDLERS.clear()


@pytest.fixture
def client(tmp_path: Path, store: ProjectStore, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """带离线配置的 Web 客户端（不读取本机可能不可写的 GPU 路径）。"""
    monkeypatch.setattr(
        "novel_drama.interface.http.routers.media_bus.load_config",
        lambda *_args, **_kwargs: None,
    )
    config = tmp_path / "offline.yaml"
    config.write_text("adapters:\n  writing: offline\n", encoding="utf-8")
    return TestClient(create_app(project_root=str(store.root), config_path=str(config)))


def _wait_terminal(store: ProjectStore, job_id: str) -> dict:
    """轮询任务直到进入终态并返回快照。

    参数:
        store: 工程。
        job_id: 任务 ID。

    返回:
        终态任务快照。

    异常:
        AssertionError: 超时仍未进入终态。
    """
    deadline = time.monotonic() + _WAIT_S
    latest: dict | None = None
    while time.monotonic() < deadline:
        latest = media_bus.snapshot(store, job_id)
        if latest and latest.get("status") in job_tracker.TERMINAL_STATUSES:
            return latest
        time.sleep(0.02)
    raise AssertionError(f"任务未在 {_WAIT_S} 秒内结束：{latest}")


def test_action_registry_is_complete():
    """12 个媒体动作齐全，通道、任务类型与展示字段合法。"""
    assert len(media_plan.ACTIONS) == 12
    rows = media_plan.action_rows()
    assert len(rows) == 12
    assert {row["action"] for row in rows} == set(media_plan.ACTIONS)

    kinds = set(UiJobKind.values())
    for row in rows:
        assert row["channel"] in ("audio", "image", "video")
        assert row["channel_label"] in ("声音", "图片", "视频")
        assert row["kind"] in kinds, f"{row['action']} 的任务类型未登记：{row['kind']}"
        assert row["title"]
        assert isinstance(row["stages"], list) and row["stages"]
        assert isinstance(row["cancelable"], bool)
        assert isinstance(row["gpu"], bool)
        assert row["usage"]

    with pytest.raises(ValueError, match="未登记的媒体动作"):
        media_plan.action_spec("not.exists")


def test_success_job_has_unified_snapshot(store: ProjectStore):
    """成功任务：percent=100、产物与结果摘要都落到统一字段。"""

    def handler(handle, _store, _config, params):
        """模拟成功执行器：上报阶段、进度、产物与结果。"""
        assert params["sfx_id"] == "s1"
        handle.stage("合成")
        handle.progress("生成音效", 40)
        handle.artifacts(["audio/sfx/s1.wav"])
        handle.set_meta(note="测试元数据")
        return {"message": "完成 1 条音效", "items": ["audio/sfx/s1.wav"]}

    media_bus.register_handler("sfx.item", handler)
    job = media_bus.run(
        store,
        None,
        "sfx.item",
        {"path": str(store.root), "chapter": "ch1", "sfx_id": "s1"},
    )

    assert job["status"] == "running"
    assert job["kind"] == "sfx"
    assert job["action"] == "sfx.item"
    assert job["channel"] == "audio"
    assert job["title"] == "单条音效"
    assert job["target"] == {"chapter": "ch1", "sfx_id": "s1"}
    assert job["progress"]["current"] == "已提交生成请求"

    snap = _wait_terminal(store, job["job_id"])
    assert snap["status"] == "succeeded"
    assert snap["percent"] == 100
    assert snap["progress"]["percent"] == 100
    assert snap["artifacts"] == ["audio/sfx/s1.wav"]
    assert snap["meta"]["note"] == "测试元数据"
    assert snap["meta"]["result"]["items"] == ["audio/sfx/s1.wav"]
    assert snap["timings"]["started_at"]


def test_failed_job_has_chinese_reason(store: ProjectStore):
    """执行器异常：任务 failed，错误文案带动作名与原始原因。"""

    def handler(_handle, _store, _config, _params):
        """模拟上游失败。"""
        raise RuntimeError("上游 502")

    media_bus.register_handler("tts.book", handler)
    job = media_bus.run(store, None, "tts.book", {"path": str(store.root)})
    snap = _wait_terminal(store, job["job_id"])

    assert snap["status"] == "failed"
    assert snap["percent"] is None
    assert snap["progress"]["percent"] is None
    assert snap["error"].startswith("全书批量配音失败：")
    assert "上游 502" in snap["error"]


def test_cancel_stops_handler_and_skips_artifacts(store: ProjectStore):
    """取消后任务落 canceled，执行器不再写产物。"""

    def handler(handle, _store, _config, _params):
        """循环等待取消；只在被取消时返回。"""
        for _ in range(400):
            if handle.cancel_requested():
                return {"message": "已停止"}
            time.sleep(0.02)
        return {"message": "超时"}

    media_bus.register_handler("tts.chapter", handler)
    job = media_bus.run(store, None, "tts.chapter", {"path": str(store.root), "chapter": "ch1"})

    result = media_bus.cancel(store, job["job_id"])
    assert result["canceled"] is True
    assert result["status"] == "canceled"

    snap = _wait_terminal(store, job["job_id"])
    assert snap["status"] == "canceled"
    assert snap["artifacts"] == []
    assert snap["percent"] is None


def test_cancel_is_idempotent(store: ProjectStore):
    """重复取消与取消已结束任务都不抛错，canceled=False。"""

    def handler(handle, _store, _config, _params):
        """等待取消后退出。"""
        for _ in range(400):
            if handle.cancel_requested():
                return {"message": "已停止"}
            time.sleep(0.02)
        return {"message": "超时"}

    media_bus.register_handler("tts.chapter", handler)
    job = media_bus.run(store, None, "tts.chapter", {"path": str(store.root), "chapter": "ch1"})

    first = media_bus.cancel(store, job["job_id"])
    assert first["canceled"] is True
    _wait_terminal(store, job["job_id"])

    second = media_bus.cancel(store, job["job_id"])
    assert second["canceled"] is False
    assert second["status"] == "canceled"
    assert "任务已结束" in second["message"]


def test_run_rejects_unregistered_action(client: TestClient, store: ProjectStore):
    """POST /api/ui/media/run：未登记动作返回 400 与可选动作清单。"""
    response = client.post(
        "/api/ui/media/run",
        json={"action": "not.exists", "params": {"path": str(store.root)}},
    )
    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert detail["code"] == 2
    assert "未登记的媒体动作" in detail["message"]


def test_run_requires_registered_handler(client: TestClient, store: ProjectStore):
    """POST /api/ui/media/run：动作已登记但没有执行器时返回 400。"""
    media_bus._HANDLERS.clear()
    response = client.post(
        "/api/ui/media/run",
        json={"action": "tts.book", "params": {"path": str(store.root)}},
    )
    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert detail["code"] == 2
    assert "还没有可用的执行器" in detail["message"]


def test_old_schema_file_is_discarded(store: ProjectStore):
    """旧结构（schema_version=1）在读取时直接丢弃。"""
    path = store.root / ".ndw" / "ui_jobs.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "jobs": [
                    {
                        "id": "job_old",
                        "kind": "script",
                        "status": "running",
                        "created_at": "2000-01-01T00:00:00Z",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    assert job_tracker.load_ui_jobs(store) == []


def test_stale_running_job_marked_interrupted(store: ProjectStore):
    """上次进程遗留的 running 任务在读取时标记为 interrupted。"""
    path = store.root / ".ndw" / "ui_jobs.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "jobs": [
                    {
                        "id": "job_stale",
                        "job_id": "job_stale",
                        "kind": "tts.chapter",
                        "action": "tts.chapter",
                        "channel": "audio",
                        "parent_id": None,
                        "cancelable": True,
                        "title": "本章批量配音",
                        "target": {"chapter": "ch1"},
                        "status": "running",
                        "percent": None,
                        "stage": "载入",
                        "current": "合成中",
                        "artifacts": [],
                        "error": None,
                        "progress": {"percent": None, "current": "合成中"},
                        "meta": {},
                        "attempts": 0,
                        "priority": 100,
                        "idempotency_key": "k",
                        "created_at": "2000-01-01T00:00:00Z",
                        "timings": {"started_at": "2000-01-01T00:00:00Z"},
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    loaded = job_tracker.load_ui_jobs(store)
    assert len(loaded) == 1
    assert loaded[0]["status"] == "interrupted"
    assert loaded[0]["progress"]["percent"] is None
    assert loaded[0]["timings"]["finished_at"]


def test_start_writes_percent_stage_and_progress(store: ProjectStore):
    """UiJob.start 产出的记录同时有顶层进度与 progress 字段。"""
    job = job_tracker.UiJob(
        store,
        "tts.line",
        {"chapter": "ch1", "line": "ln1"},
        "单行重录",
        action="tts.line",
        channel="audio",
    )
    job.start()

    raw = next(item for item in job_tracker.load_ui_jobs(store) if item.get("id") == job.job["id"])
    assert raw["job_id"] == raw["id"]
    assert raw["action"] == "tts.line"
    assert raw["channel"] == "audio"
    assert raw["cancelable"] is True
    assert raw["percent"] is None
    assert raw["stage"] == "提交"
    assert raw["current"] == "已提交生成请求"
    assert raw["progress"] == {"percent": None, "current": "已提交生成请求"}

    job.cancel("测试结束")
