"""命令行入口 `ndw`：不打开界面也能跑完整条流水线。

退出码：0 成功 / 1 拒绝执行（无参数快照等，未做修改）/ 2 参数或配置错误 / 3 适配器未对接 / 4 前置条件缺失 / 5 任务失败 / 6 schema 校验失败。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import __version__
from .adapters import AdapterError, ErrorCode, build_adapters
from .cli_common import EXIT_FAILED, EXIT_NOT_IMPLEMENTED, EXIT_OK, EXIT_PRECONDITION, EXIT_REJECTED, EXIT_SCHEMA, EXIT_USAGE, PreconditionError, TaskFailedError, _emit, _resolve_store, _tts_service
from .cli_chapter import (
    cmd_chapter_add,
    cmd_chapter_import,
    cmd_chapter_list,
    cmd_chapter_show,
    cmd_chapter_source,
)
from .cli_character import (
    cmd_character_apply,
    cmd_character_generate,
    cmd_character_image,
    cmd_character_remove,
    cmd_character_update,
)
from .cli_comfy import cmd_comfy
from .cli_group import cmd_group_apply, cmd_group_diff, cmd_group_list
from .cli_line import (
    cmd_line_diff,
    cmd_line_list,
    cmd_line_revert,
    cmd_line_set,
    cmd_line_split,
    cmd_scene_split,
)
from .cli_media import cmd_bgm, cmd_concat, cmd_export, cmd_mux, cmd_timeline, cmd_tts_batch
from .cli_models import cmd_models_import, cmd_models_install, cmd_models_list, cmd_models_verify
from .cli_outline import cmd_outline_bind, cmd_outline_check
from .cli_produce import cmd_produce, cmd_run_show
from .cli_setting import cmd_setting
from .cli_voice import cmd_script, cmd_voice, cmd_voice_builtin
from .cli_world import (
    cmd_world_apply,
    cmd_world_generate,
    cmd_world_image,
    cmd_world_list,
    cmd_world_remove,
)
from .cli_writing import (
    cmd_chapter_edit,
    cmd_chapter_generate,
    cmd_chapter_save,
    cmd_chapter_summarize,
    cmd_outline,
    cmd_script_suggest,
)
from .config import AppConfig, load_config
from .media import load_cues, plan_mix, render_mix, run_media
from .media.runner import plan_media
from .media.timeline_builder import build_timeline, summarize_timeline
from .models import entities
from .models.ids import now_iso
from .orchestration import GpuQueue
from .orchestration.produce import STAGES as PRODUCE_STAGES
from .orchestration.pipeline import (
    build_media_jobs,
    build_tts_jobs,
    split_chapter_scenes,
    split_scene_lines,
)
from .store import ProjectStore, QueueStore, SchemaValidationError, list_workspaces, validate
from .tts import line_state
from .tts.errors import TtsError

# ---------------- 通用工具 ----------------


def _build_adapters(config: AppConfig, store: ProjectStore | None):
    """装配适配器（工程存在时合并工程级设置）。

    参数:
        config: 本机配置。
        store: 工程（可为空）。

    返回:
        Adapters。
    """
    settings = store.settings() if store is not None else None
    return build_adapters(config, settings)


def _make_queue(store: ProjectStore, config: AppConfig) -> GpuQueue:
    """创建串行队列。

    参数:
        store: 工程。
        config: 本机配置。

    返回:
        GpuQueue。
    """
    adapters = _build_adapters(config, store)
    lock_path = config.get("gpu.lock_path")
    return GpuQueue(store, adapters, lock_path, poll_interval_s=float(config.get("gpu.poll_interval_s", 2)))


def _drain(queue: GpuQueue) -> int:
    """执行队列中所有可执行任务，并按执行结果给出退出码。

    参数:
        queue: 串行队列。

    返回:
        退出码：全部成功返回 EXIT_OK；存在 NOT_IMPLEMENTED 失败返回 EXIT_NOT_IMPLEMENTED；
        存在其他失败返回 EXIT_FAILED。
    """
    exit_code = EXIT_OK
    while True:
        job = queue.run_once()
        if job is None:
            return exit_code
        print(f"[{job['status']}] {job['kind']} {job['id']}" + (f"  {job['error']['message']}" if job.get("error") else ""))
        if job["status"] == "failed":
            code = (job.get("error") or {}).get("code")
            if code == ErrorCode.NOT_IMPLEMENTED.value:
                exit_code = EXIT_NOT_IMPLEMENTED
            elif exit_code == EXIT_OK:
                exit_code = EXIT_FAILED


# ---------------- init / doctor / project ----------------


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

    from .media.ffmpeg import binary_available

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


# ---------------- character ----------------


def cmd_character_add(args, config: AppConfig) -> int:
    """新增角色。"""
    store = _resolve_store(args, config)
    table = store.load_characters()
    if any(item["name"] == args.name for item in table["characters"]):
        raise PreconditionError(f"角色已存在: {args.name}")
    character = entities.new_character(args.name, role=args.role, aliases=args.alias or [])
    if args.appearance:
        character["appearance_prompt"] = args.appearance
    table["characters"].append(character)
    store.save_characters(table)
    _emit(args, f"角色已创建: {character['id']} {character['name']}（{character['role']}）", character)
    return EXIT_OK


def cmd_character_list(args, config: AppConfig) -> int:
    """列出角色与音色绑定状态（JSON 里补齐 image_assets 字段）。

    参数:
        args: 已解析参数（json）。
        config: 本机配置。

    返回:
        进程退出码。
    """
    store = _resolve_store(args, config)
    characters = store.load_characters()["characters"]
    if getattr(args, "json", False):
        rows = [{**item, "image_assets": item.get("image_assets")} for item in characters]
        _emit(args, payload=rows)
        return EXIT_OK
    for item in characters:
        voice = (item.get("voice_binding") or {}).get("voice_id") or "未绑定音色"
        print(f"{item['id']}  {item['name']:<8} {item['role']:<12} {voice}")
    return EXIT_OK


def cmd_character_bind_voice(args, config: AppConfig) -> int:
    """绑定角色音色（变更后相关台词需重跑）。"""
    store = _resolve_store(args, config)
    table = store.load_characters()
    target = next((item for item in table["characters"] if item["id"] == args.character or item["name"] == args.character), None)
    if target is None:
        raise PreconditionError(f"角色不存在: {args.character}")
    target["voice_binding"] = {"adapter": args.adapter, "voice_id": args.voice, "reference_audio": None, "seed": None, "speed": args.speed, "emotion_map": {}}
    target["updated_at"] = now_iso()
    store.save_characters(table)
    _emit(args, f"已绑定 {target['name']} → {args.voice}（该角色台词需重跑配音）", target)
    return EXIT_OK


# ---------------- 模型任务（规划 + 入队） ----------------


def cmd_tts(args, config: AppConfig) -> int:
    """配音：plan 只规划，run 入队执行；两者共用同一套范围选择。

    范围与状态筛选走 `line_state`，与 `line list --status` 同源，
    因此 plan 的「待合成」计数必然等于 list 筛出来的行数。
    """
    store = _resolve_store(args, config)
    chapter = store.load_chapter(args.chapter)
    characters = store.load_characters()["characters"]
    adapter = config.adapters.get("tts", "placeholder")
    snapshots = line_state.load_snapshots(store.root, chapter)
    live_ids = line_state.live_line_ids(store)
    engine = line_state.engine_params(config)
    # force：忽略状态筛选（含已就绪行），强制重新生成
    force = bool(getattr(args, "force", False))
    statuses = () if force else line_state.parse_states(args.status)
    # 默认范围：给了 --line 就是「选中」；给了 --scene 就是「场景」；否则「待处理」
    if args.scope:
        scope = args.scope
    elif args.line:
        scope = "selected"
    elif args.scene:
        scope = "scene"
    else:
        scope = "chapter" if force else "pending"
    states = line_state.chapter_states(
        chapter, {item["id"]: item for item in characters}, snapshots, root=store.root, live_ids=live_ids, engine=engine
    )
    selected = line_state.select_states(
        states, scope=scope, line_ids=args.line, scene_ids=args.scene, statuses=statuses
    )
    counts = line_state.state_counts(selected)
    if force:
        # 强制重生成：范围内所有行都入队（含 ready）
        todo_ids = {row["line"]["id"] for row in selected}
        run_nonce = f"regen-{time.time_ns()}"
    else:
        todo_ids = {row["line"]["id"] for row in selected if row["state"] in line_state.PENDING_STATES}
        run_nonce = None
    jobs = build_tts_jobs(
        chapter,
        characters,
        adapter=adapter,
        snapshots=snapshots,
        engine=engine,
        root=store.root,
        live_ids=live_ids,
        line_ids=todo_ids,
        force=force,
        run_nonce=run_nonce,
    )
    if args.action == "plan":
        summary = (
            f"待合成台词行: {len(jobs)} 条（范围 {scope}，适配器 {adapter}）"
            f"｜未生成 {counts.get('pending', 0)} / 过期 {counts.get('stale', 0)}"
            f" / 失败 {counts.get('failed', 0)} / 已完成 {counts.get('ready', 0)}"
        )
        _emit(
            args,
            summary,
            {
                "scope": scope,
                "selected": len(selected),
                "counts": counts,
                "jobs": len(jobs),
                "adapter": adapter,
            },
        )
        return EXIT_OK
    if not jobs:
        _emit(args, "没有需要合成的台词行", {"jobs": 0, "scope": scope, "counts": counts})
        return EXIT_OK
    queue = _make_queue(store, config)
    created = sum(1 for job in jobs if queue.enqueue(job)[1])
    print(f"已入队 {created} 条配音任务（去重后）")
    if getattr(args, "wait", False):
        return _drain(queue)
    return EXIT_OK


# ---------------- 配音：provider / 路由 / 单句 / 对比 ----------------


def _tts_requirement(args, text: str):
    """把命令行参数翻译成路由需求向量。

    参数:
        args: 已解析参数。
        text: 台词文本。

    返回:
        Requirement。
    """
    from .tts import Requirement

    paral = []
    for item in getattr(args, "paralinguistic", None) or []:
        paral.extend([tag.strip() for tag in item.split(",") if tag.strip()])
    return Requirement(
        text=text,
        emotion=getattr(args, "emotion", None),
        emotion_intensity=int(getattr(args, "emotion_intensity", 0) or 0),
        paralinguistic=paral,
        clone_required=not getattr(args, "no_clone", False),
        language=getattr(args, "language", None) or "zh-CN",
        duration_mode="target" if getattr(args, "target_duration_ms", None) else "none",
        target_duration_ms=getattr(args, "target_duration_ms", None),
        usage=getattr(args, "usage", None) or "personal",
    )


def _tts_voice(args, service):
    """构造音色参考；命令行未指定时沿用配置默认值。

    参数:
        args: 已解析参数。
        service: TtsService（读取 default_voice）。
    """
    from .tts import VoiceRef

    ref_audio = getattr(args, "ref_audio", None) or service.default_voice.ref_audio
    ref_text = getattr(args, "ref_text", None) or service.default_voice.ref_text
    emo_audio = getattr(args, "emo_audio_prompt", None)
    return VoiceRef(
        variant_id=getattr(args, "voice_id", None),
        ref_audio=ref_audio,
        ref_text=ref_text,
        emotion_audio=emo_audio,
        source="emotion_ref" if emo_audio else "preset",
    )


def _tts_instruct(args) -> str | None:
    """取显式风格指令；不再把情绪自动改写成指令文本。

    参数:
        args: 已解析参数。

    说明:
        情绪到指令的自动改写会和 IndexTTS 的 8 维向量通路撞车（三条情绪通路
        互斥），因此只保留用户显式给的 `--emo-text` / `--instruct`；
        需要「情绪→指令」的实现（如 CosyVoice3）在 provider 内部自行改写。
    """
    return getattr(args, "emo_text", None) or getattr(args, "instruct", None)


def _read_text(args) -> str:
    """读取台词文本（--text 或 --text-file）。

    参数:
        args: 已解析参数。
    """
    if getattr(args, "text_file", None):
        return Path(args.text_file).read_text(encoding="utf-8").strip()
    return (getattr(args, "text", None) or "").strip()


def _rss_mb() -> float:
    """当前进程峰值内存（MB）；macOS 的 ru_maxrss 单位是字节。"""
    import resource

    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 / 1024, 1)


def cmd_tts_providers(args, config: AppConfig) -> int:
    """列出全部 provider 的可用性、许可与自动选中资格。"""
    service = _tts_service(args, config)
    rows = service.doctor()
    if getattr(args, "json", False):
        print(json.dumps({"providers": rows}, ensure_ascii=False, indent=2))
        return EXIT_OK
    print(f"{'provider_id':<20}{'可用':<6}{'启用':<6}{'自动':<6}{'许可':<20}{'版本':<12}说明")
    for row in rows:
        print(
            f"{row['provider_id']:<20}"
            f"{'是' if row['available'] else '否':<6}"
            f"{'是' if row['enabled'] else '否':<6}"
            f"{'是' if row['auto_select'] else '否':<6}"
            f"{str(row['license_gate']):<20}"
            f"{str(row['model_version']):<12}"
            f"{row['detail']}"
        )
    return EXIT_OK


def cmd_tts_route(args, config: AppConfig) -> int:
    """只做路由决策，不合成：解释选中理由与被淘汰项。"""
    text = _read_text(args)
    if not text:
        raise PreconditionError("缺少台词文本：用 --text 或 --text-file 指定")
    service = _tts_service(args, config)
    requirement = _tts_requirement(args, text)
    decision = service.decide(
        requirement,
        line_provider_id=getattr(args, "provider", None),
        character_provider_id=getattr(args, "character_provider", None),
    )
    if getattr(args, "json", False):
        print(json.dumps(decision, ensure_ascii=False, indent=2))
        return EXIT_OK
    selected = decision["selected"]
    print(f"路由模式 {decision['mode']}  选中 {selected['provider_id']}  得分 {selected['score']}")
    for reason in selected["reasons"]:
        print(f"  + {reason}")
    for item in decision["degradations"]:
        print(f"  ! {item['kind']}: {item.get('detail')}")
    for item in decision["rejected"]:
        print(f"  - {item['provider_id']}  [{item['stage']}/{item['reason']}] {item.get('detail')}")
    print(f"input_hash {decision['input_hash']}")
    return EXIT_OK


def cmd_tts_say(args, config: AppConfig) -> int:
    """合成一句话并落盘，写出侧车 JSON 记录 provider 与版本。"""
    text = _read_text(args)
    if not text:
        raise PreconditionError("缺少台词文本：用 --text 或 --text-file 指定")
    service = _tts_service(args, config)
    requirement = _tts_requirement(args, text)
    voice = _tts_voice(args, service)

    out = getattr(args, "out", None)
    if not out:
        base = service.project_root if getattr(service, "project_root", None) else Path.cwd()
        stamp = now_iso().replace(":", "").replace("-", "")[:15]
        out = str(Path(base) / "var" / "tts" / f"say_{stamp}.wav")

    import time

    started = time.perf_counter()
    payload = service.synthesize(
        text,
        out,
        requirement=requirement,
        voice=voice,
        instruct=_tts_instruct(args),
        line_provider_id=getattr(args, "provider", None),
        character_provider_id=getattr(args, "character_provider", None),
    )
    wall_ms = int((time.perf_counter() - started) * 1000)

    if getattr(args, "json", False):
        payload["wall_ms"] = wall_ms
        payload["rss_mb"] = _rss_mb()
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return EXIT_OK
    print(f"音频 {payload['audio_path']}")
    print(f"侧车 {payload['audio_path']}.json")
    print(
        f"provider {payload['provider_id']}  模型 {payload['model']}({payload['model_version']})  "
        f"时长 {payload['duration_ms']}ms  合成 {payload['latency_ms']}ms  总耗时 {wall_ms}ms  "
        f"块数 {payload['chunks']}  内存 {_rss_mb()}MB"
    )
    return EXIT_OK


def cmd_tts_compare(args, config: AppConfig) -> int:
    """同一句台词用多个 provider 合成，留档对比试听。"""
    text = _read_text(args)
    if not text:
        raise PreconditionError("缺少台词文本：用 --text 或 --text-file 指定")
    service = _tts_service(args, config)
    raw = getattr(args, "providers", None)
    if raw:
        provider_ids = [item.strip() for item in raw.split(",") if item.strip()]
    else:
        provider_ids = [
            pid
            for pid, provider in service.providers.items()
            if provider.spec.enabled and provider.spec.capabilities.get("allow_auto_select")
        ]
    out_dir = getattr(args, "out_dir", None) or "var/tts/compare"
    record = service.compare(
        text,
        provider_ids,
        out_dir,
        requirement=_tts_requirement(args, text),
        voice=_tts_voice(args, service),
    )
    if getattr(args, "json", False):
        print(json.dumps(record, ensure_ascii=False, indent=2))
        return EXIT_OK
    print(f"对比批次 {record['batch_id']}  记录 {record['record_path']}")
    for item in record["items"]:
        if item["status"] == "ready":
            print(f"  {item['provider_id']:<20} {item['duration_ms']}ms  {item['asset_id']}")
        else:
            print(f"  {item['provider_id']:<20} 失败 [{item['code']}] {item['message']}")
    return EXIT_OK


def cmd_media(args, config: AppConfig) -> int:
    """音效/配图/视频：plan 规划，run 入队；--dry-run 只出调用计划并报未实现。"""
    store = _resolve_store(args, config)
    chapter = store.load_chapter(args.chapter)
    kind = args.media_kind
    if args.action == "plan":
        if kind == "sfx":
            missing = [scene["id"] for scene in chapter.get("scenes") or [] if not scene.get("sfx_events")]
            _emit(args, f"待规划音效的场景: {len(missing)} 个（每场建议 1 条环境音 + 按需拟音）", {"scenes": missing})
            return EXIT_OK
        if kind == "image":
            candidates = [
                {"scene_id": scene["id"], "anchor_line_id": (scene.get("lines") or [{}])[0].get("id"), "scene_title": scene.get("title")}
                for scene in chapter.get("scenes") or []
                if scene.get("lines") and not scene.get("key_images")
            ]
            _emit(args, f"可生成关键图的场景: {len(candidates)} 个（每场默认 1 张）", {"candidates": candidates})
            return EXIT_OK
        segments = [video["id"] for scene in chapter.get("scenes") or [] for video in scene.get("video_segments") or []]
        _emit(args, f"已定义的短视频段落: {len(segments)} 个（时长上限 5000ms）", {"video_segments": segments})
        return EXIT_OK

    adapter = config.adapters.get("comfyui", "local") if kind != "sfx" else config.adapters.get("sfx", "placeholder")
    if getattr(args, "dry_run", False) and kind in ("image", "video"):
        rows = plan_media(store, config, args.chapter, kind)
        lines = [f"[plan] {kind} 共 {len(rows)} 条（dry-run，未提交任何任务）"]
        for row in rows:
            size = f"{row['width']}×{row['height']}" if row.get("width") and row.get("height") else "-"
            text = (
                f"  {row['item_id']}  模板={row['template_id'] or '-'}  画幅={row['aspect_ratio']}  "
                f"尺寸={size}  seed={row['seed']}  模式={row['source_mode']}"
                + (f"  提示：{row['warning']}" if row.get("warning") else "")
            )
            # 参考图字段由媒体规划任务补充；缺失时保持原输出，不影响 dry-run
            refs = row.get("references") or []
            if isinstance(refs, list) and refs:
                names = "/".join(
                    str(ref.get("name") or ref.get("id") or "") for ref in refs if isinstance(ref, dict)
                )
                text += f"  参考图={len(refs)}（{names}）"
            for warning in row.get("reference_warnings") or []:
                text += f"  提示：{warning}"
            lines.append(text)
        _emit(args, "\n".join(lines), {"kind": kind, "dry_run": True, "items": rows})
        return EXIT_OK
    if getattr(args, "dry_run", False):
        plan = run_media(store, config, chapter, kind, limit=getattr(args, "limit", None), dry_run=True)
        _emit(args, f"[plan] {kind} 共 {plan['total']} 条，已就绪 {plan['skipped']} 条，待生成 {plan['total'] - plan['skipped']} 条（adapter={adapter}）", plan)
        return EXIT_OK

    result = run_media(
        store,
        config,
        chapter,
        kind,
        force=bool(getattr(args, "force", False)),
        limit=getattr(args, "limit", None),
    )
    lines = [
        f"{kind} 生成完成：就绪 {result['ready']} / 跳过 {result['skipped']} / 失败 {result['failed']}（共 {result['total']} 条）"
    ]
    for item in result["items"]:
        if item["status"] == "failed":
            lines.append(f"  ! {item['id']}: {item['error']}")
        elif item.get("path"):
            elapsed = item.get("elapsed_ms")
            detail = (
                f"  模板={item['template_id']}  画幅={item['aspect_ratio']}  {item['width']}×{item['height']}"
                if item.get("template_id")
                else ""
            )
            if item.get("source_mode"):
                detail += f"  模式={item['source_mode']}"
            if elapsed is not None:
                detail += f"  耗时 {elapsed / 1000:.1f}s"
            if item.get("warning"):
                detail += f"  提示：{item['warning']}"
            lines.append(f"  - {item['id']} → {item['path']}{detail}")
    _emit(args, "\n".join(lines), result)
    return EXIT_FAILED if result["failed"] else EXIT_OK


def cmd_mix(args, config: AppConfig) -> int:
    """混音：默认只打印计划，--render 执行分轨 + 混音 + 规整。"""
    store = _resolve_store(args, config)
    path = store.timeline_path(args.chapter)
    if not path.exists():
        raise PreconditionError(f"时间轴不存在: {path}（先执行 ndw timeline build）")
    timeline = json.loads(path.read_text(encoding="utf-8"))
    plan = plan_mix(timeline, store.root, bgm_cues=load_cues(store.root, args.chapter))
    if not args.render:
        _emit(args, json.dumps(plan, ensure_ascii=False, indent=2), plan)
        return EXIT_OK
    result = render_mix(
        plan,
        binary=config.get("ffmpeg.binary", "ffmpeg"),
        audio_cfg=config.get("audio"),
        loudness_target=config.get("output.loudness_target_lufs", -16),
    )
    text = [f"混音完成：{result['mix']['path']}  时长 {result['duration_ms']}ms"]
    for stem in result["stems"]:
        text.append(f"  分轨 {stem['kind']}: {stem['path']}  素材 {stem['items']}  峰值 {stem['peak_db']}dBFS")
    raw = result["mix"]
    text.append(f"  混音峰值 {raw['peak_db']}dBFS  响度 {raw['loudness'].get('lufs')}LUFS")
    norm = result["normalized"]
    text.append(
        f"  规整 {norm['path']}  响度 {norm['before'].get('lufs')} → {norm['after'].get('lufs')}LUFS  "
        f"真峰值 {norm['after'].get('true_peak')}dBTP  耗时 {norm['elapsed_ms']}ms"
    )
    text.append(f"总耗时 {result['elapsed_ms']}ms  峰值内存 {result['peak_rss_mb']}MB")
    _emit(args, "\n".join(text), result)
    return EXIT_OK


# ---------------- queue ----------------


def cmd_queue(args, config: AppConfig) -> int:
    """队列查看、消费、重试、取消。"""
    store = _resolve_store(args, config)
    queue_store = QueueStore(store)
    if args.action == "list":
        jobs = queue_store.load()
        if args.status:
            jobs = [job for job in jobs if job.get("status") == args.status]
        if getattr(args, "json", False):
            _emit(args, payload=jobs)
            return EXIT_OK
        if not jobs:
            print("队列为空")
        for job in jobs:
            error = (job.get("error") or {}).get("message", "")
            print(f"{job['id']}  {job['kind']:<8} {job['status']:<12} attempts={job['attempts']}  {error}")
        return EXIT_OK
    if args.action == "run":
        queue = _make_queue(store, config)
        processed = queue.run(once=args.once)
        print(f"已处理 {processed} 条任务")
        return EXIT_OK
    if args.action == "retry":
        target = queue_store.find(args.job)
        if target is None:
            raise PreconditionError(f"任务不存在: {args.job}")
        target["status"] = "queued"
        target["attempts"] = 0
        target["error"] = None
        queue_store.update(target)
        _emit(args, f"已重新入队 {target['id']}", target)
        return EXIT_OK
    target = queue_store.find(args.job)
    if target is None:
        raise PreconditionError(f"任务不存在: {args.job}")
    target["status"] = "canceled"
    queue_store.update(target)
    _emit(args, f"已取消 {target['id']}", target)
    return EXIT_OK


# ---------------- pipeline / serve ----------------


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
    from .web.app import serve

    root = args.project or config.project_root
    serve(
        host=args.host,
        port=args.port,
        project_root=str(root) if root else None,
        config_path=args.config,
    )
    return EXIT_OK


# ---------------- 参数解析 ----------------


def _add_scope_args(q) -> None:
    """给 `tts plan` / `tts run` 挂上生成范围选择参数。

    参数:
        q: 子命令 parser。
    """
    q.add_argument(
        "--scope",
        choices=list(line_state.SCOPES),
        help="selected=指定行 / pending=待处理（默认）/ scene=指定场景 / chapter=整章",
    )
    q.add_argument("--line", action="append", help="选中的台词行 ID，可重复（--scope selected 必填）")
    q.add_argument("--scene", action="append", help="选中的场景 ID，可重复（--scope scene 必填）")
    q.add_argument("--status", help="逗号分隔的派生状态：pending,stale,failed,ready；与 --scope pending 互斥")


def build_parser() -> argparse.ArgumentParser:
    """构建命令行解析器。"""
    parser = argparse.ArgumentParser(prog="ndw", description="小说有声剧工作台：文本 → 配音/音效 → 时间轴 → 成片")
    parser.add_argument("--version", action="version", version=f"ndw {__version__}")
    parser.add_argument("--project", "-p", help="工程目录（也可用 NDW_PROJECT 环境变量）")
    parser.add_argument("--config", help="本机配置文件路径，默认 config/config.yaml")
    parser.add_argument("--json", action="store_true", help="输出 JSON，便于脚本消费")
    sub = parser.add_subparsers(dest="command", required=True)

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

    p = sub.add_parser("chapter", help="章节")
    p_sub = p.add_subparsers(dest="action", required=True)
    q = p_sub.add_parser("add")
    q.add_argument("--title", required=True)
    q.add_argument("--index", type=int)
    q.add_argument("--outline", default="")
    q.add_argument("--content-file")
    q.set_defaults(func=cmd_chapter_add)
    p_sub.add_parser("list").set_defaults(func=cmd_chapter_list)
    q = p_sub.add_parser("show")
    q.add_argument("chapter")
    q.set_defaults(func=cmd_chapter_show)
    q = p_sub.add_parser("import", help="按标题切章导入，重复导入可控")
    q.add_argument("file")
    q.add_argument("--pattern", default=r"^#{1,3}\s+(.+)$")
    q.add_argument("--max-chars", type=int, default=5000)
    q.add_argument(
        "--on-duplicate",
        choices=["skip", "replace", "new"],
        default="skip",
        help="命中已有章节时：skip 跳过（默认）/ replace 覆盖正文 / new 再建一份",
    )
    q.add_argument("--force", action="store_true", help="等价于 --on-duplicate new")
    q.add_argument("--force-replace", action="store_true", help="目标章节已有台词行或音频时仍强制覆盖")
    q.set_defaults(func=cmd_chapter_import)
    q = p_sub.add_parser("source", help="查看原文快照 / 与当前正文对照 / 定位某段原文")
    q.add_argument("--chapter", required=True)
    q.add_argument("--diff", action="store_true", help="按段落对齐，报 missing/added/modified")
    q.add_argument("--at", type=int, help="原文偏移（0 起算）")
    q.add_argument("--paragraph", type=int, help="原文段落序号（1 起算）")
    q.set_defaults(func=cmd_chapter_source)
    q = p_sub.add_parser("generate", help="按大纲/节拍生成正文（离线写作或 LLM）")
    q.add_argument("--chapter", required=True)
    q.add_argument("--regen", action="store_true", help="已有正文时覆盖重写")
    q.add_argument("--dry-run", action="store_true", help="只预览，不落盘")
    q.add_argument(
        "--multi-turn",
        dest="multi_turn",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="强制开启/关闭多轮续写；缺省按 writing.multi_turn.enabled 与目标字数自动判断",
    )
    q.add_argument("--segment-words", dest="segment_words", type=int, help="每段目标字数（默认取配置 segment_words）")
    q.add_argument("--max-rounds", dest="max_rounds", type=int, help="最大续写轮数（默认取配置 max_rounds）")
    q.set_defaults(func=cmd_chapter_generate)
    q = p_sub.add_parser("save", help="用手写正文覆盖章节正文（UTF-8）")
    q.add_argument("--chapter", required=True)
    save_source = q.add_mutually_exclusive_group(required=True)
    save_source.add_argument("--file", help="从 UTF-8 文本文件读取正文")
    save_source.add_argument("--stdin", action="store_true", help="从标准输入读取正文")
    save_source.add_argument("--text", help="直接给出正文文本")
    q.set_defaults(func=cmd_chapter_save)
    q = p_sub.add_parser("edit", help="按提示词让 LLM 改写正文（默认预览，--apply 才落盘）")
    q.add_argument("--chapter", required=True)
    q.add_argument("--instruction", required=True, help="改写提示词")
    q.add_argument("--range", dest="char_range", help="选区 start:end（0 起算，左闭右开）；缺省改写整章")
    q.add_argument("--chunk-words", dest="chunk_words", type=int, help="分块阈值（默认取配置 edit_chunk_words）")
    edit_mode = q.add_mutually_exclusive_group()
    edit_mode.add_argument("--dry-run", action="store_true", help="只预览（默认行为）")
    edit_mode.add_argument("--apply", action="store_true", help="写盘并追加 continuations 记录")
    q.set_defaults(func=cmd_chapter_edit)
    q = p_sub.add_parser("summarize", help="根据正文反向总结标题/摘要/大纲/节拍（默认预览，--apply 才落盘）")
    q.add_argument("--chapter", required=True)
    q.add_argument("--apply", action="store_true", help="写回章节文件与设定库大纲")
    q.add_argument("--chunk-words", dest="chunk_words", type=int, help="分块阈值（默认取配置 summary_chunk_words）")
    q.set_defaults(func=cmd_chapter_summarize)

    p = sub.add_parser("setting", help="设定库（世界观/文风/大纲）")
    p_sub = p.add_subparsers(dest="action", required=True)
    p_sub.add_parser("init", help="生成设定库骨架").set_defaults(func=cmd_setting)
    q = p_sub.add_parser("show", help="查看设定库")
    q.add_argument("--section", choices=["premise", "world", "outline", "style", "continuity"])
    q.set_defaults(func=cmd_setting)
    q = p_sub.add_parser("validate", help="只校验不写盘")
    q.set_defaults(func=cmd_setting)
    q = p_sub.add_parser("set", help="修改标量字段")
    for flag in (
        "logline",
        "theme",
        "core_conflict",
        "ending_intent",
        "target_audience",
        "target_length",
        "era",
        "tone",
        "pov",
        "tense",
        "prose_style",
        "visual_style",
    ):
        q.add_argument("--" + flag.replace("_", "-"), dest=flag)
    q.set_defaults(func=cmd_setting)
    q = p_sub.add_parser("world", help="增补世界设定（规则/阵营/地点/词条）")
    q.add_argument("--add-rule", dest="add_rule", action="append", help="新增一条世界规则，可重复")
    q.add_argument("--add-faction", dest="add_faction", action="append", help="新增一个阵营，可重复")
    q.add_argument("--add-location", dest="add_location", help="`名称|描述|氛围`")
    q.add_argument("--add-glossary", dest="add_glossary", help="`术语|释义`")
    q.set_defaults(func=cmd_setting)
    q = p_sub.add_parser("import", help="从 JSON 导入（work_id 必须一致）")
    q.add_argument("--file", required=True)
    q.set_defaults(func=cmd_setting)
    q = p_sub.add_parser("export", help="导出为 JSON")
    q.add_argument("--out")
    q.set_defaults(func=cmd_setting)
    q = p_sub.add_parser("generate", help="AI 生成世界 / 梗概 / 文风（或按区块生成）")
    q.add_argument("--step", choices=["world", "premise", "style"], help="兼容旧用法：整体生成某个分区")
    q.add_argument("--section", choices=["premise", "style", "world", "character"], help="按区块生成（world/character 返回完整条目）")
    q.add_argument("--brief", help="补充要求，如「外星世界，双恒星」")
    q.add_argument("--target-id", dest="target_id", help="world / character 区块的改写目标 ID")
    q.add_argument("--overwrite", action="store_true", help="用生成值整体替换已有内容（仅 --step 生效）")
    q.add_argument("--dry-run", action="store_true", help="只预览，不落盘")
    q.set_defaults(func=cmd_setting)
    q = p_sub.add_parser("polish", help="AI 润色单个字段")
    q.add_argument("--field", required=True, help="如 theme / world.rule / character.background")
    q.add_argument("--text", help="待润色文本；缺省读取当前值")
    q.add_argument("--apply", action="store_true", help="把润色结果写回")
    q.add_argument("--character", help="character.* 字段所属角色 ID")
    q.add_argument("--context", help="附加上下文，帮助模型理解")
    q.set_defaults(func=cmd_setting)
    q = p_sub.add_parser("apply", help="从 JSON 合并写入指定分区")
    q.add_argument("--section", required=True, choices=["world", "premise", "style"])
    q.add_argument("--file", required=True, help="JSON 文件路径，- 表示从 stdin 读取")
    q.set_defaults(func=cmd_setting)

    p = sub.add_parser("outline", help="大纲（幕/章/节拍）")
    p_sub = p.add_subparsers(dest="action", required=True)
    p_sub.add_parser("show", help="查看大纲").set_defaults(func=cmd_outline)
    q = p_sub.add_parser("generate", help="生成大纲并写入设定库")
    q.add_argument("--acts", type=int, default=3)
    q.add_argument("--chapters-per-act", dest="chapters_per_act", type=int, default=3)
    q.add_argument("--beats-per-chapter", dest="beats_per_chapter", type=int, default=3)
    q.add_argument("--words", type=int, default=1200, help="单章目标字数")
    q.add_argument("--binding", choices=["auto", "none"], default="auto", help="auto=生成后把节拍绑定到已有人物")
    q.add_argument("--multi-turn", dest="multi_turn", action="store_true", help="按幕分批生成（单轮工具调用只产出一幕）")
    q.add_argument("--acts-per-round", dest="acts_per_round", type=int, default=1, help="每轮计划生成的幕数（默认 1）")
    q.add_argument("--dry-run", action="store_true")
    q.set_defaults(func=cmd_outline)
    q = p_sub.add_parser("apply", help="把大纲落地成章节文件")
    q.add_argument("--dry-run", action="store_true")
    q.set_defaults(func=cmd_outline)
    p_sub.add_parser("check", help="黄金三章检查（不阻断）").set_defaults(func=cmd_outline_check)
    q = p_sub.add_parser("bind", help="更新章节的世界 / POV 绑定")
    q.add_argument("--chapter", required=True)
    q.add_argument("--worlds", help="世界 ID，逗号分隔；传空串表示清空")
    q.add_argument("--pov", help="POV 角色 ID；传空串表示清空")
    q.set_defaults(func=cmd_outline_bind)

    p = sub.add_parser("world", help="世界条目（多世界 / 封面图）")
    p_sub = p.add_subparsers(dest="action", required=True)
    p_sub.add_parser("list", help="列出世界条目").set_defaults(func=cmd_world_list)
    q = p_sub.add_parser("generate", help="AI 生成世界条目（默认追加落盘）")
    q.add_argument("--count", type=int, help="世界条数（1..6）")
    q.add_argument("--brief", help="补充要求，如「外星世界，双恒星」")
    q.add_argument("--overwrite", action="store_true", help="用生成结果替换全部世界条目")
    q.add_argument("--dry-run", action="store_true", help="只预览，不落盘")
    q.set_defaults(func=cmd_world_generate)
    q = p_sub.add_parser("apply", help="从 JSON 导入世界条目（带 id 且存在则更新）")
    q.add_argument("--file", required=True, help="JSON 文件路径，- 表示从 stdin 读取")
    q.set_defaults(func=cmd_world_apply)
    q = p_sub.add_parser("remove", help="按 ID 删除世界条目（被章节引用时拒绝）")
    q.add_argument("--id", required=True)
    q.set_defaults(func=cmd_world_remove)
    q = p_sub.add_parser("image", help="生成世界封面并写回 cover_asset")
    q.add_argument("--id", required=True)
    q.add_argument("--force", action="store_true", help="强制重绘")
    q.set_defaults(func=cmd_world_image)

    p = sub.add_parser("character", help="角色与音色")
    p_sub = p.add_subparsers(dest="action", required=True)
    q = p_sub.add_parser("add")
    q.add_argument("--name", required=True)
    q.add_argument("--role", default="supporting", choices=["protagonist", "antagonist", "supporting", "minor"])
    q.add_argument("--alias", action="append")
    q.add_argument("--appearance", default="")
    q.set_defaults(func=cmd_character_add)
    p_sub.add_parser("list").set_defaults(func=cmd_character_list)
    q = p_sub.add_parser("bind-voice")
    q.add_argument("--character", required=True)
    q.add_argument("--voice", required=True)
    q.add_argument("--adapter", default="placeholder")
    q.add_argument("--speed", type=float, default=1.0)
    q.set_defaults(func=cmd_character_bind_voice)
    q = p_sub.add_parser("generate", help="AI 生成人物（数量按需求自行判断）")
    q.add_argument("--count", type=int, help="人物总数；缺省时由模型按需求决定生成几个角色")
    q.add_argument("--brief", help="补充要求，如「一个主角和一个反派」")
    q.add_argument("--dry-run", action="store_true", help="只预览，不写 characters.json")
    q.set_defaults(func=cmd_character_generate)
    q = p_sub.add_parser("image", help="生成角色全身像（母图）/ 头像 / 三视图并写回 image_assets")
    q.add_argument("--id", required=True)
    q.add_argument("--kind", required=True, choices=["fullbody", "closeup", "turnaround"])
    q.add_argument("--force", action="store_true", help="强制重绘")
    q.set_defaults(func=cmd_character_image)
    q = p_sub.add_parser("apply", help="从 JSON 批量追加人物")
    q.add_argument("--file", required=True, help="JSON 文件路径，- 表示从 stdin 读取")
    q.set_defaults(func=cmd_character_apply)
    q = p_sub.add_parser("update", help="按 ID 更新角色字段")
    q.add_argument("--id", required=True)
    q.add_argument("--name")
    q.add_argument("--role", choices=["protagonist", "antagonist", "supporting", "minor"])
    q.add_argument("--gender", choices=["male", "female", "other", "unknown"])
    q.add_argument("--age-range", dest="age_range")
    q.add_argument("--personality", help="性格标签，逗号/顿号分隔")
    q.add_argument("--background")
    q.add_argument("--aliases", help="别名，逗号/顿号分隔")
    q.add_argument("--appearance-prompt", dest="appearance_prompt", help="角色外貌提示词（配图用，空串表示清空）")
    q.add_argument("--negative-prompt", dest="negative_prompt", help="角色负向提示词（配图用，空串表示清空）")
    q.add_argument("--situation", help="当前处境")
    q.add_argument("--relationship", action="append", help="`目标ID=备注`，可重复")
    q.add_argument("--relationship-remove", dest="relationship_remove", action="append", help="要移除的关系目标ID，可重复")
    q.set_defaults(func=cmd_character_update)
    q = p_sub.add_parser("remove", help="按 ID 删除角色（旁白不可删）")
    q.add_argument("--id", required=True)
    q.set_defaults(func=cmd_character_remove)

    p = sub.add_parser("voice", help="音色库（角色 → 多变体）")
    p_sub = p.add_subparsers(dest="action", required=True)
    p_sub.add_parser("list").set_defaults(func=cmd_voice)
    q = p_sub.add_parser("show")
    q.add_argument("--voice", required=True)
    q.set_defaults(func=cmd_voice)

    q = p_sub.add_parser("builtin", help="内置音色：list / install / import")
    q.add_argument("action", nargs="?", choices=["list", "install", "import"], default="list")
    q.add_argument("--key", action="append", help="只处理指定内置音色键，可重复")
    q.add_argument("--force", action="store_true", help="install 时强制重新下载")
    q.set_defaults(func=cmd_voice_builtin)

    def _add_variant_args(q, *, need_name: bool = False) -> None:
        """为音色/变体命令挂上素材参数。"""
        if need_name:
            q.add_argument("--variant-name", required=True, dest="variant_name", help="变体名称，如「默认」「愤怒」")
        q.add_argument("--source", choices=["preset", "cloned", "recorded"], default="preset")
        q.add_argument(
            "--preset",
            choices=["Vivian", "Serena", "Uncle_Fu", "Dylan", "Eric", "Ryan", "Aiden", "Ono_Anna", "Sohee"],
            help="预设音色（source=preset 时必填）",
        )
        q.add_argument("--audio", help="参考音频路径（source=cloned/recorded 时必填）")
        q.add_argument("--duration-ms", type=int, dest="duration_ms", help="参考音频时长；缺省用 ffprobe 探测")
        q.add_argument("--ref-text", dest="ref_text", default=None, help="参考音频对应文本")
        q.add_argument("--consent", action="store_true", help="确认已获授权（真人参考音频必填）")
        q.add_argument("--consent-note", dest="consent_note", default=None, help="授权说明")
        q.add_argument("--emotion-tag", action="append", dest="emotion_tag", help="适用情绪，可重复；默认 default")
        q.add_argument("--source-status", choices=["ready", "pending", "unusable"], dest="source_status")
        q.add_argument("--provider", help="记录 provider 可用性（可选）")

    q = p_sub.add_parser("add")
    q.add_argument("--name", required=True)
    q.add_argument("--gender", choices=["male", "female", "other", "unknown"], default="unknown")
    q.add_argument("--language", default="zh-CN")
    q.add_argument("--tag", action="append")
    q.add_argument("--variant-name", dest="variant_name", default="默认")
    _add_variant_args(q)
    q.set_defaults(func=cmd_voice)

    q = p_sub.add_parser("variant-add")
    q.add_argument("--voice", required=True)
    _add_variant_args(q, need_name=True)
    q.set_defaults(func=cmd_voice)

    q = p_sub.add_parser("variant-update")
    q.add_argument("--voice", required=True)
    q.add_argument("--variant", required=True)
    q.add_argument("--name", dest="variant_name")
    q.add_argument("--emotion-tag", action="append", dest="emotion_tag")
    q.add_argument("--status", dest="source_status", choices=["ready", "pending", "unusable"])
    q.add_argument(
        "--preset",
        choices=["Vivian", "Serena", "Uncle_Fu", "Dylan", "Eric", "Ryan", "Aiden", "Ono_Anna", "Sohee"],
    )
    q.add_argument("--provider")
    q.add_argument("--provider-status", dest="provider_status", choices=["ready", "pending", "unusable"])
    q.set_defaults(func=cmd_voice)

    q = p_sub.add_parser("variant-remove")
    q.add_argument("--voice", required=True)
    q.add_argument("--variant", required=True)
    q.set_defaults(func=cmd_voice)

    q = p_sub.add_parser("set-default")
    q.add_argument("--voice", required=True)
    q.add_argument("--variant", required=True)
    q.set_defaults(func=cmd_voice)

    q = p_sub.add_parser("bind")
    q.add_argument("--character", required=True)
    q.add_argument("--voice", required=True)
    q.add_argument("--adapter", default=None)
    q.set_defaults(func=cmd_voice)

    q = p_sub.add_parser("preview")
    q.add_argument("--voice", required=True)
    q.add_argument("--variant", required=True)
    q.add_argument("--text")
    q.add_argument("--emotion", default="neutral")
    q.add_argument("--instruct")
    q.set_defaults(func=cmd_voice)

    p = sub.add_parser("script", help="导演脚本（人工标注，规则解析）")
    p_sub = p.add_subparsers(dest="action", required=True)
    p_sub.add_parser("syntax", help="打印标注语法").set_defaults(func=cmd_script)
    q = p_sub.add_parser("suggest", help="按场景与台词生成标注建议")
    q.add_argument("--chapter", required=True)
    q.add_argument("--apply", action="store_true", help="解析并同步进章节（含音效/画面/视频位）")
    q.add_argument("--dry-run", action="store_true", help="只预览，不写盘")
    q.add_argument("--out", help="标注文本输出路径，默认 scripts/<chapter>.markup.txt")
    q.add_argument("--scenes-per-round", dest="scenes_per_round", type=int, help="每批镜头数（默认取配置 script_scenes_per_round）")
    q.set_defaults(func=cmd_script_suggest)
    q = p_sub.add_parser("parse")
    q.add_argument("--chapter", required=True)
    q.add_argument("--file", required=True)
    q.add_argument("--dry-run", action="store_true")
    q.set_defaults(func=cmd_script)
    q = p_sub.add_parser("apply", help="把脚本台词同步进章节")
    q.add_argument("--chapter", required=True)
    q.set_defaults(func=cmd_script)
    q = p_sub.add_parser("show")
    q.add_argument("--chapter", required=True)
    q.set_defaults(func=cmd_script)
    q = p_sub.add_parser("validate")
    q.add_argument("--chapter", required=True)
    q.set_defaults(func=cmd_script)

    p = sub.add_parser("scene", help="场景")
    p_sub = p.add_subparsers(dest="action", required=True)
    q = p_sub.add_parser("split")
    q.add_argument("--chapter", required=True)
    q.add_argument("--max-chars", type=int, default=1200)
    q.add_argument("--force", action="store_true")
    q.add_argument("--dry-run", action="store_true")
    q.set_defaults(func=cmd_scene_split)

    p = sub.add_parser("line", help="台词行")
    p_sub = p.add_subparsers(dest="action", required=True)
    q = p_sub.add_parser("split")
    q.add_argument("--chapter", required=True)
    q.add_argument("--scene", action="append")
    q.add_argument("--force", action="store_true")
    q.add_argument("--dry-run", action="store_true")
    q.set_defaults(func=cmd_line_split)
    q = p_sub.add_parser("list", help="列出派生状态，可按状态/行 ID 筛选")
    q.add_argument("--chapter", required=True)
    q.add_argument("--line", action="append", help="只看这些行，可重复")
    q.add_argument(
        "--status",
        help="逗号分隔的派生状态：pending,stale,failed,ready（历史值 unknown=未识别说话人）",
    )
    q.set_defaults(func=cmd_line_list)
    q = p_sub.add_parser("diff", help="查看一条台词相对上次生成改了哪些参数")
    q.add_argument("--chapter", required=True)
    q.add_argument("--line", required=True)
    q.set_defaults(func=cmd_line_diff)
    q = p_sub.add_parser("revert", help="把情绪/强度/语速/交付说明回滚到上次生成时的值（文本不回滚）")
    q.add_argument("--chapter", required=True)
    q.add_argument("--line", required=True)
    q.set_defaults(func=cmd_line_revert)
    q = p_sub.add_parser("set")
    q.add_argument("--chapter", required=True)
    q.add_argument("--line", required=True)
    q.add_argument("--speaker")
    q.add_argument("--emotion", choices=list(entities.EMOTIONS))
    q.add_argument("--emotion-intensity", type=float, dest="emotion_intensity")
    q.add_argument("--delivery-note", dest="delivery_note")
    q.add_argument("--speed", type=float)
    q.add_argument("--pause-before", type=int, dest="pause_before")
    q.add_argument("--pause-after", type=int, dest="pause_after")
    q.add_argument("--text")
    q.add_argument("--kind", choices=["dialogue", "narration", "inner_monologue"])
    q.set_defaults(func=cmd_line_set)

    p = sub.add_parser("group", help="虚拟分组（按条件批量改行参数，物化写入 .ndw/groups）")
    p_sub = p.add_subparsers(dest="action", required=True)
    q = p_sub.add_parser("list", help="列出本章分组，并检测未分组/重复分组的行")
    q.add_argument("--chapter", required=True)
    q.set_defaults(func=cmd_group_list)
    q = p_sub.add_parser("diff", help="预览分组会改动哪些行、字段的旧值→新值")
    q.add_argument("--chapter", required=True)
    q.add_argument("--group", required=True)
    q.set_defaults(func=cmd_group_diff)
    q = p_sub.add_parser("apply", help="把分组物化写入行参数（命中行会派生为 stale）")
    q.add_argument("--chapter", required=True)
    q.add_argument("--group", required=True)
    q.set_defaults(func=cmd_group_apply)

    p = sub.add_parser("tts", help="配音")
    p_sub = p.add_subparsers(dest="action", required=True)
    q = p_sub.add_parser("plan", help="规划要合成的行（范围选择器 + 状态筛选）")
    q.add_argument("--chapter", required=True)
    _add_scope_args(q)
    q.set_defaults(func=cmd_tts)
    q = p_sub.add_parser("run", help="按范围入队合成")
    q.add_argument("--chapter", required=True)
    q.add_argument("--wait", action="store_true")
    q.add_argument("--force", action="store_true", help="忽略状态筛选与产物缓存，强制重新生成（含已就绪行）")
    _add_scope_args(q)
    q.set_defaults(func=cmd_tts)
    q = p_sub.add_parser("batch", help="行级批量合成（单并发，失败不中断，支持跨章节与缓存续跑）")
    q.add_argument("--chapter", help="只处理该章节")
    q.add_argument("--chapters", help="逗号分隔的多个章节 ID")
    q.add_argument("--all-chapters", action="store_true", dest="all_chapters", help="批量处理工程内全部章节")
    q.add_argument("--scene", action="append", help="只跑指定场景，可重复")
    q.add_argument("--line", action="append", help="只跑指定行，可重复")
    q.add_argument(
        "--status",
        help="逗号分隔的派生状态：pending,stale,failed,ready（ready 会连已完成行一起重跑）",
    )
    q.add_argument("--limit", type=int, help="每章最多合成多少行")
    q.add_argument("--batch-size", type=int, default=5, dest="batch_size", help="每批行数（默认 5）")
    q.add_argument("--include-ready", action="store_true", dest="include_ready", help="连已完成的行一起重跑（命中缓存则不重新合成）")
    q.add_argument("--dry-run", action="store_true", help="只列出待合成行")
    q.set_defaults(func=cmd_tts_batch)

    p_sub.add_parser("providers", help="列出 provider 可用性与许可").set_defaults(func=cmd_tts_providers)

    # route/say/compare 共享同一组需求参数
    def _add_tts_inputs(q) -> None:
        """为配音子命令挂上通用的文本与路由参数。"""
        q.add_argument("--text", help="台词文本")
        q.add_argument("--text-file", help="从文件读取台词文本")
        q.add_argument("--emotion", help="情绪：neutral/joy/sad/angry/fear/surprise/tender/excited/whisper/serious，也接受中文如 愤怒/悲伤/温柔")
        q.add_argument("--emotion-intensity", type=int, default=0, help="情绪强度 0~3")
        q.add_argument("--paralinguistic", action="append", help="副语言标记，可逗号分隔：laugh,sigh")
        q.add_argument("--instruct", help="自然语言风格指令（交付说明，走 emo_text 通路）")
        q.add_argument("--emo-text", dest="emo_text", help="文字情绪描述，走 emo_text 通路；与 --emotion/--emo-audio-prompt 互斥")
        q.add_argument("--emo-audio-prompt", dest="emo_audio_prompt", help="情绪参考音频，走 emo_audio_prompt 通路；与 --emotion/--emo-text 互斥")
        q.add_argument("--language", default="zh-CN", help="语言标签")
        q.add_argument("--target-duration-ms", type=int, help="期望时长（毫秒），不足补静音")
        q.add_argument("--usage", choices=["personal", "commercial"], default="personal", help="工程用途，影响许可过滤")
        q.add_argument("--provider", help="锁定 provider（行级）")
        q.add_argument("--character-provider", help="角色绑定的 provider（角色级）")
        q.add_argument("--ref-audio", help="参考音频，缺省用配置默认音色")
        q.add_argument("--ref-text", help="参考音频对应的文本")
        q.add_argument("--voice-id", help="音色变体 id，仅用于记录")

    q = p_sub.add_parser("route", help="只做路由决策，不合成")
    _add_tts_inputs(q)
    q.set_defaults(func=cmd_tts_route)

    q = p_sub.add_parser("say", help="合成一句话并落盘")
    _add_tts_inputs(q)
    q.add_argument("--out", help="输出 wav 路径")
    q.set_defaults(func=cmd_tts_say)

    q = p_sub.add_parser("compare", help="同句多 provider 对比试听")
    _add_tts_inputs(q)
    q.add_argument("--providers", help="参与对比的 provider，逗号分隔；缺省为全部可自动选中的")
    q.add_argument("--out-dir", help="对比产物目录")
    q.set_defaults(func=cmd_tts_compare)

    for kind in ("sfx", "image", "video"):
        p = sub.add_parser(kind, help={"sfx": "音效", "image": "关键图", "video": "5 秒短视频"}[kind])
        p_sub = p.add_subparsers(dest="action", required=True)
        q = p_sub.add_parser("plan")
        q.add_argument("--chapter", required=True)
        q.set_defaults(func=cmd_media, media_kind=kind)
        q = p_sub.add_parser("run", help="真实生成（离线渲染器；--force 覆盖已就绪项）")
        q.add_argument("--chapter", required=True)
        q.add_argument("--force", action="store_true", help="已就绪项也重新生成")
        q.add_argument("--limit", type=int, help="本次最多处理条数")
        q.add_argument("--dry-run", action="store_true", help="只统计待生成条目，不写盘")
        q.set_defaults(func=cmd_media, media_kind=kind)

    p = sub.add_parser("timeline", help="时间轴")
    p_sub = p.add_subparsers(dest="action", required=True)
    q = p_sub.add_parser("build")
    q.add_argument("--chapter", required=True)
    q.add_argument("--dry-run", action="store_true")
    q.set_defaults(func=cmd_timeline)
    q = p_sub.add_parser("show")
    q.add_argument("--chapter", required=True)
    q.set_defaults(func=cmd_timeline)
    q = p_sub.add_parser("table", help="输出可核对的时间表（TSV）")
    q.add_argument("--chapter", required=True)
    q.add_argument("--out", help="把时间表写到文件")
    q.set_defaults(func=cmd_timeline)

    p = sub.add_parser("concat", help="整章语音拼接（含静音填充与响度规整）")
    p.add_argument("--chapter", required=True)
    p.add_argument("--out", help="输出 wav 路径")
    p.add_argument("--no-normalize", action="store_true", dest="no_normalize", help="跳过整章响度规整")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_concat)

    p = sub.add_parser("bgm", help="背景音乐轨（导入现成音频、按时间轴放置）")
    p_sub = p.add_subparsers(dest="action", required=True)
    q = p_sub.add_parser("add", help="导入音频并放置一条 cue")
    q.add_argument("--chapter", required=True)
    q.add_argument("--file", required=True, help="外部音频文件（会复制进 assets/audio/bgm/）")
    q.add_argument("--start-ms", type=int, default=0, dest="start_ms")
    q.add_argument("--gain-db", type=float, default=-18.0, dest="gain_db")
    q.add_argument("--fade-in-ms", type=int, default=1500, dest="fade_in_ms")
    q.add_argument("--fade-out-ms", type=int, default=2000, dest="fade_out_ms")
    q.add_argument("--no-loop", action="store_true", dest="no_loop")
    q.add_argument("--loop-until-ms", type=int, dest="loop_until_ms")
    q.set_defaults(func=cmd_bgm)
    q = p_sub.add_parser("list", help="列出章节 BGM cue")
    q.add_argument("--chapter", required=True)
    q.set_defaults(func=cmd_bgm)
    q = p_sub.add_parser("clear", help="清空章节 BGM cue")
    q.add_argument("--chapter", required=True)
    q.set_defaults(func=cmd_bgm)

    p = sub.add_parser("mux", help="混流：音轨 + 画面合成 MP4")
    p.add_argument("--chapter", required=True)
    p.add_argument("--render", action="store_true", help="真正执行混流（ffmpeg）")
    p.set_defaults(func=cmd_mux)

    p = sub.add_parser("mix", help="混音")
    p.add_argument("--chapter", required=True)
    p.add_argument("--render", action="store_true", help="真正执行渲染：分轨 + 混音 + 响度规整")
    p.set_defaults(func=cmd_mix)

    p = sub.add_parser("export", help="导出成片")
    p.add_argument("--chapter", required=True)
    p.add_argument("--format", default="srt", choices=["srt", "bundle"], help="srt=字幕与清单 / bundle=交付包（MP3+MP4+SRT+README）")
    p.add_argument("--out-dir", dest="out_dir", help="输出目录，默认 <工程>/exports")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("comfy", help="ComfyUI 模板 / 健康检查 / 模型 / 导入")
    p_sub = p.add_subparsers(dest="action", required=True)
    for name in ("list", "templates"):
        q = p_sub.add_parser(name, help="列出模板、必需占位符、固定参数与依赖模型")
        q.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
        q.set_defaults(func=cmd_comfy)
    q = p_sub.add_parser("health", help="连通性 + 模板校验 + 依赖模型")
    q.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    q.set_defaults(func=cmd_comfy)
    q = p_sub.add_parser("scan", help="扫描模型目录并写回 models.json")
    q.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    q.set_defaults(func=cmd_comfy)
    q = p_sub.add_parser("resolve", help="反查模型下载地址")
    q.add_argument("--refresh", action="store_true", help="忽略缓存重新解析")
    q.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    q.set_defaults(func=cmd_comfy)
    q = p_sub.add_parser("import", help="导入 UI/API 工作流为模板")
    q.add_argument("--id", required=True, help="模板 ID（写入 <id>.json）")
    q.add_argument("--kind", required=True, choices=["image", "video"])
    q.add_argument("--workflow", help="ComfyUI UI 格式工作流 JSON（自动转 API 格式）")
    q.add_argument("--file", help="ComfyUI API 格式工作流 JSON")
    q.add_argument("--force", action="store_true", help="覆盖已存在的模板文件")
    q.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    q.set_defaults(func=cmd_comfy)
    q = p_sub.add_parser("models", help="模型清单 / 校验 / 下载")
    q_sub = q.add_subparsers(dest="models_action", required=True)
    r = q_sub.add_parser("list", help="列出模板依赖模型的就位情况")
    r.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    r.set_defaults(func=cmd_comfy)
    r = q_sub.add_parser("verify", help="核对模型是否就位且体积一致")
    r.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    r.set_defaults(func=cmd_comfy)
    r = q_sub.add_parser("install", help="下载单个模型文件")
    r.add_argument("--key", required=True, help="相对 models 目录的下载键")
    r.add_argument("--yes", action="store_true", help="已确认体积")
    r.add_argument("--accept-license", action="store_true", dest="accept_license", help="已确认许可限制")
    r.add_argument("--dry-run", action="store_true", dest="dry_run", help="只输出下载计划")
    r.add_argument("--mirror", action="append", help="镜像地址（可多次给出，按顺序尝试）")
    r.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    r.set_defaults(func=cmd_comfy)

    p = sub.add_parser("queue", help="串行队列")
    p_sub = p.add_subparsers(dest="action", required=True)
    q = p_sub.add_parser("list")
    q.add_argument("--status")
    q.set_defaults(func=cmd_queue)
    q = p_sub.add_parser("run")
    q.add_argument("--once", action="store_true")
    q.set_defaults(func=cmd_queue)
    q = p_sub.add_parser("retry")
    q.add_argument("--job", required=True)
    q.set_defaults(func=cmd_queue)
    q = p_sub.add_parser("cancel")
    q.add_argument("--job", required=True)
    q.set_defaults(func=cmd_queue)

    p = sub.add_parser("pipeline", help="串联阶段")
    p.add_argument("action", nargs="?", default="run", choices=["run"], help="固定为 run，可省略")
    p.add_argument("--chapter", required=True)
    p.add_argument("--from", dest="from_stage", choices=["split", "lines", "tts", "timeline"])
    p.add_argument("--to", dest="to_stage", choices=["split", "lines", "tts", "timeline"])
    p.add_argument("--only", help="逗号分隔的阶段，如 split,lines")
    p.add_argument("--force", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_pipeline)

    p = sub.add_parser("models", help="模型仓库（列清单/校验/导入，不下载）")
    p_sub = p.add_subparsers(dest="action", required=True)
    q = p_sub.add_parser("list", help="列出清单与就位状态")
    q.add_argument("--present", action="store_true", help="只看已就位项")
    q.set_defaults(func=cmd_models_list)
    q = p_sub.add_parser("verify", help="校验文件完整性；--load 真加载一次")
    q.add_argument("--key", action="append", help="只验指定目录键，可重复")
    q.add_argument("--load", action="store_true", help="调用 mlx 真加载（每个约 1~2 分钟）")
    q.set_defaults(func=cmd_models_verify)
    q = p_sub.add_parser("install", help="从 hf-mirror 下载模型（先落暂存，校验后 mv）")
    q.add_argument("--key", required=True, help="目录键，如 cosyvoice3/8bit")
    q.add_argument("--yes", action="store_true", help="确认体积（超过 15G 或体积未知时必填）")
    q.add_argument("--accept-license", action="store_true", help="确认非商用/需授权许可")
    q.add_argument("--dry-run", action="store_true", help="只报体积与许可，不下载")
    q.add_argument(
        "--mirror",
        action="append",
        help="覆盖默认镜像链，可重复（默认先 https://hf-mirror.com，再 https://huggingface.co）",
    )
    q.set_defaults(func=cmd_models_install)
    q = p_sub.add_parser("import", help="把已有权重目录纳入模型根目录")
    q.add_argument("--from", dest="src", required=True, help="源目录")
    q.add_argument("--key", required=True, help="目标目录键，如 cosyvoice3/8bit")
    q.add_argument("--keep-source", action="store_true", help="跨卷迁移后保留源目录（默认删除）")
    q.set_defaults(func=cmd_models_import)

    p = sub.add_parser("produce", help="全链路编排：设定 → 大纲 → 正文 → 标注 → 配音 → 音效/配图/视频 → 混流 → 导出")
    p.add_argument("--chapter", required=True)
    p.add_argument("--from", dest="from_stage", choices=list(PRODUCE_STAGES))
    p.add_argument("--to", dest="to_stage", choices=list(PRODUCE_STAGES))
    p.add_argument("--no-resume", dest="no_resume", action="store_true", help="不跳过上次已成功的阶段")
    p.add_argument("--force", action="store_true", help="强制重跑正文/脚本/媒体")
    p.add_argument("--keep-going", dest="keep_going", action="store_true", help="阶段失败后继续后续阶段")
    p.add_argument("--dry-run", action="store_true", help="只体检前置条件，不写盘")
    p.set_defaults(func=cmd_produce)

    p = sub.add_parser("run", help="查看编排记录")
    p_sub = p.add_subparsers(dest="action", required=True)
    q = p_sub.add_parser("show", help="查看某次或最近一次编排")
    q.add_argument("--run", help="run_id；缺省取最近一次")
    q.set_defaults(func=cmd_run_show)

    p = sub.add_parser("serve", help="启动本地 Web 工作台")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.set_defaults(func=cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    """命令行主入口。

    参数:
        argv: 参数列表（默认取 sys.argv）。

    返回:
        进程退出码。
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    config = load_config(args.config)
    try:
        return args.func(args, config)
    except AdapterError as exc:
        print(f"[{exc.code.value}] {exc.message}", file=sys.stderr)
        return EXIT_NOT_IMPLEMENTED if exc.code == ErrorCode.NOT_IMPLEMENTED else EXIT_FAILED
    except TtsError as exc:
        print(f"[{exc.code}] {exc.message}", file=sys.stderr)
        if exc.hint:
            print(f"  提示：{exc.hint}", file=sys.stderr)
        return EXIT_NOT_IMPLEMENTED if exc.code == "NOT_IMPLEMENTED" else EXIT_FAILED
    except SchemaValidationError as exc:
        print(f"[SCHEMA] {exc}", file=sys.stderr)
        return EXIT_SCHEMA
    except TaskFailedError as exc:
        print(f"[FAILED] {exc}", file=sys.stderr)
        return EXIT_FAILED
    except PreconditionError as exc:
        print(f"[PRECONDITION] {exc}", file=sys.stderr)
        return EXIT_PRECONDITION
    except FileExistsError as exc:
        print(f"[EXISTS] {exc}", file=sys.stderr)
        return EXIT_REJECTED
    except (FileNotFoundError, KeyError) as exc:
        print(f"[NOT_FOUND] {exc}", file=sys.stderr)
        return EXIT_PRECONDITION
    except ValueError as exc:
        print(f"[USAGE] {exc}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
