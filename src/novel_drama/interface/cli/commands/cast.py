"""全剧音色编排 CLI（R18）：`ndw cast check / backfill / rename-slot`。

- `check`：跑一致性校验，error 时退出码 4，供配音前置门禁与人工巡检复用。
- `backfill`：给存量工程补音色描述 / 龙套槽位，默认 dry-run，只写描述不动绑定。
- `rename-slot`：龙套职业槽位改名，同时回写 `tts.cast.stock_defaults` 的键。
"""

from __future__ import annotations

import argparse
import json
import re

from novel_drama.application.cast.cast_backfill import backfill_cast
from novel_drama.application.production.lines_source import chapter_views
from novel_drama.composition import check_cast
from novel_drama.infrastructure.config import AppConfig

from ..common import EXIT_OK, EXIT_PRECONDITION, EXIT_REJECTED, _emit, _resolve_store


def _cmd_check(args, store) -> int:
    """执行一致性校验并按 error 设置退出码。"""
    chapter = getattr(args, "chapter", None)
    result = check_cast(store, chapter_views(store, [chapter] if chapter else None))
    if getattr(args, "json", False):
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        stats = result["stats"]
        print(
            f"章节 {stats['chapters']}，台词 {stats['lines']} 行，已解析 {stats['resolved']} 行；"
            f"结论：{result['level']}"
        )
        for item in result["findings"]:
            where = " ".join(
                str(item.get(key)) for key in ("chapter_id", "character_id", "line_id") if item.get(key)
            )
            print(f"[{item['level'].upper()}] {item['code']}: {item['message']}" + (f"（{where}）" if where else ""))
    return EXIT_PRECONDITION if result["level"] == "error" else EXIT_OK


def _cmd_backfill(args, store, config: AppConfig) -> int:
    """补音色描述与龙套槽位（默认 dry-run）。"""
    result = backfill_cast(
        store,
        config,
        apply=bool(getattr(args, "apply", False)),
        force=bool(getattr(args, "force", False)),
        profiles=bool(getattr(args, "profiles", False)),
        stock=bool(getattr(args, "stock", False)),
        all=bool(getattr(args, "all", False)),
    )
    apply = result["applied"]
    json_mode = bool(getattr(args, "json", False))
    for row in result["profiles"]:
        if not apply and not json_mode:
            print(f"[dry-run] {row['name']}: {row['design_prompt']}")
        # --json 时告警随 JSON 一起返回，不能混进 stdout 把 JSON 打坏
        if row["note"] and not json_mode:
            print(f"[警告] {row['name']}：{row['note']}")
    if not json_mode:
        for row in result["stock"]:
            print(f"[{'apply' if apply else 'dry-run'}] 龙套槽位: {row['name']} → {row['slot']}")

    if json_mode:
        print(
            json.dumps(
                {"changed": result["changed"], "applied": apply, "warnings": result["warnings"]},
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(("已写入" if apply else "[dry-run] 待写入") + f" {result['changed']} 个角色")
    return EXIT_OK


def _cmd_rename_slot(args, store) -> int:
    """龙套职业槽位改名，并同步 stock_defaults 的键。"""
    old, new = args.old, args.new
    if not re.match(r"^[a-z][a-z0-9_]{1,39}$", new):
        raise SystemExit("新槽位必须是 2~40 位小写英文/数字/下划线")
    table = store.load_characters()
    hit = 0
    for character in table.get("characters") or []:
        stock = character.get("stock") or {}
        if stock.get("slot") == old:
            stock["slot"] = new
            character["stock"] = stock
            hit += 1
    project = store.load_project()
    cast = ((project.get("settings") or {}).get("tts") or {}).get("cast") or {}
    defaults = cast.get("stock_defaults") or {}
    if old in defaults:
        defaults[new] = defaults.pop(old)
        cast["stock_defaults"] = defaults
        project["settings"]["tts"]["cast"] = cast
    if hit:
        store.save_characters(table)
    store.save_project(project)
    _emit(args, f"已把槽位 {old} 改为 {new}（影响 {hit} 个角色）", {"changed": hit, "from": old, "to": new})
    return EXIT_OK


def _cmd_fill_descriptions(args, store, config: AppConfig) -> int:
    """补全角色缺失的音色/外貌中英文描述（只补缺失，不覆盖已有内容）。

    参数:
        args: 已解析参数（names 来自可重复的 --name）。
        store: 工程。
        config: 本机配置。

    返回:
        进程退出码：全部成功为 0，存在失败为 1，便于重试时判断。
    """
    from novel_drama.application.cast.character_plan import fill_character_descriptions

    result = fill_character_descriptions(store, config, names=getattr(args, "name", None))
    if getattr(args, "json", False):
        payload = {key: result[key] for key in ("total", "updated", "skipped", "failures", "summary")}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for name in result["updated"]:
            print(f"[补全] {name}")
        for item in result["failures"]:
            print(f"[失败] {item['name']}：{item['message']}")
        print(result["summary"])
        if result["failures"]:
            print("可重跑 `ndw cast fill-descriptions` 只补失败的条目（已齐全的角色会自动跳过）")
    return EXIT_REJECTED if result["failures"] else EXIT_OK


def cmd_cast(args, config: AppConfig) -> int:
    """`ndw cast` 总入口。

    参数:
        args: 已解析参数（action=check|backfill|rename-slot|fill-descriptions）。
        config: 本机配置。

    返回:
        进程退出码。
    """
    store = _resolve_store(args, config)
    if args.action == "check":
        return _cmd_check(args, store)
    if args.action == "backfill":
        return _cmd_backfill(args, store, config)
    if args.action == "rename-slot":
        return _cmd_rename_slot(args, store)
    if args.action == "fill-descriptions":
        return _cmd_fill_descriptions(args, store, config)
    raise SystemExit(f"未知操作: {args.action}")


def add_parser(sub) -> None:
    """注册 `cast` 命令组。"""
    p = sub.add_parser("cast", help="全剧音色编排（校验 / 回填 / 槽位改名）")
    p_sub = p.add_subparsers(dest="action", required=True)
    q = p_sub.add_parser("check", help="跑全剧音色与 BGM 一致性校验")
    q.add_argument("--chapter", help="只校验该章节")
    q.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    q.set_defaults(func=cmd_cast)
    q = p_sub.add_parser("backfill", help="补音色描述 / 龙套槽位（默认 dry-run）")
    q.add_argument("--profiles", action="store_true", help="补 character.voice_profile")
    q.add_argument("--stock", action="store_true", help="给 role=minor 角色补 stock 槽位")
    q.add_argument("--all", action="store_true", help="即使已有描述也重新生成")
    q.add_argument("--force", action="store_true", help="覆盖手工改过的描述")
    q.add_argument("--apply", action="store_true", help="真正写盘；缺省只预览")
    q.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    q.set_defaults(func=cmd_cast)
    q = p_sub.add_parser("rename-slot", help="龙套职业槽位改名（同步 stock_defaults 键）")
    q.add_argument("--old", required=True)
    q.add_argument("--new", required=True)
    q.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    q.set_defaults(func=cmd_cast)
    q = p_sub.add_parser("fill-descriptions", help="补全角色缺失的音色/外貌中英文描述（不覆盖已有内容）")
    q.add_argument("--name", action="append", help="只处理指定角色（ID 或姓名），可重复")
    q.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    q.set_defaults(func=cmd_cast)
