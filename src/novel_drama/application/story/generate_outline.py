"""用例：生成大纲（单次工具调用），并做数量校验与提示。"""

from __future__ import annotations

import logging
import math
import re

from ...adapters.base import (
    AdapterError,
    ErrorCode,
    Message,
    TextGenRequest,
)
from ...adapters.persistence import ProjectStore
from ...adapters.persistence.settings_store import SettingsStore
from ...infrastructure.config import AppConfig
from ...models.ids import new_id
from ...text.titles import strip_index_prefix
from ..support.llm_calls import (
    _char_delta,
    _characters_payload,
    _generate_json,
    _progress,
    build_writing_adapter,
)
from .chapter_limits import DEFAULT_CHAPTER_WORDS
from .outline_bind import (
    bind_outline_characters,
    outline_binding_stats,
    sanitize_outline_character_ids,
)
from .outline_validate import check_golden_three

logger = logging.getLogger(__name__)


def generate_outline(
    store: ProjectStore,
    config: AppConfig,
    acts: int = 3,
    chapters_per_act: int = 3,
    beats_per_chapter: int = 3,
    chapter_words: int = DEFAULT_CHAPTER_WORDS,
    dry_run: bool = False,
    binding: str = "auto",
    on_progress=None,
) -> dict:
    """单次工具调用生成幕/章/节拍大纲并写入设定库（CLI 兼容路径）。

    页面的分层大纲走 `application.story.outline_hierarchical.generate_hierarchical_outline`：
    一次规划全部幕 → 逐幕出章 → 逐章出节拍；旧的「按幕多轮」路径已删除。

    参数:
        store: 工程。
        config: 本机配置。
        acts: 幕数。
        chapters_per_act: 每幕章节数。
        beats_per_chapter: 每章节拍数。
        chapter_words: 单章目标字数（用于分配节拍字数）。
        dry_run: 只生成不落盘。
        binding: auto=把节拍绑定到已有人物；none=不改动角色引用。
        on_progress: 进度回调（batch/chars 事件）；仅页面流式接口传入。

    返回:
        {"acts": [...], "chapters": N, "beats": M, "provider": name, "settings_revision": r,
        "golden_three": {"passed", "issues"}, "validation": {...}, "summary": "..."}。

    异常:
        AdapterError: 模型输出不是合法大纲 JSON。
    """
    settings_store = SettingsStore(store.root)
    bible = settings_store.ensure()
    project = store.load_project()
    work = project.get("work") or {}
    characters = store.load_characters()["characters"]
    adapter = build_writing_adapter(config, bible)

    # 工具参数里的 character_ids 需要真实 id，把角色表一并写进提示词
    char_rows = _characters_payload(characters)
    context_parts = [f"为《{work.get('title') or '未命名'}》生成 {acts} 幕大纲，每幕 {chapters_per_act} 章，每章 {beats_per_chapter} 个节拍。"]
    logline = (bible.get("premise") or {}).get("logline")
    if logline:
        context_parts.append(f"一句话故事：{logline}")
    genres = work.get("genre") or []
    if genres:
        context_parts.append(f"题材：{'、'.join(str(item) for item in genres)}")
    if char_rows:
        context_parts.append("可用人物（id=姓名）：" + "、".join(f"{item['id']}={item.get('name')}" for item in char_rows))

    req = TextGenRequest(
        task="outline",
        system="你是长篇小说的结构编辑，按工具参数输出大纲，不要解释。",
        messages=[Message("user", "\n".join(context_parts))],
        stream=True,
        metadata={
            "acts": acts,
            "chapters_per_act": chapters_per_act,
            "beats_per_chapter": beats_per_chapter,
            "chapter_words": chapter_words,
            "title": work.get("title"),
            "logline": logline,
            "genre": genres,
            "characters": char_rows,
        },
    )
    progress_state = {"chars": 0}
    _progress(on_progress, {"type": "batch", "index": 1, "total": 1, "unit": "outline"})
    payload = _generate_json(
        adapter,
        req,
        "outline",
        on_delta=_char_delta(progress_state, on_progress, 1, 1),
    )
    raw_acts = payload.get("acts")
    # 只校验不改内容：数量偏差在生成完成后的校验摘要里标注
    if isinstance(raw_acts, list):
        for index, raw_act in enumerate(raw_acts, start=1):
            if isinstance(raw_act, dict):
                issues = _act_validation(raw_act, index, chapters_per_act, beats_per_chapter, chapter_words)
                if not issues["in_range"]:
                    logger.warning(
                        "大纲第 %s 幕数量超出允许区间：设定 %s 章，实际 %s 章",
                        index,
                        issues["expected"]["chapters"],
                        issues["actual"]["chapters"],
                    )
    if not isinstance(raw_acts, list) or not raw_acts:
        raise AdapterError(ErrorCode.INVALID_INPUT, "写作适配器返回的大纲为空：acts 必须是非空数组")
    # 结构化补全：分配稳定 ID、合并到设定库（不覆盖已有正文）
    built_acts = []
    chapter_count = 0
    beat_count = 0
    for act_index, raw_act in enumerate(raw_acts):
        if not isinstance(raw_act, dict):
            raise AdapterError(ErrorCode.INVALID_INPUT, f"大纲第 {act_index + 1} 幕必须是对象，实际为：{str(raw_act)[:80]}")
        act = SettingsStore.new_act(
            strip_index_prefix(str(raw_act.get("title") or "")) or f"第{act_index + 1}幕",
            raw_act.get("summary") or "",
        )
        for raw_chapter in raw_act.get("chapters") or []:
            chapter_count += 1
            if not isinstance(raw_chapter, dict):
                raise AdapterError(ErrorCode.INVALID_INPUT, f"大纲第 {chapter_count} 章必须是对象，实际为：{str(raw_chapter)[:80]}")
            existing = None
            for item in settings_store.all_outline_chapters(bible):
                if int(item.get("index") or 0) == chapter_count:
                    existing = item
                    break
            chapter_id = (existing or {}).get("id") or new_id("chp")
            outline_chapter = SettingsStore.new_outline_chapter(
                chapter_id,
                chapter_count,
                strip_index_prefix(str(raw_chapter.get("title") or "")) or f"第{chapter_count}章",
                raw_chapter.get("summary") or "",
            )
            for raw_beat in raw_chapter.get("beats") or []:
                beat_count += 1
                if not isinstance(raw_beat, dict):
                    raise AdapterError(ErrorCode.INVALID_INPUT, f"大纲第 {beat_count} 个节拍必须是对象，实际为：{str(raw_beat)[:80]}")
                outline_chapter["beats"].append(
                    SettingsStore.new_beat(
                        raw_beat.get("summary") or f"第{beat_count}个节拍",
                        character_ids=raw_beat.get("character_ids") or [],
                        turning_point=bool(raw_beat.get("turning_point")),
                        target_words=int(raw_beat.get("target_words") or chapter_words // max(1, beats_per_chapter)),
                    )
                )
            act["chapters"].append(outline_chapter)
        built_acts.append(act)

    bible.setdefault("outline", {})["acts"] = built_acts
    # 角色引用：先清洗非法引用（人名/空串/重复 ID）；auto 再按幕绑定世界并回落到 POV，none 只清洗
    stats = sanitize_outline_character_ids(bible, characters)
    ignored_character_refs = int(stats["dropped"])
    if binding == "auto":
        bind_outline_characters(store, bible)
    if ignored_character_refs:
        suffix = "已回落到本章 POV" if binding == "auto" else "binding=none 不自动选角"
        message = f"已忽略 {ignored_character_refs} 个无法识别的角色引用（{suffix}）"
        logger.info(message)
        _progress(on_progress, {"type": "notice", "message": message})
    bound_characters = outline_binding_stats(bible)["characters"]
    golden = check_golden_three(bible)
    if not dry_run:
        bible = settings_store.save(bible)
    # 逐层校验（只校验不改内容）：幕数、每幕章数、每章节拍数、每幕目标字数
    validation_rows: list[dict] = []
    for index, raw_act in enumerate(raw_acts, start=1):
        if not isinstance(raw_act, dict):
            continue
        row = _act_validation(
            raw_act,
            index,
            chapters_per_act,
            beats_per_chapter,
            chapter_words,
            str(raw_act.get("_finish_reason") or ""),
        )
        rewrite = raw_act.get("_rewrite") or {}
        row["rewrites"] = {
            "act_attempts": int(rewrite.get("act_attempts") or 0),
            "unresolved": list(rewrite.get("unresolved") or []),
            "chapter_issues": {
                key: value for key, value in (rewrite.get("chapter_attempts") or {}).items() if value
            },
        }
        if not row["in_range"]:
            row["reasons"].append(
                f"已按层重写 {row['rewrites']['act_attempts']} 次仍越界，保留最后一次结果供你决定"
                if row["rewrites"]["act_attempts"]
                else "未触发重写：越界发生在重写之后或尚未重写"
            )
        validation_rows.append(row)
    report = _validation_report(
        validation_rows, chapters_per_act, beats_per_chapter, chapter_words, max(1, int(acts))
    )
    return {
        "acts": built_acts,
        "chapters": chapter_count,
        "beats": beat_count,
        "provider": getattr(adapter, "model", "") or "offline-writing",
        "settings_revision": bible.get("settings_revision"),
        "binding": binding,
        "ignored_character_refs": ignored_character_refs,
        "bound_characters": bound_characters,
        "golden_three": golden,
        "dry_run": dry_run,
        "expected": {
            "acts": max(1, int(acts)),
            "chapters_per_act": max(1, int(chapters_per_act)),
            "beats_per_chapter": max(1, int(beats_per_chapter)),
        },
        "actual": {"acts": len(built_acts), "chapters": chapter_count, "beats": beat_count},
        "matches": report["matches"],
        "validation": report,
        "summary": report["summary"],
    }

def _act_validation(
    act: dict,
    index: int,
    chapters_per_act: int,
    beats_per_chapter: int,
    chapter_words: int,
    finish_reason: str = "",
) -> dict:
    """校验单幕的章数/节拍数/字数是否落在允许区间，并给出偏差原因（只校验不改内容）。

    参数:
        act: 单幕对象（模型原始输出，未裁剪）。
        index: 幕序号（从 1 开始）。
        chapters_per_act: 设定每幕章数。
        beats_per_chapter: 设定每章节拍数。
        chapter_words: 设定单章字数。
        finish_reason: 本幕上一次请求的 finish_reason。

    返回:
        `{act_index, title, expected, actual, delta, range, in_range, flags, reasons, finish_reason}`。
    """
    chapters = [item for item in (act.get("chapters") or []) if isinstance(item, dict)]
    actual_chapters = len(chapters)
    actual_beats = sum(
        len([beat for beat in (chapter.get("beats") or []) if isinstance(beat, dict)]) for chapter in chapters
    )
    actual_words = 0
    words_known = False
    for chapter in chapters:
        for beat in chapter.get("beats") or []:
            if not isinstance(beat, dict):
                continue
            try:
                words = int(beat.get("target_words") or 0)
            except (TypeError, ValueError):
                words = 0
            if words > 0:
                words_known = True
            actual_words += max(0, words)
    exp_chapters = max(1, int(chapters_per_act))
    exp_beats = exp_chapters * max(1, int(beats_per_chapter))
    exp_words = exp_chapters * max(1, int(chapter_words))
    ch_tol = _tolerance(exp_chapters)
    beat_tol = _tolerance(exp_beats)
    word_tol = max(1, math.ceil(exp_words * 0.2))
    flags = {
        "chapters": exp_chapters - ch_tol <= actual_chapters <= exp_chapters + ch_tol,
        # 节拍层按「每章单独校验」，任一章越界就算本幕节拍层越界
        "beats": all(not _chapter_beat_issues(chapter, beats_per_chapter) for chapter in chapters),
        "words": not words_known or exp_words - word_tol <= actual_words <= exp_words + word_tol,
    }
    reasons: list[str] = []
    note = str(act.get("note") or act.get("reason") or "").strip()
    if note:
        reasons.append("模型旁注：" + note[:160])
    if not flags["chapters"]:
        if actual_chapters > exp_chapters:
            if _looks_split(chapters):
                reasons.append("推断：模型把一章拆成多章（多个标题重复或互相包含）")
            else:
                reasons.append("推断：模型自行扩写，新增支线或情节点，把本幕展开得比设定更长")
        elif finish_reason == "length":
            reasons.append("输出被截断（finish_reason=length），模型只返回了部分章节")
        else:
            reasons.append("推断：模型少给章节，可能把多章合并，或单次输出空间不足")
    if not flags["beats"]:
        detail = []
        for index, chapter in enumerate(chapters, start=1):
            issues = _chapter_beat_issues(chapter, beats_per_chapter)
            if issues:
                detail.append(f"第 {index} 章" + issues[0])
        reasons.append("；".join(detail[:3]) or "章内节拍号数量与设定不符")
    if not flags["words"]:
        reasons.append(f"目标字数与设定不符（设定每章 {max(1, int(chapter_words))} 字，实际合计 {actual_words} 字）")
    return {
        "act_index": index,
        "title": str(act.get("title") or f"第{index}幕"),
        "expected": {"chapters": exp_chapters, "beats": exp_beats, "words": exp_words},
        "actual": {"chapters": actual_chapters, "beats": actual_beats, "words": actual_words},
        "delta": {
            "chapters": actual_chapters - exp_chapters,
            "beats": actual_beats - exp_beats,
            "words": actual_words - exp_words,
        },
        "range": {
            "chapters": [exp_chapters - ch_tol, exp_chapters + ch_tol],
            "beats": [exp_beats - beat_tol, exp_beats + beat_tol],
            "words": [exp_words - word_tol, exp_words + word_tol],
        },
        "in_range": all(flags.values()),
        "flags": flags,
        "reasons": reasons,
        "finish_reason": finish_reason,
    }

def _validation_report(
    rows: list[dict], chapters_per_act: int, beats_per_chapter: int, chapter_words: int, acts_expected: int | None = None
) -> dict:
    """把逐幕校验结果汇总成页面摘要（含整本合计）。

    参数:
        rows: `_act_validation` 的结果列表。
        chapters_per_act: 设定每幕章数。
        beats_per_chapter: 设定每章节拍数。
        chapter_words: 设定单章字数。
        acts_expected: 设定幕数；省略时按实际幕数计算。

    返回:
        `{rows, out_of_range, matches, summary}`。
    """
    out = [row for row in rows if not row["in_range"]]
    exp_acts = max(1, int(acts_expected or len(rows)))
    exp_chapters = exp_acts * max(1, int(chapters_per_act))
    exp_beats = exp_chapters * max(1, int(beats_per_chapter))
    total_chapters = sum(row["actual"]["chapters"] for row in rows)
    total_beats = sum(row["actual"]["beats"] for row in rows)
    lines = []
    for row in rows:
        rewrites = row.get("rewrites") or {}
        attempts = int(rewrites.get("act_attempts") or 0)
        mark = "✓ 在允许区间" if row["in_range"] else "⚠ 超出允许区间"
        delta = row["delta"]["chapters"]
        lines.append(
            f"第 {row['act_index']} 幕《{row['title']}》：设定 {row['expected']['chapters']} 章 / "
            f"实际 {row['actual']['chapters']} 章（偏差 {delta:+d}，允许 "
            f"{row['range']['chapters'][0]}–{row['range']['chapters'][1]} 章）{mark}"
            + (f"；已重写 {attempts} 次" if attempts else "")
            + ("；原因：" + "；".join(row["reasons"]) if row["reasons"] else "")
        )
    lines.append(
        f"合计：设定 {exp_acts} 幕 / {exp_chapters} 章 / {exp_beats} 个节拍，"
        f"实际 {len(rows)} 幕 / {total_chapters} 章 / {total_beats} 个节拍"
    )
    return {
        "rows": rows,
        "out_of_range": [row["act_index"] for row in out],
        "matches": not out,
        "summary": "\n".join(lines),
    }

def _tolerance(expected: int) -> int:
    """允许偏差：至少 1，最多为设定的 20%（向上取整）。

    参数:
        expected: 设定值。

    返回:
        允许的正负偏差。
    """
    return max(1, math.ceil(max(1, int(expected)) * 0.2))

def _looks_split(chapters: list[dict]) -> bool:
    """判断章节是否像被模型拆成了多章（标题高度重复）。

    参数:
        chapters: 章节列表。

    返回:
        True 表示存在重复或互相包含的标题。
    """
    titles = [re.sub(r"[\s\d０-９一二三四五六七八九十、：:.\-—]+", "", str(item.get("title") or "")) for item in chapters]
    titles = [title for title in titles if title]
    if len(titles) < 3:
        return False
    unique = len(set(titles))
    if unique <= len(titles) * 0.7:
        return True
    return any(
        left != right and (left in right or right in left)
        for index, left in enumerate(titles)
        for right in titles[index + 1 :]
    )

def _chapter_beat_issues(chapter: dict, beats_per_chapter: int) -> list[str]:
    """检查单章节拍数是否落在允许区间（每章单独校验）。

    参数:
        chapter: 章对象（模型原始输出）。
        beats_per_chapter: 设定每章节拍数。

    返回:
        问题描述列表；空列表表示在区间内。
    """
    expected = max(1, int(beats_per_chapter))
    tol = _tolerance(expected)
    actual = len([beat for beat in (chapter.get("beats") or []) if isinstance(beat, dict)])
    if expected - tol <= actual <= expected + tol:
        return []
    return [f"实际 {actual} 个节拍（设定 {expected} 个，允许 {expected - tol}–{expected + tol} 个）"]
