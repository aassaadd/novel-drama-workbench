"""配音批量计划预演：给页面与 CLI 同一份「本次会合成哪些行」的答案。

只做规划与复用判断，不落盘、不调模型；页面「补齐本章 / 补齐整本」与终端
`tts batch --dry-run` 都走这一份，避免两套口径给出不同的待合成行数。
"""

from __future__ import annotations

from ...adapters.persistence import ProjectStore
from ...tts import TtsService, line_state
from ...tts import reuse as reuse_plan
from ...tts.batch import history_avg_ms_per_line, plan_batch
from .lines_source import chapter_view

#: 影响清单最多返回多少条，避免整本工程一次返回过大
_ITEM_LIMIT = 300


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


def plan_batch_preview(
    store: ProjectStore,
    config,
    chapter_ids: list[str],
    include_ready: bool = False,
    skip_ids: set[str] | None = None,
    line_ids: list[str] | None = None,
    scenes: list[str] | None = None,
    limit: int | None = None,
) -> dict:
    """预演一次批量合成，给出待合成行、影响清单与复用计划。

    参数:
        store: 工程。
        config: 本机配置（取引擎参数与提供方）。
        chapter_ids: 本次覆盖的章节 ID。
        include_ready: 是否把已就绪的行也算进来。
        skip_ids: 本次要跳过的行 ID（如说话人未识别）。
        line_ids: 只看指定行。
        scenes: 只看指定场景。
        limit: 每章最多取多少行。

    返回:
        `{"head": 摘要文本, "lines": 明细行, "payload": 页面/JSON 负载}`。

    异常:
        FileNotFoundError: 章节不存在。
    """
    skip_ids = set(skip_ids or ())
    # None 表示「不按行过滤」；空列表会被 plan_batch 理解成「一行都不要」
    line_ids = list(line_ids) if line_ids else None
    chapters = [chapter_view(store, store.load_chapter(chapter_id)) for chapter_id in chapter_ids]
    service = TtsService(config, store.root if store.exists() else None)
    try:
        avg_ms = history_avg_ms_per_line(store.root)
        lines: list[str] = []
        total = 0
        lines_total = 0
        skipped_unknown = 0
        unknown_rows: list[dict] = []
        items: list[dict] = []
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
                scene_ids=scenes,
                include_ready=include_ready,
                limit=limit,
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
                if not scenes or scene.get("id") in scenes
                for line in scene.get("lines") or []
                if (line.get("text") or "").strip()
            )
            locator = line_locator(chapter)
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
            for key in (
                "keep",
                "reuse",
                "regenerate",
                "blocked",
                "skipped",
                "reused_audio_ms",
                "saved_ms_estimate",
            ):
                reuse_total[key] += int(plan.get(key) or 0)
            item_by_line = {}
            for plan_item in plan.get("items") or []:
                item_by_line[plan_item.get("line_id")] = plan_item
                if len(reuse_items) < _ITEM_LIMIT:
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
                if len(items) < _ITEM_LIMIT:
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
    head = (
        f"[dry-run] 待合成 {total} 行（{len(chapters)} 章，"
        f"跳过 {max(0, lines_total - total)} 行"
    )
    if skipped_unknown:
        head += f"，其中说话人未识别 {skipped_unknown} 行"
    reuse_note = (
        f"｜复用 {reuse_total['keep'] + reuse_total['reuse']} 行"
        f"（沿用已有 {reuse_total['keep']}，缓存命中 {reuse_total['reuse']}）"
        f"，重新合成 {reuse_total['regenerate']} 行"
    )
    if reuse_total["saved_ms_estimate"]:
        reuse_note += f"，预计省 {reuse_total['saved_ms_estimate'] / 1000:.1f}s"
    return {
        "head": head + f"）：{reuse_note}",
        "lines": lines,
        "payload": {
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
            "reuse": {
                **reuse_total,
                "items": reuse_items,
                "items_truncated": len(reuse_items) >= _ITEM_LIMIT,
            },
        },
    }


def plan_chapter(
    store: ProjectStore,
    config,
    chapter_id: str,
    scope: str | None = None,
    line_ids: list[str] | None = None,
    scene_ids: list[str] | None = None,
    statuses_raw: str | None = None,
    force: bool = False,
) -> dict:
    """规划单章的配音任务（`tts plan` 与 `tts run` 共用的前半段）。

    范围与状态筛选走 `line_state`，与 `line list --status` 同源，
    因此 plan 的「待合成」计数必然等于 list 筛出来的行数。

    参数:
        store: 工程。
        config: 本机配置。
        chapter_id: 章节 ID。
        scope: 显式范围；为空时按 line_ids / scene_ids / force 推导。
        line_ids: 只看指定行。
        scene_ids: 只看指定场景。
        statuses_raw: 逗号分隔的状态筛选。
        force: 忽略状态筛选（含已就绪行），强制重新生成。

    返回:
        `{chapter, characters, adapter, snapshots, engine, live_ids, scope, selected,
        counts, jobs, todo_ids, run_nonce}`。

    异常:
        FileNotFoundError: 章节不存在。
    """
    import time

    from ...orchestration.pipeline import build_tts_jobs

    chapter = chapter_view(store, store.load_chapter(chapter_id))
    characters = store.load_characters()["characters"]
    adapter = config.adapters.get("tts", "placeholder")
    snapshots = line_state.load_snapshots(store.root, chapter)
    live_ids = line_state.live_line_ids(store)
    engine = line_state.engine_params(config)
    statuses = () if force else line_state.parse_states(statuses_raw)
    # 默认范围：给了行就是「选中」；给了场景就是「场景」；否则「待处理」
    if scope:
        resolved_scope = scope
    elif line_ids:
        resolved_scope = "selected"
    elif scene_ids:
        resolved_scope = "scene"
    else:
        resolved_scope = "chapter" if force else "pending"
    states = line_state.chapter_states(
        chapter,
        {item["id"]: item for item in characters},
        snapshots,
        root=store.root,
        live_ids=live_ids,
        engine=engine,
    )
    selected = line_state.select_states(
        states, scope=resolved_scope, line_ids=line_ids, scene_ids=scene_ids, statuses=statuses
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
    return {
        "chapter": chapter,
        "characters": characters,
        "adapter": adapter,
        "snapshots": snapshots,
        "engine": engine,
        "live_ids": live_ids,
        "scope": resolved_scope,
        "selected": len(selected),
        "counts": counts,
        "jobs": jobs,
        "todo_ids": todo_ids,
        "run_nonce": run_nonce,
    }
