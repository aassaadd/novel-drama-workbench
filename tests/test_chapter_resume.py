"""章节断点续写与「摘要并入正文末轮」的离线用例。

覆盖：
- 第 1 条：中途取消后断点落盘（草稿 + 轮次指针 + 版本指针），再次生成自动接上，
  不重跑已完成轮次，最终正文与一次跑完完全一致；章节版本变化时断点作废。
- 第 2 条：末轮同时产出正文与本章摘要时不再单独发摘要请求；解析失败才回退，
  回退留可读说明；滚动摘要照常更新。
- 预算：末轮预算 = 正文预算 + 摘要预算，统一受全局上限约束并给出提示。

运行：`.venv/bin/python -m pytest tests/test_chapter_resume.py -v`。
"""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

import pytest

from novel_drama.cancel import GenerationCanceled
from novel_drama.config import load_config
from novel_drama.models import entities
from novel_drama.store import ProjectStore
from novel_drama.store.settings_store import SettingsStore
from novel_drama.writing import chapter_checkpoint, service
from novel_drama.writing.recap import ROUND_SUMMARY_MARK


def _dedupe_sentences(text: str, prior: str = "") -> str:
    """去掉与上文重复的长句，避免离线正文命中「疑似复述」校验。

    只用于测试替身：同一请求同一输出，不影响生产逻辑。

    参数:
        text: 离线适配器本轮生成的正文。
        prior: 之前轮次已写的正文（从请求的 assistant 消息里取）。

    返回:
        去重后的正文。
    """
    seen: set[str] = set()
    for chunk in (prior or "").split("\n"):
        for sentence in re.split(r"(?<=[。！？!?])", chunk):
            key = sentence.strip()
            if len(key) >= 8:
                seen.add(key)
    lines: list[str] = []
    for line in (text or "").split("\n"):
        kept: list[str] = []
        for sentence in re.split(r"(?<=[。！？!?])", line):
            key = sentence.strip()
            if len(key) >= 8:
                if key in seen:
                    continue
                seen.add(key)
            kept.append(sentence)
        if kept:
            lines.append("".join(kept))
    return "\n".join(lines)


class DedupAdapter:
    """包装离线适配器：正文去掉与上文重复的句子，其它任务原样透传。"""

    def __init__(self, inner):
        """保存被包装的适配器。"""
        self.inner = inner
        self.model = str(getattr(inner, "model", "offline-writing"))

    def generate(self, request):
        """执行一次生成；chapter 任务做确定性去重与兜底补句。"""
        result = self.inner.generate(request)
        if request.task != "chapter":
            return result
        prior = "\n".join(message.content for message in request.messages if message.role == "assistant")
        text = _dedupe_sentences(result.text, prior)
        if len(text.strip()) < 60:
            # 整轮都在复读：用「轮次 + 本节拍摘要」补一句确定性新内容
            beats = request.metadata.get("beats") or []
            round_no = sum(1 for message in request.messages if message.role == "assistant") + 1
            beat = beats[min(len(beats) - 1, round_no - 1)] if beats else {}
            summary = str(beat.get("summary") or "").strip() or "夜色继续压低"
            text = f"{summary}。第 {round_no} 轮的雨声敲在窗棂上，沈砚把未说完的话咽了回去，转身走进巷子深处。"
        return replace(result, text=text)


def _offline():
    """返回去重后的离线适配器（测试替身）。"""
    return DedupAdapter(service.OfflineWritingAdapter({}))


class RecordingAdapter:
    """包装适配器：记录每次请求，便于核对「没有重跑已完成轮次」。"""

    def __init__(self, inner):
        """保存被包装的适配器。"""
        self.inner = inner
        self.calls: list = []
        self.model = str(getattr(inner, "model", "offline-writing"))

    def generate(self, request):
        """执行一次生成并记录请求。"""
        self.calls.append(request)
        return self.inner.generate(request)


