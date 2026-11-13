"""全剧音色编排 CLI（R18）：`ndw cast check / backfill / rename-slot`。

- `check`：跑一致性校验，error 时退出码 4，供配音前置门禁与人工巡检复用。
- `backfill`：给存量工程补音色描述 / 龙套槽位，默认 dry-run，只写描述不动绑定。
- `rename-slot`：龙套职业槽位改名，同时回写 `tts.cast.stock_defaults` 的键。
"""

from __future__ import annotations

import hashlib
import json
import re

from .cli_common import EXIT_OK, EXIT_PRECONDITION, EXIT_REJECTED, _emit, _resolve_store
from .config import AppConfig
from .models.ids import now_iso
from .store import check_cast, profile_source_hash
from .store.settings_store import SettingsStore


def _slug(text: str) -> str:
    """把中文职业名转成稳定的英文风格槽位键（中文按 hash 后缀兜底）。"""
    cleaned = re.sub(r"[^a-z0-9_]+", "_", (text or "").lower()).strip("_")
    if cleaned:
        return cleaned[:40]
    return "minor_" + hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:8]


def _gender_voice_slot(gender: str | None, age_range: str | None) -> str:
    """按性别与年龄感给出默认音色池槽位。"""
    age = str(age_range or "")
    elder = any(word in age for word in ("60", "70", "80", "老"))
    young = any(word in age for word in ("16", "17", "18", "19", "20", "21", "22", "少年", "少女"))
    if gender == "female":
        return "female_elder" if elder else ("female_young" if young else "female_mid")
    if gender == "male":
        return "male_elder" if elder else ("male_young" if young else "male_mid")
    return "neutral_mid"


def _cmd_check(args, store) -> int:
    """执行一致性校验并按 error 设置退出码。"""
    result = check_cast(store, getattr(args, "chapter", None))
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
    table = store.load_characters()
    apply = bool(getattr(args, "apply", False))
    force = bool(getattr(args, "force", False))
    want_profiles = bool(getattr(args, "profiles", False)) or not getattr(args, "stock", False)
    want_stock = bool(getattr(args, "stock", False))
    changed = 0

    if want_profiles:
        from .tts.voice_design import build_profile
        from .writing.service import build_writing_adapter

        adapter = build_writing_adapter(config, SettingsStore(store.root).load_or_none() or {})
        for character in table.get("characters") or []:
            profile = character.get("voice_profile") or {}
            # status=draft 表示上次只回落到规则模板，默认允许重试补齐，不要求用户加 --force
            if profile and profile.get("status") != "draft" and not (force or getattr(args, "all", False)):
                continue
            if (profile.get("source") or {}).get("override_fields") and not force:
                continue
            built = build_profile(adapter, character)
            new_profile = dict(built["profile"])
            source = {
                "kind": built.get("source") or "rule",
                "at": now_iso(),
                "from_hash": profile_source_hash(character),
            }
            if built.get("note"):
                # 回落原因写进来源，页面与后续 cast check 可据此解释 draft
                source["note"] = str(built["note"])[:200]
            new_profile.update(
                {
                    "schema": "voice_profile/1",
                    "summary": new_profile.get("voice_summary"),
                    "source": source,
                    "status": "ready" if built.get("source") == "llm" else "draft",
                }
            )
            character["voice_profile"] = new_profile
            changed += 1
            if not apply and not getattr(args, "json", False):
                print(f"[dry-run] {character.get('name')}: {(new_profile.get('design_prompt') or '')[:80]}")
            if built.get("note"):
                print(f"[警告] {character.get('name')}：{built['note']}")

    if want_stock:
        for index, character in enumerate(table.get("characters") or []):
            if character.get("role") != "minor" or (character.get("stock") or {}).get("slot"):
                continue
            label = re.sub(r"^龙套[·.・]\s*", "", character.get("name") or "") or f"龙套{index}"
            character["stock"] = {
                "slot": _slug(label) or f"minor_{index:02d}",
                "voice_slot": _gender_voice_slot(character.get("gender"), character.get("age_range")),
                "label": label[:40],
                "archetype_tags": list(character.get("personality") or [])[:3],
                "reuse_policy": "reuse",
            }
            changed += 1
            if not getattr(args, "json", False):
                print(f"[{'apply' if apply else 'dry-run'}] 龙套槽位: {character.get('name')} → {character['stock']['slot']}")

    if apply and changed:
        store.save_characters(table)
    if getattr(args, "json", False):
        print(json.dumps({"changed": changed, "applied": apply}, ensure_ascii=False, indent=2))
    else:
        print(("已写入" if apply else "[dry-run] 待写入") + f" {changed} 个角色")
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
    from .writing.character_plan import fill_character_descriptions

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
