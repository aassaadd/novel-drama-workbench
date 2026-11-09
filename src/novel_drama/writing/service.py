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
    name = str(config.get("providers.writing") or "offline").strip()
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

    返回:
        {"acts": [...], "chapters": N, "beats": M, "provider": name, "settings_revision": r}。

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
    if not dry_run:
        bible = settings_store.save(bible)
    return {
        "acts": built_acts,
        "chapters": chapter_count,
        "beats": beat_count,
        "provider": getattr(result, "model", "") or "offline-writing",
        "settings_revision": bible.get("settings_revision"),
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
    existing = {item["id"]: item for item in store.list_chapters()}
    created: list[str] = []
    updated: list[str] = []

    for entry in sorted(entries, key=lambda item: int(item.get("index") or 0)):
        outline_text = "\n".join(
            f"{index}. {beat.get('summary')}" for index, beat in enumerate(entry.get("beats") or [], start=1)
        ) or (entry.get("summary") or "")
        chapter = existing.get(entry["id"])
        if chapter is None:
            if dry_run:
                created.append(f"{entry.get('index')} {entry.get('title')}（待创建）")
                continue
            chapter = entities.new_chapter(work_id, int(entry.get("index") or 1), entry.get("title") or "未命名", outline=outline_text)
            chapter["id"] = entry["id"]
            store.save_chapter(chapter)
            created.append(chapter["id"])
            existing[chapter["id"]] = chapter
        else:
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

    settings_store = SettingsStore(store.root)
    bible = settings_store.ensure()
    characters = store.load_characters()["characters"]
    beats = _chapter_beats(bible, chapter, settings_store)
    chapter_words = int(sum(int(beat.get("target_words") or 0) for beat in beats) or DEFAULT_CHAPTER_WORDS)
    if chapter_words <= 0:
        chapter_words = DEFAULT_CHAPTER_WORDS
    adapter = build_writing_adapter(config, bible)

    req = TextGenRequest(
        task="chapter",
        system="你是中文小说作者，按节拍写正文；对白用中文引号，不要写标题。",
        messages=[Message("user", f"写《{chapter.get('title')}》正文，约 {chapter_words} 字。")],
        stream=False,
        metadata={
            "title": chapter.get("title"),
            "summary": (settings_store.find_outline_chapter(bible, chapter_id) or {}).get("summary") or chapter.get("outline"),
            "beats": beats,
            "characters": _characters_payload(characters),
            "pov": (bible.get("style") or {}).get("pov"),
            "tone": (bible.get("style") or {}).get("tone"),
            "target_words": chapter_words,
        },
    )
    result = adapter.generate(req)
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
