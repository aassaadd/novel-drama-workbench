"""退化熔断与看门狗的离线用例。

覆盖：
- 阈值口径：同一段连续重复 3 次 / 12 字片段覆盖率 30% / 同句 3 次，任一命中即止损；
  判定窗口按 `max(1.5 × 目标字数, 400)` 推导。
- 正常文本不误杀。
- 命中后先局部续写（截到循环起点前一句），收敛后正常落盘。
- 局部续写 2 次仍不收敛时整节重写 1 次；再失败报错且不落盘。
- 看门狗：不同目标字数的值、末轮加摘要额度、被上下文压住时取剩余空间、
  命中后已写正文不丢。

运行：`.venv/bin/python -m pytest tests/test_degeneration.py -v`。
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from test_chapter_resume import (
    InlineSummaryAdapter,
    RecordingAdapter,
    _offline,
    _patch_adapter,
    _project,
)

from novel_drama.adapters.base import AdapterError, Message, TextGenRequest, TextGenResult
from novel_drama.writing import chapter_checkpoint, service
from novel_drama.writing.degeneration import (
    DegenerationGuard,
    SENTENCE_STOP_COUNT,
    SHINGLE_COVERAGE_STOP,
    SUFFIX_REPEATS,
    resolve_window,
    split_sentences,
)
from novel_drama.writing.recap import ROUND_SUMMARY_MARK
from novel_drama.writing.watchdog import apply_context_limit, describe, watchdog_tokens

#: 线上现象的对白循环（整段反复）
LOOP_TEXT = "「你有。」周也说。「我没有！」九斤说。" * 40

#: 正常正文（不应触发任何退化信号）
NORMAL_TEXT = (
    "雨从傍晚就开始下，屋檐下的水声一直没有停。他把外衣挂在门后，走到桌边坐下，手指在杯沿上停了很久。"
    "窗外的街灯被水汽泡得发白，远处传来一阵很轻的车声。她推门进来时带进一股凉气，头发上还沾着细小的水珠。"
    "两人都没有先开口，屋子里只剩钟摆来回走动的声音。他终于抬起头，问了一句路上顺不顺利。"
    "她说还好，只是桥那头堵了很久。他把杯子往她那边推了推，说先暖暖手。"
    "她低头看着杯里的热气，忽然问他是不是又整夜没睡。他没有否认，只把窗户推开一条缝。"
    "风挤进来，把桌上的纸角掀起又落下。她说这样下去不是办法，他说他知道。"
)


def test_window_derived_from_target_chars() -> None:
    """判定窗口按 1.5 倍目标字数推导，并有 400 / 2000 的上下限。"""
    assert resolve_window(0) == 400
    assert resolve_window(100) == 400
    assert resolve_window(1200) == 1800
    assert resolve_window(5000) == 2000


def test_guard_detects_suffix_loop() -> None:
    """同一段连续重复 3 次即命中，并要求截到循环起点前一句。"""
    guard = DegenerationGuard(1200)
    reason = guard.feed(LOOP_TEXT)
    assert reason and f"连续重复 {SUFFIX_REPEATS} 次" in reason
    exc = guard.exception()
    assert 0 <= exc.keep_index <= exc.loop_start < len(guard.text)


def test_guard_detects_sentence_repeat_and_shingle() -> None:
    """同句出现 3 次、12 字片段覆盖率超阈值都能单独触发。"""
    repeated_sentence = "他说这话的时候没有看任何人。"
    # 用来自正常长文的填充把覆盖率压到阈值以下，确保命中的是「同句 3 次」
    filler = [
        "雨声压下来，檐下的水线连成一片。",
        "她把伞收起来，靠在门框边，水顺着伞骨流到砖缝里。",
        "桌上的灯晃了一下，光圈落在摊开的纸页上。",
        "楼道里有人走过，脚步在拐角处停了停。",
        "风从窗缝里挤进来，把纸角掀起又落下。",
        "他数着杯壁上那圈浅浅的茶印，没有说话。",
        "远处有人在关门，铁链拖过地面，声音很闷。",
        "她低头看着自己的手，指尖还沾着雨水。",
    ]
    sentence_guard = DegenerationGuard(1200)
    text = (
        filler[0] + repeated_sentence + filler[1] + filler[2] + repeated_sentence
        + filler[3] + filler[4] + filler[5] + repeated_sentence + filler[6] + filler[7]
    )
    reason = sentence_guard.feed(text) or sentence_guard.check_all()
    assert reason and f"同一句出现 {SENTENCE_STOP_COUNT} 次" in reason

    shingle_guard = DegenerationGuard(1200)
    # 不是首尾周期重复，但同一 12 字片段在窗口内出现 3 次、覆盖占比超阈值
    block = "他把窗推开一条缝，湿冷的风挤进来。"
    reason = shingle_guard.feed(
        "夜里很安静。" + block + "她放下杯子。" + block + "楼道里有人走过。" + block + "天亮之前谁都没再开口。"
    ) or shingle_guard.check_all()
    assert reason and "覆盖率" in reason
    assert SHINGLE_COVERAGE_STOP == 0.30


def test_normal_text_is_not_killed() -> None:
    """正常长文不触发熔断。"""
    guard = DegenerationGuard(1200)
    assert guard.feed(NORMAL_TEXT) is None
    assert guard.check_all() is None
    assert len(split_sentences(NORMAL_TEXT)) > 6


def test_watchdog_values_and_context_clamp() -> None:
    """看门狗按目标字数动态计算，末轮加摘要额度，并与剩余上下文取小。"""
    assert watchdog_tokens(800) == 1170  # ceil(800 × 1.65 × 0.886)
    # 末轮同时出摘要：额度更大
    assert watchdog_tokens(800, True) > watchdog_tokens(800)
    # 目标越大，看门狗越大
    assert watchdog_tokens(2000) > watchdog_tokens(800)

    value, clamped = apply_context_limit(1170, 800)
    assert value == 800 and clamped is True
    value, clamped = apply_context_limit(1170, None)
    assert value == 1170 and clamped is False

    text = describe(800, True, 800, 800)
    assert "被上下文压住" in text and "止损线" in text


class LoopThenNormalAdapter(RecordingAdapter):
    """第一次输出对白循环，收到续写指令后输出正常正文；非正文任务交给内层。"""

    def generate(self, request: TextGenRequest) -> TextGenResult:
        """首轮返回对白循环；续写指令与后续轮次返回正常正文。"""
        result = super().generate(request)
        if request.task != "chapter":
            return result
        users = [message.content for message in request.messages if message.role == "user"]
        if any(ROUND_SUMMARY_MARK in text for text in users):
            # 末轮：沿用内层输出（内层会追加摘要 JSON 块）
            return result
        last_user = users[-1] if users else ""
        if "继续写第" in last_user or "重复退化" in last_user or "看门狗" in last_user:
            text = self._body(f"续写{len(users)}")
        else:
            text = LOOP_TEXT
        return replace(result, text=text)

    @staticmethod
    def _body(tag: str) -> str:
        """构造不含重复句、不含重复片段的确定性正文。"""
        return "\n\n".join(
            [
                f"{tag}，他数着檐下的水滴，一颗接一颗砸在石阶上。",
                "风从巷口灌进来，把晾着的衣服吹得贴住墙。",
                "她把手里的伞收起，水顺着伞骨流到脚边。",
                "远处有人推开窗，灯光先在墙上晃了一下才照出来。",
                "两个人谁也没先提那件事，只听着楼道里的脚步远去。",
                "他终于说明天一早去旧档案楼看看。",
            ]
        )


class AlwaysLoopAdapter(RecordingAdapter):
    """每一轮都返回对白循环，用于验证「重写后仍重复即失败且不落盘」。"""

    def generate(self, request: TextGenRequest) -> TextGenResult:
        """始终返回重复对白。"""
        super().generate(request)
        return TextGenResult(text=LOOP_TEXT, finish_reason="stop", model="fake")


def test_local_continuation_keeps_writing(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """命中退化后先截断再局部续写，收敛后正常落盘。"""
    store, config, chapter = _project(tmp_path)
    adapter = LoopThenNormalAdapter(InlineSummaryAdapter(_offline()))
    _patch_adapter(monkeypatch, adapter)

    notices: list[str] = []
    result = service.generate_chapter(store, config, chapter["id"], on_notice=notices.append)

    saved = store.load_chapter(chapter["id"])
    assert "我没有！" not in saved["content"]
    assert "续写" in saved["content"]
    assert any("局部续写" in text for text in notices)
    # 首轮 1 次 + 局部续写 1 次 + 其余 3 轮 = 5 次
    assert len(adapter.calls) == 5
    assert result["rounds"] == 4


def test_rewrite_failure_does_not_write(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """局部续写 2 次 + 整节重写 1 次仍重复时报错，且正文不落盘。"""
    store, config, chapter = _project(tmp_path)
    adapter = AlwaysLoopAdapter(_offline())
    _patch_adapter(monkeypatch, adapter)
    before = store.load_chapter(chapter["id"])

    with pytest.raises(AdapterError) as exc:
        service.generate_chapter(store, config, chapter["id"])
    assert "整节重写" in exc.value.message or "局部续写" in exc.value.message
    assert "未写入章节" in exc.value.message

    after = store.load_chapter(chapter["id"])
    assert after.get("content") == before.get("content")
    assert after.get("revision") == before.get("revision")
    assert not chapter_checkpoint.checkpoint_path(store, chapter["id"]).exists()
    # 首轮：1 主 + 2 局部续写 + 1 整节重写 = 4 次
    assert len(adapter.calls) == 4


class LengthAdapter:
    """始终以 finish_reason=length 结束，用于验证看门狗命中后不丢内容。"""

    def __init__(self) -> None:
        """初始化调用计数。"""
        self.calls = 0

    def generate(self, request: TextGenRequest) -> TextGenResult:
        """每次返回不同且不含重复片段的正文，模拟被看门狗截停。"""
        self.calls += 1
        text = "\n\n".join(
            [
                f"第{self.calls}次生成：檐下的水一滴一滴落进铁桶。",
                "她把湿透的鞋放在门槛外，回头看了一眼屋里。",
                "桌上的茶已经凉了，杯壁上留着一圈浅浅的印。",
                "他说完这句话，就不再解释，也没有再看她。",
            ]
        )
        return TextGenResult(text=text, finish_reason="length", model="fake")


def test_watchdog_hit_keeps_content() -> None:
    """看门狗命中后保留已写正文，走局部续写，不整轮丢弃。"""
    adapter = LengthAdapter()
    notices: list[str] = []
    request = TextGenRequest(
        task="chapter",
        system="",
        messages=[Message("user", "写一章")],
        metadata={"watchdog_target_words": 800, "with_summary": False},
    )

    result, text, _, _ = service._generate_validated(
        adapter,
        request,
        None,
        "第 1 轮正文",
        on_notice=notices.append,
        target_chars=800,
    )

    assert adapter.calls == 3  # 1 主 + 2 次局部续写
    assert result.finish_reason == "watchdog"
    for index in range(1, 4):
        assert f"第{index}次生成" in text
    assert any("看门狗上限" in item for item in notices)
    assert any("未丢弃正文" in item for item in notices)
