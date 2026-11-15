"""用例：在大纲基础上扩写新幕/章/节拍，含数量闸门与重写。"""

from __future__ import annotations

from ...adapters.errors import AdapterError, ErrorCode
from ...adapters.persistence import ProjectStore
from ...adapters.persistence.settings_store import SettingsStore
from ...application.cast.sync_cast import (
    load_pending,
)
from ...infrastructure.config import AppConfig
from ...models.ids import now_iso
from ..support.llm_calls import (
    _characters_payload,
    _progress,
    build_writing_adapter,
)
from .generate_outline import _act_validation, _validation_report
from .outline_bind import (
    bind_outline_context,
    outline_binding_stats,
    sanitize_outline_character_ids,
)
from .outline_expand_common import MODES, _base_context, _most_common
from .outline_expand_steps import (
    _apply_expand_cast,
    _gen_acts,
    _gen_beats,
    _gen_chapters,
    _renumber,
    _to_outline_act,
)


def expand_outline(
    store: ProjectStore,
    config: AppConfig,
    mode: str = "append",
    anchor_act: int | None = None,
    anchor_chapter: int | None = None,
    new_acts: int = 1,
    new_chapters: int | None = None,
    chapters_per_act: int | None = None,
    beats_per_chapter: int | None = None,
    chapter_words: int | None = None,
    brief: str = "",
    dry_run: bool = False,
    binding: str = "auto",
    on_progress=None,
) -> dict:
    """扩充或续写大纲：只新增，不重写已有内容。

    参数:
        store: 工程。
        config: 本机配置。
        mode: after_act=在某幕后插入；after_chapter=在某章后插入；append=结局后续写。
        anchor_act: 锚点幕序号（1 开始），after_act 必填。
        anchor_chapter: 锚点章序号（全书序号，1 开始），after_chapter 必填。
        new_acts: 新增幕数（after_act / append）。
        new_chapters: 新增章数（after_chapter），默认沿用每幕章数。
        chapters_per_act: 每幕章数；省略时按已有大纲推断。
        beats_per_chapter: 每章节拍数；省略时按已有大纲推断。
        chapter_words: 单章目标字数；省略时按已有大纲推断。
        brief: 用户补充要求。
        dry_run: 只生成不落盘。
        binding: auto=把节拍绑定到已有人物（非法引用过滤后回落到 POV）；
            none=只过滤非法角色引用（人名/空串/重复 ID），不回落、不改世界绑定。
        on_progress: 进度回调（stage/batch/chars/rewrite）。

    返回:
        `{mode, inserted_acts, inserted_chapters, validation, summary, total, ...}`。

    异常:
        AdapterError: 没有大纲、锚点不合法，或某层重写后仍越界。
    """
    settings_store = SettingsStore(store.root)
    bible = settings_store.ensure()
    acts = (bible.get("outline") or {}).get("acts") or []
    if not acts:
        raise AdapterError(ErrorCode.INVALID_INPUT, "还没有大纲，无法扩充或续写：请先生成一份完整的分层大纲。")
    mode = mode if mode in MODES else "append"
    project = store.load_project()
    work_info = project.get("work") or {}
    characters = [item for item in store.load_characters()["characters"] if item.get("role") != "narrator"]
    char_rows = _characters_payload(characters)
    adapter = build_writing_adapter(config, bible)

    # 推断默认值：优先沿用参数，其次取已有大纲里最常见的规模
    inferred_chapters = _most_common([len(act.get("chapters") or []) for act in acts], 5)
    beat_counts = [len(chapter.get("beats") or []) for act in acts for chapter in (act.get("chapters") or [])]
    inferred_beats = _most_common(beat_counts, 4)
    word_sums = [
        sum(int(beat.get("target_words") or 0) for beat in (chapter.get("beats") or []))
        for act in acts
        for chapter in (act.get("chapters") or [])
    ]
    inferred_words = _most_common([item for item in word_sums if item > 0], 3000)
    expected_chapters = max(1, int(chapters_per_act or inferred_chapters))
    expected_beats = max(1, int(beats_per_chapter or inferred_beats))
    expected_words = max(1, int(chapter_words or inferred_words))

    # 锚点定位与来源标注
    origin = {"kind": "continuation" if mode == "append" else "expansion", "mode": mode, "created_at": now_iso()}
    anchor_act_obj = None
    anchor_chapter_obj = None
    if mode == "after_act":
        index = max(1, min(len(acts), int(anchor_act or len(acts))))
        anchor_act_obj = acts[index - 1]
        anchor_act = index
        origin["anchor_act_id"] = anchor_act_obj.get("id")
        origin["anchor_act_title"] = anchor_act_obj.get("title") or ""
        placement = f"插入位置：第 {index} 幕《{anchor_act_obj.get('title') or ''}》之后（原有后续幕顺延）。"
    elif mode == "after_chapter":
        flat = [(act_pos, act, chapter) for act_pos, act in enumerate(acts, start=1) for chapter in (act.get("chapters") or [])]
        if not flat:
            raise AdapterError(ErrorCode.INVALID_INPUT, "大纲里还没有章节，无法在某章之后插入；请改用「在某幕之后」或「末尾续写」。")
        number = max(1, min(len(flat), int(anchor_chapter or len(flat))))
        act_pos, act_obj, chapter_obj = flat[number - 1]
        anchor_act = act_pos
        anchor_act_obj = act_obj
        anchor_chapter = number
        anchor_chapter_obj = chapter_obj
        origin["anchor_act_id"] = act_obj.get("id")
        origin["anchor_act_title"] = act_obj.get("title") or ""
        origin["anchor_chapter_id"] = chapter_obj.get("id")
        origin["anchor_chapter_title"] = chapter_obj.get("title") or ""
        placement = f"插入位置：第 {number} 章《{chapter_obj.get('title') or ''}》之后（同属第 {act_pos} 幕，后续章节顺延）。"
    else:
        placement = "插入位置：全书结局之后（第二部式延展）。"

    context = _base_context(store, bible, work_info, char_rows, acts)
    meta = {
        "title": work_info.get("title"),
        "logline": (bible.get("premise") or {}).get("logline"),
        "genre": work_info.get("genre") or [],
        "characters": char_rows,
        "acts": len(acts),
        "chapters_per_act": expected_chapters,
        "beats_per_chapter": expected_beats,
        "mode": mode,
        "placement": placement,
    }
    _progress(on_progress, {"type": "batch", "unit": "expand", "index": 1, "total": 1, "name": placement})

    new_blocks: list[dict] = []
    cast_created_total = 0
    cast_reused_total = 0
    if mode == "after_chapter":
        count = max(1, int(new_chapters or expected_chapters))
        block = {"title": anchor_act_obj.get("title") or "新增章", "summary": "", "chapters": []}
        chapters = _gen_chapters(
            adapter,
            config,
            context,
            {**meta, "anchor_note": f"上一章是第 {anchor_chapter} 章《{anchor_chapter_obj.get('title') or ''}》，新章要承接它并在其后续章节之前收束。"},
            anchor_act,
            {"title": anchor_act_obj.get("title") or f"第{anchor_act}幕", "summary": anchor_act_obj.get("summary") or ""},
            count,
            brief,
            on_progress,
            len(acts),
            chapter_total=count,
            chapters_done=0,
        )
        planned_beats = count * expected_beats
        pending: dict = {} if dry_run else load_pending(store)
        for offset, chapter in enumerate(chapters, start=1):
            beats, beat_payload = _gen_beats(
                adapter,
                config,
                context,
                meta,
                anchor_act,
                anchor_act_obj,
                offset,
                chapter,
                expected_beats,
                expected_words,
                on_progress,
                len(acts),
                chapter_total=count,
                beats_before=(offset - 1) * expected_beats,
                beat_total=planned_beats,
            )
            chapter["beats"] = beats
            # 节拍与本章 cast 同一次调用；这里只做确定性匹配与绑定
            report = _apply_expand_cast(
                store, chapter, beat_payload, anchor_chapter + offset, dry_run, on_progress, characters, pending
            )
            cast_created_total += len(report.get("created") or [])
            cast_reused_total += len(report.get("reused") or [])
            characters = list(report.get("characters") or characters)
            meta["characters"] = _characters_payload(characters)
        block["chapters"] = chapters
        new_blocks.append(block)
    else:
        count = max(1, int(new_acts or 1))
        work_acts = _gen_acts(adapter, config, context, meta, count, brief, on_progress)
        planned_chapters = count * expected_chapters
        planned_beats = planned_chapters * expected_beats
        pending: dict = {} if dry_run else load_pending(store)
        # 追加到全书之后：章序号接着现有章节数，登记与「本章新建」才对得上
        chapter_base = sum(len(item.get("chapters") or []) for item in acts)
        for act_pos, act in enumerate(work_acts, start=1):
            act_name = act.get("title") or f"新增第{act_pos}幕"
            _progress(
                on_progress,
                {
                    "type": "batch",
                    "unit": "act",
                    "phase": "chapters",
                    "index": act_pos,
                    "total": len(work_acts),
                    "name": act_name,
                    "chapters_done": (act_pos - 1) * expected_chapters,
                    "chapters_expected": expected_chapters,
                    "chapters_total": planned_chapters,
                },
            )
            act["chapters"] = _gen_chapters(
                adapter,
                config,
                context,
                meta,
                act_pos,
                act,
                expected_chapters,
                brief,
                on_progress,
                len(work_acts),
                chapter_total=count * expected_chapters,
                chapters_done=(act_pos - 1) * expected_chapters,
            )
            for local, chapter in enumerate(act["chapters"], start=1):
                chapter_pos = (act_pos - 1) * expected_chapters + local
                beats, beat_payload = _gen_beats(
                    adapter,
                    config,
                    context,
                    meta,
                    act_pos,
                    act,
                    chapter_pos,
                    chapter,
                    expected_beats,
                    expected_words,
                    on_progress,
                    len(work_acts),
                    chapter_total=planned_chapters,
                    beats_before=(chapter_pos - 1) * expected_beats,
                    beat_total=planned_beats,
                )
                chapter["beats"] = beats
                # 节拍与本章 cast 同一次调用；这里只做确定性匹配与绑定
                report = _apply_expand_cast(
                    store, chapter, beat_payload, chapter_base + chapter_pos, dry_run, on_progress, characters, pending
                )
                cast_created_total += len(report.get("created") or [])
                cast_reused_total += len(report.get("reused") or [])
                characters = list(report.get("characters") or characters)
                meta["characters"] = _characters_payload(characters)
            new_blocks.append(act)

    # 插入：新内容先转成设定库结构，再按模式拼接；已有对象原样保留
    built_blocks = [
        _to_outline_act(block, origin, expected_beats, expected_words) for block in new_blocks
    ]
    if mode == "after_act":
        insert_at = int(anchor_act or len(acts))
        acts[insert_at:insert_at] = built_blocks
    elif mode == "after_chapter":
        # 新章插在锚点章之后；章对象来自同一幕的章节块
        act_obj = anchor_act_obj
        position = list(act_obj.get("chapters") or []).index(anchor_chapter_obj) + 1
        act_obj["chapters"][position:position] = built_blocks[0]["chapters"]
    else:
        acts.extend(built_blocks)

    bible.setdefault("outline", {})["acts"] = acts
    renumber_failures = _renumber(bible, store, dry_run=dry_run)
    # 角色引用：先清洗非法引用（人名/空串/重复 ID）；auto 再按幕绑定世界并回落到 POV，none 只清洗
    # 只处理本次新增的章节，旧章节的引用保持原样
    new_view = (
        {"outline": {"acts": [{"chapters": built_blocks[0]["chapters"]}]}}
        if mode == "after_chapter"
        else {"outline": {"acts": built_blocks}}
    )
    stats = sanitize_outline_character_ids(new_view, characters)
    ignored_character_refs = int(stats["dropped"])
    if binding == "auto":
        # 只给新增章节绑世界/POV，已合格的旧章节保持原绑定不动
        from ...adapters.persistence.world_store import WorldStore

        world_store = WorldStore(store.root)
        worlds = world_store.list_worlds() if world_store.exists() else []
        if mode == "after_chapter":
            bind_outline_context({"outline": {"acts": [{"chapters": built_blocks[0]["chapters"]}]}}, worlds, characters)
        else:
            bind_outline_context({"outline": {"acts": built_blocks}}, worlds, characters)
    if ignored_character_refs:
        suffix = "已回落到本章 POV" if binding == "auto" else "binding=none 不自动选角"
        message = f"已忽略 {ignored_character_refs} 个无法识别的角色引用（{suffix}）"
        _progress(on_progress, {"type": "notice", "message": message})
    bound_stats = outline_binding_stats(new_view)
    if not dry_run:
        bible = settings_store.save(bible)

    # 逐层校验摘要：只看新增部分，设定值与实际值都列清楚
    rows: list[dict] = []
    inserted_acts: list[dict] = []
    inserted_chapters: list[dict] = []
    if mode == "after_chapter":
        # 某章后扩充只新增章：校验口径按「本次新增章数」算，位置沿用锚点幕
        block = built_blocks[0]
        expected_rows = max(1, len(block.get("chapters") or []))
        row = _act_validation(
            {"title": anchor_act_obj.get("title") or f"第{anchor_act}幕", "chapters": block.get("chapters") or []},
            anchor_act,
            expected_rows,
            expected_beats,
            expected_words,
            "",
        )
        row["origin"] = "新增章"
        rows.append(row)
        for chapter in block.get("chapters") or []:
            inserted_chapters.append(
                {
                    "chapter_id": chapter.get("id"),
                    "index": chapter.get("index"),
                    "title": chapter.get("title"),
                    "act_index": anchor_act,
                }
            )
        report_chapters = expected_rows
    else:
        for block in built_blocks:
            act_row = next((item for item in acts if item.get("id") == block.get("id")), None)
            position = acts.index(act_row) + 1 if act_row else 1
            row = _act_validation({"title": block.get("title"), "chapters": block.get("chapters")}, position,
                                  expected_chapters, expected_beats, expected_words, "")
            row["origin"] = "新增"
            rows.append(row)
            inserted_acts.append(
                {
                    "act_id": block.get("id"),
                    "title": block.get("title"),
                    "position": position,
                    "chapters": len(block.get("chapters") or []),
                }
            )
            for chapter in block.get("chapters") or []:
                inserted_chapters.append(
                    {
                        "chapter_id": chapter.get("id"),
                        "index": chapter.get("index"),
                        "title": chapter.get("title"),
                        "act_index": position,
                    }
                )
        report_chapters = expected_chapters

    report = _validation_report(rows, report_chapters, expected_beats, expected_words, len(rows) or 1)
    if mode == "after_chapter":
        # 只新增章的场景不写「幕」的合计，避免出现「设定 1 幕」的误导
        row = rows[0]
        report_text = (
            f"第 {anchor_act} 幕《{row['title']}》新增章：设定 {row['expected']['chapters']} 章 / "
            f"实际 {row['actual']['chapters']} 章（偏差 {row['delta']['chapters']:+d}，允许 "
            f"{row['range']['chapters'][0]}–{row['range']['chapters'][1]} 章）"
            + ("✓ 在允许区间" if row["in_range"] else "⚠ 超出允许区间")
            + (("；原因：" + "；".join(row["reasons"])) if row["reasons"] else "")
        )
    else:
        report_text = report["summary"]
    total_chapters = sum(len(act.get("chapters") or []) for act in acts)
    total_beats = sum(len(chapter.get("beats") or []) for act in acts for chapter in (act.get("chapters") or []))
    label = "续写" if mode == "append" else "扩充"
    lines = [f"{placement}", report_text,
             f"全书现为 {len(acts)} 幕 / {total_chapters} 章 / {total_beats} 个节拍；已有内容未改动，只新增。"]
    if renumber_failures:
        # 序号顺延失败不阻断大纲保存，但必须在页面上说明哪些章节没同步
        message = f"有 {len(renumber_failures)} 个章节序号顺延失败，可重新打开大纲或手动核对"
        _progress(on_progress, {"type": "notice", "message": message})
        lines.append(f"⚠ {message}：")
        lines.extend(f"- {item}" for item in renumber_failures)
    new_act_count = 0 if mode == "after_chapter" else len(built_blocks)
    return {
        "mode": mode,
        "origin": origin,
        "label": label,
        "inserted_acts": inserted_acts,
        "inserted_chapters": inserted_chapters,
        "expected": {"acts": new_act_count, "chapters_per_act": report_chapters, "beats_per_chapter": expected_beats},
        "actual": {"acts": new_act_count, "chapters": len(inserted_chapters)},
        "matches": report["matches"],
        "validation": report,
        "summary": "\n".join(lines),
        "total": {"acts": len(acts), "chapters": total_chapters, "beats": total_beats},
        "provider": getattr(adapter, "model", "") or "offline-writing",
        "settings_revision": bible.get("settings_revision"),
        "binding": binding,
        "ignored_character_refs": ignored_character_refs,
        "bound_characters": bound_stats["characters"],
        "bound_character_refs": bound_stats["refs"],
        "characters_created": cast_created_total,
        "characters_reused": cast_reused_total,
        "renumber_failures": renumber_failures,
        "dry_run": dry_run,
    }
