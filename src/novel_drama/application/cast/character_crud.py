"""角色表的写用例：批量追加、单字段更新、删除与引用统计。

CLI（`character apply/update/remove`）与 Web 共用本模块，写入规则只有一份。
"""

from __future__ import annotations

import json

from ...adapters.persistence import ProjectStore
from ...adapters.persistence.settings_store import SettingsStore
from ...composition import render_character_sheet
from ...domain.shared.errors import PreconditionError
from ...models import entities
from ...models.enums import AgeRange, SpeechPitch, SpeechTempo, choices_text
from ...models.ids import now_iso
from ..production.lines_source import chapter_view

#: 允许写入的角色定位（narrator 由系统保留）
ROLE_CHOICES = ("protagonist", "antagonist", "supporting", "minor")
#: 允许写入的性别枚举
GENDER_CHOICES = ("male", "female", "other", "unknown")

#: `update_character` 接受的标量字段（与 CLI 参数名一致）
UPDATE_FIELDS = (
    "name",
    "role",
    "gender",
    "age_range",
    "personality",
    "background",
    "aliases",
    "appearance_prompt",
    "negative_prompt",
    "voice_description_zh",
    "voice_description_en",
    "appearance_desc_zh",
    "appearance_desc_en",
    "appearance_negative_zh",
    "appearance_negative_en",
    "situation",
    "knowledge",
    "relations",
    "speech_style",
    "voice_profile",
    "stock",
)


def split_items(text: str) -> list[str]:
    """把逗号/顿号分隔的文本拆成去空列表。

    参数:
        text: 原始文本。

    返回:
        条目列表。
    """
    import re

    return [part.strip() for part in re.split(r"[、,，;；]+", text or "") if part.strip()]


def clean_speech_style(style: dict, current: dict | None = None) -> dict:
    """清洗说话方式：语速 / 音高只接受枚举值，口音与口头禅保留自由文本。

    参数:
        style: 界面或命令行传入的说话方式对象。
        current: 磁盘上现有的说话方式；传入时，与现有值完全相同的旧非法值原样保留，
            这样只修一个字段（如语速）不会因为另一个历史字段（如音高「高」）被连带拒绝。

    返回:
        `{tempo, pitch, accent, catchphrases}`，未填写项为 None / 空数组。

    异常:
        ValueError: 语速或音高不在可选范围（中文消息含可选值）。
    """
    raw_tempo = str(style.get("tempo") or "").strip()
    raw_pitch = str(style.get("pitch") or "").strip()
    old_tempo = str((current or {}).get("tempo") or "").strip()
    old_pitch = str((current or {}).get("pitch") or "").strip()
    tempo = SpeechTempo.coerce(raw_tempo) if raw_tempo else None
    pitch = SpeechPitch.coerce(raw_pitch) if raw_pitch else None
    if raw_tempo and tempo is None:
        if raw_tempo != old_tempo:
            raise ValueError(f"语速「{raw_tempo}」不在可选范围；可选值：{choices_text(SpeechTempo)}")
        tempo = raw_tempo
    if raw_pitch and pitch is None:
        if raw_pitch != old_pitch:
            raise ValueError(f"音高「{raw_pitch}」不在可选范围；可选值：{choices_text(SpeechPitch)}")
        pitch = raw_pitch
    return {
        "tempo": tempo,
        "pitch": pitch,
        "accent": str(style.get("accent") or "").strip()[:40] or None,
        "catchphrases": [str(item).strip()[:60] for item in (style.get("catchphrases") or []) if str(item).strip()],
    }


