"""设定库 CLI：`ndw setting ...`。

设定库是世界观、大纲、文风的唯一事实来源；本模块只做展示与人工编辑，
自动生成在 `ndw outline generate`、`ndw setting generate` 与 `ndw chapter generate`。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from .cli_common import PreconditionError, _emit, _resolve_store
from .config import AppConfig
from .models.ids import new_id
from .store.settings_store import SettingsStore
from .writing import generate_settings, polish_field

# `setting set` 支持标量字段：解析参数名 → (设定库路径, 字段名)
SCALAR_FIELDS = {
    "logline": ("premise", "logline"),
    "theme": ("premise", "theme"),
    "core_conflict": ("premise", "core_conflict"),
    "ending_intent": ("premise", "ending_intent"),
    "target_audience": ("premise", "target_audience"),
    "target_length": ("premise", "target_length"),
    "era": ("world", "era"),
    "tone": ("style", "tone"),
    "pov": ("style", "pov"),
    "tense": ("style", "tense"),
    "prose_style": ("style", "prose_style"),
    "visual_style": ("style", "visual_style"),
}

# `setting apply` / `setting generate` 各分区允许写入的字段
SECTION_FIELDS = {
    "world": ("era", "rules", "factions", "locations", "glossary"),
    "premise": ("logline", "theme", "core_conflict", "ending_intent", "target_audience", "target_length"),
    "style": ("tone", "pov", "tense", "prose_style", "forbidden", "visual_style"),
}


def _as_str_list(rows) -> list[str]:
    """把字符串或数组统一成去空字符串列表。

    参数:
        rows: 字符串、数组或 None。

    返回:
        去空白后的字符串列表。
    """
    if rows is None:
        return []
    if isinstance(rows, str):
        rows = [rows]
    if not isinstance(rows, list):
        raise ValueError("该字段必须是字符串数组")
    return [str(item).strip() for item in rows if str(item).strip()]


def _split_items(text: str) -> list[str]:
    """按常见分隔符把一段文本拆成列表。

    参数:
        text: 如「冷静、克制，敏锐」的文本。

    返回:
        去空后的条目列表。
    """
    return [part.strip() for part in re.split(r"[、,，;；\n]+", text or "") if part.strip()]


def _parse_pipe(raw: str) -> list[str]:
    """把 `a|b|c` 形式的文本拆成去空列表。

    参数:
        raw: 竖线分隔的原始文本。

    返回:
        去空白后的分段列表。
    """
    return [part.strip() for part in (raw or "").split("|")]


def _merge_section(bible: dict, section: str, fields: dict) -> list[str]:
    """把字段合并进设定库对应分区（地点/词条自动补 id 与 aliases）。

    参数:
        bible: 设定库 dict（就地修改）。
        section: world / premise / style。
        fields: 待写入字段。

    返回:
        实际写入的字段名列表。

    异常:
        ValueError: 分区非法、fields 不是对象或没有可写入字段。
    """
    if section not in SECTION_FIELDS:
        raise ValueError(f"未知设定分区: {section}（可选 world / premise / style）")
    if not isinstance(fields, dict):
        raise ValueError("fields 必须是 JSON 对象")
    target = bible.setdefault(section, {})
    changed: list[str] = []

    if section == "world":
        if "era" in fields:
            target["era"] = str(fields.get("era") or "").strip()
            changed.append("era")
        for key in ("rules", "factions"):
            if key in fields:
                target[key] = _as_str_list(fields.get(key))
                changed.append(key)
        if "locations" in fields:
            rows = fields.get("locations") or []
            if not isinstance(rows, list):
                raise ValueError("locations 必须是数组")
            locations = []
            for index, item in enumerate(rows):
                if not isinstance(item, dict):
                    raise ValueError(f"locations 第 {index + 1} 项必须是对象")
                locations.append(
                    {
                        "id": str(item.get("id") or new_id("loc")),
                        "name": str(item.get("name") or "").strip(),
                        "description": str(item.get("description") or "").strip(),
                        "atmosphere": str(item.get("atmosphere") or "").strip(),
                    }
                )
            target["locations"] = locations
            changed.append("locations")
        if "glossary" in fields:
            rows = fields.get("glossary") or []
            if not isinstance(rows, list):
                raise ValueError("glossary 必须是数组")
            glossary = []
            for index, item in enumerate(rows):
                if not isinstance(item, dict):
                    raise ValueError(f"glossary 第 {index + 1} 项必须是对象")
                glossary.append(
                    {
                        "term": str(item.get("term") or "").strip(),
                        "meaning": str(item.get("meaning") or "").strip(),
                        "aliases": _as_str_list(item.get("aliases")),
                    }
                )
            target["glossary"] = glossary
            changed.append("glossary")
    else:
        for key in SECTION_FIELDS[section]:
            if key not in fields:
                continue
            value = fields.get(key)
            target[key] = _as_str_list(value) if key == "forbidden" else str(value or "").strip()
            changed.append(key)

    if not changed:
        raise ValueError(f"fields 里没有可写入 {section} 分区的字段")
    return changed


def _read_field_value(store, bible: dict, field: str, character_id: str | None = None) -> str:
    """读取待润色字段的当前值。

    参数:
        store: 工程。
        bible: 设定库 dict。
        field: 润色字段名。
        character_id: character.* 字段所属角色 ID。

    返回:
        字段当前文本（数组用「、」连接）。

    异常:
        ValueError: 列表型字段未指定具体条目，或缺少 character_id。
        PreconditionError: 角色不存在。
    """
    if field in SCALAR_FIELDS:
        section, key = SCALAR_FIELDS[field]
        return str((bible.get(section) or {}).get(key) or "")
    if field == "work.genre":
        work = store.load_project().get("work") or {}
        return "、".join(work.get("genre") or [])
    if field in ("world.rule", "world.faction", "world.location", "world.glossary"):
        raise ValueError("该字段是列表，请用 --text 指定要润色的条目")
    if field.startswith("character."):
        if not character_id:
            raise ValueError("润色人物字段需要用 --character 指定角色 ID")
        table = store.load_characters()
        target = next((item for item in table["characters"] if item.get("id") == character_id), None)
        if target is None:
            raise PreconditionError(f"角色不存在: {character_id}")
        if field == "character.personality":
            return "、".join(target.get("personality") or [])
        return str(target.get("background") or "")
    raise ValueError(f"不支持的润色字段: {field}")


def _apply_polished(
    store,
    settings: SettingsStore,
    bible: dict,
    field: str,
    source: str,
    polished: str,
    character_id: str | None = None,
) -> dict:
    """把润色结果写回设定库 / 人物表 / 工程元信息。

    参数:
        store: 工程。
        settings: 设定库读写入口。
        bible: 设定库 dict（就地修改）。
        field: 润色字段名。
        source: 润色前的原始文本（用于定位列表条目）。
        polished: 润色后的文本。
        character_id: character.* 字段所属角色 ID。

    返回:
        {"field", "applied": True, 附加的 settings_revision / character_id / genres}。

    异常:
        ValueError: 列表字段找不到对应条目。
        PreconditionError: 角色不存在。
    """
    if field == "work.genre":
        project = store.load_project()
        project["work"]["genre"] = _split_items(polished)
        store.save_project(project)
        return {"field": field, "applied": True, "genres": project["work"]["genre"]}

    if field.startswith("character."):
        if not character_id:
            raise ValueError("润色人物字段需要用 --character 指定角色 ID")
        table = store.load_characters()
        target = next((item for item in table["characters"] if item.get("id") == character_id), None)
        if target is None:
            raise PreconditionError(f"角色不存在: {character_id}")
        if field == "character.personality":
            target["personality"] = _split_items(polished)
        else:
            target["background"] = polished
        store.save_characters(table)
        return {"field": field, "applied": True, "character_id": character_id}

    if field in SCALAR_FIELDS:
        section, key = SCALAR_FIELDS[field]
        bible.setdefault(section, {})[key] = polished
        bible = settings.save(bible)
        return {"field": field, "applied": True, "settings_revision": bible["settings_revision"]}

    world = bible.setdefault("world", {})
    if field in ("world.rule", "world.faction"):
        key = "rules" if field == "world.rule" else "factions"
        rows = world.setdefault(key, [])
        if source in rows:
            rows[rows.index(source)] = polished
        else:
            rows.append(polished)
        bible = settings.save(bible)
        return {"field": field, "applied": True, "settings_revision": bible["settings_revision"]}

    if field == "world.location":
        rows = world.setdefault("locations", [])
        src_parts = _parse_pipe(source)
        out_parts = _parse_pipe(polished)
        match = next((item for item in rows if item.get("name") == (src_parts[0] if src_parts else "")), None)
        if match is None:
            match = next((item for item in rows if item.get("description") == source), None)
        if match is None:
            if len(out_parts) < 2:
                raise ValueError("找不到要更新的地点，请用 `名称|描述` 指定")
            rows.append({"id": new_id("loc"), "name": out_parts[0], "description": out_parts[1], "atmosphere": ""})
        else:
            match["name"] = out_parts[0] if out_parts else match.get("name")
            match["description"] = out_parts[1] if len(out_parts) > 1 else polished
            if len(out_parts) > 2:
                match["atmosphere"] = "|".join(out_parts[2:])
        bible = settings.save(bible)
        return {"field": field, "applied": True, "settings_revision": bible["settings_revision"]}

    if field == "world.glossary":
        rows = world.setdefault("glossary", [])
        src_parts = _parse_pipe(source)
        out_parts = _parse_pipe(polished)
        match = next((item for item in rows if item.get("term") == (src_parts[0] if src_parts else "")), None)
        if match is None:
            match = next((item for item in rows if item.get("meaning") == source), None)
        if match is None:
            if len(out_parts) < 2:
                raise ValueError("找不到要更新的词条，请用 `术语|释义` 指定")
            rows.append({"term": out_parts[0], "meaning": out_parts[1], "aliases": []})
        else:
            match["term"] = out_parts[0] if out_parts else match.get("term")
            match["meaning"] = out_parts[1] if len(out_parts) > 1 else polished
        bible = settings.save(bible)
        return {"field": field, "applied": True, "settings_revision": bible["settings_revision"]}

    raise ValueError(f"不支持的润色字段: {field}")


def _fmt_bible(bible: dict) -> str:
    """把设定库转成便于终端阅读的摘要。

    参数:
        bible: 设定库 dict。

    返回:
        多行文本摘要。
    """
    premise = bible.get("premise") or {}
    world = bible.get("world") or {}
    style = bible.get("style") or {}
    acts = (bible.get("outline") or {}).get("acts") or []
    chapters = sum(len(act.get("chapters") or []) for act in acts)
    beats = sum(len(item.get("beats") or []) for act in acts for item in act.get("chapters") or [])
    lines = [
        f"设定库 r{bible.get('settings_revision')}（{bible.get('updated_at')}）",
        f"  一句话：{premise.get('logline') or '—'}",
        f"  主题：{premise.get('theme') or '—'}　核心冲突：{premise.get('core_conflict') or '—'}",
        f"  世界：{world.get('era') or '—'}（地点 {len(world.get('locations') or [])} / 规则 {len(world.get('rules') or [])} / 阵营 {len(world.get('factions') or [])}）",
        f"  文风：{style.get('tone') or '—'}　视角：{style.get('pov') or '—'}　时态：{style.get('tense') or '—'}",
        f"  大纲：{len(acts)} 幕 / {chapters} 章 / {beats} 个节拍",
    ]
    return "\n".join(lines)


def _parse_triplet(raw: str) -> list[str]:
    """把 `a|b|c` 形式的参数拆成列表（允许少于三段）。

    参数:
        raw: 命令行原始字符串。

    返回:
        去空白后的字段列表。
    """
    return [part.strip() for part in raw.split("|")]


def cmd_setting(args, config: AppConfig) -> int:
    """设定库命令统一入口。

    参数:
        args: 已解析参数（action 决定分支）。
        config: 本机配置。

    返回:
        进程退出码。
    """
    store = _resolve_store(args, config)
    settings = SettingsStore(store.root)
    action = args.action

    if action == "init":
        existed = settings.exists()
        bible = settings.ensure()
        _emit(args, "设定库已就绪" if existed else f"已生成设定库: {settings.path}", bible)
        return 0

    if action == "show":
        bible = settings.ensure()
        section = getattr(args, "section", None)
        if section:
            payload = {"schema_version": bible.get("schema_version"), "settings_revision": bible.get("settings_revision"), "work_id": bible.get("work_id"), section: bible.get(section)}
            _emit(args, json.dumps(payload.get(section), ensure_ascii=False, indent=2), payload)
        else:
            _emit(args, _fmt_bible(bible), bible)
        return 0

    if action == "validate":
        bible = settings.ensure()
        settings.validate(bible)
        print(f"设定库通过校验（settings_revision={bible.get('settings_revision')}）")
        return 0

    if action == "set":
        bible = settings.ensure()
        changed = {}
        for flag, (section, field) in SCALAR_FIELDS.items():
            value = getattr(args, flag, None)
            if value is None:
                continue
            bible.setdefault(section, {})[field] = value
            changed[f"{section}.{field}"] = value
        if not changed:
            raise ValueError("没有要修改的字段：至少给一个（如 --logline / --era / --tone / --visual-style）")
        bible = settings.save(bible)
        _emit(args, f"已更新设定库 r{bible['settings_revision']}：" + "、".join(changed), {"changed": changed, "settings_revision": bible["settings_revision"]})
        return 0

    if action == "generate":
        step = args.step
        result = generate_settings(store, config, step, overwrite=bool(getattr(args, "overwrite", False)))
        label = {"world": "世界设定", "premise": "故事梗概", "style": "文风"}[step]
        applied = False
        revision = (settings.load_or_none() or {}).get("settings_revision")
        if not getattr(args, "dry_run", False):
            bible = settings.ensure()
            _merge_section(bible, step, result["fields"])
            bible = settings.save(bible)
            applied = True
            revision = bible["settings_revision"]
        text = f"{'[dry-run] ' if not applied else ''}{label}已生成：{len(result['fields'])} 个字段"
        if result["skipped"]:
            text += f"（保留已有：{'、'.join(result['skipped'])}）"
        _emit(args, text, {**result, "applied": applied, "settings_revision": revision})
        return 0

    if action == "polish":
        field = args.field
        character_id = getattr(args, "character", None)
        source_text = args.text
        if source_text is None:
            source_text = _read_field_value(store, settings.ensure(), field, character_id)
        result = polish_field(store, config, field, source_text, context=getattr(args, "context", None))
        payload = dict(result)
        applied = False
        if getattr(args, "apply", False):
            bible = settings.ensure()
            payload.update(_apply_polished(store, settings, bible, field, source_text, result["text"], character_id))
            applied = True
        payload["applied"] = applied
        head = "已写回" if applied else "润色结果"
        _emit(args, f"{head}（{field}）：\n{result['text']}", payload)
        return 0

    if action == "apply":
        raw = sys.stdin.read() if args.file == "-" else Path(args.file).expanduser().read_text(encoding="utf-8")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"JSON 解析失败: {exc}") from exc
        fields = data.get("fields") if isinstance(data, dict) and isinstance(data.get("fields"), dict) else data
        bible = settings.ensure()
        changed = _merge_section(bible, args.section, fields)
        bible = settings.save(bible)
        _emit(
            args,
            f"已写入 {args.section}（{len(changed)} 个字段）r{bible['settings_revision']}",
            {"section": args.section, "changed": changed, "settings_revision": bible["settings_revision"]},
        )
        return 0

    if action == "world":
        bible = settings.ensure()
        world = bible.setdefault("world", {})
        changed = []
        if getattr(args, "add_rule", None):
            # argparse 用 append，这里按条展开，schema 要求 rules 元素是字符串
            world.setdefault("rules", []).extend(args.add_rule)
            changed.append(f"规则×{len(args.add_rule)}")
        if getattr(args, "add_faction", None):
            world.setdefault("factions", []).extend(args.add_faction)
            changed.append(f"阵营×{len(args.add_faction)}")
        if getattr(args, "add_location", None):
            name, desc, atmosphere = (_parse_triplet(args.add_location) + ["", "", ""])[:3]
            from .models.ids import new_id

            world.setdefault("locations", []).append(
                {"id": new_id("loc"), "name": name, "description": desc, "atmosphere": atmosphere}
            )
            changed.append(f"地点 {name}")
        if getattr(args, "add_glossary", None):
            term, meaning = (_parse_triplet(args.add_glossary) + ["", ""])[:2]
            if not term or not meaning:
                raise ValueError("--add-glossary 需要 `术语|释义` 两段")
            world.setdefault("glossary", []).append({"term": term, "meaning": meaning, "aliases": []})
            changed.append(f"词条 {term}")
        if not changed:
            raise ValueError("没有要新增的内容：用 --add-rule / --add-faction / --add-location / --add-glossary")
        bible = settings.save(bible)
        _emit(args, f"已更新世界设定 r{bible['settings_revision']}：" + "、".join(changed), {"changed": changed, "settings_revision": bible["settings_revision"]})
        return 0

    if action == "import":
        source = Path(args.file).expanduser()
        if not source.exists():
            raise FileNotFoundError(f"文件不存在: {source}")
        bible = json.loads(source.read_text(encoding="utf-8"))
        work_id = store.load_project()["work"]["id"]
        if bible.get("work_id") != work_id:
            raise ValueError(f"work_id 不匹配：文件是 {bible.get('work_id')}，当前工程是 {work_id}")
        bible = settings.save(bible)
        _emit(args, f"已导入设定库 r{bible['settings_revision']}", {"settings_revision": bible["settings_revision"], "path": str(settings.path)})
        return 0

    if action == "export":
        bible = settings.ensure()
        target = Path(args.out).expanduser() if getattr(args, "out", None) else (store.root / "exports" / "setting-bible.json")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(bible, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        _emit(args, f"已导出: {target}", {"path": str(target), "settings_revision": bible.get("settings_revision")})
        return 0

    raise ValueError(f"未知 setting 动作: {action}")
