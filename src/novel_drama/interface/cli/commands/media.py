"""媒体相关 CLI：批量配音、时间轴、拼接、导出、混流。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from novel_drama.application.production.media_ops import (
    cast_warning_text,
    export_chapter,
    run_media_kind,
)
from novel_drama.application.production.mix import mix_chapter
from novel_drama.application.production.timeline import (
    build_chapter_timeline,
    silence_events,
)
from novel_drama.application.production.tts_batch_run import (
    BatchOptions,
    check_cast_gate,
    line_filter,
    run_batch,
    target_chapters,
    unknown_line_ids,
)
from novel_drama.application.production.tts_plan import plan_batch_preview
from novel_drama.composition import (
    ProjectStore,
    build_schedule,
    concat_voice,
    format_schedule_tsv,
    load_manifest,
    normalize_audio,
    plan_concat,
    plan_media,
    plan_mix,
    plan_mux,
    run_media,
    summarize_timeline,
    templates_dir,
)
from novel_drama.infrastructure.config import AppConfig
from novel_drama.interface.cli.commands.sfx import cmd_sfx_generate, cmd_sfx_providers

from ..common import (
    EXIT_FAILED,
    EXIT_OK,
    PreconditionError,
    _emit,
    _resolve_store,
)


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

    result = run_media_kind(
        store,
        config,
        args.chapter,
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
    result = mix_chapter(
        str(store.root),
        args.chapter,
        config_path=getattr(args, "config", None),
        render=args.render,
    )
    if not args.render:
        _emit(args, json.dumps(result, ensure_ascii=False, indent=2), result)
        return EXIT_OK
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


def _target_chapters(store: ProjectStore, args) -> list[str]:
    """解析本次要处理的章节列表（规则在 application，CLI 只转参数）。

    参数:
        store: 工程。
        args: 已解析参数（--chapter / --chapters / --all-chapters）。

    返回:
        章节 ID 列表。
    """
    return target_chapters(store, args.chapter, args.chapters, args.all_chapters)


# ---------------- 配音：行级批量合成 ----------------


def _line_filter(store: ProjectStore, chapters: list[dict], args, config) -> tuple[set[str] | None, bool]:
    """按 `--line` / `--status` 解析本次要处理的行（规则在 application）。

    参数:
        store: 工程。
        chapters: 本次要处理的章节。
        args: 已解析参数。
        config: 本机配置。

    返回:
        `(行 ID 集合, 是否连已完成行一起跑)`。
    """
    return line_filter(
        store,
        chapters,
        config,
        status=args.status,
        line_ids=args.line,
        include_ready=args.include_ready,
    )


def cmd_tts_batch(args, config: AppConfig) -> int:
    """行级批量合成（规则与执行在 application，CLI 只转参数并输出）。"""
    store = _resolve_store(args, config)
    options = BatchOptions(
        chapter=args.chapter,
        chapters=args.chapters,
        all_chapters=args.all_chapters,
        scene=args.scene,
        line=args.line,
        status=args.status,
        limit=args.limit,
        batch_size=args.batch_size,
        include_ready=args.include_ready,
        dry_run=args.dry_run,
        progress_file=args.progress_file,
        skip_cast_check=args.skip_cast_check,
        skip_unknown_speakers=args.skip_unknown_speakers,
        human_summary=not args.json,
    )
    if options.dry_run:
        chapter_ids = _target_chapters(store, args)
        chapters = [store.load_chapter(chapter_id) for chapter_id in chapter_ids]
        line_ids, include_ready = _line_filter(store, chapters, args, config)
        skip_ids = unknown_line_ids(store, chapters, options.skip_unknown_speakers)
        # dry-run 也要过门禁：只体检前置条件，不能绕过音色/BGM 阻断项
        check_cast_gate(store, chapter_ids, skip_ids, options.skip_cast_check)
        preview = plan_batch_preview(
            store,
            config,
            chapter_ids,
            include_ready=include_ready,
            skip_ids=skip_ids,
            line_ids=line_ids,
            scenes=options.scene,
            limit=options.limit,
        )
        _emit(args, preview["head"] + "\n" + "\n".join(preview["lines"]), preview["payload"])
        return EXIT_OK
    code, report = run_batch(store, config, options)
    if args.json:
        _emit(args, payload=report)
    return code


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
    plan = plan_mux(
        timeline,
        store.root,
        mix_plan,
        settings=store.settings(),
        manifest=load_manifest(templates_dir(config)),
    )
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

    from novel_drama.composition import mux as run_mux

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
    silences = silence_events(store, chapter["id"])
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

    timeline = build_chapter_timeline(store, chapter["id"], dry_run=args.dry_run)
    summary = summarize_timeline(timeline)
    if args.dry_run:
        _emit(args, "[dry-run] " + summary + "\n" + "\n".join(timeline.get("warnings") or []), timeline)
        return EXIT_OK
    warnings = "\n".join(timeline.get("warnings") or [])
    _emit(args, summary + ("\n" + warnings if warnings else ""), timeline)
    return EXIT_OK


def cmd_export(args, config: AppConfig) -> int:
    """导出：默认只出 SRT/清单/时间表；--format bundle 额外收 MP3/MP4 与交付说明。"""
    store = _resolve_store(args, config)
    path = store.timeline_path(args.chapter)
    if not path.exists():
        raise PreconditionError("时间轴不存在，先执行 ndw timeline build")
    fmt = getattr(args, "format", "srt")
    warning = cast_warning_text(store, args.chapter)
    if warning:
        print(warning, file=sys.stderr)
    result = export_chapter(
        store, config, args.chapter, fmt=fmt, out_dir=getattr(args, "out_dir", None)
    )
    if fmt == "jianying":
        counts = result["counts"]
        reconcile = result["reconcile"]
        lines = [
            f"剪映草稿: {result['draft_dir']}",
            f"压缩包: {result['zip']}",
            f"导入位置: {result['install_dir']}",
            f"清单: {result['manifest']}",
            (
                f"分镜 {counts['video_segments']} 视频 + {counts['image_segments']} 图  "
                f"配音 {counts['voice_segments']}  音效 {counts['sfx_segments']}  BGM {counts['bgm_segments']}  "
                f"字幕 {counts['subtitles']}/{counts['dialogue_lines']} 行  轨道 {counts['tracks']}"
            ),
            (
                f"素材对账: 引用 {reconcile['referenced']} / 已复制 {reconcile['copied']} / "
                f"缺失 {len(reconcile['missing'])}"
            ),
        ]
        if result["subtitle_gaps"]:
            lines.append(f"字幕兜底 {len(result['subtitle_gaps'])} 行（已按估算时间补齐，详见清单）")
            for item in result["subtitle_gaps"]:
                lines.append(f"  ~ {item['line_id']}  {item['reason']}")
        if result["warnings"]:
            lines.extend(["告警："] + [f"  ! {item}" for item in result["warnings"]])
        _emit(args, "\n".join(lines), result)
        return EXIT_OK
    if fmt == "bundle":
        lines = [f"交付包: {result['dir']}"]
        for item in result["artifacts"]:
            lines.append(f"  - {item['kind']:<8} {item['status']:<8} {item['path']}")
        lines.append(f"清单: {result['bundle']}")
        lines.append(f"说明: {result['readme']}")
        if result["missing"]:
            lines.extend(["缺失项："] + [f"  ! {item}" for item in result["missing"]])
        _emit(args, "\n".join(lines), result)
        return EXIT_OK
    _emit(
        args,
        f"SRT: {result['srt']}\n清单: {result['manifest']}\n时间表: {result['schedule']}\n字幕条目: {result['entries']}",
        result,
    )
    return EXIT_OK


def add_parser(sub) -> None:
    """注册 `sfx` / `image` / `video` / `timeline` / `concat` 命令组。"""
    media_subs: dict = {}
    for kind in ("sfx", "image", "video"):
        p = sub.add_parser(kind, help={"sfx": "音效", "image": "关键图", "video": "5 秒短视频"}[kind])
        p_sub = p.add_subparsers(dest="action", required=True)
        media_subs[kind] = p_sub
        q = p_sub.add_parser("plan")
        q.add_argument("--chapter", required=True)
        q.set_defaults(func=cmd_media, media_kind=kind)
        q = p_sub.add_parser("run", help="真实生成（离线渲染器；--force 覆盖已就绪项）")
        q.add_argument("--chapter", required=True)
        q.add_argument("--force", action="store_true", help="已就绪项也重新生成")
        q.add_argument("--limit", type=int, help="本次最多处理条数")
        q.add_argument("--dry-run", action="store_true", help="只统计待生成条目，不写盘")
        q.set_defaults(func=cmd_media, media_kind=kind)

    # 音效：模型生成（Stable Audio 3）+ provider 自检，挂在已有的 sfx 命令下
    p_sub = media_subs["sfx"]
    q = p_sub.add_parser("generate", help="按英文提示词生成音效并落盘到 assets/audio/sfx/")
    q.add_argument("--prompt-en", dest="prompt_en", help="英文提示词（标注阶段产出；常规路径必填）")
    q.add_argument("--prompt", help="中文描述；仅与 --rewrite-prompt 搭配做临时调试")
    q.add_argument("--rewrite-prompt", action="store_true", dest="rewrite_prompt",
                   help="调试选项：把 --prompt 中文临时改写成英文（不参与常规链路）")
    q.add_argument("--duration", type=float, required=True, help="时长秒数（1~120；10 秒内热推理约 1 秒）")
    q.add_argument("--negative", help="负面描述（避免出现的内容）")
    q.add_argument("--name", help="文件名标签；缺省取英文描述前几个词")
    q.add_argument("--steps", type=int, help="扩散步数；缺省用 provider 配置（8）")
    q.add_argument("--cfg", type=float, help="CFG 值；缺省用 provider 配置（1.0）")
    q.add_argument("--seed", type=int, help="随机种子；缺省随机并写回元数据")
    q.add_argument("--provider", help="指定音效 provider；缺省取 sfx.default_provider_id")
    q.add_argument("--no-llm", action="store_true", help="仅与 --rewrite-prompt 搭配：跳过 LLM，直接用词典改写")
    q.set_defaults(func=cmd_sfx_generate)
    q = p_sub.add_parser("providers", help="列出音效 provider 与可用性")
    q.set_defaults(func=cmd_sfx_providers)

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


def add_export_parser(sub) -> None:
    """注册 `mux` / `mix` / `export` 三个成品命令。"""
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
    p.add_argument("--format", default="srt", choices=["srt", "bundle", "jianying"], help="srt=字幕与清单 / bundle=交付包（MP3+MP4+SRT+README）/ jianying=剪映草稿 zip")
    p.add_argument("--out-dir", dest="out_dir", help="输出目录，默认 <工程>/exports")
    p.set_defaults(func=cmd_export)
