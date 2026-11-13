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
from pathlib import Path

from ..adapters.base import ErrorCode, AdapterError, Message, TextGenRequest
from ..config import AppConfig
from ..models.ids import new_id, now_iso
from ..store import ProjectStore
from ..store.settings_store import SettingsStore
from ..text.titles import strip_index_prefix
from .service import (
    _characters_payload,
    _generate_json,
    _progress,
    _tool_max_tokens,
    bind_outline_characters,
    build_writing_adapter,
    check_golden_three,
)

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
    world = bible.get("world") or {}
    era = _text(world.get("era"), 40)
    if era:
        lines.append(f"世界：{era}")
    if characters:
        lines.append("可用人物（id=姓名）：" + "、".join(f"{item['id']}={item.get('name')}" for item in characters))
    return "\n".join(lines)


def _delta_reporter(on_progress, unit: str, index: int, total: int, name: str, extra: dict | None = None):
    """生成 on_delta：累计本层接收长度并上报 chars 事件。"""
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
        try:
            count = int(raw.get("chapters_count") or chapters_per_act)
        except (TypeError, ValueError):
            count = chapters_per_act
        rows.append(
            {
                "title": title,
                "summary": _text(raw.get("summary") or raw.get("goal"), 400),
                "chapters_count": max(1, min(12, count)),
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
        章节列表，每项 `{title, summary, beats, status, error}`。

    异常:
        AdapterError: 未返回可用章节。
    """
    raw = payload.get("chapters") if isinstance(payload, dict) else None
    if not isinstance(raw, list) or not raw:
        raise AdapterError(ErrorCode.INVALID_INPUT, "分层大纲阶段二失败：emit_chapters 未返回非空 chapters 数组")
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
    if not rows:
        raise AdapterError(ErrorCode.INVALID_INPUT, "分层大纲阶段二失败：章节列表里没有可用对象")
    return rows


def _normalize_beats(payload: dict, expected: int, chapter_words: int, beats_per_chapter: int) -> list[dict]:
    """清洗阶段三某一章的节拍列表。

    参数:
        payload: `emit_beats` 的返回值。
        expected: 本章计划节拍数。
        chapter_words: 单章目标字数。
        beats_per_chapter: 未给出目标字数时的默认等分数。

    返回:
        节拍列表。

    异常:
        AdapterError: 未返回可用节拍。
    """
    raw = payload.get("beats") if isinstance(payload, dict) else None
    if not isinstance(raw, list) or not raw:
        raise AdapterError(ErrorCode.INVALID_INPUT, "分层大纲阶段三失败：emit_beats 未返回非空 beats 数组")
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
                "turning_point": bool(item.get("turning_point")),
                "target_words": words,
            }
        )
    if not rows:
        raise AdapterError(ErrorCode.INVALID_INPUT, "分层大纲阶段三失败：节拍列表里没有可用对象")
    return rows


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
            max_tokens=_tool_max_tokens(config, "outline_acts", max(1, int(acts))),
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
        payload = _generate_json(adapter, req, "outline_acts")
        work["acts"] = _normalize_acts(payload, acts, chapters_per_act)
        save_work(store, work)

    # 阶段二：逐幕补章节（已完成的幕跳过，失败只记录）
    act_total = len(work["acts"])
    for act_pos, act in enumerate(work["acts"], start=1):
        if act.get("chapters"):
            continue
        expected = int(act.get("chapters_count") or chapters_per_act)
        name = act.get("title") or f"第{act_pos}幕"
        _progress(on_progress, {"type": "batch", "unit": "act", "index": act_pos, "total": act_total, "name": name})
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
            max_tokens=_tool_max_tokens(config, "outline_chapters", expected),
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
                on_delta=_delta_reporter(on_progress, "act", act_pos, act_total, name),
            )
            act["chapters"] = _normalize_chapters(payload, expected)
            act["status"] = "done"
            act["error"] = None
        except Exception as exc:  # 单幕失败不影响其它幕
            act["status"] = "failed"
            act["error"] = _error_text(exc)
            failures.append(_failure("act", act["error"], act_pos, name))
        save_work(store, work)

    # 阶段三：逐章补齐节拍（按全文章序号显示进度）
    chapter_total = sum(len(act.get("chapters") or []) for act in work["acts"])
    chapter_pos = 0
    for act_pos, act in enumerate(work["acts"], start=1):
        for chapter in act.get("chapters") or []:
            chapter_pos += 1
            if chapter.get("beats"):
                continue
            chapter_title = chapter.get("title") or f"第{chapter_pos}章"
            _progress(
                on_progress,
                {
                    "type": "batch",
                    "unit": "chapter",
                    "index": chapter_pos,
                    "total": chapter_total,
                    "name": chapter_title,
                    "act_index": act_pos,
                    "act_total": act_total,
                    "act_name": act.get("title") or f"第{act_pos}幕",
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
            ]
            if siblings:
                lines.append("本幕其他章节（用于前后衔接）：" + siblings)
            req = TextGenRequest(
                task="outline",
                system="你是长篇小说的结构编辑，只按工具参数输出本章全部节拍，不要解释。",
                messages=[Message("user", "\n".join(lines))],
                stream=True,
                max_tokens=_tool_max_tokens(config, "outline_beats", beats_per_chapter),
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
                payload = _generate_json(
                    adapter,
                    req,
                    "outline_beats",
                    on_delta=_delta_reporter(
                        on_progress,
                        "chapter",
                        chapter_pos,
                        chapter_total,
                        chapter_title,
                        {"act_index": act_pos, "act_total": act_total},
                    ),
                )
                chapter["beats"] = _normalize_beats(payload, beats_per_chapter, chapter_words, beats_per_chapter)
                chapter["status"] = "done"
                chapter["error"] = None
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
                    )
                )
            built["chapters"].append(outline_chapter)
        built_acts.append(built)

    bible.setdefault("outline", {})["acts"] = built_acts
    if binding == "auto":
        bind_outline_characters(store, bible)
    golden = check_golden_three(bible)
    if not dry_run:
        bible = settings_store.save(bible)
    return {
        "acts": built_acts,
        "chapters": chapter_count,
        "beats": beat_count,
        "provider": getattr(adapter, "model", "") or "offline-writing",
        "settings_revision": bible.get("settings_revision"),
        "binding": binding,
        "golden_three": golden,
        "dry_run": dry_run,
        "failures": failures,
        "resumed": bool(resume),
        "work_file": str(work_file(store)),
    }