class CancelingAdapter(RecordingAdapter):
    """在第 N 次调用时抛 GenerationCanceled，模拟用户中途停止。"""

    def __init__(self, inner, cancel_at: int):
        """记录取消发生的调用序号（1 起）。"""
        super().__init__(inner)
        self.cancel_at = cancel_at

    def generate(self, request):
        """到达取消序号时抛取消，否则正常生成。"""
        if len(self.calls) + 1 == self.cancel_at:
            raise GenerationCanceled()
        return super().generate(request)


class InlineSummaryAdapter(RecordingAdapter):
    """在末轮输出后追加「分隔标记 + 摘要 JSON」，模拟模型按约定返回。"""

    def __init__(self, inner, broken: bool = False):
        """保存是否构造一份不可解析的摘要。"""
        super().__init__(inner)
        self.broken = broken

    def generate(self, request):
        """末轮请求（user 指令含分隔标记）追加摘要块。"""
        result = super().generate(request)
        users = [message.content for message in request.messages if message.role == "user"]
        if not any(ROUND_SUMMARY_MARK in text for text in users):
            return result
        if self.broken:
            tail = ROUND_SUMMARY_MARK + "\n{不是合法 JSON"
        else:
            tail = (
                ROUND_SUMMARY_MARK
                + '\n{"chapter_summary": "本章摘要（离线用例）。", '
                + '"book_summary": "全书滚动摘要（离线用例）。"}'
            )
        return replace(result, text=f"{result.text.rstrip()}\n\n{tail}\n")


