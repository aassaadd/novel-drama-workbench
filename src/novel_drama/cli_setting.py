"""设定库 CLI：`ndw setting ...`。

设定库是世界观、大纲、文风的唯一事实来源；本模块只做展示与人工编辑，
自动生成在 `ndw outline generate` 与 `ndw chapter generate`。
"""

from __future__ import annotations

import json
from pathlib import Path

from .cli_common import _emit, _resolve_store
from .config import AppConfig
from .store.settings_store import SettingsStore

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
