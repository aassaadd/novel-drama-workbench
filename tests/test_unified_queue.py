"""统一任务队列与取消入口。

覆盖：
1. `/api/ui/queue` 只返回统一结构：界面任务带 `ui=True`，GPU 队列任务字段一致；
2. 统一取消按 `id` / `job_id` 都能定位，任务不存在返回 404；
3. 媒体任务重复取消幂等（已结束任务 canceled=False）；
4. 跨章节任务出现在同一任务列表，不被章节过滤。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from novel_drama.adapters.persistence import ProjectStore, QueueStore
from novel_drama.application.support import media_bus
from novel_drama.interface.http.app import create_app
from novel_drama.models.entities import new_job

#: 统一任务结构必须包含的字段（界面任务与 GPU 队列任务一致）
_UNIFIED_FIELDS = {
    "id",
    "job_id",
    "kind",
    "action",
    "channel",
    "parent_id",
    "cancelable",
    "ui",
    "title",
    "target",
    "status",
    "percent",
    "stage",
    "current",
    "error",
    "artifacts",
    "progress",
    "timings",
    "created_at",
    "meta",
}


@pytest.fixture
def store(tmp_path: Path) -> ProjectStore:
    """建一个临时工程并返回可复用的 ProjectStore。"""
    ProjectStore(str(tmp_path)).create("统一任务队列测试")
    return ProjectStore(str(tmp_path))


@pytest.fixture(autouse=True)
def _clean_handlers():
    """每个用例前后清空执行器登记，避免用例之间串味。"""
    media_bus._HANDLERS.clear()
    yield
    media_bus._HANDLERS.clear()


@pytest.fixture
def client(tmp_path: Path, store: ProjectStore) -> TestClient:
    """带离线配置的 Web 客户端。"""
    config = tmp_path / "offline.yaml"
    config.write_text("adapters:\n  writing: offline\n", encoding="utf-8")
    return TestClient(create_app(project_root=str(store.root), config_path=str(config)))


def _blocking_handler(handle, _store, _config, _params):
    """模拟长时间执行器：只在收到取消或超时后返回。"""
    for _ in range(500):
        if handle.cancel_requested():
            return {"message": "已停止"}
        time.sleep(0.01)
    return {"message": "超时"}


def _wait(predicate, timeout: float = 5.0):
    """轮询等待条件成立，返回最后一次结果。

    参数:
        predicate: 无参函数，返回真值表示命中。
        timeout: 最长等待秒数。

    返回:
        predicate 最后一次返回值。

    异常:
        AssertionError: 超时仍未命中。
    """
    deadline = time.monotonic() + timeout
    latest = None
    while time.monotonic() < deadline:
        latest = predicate()
        if latest:
            return latest
        time.sleep(0.02)
    raise AssertionError(f"等待超时：{latest}")


def test_queue_rows_share_unified_fields(client: TestClient, store: ProjectStore):
    """媒体任务与 GPU 队列任务都返回同一套统一字段，并标记来源。"""
    # create_app 会注册全部执行器；本用例只保留自定义执行器
    media_bus._HANDLERS.clear()
    media_bus.register_handler("tts.chapter", _blocking_handler)
    media_job = media_bus.run(
        store, None, "tts.chapter", {"path": str(store.root), "chapter": "ch01"}
    )
    QueueStore(store).save([new_job("image", {"chapter": "ch02"}, "sha256:queue-test")])

    rows = client.get("/api/ui/queue", params={"path": str(store.root)}).json()
    assert len(rows) == 2, rows
    by_kind = {row["kind"]: row for row in rows}

    media_row = by_kind["tts.chapter"]
    assert _UNIFIED_FIELDS <= set(media_row)
    assert media_row["ui"] is True
    assert media_row["job_id"] == media_job["job_id"]
    assert media_row["channel"] == "audio"
    assert media_row["cancelable"] is True
    assert media_row["title"] == "本章批量配音"
    assert media_row["target"] == {"chapter": "ch01"}

    gpu_row = by_kind["image"]
    assert _UNIFIED_FIELDS <= set(gpu_row)
    assert gpu_row["ui"] is False
    assert gpu_row["cancelable"] is False
    assert gpu_row["channel"] == "image"
    assert gpu_row["title"] == "配图"
    assert gpu_row["status"] == "queued"
    assert gpu_row["percent"] is None
    assert gpu_row["progress"] == {"percent": None, "current": ""}

    created = [row["created_at"] for row in rows]
    assert created == sorted(created, reverse=True)

    media_bus.cancel(store, media_job["job_id"])


def test_cancel_unknown_job_returns_404(client: TestClient, store: ProjectStore):
    """统一取消：任务不存在时返回 404，而不是静默成功。"""
    response = client.post(
        "/api/ui/jobs/cancel",
        json={"path": str(store.root), "job_id": "job_missing"},
    )
    assert response.status_code == 404, response.text
    assert response.json()["detail"]["code"] == 4


def test_media_job_cancel_is_idempotent(client: TestClient, store: ProjectStore):
    """媒体任务取消：按 job_id 定位、重复取消返回 canceled=False。"""
    media_bus._HANDLERS.clear()
    media_bus.register_handler("tts.chapter", _blocking_handler)
    job = media_bus.run(
        store, None, "tts.chapter", {"path": str(store.root), "chapter": "ch01"}
    )

    first = client.post(
        "/api/ui/jobs/cancel",
        json={"path": str(store.root), "job_id": job["job_id"]},
    )
    assert first.status_code == 200, first.text
    assert first.json()["canceled"] is True
    assert first.json()["status"] == "canceled"

    second = client.post(
        "/api/ui/jobs/cancel",
        json={"path": str(store.root), "id": job["job_id"]},
    )
    assert second.status_code == 200, second.text
    assert second.json()["canceled"] is False
    assert second.json()["status"] == "canceled"


def test_cross_chapter_jobs_are_visible_together(client: TestClient, store: ProjectStore):
    """不同章节的媒体任务出现在同一任务列表（任务页不按章节过滤）。"""
    media_bus._HANDLERS.clear()
    media_bus.register_handler("tts.chapter", _blocking_handler)
    first = media_bus.run(
        store, None, "tts.chapter", {"path": str(store.root), "chapter": "ch01"}
    )
    second = media_bus.run(
        store, None, "tts.chapter", {"path": str(store.root), "chapter": "ch02"}
    )

    rows = client.get("/api/ui/queue", params={"path": str(store.root)}).json()
    chapters = {
        str((row.get("target") or {}).get("chapter") or "")
        for row in rows
        if row.get("kind") == "tts.chapter"
    }
    assert {"ch01", "ch02"} <= chapters
    ids = {row["job_id"] for row in rows}
    assert {first["job_id"], second["job_id"]} <= ids

    media_bus.cancel(store, first["job_id"])
    media_bus.cancel(store, second["job_id"])
    _wait(lambda: [row for row in client.get("/api/ui/queue", params={"path": str(store.root)}).json()
                   if row["job_id"] in {first["job_id"], second["job_id"]}
                   and row["status"] == "canceled"])
