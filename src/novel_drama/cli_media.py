"""媒体相关 CLI：批量配音、时间轴、拼接、导出、混流。"""

from __future__ import annotations

import json
from pathlib import Path

from .cli_common import (
    EXIT_FAILED,
    EXIT_NOT_IMPLEMENTED,
    EXIT_OK,
    PreconditionError,
    _emit,
    _resolve_store,
    _tts_service,
)
from .media import (
    add_cue,
    build_schedule,
    build_timeline,
    concat_voice,
    format_schedule_tsv,
    import_asset,
    load_cues,
    normalize_audio,
    plan_concat,
    plan_mix,
    plan_mux,
    render_mix,
    summarize_timeline,
    write_export,
)
from .models.ids import now_iso
from .store import ProjectStore, validate


def _target_chapters(store: ProjectStore, args) -> list[str]:
    """解析本次要处理的章节列表。

    参数:
        store: 工程。
        args: 已解析参数（--chapter / --chapters / --all-chapters）。

    返回:
        章节 ID 列表。

    异常:
        PreconditionError: 未指定章节。
    """
    raw = getattr(args, "chapters", None)
    if raw:
        return [item.strip() for item in raw.split(",") if item.strip()]
    if getattr(args, "all_chapters", False):
        chapters = [item["id"] for item in store.list_chapters()]
        if not chapters:
            raise PreconditionError("工程里还没有章节")
        return chapters
    chapter = getattr(args, "chapter", None)
    if not chapter:
        raise PreconditionError("请用 --chapter 指定章节，或用 --all-chapters 批量处理")
    return [chapter]


# ---------------- 配音：行级批量合成 ----------------


def _silence_events(store: ProjectStore, chapter_id: str) -> list[dict]:
    """从导演脚本里拍平静音事件（缺失脚本时返回空列表）。

    参数:
        store: 工程。
        chapter_id: 章节 ID。

    返回:
        静音事件列表（已展开 shots）。
    """
    path = store.script_path(chapter_id)
    if not path.exists():
        return []
    script = json.loads(path.read_text(encoding="utf-8"))
    events = []
    for shot in script.get("shots") or []:
        events.extend(shot.get("silence_events") or [])
    return events


def _line_filter(store: ProjectStore, chapters: list[dict], args, config) -> tuple[set[str] | None, bool]:
    """按 `--line` / `--status` 解析本次要处理的行。

    与 `line list` / `tts plan` 共用 `line_state` 的派生状态，避免两套判定打架。

    参数:
        store: 工程。
        chapters: 本次要处理的章节。
        args: 已解析参数。
        config: 本机配置。

    返回:
        `(行 ID 集合, 是否连已完成行一起跑)`；未给筛选时集合为 None。

    异常:
        ValueError: 状态名非法或行 ID 不存在（CLI 转成退出码 2）。
    """
    from .tts import line_state

    statuses = line_state.parse_states(getattr(args, "status", None))
    line_ids = list(getattr(args, "line", None) or [])
    if not statuses and not line_ids:
        return None, bool(args.include_ready)
    engine = line_state.engine_params(config)
    characters = {item["id"]: item for item in store.load_characters()["characters"]}
    live_ids = line_state.live_line_ids(store)
    all_states: list[dict] = []
    for chapter in chapters:
        all_states.extend(
            line_state.chapter_states(
                chapter,
                characters,
                line_state.load_snapshots(store.root, chapter),
                root=store.root,
                live_ids=live_ids,
                engine=engine,
            )
        )
    picked = line_state.select_states(
        all_states,
        scope="selected" if line_ids else "chapter",
        line_ids=line_ids,
        statuses=statuses,
    )
    return {row["line"]["id"] for row in picked}, bool(args.include_ready) or ("ready" in statuses)


