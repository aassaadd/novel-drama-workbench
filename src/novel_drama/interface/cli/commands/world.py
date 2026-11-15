"""世界模块 CLI：`ndw world list / generate / apply / remove / image`。

worlds.json 存一套套世界（世界观总述 + 地点/势力/术语/其它条目），也是章节绑定的
唯一来源；本模块只做查看、生成、导入、删除与封面生成，删除前会检查大纲章节引用。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from novel_drama.application.story.queries import world_list
from novel_drama.application.story.settings_generate import generate_worlds
from novel_drama.application.story.world_crud import remove_world, upsert_worlds
from novel_drama.composition import WorldStore, render_world_cover
from novel_drama.infrastructure.config import AppConfig

from ..common import EXIT_OK, PreconditionError, _emit, _resolve_store


def _read_rows(path: str) -> list[dict]:
    """读取 `--file` 里的世界条目 JSON，兼容三种结构。

    参数:
        path: JSON 文件路径，`-` 表示从标准输入读取。

    返回:
        世界条目 dict 列表。

    异常:
        ValueError: JSON 解析失败或结构不合法。
    """
    raw = sys.stdin.read() if path == "-" else Path(path).expanduser().read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON 解析失败: {exc}") from exc
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict) and isinstance(data.get("world"), dict):
        rows = [data["world"]]
    elif isinstance(data, dict) and isinstance(data.get("worlds"), list):
        rows = data["worlds"]
    else:
        raise ValueError("JSON 里没有 world 或 worlds 数组")
    if not rows:
        raise ValueError("世界条目不能为空")
    return rows


def _fmt_worlds(worlds: list[dict]) -> str:
    """把世界条目列表渲染成终端摘要。

    参数:
        worlds: 世界条目列表。

    返回:
        多行文本；列表为空时提示。
    """
    if not worlds:
        return "（世界条目为空）"
    return "\n".join(
        f"{item.get('id')}  {item.get('name') or '未命名'}（{item.get('era') or '未注明'}）"
        f"  地点 {len(item.get('locations') or [])} / 势力 {len(item.get('factions') or [])}"
        f" / 术语 {len(item.get('glossary') or [])} / 其它 {len(item.get('others') or [])}"
        for item in worlds
    )


def cmd_world_list(args, config: AppConfig) -> int:
    """列出全部世界模块（世界观总述 + 分类条目计数）。

    参数:
        args: 已解析参数（json）。
        config: 本机配置。

    返回:
        进程退出码。
    """
    store = _resolve_store(args, config)
    payload = world_list(store)
    _emit(args, _fmt_worlds(payload["worlds"]), payload)
    return EXIT_OK


def cmd_world_generate(args, config: AppConfig) -> int:
    """AI 生成世界条目，默认落盘追加（--overwrite 时整体替换）。

    参数:
        args: 已解析参数（count / brief / overwrite / dry-run / json）。
        config: 本机配置。

    返回:
        进程退出码。

    异常:
        ValueError: 数量越界或模型输出不合法。
    """
    store = _resolve_store(args, config)
    result = generate_worlds(
        store,
        config,
        count=getattr(args, "count", None),
        overwrite=bool(getattr(args, "overwrite", False)),
        brief=getattr(args, "brief", None),
    )
    generated = result["worlds"]
    if getattr(args, "dry_run", False):
        _emit(
            args,
            f"[dry-run] 已生成 {len(generated)} 条世界（未落盘）\n{_fmt_worlds(generated)}",
            {"worlds": generated, "count": len(generated), "saved": False},
        )
        return EXIT_OK

    worlds = WorldStore(store.root)
    worlds.ensure()
    if getattr(args, "overwrite", False):
        # 整体替换：先清空再逐条 add，保证每条的 id / 时间戳由同一入口补齐
        worlds.save({"schema_version": 1, "worlds": []})
    saved = [worlds.add(entry) for entry in generated]
    _emit(
        args,
        f"已写入 {len(saved)} 条世界\n{_fmt_worlds(saved)}",
        {"worlds": saved, "count": len(saved), "saved": True},
    )
    return EXIT_OK


def cmd_world_apply(args, config: AppConfig) -> int:
    """从 JSON 导入世界条目：带 id 且已存在则更新，否则新增。

    参数:
        args: 已解析参数（file / json）。
        config: 本机配置。

    返回:
        进程退出码。

    异常:
        ValueError: JSON 结构不合法或条目缺少必要字段。
    """
    store = _resolve_store(args, config)
    payload = upsert_worlds(store, _read_rows(args.file))
    _emit(args, f"已导入世界条目，当前共 {payload['count']} 条\n{_fmt_worlds(payload['worlds'])}", payload)
    return EXIT_OK


def cmd_world_remove(args, config: AppConfig) -> int:
    """删除整套世界（总述 + 地点/势力/术语/其它条目）并解除相关引用。

    参数:
        args: 已解析参数（id / json）。
        config: 本机配置。

    返回:
        进程退出码。
    """
    store = _resolve_store(args, config)
    payload = remove_world(store, args.id)
    message = f"已删除世界「{payload.get('name') or args.id}」及其全部条目"
    if payload["chapters"]:
        message += f"；已解除 {len(payload['chapters'])} 个大纲章节的世界绑定"
    _emit(args, message, payload)
    return EXIT_OK


def cmd_world_image(args, config: AppConfig) -> int:
    """生成世界封面并写回 cover_asset。

    参数:
        args: 已解析参数（id / force / json）。
        config: 本机配置。

    返回:
        进程退出码。

    异常:
        PreconditionError: 世界条目不存在。
    """
    store = _resolve_store(args, config)
    worlds = WorldStore(store.root)
    worlds.ensure()
    if worlds.get(args.id) is None:
        raise PreconditionError(f"世界条目不存在: {args.id}")
    result = render_world_cover(store, config, args.id, force=bool(getattr(args, "force", False)))
    state = "已生成" if result.get("created") else "已存在，复用"
    _emit(args, f"世界封面{state}: {result.get('cover_asset')}", result)
    return EXIT_OK
