"""角色识别补跑：节拍已存在但从未同步角色时，正文生成前补一次 cast-only 调用。

覆盖：无绑定章节补跑后写标记、第二次出现才建卡、识别为空也写标记且提示、
旧版已绑定章节只补标记不调模型、补跑计划只列出未同步章节、反推大纲同样触发补跑。
运行：`.venv/bin/python -m pytest -q tests/test_cast_capture.py`。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from novel_drama.application.cast.capture_cast import (
    capture_chapter_cast,
    cast_capture_plan,
    cast_status_overview,
    ensure_chapter_cast,
    latest_capture_job,
    pending_overview,
    start_capture_job,
)
from novel_drama.application.cast.sync_cast import discard_pending, promote_pending
from novel_drama.application.story.apply_outline import apply_outline as apply_outline
from novel_drama.application.story.generate_outline import (
    generate_outline as generate_outline,
)
from novel_drama.application.story.summarize_chapter import apply_summary
from novel_drama.config import load_config
from novel_drama.models import entities
from novel_drama.store import ProjectStore
from novel_drama.store.settings_store import SettingsStore


class CastAdapter:
    """只返回固定 cast 的适配器（记录调用次数）。"""

    def __init__(self, cast: list[dict]) -> None:
        """绑定要返回的 cast 行。

        参数:
            cast: `emit_cast` 的返回行。
        """
        self.cast = cast
        self.calls = 0

    def generate_tool(self, req, tool, timeout_s=None, on_delta=None) -> dict:
        """返回固定 cast 并记录调用。

        参数:
            req: 请求（忽略）。
            tool: 工具定义（忽略）。
            timeout_s: 超时（忽略）。
            on_delta: 增量回调（忽略）。

        返回:
            `{"cast": [...]}`。
        """
        self.calls += 1
        return {"cast": list(self.cast)}


def _offline_config(tmp_path: Path):
    """生成最小离线配置。

    参数:
        tmp_path: 临时目录。

    返回:
        AppConfig。
    """
    path = tmp_path / "offline.yaml"
    path.write_text(
        "\n".join(
            [
                "adapters:",
                "  writing: offline",
                "  sfx: offline",
                "gpu:",
                f"  lock_path: {tmp_path / 'gpu.lock'}",
                "tts:",
                f"  hf_home: {tmp_path / 'hf'}",
                "  builtin_voices:",
                f"    root: {tmp_path / 'voices'}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return load_config(str(path), str(tmp_path / "missing.ui.yaml"))


def _project(tmp_path: Path):
    """建一个 1 幕 2 章（每章 3 拍）的工程，并还原成「节拍存在但从未同步角色」。

    参数:
        tmp_path: 临时目录。

    返回:
        `(store, config, chapters)`。
    """
    store = ProjectStore(tmp_path / "proj")
    store.create("角色捕获测试", author="ndw")
    config = _offline_config(tmp_path)
    generate_outline(store, config, acts=1, chapters_per_act=2, beats_per_chapter=3, chapter_words=600)
    apply_outline(store)

    table = store.load_characters()
    narrator = [item for item in table["characters"] if item.get("id") == entities.NARRATOR_ID]
    store.save_characters({"schema_version": 1, "characters": narrator})
    for name in ("cast_pending.json", "cast_created.json"):
        path = store.root / ".ndw" / name
        if path.exists():
            path.unlink()

    settings = SettingsStore(store.root)
    bible = settings.load()
    chapters: list[dict] = []
    for act in (bible.get("outline") or {}).get("acts") or []:
        for chapter in act.get("chapters") or []:
            chapters.append(chapter)
            for key in ("cast_synced", "cast_synced_at", "cast_created", "cast_reused", "cast_extras", "cast_pending"):
                chapter.pop(key, None)
            for beat in chapter.get("beats") or []:
                beat["character_ids"] = []
    settings.save(bible)
    return store, config, chapters


def _cast_rows(*names: str) -> list[dict]:
    """构造带节拍绑定的 cast 行。

    参数:
        names: 角色姓名。

    返回:
        cast 行列表。
    """
    return [{"name": name, "beats": [1], "role": "protagonist"} for name in names]


def test_capture_marks_chapter_and_creates_on_second_appearance(tmp_path: Path) -> None:
    """两章都未同步时：首章进待定，次章建卡；已同步章节不再重复调用。"""
    store, _config, chapters = _project(tmp_path)
    first, second = chapters[0]["id"], chapters[1]["id"]
    adapter = CastAdapter(_cast_rows("林九斤", "周也"))

    report = capture_chapter_cast(store, first, adapter=adapter)
    assert report["triggered"] and report["created"] == []
    assert sorted(report["pending"]) == ["周也", "林九斤"]
    assert "待定" in report["message"]

    bible = SettingsStore(store.root).load()
    entry = SettingsStore(store.root).find_outline_chapter(bible, first)
    assert entry["cast_synced"] is True
    assert sorted(entry["cast_pending"]) == ["周也", "林九斤"]

    report = capture_chapter_cast(store, second, adapter=adapter)
    assert report["triggered"] and sorted(report["created"]) == ["周也", "林九斤"]
    assert report["pending"] == []
    assert "新建 2 张卡" in report["message"]

    names = [item["name"] for item in store.load_characters()["characters"] if item["role"] != "narrator"]
    assert sorted(names) == ["周也", "林九斤"]

    calls = adapter.calls
    again = capture_chapter_cast(store, first, adapter=adapter)
    assert again["triggered"] is False and again["state"] == "synced"
    assert adapter.calls == calls


def test_empty_cast_still_marks_and_reports(tmp_path: Path) -> None:
    """识别为空也要写标记并明确提示，避免每次生成都多打一次调用。"""
    store, _config, chapters = _project(tmp_path)
    adapter = CastAdapter([])
    report = capture_chapter_cast(store, chapters[0]["id"], adapter=adapter)
    assert report["triggered"] and report["empty"] is True
    assert "未识别到" in report["message"]

    calls = adapter.calls
    capture_chapter_cast(store, chapters[0]["id"], adapter=adapter)
    assert adapter.calls == calls


def test_bound_legacy_chapter_only_marks_without_model_call(tmp_path: Path) -> None:
    """旧版已绑定现有角色的章节只补标记，不调用模型。"""
    store, _config, chapters = _project(tmp_path)
    table = store.load_characters()
    lead = entities.new_character("沈砚", role="protagonist")
    table["characters"].append(lead)
    store.save_characters(table)

    settings = SettingsStore(store.root)
    bible = settings.load()
    entry = settings.find_outline_chapter(bible, chapters[0]["id"])
    for beat in entry["beats"]:
        beat["character_ids"] = [lead["id"]]
    settings.save(bible)

    adapter = CastAdapter(_cast_rows("沈砚"))
    report = ensure_chapter_cast(store, None, chapters[0]["id"], adapter=adapter)
    assert report["triggered"] is False and report["state"] == "bound"
    assert adapter.calls == 0

    bible = SettingsStore(store.root).load()
    assert SettingsStore(store.root).find_outline_chapter(bible, chapters[0]["id"])["cast_synced"] is True


def test_capture_plan_lists_only_unsynced_chapters(tmp_path: Path) -> None:
    """补跑计划只列出未同步章节，并给出按章累加的预估耗时。"""
    store, _config, chapters = _project(tmp_path)
    plan = cast_capture_plan(store, act_id=(SettingsStore(store.root).load()["outline"]["acts"][0].get("id")))
    assert plan["count"] == 2 and plan["seconds"] == plan["seconds_per_chapter"] * 2

    capture_chapter_cast(store, chapters[0]["id"], adapter=CastAdapter([]))
    plan = cast_capture_plan(store)
    assert [row["id"] for row in plan["chapters"]] == [chapters[1]["id"]]


def test_capture_matches_entry_by_index_when_ids_diverged(tmp_path: Path) -> None:
    """设定库章节 ID 与章节文件不一致时，按序号兜底仍能补跑（旧工程常见）。"""
    store, _config, chapters = _project(tmp_path)
    settings = SettingsStore(store.root)
    bible = settings.load()
    entry = settings.find_outline_chapter(bible, chapters[0]["id"])
    entry["id"] = "chp_regenerated01"
    settings.save(bible)

    report = capture_chapter_cast(store, chapters[0]["id"], adapter=CastAdapter(_cast_rows("沈砚")))
    assert report["triggered"] is True and report["pending"] == ["沈砚"]
    bible = SettingsStore(store.root).load()
    assert SettingsStore(store.root).find_outline_chapter(bible, "", index=1)["cast_synced"] is True


def test_apply_summary_triggers_capture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """从既有正文反推大纲后同样走角色捕获，不只挂 POV。"""
    store, config, chapters = _project(tmp_path)
    chapter_id = chapters[0]["id"]
    called: dict = {}

    def fake_ensure(_store, _config, target_id, **_kwargs):
        """记录补跑调用并返回可读反馈。"""
        called["chapter"] = target_id
        return {"triggered": True, "message": "角色识别完成：识别到 1 个角色（沈砚）。"}

    monkeypatch.setattr("novel_drama.application.cast.capture_cast.ensure_chapter_cast", fake_ensure)
    preview = {
        "title": "新的章名",
        "synopsis": "反推摘要",
        "outline": "1. 新的推进",
        "beats": [{"summary": "新的推进", "turning_point": False}],
    }
    result = apply_summary(store, config, chapter_id, preview)
    assert called["chapter"] == chapter_id
    assert result["cast"]["message"].startswith("角色识别完成")
    assert any("角色识别完成" in item for item in result["warnings"])


def test_pending_overview_promote_and_discard(tmp_path: Path) -> None:
    """待定队列可读（次数/线索），建卡沿用角色字段，丢弃不影响已建角色。"""
    store, _config, chapters = _project(tmp_path)
    capture_chapter_cast(store, chapters[0]["id"], adapter=CastAdapter(_cast_rows("林九斤", "周也")))

    view = pending_overview(store)
    assert view["count"] == 2
    row = next(item for item in view["items"] if item["name"] == "林九斤")
    assert row["first_index"] == 1 and row["first_title"]
    assert row["occurrences"] >= 1 and row["chapter_count"] >= 1
    assert row["clue"]

    promoted = promote_pending(store, row["key"])
    assert promoted["created"] is True
    assert promoted["character"]["name"] == "林九斤"
    assert promoted["character"]["role"] in ("supporting", "protagonist", "antagonist", "minor")
    assert pending_overview(store)["count"] == 1
    with pytest.raises(ValueError):
        promote_pending(store, row["key"])

    other = pending_overview(store)["items"][0]
    assert discard_pending(store, other["key"])["discarded"] == other["name"]
    assert pending_overview(store)["count"] == 0
    names = [item["name"] for item in store.load_characters()["characters"] if item["role"] != "narrator"]
    assert names == ["林九斤"]


def test_cast_status_overview_matches_capture_plan(tmp_path: Path) -> None:
    """章节状态与补跑计划同一口径：补过就 synced，没补仍是 missing。"""
    store, _config, chapters = _project(tmp_path)
    overview = cast_status_overview(store)
    assert overview["counts"]["missing"] == 2
    assert {row["state"] for row in overview["chapters"]} == {"missing"}

    capture_chapter_cast(store, chapters[0]["id"], adapter=CastAdapter([]))
    overview = cast_status_overview(store)
    states = {row["id"]: row["state"] for row in overview["chapters"]}
    assert states[chapters[0]["id"]] == "synced"
    assert states[chapters[1]["id"]] == "missing"
    assert [row["id"] for row in cast_capture_plan(store)["chapters"]] == [chapters[1]["id"]]


def test_capture_works_for_unlanded_chapter(tmp_path: Path) -> None:
    """整幕手动补跑会覆盖尚未落成正文的章节：没有章节文件也能按大纲条目识别。"""
    store, _config, chapters = _project(tmp_path)
    store.chapter_path(chapters[1]["id"]).unlink()
    report = capture_chapter_cast(store, chapters[1]["id"], adapter=CastAdapter([]))
    assert report["triggered"] is True
    assert report["state"] == "captured"


def test_llm_plan_counts_and_thinking(tmp_path: Path) -> None:
    """统一确认弹窗的事实来自后端：次数、思考、耗时与取消能力都可解释。"""
    from novel_drama.web.llm_plan import llm_plan

    store, config, chapters = _project(tmp_path)
    chapter_id = chapters[0]["id"]
    single = llm_plan(
        store,
        config,
        "chapter.generate",
        {"chapter": chapter_id, "regen": True, "multi_turn": False},
    )
    assert single["calls"] == 1
    assert single["thinking_text"] in ("开启", "关闭", "部分步骤开启")
    assert "视章节长度而定" in single["seconds_text"]
    assert single["cancelable"] is True

    multi = llm_plan(
        store,
        config,
        "chapter.generate",
        {"chapter": chapter_id, "regen": True, "multi_turn": True, "beats_per_round": 2, "max_rounds": 8},
    )
    assert multi["calls"] == 2

    # 已有正文且只补缺失：后端判定不会调用模型，前端不弹提示
    chapter = store.load_chapter(chapter_id)
    chapter["word_count"] = 600
    store.save_chapter(chapter)
    skip = llm_plan(store, config, "chapter.generate", {"chapter": chapter_id, "regen": False})
    assert skip["calls"] == 0

    capture = llm_plan(store, config, "cast.capture", {"act_index": 1})
    assert capture["calls"] == 2
    assert capture["calls_text"] == "2 次"

    with pytest.raises(ValueError):
        llm_plan(store, config, "cast.check", {})


def test_llm_plan_endpoint(tmp_path: Path) -> None:
    """接口层：未登记动作返回 400，正常动作返回统一字段。"""
    from fastapi.testclient import TestClient

    from novel_drama.web.app import create_app

    store, _config, chapters = _project(tmp_path)
    client = TestClient(create_app(project_root=str(store.root), config_path=str(tmp_path / "offline.yaml")))
    base = {"path": str(store.root), "chapter": chapters[0]["id"], "regen": True}
    ok = client.get("/api/ui/llm/plan", params={**base, "action": "chapter.generate"})
    assert ok.status_code == 200
    assert ok.json()["calls"] == 1
    bad = client.get("/api/ui/llm/plan", params={"path": str(store.root), "action": "cast.check"})
    assert bad.status_code == 400


def test_manual_capture_job_runs_unsynced_chapters(tmp_path: Path) -> None:
    """手动补跑按幕内章节顺序执行，逐章给出可读结果，完成后不再需要补跑。"""
    store, config, _chapters = _project(tmp_path)
    job = start_capture_job(store, config, act_index=1)
    assert job["total"] == 2 and job["status"] == "running"

    deadline = time.time() + 15
    while time.time() < deadline:
        job = latest_capture_job(store)
        if job.get("status") != "running":
            break
        time.sleep(0.05)
    assert job["status"] == "succeeded"
    assert [item["status"] for item in job["items"]] == ["captured", "captured"]
    assert all("未识别到" in item["message"] for item in job["items"])
    assert cast_capture_plan(store)["count"] == 0
    with pytest.raises(ValueError):
        start_capture_job(store, config, act_index=1)


def test_cast_pending_and_status_endpoints(tmp_path: Path) -> None:
    """接口层：待定队列可读、可建卡；状态口径与补跑计划一致；手动补跑可启动。"""
    from fastapi.testclient import TestClient

    from novel_drama.web.app import create_app

    store, _config, chapters = _project(tmp_path)
    capture_chapter_cast(store, chapters[0]["id"], adapter=CastAdapter(_cast_rows("林九斤")))
    client = TestClient(create_app(project_root=str(store.root), config_path=str(tmp_path / "offline.yaml")))
    params = {"path": str(store.root)}

    pending = client.get("/api/ui/cast/pending", params=params).json()
    assert pending["count"] == 1
    key = pending["items"][0]["key"]
    promoted = client.post("/api/ui/cast/pending/promote", json={**params, "key": key}).json()
    assert promoted["created"] is True and promoted["character"]["name"] == "林九斤"

    status = client.get("/api/ui/cast/status", params=params).json()
    assert status["counts"]["synced"] == 1 and status["counts"]["missing"] == 1

    started = client.post("/api/ui/cast/capture/start", json={**params, "chapter": chapters[1]["id"]}).json()
    assert started["job"]["total"] == 1 and started["job"]["scope"] == "chapter"
