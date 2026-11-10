"""写作侧业务编排：大纲生成/落地、章节正文生成、导演脚本建议。

模型调用统一走 `WritingAdapter`：默认 `offline`（离线规则实现，零依赖可跑通），
配置 `providers.writing=omlx` 时切到真实 LLM，业务代码不变。
"""

from __future__ import annotations

import json

from ..adapters.base import AdapterError, ErrorCode, Message, TextGenRequest
from ..adapters.offline_writing import OfflineWritingAdapter
from ..config import AppConfig
from ..director.markup import parse_markup
from ..models import entities
from ..models.ids import new_id, now_iso
from ..store import ProjectStore
from ..store.settings_store import SettingsStore
from ..store.world_store import WorldStore
from ..text.context import POV_LABELS, ContextAssembler
from .worlds import build_art_prompt as _build_art_prompt
from .worlds import clean_world_entry, worlds_digest

# 默认每章目标字数
DEFAULT_CHAPTER_WORDS = 1200


def build_writing_adapter(config: AppConfig, settings: dict | None = None):
    """按配置装配写作适配器。

    参数:
        config: 本机配置。
        settings: 工程设置（透传给实现，可选）。

    返回:
        实现 `WritingAdapter` 协议的实例。

    异常:
        AdapterError(INVALID_INPUT): 配置名未知。
    """
    # 配置里适配器写在 adapters.writing；providers.writing 是旧键，保留兼容
    name = str(config.get("adapters.writing") or config.get("providers.writing") or "offline").strip()
    if name in ("offline", "offline-writing"):
        return OfflineWritingAdapter(config.get("writing.offline") or settings or {})
    if name == "omlx":
        from ..adapters.writing import OmlxWritingAdapter

        return OmlxWritingAdapter(config.get("omlx") or {})
    raise AdapterError(ErrorCode.INVALID_INPUT, f"未知写作适配器: {name}（可选 offline / omlx）")


def _characters_payload(characters: list[dict]) -> list[dict]:
    """裁剪角色表供提示词使用。

    参数:
        characters: 完整角色表。

    返回:
        [{id, name, role, aliases}]。
    """
    return [
        {"id": item["id"], "name": item.get("name"), "role": item.get("role"), "aliases": item.get("aliases") or []}
        for item in characters
        if item.get("role") != "narrator"
    ]


def generate_outline(
    store: ProjectStore,
    config: AppConfig,
    acts: int = 3,
    chapters_per_act: int = 3,
    beats_per_chapter: int = 3,
    chapter_words: int = DEFAULT_CHAPTER_WORDS,
    dry_run: bool = False,
    binding: str = "auto",
) -> dict:
    """生成幕/章/节拍大纲并写入设定库。

    参数:
        store: 工程。
        config: 本机配置。
        acts: 幕数。
        chapters_per_act: 每幕章节数。
        beats_per_chapter: 每章节拍数。
        chapter_words: 单章目标字数（用于分配节拍字数）。
        dry_run: 只生成不落盘。
        binding: auto=把节拍绑定到已有人物；none=不改动角色引用。

    返回:
        {"acts": [...], "chapters": N, "beats": M, "provider": name, "settings_revision": r,
        "golden_three": {"passed", "issues"}}。

    异常:
        AdapterError: 模型输出不是合法大纲 JSON。
    """
    settings_store = SettingsStore(store.root)
    bible = settings_store.ensure()
    project = store.load_project()
    work = project.get("work") or {}
    characters = store.load_characters()["characters"]
    adapter = build_writing_adapter(config, bible)

    req = TextGenRequest(
        task="outline",
        system="你是长篇小说的结构编辑，只输出 JSON，不要解释。",
        messages=[Message("user", f"为《{work.get('title')}》生成 {acts} 幕大纲，每幕 {chapters_per_act} 章，每章 {beats_per_chapter} 个节拍。")],
        stream=False,
        metadata={
            "acts": acts,
            "chapters_per_act": chapters_per_act,
            "beats_per_chapter": beats_per_chapter,
            "chapter_words": chapter_words,
            "title": work.get("title"),
            "logline": (bible.get("premise") or {}).get("logline"),
            "genre": work.get("genre") or [],
            "characters": _characters_payload(characters),
        },
    )
    result = adapter.generate(req)
    try:
        payload = json.loads(result.text)
        raw_acts = payload["acts"]
        if not isinstance(raw_acts, list) or not raw_acts:
            raise ValueError("acts 为空")
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise AdapterError(
            ErrorCode.INVALID_INPUT,
            f"写作适配器返回的大纲不是合法 JSON：{exc}；原始输出前 200 字：{result.text[:200]}",
            detail={"raw": result.text[:2000]},
        ) from exc

    # 结构化补全：分配稳定 ID、合并到设定库（不覆盖已有正文）
    built_acts = []
    chapter_count = 0
    beat_count = 0
    for act_index, raw_act in enumerate(raw_acts):
        act = SettingsStore.new_act(raw_act.get("title") or f"第{act_index + 1}幕", raw_act.get("summary") or "")
        for raw_chapter in raw_act.get("chapters") or []:
            chapter_count += 1
            existing = None
            for item in settings_store.all_outline_chapters(bible):
                if int(item.get("index") or 0) == chapter_count:
                    existing = item
                    break
            chapter_id = (existing or {}).get("id") or new_id("chp")
            outline_chapter = SettingsStore.new_outline_chapter(
                chapter_id,
                chapter_count,
                raw_chapter.get("title") or f"第{chapter_count}章",
                raw_chapter.get("summary") or "",
            )
            for raw_beat in raw_chapter.get("beats") or []:
                beat_count += 1
                outline_chapter["beats"].append(
                    SettingsStore.new_beat(
                        raw_beat.get("summary") or f"第{beat_count}个节拍",
                        character_ids=raw_beat.get("character_ids") or [],
                        turning_point=bool(raw_beat.get("turning_point")),
                        target_words=int(raw_beat.get("target_words") or chapter_words // max(1, beats_per_chapter)),
                    )
                )
            act["chapters"].append(outline_chapter)
        built_acts.append(act)

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
        "provider": getattr(result, "model", "") or "offline-writing",
        "settings_revision": bible.get("settings_revision"),
        "binding": binding,
        "golden_three": golden,
        "dry_run": dry_run,
    }


def apply_outline(store: ProjectStore, dry_run: bool = False) -> dict:
    """把设定库大纲落地成章节文件。

    已存在的章节只同步标题与大纲文本，不覆盖正文；缺失的按序创建。

    参数:
        store: 工程。
        dry_run: 只统计不落盘。

    返回:
        {"created": [...], "updated": [...], "chapters": N}。

    异常:
        ValueError: 设定库里没有大纲。
    """
    settings_store = SettingsStore(store.root)
    bible = settings_store.ensure()
    entries = settings_store.all_outline_chapters(bible)
    if not entries:
        raise ValueError("设定库还没有大纲：先执行 ndw outline generate")

    project = store.load_project()
    work_id = project["work"]["id"]
    existing_ids = {item["id"] for item in store.list_chapters()}
    created: list[str] = []
    updated: list[str] = []

    for entry in sorted(entries, key=lambda item: int(item.get("index") or 0)):
        outline_text = "\n".join(
            f"{index}. {beat.get('summary')}" for index, beat in enumerate(entry.get("beats") or [], start=1)
        ) or (entry.get("summary") or "")
        if entry["id"] not in existing_ids:
            if dry_run:
                created.append(f"{entry.get('index')} {entry.get('title')}（待创建）")
                continue
            chapter = entities.new_chapter(work_id, int(entry.get("index") or 1), entry.get("title") or "未命名", outline=outline_text)
            chapter["id"] = entry["id"]
            store.save_chapter(chapter)
            created.append(chapter["id"])
            existing_ids.add(chapter["id"])
        else:
            # 列表接口只返回轻量元信息，写回前必须读完整章节，否则会丢掉 work_id 等必填字段
            chapter = store.load_chapter(entry["id"])
            changed = False
            for key, value in (("title", entry.get("title")), ("outline", outline_text)):
                if value and chapter.get(key) != value:
                    chapter[key] = value
                    changed = True
            if changed:
                chapter["revision"] = int(chapter.get("revision") or 1) + 1
                chapter["updated_at"] = now_iso()
                if not dry_run:
                    store.save_chapter(chapter)
                updated.append(chapter["id"])
    return {"created": created, "updated": updated, "chapters": len(entries), "dry_run": dry_run}


