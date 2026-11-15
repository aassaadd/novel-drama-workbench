"""大纲阶段的角色设定：每章节拍生成后立即对齐角色卡。

目的：让「这一章该有谁」在正文之前就定下来，避免人物漏建卡。
做法：
1. 把本章标题、摘要、逐条节拍与现有角色卡交给模型；
2. 已有同名/同别名角色一律复用（只补别名），没有的才新建角色卡；
3. 返回的每个节拍都绑定到具体角色 ID；节拍为空时用本章视角人物兜底；
4. 新建卡立即写入 characters.json，后续章节与正文生成都能直接用上。

正文生成后的状态回流与一致性检查只是补充，不在这里做。
"""

from __future__ import annotations

import json
import re

from ..adapters.base import AdapterError, ErrorCode, Message, TextGenRequest
from ..adapters.offline_writing import OfflineWritingAdapter
from ..config import AppConfig
from ..models import entities
from ..store import ProjectStore
from .service import (
    _clean_speech_style,
    _generate_json,
    _tool_max_tokens,
)

#: 允许的角色定位（与 character.schema.json 对齐）
ROLES = ("protagonist", "antagonist", "supporting", "minor")
#: 允许的性别取值
GENDERS = ("male", "female", "other", "unknown")

#: 角色设定工具：模型只输出本章 cast 与节拍归属
CAST_SYNC_TOOL = {
    "type": "function",
    "function": {
        "name": "emit_chapter_cast",
        "description": "输出本章出场角色与每个节拍的角色归属",
        "parameters": {
            "type": "object",
            "required": ["cast"],
            "properties": {
                "cast": {
                    "type": "array",
                    "description": "本章全部出场角色；已有角色必须带 existing_id 复用，不得重复建卡",
                    "items": {
                        "type": "object",
                        "required": ["name"],
                        "properties": {
                            "existing_id": {
                                "type": "string",
                                "description": "复用现有角色时填其 ID；新建角色留空",
                            },
                            "name": {"type": "string", "description": "角色姓名"},
                            "aliases": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "本章出现的称呼/别名",
                            },
                            "role": {"type": "string", "enum": list(ROLES)},
                            "gender": {"type": "string", "enum": list(GENDERS)},
                            "age_range": {"type": "string"},
                            "personality": {"type": "array", "items": {"type": "string"}},
                            "background": {"type": "string", "description": "新建角色必须给，已有角色可留空"},
                            "speech_style": {
                                "type": "object",
                                "properties": {
                                    "tempo": {"type": "string"},
                                    "pitch": {"type": "string"},
                                    "accent": {"type": "string"},
                                    "catchphrases": {"type": "array", "items": {"type": "string"}},
                                },
                            },
                            "beats": {
                                "type": "array",
                                "items": {"type": "integer"},
                                "description": "该角色出场的节拍序号（从 1 开始）",
                            },
                        },
                    },
                }
            },
        },
    },
}


def _clean_text(value, limit: int) -> str:
    """把任意值转成去空白字符串并截断。"""
    return str(value or "").strip()[:limit]


def _norm(name: str) -> str:
    """姓名归一化：去空白，英文统一小写，用于匹配复用。"""
    return re.sub(r"\s+", "", str(name or "")).casefold()


def _clean_list(value, limit: int, max_items: int = 6) -> list[str]:
    """清洗字符串列表：去空、去重、截断。"""
    rows: list[str] = []
    for item in value or []:
        text = _clean_text(item, limit)
        if text and text not in rows:
            rows.append(text)
        if len(rows) >= max_items:
            break
    return rows


def _cast_brief(characters: list[dict]) -> list[dict]:
    """给模型看的现有角色摘要（只保留复用判断需要的字段）。"""
    return [
        {
            "id": item.get("id"),
            "name": item.get("name"),
            "aliases": item.get("aliases") or [],
            "role": item.get("role"),
            "personality": (item.get("personality") or [])[:3],
            "background": _clean_text(item.get("background"), 120),
        }
        for item in characters
        if item.get("id") and item.get("role") != "narrator"
    ]