def append_characters(store: ProjectStore, rows: list[dict]) -> dict:
    """把生成/导入的人物追加到 characters.json。

    参数:
        store: 工程。
        rows: [{name, role, gender, age_range, personality, background, appearance_prompt,
        negative_prompt, relationships（或 state.relationship_notes）}]。

    返回:
        `{"characters": 新建人物列表, "skipped": 重名被跳过的人物名列表}`。

    异常:
        ValueError: 字段不合法。
    """
    table = store.load_characters()
    existing = {item.get("name") for item in table["characters"]}
    id_by_name = {item.get("name"): item.get("id") for item in table["characters"] if item.get("name")}
    created: list[dict] = []
    skipped: list[str] = []
    pending: list[tuple[dict, dict]] = []

    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"第 {index + 1} 个人物必须是对象")
        name = str(row.get("name") or "").strip()
        if not name:
            raise ValueError("人物姓名不能为空")
        if len(name) > 60:
            raise ValueError(f"人物姓名超过 60 字上限: {name}")
        if name in existing:
            skipped.append(name)
            continue
        role = str(row.get("role") or "supporting").strip()
        if role not in ROLE_CHOICES:
            raise ValueError(f"人物 {name} 的 role 非法: {role}（可选 {'/'.join(ROLE_CHOICES)}）")
        gender = str(row.get("gender") or "unknown").strip()
        character = entities.new_character(name, role=role)
        character["gender"] = gender if gender in GENDER_CHOICES else "unknown"
        character["age_range"] = AgeRange.normalize(row.get("age_range")) if str(row.get("age_range") or "").strip() else None
        character["personality"] = [str(item).strip() for item in (row.get("personality") or []) if str(item).strip()]
        character["background"] = str(row.get("background") or "").strip()
        character["appearance_prompt"] = str(row.get("appearance_prompt") or "").strip()
        character["negative_prompt"] = str(row.get("negative_prompt") or "").strip()
        relationships = row.get("relationships") or {}
        state = row.get("state") if isinstance(row.get("state"), dict) else {}
        if not relationships and isinstance(state.get("relationship_notes"), dict):
            relationships = state["relationship_notes"]
        character["state"] = {"situation": None, "relationship_notes": None, "knowledge": None, "as_of_chapter_id": None}
        if isinstance(row.get("voice_profile"), dict):
            character["voice_profile"] = row["voice_profile"]
        if isinstance(row.get("stock"), dict):
            character["stock"] = row["stock"]
        if isinstance(row.get("voice_binding"), dict):
            character["voice_binding"] = row["voice_binding"]
        table["characters"].append(character)
        existing.add(name)
        id_by_name[name] = character["id"]
        created.append(character)
        if isinstance(relationships, dict):
            pending.append((character, relationships))

    # 全部角色建好后再解析关系目标，保证同批生成的人物也能映射到真实 ID
    for character, relationships in pending:
        notes: dict[str, str] = {}
        for target, note in relationships.items():
            key = str(target or "").strip()
            if not key:
                continue
            notes[id_by_name.get(key, key)] = str(note or "").strip()[:500]
        character["state"]["relationship_notes"] = notes or None

    if created:
        store.save_characters(table)
    return {"characters": created, "skipped": skipped}


