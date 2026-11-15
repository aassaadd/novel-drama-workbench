"""分层大纲生成的进度上报与失败信息（页面流式事件用）。"""

from __future__ import annotations

from ..support.llm_calls import (
    _progress,
)


def _failure(scope: str, message: str, act_index: int, act_name: str, chapter_index: int | None = None,
             chapter_title: str = "") -> dict:
    """构造一条失败记录（供页面展示与按项重试）。

    参数:
        scope: act / chapter。
        message: 中文原因。
        act_index: 幕序号（从 1 开始）。
        act_name: 幕标题。
        chapter_index: 全文章序号（章级失败时）。
        chapter_title: 章标题。

    返回:
        失败记录 dict。
    """
    return {
        "scope": scope,
        "act_index": act_index,
        "act_name": act_name,
        "chapter_index": chapter_index,
        "chapter_title": chapter_title,
        "message": message,
    }

def _error_text(exc: Exception) -> str:
    """把异常转成可读中文。

    参数:
        exc: 异常。

    返回:
        AdapterError 取 message，其它取 str 或类名。
    """
    return getattr(exc, "message", None) or str(exc) or exc.__class__.__name__

def _project_context(work: dict, bible: dict, characters: list[dict]) -> str:
    """拼三个阶段共用的作品上下文。

    参数:
        work: 作品信息。
        bible: 设定库。
        characters: 已有人物（不含旁白）。

    返回:
        多行文本。
    """
    lines = [f"作品：《{work.get('title') or '未命名'}》"]
    logline = (bible.get("premise") or {}).get("logline")
    if logline:
        lines.append(f"一句话故事：{logline}")
    genres = work.get("genre") or []
    if genres:
        lines.append("题材：" + "、".join(str(item) for item in genres))
    if characters:
        lines.append("可用人物（id=姓名）：" + "、".join(f"{item['id']}={item.get('name')}" for item in characters))
    return "\n".join(lines)

def _delta_reporter(on_progress, unit: str, index: int, total: int, name: str, extra: dict | None = None):
    """生成 on_delta：累计本层接收长度并上报 chars 事件。

    参数:
        on_progress: 进度回调。
        unit: 事件单位（act / beat），页面据此选择文案模板。
        index: 当前项序号（从 1 开始；幕阶段为 0，表示尚未拆分）。
        total: 当前层总项数。
        name: 当前项名称。
        extra: 附加字段（phase / act_index / chapter_index / percent 等）。

    返回:
        `(text) -> None` 回调。
    """
    state = {"chars": 0}

    def on_delta(text: str) -> None:
        """接收模型增量并上报。"""
        if not text:
            return
        state["chars"] += len(str(text))
        payload = {"type": "chars", "chars": state["chars"], "unit": unit, "index": index, "total": total, "name": name}
        payload.update(extra or {})
        _progress(on_progress, payload)

    return on_delta

def _beat_delta_reporter(
    on_progress,
    done_before: int,
    beat_total: int,
    chapter_pos: int,
    chapter_total: int,
    act_pos: int,
    act_total: int,
    chapter_title: str,
    percent,
):
    """节拍阶段的增量回调：按已出现的节拍概要数上报「第 y/200 节拍」。

    工具参数是流式 JSON，每写一个节拍就会出现一次 `"summary"` 键；累计原文后
    统计出现次数，得到本章已经产出的节拍数，再叠加前面各章已完成的节拍数。

    参数:
        on_progress: 进度回调。
        done_before: 本章之前已完成的节拍总数。
        beat_total: 全书节拍总数。
        chapter_pos: 全书章序号。
        chapter_total: 全书章总数。
        act_pos: 幕序号。
        act_total: 幕总数。
        chapter_title: 本章标题。
        percent: `(已完成数) -> int` 百分比函数。

    返回:
        `(text) -> None` 回调。
    """
    state = {"chars": 0, "raw": ""}

    def on_delta(text: str) -> None:
        """接收增量并按节拍概要计数。"""
        if not text:
            return
        state["chars"] += len(str(text))
        state["raw"] = (state.get("raw") or "") + str(text)
        local = state["raw"].count('"summary"')
        index = min(beat_total, done_before + max(1, local))
        _progress(
            on_progress,
            {
                "type": "chars",
                "chars": state["chars"],
                "unit": "beat",
                "phase": "beats",
                "index": index,
                "total": beat_total,
                "beat_index": index,
                "beat_total": beat_total,
                "act_index": act_pos,
                "act_total": act_total,
                "chapter_index": chapter_pos,
                "chapter_total": chapter_total,
                "chapter": chapter_title,
                "percent": percent(done_before + max(1, local)),
            },
        )

    return on_delta
