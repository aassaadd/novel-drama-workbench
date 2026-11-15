"""按幕批量生成章节正文。

复用单章生成链路（`generate_chapter`）：每章仍按节拍分批推进、生成后写入
滚动摘要与历史版本，这里只负责「按幕取章节顺序、逐章调度、汇总结果」。

调度约定：
- `mode=fill`：已有正文的章直接跳过并计入结果；`mode=regen`：已有正文也重写；
- 单章失败不中断整幕，失败原因逐章记录，最后统一汇总；
- 章与章之间检查取消标志，取消时已完成章节保持落盘，未开始的章节不再调用模型；
- 进度通过 `on_event` 上报（章开始 / 跳过 / 完成 / 失败），页面与任务队列据此展示。
"""

from __future__ import annotations

from ...adapters.base import AdapterError
from ...cancel import GenerationCanceled
from ...store import ProjectStore
from ...store.settings_store import SettingsStore
from .service import generate_chapter

#: 生成模式：只补缺失 / 重新生成已有正文
MODES = ("fill", "regen")


class ActBatchCanceled(GenerationCanceled):
    """用户停止整幕生成：携带已跑完的章节汇总，供前端展示后收尾。"""

    def __init__(self, result: dict):
        """保存取消时的部分结果。

        参数:
            result: 与正常完成同结构的汇总 dict（含 canceled=True）。
        """
        super().__init__("用户已停止生成")
        self.result = result


def resolve_act(store: ProjectStore, act_id: str = "", act_index: int | None = None) -> dict:
    """在大纲中定位一幕。

    参数:
        store: 工程。
        act_id: 幕 ID（优先）。
        act_index: 幕序号（从 1 开始，ID 缺失时使用）。

    返回:
        `{act, act_index, act_total, chapters}`；act_total 为全书幕数。

    异常:
        ValueError: 没有大纲、找不到该幕或该幕没有章节。
    """
    bible = SettingsStore(store.root).ensure()
    acts = (bible.get("outline") or {}).get("acts") or []
    if not acts:
        raise ValueError("设定库还没有大纲：先在大纲里生成幕与章节")
    position = -1
    if act_id:
        position = next((index for index, act in enumerate(acts) if str(act.get("id") or "") == act_id), -1)
    if position < 0 and act_index:
        candidate = int(act_index) - 1
        if 0 <= candidate < len(acts):
            position = candidate
    if position < 0:
        raise ValueError(f"找不到大纲里的该幕（act={act_id or act_index or '空'}）")
    act = acts[position]
    chapters = list(act.get("chapters") or [])
    if not chapters:
        raise ValueError(f"第 {position + 1} 幕《{act.get('title') or '未命名幕'}》还没有章节：先补齐大纲")
    return {
        "act": act,
        "act_index": int(act.get("index") or position + 1),
        "act_total": len(acts),
        "chapters": chapters,
    }


def _count_words(text: str) -> int:
    """统计正文有效字数（去掉空白）。

    参数:
        text: 正文文本。

    返回:
        字数；空值返回 0。
    """
    return sum(1 for char in (text or "") if not char.isspace())


def _error_message(exc: Exception) -> str:
    """把异常转成可读中文原因。

    参数:
        exc: 捕获到的异常。

    返回:
        AdapterError 取 message，其它取 str 或类名。
    """
    if isinstance(exc, AdapterError):
        return exc.message
    return str(exc) or exc.__class__.__name__


def _error_code(exc: Exception) -> str:
    """取错误的机器可读码，供页面聚合与跳转处理。

    参数:
        exc: 捕获到的异常。

    返回:
        `detail.code`（如 BINDING_POV_MISSING）优先，其次统一错误码；非适配器错误返回空串。
    """
    if not isinstance(exc, AdapterError):
        return ""
    return str((exc.detail or {}).get("code") or exc.code.value or "")


def _emit(on_event, kind: str, **payload) -> None:
    """上报一条进度事件（未传回调时忽略）。

    参数:
        on_event: `(event) -> None` 或 None。
        kind: 事件类型（chapter_start / chapter_skip / chapter_done / chapter_error / batch）。
        **payload: 事件字段。

    返回:
        无。
    """
    if on_event:
        on_event({"type": kind, **payload})


