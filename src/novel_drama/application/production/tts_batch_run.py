"""行级批量合成：章节选择、待合成预演、进度文件与执行。

CLI 的 `tts batch` 与 Web 的「整本补齐」共用本模块，避免两处判定漂移；
后台进程通过 `run_batch_entry` 进入，不再拼 CLI 命令串。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from ...composition import ProjectStore, check_cast
from ...domain.shared.errors import PreconditionError
from ...infrastructure.config import AppConfig, load_config
from ...models.ids import now_iso
from .lines_source import chapter_view, chapter_views

#: 队列执行遇到普通失败时的退出码（与 CLI 语义一致）
EXIT_FAILED = 5


@dataclass
class BatchOptions:
    """一次批量合成的范围与开关（CLI 与 Web 共用）。

    属性:
        chapter: 单章 ID。
        chapters: 逗号分隔的多章 ID。
        all_chapters: 是否处理全部章节。
        scene: 只处理这些场景。
        line: 只处理这些行。
        status: 逗号分隔的派生状态筛选。
        limit: 每章最多合成多少行。
        batch_size: 每批行数。
        include_ready: 是否连已完成的行一起重跑。
        dry_run: 只预演不合成。
        progress_file: 进度 JSON 路径（页面轮询用）。
        skip_cast_check: 跳过配音前的音色/BGM 门禁。
        skip_unknown_speakers: 跳过说话人未识别的行。
        human_summary: 是否打印面向人的总结（页面后台进程不需要）。
    """

    chapter: str | None = None
    chapters: str | None = None
    all_chapters: bool = False
    scene: list[str] | None = None
    line: list[str] | None = None
    status: str | None = None
    limit: int | None = None
    batch_size: int = 5
    include_ready: bool = False
    dry_run: bool = False
    progress_file: str | None = None
    skip_cast_check: bool = False
    skip_unknown_speakers: bool = False
    human_summary: bool = True


def target_chapters(
    store: ProjectStore,
    chapter: str | None = None,
    chapters: str | None = None,
    all_chapters: bool = False,
) -> list[str]:
    """解析本次要处理的章节列表。

    参数:
        store: 工程。
        chapter: 单章 ID。
        chapters: 逗号分隔的多章 ID。
        all_chapters: 是否处理全部章节。

    返回:
        章节 ID 列表。

    异常:
        PreconditionError: 未指定章节。
    """
    if chapters:
        return [item.strip() for item in chapters.split(",") if item.strip()]
    if all_chapters:
        ids = [item["id"] for item in store.list_chapters()]
        if not ids:
            raise PreconditionError("工程里还没有章节")
        return ids
    if not chapter:
        raise PreconditionError("请用 --chapter 指定章节，或用 --all-chapters 批量处理")
    return [chapter]


def line_filter(
    store: ProjectStore,
    chapters: list[dict],
    config: AppConfig,
    status: str | None = None,
    line_ids: list[str] | None = None,
    include_ready: bool = False,
) -> tuple[set[str] | None, bool]:
    """按状态 / 行 ID 解析本次要处理的行。

    参数:
        store: 工程。
        chapters: 本次要处理的章节。
        config: 本机配置。
        status: 逗号分隔的派生状态。
        line_ids: 只处理这些行。
        include_ready: 是否连已完成行一起跑。

    返回:
        `(行 ID 集合, 是否连已完成行一起跑)`；未给筛选时集合为 None。

    异常:
        ValueError: 状态名非法或行 ID 不存在（CLI 转成退出码 2）。
    """
    from ...tts import line_state

    statuses = line_state.parse_states(status)
    picked_lines = list(line_ids or [])
    if not statuses and not picked_lines:
        return None, bool(include_ready)
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
        scope="selected" if picked_lines else "chapter",
        line_ids=picked_lines,
        statuses=statuses,
    )
    return {row["line"]["id"] for row in picked}, bool(include_ready) or ("ready" in statuses)


def line_locator(chapter: dict) -> dict[str, tuple[int, int]]:
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


def unknown_line_ids(store: ProjectStore, chapters: list[dict], skip_unknown: bool = False) -> set[str]:
    """取本次范围内「说话人无法归属」的行 ID（未显式跳过时返回空集）。

    参数:
        store: 工程。
        chapters: 本次要处理的章节。
        skip_unknown: 是否跳过说话人未识别的行。

    返回:
        行 ID 集合；未开启跳过时为 `set()`。
    """
    if not skip_unknown:
        return set()
    from ...composition import unresolved_speaker_lines

    return {
        row["line_id"]
        for row in unresolved_speaker_lines(store, [chapter_view(store, chapter) for chapter in chapters])
    }


def planned_counts(
    store: ProjectStore,
    chapters: list[dict],
    characters: list[dict],
    config: AppConfig,
    options: BatchOptions,
    service=None,
) -> tuple[list[dict], list[dict]]:
    """按与正式批量完全相同的条件预演每章待合成行数。

    参数:
        store: 工程。
        chapters: 本次要处理的章节。
        characters: 角色表。
        config: 本机配置。
        options: 范围与筛选开关。
        service: TtsService；给出时把「音色变体/参考音频已变」的 ready 行也算进分母。

    返回:
        `(counts, skipped_rows)`：counts 为 `[{chapter_id, title, rows}]` 用于进度条分母；
        skipped_rows 为因说话人未识别被排除的行（含场景/行号定位）。
    """
    from ...tts import line_state
    from ...tts import reuse as reuse_plan
    from ...tts.batch import plan_batch

    line_ids, include_ready = line_filter(
        store,
        chapters,
        config,
        status=options.status,
        line_ids=options.line,
        include_ready=options.include_ready,
    )
    skip_ids = unknown_line_ids(store, chapters, options.skip_unknown_speakers)
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
            scene_ids=options.scene,
            include_ready=include_ready,
            limit=options.limit,
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
                scene_ids=options.scene,
                include_ready=include_ready,
                limit=options.limit,
                snapshots=snapshots,
                root=store.root,
                engine=engine,
                live_ids=live_ids,
                line_ids=line_ids,
                changed_checker=checker,
            )
            locator = line_locator(chapter)
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


def write_progress(path: str | None, payload: dict) -> None:
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


def check_cast_gate(
    store: ProjectStore,
    chapter_ids: list[str],
    skip_ids: set[str],
    skip_cast_check: bool = False,
) -> None:
    """配音前的音色/BGM 门禁：有阻断项时拒绝整批。

    参数:
        store: 工程。
        chapter_ids: 本次要处理的章节。
        skip_ids: 已显式跳过（说话人未识别）的行；这些行不再计入阻断项。
        skip_cast_check: 是否跳过门禁。

    返回:
        无。

    异常:
        PreconditionError: 存在阻断项。
    """
    if skip_cast_check:
        return
    gate = check_cast(store, chapter_views(store, chapter_ids if len(chapter_ids) == 1 else None))
    errors = [item for item in gate["findings"] if item["level"] == "error"]
    if skip_ids:
        # 这些行已由用户显式选择跳过（保持原样不动），不再计入阻断项
        errors = [item for item in errors if item.get("line_id") not in skip_ids]
    if errors:
        detail = "\n".join(f"- {item['code']}: {item['message']}" for item in errors[:8])
        raise PreconditionError(
            f"音色/BGM 编排有 {len(errors)} 个阻断项，已取消配音（可用 --skip-cast-check 跳过）：\n{detail}"
        )


def run_batch(store: ProjectStore, config: AppConfig, options: BatchOptions) -> tuple[int, dict]:
    """执行一次批量合成，并按失败行数给出退出码。

    参数:
        store: 工程。
        config: 本机配置。
        options: 范围与开关。

    返回:
        `(退出码, 批次报告)`；有失败行时退出码为 5。

    异常:
        PreconditionError: 音色/BGM 存在阻断项。
    """
    from ...tts import TtsService
    from ...tts.batch import history_avg_ms_per_line, run_chapters

    chapter_ids = target_chapters(store, options.chapter, options.chapters, options.all_chapters)
    chapters = [store.load_chapter(chapter_id) for chapter_id in chapter_ids]
    characters = store.load_characters()["characters"]
    line_ids, include_ready = line_filter(
        store,
        chapters,
        config,
        status=options.status,
        line_ids=options.line,
        include_ready=options.include_ready,
    )
    # 说话人无法归属的行：显式跳过时不再参与合成，也不会因它们阻断整章
    skip_ids = unknown_line_ids(store, chapters, options.skip_unknown_speakers)
    check_cast_gate(store, chapter_ids, skip_ids, options.skip_cast_check)

    service = TtsService(config, store.root)
    progress_file = options.progress_file
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
        counts, skipped_rows = planned_counts(
            store,
            chapters,
            characters,
            config,
            options,
            service=service,
        )
        progress["total"] = sum(item["rows"] for item in counts)
        progress["chapters"] = counts
        progress["skipped"] = len(skipped_rows)
        progress["skipped_rows"] = skipped_rows
        if progress_file:
            write_progress(progress_file, progress)

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
        write_progress(progress_file, progress)

    try:
        report = run_chapters(
            service,
            store,
            chapters,
            batch_size=options.batch_size,
            dry_run=False,
            reporter=reporter,
            scene_ids=options.scene,
            include_ready=include_ready,
            limit=options.limit,
            line_ids=line_ids,
            skip_line_ids=skip_ids,
            avg_ms_per_line=history_avg_ms_per_line(store.root),
        )
    except Exception as exc:  # 进度文件要记录中断原因，再原样抛出
        progress.update({"finished": True, "error": str(exc), "updated_at": now_iso()})
        write_progress(progress_file, progress)
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
        write_progress(progress_file, progress)
    stats = report["stats"]
    skip_note = f"，跳过 {progress['skipped']} 行（说话人未识别）" if progress.get("skipped") else ""
    reuse = report.get("reuse") or {}
    reuse_note = f"，复用 {reuse.get('reused', 0)} 行"
    if reuse.get("saved_ms_estimate"):
        reuse_note += f"（约省 {reuse['saved_ms_estimate'] / 1000:.1f}s）"
    if options.human_summary:
        print(
            f"批量合成完成：{stats['chapters']} 章 / 请求 {stats['requested']} 行，成功 {stats['succeeded']}"
            f"（缓存命中 {stats['cache_hits']}{reuse_note}），失败 {stats['failed']}{skip_note}；"
            f"累计音频 {stats['audio_ms'] / 1000:.1f}s；总耗时 {report['elapsed_ms'] / 1000:.1f}s；"
            f"峰值内存 {report['peak_rss_mb']}MB\n报告 {report['report_path']}"
        )
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
    return (0 if stats["failed"] == 0 else EXIT_FAILED), report


def run_batch_entry(
    project_root: str,
    config_path: str | None = None,
    chapter_id: str | None = None,
    all_chapters: bool = False,
    skip_unknown: bool = False,
    batch_size: int | None = None,
    progress_file: str | None = None,
) -> dict:
    """页面「整本补齐」的后台进程入口。

    参数:
        project_root: 工程目录。
        config_path: 本机配置文件路径（可空）。
        chapter_id: 单章 ID（与 all_chapters 二选一）。
        all_chapters: 是否处理全书。
        skip_unknown: 是否跳过说话人未识别的行。
        batch_size: 每批行数。
        progress_file: 进度 JSON 路径（页面轮询用）。

    返回:
        `{"exit_code": 队列执行退出码}`。
    """
    config = load_config(config_path) if config_path else load_config()
    store = ProjectStore(project_root)
    options = BatchOptions(
        chapter=chapter_id,
        all_chapters=all_chapters,
        batch_size=int(batch_size or 5),
        progress_file=progress_file,
        skip_unknown_speakers=skip_unknown,
        human_summary=False,
    )
    code, _ = run_batch(store, config, options)
    return {"exit_code": code}
