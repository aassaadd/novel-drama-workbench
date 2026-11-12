"""大纲与正文续写（R19）：在已有内容之后接着写，只追加不重写。

- 正文续写 `continue_chapter`：从 `chapter.content` 末尾继续写，默认只预览并把结果存为
  「未应用草稿」（`.ndw/drafts/<chapter>.continue.json`）；`dry_run=False` 才写正文，
  且优先消费已有草稿（`--apply`），`--retry` 才会重抽覆盖草稿。
- 大纲续写 `continue_outline`：从已有章节最大序号之后追加新章，按幕生成并逐幕落盘。

上下文仍由 `text.context.ContextAssembler` 装配；本模块只负责「续写编排 + 草稿/落盘语义」，
不改动原有 `chapter generate` / `outline generate` 的行为。
"""

from __future__ import annotations

import json
from dataclasses import replace

from ..adapters.base import AdapterError, Message, TextGenRequest
from ..config import AppConfig
from ..models.ids import new_id, now_iso
from ..store import ProjectStore
from ..store.settings_store import SettingsStore
from ..text.context import ContextAssembler
from .editing import INSTRUCTION_LIMIT, STALE_WARNING, _usage_payload
from .service import (
    DEFAULT_CHAPTER_WORDS,
    _append_segment,
    _characters_payload,
    _continue_instruction,
    _count_words,
    _generate_json,
    _tool_max_tokens,
    build_chapter_context,
    build_writing_adapter,
    stream_text,
)

#: 正文每轮目标字数（默认）
DEFAULT_SEGMENT_WORDS = 800
#: 正文单次最多轮数（默认，页面不暴露，内部多轮）
DEFAULT_MAX_ROUNDS = 3
#: 正文单次新增硬上限（默认）
DEFAULT_MAX_TOTAL_WORDS = 3000
#: 注入「最近原文」的总字数（默认）
DEFAULT_RECENT_CHARS = 2000
#: 注入前情摘要的章数（默认）
DEFAULT_PREVIOUS_SUMMARIES = 5
#: 去重比较窗口（默认）
DEFAULT_OVERLAP_CHARS = 400
#: 大纲续写默认追加章数
DEFAULT_OUTLINE_CHAPTERS = 3
#: 衔接锚点原样引用的字数
ANCHOR_CHARS = 200
#: 「禁止重复」参考的字数（取最近 1–2 个自然段）
FORBID_CHARS = 1000
#: 全剧梗概的字数上限（固定保留，不参与降级裁剪）
DIGEST_LIMIT = 600
#: 一轮新增少于此字数视为复述/卡住
MIN_ADDED_WORDS = 50
#: 中文正文字符上限（schema 为 200000，留出续写余量）
CONTENT_CHAR_LIMIT = 195000
#: 句末标点与换行：用于判断本轮是否停在自然段结束
_BOUNDARY_CHARS = "。！？…”』」\n"


def _cfg_int(config: AppConfig, key: str, default: int, minimum: int = 1) -> int:
    """读取 `writing.continue.*` 配置项并转成整数。

    参数:
        config: 本机配置。
        key: `writing.continue` 下的键名。
        default: 缺失或非法时的默认值。
        minimum: 允许的最小值。

    返回:
        配置值（不小于 minimum）。
    """
    raw = config.get(f"writing.continue.{key}")
    if raw is None:
        return default
    try:
        return max(minimum, int(raw))
    except (TypeError, ValueError):
        return default


def _chapter_plain(chapter: dict) -> str:
    """取章节正文并去掉末尾换行，作为续写起点。

    参数:
        chapter: 章节 dict。

    返回:
        去掉末尾空白的正文；空正文返回空串。
    """
    return str(chapter.get("content") or "").rstrip()


# ---------- 未应用草稿 ----------


def _draft_path(store: ProjectStore, chapter_id: str):
    """返回章节续写草稿的文件路径。

    参数:
        store: 工程。
        chapter_id: 章节 ID。

    返回:
        `.ndw/drafts/<chapter>.continue.json` 的 Path。
    """
    return store.root / ".ndw" / "drafts" / f"{chapter_id}.continue.json"