def update_character(store: ProjectStore, character_id: str, changes: dict) -> dict:
    """按 ID 更新单个人物的字段。

    参数:
        store: 工程。
        character_id: 角色 ID。
        changes: 字段名 → 原值（与 CLI 参数同类型；JSON 型字段传字符串）。
            `relationship` 为 `目标ID=备注` 列表，`relationship_remove` 为要删除的关系目标。

    返回:
        `{"character": 更新后的角色, "changed": ["name=新名", ...]}`。

    异常:
        PreconditionError: 角色不存在或新名字已被占用。
        ValueError: 字段值不合法或没有任何可修改字段。
    """
    table = store.load_characters()
    target = next((item for item in table["characters"] if item.get("id") == character_id), None)
    if target is None:
        raise PreconditionError(f"角色不存在: {character_id}")
    # 关系键统一为角色 ID：姓名只用于展示，写入前先映射成 ID
    id_by_name = {str(item.get("name") or ""): str(item.get("id") or "") for item in table["characters"]}

    def value(key: str):
        return changes.get(key)

    changed: list[str] = []
    if value("name") is not None:
        name = str(value("name")).strip()
        if not name:
            raise ValueError("角色名不能为空")
        if any(item.get("name") == name and item.get("id") != character_id for item in table["characters"]):
            raise PreconditionError(f"角色名已存在: {name}")
        target["name"] = name
        changed.append(f"name={name}")
    if value("role"):
        target["role"] = value("role")
        changed.append(f"role={value('role')}")
    if value("gender"):
        target["gender"] = value("gender")
        changed.append(f"gender={value('gender')}")
    if value("age_range") is not None:
        raw_age = str(value("age_range")).strip()
        if not raw_age:
            target["age_range"] = None
        else:
            age = AgeRange.coerce(raw_age)
            if age is None:
                # 历史非法值原样提交时放行（由界面提示修正），改动成新的非法值仍然拒绝
                if raw_age != str(target.get("age_range") or "").strip():
                    raise ValueError(f"年龄段「{raw_age}」不在可选范围；可选值：{choices_text(AgeRange)}")
                age = raw_age
            target["age_range"] = age
        changed.append("age_range")
    if value("personality"):
        target["personality"] = split_items(value("personality"))
        changed.append("personality")
    if value("background") is not None:
        target["background"] = value("background")
        changed.append("background")
    if value("aliases") is not None:
        target["aliases"] = split_items(value("aliases"))
        changed.append("aliases")
    if value("appearance_prompt") is not None:
        target["appearance_prompt"] = str(value("appearance_prompt")).strip()
        changed.append("appearance_prompt")
    if value("negative_prompt") is not None:
        target["negative_prompt"] = str(value("negative_prompt")).strip()
        changed.append("negative_prompt")
    # 六项双语描述（音色 / 外貌 / 负向）：后续配音与出图直接取用，允许手改与清空
    for field in (
        "voice_description_zh",
        "voice_description_en",
        "appearance_desc_zh",
        "appearance_desc_en",
        "appearance_negative_zh",
        "appearance_negative_en",
    ):
        if value(field) is not None:
            target[field] = str(value(field)).strip()
            changed.append(field)
    if value("voice_profile") is not None:
        raw = str(value("voice_profile")).strip()
        if raw:
            try:
                profile = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(f"voice_profile 不是合法 JSON: {exc}") from exc
            if not isinstance(profile, dict):
                raise ValueError("voice_profile 必须是 JSON 对象")
            target["voice_profile"] = profile
        else:
            target.pop("voice_profile", None)
        changed.append("voice_profile")
    if value("stock") is not None:
        raw = str(value("stock")).strip()
        if raw:
            try:
                stock = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(f"stock 不是合法 JSON: {exc}") from exc
            if not isinstance(stock, dict) or not stock.get("slot"):
                raise ValueError("stock 必须是含 slot 的 JSON 对象")
            target["stock"] = stock
        else:
            target.pop("stock", None)
        changed.append("stock")

    if value("speech_style") is not None:
        raw = str(value("speech_style") or "").strip()
        if raw:
            try:
                style = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(f"speech_style 不是合法 JSON: {exc}") from exc
            if not isinstance(style, dict):
                raise ValueError("speech_style 必须是 JSON 对象")
            target["speech_style"] = clean_speech_style(style, target.get("speech_style"))
        else:
            target["speech_style"] = {"tempo": None, "pitch": None, "accent": None, "catchphrases": []}
        changed.append("speech_style")

    state = target.get("state") or {}
    state_changed = False
    if value("situation") is not None:
        state["situation"] = str(value("situation")).strip() or None
        state_changed = True
        changed.append("situation")
    if value("knowledge") is not None:
        raw = str(value("knowledge") or "").strip()
        if raw.startswith("["):
            try:
                items = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(f"knowledge 不是合法 JSON: {exc}") from exc
        else:
            items = split_items(raw)
        state["knowledge"] = [str(item).strip()[:200] for item in items if str(item).strip()] or None
        state_changed = True
        changed.append("knowledge")
    if value("relations") is not None:
        raw = str(value("relations") or "").strip()
        rows = json.loads(raw) if raw else []
        if not isinstance(rows, list):
            raise ValueError("relations 必须是 JSON 数组")
        clean_rows: list[dict] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            target_id = str(row.get("target_id") or "").strip()
            if not target_id:
                continue
            if target_id not in id_by_name.values():
                raise ValueError(f"关系目标角色不存在：{target_id}")
            clean_rows.append(
                {
                    "target_id": target_id,
                    "label": str(row.get("label") or "").strip()[:40],
                    "sentiment": row.get("sentiment") if row.get("sentiment") in ("ally", "enemy", "family", "romance", "neutral") else None,
                    "since_chapter_id": str(row.get("since_chapter_id") or "").strip() or None,
                    "note": str(row.get("note") or "").strip()[:500],
                }
            )
        state["relations"] = clean_rows or None
        state_changed = True
        changed.append("relations")
    for item in changes.get("relationship") or []:
        if "=" not in item:
            raise ValueError(f"--relationship 需要 `目标ID=备注` 格式: {item}")
        key, note = item.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError("--relationship 的目标角色 ID 不能为空")
        # 允许命令行传姓名，落盘前统一换成角色 ID
        key = id_by_name.get(key, key)
        if key not in id_by_name.values():
            raise ValueError(f"关系目标角色不存在：{key}；请使用角色 ID（人物页可复制）")
        notes = state.get("relationship_notes") or {}
        notes[key] = note.strip()[:500]
        state["relationship_notes"] = notes
        state_changed = True
        changed.append(f"relationship:{key}")
    for item in changes.get("relationship_remove") or []:
        key = str(item or "").strip()
        if not key:
            continue
        key = id_by_name.get(key, key)
        notes = state.get("relationship_notes") or {}
        if key in notes:
            del notes[key]
            state["relationship_notes"] = notes
            state_changed = True
            changed.append(f"relationship-remove:{key}")
        # 结构化关系同步删除，避免关系图残留悬空边
        relations = state.get("relations") or []
        kept = [row for row in relations if str(row.get("target_id") or "") != key]
        if len(kept) != len(relations):
            state["relations"] = kept or None
            state_changed = True
    if state_changed:
        target["state"] = state

    if not changed:
        raise ValueError("没有要修改的字段：至少给一个（如 --name / --role / --background）")
    target["updated_at"] = now_iso()
    store.save_characters(table)
    return {"character": target, "changed": changed}