def _chapter_beats(bible: dict, chapter: dict, settings_store: SettingsStore) -> list[dict]:
    """取章节对应的节拍；大纲中没有时从 chapter.outline 文本回退拆解。

    参数:
        bible: 设定库。
        chapter: 章节。
        settings_store: 设定库读写。

    返回:
        节拍列表（至少一项）。
    """
    entry = settings_store.find_outline_chapter(bible, chapter["id"])
    if entry and entry.get("beats"):
        return entry["beats"]
    outline = (chapter.get("outline") or "").strip()
    if outline:
        lines = [line.strip(" .、0123456789") for line in outline.splitlines() if line.strip()]
        if lines:
            return [{"summary": line, "character_ids": [], "turning_point": index == len(lines) - 1, "target_words": 0} for index, line in enumerate(lines)]
    return [{"summary": chapter.get("title") or "推进主线", "character_ids": [], "turning_point": False, "target_words": 0}]


def build_chapter_context(store: ProjectStore, config: AppConfig, chapter: dict) -> dict:
    """准备章节生成与 LLM 编辑共用的上下文素材。

    参数:
        store: 工程。
        config: 本机配置。
        chapter: 章节 dict（含 id/index/title/outline/content）。

    返回:
        {bible, work, characters, beats, chapter_words, entry, world_ids, world_context,
         character_payload, speaker_ids, previous_chapters, style, adapter, metadata}。
        metadata 字段与 `generate_chapter` 原有约定完全一致，离线适配器行为不变。
    """
    settings_store = SettingsStore(store.root)
    bible = settings_store.ensure()
    work = store.load_project().get("work") or {}
    characters = store.load_characters()["characters"]
    beats = _chapter_beats(bible, chapter, settings_store)
    chapter_words = int(sum(int(beat.get("target_words") or 0) for beat in beats) or DEFAULT_CHAPTER_WORDS)
    if chapter_words <= 0:
        chapter_words = DEFAULT_CHAPTER_WORDS
    adapter = build_writing_adapter(config, bible)

    entry = settings_store.find_outline_chapter(bible, chapter.get("id"))
    world_ids = list((entry or {}).get("world_ids") or [])
    world_store = WorldStore(store.root)
    world_context = _worlds_digest(world_store.find_many(world_ids) if (world_ids and world_store.exists()) else [], world_ids)
    if world_context is None:
        # 未绑定世界时回退设定库摘要，保持旧章节的上下文口味
        world_context = _bible_digest(bible)

    character_payload = _characters_payload(characters)
    speaker_ids: list[str] | None = None
    if entry:
        # 角色上下文只取本章 POV 与节拍涉及的角色；找不到时保留全量角色
        focus = {cid for cid in [entry.get("pov_character_id")] if cid}
        for beat in beats:
            focus.update(cid for cid in (beat.get("character_ids") or []) if cid)
        focus.discard(entities.NARRATOR_ID)
        focused = [item for item in character_payload if item["id"] in focus]
        if focused:
            character_payload = focused
        if focus:
            speaker_ids = sorted(focus)

    # 前序章节：index 小于当前章节，按 index 倒序最多取 3 章，再转成从旧到新
    previous_index = int(chapter.get("index") or 0)
    previous_meta = [item for item in store.list_chapters() if int(item.get("index") or 0) < previous_index]
    previous_meta.sort(key=lambda item: int(item.get("index") or 0), reverse=True)
    previous_chapters: list[dict] = []
    for meta in previous_meta[:3]:
        full = store.load_chapter(meta["id"])
        previous_chapters.append(
            {
                "index": full.get("index"),
                "title": full.get("title"),
                "synopsis": full.get("synopsis") or "",
                "content": full.get("content") or "",
            }
        )
    previous_chapters.reverse()

    metadata = {
        "title": chapter.get("title"),
        "summary": (entry or {}).get("summary") or chapter.get("outline"),
        "beats": beats,
        "characters": character_payload,
        "world": world_context,
        "world_ids": world_ids,
        "pov_character_id": (entry or {}).get("pov_character_id") or chapter.get("pov_character_id"),
        "pov": (bible.get("style") or {}).get("pov"),
        "tone": (bible.get("style") or {}).get("tone"),
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
        "previous_chapters": previous_chapters,
        "style": bible.get("style") or {},
        "adapter": adapter,
        "metadata": metadata,
    }


def generate_chapter(store: ProjectStore, config: AppConfig, chapter_id: str, regen: bool = False, dry_run: bool = False) -> dict:
    """生成章节正文并落盘。

    参数:
        store: 工程。
        config: 本机配置。
        chapter_id: 章节 ID。
        regen: 已有正文时是否覆盖重写。
        dry_run: 只生成不落盘。

    返回:
        {"chapter_id", "status", "words", "provider", "content"}（content 在 dry_run 时为全文预览）。

    异常:
        ValueError: 已有正文且未指定 regen。
    """
    chapter = store.load_chapter(chapter_id)
    if (chapter.get("content") or "").strip() and not regen and not dry_run:
        raise ValueError(f"章节 {chapter_id} 已有正文；确认重写请加 --regen")

    context = build_chapter_context(store, config, chapter)
    bible = context["bible"]
    beats = context["beats"]
    chapter_words = context["chapter_words"]
    entry = context["entry"]
    style = context["style"]

    summary_text = (entry or {}).get("summary") or chapter.get("outline") or ""
    pov_label = POV_LABELS.get((style.get("pov") or "").strip(), style.get("pov") or "第三人称有限视角")
    tone_label = style.get("tone") or "沿用既有基调"
    instruction_lines = [
        f"写《{chapter.get('title')}》本章正文，目标字数约 {chapter_words} 字。",
        f"本章摘要：{summary_text or '按设定推进主线'}",
        "按以下节拍顺序推进：",
    ]
    for order, beat in enumerate(beats, start=1):
        try:
            target = int(beat.get("target_words") or 0)
        except (TypeError, ValueError):
            target = 0
        turning = "是" if beat.get("turning_point") else "否"
        instruction_lines.append(f"{order}. {beat.get('summary')}（目标 {target} 字，转折点：{turning}）")
    instruction_lines.append(f"视角：{pov_label}；基调：{tone_label}。")
    instruction_lines.append("输出格式：只输出正文，不要写章节标题或小标题，不要解释；对白使用中文引号“”。")
    instruction = "\n".join(instruction_lines)

    assembler = ContextAssembler(
        work=context["work"],
        bible=bible,
        world_digest=context["world_context"],
        characters=context["characters"],
        chapter=chapter,
        outline_chapter=entry or {},
        previous_chapters=context["previous_chapters"],
        current_content=chapter.get("content") or "",
        budget_tokens=int(config.get("writing.context_budget_tokens") or 32768),
    )
    req = assembler.build("chapter", instruction, context["speaker_ids"])
    # metadata 保持原有字段不变：离线适配器依赖它，且产物需要确定性一致
    req.metadata = context["metadata"]
    result = context["adapter"].generate(req)
    content = (result.text or "").strip()
    if not content:
        raise AdapterError(ErrorCode.UPSTREAM_ERROR, "写作适配器返回了空正文", retryable=True)

    words = sum(1 for char in content if not char.isspace())
    if dry_run:
        return {"chapter_id": chapter_id, "status": chapter.get("status"), "words": words, "provider": result.model, "content": content}

    chapter["content"] = content + "\n"
    chapter["word_count"] = words
    chapter["status"] = "written"
    chapter["revision"] = int(chapter.get("revision") or 1) + 1
    chapter["updated_at"] = now_iso()
    store.save_chapter(chapter)
    return {"chapter_id": chapter_id, "status": chapter["status"], "words": words, "provider": result.model, "content": None}