def _load_draft(store: ProjectStore, chapter_id: str) -> dict | None:
    """读取章节的续写草稿。

    参数:
        store: 工程。
        chapter_id: 章节 ID。

    返回:
        草稿 dict；不存在或损坏时返回 None。
    """
    path = _draft_path(store, chapter_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def load_continue_draft(store: ProjectStore, chapter_id: str) -> dict | None:
    """读取章节的未应用续写草稿（页面刷新后仍能提示）。

    参数:
        store: 工程。
        chapter_id: 章节 ID。

    返回:
        草稿 dict；不存在或损坏时返回 None。
    """
    return _load_draft(store, chapter_id)


def _save_draft(store: ProjectStore, draft: dict) -> str:
    """写入续写草稿（覆盖同名草稿，重抽即覆盖）。

    参数:
        store: 工程。
        draft: 草稿内容。

    返回:
        草稿文件路径字符串。
    """
    path = _draft_path(store, draft["chapter_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(draft, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(path)


def _clear_draft(store: ProjectStore, chapter_id: str) -> None:
    """删除章节的续写草稿（应用成功后调用）。

    参数:
        store: 工程。
        chapter_id: 章节 ID。
    """
    path = _draft_path(store, chapter_id)
    if path.exists():
        path.unlink()


# ---------- 上下文素材 ----------


def _previous_chapters(store: ProjectStore, chapter: dict, limit: int) -> list[dict]:
    """按 index 倒序取前序章节，再转成从旧到新。

    参数:
        store: 工程。
        chapter: 当前章节。
        limit: 最多取几章。

    返回:
        [{index, title, synopsis, content}]；缺摘要的章节由装配器回退正文末尾。
    """
    current_index = int(chapter.get("index") or 0)
    meta = [item for item in store.list_chapters() if int(item.get("index") or 0) < current_index]
    meta.sort(key=lambda item: int(item.get("index") or 0), reverse=True)
    rows: list[dict] = []
    for item in meta[: max(1, limit)]:
        full = store.load_chapter(item["id"])
        rows.append(
            {
                "index": full.get("index"),
                "title": full.get("title"),
                "synopsis": full.get("synopsis") or "",
                "content": full.get("content") or "",
            }
        )
    rows.reverse()
    return rows


def _story_digest(bible: dict, previous: list[dict], limit: int = DIGEST_LIMIT) -> str:
    """从设定库大纲与最近章节摘要派生「全剧梗概」，不额外调用模型。

    参数:
        bible: 设定库。
        previous: 前序章节列表（从旧到新）。
        limit: 字数上限。

    返回:
        梗概文本；没有可用素材时返回空串。
    """
    acts = (bible.get("outline") or {}).get("acts") or []
    lines: list[str] = []
    for index, act in enumerate(acts, start=1):
        title = str(act.get("title") or "").strip() or f"第{index}幕"
        summary = str(act.get("summary") or "").strip()
        lines.append(f"第 {index} 幕《{title}》" + (f"：{summary}" if summary else ""))
    if not lines:
        # 设定库没有大纲时退化为最近章节摘要
        for item in previous[-5:]:
            title = str(item.get("title") or "").strip()
            synopsis = str(item.get("synopsis") or "").strip()
            if not synopsis:
                continue
            lines.append(f"第 {item.get('index')} 章《{title}》" + (f"：{synopsis}" if synopsis else ""))
        limit = min(limit, 1000)
    text = "\n".join(lines)
    if len(text) > limit:
        text = text[:limit].rstrip() + "……"
    return text


def _recent_reference(content: str, limit: int = FORBID_CHARS) -> str:
    """取最近 1–2 个自然段作为「禁止重复」参考。

    参数:
        content: 已有正文。
        limit: 字数上限。

    返回:
        参考文本；空正文返回空串。
    """
    text = content.rstrip()
    if not text:
        return ""
    paragraphs = [item for item in text.split("\n\n") if item.strip()]
    if not paragraphs:
        return text[-limit:]
    picked = paragraphs[-1]
    if len(paragraphs) >= 2 and len(picked) < limit:
        picked = paragraphs[-2] + "\n\n" + picked
    return picked[-limit:]


def _from_beat(from_beat: int | None, written_words: int, chapter_words: int, beat_count: int) -> int:
    """确定本次续写从第几个节拍开始。

    参数:
        from_beat: 用户显式指定的起始节拍（1 起）；None 时按字数比例估算。
        written_words: 本章已写字数。
        chapter_words: 本章目标字数。
        beat_count: 节拍总数。

    返回:
        起始节拍序号（1..beat_count）；没有节拍时返回 1。
    """
    if beat_count <= 0:
        return 1
    if from_beat:
        value = int(from_beat)
    else:
        ratio = written_words / max(1, chapter_words)
        value = int(ratio * beat_count) + 1
    return max(1, min(beat_count, value))


def _build_continue_instruction(
    chapter: dict,
    content: str,
    beats: list[dict],
    from_beat: int,
    seg_words: int,
    total_target: int,
    rounds: int,
    intent: str | None,
    extra: str | None,
) -> str:
    """构造正文续写的任务指令（自由意图 > 衔接锚点 + 剩余节拍 + 禁止重复）。

    参数:
        chapter: 当前章节。
        content: 已有正文。
        beats: 剩余节拍（从 from_beat 开始）。
        from_beat: 起始节拍序号。
        seg_words: 每轮目标字数。
        total_target: 本次计划新增总字数。
        rounds: 计划轮数。
        intent: 用户自由意图「接下来发生什么」（最高优先级，可为空）。
        extra: 兼容旧参数的续写思路（可选）。

    返回:
        任务指令文本。
    """
    lines: list[str] = []
    if intent:
        lines.append(f"本次续写的明确意图（最高优先级，必须照此写）：{intent}")
    lines.append(f"接着《{chapter.get('title') or '本章'}》已有正文的最后一个字，继续写后续正文，不要重写前文。")
    lines.append(
        f"本次计划新增约 {total_target} 字（分 {rounds} 轮，每轮约 {seg_words} 字），当前已有 {_count_words(content)} 字。"
    )
    if beats:
        lines.append(f"当前必须推进的节拍（从第 {from_beat} 个开始，按顺序写，不要跳过节拍）：")
        for offset, beat in enumerate(beats, start=from_beat):
            target = int(beat.get("target_words") or 0)
            lines.append(f"{offset}. {beat.get('summary')}（目标 {target} 字）")
    lines.append(f"【衔接锚点】{content[-ANCHOR_CHARS:]}")
    lines.append("【写作起点】从锚点最后一个字之后开始写第一句，禁止复述锚点里的任何句子或段落。")
    lines.append(f"【禁止重复】以下内容已经写过，不得以任何形式重复：\n{_recent_reference(content)}")
    tail_paragraph = content.split("\n\n")[-1].strip()
    if tail_paragraph:
        lines.append(f"【接续状态】上一段结尾：{tail_paragraph[:200]}")
    if extra and extra != intent:
        lines.append(f"续写思路：{extra}")
    lines.append(
        "要求：本轮写到自然段结束就停，不要停在半句；只输出新增正文，不要写章节标题或小标题，不要解释；对白使用中文引号“”。"
    )
    return "\n".join(lines)


def _continuation_record(mode: str, instruction: str, total_target: int, start_beat: int, previous_count: int, recent_chars: int, budget: int, append_start: int) -> dict:
    """构造一条 `chapter.continuations[]` 记录（schema 已有字段）。

    参数:
        mode: continue / outline_driven。
        instruction: 本次任务指令。
        total_target: 目标新增字数。
        start_beat: 起始节拍。
        previous_count: 注入的前情章节数。
        recent_chars: 注入的最近正文字数。
        budget: 上下文预算。
        append_start: 追加起点（旧正文长度）。

    返回:
        符合 chapter.schema.json 的续写记录。
    """
    return {
        "id": new_id("cnt"),
        "mode": mode,
        "instruction": instruction[:INSTRUCTION_LIMIT],
        "target_words": max(100, min(20000, total_target)),
        "outline_ref": f"beat:{start_beat}",
        "range": None,
        "context": {
            "include_bible": True,
            "include_previous_summaries": min(previous_count, 20),
            "include_recent_chars": min(recent_chars, 100000),
            "budget_tokens": max(1024, min(budget, 262144)),
        },
        "status": "pending",
        "produced": {"char_start": append_start, "char_end": append_start},
        "model": None,
        "usage": None,
        "created_at": now_iso(),
    }


def _apply_draft(store: ProjectStore, chapter: dict, content: str, draft: dict) -> dict:
    """把未应用草稿追加进正文（校验草稿基于的正文未变）。

    参数:
        store: 工程。
        chapter: 章节 dict。
        content: 当前正文（去末尾空白）。
        draft: 草稿 dict。

    返回:
        与 `continue_chapter` 相同形状的结果（额外带 applied_from_draft=True）。

    异常:
        ValueError: 草稿失效或没有可追加内容。
    """
    if int(draft.get("base_char_end") or 0) != len(content) or int(draft.get("base_revision") or 0) != int(chapter.get("revision") or 1):
        raise ValueError("正文已变更，续写草稿已失效：请重抽（加 --retry 重新生成）后再应用")
    merged = _append_segment(content, str(draft.get("text") or ""))
    added = _count_words(merged) - _count_words(content)
    if added <= 0:
        raise ValueError("续写草稿没有可追加的新内容：请重抽（加 --retry 重新生成）")

    has_downstream = bool(chapter.get("scenes") or chapter.get("script_path"))
    warnings: list[str] = []
    if has_downstream:
        warnings.append(STALE_WARNING)
    warnings.append("续写完成后建议重新生成章节摘要（chapter summarize），让后续章节的前情更准确")

    record = _continuation_record(
        str(draft.get("mode") or "outline_driven"),
        str(draft.get("instruction") or ""),
        int(draft.get("target_words") or added),
        int(draft.get("from_beat") or 1),
        int((draft.get("context") or {}).get("include_previous_summaries") or 0),
        int((draft.get("context") or {}).get("include_recent_chars") or 0),
        int((draft.get("context") or {}).get("budget_tokens") or 32768),
        len(content),
    )
    record["status"] = "done"
    record["produced"] = {"char_start": len(content), "char_end": len(merged)}
    record["model"] = str(draft.get("model") or "")[:200] or None

    chapter["content"] = merged + "\n"
    chapter["word_count"] = _count_words(merged)
    chapter["status"] = "stale" if has_downstream else "written"
    chapter["revision"] = int(chapter.get("revision") or 1) + 1
    continuations = chapter.get("continuations")
    if not isinstance(continuations, list):
        continuations = []
    continuations.append(record)
    chapter["continuations"] = continuations
    store.save_chapter(chapter)
    _clear_draft(store, draft["chapter_id"])

    return {
        "chapter_id": chapter["id"],
        "mode": record["mode"],
        "dry_run": False,
        "applied_from_draft": True,
        "added_words": added,
        "before_words": _count_words(content),
        "after_words": chapter["word_count"],
        "append_start": len(content),
        "preview": draft.get("text") or "",
        "rounds": int(draft.get("rounds") or 0),
        "stop_reason": str(draft.get("stop_reason") or "draft"),
        "from_beat": int(draft.get("from_beat") or 1),
        "covered_beats": draft.get("covered_beats") or [],
        "context_warnings": [],
        "provider": str(draft.get("provider") or ""),
        "status": chapter["status"],
        "revision": chapter["revision"],
        "continuation_id": record["id"],
        "warnings": warnings,
    }


def continue_chapter(
    store: ProjectStore,
    config: AppConfig,
    chapter_id: str,
    words: int | None = None,
    rounds: int | None = None,
    intent: str | None = None,
    instruction: str | None = None,
    from_beat: int | None = None,
    dry_run: bool = True,
    retry: bool = False,
    on_delta=None,
) -> dict:
    """在已有正文末尾续写；默认只预览并把结果存为未应用草稿。

    参数:
        store: 工程。
        config: 本机配置。
        chapter_id: 章节 ID。
        words: 每轮目标字数；None 时取 `writing.continue.segment_words`（默认 800）。
        rounds: 最大轮数（页面不暴露）；None 时取 `writing.continue.max_rounds`（默认 3）。
        intent: 用户自由意图「接下来发生什么」（最高优先级，可为空）。
        instruction: 兼容旧参数的续写思路（可选）。
        from_beat: 起始节拍（1 起）；None 时按已写字数比例估算。
        dry_run: True 只生成预览并保存草稿；False 写正文。
        retry: True 忽略已有草稿重新生成（重抽）；False 时 `--apply` 优先应用已有草稿。
        on_delta: 流式增量回调 `(text) -> None`；传入时改为流式生成，仅用于页面实时预览。

    返回:
        {"chapter_id", "mode", "dry_run", "added_words", "before_words", "after_words",
         "append_start", "preview", "rounds", "stop_reason", "from_beat", "covered_beats",
         "context_warnings", "provider", "status", "revision", "warnings", "draft_path"}；
        失败时额外带 "error"（已生成内容会保留为草稿）。

    异常:
        ValueError: 正文为空、接近长度上限或草稿已失效。
    """
    chapter = store.load_chapter(chapter_id)
    content = _chapter_plain(chapter)
    if not content:
        raise ValueError(f"章节 {chapter_id} 还没有正文：先用 chapter generate 生成一部分，或 chapter save 导入正文后再续写")
    if len(content) >= CONTENT_CHAR_LIMIT:
        raise ValueError(f"章节 {chapter_id} 已接近长度上限（{CONTENT_CHAR_LIMIT} 字符），无法继续追加")

    # 应用模式优先消费上一次预览留下的草稿：不重复调用模型，也不追加第二份
    if not dry_run and not retry:
        draft = _load_draft(store, chapter_id)
        if draft:
            try:
                return _apply_draft(store, chapter, content, draft)
            except ValueError:
                # 草稿基于的正文已变：清掉失效草稿，按新正文重新生成后再应用
                _clear_draft(store, chapter_id)
    if retry:
        # 重抽：旧草稿作废，避免生成失败后留下误导性的旧稿
        _clear_draft(store, chapter_id)

    context = build_chapter_context(store, config, chapter)
    beats = context["beats"] or []
    chapter_words = int(context["chapter_words"] or DEFAULT_CHAPTER_WORDS) or DEFAULT_CHAPTER_WORDS

    seg_words = max(100, int(words or _cfg_int(config, "segment_words", DEFAULT_SEGMENT_WORDS)))
    max_rounds = max(1, int(rounds or _cfg_int(config, "max_rounds", DEFAULT_MAX_ROUNDS)))
    max_total_words = max(seg_words, _cfg_int(config, "max_total_words", DEFAULT_MAX_TOTAL_WORDS))
    recent_chars = _cfg_int(config, "recent_chars", DEFAULT_RECENT_CHARS, minimum=400)
    previous_limit = _cfg_int(config, "previous_summaries", DEFAULT_PREVIOUS_SUMMARIES)
    overlap_chars = _cfg_int(config, "overlap_chars", DEFAULT_OVERLAP_CHARS, minimum=50)

    written_words = _count_words(content)
    start_beat = _from_beat(from_beat, written_words, chapter_words, len(beats))
    remaining_beats = beats[start_beat - 1 :] if beats else []
    total_target = min(seg_words * max_rounds, max_total_words)

    previous = _previous_chapters(store, chapter, previous_limit)
    digest = _story_digest(context["bible"], previous)
    instruction_text = _build_continue_instruction(
        chapter, content, remaining_beats, start_beat, seg_words, total_target, max_rounds, intent, instruction
    )

    budget = int(config.get("writing.context_budget_tokens") or 32768)
    assembler = ContextAssembler(
        work=context["work"],
        bible=context["bible"],
        world_digest=context["world_context"],
        characters=context["characters"],
        chapter=chapter,
        outline_chapter=context["entry"] or {},
        previous_chapters=previous,
        current_content=content[-recent_chars:],
        budget_tokens=budget,
        excerpt_chars=recent_chars,
        story_digest=digest,
    )
    req = assembler.build("chapter", instruction_text, context["speaker_ids"])
    req.metadata = context["metadata"]
    mt = config.get("writing.multi_turn") or {}
    req.max_tokens = max(1, int(mt.get("round_max_tokens") or 2048))
    work_id = str(context["work"].get("id") or "").strip()
    if work_id:
        req.prompt_cache_key = f"{work_id}:{chapter_id}:continue"

    context_warnings = list(assembler.warnings)
    has_downstream = bool(chapter.get("scenes") or chapter.get("script_path"))
    warnings = list(context_warnings)
    if has_downstream:
        warnings.append(STALE_WARNING)
    warnings.append("续写完成后建议重新生成章节摘要（chapter summarize），让后续章节的前情更准确")

    mode = "outline_driven" if beats else "continue"
    append_start = len(content)
    record = _continuation_record(
        mode, instruction_text, total_target, start_beat, len(previous), min(len(content), recent_chars), budget, append_start
    )
    record["status"] = "running"

    base_messages = list(req.messages) + [
        Message("assistant", content[-recent_chars:]),
        Message("user", _continue_instruction(seg_words)),
    ]

    current = content
    added_total = 0
    added_text = ""
    pending_text = ""
    rounds_used = 0
    stop_reason = "max_rounds"
    provider = ""
    error_note: str | None = None
    record_saved = False

    for index in range(1, max_rounds + 1):
        if index == 1:
            request = replace(req, messages=base_messages)
        else:
            request = replace(
                req,
                messages=base_messages
                + [Message("assistant", added_text), Message("user", _continue_instruction(seg_words))],
            )
        try:
            result = stream_text(context["adapter"], request, on_delta) if on_delta else context["adapter"].generate(request)
        except AdapterError as exc:
            error_note = f"第 {index} 轮续写失败：{exc}"
            record["status"] = "failed"
            break
        except Exception as exc:  # 适配器内部异常统一转成中文可读错误
            error_note = f"第 {index} 轮续写失败：模型调用异常（{exc}）"
            record["status"] = "failed"
            break

        provider = getattr(result, "model", "") or provider
        record["usage"] = _usage_payload(result) or record.get("usage")
        segment = (result.text or "").strip()
        rounds_used = index
        if not segment:
            stop_reason = "empty_output"
            break

        candidate = _append_segment(current, segment, max_overlap=overlap_chars)
        added = _count_words(candidate) - _count_words(current)
        if added < MIN_ADDED_WORDS:
            # 本轮基本在复述上一段：不写正文，整轮作为「未应用草稿」留给用户确认或重抽
            stop_reason = "short_output"
            pending_text = segment
            break

        current = candidate
        added_total += added
        added_text = (added_text + "\n\n" + segment).strip()

        if not dry_run:
            record["produced"] = {"char_start": append_start, "char_end": len(current)}
            record["model"] = (provider or "")[:200] or None
            chapter["content"] = current + "\n"
            chapter["word_count"] = _count_words(current)
            chapter["status"] = "stale" if has_downstream else "written"
            chapter["revision"] = int(chapter.get("revision") or 1) + 1
            continuations = chapter.get("continuations")
            if not isinstance(continuations, list):
                continuations = []
            if not record_saved:
                continuations.append(record)
                record_saved = True
            chapter["continuations"] = continuations
            store.save_chapter(chapter)

        if added_total >= max_total_words:
            stop_reason = "max_total_words"
            break
        if added_total >= total_target * 1.1:
            stop_reason = "target_reached"
            break
        if added_total >= total_target * 0.8 and current and current[-1] in _BOUNDARY_CHARS:
            stop_reason = "paragraph_boundary"
            break

    if not dry_run and record_saved:
        if record["status"] != "failed":
            record["status"] = "done"
        record["produced"] = {"char_start": append_start, "char_end": len(current)}
        store.save_chapter(chapter)

    # 未应用草稿：预览结果，或失败/复述轮留下的内容，都保存下来供用户确认或重抽
    if dry_run:
        draft_text = added_text or pending_text
    else:
        draft_text = pending_text if (pending_text and not record_saved) else ""
    draft_path = ""
    if draft_text:
        draft_path = _save_draft(
            store,
            {
                "chapter_id": chapter_id,
                "base_revision": int(chapter.get("revision") or 1),
                "base_char_end": append_start,
                "mode": mode,
                "intent": intent or "",
                "instruction": instruction_text[:INSTRUCTION_LIMIT],
                "target_words": max(100, min(20000, total_target)),
                "from_beat": start_beat,
                "covered_beats": list(range(start_beat, len(beats) + 1)) if beats else [],
                "rounds": rounds_used,
                "stop_reason": stop_reason,
                "provider": provider,
                "model": (provider or "")[:200],
                "text": draft_text,
                "error": error_note,
                "created_at": now_iso(),
                "status": "draft",
                "context": record.get("context") or {},
            },
        )

    if error_note and added_total:
        error_note = f"{error_note}；已保留前 {rounds_used} 轮共 {added_total} 字，下次从新的正文末尾继续"
    elif error_note:
        error_note = f"{error_note}；本次未写入正文"

    after_words = _count_words(current)
    response = {
        "chapter_id": chapter_id,
        "mode": mode,
        "dry_run": dry_run,
        "applied_from_draft": False,
        "added_words": after_words - written_words,
        "before_words": written_words,
        "after_words": after_words,
        "append_start": append_start,
        "preview": added_text if dry_run else (pending_text or ""),
        "rounds": rounds_used,
        "stop_reason": stop_reason,
        "from_beat": start_beat,
        "covered_beats": list(range(start_beat, len(beats) + 1)) if beats else [],
        "context_warnings": context_warnings,
        "provider": provider,
        "status": chapter.get("status"),
        "revision": chapter.get("revision"),
        "continuation_id": record["id"] if record_saved else None,
        "draft_path": draft_path,
        "warnings": warnings,
        # 回显续写需求，页面可在预览弹窗里确认「按这个要求续写」
        "intent": str(intent or "").strip(),
    }
    if error_note:
        response["error"] = error_note
    return response


def _build_outline_act(
    raw_act: dict,
    act_no: int,
    start_index: int,
    chapter_count: int,
    beats_per_chapter: int,
    chapter_words: int,
) -> dict:
    """把模型返回的一幕转成设定库结构，章节序号从 start_index 起重新编号。

    参数:
        raw_act: 模型返回的一幕。
        act_no: 幕序号（用于兜底标题）。
        start_index: 本幕首章的全书序号。
        chapter_count: 本幕应包含的章数。
        beats_per_chapter: 每章节拍数。
        chapter_words: 单章目标字数。

    返回:
        可直接追加进 `outline.acts` 的幕 dict。
    """
    act = SettingsStore.new_act(raw_act.get("title") or f"第{act_no}幕", raw_act.get("summary") or "")
    raw_chapters = [item for item in (raw_act.get("chapters") or []) if isinstance(item, dict)]
    for offset in range(chapter_count):
        raw = raw_chapters[offset] if offset < len(raw_chapters) else {}
        index = start_index + offset
        outline_chapter = SettingsStore.new_outline_chapter(
            new_id("chp"), index, raw.get("title") or f"第{index}章", raw.get("summary") or ""
        )
        raw_beats = [item for item in (raw.get("beats") or []) if isinstance(item, dict)]
        if not raw_beats:
            raw_beats = [{"summary": outline_chapter["summary"] or "推进主线"}]
        for raw_beat in raw_beats[: max(1, beats_per_chapter)]:
            outline_chapter["beats"].append(
                SettingsStore.new_beat(
                    raw_beat.get("summary") or f"第{index}章节拍",
                    character_ids=raw_beat.get("character_ids") or [],
                    turning_point=bool(raw_beat.get("turning_point")),
                    target_words=int(raw_beat.get("target_words") or chapter_words // max(1, beats_per_chapter)),
                )
            )
        act["chapters"].append(outline_chapter)
    return act


def _check_outline_indexes(outline_indexes: list[int], file_indexes: list[int]) -> int:
    """检查大纲章节序号是否连续，并返回续写起点。

    参数:
        outline_indexes: 设定库大纲中的章节序号。
        file_indexes: 章节文件中的章节序号。

    返回:
        续写起始序号（最大序号 + 1）。

    异常:
        ValueError: 存在序号空洞或大纲与章节文件不一致。
    """
    if outline_indexes:
        ordered = sorted(set(item for item in outline_indexes if item > 0))
        expected = list(range(1, (ordered[-1] if ordered else 0) + 1))
        missing = [item for item in expected if item not in ordered]
        if missing:
            raise ValueError(f"大纲章节序号存在空洞（缺第 {'、'.join(str(item) for item in missing)} 章），请先修复后再续写")
    if outline_indexes and file_indexes and max(outline_indexes) != max(file_indexes):
        raise ValueError(
            f"大纲与章节文件序号冲突：大纲到第 {max(outline_indexes)} 章，章节文件到第 {max(file_indexes)} 章，"
            "请先执行 outline apply 或修复序号后再续写"
        )
    return max(list(outline_indexes) + list(file_indexes) + [0]) + 1


def continue_outline(
    store: ProjectStore,
    config: AppConfig,
    chapters: int | None = None,
    acts: int | None = None,
    chapters_per_act: int = 3,
    beats_per_chapter: int = 3,
    chapter_words: int = DEFAULT_CHAPTER_WORDS,
    brief: str | None = None,
    intent: str | None = None,
    dry_run: bool = True,
) -> dict:
    """在已有大纲之后追加若干章（跨幕自动建幕），默认只预览。

    参数:
        store: 工程。
        config: 本机配置。
        chapters: 本次追加章数；与 `acts` 同时给出时以本参数为准。
        acts: 本次追加幕数（章数 = acts × chapters_per_act）。
        chapters_per_act: 每幕章数。
        beats_per_chapter: 每章节拍数。
        chapter_words: 单章目标字数。
        brief: 补充要求（可选）。
        intent: 本次续写的明确意图（最高优先级，可留空）；留空时按现有大纲自然续写。
        dry_run: True 只返回预览不写盘；False 逐幕写入设定库。

    返回:
        {"dry_run", "start_index", "acts_added", "chapters_added", "new_acts",
         "new_chapters", "provider", "settings_revision", "applied", "warnings"}；
        中途失败时额外带 "error"，已写入的幕保留。

    异常:
        ValueError: 大纲章节序号存在空洞，或大纲与章节文件序号冲突。
    """
    settings_store = SettingsStore(store.root)
    bible = settings_store.ensure()
    existing_acts = list((bible.get("outline") or {}).get("acts") or [])
    warnings: list[str] = []

    outline_indexes = [int(item.get("index") or 0) for item in settings_store.all_outline_chapters(bible)]
    file_indexes = [int(item.get("index") or 0) for item in store.list_chapters()]
    start_index = _check_outline_indexes(outline_indexes, file_indexes)

    per_act = max(1, int(chapters_per_act))
    if chapters is None:
        chapters = max(1, int(acts) * per_act) if acts else _cfg_int(config, "outline_chapters_per_turn", DEFAULT_OUTLINE_CHAPTERS)
    chapters = max(1, int(chapters))
    new_act_count = (chapters + per_act - 1) // per_act

    project = store.load_project()
    work = project.get("work") or {}
    characters = store.load_characters()["characters"]
    char_rows = _characters_payload(characters)
    logline = (bible.get("premise") or {}).get("logline")
    genre = work.get("genre") or []
    adapter = build_writing_adapter(config, bible)

    context_lines = [
        f"为《{work.get('title') or '未命名'}》续写大纲：接着第 {start_index - 1} 章之后新增 {chapters} 章，"
        f"每幕 {per_act} 章，每章 {beats_per_chapter} 个节拍。"
    ]
    intent_text = str(intent or "").strip()
    if intent_text:
        # 用户填写的续写需求优先级最高：后续每轮都带着这条，避免跨幕跑偏
        context_lines.append(f"本次续写的明确意图（最高优先级，必须照此写）：{intent_text}")
    if logline:
        context_lines.append(f"一句话故事：{logline}")
    if genre:
        context_lines.append(f"题材：{'、'.join(str(item) for item in genre)}")
    if char_rows:
        context_lines.append("可用人物（id=姓名）：" + "、".join(f"{item['id']}={item.get('name')}" for item in char_rows))
    # 上一章结尾原文：让新章能接住语气、场景与人物状态
    files = store.list_chapters()
    if files:
        last_meta = max(files, key=lambda item: int(item.get("index") or 0))
        last_chapter = store.load_chapter(last_meta["id"])
        last_tail = str(last_chapter.get("content") or "").strip()
        if last_tail:
            context_lines.append(
                f"上一章（第 {last_chapter.get('index')} 章《{last_chapter.get('title')}》）结尾原文：\n{last_tail[-800:]}"
            )

    req = TextGenRequest(
        task="outline",
        system="你是长篇小说的结构编辑，按工具参数输出大纲，不要解释。",
        messages=[Message("user", "\n".join(context_lines))],
        stream=True,
        metadata={
            "acts": new_act_count,
            "chapters_per_act": per_act,
            "beats_per_chapter": beats_per_chapter,
            "chapter_words": chapter_words,
            "title": work.get("title"),
            "logline": logline,
            "genre": genre,
            "characters": char_rows,
        },
    )

    done_lines = [
        f"第 {index} 幕《{act.get('title') or ''}》：{act.get('summary') or ''}"
        for index, act in enumerate(existing_acts, start=1)
    ]
    built_acts: list[dict] = []
    built_chapters: list[dict] = []
    applied = 0
    error: str | None = None

    for act_offset in range(new_act_count):
        act_no = len(existing_acts) + act_offset + 1
        first_index = start_index + len(built_chapters)
        remaining = chapters - len(built_chapters)
        count = min(per_act, remaining)
        lines = [f"请生成第 {act_no} 幕（从第 {first_index} 章到第 {first_index + count - 1} 章，共 {count} 章，每章 {beats_per_chapter} 个节拍）。"]
        if done_lines:
            lines.append("已完成（不要重复）：\n" + "\n".join(done_lines[-8:]))
        if brief:
            lines.append(f"补充要求：{brief}")
        request = replace(req, messages=list(req.messages) + [Message("user", "\n".join(lines))])
        # 单幕输出预算按本幕章数 × 每章节拍数估算，避免沿用默认 2048 导致参数被截断
        request.max_tokens = _tool_max_tokens(config, "act", per_act * beats_per_chapter)
        request.metadata = dict(req.metadata or {}, act_offset=act_offset)
        try:
            payload = _generate_json(adapter, request, "act")
        except AdapterError as exc:
            error = f"第 {act_no} 幕生成失败：{exc}"
            break
        act = payload.get("act") if isinstance(payload, dict) else None
        if not isinstance(act, dict) and isinstance(payload, dict):
            fallback = payload.get("acts")
            if isinstance(fallback, list) and fallback and isinstance(fallback[0], dict):
                act = fallback[0]
        if not isinstance(act, dict) or not act:
            error = f"第 {act_no} 幕生成失败：工具调用未返回 act 对象"
            break

        built = _build_outline_act(act, act_no, first_index, count, beats_per_chapter, chapter_words)
        built_acts.append(built)
        built_chapters.extend(built["chapters"])
        done_lines.append(f"第 {act_no} 幕《{built.get('title')}》：{built.get('summary') or ''}")

        if not dry_run:
            outline = bible.setdefault("outline", {})
            outline.setdefault("acts", []).append(built)
            bible = settings_store.save(bible)
            applied += 1

    if error:
        warnings.append(error)
    result = {
        "dry_run": dry_run,
        "start_index": start_index,
        "acts_added": len(built_acts),
        "chapters_added": len(built_chapters),
        "planned_chapters": chapters,
        "new_acts": [
            {"title": item.get("title"), "summary": item.get("summary"), "chapters": len(item.get("chapters") or [])}
            for item in built_acts
        ],
        "new_chapters": [
            {"index": item.get("index"), "title": item.get("title"), "summary": item.get("summary")}
            for item in built_chapters
        ],
        "provider": getattr(adapter, "model", "") or "offline-writing",
        "settings_revision": bible.get("settings_revision"),
        "applied": applied,
        "warnings": warnings,
        "intent": intent_text,
        "brief": str(brief or "").strip(),
    }
    if error:
        result["error"] = error
    return result
