"""大纲阶段的角色对齐：随「本章节拍」一次生成，不再单独发角色请求。

主线只有一条：`emit_beats` / `emit_act` 在返回节拍的同时返回本章 `cast`，
由 `apply_chapter_cast()` 做纯确定性的落地：

1. 姓名、别名规范化后先与现有角色卡比对，命中就复用并补别名；
2. 「她」「它」「女人」「男人」「对方」这类叙述代称一律不建卡；
3. 现有卡里没有的候选，第二次出现（跨章）才建卡；一次性的龙套按 minor 处理，
   只记在节拍的 `extra_characters` 里，不进入角色表；
4. 节拍没有点名任何角色时回退旁白（`chr_narrator`），不再硬绑主角；
5. 新建卡登记到 `.ndw/cast_created.json`，供界面「本章新建」查看。

正文生成后的状态回流与一致性检查仍只是补充，不在这里做。
"""

from __future__ import annotations

import json

from ...adapters.persistence import ProjectStore
from ...domain.cast.rules.naming import add_aliases, is_descriptor
from ...domain.shared.text import normalize_name
from ...models import entities
from ...models.ids import now_iso
from .character_fields import _clean_speech_style

#: 姓名归一化：阶段 3 起由 domain 单点实现（NFKC → 去空白 → casefold）
_norm = normalize_name

#: 允许的角色定位（与 character.schema.json 对齐）
ROLES = ("protagonist", "antagonist", "supporting", "minor")
#: 允许的性别取值
GENDERS = ("male", "female", "other", "unknown")

#: 待定角色登记（第二次出现才建卡）
PENDING_VERSION = "cast_pending/1"
#: 新建角色登记（界面「本章新建」）
CREATED_VERSION = "cast_created/1"


def _clean_text(value, limit: int) -> str:
    """把任意值转成去空白字符串并截断。

    参数:
        value: 任意值。
        limit: 最大长度。

    返回:
        去空白后的字符串。
    """
    return str(value or "").strip()[:limit]


def _registry_path(store: ProjectStore, name: str):
    """返回角色登记文件路径。

    参数:
        store: 工程。
        name: 文件名（不含扩展名）。

    返回:
        `.ndw/<name>.json` 的 Path。
    """
    return store.root / ".ndw" / f"{name}.json"


