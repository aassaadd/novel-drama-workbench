"""设定库 CLI：`ndw setting ...`。

设定库存放故事层设定（故事内核、文风、大纲、连续性）；世界观与分类条目统一归
世界库（`ndw world ...`）。本模块只做展示与人工编辑，自动生成在
`ndw outline generate`、`ndw setting generate` 与 `ndw chapter generate`。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .adapters.persistence.settings_store import SettingsStore
from .application.story.queries import settings_view
from .application.story.settings_edit import (
    SCALAR_FIELDS,
    apply_section,
    set_scalars,
)
from .application.story.settings_edit import (
    apply_polished as _apply_polished,
)
from .application.story.settings_edit import (
    merge_section as _merge_section,
)
from .application.story.settings_edit import (
    read_field_value as _read_field_value,
)
from .application.story.settings_generate import (
    generate_section,
    generate_settings,
    polish_field,
)
from .cli_common import _emit, _resolve_store
from .infrastructure.config import AppConfig


def _fmt_bible(bible: dict) -> str:
    """把设定库转成便于终端阅读的摘要。

    参数:
        bible: 设定库 dict。

    返回:
        多行文本摘要。
    """
    premise = bible.get("premise") or {}
    style = bible.get("style") or {}
    acts = (bible.get("outline") or {}).get("acts") or []
    chapters = sum(len(act.get("chapters") or []) for act in acts)
    beats = sum(len(item.get("beats") or []) for act in acts for item in act.get("chapters") or [])
    lines = [
        f"设定库 r{bible.get('settings_revision')}（{bible.get('updated_at')}）",
        f"  一句话：{premise.get('logline') or '—'}",
        f"  主题：{premise.get('theme') or '—'}　核心冲突：{premise.get('core_conflict') or '—'}",
        f"  文风：{style.get('tone') or '—'}　视角：{style.get('pov') or '—'}　时态：{style.get('tense') or '—'}",
        f"  大纲：{len(acts)} 幕 / {chapters} 章 / {beats} 个节拍",
    ]
    return "\n".join(lines)


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
        bible = settings_view(store)
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
        payload = set_scalars(store, {flag: getattr(args, flag, None) for flag in SCALAR_FIELDS})
        _emit(
            args,
            f"已更新设定库 r{payload['settings_revision']}：" + "、".join(payload["changed"]),
            payload,
        )
        return 0

    if action == "generate":
        section = str(getattr(args, "section", None) or "").strip()
        if section:
            result = generate_section(
                store,
                config,
                section,
                brief=getattr(args, "brief", None),
                target_id=getattr(args, "target_id", None),
                seed=getattr(args, "seed", None),
                overwrite=not bool(getattr(args, "keep_existing", False)),
            )
            fields = result["fields"] or {}
            head = "[dry-run] " if getattr(args, "dry_run", False) else ""
            kept = result.get("skipped") or []
            text = f"{head}已生成 {section} 区块：{len(fields)} 个字段"
            if kept:
                text += f"（保留已有：{'、'.join(kept)}）"
            _emit(
                args,
                text,
                {
                    "section": result["section"],
                    "fields": fields,
                    "skipped": kept,
                    "overwrite": not bool(getattr(args, "keep_existing", False)),
                },
            )
            return 0

        step = getattr(args, "step", None)
        if not step:
            raise ValueError("需要指定 --section（premise/style/character）或 --step（premise/style）")
        result = generate_settings(
            store,
            config,
            step,
            overwrite=bool(getattr(args, "overwrite", False)),
            brief=getattr(args, "brief", None),
            seed=getattr(args, "seed", None),
        )
        label = {"premise": "故事梗概", "style": "文风"}[step]
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
        payload = apply_section(store, args.section, fields)
        _emit(
            args,
            f"已写入 {args.section}（{len(payload['changed'])} 个字段）r{payload['settings_revision']}",
            {key: payload[key] for key in ("section", "changed", "settings_revision")},
        )
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
