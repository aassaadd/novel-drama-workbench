"""R30 音色编排面板「按角色自动补齐」。

从人物档案与音色库推断 `project.settings.tts.cast` 里的可推断项：
- 音色池：龙套按 `性别:年龄段` 归池，池子不存在则建，并按性别与音色标签推荐音色；
- 龙套映射：把龙套的职业槽位 / 音色池槽位映射到推断出的池音色；
- 旁白人格：按旁白（缺档案时用主角）的描述挑选合适预设，并补默认人格；
- 旁白切换点：属于叙事决策，不从档案臆测，统一标「需要人工指定」。

原则：
- 只补空项，不覆盖已经填好的值（不静默改配置）；
- 输出每一项的「改成什么 / 依据 / 为什么不动」，供页面先预览再确认；
- 不改 schema，不改 `store.voice_cast.resolve_cast_voice` 的优先级。
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

from ..models import entities
from ..store.voice_cast import cast_config
from ..store.voice_store import pick_variant
from .voice_casting import _score, age_band

#: 年龄段 → 音色池槽位后缀（与页面默认 6 个槽位一致）
_BAND_SLOT = {"child": "young", "youth": "young", "adult": "mid", "middle": "mid", "senior": "elder"}
#: 槽位后缀 → 中文年龄段
_SLOT_LABEL = {"young": "青年", "mid": "中年", "elder": "老年"}
#: 性别 → 中文
_GENDER_LABEL = {"male": "男", "female": "女"}
#: 动作中文名（页面展示用）
ACTION_LABELS = {
    "create": "新建",
    "fill": "补齐",
    "keep": "保持不动",
    "manual": "需要人工指定",
}


def _now() -> str:
    """当前 UTC 时间（ISO 8601，秒级）。"""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _usable(voice: dict) -> bool:
    """音色是否可用于绑定（状态正常且至少一个可用变体）。"""
    if voice.get("status") in ("unusable", "archived"):
        return False
    return pick_variant(voice, None, False) is not None


def _snapshot_path(store) -> Path:
    """上次自动补齐前的 cast 快照路径（只保留最近一次）。"""
    return store.voice_asset_dir("cast_autofill") / "last.json"


def _usage(characters: list[dict]) -> dict[str, int]:
    """统计非龙套角色已占用的音色次数（与自动编排的打分口径一致）。"""
    usage: dict[str, int] = {}
    for character in characters:
        if character.get("role") == "minor":
            continue
        voice_id = (character.get("voice_binding") or {}).get("voice_id")
        if voice_id:
            usage[str(voice_id)] = usage.get(str(voice_id), 0) + 1
    return usage


def _voice_name(voices: dict, voice_id: str | None) -> str:
    """音色 ID → 名称；缺失时返回空串。"""
    return str((voices.get(str(voice_id)) or {}).get("name") or "") if voice_id else ""


def _rank_for_group(voices: list[dict], characters: list[dict], usage: dict[str, int]) -> tuple[dict | None, str]:
    """为一个龙套分组挑音色：组内角色得分求和，取最高分。

    参数:
        voices: 可用音色列表。
        characters: 同 性别:年龄段 的龙套。
        usage: 非龙套角色的音色占用次数。

    返回:
        `(voice, reason)`；没有可用音色时 voice 为 None，reason 说明原因。
    """
    best = None
    for voice in voices:
        total = 0
        reasons: list[str] = []
        for character in characters:
            pair = _score(character, voice, usage)
            if pair is None:
                total = None
                break
            total += pair[0]
            if pair[0] > 0:
                reasons = pair[1]
        if total is None:
            continue
        if best is None or (total, str(voice.get("id"))) > (best[0], str(best[1].get("id"))):
            best = (total, voice, reasons)
    if best is None:
        return None, "音色库里没有性别匹配的可用音色"
    voice, reasons = best[1], best[2]
    detail = "、".join([item for item in reasons if "降权" not in item][:3]) or "按性别与标签推荐"
    return voice, detail


def _plan_pool(characters: list[dict], voices: list[dict], cast: dict) -> tuple[list[dict], list[dict], dict]:
    """规划音色池与龙套映射。

    返回:
        `(items, manual, planned)`；planned 为应用后的 `voice_pool` / `stock_defaults`。
    """
    pool = [dict(item) for item in cast.get("voice_pool") or []]
    by_slot = {item.get("slot"): item for item in pool}
    stock_defaults = dict(cast.get("stock_defaults") or {})
    usage = _usage(characters)
    items: list[dict] = []
    manual: list[dict] = []

    groups: dict[str, dict] = {}
    for character in characters:
        if character.get("role") != "minor":
            continue
        gender = character.get("gender")
        band = age_band(character.get("age_range")) or "adult"
        name = character.get("name") or character.get("id")
        if gender not in ("male", "female"):
            manual.append(
                {
                    "section": "pool",
                    "key": str(character.get("id")),
                    "message": f"龙套「{name}」性别未定，无法按性别归池",
                }
            )
            continue
        suffix = _BAND_SLOT.get(band, "mid")
        slot = f"{gender}_{suffix}"
        group = groups.setdefault(slot, {"gender": gender, "suffix": suffix, "characters": []})
        group["characters"].append(character)

    # 每个分组：池槽位 + 推荐音色
    group_voice: dict[str, str] = {}
    for slot, group in groups.items():
        suffix = group["suffix"]
        names = "、".join(str(item.get("name") or item.get("id")) for item in group["characters"][:3])
        label = f"{_GENDER_LABEL[group['gender']]}{_SLOT_LABEL.get(suffix, '中年')}声"
        voice, reason = _rank_for_group(voices, group["characters"], usage)
        existing = by_slot.get(slot)
        basis = f"龙套 {names}（{group['gender']}:{suffix}）共用；{reason}"
        if existing is None:
            if voice is None:
                manual.append({"section": "pool", "key": slot, "message": f"{label}：{reason}，需要人工指定"})
                items.append(
                    {
                        "section": "pool",
                        "key": slot,
                        "action": "manual",
                        "label": label,
                        "current": None,
                        "proposed": None,
                        "proposed_name": "",
                        "reason": basis,
                    }
                )
                continue
            pool.append(
                {
                    "slot": slot,
                    "label": label,
                    "gender": group["gender"],
                    "age_feel": _SLOT_LABEL.get(suffix),
                    "voice_id": voice.get("id"),
                }
            )
            group_voice[slot] = str(voice.get("id"))
            items.append(
                {
                    "section": "pool",
                    "key": slot,
                    "action": "create",
                    "label": label,
                    "current": None,
                    "proposed": voice.get("id"),
                    "proposed_name": voice.get("name"),
                    "reason": basis,
                }
            )
            continue
        current = str(existing.get("voice_id") or "")
        if not current:
            if voice is None:
                manual.append({"section": "pool", "key": slot, "message": f"{label}：{reason}，需要人工指定"})
                items.append(
                    {
                        "section": "pool",
                        "key": slot,
                        "action": "manual",
                        "label": label,
                        "current": None,
                        "proposed": None,
                        "proposed_name": "",
                        "reason": basis,
                    }
                )
                continue
            existing["voice_id"] = voice.get("id")
            group_voice[slot] = str(voice.get("id"))
            items.append(
                {
                    "section": "pool",
                    "key": slot,
                    "action": "fill",
                    "label": existing.get("label") or label,
                    "current": None,
                    "proposed": voice.get("id"),
                    "proposed_name": voice.get("name"),
                    "reason": basis,
                }
            )
            continue
        group_voice[slot] = current
        same = voice is not None and str(voice.get("id")) == current
        items.append(
            {
                "section": "pool",
                "key": slot,
                "action": "keep",
                "label": existing.get("label") or label,
                "current": current,
                "proposed": current,
                "proposed_name": _voice_name({str(item.get("id")): item for item in voices}, current),
                "reason": "已与推荐一致，保持不动" if same else "已有手动指定，保持不动",
            }
        )

    # 龙套映射：职业槽位 / 音色池槽位 → 池音色
    stock_groups: dict[str, dict] = {}
    for character in characters:
        if character.get("role") != "minor":
            continue
        stock = character.get("stock") or {}
        key = stock.get("slot") or stock.get("voice_slot")
        if not key:
            continue
        stock_groups.setdefault(key, {"characters": []})["characters"].append(character)
    for key, group in stock_groups.items():
        names = "、".join(str(item.get("name") or item.get("id")) for item in group["characters"][:3])
        current = str(stock_defaults.get(key) or "")
        if current:
            items.append(
                {
                    "section": "stock",
                    "key": key,
                    "action": "keep",
                    "label": f"龙套映射 {key}",
                    "current": current,
                    "proposed": current,
                    "proposed_name": _voice_name({str(item.get("id")): item for item in voices}, current),
                    "reason": "已有映射，保持不动",
                }
            )
            continue
        # 同一职业槽位的角色可能跨分组：取第一个能拿到池音色的分组
        proposed = None
        for character in group["characters"]:
            gender = character.get("gender")
            band = age_band(character.get("age_range")) or "adult"
            slot = f"{gender}_{_BAND_SLOT.get(band, 'mid')}" if gender in ("male", "female") else ""
            if slot and group_voice.get(slot):
                proposed = group_voice[slot]
                break
        if not proposed:
            manual.append({"section": "stock", "key": key, "message": f"龙套映射 {key}：没有可复用的池音色，需要人工指定"})
            items.append(
                {
                    "section": "stock",
                    "key": key,
                    "action": "manual",
                    "label": f"龙套映射 {key}",
                    "current": None,
                    "proposed": None,
                    "proposed_name": "",
                    "reason": f"龙套 {names} 使用该槽位；{_reason_no_pool(group['characters'])}",
                }
            )
            continue
        stock_defaults[key] = proposed
        items.append(
            {
                "section": "stock",
                "key": key,
                "action": "fill",
                "label": f"龙套映射 {key}",
                "current": None,
                "proposed": proposed,
                "proposed_name": _voice_name({str(item.get("id")): item for item in voices}, proposed),
                "reason": f"龙套 {names} 使用该槽位，复用同组池音色",
            }
        )

    return items, manual, {"voice_pool": pool, "stock_defaults": stock_defaults}


def _reason_no_pool(characters: list[dict]) -> str:
    """说明取不到池音色的原因。"""
    for character in characters:
        if character.get("gender") not in ("male", "female"):
            return f"「{character.get('name') or character.get('id')}」性别未定"
    return "同组音色池未定音"


def _plan_narrator(characters: list[dict], voices: list[dict], cast: dict) -> tuple[list[dict], list[dict], dict]:
    """规划旁白人格与默认旁白。

    返回:
        `(items, manual, planned)`；planned 为应用后的 narrators/default_narrator_id。
    """
    narrators = [dict(item) for item in cast.get("narrators") or []]
    default_id = cast.get("default_narrator_id") or ""
    configured = [item for item in narrators if item.get("voice_id")]
    items: list[dict] = []
    manual: list[dict] = []
    voices_by_id = {str(item.get("id")): item for item in voices}

    if configured:
        items.append(
            {
                "section": "narrator",
                "key": "narrators",
                "action": "keep",
                "label": "旁白人格",
                "current": configured[0].get("voice_id"),
                "proposed": configured[0].get("voice_id"),
                "proposed_name": _voice_name(voices_by_id, configured[0].get("voice_id")),
                "reason": f"已有 {len(configured)} 个可用旁白人格，保持不动",
            }
        )
        if not default_id:
            items.append(
                {
                    "section": "narrator",
                    "key": "default_narrator_id",
                    "action": "fill",
                    "label": "默认旁白",
                    "current": None,
                    "proposed": configured[0].get("id"),
                    "proposed_name": configured[0].get("label") or configured[0].get("id"),
                    "reason": "旁白人格已存在但未设默认，补到第一个可用人格",
                }
            )
        return items, manual, {"narrators": narrators, "default_narrator_id": default_id or configured[0].get("id")}

    narrator = next((item for item in characters if item.get("role") == "narrator"), None)
    protagonist = next((item for item in characters if item.get("role") == "protagonist"), None)
    source = narrator if (narrator and _has_profile(narrator)) else (protagonist or narrator)
    if source is None:
        manual.append({"section": "narrator", "key": "narrators", "message": "没有旁白或主角档案，旁白人格需要人工指定"})
        items.append(
            {
                "section": "narrator",
                "key": "narrators",
                "action": "manual",
                "label": "旁白人格",
                "current": None,
                "proposed": None,
                "proposed_name": "",
                "reason": "没有旁白或主角档案，无法推断旁白音色",
            }
        )
        return items, manual, {"narrators": narrators, "default_narrator_id": default_id or None}

    binding = str(((source.get("voice_binding") or {}).get("voice_id")) or "")
    voice = voices_by_id.get(binding) if binding else None
    reason = f"沿用{_role_label(source)}当前绑定"
    if voice is None or not _usable(voice):
        voice, detail = _rank_for_group(voices, [source], _usage(characters))
        reason = f"按{_role_label(source)}档案推荐：{detail}"
    if voice is None:
        manual.append({"section": "narrator", "key": "narrators", "message": "旁白没有可用音色，需要人工指定"})
        items.append(
            {
                "section": "narrator",
                "key": "narrators",
                "action": "manual",
                "label": "旁白人格",
                "current": None,
                "proposed": None,
                "proposed_name": "",
                "reason": "音色库里没有可用音色",
            }
        )
        return items, manual, {"narrators": narrators, "default_narrator_id": default_id or None}

    narrators.append({"id": entities.new_id("npr"), "label": "默认旁白（自动）", "voice_id": voice.get("id")})
    items.append(
        {
            "section": "narrator",
            "key": "narrators",
            "action": "create",
            "label": "旁白人格",
            "current": None,
            "proposed": voice.get("id"),
            "proposed_name": voice.get("name"),
            "reason": reason,
        }
    )
    if not default_id:
        items.append(
            {
                "section": "narrator",
                "key": "default_narrator_id",
                "action": "fill",
                "label": "默认旁白",
                "current": None,
                "proposed": narrators[-1]["id"],
                "proposed_name": "默认旁白（自动）",
                "reason": "新人格设为默认旁白",
            }
        )
    return items, manual, {"narrators": narrators, "default_narrator_id": default_id or narrators[-1]["id"]}


def _has_profile(character: dict) -> bool:
    """角色是否有可用于推荐音色的档案字段。"""
    return bool(
        character.get("voice_profile")
        or character.get("voice_description_zh")
        or character.get("voice_description_en")
        or character.get("gender")
        or character.get("age_range")
    )


def _role_label(character: dict) -> str:
    """角色定位中文名（用于理由文案）。"""
    return {"narrator": "旁白", "protagonist": "主角", "supporting": "配角", "minor": "龙套"}.get(
        str(character.get("role") or ""), "角色"
    )


def _plan_switches(cast: dict) -> tuple[list[dict], list[dict], list]:
    """规划旁白切换点：不从档案臆测，已配置则保持。"""
    switches = copy.deepcopy(cast.get("narrator_switches") or [])
    if switches:
        return (
            [
                {
                    "section": "switch",
                    "key": "narrator_switches",
                    "action": "keep",
                    "label": "旁白切换点",
                    "current": len(switches),
                    "proposed": len(switches),
                    "proposed_name": f"{len(switches)} 个",
                    "reason": "已有切换点配置，保持不动",
                }
            ],
            [],
            switches,
        )
    message = "旁白切换点需要人工指定：无法从角色档案推断从第几章开始切换"
    return (
        [
            {
                "section": "switch",
                "key": "narrator_switches",
                "action": "manual",
                "label": "旁白切换点",
                "current": None,
                "proposed": None,
                "proposed_name": "",
                "reason": message,
            }
        ],
        [{"section": "switch", "key": "narrator_switches", "message": message}],
        switches,
    )


def build_plan(store) -> dict:
    """构造「按角色自动补齐」的预览计划（不落盘）。

    参数:
        store: ProjectStore。

    返回:
        `{items, manual, cast, stats, sections}`；items 每项含 action / reason。
    """
    characters = store.load_characters().get("characters") or []
    voices = [item for item in store.load_voices().get("voices") or [] if _usable(item)]
    cast = cast_config(store)

    pool_items, pool_manual, pool_plan = _plan_pool(characters, voices, cast)
    narrator_items, narrator_manual, narrator_plan = _plan_narrator(characters, voices, cast)
    switch_items, switch_manual, switches = _plan_switches(cast)

    planned = copy.deepcopy(cast)
    planned["voice_pool"] = pool_plan["voice_pool"]
    planned["stock_defaults"] = pool_plan["stock_defaults"]
    planned["narrators"] = narrator_plan["narrators"]
    if narrator_plan.get("default_narrator_id"):
        planned["default_narrator_id"] = narrator_plan["default_narrator_id"]
    planned["narrator_switches"] = switches

    items = pool_items + narrator_items + switch_items
    manual = pool_manual + narrator_manual + switch_manual
    stats = {key: 0 for key in ACTION_LABELS}
    for item in items:
        stats[item["action"]] = stats.get(item["action"], 0) + 1
    stats["changed"] = stats["create"] + stats["fill"]
    return {"items": items, "manual": manual, "cast": planned, "stats": stats}


def undo_state(store) -> dict:
    """自动补齐的撤销可用性。"""
    path = _snapshot_path(store)
    if not path.exists():
        return {"available": False}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"available": False}
    return {"available": True, "created_at": payload.get("created_at"), "changed": payload.get("changed")}


def apply(store) -> dict:
    """应用自动补齐：先快照旧 cast，再写回；可撤回本次。

    参数:
        store: ProjectStore。

    返回:
        `{applied, changed, plan, undo}`。
    """
    plan = build_plan(store)
    if not plan["stats"]["changed"]:
        return {"applied": False, "changed": 0, "plan": plan, "undo": undo_state(store)}

    previous = cast_config(store)
    path = _snapshot_path(store)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"created_at": _now(), "changed": plan["stats"]["changed"], "cast": copy.deepcopy(previous)},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    project = store.load_project()
    settings = project.setdefault("settings", {})
    tts = settings.setdefault("tts", {})
    tts["cast"] = plan["cast"]
    store.save_project(project)
    return {"applied": True, "changed": plan["stats"]["changed"], "plan": build_plan(store), "undo": undo_state(store)}


def undo_last(store) -> dict:
    """撤回最近一次自动补齐，恢复到补齐前的 cast。

    参数:
        store: ProjectStore。

    返回:
        `{restored, undo}`。

    异常:
        ValueError: 没有可撤回的记录。
    """
    path = _snapshot_path(store)
    if not path.exists():
        raise ValueError("没有可撤回的自动补齐记录")
    payload = json.loads(path.read_text(encoding="utf-8"))
    project = store.load_project()
    settings = project.setdefault("settings", {})
    tts = settings.setdefault("tts", {})
    tts["cast"] = payload.get("cast") or {}
    store.save_project(project)
    path.unlink(missing_ok=True)
    return {"restored": True, "undo": undo_state(store)}
