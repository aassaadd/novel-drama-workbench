"""章节上下文装配与流式生成：把设定库、角色、世界、最近摘要装进提示词。"""

from __future__ import annotations

from dataclasses import replace

from ...adapters.errors import AdapterError, ErrorCode
from ...adapters.llm.contracts import TextGenRequest, TextGenResult
from ...adapters.observability import set_llm_abort_reason, set_llm_call_reason
from ...adapters.persistence import ProjectStore
from ...adapters.persistence.settings_store import SettingsStore
from ...adapters.persistence.world_store import WorldStore
from ...application.story.world import worlds_digest
from ...infrastructure.config import AppConfig
from ...models import entities
from ..support.llm_calls import _characters_payload, build_writing_adapter
from .apply_outline import _chapter_beats
from .chapter_limits import DEFAULT_CHAPTER_WORDS
from .degeneration import (
    DegenerationGuard,
)


def build_chapter_context(store: ProjectStore, config: AppConfig, chapter: dict) -> dict:
    """准备章节生成与 LLM 编辑共用的上下文素材。

    参数:
        store: 工程。
        config: 本机配置。
        chapter: 章节 dict（含 id/index/title/outline/content）。

    返回:
        {bible, work, characters, beats, chapter_words, entry, world_ids, world_context,
         character_payload, speaker_ids, previous_chapters, book_digest, style, adapter, metadata}。
        metadata 字段与 `generate_chapter` 原有约定完全一致，离线适配器行为不变。
    """
    settings_store = SettingsStore(store.root)
    bible = settings_store.ensure()
    work = store.load_project().get("work") or {}
    characters = store.load_characters()["characters"]
    beats = _chapter_beats(bible, chapter, settings_store)
    chapter_words = int(sum(int(beat.get("target_words") or 0) for beat in beats) or config.get("writing.default_chapter_words") or 1200)
    if chapter_words <= 0:
        chapter_words = DEFAULT_CHAPTER_WORDS
    adapter = build_writing_adapter(config, bible)

    entry = settings_store.find_outline_chapter(bible, chapter.get("id"), index=int(chapter.get("index") or 0))
    world_ids = list((entry or {}).get("world_ids") or [])
    world_store = WorldStore(store.root)
    world_context = _worlds_digest(world_store.find_many(world_ids) if (world_ids and world_store.exists()) else [], world_ids)
    if world_context is None and world_store.exists():
        # 未绑定世界时回退到世界库里的第一套世界（总述 + 分类条目），写作设定不再保存世界观
        all_worlds = world_store.list_worlds()
        if all_worlds:
            world_context = _worlds_digest(all_worlds, [str(all_worlds[0].get("id") or "")])

    character_payload = _characters_payload(characters)
    # POV 以设定库为唯一主源；章节文件里的 pov_character_id 只是副本（保存时写通）
    pov_character_id = (entry or {}).get("pov_character_id") or chapter.get("pov_character_id")
    if pov_character_id == entities.NARRATOR_ID:
        pov_character_id = None
    # 章节视角覆盖优先于全书；显式全知时不要求绑定具体角色
    pov_mode = (entry or {}).get("pov_mode") or chapter.get("pov_mode") or None
    name_by_id = {item.get("id"): item.get("name") for item in characters if item.get("id")}
    planned_counts: dict[str, int] = {}
    for beat in beats:
        for cid in beat.get("character_ids") or []:
            if cid:
                planned_counts[str(cid)] = planned_counts.get(str(cid), 0) + 1
    speaker_ids: list[str] | None = None
    if entry:
        # 角色上下文取本章 POV 与节拍涉及的角色；找不到时保留全量角色
        focus = {cid for cid in [pov_character_id] if cid}
        focus.update(planned_counts)
        focus.discard(entities.NARRATOR_ID)
        focused = [item for item in character_payload if item["id"] in focus]
        if focused:
            character_payload = focused
        if focus:
            speaker_ids = sorted(focus)

    # 前序章节：index 小于当前章节，取最近 N 章（默认 6），再转成从旧到新
    previous_index = int(chapter.get("index") or 0)
    try:
        recent_limit = max(1, int(config.get("writing.context.recent_summaries") or 6))
    except (TypeError, ValueError):
        recent_limit = 6
    previous_meta = [item for item in store.list_chapters() if int(item.get("index") or 0) < previous_index]
    previous_meta.sort(key=lambda item: int(item.get("index") or 0), reverse=True)
    selected = sorted(previous_meta[:recent_limit], key=lambda item: int(item.get("index") or 0))
    previous_chapters: list[dict] = []
    for position, meta in enumerate(selected):
        # 只有最后一章需要正文末尾做「上一章结尾」摘录，其余章只带摘要，避免逐章读全文
        content = ""
        synopsis = meta.get("synopsis") or ""
        if position == len(selected) - 1:
            full = store.load_chapter(meta["id"])
            content = full.get("content") or ""
            synopsis = synopsis or full.get("synopsis") or ""
        previous_chapters.append(
            {
                "index": meta.get("index"),
                "title": meta.get("title"),
                "synopsis": synopsis,
                "content": content,
            }
        )

    # 全书（或前半部）滚动摘要：与最近章摘要一起注入，长书不会退化成只看最近几章
    book_digest = str(store.load_rolling_summary().get("book_summary") or "")

    style = bible.get("style") or {}
    pov_name = None
    if pov_character_id:
        pov_name = name_by_id.get(pov_character_id) or "已删除角色"
    beat_payload = [
        {
            "id": beat.get("id"),
            "index": position,
            "summary": beat.get("summary"),
            "character_ids": list(beat.get("character_ids") or []),
            "character_names": [
                name_by_id.get(str(cid)) or "已删除角色"
                for cid in (beat.get("character_ids") or [])
                if str(cid).strip()
            ],
            "turning_point": bool(beat.get("turning_point")),
            "target_words": int(beat.get("target_words") or 0),
        }
        for position, beat in enumerate(beats, start=1)
    ]
    metadata = {
        "title": chapter.get("title"),
        "summary": (entry or {}).get("summary") or chapter.get("outline"),
        "beats": beats,
        "beat_characters": beat_payload,
        "characters": character_payload,
        "character_names": name_by_id,
        "planned_counts": planned_counts,
        "world": world_context,
        "world_ids": world_ids,
        "pov_character_id": pov_character_id,
        "pov_name": pov_name,
        "pov_mode": pov_mode,
        "pov": style.get("pov"),
        "tone": style.get("tone"),
        "target_words": chapter_words,
    }
    return {
        "bible": bible,
        "work": work,
        "characters": characters,
        "beats": beats,
        "chapter_words": chapter_words,
        "entry": entry,
        "world_ids": world_ids,
        "world_context": world_context,
        "character_payload": character_payload,
        "speaker_ids": speaker_ids,
        "pov_character_id": pov_character_id,
        "pov_mode": pov_mode,
        "planned_counts": planned_counts,
        "previous_chapters": previous_chapters,
        "book_digest": book_digest,
        "style": style,
        "adapter": adapter,
        "metadata": metadata,
    }

