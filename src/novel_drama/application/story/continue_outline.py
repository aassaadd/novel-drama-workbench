"""用例：续写大纲（新建幕并挂到设定库）。"""

from __future__ import annotations

from dataclasses import replace

from ...adapters.errors import AdapterError
from ...adapters.llm.contracts import Message, TextGenRequest
from ...adapters.persistence import ProjectStore
from ...adapters.persistence.settings_store import SettingsStore
from ...application.cast.sync_cast import (
    apply_chapter_cast,
    cast_prompt_block,
    load_pending,
    normalize_cast,
)
from ...infrastructure.config import AppConfig
from ...models.ids import new_id
from ...text.titles import strip_index_prefix
from ..support.llm_calls import (
    _characters_payload,
    _generate_json,
    build_writing_adapter,
)
from .chapter_limits import DEFAULT_CHAPTER_WORDS
from .continue_chapter import DEFAULT_OUTLINE_CHAPTERS, _cfg_int


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
    act = SettingsStore.new_act(
        strip_index_prefix(str(raw_act.get("title") or "")) or f"第{act_no}幕",
        raw_act.get("summary") or "",
    )
    raw_chapters = [item for item in (raw_act.get("chapters") or []) if isinstance(item, dict)]
    for offset in range(chapter_count):
        raw = raw_chapters[offset] if offset < len(raw_chapters) else {}
        index = start_index + offset
        outline_chapter = SettingsStore.new_outline_chapter(
            new_id("chp"),
            index,
            strip_index_prefix(str(raw.get("title") or "")) or f"第{index}章",
            raw.get("summary") or "",
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
                    extra_characters=raw_beat.get("extra_characters") or [],
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

def _apply_built_act_cast(
    store: ProjectStore,
    chapters: list[dict],
    raw_chapters: list[dict],
    dry_run: bool,
    characters: list[dict],
    pending: dict,
) -> tuple[list[str], list[str], list[dict]]:
    """把本幕同一次生成里的 per-chapter cast 落到角色卡（不再单独请求模型）。

    参数:
        store: 工程。
        chapters: `_build_outline_act` 产出的章节（会原地写入 character_ids）。
        raw_chapters: 模型返回的本章原始对象（读 cast）。
        dry_run: True 时不写 characters.json，新建角色只留在内存里。
        characters: 当前内存角色表。
        pending: 待定角色登记（第二次出现才建卡）。

    返回:
        `(新建姓名列表, 复用姓名列表, 更新后的角色表)`。
    """
    created: list[str] = []
    reused: list[str] = []
    for offset, chapter in enumerate(chapters):
        raw = raw_chapters[offset] if offset < len(raw_chapters) else {}
        report = apply_chapter_cast(
            store,
            chapter,
            normalize_cast({"cast": (raw or {}).get("cast")}),
            characters=characters,
            pending=pending,
            persist=not dry_run,
            chapter_index=int(chapter.get("index") or 0) or None,
            chapter_title=chapter.get("title") or "",
        )
        # 标记写回章节，随后续写结果一起落库；正文生成不再重复补跑
        chapter["cast_synced"] = True
        chapter["cast_created"] = list(report.get("created") or [])
        chapter["cast_reused"] = list(report.get("reused") or [])
        chapter["cast_extras"] = list(report.get("extras") or [])
        created.extend(report.get("created") or [])
        reused.extend(report.get("reused") or [])
        characters = list(report.get("characters") or characters)
    return created, reused, characters

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
    context_lines.append(cast_prompt_block(characters))
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
    cast_created_total = 0
    cast_reused_total = 0
    pending: dict = {} if dry_run else load_pending(store)

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
        # 输出侧不设人为上限：请求不带 max_tokens，由适配器按上下文剩余空间计算
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
        raw_chapters = [item for item in (act.get("chapters") or []) if isinstance(item, dict)]
        # 本幕 cast 与节拍同一次调用；这里只做确定性匹配、建卡与绑定
        try:
            cast_created, cast_reused, characters = _apply_built_act_cast(
                store, built["chapters"], raw_chapters, dry_run, characters, pending
            )
        except ValueError as exc:
            error = f"第 {act_no} 幕角色对齐失败：{exc}"
            break
        cast_created_total += len(cast_created)
        cast_reused_total += len(cast_reused)
        char_rows = _characters_payload(characters)
        # 后续幕能看到本幕新建的角色，避免下一幕重复造人
        req.metadata["characters"] = char_rows
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
        "characters_created": cast_created_total,
        "characters_reused": cast_reused_total,
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
