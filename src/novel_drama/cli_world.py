"""世界条目 CLI：`ndw world list / generate / apply / remove / image`。

世界条目库（worlds.json）是章节绑定的唯一来源；本模块只做查看、生成、
导入、删除与封面生成，删除前会检查大纲章节是否仍在引用。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .cli_common import EXIT_OK, EXIT_REJECTED, PreconditionError, _emit, _resolve_store
from .config import AppConfig
from .media.art import render_world_cover
from .store.settings_store import SettingsStore
from .store.world_store import WorldStore
from .writing import generate_worlds
from .writing.worlds import clean_world_entry


def _chapter_refs(store, world_id: str) -> list[dict]:
    """查找大纲中引用了指定世界的章节。

    参数:
        store: 工程。
        world_id: 世界 ID。

    返回:
        `[{"id": 章节 ID, "title": 章节标题}]`；没有引用时返回空列表。
    """
    bible = SettingsStore(store.root).load_or_none() or {}
    refs: list[dict] = []
    for act in (bible.get("outline") or {}).get("acts") or []:
        for chapter in act.get("chapters") or []:
            if world_id in (chapter.get("world_ids") or []):
                refs.append({"id": chapter.get("id"), "title": chapter.get("title")})
    return refs


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
        for item in worlds
    )


def cmd_world_list(args, config: AppConfig) -> int:
    """列出全部世界条目（不存在时先触发迁移）。

    参数:
        args: 已解析参数（json）。
        config: 本机配置。

    返回:
        进程退出码。
    """
    store = _resolve_store(args, config)
    table = WorldStore(store.root).ensure()
    worlds = table.get("worlds") or []
    _emit(args, _fmt_worlds(worlds), {"worlds": worlds, "count": len(worlds)})
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
    worlds = WorldStore(store.root)
    worlds.ensure()
    for row in _read_rows(args.file):
        if not isinstance(row, dict):
            raise ValueError("世界条目必须是 JSON 对象")
        world_id = str(row.get("id") or "").strip()
        if world_id and worlds.get(world_id):
            worlds.update(world_id, row)
            continue
        entry = dict(row) if world_id else clean_world_entry(row)
        worlds.add(entry)
    table = worlds.list_worlds()
    _emit(args, f"已导入世界条目，当前共 {len(table)} 条\n{_fmt_worlds(table)}", {"worlds": table, "count": len(table)})
    return EXIT_OK


def cmd_world_remove(args, config: AppConfig) -> int:
    """删除世界条目；仍被大纲章节引用时拒绝删除。

    参数:
        args: 已解析参数（id / json）。
        config: 本机配置。

    返回:
        进程退出码（被引用时返回 EXIT_REJECTED 且 JSON 里带引用章节列表）。

    异常:
        PreconditionError: 世界条目不存在。
    """
    store = _resolve_store(args, config)
    worlds = WorldStore(store.root)
    worlds.ensure()
    refs = _chapter_refs(store, args.id)
    if refs:
        titles = "、".join(f"《{item['title']}》（{item['id']}）" for item in refs)
        message = f"世界条目 {args.id} 仍被 {len(refs)} 个章节引用，无法删除：{titles}；请先在大纲页解绑"
        _emit(args, message, {"removed": False, "id": args.id, "chapters": refs, "message": message})
        return EXIT_REJECTED
    if not worlds.remove(args.id):
        raise PreconditionError(f"世界条目不存在: {args.id}")
    _emit(args, f"已删除世界条目 {args.id}", {"removed": True, "id": args.id})
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
