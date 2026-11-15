"""正文不足时的「补写」动作（离线用例）。

覆盖：
- 正文明显不足（低于目标 0.7 倍）时给出补写动作；达到阈值时不提供动作。
- 补写只追加不改写：原有正文完整保留，新增字数与要点清单写进结果。
- 补写结果缺少要求的要点时拒绝写入。

运行：`.venv/bin/python -m pytest tests/test_chapter_supplement.py -v`。
"""

from __future__ import annotations

import pytest
from test_chapter_resume import InlineSummaryAdapter, _offline, _patch_adapter, _project

from novel_drama.application.story import chapter_checkpoint as chapter_checkpoint
from novel_drama.application.story.chapter_limits import UNDERFILL_ACTION_RATIO
from novel_drama.application.story.chapter_text import _count_words as _count_words
from novel_drama.application.story.chapter_text import (
    _split_supplement_sections,
    _uncovered_beats,
    _word_deviation,
)
from novel_drama.application.story.generate_chapter import (
    generate_chapter as generate_chapter,
)

#: 与本章节拍无关的短正文：用来模拟「明显不足且要点没写到」
SHORT_TEXT = (
    "雨停在半夜，巷子里的水还在往低处流。\n\n"
    "他靠着墙站了一会儿，听见远处有人关门，铁链拖过地面。\n\n"
    "灯灭之后，整条街只剩水滴落在铁皮上的声音。"
)


def test_underfill_action_threshold() -> None:
    """低于 0.7 倍给补写动作；达到阈值不给动作。"""
    short = _word_deviation(500, 1000)
    assert short["level"] == "action"
    assert [item["id"] for item in short["actions"]] == ["supplement"]
    assert short["under_ratio"] == UNDERFILL_ACTION_RATIO
    assert "补写" in short["message"]

    enough = _word_deviation(750, 1000)
    assert enough["level"] == "none" and enough["actions"] == []


def test_split_supplement_sections_rejects_missing() -> None:
    """补写结果缺少要求的要点时必须报中文原因。"""
    body, error = _split_supplement_sections("【补写节拍 1】写了一点。", [1, 2])
    assert body == "" and "第 2 个节拍要点" in error
    body, error = _split_supplement_sections("【补写节拍 1】一。\n\n【补写节拍 2】二。", [1, 2])
    assert error is None and "一。" in body and "二。" in body


def test_uncovered_beats_detects_missing() -> None:
    """正文没写到的节拍会被识别为待补要点。"""
    beats = [
        {"summary": "沈砚在旧档案楼找到第一份卷宗"},
        {"summary": "林见夏在长明街市被中间人跟踪"},
    ]
    rows = _uncovered_beats("雨停了，巷子里没有人。", beats)
    assert [number for number, _beat in rows] == [1, 2]
    covered = _uncovered_beats("沈砚在旧档案楼找到第一份卷宗，林见夏在长明街市被中间人跟踪。", beats)
    assert covered == []


def test_supplement_appends_and_reports(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """补写按缺口续写并合并，结果写清新增字数与补的要点。"""
    store, config, chapter = _project(tmp_path)
    _patch_adapter(monkeypatch, InlineSummaryAdapter(_offline()))
    generate_chapter(store, config, chapter["id"])

    # 把正文换成语义明显不足的短文本（连接点前的正常状态保留）
    saved = store.load_chapter(chapter["id"])
    saved["content"] = SHORT_TEXT + "\n"
    saved["word_count"] = _count_words(SHORT_TEXT)
    store.save_chapter(saved)
    before_revision = saved["revision"]
    before_words = saved["word_count"]

    result = generate_chapter(store, config, chapter["id"], mode="supplement")

    assert result["mode"] == "supplement"
    assert result["before_words"] == before_words
    assert result["added_words"] > 0
    assert result["word_count"] == before_words + result["added_words"]
    assert result["gap_words"] > 0
    assert 0 < result["added_words"] <= result["gap_words"] * 2
    assert result["supplemented_beats"], "应列出补写覆盖的要点"
    assert "补写" in result["overshoot_note"] and "新增" in result["overshoot_note"]
    assert all(item["summary"] for item in result["supplemented_beats"])

    after = store.load_chapter(chapter["id"])
    assert after["content"].startswith(SHORT_TEXT)
    assert "【补写节拍" not in after["content"]
    assert after["revision"] == before_revision + 1
    assert not chapter_checkpoint.checkpoint_path(store, chapter["id"]).exists()


def test_supplement_rejects_when_enough(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """正文达到补写阈值时拒绝执行，正文不变。"""
    store, config, chapter = _project(tmp_path)
    _patch_adapter(monkeypatch, InlineSummaryAdapter(_offline()))
    generate_chapter(store, config, chapter["id"])
    before = store.load_chapter(chapter["id"])

    with pytest.raises(ValueError) as exc:
        generate_chapter(store, config, chapter["id"], mode="supplement")
    assert "补写阈值" in str(exc.value)

    after = store.load_chapter(chapter["id"])
    assert after["content"] == before["content"]
    assert after["revision"] == before["revision"]