# ---------------- 引用统计与删除保护 ----------------

#: 引用来源的中文名（同时决定展示顺序）
REFERENCE_LABELS = {
    "pov": "大纲视角（POV）",
    "beats": "大纲节拍选角",
    "arcs": "大纲角色弧光",
    "scenes": "章节场景出场",
    "lines": "台词说话人",
    "overrides": "章节音色覆盖",
    "relations": "其它角色的关系",
}

#: 各来源的计数单位（提示文案用）
_REFERENCE_UNITS = {"pov": "章", "beats": "个", "arcs": "条", "scenes": "个", "lines": "行", "overrides": "章", "relations": "条"}


def _chapter_names(rows: list[dict], key: str) -> str:
    """把命中某来源的章节渲染成「第N章《标题》」列表文本。

    参数:
        rows: 合并后的章节明细（含 index/title 与各来源计数）。
        key: 来源键（pov / beats / arcs / scenes / lines / overrides）。

    返回:
        中文列表；超过 6 章时省略并给出总数。
    """
    names = []
    for row in rows:
        if not row.get(key):
            continue
        index = row.get("index")
        title = str(row.get("title") or "").strip()
        prefix = f"第{index}章" if index is not None else "未编号章"
        names.append(f"{prefix}《{title}》" if title else prefix)
    if not names:
        return ""
    if len(names) > 6:
        return "、".join(names[:6]) + f" 等 {len(names)} 章"
    return "、".join(names)


