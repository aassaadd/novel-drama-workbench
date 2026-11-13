"""R28 音色编排：自动匹配、预览、应用与撤销。

设计文档：`docs/spec-r28-voice-orchestration-auto.md`。

原则：
- 规则匹配、结果可解释（每条建议带理由）；
- 旁白固定（`tts.cast.narrators` 优先），龙套按 `性别:年龄段` 复用同一音色池；
- 只写 `character.voice_binding` 与 `chapter.voice_overrides`，不改音色库；
- 自动编排 / 单行替换 / 批量自动采用都写撤销日志，至少可撤销最近一次。
"""

from __future__ import annotations

import copy
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from ..models import entities
from ..store.voice_cast import cast_config
from ..store.voice_store import find_voice, pick_variant

#: 年龄段（由弱到强），用于年龄相近性打分
_AGE_BANDS = ["child", "youth", "adult", "middle", "senior"]
_AGE_WORDS = {
    "child": ("童", "幼", "小孩", "少年"),
    "youth": ("少年", "青年", "青春", "20", "18"),
    "adult": ("成年", "青年", "30", "20"),
    "middle": ("中年", "40", "50", "壮年"),
    "senior": ("老年", "老", "60", "70", "晚年"),
}

#: 音色被非龙套角色占用超过该数量视为「重复过多」
_DUPLICATE_LIMIT = 3

#: 固定筛选
_FILTERS = ["need", "unbound", "chapter", "duplicate", "all"]