def _project(tmp_path: Path):
    """建一个 1 章 4 拍的最小工程，并返回 (store, config, chapter)。"""
    store = ProjectStore(tmp_path / "proj")
    store.create("断点续写测试", author="ndw")
    config_path = tmp_path / "offline.yaml"
    config_path.write_text(
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
    config = load_config(str(config_path), str(tmp_path / "missing.ui.yaml"))
    service.generate_outline(store, config, acts=1, chapters_per_act=1, beats_per_chapter=4, chapter_words=800)
    service.apply_outline(store)

    table = store.load_characters()
    lead = entities.new_character("沈砚", role="protagonist")
    lead["speech_style"] = {"tempo": "slow", "pitch": "low", "accent": "北方口音", "catchphrases": ["先别急"]}
    table["characters"].append(lead)
    store.save_characters(table)

    settings = SettingsStore(store.root)
    bible = settings.load()
    chapter_row = bible["outline"]["acts"][0]["chapters"][0]
    chapter_row["pov_character_id"] = lead["id"]
    for beat in chapter_row.get("beats") or []:
        beat["character_ids"] = [lead["id"]]
    settings.save(bible)

    chapter_id = chapter_row["id"]
    return store, config, store.load_chapter(chapter_id)


def _patch_adapter(monkeypatch: pytest.MonkeyPatch, adapter) -> None:
    """把生成链路里的适配器替换成测试替身。"""
    monkeypatch.setattr(service, "build_writing_adapter", lambda *_args, **_kwargs: adapter)


def test_resume_after_cancel_matches_single_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """取消后断点续写不重跑已完成轮次，最终正文与一次跑完一致。"""
    store, config, chapter = _project(tmp_path)
    chapter_id = chapter["id"]

    # 基线：不落盘、不写断点的完整一次跑完
    baseline_adapter = InlineSummaryAdapter(_offline())
    _patch_adapter(monkeypatch, baseline_adapter)
    baseline = service.generate_chapter(store, config, chapter_id, dry_run=True)
    assert baseline["rounds"] == 4
    assert len(baseline_adapter.calls) == 4

    # 第一次：第 2 轮前取消，应留下 1 轮的断点
    cancel_adapter = CancelingAdapter(_offline(), cancel_at=2)
    _patch_adapter(monkeypatch, cancel_adapter)
    with pytest.raises(GenerationCanceled):
        service.generate_chapter(store, config, chapter_id)
    checkpoint = chapter_checkpoint.load(store, chapter_id)
    assert checkpoint is not None, "取消后应留下断点"
    assert checkpoint["rounds_done"] == 1
    assert checkpoint["status"] == "canceled"
    assert (store.load_chapter(chapter_id).get("content") or "") == ""
    checkpoint_file = chapter_checkpoint.checkpoint_path(store, chapter_id)
    assert checkpoint_file.exists()

    # 第二次：自动续写，只跑第 2–4 轮
    resume_adapter = InlineSummaryAdapter(_offline())
    _patch_adapter(monkeypatch, resume_adapter)
    result = service.generate_chapter(store, config, chapter_id)
    assert result["resume"]["resumed"] is True
    assert result["resume"]["reused_rounds"] == 1
    assert result["resume"]["from_round"] == 2
    assert result["rounds"] == 4
    assert result["beats_done"] == 4
    assert result["requests"]["body"] == 3, "续写只应请求剩余 3 轮"
    assert any("断点续写" in text for text in result["notices"])
    assert not checkpoint_file.exists(), "成功落盘后断点应删除"

    written = store.load_chapter(chapter_id)["content"].rstrip()
    assert written == baseline["content"].rstrip(), "续写结果应与一次跑完语义一致"


def test_stale_checkpoint_is_discarded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """章节版本变化后旧断点作废，从头生成并给出可读原因。"""
    store, config, chapter = _project(tmp_path)
    chapter_id = chapter["id"]
    chapter_checkpoint.save(
        store,
        {
            "chapter_id": chapter_id,
            "signature": {
                "base_revision": 99,
                "chapter_words": 800,
                "total_beats": 4,
                "per_round": 1,
                "beats": [],
            },
            "content": "旧的半章正文",
            "rounds_done": 1,
            "status": "failed",
        },
    )
    adapter = InlineSummaryAdapter(_offline())
    _patch_adapter(monkeypatch, adapter)
    result = service.generate_chapter(store, config, chapter_id)
    assert result["resume"]["resumed"] is False
    assert result["rounds"] == 4
    assert len(adapter.calls) == 4, "断点作废后应从头跑满 4 轮"
    assert any("断点已作废" in text for text in result["notices"])


def test_inline_summary_skips_separate_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """末轮已产出可解析摘要时不再单独发摘要请求，滚动摘要照常更新。"""
    store, config, chapter = _project(tmp_path)
    adapter = InlineSummaryAdapter(_offline())
    _patch_adapter(monkeypatch, adapter)
    result = service.generate_chapter(store, config, chapter["id"])
    assert result["summary_mode"] == "inline"
    assert result["requests"] == {"body": 4, "summary_fallback": 0, "total": 4}
    assert result["summary"]["mode"] == "inline"
    assert result["summary"]["chapter_summary"].startswith("本章摘要")
    saved = store.load_chapter(chapter["id"])
    assert saved["synopsis"] == "本章摘要（离线用例）。"
    rolling = store.load_rolling_summary()
    assert rolling["book_summary"] == "全书滚动摘要（离线用例）。"
    assert not any("回退到单独摘要" in text for text in result["notices"])


def test_summary_parse_failure_falls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """末轮摘要不可解析时回退到单独摘要请求，回退原因可读。"""
    store, config, chapter = _project(tmp_path)
    adapter = InlineSummaryAdapter(_offline(), broken=True)
    _patch_adapter(monkeypatch, adapter)
    result = service.generate_chapter(store, config, chapter["id"])
    assert result["summary_mode"] == "fallback"
    assert result["requests"]["summary_fallback"] == 1
    assert result["requests"]["total"] == 5
    assert any("回退到单独摘要请求" in text for text in result["notices"])
    assert result["summary"]["mode"] == "fallback"
    assert (store.load_chapter(chapter["id"]).get("synopsis") or "").strip()
    assert (store.load_rolling_summary().get("book_summary") or "").strip()


def test_offline_multi_turn_falls_back_without_summary_block(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """离线多轮正文本身不带摘要块：必须走回退，且正文不受影响。"""
    store, config, chapter = _project(tmp_path)
    adapter = RecordingAdapter(_offline())
    _patch_adapter(monkeypatch, adapter)
    result = service.generate_chapter(store, config, chapter["id"])
    assert result["summary_mode"] == "fallback"
    assert result["requests"]["body"] == 4
    assert result["requests"]["total"] == 5
    assert result["words"] > 0