def _chapter_text(chapter: dict) -> str:
    """拼出本章标题、摘要与节拍，供匹配姓名使用。"""
    beats = [str(beat.get("summary") or "") for beat in chapter.get("beats") or []]
    return "\n".join([str(chapter.get("title") or ""), str(chapter.get("summary") or ""), *beats])


def _match_existing(row: dict, by_id: dict, by_name: dict) -> dict | None:
    """按 existing_id / 姓名 / 别名匹配现有角色。"""
    existing_id = _clean_text(row.get("existing_id"), 40)
    if existing_id and existing_id in by_id:
        return by_id[existing_id]
    keys = [row.get("name"), *(row.get("aliases") or [])]
    for key in keys:
        hit = by_name.get(_norm(key))
        if hit is not None:
            return hit
    return None


def _new_character(row: dict) -> dict:
    """按模型返回的字段创建角色卡（只写写作与配音都用得上的字段）。"""
    name = _clean_text(row.get("name"), 40)
    role = _clean_text(row.get("role"), 20)
    if role not in ROLES:
        role = "supporting"
    gender = _clean_text(row.get("gender"), 10)
    character = entities.new_character(name, role=role)
    character["aliases"] = _clean_list(row.get("aliases"), 40, 6)
    character["gender"] = gender if gender in GENDERS else "unknown"
    character["age_range"] = _clean_text(row.get("age_range"), 20) or None
    character["personality"] = _clean_list(row.get("personality"), 40, 6)
    character["background"] = _clean_text(row.get("background"), 1000)
    speech = _clean_speech_style(row.get("speech_style"))
    if speech:
        character["speech_style"] = speech
    return character


def _match_name_from_text(text: str, known: list[str]) -> list[str]:
    """离线规则：从文本里找出已知姓名，用于测试替身。"""
    return [name for name in known if name and name in text]


#: 离线规则里不应当作人名的常见词
_OFFLINE_STOP_NAMES = {"他们", "她们", "我们", "你们", "自己", "众人", "有人", "两人", "三人", "后者", "前者"}


def _offline_names_in(text: str) -> list[str]:
    """离线规则：从节拍摘要里提取“某某在/说/把”这类人名。"""
    rows: list[str] = []
    for match in re.finditer(r"(?:^|[，。；：、\s“”])([\u4e00-\u9fa5]{2,3})(?=在|说|把|从|站|走|推|看)", text):
        name = match.group(1)
        if name in _OFFLINE_STOP_NAMES or name in rows:
            continue
        rows.append(name)
    return rows


def _payload_for_offline(chapter: dict, characters: list[dict]) -> str:
    """离线适配器使用：按文本里出现过的姓名返回 cast。

    仅在显式配置 offline 时使用；真实模型走工具调用。
    """
    beats = chapter.get("beats") or []
    known = [str(item.get("name") or "") for item in characters if item.get("name")] or ["主人公"]
    texts = [str(beat.get("summary") or "") for beat in beats]
    cast: dict[str, dict] = {}
    used_any = False
    for position, text in enumerate(texts, start=1):
        hits = list(dict.fromkeys([*_match_name_from_text(text, known), *_offline_names_in(text)]))
        if not hits:
            continue
        used_any = True
        for name in hits:
            row = cast.setdefault(name, {"name": name, "beats": []})
            if position not in row["beats"]:
                row["beats"].append(position)
    if not used_any:
        # 节拍里没有点名任何角色：用第一个角色兜住全部节拍，避免空绑定
        row = cast.setdefault(known[0], {"name": known[0], "beats": []})
        row["beats"] = list(range(1, len(texts) + 1))
    existing_by_name = {_norm(item.get("name")): item for item in characters if item.get("name")}
    rows = []
    for name, item in cast.items():
        hit = existing_by_name.get(_norm(name))
        rows.append(
            {
                "existing_id": hit.get("id") if hit else "",
                "name": name,
                "aliases": [],
                "role": "protagonist" if not hit and len(rows) == 0 else "supporting",
                "beats": item["beats"],
            }
        )
    return json.dumps({"cast": rows}, ensure_ascii=False)