def _now() -> str:
    """当前 UTC 时间（ISO 8601）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _undo_dir(store) -> Path:
    """撤销日志目录。"""
    return store.voice_asset_dir("undo")


def age_band(text: str | None) -> str:
    """把 `age_range` 文本归一为年龄段。

    参数:
        text: 例如「30s」「中年」「少年」。

    返回:
        child/youth/adult/middle/senior；无法识别时返回空串。
    """
    raw = str(text or "")
    for band in _AGE_BANDS:
        if any(word in raw for word in _AGE_WORDS[band]):
            return band
    nums = [int(item) for item in re.findall(r"\d+", raw)]
    if not nums:
        return ""
    age = nums[0]
    if age < 14:
        return "child"
    if age < 26:
        return "youth"
    if age < 40:
        return "adult"
    if age < 55:
        return "middle"
    return "senior"


def _voice_age_band(voice: dict) -> str:
    """从音色名称与标签推断年龄段。"""
    text = " ".join([str(voice.get("name") or "")] + [str(item) for item in voice.get("tags") or []])
    return age_band(text)


def _variant_of(voice: dict) -> dict | None:
    """挑一个可用变体（默认变体优先）。"""
    return pick_variant(voice, None, False)


def _usable(voice: dict) -> bool:
    """音色是否可用于绑定（状态正常且至少一个可用变体）。"""
    if voice.get("status") in ("unusable", "archived"):
        return False
    return _variant_of(voice) is not None


def _voice_options(voices: list[dict]) -> list[dict]:
    """下拉选项：只列可用音色。"""
    return [
        {"id": voice.get("id"), "name": voice.get("name"), "gender": voice.get("gender")}
        for voice in voices
        if _usable(voice)
    ]


def load_minor_pool(store) -> dict:
    """读取龙套音色池（`assets/voice/casting.json`）。"""
    try:
        return (json.loads((store.voice_asset_dir("casting") / "casting.json").read_text(encoding="utf-8")) or {}).get("minor_pool") or {}
    except (OSError, ValueError):
        return {}


def save_minor_pool(store, pool: dict) -> None:
    """落盘龙套音色池，保证跨章节复用同一音色。"""
    target = store.voice_asset_dir("casting")
    target.mkdir(parents=True, exist_ok=True)
    (target / "casting.json").write_text(
        json.dumps({"minor_pool": pool, "updated_at": _now()}, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _narrator_voice(store, characters_by_id: dict) -> tuple[str | None, str]:
    """旁白固定音色：`tts.cast.narrators` 第一个可用音色，其次沿用旁白绑定。"""
    for persona in cast_config(store).get("narrators") or []:
        voice_id = str(persona.get("voice_id") or "")
        if voice_id:
            return voice_id, f"旁白固定人格「{persona.get('label') or persona.get('id')}」"
    narrator = characters_by_id.get(getattr(entities, "NARRATOR_ID", "chr_narrator")) or {}
    binding = (narrator.get("voice_binding") or {}).get("voice_id")
    if binding:
        return str(binding), "沿用旁白当前绑定"
    return None, "旁白未指定固定音色（请在 tts.cast.narrators 配置）"


def _score(character: dict, voice: dict, usage: dict[str, int]) -> tuple[int, list[str]] | None:
    """按角色设定给音色打分；明确性别不一致时直接排除。"""
    reasons = []
    score = 0
    c_gender = character.get("gender")
    v_gender = voice.get("gender")
    if c_gender in ("male", "female"):
        if v_gender != c_gender:
            return None
        score += 40
        reasons.append("性别一致")
    elif c_gender in ("other", "unknown") and v_gender == c_gender:
        score += 4
        reasons.append("性别标签一致")
    c_band = age_band(character.get("age_range"))
    v_band = _voice_age_band(voice)
    if c_band and v_band:
        if c_band == v_band:
            score += 20
            reasons.append("年龄感一致")
        elif abs(_AGE_BANDS.index(c_band) - _AGE_BANDS.index(v_band)) == 1:
            score += 8
            reasons.append("年龄感接近")
    text = " ".join([str(voice.get("name") or "")] + [str(item) for item in voice.get("tags") or []])
    role_words = {
        "protagonist": ("主角", "少年", "青年", "明亮", "有力"),
        "antagonist": ("反派", "低沉", "沙哑", "压迫"),
        "supporting": ("配角", "温和", "自然"),
    }.get(str(character.get("role") or ""), ())
    if any(word in text for word in role_words):
        score += 10
        reasons.append("身份标签匹配")
    style = character.get("speech_style") or {}
    if style.get("accent") and str(style["accent"]) in text:
        score += 8
        reasons.append("口音匹配")
    if style.get("pitch") and str(style["pitch"]) in text:
        score += 5
        reasons.append("音区匹配")
    used = int(usage.get(voice.get("id")) or 0)
    if used:
        score -= 15 * used
        reasons.append(f"已被 {used} 个角色使用，降权")
    else:
        reasons.append("未被占用")
    if any(str(item.get("source") or "") == "designed" for item in voice.get("variants") or []):
        score += 3
        reasons.append("本工程生成音色优先")
    return score, reasons


def _suggest(store, character: dict, voices: list[dict], usage: dict[str, int], pool: dict, narrator_voice) -> dict | None:
    """为单个角色给出建议音色与理由。"""
    role = str(character.get("role") or "")
    current = (character.get("voice_binding") or {}).get("voice_id")
    if role == "narrator":
        if not narrator_voice:
            return None
        voice = find_voice({"voices": voices}, narrator_voice)
        if not voice:
            return None
        return {"voice_id": voice.get("id"), "voice_name": voice.get("name"), "reason": "旁白固定音色，不参与匹配"}
    if role == "minor":
        key = f"{character.get('gender') or 'unknown'}:{age_band(character.get('age_range')) or 'adult'}"
        voice = find_voice({"voices": voices}, pool.get(key) or "")
        if voice is None:
            ranked = []
            for item in voices:
                pair = _score(character, item, usage)
                if pair is not None:
                    ranked.append((pair[0], item))
            if not ranked:
                return None
            voice = max(ranked, key=lambda item: (item[0], str(item[1].get("id"))))[1]
        pool[key] = voice.get("id")
        return {"voice_id": voice.get("id"), "voice_name": voice.get("name"), "reason": f"龙套池 {key} 复用，保证跨章节不串音"}
    scored = []
    for voice in voices:
        pair = _score(character, voice, usage)
        if pair is None:
            continue
        scored.append((pair[0], voice, pair[1]))
    if not scored:
        return None
    score, voice, reasons = max(scored, key=lambda item: (item[0], str(item[1].get("id"))))
    reason = "；".join(reasons)
    if voice.get("id") == current:
        reason += "；与当前绑定一致"
    return {"voice_id": voice.get("id"), "voice_name": voice.get("name"), "reason": reason, "score": score}


def _chapter_overrides(store) -> dict[str, list[dict]]:
    """收集每个角色的章节覆盖：{character_id: [{chapter_id,chapter_title,voice_id}]}。"""
    result: dict[str, list[dict]] = {}
    for summary in store.list_chapters():
        chapter = store.load_chapter(summary["id"])
        for character_id, override in (chapter.get("voice_overrides") or {}).items():
            if not isinstance(override, dict):
                continue
            result.setdefault(character_id, []).append(
                {
                    "chapter_id": chapter.get("id"),
                    "chapter_title": chapter.get("title"),
                    "voice_id": override.get("voice_id"),
                }
            )
    return result


def _usage(characters: list[dict]) -> dict[str, int]:
    """统计非龙套角色的音色占用次数。"""
    usage: dict[str, int] = {}
    for character in characters:
        if character.get("role") == "minor":
            continue
        voice_id = (character.get("voice_binding") or {}).get("voice_id")
        if voice_id:
            usage[str(voice_id)] = usage.get(str(voice_id), 0) + 1
    return usage


def _has_chapter_override(character: dict) -> bool:
    """角色是否允许章节级音色覆盖（旁白或带 stock 的龙套）。

    章节覆盖只对旁白 / 龙套生效（见 voice_cast._chapter_override），普通角色
    即使残留覆盖也不会被采用，因此不参与「跨章不一致」判定。
    """
    return character.get("role") == "narrator" or bool(character.get("stock"))


def _problems(
    character: dict,
    voices_by_id: dict,
    overrides: dict[str, list[dict]],
    usage: dict[str, int],
    pool: dict,
) -> list[dict]:
    """判定角色的问题行（未绑定 / 缺失 / 性别不一致 / 跨章不一致 / 重复过多 / 龙套池缺失）。"""
    problems = []
    binding = character.get("voice_binding") or {}
    voice_id = binding.get("voice_id") or binding.get("voice_library_id")
    if not voice_id:
        problems.append({"code": "unbound", "message": "未绑定音色"})
        return problems
    voice = voices_by_id.get(str(voice_id))
    if voice is None:
        problems.append({"code": "missing", "message": "绑定音色不在音色库"})
    else:
        if voice.get("status") in ("unusable", "archived") or _variant_of(voice) is None:
            problems.append({"code": "missing", "message": "绑定音色没有可用变体"})
        c_gender, v_gender = character.get("gender"), voice.get("gender")
        if c_gender in ("male", "female") and v_gender in ("male", "female") and c_gender != v_gender:
            problems.append({"code": "gender", "message": "角色性别与音色性别不一致"})
    if _has_chapter_override(character):
        chapter_overrides = overrides.get(character.get("id")) or []
        distinct = {str(item.get("voice_id")) for item in chapter_overrides if item.get("voice_id")}
        if len(distinct) > 1 or (distinct and str(voice_id) not in distinct):
            problems.append({"code": "chapter", "message": "同一角色跨章节音色不一致"})
    if character.get("role") != "minor" and int(usage.get(str(voice_id)) or 0) > _DUPLICATE_LIMIT:
        problems.append({"code": "duplicate", "message": f"音色被 {usage[str(voice_id)]} 个角色重复使用"})
    if character.get("role") == "minor":
        key = f"{character.get('gender') or 'unknown'}:{age_band(character.get('age_range')) or 'adult'}"
        if key not in pool:
            problems.append({"code": "pool", "message": "龙套音色池缺少该性别/年龄段"})
    return problems


def _rows(store, query: str = "", filter_name: str = "need", preview: dict | None = None) -> dict:
    """构造音色编排总览（问题行 + 正常行摘要 + 下拉选项）。"""
    characters = store.load_characters().get("characters") or []
    voices = store.load_voices().get("voices") or []
    voices_by_id = {str(item.get("id")): item for item in voices}
    overrides = _chapter_overrides(store)
    usage = _usage(characters)
    pool = load_minor_pool(store)
    suggestions = {item["character_id"]: item for item in (preview or {}).get("items") or []}

    rows, normal = [], []
    for character in characters:
        problems = _problems(character, voices_by_id, overrides, usage, pool)
        binding = character.get("voice_binding") or {}
        voice_id = binding.get("voice_id") or binding.get("voice_library_id")
        voice = voices_by_id.get(str(voice_id)) if voice_id else None
        suggestion = suggestions.get(character.get("id"))
        row = {
            "character_id": character.get("id"),
            "name": character.get("name"),
            "role": character.get("role"),
            "gender": character.get("gender"),
            "age_range": character.get("age_range"),
            "current_voice_id": voice_id,
            "current_voice_name": (voice or {}).get("name"),
            "problems": problems,
            "suggestion": suggestion,
        }
        keyword = str(query or "").strip()
        if keyword and keyword not in str(row["name"] or "") and keyword not in str(row["current_voice_name"] or ""):
            continue
        wanted = filter_name if filter_name in _FILTERS else "need"
        codes = {item["code"] for item in problems}
        if wanted == "unbound" and "unbound" not in codes:
            continue
        if wanted == "chapter" and "chapter" not in codes:
            continue
        if wanted == "duplicate" and "duplicate" not in codes:
            continue
        if wanted == "need" and not problems:
            normal.append(
                {"character_id": row["character_id"], "name": row["name"], "role": row["role"], "current_voice_name": row["current_voice_name"]}
            )
            continue
        rows.append(row)
    return {
        "stats": {
            "characters": len(characters),
            "need": sum(1 for item in characters if _problems(item, voices_by_id, overrides, usage, pool)),
            "normal": sum(1 for item in characters if not _problems(item, voices_by_id, overrides, usage, pool)),
        },
        "rows": rows,
        "normal": normal,
        "voices": _voice_options(voices),
        "filters": _FILTERS,
    }


def overview(store, query: str = "", filter_name: str = "need") -> dict:
    """音色编排总览：默认只返回问题行，正常行只给摘要。"""
    preview = _build_preview(store)
    return _rows(store, query, filter_name, preview)


def _build_preview(store) -> dict:
    """生成自动编排建议（旁白固定、龙套池、普通角色打分）。"""
    characters = store.load_characters().get("characters") or []
    voices = [item for item in store.load_voices().get("voices") or [] if _usable(item)]
    by_id = {str(item.get("id")): item for item in characters}
    usage = _usage(characters)
    pool = load_minor_pool(store)
    narrator_voice, narrator_reason = _narrator_voice(store, by_id)
    overrides = _chapter_overrides(store)
    items, skipped = [], []
    for character in characters:
        current = (character.get("voice_binding") or {}).get("voice_id")
        suggestion = _suggest(store, character, voices, usage, pool, narrator_voice)
        if not suggestion:
            skipped.append(
                {
                    "character_id": character.get("id"),
                    "name": character.get("name"),
                    "role": character.get("role"),
                    "reason": "旁白固定音色不可用，请检查 tts.cast.narrators 或音色库"
                    if character.get("role") == "narrator"
                    else "音色库没有符合性别/年龄条件的可用音色",
                }
            )
            continue
        if character.get("role") == "narrator" and not suggestion.get("reason"):
            suggestion["reason"] = narrator_reason
        # 章节覆盖只对旁白 / 龙套生效，普通角色的残留覆盖会被解析层忽略，不做自动改写
        chapter_overrides = (overrides.get(character.get("id")) or []) if _has_chapter_override(character) else []
        align = [
            {"chapter_id": item["chapter_id"], "chapter_title": item["chapter_title"], "before": item.get("voice_id"), "after": suggestion["voice_id"]}
            for item in chapter_overrides
            if str(item.get("voice_id") or "") != str(suggestion["voice_id"])
        ]
        items.append(
            {
                "character_id": character.get("id"),
                "name": character.get("name"),
                "role": character.get("role"),
                "current": current,
                "voice_id": suggestion["voice_id"],
                "voice_name": suggestion["voice_name"],
                "reason": suggestion["reason"],
                "action": "bind" if str(current or "") != str(suggestion["voice_id"]) else "keep",
                "overrides": align,
            }
        )
        usage[suggestion["voice_id"]] = usage.get(suggestion["voice_id"], 0) + 1
    return {"items": items, "skipped": skipped, "minor_pool": pool}


def preview(store) -> dict:
    """自动编排预览：每个角色的建议、理由与需对齐的章节覆盖。"""
    result = _build_preview(store)
    result["changed"] = sum(1 for item in result["items"] if item["action"] == "bind")
    result["stats"] = {"items": len(result["items"]), "changed": result["changed"], "skipped": len(result["skipped"])}
    return result


def _write_undo(store, kind: str, characters: list[dict], chapters: list[dict] | None = None) -> str:
    """写撤销日志（只保留最近 20 条）。"""
    target = _undo_dir(store)
    target.mkdir(parents=True, exist_ok=True)
    undo_id = entities.new_id("undo")
    payload = {
        "id": undo_id,
        "kind": kind,
        "created_at": _now(),
        "characters": characters,
        "chapters": chapters or [],
    }
    path = target / f"{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}-{undo_id}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    files = sorted(target.glob("*.json"))
    for item in files[:-20]:
        item.unlink(missing_ok=True)
    return undo_id


def record_binding_undo(store, character: dict) -> str:
    """记录单角色绑定快照（供候选采用等外部流程调用）。"""
    return _write_undo(
        store,
        "generation",
        [{"character_id": character.get("id"), "voice_binding": copy.deepcopy(character.get("voice_binding"))}],
    )


def record_batch_undo(store, characters: list[dict]) -> str:
    """批量自动生成前记录参与角色的绑定快照（整批一次撤销）。"""
    return _write_undo(
        store,
        "generation",
        [{"character_id": item.get("id"), "voice_binding": copy.deepcopy(item.get("voice_binding"))} for item in characters],
    )


def _binding_for(voice: dict, previous: dict | None) -> dict:
    """构造角色绑定（保留原有 adapter 与情绪映射）。"""
    previous = previous or {}
    variant = _variant_of(voice) or {}
    return {
        "adapter": previous.get("adapter") or "placeholder",
        "voice_id": voice.get("id"),
        "voice_library_id": voice.get("id"),
        "default_variant_id": previous.get("default_variant_id") if previous.get("voice_id") == voice.get("id") else variant.get("id"),
        "emotion_variant_map": previous.get("emotion_variant_map") or {},
    }


def apply(store, character_ids: list[str] | None = None) -> dict:
    """应用自动编排建议并写撤销日志。

    参数:
        store: 工程。
        character_ids: 只应用这些角色；为空应用全部有变化的角色。

    返回:
        `{applied: [...], undo_id}`。
    """
    plan = _build_preview(store)
    wanted = set(character_ids or [])
    targets = [
        item
        for item in plan["items"]
        if item.get("action") == "bind" and (not wanted or item["character_id"] in wanted)
    ]
    if not targets:
        return {"applied": [], "undo_id": None}
    characters = store.load_characters()
    rows = {item["id"]: item for item in characters["characters"]}
    voices = {str(item.get("id")): item for item in store.load_voices().get("voices") or []}
    undo_characters, undo_chapters, applied = [], [], []
    chapters_cache: dict[str, dict] = {}
    chapter_snapshots: set[str] = set()
    for item in targets:
        character = rows.get(item["character_id"])
        voice = voices.get(str(item["voice_id"]))
        if character is None or voice is None or not _usable(voice):
            continue
        undo_characters.append({"character_id": character["id"], "voice_binding": copy.deepcopy(character.get("voice_binding"))})
        character["voice_binding"] = _binding_for(voice, character.get("voice_binding"))
        character["updated_at"] = _now()
        for override in item.get("overrides") or []:
            chapter_id = override["chapter_id"]
            chapter = chapters_cache.get(chapter_id) or store.load_chapter(chapter_id)
            chapters_cache[chapter_id] = chapter
            # 同一章节只快照一次，避免多次覆盖后撤销回到中间态
            if chapter_id not in chapter_snapshots:
                chapter_snapshots.add(chapter_id)
                undo_chapters.append(
                    {"chapter_id": chapter_id, "voice_overrides": copy.deepcopy(chapter.get("voice_overrides") or {})}
                )
            section = chapter.setdefault("voice_overrides", {}).setdefault(character["id"], {})
            section["voice_id"] = voice.get("id")
            # 换音色后旧的变体 / 人格指向失效，一并清掉
            section.pop("variant_id", None)
            section.pop("persona_id", None)
            section["reason"] = section.get("reason") or "自动编排：与全局音色对齐"
        applied.append({"character_id": character["id"], "voice_id": voice.get("id"), "voice_name": voice.get("name")})
    if not applied:
        return {"applied": [], "undo_id": None}
    undo_id = _write_undo(store, "casting", undo_characters, undo_chapters)
    store.save_characters(characters)
    for chapter in chapters_cache.values():
        store.save_chapter(chapter)
    save_minor_pool(store, plan["minor_pool"])
    return {"applied": applied, "undo_id": undo_id}


def bind_one(store, character_id: str, voice_id: str | None) -> dict:
    """单行替换角色音色并写撤销日志（voice_id 为空表示解绑）。"""
    characters = store.load_characters()
    character = next((item for item in characters["characters"] if item.get("id") == character_id), None)
    if character is None:
        raise ValueError("角色不存在")
    voice = find_voice(store.load_voices(), voice_id) if voice_id else None
    if voice_id and (voice is None or not _usable(voice)):
        raise ValueError("音色不存在或没有可用变体")
    undo_characters = [{"character_id": character_id, "voice_binding": copy.deepcopy(character.get("voice_binding"))}]
    if voice is None:
        character["voice_binding"] = None
    else:
        character["voice_binding"] = _binding_for(voice, character.get("voice_binding"))
    character["updated_at"] = _now()
    undo_id = _write_undo(store, "manual", undo_characters)
    store.save_characters(characters)
    return {"character_id": character_id, "voice_id": (voice or {}).get("id"), "undo_id": undo_id}


def undo_state(store) -> dict:
    """最近一次可撤销动作的摘要（用于按钮可用态）。"""
    files = sorted(_undo_dir(store).glob("*.json")) if _undo_dir(store).exists() else []
    if not files:
        return {"available": False}
    try:
        payload = json.loads(files[-1].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"available": False}
    return {
        "available": True,
        "id": payload.get("id"),
        "kind": payload.get("kind"),
        "created_at": payload.get("created_at"),
        "characters": len(payload.get("characters") or []),
    }


def undo_last(store, kind: str | None = None) -> dict:
    """撤销最近一次自动编排/自动生成/单行替换。

    参数:
        store: 工程。
        kind: 只撤销该类型（casting/generation/manual）；为空取最近一条。

    返回:
        `{restored: [...], kind, id}`。

    异常:
        ValueError: 没有可撤销的记录。
    """
    files = sorted(_undo_dir(store).glob("*.json"), reverse=True) if _undo_dir(store).exists() else []
    target = None
    for path in files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if kind and payload.get("kind") != kind:
            continue
        target = (path, payload)
        break
    if target is None:
        raise ValueError("没有可撤销的记录")
    path, payload = target
    characters = store.load_characters()
    rows = {item["id"]: item for item in characters["characters"]}
    restored = []
    for item in payload.get("characters") or []:
        character = rows.get(item.get("character_id"))
        if character is None:
            continue
        character["voice_binding"] = item.get("voice_binding")
        character["updated_at"] = _now()
        restored.append(item.get("character_id"))
    store.save_characters(characters)
    for snapshot in payload.get("chapters") or []:
        try:
            chapter = store.load_chapter(snapshot["chapter_id"])
        except Exception:  # noqa: BLE001 - 章节已删除时跳过该条
            continue
        chapter["voice_overrides"] = snapshot.get("voice_overrides") or {}
        store.save_chapter(chapter)
    path.unlink(missing_ok=True)
    return {"restored": restored, "kind": payload.get("kind"), "id": payload.get("id")}