def cmd_tts_batch(args, config: AppConfig) -> int:
    """行级批量合成：单并发、可控批次、失败逐行记录不中断；支持跨章节与断点续跑。"""
    from .tts.batch import plan_batch, run_chapters

    store = _resolve_store(args, config)
    chapter_ids = _target_chapters(store, args)
    chapters = [store.load_chapter(chapter_id) for chapter_id in chapter_ids]
    characters = store.load_characters()["characters"]
    line_ids, include_ready = _line_filter(store, chapters, args, config)

    if args.dry_run:
        from .tts import line_state

        lines: list[str] = []
        total = 0
        for chapter in chapters:
            rows = plan_batch(
                chapter,
                characters,
                scene_ids=args.scene,
                include_ready=include_ready,
                limit=args.limit,
                snapshots=line_state.load_snapshots(store.root, chapter),
                root=store.root,
                engine=line_state.engine_params(config),
                live_ids=line_state.live_line_ids(store),
                line_ids=line_ids,
            )
            total += len(rows)
            for row in rows:
                lines.append(
                    f"{chapter.get('id')}  {row['line']['id']}  {row['line'].get('speaker_id')}  {row['line']['text'][:24]}"
                )
        _emit(
            args,
            f"[dry-run] 待合成 {total} 行（{len(chapters)} 章）：\n" + "\n".join(lines),
            {"rows": total, "chapters": chapter_ids},
        )
        return EXIT_OK

    service = _tts_service(args, config)

    def reporter(chapter_no: int, chapter: dict, batch_no: int, batch_results: list[dict], stats: dict) -> None:
        """打印章节内批次进度。"""
        ok = sum(1 for item in batch_results if item["status"] == "ready")
        failed = sum(1 for item in batch_results if item["status"] == "failed")
        cached = sum(1 for item in batch_results if item.get("cache_hit"))
        print(
            f"[章 {chapter_no} {chapter.get('id')} / 批 {batch_no}] 本批 {len(batch_results)} 行："
            f"成功 {ok}（缓存 {cached}）失败 {failed}；累计 {stats['succeeded']}/{stats['requested']}"
        )

    report = run_chapters(
        service,
        store,
        chapters,
        batch_size=args.batch_size,
        dry_run=False,
        reporter=reporter,
        scene_ids=args.scene,
        include_ready=include_ready,
        limit=args.limit,
        line_ids=line_ids,
    )
    stats = report["stats"]
    text = (
        f"批量合成完成：{stats['chapters']} 章 / 请求 {stats['requested']} 行，成功 {stats['succeeded']}"
        f"（缓存命中 {stats['cache_hits']}），失败 {stats['failed']}；"
        f"累计音频 {stats['audio_ms'] / 1000:.1f}s；总耗时 {report['elapsed_ms'] / 1000:.1f}s；"
        f"峰值内存 {report['peak_rss_mb']}MB\n报告 {report['report_path']}"
    )
    if getattr(args, "json", False):
        _emit(args, payload=report)
    else:
        print(text)
        for chapter in report.get("chapters") or []:
            sub = chapter.get("stats") or {}
            if sub:
                print(
                    f"  - {chapter['chapter_id']}  {chapter.get('title') or ''}  "
                    f"成功 {sub.get('succeeded', 0)} 缓存 {sub.get('cache_hits', 0)} 失败 {sub.get('failed', 0)}"
                )
        for row in report["rows"]:
            if row["status"] == "failed":
                print(f"  ! {row['line_id']}  [{row.get('error', {}).get('code')}] {row.get('error', {}).get('message')}")
    return EXIT_OK if stats["failed"] == 0 else EXIT_FAILED


# ---------------- 媒体：dry-run / 混流 ----------------


