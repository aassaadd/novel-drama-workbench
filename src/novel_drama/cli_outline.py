"""大纲检查与绑定 CLI：`ndw outline check` / `ndw outline bind`。"""

from __future__ import annotations

import re

from .cli_common import EXIT_FAILED, _emit, _resolve_store
from .config import AppConfig
from .store.settings_store import SettingsStore
from .store.world_store import WorldStore
from .writing import check_golden_three


def cmd_outline_check(args, config: AppConfig) -> int:
    """检查大纲前三章是否满足「黄金三章」要求。

    参数:
        args: 已解析参数（json）。
        config: 本机配置。

    返回:
        进程退出码（检查结果放在 passed 字段里，不阻断流程）。
    """
    store = _resolve_store(args, config)
    bible = SettingsStore(store.root).ensure()
    result = check_golden_three(bible)
    if result["passed"]:
        text = "黄金三章检查通过"
    else:
        lines = [f"黄金三章检查未通过：{len(result['issues'])} 项"]
        for issue in result["issues"]:
            lines.append(f"  第 {issue['chapter']} 章：{issue['problem']}——{issue['hint']}")
        text = "\n".join(lines)
    _emit(args, text, {**result, "settings_revision": bible.get("settings_revision")})
    return 0


def _split_ids(raw: str) -> list[str]:
    """把逗号/顿号分隔的 ID 串拆成去空列表。

    参数:
        raw: 命令行原始文本；空串表示清空绑定。

    返回:
        去空白后的 ID 列表。
    """
    return [part.strip() for part in re.split(r"[、,，;；\s]+", raw or "") if part.strip()]


def cmd_outline_bind(args, config: AppConfig) -> int:
    """按参数更新某一章的 world_ids / pov_character_id。

    参数:
        args: 已解析参数（chapter / worlds / pov / json）。
        config: 本机配置。

    返回:
        进程退出码（世界或角色 ID 不存在时返回 EXIT_FAILED）。

    异常:
        无：错误以返回值 + 中文提示表达。
    """
    store = _resolve_store(args, config)
    settings = SettingsStore(store.root)
    bible = settings.ensure()
    chapter = settings.find_outline_chapter(bible, args.chapter)
    if chapter is None:
        _emit(
            args,
            f"大纲章节不存在: {args.chapter}（先用 ndw outline generate / apply 生成大纲）",
            {"chapter": args.chapter, "message": "大纲章节不存在"},
        )
        return EXIT_FAILED

    raw_worlds = getattr(args, "worlds", None)
    raw_pov = getattr(args, "pov", None)
    if raw_worlds is None and raw_pov is None:
        _emit(args, "至少给 --worlds 或 --pov 之一", {"chapter": args.chapter, "message": "缺少绑定参数"})
        return 2

    if raw_worlds is not None:
        world_ids = _split_ids(raw_worlds)
        if world_ids:
            known = {item.get("id") for item in WorldStore(store.root).ensure().get("worlds") or []}
            missing = [world_id for world_id in world_ids if world_id not in known]
            if missing:
                message = f"世界条目不存在：{'、'.join(missing)}；请先在世界页生成世界"
                _emit(args, message, {"chapter": args.chapter, "message": message})
                return EXIT_FAILED
        chapter["world_ids"] = world_ids

    if raw_pov is not None:
        pov = raw_pov.strip() or None
        if pov is not None:
            cast_ids = {item.get("id") for item in store.load_characters()["characters"]}
            if pov not in cast_ids:
                message = f"角色不存在：{pov}；请先在人物页生成人物"
                _emit(args, message, {"chapter": args.chapter, "message": message})
                return EXIT_FAILED
        chapter["pov_character_id"] = pov

    bible = settings.save(bible)
    payload = {
        "chapter": args.chapter,
        "world_ids": chapter.get("world_ids") or [],
        "pov_character_id": chapter.get("pov_character_id"),
        "settings_revision": bible.get("settings_revision"),
    }
    _emit(
        args,
        f"已更新章节绑定 {args.chapter}：世界 {len(payload['world_ids'])} 个，POV {payload['pov_character_id'] or '未设置'}",
        payload,
    )
    return 0