def stream_text(
    adapter,
    request: TextGenRequest,
    on_delta,
    guard: DegenerationGuard | None = None,
    on_notice=None,
) -> TextGenResult:
    """流式调用写作适配器，逐段回调增量文本并聚合为 TextGenResult。

    适配器不支持流式（抛 NotImplementedError）时回落到一次性生成，
    并把整段文本作为一次增量回调，保证调用方逻辑一致。
    传入 `guard` 时在接收过程中检测重复退化：命中立即停止读取并抛
    `DegenerationDetected`，异常里带可读原因、已接收字数与建议保留长度
    （截到循环起点前一句），由调用方决定局部续写还是整节重写。

    「输出不设人为上限」的补充保障：正文为空且上游因额度用尽而结束时
    （几乎都是思考把额度吃光），关掉思考重试一次；仍为空则报明确错误，
    不返回空正文、不落盘。

    参数:
        adapter: 写作适配器。
        request: 统一文本请求。
        on_delta: 增量回调 `(text) -> None`；可为 None。
        guard: 退化检测器；None 时不做检测。
        on_notice: 自动补救说明回调 `(text) -> None`；可为 None。

    返回:
        聚合后的 TextGenResult。

    异常:
        DegenerationDetected: 命中重复退化，携带可读原因与已接收字数。
        AdapterError: 正文为空且关闭思考重试后仍为空。
    """
    result = _stream_once(adapter, request, on_delta, guard)
    if (result.text or "").strip() or result.finish_reason != "length":
        return result

    # 正文为空 + 额度用尽：思考占满了输出额度，关掉思考再试一次
    set_llm_call_reason("empty_body_retry", "stream_text: 空正文重试（关闭思考）")
    if on_notice:
        on_notice("本轮正文为空且额度用尽（思考占满额度），已关闭思考重试一次")
    retry_request = replace(
        request,
        metadata={**(request.metadata or {}), "enable_thinking": False},
    )
    retried = _stream_once(adapter, retry_request, on_delta, guard)
    if (retried.text or "").strip():
        return retried

    set_llm_call_reason("thinking_exhausted", "stream_text: 关闭思考重试仍无正文")
    raise AdapterError(
        ErrorCode.UPSTREAM_ERROR,
        "本轮正文为空：思考占满输出额度（finish_reason=length），关闭思考重试后仍无正文；本轮未写入章节",
        retryable=True,
        detail={"code": "thinking_exhausted"},
    )