def _summary_text(result: dict) -> str:
    """拼装整幕结果的一句话汇总。

    参数:
        result: 汇总 dict。

    返回:
        形如「成功 3 章 / 跳过 2 章 / 失败 1 章」。
    """
    return f"成功 {result['succeeded']} 章 / 跳过 {result['skipped']} 章 / 失败 {result['failed']} 章"


def _chapter_progress(on_event, act_info: dict, result: dict, position: int, total: int, title: str, label: str) -> None:
    """上报「本章已处理完」的任务队列进度（百分比按已处理章数推进）。

    参数:
        on_event: 进度回调。
        act_info: `resolve_act` 的返回。
        result: 当前汇总 dict（取幕序号）。
        position: 本幕内章序号（从 1 开始）。
        total: 本幕章数。
        title: 章节标题。
        label: 处理结果文案（完成 / 跳过 / 失败）。

    返回:
        无。
    """
    _emit(
        on_event,
        "batch",
        phase="chapters",
        act_index=result["act_index"],
        act_total=act_info["act_total"],
        chapters_done=position,
        chapter_total=total,
        chapter_index=position,
        name=f"第 {position}/{total} 章《{title}》{label}",
        percent=round(position / total * 100),
    )


def _build_result(act_info: dict, mode: str) -> dict:
    """构造空的结果骨架。

    参数:
        act_info: `resolve_act` 的返回。
        mode: 生成模式。

    返回:
        结果 dict（counts 为 0，chapters 为空）。
    """
    act = act_info["act"]
    return {
        "act_id": str(act.get("id") or ""),
        "act_index": act_info["act_index"],
        "act_title": str(act.get("title") or ""),
        "mode": mode,
        "total": len(act_info["chapters"]),
        "succeeded": 0,
        "skipped": 0,
        "failed": 0,
        "chapters": [],
        "canceled": False,
        "message": "",
    }


