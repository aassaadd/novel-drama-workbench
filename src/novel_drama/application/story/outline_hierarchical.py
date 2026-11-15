"""三级分层大纲生成：幕 → 章 → 节拍，三个阶段一次走完。

- 阶段一（1 次调用）：只产幕列表（标题、目标/概要、预计章数）；
- 阶段二（每幕 1 次）：逐幕产出该幕章节（标题 + 概要）；
- 阶段三（每章 1 次）：逐章补齐该章全部节拍。

中间结果落到 `<工程>/.ndw/outline_hier.json`：单幕/单章失败只记录失败项，
其余结果保留；再次以 `resume=True` 调用时只补做缺失部分，不从头再来。

原有单轮 / 按幕多轮的 `service.generate_outline` 保持不动，供旧接口使用。
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from pathlib import Path

from ...adapters.base import AdapterError, ErrorCode, Message, TextGenRequest
from ...application.cast.sync_cast import (
    apply_chapter_cast,
    cast_prompt_block,
    load_pending,
    normalize_cast,
    summarize_cast,
)
from ...config import AppConfig
from ...models.ids import new_id, now_iso
from ...store import ProjectStore
from ...store.settings_store import SettingsStore
from ...store.world_store import WorldStore
from ...text.titles import strip_index_prefix
from ..support.llm_calls import (
    _characters_payload,
    _generate_json,
    _progress,
    build_writing_adapter,
)
from .generate_outline import _act_validation, _tolerance, _validation_report
from .outline_bind import (
    bind_outline_context,
    outline_binding_stats,
    sanitize_outline_character_ids,
)
from .outline_validate import check_golden_three

#: 日志：分层大纲的章数/节拍数纠偏过程
logger = logging.getLogger(__name__)

#: 分层生成中间文件版本
WORK_VERSION = "outline_hier/1"


def work_file(store: ProjectStore) -> Path:
    """中间结果文件路径：`<工程>/.ndw/outline_hier.json`。

    参数:
        store: 工程。

    返回:
        Path。
    """
    return store.root / ".ndw" / "outline_hier.json"


def load_work(store: ProjectStore) -> dict | None:
    """读取上次的分层生成中间结果。

    参数:
        store: 工程。

    返回:
        work dict；不存在或格式不对返回 None。
    """
    path = work_file(store)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("acts"), list) else None


def save_work(store: ProjectStore, work: dict) -> None:
    """原子保存中间结果（每完成一幕/一章都会调用，便于中断后续跑）。

    参数:
        store: 工程。
        work: 工作区 dict。

    返回:
        无。
    """
    path = work_file(store)
    path.parent.mkdir(parents=True, exist_ok=True)
    work["updated_at"] = now_iso()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(work, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _text(value, limit: int = 300) -> str:
    """取字符串并裁掉超长部分。

    参数:
        value: 任意值。
        limit: 最大长度。

    返回:
        去空白后的字符串。
    """
    return str(value or "").strip()[:limit]


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


def _count_correction(kind: str, got: int, expected: int, existing: list[str]) -> str:
    """生成「章数/节拍数不符」的重试提示。

    参数:
        kind: chapters / beats。
        got: 上一次实际数量（0 表示模型没返回任何条目）。
        expected: 设定数量。
        existing: 上一次已给出的标题或概要，用于避免重复。

    返回:
        中文纠正提示；空数组时明确写「没有返回任何节拍/章节」。
    """
    noun = {"acts": "幕", "chapters": "章节", "beats": "节拍"}.get(kind, kind)
    unit = {"acts": "幕", "chapters": "章", "beats": "个节拍"}.get(kind, kind)
    subject = {"acts": "整体", "chapters": "本幕", "beats": "本章"}.get(kind, "本次")
    if got <= 0:
        return (
            f"上一版没有返回任何{noun}，{subject}需要 {expected} {unit}。"
            f"请完整给出 {expected} {unit}，一条都不要省略。"
        )
    listed = "、".join(title for title in existing if title)
    return (
        f"上一次只给了 {got} {unit}，必须恰好 {expected} {unit}。"
        f"请补齐到 {expected} {unit}，不要少给、不要多给，也不要重复已有内容：{listed or '（无）'}"
    )


def _count_reason(kind: str, got: int, expected: int) -> str:
    """生成数量不符时的进度说明（空数组写明「为空」，供页面直接展示）。

    参数:
        kind: chapters / beats / acts。
        got: 上一次实际数量。
        expected: 设定数量。

    返回:
        中文说明。
    """
    if kind == "chapters":
        return f"本幕章节为空，需要 {expected} 章" if got <= 0 else f"上一版给了 {got} 章，本幕要求 {expected} 章"
    if kind == "beats":
        return f"本章节拍为空，需要 {expected} 个节拍" if got <= 0 else f"上一版给了 {got} 个节拍，本章要求 {expected} 个"
    return f"上一版给了 {got} 幕，本次要求 {expected} 幕"


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


def _pct(done: int, total: int) -> int:
    """按已完成条目数折算总体百分比（幕 + 章 + 节拍合计）。

    参数:
        done: 已完成条目数。
        total: 条目总数。

    返回:
        0–100 的整数。
    """
    total = max(1, int(total))
    return max(0, min(100, int(round(100 * max(0, int(done)) / total))))


def _normalize_acts(payload: dict, acts: int, chapters_per_act: int) -> list[dict]:
    """清洗阶段一的幕列表。

    参数:
        payload: `emit_acts` 的返回值。
        acts: 目标幕数。
        chapters_per_act: 未给出预计章数时的默认值。

    返回:
        幕列表，每项 `{title, summary, chapters, status, error}`。

    异常:
        AdapterError: 未返回可用幕。
    """
    raw_acts = payload.get("acts") if isinstance(payload, dict) else None
    if not isinstance(raw_acts, list) or not raw_acts:
        raise AdapterError(ErrorCode.INVALID_INPUT, "分层大纲阶段一失败：emit_acts 未返回非空 acts 数组")
    rows: list[dict] = []
    for index, raw in enumerate(raw_acts[: max(1, int(acts))], start=1):
        if not isinstance(raw, dict):
            continue
        title = strip_index_prefix(_text(raw.get("title"), 80)) or f"第{index}幕"
        # 章数以用户设定为准：模型给的 chapters_count 只作为参考，不写进中间结果
        try:
            suggested = int(raw.get("chapters_count") or chapters_per_act)
        except (TypeError, ValueError):
            suggested = chapters_per_act
        if suggested != max(1, int(chapters_per_act)):
            logger.warning(
                "大纲第 %s 幕模型建议 %s 章，按设定强制为 %s 章", index, suggested, max(1, int(chapters_per_act))
            )
        rows.append(
            {
                "title": title,
                "summary": _text(raw.get("summary") or raw.get("goal"), 400),
                "chapters_count": max(1, int(chapters_per_act)),
                "chapters": [],
                "status": "pending",
                "error": None,
            }
        )
    if not rows:
        raise AdapterError(ErrorCode.INVALID_INPUT, "分层大纲阶段一失败：幕列表里没有可用对象")
    return rows


def _normalize_chapters(payload: dict, expected: int) -> list[dict]:
    """清洗阶段二某一幕的章节列表。

    参数:
        payload: `emit_chapters` 的返回值。
        expected: 本幕计划章数（缺失时补足到该数量）。

    返回:
        章节列表，每项 `{title, summary, beats, status, error}`；
        未返回可用章节时返回空列表（交给章数闸门重写，而不是直接记失败）。
    """
    raw = payload.get("chapters") if isinstance(payload, dict) else None
    if not isinstance(raw, list) or not raw:
        return []
    rows: list[dict] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "title": strip_index_prefix(_text(item.get("title"), 80)) or f"第{index}章",
                "summary": _text(item.get("summary"), 400),
                "beats": [],
                "status": "pending",
                "error": None,
            }
        )
    return rows


def _normalize_character_arcs(payload: dict, cast_ids: set[str]) -> list[dict]:
    """清洗阶段三返回的本章角色弧线。

    参数:
        payload: `emit_beats` 的返回值。
        cast_ids: 允许引用的角色 ID 集合。

    返回:
        最多 3 条弧线；角色不存在或状态为空时丢弃。
    """
    raw = payload.get("character_arcs") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return []
    rows: list[dict] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        character_id = str(item.get("character_id") or "").strip()
        if not character_id or character_id not in cast_ids or character_id in seen:
            continue
        seen.add(character_id)
        to_state = _text(item.get("to_state"), 500)
        if not to_state:
            continue
        rows.append(
            {
                "character_id": character_id,
                "from_state": _text(item.get("from_state"), 500),
                "to_state": to_state,
                "note": _text(item.get("note"), 500),
            }
        )
        if len(rows) >= 3:
            break
    return rows


def _normalize_beats(payload: dict, expected: int, chapter_words: int, beats_per_chapter: int) -> list[dict]:
    """清洗阶段三某一章的节拍列表。

    参数:
        payload: `emit_beats` 的返回值。
        expected: 本章计划节拍数。
        chapter_words: 单章目标字数。
        beats_per_chapter: 未给出目标字数时的默认等分数。

    返回:
        节拍列表；未返回可用节拍时返回空列表
        （交给节拍数闸门按数量越界重写，而不是直接记失败）。
    """
    raw = payload.get("beats") if isinstance(payload, dict) else None
    if not isinstance(raw, list) or not raw:
        return []
    default_words = max(1, int(chapter_words) // max(1, int(beats_per_chapter)))
    rows: list[dict] = []
    for index, item in enumerate(raw[: max(1, int(expected))], start=1):
        if not isinstance(item, dict):
            continue
        ids = [str(value) for value in (item.get("character_ids") or []) if str(value).strip()]
        try:
            words = int(item.get("target_words") or default_words)
        except (TypeError, ValueError):
            words = default_words
        rows.append(
            {
                "summary": _text(item.get("summary"), 240) or f"第{index}个节拍",
                "character_ids": ids,
                "extra_characters": [
                    _text(name, 60) for name in (item.get("extra_characters") or []) if _text(name, 60)
                ],
                "turning_point": bool(item.get("turning_point")),
                "target_words": words,
            }
        )
    return rows


def _apply_chapter_cast(
    store: ProjectStore,
    chapter: dict,
    payload: dict,
    chapter_pos: int,
    characters: list[dict],
    pending: dict,
    dry_run: bool,
    on_progress,
) -> dict:
    """把本次节拍调用里的 cast 落到角色卡与节拍绑定（不再单独请求模型）。

    参数:
        store: 工程。
        chapter: 大纲章节（节拍已生成，会被原地写入 character_ids）。
        payload: `emit_beats` 的同一次返回（含 beats 与 cast）。
        chapter_pos: 全文章序号。
        characters: 当前内存角色表（不含旁白）。
        pending: 待定角色登记（第二次出现才建卡）。
        dry_run: True 时只预览，不写角色卡与登记。
        on_progress: 进度回调。

    返回:
        `apply_chapter_cast()` 的返回值。
    """
    report = apply_chapter_cast(
        store,
        chapter,
        normalize_cast(payload),
        characters=characters,
        pending=pending,
        persist=not dry_run,
        chapter_index=chapter_pos,
        chapter_title=chapter.get("title") or "",
    )
    title = chapter.get("title") or f"第{chapter_pos}章"
    summary = f"第 {chapter_pos} 章《{title}》角色对齐：{summarize_cast(report)}"
    logger.info(summary)
    _progress(on_progress, {"type": "notice", "message": summary})
    return report


def _bind_outline_characters(store: ProjectStore, bible: dict, characters: list[dict]) -> dict:
    """按给定角色表绑定大纲里的世界与 POV（不重新读盘）。

    参数:
        store: 工程（只读取 worlds.json）。
        bible: 设定库 dict（就地修改）。
        characters: 本次生成使用的角色表（含新建角色）。

    返回:
        `bind_outline_context` 的统计结果。
    """
    world_store = WorldStore(store.root)
    worlds = world_store.list_worlds() if world_store.exists() else []
    return bind_outline_context(bible, worlds, characters)


def generate_hierarchical_outline(
    store: ProjectStore,
    config: AppConfig,
    acts: int = 3,
    chapters_per_act: int = 3,
    beats_per_chapter: int = 3,
    chapter_words: int = 1200,
    dry_run: bool = False,
    binding: str = "auto",
    resume: bool = False,
    only_acts: list[int] | None = None,
    on_progress=None,
) -> dict:
    """三级分层生成大纲（幕 → 章 → 节拍），失败项可续跑。

    参数:
        store: 工程。
        config: 本机配置。
        acts: 幕数（阶段一的目标数量）。
        chapters_per_act: 每幕默认章数（阶段一未给预计章数时使用）。
        beats_per_chapter: 每章节拍数。
        chapter_words: 单章目标字数。
        dry_run: 只生成不落盘。
        binding: auto=把节拍绑定到已有人物；none=不改动角色引用。
        resume: True 时读取上次中间结果，只补做缺失的幕/章。
        on_progress: 进度回调，事件为 `stage` / `batch{unit}` / `chars`。

    返回:
        `{acts, chapters, beats, provider, settings_revision, binding, golden_three,
        dry_run, failures, resumed, work_file}`。
    """
    settings_store = SettingsStore(store.root)
    bible = settings_store.ensure()
    project = store.load_project()
    work_info = project.get("work") or {}
    characters = [item for item in store.load_characters()["characters"] if item.get("role") != "narrator"]
    char_rows = _characters_payload(characters)
    adapter = build_writing_adapter(config, bible)
    context = _project_context(work_info, bible, char_rows)
    failures: list[dict] = []

    work = load_work(store) if resume else None
    if work is None:
        work = {
            "version": WORK_VERSION,
            "created_at": now_iso(),
            "params": {
                "acts": max(1, int(acts)),
                "chapters_per_act": max(1, int(chapters_per_act)),
                "beats_per_chapter": max(1, int(beats_per_chapter)),
                "chapter_words": max(1, int(chapter_words)),
            },
            "acts": [],
        }
        _progress(on_progress, {"type": "stage", "stage": "acts", "total": max(1, int(acts))})
        prompt = (
            f"{context}\n\n任务：为这部作品规划幕结构，共 {max(1, int(acts))} 幕，每幕约 "
            f"{max(1, int(chapters_per_act))} 章。\n"
            "只输出幕列表：每幕给 title（标题）、summary（本幕目标与概要）、chapters_count（预计章数）。\n"
            "不要输出章节或节拍。"
        )
        req = TextGenRequest(
            task="outline",
            system="你是长篇小说的结构编辑，只按工具参数输出幕结构，不要解释。",
            messages=[Message("user", prompt)],
            stream=True,
            metadata={
                "acts": max(1, int(acts)),
                "chapters_per_act": max(1, int(chapters_per_act)),
                "beats_per_chapter": max(1, int(beats_per_chapter)),
                "title": work_info.get("title"),
                "logline": (bible.get("premise") or {}).get("logline"),
                "genre": work_info.get("genre") or [],
                "characters": char_rows,
            },
        )
        payload = _generate_json(
            adapter,
            req,
            "outline_acts",
            # 幕是一次请求出全量，这里只报「正在规划 N 幕 + 已接收字数」，
            # 不把后面的章数错配到「幕」的单位上
            on_delta=_delta_reporter(
                on_progress, "act", 0, max(1, int(acts)), "幕结构", {"phase": "acts", "percent": 0}
            ),
        )
        work["acts"] = _normalize_acts(payload, acts, chapters_per_act)
        # 第一轮闸门：幕数必须落在 ±1 内，不合格只重写本层
        acts_expected = max(1, int(acts))
        act_gate_attempts = 0
        while abs(len(work["acts"]) - acts_expected) > 1 and act_gate_attempts < 2:
            act_gate_attempts += 1
            _progress(
                on_progress,
                {
                    "type": "rewrite",
                    "level": "acts",
                    "attempt": act_gate_attempts,
                    "reason": f"上一版给了 {len(work['acts'])} 幕，设定 {acts_expected} 幕（允许 ±1 幕）",
                    "index": 1,
                    "total": acts_expected,
                },
            )
            retry_req = replace(
                req,
                messages=list(req.messages)
                + [
                    Message(
                        "user",
                        _count_correction("acts", len(work["acts"]), acts_expected, [item["title"] for item in work["acts"]]),
                    )
                ],
            )
            payload = _generate_json(adapter, retry_req, "outline_acts")
            work["acts"] = _normalize_acts(payload, acts, chapters_per_act)
        if abs(len(work["acts"]) - acts_expected) > 1:
            save_work(store, work)
            raise AdapterError(
                ErrorCode.UPSTREAM_ERROR,
                f"大纲生成停在「幕」层：设定 {acts_expected} 幕（允许 ±1 幕），重写 {act_gate_attempts} 次后实际 "
                f"{len(work['acts'])} 幕。已完成内容已保留，可从当前层继续。",
                retryable=True,
                detail={
                    "stage": "acts",
                    "attempts": act_gate_attempts,
                    "act_index": 1,
                    "reason": f"上一版给了 {len(work['acts'])} 幕，设定 {acts_expected} 幕",
                    "resume": True,
                    "gate": True,
                    "failures": [{"act_index": 1, "act_name": "幕层", "kind": "幕数越界", "raw": ""}],
                },
            )
        save_work(store, work)

    # 只重写指定幕（页面「重新生成该幕」）：其余幕必须已有章节，否则无法只重写
    if only_acts:
        targets = {int(item) - 1 for item in only_acts if str(item).isdigit()}
        for index, act in enumerate(work["acts"]):
            if index in targets:
                act["chapters"] = []
                act["status"] = "pending"
                act["error"] = None
            elif not act.get("chapters"):
                raise AdapterError(
                    ErrorCode.INVALID_INPUT,
                    f"缺少第 {index + 1} 幕的章节，无法只重写指定幕；请重新生成整份大纲。",
                )

    # 阶段二：逐幕补章节（章数必须落在允许区间；不合格只重写本幕，上限 2 次）
    act_total = len(work["acts"])
    chapters_expected_total = act_total * max(1, int(chapters_per_act))
    beats_expected = max(1, int(beats_per_chapter))
    total_units = max(1, act_total + chapters_expected_total + chapters_expected_total * beats_expected)
    chapters_done = 0
    _progress(
        on_progress,
        {"type": "stage", "stage": "chapters", "total": act_total, "chapters_total": chapters_expected_total},
    )
    for act_pos, act in enumerate(work["acts"], start=1):
        expected = max(1, int(chapters_per_act))
        if act.get("chapters") and abs(len(act["chapters"]) - expected) <= _tolerance(expected):
            chapters_done += len(act["chapters"])
            continue
        name = act.get("title") or f"第{act_pos}幕"
        phase_extra = {
            "phase": "chapters",
            "chapters_done": chapters_done,
            "chapters_expected": expected,
            "chapters_total": chapters_expected_total,
            "percent": _pct(act_total + chapters_done, total_units),
        }
        _progress(
            on_progress,
            {
                "type": "batch",
                "unit": "act",
                "index": act_pos,
                "total": act_total,
                "name": name,
                **phase_extra,
            },
        )
        prev = "；".join(f"第 {i} 幕《{item['title']}》：{item.get('summary') or ''}" for i, item in enumerate(work["acts"][: act_pos - 1], start=1))
        nxt = "、".join(f"第 {i} 幕《{item['title']}》" for i, item in enumerate(work["acts"][act_pos:], start=act_pos + 1))
        lines = [
            context,
            "",
            f"第 {act_pos} 幕《{name}》：{act.get('summary') or ''}",
            f"请输出本幕的 {expected} 个章节：每章 title 与 summary，不写节拍。",
        ]
        if prev:
            lines.append("前序幕：" + prev)
        if nxt:
            lines.append("后续幕（只需衔接，不要展开）：" + nxt)
        req = TextGenRequest(
            task="outline",
            system="你是长篇小说的结构编辑，只按工具参数输出本幕章节列表，不要解释。",
            messages=[Message("user", "\n".join(lines))],
            stream=True,
            metadata={
                "acts": act_total,
                "chapters_per_act": expected,
                "beats_per_chapter": max(1, int(beats_per_chapter)),
                "title": work_info.get("title"),
                "logline": (bible.get("premise") or {}).get("logline"),
                "genre": work_info.get("genre") or [],
                "characters": char_rows,
                "act_offset": act_pos - 1,
            },
        )
        try:
            payload = _generate_json(
                adapter,
                req,
                "outline_chapters",
                on_delta=_delta_reporter(on_progress, "act", act_pos, act_total, name, phase_extra),
            )
            chapters = _normalize_chapters(payload, expected)
            # 按层重写：章数越界只重写本幕章节（最多 2 次），不裁剪
            attempts = 0
            ch_tol = _tolerance(expected)
            while abs(len(chapters) - expected) > ch_tol:
                if attempts >= 2:
                    break
                attempts += 1
                reason = _count_reason("chapters", len(chapters), expected)
                _progress(on_progress, {"type": "rewrite", "level": "chapters", "index": act_pos,
                                        "total": act_total, "attempt": attempts, "reason": reason,
                                        "actual": len(chapters), "expected": expected})
                retry_req = replace(
                    req,
                    messages=list(req.messages)
                    + [
                        Message(
                            "user",
                            _count_correction("chapters", len(chapters), expected, [c["title"] for c in chapters]),
                        )
                    ],
                )
                payload = _generate_json(
                    adapter,
                    retry_req,
                    "outline_chapters",
                    on_delta=_delta_reporter(on_progress, "act", act_pos, act_total, name, phase_extra),
                )
                chapters = _normalize_chapters(payload, expected)
            if abs(len(chapters) - expected) > _tolerance(expected):
                act["chapters"] = chapters
                act["status"] = "failed"
                effect = "章节为空" if not chapters else f"实际 {len(chapters)} 章"
                act["error"] = f"章层校验未过：设定 {expected} 章，{effect}"
                save_work(store, work)
                raise AdapterError(
                    ErrorCode.UPSTREAM_ERROR,
                    f"大纲生成停在第 {act_pos} 幕《{name}》的「章」层：{effect}，需要 {expected} 章"
                    f"（允许 ±{_tolerance(expected)} 章），已尝试 {attempts} 次。已合格的幕与章已保留，可从当前层继续。",
                    retryable=True,
                    detail={
                        "stage": "chapters",
                        "act_index": act_pos,
                        "attempts": attempts,
                        "reason": _count_reason("chapters", len(chapters), expected),
                        "resume": True,
                        "gate": True,
                        "failures": [
                            {
                                "act_index": act_pos,
                                "act_name": name,
                                "kind": "章层为空" if not chapters else "章层越界",
                                "raw": f"{len(chapters)}/{expected}",
                            }
                        ],
                    },
                )
            act["chapters"] = chapters
            act["status"] = "done"
            act["error"] = None
            act["_chapters_attempts"] = attempts
            chapters_done += len(chapters)
        except AdapterError as exc:
            if (exc.detail or {}).get("gate"):
                raise
            act["status"] = "failed"
            act["error"] = _error_text(exc)
            failures.append(_failure("act", act["error"], act_pos, name))
        except Exception as exc:  # 单幕失败不影响其它幕
            act["status"] = "failed"
            act["error"] = _error_text(exc)
            failures.append(_failure("act", act["error"], act_pos, name))
        save_work(store, work)

    # 阶段三：逐章补齐节拍（按全文章序号显示进度）
    chapter_total = sum(len(act.get("chapters") or []) for act in work["acts"])
    chapter_pos = 0
    beats_expected = max(1, int(beats_per_chapter))
    beats_total = chapter_total * beats_expected
    beats_done = 0
    chars_done = 0
    cast_created_total = 0
    cast_reused_total = 0
    # 待定角色：第一次出现只登记，第二次出现（跨章）才建卡
    pending: dict = {} if dry_run else load_pending(store)
    total_units = max(1, act_total + chapter_total + beats_total)
    _progress(on_progress, {"type": "stage", "stage": "beats", "total": chapter_total, "beats_total": beats_total})
    for act_pos, act in enumerate(work["acts"], start=1):
        for chapter in act.get("chapters") or []:
            chapter_pos += 1
            if chapter.get("beats") and len(chapter["beats"]) == beats_expected:
                # 续跑：节拍已齐即视为该章完成；角色对齐与节拍在同一次调用里，
                # 不再单独补一次角色请求（不留第二套路径）
                beats_done += len(chapter["beats"])
                continue
            chapter_title = chapter.get("title") or f"第{chapter_pos}章"
            _progress(
                on_progress,
                {
                    "type": "batch",
                    "unit": "beat",
                    "phase": "beats",
                    "index": min(beats_total, beats_done + 1),
                    "total": beats_total,
                    "beat_index": min(beats_total, beats_done + 1),
                    "beat_total": beats_total,
                    "name": chapter_title,
                    "act_index": act_pos,
                    "act_total": act_total,
                    "act_name": act.get("title") or f"第{act_pos}幕",
                    "chapter_index": chapter_pos,
                    "chapter_total": chapter_total,
                    "chapter": chapter_title,
                    "percent": _pct(act_total + chapter_total + beats_done, total_units),
                },
            )
            siblings = "；".join(
                f"第 {i} 章《{item.get('title')}》" + (f"：{item.get('summary')}" if item.get("summary") else "")
                for i, item in enumerate(act["chapters"], start=1)
                if item is not chapter
            )
            lines = [
                context,
                "",
                f"第 {act_pos} 幕《{act.get('title') or ''}》：{act.get('summary') or ''}",
                f"第 {chapter_pos} 章《{chapter_title}》：{chapter.get('summary') or ''}",
                f"请一次补齐本章的 {max(1, int(beats_per_chapter))} 个节拍：只写节拍，不要重复章节标题。",
                cast_prompt_block(characters),
            ]
            if siblings:
                lines.append("本幕其他章节（用于前后衔接）：" + siblings)
            req = TextGenRequest(
                task="outline",
                system="你是长篇小说的结构编辑，只按工具参数输出本章全部节拍，不要解释。",
                messages=[Message("user", "\n".join(lines))],
                stream=True,
                metadata={
                    "acts": act_total,
                    "chapters_per_act": len(act["chapters"]),
                    "beats_per_chapter": max(1, int(beats_per_chapter)),
                    "chapter_words": max(1, int(chapter_words)),
                    "title": work_info.get("title"),
                    "logline": (bible.get("premise") or {}).get("logline"),
                    "genre": work_info.get("genre") or [],
                    "characters": char_rows,
                    "act_offset": act_pos - 1,
                    "chapter_offset": act["chapters"].index(chapter),
                },
            )
            try:
                beats: list[dict] = []
                beat_attempts = 0
                while True:
                    payload = _generate_json(
                        adapter,
                        req if not beat_attempts else retry_req,
                        "outline_beats",
                        on_delta=_beat_delta_reporter(
                            on_progress,
                            beats_done,
                            beats_total,
                            chapter_pos,
                            chapter_total,
                            act_pos,
                            act_total,
                            chapter_title,
                            lambda done: _pct(act_total + chapter_total + done, total_units),
                        ),
                    )
                    beats = _normalize_beats(payload, beats_expected, chapter_words, beats_per_chapter)
                    if abs(len(beats) - beats_expected) <= _tolerance(beats_expected):
                        break
                    if beat_attempts >= 2:
                        break
                    beat_attempts += 1
                    logger.warning(
                        "大纲第 %s 章节拍 %s/%s，第 %s 次按层重写", chapter_pos, len(beats), beats_expected, beat_attempts
                    )
                    _progress(
                        on_progress,
                        {
                            "type": "rewrite",
                            "level": "beats",
                            "index": act_pos,
                            "total": act_total,
                            "chapter_index": chapter_pos,
                            "chapter": chapter_title,
                            "attempt": beat_attempts,
                            "actual": len(beats),
                            "expected": beats_expected,
                            "empty": not beats,
                            "reason": _count_reason("beats", len(beats), beats_expected),
                        },
                    )
                    retry_req = replace(
                        req,
                        messages=list(req.messages)
                        + [
                            Message(
                                "user",
                                _count_correction(
                                    "beats", len(beats), beats_expected, [item["summary"] for item in beats]
                                ),
                            )
                        ],
                    )
                if abs(len(beats) - beats_expected) > _tolerance(beats_expected):
                    chapter["beats"] = beats
                    chapter["status"] = "failed"
                    state = "节拍为空" if not beats else f"实际 {len(beats)} 个节拍"
                    chapter["error"] = (
                        f"节拍为空：设定 {beats_expected} 个"
                        if not beats
                        else f"节拍数越界：设定 {beats_expected} 个，实际 {len(beats)} 个"
                    )
                    save_work(store, work)
                    raise AdapterError(
                        ErrorCode.UPSTREAM_ERROR,
                        f"大纲生成停在第 {act_pos} 幕 · 第 {chapter_pos} 章《{chapter_title}》的「节拍」层：{state}，"
                        f"需要 {beats_expected} 个（允许 ±{_tolerance(beats_expected)} 个），已尝试 {beat_attempts} 次。"
                        "已合格的幕与章已保留，可从该章继续。",
                        retryable=True,
                        detail={
                            "stage": "beats",
                            "act_index": act_pos,
                            "chapter_index": chapter_pos,
                            "chapter": chapter_title,
                            "attempts": beat_attempts,
                            "reason": _count_reason("beats", len(beats), beats_expected),
                            "resume": True,
                            "gate": True,
                            "failures": [
                                {
                                    "act_index": act_pos,
                                    "act_name": act.get("title") or f"第{act_pos}幕",
                                    "kind": f"节拍层为空（第 {chapter_pos} 章）" if not beats else f"节拍层越界（第 {chapter_pos} 章）",
                                    "raw": f"{len(beats)}/{beats_expected}",
                                }
                            ],
                        },
                    )
                chapter["beats"] = beats
                # 节拍与本章 cast 是同一次调用；这里只做确定性的匹配、建卡与绑定
                cast_report = _apply_chapter_cast(
                    store, chapter, payload, chapter_pos, characters, pending, dry_run, on_progress
                )
                cast_created_total += len(cast_report.get("created") or [])
                cast_reused_total += len(cast_report.get("reused") or [])
                chapter["cast_synced"] = True
                chapter["cast_created"] = list(cast_report.get("created") or [])
                chapter["cast_reused"] = list(cast_report.get("reused") or [])
                chapter["cast_extras"] = list(cast_report.get("extras") or [])
                characters = list(cast_report.get("characters") or characters)
                char_rows = _characters_payload(characters)
                chapter["character_arcs"] = _normalize_character_arcs(
                    payload, {str(item.get("id")) for item in characters if item.get("id")}
                )
                chars_done += 1
                chapter["status"] = "done"
                chapter["error"] = None
                beats_done += len(beats)
            except AdapterError as exc:
                if (exc.detail or {}).get("gate"):
                    raise
                chapter["status"] = "failed"
                chapter["error"] = _error_text(exc)
                failures.append(
                    _failure("chapter", chapter["error"], act_pos, act.get("title") or f"第{act_pos}幕", chapter_pos, chapter_title)
                )
            except Exception as exc:  # 单章失败不影响其它章
                chapter["status"] = "failed"
                chapter["error"] = _error_text(exc)
                failures.append(
                    _failure("chapter", chapter["error"], act_pos, act.get("title") or f"第{act_pos}幕", chapter_pos, chapter_title)
                )
            save_work(store, work)

    # 组装成设定库结构：章序号全局连续，复用同序号已有的章节 ID
    existing = {int(item.get("index") or 0): item.get("id") for item in settings_store.all_outline_chapters(bible)}
    built_acts: list[dict] = []
    chapter_count = 0
    beat_count = 0
    for act_pos, act in enumerate(work["acts"], start=1):
        built = SettingsStore.new_act(act.get("title") or f"第{act_pos}幕", act.get("summary") or "")
        for chapter in act.get("chapters") or []:
            chapter_count += 1
            chapter_id = existing.get(chapter_count) or new_id("chp")
            outline_chapter = SettingsStore.new_outline_chapter(
                chapter_id, chapter_count, chapter.get("title") or f"第{chapter_count}章", chapter.get("summary") or ""
            )
            for beat in chapter.get("beats") or []:
                beat_count += 1
                outline_chapter["beats"].append(
                    SettingsStore.new_beat(
                        beat.get("summary") or f"第{beat_count}个节拍",
                        character_ids=beat.get("character_ids") or [],
                        turning_point=bool(beat.get("turning_point")),
                        target_words=int(beat.get("target_words") or chapter_words // max(1, beats_per_chapter)),
                        extra_characters=beat.get("extra_characters") or [],
                    )
                )
            outline_chapter["character_arcs"] = [
                dict(arc) for arc in (chapter.get("character_arcs") or []) if isinstance(arc, dict)
            ]
            # 角色同步结果随章节一起写进设定库：后续据此判断「已识别，不再补跑」
            for key in ("cast_synced", "cast_synced_at", "cast_created", "cast_reused", "cast_extras", "cast_pending"):
                if key in chapter:
                    outline_chapter[key] = chapter[key]
            built["chapters"].append(outline_chapter)
        built_acts.append(built)

    bible.setdefault("outline", {})["acts"] = built_acts
    # 角色引用：先清洗非法引用（人名/空串/重复 ID）；auto 再按幕绑定世界并回落到 POV，none 只清洗
    stats = sanitize_outline_character_ids(bible, characters)
    ignored_character_refs = int(stats["dropped"])
    if binding == "auto":
        _bind_outline_characters(store, bible, characters)
    if ignored_character_refs:
        suffix = "已回落到本章 POV" if binding == "auto" else "binding=none 不自动选角"
        message = f"已忽略 {ignored_character_refs} 个无法识别的角色引用（{suffix}）"
        logger.info(message)
        _progress(on_progress, {"type": "notice", "message": message})
    bound_stats = outline_binding_stats(bible)
    golden = check_golden_three(bible)
    if not dry_run:
        bible = settings_store.save(bible)
    # 生成完成的逐幕校验摘要（幕/章/节拍三层，含实际值、偏差、允许区间与原因）
    validation_rows: list[dict] = []
    for index, act in enumerate(work["acts"], start=1):
        row = _act_validation(act, index, chapters_per_act, beats_per_chapter, chapter_words, "")
        row["rewrites"] = {
            "act_attempts": int(act.get("_chapters_attempts") or 0),
            "unresolved": [] if act.get("status") == "done" else [str(act.get("error") or "")],
            "chapter_issues": {},
        }
        validation_rows.append(row)
    report = _validation_report(
        validation_rows, chapters_per_act, beats_per_chapter, chapter_words, max(1, int(acts))
    )
    return {
        "acts": built_acts,
        "chapters": chapter_count,
        "beats": beat_count,
        "provider": getattr(adapter, "model", "") or "offline-writing",
        "settings_revision": bible.get("settings_revision"),
        "binding": binding,
        "ignored_character_refs": ignored_character_refs,
        "bound_characters": bound_stats["characters"],
        "bound_character_refs": bound_stats["refs"],
        "cast_chapters": chars_done,
        "characters_created": cast_created_total,
        "characters_reused": cast_reused_total,
        "characters_total": len(characters),
        "golden_three": golden,
        "dry_run": dry_run,
        "failures": failures,
        "resumed": bool(resume),
        "work_file": str(work_file(store)),
        "expected": {
            "acts": max(1, int(acts)),
            "chapters_per_act": max(1, int(chapters_per_act)),
            "beats_per_chapter": max(1, int(beats_per_chapter)),
        },
        "actual": {"acts": len(built_acts), "chapters": chapter_count, "beats": beat_count},
        "matches": report["matches"],
        "validation": report,
        "summary": report["summary"],
    }