def _stream_once(
    adapter,
    request: TextGenRequest,
    on_delta,
    guard: DegenerationGuard | None,
) -> TextGenResult:
    """执行一次流式（或回落一次性）调用，聚合文本与结束原因。

    参数:
        adapter: 写作适配器。
        request: 统一文本请求。
        on_delta: 增量回调；可为 None。
        guard: 退化检测器；None 时不做检测。

    返回:
        聚合后的 TextGenResult（finish_reason 取上游 done 块上的值）。

    异常:
        DegenerationDetected: 命中重复退化。
    """
    parts: list[str] = []
    usage = None
    finish = "stop"
    if on_delta is None:
        # 非流式：一次拿到全文，退化检测与流式共用同一个 guard
        result = adapter.generate(request)
        if guard is not None and result.text:
            reason = guard.feed(result.text) or guard.check_all()
            if reason:
                raise guard.exception(len(result.text))
        return result
    try:
        for chunk in adapter.stream(request):
            if chunk.delta:
                parts.append(chunk.delta)
                if guard is not None:
                    reason = guard.feed(chunk.delta)
                    if reason:
                        # 日志原因码：本轮是我们自己因退化主动停流的
                        set_llm_abort_reason("degeneration", "service.stream_text: 退化熔断")
                        raise guard.exception(len("".join(parts)))
                if on_delta is not None:
                    on_delta(chunk.delta)
            if chunk.usage is not None:
                usage = chunk.usage
            if chunk.finish_reason:
                finish = str(chunk.finish_reason)
            if chunk.done:
                # 上游已给 finish_reason：属于正常结束，关闭迭代器不应记成「调用方断开」
                set_llm_abort_reason("natural_end", "service.stream_text: done 后停止读取")
                break
    except NotImplementedError:
        result = adapter.generate(request)
        if guard is not None and result.text:
            reason = guard.feed(result.text) or guard.check_all()
            if reason:
                raise guard.exception(len(result.text)) from None
        if result.text and on_delta is not None:
            on_delta(result.text)
        return result
    # 收尾再查一次：增量不足一个检查步长时（短输出）也要判定
    if guard is not None and guard.text:
        reason = guard.check_all()
        if reason:
            raise guard.exception(len(guard.text))
    return TextGenResult(
        text="".join(parts),
        finish_reason=finish,
        model=str(getattr(adapter, "model", "") or ""),
        usage=usage,
    )