def suggest_script(store: ProjectStore, config: AppConfig, chapter_id: str, dry_run: bool = False) -> dict:
    """生成导演脚本标注建议并试解析。

    参数:
        store: 工程。
        config: 本机配置。
        chapter_id: 章节 ID。
        dry_run: 只解析不落盘。

    返回:
        {"markup", "script", "warnings", "shots", "lines"}。

    异常:
        ValueError: 章节没有场景或台词行。
        AdapterError: 模型输出的标注无法解析。
    """
    chapter = store.load_chapter(chapter_id)
    scenes = chapter.get("scenes") or []
    if not scenes:
        raise ValueError(f"章节 {chapter_id} 还没有场景：先执行 ndw scene split")
    characters = store.load_characters()["characters"]
    char_names = {item["id"]: item.get("name") or "" for item in characters}
    settings_store = SettingsStore(store.root)
    bible = settings_store.ensure()

    scenes_payload = []
    for scene in sorted(scenes, key=lambda item: item.get("index") or 0):
        lines = []
        for line in scene.get("lines") or []:
            lines.append(
                {
                    "speaker_id": line.get("speaker_id"),
                    "speaker_name": char_names.get(line.get("speaker_id")) or ("旁白" if line.get("kind") == "narration" else ""),
                    "text": line.get("text"),
                    "kind": line.get("kind"),
                    "emotion": line.get("emotion") or "neutral",
                }
            )
        if not lines:
            continue
        scenes_payload.append(
            {
                "index": scene.get("index") or 0,
                "title": scene.get("title") or scene.get("scene_label") or f"场景 {(scene.get('index') or 0) + 1}",
                "location": scene.get("location") or "未标注",
                "time_of_day": scene.get("time_of_day") or "unknown",
                "mood": scene.get("mood") or "克制",
                "lines": lines,
            }
        )
    if not scenes_payload:
        raise ValueError(f"章节 {chapter_id} 的场景里没有任何台词行：先执行 ndw line split")

    adapter = build_writing_adapter(config, bible)
    req = TextGenRequest(
        task="script",
        system="你是有声剧导演，输出 NDW 标注文本（@shot / !sfx / !visual / !anim），不要解释。",
        messages=[Message("user", f"为章节《{chapter.get('title')}》生成导演标注，共 {len(scenes_payload)} 个镜头。")],
        stream=False,
        metadata={"scenes": scenes_payload, "style": bible.get("style") or {}},
    )
    result = adapter.generate(req)
    markup = (result.text or "").strip()
    try:
        script = parse_markup(markup, chapter, characters, store.settings())
    except ValueError as exc:
        raise AdapterError(ErrorCode.INVALID_INPUT, f"导演标注无法解析：{exc}", detail={"raw": markup[:2000]}) from exc
    summary = {
        "markup": markup,
        "script": script,
        "warnings": script.get("warnings") or [],
        "shots": len(script.get("shots") or []),
        "lines": sum(len(shot.get("lines") or []) for shot in script.get("shots") or []),
        "provider": result.model,
        "dry_run": dry_run,
    }
    return summary


# ---------------- AI 生书：设定 / 人物 / 润色 ----------------

#: 设定生成步骤
SETTING_STEPS = ("world", "premise", "style")
#: 梗概分区字段（与 setting-bible.schema.json 对齐）
PREMISE_FIELDS = ("logline", "theme", "core_conflict", "ending_intent", "target_audience", "target_length")
#: 文风分区字段
STYLE_FIELDS = ("tone", "pov", "tense", "prose_style", "forbidden", "visual_style")
#: 世界分区字段
WORLD_FIELDS = ("era", "rules", "factions", "locations", "glossary")
#: 标量字段长度上限（来自 schema）
SCALAR_LIMITS = {
    "logline": 500,
    "theme": 500,
    "core_conflict": 1000,
    "ending_intent": 1000,
    "target_audience": 200,
    "era": 200,
    "tone": 300,
    "prose_style": 1000,
    "visual_style": 500,
    "world.name": 120,
    "world.era": 200,
    "world.summary": 2000,
    "world.visual_style": 500,
    "character.appearance_prompt": 2000,
    "character.negative_prompt": 1000,
}
#: 枚举取值
POV_OPTIONS = ("first", "third_limited", "third_omniscient", "second")
TENSE_OPTIONS = ("past", "present")
LENGTH_OPTIONS = ("short", "novella", "novel", "series")
ROLE_OPTIONS = ("protagonist", "antagonist", "supporting", "minor")
GENDER_OPTIONS = ("male", "female", "other", "unknown")
#: 模型偶尔把枚举写成中文（含括号说明），校验前先按前缀归一；长别名放前面避免误匹配
CHOICE_ALIASES = {
    "短篇": "short",
    "中篇": "novella",
    "长篇连载": "series",
    "系列": "series",
    "中长篇": "novella",
    "长篇": "novel",
    "第三人称有限": "third_limited",
    "第三人称全知": "third_omniscient",
    "第三人称": "third_limited",
    "第一人称": "first",
    "第二人称": "second",
    "过去时": "past",
    "现在时": "present",
    "主角": "protagonist",
    "男主": "protagonist",
    "女主": "protagonist",
    "反派": "antagonist",
    "配角": "supporting",
    "男配": "supporting",
    "女配": "supporting",
    "龙套": "minor",
    "男性": "male",
    "女性": "female",
    "男": "male",
    "女": "female",
    "其他": "other",
    "未知": "unknown",
}
#: 单字段润色白名单
POLISH_FIELDS = (
    "logline",
    "theme",
    "core_conflict",
    "ending_intent",
    "target_audience",
    "era",
    "tone",
    "prose_style",
    "visual_style",
    "world.name",
    "world.era",
    "world.summary",
    "world.visual_style",
    "world.rule",
    "world.faction",
    "world.location",
    "world.glossary",
    "character.background",
    "character.personality",
    "character.appearance_prompt",
    "character.negative_prompt",
    "work.genre",
)
#: 黄金三章检查用的冲突语义关键词
CONFLICT_KEYWORDS = ("冲突", "对抗", "矛盾", "争执", "对峙", "摊牌", "威胁", "追杀", "决裂", "争夺", "冲突升级")


def _extract_json_object(text: str) -> dict:
    """从模型输出中提取第一个 `{` 到最后一个 `}` 之间的 JSON 对象。

    参数:
        text: 模型原始输出（允许被 ```json 代码块包裹）。

    返回:
        解析后的 dict。

    异常:
        ValueError: 没有 JSON 对象或解析失败。
    """
    raw = (text or "").strip()
    if not raw:
        raise ValueError("LLM 返回内容为空")
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("LLM 返回内容不是合法 JSON")
    payload = json.loads(raw[start : end + 1])
    if not isinstance(payload, dict):
        raise ValueError("LLM 返回内容不是合法 JSON")
    return payload


# ---------- 结构化生成工具（LLM function calling） ----------

#: 世界条目参数 schema（单条与批量共用）
_WORLD_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "世界/时代名称，80 字以内"},
        "era": {"type": "string", "description": "时代背景，200 字以内"},
        "summary": {"type": "string", "description": "世界概述，500 字以内"},
        "rules": {"type": "array", "items": {"type": "string"}, "description": "世界规则/力量体系，每条 120 字以内"},
        "factions": {"type": "array", "items": {"type": "string"}, "description": "主要势力，每条 120 字以内"},
        "locations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "atmosphere": {"type": "string"},
                },
                "required": ["name"],
            },
            "description": "关键地点：JSON 数组，每个元素必须是对象（不要写成字符串），含 name/description/atmosphere",
        },
        "glossary": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "term": {"type": "string"},
                    "meaning": {"type": "string"},
                    "aliases": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["term", "meaning"],
            },
            "description": "专有名词表：JSON 数组，每个元素必须是对象（不要写成字符串），含 term/meaning",
        },
        "visual_style": {"type": "string", "description": "画面风格关键词，200 字以内"},
    },
    "required": ["name", "era"],
}

#: 人物条目参数 schema（单个与批量共用）
_CHARACTER_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "姓名，60 字以内"},
        "role": {
            "type": "string",
            "enum": list(ROLE_OPTIONS),
            "description": "protagonist=主角，antagonist=反派，supporting=配角，minor=龙套",
        },
        "gender": {"type": "string", "enum": list(GENDER_OPTIONS)},
        "age_range": {"type": "string", "description": "年龄区间，如「18-24」，40 字以内"},
        "personality": {
            "type": "array",
            "items": {"type": "string"},
            "description": "3-5 个性格短标签，每个不超过 20 字",
        },
        "background": {"type": "string", "description": "人物背景，500 字以内"},
        "appearance_prompt": {"type": "string", "description": "外貌提示词，用于配图"},
        "negative_prompt": {"type": "string", "description": "配图负向提示词"},
        "relationships": {
            "type": "object",
            "additionalProperties": {"type": "string"},
            "description": "与其他人物/势力的关系（名字→备注）",
        },
        "state": {
            "type": "object",
            "properties": {
                "relationship_notes": {"type": "object", "additionalProperties": {"type": "string"}}
            },
            "description": "人物状态；单人物生成时关系备注写在 relationship_notes",
        },
    },
    "required": ["name"],
}