def outline_references(bible: dict, character_id: str) -> dict:
    """统计大纲（设定库）里对某角色的引用，并给出逐章明细。

    覆盖三类真实引用：章节视角（`pov_character_id`）、节拍选角（`beats[].character_ids`）、
    角色弧光（`character_arcs[].character_id`）。

    参数:
        bible: 设定库 dict（未初始化时传空 dict）。
        character_id: 角色 ID。

    返回:
        `{"pov", "beats", "arcs", "chapters": [{id, index, title, pov, beats, arcs}]}`。
    """
    rows: list[dict] = []
    totals = {"pov": 0, "beats": 0, "arcs": 0}
    for act in (bible.get("outline") or {}).get("acts") or []:
        for chapter in act.get("chapters") or []:
            pov = 1 if chapter.get("pov_character_id") == character_id else 0
            beats = sum(1 for beat in chapter.get("beats") or [] if character_id in (beat.get("character_ids") or []))
            arcs = sum(1 for row in chapter.get("character_arcs") or [] if row.get("character_id") == character_id)
            if not (pov or beats or arcs):
                continue
            totals["pov"] += pov
            totals["beats"] += beats
            totals["arcs"] += arcs
            rows.append(
                {
                    "id": chapter.get("id"),
                    "index": chapter.get("index"),
                    "title": chapter.get("title"),
                    "pov": pov,
                    "beats": beats,
                    "arcs": arcs,
                }
            )
    return {**totals, "chapters": rows}


def chapter_references(store: ProjectStore, character_id: str) -> dict:
    """统计章节文件里对某角色的引用，并给出逐章明细。

    覆盖三类真实引用：场景出场（`scene.character_ids`）、台词说话人（`line.speaker_id`）、
    章节音色覆盖（`chapter.voice_overrides` 的键）。读不出来的章节跳过，不打断整次统计。

    参数:
        store: 工程。
        character_id: 角色 ID。

    返回:
        `{"scenes", "lines", "overrides", "chapters": [...]}`；`lines` 最多保留 50 条样本，
        `line_count` 是完整条数。
    """
    rows: list[dict] = []
    totals = {"scenes": 0, "lines": 0, "overrides": 0}
    for meta in store.list_chapters():
        try:
            chapter = chapter_view(store, store.load_chapter(meta["id"]))
        except (OSError, ValueError):
            continue
        lines = [
            {"line_id": line.get("id"), "text": line.get("text")}
            for scene in chapter.get("scenes") or []
            for line in scene.get("lines") or []
            if line.get("speaker_id") == character_id
        ]
        scenes = sum(1 for scene in chapter.get("scenes") or [] if character_id in (scene.get("character_ids") or []))
        overrides = 1 if character_id in (chapter.get("voice_overrides") or {}) else 0
        if not (lines or scenes or overrides):
            continue
        totals["scenes"] += scenes
        totals["lines"] += len(lines)
        totals["overrides"] += overrides
        rows.append(
            {
                "id": meta["id"],
                "index": meta.get("index"),
                "title": meta.get("title"),
                "lines": lines[:50],
                "line_count": len(lines),
                "scenes": scenes,
                "overrides": overrides,
            }
        )
    return {**totals, "chapters": rows}


def relation_references(table: dict, character_id: str) -> dict:
    """统计其它角色指向该角色的关系（关系行与关系备注）。

    参数:
        table: characters.json 内容。
        character_id: 角色 ID。

    返回:
        `{"notes": n, "relations": n, "characters": [{id, name, note, relations}]}`；
        `characters` 是「谁会受影响」的名单。
    """
    rows: list[dict] = []
    notes_total = 0
    relations_total = 0
    for item in table.get("characters") or []:
        if str(item.get("id")) == character_id:
            continue
        state = item.get("state") or {}
        note = (state.get("relationship_notes") or {}).get(character_id)
        hits = [row for row in state.get("relations") or [] if str(row.get("target_id")) == character_id]
        if note is None and not hits:
            continue
        notes_total += 1 if note is not None else 0
        relations_total += len(hits)
        rows.append({"id": item.get("id"), "name": item.get("name"), "note": note or "", "relations": len(hits)})
    return {"notes": notes_total, "relations": relations_total, "characters": rows}


