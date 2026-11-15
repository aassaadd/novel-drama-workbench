"""媒体相关 CLI：批量配音、时间轴、拼接、导出、混流。"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from novel_drama.composition import (
    ProjectStore,
    build_schedule,
    build_timeline,
    concat_voice,
    format_schedule_tsv,
    load_cues,
    load_manifest,
    normalize_audio,
    plan_concat,
    plan_media,
    plan_mix,
    plan_mux,
    render_mix,
    run_media,
    summarize_timeline,
    templates_dir,
    validate,
    write_export,
)
from novel_drama.infrastructure.config import AppConfig
from novel_drama.interface.cli.commands.sfx import cmd_sfx_generate, cmd_sfx_providers
from novel_drama.models.ids import now_iso

from ..common import (
    EXIT_FAILED,
    EXIT_OK,
    PreconditionError,
    _emit,
    _resolve_store,
    _tts_service,
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
    from novel_drama.tts import line_state

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


def _line_locator(chapter: dict) -> dict[str, tuple[int, int]]:
    """建立「行 ID → (场景序号, 场景内行号)」，两个序号均从 1 开始。

    参数:
        chapter: 章节 dict。

    返回:
        行定位映射；供「跳过行」清单给出人类可读位置。
    """
    mapping: dict[str, tuple[int, int]] = {}
    scenes = sorted(chapter.get("scenes") or [], key=lambda item: item.get("index") or 0)
    for scene_no, scene in enumerate(scenes, start=1):
        lines = sorted(scene.get("lines") or [], key=lambda item: item.get("index") or 0)
        for line_no, line in enumerate(lines, start=1):
            if line.get("id"):
                mapping[str(line["id"])] = (scene_no, line_no)
    return mapping


def _planned_counts(
    store: ProjectStore,
    chapters: list[dict],
    characters: list[dict],
    args,
    config,
    service=None,
) -> tuple[list[dict], list[dict]]:
    """按与正式批量完全相同的条件预演每章待合成行数。

    参数:
        store: 工程。
        chapters: 本次要处理的章节。
        characters: 角色表。
        args: 已解析参数（scene / limit / line / status 等）。
        config: 本机配置。
        service: TtsService；给出时把「音色变体/参考音频已变」的 ready 行也算进分母，
            与 `run_chapters` 的规划口径保持一致。

    返回:
        `(counts, skipped_rows)`：counts 为 `[{chapter_id, title, rows}]` 用于进度条分母；
        skipped_rows 为因说话人未识别被排除的行（含场景/行号定位）。
    """
    from novel_drama.tts import line_state
    from novel_drama.tts import reuse as reuse_plan
    from novel_drama.tts.batch import plan_batch

    line_ids, include_ready = _line_filter(store, chapters, args, config)
    skip_ids = _unknown_line_ids(store, chapters, args)
    counts: list[dict] = []
    skipped_rows: list[dict] = []
    for chapter in chapters:
        characters_table = {"characters": characters}
        voices = store.load_voices()
        snapshots = line_state.load_snapshots(store.root, chapter)
        engine = line_state.engine_params(config)
        live_ids = line_state.live_line_ids(store)
        checker = (
            reuse_plan.changed_checker(service, store, characters_table, chapter, voices=voices)
            if service is not None
            else None
        )
        rows = plan_batch(
            chapter,
            characters,
            scene_ids=args.scene,
            include_ready=include_ready,
            limit=args.limit,
            snapshots=snapshots,
            root=store.root,
            engine=engine,
            live_ids=live_ids,
            line_ids=line_ids,
            skip_line_ids=skip_ids,
            changed_checker=checker,
        )
        if skip_ids:
            # 被跳过的行必须本来就在本次待补范围内，才算「跳过 N 行」
            planned = plan_batch(
                chapter,
                characters,
                scene_ids=args.scene,
                include_ready=include_ready,
                limit=args.limit,
                snapshots=snapshots,
                root=store.root,
                engine=engine,
                live_ids=live_ids,
                line_ids=line_ids,
                changed_checker=checker,
            )
            locator = _line_locator(chapter)
            for row in planned:
                line = row["line"]
                if line.get("id") not in skip_ids:
                    continue
                scene_no, line_no = locator.get(str(line.get("id")), (0, 0))
                skipped_rows.append(
                    {
                        "chapter_id": chapter.get("id"),
                        "chapter_title": chapter.get("title"),
                        "scene_id": row.get("scene_id"),
                        "scene_index": scene_no,
                        "line_id": line.get("id"),
                        "line_index": line_no,
                        "speaker_id": line.get("speaker_id"),
                        "text": line.get("text"),
                    }
                )
        counts.append({"chapter_id": chapter.get("id"), "title": chapter.get("title"), "rows": len(rows)})
    return counts, skipped_rows


def _unknown_line_ids(store: ProjectStore, chapters: list[dict], args) -> set[str]:
    """取本次范围内「说话人无法归属」的行 ID（未显式跳过时返回空集）。

    参数:
        store: 工程。
        chapters: 本次要处理的章节。
        args: 已解析参数（读取 `--skip-unknown-speakers`）。

    返回:
        行 ID 集合；未开启跳过时为 `set()`。
    """
    if not getattr(args, "skip_unknown_speakers", False):
        return set()
    from novel_drama.composition import unresolved_speaker_lines

    return {row["line_id"] for row in unresolved_speaker_lines(store, [chapter.get("id") for chapter in chapters])}


def _write_progress(path: str | None, payload: dict) -> None:
    """原子写批量进度文件；未指定路径时静默跳过。

    参数:
        path: 进度文件路径。
        payload: 进度内容。

    返回:
        无。
    """
    if not path:
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, target)


def cmd_tts_batch(args, config: AppConfig) -> int:
    """行级批量合成：单并发、可控批次、失败逐行记录不中断；支持跨章节与断点续跑。"""
    from novel_drama.tts.batch import history_avg_ms_per_line, plan_batch, run_chapters

    store = _resolve_store(args, config)
    chapter_ids = _target_chapters(store, args)
    chapters = [store.load_chapter(chapter_id) for chapter_id in chapter_ids]
    characters = store.load_characters()["characters"]
    line_ids, include_ready = _line_filter(store, chapters, args, config)
    # 说话人无法归属的行：显式跳过时不再参与合成，也不会因它们阻断整章
    skip_ids = _unknown_line_ids(store, chapters, args)
    if not getattr(args, "skip_cast_check", False):
        from novel_drama.composition import check_cast

        gate = check_cast(store, chapter_ids[0]) if len(chapter_ids) == 1 else check_cast(store)
        errors = [item for item in gate["findings"] if item["level"] == "error"]
        if skip_ids:
            # 这些行已由用户显式选择跳过（保持原样不动），不再计入阻断项
            errors = [item for item in errors if item.get("line_id") not in skip_ids]
        if errors:
            detail = "\n".join(f"- {item['code']}: {item['message']}" for item in errors[:8])
            raise PreconditionError(
                f"音色/BGM 编排有 {len(errors)} 个阻断项，已取消配音（可用 --skip-cast-check 跳过）：\n{detail}"
            )

    if args.dry_run:
        from novel_drama.tts import line_state
        from novel_drama.tts import reuse as reuse_plan
        from novel_drama.tts.batch import history_avg_ms_per_line

        service = _tts_service(args, config)
        avg_ms = history_avg_ms_per_line(store.root)
        lines: list[str] = []
        total = 0
        lines_total = 0
        skipped_unknown = 0
        unknown_rows: list[dict] = []
        # 影响清单：给页面列出「本次会动到哪些场景/行」，最多 300 条，避免整本工程一次返回过大
        items: list[dict] = []
        item_limit = 300
        per_chapter: list[dict] = []
        # 复用计划汇总：沿用 / 缓存命中 / 重新合成 / 跳过 / 阻塞
        reuse_total = {
            "keep": 0,
            "reuse": 0,
            "regenerate": 0,
            "blocked": 0,
            "skipped": 0,
            "reused_audio_ms": 0,
            "saved_ms_estimate": 0,
            "avg_ms_per_line": avg_ms,
        }
        reuse_items: list[dict] = []
        try:
            for chapter in chapters:
                characters_table = store.load_characters()
                chapter_characters = characters_table["characters"]
                snapshots = line_state.load_snapshots(store.root, chapter)
                engine = line_state.engine_params(config)
                live_ids = line_state.live_line_ids(store)
                voices = store.load_voices()
                checker = reuse_plan.changed_checker(
                    service, store, characters_table, chapter, voices=voices
                )
                rows = plan_batch(
                    chapter,
                    chapter_characters,
                    scene_ids=args.scene,
                    include_ready=include_ready,
                    limit=args.limit,
                    snapshots=snapshots,
                    root=store.root,
                    engine=engine,
                    live_ids=live_ids,
                    line_ids=line_ids,
                    changed_checker=checker,
                )
                chapter_lines = sum(
                    1
                    for scene in chapter.get("scenes") or []
                    if not args.scene or scene.get("id") in args.scene
                    for line in scene.get("lines") or []
                    if (line.get("text") or "").strip()
                )
                locator = _line_locator(chapter)
                if skip_ids:
                    kept = []
                    for row in rows:
                        line = row["line"]
                        if line.get("id") not in skip_ids:
                            kept.append(row)
                            continue
                        scene_no, line_no = locator.get(str(line.get("id")), (0, 0))
                        unknown_rows.append(
                            {
                                "chapter_id": chapter.get("id"),
                                "chapter_title": chapter.get("title"),
                                "scene_id": row.get("scene_id"),
                                "scene_index": scene_no,
                                "line_id": line.get("id"),
                                "line_index": line_no,
                                "speaker_id": line.get("speaker_id"),
                                "text": line.get("text"),
                            }
                        )
                    skipped_unknown += len(rows) - len(kept)
                    rows = kept
                total += len(rows)
                lines_total += chapter_lines

                # 复用计划：只看状态不够，还要判断「过期行是否已有同签名音频可复用」
                states = line_state.chapter_states(
                    chapter,
                    {item["id"]: item for item in chapter_characters},
                    snapshots,
                    root=store.root,
                    live_ids=live_ids,
                    engine=engine,
                )
                plan = reuse_plan.plan_reuse(
                    service,
                    store,
                    chapter,
                    rows,
                    characters_table=characters_table,
                    snapshots=snapshots,
                    engine=engine,
                    live_ids=live_ids,
                    states=states,
                    avg_ms_per_line=avg_ms,
                    voices=voices,
                )
                for key in ("keep", "reuse", "regenerate", "blocked", "skipped", "reused_audio_ms", "saved_ms_estimate"):
                    reuse_total[key] += int(plan.get(key) or 0)
                item_by_line = {}
                for plan_item in plan.get("items") or []:
                    item_by_line[plan_item.get("line_id")] = plan_item
                    if len(reuse_items) < item_limit:
                        reuse_items.append(plan_item)

                per_chapter.append(
                    {
                        "chapter_id": chapter.get("id"),
                        "title": chapter.get("title"),
                        "rows": len(rows),
                        "lines_total": chapter_lines,
                        "skipped": max(0, chapter_lines - len(rows)),
                        "reuse": {
                            "keep": plan.get("keep"),
                            "reuse": plan.get("reuse"),
                            "regenerate": plan.get("regenerate"),
                            "blocked": plan.get("blocked"),
                        },
                    }
                )
                for row in rows:
                    line = row["line"]
                    lines.append(
                        f"{chapter.get('id')}  {line['id']}  {line.get('speaker_id')}  {line['text'][:24]}"
                    )
                    if len(items) < item_limit:
                        scene_no, line_no = locator.get(str(line.get("id")), (0, 0))
                        plan_item = item_by_line.get(line.get("id")) or {}
                        items.append(
                            {
                                "chapter_id": chapter.get("id"),
                                "chapter_title": chapter.get("title"),
                                "scene_id": row.get("scene_id"),
                                "scene_index": scene_no,
                                "line_id": line.get("id"),
                                "line_index": line_no,
                                "speaker_id": line.get("speaker_id"),
                                "text": line.get("text"),
                                "has_audio": bool((line.get("tts") or {}).get("asset_id")),
                                "decision": plan_item.get("decision"),
                                "reuse_reason": plan_item.get("reason"),
                                "changes": (plan_item.get("changes") or [])[:3],
                            }
                        )
        finally:
            service.close()
        head = f"[dry-run] 待合成 {total} 行（{len(chapters)} 章，跳过 {max(0, lines_total - total)} 行"
        if skipped_unknown:
            head += f"，其中说话人未识别 {skipped_unknown} 行"
        reuse_note = (
            f"｜复用 {reuse_total['keep'] + reuse_total['reuse']} 行"
            f"（沿用已有 {reuse_total['keep']}，缓存命中 {reuse_total['reuse']}）"
            f"，重新合成 {reuse_total['regenerate']} 行"
        )
        if reuse_total["saved_ms_estimate"]:
            reuse_note += f"，预计省 {reuse_total['saved_ms_estimate'] / 1000:.1f}s"
        _emit(
            args,
            head + f"）：{reuse_note}\n" + "\n".join(lines),
            {
                "rows": total,
                "chapters": chapter_ids,
                "per_chapter": per_chapter,
                "lines_total": lines_total,
                "skipped": max(0, lines_total - total),
                "skipped_unknown": skipped_unknown,
                "unknown_rows": unknown_rows,
                "unknown_line_ids": sorted(skip_ids),
                "items": items,
                "items_truncated": total > len(items),
                "assets": sum(1 for item in items if item["has_audio"]),
                "reuse": {**reuse_total, "items": reuse_items, "items_truncated": len(reuse_items) >= item_limit},
            },
        )
        return EXIT_OK

    service = _tts_service(args, config)
    progress_file = getattr(args, "progress_file", None)
    progress = {
        "pid": os.getpid(),
        "total": 0,
        "done": 0,
        "failed": 0,
        "skipped": 0,
        "reused": 0,
        "skipped_rows": [],
        "failed_rows": [],
        "current_chapter": None,
        "started_at": now_iso(),
        "finished": False,
    }
    if progress_file or skip_ids:
        # 进度分母提前算好：run_chapters 是逐章规划的，不预先汇总页面就无法显示 X/Y
        counts, skipped_rows = _planned_counts(store, chapters, characters, args, config, service=service)
        progress["total"] = sum(item["rows"] for item in counts)
        progress["chapters"] = counts
        progress["skipped"] = len(skipped_rows)
        progress["skipped_rows"] = skipped_rows
        if progress_file:
            _write_progress(progress_file, progress)

    def reporter(chapter_no: int, chapter: dict, batch_no: int, batch_results: list[dict], stats: dict) -> None:
        """打印章节内批次进度，并在需要时刷新进度文件。"""
        ok = sum(1 for item in batch_results if item["status"] == "ready")
        failed = sum(1 for item in batch_results if item["status"] == "failed")
        reused = sum(1 for item in batch_results if item.get("decision") == "reuse")
        print(
            f"[章 {chapter_no} {chapter.get('id')} / 批 {batch_no}] 本批 {len(batch_results)} 行："
            f"成功 {ok}（复用 {reused}）失败 {failed}；累计 {stats['succeeded']}/{stats['requested']}"
        )
        if not progress_file:
            return
        progress["done"] += len(batch_results)
        progress["failed"] += failed
        progress["reused"] += reused
        progress["current_chapter"] = chapter.get("id")
        for item in batch_results:
            if item.get("status") != "failed":
                continue
            error = item.get("error") or {}
            progress["failed_rows"].append(
                {
                    "chapter_id": chapter.get("id"),
                    "chapter_title": chapter.get("title"),
                    "line_id": item.get("line_id"),
                    "code": error.get("code"),
                    "message": error.get("message"),
                }
            )
        progress["updated_at"] = now_iso()
        _write_progress(progress_file, progress)

    try:
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
            skip_line_ids=skip_ids,
            avg_ms_per_line=history_avg_ms_per_line(store.root),
        )
    except Exception as exc:  # noqa: BLE001 - 进度文件要记录中断原因，再原样抛出
        progress.update({"finished": True, "error": str(exc), "updated_at": now_iso()})
        _write_progress(progress_file, progress)
        raise
    finally:
        # 整批结束回收常驻 worker，避免跨命令残留进程
        service.close()
    if progress_file:
        progress.update(
            {
                "finished": True,
                "done": progress["total"] or progress["done"],
                "report_path": report.get("report_path"),
                "saved_ms": (report.get("reuse") or {}).get("saved_ms_estimate"),
                "reused_audio_ms": (report.get("reuse") or {}).get("reused_audio_ms"),
                "updated_at": now_iso(),
            }
        )
        _write_progress(progress_file, progress)
    stats = report["stats"]
    skip_note = f"，跳过 {progress['skipped']} 行（说话人未识别）" if progress.get("skipped") else ""
    reuse = report.get("reuse") or {}
    reuse_note = f"，复用 {reuse.get('reused', 0)} 行"
    if reuse.get("saved_ms_estimate"):
        reuse_note += f"（约省 {reuse['saved_ms_estimate'] / 1000:.1f}s）"
    text = (
        f"批量合成完成：{stats['chapters']} 章 / 请求 {stats['requested']} 行，成功 {stats['succeeded']}"
        f"（缓存命中 {stats['cache_hits']}{reuse_note}），失败 {stats['failed']}{skip_note}；"
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
    from novel_drama.composition import check_cast

    cast_warnings = check_cast(store, chapter["id"])["findings"]
    if cast_warnings:
        detail = "；".join(item["message"] for item in cast_warnings[:5])
        print(f"[warn] 音色/BGM 编排提示 {len(cast_warnings)} 条：{detail}", file=sys.stderr)
    silences = _silence_events(store, chapter["id"])
    if getattr(args, "format", "srt") == "jianying":
        from novel_drama.composition import JianyingExportError, export_jianying

        try:
            result = export_jianying(
                store,
                chapter,
                timeline,
                drafts_root=config.get("export.jianying.drafts_root"),
                ffprobe=config.get("ffmpeg.probe_binary", "ffprobe"),
                out_dir=(Path(args.out_dir) if args.out_dir else store.root / "exports" / "jianying"),
            )
        except JianyingExportError as error:
            # 素材缺失等属于前置条件未满足：转成 PreconditionError，
            # 命令行与网页都能拿到可读原因，而不是 500 或裸异常。
            raise PreconditionError(str(error)) from error
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
    if getattr(args, "format", "srt") == "bundle":
        from novel_drama.composition import export_bundle

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