#: 梗概参数 schema
_PREMISE_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "logline": {"type": "string", "description": "一句话故事核心，120 字以内"},
        "theme": {"type": "string", "description": "主题，200 字以内"},
        "core_conflict": {"type": "string", "description": "核心冲突，300 字以内"},
        "ending_intent": {"type": "string", "description": "结局意图，200 字以内"},
        "target_audience": {"type": "string", "description": "目标读者，200 字以内"},
        "target_length": {
            "type": "string",
            "enum": list(LENGTH_OPTIONS),
            "description": "short=短篇，novella=中篇，novel=长篇，series=长篇连载",
        },
    },
    "required": ["logline"],
}

#: 文风参数 schema
_STYLE_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "tone": {"type": "string", "description": "整体基调，200 字以内"},
        "pov": {"type": "string", "enum": list(POV_OPTIONS), "description": "叙事视角"},
        "tense": {"type": "string", "enum": list(TENSE_OPTIONS), "description": "时态"},
        "prose_style": {"type": "string", "description": "文笔风格描述，500 字以内"},
        "forbidden": {
            "type": "array",
            "items": {"type": "string"},
            "description": "需要避免的写法，每条 40 字以内",
        },
        "visual_style": {"type": "string", "description": "画面风格关键词，200 字以内"},
    },
    "required": ["tone"],
}


def _gen_tool(step: str | None) -> dict | None:
    """按生成步骤返回对应的工具定义（OpenAI tools 单项）。

    参数:
        step: premise|style|world|worlds|character|characters。

    返回:
        工具定义 dict；未知步骤返回 None。

    异常:
        无。
    """
    if step == "premise":
        return {
            "type": "function",
            "function": {"name": "emit_premise", "description": "输出故事梗概设定", "parameters": _PREMISE_TOOL_SCHEMA},
        }
    if step == "style":
        return {
            "type": "function",
            "function": {"name": "emit_style", "description": "输出文风设定", "parameters": _STYLE_TOOL_SCHEMA},
        }
    if step == "world":
        return {
            "type": "function",
            "function": {"name": "emit_world", "description": "输出一条完整世界设定", "parameters": _WORLD_TOOL_SCHEMA},
        }
    if step == "worlds":
        return {
            "type": "function",
            "function": {
                "name": "emit_worlds",
                "description": "输出多条世界设定",
                "parameters": {
                    "type": "object",
                    "properties": {"worlds": {"type": "array", "items": _WORLD_TOOL_SCHEMA}},
                    "required": ["worlds"],
                },
            },
        }
    if step == "character":
        return {
            "type": "function",
            "function": {"name": "emit_character", "description": "输出一个完整人物设定", "parameters": _CHARACTER_TOOL_SCHEMA},
        }
    if step == "characters":
        return {
            "type": "function",
            "function": {
                "name": "emit_characters",
                "description": "输出 1 名主角与 2-6 名主要配角",
                "parameters": {
                    "type": "object",
                    "properties": {"characters": {"type": "array", "items": _CHARACTER_TOOL_SCHEMA}},
                    "required": ["characters"],
                },
            },
        }
    return None


def _generate_json(adapter, req: TextGenRequest, step: str) -> dict:
    """用 LLM 工具调用产出结构化参数（不再解析自由文本）。

    参数:
        adapter: 写作适配器（需实现 generate_tool）。
        req: 文本生成请求。
        step: 生成步骤（premise/style/world/worlds/character/characters），决定工具 schema。

    返回:
        工具参数解析后的 dict。

    异常:
        ValueError: 未知步骤或适配器不支持工具调用。
        AdapterError: 模型未返回工具调用，或工具参数不合法。
    """
    tool = _gen_tool(step)
    if tool is None:
        raise ValueError(f"未知的生成步骤: {step or '(空)'}")
    generate_tool = getattr(adapter, "generate_tool", None)
    if generate_tool is None:
        raise ValueError(f"写作适配器不支持工具调用: {type(adapter).__name__}")
    return generate_tool(req, tool)


def _check_text(value, limit: int, label: str, required: bool = False) -> str:
    """校验并清洗字符串字段。

    参数:
        value: 原始值。
        limit: 最大长度（schema 上限）。
        label: 字段中文名，用于错误提示。
        required: 是否允许为空。

    返回:
        去空白后的字符串。

    异常:
        ValueError: 必填为空或超长。
    """
    text = str(value or "").strip()
    if required and not text:
        raise ValueError(f"{label}不能为空")
    if len(text) > limit:
        raise ValueError(f"{label}超过 {limit} 字上限（当前 {len(text)} 字）")
    return text


def _normalize_choice(value, options: tuple[str, ...]) -> str | None:
    """把枚举值归一成英文选项（兼容模型输出的中文别名）。

    参数:
        value: 原始值。
        options: 允许的取值。

    返回:
        归一后的枚举值；无法识别时返回 None。
    """
    text = str(value or "").strip()
    if text in options:
        return text
    # 模型可能输出「中长篇小说，约15-20万字」这类中文，按最长别名命中，避免「中长篇」被「长篇」抢走
    for alias, canonical in sorted(CHOICE_ALIASES.items(), key=lambda item: -len(item[0])):
        if alias in text and canonical in options:
            return canonical
    return None


def _check_choice(value, options: tuple[str, ...], label: str) -> str:
    """校验枚举字段。

    参数:
        value: 原始值。
        options: 允许的取值。
        label: 字段中文名。

    返回:
        归一后的枚举值。

    异常:
        ValueError: 取值不在枚举内。
    """
    normalized = _normalize_choice(value, options)
    if normalized is None:
        text = str(value or "").strip()
        raise ValueError(f"{label}取值非法: {text or '(空)'}（可选 {'/'.join(options)}）")
    return normalized


def _clean_str_list(rows, item_limit: int, label: str) -> list[str]:
    """清洗字符串数组（允许单个字符串自动成组）。

    参数:
        rows: 原始数组或字符串。
        item_limit: 单项最大长度。
        label: 字段中文名。

    返回:
        去空后的字符串列表。

    异常:
        ValueError: 类型错误或单项超长。
    """
    if rows is None:
        return []
    if isinstance(rows, str):
        rows = [rows]
    if not isinstance(rows, list):
        raise ValueError(f"{label}必须是字符串数组")
    result = []
    for index, item in enumerate(rows):
        text = _check_text(item, item_limit, f"{label}第 {index + 1} 项")
        if text:
            result.append(text)
    return result


def _clean_locations(rows) -> list[dict]:
    """清洗世界地点数组。

    参数:
        rows: 原始地点数组。

    返回:
        [{name, description, atmosphere}]。

    异常:
        ValueError: 结构或长度不合法。
    """
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise ValueError("locations 必须是数组")
    result = []
    for index, item in enumerate(rows):
        if not isinstance(item, dict):
            raise ValueError(f"locations 第 {index + 1} 项必须是对象")
        result.append(
            {
                "name": _check_text(item.get("name"), 120, f"locations 第 {index + 1} 项名称", required=True),
                "description": _check_text(item.get("description"), 1000, f"locations 第 {index + 1} 项描述"),
                "atmosphere": _check_text(item.get("atmosphere"), 300, f"locations 第 {index + 1} 项氛围"),
            }
        )
    return result