def _bible_digest(bible: dict) -> dict:
    """整理写作设定（故事层）摘要，供提示词与增量生成使用。

    世界观与地点已归世界库，这里只保留故事内核与文风。

    参数:
        bible: 设定库 dict（可为空）。

    返回:
        {logline, theme, core_conflict, target_length, target_words, tone, pov}。
    """
    premise = bible.get("premise") or {}
    style = bible.get("style") or {}
    return {
        "logline": premise.get("logline"),
        "theme": premise.get("theme"),
        "core_conflict": premise.get("core_conflict"),
        "target_length": premise.get("target_length"),
        "target_words": premise.get("target_words"),
        "tone": style.get("tone"),
        "pov": style.get("pov"),
    }

def _project_context(
    work: dict,
    bible: dict,
    characters: list[dict] | None = None,
    worlds: list[dict] | None = None,
) -> str:
    """把项目基础信息拼成提示词上下文，保证生成内容不脱离小说设定。

    参数:
        work: 作品信息（title/author/genre）。
        bible: 设定库（premise/style）。
        characters: 已有角色表，用于避免重名并呼应既有关系。
        worlds: 世界库（每套含世界观总述与分类条目）。

    返回:
        多行上下文文本；一条信息都没有时返回空串。
    """
    premise = bible.get("premise") or {}
    style = bible.get("style") or {}
    lines: list[str] = []

    title = work.get("title") or "未命名"
    genres = "、".join(str(item) for item in (work.get("genre") or []) if item)
    lines.append(f"作品：{title}" + (f"（题材：{genres}）" if genres else ""))
    if premise.get("logline"):
        lines.append(f"一句话故事：{premise['logline']}")
    if premise.get("theme"):
        lines.append(f"主题：{premise['theme']}")
    if premise.get("core_conflict"):
        lines.append(f"核心冲突：{premise['core_conflict']}")
    if premise.get("ending_intent"):
        lines.append(f"结局意图：{premise['ending_intent']}")
    if premise.get("target_audience"):
        lines.append(f"目标读者：{premise['target_audience']}")
    if style.get("tone"):
        lines.append(f"叙事基调：{style['tone']}")
    if style.get("prose_style"):
        lines.append(f"文笔风格：{str(style['prose_style'])[:200]}")

    world = next((item for item in (worlds or []) if isinstance(item, dict)), None) or {}
    if world:
        if world.get("era"):
            lines.append(f"时代与舞台：{world['era']}")
        if world.get("summary"):
            lines.append(f"世界观总述：{str(world['summary'])[:600]}")
        rules = [str(item) for item in (world.get("rules") or []) if str(item).strip()]
        if rules:
            lines.append("世界规则：" + "；".join(rules[:6]))

        def _names(key: str, label: str, name_key: str) -> None:
            """把某一类条目压成名称列表追加到上下文。"""
            values = [
                str(item.get(name_key)).strip()
                for item in (world.get(key) or [])
                if isinstance(item, dict) and str(item.get(name_key) or "").strip()
            ]
            if values:
                lines.append(f"{label}：" + "、".join(values[:8]))

        _names("factions", "主要势力", "name")
        _names("locations", "关键地点", "name")
        _names("glossary", "专有名词", "term")
        _names("others", "其它设定", "name")

    names = [str(item.get("name")) for item in (characters or []) if item.get("name") and item.get("role") != "narrator"]
    if names:
        lines.append("已有角色（不要重名，可呼应既有关系）：" + "、".join(names[:20]))
    return "\n".join([line for line in lines if line.strip()])

def _worlds_digest(worlds: list[dict], ids: list[str]) -> str | None:
    """把选中世界条目拼成章节上下文文本（委托 application/story/world.worlds_digest）。

    参数:
        worlds: 世界条目列表。
        ids: 本章要注入的世界 ID 列表。

    返回:
        多行上下文文本；ids 为空或一个都没命中时返回 None。
    """
    return worlds_digest(worlds, ids)