def character_refs(store: ProjectStore, character_id: str) -> dict:
    """汇总某个角色被哪些地方引用（删除前的影响面清单）。

    统计口径覆盖全部真实引用来源：大纲 POV / 节拍选角 / 角色弧光、章节场景出场 /
    台词说话人 / 章节音色覆盖、其它角色的关系行与关系备注。
    删除保护与页面提示都走这一份口径，避免「哪里算引用」出现两套答案。

    参数:
        store: 工程。
        character_id: 角色 ID。

    返回:
        `{total, lines_total, chapters, relations, outline, chapter_refs, sources, settings_available}`；
        `sources` 每项含 `{key, label, count, where}`，直接用于删除确认框的「多少处 + 来自哪里」。

    异常:
        PreconditionError: 角色不存在。
    """
    table = store.load_characters()
    if not any(item.get("id") == character_id for item in table.get("characters") or []):
        raise PreconditionError(f"角色不存在: {character_id}")

    bible = SettingsStore(store.root).load_or_none() or {}
    outline = outline_references(bible, character_id)
    chapters = chapter_references(store, character_id)
    relations = relation_references(table, character_id)

    # 大纲章与章节文件按章节 ID 合并，页面只读一份「每章多少处引用」
    merged: dict[str, dict] = {}
    for row in outline["chapters"]:
        key = str(row.get("id") or f"outline:{row.get('index')}:{row.get('title')}")
        merged[key] = {
            "id": row.get("id"),
            "index": row.get("index"),
            "title": row.get("title"),
            "lines": [],
            "line_count": 0,
            "scenes": 0,
            "pov": row["pov"],
            "beats": row["beats"],
            "arcs": row["arcs"],
            "overrides": 0,
        }
    for row in chapters["chapters"]:
        target = merged.setdefault(
            str(row.get("id")),
            {
                "id": row.get("id"),
                "index": row.get("index"),
                "title": row.get("title"),
                "lines": [],
                "line_count": 0,
                "scenes": 0,
                "pov": 0,
                "beats": 0,
                "arcs": 0,
                "overrides": 0,
            },
        )
        target.update(
            {
                "title": target.get("title") or row.get("title"),
                "index": target.get("index") if target.get("index") is not None else row.get("index"),
                "lines": row["lines"],
                "line_count": row["line_count"],
                "scenes": row["scenes"],
                "overrides": row["overrides"],
            }
        )
    chapter_rows = sorted(merged.values(), key=lambda item: int(item.get("index") or 0))

    # 每个来源给出「多少处 + 来自哪里」，删除确认框直接照这份清单展示
    sources = [
        {"key": "pov", "label": REFERENCE_LABELS["pov"], "count": outline["pov"], "where": _chapter_names(chapter_rows, "pov")},
        {
            "key": "beats",
            "label": REFERENCE_LABELS["beats"],
            "count": outline["beats"],
            "where": _chapter_names(chapter_rows, "beats"),
        },
        {"key": "arcs", "label": REFERENCE_LABELS["arcs"], "count": outline["arcs"], "where": _chapter_names(chapter_rows, "arcs")},
        {
            "key": "scenes",
            "label": REFERENCE_LABELS["scenes"],
            "count": chapters["scenes"],
            "where": _chapter_names(chapter_rows, "scenes"),
        },
        {
            "key": "lines",
            "label": REFERENCE_LABELS["lines"],
            "count": chapters["lines"],
            "where": _chapter_names(chapter_rows, "lines"),
        },
        {
            "key": "overrides",
            "label": REFERENCE_LABELS["overrides"],
            "count": chapters["overrides"],
            "where": _chapter_names(chapter_rows, "overrides"),
        },
        {
            "key": "relations",
            "label": REFERENCE_LABELS["relations"],
            "count": relations["notes"] + relations["relations"],
            "where": "、".join(item.get("name") or item.get("id") or "" for item in relations["characters"]),
        },
    ]
    sources = [item for item in sources if item["count"]]
    total = sum(item["count"] for item in sources)
    return {
        "total": total,
        "lines_total": chapters["lines"],
        "chapters": chapter_rows,
        "relations": relations["characters"],
        "outline": {"pov": outline["pov"], "beats": outline["beats"], "arcs": outline["arcs"]},
        "chapter_refs": {"scenes": chapters["scenes"], "lines": chapters["lines"], "overrides": chapters["overrides"]},
        "sources": sources,
        "settings_available": True,
    }