def _clean_glossary(rows) -> list[dict]:
    """清洗世界观词条数组。

    参数:
        rows: 原始词条数组。

    返回:
        [{term, meaning, aliases}]。

    异常:
        ValueError: 结构或长度不合法。
    """
    if rows is None:
        return []
    if isinstance(rows, str):
        # 模型有时把词条写成整段文本，按换行/分号拆成多项
        rows = [part.strip() for part in rows.replace("；", "\n").replace(";", "\n").splitlines() if part.strip()]
    if isinstance(rows, dict):
        # 单个词条对象，或「术语→释义」映射，统一成数组
        rows = [rows] if any(key in rows for key in ("term", "name", "word")) else [
            {"term": key, "meaning": value} for key, value in rows.items()
        ]
    if not isinstance(rows, list):
        raise ValueError("glossary 必须是数组")
    result = []
    for index, item in enumerate(rows):
        if isinstance(item, str):
            # 模型常把词条写成「术语：释义」字符串，这里拆成对象
            text = str(item).strip()
            for sep in ("：", ":", "——", " - ", "—"):
                if sep in text:
                    term, meaning = text.split(sep, 1)
                    item = {"term": term.strip(), "meaning": meaning.strip()}
                    break
            else:
                item = {"term": text, "meaning": text}
        if not isinstance(item, dict):
            # 无法识别的项直接跳过，不让整次生成失败
            continue
        # 模型常用 name/definition 等别名，统一成 term/meaning
        term = item.get("term") or item.get("name") or item.get("word")
        meaning = item.get("meaning") or item.get("definition") or item.get("description") or item.get("desc")
        if not str(meaning or "").strip():
            # 只有术语没有释义时跳过，不让整次生成失败
            continue
        entry = {
            "term": _check_text(term, 120, f"glossary 第 {index + 1} 项术语", required=True),
            "meaning": _check_text(meaning, 1000, f"glossary 第 {index + 1} 项释义", required=True),
        }
        aliases = _clean_str_list(item.get("aliases"), 120, f"glossary 第 {index + 1} 项别名")
        if aliases:
            entry["aliases"] = aliases
        result.append(entry)
    return result


def _clean_setting_fields(step: str, payload: dict) -> dict:
    """按步骤清洗并校验生成字段。

    参数:
        step: world / premise / style。
        payload: 模型返回的 JSON 对象。

    返回:
        只包含该步骤允许字段的清洗结果。

    异常:
        ValueError: 字段类型、枚举或长度不合法。
    """
    if step == "premise":
        if "logline" not in payload:
            raise ValueError("logline 不能为空")
        fields = {"logline": _check_text(payload.get("logline"), 500, "logline", required=True)}
        for key in ("theme", "core_conflict", "ending_intent", "target_audience"):
            if key in payload:
                fields[key] = _check_text(payload.get(key), SCALAR_LIMITS[key], key)
        if "target_length" in payload:
            fields["target_length"] = _check_choice(payload.get("target_length"), LENGTH_OPTIONS, "target_length")
        return fields

    if step == "style":
        if "tone" not in payload:
            raise ValueError("tone 不能为空")
        fields = {"tone": _check_text(payload.get("tone"), 300, "tone", required=True)}
        if "pov" in payload:
            fields["pov"] = _check_choice(payload.get("pov"), POV_OPTIONS, "pov")
        if "tense" in payload:
            fields["tense"] = _check_choice(payload.get("tense"), TENSE_OPTIONS, "tense")
        if "prose_style" in payload:
            fields["prose_style"] = _check_text(payload.get("prose_style"), 1000, "prose_style")
        if "forbidden" in payload:
            fields["forbidden"] = _clean_str_list(payload.get("forbidden"), 200, "forbidden")
        if "visual_style" in payload:
            fields["visual_style"] = _check_text(payload.get("visual_style"), 500, "visual_style")
        return fields

    fields = {"era": _check_text(payload.get("era"), 200, "era", required=True)}
    if "rules" in payload:
        fields["rules"] = _clean_str_list(payload.get("rules"), 500, "rules")
    if "factions" in payload:
        fields["factions"] = _clean_str_list(payload.get("factions"), 200, "factions")
    if "locations" in payload:
        fields["locations"] = _clean_locations(payload.get("locations"))
    if "glossary" in payload:
        fields["glossary"] = _clean_glossary(payload.get("glossary"))
    return fields


def _bible_digest(bible: dict) -> dict:
    """整理已有设定摘要，供提示词与增量生成使用。

    参数:
        bible: 设定库 dict（可为空）。

    返回:
        {logline, theme, era, tone, rules, factions, locations, glossary}。
    """
    premise = bible.get("premise") or {}
    world = bible.get("world") or {}
    style = bible.get("style") or {}
    return {
        "logline": premise.get("logline"),
        "theme": premise.get("theme"),
        "core_conflict": premise.get("core_conflict"),
        "target_length": premise.get("target_length"),
        "era": world.get("era"),
        "tone": style.get("tone"),
        "pov": style.get("pov"),
        "rules": (world.get("rules") or [])[:8],
        "factions": (world.get("factions") or [])[:8],
        "locations": [item.get("name") for item in (world.get("locations") or []) if item.get("name")][:8],
        "glossary": [item.get("term") for item in (world.get("glossary") or []) if item.get("term")][:8],
    }


def _worlds_digest(worlds: list[dict], ids: list[str]) -> str | None:
    """把选中世界条目拼成章节上下文文本（委托 writing/worlds.worlds_digest）。

    参数:
        worlds: 世界条目列表。
        ids: 本章要注入的世界 ID 列表。

    返回:
        多行上下文文本；ids 为空或一个都没命中时返回 None。
    """
    return worlds_digest(worlds, ids)


def _is_meaningful(current, baseline, key: str = "") -> bool:
    """判断已有值是否算「用户真实内容」（骨架占位值视为空）。

    只有 logline / era / tone 这类骨架占位字段会与默认值比较，
    其余字段只要非空就保留（避免未经用户确认被生成结果覆盖）。

    参数:
        current: 设定库里的当前值。
        baseline: 设定库骨架里的默认值。
        key: 字段名。

    返回:
        True 表示应当保留，不被生成结果覆盖。
    """
    if current is None:
        return False
    if isinstance(current, list):
        return len(current) > 0
    if isinstance(current, dict):
        return bool(current)
    text = str(current).strip()
    if not text:
        return False
    if key in ("logline", "era", "tone"):
        if "待填写" in text:
            return False
        if baseline is not None and text == str(baseline).strip():
            return False
    return True


def generate_worlds(
    store: ProjectStore,
    config: AppConfig,
    count: int | None = None,
    overwrite: bool = False,
    brief: str | None = None,
) -> dict:
    """一次生成 1..6 条世界条目（只返回预览，不落盘）。

    参数:
        store: 工程。
        config: 本机配置。
        count: 期望世界条数（1..6）；为空时由适配器决定。
        overwrite: True 表示调用方打算覆盖已有世界；本函数仍只预览，仅在返回值里标记。
        brief: 用户补充要求（拼进提示词与请求元信息）。

    返回:
        {"step": "worlds", "worlds": [...], "count", "brief", "overwrite", "provider"}；
        worlds 为清洗后通过 world schema 的完整条目（可直接交给 WorldStore.add）。

    异常:
        ValueError: count 越界、模型输出不是合法 JSON 或世界字段不合法。
    """
    if count is not None:
        try:
            wanted = int(count)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"世界数量必须是 1..6 的整数：{count}") from exc
        if wanted < 1 or wanted > 6:
            raise ValueError(f"世界数量必须在 1..6 之间：{wanted}")
    else:
        wanted = None

    settings_store = SettingsStore(store.root)
    bible = settings_store.load_or_none() or {}
    project = store.load_project()
    work = project.get("work") or {}
    adapter = build_writing_adapter(config, bible)
    brief_text = str(brief or "").strip()

    world_store = WorldStore(store.root)
    existing = [{"name": item.get("name"), "era": item.get("era")} for item in (world_store.list_worlds() if world_store.exists() else [])]
    ask = "生成世界设定列表，每条包含 name/era/summary/rules/factions/locations/glossary/visual_style；locations/glossary 必须是数组，元素为对象，不要用字符串包裹。"
    if wanted:
        ask += f"共 {wanted} 条世界。"
    if brief_text:
        ask += f"补充要求：{brief_text}。"
    req = TextGenRequest(
        task="setting",
        system="你是中文小说的世界观编辑，只输出一个 JSON 对象（键为 worlds），不要解释。",
        messages=[Message("user", f"为《{work.get('title') or '未命名'}》{ask}")],
        stream=False,
        temperature=0.7,
        metadata={
            "step": "worlds",
            "work": {"title": work.get("title"), "author": work.get("author"), "genre": work.get("genre") or []},
            "existing": _bible_digest(bible),
            "existing_worlds": existing,
            "count": wanted,
            "brief": brief_text,
            "overwrite": bool(overwrite),
            "seed": work.get("id"),
        },
    )
    payload = _generate_json(adapter, req, "worlds")
    rows = payload.get("worlds")
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list) or not rows:
        raise ValueError("LLM 返回的世界列表为空")
    if wanted:
        rows = rows[:wanted]
    worlds = [clean_world_entry(row) for row in rows]
    return {
        "step": "worlds",
        "worlds": worlds,
        "count": len(worlds),
        "brief": brief_text or None,
        "overwrite": bool(overwrite),
        "provider": str(config.get("adapters.writing") or config.get("providers.writing") or "offline"),
    }


