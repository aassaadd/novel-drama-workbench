"""从章节正文抽取角色：确定性扫描、模型补充、预览确认与建卡。

职责边界：
- 抽取阶段只产出「已关联」与「待新建」两类结果，**不写角色库**；
- 作者确认后才建最小角色卡并把角色关联到本章；
- 结果落到 `.ndw/prose_cast/<chapterId>.json`，带正文 `content_hash`，正文未改时复用缓存。

与 `capture_cast.py` 的差异：那边只读大纲节拍，这里只读章节正文 `content`。
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from ...adapters.llm.contracts import Message, TextGenRequest
from ...adapters.persistence import ProjectStore
from ...adapters.persistence.settings_store import SettingsStore
from ...domain.cast.rules.naming import add_aliases, is_descriptor, normalize_name
from ...models import entities
from ...models.enums import AgeRange
from ...models.ids import now_iso
from ..support.llm_calls import build_writing_adapter
from .character_fields import _clean_speech_style
from .sync_cast import GENDERS, ROLES, _index_characters, _record_created

#: 抽取记录的版本号
PROSE_CAST_VERSION = "prose_cast/1"

#: 姓名归一化：与写作侧保持同一口径
_norm = normalize_name

#: 允许的年龄段落盘值
AGE_RANGES = AgeRange.values()

#: 证据句截断长度
EVIDENCE_LIMIT = 60


def _clean_text(value, limit: int) -> str:
    """把任意值转成去空白字符串并截断。

    参数:
        value: 任意值。
        limit: 最大长度。

    返回:
        去空白后的字符串。
    """
    return str(value or "").strip()[:limit]


def _clean_list(value, limit: int, max_items: int = 6) -> list[str]:
    """清洗字符串列表：去空、去重、截断。

    参数:
        value: 原始列表。
        limit: 单项最大长度。
        max_items: 最多保留多少项。

    返回:
        清洗后的列表。
    """
    rows: list[str] = []
    for item in value or []:
        text = _clean_text(item, limit)
        if text and text not in rows:
            rows.append(text)
        if len(rows) >= max_items:
            break
    return rows


def _parse_json(text: str) -> dict:
    """从模型输出里解析 JSON（容忍 ```json 代码块与前后说明）。

    参数:
        text: 模型原始输出。

    返回:
        解析后的 dict；失败时返回空 dict。
    """
    raw = str(text or "").strip()
    match = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw)
    if match:
        raw = match.group(1).strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        value = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _content_hash(content: str) -> str:
    """计算正文的稳定哈希。

    参数:
        content: 章节正文。

    返回:
        `sha256:<64 位十六进制>`。
    """
    return "sha256:" + hashlib.sha256(str(content or "").encode("utf-8")).hexdigest()


def _chapter_content(store: ProjectStore, chapter_id: str) -> str:
    """读取章节正文；章节文件不存在时返回空串。

    参数:
        store: 工程。
        chapter_id: 章节 ID。

    返回:
        去首尾空白的正文。
    """
    try:
        chapter = store.load_chapter(chapter_id)
    except FileNotFoundError:
        return ""
    return str(chapter.get("content") or "").strip()


def _record_path(store: ProjectStore, chapter_id: str) -> Path:
    """返回抽取记录文件路径。

    参数:
        store: 工程。
        chapter_id: 章节 ID。

    返回:
        `.ndw/prose_cast/<chapterId>.json` 路径。
    """
    return store.root / ".ndw" / "prose_cast" / f"{chapter_id}.json"


def _write_record(store: ProjectStore, record: dict) -> None:
    """原子写入抽取记录。

    参数:
        store: 工程。
        record: 抽取记录。

    返回:
        无。
    """
    path = _record_path(store, str(record.get("chapter_id") or ""))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _empty_record(chapter_id: str) -> dict:
    """构造空抽取记录骨架。

    参数:
        chapter_id: 章节 ID。

    返回:
        空骨架 dict（含 exists/stale 两个派生标记）。
    """
    return {
        "version": PROSE_CAST_VERSION,
        "chapter_id": chapter_id,
        "content_hash": "",
        "created_at": None,
        "model": "",
        "characters": [],
        "linked_ids": [],
        "applied": False,
        "applied_at": None,
        "exists": False,
        "stale": True,
    }


def _sentence_at(content: str, position: int, limit: int = EVIDENCE_LIMIT) -> str:
    """截取命中位置所在的句子。

    参数:
        content: 正文。
        position: 命中位置。
        limit: 最大字数。

    返回:
        句子文本（去空白、截断）；位置非法时返回空串。
    """
    if position < 0 or position >= len(content):
        return ""
    start = max((content.rfind(mark, 0, position) for mark in "。！？\n"), default=-1) + 1
    ends = [content.find(mark, position) for mark in "。！？\n"]
    ends = [end for end in ends if end >= 0]
    end = min(ends) + 1 if ends else len(content)
    return content[start:end].strip()[:limit]


def _scan_terms(characters: list[dict]) -> list[dict]:
    """把角色姓名与别名整理成扫描词条（长名优先）。

    参数:
        characters: 非旁白角色列表。

    返回:
        `[{text, norm, character}]`，按文本长度倒序。
    """
    terms: list[dict] = []
    seen: set[str] = set()
    for item in characters:
        if item.get("role") == "narrator" or not item.get("id"):
            continue
        for raw in [item.get("name"), *(item.get("aliases") or [])]:
            text = str(raw or "").strip()
            norm = _norm(text)
            # 归一化后不足 2 个字符的中文名（如单字姓）不参与扫描，避免误命中
            if not text or len(norm) < 2 or is_descriptor(text) or norm in seen:
                continue
            seen.add(norm)
            terms.append({"text": text, "norm": norm, "character": item})
    terms.sort(key=lambda row: (-len(row["text"]), row["text"]))
    return terms


def scan_mentions(content: str, characters: list[dict]) -> list[dict]:
    """确定性地扫描正文里的姓名/别名提及（长名优先、非重叠计数）。

    参数:
        content: 章节正文。
        characters: 角色表（含旁白也可，内部过滤）。

    返回:
        `[{character_id, name, mentions, evidence}]`，按首次命中位置排序。
    """
    text = str(content or "")
    if not text:
        return []
    occupied = [False] * len(text)
    hits: dict[str, dict] = {}
    for term in _scan_terms(characters):
        needle = term["text"]
        start = 0
        while True:
            position = text.find(needle, start)
            if position < 0:
                break
            span = range(position, position + len(needle))
            if not any(occupied[index] for index in span):
                for index in span:
                    occupied[index] = True
                character_id = str(term["character"].get("id") or "")
                row = hits.setdefault(
                    character_id,
                    {
                        "character_id": character_id,
                        "name": term["character"].get("name"),
                        "mentions": 0,
                        "evidence": "",
                        "_first": position,
                    },
                )
                row["mentions"] += 1
                if not row["evidence"]:
                    row["evidence"] = _sentence_at(text, position)
            start = position + 1
    rows = sorted(hits.values(), key=lambda row: row["_first"])
    for row in rows:
        row.pop("_first", None)
    return rows


def _characters(store: ProjectStore) -> list[dict]:
    """读取非旁白角色列表。

    参数:
        store: 工程。

    返回:
        角色列表（不含旁白）。
    """
    return [
        item
        for item in store.load_characters().get("characters") or []
        if item.get("id") and item.get("role") != "narrator"
    ]


def _brief(characters: list[dict]) -> list[dict]:
    """构造给模型看的角色摘要。

    参数:
        characters: 非旁白角色列表。

    返回:
        `[{id, name, aliases, role, gender, age_range, personality, background, speech_style}]`。
    """
    return [
        {
            "id": item.get("id"),
            "name": item.get("name"),
            "aliases": list(item.get("aliases") or [])[:6],
            "role": item.get("role"),
            "gender": item.get("gender"),
            "age_range": item.get("age_range"),
            "personality": list(item.get("personality") or [])[:6],
            "background": _clean_text(item.get("background"), 200),
            "speech_style": item.get("speech_style") or {},
        }
        for item in characters
    ]


def _safe_speech(value) -> dict:
    """清洗说话方式；非法值按未填写处理。

    参数:
        value: 模型返回的 speech_style。

    返回:
        合法说话方式 dict；无法清洗时返回空 dict。
    """
    try:
        return _clean_speech_style(value) or {}
    except ValueError:
        return {}


def _draft(raw: dict) -> dict:
    """清洗待新建候选取的草稿字段。

    参数:
        raw: 模型返回的单行角色。

    返回:
        `{role, gender, age_range, personality, background, speech_style}`。
    """
    role = _clean_text(raw.get("role"), 20)
    gender = _clean_text(raw.get("gender"), 10)
    age_range = _clean_text(raw.get("age_range"), 20)
    return {
        "role": role if role in ROLES else "minor",
        "gender": gender if gender in GENDERS else "unknown",
        # 年龄段只接受合法分档，不做模糊归一，避免把模型猜的数字区间直接落盘
        "age_range": age_range if age_range in AGE_RANGES else None,
        "personality": _clean_list(raw.get("personality"), 40, 6),
        "background": _clean_text(raw.get("background"), 1000),
        "speech_style": _safe_speech(raw.get("speech_style")),
    }


def _as_int(value) -> int:
    """把模型返回的提及次数转成正整数。

    参数:
        value: 原始值。

    返回:
        正整数；无法解析或小于等于 0 时返回 0。
    """
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def _merge_rows(
    model_rows: list,
    characters: list[dict],
    scanned: list[dict],
) -> list[dict]:
    """合并确定性扫描与模型返回的角色行。

    参数:
        model_rows: 模型返回的 `characters` 数组。
        characters: 非旁白角色列表。
        scanned: `scan_mentions()` 的结果。

    返回:
        记录里的 `characters` 数组（linked 在前、new 在后）。
    """
    by_id, by_name = _index_characters(characters)
    linked: dict[str, dict] = {}
    new_rows: list[dict] = []
    new_by_norm: dict[str, dict] = {}
    scanned_ids: set[str] = set()

    for hit in scanned:
        character = by_id.get(hit["character_id"])
        if character is None:
            continue
        scanned_ids.add(str(character["id"]))
        linked[str(character["id"])] = {
            "character_id": character["id"],
            "name": character.get("name"),
            "aliases": list(character.get("aliases") or []),
            "mentions": int(hit.get("mentions") or 0),
            "evidence": str(hit.get("evidence") or ""),
            "status": "linked",
        }

    for raw in model_rows or []:
        if not isinstance(raw, dict):
            continue
        existing_id = _clean_text(raw.get("existing_id"), 40)
        aliases = _clean_list(raw.get("aliases"), 40, 6)
        name = _clean_text(raw.get("name"), 40)
        if not name and existing_id:
            name = str((by_id.get(existing_id) or {}).get("name") or "")
        if not name:
            continue
        mentions = _as_int(raw.get("mentions"))
        evidence = _clean_text(raw.get("evidence"), EVIDENCE_LIMIT)

        target = by_id.get(existing_id) if existing_id else None
        if target is None:
            for key in [name, *aliases]:
                target = by_name.get(_norm(key))
                if target is not None:
                    break
        if target is not None:
            character_id = str(target["id"])
            row = linked.get(character_id)
            if row is None:
                row = {
                    "character_id": character_id,
                    "name": target.get("name"),
                    "aliases": list(target.get("aliases") or []),
                    "mentions": mentions,
                    "evidence": evidence,
                    "status": "linked",
                }
                linked[character_id] = row
            # 确定性扫描值优先，模型值只用于补充扫描不到的情况
            if character_id not in scanned_ids:
                row["mentions"] = max(int(row.get("mentions") or 0), mentions)
            if not row.get("evidence") and evidence:
                row["evidence"] = evidence
            known = {_norm(item) for item in [row.get("name"), *(row.get("aliases") or [])] if _norm(item)}
            for alias in aliases:
                if _norm(alias) and _norm(alias) not in known:
                    row.setdefault("aliases", []).append(alias)
                    known.add(_norm(alias))
            continue

        if is_descriptor(name):
            continue
        key = _norm(name)
        existing_new = new_by_norm.get(key)
        if existing_new is not None:
            seen = {_norm(item) for item in [existing_new.get("name"), *(existing_new.get("aliases") or [])] if _norm(item)}
            for alias in aliases:
                if _norm(alias) and _norm(alias) not in seen:
                    existing_new.setdefault("aliases", []).append(alias)
                    seen.add(_norm(alias))
            existing_new["mentions"] = max(int(existing_new.get("mentions") or 0), mentions)
            if not existing_new.get("evidence") and evidence:
                existing_new["evidence"] = evidence
            continue
        row = {
            "character_id": None,
            "name": name,
            "aliases": aliases,
            "mentions": mentions or 1,
            "evidence": evidence,
            "status": "new",
            "draft": _draft(raw),
        }
        new_by_norm[key] = row
        new_rows.append(row)

    return [*linked.values(), *new_rows]


def _build_request(chapter: dict, chapter_id: str, characters: list[dict], scanned: list[dict]) -> TextGenRequest:
    """构造正文角色抽取请求。

    参数:
        chapter: 章节 dict。
        chapter_id: 章节 ID。
        characters: 非旁白角色列表。
        scanned: 确定性扫描命中。

    返回:
        `TextGenRequest`。
    """
    content = str(chapter.get("content") or "").strip()
    prompt = (
        f"下面是第 {int(chapter.get('index') or 0)} 章《{chapter.get('title') or ''}》的正文与现有角色表。\n"
        "请从正文里找出**有姓名的**行动或说话人物，并按下面规则输出 JSON，不要解释：\n"
        '{"characters":[{"existing_id":"chr_xxx（命中现有角色时必填）","name":"姓名",'
        '"aliases":["正文里出现的别名/简称"],"role":"protagonist|antagonist|supporting|minor",'
        '"gender":"male|female|other|unknown","age_range":"10-14|18-25|26-39|40-54|55-70",'
        '"personality":["性格词"],"background":"依据正文能确认的背景",'
        '"speech_style":{"tempo":"slow|medium|fast","pitch":"low|medium|high","accent":"口音或用词习惯","catchphrases":["口头禅"]},'
        '"mentions":3,"evidence":"正文原句"}]}\n'
        "约束：\n"
        "1) 姓名或别名能对上现有角色的，必须给 existing_id 并复用，不要另起新名；\n"
        "2) 角色表里没有的有名人物才作为新候选，姓名必须来自正文；\n"
        "3) 「他/她/女人/男人/对方/众人」这类代词与泛称不要输出；地名、组织名、物品名不要输出；\n"
        "4) evidence 必须是正文原句，不要改写；mention 次数按正文实际出现估算。\n\n"
        f"现有角色表：{json.dumps(_brief(characters), ensure_ascii=False)}\n"
        f"确定性扫描命中：{json.dumps(scanned, ensure_ascii=False)}\n\n"
        f"本章正文：\n{content}"
    )
    return TextGenRequest(
        task="cast_extract",
        system="你是长篇小说的角色统筹编辑，只负责从正文里识别有姓名的人物并复用现有角色卡，只输出 JSON。",
        messages=[Message("user", prompt)],
        metadata={
            "chapter_id": chapter_id,
            "chapter_index": int(chapter.get("index") or 0),
            "characters": _brief(characters),
            "content": content,
            "scanned": scanned,
            "thinking_stage": "characters",
        },
    )


def extract_prose_cast(store: ProjectStore, config, chapter_id: str, force: bool = False) -> dict:
    """从本章正文抽取角色并落盘（不建卡）。

    参数:
        store: 工程。
        config: 本机配置（用于装配写作适配器）。
        chapter_id: 章节 ID。
        force: 是否强制重抽；默认 False 表示正文未改时直接复用缓存，不调用模型。

    返回:
        抽取记录（`version=prose_cast/1`，已写入 `.ndw/prose_cast/<chapterId>.json`）；
        复用缓存时记录额外带 `source="cache"`。

    异常:
        ValueError: 章节不存在或本章正文为空。
    """
    if not force:
        cached = load_prose_cast(store, chapter_id)
        if cached.get("exists") and not cached.get("stale"):
            cached["source"] = "cache"
            return cached
    try:
        chapter = store.load_chapter(chapter_id)
    except FileNotFoundError as exc:
        raise ValueError(f"章节不存在：{chapter_id}") from exc
    content = str(chapter.get("content") or "").strip()
    if not content:
        raise ValueError("本章还没有正文：无法从正文抽取角色")
    characters = _characters(store)
    scanned = scan_mentions(content, characters)
    adapter = build_writing_adapter(config, SettingsStore(store.root).ensure(), action="cast.extract")
    result = adapter.generate(_build_request(chapter, chapter_id, characters, scanned))
    payload = _parse_json(getattr(result, "text", ""))
    rows = _merge_rows(payload.get("characters") or [], characters, scanned)
    record = {
        "version": PROSE_CAST_VERSION,
        "chapter_id": chapter_id,
        "content_hash": _content_hash(content),
        "created_at": now_iso(),
        "model": str(getattr(result, "model", "") or ""),
        "characters": rows,
        "linked_ids": [str(row["character_id"]) for row in rows if row.get("status") == "linked"],
        "applied": False,
        "applied_at": None,
    }
    _write_record(store, record)
    return record


def load_prose_cast(store: ProjectStore, chapter_id: str) -> dict:
    """读取本章的正文角色抽取记录。

    参数:
        store: 工程。
        chapter_id: 章节 ID。

    返回:
        抽取记录；不存在或损坏时返回空骨架。始终附带 `exists` 与 `stale`
        （正文 `content_hash` 与记录不一致时为 True）。
    """
    path = _record_path(store, chapter_id)
    if not path.exists():
        return _empty_record(chapter_id)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty_record(chapter_id)
    if not isinstance(record, dict):
        return _empty_record(chapter_id)
    record.setdefault("version", PROSE_CAST_VERSION)
    record.setdefault("chapter_id", chapter_id)
    record.setdefault("model", "")
    record.setdefault("characters", [])
    record.setdefault("linked_ids", [])
    record.setdefault("applied", False)
    record.setdefault("applied_at", None)
    record["exists"] = True
    record["stale"] = str(record.get("content_hash") or "") != _content_hash(_chapter_content(store, chapter_id))
    return record


def scope_character_ids(binding: dict, record: dict | None) -> tuple[set[str], str]:
    """计算回流与一致性检查共用的角色范围。

    范围 = 抽取记录里已关联（`status=linked`）且有 `character_id` 的角色
    ∪ 绑定里 `planned` 或 `is_pov` 的角色。

    降级记录（`source` 或 `model` 为 `scan-fallback`）不参与范围计算：这类结果
    只保留确定性姓名扫描，不能代表正文里的真实角色，退回大纲绑定范围。

    参数:
        binding: `chapter_binding()` 的返回值（可为空 dict）。
        record: 正文角色抽取记录；没有抽取结果时传 None。

    返回:
        `(ids, source)`；有已关联角色时 source 为 `prose`，否则（含降级记录）为 `binding`。
    """
    degraded = bool(
        str((record or {}).get("source") or "") == "scan-fallback"
        or str((record or {}).get("model") or "") == "scan-fallback"
    )
    linked: set[str] = set()
    if not degraded:
        for row in (record or {}).get("characters") or []:
            if not isinstance(row, dict) or row.get("status") != "linked":
                continue
            character_id = str(row.get("character_id") or "").strip()
            if character_id:
                linked.add(character_id)
    planned = {
        str(row.get("id"))
        for row in (binding or {}).get("characters") or []
        if row.get("id") and (row.get("planned") or row.get("is_pov"))
    }
    return linked | planned, ("prose" if linked else "binding")


def _scan_fallback(store: ProjectStore, chapter_id: str, content: str) -> dict:
    """构造纯确定性扫描结果（模型失败时的兜底记录）。

    参数:
        store: 工程。
        chapter_id: 章节 ID。
        content: 章节正文。

    返回:
        抽取记录（`model=scan-fallback`）。
    """
    scanned = scan_mentions(content, _characters(store))
    rows = [
        {
            "character_id": hit["character_id"],
            "name": hit["name"],
            "aliases": [],
            "mentions": int(hit.get("mentions") or 0),
            "evidence": hit.get("evidence") or "",
            "status": "linked",
        }
        for hit in scanned
    ]
    return {
        "version": PROSE_CAST_VERSION,
        "chapter_id": chapter_id,
        "content_hash": _content_hash(content),
        "created_at": now_iso(),
        "model": "scan-fallback",
        "characters": rows,
        "linked_ids": [str(row["character_id"]) for row in rows],
        "applied": False,
        "applied_at": None,
    }


def ensure_prose_cast(store: ProjectStore, config, chapter_id: str) -> dict:
    """确保本章有可用且未过期的抽取记录。

    - 记录存在且 `content_hash` 一致：直接复用；
    - 否则重新抽取；抽取抛错时回退为纯确定性扫描，不向外抛错。

    参数:
        store: 工程。
        config: 本机配置。
        chapter_id: 章节 ID。

    返回:
        抽取记录，额外带 `source`（cache/model/scan-fallback）。
    """
    record = load_prose_cast(store, chapter_id)
    if record.get("exists") and not record.get("stale"):
        record["source"] = "cache"
        return record
    try:
        record = extract_prose_cast(store, config, chapter_id)
        record["source"] = "model"
        return record
    except Exception:  # noqa: BLE001 - 抽取失败不阻断回流/一致性，降级为确定性扫描
        content = _chapter_content(store, chapter_id)
        record = _scan_fallback(store, chapter_id, content)
        _write_record(store, record)
        record["source"] = "scan-fallback"
        return record


def _allowed(value, options: tuple[str, ...], default: str) -> str:
    """枚举字段只接受合法值，否则回退默认值。

    参数:
        value: 原始值。
        options: 允许的取值。
        default: 非法时的回退值。

    返回:
        合法枚举值。
    """
    text = str(value or "").strip()
    return text if text in options else default


def _save_characters(store: ProjectStore, characters: list[dict]) -> None:
    """写回角色表（保留旁白条目）。

    参数:
        store: 工程。
        characters: 非旁白角色列表。

    返回:
        无。
    """
    table = store.load_characters()
    keep = [item for item in table.get("characters") or [] if item.get("role") == "narrator"]
    table["characters"] = [*keep, *characters]
    store.save_characters(table)


def apply_prose_cast(store: ProjectStore, chapter_id: str, selections: list[dict] | None) -> dict:
    """按作者勾选把待新建候选建成角色卡并关联本章。

    参数:
        store: 工程。
        chapter_id: 章节 ID。
        selections: 勾选项 `[{name, character_id?}]`；None 表示全部待新建候选。
            `character_id` 可选，给出时优先复用该角色卡。

    返回:
        `{chapter_id, created, reused, linked_ids, skipped, record}`；
        created/reused 是角色姓名列表。

    异常:
        ValueError: 本章还没有抽取记录。
    """
    record = load_prose_cast(store, chapter_id)
    if not record.get("exists"):
        raise ValueError("本章还没有正文角色抽取记录：请先抽取")
    try:
        chapter = store.load_chapter(chapter_id)
    except FileNotFoundError as exc:
        raise ValueError(f"章节不存在：{chapter_id}") from exc
    index = int(chapter.get("index") or 0)
    title = str(chapter.get("title") or "")

    wanted: dict[str, dict] = {}
    for item in selections or []:
        if not isinstance(item, dict):
            continue
        key = _norm(item.get("name"))
        if key:
            wanted[key] = item
    apply_all = selections is None

    characters = _characters(store)
    by_id, by_name = _index_characters(characters)
    created: list[str] = []
    reused: list[str] = []
    skipped: list[str] = []
    created_items: list[dict] = []

    for row in record.get("characters") or []:
        if not isinstance(row, dict) or row.get("status") != "new":
            continue
        name = str(row.get("name") or "").strip()
        key = _norm(name)
        if not name or (not apply_all and key not in wanted):
            continue
        aliases = _clean_list(row.get("aliases"), 40, 6)
        selection = wanted.get(key) or {}
        target_id = _clean_text(selection.get("character_id"), 40)
        target = by_id.get(target_id) if target_id else None
        if target is None:
            for probe in [name, *aliases]:
                target = by_name.get(_norm(probe))
                if target is not None:
                    break

        if target is not None:
            add_aliases(target, aliases)
            row["status"] = "linked"
            row["character_id"] = target.get("id")
            reused.append(str(target.get("name") or name))
            continue

        if is_descriptor(name):
            skipped.append(name)
            continue

        draft = row.get("draft") if isinstance(row.get("draft"), dict) else {}
        role = _allowed(draft.get("role"), ROLES, "minor")
        character = entities.new_character(name, role=role, aliases=aliases)
        character["gender"] = _allowed(draft.get("gender"), GENDERS, "unknown")
        age_range = str(draft.get("age_range") or "").strip()
        character["age_range"] = age_range if age_range in AGE_RANGES else None
        character["personality"] = _clean_list(draft.get("personality"), 40, 6)
        character["background"] = _clean_text(draft.get("background"), 1000) or f"正文抽取于第 {index} 章"
        speech = _safe_speech(draft.get("speech_style"))
        if speech:
            character["speech_style"] = speech
        evidence = _clean_text(row.get("evidence"), EVIDENCE_LIMIT)
        character["state"] = {
            "situation": f"首次出现在第 {index} 章正文：{evidence}" if evidence else f"首次出现在第 {index} 章正文",
            "as_of_chapter_id": chapter_id,
        }
        characters.append(character)
        by_id[str(character["id"])] = character
        for probe in [character.get("name"), *aliases]:
            norm = _norm(probe)
            if norm:
                by_name.setdefault(norm, character)
        row["status"] = "linked"
        row["character_id"] = character["id"]
        created.append(name)
        created_items.append({"id": character["id"], "name": name, "created_at": now_iso()})

    linked_ids: list[str] = []
    for row in record.get("characters") or []:
        character_id = str((row or {}).get("character_id") or "")
        if (row or {}).get("status") == "linked" and character_id and character_id not in linked_ids:
            linked_ids.append(character_id)
    record["linked_ids"] = linked_ids
    record["applied"] = bool(linked_ids)
    record["applied_at"] = now_iso() if record["applied"] else None

    if created or reused:
        # 复用分支可能补过别名，同样要写回角色表
        _save_characters(store, characters)
    if created:
        _record_created(store, index, title, created_items)
    _write_record(store, record)
    return {
        "chapter_id": chapter_id,
        "created": created,
        "reused": reused,
        "linked_ids": linked_ids,
        "skipped": skipped,
        "record": record,
    }