def _clean_character_references(store: ProjectStore, character_id: str) -> dict:
    """删除角色前清理全部引用，避免留下悬空 ID（仅供 `force` 删除使用）。

    处理范围与 `character_refs` 的统计口径一致：
    - 大纲：清空视角、从节拍选角里移除、删除该角色的弧光行；
    - 章节：从场景出场里移除、把该角色的台词说话人改为「未知角色」、删除章节音色覆盖；
    - 角色表：删除其它角色指向它的关系行与关系备注。

    参数:
        store: 工程。
        character_id: 被删除的角色 ID。

    返回:
        各来源实际清理数量。
    """
    removed = {"pov": 0, "beats": 0, "arcs": 0, "scenes": 0, "lines": 0, "overrides": 0, "relations": 0}

    settings = SettingsStore(store.root)
    bible = settings.load_or_none()
    if bible:
        changed = False
        for act in (bible.get("outline") or {}).get("acts") or []:
            for chapter in act.get("chapters") or []:
                if chapter.get("pov_character_id") == character_id:
                    chapter["pov_character_id"] = None
                    removed["pov"] += 1
                    changed = True
                beats = chapter.get("beats") or []
                for beat in beats:
                    ids = beat.get("character_ids") or []
                    if character_id in ids:
                        # 节拍本身（梗概）保留，只把该角色从选角里摘掉
                        beat["character_ids"] = [item for item in ids if item != character_id]
                        removed["beats"] += 1
                        changed = True
                arcs = chapter.get("character_arcs") or []
                kept_arcs = [row for row in arcs if row.get("character_id") != character_id]
                if len(kept_arcs) != len(arcs):
                    removed["arcs"] += len(arcs) - len(kept_arcs)
                    if kept_arcs:
                        chapter["character_arcs"] = kept_arcs
                    else:
                        chapter.pop("character_arcs", None)
                    changed = True
        if changed:
            settings.save(bible)

    for meta in store.list_chapters():
        try:
            chapter = store.load_chapter(meta["id"])
        except (OSError, ValueError):
            continue
        changed = False
        for scene in chapter.get("scenes") or []:
            ids = scene.get("character_ids") or []
            if character_id in ids:
                scene["character_ids"] = [item for item in ids if item != character_id]
                removed["scenes"] += 1
                changed = True
            for line in scene.get("lines") or []:
                if line.get("speaker_id") == character_id:
                    # 台词本身保留，说话人退化为「未知角色」，界面会提示重新识别
                    line["speaker_id"] = entities.UNKNOWN_SPEAKER_ID
                    removed["lines"] += 1
                    changed = True
        overrides = chapter.get("voice_overrides") or {}
        if character_id in overrides:
            overrides.pop(character_id, None)
            if overrides:
                chapter["voice_overrides"] = overrides
            else:
                chapter.pop("voice_overrides", None)
            removed["overrides"] += 1
            changed = True
        if changed:
            store.save_chapter(chapter)

    table = store.load_characters()
    relations_changed = False
    for item in table.get("characters") or []:
        if str(item.get("id")) == character_id:
            continue
        state = item.get("state") or {}
        notes = state.get("relationship_notes") or {}
        if character_id in notes:
            notes.pop(character_id, None)
            removed["relations"] += 1
            relations_changed = True
        rows = state.get("relations") or []
        kept = [row for row in rows if str(row.get("target_id")) != character_id]
        if len(kept) != len(rows):
            removed["relations"] += len(rows) - len(kept)
            state["relations"] = kept or None
            relations_changed = True
    if relations_changed:
        store.save_characters(table)
    return removed