def cmd_concat(args, config: AppConfig) -> int:
    """整章语音拼接（时间轴 → 单个 wav，含静音填充 + 可选规整）。"""
    store = _resolve_store(args, config)
    path = store.timeline_path(args.chapter)
    if not path.exists():
        raise PreconditionError(f"时间轴不存在: {path}（先执行 ndw timeline build）")
    timeline = json.loads(path.read_text(encoding="utf-8"))
    plan = plan_concat(timeline, store.root)
    if args.out:
        plan["output"] = args.out
    if args.dry_run:
        _emit(
            args,
            f"[dry-run] 输出 {plan['output']}\n片段 {len(plan['segments'])}  缺失 {len(plan['missing'])}  总时长 {plan['duration_ms']}ms",
            plan,
        )
        return EXIT_OK
    result = concat_voice(plan, binary=config.get("ffmpeg.binary", "ffmpeg"))
    drift = abs(int(result["duration_ms"]) - int(plan["duration_ms"]))
    text = (
        f"整章语音已拼接: {result['path']}\n"
        f"时长 {result['duration_ms']}ms（时间轴 {plan['duration_ms']}ms，偏差 {drift}ms）  "
        f"片段 {result['inputs']}  缺失 {len(result['missing'])}  耗时 {result['elapsed_ms']}ms  峰值内存 {result['peak_rss_mb']}MB"
    )
    if result["missing"]:
        text += "\n缺失片段（已用静音填充）: " + ", ".join(str(item) for item in result["missing"])

    if not getattr(args, "no_normalize", False):
        target = Path(plan["output"]).with_name(Path(plan["output"]).stem + ".norm.wav")
        norm = normalize_audio(
            result["path"],
            target,
            audio_cfg=config.get("audio"),
            loudness_target=config.get("output.loudness_target_lufs", -16),
            sample_rate=int(timeline.get("sample_rate") or config.get("output.sample_rate", 48000)),
            channels=int(timeline.get("channels") or config.get("output.channels", 2)),
            binary=config.get("ffmpeg.binary", "ffmpeg"),
        )
        result["normalized"] = norm
        before = norm.get("before") or {}
        after = norm.get("after") or {}
        text += (
            f"\n规整产物: {norm['path']}\n"
            f"响度 {before.get('lufs')} → {after.get('lufs')} LUFS  "
            f"真峰值 {before.get('true_peak')} → {after.get('true_peak')} dBTP  "
            f"高频占比 {before.get('high_ratio_db')} → {after.get('high_ratio_db')} dB（8kHz 以上相对低频）  "
            f"耗时 {norm['elapsed_ms']}ms"
        )
    _emit(args, text, result)
    return EXIT_OK


def cmd_mux(args, config: AppConfig) -> int:
    """混流：默认只出计划，--render 执行音画合成。"""
    store = _resolve_store(args, config)
    path = store.timeline_path(args.chapter)
    if not path.exists():
        raise PreconditionError(f"时间轴不存在: {path}（先执行 ndw timeline build）")
    timeline = json.loads(path.read_text(encoding="utf-8"))
    mix_plan = plan_mix(timeline, store.root)
    plan = plan_mux(timeline, store.root, mix_plan)
    plan["binary"] = config.get("ffmpeg.binary", "ffmpeg")
    if not args.render:
        _emit(
            args,
            f"[{plan['status']}] {plan['reason']}\n输出计划 {plan['output']}\n"
            + ("\n".join(f"  ! {item}" for item in plan["blockers"]) or "  前置条件齐全"),
            plan,
        )
        return EXIT_OK
    if plan["blockers"]:
        raise PreconditionError("；".join(plan["blockers"]))

    from .media import mux as run_mux

    result = run_mux(plan)
    _emit(
        args,
        f"混流完成：{result['path']}\n  时长 {result['duration_ms']}ms  片段 {result['clips']} 个  "
        f"音轨 {result['audio']}  耗时 {result['elapsed_ms']}ms",
        result,
    )
    return EXIT_OK


# ---------------- timeline / mix / export ----------------


def cmd_timeline(args, config: AppConfig) -> int:
    """构建、查看时间轴，或输出可核对的时间表。"""
    store = _resolve_store(args, config)
    chapter = store.load_chapter(args.chapter)
    silences = _silence_events(store, chapter["id"])
    if args.action == "show":
        path = store.timeline_path(chapter["id"])
        if not path.exists():
            raise PreconditionError(f"时间轴不存在: {path}（先执行 ndw timeline build）")
        timeline = json.loads(path.read_text(encoding="utf-8"))
        _emit(args, summarize_timeline(timeline), timeline)
        return EXIT_OK

    if args.action == "table":
        path = store.timeline_path(chapter["id"])
        if not path.exists():
            raise PreconditionError(f"时间轴不存在: {path}（先执行 ndw timeline build）")
        timeline = json.loads(path.read_text(encoding="utf-8"))
        characters = {item["id"]: item for item in store.load_characters()["characters"]}
        rows = build_schedule(timeline, chapter, characters, silences)
        tsv = format_schedule_tsv(rows)
        out = getattr(args, "out", None)
        if out:
            target = Path(out)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(tsv, encoding="utf-8")
        _emit(args, tsv + (f"\n时间表已写入 {out}" if out else ""), {"rows": rows, "out": out})
        return EXIT_OK

    existing = None
    path = store.timeline_path(chapter["id"])
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
    timeline = build_timeline(chapter, store.settings(), existing=existing, silence_events=silences)
    summary = summarize_timeline(timeline)
    if args.dry_run:
        _emit(args, "[dry-run] " + summary + "\n" + "\n".join(timeline.get("warnings") or []), timeline)
        return EXIT_OK
    timeline["built_at"] = now_iso()
    validate(timeline, "timeline.schema.json")
    store._write_json(path, timeline)  # 时间轴为派生物，不走章节校验
    warnings = "\n".join(timeline.get("warnings") or [])
    _emit(args, summary + ("\n" + warnings if warnings else ""), timeline)
    return EXIT_OK