def generate_settings(store: ProjectStore, config: AppConfig, step: str, overwrite: bool = False, brief: str | None = None) -> dict:
    """生成世界 / 梗概 / 文风设定（只返回结果，不写盘）。

    参数:
        store: 工程。
        config: 本机配置。
        step: world|premise|style。
        overwrite: True 时用生成值整体替换该分区；False 时保留已有非空值并记入 skipped。
        brief: 可选的用户补充要求（拼进提示词与请求元信息）。

    返回:
        {"step", "fields", "skipped"}。

    异常:
        ValueError: step 非法、模型输出不是合法 JSON 或字段校验失败。
    """
    if step not in SETTING_STEPS:
        raise ValueError(f"未知设定步骤: {step}（可选 world / premise / style）")
    settings_store = SettingsStore(store.root)
    bible = settings_store.load_or_none() or {}
    project = store.load_project()
    work = project.get("work") or {}
    skeleton = settings_store.build_skeleton(work)
    adapter = build_writing_adapter(config, bible)

    label = {"world": "世界设定", "premise": "故事梗概", "style": "文风设定"}[step]
    allowed = {"world": WORLD_FIELDS, "premise": PREMISE_FIELDS, "style": STYLE_FIELDS}[step]
    target_length = (bible.get("premise") or {}).get("target_length") or (skeleton.get("premise") or {}).get("target_length")
    brief_text = str(brief or "").strip()
    ask = f"为《{work.get('title') or '未命名'}》生成{label}，字段：{', '.join(allowed)}。"
    if brief_text:
        ask += f"补充要求：{brief_text}。"
    req = TextGenRequest(
        task="setting",
        system="你是中文小说的设定编辑，只输出一个 JSON 对象，不要解释，不要输出 JSON 以外的内容。",
        messages=[Message("user", ask)],
        stream=False,
        temperature=0.7,
        metadata={
            "step": step,
            "work": {"title": work.get("title"), "author": work.get("author"), "genre": work.get("genre") or []},
            "existing": _bible_digest(bible),
            "target_length": target_length,
            "brief": brief_text,
            "seed": work.get("id"),
        },
    )
    payload = _generate_json(adapter, req, step)
    if isinstance(payload.get(step), dict):
        payload = payload[step]
    fields = _clean_setting_fields(step, payload)

    existing_section = bible.get(step) or {}
    baseline_section = skeleton.get(step) or {}
    final: dict = {}
    skipped: list[str] = []
    for key, value in fields.items():
        current = existing_section.get(key)
        if not overwrite and _is_meaningful(current, baseline_section.get(key), key):
            final[key] = current
            skipped.append(key)
        else:
            final[key] = value
    return {"step": step, "fields": final, "skipped": skipped}


def generate_characters(store: ProjectStore, config: AppConfig, count: int | None = None, brief: str | None = None) -> dict:
    """生成主角与主要配角（只返回结果，不写盘）。

    参数:
        store: 工程。
        config: 本机配置。
        count: 期望人物总数（含主角），缺省交给模型按 1 主角 + 2–6 配角产出。
        brief: 用户补充要求（拼进提示词，离线实现会写进人物小传/性格）。

    返回:
        {"characters": [{name, role, gender, age_range, personality, background,
        appearance_prompt, negative_prompt, relationships}]}。

    异常:
        ValueError: 模型输出不是合法 JSON，或缺少主角 / 配角数量越界 / 重名。
    """
    settings_store = SettingsStore(store.root)
    bible = settings_store.load_or_none() or {}
    project = store.load_project()
    work = project.get("work") or {}
    adapter = build_writing_adapter(config, bible)
    brief_text = str(brief or "").strip()

    ask = f"为《{work.get('title') or '未命名'}》生成 1 名主角与 2–6 名主要配角。"
    if brief_text:
        ask += f"补充要求：{brief_text}。"
    req = TextGenRequest(
        task="character",
        system="你是中文小说的人物编辑，只输出一个 JSON 对象（键为 characters），不要解释。",
        messages=[Message("user", ask)],
        stream=False,
        temperature=0.8,
        metadata={
            "work": {"title": work.get("title"), "author": work.get("author"), "genre": work.get("genre") or []},
            "premise": bible.get("premise") or {},
            "world": _bible_digest(bible),
            "count": count,
            "brief": brief_text,
            "seed": work.get("id"),
        },
    )
    payload = _generate_json(adapter, req, "characters")
    rows = payload.get("characters")
    if not isinstance(rows, list) or not rows:
        raise ValueError("LLM 返回的人物列表为空")

    characters: list[dict] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"第 {index + 1} 个人物必须是对象")
        name = _check_text(row.get("name"), 60, f"第 {index + 1} 个人物姓名", required=True)
        if name in seen:
            raise ValueError(f"人物姓名重复: {name}")
        seen.add(name)
        role = _normalize_choice(row.get("role"), ROLE_OPTIONS) or "supporting"
        gender = _normalize_choice(row.get("gender"), GENDER_OPTIONS) or "unknown"
        relationships = row.get("relationships") or {}
        if not isinstance(relationships, dict):
            relationships = {}
        characters.append(
            {
                "name": name,
                "role": role,
                "gender": gender,
                "age_range": _check_text(row.get("age_range"), 40, f"人物 {name} 的年龄区间") or None,
                "personality": _clean_str_list(row.get("personality"), 120, f"人物 {name} 的性格"),
                "background": _check_text(row.get("background"), 2000, f"人物 {name} 的背景"),
                "appearance_prompt": _check_text(row.get("appearance_prompt"), 2000, f"人物 {name} 的外貌提示词"),
                "negative_prompt": _check_text(row.get("negative_prompt"), 1000, f"人物 {name} 的负向提示词"),
                "relationships": {
                    _check_text(key, 60, f"人物 {name} 的关系对象", required=True): _check_text(note, 500, f"人物 {name} 的关系备注")
                    for key, note in relationships.items()
                },
            }
        )

    if not any(item["role"] == "protagonist" for item in characters):
        raise ValueError("人物里至少需要 1 个 protagonist（主角）")
    supporting = [item for item in characters if item["role"] in ("antagonist", "supporting", "minor")]
    if not 2 <= len(supporting) <= 6:
        raise ValueError(f"配角数量必须为 2–6 人（当前 {len(supporting)} 人）")
    return {"characters": characters}


def _clean_character_entry(row: dict) -> dict:
    """清洗单个完整人物（含配图提示词与关系备注）。

    参数:
        row: 模型返回的人物对象。

    返回:
        {name, role, gender, age_range, personality, background, appearance_prompt,
        negative_prompt, state.relationship_notes}。

    异常:
        ValueError: 类型或字段长度不合法。
    """
    if not isinstance(row, dict):
        raise ValueError("人物必须是 JSON 对象")
    name = _check_text(row.get("name"), 60, "人物姓名", required=True)
    role = _normalize_choice(row.get("role"), ROLE_OPTIONS) or "supporting"
    gender = _normalize_choice(row.get("gender"), GENDER_OPTIONS) or "unknown"
    state = row.get("state") if isinstance(row.get("state"), dict) else {}
    notes = state.get("relationship_notes")
    if not isinstance(notes, dict):
        notes = row.get("relationships") if isinstance(row.get("relationships"), dict) else {}
    return {
        "name": name,
        "role": role,
        "gender": gender,
        "age_range": _check_text(row.get("age_range"), 40, f"人物 {name} 的年龄区间") or None,
        "personality": _clean_str_list(row.get("personality"), 120, f"人物 {name} 的性格"),
        "background": _check_text(row.get("background"), 2000, f"人物 {name} 的背景"),
        "appearance_prompt": _check_text(row.get("appearance_prompt"), 2000, f"人物 {name} 的外貌提示词"),
        "negative_prompt": _check_text(row.get("negative_prompt"), 1000, f"人物 {name} 的负向提示词"),
        "state": {
            "relationship_notes": {
                _check_text(key, 60, f"人物 {name} 的关系对象", required=True): _check_text(note, 500, f"人物 {name} 的关系备注")
                for key, note in notes.items()
            }
        },
    }