def _load_registry(store: ProjectStore, name: str, version: str) -> dict:
    """读取登记文件；不存在或损坏时返回空骨架。

    参数:
        store: 工程。
        name: 文件名。
        version: 版本号。

    返回:
        登记 dict。
    """
    path = _registry_path(store, name)
    if not path.exists():
        return {"version": version, "chapters": {}, "pending": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": version, "chapters": {}, "pending": {}}
    if not isinstance(data, dict):
        return {"version": version, "chapters": {}, "pending": {}}
    data.setdefault("version", version)
    data.setdefault("chapters", {})
    data.setdefault("pending", {})
    return data


def _save_registry(store: ProjectStore, name: str, data: dict) -> None:
    """原子写入登记文件。

    参数:
        store: 工程。
        name: 文件名。
        data: 登记内容。

    返回:
        无。
    """
    path = _registry_path(store, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def load_pending(store: ProjectStore) -> dict:
    """读取「第二次出现才建卡」的待定登记。

    参数:
        store: 工程。

    返回:
        `{归一化姓名: {name, chapter, chapter_title, aliases}}`。
    """
    return dict(_load_registry(store, "cast_pending", PENDING_VERSION).get("pending") or {})


def save_pending(store: ProjectStore, pending: dict) -> None:
    """写入待定登记（只保留仍待定的候选）。

    参数:
        store: 工程。
        pending: 待定登记 dict。

    返回:
        无。
    """
    _save_registry(
        store,
        "cast_pending",
        {"version": PENDING_VERSION, "chapters": {}, "pending": pending},
    )


def promote_pending(store: ProjectStore, key: str, *, role: str = "supporting") -> dict:
    """把待定角色立即建卡，并从待定队列移除。

    建卡字段与 `apply_chapter_cast()` 的第二次出现路径一致（同一个 `_new_character`），
    不修改任何已建角色；若同名角色已存在，只从待定队列移除。

    参数:
        store: 工程。
        key: 待定队列键（归一化姓名）；也接受直接传姓名。
        role: 新建角色时的定位，默认配角。

    返回:
        `{created, character, pending}`；created=False 表示复用了已有角色卡。

    异常:
        ValueError: 待定角色不存在或已被处理。
    """
    pending = load_pending(store)
    norm = key if key in pending else _norm(key)
    record = pending.get(norm)
    if not record:
        raise ValueError("待定角色不存在或已处理，请刷新后重试")

    characters = [
        item for item in store.load_characters().get("characters") or [] if item.get("role") != "narrator"
    ]
    _, by_name = _index_characters(characters)
    existing = by_name.get(_norm(record.get("name")))
    if existing is not None:
        character = existing
        created = False
    else:
        row = dict(record)
        row["role"] = role if role in ROLES else "supporting"
        row["beats"] = []
        row["_chapter"] = record.get("chapter")
        character = _new_character(row)
        characters.append(character)
        created = True
    pending.pop(norm, None)
    _persist(store, characters, pending)
    return {"created": created, "character": character, "pending": pending}


def discard_pending(store: ProjectStore, key: str) -> dict:
    """从待定队列丢弃一个候选人，不影响已建角色与节拍绑定。

    参数:
        store: 工程。
        key: 待定队列键（归一化姓名）；也接受直接传姓名。

    返回:
        `{discarded, pending}`。

    异常:
        ValueError: 待定角色不存在或已被处理。
    """
    pending = load_pending(store)
    norm = key if key in pending else _norm(key)
    record = pending.pop(norm, None)
    if record is None:
        raise ValueError("待定角色不存在或已处理，请刷新后重试")
    save_pending(store, pending)
    return {"discarded": record.get("name") or key, "pending": pending}


def created_in_chapter(store: ProjectStore, chapter_index: int) -> list[dict]:
    """读取某章新建的角色（界面「本章新建」）。

    参数:
        store: 工程。
        chapter_index: 全文章序号。

    返回:
        `[{id, name, created_at}]`；没有记录时返回空列表。
    """
    data = _load_registry(store, "cast_created", CREATED_VERSION)
    row = (data.get("chapters") or {}).get(str(int(chapter_index or 0))) or {}
    return list(row.get("items") or [])


def list_created_chapters(store: ProjectStore) -> list[dict]:
    """列出所有有「本章新建」记录的章节，按章序号倒序。

    参数:
        store: 工程。

    返回:
        `[{index, title, items}]`。
    """
    data = _load_registry(store, "cast_created", CREATED_VERSION)
    rows: list[dict] = []
    for key, row in (data.get("chapters") or {}).items():
        try:
            index = int(key)
        except (TypeError, ValueError):
            continue
        rows.append(
            {
                "index": index,
                "title": (row or {}).get("title") or "",
                "items": list((row or {}).get("items") or []),
            }
        )
    rows.sort(key=lambda item: item["index"], reverse=True)
    return rows


def _record_created(store: ProjectStore, index: int, title: str, items: list[dict]) -> None:
    """把本章新建的角色记进新建登记。

    参数:
        store: 工程。
        index: 全文章序号。
        title: 章节标题。
        items: 新建角色 `[{id, name, created_at}]`。

    返回:
        无。
    """
    if not items:
        return
    data = _load_registry(store, "cast_created", CREATED_VERSION)
    key = str(int(index or 0))
    row = (data["chapters"].get(key) or {})
    existing = list(row.get("items") or [])
    known = {str(item.get("id")) for item in existing}
    for item in items:
        if str(item.get("id")) not in known:
            existing.append(item)
    data["chapters"][key] = {"title": title, "items": existing}
    _save_registry(store, "cast_created", data)


def cast_brief(characters: list[dict], limit: int = 200) -> list[dict]:
    """给模型看的现有角色摘要（只保留复用判断需要的字段）。

    参数:
        characters: 角色表（可含旁白，内部过滤）。
        limit: 最多列出多少个角色。

    返回:
        `[{id, name, aliases, role}]`。
    """
    rows = [
        {
            "id": item.get("id"),
            "name": item.get("name"),
            "aliases": (item.get("aliases") or [])[:6],
            "role": item.get("role"),
        }
        for item in characters
        if item.get("id") and item.get("role") != "narrator"
    ]
    return rows[: max(1, int(limit))]


def cast_prompt_block(characters: list[dict]) -> str:
    """拼节拍生成用的角色对齐规则（与 cast 字段一起交给同一次调用）。

    参数:
        characters: 现有角色表（可含旁白）。

    返回:
        中文提示块。
    """
    brief = cast_brief(characters)
    lines = [
        "同时输出本章 cast（出场角色）与每个角色对应的节拍序号：",
        "1) 先用姓名或别名与现有角色表比对，命中的填 existing_id 复用，只补本章新出现的别名，不得重复建卡；",
        "2) 「她」「它」「女人」「男人」「对方」这类叙述代称不要写进 cast，也不要建卡；",
        "3) 只出现一次的路人不要写进 cast（按 minor 处理），写在对应节拍的 extra_characters 里即可；",
        "4) cast 每项必须给 beats（该角色在本章出场的节拍序号，从 1 开始），没有节拍依据的角色不要列；",
        "5) 新建角色给 role/gender/age_range/personality/background/speech_style；复用角色不用重复给设定；",
        "6) 每个节拍至少要有一个角色；确实没人物的节拍留空，系统会回退旁白。",
        f"现有角色表：{json.dumps(brief, ensure_ascii=False)}",
    ]
    return "\n".join(lines)


def normalize_cast(payload: dict) -> list[dict]:
    """清洗模型返回的 cast 行（只保留可落地字段，不做业务判断）。

    参数:
        payload: `emit_beats` / `emit_act` 的返回值。

    返回:
        cast 行列表；格式不对时返回空列表。
    """
    raw = payload.get("cast") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return []
    rows: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = _clean_text(item.get("name"), 40)
        if not name:
            continue
        beats: list[int] = []
        for value in item.get("beats") or []:
            try:
                number = int(value)
            except (TypeError, ValueError):
                continue
            if number > 0 and number not in beats:
                beats.append(number)
        rows.append(
            {
                "existing_id": _clean_text(item.get("existing_id"), 40),
                "name": name,
                "aliases": [_clean_text(alias, 40) for alias in (item.get("aliases") or []) if _clean_text(alias, 40)],
                "role": _clean_text(item.get("role"), 20),
                "gender": _clean_text(item.get("gender"), 10),
                "age_range": _clean_text(item.get("age_range"), 20),
                "personality": [_clean_text(word, 40) for word in (item.get("personality") or []) if _clean_text(word, 40)][:6],
                "background": _clean_text(item.get("background"), 1000),
                "speech_style": item.get("speech_style") if isinstance(item.get("speech_style"), dict) else {},
                "beats": beats,
            }
        )
    return rows


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


def _new_character(row: dict) -> dict:
    """按 cast 行创建角色卡（只写写作与配音都用得上的字段）。

    参数:
        row: 清洗后的 cast 行。

    返回:
        角色 dict。
    """
    role = row.get("role") if row.get("role") in ROLES else "supporting"
    character = entities.new_character(row["name"], role=role)
    character["aliases"] = _clean_list(row.get("aliases"), 40, 6)
    gender = row.get("gender")
    character["gender"] = gender if gender in GENDERS else "unknown"
    character["age_range"] = row.get("age_range") or None
    character["personality"] = _clean_list(row.get("personality"), 40, 6)
    character["background"] = row.get("background") or f"大纲第 {row.get('_chapter') or '?'} 章创建"
    speech = _clean_speech_style(row.get("speech_style"))
    if speech:
        character["speech_style"] = speech
    return character


def _index_characters(characters: list[dict]) -> tuple[dict[str, dict], dict[str, dict]]:
    """建立角色索引：ID → 角色、归一化姓名/别名 → 角色。

    参数:
        characters: 角色表（不含旁白）。

    返回:
        `(by_id, by_name)`。
    """
    by_id: dict[str, dict] = {}
    by_name: dict[str, dict] = {}
    for item in characters:
        character_id = str(item.get("id") or "")
        if not character_id:
            continue
        by_id[character_id] = item
        for key in [item.get("name"), *(item.get("aliases") or [])]:
            norm = _norm(key)
            if norm:
                by_name.setdefault(norm, item)
    return by_id, by_name


def _match_existing(row: dict, by_id: dict[str, dict], by_name: dict[str, dict]) -> dict | None:
    """按 existing_id / 姓名 / 别名匹配现有角色卡。

    参数:
        row: cast 行。
        by_id: ID 索引。
        by_name: 归一化姓名索引。

    返回:
        命中的角色 dict；没有命中返回 None。
    """
    existing_id = row.get("existing_id") or ""
    if existing_id and existing_id in by_id:
        return by_id[existing_id]
    for key in [row.get("name"), *(row.get("aliases") or [])]:
        hit = by_name.get(_norm(key))
        if hit is not None:
            return hit
    return None


def apply_chapter_cast(
    store: ProjectStore,
    chapter: dict,
    rows: list[dict],
    *,
    characters: list[dict] | None = None,
    pending: dict | None = None,
    persist: bool = True,
    chapter_index: int | None = None,
    chapter_title: str = "",
) -> dict:
    """把一次生成里的本章 cast 落到角色卡与节拍绑定上（纯确定性，不调模型）。

    参数:
        store: 工程。
        chapter: 大纲章节（含 beats；会被原地写入 character_ids 与 extra_characters）。
        rows: `normalize_cast()` 的输出。
        characters: 本次生成使用的内存角色表（不含旁白）；None 时从工程读取。
        pending: 待定登记；None 时按 persist 读取/忽略。
        persist: False 时只预览，不写角色卡与登记文件。
        chapter_index: 全文章序号；None 时读 chapter.index。
        chapter_title: 章节标题（登记用）；None 时读 chapter.title。

    返回:
        `{created, reused, extras, cast, new_characters, narrator_beats, characters, pending}`；
        created/reused/extras 都是姓名列表。
    """
    index = int(chapter_index if chapter_index is not None else chapter.get("index") or 0)
    title = chapter_title or str(chapter.get("title") or "")
    beats = chapter.get("beats") or []
    if not beats:
        raise ValueError(f"第 {index} 章没有节拍：角色对齐必须与节拍在同一次生成里完成")

    if characters is None:
        characters = [
            item for item in store.load_characters()["characters"] if item.get("role") != "narrator"
        ]
    else:
        characters = list(characters)
    if pending is None:
        pending = load_pending(store) if persist else {}

    by_id, by_name = _index_characters(characters)
    created: list[str] = []
    reused: list[str] = []
    extras: list[str] = []
    extra_rows: list[dict] = []
    new_characters: list[dict] = []
    cast: list[dict] = []
    seen_ids: set[str] = set()
    total = len(beats)

    for row in rows or []:
        name = row.get("name") or ""
        if not name:
            continue
        if _norm(name) == _norm("旁白"):
            # 旁白是保留实体，只作为兜底，不补别名也不建卡
            continue
        existing = _match_existing(row, by_id, by_name)
        if existing is None and is_descriptor(name):
            extras.append(name)
            extra_rows.append({"name": name, "beats": list(row.get("beats") or [])})
            continue
        if existing is not None:
            character = existing
            add_aliases(character, row.get("aliases") or [])
            if character.get("name") not in reused:
                reused.append(character.get("name"))
            pending.pop(_norm(character.get("name")), None)
        else:
            key = _norm(name)
            first = pending.get(key)
            if not first or int(first.get("chapter") or 0) == index:
                # 第一次出现（或仍在同一章）：只登记待定，本章按龙套处理
                if not first:
                    pending[key] = {
                        "name": name,
                        "chapter": index,
                        "chapter_title": title,
                        "aliases": list(row.get("aliases") or []),
                    }
                extras.append(name)
                extra_rows.append({"name": name, "beats": list(row.get("beats") or [])})
                continue
            # 第二次出现（跨章）：建卡
            record = dict(row)
            record["_chapter"] = index
            aliases = list(dict.fromkeys([*(first.get("aliases") or []), *(row.get("aliases") or [])]))
            record["aliases"] = aliases
            character = _new_character(record)
            characters.append(character)
            new_characters.append(character)
            created.append(character["name"])
            by_id[str(character["id"])] = character
            for alias_key in [character.get("name"), *aliases]:
                norm = _norm(alias_key)
                if norm:
                    by_name.setdefault(norm, character)
            pending.pop(key, None)

        character_id = str(character.get("id") or "")
        if not character_id or character_id in seen_ids:
            continue
        seen_ids.add(character_id)
        numbers = [number for number in row.get("beats") or [] if 1 <= int(number) <= total]
        cast.append(
            {
                "id": character_id,
                "name": character.get("name"),
                "role": character.get("role"),
                "beats": numbers,
            }
        )

    # 节拍绑定：只保留本章有效角色，未点名的节拍回退旁白
    valid_ids = {item["id"] for item in cast}
    narrator_beats = 0
    for beat in beats:
        ids = [
            str(cid)
            for cid in (beat.get("character_ids") or [])
            if str(cid) in valid_ids or str(cid) == entities.NARRATOR_ID
        ]
        if str(entities.NARRATOR_ID) in ids:
            # 模型直接写了旁白 ID 时同样保留，但排到有效角色之后
            ids = [cid for cid in ids if cid != entities.NARRATOR_ID]
        beat["character_ids"] = ids
    for item in cast:
        for position in item["beats"]:
            ids = beats[position - 1].setdefault("character_ids", [])
            if item["id"] not in ids and entities.NARRATOR_ID not in ids:
                ids.append(item["id"])
    for beat in beats:
        if not beat.get("character_ids"):
            beat["character_ids"] = [entities.NARRATOR_ID]
            narrator_beats += 1

    # 未建卡的龙套：按 minor 处理，只记在对应节拍上，不进角色表
    for row in extra_rows:
        positions = [number for number in row.get("beats") or [] if 1 <= int(number) <= total]
        if not positions:
            positions = [pos for pos, beat in enumerate(beats, start=1) if row["name"] in str(beat.get("summary") or "")]
        for position in positions:
            beat = beats[position - 1]
            names = [str(item) for item in (beat.get("extra_characters") or [])]
            if row["name"] not in names:
                names.append(row["name"])
            beat["extra_characters"] = names

    if persist:
        _persist(store, characters, pending)
        _record_created(
            store,
            index,
            title,
            [
                {"id": item["id"], "name": item.get("name"), "created_at": now_iso()}
                for item in new_characters
            ],
        )
    return {
        "created": created,
        "reused": reused,
        "extras": extras,
        "cast": cast,
        "new_characters": new_characters,
        "narrator_beats": narrator_beats,
        "characters": characters,
        "pending": pending,
    }


def _persist(store: ProjectStore, characters: list[dict], pending: dict) -> None:
    """把内存角色表与待定登记写回工程（保留旁白条目）。

    参数:
        store: 工程。
        characters: 内存角色表（不含旁白）。
        pending: 待定登记。

    返回:
        无。
    """
    table = store.load_characters()
    keep = [item for item in table.get("characters") or [] if item.get("role") == "narrator"]
    table["characters"] = [*keep, *characters]
    store.save_characters(table)
    save_pending(store, pending)


def summarize_cast(report: dict) -> str:
    """把一次角色对齐结果压成一行中文摘要（进度提示用）。

    参数:
        report: `apply_chapter_cast()` 的返回值。

    返回:
        中文摘要。
    """
    parts = [f"新建 {len(report.get('created') or [])}", f"复用 {len(report.get('reused') or [])}"]
    if report.get("extras"):
        parts.append(f"龙套 {len(report['extras'])}")
    if report.get("narrator_beats"):
        parts.append(f"旁白兜底 {report['narrator_beats']} 拍")
    return "，".join(parts)
