"""`ndw pipeline` / `ndw serve`：阶段串联与本地 Web 工作台启动。"""

from __future__ import annotations

from novel_drama.composition import (
    AdapterError,
    ErrorCode,
    build_timeline,
    summarize_timeline,
    validate,
)
from novel_drama.infrastructure.config import AppConfig
from novel_drama.interface.cli.common import EXIT_OK, _resolve_store
from novel_drama.interface.cli.helpers import _make_queue
from novel_drama.models.ids import now_iso
from novel_drama.orchestration.pipeline import (
    build_tts_jobs,
    split_chapter_scenes,
    split_scene_lines,
)
from novel_drama.tts import line_state


def cmd_pipeline(args, config: AppConfig) -> int:
    """一条命令串起多个阶段；模型阶段本轮只会规划/入队。"""
    store = _resolve_store(args, config)
    stages = ["split", "lines", "tts", "timeline"]
    start = stages.index(args.from_stage) if args.from_stage else 0
    end = stages.index(args.to_stage) + 1 if args.to_stage else len(stages)
    targets = args.only.split(",") if args.only else stages[start:end]

    chapter = store.load_chapter(args.chapter)
    for stage in targets:
        print(f"== {stage} ==")
        if stage == "split":
            scenes = split_chapter_scenes(store, chapter, force=args.force, dry_run=args.dry_run)
            chapter = store.load_chapter(args.chapter)
            print(f"场景: {len(scenes)}")
        elif stage == "lines":
            characters = store.load_characters()["characters"]
            result = split_scene_lines(store, chapter, characters, force=args.force, dry_run=args.dry_run)
            chapter = store.load_chapter(args.chapter)
            print(f"台词行: {result['lines']}（未识别 {result['unknown']}）")
        elif stage == "tts":
            characters = store.load_characters()["characters"]
            jobs = build_tts_jobs(
                chapter,
                characters,
                adapter=config.adapters.get("tts", "placeholder"),
                snapshots=line_state.load_snapshots(store.root, chapter),
                engine=line_state.engine_params(config),
            )
            print(f"待配音: {len(jobs)} 条")
            if not args.dry_run and jobs:
                queue = _make_queue(store, config)
                for job in jobs:
                    queue.enqueue(job)
                print("已入队；适配器未对接时会在执行阶段报 NOT_IMPLEMENTED")
        elif stage == "timeline":
            if args.dry_run:
                timeline = build_timeline(chapter, store.settings())
                print("[dry-run] " + summarize_timeline(timeline))
            else:
                timeline = build_timeline(chapter, store.settings())
                timeline["built_at"] = now_iso()
                validate(timeline, "timeline.schema.json")
                store._write_json(store.timeline_path(chapter["id"]), timeline)
                print(summarize_timeline(timeline))
        else:
            raise AdapterError(ErrorCode.NOT_IMPLEMENTED, f"阶段 {stage} 尚未实现")
    return EXIT_OK


def cmd_serve(args, config: AppConfig) -> int:
    """启动本地 Web 工作台。"""
    from novel_drama.interface.http.app import serve

    root = args.project or config.project_root
    if root:
        # 打开工程自检：迁移旧内嵌场景、补台本、清点孤儿行（结果提示到 stderr）
        from novel_drama.composition import ProjectStore
        from novel_drama.interface.cli.common import _report_upgrade

        store = ProjectStore(str(root))
        if store.exists():
            _report_upgrade(store)
    serve(
        host=args.host,
        port=args.port,
        project_root=str(root) if root else None,
        config_path=args.config,
    )
    return EXIT_OK


def add_parser(sub) -> None:
    """注册 `pipeline` 命令。"""
    p = sub.add_parser("pipeline", help="串联阶段")
    p.add_argument("action", nargs="?", default="run", choices=["run"], help="固定为 run，可省略")
    p.add_argument("--chapter", required=True)
    p.add_argument("--from", dest="from_stage", choices=["split", "lines", "tts", "timeline"])
    p.add_argument("--to", dest="to_stage", choices=["split", "lines", "tts", "timeline"])
    p.add_argument("--only", help="逗号分隔的阶段，如 split,lines")
    p.add_argument("--force", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_pipeline)


def add_serve_parser(sub) -> None:
    """注册 `serve` 命令（保持它在帮助列表末尾的位置）。"""
    p = sub.add_parser("serve", help="启动本地 Web 工作台")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.set_defaults(func=cmd_serve)