def _section_fields(section: str, generated: dict, bible: dict) -> dict:
    """补齐一次生成区块的全部字段（生成值优先，回退已有值/空值）。

    参数:
        section: premise / style。
        generated: generate_settings 返回的字段。
        bible: 当前设定库。

    返回:
        该区块全部字段的 dict。
    """
    allowed = PREMISE_FIELDS if section == "premise" else STYLE_FIELDS
    existing = bible.get(section) or {}
    fields: dict = {}
    for key in allowed:
        value = generated.get(key)
        if value is None:
            value = existing.get(key)
        if value is None:
            if key == "forbidden":
                value = []
            elif key == "target_length":
                value = "novella"
            else:
                value = ""
        fields[key] = value
    return fields


def generate_section(
    store: ProjectStore,
    config: AppConfig,
    section: str,
    brief: str | None = None,
    target_id: str | None = None,
) -> dict:
    """一次生成一个区块的全部字段（梗概 / 文风 / 单条世界 / 单个人物）。

    参数:
        store: 工程。
        config: 本机配置。
        section: premise|style|world|character。
        brief: 用户补充要求。
        target_id: world 用世界 ID、character 用角色 ID；命中时作为改写上下文。

    返回:
        {"section", "fields", "target_id"?}；world/character 的 fields 为完整条目。

    异常:
        ValueError: section 非法、目标条目不存在或模型输出不合法。
    """
    key = str(section or "").strip()
    if key not in ("premise", "style", "world", "character"):
        raise ValueError(f"不支持的生成区块: {section}")

    settings_store = SettingsStore(store.root)
    bible = settings_store.load_or_none() or {}
    if key in ("premise", "style"):
        result = generate_settings(store, config, key, brief=brief)
        return {
            "section": key,
            "fields": _section_fields(key, result.get("fields") or {}, bible),
            "skipped": result.get("skipped") or [],
        }

    project = store.load_project()
    work = project.get("work") or {}
    adapter = build_writing_adapter(config, bible)
    brief_text = str(brief or "").strip()
    work_payload = {"title": work.get("title"), "author": work.get("author"), "genre": work.get("genre") or []}

    if key == "world":
        world_store = WorldStore(store.root)
        world_store.ensure()
        existing = world_store.get(target_id) if target_id else None
        if target_id and existing is None:
            raise ValueError(f"世界条目不存在: {target_id}")
        ask = "生成一条完整世界设定，字段：name/era/summary/rules/factions/locations/glossary/visual_style；locations/glossary 必须是数组，元素为对象，不要用字符串包裹。"
        if brief_text:
            ask += f"补充要求：{brief_text}。"
        req = TextGenRequest(
            task="setting",
            system="你是中文小说的世界观编辑，只输出一个 JSON 对象，不要解释。",
            messages=[Message("user", f"为《{work.get('title') or '未命名'}》{ask}")],
            stream=False,
            temperature=0.7,
            metadata={
                "step": "world",
                "work": work_payload,
                "existing": _bible_digest(bible),
                "existing_world": existing,
                "worlds": [{"name": item.get("name"), "era": item.get("era")} for item in world_store.list_worlds()],
                "brief": brief_text,
                "seed": work.get("id"),
            },
        )
        payload = _generate_json(adapter, req, "world")
        if isinstance(payload.get("world"), dict):
            payload = payload["world"]
        entry = clean_world_entry(payload)
        if existing:
            entry["id"] = existing.get("id") or entry["id"]
            entry["created_at"] = existing.get("created_at") or entry["created_at"]
            entry["cover_asset"] = existing.get("cover_asset")
        return {"section": "world", "fields": entry, "target_id": target_id}

    characters = store.load_characters()["characters"]
    existing_char = next((item for item in characters if item.get("id") == target_id), None) if target_id else None
    if target_id and existing_char is None:
        raise ValueError(f"角色不存在: {target_id}")
    ask = "生成一个完整人物设定，字段：name/role/gender/age_range/personality/background/appearance_prompt/negative_prompt/state.relationship_notes；personality 写 3-5 个短标签，每个不超过 20 字。"
    if brief_text:
        ask += f"补充要求：{brief_text}。"
    req = TextGenRequest(
        task="character",
        system="你是中文小说的人物编辑，只输出一个 JSON 对象，不要解释。",
        messages=[Message("user", f"为《{work.get('title') or '未命名'}》{ask}")],
        stream=False,
        temperature=0.8,
        metadata={
            "mode": "single",
            "work": work_payload,
            "premise": bible.get("premise") or {},
            "world": _bible_digest(bible),
            "characters": _characters_payload(characters),
            "existing_character": existing_char,
            "brief": brief_text,
            "seed": work.get("id"),
        },
    )
    payload = _generate_json(adapter, req, "character")
    raw = payload.get("character")
    if not isinstance(raw, dict):
        rows = payload.get("characters")
        raw = rows[0] if isinstance(rows, list) and rows else None
    if not isinstance(raw, dict) and any(key in payload for key in ("name", "role", "personality")):
        # 模型常把人物字段直接平铺返回，不再包一层 character
        raw = payload
    if not isinstance(raw, dict):
        raise ValueError("LLM 返回的人物不是合法 JSON 对象")
    entry = _clean_character_entry(raw)
    if existing_char:
        entry["id"] = existing_char.get("id") or entry.get("id")
    return {"section": "character", "fields": entry, "target_id": target_id}


def polish_field(
    store: ProjectStore,
    config: AppConfig,
    field: str,
    text: str,
    context: str | None = None,
) -> dict:
    """润色单个可填写字段（只返回文本，不写盘）。

    参数:
        store: 工程。
        config: 本机配置。
        field: 白名单内的字段名（见 POLISH_FIELDS）。
        text: 待润色文本。
        context: 可选上下文（如所属人物、章节摘要）。

    返回:
        {"field", "text"}。

    异常:
        ValueError: 字段不在白名单，或没有可润色的内容。
    """
    key = (field or "").strip()
    # 兼容前端按区块传参的 `premise.*` / `style.*` 前缀写法，归一化后再查白名单
    for prefix in ("premise.", "style."):
        if key.startswith(prefix):
            key = key[len(prefix):]
            break
    if key not in POLISH_FIELDS:
        raise ValueError(f"不支持的润色字段: {key or '(空)'}（可选 {'、'.join(POLISH_FIELDS)}）")
    source = (text or "").strip()
    if not source:
        raise ValueError("没有可润色的内容")

    bible = SettingsStore(store.root).load_or_none() or {}
    work = store.load_project().get("work") or {}
    adapter = build_writing_adapter(config, bible)
    req = TextGenRequest(
        task="polish",
        system="你是中文小说编辑，只润色给定字段，保持原意与信息量，只输出 JSON（键为 text）。",
        messages=[Message("user", f"润色字段 {key}：{source}")],
        stream=False,
        temperature=0.6,
        metadata={
            "field": key,
            "text": source,
            "context": context or "",
            "work": {"title": work.get("title"), "genre": work.get("genre") or []},
            "seed": work.get("id"),
        },
    )
    result = adapter.generate(req)
    polished = (result.text or "").strip()
    try:
        payload = _extract_json_object(polished)
        candidate = str(payload.get("text") or "").strip()
        if candidate:
            polished = candidate
    except ValueError:
        # 模型直接返回纯文本时按原文使用，不强求 JSON
        pass
    if not polished:
        raise ValueError("LLM 返回内容不是合法 JSON")
    limit = SCALAR_LIMITS.get(key)
    if limit and len(polished) > limit:
        polished = polished[:limit]
    return {"field": key, "text": polished}


