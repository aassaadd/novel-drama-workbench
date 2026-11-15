"""`ndw init` / `ndw doctor` / `ndw project`：工程创建、环境自检与元信息维护。"""

from __future__ import annotations

import sys
from pathlib import Path

from novel_drama import __version__
from novel_drama.composition import ProjectStore, list_workspaces
from novel_drama.infrastructure.config import AppConfig
from novel_drama.interface.cli.common import EXIT_OK, _emit, _resolve_store
from novel_drama.interface.cli.helpers import _build_adapters


def cmd_init(args, config: AppConfig) -> int:
    """新建工程目录并写入初始文件。"""
    store = ProjectStore(args.dir)
    project = store.create(args.title, author=args.author, language=args.language, genre=args.genre)
    _emit(
        args,
        f"工程已创建: {store.root}\n作品 ID: {project['work']['id']}\n下一步: ndw --project {store.root} chapter add --title 第一章",
        project,
    )
    return EXIT_OK


def cmd_doctor(args, config: AppConfig) -> int:
    """环境自检：依赖、目录、适配器、ffmpeg、锁路径。"""
    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"check": name, "ok": ok, "detail": detail})

    add("python", sys.version_info >= (3, 10), f"Python {sys.version.split()[0]}（要求 3.10+）")
    add("config", config.path is not None, str(config.path) if config.path else "未找到 config/config.yaml，使用默认值")
    try:
        import jsonschema  # noqa: F401

        add("jsonschema", True, "已安装")
    except ImportError:
        add("jsonschema", False, "未安装：pip install jsonschema（缺少则无法校验落盘）")
    try:
        import yaml  # noqa: F401

        add("pyyaml", True, "已安装")
    except ImportError:
        add("pyyaml", False, "未安装：pip install pyyaml")

    from novel_drama.composition import binary_available

    add("ffmpeg", binary_available(config.get("ffmpeg.binary", "ffmpeg")), "混音/导出需要（M4）")

    store = None
    root = args.project or config.project_root
    if root and ProjectStore(root).exists():
        store = ProjectStore(root)
        add("project", True, str(store.root))
        adapters = _build_adapters(config, store)
        for name in ("writing", "tts", "sfx", "comfy"):
            status = getattr(adapters, name).health()
            add(f"adapter:{name}", status.ok, status.detail)
        templates = adapters.comfy.list_templates()
        add("comfy:templates", bool(templates), f"{len(templates)} 个模板" if templates else "config/comfy 下暂无模板")
    else:
        add("project", False, "未指定或未初始化工程（可先 ndw init）")

    lock_path = Path(config.get("gpu.lock_path", "/Volumes/other/gpu.lock"))
    add("gpu:lock", lock_path.parent.exists(), f"{lock_path}（父目录{'存在' if lock_path.parent.exists() else '不存在'}）")
    add("serial", config.get("gpu.max_concurrent") == 1, "GPU 最大并发固定为 1")

    payload = {"version": __version__, "checks": checks, "warnings": config.warnings}
    if getattr(args, "json", False):
        _emit(args, payload=payload)
    else:
        for item in checks:
            print(f"[{'OK' if item['ok'] else 'WARN'}] {item['check']}: {item['detail']}")
        for warning in config.warnings:
            print(f"[WARN] {warning}")
    return EXIT_OK


def cmd_project_info(args, config: AppConfig) -> int:
    """打印工程概览。"""
    store = _resolve_store(args, config)
    project = store.load_project()
    characters = store.load_characters()["characters"]
    chapters = store.list_chapters()
    payload = {"root": str(store.root), "work": project["work"], "chapters": chapters, "characters": len(characters), "settings": project.get("settings")}
    text = (
        f"工程: {store.root}\n"
        f"作品: {project['work']['title']}（{project['work']['id']}）\n"
        f"章节: {len(chapters)} / 角色: {len(characters)}\n"
        f"写作适配器: {project.get('settings', {}).get('writing', {}).get('adapter')}"
    )
    _emit(args, text, payload)
    return EXIT_OK


def cmd_project_list(args, config: AppConfig) -> int:
    """列出本机所有工程。"""
    workspaces = list_workspaces()
    if getattr(args, "json", False):
        _emit(args, payload=workspaces)
    else:
        if not workspaces:
            print("暂无工程，执行 ndw init <dir> --title 书名 新建")
        for item in workspaces:
            print(f"{item.get('title') or '(无标题)':<20} {item['path']}")
    return EXIT_OK


def cmd_project_set(args, config: AppConfig) -> int:
    """修改作品元信息（书名 / 作者 / 类型标签）。

    参数:
        args: 已解析参数（title / author / genre / clear_genres）。
        config: 本机配置。

    返回:
        进程退出码。

    异常:
        ValueError: 没有任何要修改的字段，或书名为空。
    """
    store = _resolve_store(args, config)
    project = store.load_project()
    work = project["work"]
    changed = []
    if getattr(args, "title", None) is not None:
        title = args.title.strip()
        if not title:
            raise ValueError("书名不能为空")
        work["title"] = title
        changed.append(f"title={title}")
    if getattr(args, "author", None) is not None:
        work["author"] = args.author.strip()
        changed.append(f"author={work['author']}")
    if getattr(args, "clear_genres", False):
        work["genre"] = []
        changed.append("genre=[]")
    elif getattr(args, "genre", None) is not None:
        work["genre"] = [item.strip() for item in args.genre if item.strip()]
        changed.append("genre=" + "、".join(work["genre"]))
    if not changed:
        raise ValueError("没有要修改的字段：至少给一个（--title / --author / --genre）")
    store.save_project(project)
    payload = {"title": work.get("title"), "author": work.get("author") or "", "genres": work.get("genre") or []}
    _emit(args, "已更新作品信息：" + "、".join(changed), payload)
    return EXIT_OK


def add_parser(sub) -> None:
    """注册 init / doctor / project 三个命令组。"""
    p = sub.add_parser("init", help="新建工程")
    p.add_argument("dir")
    p.add_argument("--title", required=True)
    p.add_argument("--author", default="")
    p.add_argument("--language", default="zh-CN")
    p.add_argument("--genre", action="append")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("doctor", help="环境自检")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("project", help="工程信息")
    p_sub = p.add_subparsers(dest="action", required=True)
    p_sub.add_parser("info").set_defaults(func=cmd_project_info)
    p_sub.add_parser("list").set_defaults(func=cmd_project_list)
    q = p_sub.add_parser("set", help="修改作品元信息（书名/作者/类型）")
    q.add_argument("--title")
    q.add_argument("--author")
    q.add_argument("--genre", action="append", help="类型标签，可重复；给出即整体替换")
    q.add_argument("--clear-genres", dest="clear_genres", action="store_true", help="清空类型标签")
    q.set_defaults(func=cmd_project_set)