def _reference_summary(refs: dict) -> str:
    """把引用清单压成一行中文摘要（拦截提示用）。

    参数:
        refs: `character_refs` 的返回值。

    返回:
        形如「大纲视角（POV） 2 章、大纲节拍选角 34 个、台词说话人 34 行」的文本。
    """
    return "、".join(
        f"{item['label']} {item['count']} {_REFERENCE_UNITS.get(item['key'], '处')}" for item in refs.get("sources") or []
    )


def remove_character(store: ProjectStore, character_id: str, *, force: bool = False) -> dict:
    """按 ID 删除角色（旁白不可删除；默认拦截仍被引用的角色）。

    参数:
        store: 工程。
        character_id: 角色 ID。
        force: 用户已明确确认删除时置 True：先清理全部引用，再删除角色。

    返回:
        `{"removed", "characters", "references", "cleaned"}`。

    异常:
        ValueError: 试图删除旁白。
        PreconditionError: 角色不存在，或仍被引用且未确认（消息里带影响面清单）。
    """
    if character_id == entities.NARRATOR_ID:
        raise ValueError("旁白是保留角色，不能删除")
    table = store.load_characters()
    target = next((item for item in table.get("characters") or [] if item.get("id") == character_id), None)
    if target is None:
        raise PreconditionError(f"角色不存在: {character_id}")

    refs = character_refs(store, character_id)
    cleaned = {"pov": 0, "beats": 0, "arcs": 0, "scenes": 0, "lines": 0, "overrides": 0, "relations": 0}
    if refs["total"] and not force:
        raise PreconditionError(
            f"角色「{target.get('name') or character_id}」仍被引用，共 {refs['total']} 处：{_reference_summary(refs)}。"
            "请先改派台词、调整大纲视角/节拍选角、清理其它角色的关系；"
            "若确认要删除并同时清理这些引用，请加 --force（界面会二次确认）。"
        )
    if refs["total"]:
        cleaned = _clean_character_references(store, character_id)
        table = store.load_characters()
    # 删除中间角色会让后面的角色下标整体前移：把旧文档也去掉同一角色，
    # 历史非法枚举值才会按「仍是同一个人」放行，而不是被误判成本次新写入
    baseline = json.loads(json.dumps(table))
    baseline["characters"] = [item for item in baseline["characters"] if item.get("id") != character_id]
    table["characters"] = [item for item in table["characters"] if item.get("id") != character_id]
    store.save_characters(table, baseline=baseline)
    return {
        "removed": character_id,
        "characters": len(table["characters"]),
        "references": refs["total"],
        "cleaned": cleaned,
    }


def character_sheet(
    store: ProjectStore, config, character_id: str, kind: str, force: bool = False
) -> dict:
    """生成角色特写 / 三视图并写回 image_assets。

    参数:
        store: 工程。
        config: 本机配置。
        character_id: 角色 ID。
        kind: `fullbody` / `closeup` / `turnaround`。
        force: 已存在时也重新生成。

    返回:
        `render_character_sheet` 的结构化结果（含 `created` 与 `image_asset`）。

    异常:
        PreconditionError: 角色不存在。
    """
    table = store.load_characters()
    if not any(item.get("id") == character_id for item in table["characters"]):
        raise PreconditionError(f"角色不存在: {character_id}")
    return render_character_sheet(store, config, character_id, kind, force=force)
