"""统一媒体执行器：12 个动作的注册、进度、取消、产物与失败文案。

覆盖：
1. 动作登记表里的每个动作都注册了执行器；
2. `tts.chapter` 把批次进度转成任务百分比，取消后任务落成 canceled；
3. 音效 / 配乐 / 配图成功时返回结果并写 artifacts；
4. 底层异常落成 failed，错误文案带「失败：」；
5. 图生视频没有 i2v 条目时给出中文原因；
6. 已删除的旧接口在 TestClient 下不再可达。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from novel_drama.adapters.persistence import ProjectStore
from novel_drama.application.support import media_bus, media_handlers, media_plan
from novel_drama.interface.http.app import create_app

#: 轮询终态的最长等待（秒）
_WAIT_S = 10.0


@pytest.fixture
def store(tmp_path: Path) -> ProjectStore:
    """建一个临时工程并返回可复用的 ProjectStore。"""
    ProjectStore(str(tmp_path)).create("执行器测试")
    return ProjectStore(str(tmp_path))


@pytest.fixture(autouse=True)
def _clean_handlers():
    """每个用例前后重置执行器登记，避免用例之间串味。"""
    media_bus._HANDLERS.clear()
    media_handlers._REGISTERED = False
    media_handlers.register_all()
    yield
    media_bus._HANDLERS.clear()
    media_handlers._REGISTERED = False


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
    while time.monotonic() < deadline:
        snap = media_bus.snapshot(store, job_id)
        if snap and snap.get("status") not in ("running", "pending", "queued"):
            return snap
        time.sleep(0.05)
    raise AssertionError(f"任务未在 {_WAIT_S}s 内结束: {job_id}")


def _wait_running_percent(store: ProjectStore, job_id: str, percent: int) -> dict:
    """轮询任务直到它处于 running 且百分比达到期望值。

    参数:
        store: 工程。
        job_id: 任务 ID。
        percent: 期望百分比。

    返回:
        命中的任务快照。

    异常:
        AssertionError: 超时仍未命中。
    """
    deadline = time.monotonic() + _WAIT_S
    while time.monotonic() < deadline:
        snap = media_bus.snapshot(store, job_id)
        if snap and snap.get("status") == "running" and snap.get("percent") == percent:
            return snap
        time.sleep(0.05)
    raise AssertionError(f"任务未达到 running/{percent}%: {media_bus.snapshot(store, job_id)}")


@pytest.fixture
def chapter_ch01() -> dict:
    """最小章节（含一条音效、一条非 i2v 视频片段）。"""
    return {
        "schema_version": 1,
        "id": "ch01",
        "title": "第一章",
        "revision": 1,
        "scenes": [
            {
                "id": "sc01",
                "lines": [],
                "sfx_events": [{"id": "sfx01", "prompt": "远处关门声"}],
                "key_images": [],
                "video_segments": [{"id": "v01", "mode": "t2v", "prompt": "空镜"}],
            }
        ],
    }


def test_all_actions_have_handlers() -> None:
    """登记表里的 12 个动作都必须有执行器。"""
    handlers = media_bus.registered_handlers()
    assert set(media_plan.ACTIONS) <= set(handlers)
    assert len(media_plan.ACTIONS) == 12


def test_tts_chapter_progress_and_cancel(store: ProjectStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """`tts.chapter` 把 done/total 转成 percent，取消后任务落成 canceled。"""
    options_seen: dict = {}

    def fake_run_batch(_store, _config, options):
        """模拟批次：先报一次 1/2 进度，再等取消。"""
        options_seen["options"] = options
        assert options.on_progress is not None, "执行器必须传 on_progress"
        assert options.should_cancel is not None, "执行器必须传 should_cancel"
        # 首次写盘有 1s 节流：先等过窗口，保证进度一定落盘再让用例读
        time.sleep(1.05)
        options.on_progress({"done": 1, "total": 2, "failed": 1, "current_chapter": "ch01"})
        while not options.should_cancel():
            time.sleep(0.01)
        return 0, {"stats": {"succeeded": 1, "failed": 1, "skipped": 0}, "reuse": {"reused": 0}}

    monkeypatch.setattr(media_handlers.tts_batch_run, "run_batch", fake_run_batch)
    job = media_bus.run(store, None, "tts.chapter", {"path": str(store.root), "chapter": "ch01"})
    running = _wait_running_percent(store, job["id"], 50)
    assert "1/2" in running["current"]

    canceled = media_bus.cancel(store, job["id"])
    assert canceled["status"] == "canceled"
    final = _wait_terminal(store, job["id"])
    assert final["status"] == "canceled"
    assert options_seen["options"].all_chapters is False


def test_sfx_item_success_writes_artifact(
    store: ProjectStore, chapter_ch01: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """单条音效成功时返回条目并写 artifacts。"""
    monkeypatch.setattr(store, "load_chapter", lambda _chapter_id: chapter_ch01)
    monkeypatch.setattr(store, "save_chapter", lambda _chapter: None)
    monkeypatch.setattr(media_handlers.sfx_adapter, "get_provider", lambda _config: ("offline", object()))

    def fake_generate(_store, _config, item, provider=None, provider_id=None):
        """把条目置为就绪并返回。"""
        item.update({"status": "ready", "asset_id": "assets/sfx/ch01/sfx01.wav"})
        return dict(item)

    monkeypatch.setattr(media_handlers.sfx_adapter, "generate_sfx_item", fake_generate)
    job = media_bus.run(
        store, None, "sfx.item", {"path": str(store.root), "chapter": "ch01", "sfx_id": "sfx01"}
    )
    snap = _wait_terminal(store, job["id"])
    assert snap["status"] == "succeeded", snap
    assert snap["artifacts"] == ["assets/sfx/ch01/sfx01.wav"]
    assert snap["meta"]["result"]["message"] == "音效已生成"


def test_bgm_success_writes_artifact(store: ProjectStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """配乐成功时返回时长文案并写 artifacts。"""
    monkeypatch.setattr(
        media_handlers.bgm_generate,
        "generate_bgm",
        lambda _store, _config, _params: {
            "asset_id": "assets/audio/bgm/ch01/bgm.wav",
            "duration_s": 30,
            "provider_requested": "local",
            "auto_switched": False,
        },
    )
    job = media_bus.run(
        store, None, "bgm", {"path": str(store.root), "chapter": "ch01", "duration": 30}
    )
    snap = _wait_terminal(store, job["id"])
    assert snap["status"] == "succeeded", snap
    assert snap["artifacts"] == ["assets/audio/bgm/ch01/bgm.wav"]
    assert snap["meta"]["result"]["message"] == "已生成 30s BGM"


def test_image_generate_success_collects_artifacts(store: ProjectStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """配图成功时只把 ready 条目的路径写 artifacts。"""
    monkeypatch.setattr(
        media_handlers.media_ops,
        "run_media_kind",
        lambda *_args, **_kwargs: {
            "kind": "image",
            "total": 2,
            "ready": 1,
            "skipped": 1,
            "failed": 0,
            "items": [
                {"id": "img01", "status": "ready", "path": "assets/images/ch01/img01.png"},
                {"id": "img02", "status": "skipped", "path": "assets/images/ch01/img02.png"},
            ],
        },
    )
    job = media_bus.run(
        store, None, "image.generate", {"path": str(store.root), "chapter": "ch01"}
    )
    snap = _wait_terminal(store, job["id"])
    assert snap["status"] == "succeeded", snap
    assert snap["artifacts"] == ["assets/images/ch01/img01.png"]
    assert "1 张图片" in snap["meta"]["result"]["message"]


def test_all_items_failed_lands_failed_with_reason(
    store: ProjectStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """全部条目失败时任务落 failed，不能显示成「已完成」。"""
    monkeypatch.setattr(
        media_handlers.media_ops,
        "run_media_kind",
        lambda *_args, **_kwargs: {
            "kind": "image",
            "total": 2,
            "ready": 0,
            "skipped": 0,
            "failed": 2,
            "items": [
                {"id": "img01", "status": "failed", "error": "ComfyUI 未连接"},
                {"id": "img02", "status": "failed", "error": "ComfyUI 未连接"},
            ],
        },
    )
    job = media_bus.run(
        store, None, "image.generate", {"path": str(store.root), "chapter": "ch01"}
    )
    snap = _wait_terminal(store, job["id"])
    assert snap["status"] == "failed", snap
    assert "全部 2 项生成失败" in snap["error"]
    assert "ComfyUI 未连接" in snap["error"]


def test_partial_items_land_partial(store: ProjectStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """有产出也有失败时任务落 partial，并保留汇总文案。"""
    monkeypatch.setattr(
        media_handlers.media_ops,
        "run_media_kind",
        lambda *_args, **_kwargs: {
            "kind": "image",
            "total": 2,
            "ready": 1,
            "skipped": 0,
            "failed": 1,
            "items": [
                {"id": "img01", "status": "ready", "path": "assets/images/ch01/img01.png"},
                {"id": "img02", "status": "failed", "error": "ComfyUI 超时"},
            ],
        },
    )
    job = media_bus.run(
        store, None, "image.generate", {"path": str(store.root), "chapter": "ch01"}
    )
    snap = _wait_terminal(store, job["id"])
    assert snap["status"] == "partial", snap
    assert "失败 1" in snap["current"]
    assert snap["artifacts"] == ["assets/images/ch01/img01.png"]


def test_failure_lands_failed_with_chinese_reason(store: ProjectStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """底层异常必须落成 failed，错误文案带「失败：」与原始中文原因。"""
    def boom(*_args, **_kwargs):
        """模拟 GPU 不可用。"""
        raise RuntimeError("GPU 不可用，请检查 ComfyUI")

    monkeypatch.setattr(media_handlers.media_ops, "run_media_kind", boom)
    job = media_bus.run(
        store, None, "image.generate", {"path": str(store.root), "chapter": "ch01"}
    )
    snap = _wait_terminal(store, job["id"])
    assert snap["status"] == "failed"
    assert "失败：" in snap["error"]
    assert "GPU 不可用" in snap["error"]


def test_anim_without_i2v_fails_with_reason(
    store: ProjectStore, chapter_ch01: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """本章没有 i2v 片段时 `anim` 失败并给出中文原因。"""
    monkeypatch.setattr(store, "load_chapter", lambda _chapter_id: chapter_ch01)
    job = media_bus.run(store, None, "anim", {"path": str(store.root), "chapter": "ch01"})
    snap = _wait_terminal(store, job["id"])
    assert snap["status"] == "failed"
    assert "本章没有图生视频条目" in snap["error"]


def test_removed_endpoints_are_gone(tmp_path: Path, store: ProjectStore) -> None:
    """旧的启动 / 轮询接口已删除（静态挂载会兜底成 404 或 405）。"""
    config = tmp_path / "offline.yaml"
    config.write_text("adapters:\n  writing: offline\n", encoding="utf-8")
    client = TestClient(create_app(project_root=str(store.root), config_path=str(config)))
    assert client.get("/api/ui/sfx/job").status_code in (404, 405)
    assert client.get("/api/ui/tts/batch-all/progress").status_code in (404, 405)
    assert client.post("/api/ui/line/rerun", json={}).status_code in (404, 405)
    assert client.get("/api/ui/voices/design/batches/missing").status_code in (404, 405)
