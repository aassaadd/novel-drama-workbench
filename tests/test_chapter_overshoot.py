"""章节超纲处理（压缩 / 重写）的离线用例。

覆盖：
- 阈值口径：1.5 倍以内只标字数；>1.5 提示；≥2 倍才给出两个动作。
- 压缩：按「【节拍 N】」逐段校验，节拍一个都不能少；落盘后版本 +1，并留下前后字数说明。
- 压缩缺节拍：拒绝写入，正文不变。
- 重写：结果带「重写前 / 重写后」字数说明。

运行：`.venv/bin/python -m pytest tests/test_chapter_overshoot.py -v`。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from test_chapter_resume import (
    InlineSummaryAdapter,
    RecordingAdapter,
    _offline,
    _patch_adapter,
    _project,
)

from novel_drama.adapters.base import AdapterError
from novel_drama.application.story import chapter_checkpoint as chapter_checkpoint
from novel_drama.application.story.chapter_limits import (
    OVERSHOOT_ACTION_RATIO,
    OVERSHOOT_NOTICE_RATIO,
)
from novel_drama.application.story.chapter_text import _count_words as _count_words
from novel_drama.application.story.chapter_text import _word_deviation
from novel_drama.application.story.generate_chapter import (
    generate_chapter as generate_chapter,
)


class MissingBeatAdapter(RecordingAdapter):
    """压缩时故意少写一个节拍，验证「不能靠删节拍压缩」的兜底。"""

    def generate(self, request):
        """压缩任务只返回第 1 个节拍。"""
        result = super().generate(request)
        if request.task != "chapter_compress":
            return result
        return replace(result, text="【节拍 1】只写了第一个节拍，其余节拍被省略。")


def test_deviation_thresholds() -> None:
    """阈值集中在 service：1.5 倍只提示、2 倍才给动作。"""
    inside = _word_deviation(1500, 1000)
    assert inside["level"] == "none" and inside["actions"] == []
    assert inside["notice_ratio"] == OVERSHOOT_NOTICE_RATIO
    assert "1.5 倍以内" in inside["message"]

    notice = _word_deviation(1700, 1000)
    assert notice["level"] == "notice" and notice["actions"] == []
    assert "已超纲" in notice["message"]

    action = _word_deviation(2000, 1000)
    assert action["level"] == "action"
    assert [item["id"] for item in action["actions"]] == ["compress", "rewrite"]
    assert action["action_ratio"] == OVERSHOOT_ACTION_RATIO
    assert "1.5 倍仅提示" in action["message"] and "2.0 倍及以上" in action["message"]


def _long_chapter(store, chapter_id: str, times: int = 3) -> dict:
    """把已有正文放大若干倍，模拟明显超纲；返回更新后的章节。"""
    chapter = store.load_chapter(chapter_id)
    content = (chapter.get("content") or "").strip()
    assert content
    chapter["content"] = "\n\n".join([content] * times)
    chapter["word_count"] = _count_words(chapter["content"])
    store.save_chapter(chapter)
    return store.load_chapter(chapter_id)


def test_compress_keeps_every_beat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """压缩必须逐节拍保留，落盘后版本 +1，说明里写清前后字数。"""
    store, config, chapter = _project(tmp_path)
    adapter = InlineSummaryAdapter(_offline())
    _patch_adapter(monkeypatch, adapter)
    generate_chapter(store, config, chapter["id"])
    long_chapter = _long_chapter(store, chapter["id"])
    before_words = long_chapter["word_count"]
    before_revision = long_chapter["revision"]
    # 伪造一个旧断点：压缩替换正文后必须作废，避免下次续写覆盖压缩结果
    chapter_checkpoint.save(
        store,
        {
            "chapter_id": chapter["id"],
            "signature": {"base_revision": before_revision},
            "content": "旧的半章正文",
            "rounds_done": 1,
            "status": "canceled",
        },
    )

    result = generate_chapter(store, config, chapter["id"], mode="compress")
    assert result["mode"] == "compress"
    assert result["before_words"] == before_words
    assert result["word_count"] < before_words
    assert "按目标字数压缩" in result["overshoot_note"]
    assert str(before_words) in result["overshoot_note"]
    assert str(result["word_count"]) in result["overshoot_note"]

    saved = store.load_chapter(chapter["id"])
    assert saved["revision"] == before_revision + 1
    assert "【节拍" not in saved["content"]
    assert not chapter_checkpoint.checkpoint_path(store, chapter["id"]).exists()
    for beat in _beats(store, chapter["id"]):
        assert beat["summary"] in saved["content"], f"压缩后丢失节拍：{beat['summary']}"


def _beats(store, chapter_id: str) -> list[dict]:
    """从设定库大纲里取本章节拍。"""
    from novel_drama.store.settings_store import SettingsStore

    bible = SettingsStore(store.root).load()
    for act in (bible.get("outline") or {}).get("acts") or []:
        for chapter in act.get("chapters") or []:
            if chapter.get("id") == chapter_id:
                return chapter.get("beats") or []
    return []


def test_compress_rejects_missing_beats(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """压缩结果缺节拍时拒绝写入，正文与版本保持不变。"""
    store, config, chapter = _project(tmp_path)
    adapter = InlineSummaryAdapter(_offline())
    _patch_adapter(monkeypatch, adapter)
    generate_chapter(store, config, chapter["id"])
    long_chapter = _long_chapter(store, chapter["id"])
    before = store.load_chapter(chapter["id"])

    _patch_adapter(monkeypatch, MissingBeatAdapter(_offline()))
    with pytest.raises(AdapterError) as exc:
        generate_chapter(store, config, chapter["id"], mode="compress")
    assert "不能靠删节拍" in exc.value.message

    after = store.load_chapter(chapter["id"])
    assert after["content"] == before["content"]
    assert after["revision"] == before["revision"]
    assert long_chapter["word_count"] == before["word_count"]


def test_rewrite_reports_before_and_after(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """重写结果写清原因与前后字数，并正常覆盖正文。"""
    store, config, chapter = _project(tmp_path)
    adapter = InlineSummaryAdapter(_offline())
    _patch_adapter(monkeypatch, adapter)
    generate_chapter(store, config, chapter["id"])
    long_chapter = _long_chapter(store, chapter["id"], times=2)
    before_words = long_chapter["word_count"]
    before_revision = long_chapter["revision"]

    result = generate_chapter(store, config, chapter["id"], mode="rewrite")
    assert result["mode"] == "rewrite"
    assert "已按「重写」处理" in result["overshoot_note"]
    assert str(before_words) in result["overshoot_note"]
    assert str(result["word_count"]) in result["overshoot_note"]
    saved = store.load_chapter(chapter["id"])
    assert saved["revision"] == before_revision + 1
    assert _count_words(saved["content"]) == result["word_count"]