def cmd_export(args, config: AppConfig) -> int:
    """导出：默认只出 SRT/清单/时间表；--format bundle 额外收 MP3/MP4 与交付说明。"""
    store = _resolve_store(args, config)
    path = store.timeline_path(args.chapter)
    if not path.exists():
        raise PreconditionError("时间轴不存在，先执行 ndw timeline build")
    timeline = json.loads(path.read_text(encoding="utf-8"))
    chapter = store.load_chapter(args.chapter)
    characters = {item["id"]: item for item in store.load_characters()["characters"]}
    out_dir = Path(args.out_dir) if args.out_dir else store.root / "exports"
    silences = _silence_events(store, chapter["id"])
    if getattr(args, "format", "srt") == "bundle":
        from .media import export_bundle

        result = export_bundle(
            store,
            chapter,
            timeline,
            characters,
            silences,
            out_dir=out_dir,
            binary=config.get("ffmpeg.binary", "ffmpeg"),
        )
        lines = [f"交付包: {result['dir']}"]
        for item in result["artifacts"]:
            lines.append(f"  - {item['kind']:<8} {item['status']:<8} {item['path']}")
        lines.append(f"清单: {result['bundle']}")
        lines.append(f"说明: {result['readme']}")
        if result["missing"]:
            lines.extend(["缺失项："] + [f"  ! {item}" for item in result["missing"]])
        _emit(args, "\n".join(lines), result)
        return EXIT_OK
    result = write_export(timeline, chapter, characters, out_dir, silences)
    _emit(
        args,
        f"SRT: {result['srt']}\n清单: {result['manifest']}\n时间表: {result['schedule']}\n字幕条目: {result['entries']}",
        result,
    )
    return EXIT_OK


def cmd_bgm(args, config: AppConfig) -> int:
    """BGM 轨管理：导入现成音频、按时间轴放置、设置增益与淡入淡出。"""
    store = _resolve_store(args, config)
    if args.action == "add":
        if not args.file:
            raise PreconditionError("请用 --file 指定要导入的音频文件")
        asset_id = import_asset(store.root, args.file)
        cue = add_cue(
            store.root,
            args.chapter,
            asset_id,
            start_ms=args.start_ms,
            gain_db=args.gain_db,
            fade_in_ms=args.fade_in_ms,
            fade_out_ms=args.fade_out_ms,
            loop=not args.no_loop,
            loop_until_ms=args.loop_until_ms,
        )
        _emit(args, f"已加入 BGM cue: {cue['id']}  {cue['asset_id']}  start={cue['start_ms']}ms  gain={cue['gain_db']}dB", cue)
        return EXIT_OK
    if args.action == "clear":
        from .media import save_cues

        save_cues(store.root, args.chapter, [])
        _emit(args, f"已清空 {args.chapter} 的 BGM cue", {"chapter": args.chapter})
        return EXIT_OK

    cues = load_cues(store.root, args.chapter)
    if getattr(args, "json", False):
        _emit(args, payload={"chapter": args.chapter, "cues": cues})
        return EXIT_OK
    if not cues:
        print(f"{args.chapter} 还没有 BGM cue")
        return EXIT_OK
    for cue in cues:
        print(
            f"{cue['id']}  {cue.get('name') or ''}  {cue['asset_id']}  "
            f"start={cue['start_ms']}ms  gain={cue.get('gain_db')}dB  "
            f"fade={cue.get('fade_in_ms')}/{cue.get('fade_out_ms')}ms  loop={cue.get('loop')}"
        )
    return EXIT_OK