def bind_outline_context(bible: dict, worlds: list[dict], characters: list[dict]) -> dict:
    """把大纲章节与节拍绑定到世界条目与角色（就地修改 bible）。

    参数:
        bible: 设定库 dict。
        worlds: 世界条目列表；为空时把每章 world_ids 写成空数组。
        characters: 角色表（含旁白，内部会过滤）。

    返回:
        {"chapters": 绑定章节数, "beats": 绑定节拍数, "worlds": 可用世界数}。
    """
    cast = [item for item in characters or [] if item.get("id") and item.get("role") != "narrator" and item.get("id") != entities.NARRATOR_ID]
    cast_ids = {item["id"] for item in cast}
    protagonist = next((item for item in cast if item.get("role") == "protagonist"), cast[0] if cast else None)
    order = ([protagonist] + [item for item in cast if item is not protagonist]) if protagonist else []
    world_ids = [item["id"] for item in worlds or [] if item.get("id")]

    chapters_bound = 0
    beats_bound = 0
    index = 0
    for act in (bible.get("outline") or {}).get("acts") or []:
        for chapter in act.get("chapters") or []:
            if world_ids:
                chapter["world_ids"] = [world_ids[index % len(world_ids)]]
            else:
                chapter["world_ids"] = []
            existing_pov = chapter.get("pov_character_id")
            if existing_pov in cast_ids:
                pov = existing_pov
            elif order:
                pov = order[index % len(order)]["id"]
            else:
                pov = None
            chapter["pov_character_id"] = pov
            chapters_bound += 1
            for beat in chapter.get("beats") or []:
                valid = [cid for cid in (beat.get("character_ids") or []) if cid in cast_ids]
                if not valid and pov:
                    valid = [pov]
                beat["character_ids"] = valid
                beats_bound += 1
            index += 1
    return {"chapters": chapters_bound, "beats": beats_bound, "worlds": len(world_ids)}


def bind_outline_characters(store: ProjectStore, bible: dict) -> dict:
    """把大纲章节与节拍绑定到已有世界与人物（兼容旧函数名）。

    读取工程内的世界列表后交给 `bind_outline_context`，每章按幕轮转绑定 1 个世界；
    工程里还没有世界条目时，每章 `world_ids` 写成空数组。

    参数:
        store: 工程（读取 worlds.json 与 characters.json）。
        bible: 设定库 dict。

    返回:
        {"chapters": 绑定章节数, "beats": 绑定节拍数, "worlds": 可用世界数}。
    """
    world_store = WorldStore(store.root)
    worlds = world_store.list_worlds() if world_store.exists() else []
    return bind_outline_context(bible, worlds, store.load_characters()["characters"])


def validate_chapter_binding(store: ProjectStore, chapter_id: str) -> dict:
    """校验章节是否已绑定可用的世界与角色。

    参数:
        store: 工程。
        chapter_id: 章节 ID。

    返回:
        {"chapter_id", "source", "world_ids", "pov_character_id", "character_ids"}。

    异常:
        ValueError: 绑定的世界条目不存在，或缺少非旁白角色，提示文案带操作指引。
    """
    settings_store = SettingsStore(store.root)
    bible = settings_store.ensure()
    characters = [
        item
        for item in store.load_characters()["characters"]
        if item.get("id") and item.get("role") != "narrator" and item.get("id") != entities.NARRATOR_ID
    ]
    cast_ids = {item["id"] for item in characters}
    entry = settings_store.find_outline_chapter(bible, chapter_id)

    if entry:
        # 世界是可选项：未绑定时回退设定库摘要继续生成；一旦绑定，引用的条目必须存在
        world_ids = list(entry.get("world_ids") or [])
        if world_ids:
            world_store = WorldStore(store.root)
            known = {item.get("id") for item in (world_store.list_worlds() if world_store.exists() else [])}
            missing = [world_id for world_id in world_ids if world_id not in known]
            if missing:
                raise ValueError(f"本章绑定的世界条目不存在：{'、'.join(missing)}；请先在世界页生成并绑定世界")
        beat_ids = {cid for beat in (entry.get("beats") or []) for cid in (beat.get("character_ids") or [])}
        linked = {cid for cid in {entry.get("pov_character_id"), *beat_ids} if cid and cid != entities.NARRATOR_ID}
        valid = linked & cast_ids
        if not valid:
            raise ValueError("本章还没有绑定角色：请先在人物页生成主要人物并在大纲页绑定")
        return {
            "chapter_id": chapter_id,
            "source": "outline",
            "world_ids": world_ids,
            "pov_character_id": entry.get("pov_character_id"),
            "character_ids": sorted(valid),
        }

    if not cast_ids:
        raise ValueError("本章还没有角色：请先在人物页生成主要人物并在大纲页绑定")
    chapter = store.load_chapter(chapter_id)
    pov = chapter.get("pov_character_id")
    return {
        "chapter_id": chapter_id,
        "source": "bible",
        "world_ids": [],
        "pov_character_id": pov if pov in cast_ids else None,
        "character_ids": sorted(cast_ids),
    }


def build_art_prompt(kind: str, entity: dict, style: dict | None = None, width: int | None = None, height: int | None = None) -> dict:
    """拼一条设定图提示词（委托 writing/worlds.build_art_prompt）。

    参数:
        kind: world|closeup|turnaround。
        entity: 世界条目或角色条目。
        style: 文风分区（读取 visual_style，可选 width/height）。
        width: 输出宽度；为空时取 style.width 或 1024。
        height: 输出高度；为空时取 style.height 或 1024。

    返回:
        {"prompt", "negative_prompt", "width", "height"}。

    异常:
        ValueError: kind 非法。
    """
    return _build_art_prompt(kind, entity, style, width=width, height=height)


def _outline_chapters(bible: dict) -> list[dict]:
    """按序号展开设定库里的全部大纲章节。

    参数:
        bible: 设定库 dict。

    返回:
        大纲章节列表（按 index 升序）。
    """
    rows: list[dict] = []
    for act in (bible.get("outline") or {}).get("acts") or []:
        rows.extend(act.get("chapters") or [])
    return sorted(rows, key=lambda item: int(item.get("index") or 0))


def check_golden_three(bible: dict) -> dict:
    """检查前三章是否满足「黄金三章」约束（不阻断落盘）。

    规则:
        第 1 章首个节拍摘要非空（钩子）；
        第 2 章节拍 ≥ 2，或章节摘要含冲突/对抗语义；
        第 3 章存在 turning_point=true 的节拍。

    参数:
        bible: 设定库 dict。

    返回:
        {"passed": bool, "issues": [{"chapter", "problem", "hint"}]}。
    """
    chapters = _outline_chapters(bible)[:3]
    issues: list[dict] = []

    if not chapters:
        issues.append({"chapter": 1, "problem": "设定库还没有大纲", "hint": "先执行 ndw outline generate 生成幕/章/节拍"})
        return {"passed": False, "issues": issues}

    first = chapters[0]
    first_beats = first.get("beats") or []
    if not (first_beats and str((first_beats[0] or {}).get("summary") or "").strip()):
        issues.append(
            {"chapter": 1, "problem": "第 1 章缺少开篇钩子", "hint": "在第 1 章首拍写清悬念/冲突/能力亮相，前 300 字抓住读者"}
        )

    if len(chapters) < 2:
        issues.append({"chapter": 2, "problem": "缺少第 2 章", "hint": "第 2 章需要确立核心矛盾与对抗关系"})
    else:
        second = chapters[1]
        second_beats = second.get("beats") or []
        summary = str(second.get("summary") or "")
        has_conflict = len(second_beats) >= 2 or any(word in summary for word in CONFLICT_KEYWORDS)
        if not has_conflict:
            issues.append(
                {"chapter": 2, "problem": "第 2 章没有确立核心矛盾", "hint": "把冲突写进章节摘要，或拆成至少 2 个节拍呈现对抗"}
            )

    if len(chapters) < 3:
        issues.append({"chapter": 3, "problem": "缺少第 3 章", "hint": "第 3 章需要出现第一次小高潮，让读者确认「这本书能看」"})
    else:
        third = chapters[2]
        turned = any(bool((beat or {}).get("turning_point")) for beat in (third.get("beats") or []))
        if not turned:
            issues.append(
                {"chapter": 3, "problem": "第 3 章缺少第一次小高潮", "hint": "把第 3 章某个节拍标记为 turning_point=true，形成阶段性反转"}
            )

    return {"passed": not issues, "issues": issues}
