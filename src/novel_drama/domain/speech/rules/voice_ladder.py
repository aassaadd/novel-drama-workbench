"""音色解析优先级（10 级）与音色描述推导哈希。

优先级（从高到低），所有配音入口必须共用这一份：

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

`lookup(voice_id, emotion, variant_id)` 负责把「音色 + 变体」解析成合成条目（含参考音频），
由适配层提供；本模块只决定优先级、来源标注与告警文案，不做 IO。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable

#: 允许被章节级覆盖的角色：旁白与龙套
OVERRIDABLE_ROLES: frozenset[str] = frozenset({"narrator"})


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
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    return "sha256:" + digest


def empty_result(source: str = "missing") -> dict:
    """构造统一的解析结果（未命中时也保证字段齐全）。

    参数:
        source: 来源标注。

    返回:
        `{voice_id, variant_id, ref_audio, ref_text, source, decided_by, warnings}`。
    """
    return {
        "voice_id": None,
        "variant_id": None,
        "ref_audio": None,
        "ref_text": None,
        "source": source,
        "decided_by": {"level": 0, "evidence": None},
        "warnings": [],
    }


def _chapter_override(
    character: dict | None,
    line: dict,
    chapter: dict,
    cast: dict,
    lookup: Callable[[str | None, str | None, str | None], dict | None],
) -> dict | None:
    """解析章节级覆盖；不合法时返回带 `invalid` 说明的 dict。

    参数:
        character: 角色 dict（可能为空）。
        line: 台词行。
        chapter: 章节 dict。
        cast: `tts.cast` 配置（用于按 `persona_id` 回查音色）。
        lookup: 条目解析函数。

    返回:
        命中条目、带 `invalid` 的说明，或 None（该角色没有覆盖）。
    """
    overrides = chapter.get("voice_overrides") or {}
    override = overrides.get(line.get("speaker_id"))
    if not isinstance(override, dict):
        return None
    role = (character or {}).get("role")
    stock = (character or {}).get("stock") or {}
    if role not in OVERRIDABLE_ROLES and not stock:
        return {"invalid": "章节覆盖只允许旁白或龙套角色"}
    if not str(override.get("reason") or "").strip():
        return {"invalid": "章节覆盖必须写 reason"}
    entry = lookup(override.get("voice_id"), line.get("emotion"), override.get("variant_id"))
    if entry is None and override.get("persona_id"):
        persona = next(
            (item for item in (cast.get("narrators") or []) if item.get("id") == override.get("persona_id")),
            None,
        )
        if persona:
            entry = lookup(persona.get("voice_id"), line.get("emotion"), override.get("variant_id"))
    return entry or {"invalid": "章节覆盖指向的音色不存在"}


def resolve_voice(
    line: dict,
    character: dict | None,
    chapter: dict,
    cast: dict,
    settings: dict,
    lookup: Callable[[str | None, str | None, str | None], dict | None],
    persona: dict | None = None,
    *,
    is_narrator: bool = False,
    is_unknown_speaker: bool = False,
    honor_line_override: bool = True,
) -> dict:
    """解析一行台词最终应使用的音色。

    参数:
        line: 台词行。
        character: 说话人角色 dict（未建过的路人为 None）。
        chapter: 章节 dict（可带 `voice_overrides`）。
        cast: `tts.cast` 配置。
        settings: 工程设置（读取 `tts.default_voice_id`）。
        lookup: `(voice_id, emotion, variant_id) -> 合成条目`，由适配层提供。
        persona: 当前生效的旁白人格（`narrator_persona` 的结果）。
        is_narrator: 说话人是否是旁白。
        is_unknown_speaker: 说话人是否是「未识别说话人」。
        honor_line_override: 是否启用行级音色覆盖。

    返回:
        `{"voice_id","variant_id","ref_audio","ref_text","source","decided_by","warnings"}`。
    """
    emotion = line.get("emotion")
    speaker_id = line.get("speaker_id")
    warnings: list[str] = []
    line_voice = line.get("voice") or {}

    def hit(entry: dict | None, level: int, evidence: str, source: str) -> dict | None:
        """把命中条目包装成统一返回结构。"""
        if not entry:
            return None
        result = empty_result(source)
        result.update({key: entry.get(key) for key in ("voice_id", "variant_id", "ref_audio", "ref_text")})
        result["decided_by"] = {"level": level, "evidence": evidence}
        result["warnings"] = warnings
        result["issue"] = entry.get("issue")
        return result

    if honor_line_override:
        if line_voice.get("variant_id"):
            entry = lookup(line_voice.get("voice_id"), emotion, line_voice.get("variant_id"))
            if not str(line_voice.get("reason") or "").strip():
                warnings.append("行级音色覆盖未写 reason")
            result = hit(entry, 1, "line.voice.variant_id", "line_variant")
            if result:
                return result
        if line_voice.get("voice_id"):
            entry = lookup(line_voice.get("voice_id"), emotion, None)
            if not str(line_voice.get("reason") or "").strip():
                warnings.append("行级音色覆盖未写 reason")
            result = hit(entry, 2, "line.voice.voice_id", "line_voice")
            if result:
                return result

    override = _chapter_override(character, line, chapter, cast, lookup)
    if isinstance(override, dict) and override.get("invalid"):
        warnings.append(str(override["invalid"]))
    else:
        result = hit(override, 3, f"chapter.voice_overrides[{speaker_id}]", "chapter_override")
        if result:
            return result

    if is_narrator and persona and persona.get("voice_id"):
        entry = lookup(persona.get("voice_id"), emotion, None)
        result = hit(entry, 4, f"tts.cast.narrators[{persona.get('id')}]", "narrator_persona")
        if result:
            return result

    binding = (character or {}).get("voice_binding") or {}
    binding_voice = binding.get("voice_library_id") or binding.get("voice_id")
    if binding_voice:
        entry = lookup(binding_voice, emotion, binding.get("default_variant_id"))
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
            entry = lookup(stock_voice, emotion, None)
            result = hit(entry, 6, f"tts.cast.stock_defaults[{key}]", "stock_pool")
            if result:
                return result

    if (character or {}).get("role") == "minor" and cast.get("minor_fallback_voice_id"):
        entry = lookup(cast["minor_fallback_voice_id"], emotion, None)
        result = hit(entry, 7, "tts.cast.minor_fallback_voice_id", "minor_fallback")
        if result:
            return result

    if is_narrator:
        if cast.get("narrator_fallback_voice_id"):
            entry = lookup(cast["narrator_fallback_voice_id"], emotion, None)
            result = hit(entry, 7, "tts.cast.narrator_fallback_voice_id", "narrator_fallback")
            if result:
                return result
        if cast.get("unknown_voice_id"):
            entry = lookup(cast["unknown_voice_id"], emotion, None)
            result = hit(entry, 9, "tts.cast.unknown_voice_id", "unknown_fallback")
            if result:
                return result
        default_voice = (settings.get("tts") or {}).get("default_voice_id")
        if default_voice:
            entry = lookup(default_voice, emotion, None)
            result = hit(entry, 10, "project.tts.default_voice_id", "project_default")
            if result:
                warnings.append("旁白未配置人格与绑定，已回落到工程默认音色")
                return result
        return empty_result()

    if is_unknown_speaker or character is None:
        if cast.get("unknown_voice_id"):
            entry = lookup(cast.get("unknown_voice_id"), emotion, None)
            result = hit(entry, 8, "tts.cast.unknown_voice_id", "unknown_fallback")
            if result:
                warnings.append(f"未建过的路人 {speaker_id} 使用通用路人音色，请归入龙套池")
                return result

    default_voice = (settings.get("tts") or {}).get("default_voice_id")
    if default_voice:
        entry = lookup(default_voice, emotion, None)
        result = hit(entry, 9, "project.tts.default_voice_id", "project_default")
        if result:
            warnings.append("角色未绑定音色，已回落到工程默认音色")
            return result
    return empty_result()