def sync_chapter_cast(
    store: ProjectStore,
    config: AppConfig,
    chapter: dict,
    adapter=None,
    persist: bool = True,
    chapter_index: int | None = None,
) -> dict:
    """把本章出场角色与现有角色卡对齐，并绑定到每个节拍。

    参数:
        store: 工程。
        config: 本机配置。
        chapter: 大纲章节（需含 title/summary/beats；会被原地写入 character_ids）。
        adapter: 写作适配器；None 时按配置装配。
        persist: False 时只预览，不写 characters.json（新建角色只留在内存）。
        chapter_index: 章序号，用于提示与新建卡备注；None 时读 chapter.index。

    返回:
        `{created, reused, cast, new_characters, beat_ids}`；created/reused 都是姓名列表。

    异常:
        AdapterError: 模型没有返回任何角色，或角色设定结果无法解析。
        ValueError: 本章没有任何角色可绑定。
    """
    if adapter is None:
        from .service import build_writing_adapter

        adapter = build_writing_adapter(config)
    index = chapter_index if chapter_index is not None else chapter.get("index")
    table = store.load_characters()
    characters = [item for item in table["characters"] if item.get("role") != "narrator"]
    by_id = {str(item.get("id")): item for item in characters if item.get("id")}
    by_name: dict[str, dict] = {}
    for item in characters:
        for key in [item.get("name"), *(item.get("aliases") or [])]:
            norm = _norm(key)
            if norm:
                by_name.setdefault(norm, item)
    beats = chapter.get("beats") or []
    if not beats:
        raise ValueError(f"第 {index} 章没有节拍：角色设定必须在节拍生成之后执行")

    existing_brief = _cast_brief(characters)
    beat_lines = [
        f"{position}. {str(beat.get('summary') or '').strip()}"
        for position, beat in enumerate(beats, start=1)
    ]
    prompt = (
        f"第 {index} 章《{chapter.get('title') or '未命名'}》\n"
        f"本章摘要：{chapter.get('summary') or '（无）'}\n"
        f"本章节拍：\n" + "\n".join(beat_lines) + "\n\n"
        f"现有角色卡：{json.dumps(existing_brief, ensure_ascii=False)}\n\n"
        "任务：把本章真正出场的人物整理成角色表，并标出每个节拍谁出场。\n"
        "规则：\n"
        "1) 现有角色卡里已有同一个人（同名或同别名）必须填 existing_id 复用，只补别名，不得重复建卡；\n"
        "2) 现有卡里没有、但本章节拍明确出现的人物，新建角色卡，给出 role/gender/age_range/personality/background/speech_style；\n"
        "3) 每个节拍至少要有一个角色，不能留空；不要把只在别章出现的人塞进本章；\n"
        "4) 不要给同一角色编造多个名字；别名只填本章真实出现的称呼。\n"
        "角色设定粒度：\n"
        "1) 新建角色必须给 role（protagonist/antagonist/supporting/minor）、gender、age_range、"
        "personality（2-4 个词）、background（一句话）与 speech_style"
        "（tempo 语速、pitch 音高、accent 口音、catchphrases 1-3 句口头禅）；不同角色的语速、用词和口头禅要能区分；\n"
        "2) 复用现有角色时只补本章新出现的别名与节拍归属，不要改已有设定。"
    )
    req = TextGenRequest(
        task="character_sync",
        system="你是小说角色统筹，只按工具参数输出本章角色表与节拍归属，不要解释。",
        messages=[Message("user", prompt)],
        stream=False,
        max_tokens=_tool_max_tokens(config, "character_sync", max(1, len(beats))),
        metadata={
            "chapter": {
                "title": chapter.get("title"),
                "summary": chapter.get("summary"),
                "beats": [{"summary": beat.get("summary")} for beat in beats],
            },
            "characters": [
                {"id": item.get("id"), "name": item.get("name"), "aliases": item.get("aliases") or []}
                for item in characters
            ],
        },
    )
    if isinstance(adapter, OfflineWritingAdapter):
        # 离线适配器不支持 character_sync 任务，直接用确定性规则给出本章 cast
        payload = {"cast": json.loads(_payload_for_offline(chapter, characters))["cast"]}
    else:
        payload = _generate_json(adapter, req, "character_sync", tool=CAST_SYNC_TOOL)
    rows = payload.get("cast") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows:
        raise AdapterError(
            ErrorCode.UPSTREAM_ERROR,
            f"第 {index} 章的角色设定阶段没有返回任何角色；请重试本章大纲",
            retryable=True,
            detail={"stage": "cast", "chapter_index": index},
        )

    created: list[str] = []
    reused: list[str] = []
    cast: list[dict] = []
    seen_ids: set[str] = set()
    owned_new: list[dict] = []

    def beat_numbers(row: dict) -> list[int]:
        """把模型给的节拍序号清洗成 1..N 的整数。"""
        numbers: list[int] = []
        for raw in row.get("beats") or []:
            try:
                value = int(raw)
            except (TypeError, ValueError):
                continue
            if 1 <= value <= len(beats) and value not in numbers:
                numbers.append(value)
        return numbers

    for row in rows:
        if not isinstance(row, dict):
            continue
        name = _clean_text(row.get("name"), 40)
        if not name:
            continue
        existing = _match_existing(row, by_id, by_name)
        if existing is not None:
            character = existing
            merged = list(character.get("aliases") or [])
            for alias in _clean_list(row.get("aliases"), 40, 8):
                if _norm(alias) not in {_norm(item) for item in [character.get("name"), *merged]} and alias not in merged:
                    merged.append(alias)
            if merged:
                character["aliases"] = merged
            if character.get("name") not in reused:
                reused.append(character.get("name"))
        else:
            character = _new_character(row)
            owned_new.append(character)
            created.append(character["name"])
            by_id[character["id"]] = character
            for key in [character.get("name"), *(character.get("aliases") or [])]:
                norm = _norm(key)
                if norm:
                    by_name.setdefault(norm, character)
        character_id = str(character.get("id"))
        if not character_id or character_id in seen_ids:
            continue
        seen_ids.add(character_id)
        cast.append(
            {
                "id": character_id,
                "name": character.get("name"),
                "role": character.get("role"),
                "beats": beat_numbers(row),
            }
        )

    if not cast:
        raise AdapterError(
            ErrorCode.UPSTREAM_ERROR,
            f"第 {index} 章的角色设定结果无法解析出任何角色；请重试本章大纲",
            retryable=True,
            detail={"stage": "cast", "chapter_index": index},
        )

    # 先把节拍里已有的引用过滤到本章角色表，再叠加本次角色归属
    valid_ids = {item["id"] for item in cast}
    for beat in beats:
        beat["character_ids"] = [
            str(cid) for cid in (beat.get("character_ids") or []) if str(cid) in valid_ids
        ]
    for item in cast:
        for position in item["beats"] or range(1, len(beats) + 1):
            ids = beats[position - 1].setdefault("character_ids", [])
            if item["id"] not in ids:
                ids.append(item["id"])

    # 视角人物优先兜底；否则用主角，再否则用第一个角色
    lead_id = None
    pov_id = chapter.get("pov_character_id")
    if pov_id in valid_ids:
        lead_id = pov_id
    if lead_id is None:
        lead_id = next(
            (item["id"] for item in cast if item.get("role") == "protagonist"),
            cast[0]["id"],
        )
    for beat in beats:
        if not beat.get("character_ids"):
            beat["character_ids"] = [lead_id]

    if persist:
        if owned_new:
            table["characters"].extend(owned_new)
        store.save_characters(table)
    return {
        "created": created,
        "reused": reused,
        "cast": cast,
        "new_characters": owned_new,
        "beat_ids": [list(beat.get("character_ids") or []) for beat in beats],
    }
