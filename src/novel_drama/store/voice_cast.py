"""全剧音色编排（R18）：统一解析优先级与一致性校验。

设计要点：
- 所有配音入口统一走 `resolve_cast_voice()`，避免「章节级临时决定」散落各处；
- 解析优先级固定为 10 级（行级 > 章节覆盖 > 角色绑定 > 龙套池 > 兜底 > 工程默认）；
- 旁白保持单实体 `chr_narrator`，男/女声用命名人格（`tts.cast.narrators`）+ 切换点表达；
- 校验只报「证据可定位」的问题，error 阻断配音、warn 只提示。
"""

from __future__ import annotations

import hashlib
import json

from ..models.entities import NARRATOR_ID, UNKNOWN_SPEAKER_ID
from .voice_store import find_voice, pick_variant, variant_ref

#: 允许被章节级覆盖的角色：旁白与龙套
_OVERRIDABLE_ROLES = {"narrator"}


def _sha(text: str) -> str:
    """计算 sha256 摘要（带 sha256: 前缀）。"""
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def profile_source_hash(character: dict) -> str:
    """计算音色描述的推导输入哈希（字段一变即判 stale）。

    参数:
        character: 角色 dict。

    返回:
        `sha256:...` 字符串。
    """
    payload = {
        "gender": character.get("gender"),
        "age_range": character.get("age_range"),
        "personality": character.get("personality") or [],
        "background": character.get("background") or "",
        "speech_style": character.get("speech_style") or {},
        "appearance_prompt": character.get("appearance_prompt") or "",
    }
    return _sha(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def cast_config(store) -> dict:
    """读取工程级音色编排配置（缺失时返回空 dict）。"""
    return ((store.settings().get("tts") or {}).get("cast") or {})


def _empty(source: str = "missing") -> dict:
    """构造统一的解析结果（未命中时也保证字段齐全）。"""
    return {
        "voice_id": None,
        "variant_id": None,
        "ref_audio": None,
        "ref_text": None,
        "source": source,
        "decided_by": {"level": 0, "evidence": None},
        "warnings": [],
    }


def _entry_from_voice(store, voices: dict, voice_id: str | None, emotion: str | None, variant_id: str | None = None) -> dict | None:
    """把「音色 + 变体」解析成合成所需条目。

    参数:
        store: ProjectStore。
        voices: 音色库文档。
        voice_id: 音色 ID。
        emotion: 台词情绪。
        variant_id: 指定变体 ID（可为空）。

    返回:
        条目 dict；音色不存在时返回 None。
    """
    voice = find_voice(voices, voice_id) if voice_id else None
    if voice is None and variant_id:
        for item in voices.get("voices") or []:
            if any(v.get("id") == variant_id for v in item.get("variants") or []):
                voice = item
                break
    if voice is None:
        return None
    if voice.get("status") in ("unusable", "archived"):
        # 音色整条不可用：即使变体仍为 ready，也不允许拿去合成
        return {
            "voice_id": voice.get("id"),
            "variant_id": None,
            "ref_audio": None,
            "ref_text": None,
            "source": "unavailable",
            "issue": "voice_not_usable",
        }
    variant = None
    if variant_id:
        for item in voice.get("variants") or []:
            if item.get("id") == variant_id:
                variant = item
                break
    if variant is None:
        variant = pick_variant(voice, emotion, require_reference=True)
    if variant is None:
        return {
            "voice_id": voice.get("id"),
            "variant_id": None,
            "ref_audio": None,
            "ref_text": None,
            "source": "unavailable",
            "issue": "variant_not_ready",
        }
    ref_audio, ref_text = variant_ref(store, variant)
    return {
        "voice_id": voice.get("id"),
        "variant_id": variant.get("id"),
        "ref_audio": ref_audio,
        "ref_text": ref_text,
        "source": "cast",
    }


def _chapter_order(store) -> dict[str, int]:
    """返回章节 ID → 序号映射（按 chapters 目录 index 排序，与创建时间无关）。"""
    cached = getattr(store, "_r18_chapter_order", None)
    if cached is not None:
        return cached
    order = {item["id"]: int(item.get("index") or 0) for item in store.list_chapters() if item.get("id")}
    try:
        store._r18_chapter_order = order  # noqa: SLF001 - 进程内缓存，避免逐行重扫目录
    except AttributeError:
        pass
    return order


def _effective_switches(cast: dict, order: dict[str, int], max_index: int) -> list[dict]:
    """把切换点展开成带起止序号的区间（end 缺省时延续到下一个切换点之前）。

    参数:
        cast: `tts.cast` 配置。
        order: 章节 ID → 序号映射。
        max_index: 全剧最大章节序号。

    返回:
        [{start, end, persona_id, reason}]，按 start 升序。
    """
    raw = []
    for switch in cast.get("narrator_switches") or []:
        start = int(order.get(switch.get("at_chapter_id")) or 0)
        if not start:
            continue
        raw.append(
            {
                "start": start,
                "end": int(order.get(switch.get("end_chapter_id")) or 0) if switch.get("end_chapter_id") else 0,
                "persona_id": switch.get("persona_id"),
                "reason": switch.get("reason"),
                "only": switch.get("scope") == "this_chapter_only",
            }
        )
    raw.sort(key=lambda item: item["start"])
    for index, item in enumerate(raw):
        if item["only"]:
            item["end"] = item["start"]
        elif not item["end"]:
            following = raw[index + 1]["start"] if index + 1 < len(raw) else 0
            item["end"] = (following - 1) if following else max_index
    return raw


def narrator_persona(cast: dict, chapter: dict, order: dict[str, int]) -> dict | None:
    """按章节位置解析当前生效的旁白人格（含区间结束章节）。

    参数:
        cast: `tts.cast` 配置。
        chapter: 当前章节 dict。
        order: 章节 ID → 序号映射。

    返回:
        人格 dict；未配置时返回 None。
    """
    personas = {item.get("id"): item for item in cast.get("narrators") or [] if item.get("id")}
    current = int(order.get(chapter.get("id")) or chapter.get("index") or 0)
    max_index = max(order.values()) if order else current
    chosen = None
    for item in _effective_switches(cast, order, max_index):
        if item["start"] <= current <= item["end"]:
            chosen = item["persona_id"]
    if chosen is None:
        chosen = cast.get("default_narrator_id")
    return personas.get(chosen) if chosen else None


def narrator_plan(cast: dict, chapters: list[dict]) -> list[dict]:
    """生成「每一章用哪个旁白」的展开表，供页面预览与校验共用。

    参数:
        cast: `tts.cast` 配置。
        chapters: 章节列表（需含 id/index/title）。

    返回:
        [{chapter_id, index, title, persona_id, label, source}]，source=switch|default。
    """
    order = {item.get("id"): int(item.get("index") or 0) for item in chapters if item.get("id")}
    max_index = max(order.values()) if order else 0
    personas = {item.get("id"): item for item in cast.get("narrators") or [] if item.get("id")}
    ranges = _effective_switches(cast, order, max_index)
    plan = []
    for chapter in sorted(chapters, key=lambda item: int(item.get("index") or 0)):
        index = int(chapter.get("index") or 0)
        persona_id = cast.get("default_narrator_id")
        source = "default"
        for item in ranges:
            if item["start"] <= index <= item["end"]:
                persona_id = item["persona_id"]
                source = "switch"
        persona = personas.get(persona_id) or {}
        plan.append(
            {
                "chapter_id": chapter.get("id"),
                "index": index,
                "title": chapter.get("title"),
                "persona_id": persona_id,
                "label": persona.get("label") or "（未配置旁白人格）",
                "source": source,
            }
        )
    return plan


def normalize_cast(raw: dict) -> dict:
    """清洗页面提交的 `tts.cast`：丢掉空行、空串转 None、补齐默认 scope。

    参数:
        raw: 前端提交的 cast 对象。

    返回:
        可直接落盘并过 schema 的 cast 对象。
    """
    def text(value) -> str | None:
        """空串/空白转 None，其余去首尾空白。"""
        cleaned = str(value or "").strip()
        return cleaned or None

    cast: dict = {}
    narrators = []
    for item in raw.get("narrators") or []:
        if not isinstance(item, dict):
            continue
        pid = text(item.get("id"))
        label = text(item.get("label"))
        if not pid or not label:
            continue
        persona = {"id": pid, "label": label, "voice_id": text(item.get("voice_id"))}
        if isinstance(item.get("voice_profile"), dict):
            persona["voice_profile"] = item["voice_profile"]
        narrators.append(persona)
    if narrators:
        cast["narrators"] = narrators
    cast["default_narrator_id"] = text(raw.get("default_narrator_id"))

    switches = []
    for item in raw.get("narrator_switches") or []:
        if not isinstance(item, dict):
            continue
        at = text(item.get("at_chapter_id"))
        persona_id = text(item.get("persona_id"))
        reason = text(item.get("reason"))
        if not at or not persona_id:
            continue
        switch = {"at_chapter_id": at, "persona_id": persona_id, "reason": reason or ""}
        end = text(item.get("end_chapter_id"))
        if end:
            switch["end_chapter_id"] = end
        switch["scope"] = "this_chapter_only" if item.get("scope") == "this_chapter_only" else "from_here"
        switches.append(switch)
    cast["narrator_switches"] = switches

    pool = []
    for item in raw.get("voice_pool") or []:
        if not isinstance(item, dict):
            continue
        slot = text(item.get("slot"))
        label = text(item.get("label"))
        if not slot or not label:
            continue
        entry = {"slot": slot, "label": label, "voice_id": text(item.get("voice_id"))}
        if text(item.get("gender")):
            entry["gender"] = text(item.get("gender"))
        if text(item.get("age_feel")):
            entry["age_feel"] = text(item.get("age_feel"))
        pool.append(entry)
    cast["voice_pool"] = pool

    defaults = {}
    for key, value in (raw.get("stock_defaults") or {}).items():
        if text(key) and text(value):
            defaults[text(key)] = text(value)
    cast["stock_defaults"] = defaults

    for field in ("unknown_voice_id", "minor_fallback_voice_id", "narrator_fallback_voice_id"):
        cast[field] = text(raw.get(field))
    for flag in ("strict_minor", "strict_bgm", "honor_line_override"):
        if isinstance(raw.get(flag), bool):
            cast[flag] = raw[flag]
    return cast


def validate_cast_config(
    cast: dict,
    chapters: list[dict],
    voice_ids: set[str] | None = None,
    stock_slots: set[str] | None = None,
) -> list[str]:
    """校验音色池与旁白切换点，返回中文错误列表（空列表表示通过）。

    参数:
        cast: 待保存的 `tts.cast`。
        chapters: 章节列表（含 id/index/title）。
        voice_ids: 允许引用的音色 ID 集合；为空表示不校验音色存在性。
        stock_slots: 角色实际使用的龙套槽位集合。

    返回:
        错误信息列表。
    """
    errors: list[str] = []
    personas = {}
    for index, persona in enumerate(cast.get("narrators") or [], start=1):
        pid = str(persona.get("id") or "").strip()
        if not pid:
            errors.append(f"第 {index} 个旁白人格缺少 ID")
            continue
        if pid in personas:
            errors.append(f"旁白人格 ID 重复：{pid}")
        personas[pid] = persona
        if not str(persona.get("label") or "").strip():
            errors.append(f"旁白人格 {pid} 缺少名称")
        voice_id = persona.get("voice_id")
        if voice_id and voice_ids is not None and voice_id not in voice_ids:
            errors.append(f"旁白人格「{persona.get('label') or pid}」指向不存在的音色：{voice_id}")

    default_id = cast.get("default_narrator_id")
    if default_id and default_id not in personas:
        errors.append(f"默认旁白人格不存在：{default_id}")

    order = {item.get("id"): int(item.get("index") or 0) for item in chapters if item.get("id")}
    max_index = max(order.values()) if order else 0
    switches = []
    for index, switch in enumerate(cast.get("narrator_switches") or [], start=1):
        start = order.get(switch.get("at_chapter_id"))
        if not start:
            errors.append(f"第 {index} 个切换点的起始章节不存在")
            continue
        end_chapter = switch.get("end_chapter_id")
        end = order.get(end_chapter) if end_chapter else None
        if end_chapter and not end:
            errors.append(f"第 {index} 个切换点的结束章节不存在：{end_chapter}")
            continue
        if switch.get("scope") == "this_chapter_only":
            end = start
        if end is not None and end < start:
            errors.append(f"第 {index} 个切换点的结束章节早于起始章节")
            continue
        persona_id = switch.get("persona_id")
        if persona_id not in personas:
            errors.append(f"第 {index} 个切换点指向不存在的旁白人格：{persona_id}")
        if not str(switch.get("reason") or "").strip():
            errors.append(f"第 {index} 个切换点缺少切换理由")
        switches.append({"start": start, "end": end, "persona_id": persona_id, "index": index})

    switches.sort(key=lambda item: item["start"])
    for position, current in enumerate(switches):
        following = switches[position + 1] if position + 1 < len(switches) else None
        current_end = current["end"] if current["end"] is not None else (following["start"] - 1 if following else max_index)
        if following and following["start"] <= current_end:
            errors.append(
                f"切换区间重叠：第 {following['start']} 章同时被第 {current['index']} 与第 {following['index']} 个切换点覆盖"
            )
        if following and current["end"] is not None and following["start"] > current["end"] + 1:
            missing = (
                f"第 {current['end'] + 1} 章"
                if following["start"] - 1 == current["end"] + 1
                else f"第 {current['end'] + 1}~{following['start'] - 1} 章"
            )
            errors.append(f"切换区间不连续：{missing}没有旁白归属（要么延长上一段，要么补一个切换点）")

    seen_pool = set()
    for index, slot in enumerate(cast.get("voice_pool") or [], start=1):
        name = str(slot.get("slot") or "").strip()
        if not name:
            errors.append(f"第 {index} 个音色池槽位缺少 slot")
            continue
        if name in seen_pool:
            errors.append(f"音色池槽位重复：{name}")
        seen_pool.add(name)
        if not str(slot.get("label") or "").strip():
            errors.append(f"音色池槽位 {name} 缺少名称")
        voice_id = slot.get("voice_id")
        if voice_id and voice_ids is not None and voice_id not in voice_ids:
            errors.append(f"音色池槽位 {name} 指向不存在的音色：{voice_id}")

    known_keys = seen_pool | (stock_slots or set())
    for key, voice_id in (cast.get("stock_defaults") or {}).items():
        if stock_slots is not None and key not in known_keys:
            errors.append(f"龙套映射 {key} 没有对应的音色池或职业槽位")
        if voice_ids is not None and voice_id not in voice_ids:
            errors.append(f"龙套映射 {key} 指向不存在的音色：{voice_id}")

    for field, label in (
        ("unknown_voice_id", "通用路人音色"),
        ("minor_fallback_voice_id", "龙套兜底音色"),
        ("narrator_fallback_voice_id", "旁白兜底音色"),
    ):
        voice_id = cast.get(field)
        if voice_id and voice_ids is not None and voice_id not in voice_ids:
            errors.append(f"{label}指向不存在的音色：{voice_id}")
    return errors


def _chapter_override(store, chapter: dict, character: dict, line: dict, voices: dict) -> dict | None:
    """解析章节级覆盖；不合法时返回 None 并在 warnings 里留痕。

    参数:
        store: ProjectStore。
        chapter: 章节 dict。
        character: 角色 dict（可能为空）。
        line: 台词行。
        voices: 音色库文档。

    返回:
        命中条目或 None。
    """
    overrides = chapter.get("voice_overrides") or {}
    override = overrides.get(line.get("speaker_id"))
    if not isinstance(override, dict):
        return None
    role = (character or {}).get("role")
    stock = (character or {}).get("stock") or {}
    if role not in _OVERRIDABLE_ROLES and not stock:
        return {"invalid": "章节覆盖只允许旁白或龙套角色"}
    if not str(override.get("reason") or "").strip():
        return {"invalid": "章节覆盖必须写 reason"}
    entry = _entry_from_voice(store, voices, override.get("voice_id"), line.get("emotion"), override.get("variant_id"))
    if entry is None and override.get("persona_id"):
        persona = next(
            (item for item in (cast_config(store).get("narrators") or []) if item.get("id") == override.get("persona_id")),
            None,
        )
        if persona:
            entry = _entry_from_voice(store, voices, persona.get("voice_id"), line.get("emotion"), override.get("variant_id"))
    return entry or {"invalid": "章节覆盖指向的音色不存在"}


def resolve_cast_voice(store, characters_table: dict, chapter: dict, line: dict, voices: dict | None = None) -> dict:
    """解析一行台词最终应使用的音色（R18 唯一入口）。

    优先级（从高到低）：
        1. `line.voice.variant_id`
        2. `line.voice.voice_id`
        3. `chapter.voice_overrides[character_id]`（仅旁白/龙套，必须写 reason）
        4. 旁白人格切换点 / `character.voice_binding`
        5. `tts.cast.stock_defaults[slot|voice_slot]`
        6. `tts.cast.minor_fallback_voice_id`
        7. `tts.cast.narrator_fallback_voice_id`
        8. `tts.cast.unknown_voice_id`（未建过的路人）
        9. `project.tts.default_voice_id`
        10. 未命中：`source=missing`，由调用方报 VOICE_MISSING

    参数:
        store: ProjectStore。
        characters_table: 角色表。
        chapter: 章节 dict。
        line: 台词行 dict。
        voices: 音色库文档（可复用，避免逐行读盘）。

    返回:
        {"voice_id","variant_id","ref_audio","ref_text","source","decided_by","warnings"}。
    """
    voices = voices if voices is not None else store.load_voices()
    cast = cast_config(store)
    settings = store.settings()
    characters = {item.get("id"): item for item in characters_table.get("characters") or []}
    speaker_id = line.get("speaker_id")
    character = characters.get(speaker_id)
    emotion = line.get("emotion")
    warnings: list[str] = []
    line_voice = line.get("voice") or {}

    def hit(entry: dict | None, level: int, evidence: str, source: str) -> dict | None:
        """把命中条目包装成统一返回结构。"""
        if not entry:
            return None
        result = _empty(source)
        result.update({key: entry.get(key) for key in ("voice_id", "variant_id", "ref_audio", "ref_text")})
        result["decided_by"] = {"level": level, "evidence": evidence}
        result["warnings"] = warnings
        result["issue"] = entry.get("issue")
        return result

    if cast.get("honor_line_override", True):
        if line_voice.get("variant_id"):
            entry = _entry_from_voice(store, voices, line_voice.get("voice_id"), emotion, line_voice.get("variant_id"))
            if not str(line_voice.get("reason") or "").strip():
                warnings.append("行级音色覆盖未写 reason")
            result = hit(entry, 1, "line.voice.variant_id", "line_variant")
            if result:
                return result
        if line_voice.get("voice_id"):
            entry = _entry_from_voice(store, voices, line_voice.get("voice_id"), emotion, None)
            if not str(line_voice.get("reason") or "").strip():
                warnings.append("行级音色覆盖未写 reason")
            result = hit(entry, 2, "line.voice.voice_id", "line_voice")
            if result:
                return result

    override = _chapter_override(store, chapter, character, line, voices)
    if isinstance(override, dict) and override.get("invalid"):
        warnings.append(str(override["invalid"]))
    else:
        result = hit(override, 3, f"chapter.voice_overrides[{speaker_id}]", "chapter_override")
        if result:
            return result

    if speaker_id == NARRATOR_ID:
        persona = narrator_persona(cast, chapter, _chapter_order(store))
        if persona and persona.get("voice_id"):
            entry = _entry_from_voice(store, voices, persona.get("voice_id"), emotion)
            result = hit(entry, 4, f"tts.cast.narrators[{persona.get('id')}]", "narrator_persona")
            if result:
                return result

    binding = (character or {}).get("voice_binding") or {}
    binding_voice = binding.get("voice_library_id") or binding.get("voice_id")
    if binding_voice:
        entry = _entry_from_voice(store, voices, binding_voice, emotion, binding.get("default_variant_id"))
        result = hit(entry, 5, "character.voice_binding", "character_binding")
        if result:
            return result

    stock = (character or {}).get("stock") or {}
    if stock:
        defaults = cast.get("stock_defaults") or {}
        # 职业槽位优先，其次音色池槽位；evidence 记录实际命中的键，避免回落时误导排查
        for key in (stock.get("slot"), stock.get("voice_slot")):
            stock_voice = defaults.get(key) if key else None
            if not stock_voice:
                continue
            entry = _entry_from_voice(store, voices, stock_voice, emotion)
            result = hit(entry, 6, f"tts.cast.stock_defaults[{key}]", "stock_pool")
            if result:
                return result

    if (character or {}).get("role") == "minor" and cast.get("minor_fallback_voice_id"):
        entry = _entry_from_voice(store, voices, cast["minor_fallback_voice_id"], emotion)
        result = hit(entry, 7, "tts.cast.minor_fallback_voice_id", "minor_fallback")
        if result:
            return result

    if speaker_id == NARRATOR_ID:
        if cast.get("narrator_fallback_voice_id"):
            entry = _entry_from_voice(store, voices, cast["narrator_fallback_voice_id"], emotion)
            result = hit(entry, 7, "tts.cast.narrator_fallback_voice_id", "narrator_fallback")
            if result:
                return result
        if cast.get("unknown_voice_id"):
            entry = _entry_from_voice(store, voices, cast["unknown_voice_id"], emotion)
            result = hit(entry, 9, "tts.cast.unknown_voice_id", "unknown_fallback")
            if result:
                return result
        default_voice = (settings.get("tts") or {}).get("default_voice_id")
        if default_voice:
            entry = _entry_from_voice(store, voices, default_voice, emotion)
            result = hit(entry, 10, "project.tts.default_voice_id", "project_default")
            if result:
                warnings.append("旁白未配置人格与绑定，已回落到工程默认音色")
                return result
        return _empty()

    if speaker_id == UNKNOWN_SPEAKER_ID or character is None:
        if cast.get("unknown_voice_id"):
            entry = _entry_from_voice(store, voices, cast["unknown_voice_id"], emotion)
            result = hit(entry, 8, "tts.cast.unknown_voice_id", "unknown_fallback")
            if result:
                warnings.append(f"未建过的路人 {speaker_id} 使用通用路人音色，请归入龙套池")
                return result

    default_voice = (settings.get("tts") or {}).get("default_voice_id")
    if default_voice:
        entry = _entry_from_voice(store, voices, default_voice, emotion)
        result = hit(entry, 9, "project.tts.default_voice_id", "project_default")
        if result:
            warnings.append("角色未绑定音色，已回落到工程默认音色")
            return result
    return _empty()


def _voice_of(entry: dict) -> str | None:
    """从解析结果里取音色 ID（仅当有可用参考音频时视为「已定音」）。"""
    return entry.get("voice_id") if entry.get("ref_audio") else None


def _add(findings: list[dict], level: str, code: str, message: str, **where) -> None:
    """追加一条校验结果。"""
    findings.append({"level": level, "code": code, "message": message, **{k: v for k, v in where.items() if v}})


def check_cast(store, chapter_id: str | None = None) -> dict:
    """跑全剧音色/BGM 一致性校验。

    参数:
        store: ProjectStore。
        chapter_id: 只校验该章；为空校验全剧。

    返回:
        {"level": error|warn|ok, "findings": [...], "stats": {...}}。
    """
    characters_table = store.load_characters()
    characters = {item.get("id"): item for item in characters_table.get("characters") or []}
    voices = store.load_voices()
    cast = cast_config(store)
    settings = store.settings()
    strict_minor = bool(cast.get("strict_minor"))
    strict_bgm = bool(cast.get("strict_bgm"))
    chapters = [store.load_chapter(chapter_id)] if chapter_id else [store.load_chapter(item["id"]) for item in store.list_chapters()]
    findings: list[dict] = []
    stats = {"chapters": len(chapters), "lines": 0, "speakers": 0, "resolved": 0, "warnings": 0}

    # 角色池：描述缺失 / 过期 / 龙套槽位冲突
    speaking: set[str] = set()
    for chapter in chapters:
        for scene in chapter.get("scenes") or []:
            for line in scene.get("lines") or []:
                if (line.get("text") or "").strip():
                    speaking.add(line.get("speaker_id"))

    slot_owner: dict[str, str] = {}
    for character in characters.values():
        stock = character.get("stock") or {}
        if stock.get("slot"):
            other = slot_owner.get(stock["slot"])
            if other and other != character.get("id") and stock.get("reuse_policy") != "per_scene":
                _add(
                    findings,
                    "error",
                    "STOCK_SLOT_CONFLICT",
                    f"龙套槽位 {stock['slot']} 被 {other} 与 {character.get('id')} 同时占用",
                    character_id=character.get("id"),
                )
            slot_owner.setdefault(stock["slot"], character.get("id"))

    for character in characters.values():
        if character.get("id") not in speaking and character.get("role") != "narrator":
            continue
        profile = character.get("voice_profile") or {}
        if not profile:
            _add(
                findings,
                "error" if character.get("role") in ("narrator", "protagonist") else "warn",
                "VOICE_PROFILE_MISSING",
                f"角色「{character.get('name')}」缺少音色描述",
                character_id=character.get("id"),
            )
            continue
        expected = profile_source_hash(character)
        actual = (profile.get("source") or {}).get("from_hash")
        if actual and actual != expected:
            _add(
                findings,
                "warn",
                "VOICE_PROFILE_STALE",
                f"角色「{character.get('name')}」的音色描述已过期（人物档案有改动）",
                character_id=character.get("id"),
            )

    # 逐章解析 + 同场景撞音
    narrator_voice_by_chapter: list[tuple[int, str, str | None]] = []
    character_voice_seen: dict[str, set[str]] = {}
    order_map = _chapter_order(store)
    max_index = max(order_map.values()) if order_map else 0
    switch_ranges = _effective_switches(cast, order_map, max_index)
    for chapter in chapters:
        order = order_map
        chapter_index = int(order.get(chapter.get("id")) or chapter.get("index") or 0)
        # 章节级覆盖：只允许旁白/龙套，旁白还必须命中已登记的切换点区间
        for overridden_id, override in (chapter.get("voice_overrides") or {}).items():
            if not isinstance(override, dict):
                continue
            character = characters.get(overridden_id) or {}
            name = character.get("name") or "未登记角色"
            if not str(override.get("reason") or "").strip():
                _add(
                    findings,
                    "error",
                    "CHAPTER_OVERRIDE_FORBIDDEN",
                    f"章节「{chapter.get('title')}」对角色「{name}」的音色覆盖缺少切换理由",
                    chapter_id=chapter.get("id"),
                    character_id=overridden_id,
                )
                continue
            if overridden_id == NARRATOR_ID:
                persona_id = override.get("persona_id")
                allowed = any(
                    item["start"] <= chapter_index <= item["end"]
                    and (not persona_id or item["persona_id"] == persona_id)
                    for item in switch_ranges
                )
                if not allowed:
                    _add(
                        findings,
                        "error",
                        "NARRATOR_SWITCH_ILLEGAL",
                        f"章节「{chapter.get('title')}」改了旁白音色，但不在已登记的旁白切换点区间内",
                        chapter_id=chapter.get("id"),
                        character_id=overridden_id,
                    )
            elif not character.get("stock"):
                _add(
                    findings,
                    "error",
                    "CHAPTER_OVERRIDE_FORBIDDEN",
                    f"章节「{chapter.get('title')}」不允许覆盖角色「{name}」的音色（仅旁白或龙套可覆盖）",
                    chapter_id=chapter.get("id"),
                    character_id=overridden_id,
                )
        for scene in chapter.get("scenes") or []:
            scene_voices: dict[str, list[str]] = {}
            for line in scene.get("lines") or []:
                if not (line.get("text") or "").strip():
                    continue
                stats["lines"] += 1
                line_voice = line.get("voice") or {}
                explicit_override = bool(line_voice.get("voice_id") or line_voice.get("variant_id"))
                if explicit_override and cast.get("honor_line_override", True) and not str(line_voice.get("reason") or "").strip():
                    _add(
                        findings,
                        "warn",
                        "LINE_OVERRIDE_NO_REASON",
                        f"章节「{chapter.get('title')}」有台词行指定了音色但没写 reason",
                        chapter_id=chapter.get("id"),
                        line_id=line.get("id"),
                    )
                entry = resolve_cast_voice(store, characters_table, chapter, line, voices=voices)
                stats["warnings"] += len(entry.get("warnings") or [])
                voice_id = _voice_of(entry)
                speaker = line.get("speaker_id")
                if speaker == UNKNOWN_SPEAKER_ID or speaker not in characters:
                    _add(
                        findings,
                        "error" if strict_minor else "warn",
                        "STOCK_UNCLASSIFIED",
                        f"未归类说话人 {speaker}（章节「{chapter.get('title')}」）",
                        chapter_id=chapter.get("id"),
                        line_id=line.get("id"),
                    )
                if not voice_id:
                    name = (characters.get(speaker) or {}).get("name") or speaker
                    issue = entry.get("issue")
                    if issue in ("variant_not_ready", "voice_not_usable"):
                        message = (
                            f"角色「{name}」绑定的音色变体未就绪"
                            if issue == "variant_not_ready"
                            else f"角色「{name}」绑定的音色已归档或不可用"
                        )
                        _add(
                            findings,
                            "error",
                            "VOICE_VARIANT_NOT_READY",
                            message,
                            chapter_id=chapter.get("id"),
                            line_id=line.get("id"),
                            character_id=speaker,
                        )
                    else:
                        _add(
                            findings,
                            "error",
                            "VOICE_BINDING_UNRESOLVED",
                            f"角色「{name}」无可用音色",
                            chapter_id=chapter.get("id"),
                            line_id=line.get("id"),
                            character_id=speaker,
                        )
                    continue
                stats["resolved"] += 1
                if speaker:
                    # 显式行级覆盖不算漂移，避免把正常例外误判为跨章不一致
                    if not explicit_override:
                        character_voice_seen.setdefault(speaker, set()).add(voice_id)
                    if speaker not in (NARRATOR_ID, UNKNOWN_SPEAKER_ID):
                        scene_voices.setdefault(voice_id, []).append(speaker)
            for voice_id, speakers in scene_voices.items():
                if len(set(speakers)) > 1:
                    _add(
                        findings,
                        "warn",
                        "SCENE_VOICE_COLLISION",
                        f"场景内多个角色共用音色 {voice_id}：{'、'.join(sorted(set(speakers)))}",
                        chapter_id=chapter.get("id"),
                    )
        narrator_entry = None
        for scene in chapter.get("scenes") or []:
            for line in scene.get("lines") or []:
                if line.get("speaker_id") == NARRATOR_ID and (line.get("text") or "").strip():
                    narrator_entry = resolve_cast_voice(store, characters_table, chapter, line, voices=voices)
                    break
            if narrator_entry:
                break
        if narrator_entry:
            narrator_voice_by_chapter.append(
                (int(chapter.get("index") or 0), chapter.get("id"), _voice_of(narrator_entry))
            )

    narrator_voice_by_chapter.sort()
    switches = {item.get("at_chapter_id") for item in cast.get("narrator_switches") or []}
    previous = None
    for _, chapter_key, voice_id in narrator_voice_by_chapter:
        if previous and voice_id and previous != voice_id and chapter_key not in switches:
            _add(
                findings,
                "error",
                "NARRATOR_DRIFT",
                "旁白音色在章节间发生变化，但没有登记切换点",
                chapter_id=chapter_key,
            )
        if voice_id:
            previous = voice_id

    for character_id, seen in character_voice_seen.items():
        character = characters.get(character_id) or {}
        if character_id in (NARRATOR_ID, UNKNOWN_SPEAKER_ID):
            continue
        if len(seen) > 1 and not (character.get("stock") or {}).get("slot"):
            _add(
                findings,
                "error",
                "CHARACTER_VOICE_DRIFT",
                f"角色「{character.get('name')}」在不同章节解析到不同音色",
                character_id=character_id,
            )

    findings.extend(_check_bgm(store, chapters, strict_bgm))
    stats["speakers"] = len(speaking)
    errors = [item for item in findings if item["level"] == "error"]
    return {
        "level": "error" if errors else ("warn" if findings else "ok"),
        "findings": findings,
        "stats": stats,
    }


def _check_bgm(store, chapters: list[dict], strict: bool) -> list[dict]:
    """检查 BGM 规划、cue 锚点与循环一致性。

    参数:
        store: ProjectStore。
        chapters: 章节列表。
        strict: true 时未规划 BGM 记为 error。

    返回:
        findings 列表。
    """
    from ..media.bgm import load_cues

    findings: list[dict] = []
    for chapter in chapters:
        chapter_id = chapter.get("id")
        plans: dict[str, dict] = {}
        script_path = store.script_path(chapter_id)
        if script_path.exists():
            try:
                script = store.load_script(chapter_id)
            except (OSError, ValueError):
                script = {}
            for shot in script.get("shots") or []:
                plan = shot.get("bgm")
                if plan:
                    plans[plan.get("id")] = {"plan": plan, "shot": shot}
                else:
                    _add(
                        findings,
                        "error" if strict else "warn",
                        "BGM_PLAN_MISSING",
                        f"镜头 {shot.get('index')}「{shot.get('scene_label')}」未规划 BGM",
                        chapter_id=chapter_id,
                    )
                for segment in (plan or {}).get("segments") or []:
                    line_ids = {item.get("id") for item in shot.get("lines") or []}
                    if segment.get("anchor_line_id") and segment["anchor_line_id"] not in line_ids:
                        _add(
                            findings,
                            "warn",
                            "BGM_SEGMENT_ANCHOR_ORPHAN",
                            f"镜头 {shot.get('index')} 的第 {segment.get('index')} 段锚点行已不存在",
                            chapter_id=chapter_id,
                        )
        for cue in load_cues(store.root, chapter_id):
            plan_ref = cue.get("plan_ref") or {}
            plan_id = plan_ref.get("plan_id")
            if plan_id and plan_id not in plans:
                _add(
                    findings,
                    "warn",
                    "BGM_PLAN_GONE",
                    f"cue「{cue.get('name') or cue.get('id')}」引用的 BGM 描述已不存在",
                    chapter_id=chapter_id,
                )
            if plan_id:
                plan = (plans.get(plan_id) or {}).get("plan") or {}
                if plan.get("loop_required") and not cue.get("loop"):
                    _add(
                        findings,
                        "warn",
                        "BGM_LOOP_MISMATCH",
                        f"cue「{cue.get('name') or cue.get('id')}」未按描述开启循环",
                        chapter_id=chapter_id,
                    )
            if not cue.get("asset_id"):
                _add(
                    findings,
                    "warn",
                    "BGM_ASSET_MISSING",
                    f"cue「{cue.get('name') or cue.get('id')}」尚未选择音频素材",
                    chapter_id=chapter_id,
                )
    return findings