def generate_act_chapters(
    store: ProjectStore,
    config,
    act_id: str = "",
    act_index: int | None = None,
    mode: str = "fill",
    on_event=None,
    on_delta=None,
    is_canceled=None,
    multi_turn: bool | None = None,
    beats_per_round: int | None = None,
    max_rounds: int | None = None,
) -> dict:
    """按章节顺序生成一幕的全部正文。

    参数:
        store: 工程。
        config: 本机配置。
        act_id: 幕 ID（优先）。
        act_index: 幕序号（从 1 开始，ID 缺失时使用）。
        mode: `fill` 只补缺失 / `regen` 重新生成已有正文。
        on_event: 进度回调 `(event: dict) -> None`。
        on_delta: 单章生成时的正文增量回调（同时承担取消检查与页面实时预览）。
        is_canceled: 章与章之间检查取消的回调 `() -> bool`。
        multi_turn: 显式分轮开关，透传给 `generate_chapter`。
        beats_per_round: 每轮节拍数，透传给 `generate_chapter`。
        max_rounds: 最大轮数，透传给 `generate_chapter`。

    返回:
        `{act_id, act_index, act_title, mode, total, succeeded, skipped, failed,
          chapters, canceled, message}`；chapters 每项含
          `{index, chapter_index, chapter_id, title, status, words, message}`。

    异常:
        ValueError: 找不到幕 / 该幕没有章节 / mode 非法。
        ActBatchCanceled: 用户在章与章之间停止（携带部分结果）。
        AdapterError: 取消检查之外的致命错误（单章错误已在内部记录）。
    """
    if mode not in MODES:
        raise ValueError(f"未知生成模式: {mode}（可选 {' / '.join(MODES)}）")

    act_info = resolve_act(store, act_id, act_index)
    total = len(act_info["chapters"])
    result = _build_result(act_info, mode)
    _emit(
        on_event,
        "act_start",
        act_id=result["act_id"],
        act_index=result["act_index"],
        act_total=act_info["act_total"],
        act_title=result["act_title"],
        total=total,
        mode=mode,
        chapters=[
            {
                "id": str(entry.get("id") or ""),
                "index": int(entry.get("index") or 0),
                "title": str(entry.get("title") or ""),
            }
            for entry in act_info["chapters"]
        ],
    )

    for position, entry in enumerate(act_info["chapters"], start=1):
        # 章间取消：已完成的章都已落盘，直接带部分结果退出，不再调用模型
        if is_canceled and is_canceled():
            result["canceled"] = True
            result["message"] = f"已停止：{_summary_text(result)}"
            raise ActBatchCanceled(result)

        chapter_id = str(entry.get("id") or "")
        title = str(entry.get("title") or "")
        global_index = int(entry.get("index") or 0)
        row = {
            "index": position,
            "chapter_index": global_index,
            "chapter_id": chapter_id,
            "title": title,
            "status": "",
            "code": "",
            "words": 0,
            "message": "",
            "notices": [],
        }

        # 任务队列进度：按章推进，文字沿用「第 x/y 幕 · 已完成 k/n 章」
        _emit(
            on_event,
            "batch",
            phase="chapters",
            act_index=result["act_index"],
            act_total=act_info["act_total"],
            chapters_done=position - 1,
            chapter_total=total,
            chapter_index=position,
            name=f"第 {position}/{total} 章 · 《{title}》",
            percent=round((position - 1) / total * 100),
        )
        _emit(
            on_event,
            "chapter_start",
            index=position,
            total=total,
            chapter_index=global_index,
            chapter_id=chapter_id,
            title=title,
        )

        try:
            chapter = store.load_chapter(chapter_id)
        except FileNotFoundError:
            row.update(
                status="failed",
                code="CHAPTER_NOT_LANDED",
                message="章节尚未落成正文文件：先在大纲里点「落地成章节」",
            )
            result["failed"] += 1
            result["chapters"].append(row)
            _emit(
                on_event,
                "chapter_error",
                index=position,
                total=total,
                chapter_id=chapter_id,
                title=title,
                code=row["code"],
                message=row["message"],
            )
            _chapter_progress(on_event, act_info, result, position, total, title, "失败")
            continue

        existing_words = int(chapter.get("word_count") or 0) or _count_words(chapter.get("content") or "")
        if mode == "fill" and (chapter.get("content") or "").strip():
            row.update(status="skipped", words=existing_words, message="已有正文，按「只补缺失」跳过")
            result["skipped"] += 1
            result["chapters"].append(row)
            _emit(
                on_event,
                "chapter_skip",
                index=position,
                total=total,
                chapter_id=chapter_id,
                title=title,
                words=existing_words,
                reason=row["message"],
            )
            _chapter_progress(on_event, act_info, result, position, total, title, "跳过")
            continue

        try:
            generated = generate_chapter(
                store,
                config,
                chapter_id,
                regen=mode == "regen",
                multi_turn=multi_turn,
                beats_per_round=beats_per_round,
                max_rounds=max_rounds,
                on_delta=on_delta,
            )
        except AdapterError as exc:
            # 保留错误码（如 BINDING_POV_MISSING），页面据此聚合原因并给出处理入口
            row.update(status="failed", code=_error_code(exc), message=exc.message)
            result["failed"] += 1
            result["chapters"].append(row)
            _emit(
                on_event,
                "chapter_error",
                index=position,
                total=total,
                chapter_id=chapter_id,
                title=title,
                code=row["code"],
                message=row["message"],
            )
            _chapter_progress(on_event, act_info, result, position, total, title, "失败")
            continue
        except Exception as exc:  # noqa: BLE001 - 单章失败不能中断整幕，原因逐章记录
            row.update(status="failed", message=_error_message(exc))
            result["failed"] += 1
            result["chapters"].append(row)
            _emit(
                on_event,
                "chapter_error",
                index=position,
                total=total,
                chapter_id=chapter_id,
                title=title,
                code=row["code"],
                message=row["message"],
            )
            _chapter_progress(on_event, act_info, result, position, total, title, "失败")
            continue

        row.update(
            status="succeeded",
            words=int(generated.get("words") or generated.get("word_count") or 0),
            message=str(generated.get("summary_error") or ""),
            rounds=int(generated.get("rounds") or 1),
            beats_done=int(generated.get("beats_done") or 0),
            beats_total=int(generated.get("beats_total") or 0),
            notices=list(generated.get("notices") or []),
        )
        result["succeeded"] += 1
        result["chapters"].append(row)
        _emit(
            on_event,
            "chapter_done",
            index=position,
            total=total,
            chapter_id=chapter_id,
            title=title,
            words=row["words"],
            rounds=row["rounds"],
            beats_done=row["beats_done"],
            beats_total=row["beats_total"],
            summary_error=row["message"],
            notices=row["notices"],
            percent=round(position / total * 100),
        )
        _chapter_progress(on_event, act_info, result, position, total, title, "完成")

    result["message"] = _summary_text(result)
    return result
