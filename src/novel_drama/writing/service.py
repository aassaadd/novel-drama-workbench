"""写作侧业务编排：大纲生成/落地、章节正文生成、导演脚本建议。

模型调用统一走 `WritingAdapter`：默认 `offline`（离线规则实现，零依赖可跑通），
配置 `providers.writing=omlx` 时切到真实 LLM，业务代码不变。
"""

from __future__ import annotations

import copy
import inspect
import json
import logging
import math
import re
from dataclasses import replace

from ..adapters.base import (
    AdapterError,
    ErrorCode,
    Message,
    TextGenRequest,
    TextGenResult,
)
from ..adapters.offline_writing import OfflineWritingAdapter
from ..cancel import GenerationCanceled
from ..config import AppConfig, resolve_temperature
from ..director.markup import parse_markup
from ..domain.story.rules.chapter_rules import bump_revision, count_words
from ..models import entities
from ..models.enums import AgeRange, SpeechPitch, SpeechTempo
from ..models.ids import new_id, now_iso
from ..oplog import set_llm_abort_reason, set_llm_call_reason
from ..store import ProjectStore
from ..store.settings_store import SettingsStore
from ..store.world_store import WorldStore
from ..text.context import POV_LABELS, ContextAssembler
from ..text.dialogue import normalize_dialogue
from ..text.titles import strip_index_prefix
from . import chapter_checkpoint
from .budgets import context_budget_tokens
from .content_guard import check_chapter_content, strict_retry_instruction
from .degeneration import (
    DegenerationDetected,
    DegenerationGuard,
    keep_at_sentence_end,
)
from .degeneration import (
    continue_instruction as degeneration_continue_instruction,
)
from .degeneration import (
    retry_instruction as degeneration_retry_instruction,
)
from .length_plan import length_instruction, length_plan
from .recap import (
    final_round_summary_instruction,
    save_summary,
    split_final_output,
)
from .thinking import collect_overrides, step_stage
from .watchdog import describe as describe_watchdog
from .worlds import build_art_prompt as _build_art_prompt
from .worlds import clean_world_entry, worlds_digest

# 默认每章目标字数
DEFAULT_CHAPTER_WORDS = 1200

#: 超纲阈值（全工程唯一出处，页面文案与此同源）：
#: > 1.5 倍只在结果里标字数；>= 2 倍才提供「按目标字数压缩」「重写」两个动作。
OVERSHOOT_NOTICE_RATIO = 1.5
OVERSHOOT_ACTION_RATIO = 2.0
#: 不足阈值：低于目标 0.7 倍时提供「补写」动作
UNDERFILL_ACTION_RATIO = 0.7
#: 页面上展示的动作（id 与 generate_chapter 的 mode 对应）
OVERSHOOT_ACTIONS = (
    {"id": "compress", "label": "按目标字数压缩"},
    {"id": "rewrite", "label": "重写"},
)
UNDERFILL_ACTIONS = ({"id": "supplement", "label": "补写"},)

#: 补写时的要点覆盖判定：节拍摘要关键词在正文里的命中率低于此值，视为「尚未覆盖」。
#: 只用于挑出要补的情节要点，不参与长度判断。
SUPPLEMENT_COVERAGE_RATIO = 0.34
#: 退化后的局部续写次数上限（调研 2.3 / 6.1：同一节最多回退 2 次，再失败整节重写）
MAX_LOCAL_CONTINUATIONS = 2

#: 日志：工具调用解析失败时记录原始片段，便于定位模型返回了什么
logger = logging.getLogger(__name__)


def build_writing_adapter(config: AppConfig, settings: dict | None = None):
    """按配置装配写作适配器。

    参数:
        config: 本机配置。
        settings: 工程设置（透传给实现，可选）。

    返回:
        实现 `WritingAdapter` 协议的实例。

    异常:
        AdapterError(INVALID_INPUT): 配置名未知。
    """
    # 配置里适配器写在 adapters.writing；providers.writing 是旧键，保留兼容
    name = str(config.get("adapters.writing") or config.get("providers.writing") or "").strip()
    if not name:
        # 不能默认回落到离线假数据：配置缺失必须明确报错，避免假结果写进正式产物
        raise AdapterError(
            ErrorCode.INVALID_INPUT,
            "未配置写作模型（adapters.writing）：请在设置页选择 omlx 并填写真实服务地址与模型；"
            "离线适配器只能显式配置为 offline 时使用",
        )
    if name in ("offline", "offline-writing"):
        return OfflineWritingAdapter(config.get("writing.offline") or settings or {})
    if name == "omlx":
        from ..adapters.writing import OmlxWritingAdapter

        # 思考：全局开关 + 强度 + 设置页按调用点覆盖（没有覆盖项即系统自动）
        settings = dict(config.get("omlx") or {})
        settings["thinking_stages"] = collect_overrides(config)
        return OmlxWritingAdapter(settings)
    raise AdapterError(ErrorCode.INVALID_INPUT, f"未知写作适配器: {name}（可选 offline / omlx）")


def _characters_payload(characters: list[dict]) -> list[dict]:
    """裁剪角色表供提示词使用。

    参数:
        characters: 完整角色表。

    返回:
        [{id, name, role, aliases, speech_style}]。
    """
    return [
        {
            "id": item["id"],
            "name": item.get("name"),
            "role": item.get("role"),
            "aliases": item.get("aliases") or [],
            "speech_style": item.get("speech_style") or {},
        }
        for item in characters
        if item.get("role") != "narrator"
    ]


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

    页面的分层大纲走 `writing.outline_hier.generate_hierarchical_outline`：
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


def _raw_preview(payload) -> str:
    """把工具参数压成一段可写入错误的原文片段。"""
    try:
        text = json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(payload)
    return text[:500] or "（空）"


def _maybe_json_obj(value) -> dict | None:
    """把 JSON 字符串（可被 ``` 代码块包裹）解析成非空对象。

    参数:
        value: 任意值；只有字符串会尝试解析。

    返回:
        解析出的非空 dict；不是可解析对象时返回 None。
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    fence = re.match(r"^```[a-zA-Z0-9_-]*\s*(.*?)\s*```$", text, flags=re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) and data else None


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


def apply_outline(store: ProjectStore, dry_run: bool = False) -> dict:
    """把设定库大纲落地成章节文件。

    已存在的章节只同步标题与大纲文本，不覆盖正文；缺失的按序创建。

    参数:
        store: 工程。
        dry_run: 只统计不落盘。

    返回:
        {"created": [...], "updated": [...], "chapters": N}。

    异常:
        ValueError: 设定库里没有大纲。
    """
    settings_store = SettingsStore(store.root)
    bible = settings_store.ensure()
    entries = settings_store.all_outline_chapters(bible)
    if not entries:
        raise ValueError("设定库还没有大纲：先执行 ndw outline generate")

    project = store.load_project()
    work_id = project["work"]["id"]
    existing_ids = {item["id"] for item in store.list_chapters()}
    created: list[str] = []
    updated: list[str] = []

    for entry in sorted(entries, key=lambda item: int(item.get("index") or 0)):
        outline_text = "\n".join(
            f"{index}. {beat.get('summary')}" for index, beat in enumerate(entry.get("beats") or [], start=1)
        ) or (entry.get("summary") or "")
        if entry["id"] not in existing_ids:
            if dry_run:
                created.append(f"{entry.get('index')} {entry.get('title')}（待创建）")
                continue
            chapter = entities.new_chapter(work_id, int(entry.get("index") or 1), entry.get("title") or "未命名", outline=outline_text)
            chapter["id"] = entry["id"]
            # POV 以设定库为主源，章节文件只是副本：落地时一并写通
            chapter["pov_character_id"] = entry.get("pov_character_id")
            chapter["pov_mode"] = entry.get("pov_mode")
            store.save_chapter(chapter)
            created.append(chapter["id"])
            existing_ids.add(chapter["id"])
        else:
            # 列表接口只返回轻量元信息，写回前必须读完整章节，否则会丢掉 work_id 等必填字段
            chapter = store.load_chapter(entry["id"])
            changed = False
            for key, value in (
                ("title", entry.get("title")),
                ("outline", outline_text),
                ("pov_character_id", entry.get("pov_character_id")),
                ("pov_mode", entry.get("pov_mode")),
            ):
                if key in ("pov_character_id", "pov_mode"):
                    # POV 副本允许为空（全知视角），不能因为空值跳过写通
                    if chapter.get(key) != value:
                        chapter[key] = value
                        changed = True
                    continue
                if value and chapter.get(key) != value:
                    chapter[key] = value
                    changed = True
            if changed:
                bump_revision(chapter)
                chapter["updated_at"] = now_iso()
                if not dry_run:
                    store.save_chapter(chapter)
                updated.append(chapter["id"])
    return {"created": created, "updated": updated, "chapters": len(entries), "dry_run": dry_run}


def _chapter_beats(bible: dict, chapter: dict, settings_store: SettingsStore) -> list[dict]:
    """取章节对应的节拍；大纲中没有时从 chapter.outline 文本回退拆解。

    参数:
        bible: 设定库。
        chapter: 章节。
        settings_store: 设定库读写。

    返回:
        节拍列表（至少一项）。
    """
    entry = settings_store.find_outline_chapter(bible, chapter["id"], index=int(chapter.get("index") or 0))
    if entry and entry.get("beats"):
        return entry["beats"]
    outline = (chapter.get("outline") or "").strip()
    if outline:
        lines = [line.strip(" .、0123456789") for line in outline.splitlines() if line.strip()]
        if lines:
            return [{"summary": line, "character_ids": [], "turning_point": index == len(lines) - 1, "target_words": 0} for index, line in enumerate(lines)]
    return [{"summary": chapter.get("title") or "推进主线", "character_ids": [], "turning_point": False, "target_words": 0}]


def build_chapter_context(store: ProjectStore, config: AppConfig, chapter: dict) -> dict:
    """准备章节生成与 LLM 编辑共用的上下文素材。

    参数:
        store: 工程。
        config: 本机配置。
        chapter: 章节 dict（含 id/index/title/outline/content）。

    返回:
        {bible, work, characters, beats, chapter_words, entry, world_ids, world_context,
         character_payload, speaker_ids, previous_chapters, book_digest, style, adapter, metadata}。
        metadata 字段与 `generate_chapter` 原有约定完全一致，离线适配器行为不变。
    """
    settings_store = SettingsStore(store.root)
    bible = settings_store.ensure()
    work = store.load_project().get("work") or {}
    characters = store.load_characters()["characters"]
    beats = _chapter_beats(bible, chapter, settings_store)
    chapter_words = int(sum(int(beat.get("target_words") or 0) for beat in beats) or config.get("writing.default_chapter_words") or 1200)
    if chapter_words <= 0:
        chapter_words = DEFAULT_CHAPTER_WORDS
    adapter = build_writing_adapter(config, bible)

    entry = settings_store.find_outline_chapter(bible, chapter.get("id"), index=int(chapter.get("index") or 0))
    world_ids = list((entry or {}).get("world_ids") or [])
    world_store = WorldStore(store.root)
    world_context = _worlds_digest(world_store.find_many(world_ids) if (world_ids and world_store.exists()) else [], world_ids)
    if world_context is None and world_store.exists():
        # 未绑定世界时回退到世界库里的第一套世界（总述 + 分类条目），写作设定不再保存世界观
        all_worlds = world_store.list_worlds()
        if all_worlds:
            world_context = _worlds_digest(all_worlds, [str(all_worlds[0].get("id") or "")])

    character_payload = _characters_payload(characters)
    # POV 以设定库为唯一主源；章节文件里的 pov_character_id 只是副本（保存时写通）
    pov_character_id = (entry or {}).get("pov_character_id") or chapter.get("pov_character_id")
    if pov_character_id == entities.NARRATOR_ID:
        pov_character_id = None
    # 章节视角覆盖优先于全书；显式全知时不要求绑定具体角色
    pov_mode = (entry or {}).get("pov_mode") or chapter.get("pov_mode") or None
    name_by_id = {item.get("id"): item.get("name") for item in characters if item.get("id")}
    planned_counts: dict[str, int] = {}
    for beat in beats:
        for cid in beat.get("character_ids") or []:
            if cid:
                planned_counts[str(cid)] = planned_counts.get(str(cid), 0) + 1
    speaker_ids: list[str] | None = None
    if entry:
        # 角色上下文取本章 POV 与节拍涉及的角色；找不到时保留全量角色
        focus = {cid for cid in [pov_character_id] if cid}
        focus.update(planned_counts)
        focus.discard(entities.NARRATOR_ID)
        focused = [item for item in character_payload if item["id"] in focus]
        if focused:
            character_payload = focused
        if focus:
            speaker_ids = sorted(focus)

    # 前序章节：index 小于当前章节，取最近 N 章（默认 6），再转成从旧到新
    previous_index = int(chapter.get("index") or 0)
    try:
        recent_limit = max(1, int(config.get("writing.context.recent_summaries") or 6))
    except (TypeError, ValueError):
        recent_limit = 6
    previous_meta = [item for item in store.list_chapters() if int(item.get("index") or 0) < previous_index]
    previous_meta.sort(key=lambda item: int(item.get("index") or 0), reverse=True)
    selected = sorted(previous_meta[:recent_limit], key=lambda item: int(item.get("index") or 0))
    previous_chapters: list[dict] = []
    for position, meta in enumerate(selected):
        # 只有最后一章需要正文末尾做「上一章结尾」摘录，其余章只带摘要，避免逐章读全文
        content = ""
        synopsis = meta.get("synopsis") or ""
        if position == len(selected) - 1:
            full = store.load_chapter(meta["id"])
            content = full.get("content") or ""
            synopsis = synopsis or full.get("synopsis") or ""
        previous_chapters.append(
            {
                "index": meta.get("index"),
                "title": meta.get("title"),
                "synopsis": synopsis,
                "content": content,
            }
        )

    # 全书（或前半部）滚动摘要：与最近章摘要一起注入，长书不会退化成只看最近几章
    book_digest = str(store.load_rolling_summary().get("book_summary") or "")

    style = bible.get("style") or {}
    pov_name = None
    if pov_character_id:
        pov_name = name_by_id.get(pov_character_id) or "已删除角色"
    beat_payload = [
        {
            "id": beat.get("id"),
            "index": position,
            "summary": beat.get("summary"),
            "character_ids": list(beat.get("character_ids") or []),
            "character_names": [
                name_by_id.get(str(cid)) or "已删除角色"
                for cid in (beat.get("character_ids") or [])
                if str(cid).strip()
            ],
            "turning_point": bool(beat.get("turning_point")),
            "target_words": int(beat.get("target_words") or 0),
        }
        for position, beat in enumerate(beats, start=1)
    ]
    metadata = {
        "title": chapter.get("title"),
        "summary": (entry or {}).get("summary") or chapter.get("outline"),
        "beats": beats,
        "beat_characters": beat_payload,
        "characters": character_payload,
        "character_names": name_by_id,
        "planned_counts": planned_counts,
        "world": world_context,
        "world_ids": world_ids,
        "pov_character_id": pov_character_id,
        "pov_name": pov_name,
        "pov_mode": pov_mode,
        "pov": style.get("pov"),
        "tone": style.get("tone"),
        "target_words": chapter_words,
    }
    return {
        "bible": bible,
        "work": work,
        "characters": characters,
        "beats": beats,
        "chapter_words": chapter_words,
        "entry": entry,
        "world_ids": world_ids,
        "world_context": world_context,
        "character_payload": character_payload,
        "speaker_ids": speaker_ids,
        "pov_character_id": pov_character_id,
        "pov_mode": pov_mode,
        "planned_counts": planned_counts,
        "previous_chapters": previous_chapters,
        "book_digest": book_digest,
        "style": style,
        "adapter": adapter,
        "metadata": metadata,
    }


def stream_text(
    adapter,
    request: TextGenRequest,
    on_delta,
    guard: DegenerationGuard | None = None,
) -> TextGenResult:
    """流式调用写作适配器，逐段回调增量文本并聚合为 TextGenResult。

    适配器不支持流式（抛 NotImplementedError）时回落到一次性生成，
    并把整段文本作为一次增量回调，保证调用方逻辑一致。
    传入 `guard` 时在接收过程中检测重复退化：命中立即停止读取并抛
    `DegenerationDetected`，异常里带可读原因、已接收字数与建议保留长度
    （截到循环起点前一句），由调用方决定局部续写还是整节重写。

    参数:
        adapter: 写作适配器。
        request: 统一文本请求。
        on_delta: 增量回调 `(text) -> None`；可为 None。
        guard: 退化检测器；None 时不做检测。

    返回:
        聚合后的 TextGenResult。

    异常:
        DegenerationDetected: 命中重复退化，携带可读原因与已接收字数。
    """
    parts: list[str] = []
    usage = None
    try:
        for chunk in adapter.stream(request):
            if chunk.delta:
                parts.append(chunk.delta)
                if guard is not None:
                    reason = guard.feed(chunk.delta)
                    if reason:
                        # 日志原因码：本轮是我们自己因退化主动停流的
                        set_llm_abort_reason("degeneration", "service.stream_text: 退化熔断")
                        raise guard.exception(len("".join(parts)))
                if on_delta is not None:
                    on_delta(chunk.delta)
            if chunk.usage is not None:
                usage = chunk.usage
            if chunk.done:
                # 上游已给 finish_reason：属于正常结束，关闭迭代器不应记成「调用方断开」
                set_llm_abort_reason("natural_end", "service.stream_text: done 后停止读取")
                break
    except NotImplementedError:
        result = adapter.generate(request)
        if guard is not None and result.text:
            reason = guard.feed(result.text) or guard.check_all()
            if reason:
                raise guard.exception(len(result.text)) from None
        if result.text and on_delta is not None:
            on_delta(result.text)
        return result
    # 收尾再查一次：增量不足一个检查步长时（短输出）也要判定
    if guard is not None and guard.text:
        reason = guard.check_all()
        if reason:
            raise guard.exception(len(guard.text))
    return TextGenResult(
        text="".join(parts),
        finish_reason="stop",
        model=str(getattr(adapter, "model", "") or ""),
        usage=usage,
    )


def generate_chapter(
    store: ProjectStore,
    config: AppConfig,
    chapter_id: str,
    regen: bool = False,
    dry_run: bool = False,
    multi_turn: bool | None = None,
    beats_per_round: int | None = None,
    max_rounds: int | None = None,
    on_delta=None,
    on_notice=None,
    on_progress=None,
    mode: str = "generate",
) -> dict:
    """生成章节正文并落盘；按本章节拍逐段推进，节拍写完即收尾。

    停止条件只看节拍：一轮写一个或一组节拍，写完后推进指针；全部节拍写完就结束。
    字数只作提示与偏差说明，不作为硬性截断依据；`max_rounds` 仅作保险上限。

    `mode` 只用于超纲后的用户主动动作（不会自动触发）：
    - `generate`：正常生成 / 断点续写；
    - `compress`：在保留全部节拍要点的前提下把已有正文压缩到目标字数；
    - `rewrite`：按原参数整章重写（等同 `regen=True`，但结果会带前后字数说明）。

    参数:
        store: 工程。
        config: 本机配置。
        chapter_id: 章节 ID。
        regen: 已有正文时是否覆盖重写。
        dry_run: 只生成不落盘。
        multi_turn: 显式分轮开关；None 时按 `writing.multi_turn.enabled` 自动判断。
        beats_per_round: 每轮写几个节拍；None 时按「节拍总数 / max_rounds」自动分配。
        max_rounds: 最大轮数（保险）；None 时取 `writing.multi_turn.max_rounds`（默认 8）。
        on_delta: 流式增量回调 `(text) -> None`；传入时改为流式生成，仅用于页面实时预览。
        on_notice: 自动补救说明回调 `(text) -> None`；内容校验重试成功时上报，
            调用方需把说明带进最终结果，不能静默处理。
        on_progress: 进度回调 `(event) -> None`；每轮开始时上报
            `{"type": "round", "round", "rounds_total", "beats_done", "chars"}`，
            供页面显示「第几轮 + 已写字数」，避免超长或退化时看起来像卡死。
        mode: generate / compress / rewrite；compress 与 rewrite 必须由用户点击触发。

    返回:
        {"chapter_id", "status", "words", "word_count", "provider", "content",
         "rounds", "beats_total", "beats_done", "beats_remaining", "multi_turn",
         "stop_reason", "word_deviation", "dialogue_warnings", "summary", "summary_error",
         "mode", "overshoot_note"}；
        content 在 dry_run 时为全文预览。

    异常:
        ValueError: 已有正文且未指定 regen；mode 非法或压缩时没有正文。
        AdapterError: 模型返回空正文、压缩结果未保住全部节拍或调用失败（明确报错，不做降级）。
    """
    mode = str(mode or "generate").strip().lower()
    if mode not in ("generate", "compress", "rewrite", "supplement"):
        raise ValueError("mode 只能是 generate（正常生成）、compress（压缩）、rewrite（重写）或 supplement（补写）")
    if mode == "compress":
        return compress_chapter(
            store, config, chapter_id, on_delta=on_delta, on_notice=on_notice, on_progress=on_progress
        )
    if mode == "supplement":
        return supplement_chapter(
            store, config, chapter_id, on_delta=on_delta, on_notice=on_notice, on_progress=on_progress
        )

    notices: list[str] = []

    def notify(text: str) -> None:
        """收集自动补救说明，并同步透传给调用方（页面/批量调度）。"""
        notices.append(text)
        if on_notice:
            on_notice(text)

    chapter = store.load_chapter(chapter_id)
    before_words = 0
    if mode == "rewrite":
        # 用户主动选择「重写」：记录重写前字数，写入结果作为可读说明
        before_words = int(chapter.get("word_count") or 0) or _count_words(chapter.get("content") or "")
        regen = True
    if (chapter.get("content") or "").strip() and not regen and not dry_run:
        raise ValueError(f"章节 {chapter_id} 已有正文；确认重写请加 --regen")

    # 节拍先于角色同步存在的历史章节：正文生成前补一次 cast-only 识别（每章一次性）。
    # 失败不阻断正文，但必须把原因透传给页面/任务通知，不能静默。
    if not dry_run:
        from .cast_capture import ensure_chapter_cast

        try:
            ensure_chapter_cast(store, config, chapter_id, on_notice=notify, on_progress=on_progress)
        except Exception as exc:  # noqa: BLE001 - 角色识别失败不影响正文生成，但要透传原因
            notify(f"角色识别补跑失败：{getattr(exc, 'message', None) or exc}；本章继续按现有节拍绑定生成")
            logger.warning("角色识别补跑失败：%s", exc)

    context = build_chapter_context(store, config, chapter)
    bible = context["bible"]
    beats = context["beats"] or []
    chapter_words = context["chapter_words"]
    entry = context["entry"]
    style = context["style"]
    total_beats = len(beats)

    # 多轮参数：显式参数优先，其次读配置，最后用默认值兜底
    mt = config.get("writing.multi_turn") or {}
    enabled = bool(mt.get("enabled", True))
    rounds_cap = max(1, int(max_rounds or mt.get("max_rounds") or 8))
    target_words = int(context["metadata"].get("target_words") or chapter_words or 0)
    plan = length_plan(target_words)
    logger.info(
        "章节长度档位：%s；目标区间 %s–%s 字；%s–%s 段；每段 %s–%s 字（目标 %s 字）",
        plan["tier_label"],
        plan["low"],
        plan["high"],
        plan["min_paragraphs"],
        plan["max_paragraphs"],
        plan["paragraph_low"],
        plan["paragraph_high"],
        target_words,
    )

    # 每轮节拍数：显式指定优先；否则按节拍总数与保险轮数自动分配
    if beats_per_round is not None and int(beats_per_round) > 0:
        per_round = min(total_beats, max(1, int(beats_per_round)))
    else:
        per_round = max(1, math.ceil(total_beats / rounds_cap)) if total_beats else 1
    groups = [beats[start : start + per_round] for start in range(0, total_beats, per_round)] or [[]]
    use_multi = (enabled if multi_turn is None else bool(multi_turn)) and len(groups) > 1
    # 输出侧不设人为上限：请求里的 max_tokens 由适配器按上下文剩余空间计算
    # 摘要并入末轮不额外抬高输出上限：末轮与普通轮用同一口径，
    # 服务端需要上限字段时由适配器按上下文剩余可用空间收敛（不做人为截断）。
    # 摘要并入末轮时，旧全书摘要必须在生成开始时就固定下来：
    # 断点续写要恢复同一份输入，保证续写结果与一次跑完语义一致。
    book_summary_before = str(store.load_rolling_summary().get("book_summary") or "").strip()
    summary_contract = final_round_summary_instruction(book_summary_before)

    # 断点：每写完一轮落盘；取消 / 失败 / 服务重启后自动从这里接上。
    # 预览（dry_run）不写也不消费断点；显式重新生成（regen）直接作废旧断点。
    checkpoint_signature = {
        "base_revision": int(chapter.get("revision") or 1),
        "chapter_words": int(chapter_words),
        "total_beats": total_beats,
        "per_round": per_round,
        "beats": [
            {
                "summary": str(beat.get("summary") or ""),
                "target_words": int(beat.get("target_words") or 0),
                "character_ids": [str(cid) for cid in (beat.get("character_ids") or []) if str(cid).strip()],
            }
            for beat in beats
        ],
    }
    resume_state = None
    start_round = 0
    start_content = ""
    if dry_run:
        pass
    elif regen:
        chapter_checkpoint.clear(store, chapter_id)
    elif use_multi:
        state = chapter_checkpoint.load(store, chapter_id)
        if state is not None:
            reason = chapter_checkpoint.mismatch_reason(state, checkpoint_signature)
            checkpoint_file = str(chapter_checkpoint.checkpoint_path(store, chapter_id))
            if reason:
                chapter_checkpoint.clear(store, chapter_id)
                notify(f"上次的章节生成断点已作废（{reason}），本次从头生成")
            else:
                resume_state = state
                start_round = min(int(state.get("rounds_done") or 0), len(groups))
                start_content = str(state.get("content") or "")
                if start_round >= len(groups):
                    notify(
                        f"断点续写：检测到本章 {len(groups)} 轮已全部写完，"
                        f"本次只补落盘与摘要、不再请求模型（断点文件 {checkpoint_file}）"
                    )
                else:
                    notify(
                        f"断点续写：沿用前 {start_round} 轮已写正文，从第 {start_round + 1} 轮继续"
                        f"（断点文件 {checkpoint_file}）"
                    )

    summary_text = (entry or {}).get("summary") or chapter.get("outline") or ""
    pov_value = (context.get("pov_mode") or (style.get("pov") or "").strip() or "third_limited").strip()
    pov_label = POV_LABELS.get(pov_value, pov_value)
    pov_id = context.get("pov_character_id")
    name_by_id = {item.get("id"): item.get("name") for item in context["characters"] if item.get("id")}
    # POV 缺失时不允许静默套用默认值：页面必须先让作者指定视角人物或显式选择全知视角
    if not pov_id and pov_value != "third_omniscient":
        raise AdapterError(
            ErrorCode.INVALID_INPUT,
            "本章还没有指定视角人物：请先选择本章视角人物，或显式切换为全知视角后再生成",
            detail={"code": "BINDING_POV_MISSING", "chapter": chapter_id},
        )
    if pov_value == "third_omniscient":
        pov_line = f"视角：全知视角（{pov_label}，不绑定单一角色）"
    else:
        pov_line = (
            f"视角人物：{name_by_id.get(pov_id) or '已删除角色'}（{pov_label}，"
            "只写该人物能感知到的内容，其他人物的内心活动不得越过视角）"
        )
    tone_label = style.get("tone") or "沿用既有基调"
    instruction_lines = [
        f"写《{chapter.get('title')}》本章正文。{length_instruction(chapter_words)}",
        f"本章摘要：{summary_text or '按设定推进主线'}",
        pov_line,
        "按以下节拍顺序推进：",
    ]
    for order, beat in enumerate(beats, start=1):
        try:
            target = int(beat.get("target_words") or 0)
        except (TypeError, ValueError):
            target = 0
        turning = "是" if beat.get("turning_point") else "否"
        names = [name_by_id.get(str(cid)) or "已删除角色" for cid in (beat.get("character_ids") or []) if str(cid).strip()]
        present = "、".join(names) if names else "未指定"
        instruction_lines.append(f"{order}. {beat.get('summary')}（目标 {target} 字，转折点：{turning}，出场：{present}）")
    instruction_lines.append(f"基调：{tone_label}。")
    instruction_lines.append(
        "人物必须按角色卡的性格、背景、说话方式和关系行动，像真人在这个世界里做选择；"
        "每个角色的语速、用词、口头禅与称呼习惯都要体现出来，不同角色的对白必须能分辨出是谁在说。"
    )
    instruction_lines.append(
        "输出格式：只输出正文，不要写章节标题或小标题，不要解释。"
        "硬性排版规则：人物对白一律用中文全角双引号“”包裹；禁止使用「」『』【】、英文双引号\"\"与单引号，"
        "禁止不加引号直接成段写对白；旁白、心理描写不加引号，与对白区分清楚。"
    )
    if use_multi:
        first_end = len(groups[0])
        instruction_lines.append(
            f"本轮只写第 1–{first_end} 个节拍（全章共 {total_beats} 个）：写到第 {first_end} 个节拍结束就停，"
            "不要提前写后续节拍。"
        )
        instruction_lines.append(
            f"全章{length_instruction(chapter_words)}"
            f"本轮参考约 {_group_words(groups[0], total_beats, chapter_words)} 字；"
            f"本章目标 {plan['low']}–{plan['high']} 字；当前已写 0 字，剩余约 {target_words} 字；"
            "节拍写完即收尾，不要为凑字数续写。"
        )
    else:
        instruction_lines.append(
            f"一次写完全部 {total_beats} 个节拍，写完最后一个节拍即收尾。"
            f"{length_instruction(chapter_words)}"
            f"本章目标 {plan['low']}–{plan['high']} 字；当前已写 0 字，剩余约 {target_words} 字；"
            "接近目标且写到句末即可收尾。"
        )
        # 单轮模式本身就是「末轮」：同样把本章摘要并入这次请求
        instruction_lines.append(summary_contract)
    instruction = "\n".join(instruction_lines)

    assembler = ContextAssembler(
        work=context["work"],
        bible=bible,
        world_digest=context["world_context"],
        characters=context["characters"],
        chapter=chapter,
        outline_chapter=entry or {},
        previous_chapters=context["previous_chapters"],
        current_content=chapter.get("content") or "",
        budget_tokens=context_budget_tokens(config),
        book_digest=context["book_digest"],
        pov_character_id=context.get("pov_character_id"),
        planned_counts=context.get("planned_counts"),
        pov_mode=context.get("pov_mode"),
    )
    req = assembler.build("chapter", instruction, context["speaker_ids"])
    # metadata 保持原有字段不变：离线适配器依赖它，且产物需要确定性一致
    # 章节目标字数同时作为看门狗的基准（单轮即整章；多轮由每轮指令覆盖）
    req.metadata = {
        **context["metadata"],
        "watchdog_target_words": target_words,
        "with_summary": not use_multi,
        "thinking_stage": "chapter_round" if use_multi else "chapter_single",
    }

    summary_payload: dict | None = None
    summary_parse_error: str | None = None
    body_requests = 0
    fallback_requests = 0

    def save_round_checkpoint(state: dict) -> None:
        """把本轮结束后的草稿与轮次指针落盘（断点续写的唯一数据源）。

        参数:
            state: `_generate_chapter_rounds` 每轮结束后给出的快照
                `{content, rounds, beats_done, provider, summary, summary_error}`。

        返回:
            无。
        """
        chapter_checkpoint.save(
            store,
            {
                "chapter_id": chapter_id,
                "signature": checkpoint_signature,
                "base_revision": checkpoint_signature["base_revision"],
                "chapter_words": chapter_words,
                "total_beats": total_beats,
                "per_round": per_round,
                "book_summary_before": book_summary_before,
                "groups": [
                    [str(beat.get("id") or position) for position, beat in enumerate(group)]
                    for group in groups
                ],
                "content": state["content"],
                "rounds_done": state["rounds"],
                "beats_done": state["beats_done"],
                "provider": state.get("provider") or "",
                "status": "running",
                "summary": state.get("summary"),
                "summary_error": state.get("summary_error"),
            },
        )

    if use_multi:
        # 首轮复用装配结果；前缀缓存键固定为 工程 id + 章节 id
        work_id = str(context["work"].get("id") or "").strip()
        if work_id:
            req.prompt_cache_key = f"{work_id}:{chapter_id}"
        try:
            outcome = _generate_chapter_rounds(
                context["adapter"], req, groups, total_beats, chapter_words, rounds_cap,
                on_delta=on_delta, name_by_id=name_by_id, on_notice=notify,
                on_progress=on_progress,
                summary_contract=summary_contract,
                start_round=start_round,
                start_content=start_content,
                start_summary=(resume_state or {}).get("summary"),
                start_summary_error=(resume_state or {}).get("summary_error"),
                # 预览不写断点：避免下一次正式生成误把预览当成已完成轮次
                on_round=None if dry_run else save_round_checkpoint,
            )
        except GenerationCanceled as exc:
            # 取消：已完成轮次保留在断点里，下次生成本章自动接上（预览不参与）
            if not dry_run:
                chapter_checkpoint.mark(store, chapter_id, "canceled", "用户已停止生成")
            state = None if dry_run else chapter_checkpoint.load(store, chapter_id)
            if state is not None:
                exc.resume = chapter_checkpoint.resume_info(
                    state, str(chapter_checkpoint.checkpoint_path(store, chapter_id))
                )
            raise
        except AdapterError as exc:
            if not dry_run:
                chapter_checkpoint.mark(store, chapter_id, "failed", exc.message)
            raise
        content = outcome["content"].strip()
        provider = outcome["provider"]
        rounds = outcome["rounds"]
        beats_done = outcome["beats_done"]
        stop_reason = outcome["stop_reason"]
        summary_payload = outcome.get("summary")
        summary_parse_error = outcome.get("summary_error")
        body_requests = int(outcome.get("requests") or rounds)
    else:
        # 不分轮时整章一次写完：正文 + 本章摘要共用这一次请求，不设置人为输出预算
        result, content, summary_payload, summary_parse_error, body_requests = _generate_final_round(
            context["adapter"], req, on_delta, "章节正文", on_notice=notify
        )
        provider = result.model
        rounds = 1
        beats_done = total_beats
        stop_reason = "single_round"
    if not content:
        # 正文不可用时断点没有意义：清掉，避免下次续写直接撞上同样的空结果
        if not dry_run:
            chapter_checkpoint.clear(store, chapter_id)
        raise AdapterError(ErrorCode.UPSTREAM_ERROR, "写作适配器返回了空正文", retryable=True)

    # 引号收口：只改引号字符，疑似漏引号的对白只记提示
    content, dialogue_warnings = normalize_dialogue(
        content, [str(item.get("name") or "") for item in context["characters"]]
    )

    words = _count_words(content)
    word_deviation = _word_deviation(words, target_words)
    beats_remaining = max(0, total_beats - beats_done)
    resume_payload = (
        chapter_checkpoint.resume_info(
            resume_state, str(chapter_checkpoint.checkpoint_path(store, chapter_id))
        )
        if resume_state
        else {"resumed": False}
    )
    # 用户点了「重写」才走到这里：把原因与前后字数写进结果，供页面直接展示
    overshoot_note = ""
    if mode == "rewrite":
        before_ratio = round(before_words / target_words, 2) if target_words else None
        ratio_text = f"{before_ratio} 倍" if before_ratio is not None else "倍率未知"
        overshoot_note = (
            f"已按「重写」处理：原 {before_words} 字（目标 {target_words} 字，{ratio_text}，"
            f"达到 {OVERSHOOT_ACTION_RATIO} 倍动作阈值）→ 重写后 {words} 字"
        )

    if dry_run:
        return {
            "chapter_id": chapter_id,
            "status": chapter.get("status"),
            "words": words,
            "word_count": words,
            "provider": provider,
            "content": content,
            "rounds": rounds,
            "beats_total": total_beats,
            "beats_done": beats_done,
            "beats_remaining": beats_remaining,
            "multi_turn": use_multi,
            "stop_reason": stop_reason,
            "word_deviation": word_deviation,
            "dialogue_warnings": dialogue_warnings,
            "notices": notices,
            "mode": mode,
            "overshoot_note": overshoot_note,
            "summary": None,
            "summary_error": None,
            "summary_mode": "preview",
            "resume": resume_payload,
            "revision": chapter.get("revision"),
        }

    # 落盘前最后一道校验：确认是小说正文，复述任务/准则清单一律不写盘
    ok, reason = check_chapter_content(content)
    if not ok:
        # 正文内容本身不合格：重试不会改变结果，断点作废后重新生成
        chapter_checkpoint.clear(store, chapter_id)
        raise AdapterError(
            ErrorCode.UPSTREAM_ERROR,
            f"章节正文落盘前校验未通过（{reason}）：未写入章节，请重试生成",
            retryable=True,
            detail={"reason": reason, "head": content[:300]},
        )

    chapter["content"] = content + "\n"
    chapter["word_count"] = words
    chapter["status"] = "written"
    bump_revision(chapter)
    chapter["updated_at"] = now_iso()
    store.save_chapter(chapter)

    # 摘要：末轮已产出且校验通过就直接落盘；解析失败才回退到单独摘要请求，
    # 回退原因写给用户看，不静默降级。
    summary_info = None
    summary_error = None
    summary_mode = "inline"
    if summary_payload:
        try:
            summary_info = save_summary(
                store,
                chapter,
                summary_payload["chapter_summary"],
                summary_payload["book_summary"],
                provider=provider,
            )
            summary_info["mode"] = "inline"
        except AdapterError as exc:
            summary_mode = "failed"
            logger.warning("末轮摘要落盘失败：%s", exc.message)
            summary_error = exc.message
    else:
        reason_text = summary_parse_error or "末轮没有返回本章摘要"
        notify(f"本章摘要未能随正文末轮产出（{reason_text}），已回退到单独摘要请求")
        fallback_requests = 1
        try:
            from .recap import update_rolling_summary

            summary_info = update_rolling_summary(store, config, chapter, adapter=context["adapter"])
            summary_info["mode"] = "fallback"
            summary_mode = "fallback"
        except AdapterError as exc:
            logger.warning("章节摘要更新失败：%s", exc.message)
            summary_error = exc.message
            summary_mode = "failed"
        except Exception as exc:  # noqa: BLE001 - 摘要失败不影响正文，但必须明确告知
            logger.warning("章节摘要更新失败：%s", exc)
            summary_error = str(exc)
            summary_mode = "failed"

    # 正文与摘要都已处理完，断点没有继续保留的意义
    chapter_checkpoint.clear(store, chapter_id)

    return {
        "chapter_id": chapter_id,
        "status": chapter["status"],
        "words": words,
        "word_count": words,
        "provider": provider,
        "content": None,
        "rounds": rounds,
        "beats_total": total_beats,
        "beats_done": beats_done,
        "beats_remaining": beats_remaining,
        "multi_turn": use_multi,
        "stop_reason": stop_reason,
        "word_deviation": word_deviation,
        "dialogue_warnings": dialogue_warnings,
        "notices": notices,
        "summary": summary_info,
        "summary_error": summary_error,
        "summary_mode": summary_mode,
        "resume": resume_payload,
        "mode": mode,
        "overshoot_note": overshoot_note,
        "requests": {
            "body": body_requests,
            "summary_fallback": fallback_requests,
            "total": body_requests + fallback_requests,
        },
        "revision": chapter["revision"],
    }


def _split_beat_sections(text: str, total_beats: int) -> tuple[str, str | None]:
    """按「【节拍 N】」切分压缩结果，确认 1..N 全部存在且非空。

    这是「不能靠删节拍压缩」的结构化兜底：段数、顺序与非空都校验，
    通过后才拼回正文；正文里不保留分段标记。

    参数:
        text: 压缩模型原始输出。
        total_beats: 本章节拍总数；0 时不做分段要求。

    返回:
        `(body, error)`：body 为去掉标记后的正文；error 为中文原因（成功为 None）。
    """
    raw = (text or "").strip()
    if total_beats <= 0:
        return raw, None
    matches = list(re.finditer(r"(?m)^\s*【\s*节拍\s*(\d+)\s*】\s*", raw))
    if not matches:
        return "", "压缩结果缺少「【节拍 N】」分段标记"
    sections: dict[int, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(raw)
        segment = raw[match.end() : end].strip()
        if segment:
            sections[int(match.group(1))] = segment
    missing = [str(number) for number in range(1, total_beats + 1) if number not in sections]
    if missing:
        return "", f"压缩结果缺少第 {'、'.join(missing)} 个节拍（不能靠删节拍压缩）"
    for number in sections:
        if number > total_beats:
            return "", f"压缩结果出现多余的【节拍 {number}】（本章共 {total_beats} 个节拍）"
    return "\n\n".join(sections[number] for number in sorted(sections)), None


def _compress_prompt(chapter: dict, content: str, beats: list[dict], target_words: int) -> str:
    """构造「按目标字数压缩」的提示词：逐节拍分段，保住全部要点。

    参数:
        chapter: 章节 dict（用于标题）。
        content: 现有正文。
        beats: 本章节拍列表。
        target_words: 目标字数。

    返回:
        中文提示词。
    """
    beat_lines = []
    for index, beat in enumerate(beats):
        beat_words = int(beat.get("target_words") or 0)
        suffix = f"（目标约 {beat_words} 字）" if beat_words else ""
        beat_lines.append(f"第 {index + 1} 个：{str(beat.get('summary') or '').strip() or '（无摘要）'}{suffix}")
    return "\n".join(
        [
            f"下面是《{chapter.get('title') or ''}》已写好的正文，明显超出目标字数，需要压缩。",
            f"目标字数：{target_words} 字；当前字数：{_count_words(content)} 字。",
            "",
            "本章节拍（必须全部保留，不得删除、合并或跳过；每个节拍的核心事件、人物关系、转折与结果都不能丢）：",
            *beat_lines,
            "",
            "要求：",
            "1. 按【节拍 1】【节拍 2】…逐段输出压缩后的正文，段数和顺序与上面的节拍一一对应；",
            "2. 只压缩描写、铺陈、重复表达与冗余过渡，不得删除任何节拍，也不得把两个节拍并成一段；",
            "3. 不新增情节、不改变人物关系与结局，不输出解释、字数统计或除分段标记外的任何标题；",
            "4. 尽量压缩到目标字数附近（允许小幅浮动），保持原有人称、时态与叙事口吻。",
            "",
            "【原文】",
            content,
        ]
    )


def compress_chapter(
    store: ProjectStore,
    config: AppConfig,
    chapter_id: str,
    on_delta=None,
    on_notice=None,
    on_progress=None,
) -> dict:
    """把已有章节正文按目标字数压缩，逐节拍分段校验，保住全部节拍要点。

    只在用户点击「按目标字数压缩」时调用，不自动触发。压缩结果必须按
    「【节拍 N】」逐段覆盖 1..N，缺段或多段都拒绝写入并报中文原因；
    成功后写入新版本（revision +1），保留原摘要不重新生成。

    参数:
        store: 工程。
        config: 本机配置。
        chapter_id: 章节 ID。
        on_delta: 流式增量回调；传入时改走流式。
        on_notice: 自动补救说明回调（格式重试成功时上报）。
        on_progress: 进度回调；上报 `{type, round, rounds_total, chars}` 供页面显示。

    返回:
        与 `generate_chapter` 同形的结果 dict，另含 `before_words`、
        `overshoot_note`，`mode="compress"`。

    异常:
        ValueError: 本章还没有正文。
        AdapterError: 压缩结果为空、缺少节拍或不是小说正文。
    """
    chapter = store.load_chapter(chapter_id)
    content = (chapter.get("content") or "").strip()
    if not content:
        raise ValueError(f"章节 {chapter_id} 还没有正文，无法压缩；请先生成正文")

    context = build_chapter_context(store, config, chapter)
    beats = context["beats"] or []
    target_words = int(context["metadata"].get("target_words") or context["chapter_words"] or 0)
    before_words = int(chapter.get("word_count") or 0) or _count_words(content)
    notices: list[str] = []
    if on_progress:
        on_progress({"type": "round", "round": 1, "rounds_total": 1, "beats_done": 0, "chars": 0})

    def notify(text: str) -> None:
        """收集压缩过程的自动补救说明。"""
        notices.append(text)
        if on_notice:
            on_notice(text)

    request = TextGenRequest(
        task="chapter_compress",
        system="你是资深小说编辑，只在保留全部节拍要点的前提下压缩正文。只输出压缩后的正文。",
        messages=[Message("user", _compress_prompt(chapter, content, beats, target_words))],
        stream=on_delta is not None,
        temperature=resolve_temperature(config, "writing"),
        metadata={
            "chapter_id": chapter_id,
            "index": chapter.get("index"),
            "title": chapter.get("title"),
            "target_words": target_words,
            "watchdog_target_words": target_words,
            "with_summary": False,
            "before_words": before_words,
            "thinking_stage": "compress",
            "beats": [
                {"summary": str(beat.get("summary") or ""), "target_words": int(beat.get("target_words") or 0)}
                for beat in beats
            ],
            "content": content,
        },
    )

    def splitter(text: str) -> tuple[str, dict | None, str | None]:
        """按节拍分段拆出正文；缺段时返回中文原因。"""
        body, error = _split_beat_sections(text, len(beats))
        return body, None, error

    counter = {"calls": 0}
    body, cover_error = "", None
    result = None
    for attempt in range(2):
        result, body, _payload, cover_error = _generate_validated(
            context["adapter"],
            request,
            on_delta,
            "压缩正文",
            on_notice=notify,
            counter=counter,
            extract=splitter,
            target_chars=target_words,
            signals=("suffix",),
        )
        if not cover_error:
            break
        logger.warning("压缩结果未保住全部节拍：%s；追加约束后重试一次", cover_error)
        if attempt == 0:
            request = replace(
                request,
                messages=list(request.messages)
                + [
                    Message(
                        "user",
                        f"上一次输出不合格（{cover_error}）。请严格按【节拍 1】…【节拍 {max(1, len(beats))}】"
                        "逐段输出，每个节拍都要有正文，不要合并或省略。",
                    )
                ],
            )
    if cover_error:
        raise AdapterError(
            ErrorCode.UPSTREAM_ERROR,
            f"压缩结果未保住全部节拍（{cover_error}）：未写入章节，请重试或改用「重写」",
            retryable=True,
        )

    body = (body or "").strip()
    if not body:
        raise AdapterError(ErrorCode.UPSTREAM_ERROR, "压缩模型返回了空正文", retryable=True)
    body, dialogue_warnings = normalize_dialogue(
        body, [str(item.get("name") or "") for item in context["characters"]]
    )
    words = _count_words(body)
    word_deviation = _word_deviation(words, target_words)
    before_ratio = round(before_words / target_words, 2) if target_words else None
    ratio_text = f"{before_ratio} 倍" if before_ratio is not None else "倍率未知"
    overshoot_note = (
        f"已按「按目标字数压缩」处理：原 {before_words} 字（目标 {target_words} 字，{ratio_text}，"
        f"达到 {OVERSHOOT_ACTION_RATIO} 倍动作阈值）→ 压缩后 {words} 字；"
        f"本章 {len(beats)} 个节拍全部保留"
    )

    chapter["content"] = body + "\n"
    chapter["word_count"] = words
    chapter["status"] = "written"
    bump_revision(chapter)
    chapter["updated_at"] = now_iso()
    store.save_chapter(chapter)
    # 压缩替换了正文：旧的生成断点已失去上下文，直接作废，避免下次续写覆盖压缩结果
    chapter_checkpoint.clear(store, chapter_id)

    return {
        "chapter_id": chapter_id,
        "status": chapter["status"],
        "words": words,
        "word_count": words,
        "provider": result.model if result is not None else "",
        "content": None,
        "rounds": 0,
        "beats_total": len(beats),
        "beats_done": len(beats),
        "beats_remaining": 0,
        "multi_turn": False,
        "stop_reason": "compressed",
        "word_deviation": word_deviation,
        "dialogue_warnings": dialogue_warnings,
        "notices": notices,
        "summary": None,
        "summary_error": None,
        "summary_mode": None,
        "resume": {"resumed": False},
        "mode": "compress",
        "overshoot_note": overshoot_note,
        "before_words": before_words,
        "requests": {"body": int(counter["calls"]), "summary_fallback": 0, "total": int(counter["calls"])},
        "revision": chapter["revision"],
    }


def _count_words(text: str) -> int:
    """统计正文有效字数（去掉空白后的字符数）。

    参数:
        text: 正文文本。

    返回:
        字数；None 视为 0。
    """
    return count_words(text)


def _beat_key_tokens(summary: str) -> list[str]:
    """把节拍摘要拆成用于覆盖判定的关键词（中文按 2 字滑窗，英文/数字整段）。

    参数:
        summary: 节拍摘要文本。

    返回:
        去重后的关键词列表；摘要为空时返回空列表。
    """
    chunks = re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9]{2,}", summary or "")
    tokens: list[str] = []
    for chunk in chunks:
        if len(chunk) <= 3:
            tokens.append(chunk)
        else:
            tokens.extend(chunk[index : index + 2] for index in range(len(chunk) - 1))
    return list(dict.fromkeys(tokens))


def _uncovered_beats(content: str, beats: list[dict]) -> list[tuple[int, dict]]:
    """找出正文里尚未覆盖的节拍要点。

    判定口径：节拍摘要关键词在正文里的命中率低于 `SUPPLEMENT_COVERAGE_RATIO`
    即视为没写到。这是为了决定「补写哪几个要点」，不是字数判断。

    参数:
        content: 现有正文。
        beats: 本章节拍列表。

    返回:
        `[(节拍序号从 1 开始, 节拍 dict)]`，按原顺序；全部覆盖时为空列表。
    """
    text = content or ""
    rows: list[tuple[int, dict]] = []
    for index, beat in enumerate(beats or [], start=1):
        tokens = _beat_key_tokens(str(beat.get("summary") or ""))
        if not tokens:
            continue
        hit = sum(1 for token in tokens if token in text)
        if hit / len(tokens) < SUPPLEMENT_COVERAGE_RATIO:
            rows.append((index, beat))
    return rows


def _split_supplement_sections(text: str, expected: list[int]) -> tuple[str, str | None]:
    """按「【补写节拍 N】」切分补写结果，校验要求的要点全部存在。

    参数:
        text: 补写模型原始输出。
        expected: 本次要求补写的节拍序号列表；为空时不做分段校验。

    返回:
        `(body, error)`：body 为去掉标记后的新增正文；error 为中文原因（成功为 None）。
    """
    raw = (text or "").strip()
    if not expected:
        return raw, None
    matches = list(re.finditer(r"(?m)^\s*【\s*补写节拍\s*(\d+)\s*】\s*", raw))
    if not matches:
        return "", "补写结果缺少「【补写节拍 N】」分段标记"
    sections: dict[int, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(raw)
        segment = raw[match.end() : end].strip()
        if segment:
            sections[int(match.group(1))] = segment
    missing = [str(number) for number in expected if number not in sections]
    if missing:
        return "", f"补写结果缺少第 {'、'.join(missing)} 个节拍要点"
    extra = sorted(number for number in sections if number not in expected)
    if extra:
        return "", f"补写结果出现未要求的【补写节拍 {'、'.join(str(number) for number in extra)}】"
    return "\n\n".join(sections[number] for number in sorted(sections)), None


def _supplement_prompt(
    chapter: dict,
    content: str,
    target_words: int,
    gap_words: int,
    uncovered: list[tuple[int, dict]],
    tail_chars: int = 1200,
) -> str:
    """构造「补写」提示词：按缺口字数与未覆盖要点续写，只输出新增正文。

    参数:
        chapter: 章节 dict（用于标题）。
        content: 现有正文。
        target_words: 本章目标字数。
        gap_words: 缺口字数（目标 − 现有）。
        uncovered: `_uncovered_beats` 的结果。
        tail_chars: 附上的正文结尾字数（让模型接得上）。

    返回:
        中文提示词。
    """
    actual = _count_words(content)
    lines = [
        f"《{chapter.get('title') or ''}》已写好的正文只有 {actual} 字，比目标 {target_words} 字少约 {gap_words} 字，需要补写。",
        "要求：",
        f"1. 只输出新增正文，紧接已有正文的结尾继续写，约 {gap_words} 字；不要复述已有内容，不要重写已经写过的情节。",
        "2. 禁止复述同一句对白、同一句式或同一段落结构；每次对话都要推进新信息，禁止原地来回的重复对白。",
    ]
    if uncovered:
        lines.append("3. 按下列要点逐段补写，每段都要有正文，不得省略、合并或跳过：")
        for number, beat in uncovered:
            summary = str(beat.get("summary") or "").strip() or "（无摘要）"
            lines.append(f"【补写节拍 {number}】：{summary}")
        lines.append(
            "输出格式：每段以「【补写节拍 N】」开头，段与段之间空一行；"
            "除这些标记外不要写标题、不要解释、不要输出字数统计。"
        )
    else:
        lines.append("3. 本章节拍要点已覆盖，请顺着上文继续推进情节、补足篇幅，同样不要复述已有内容。")
        lines.append("输出格式：只输出新增正文，不要写标题、不要解释、不要输出字数统计。")
    lines.extend(["", "【已有正文结尾】", content[-tail_chars:] if tail_chars > 0 else content])
    return "\n".join(lines)


def supplement_chapter(
    store: ProjectStore,
    config: AppConfig,
    chapter_id: str,
    on_delta=None,
    on_notice=None,
    on_progress=None,
) -> dict:
    """按缺口字数与未覆盖要点给已有正文补写，并合并为新版本。

    只在用户点击「补写」时调用，不自动触发。补写只追加、不改写已有正文；
    有未覆盖要点时要求模型按「【补写节拍 N】」逐段输出，缺段即拒绝写入。

    参数:
        store: 工程。
        config: 本机配置。
        chapter_id: 章节 ID。
        on_delta: 流式增量回调；传入时改走流式。
        on_notice: 自动补救说明回调（重试成功时上报）。
        on_progress: 进度回调；上报 `{type, round, rounds_total, chars}` 供页面显示。

    返回:
        与 `generate_chapter` 同形的结果 dict，另含 `before_words`、`added_words`、
        `gap_words`、`supplemented_beats`、`overshoot_note`，`mode="supplement"`。

    异常:
        ValueError: 本章还没有正文，或当前已不低于补写阈值。
        AdapterError: 补写结果为空、缺少要求的要点或没有新增正文。
    """
    chapter = store.load_chapter(chapter_id)
    content = (chapter.get("content") or "").strip()
    if not content:
        raise ValueError(f"章节 {chapter_id} 还没有正文，无法补写；请先生成正文")

    # 补写会新增台词，同样需要角色绑定；未同步时先补一次识别（每章一次性）
    try:
        from .cast_capture import ensure_chapter_cast

        ensure_chapter_cast(store, config, chapter_id, on_notice=on_notice, on_progress=on_progress)
    except Exception as exc:  # noqa: BLE001 - 角色识别失败不阻断补写，但记录原因
        logger.warning("补写前角色识别失败：%s", exc)

    context = build_chapter_context(store, config, chapter)
    beats = context["beats"] or []
    target_words = int(context["metadata"].get("target_words") or context["chapter_words"] or 0)
    before_words = int(chapter.get("word_count") or 0) or _count_words(content)
    gap_words = max(0, target_words - before_words)
    if target_words > 0 and before_words >= int(target_words * UNDERFILL_ACTION_RATIO):
        raise ValueError(
            f"当前正文 {before_words} 字，已达到目标 {target_words} 字的 {UNDERFILL_ACTION_RATIO} 倍补写阈值，无需补写"
        )
    uncovered = _uncovered_beats(content, beats)
    notices: list[str] = []
    if on_progress:
        on_progress({"type": "round", "round": 1, "rounds_total": 1, "beats_done": 0, "chars": 0})

    def notify(text: str) -> None:
        """收集补写过程的自动补救说明。"""
        notices.append(text)
        if on_notice:
            on_notice(text)

    request = TextGenRequest(
        task="chapter_supplement",
        system="你是资深小说作者，只在已有正文之后续写补足篇幅，不改写已写内容。只输出新增正文。",
        messages=[Message("user", _supplement_prompt(chapter, content, target_words, gap_words, uncovered))],
        stream=on_delta is not None,
        temperature=resolve_temperature(config, "writing"),
        metadata={
            "chapter_id": chapter_id,
            "index": chapter.get("index"),
            "title": chapter.get("title"),
            "target_words": target_words,
            "watchdog_target_words": max(gap_words, 200),
            "with_summary": False,
            "before_words": before_words,
            "gap_words": gap_words,
            "thinking_stage": "supplement",
            "uncovered_beats": [
                {"index": number, "summary": str(beat.get("summary") or "")} for number, beat in uncovered
            ],
            "beats": [
                {"summary": str(beat.get("summary") or ""), "target_words": int(beat.get("target_words") or 0)}
                for beat in beats
            ],
            "content": content,
        },
    )

    expected = [number for number, _beat in uncovered]

    def splitter(text: str) -> tuple[str, dict | None, str | None]:
        """按「【补写节拍 N】」拆出新增正文；缺段时返回中文原因。"""
        body, error = _split_supplement_sections(text, expected)
        return body, None, error

    counter = {"calls": 0}
    body, cover_error = "", None
    result = None
    for attempt in range(2):
        result, body, _payload, cover_error = _generate_validated(
            context["adapter"],
            request,
            on_delta,
            "补写正文",
            on_notice=notify,
            counter=counter,
            extract=splitter,
            target_chars=max(gap_words, 200),
        )
        if not cover_error:
            break
        logger.warning("补写结果缺少要点：%s；追加约束后重试一次", cover_error)
        if attempt == 0:
            marks = "、".join(f"【补写节拍 {number}】" for number in expected)
            request = replace(
                request,
                messages=list(request.messages)
                + [
                    Message(
                        "user",
                        f"上一次输出不合格（{cover_error}）。请严格按 {marks} 逐段输出新增正文，"
                        "每个要点都要有正文，不要合并或省略。",
                    )
                ],
            )
    if cover_error:
        raise AdapterError(
            ErrorCode.UPSTREAM_ERROR,
            f"补写结果未覆盖要求的节拍要点（{cover_error}）：未写入章节，请重试或改用「重写」",
            retryable=True,
        )

    body = (body or "").strip()
    if not body:
        raise AdapterError(ErrorCode.UPSTREAM_ERROR, "补写模型返回了空正文", retryable=True)
    body, dialogue_warnings = normalize_dialogue(
        body, [str(item.get("name") or "") for item in context["characters"]]
    )
    # 只追加不改写：裁掉与已有结尾重叠的部分，避免拼接处复述
    merged = _append_segment(content, body).strip()
    words = _count_words(merged)
    added_words = words - before_words
    if added_words <= 0:
        raise AdapterError(
            ErrorCode.UPSTREAM_ERROR,
            "补写没有产生新增内容（与已有正文重复）：未写入章节，请重试",
            retryable=True,
        )
    word_deviation = _word_deviation(words, target_words)
    before_ratio = round(before_words / target_words, 2) if target_words else None
    ratio_text = f"{before_ratio} 倍" if before_ratio is not None else "倍率未知"
    if expected:
        beats_text = "第 " + "、".join(str(number) for number in expected) + " 个节拍"
    else:
        beats_text = "本章节拍要点已覆盖，按缺口字数顺延续写"
    overshoot_note = (
        f"已按「补写」处理：原 {before_words} 字（目标 {target_words} 字，{ratio_text}，"
        f"低于 {UNDERFILL_ACTION_RATIO} 倍补写阈值）→ 补写后 {words} 字，新增 {added_words} 字；"
        f"补写要点：{beats_text}"
    )

    chapter["content"] = merged + "\n"
    chapter["word_count"] = words
    chapter["status"] = "written"
    bump_revision(chapter)
    chapter["updated_at"] = now_iso()
    store.save_chapter(chapter)
    # 正文已变化：旧的生成断点已失去上下文，直接作废
    chapter_checkpoint.clear(store, chapter_id)

    return {
        "chapter_id": chapter_id,
        "status": chapter["status"],
        "words": words,
        "word_count": words,
        "provider": result.model if result is not None else "",
        "content": None,
        "rounds": 0,
        "beats_total": len(beats),
        "beats_done": len(beats),
        "beats_remaining": 0,
        "multi_turn": False,
        "stop_reason": "supplemented",
        "word_deviation": word_deviation,
        "dialogue_warnings": dialogue_warnings,
        "notices": notices,
        "summary": None,
        "summary_error": None,
        "summary_mode": None,
        "resume": {"resumed": False},
        "mode": "supplement",
        "overshoot_note": overshoot_note,
        "before_words": before_words,
        "added_words": added_words,
        "gap_words": gap_words,
        "supplemented_beats": [
            {"index": number, "summary": str(beat.get("summary") or "")} for number, beat in uncovered
        ],
        "requests": {"body": int(counter["calls"]), "summary_fallback": 0, "total": int(counter["calls"])},
        "revision": chapter["revision"],
    }


def _append_segment(text: str, segment: str, max_overlap: int = 200) -> str:
    """把新增段落追加到正文末尾，并裁掉与结尾重复的重叠文本。

    参数:
        text: 已生成正文。
        segment: 本轮新增段落。
        max_overlap: 最多比较的重叠字数。

    返回:
        拼接后的正文；空段落返回原文，整段重复时不会重复追加。
    """
    base = (text or "").rstrip()
    piece = (segment or "").strip()
    if not piece:
        return text or ""
    if not base:
        return piece
    limit = min(max_overlap, len(base), len(piece))
    overlap = 0
    for size in range(limit, 0, -1):
        if base[-size:] == piece[:size]:
            overlap = size
            break
    if overlap:
        piece = piece[overlap:].lstrip()
    if not piece:
        return base
    return f"{base}\n\n{piece}"


def _continue_instruction(segment_words: int) -> str:
    """构造正文续写轮的用户短指令（供 `continuation.py` 使用）。

    参数:
        segment_words: 本段目标字数。

    返回:
        一句话续写指令。
    """
    return f"继续写下一段，紧接上文，不要重复已写内容，保持人物人设与文风。本段目标约 {segment_words} 字，只输出新增正文。"


def _group_words(group: list[dict], total_beats: int, chapter_words: int) -> int:
    """估算一组节拍的参考字数。

    参数:
        group: 本轮要写的节拍列表。
        total_beats: 本章节拍总数。
        chapter_words: 全章参考字数。

    返回:
        参考字数：优先用节拍目标字数之和，缺失时按节拍占比分摊全章字数。
    """
    total = 0
    for beat in group:
        try:
            total += int(beat.get("target_words") or 0)
        except (TypeError, ValueError):
            continue
    if total > 0:
        return total
    if total_beats <= 0:
        return max(1, chapter_words)
    return max(100, round(chapter_words * len(group) / total_beats))


def _beats_round_instruction(
    groups: list[list[dict]],
    index: int,
    total_beats: int,
    chapter_words: int,
    name_by_id: dict | None = None,
    written_chars: int = 0,
) -> str:
    """构造第 index 轮（0 起）的续写指令。

    参数:
        groups: 按轮切分好的节拍分组。
        index: 当前轮下标（0 起；首轮不走本函数）。
        total_beats: 本章节拍总数。
        chapter_words: 全章参考字数。
        name_by_id: 角色 ID → 姓名；用于写明本轮每个节拍的出场角色。
        written_chars: 已写正文字数；写进指令形成「已写 A，剩余 B」的进度口径。

    返回:
        一句话续写指令，写明本轮节拍范围、出场角色、篇幅区间与进度。
    """
    start = sum(len(group) for group in groups[:index])
    group = groups[index]
    end = start + len(group)
    names = name_by_id or {}

    def beat_line(offset: int, beat: dict) -> str:
        """渲染单条节拍：摘要 + 出场角色姓名（不输出内部 ID）。"""
        present = [
            names.get(str(cid)) or "已删除角色"
            for cid in (beat.get("character_ids") or [])
            if str(cid).strip()
        ]
        return f"第 {start + offset + 1} 个：{beat.get('summary') or ''}（出场：{'、'.join(present) if present else '未指定'}）"

    beat_lines = "；".join(beat_line(offset, beat) for offset, beat in enumerate(group))
    next_note = f"不要写第 {end + 1} 个及之后的节拍。" if end < total_beats else "本章节拍到此结束。"
    plan = length_plan(chapter_words)
    remaining = max(0, int(chapter_words or 0) - int(written_chars or 0))
    return (
        f"继续写第 {start + 1}–{end} 个节拍（全章共 {total_beats} 个），紧接上文，不要重复已写内容。"
        f"本轮节拍：{beat_lines}。只让这些出场角色行动，未出场的角色不得突然出现；"
        f"写完停在第 {end} 个节拍边界，{next_note}"
        f"{length_instruction(_group_words(group, total_beats, chapter_words))}"
        f"本章目标 {plan['low']}–{plan['high']} 字；当前已写 {int(written_chars or 0)} 字，剩余约 {remaining} 字；"
        "接近目标且写到句末即可收尾，不要为凑字数续写。只输出新增正文。"
    )


def _word_deviation(actual: int, target: int) -> dict:
    """计算实际字数与目标字数的偏差，按超纲阈值给出中文说明与可选动作。

    口径：生成过程不限制长度，写完后按目标字数判偏差；
    - < 0.7 倍：明显不足，提供「补写」动作（按缺口与未覆盖要点续写并合并）；
    - 0.7–1.5 倍：只在结果里标实际 / 目标字数；
    - > 1.5 倍且 < 2 倍：提示超纲，可自行精简，不提供动作；
    - ≥ 2 倍：视为明显超纲，返回「按目标字数压缩」「重写」两个动作（需用户点击才执行）。

    参数:
        actual: 实际字数。
        target: 目标字数（节拍目标之和）。

    返回:
        {"target", "actual", "ratio", "level", "notice_ratio", "action_ratio",
         "under_ratio", "message", "actions"}；目标未知时 ratio 为 None、level 为 none。
    """
    actions = [dict(item) for item in OVERSHOOT_ACTIONS]
    base = {
        "target": max(0, int(target or 0)),
        "actual": max(0, int(actual or 0)),
        "level": "none",
        "notice_ratio": OVERSHOOT_NOTICE_RATIO,
        "action_ratio": OVERSHOOT_ACTION_RATIO,
        "under_ratio": UNDERFILL_ACTION_RATIO,
        "actions": [],
    }
    if target <= 0:
        return {**base, "ratio": None, "message": ""}
    ratio_value = actual / target
    ratio = round(ratio_value, 2)
    if ratio_value < UNDERFILL_ACTION_RATIO:
        message = (
            f"实际 {actual} 字，目标 {target} 字（{ratio} 倍）：明显不足。"
            f"当前阈值低于 {UNDERFILL_ACTION_RATIO} 倍可补写；补写会按缺口字数与尚未覆盖的情节要点续写并合并，"
            "需要你点击后才执行"
        )
        base["level"] = "action"
        base["actions"] = [dict(item) for item in UNDERFILL_ACTIONS]
    elif ratio_value < 0.8:
        message = f"实际 {actual} 字，比目标 {target} 字少 {int(round((1 - ratio_value) * 100))}%；节拍已写完，可按需续写补足"
    elif ratio_value >= OVERSHOOT_ACTION_RATIO:
        message = (
            f"实际 {actual} 字，目标 {target} 字（{ratio} 倍）：明显超纲。"
            f"当前阈值 {OVERSHOOT_NOTICE_RATIO} 倍仅提示、{OVERSHOOT_ACTION_RATIO} 倍及以上可压缩或重写；"
            "两个动作都需要你点击后才执行"
        )
        base["level"] = "action"
        base["actions"] = actions
    elif ratio_value > OVERSHOOT_NOTICE_RATIO:
        message = (
            f"实际 {actual} 字，目标 {target} 字（{ratio} 倍）：已超纲，可考虑精简；"
            f"达到 {OVERSHOOT_ACTION_RATIO} 倍时可选择压缩或重写"
        )
        base["level"] = "notice"
    else:
        message = f"实际 {actual} 字，目标 {target} 字（{ratio} 倍）：在 {OVERSHOOT_NOTICE_RATIO} 倍以内"
    return {**base, "ratio": ratio, "message": message}


def _generate_validated(
    adapter,
    request: TextGenRequest,
    on_delta,
    label: str,
    on_notice=None,
    counter: dict | None = None,
    extract=None,
    retry_suffix: str = "",
    target_chars: int = 0,
    signals: tuple[str, ...] | None = None,
) -> tuple[TextGenResult, str, dict | None, str | None]:
    """生成正文并做内容校验；命中无效输出时追加严格约束重试一次。

    校验口径见 `content_guard.check_chapter_content`：复述任务、罗列写作准则、
    英文思考、列表占比过高等都算无效，绝不允许写进章节正文。
    末轮会把「正文 + 本章摘要 JSON」一起返回，因此通过 `extract` 先拆出正文
    再做校验，摘要块的解析结果原样返回给调用方决策（成功即内联落盘）。
    重试仍无效时抛出明确错误，由调用方展示，不做静默写入；重试成功时通过
    `on_notice` 留下一句可读说明，避免自动补救静默发生。

    退化保护按调研 2.3 / 6.1 的阶梯执行，不整轮丢弃：
    命中检测 → 截到循环起点前一句 → 注入「推进指令 + 剩余字数」局部续写
    （最多 `MAX_LOCAL_CONTINUATIONS` 次）→ 仍不收敛则整节重写 1 次 →
    再失败才报错且不落盘。判定阈值见 `writing/degeneration.py`，不是长度上限。

    参数:
        adapter: 写作适配器。
        request: 本轮统一文本请求。
        on_delta: 流式增量回调；为 None 时走一次性生成。
        label: 日志与报错里的轮次说明（如「第 1 轮正文」）。
        on_notice: 自动补救说明回调 `(text) -> None`；无人接收时只写日志。
        counter: 请求计数 `{"calls": n}`；每次真正调用模型加 1。
        extract: 把原始输出拆成 `(body, payload, error)`；None 时正文即原始输出。
        retry_suffix: 重试时追加在严格约束后的补充要求（如「仍要输出摘要 JSON」）。
        target_chars: 本轮目标字数；用于推导退化判定窗口与「剩余字数」提示。
        signals: 启用的退化检测信号；None 时用全量（压缩只留 `suffix`，
            因为原文本身可能带重复结构，S2/S3 会误杀）。

    返回:
        `(result, body, payload, error)`：body 为通过校验的正文。

    异常:
        AdapterError(UPSTREAM_ERROR): 局部续写与整节重写后仍退化，或重试一次后仍不是小说正文。
    """
    target_chars = max(0, int(target_chars or 0))
    guard_signals = signals or ("suffix", "shingle", "sentence")

    def notice(text: str) -> None:
        """记录并转交自动补救说明（不静默）。"""
        logger.warning(text)
        if on_notice:
            on_notice(text)

    def call(req: TextGenRequest) -> TextGenResult:
        """执行一轮生成；命中退化时先局部续写、再整节重写。"""
        base_messages = list(req.messages)
        accepted = ""
        continuations = 0
        rewritten = False
        current = req
        while True:
            guard = DegenerationGuard(target_chars, signals=guard_signals)
            if counter is not None:
                counter["calls"] = int(counter.get("calls") or 0) + 1
            try:
                if on_delta:
                    result = stream_text(adapter, current, on_delta, guard=guard)
                else:
                    result = adapter.generate(current)
                    if result.text:
                        reason = guard.feed(result.text) or guard.check_all()
                        if reason:
                            raise guard.exception(len(result.text))
            except DegenerationDetected as exc:
                # 截到循环起点前一句：保留正常行文，只丢重复段落
                accepted = (accepted + guard.text)[: len(accepted) + exc.keep_index]
                if continuations < MAX_LOCAL_CONTINUATIONS:
                    continuations += 1
                    remaining = max(0, target_chars - _count_words(accepted))
                    notice(
                        f"{label}检测到重复退化（{exc.reason}）：已截回到循环起点前一句"
                        f"（保留 {_count_words(accepted)} 字），改为局部续写"
                        f"（第 {continuations}/{MAX_LOCAL_CONTINUATIONS} 次，剩余约 {remaining} 字）"
                    )
                    extra: list[Message] = []
                    if accepted.strip():
                        extra.append(Message("assistant", accepted))
                    extra.append(
                        Message(
                            "user",
                            degeneration_continue_instruction(exc.reason, remaining),
                        )
                    )
                    current = replace(req, messages=base_messages + extra)
                    set_llm_call_reason("degeneration", "service._generate_validated: 退化后局部续写")
                    continue
                if not rewritten:
                    rewritten = True
                    accepted = ""
                    notice(
                        f"{label}局部续写 {MAX_LOCAL_CONTINUATIONS} 次仍未收敛（{exc.reason}），"
                        "改为整节重写 1 次"
                    )
                    current = replace(
                        req,
                        messages=base_messages + [Message("user", degeneration_retry_instruction(exc.reason))],
                    )
                    set_llm_call_reason("degeneration", "service._generate_validated: 退化后整节重写")
                    continue
                raise
            # 看门狗命中：不丢内容，保留已写正文、按句末收尾，再局部续写或结束本轮。
            # 只有带本轮目标字数的请求才有看门狗语义；目标未知时仍按 length 异常提示。
            if str(getattr(result, "finish_reason", "") or "").lower() == "length":
                if target_chars <= 0:
                    notice(
                        f"{label}以 finish_reason=length 结束（异常）：输出侧未设置人为上限，"
                        "说明触发了模型自身输出上限或上游异常，结尾可能不完整；本轮内容已保留，请检查后重试"
                    )
                    accepted += result.text or ""
                    return replace(result, text=accepted) if accepted != (result.text or "") else result
                with_summary = bool((req.metadata or {}).get("with_summary"))
                note = describe_watchdog(target_chars, with_summary)
                accepted += result.text or ""
                if continuations < MAX_LOCAL_CONTINUATIONS:
                    continuations += 1
                    accepted = accepted[: keep_at_sentence_end(accepted)]
                    remaining = max(0, target_chars - _count_words(accepted))
                    notice(
                        f"{label}到达看门狗上限（{note}）：已保留已写 {_count_words(accepted)} 字"
                        f"并按句末收尾，改为局部续写（第 {continuations}/{MAX_LOCAL_CONTINUATIONS} 次，"
                        f"剩余约 {remaining} 字）"
                    )
                    extra: list[Message] = []
                    if accepted.strip():
                        extra.append(Message("assistant", accepted))
                    extra.append(
                        Message(
                            "user",
                            degeneration_continue_instruction("到达看门狗上限", remaining)
                            + (retry_suffix if with_summary else ""),
                        )
                    )
                    current = replace(req, messages=base_messages + extra)
                    set_llm_call_reason("watchdog", "service._generate_validated: 看门狗后续写")
                    continue
                accepted = accepted[: keep_at_sentence_end(accepted)] or (result.text or "")
                notice(
                    f"{label}到达看门狗上限（{note}）：续写次数已用尽，按句末收尾结束本轮"
                    f"（保留已写 {_count_words(accepted)} 字，未丢弃正文）"
                )
                return replace(result, text=accepted, finish_reason="watchdog")
            accepted += result.text or ""
            if accepted != (result.text or ""):
                return replace(
                    result,
                    text=accepted,
                    usage=result.usage,
                )
            return result

    splitter = extract or (lambda text: (text, None, None))
    try:
        result = call(request)
    except DegenerationDetected as exc:
        raise AdapterError(
            ErrorCode.UPSTREAM_ERROR,
            f"{label}检测到重复退化（{exc.reason}）：局部续写 {MAX_LOCAL_CONTINUATIONS} 次"
            f"与整节重写 1 次后仍重复，已丢弃本轮产出（约 {exc.chars} 字），未写入章节；请重试生成",
            retryable=True,
            detail={"reason": exc.reason, "code": "degeneration"},
        ) from exc

    body, payload, parse_error = splitter((result.text or "").strip())
    body = (body or "").strip()
    ok, reason = check_chapter_content(body)
    if ok or not body:
        # 空正文交给调用方按原有逻辑报错，这里只负责内容形态校验
        return result, body, payload, parse_error

    logger.warning("%s校验未通过：%s；追加严格约束后重试一次", label, reason)
    retry_request = replace(
        request,
        messages=list(request.messages)
        + [Message("user", strict_retry_instruction() + retry_suffix)],
    )
    try:
        set_llm_call_reason("content_retry", "service._generate_validated: 内容校验未通过重试")
        result = call(retry_request)
    except DegenerationDetected as exc:
        raise AdapterError(
            ErrorCode.UPSTREAM_ERROR,
            f"{label}严格重试时检测到重复退化（{exc.reason}）：已丢弃该轮产出，未写入章节；请重试生成",
            retryable=True,
            detail={"reason": exc.reason, "code": "degeneration"},
        ) from exc
    body, payload, parse_error = splitter((result.text or "").strip())
    body = (body or "").strip()
    ok, reason_after = check_chapter_content(body)
    if not ok:
        raise AdapterError(
            ErrorCode.UPSTREAM_ERROR,
            f"{label}不是小说正文（{reason_after}）：已重试 1 次仍无效，内容未写入章节，请重试生成",
            retryable=True,
            detail={"reason": reason_after, "head": body[:300]},
        )
    message = f"{label}内容校验未通过（{reason}），已自动重试 1 次并采用重试结果"
    logger.info(message)
    if on_notice:
        on_notice(message)
    return result, body, payload, parse_error


#: 末轮重试时补一句摘要格式要求，避免模型在严格约束下丢掉摘要块
_RETRY_SUMMARY_SUFFIX = (
    "\n重试时仍按本轮要求：正文写完后另起一行输出分隔标记 <<<CHAPTER_SUMMARY_JSON>>>，"
    "再输出 chapter_summary / book_summary 的 JSON。"
)


def _generate_final_round(
    adapter,
    request: TextGenRequest,
    on_delta,
    label: str,
    on_notice=None,
    counter=None,
    target_chars: int = 0,
):
    """正文末轮生成：一次请求同时产出正文与本章摘要，并做结构化拆分。

    参数:
        adapter: 写作适配器。
        request: 末轮请求（user 指令里已包含摘要输出约定）。
        on_delta: 流式增量回调；为 None 时走一次性生成。
        label: 日志与报错里的轮次说明。
        on_notice: 自动补救说明回调。
        counter: 请求计数 `{"calls": n}`。
        target_chars: 本轮目标字数（退化判定窗口与剩余字数提示）。

    返回:
        `(result, body, payload, error, calls)`；payload 为
        `{chapter_summary, book_summary}`，解析失败时 error 给出中文原因。
    """
    local = counter if counter is not None else {"calls": 0}
    result, body, payload, error = _generate_validated(
        adapter,
        request,
        on_delta,
        label,
        on_notice=on_notice,
        counter=local,
        extract=split_final_output,
        retry_suffix=_RETRY_SUMMARY_SUFFIX,
        target_chars=target_chars,
    )
    return result, body, payload, error, int(local["calls"])


def _generate_chapter_rounds(
    adapter,
    req: TextGenRequest,
    groups: list[list[dict]],
    total_beats: int,
    chapter_words: int,
    max_rounds: int,
    on_delta=None,
    name_by_id: dict | None = None,
    on_notice=None,
    on_progress=None,
    summary_contract: str = "",
    start_round: int = 0,
    start_content: str = "",
    start_summary: dict | None = None,
    start_summary_error: str | None = None,
    on_round=None,
) -> dict:
    """按节拍分组串行写作，写完整组即推进节拍指针。

    前缀（system、首条 user、temperature、stop）全程不变，仅在末尾追加
    「assistant 已生成正文」与一句本轮节拍指令，保证前缀缓存可复用。
    停止只由节拍决定：全部写完为 `beats_done`，轮数用尽为 `max_rounds`。
    末轮同时产出「正文 + 本章摘要」，摘要解析失败不阻塞正文，交给调用方回退。
    每轮结束通过 `on_round` 通知调用方落盘断点；续写时从 `start_round` 接着跑，
    请求前缀与一次跑完完全一致。

    参数:
        adapter: 写作适配器（需实现 generate）。
        req: 首轮请求。
        groups: 按轮切分好的节拍分组。
        total_beats: 本章节拍总数。
        chapter_words: 全章参考字数。
        max_rounds: 最大轮数（保险上限）。
        on_delta: 流式增量回调；传入时每轮改为流式生成。
        name_by_id: 角色 ID → 姓名，用于本轮指令写明出场角色。
        on_notice: 自动补救说明回调；内容校验重试成功时上报一句可读说明。
        on_progress: 进度回调；每轮开始时上报轮次与已写字数，供页面显示进展。
        summary_contract: 末轮追加的摘要输出约定；空串表示不产出内联摘要。
        start_round: 断点续写起点（已完成轮数）。
        start_content: 断点里已写好的正文。
        start_summary: 断点里已解析出的本章摘要（可能为空）。
        start_summary_error: 断点里记录的摘要解析失败原因。
        on_round: 每轮结束回调，参数为 `{content, rounds, beats_done, provider,
            summary, summary_error}`。

    返回:
        {"content", "rounds", "beats_done", "stop_reason", "provider",
         "summary", "summary_error", "requests"}。
    """
    content = start_content
    rounds = start_round
    beats_done = sum(len(item) for item in groups[:start_round])
    provider = ""
    stop_reason = "beats_done"
    summary_payload = start_summary
    summary_error = start_summary_error
    counter = {"calls": 0}
    for index in range(start_round, len(groups)):
        if index >= max_rounds:
            stop_reason = "max_rounds"
            # 轮数是保险上限：真的被它截停时，剩余节拍与原因必须让用户看见
            remaining = max(0, total_beats - beats_done)
            if remaining and on_notice:
                on_notice(
                    f"已达到最大轮数 {max_rounds}，自动停止：本章还有 {remaining} 个节拍未写，"
                    "可调大最大轮数或每轮节拍数后重新生成"
                )
            break
        group = groups[index]
        final = index == len(groups) - 1
        if on_progress:
            # 进度口径：轮次 + 已写字数。超长 / 退化中止时用户也能看到仍在推进
            on_progress(
                {
                    "type": "round",
                    "round": index + 1,
                    "rounds_total": len(groups),
                    "beats_done": beats_done,
                    "chars": len(content),
                }
            )
        instruction = _beats_round_instruction(
            groups, index, total_beats, chapter_words, name_by_id, written_chars=len(content)
        )
        if final and summary_contract:
            instruction = f"{instruction}\n{summary_contract}"
        round_words = _group_words(group, total_beats, chapter_words)
        round_meta = dict(req.metadata or {})
        # 看门狗按本轮目标字数算；末轮同时出摘要，额外加摘要额度
        round_meta["watchdog_target_words"] = round_words
        round_meta["with_summary"] = bool(final and summary_contract)
        if index == 0:
            request = replace(req, metadata=round_meta)
        else:
            request = replace(
                req,
                metadata=round_meta,
                messages=list(req.messages)
                + [Message("assistant", content), Message("user", instruction)],
            )
        if final and summary_contract:
            result, segment, payload, parse_error, _calls = _generate_final_round(
                adapter,
                request,
                on_delta,
                f"第 {index + 1} 轮正文",
                on_notice=on_notice,
                counter=counter,
                target_chars=round_words,
            )
            summary_payload = payload
            summary_error = parse_error
        else:
            result, segment, _payload, _error = _generate_validated(
                adapter,
                request,
                on_delta,
                f"第 {index + 1} 轮正文",
                on_notice=on_notice,
                counter=counter,
                target_chars=round_words,
            )
        provider = result.model or provider
        rounds = index + 1
        start_beat = sum(len(item) for item in groups[:index])
        end_beat = start_beat + len(group)
        if not segment:
            raise AdapterError(
                ErrorCode.UPSTREAM_ERROR,
                f"第 {rounds} 轮（节拍 {start_beat + 1}–{end_beat}）返回空正文：生成已停止，"
                "请检查模型服务后重试（输出侧未设置人为上限）",
                retryable=True,
            )
        expected_words = _group_words(group, total_beats, chapter_words)
        actual_words = _count_words(segment)
        if expected_words > 0 and actual_words > max(int(expected_words * 1.5), expected_words + 300):
            message = (
                f"第 {rounds} 轮输出 {actual_words} 字，明显超出本轮预期 {expected_words} 字"
                f"（{actual_words / expected_words:.1f} 倍），疑似复述、跑题或模型未自然停止"
            )
            logger.warning(message)
            if on_notice:
                on_notice(message)
        before_chars = len(content)
        content = _append_segment(content, segment)
        added = len(content) - before_chars
        if index > 0 and len(segment) > 400 and added < len(segment) * 0.6:
            message = (
                f"第 {rounds} 轮开头与已写正文高度重复（新增 {added}/{len(segment)} 字，"
                "已自动去重）；疑似复述，请检查本轮内容"
            )
            logger.warning(message)
            if on_notice:
                on_notice(message)
        beats_done = end_beat
        if on_round:
            on_round(
                {
                    "content": content,
                    "rounds": rounds,
                    "beats_done": beats_done,
                    "provider": provider,
                    "summary": summary_payload,
                    "summary_error": summary_error,
                }
            )
    if rounds == 0:
        raise AdapterError(ErrorCode.UPSTREAM_ERROR, "生成轮数为 0：请检查 max_rounds 配置", retryable=False)
    return {
        "content": content,
        "rounds": rounds,
        "beats_done": beats_done,
        "stop_reason": stop_reason,
        "provider": provider,
        "summary": summary_payload,
        "summary_error": summary_error,
        "requests": int(counter["calls"]),
    }


def _script_work_path(store: ProjectStore, chapter_id: str):
    """标注批次中间结果文件：`<工程>/.ndw/script_batches/<章节>.json`。

    参数:
        store: 工程。
        chapter_id: 章节 ID。

    返回:
        Path。
    """
    return store.root / ".ndw" / "script_batches" / f"{chapter_id}.json"


def _script_work_load(store: ProjectStore, chapter_id: str, per_round: int, total: int) -> dict | None:
    """读取上次标注批次（仅当章节、批大小、镜头总数一致时复用）。

    参数:
        store: 工程。
        chapter_id: 章节 ID。
        per_round: 每批镜头数。
        total: 镜头总数。

    返回:
        work dict 或 None。
    """
    path = _script_work_path(store, chapter_id)
    if not path.exists():
        return None
    try:
        work = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(work, dict) or not isinstance(work.get("batches"), list):
        return None
    if work.get("chapter") != chapter_id or int(work.get("per_round") or 0) != per_round or int(work.get("total") or 0) != total:
        return None
    return work


def _script_work_save(store: ProjectStore, work: dict) -> None:
    """原子保存标注批次中间结果。

    参数:
        store: 工程。
        work: 工作区 dict。

    返回:
        无。
    """
    path = _script_work_path(store, work.get("chapter") or "")
    path.parent.mkdir(parents=True, exist_ok=True)
    work["updated_at"] = now_iso()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(work, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _generate_text(adapter, req: TextGenRequest, on_delta=None):
    """调用写作适配器生成文本；适配器支持流式时逐片回调增量。

    参数:
        adapter: 写作适配器。
        req: 文本请求。
        on_delta: 增量回调（可选）。

    返回:
        TextGenResult。
    """
    if on_delta is None:
        return adapter.generate(req)
    stream_fn = getattr(adapter, "generate_stream", None)
    if stream_fn is not None:
        return stream_fn(req, on_delta=on_delta)
    result = adapter.generate(req)
    if result.text:
        on_delta(result.text)
    return result


def _scene_block(scene: dict, prefix: str = "") -> str:
    """把一个场景渲染成提示词片段。

    参数:
        scene: 场景负载。
        prefix: 展示前缀（如「参考·前」）。

    返回:
        多行文本。
    """
    title = f"【{prefix}镜头 {scene['index'] + 1}】" if prefix else f"【镜头 {scene['index'] + 1}】"
    lines = [f"{title}{scene['title']}｜地点：{scene['location']}｜时间：{scene['time_of_day']}｜情绪：{scene['mood']}"]
    for line in scene["lines"]:
        label = line["speaker_name"] or ("旁白" if line.get("kind") == "narration" else "（未标注）")
        lines.append(f"{label}：{line['text']}")
    return "\n".join(lines)


def suggest_script(
    store: ProjectStore,
    config: AppConfig,
    chapter_id: str,
    dry_run: bool = False,
    scenes_per_round: int | None = None,
    resume: bool = False,
    on_progress=None,
) -> dict:
    """生成导演脚本标注建议并试解析（按批流式，单批失败可续跑）。

    参数:
        store: 工程。
        config: 本机配置。
        chapter_id: 章节 ID。
        dry_run: 只解析不落盘（兼容旧参数）。
        scenes_per_round: 每批镜头数；None 时取 `writing.multi_turn.script_scenes_per_round`（默认 8）。
        resume: True 时复用上次中间结果，只重试失败/未完成的批次。
        on_progress: 进度回调；事件 `batch{index,total,start,end}` / `chars{chars,...}`。

    返回:
        {"markup", "script", "warnings", "shots", "lines", "provider", "dry_run",
        "rounds", "failures", "batches", "partial"}。

    异常:
        ValueError: 章节没有场景或台词行。
        AdapterError: 全部批次失败，或模型输出完全无法解析。
    """
    chapter = store.load_chapter(chapter_id)
    scenes = chapter.get("scenes") or []
    if not scenes:
        raise ValueError(f"章节 {chapter_id} 还没有场景：先执行 ndw scene split")
    characters = store.load_characters()["characters"]
    char_names = {item["id"]: item.get("name") or "" for item in characters}
    settings_store = SettingsStore(store.root)
    bible = settings_store.ensure()

    scenes_payload = []
    for scene in sorted(scenes, key=lambda item: item.get("index") or 0):
        lines = []
        for line in scene.get("lines") or []:
            lines.append(
                {
                    "speaker_id": line.get("speaker_id"),
                    "speaker_name": char_names.get(line.get("speaker_id")) or ("旁白" if line.get("kind") == "narration" else ""),
                    "text": line.get("text"),
                    "kind": line.get("kind"),
                    "emotion": line.get("emotion") or "neutral",
                }
            )
        if not lines:
            continue
        scenes_payload.append(
            {
                "index": scene.get("index") or 0,
                "title": scene.get("title") or scene.get("scene_label") or f"场景 {(scene.get('index') or 0) + 1}",
                "location": scene.get("location") or "未标注",
                "time_of_day": scene.get("time_of_day") or "unknown",
                "mood": scene.get("mood") or "克制",
                "lines": lines,
            }
        )
    if not scenes_payload:
        raise ValueError(f"章节 {chapter_id} 的场景里没有任何台词行：先执行 ndw line split")

    adapter = build_writing_adapter(config, bible)
    work_info = store.load_project().get("work") or {}
    context = _project_context(work_info, bible, characters)
    base_parts = [context] if context else []
    base_parts.append(
        f"作品《{chapter.get('title')}》导演标注任务：按镜头输出 NDW 标注文本"
        "（@shot / !sfx / !visual / !anim / !bgm），保留台词原文与顺序，不要解释。\n"
        "严格格式：\n"
        "@shot 1 镜头名 location=验尸房 time=night mood=压抑\n"
        "旁白：夜色渐深。\n"
        "沈砚秋|calm：执照可以收，尸体不能白躺。\n"
        "!sfx ambience 8000 after-line=1 雨声敲窗 || Gentle rain tapping a window, close perspective, wet glass resonance\n"
        "!visual 画面描述\n"
        "!bgm mood=tense energy=0.4 intensity=0.6 loop=1 name=夜雨独行 style=低音弦乐+稀疏钢琴 function=铺垫危机 en=tense+low+strings+sparse+piano+slow+clock+ticks+dark+minor\n"
        "!anim i2v 5000 slow_push_in 镜头运动\n"
        "规则：@shot 后必须是整数序号与镜头名；time 取 dawn/morning/day/dusk/night/unknown；"
        "!sfx 的 kind 只能是 ambience/foley/impact/transition/music_sting，第二个参数是毫秒数，位置写 after-line=N；"
        "!sfx 的中文描述后必须用「||」跟一段英文提示词：写清声源、材质、动作、空间感与音色，不是逐字直译，也不要写时长；"
        "!visual 直接跟画面提示词，不写画幅；"
        "!bgm 每个镜头最多一条，mood 只能是 calm/tense/sad/romantic/mysterious/action/comedic/epic/horror，"
        "energy/intensity 取 0~1，style 与 function 与 en 里的空格用 + 代替；"
        "!bgm 必须带 en= 英文提示词：包含风格、情绪、配器、节奏与制作质感，不是逐字直译；"
        "同一镜头内情绪有明显变化时，追加 !bgm-seg mood=... anchor=镜头内台词序号 style=...；"
        "!silence 写成「!silence 800 after-line=N 原因」；"
        "!anim 的 mode 只能是 i2v/t2v，第二个参数是 500~5000 的毫秒数，第三个参数只能是 "
        "static/slow_push_in/pan_left/pan_right/handheld；每个镜头至少一句台词；台词保留原文。\n"
        "说话人规则：必须原样取自角色表里的名字，不得自造、不得加 *、编号、括号注释或写「未知」；"
        "某行显示为「（未标注）」时，先结合上下文与角色表推断最可能的说话人并只写名字，"
        "确实无法判断时才把说话人留空写成「：台词」（系统会标为待校对）；"
        "旁白固定写「旁白：」。音效位置 after-line=N 必须是本镜头内已出现的台词序号，不要超出。"
    )
    base_prompt = "\n\n".join(base_parts)
    system_prompt = (
        "你是有声剧导演，把给定台词改写成 NDW 标注文本，不要解释、不要 Markdown 代码块。\n"
        "只标注用户指定的本批镜头，参考镜头只用于理解上下文，不要输出。"
    )

    mt = config.get("writing.multi_turn") or {}
    try:
        per_round = int(scenes_per_round or mt.get("script_scenes_per_round") or 8)
    except (TypeError, ValueError):
        per_round = 8
    per_round = max(1, per_round)

    total = len(scenes_payload)
    rounds = (total + per_round - 1) // per_round
    work = _script_work_load(store, chapter_id, per_round, total) if resume else None
    if work is None:
        work = {
            "version": "script_batches/1",
            "chapter": chapter_id,
            "per_round": per_round,
            "total": total,
            "created_at": now_iso(),
            "batches": [
                {
                    "index": index + 1,
                    "start": index * per_round,
                    "end": min(total, index * per_round + per_round),
                    "status": "pending",
                    "markup": "",
                    "error": None,
                }
                for index in range(rounds)
            ],
        }

    # 批次总数以真实批次列表为准：续跑复用的批次可能少于按当前参数重算的轮数
    rounds = len(work["batches"])
    failures: list[dict] = []
    last_result = None
    for batch in work["batches"]:
        if batch.get("markup"):
            continue
        index = int(batch["index"])
        start, end = int(batch["start"]), int(batch["end"])
        subset = scenes_payload[start:end]
        first_no, last_no = subset[0]["index"] + 1, subset[-1]["index"] + 1
        # 直接调用进度回调：页面取消时抛出的异常要能中断本批生成
        if on_progress is not None:
            on_progress({"type": "batch", "index": index, "total": rounds, "start": first_no, "end": last_no})
        body = [f"本批镜头 {first_no}–{last_no}（全章共 {total} 个镜头，编号沿用全局序号）："]
        if start > 0:
            body.append("参考·前一个镜头（只读，不要输出标注）：\n" + _scene_block(scenes_payload[start - 1], "参考·"))
        body.append("\n\n".join(_scene_block(scene) for scene in subset))
        if end < total:
            body.append("参考·后一个镜头（只读，不要输出标注）：\n" + _scene_block(scenes_payload[end], "参考·"))
        body.append(
            f"任务：仅为本批 {len(subset)} 个镜头生成 NDW 标注文本，保留台词原文与顺序；"
            "每条 !sfx 必须带「|| 英文提示词」，不要解释。\n"
            f"编号规则：@shot 序号必须是 {first_no}–{last_no} 内的整数且与本批【镜头 N】一一对应，"
            "不得重号、跳号或新建镜头；参考镜头不要输出。"
        )
        request = TextGenRequest(
            task="script",
            system=system_prompt,
            messages=[Message("user", base_prompt + "\n\n" + "\n\n".join(body))],
            stream=True,
            metadata={"scenes": subset, "style": bible.get("style") or {}, "thinking_stage": "script"},
        )
        state = {"chars": 0}

        def on_delta(text: str) -> None:
            """累计本批接收长度并上报（取消异常需向上传播）。"""
            if not text or on_progress is None:
                return
            state["chars"] += len(str(text))
            state["tail"] = (str(state.get("tail") or "") + str(text))[-200:]
            on_progress(
                {
                    "type": "chars",
                    "chars": state["chars"],
                    "sample": state["tail"],
                    "index": index,
                    "total": rounds,
                }
            )

        try:
            result = _generate_text(adapter, request, on_delta if on_progress else None)
            last_result = result
            if str(getattr(result, "finish_reason", "") or "").lower() == "length":
                raise AdapterError(
                    ErrorCode.UPSTREAM_ERROR,
                    f"第 {first_no}–{last_no} 个镜头（第 {index}/{rounds} 批）标注输出被截断"
                    "（finish_reason=length）：输出侧没有人为上限，"
                    "说明触发了模型自身输出上限或上游异常。请调小每批场景数后重试",
                )
            markup = (result.text or "").strip()
            if not markup:
                raise AdapterError(ErrorCode.INVALID_INPUT, "本批未返回标注文本")
            batch["markup"] = markup
            batch["status"] = "done"
            batch["error"] = None
        except Exception as exc:  # 单批失败保留其它批次
            message = getattr(exc, "message", None) or str(exc) or exc.__class__.__name__
            batch["status"] = "failed"
            batch["error"] = message
            failures.append(
                {"batch": index, "start_scene": first_no, "end_scene": last_no, "message": message}
            )
        _script_work_save(store, work)

    done_batches = [batch for batch in work["batches"] if batch.get("markup")]
    if not done_batches:
        detail = "；".join(
            f"第 {item['start_scene']}–{item['end_scene']} 个镜头：{item['message']}" for item in failures[:3]
        )
        raise AdapterError(
            ErrorCode.UPSTREAM_ERROR,
            f"标注生成失败：全部 {rounds} 批都没有成功（{detail}）。已保留失败批次，可用 --resume 只重试失败批次",
            detail={"failures": failures},
        )
    markup = "\n\n".join(batch["markup"] for batch in done_batches).strip()
    #: 硬告警的结构化定位信息（页面点击告警直接跳转用）；信息级提示不写入
    warning_items: list[dict] = []
    try:
        script = parse_markup(
            markup, chapter, characters, store.settings(), diagnostics=warning_items
        )
    except ValueError as exc:
        raise AdapterError(ErrorCode.INVALID_INPUT, f"导演标注无法解析：{exc}", detail={"raw": markup[:2000]}) from exc
    if not failures:
        try:
            _script_work_path(store, chapter_id).unlink(missing_ok=True)
        except OSError:
            pass
    return {
        "markup": markup,
        "script": script,
        "warnings": script.get("warnings") or [],
        "warning_items": warning_items,
        "shots": len(script.get("shots") or []),
        "lines": sum(len(shot.get("lines") or []) for shot in script.get("shots") or []),
        "provider": getattr(last_result, "model", "") or getattr(adapter, "model", "") or "offline-writing",
        "dry_run": dry_run,
        "rounds": rounds,
        "failures": failures,
        "batches": [
            {"index": batch["index"], "start": batch["start"], "end": batch["end"], "status": batch["status"]}
            for batch in work["batches"]
        ],
        "partial": bool(failures),
    }


# ---------------- AI 生书：设定 / 人物 / 润色 ----------------

#: 设定生成步骤（世界观归世界库，写作设定只管故事层）
SETTING_STEPS = ("premise", "style")
#: 梗概分区字段（与 setting-bible.schema.json 对齐）
PREMISE_FIELDS = ("logline", "theme", "core_conflict", "ending_intent", "target_audience", "target_length")
#: 文风分区字段
STYLE_FIELDS = ("tone", "pov", "tense", "prose_style", "forbidden", "visual_style")
#: 标量字段长度上限（来自 schema）
SCALAR_LIMITS = {
    "logline": 500,
    "theme": 500,
    "core_conflict": 1000,
    "ending_intent": 1000,
    "target_audience": 200,
    "tone": 300,
    "prose_style": 1000,
    "visual_style": 500,
    "character.appearance_prompt": 2000,
    "character.negative_prompt": 1000,
}
#: 枚举取值
POV_OPTIONS = ("first", "third_limited", "third_omniscient", "second")
TENSE_OPTIONS = ("past", "present")
LENGTH_OPTIONS = ("short", "novella", "novel", "series")
ROLE_OPTIONS = ("protagonist", "antagonist", "supporting", "minor")
GENDER_OPTIONS = ("male", "female", "other", "unknown")
#: 模型偶尔把枚举写成中文（含括号说明），校验前先按前缀归一；长别名放前面避免误匹配
CHOICE_ALIASES = {
    "短篇": "short",
    "中篇": "novella",
    "长篇连载": "series",
    "系列": "series",
    "中长篇": "novella",
    "长篇": "novel",
    "第三人称有限": "third_limited",
    "第三人称全知": "third_omniscient",
    "第三人称": "third_limited",
    "第一人称": "first",
    "第二人称": "second",
    "过去时": "past",
    "现在时": "present",
    "主角": "protagonist",
    "男主": "protagonist",
    "女主": "protagonist",
    "反派": "antagonist",
    "配角": "supporting",
    "男配": "supporting",
    "女配": "supporting",
    "龙套": "minor",
    "男性": "male",
    "女性": "female",
    "男": "male",
    "女": "female",
    "其他": "other",
    "未知": "unknown",
}
#: 单字段润色白名单（世界观文本由世界库维护，不在写作设定里润色）
POLISH_FIELDS = (
    "logline",
    "theme",
    "core_conflict",
    "ending_intent",
    "target_audience",
    "tone",
    "prose_style",
    "visual_style",
    "character.background",
    "character.personality",
    "character.appearance_prompt",
    "character.negative_prompt",
    "work.genre",
)
#: 黄金三章检查用的冲突语义关键词
CONFLICT_KEYWORDS = ("冲突", "对抗", "矛盾", "争执", "对峙", "摊牌", "威胁", "追杀", "决裂", "争夺", "冲突升级")


def _extract_json_object(text: str) -> dict:
    """从模型输出中提取第一个 `{` 到最后一个 `}` 之间的 JSON 对象。

    参数:
        text: 模型原始输出（允许被 ```json 代码块包裹）。

    返回:
        解析后的 dict。

    异常:
        ValueError: 没有 JSON 对象或解析失败。
    """
    raw = (text or "").strip()
    if not raw:
        raise ValueError("LLM 返回内容为空")
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("LLM 返回内容不是合法 JSON")
    payload = json.loads(raw[start : end + 1])
    if not isinstance(payload, dict):
        raise ValueError("LLM 返回内容不是合法 JSON")
    return payload


# ---------- 结构化生成工具（LLM function calling） ----------

#: 世界条目参数 schema（单条与批量共用；世界库负责总述与分类条目）
_WORLD_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "世界/时代名称，80 字以内"},
        "era": {"type": "string", "description": "时代背景，200 字以内"},
        "summary": {"type": "string", "description": "世界观总述，500 字以内"},
        "rules": {"type": "array", "items": {"type": "string"}, "description": "世界规则/力量体系，每条 120 字以内"},
        "factions": {"type": "array", "items": {"type": "string"}, "description": "主要势力，每条 120 字以内"},
        "locations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "atmosphere": {"type": "string"},
                },
                "required": ["name"],
            },
            "description": "关键地点：JSON 数组，每个元素必须是对象（不要写成字符串），含 name/description/atmosphere",
        },
        "others": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["name"],
            },
            "description": "其它条目（关键物件、习俗、生物等未归类内容）：对象数组，含 name/description",
        },
        "glossary": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "term": {"type": "string"},
                    "meaning": {"type": "string"},
                    "aliases": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["term", "meaning"],
            },
            "description": "专有名词表：JSON 数组，每个元素必须是对象（不要写成字符串），含 term/meaning",
        },
        "visual_style": {"type": "string", "description": "画面风格关键词，200 字以内"},
    },
    "required": ["name", "era"],
}

#: 音色描述参数 schema（正式角色 / 龙套 / 旁白共用）
_VOICE_PROFILE_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "voice_summary": {"type": "string", "description": "一句话音色概括，80 字以内"},
        "gender": {"type": "string", "enum": list(GENDER_OPTIONS)},
        "age_feel": {
            "type": "string",
            "enum": ["child", "teen", "young_adult", "adult", "middle_aged", "elderly"],
        },
        "age_range": {"type": "string", "description": "年龄区间，如「30-39」"},
        "pitch": {
            "type": "string",
            "enum": ["very_low", "low", "mid_low", "mid", "mid_high", "high", "very_high"],
        },
        "timbre": {"type": "array", "items": {"type": "string"}, "description": "2-4 个音色质感词"},
        "pace": {"type": "string", "enum": ["very_slow", "slow", "normal", "fast", "very_fast"]},
        "volume": {"type": "string", "enum": ["soft", "normal", "loud"]},
        "energy": {"type": "string", "enum": ["calm", "conversational", "dramatic", "authoritative"]},
        "warmth": {"type": "string", "enum": ["warm", "neutral", "cold", "distant"]},
        "articulation": {"type": "string", "enum": ["clear", "mumbled", "clipped", "drawl"]},
        "temperament": {"type": "array", "items": {"type": "string"}, "description": "2-4 个气质词"},
        "accent": {"type": "string", "description": "口音/地域腔调；标准普通话写「标准普通话」"},
        "design_prompt": {"type": "string", "description": "可直接用于文字造音色的提示词，120 字以内"},
        "negative_prompt": {"type": "array", "items": {"type": "string"}, "description": "应避免的音色特征"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "evidence": {"type": "array", "items": {"type": "string"}, "description": "推导依据，引用人物档案里的字段"},
    },
    "required": ["voice_summary", "design_prompt"],
}

#: 龙套槽位参数 schema
_STOCK_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "slot": {
            "type": "string",
            "description": "职业槽位稳定英文键，小写+下划线，如 waiter/guard/runner",
        },
        "voice_slot": {
            "type": "string",
            "description": "音色池槽位：male_young/male_mid/male_elder/female_young/female_mid/female_elder",
        },
        "label": {"type": "string", "description": "中文职业名，如「店小二」"},
        "archetype_tags": {"type": "array", "items": {"type": "string"}, "description": "2-3 个语义标签"},
    },
    "required": ["slot", "label"],
}

#: 人物条目参数 schema（单个与批量共用）
_CHARACTER_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "姓名，60 字以内"},
        "role": {
            "type": "string",
            "enum": list(ROLE_OPTIONS),
            "description": "protagonist=主角，antagonist=反派，supporting=配角，minor=龙套",
        },
        "gender": {"type": "string", "enum": list(GENDER_OPTIONS)},
        "age_range": {
            "type": "string",
            "enum": list(AgeRange.values()),
            "description": "年龄段分档，只能从枚举里选：10-14 儿童 / 18-25 青年 / 26-39 成年 / 40-54 中年 / 55-70 老年",
        },
        "personality": {
            "type": "array",
            "items": {"type": "string"},
            "description": "3-5 个性格短标签，每个不超过 20 字",
        },
        "background": {"type": "string", "description": "人物背景，500 字以内"},
        "speech_style": {
            "type": "object",
            "description": "写作时直接注入提示词的口吻约束：语速、音高、口音/用词习惯、口头禅",
            "properties": {
                "tempo": {
                    "type": "string",
                    "enum": list(SpeechTempo.values()),
                    "description": "语速档位：slow=慢，medium=中速，fast=快（只选枚举，不要写「偏慢，常停顿」这类长句）",
                },
                "pitch": {
                    "type": "string",
                    "enum": list(SpeechPitch.values()),
                    "description": "音高档位：low=偏低，medium=中等，high=偏高（只选枚举）",
                },
                "accent": {"type": "string", "description": "口音或用词习惯，如「带江南口音，爱用反问」"},
                "catchphrases": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "1-3 句标志性口头禅，写作时可直接复用",
                },
            },
        },
        "appearance_prompt": {"type": "string", "description": "外貌提示词，用于配图（可与 appearance_desc_en 一致）"},
        "negative_prompt": {"type": "string", "description": "配图负向提示词（可与 appearance_negative_en 一致）"},
        "voice_description_zh": {
            "type": "string",
            "description": "中文音色描述：音色质感、语速、语气、年龄感、口音，80-160 字，供 TTS 参考与人工校对",
        },
        "voice_description_en": {
            "type": "string",
            "description": "英文音色描述：可直接用于语音模型的 prompt，纯英文，覆盖 timbre/pace/tone/age feel/accent",
        },
        "appearance_desc_zh": {
            "type": "string",
            "description": "中文外貌描述（正面）：脸型、五官、发型、身形、惯常着装与气质，具体不空泛",
        },
        "appearance_desc_en": {
            "type": "string",
            "description": "英文正面出图 prompt：纯英文、具体可见特征，避免 young woman 这类泛描述",
        },
        "appearance_negative_zh": {
            "type": "string",
            "description": "中文负向说明：出图应排除的元素（如眼镜、胡须、伤疤），用名词短语，不要堆否定句",
        },
        "appearance_negative_en": {
            "type": "string",
            "description": "英文 negative prompt：逗号分隔的排除项，写 glasses, beard 这类名词，不要 not/no 句式",
        },
        "relationships": {
            "type": "object",
            "additionalProperties": {"type": "string"},
            "description": "与其他人物/势力的关系（名字→备注）",
        },
        "state": {
            "type": "object",
            "properties": {
                "relationship_notes": {"type": "object", "additionalProperties": {"type": "string"}}
            },
            "description": "人物状态；单人物生成时关系备注写在 relationship_notes",
        },
        "voice_profile": _VOICE_PROFILE_TOOL_SCHEMA,
        "stock": _STOCK_TOOL_SCHEMA,
    },
    "required": ["name"],
}

#: 人物描述性字段的统一要求（人物生成 / 逐人生成 / 单人物编辑共用）
_CHARACTER_DESC_RULES = (
    "除基础设定外，必须一次给出后续可直接使用的描述性字段（缺一不可）：\n"
    "1) voice_description_zh：中文音色描述，覆盖音色质感、语速、语气、年龄感、口音，80-160 字，能直接指导 TTS；\n"
    "2) voice_description_en：同一音色的英文描述，必须是可直接用于语音模型 prompt 的英文，不得夹杂中文，"
    "覆盖 timbre、pace、tone、age feel、accent；\n"
    "3) appearance_desc_zh：中文外貌描述（脸型、五官、发型、身形、惯常着装与气质），供界面展示与人工校对；\n"
    "4) appearance_desc_en：英文正面出图 prompt，纯英文，写具体可见特征（发型长度、眉眼、肤色、服装材质等），"
    "禁止只写 young woman、beautiful 这类空泛描述；\n"
    "5) appearance_negative_zh：中文负向说明，用名词短语列出出图应排除的元素（如眼镜、胡须、伤疤），"
    "不要写成否定句堆砌；\n"
    "6) appearance_negative_en：英文 negative prompt，逗号分隔的排除项（如 glasses, beard, scar），"
    "不要写 not/no 句式；\n"
    "7) speech_style：写作时直接进提示词的口吻约束，必须给 tempo（语速，只能取 slow/medium/fast）、"
    "pitch（音高，只能取 low/medium/high）、accent（口音或用词习惯）、catchphrases（1-3 句口头禅）；"
    "四个人物之间的语速、用词和口头禅要能明显区分，不能千篇一律。"
)

#: 龙套条目 schema：在人物 schema 上要求 stock
_STOCK_CHARACTER_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        **_CHARACTER_TOOL_SCHEMA["properties"],
        "stock": _STOCK_TOOL_SCHEMA,
        "role": {"type": "string", "enum": ["minor"], "description": "龙套固定为 minor"},
    },
    "required": ["name", "stock"],
}

#: 人物名单参数 schema（两阶段生成的第一阶段：只出名单，不写详情）
_CHARACTER_PLAN_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "characters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "姓名，60 字以内；不要与已有角色重名"},
                    "role": {
                        "type": "string",
                        "enum": list(ROLE_OPTIONS),
                        "description": "protagonist=主角，antagonist=反派，supporting=配角，minor=龙套",
                    },
                    "one_line": {"type": "string", "description": "一句话定位（身份 + 在故事里的作用），40 字以内"},
                    "main": {"type": "boolean", "description": "是否主要角色（主要角色会进入候选列表）"},
                },
                "required": ["name", "role", "one_line"],
            },
        },
        "basis": {"type": "string", "description": "名单依据：篇幅、章节量、已有角色与题材需要"},
    },
    "required": ["characters"],
}

#: 梗概参数 schema
_PREMISE_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "logline": {"type": "string", "description": "一句话故事核心，120 字以内"},
        "theme": {"type": "string", "description": "主题，200 字以内"},
        "core_conflict": {"type": "string", "description": "核心冲突，300 字以内"},
        "ending_intent": {"type": "string", "description": "结局意图，200 字以内"},
        "target_audience": {"type": "string", "description": "目标读者，200 字以内"},
        "target_length": {
            "type": "string",
            "enum": list(LENGTH_OPTIONS),
            "description": "short=短篇，novella=中篇，novel=长篇，series=长篇连载",
        },
    },
    "required": ["logline"],
}

#: 文风参数 schema
_STYLE_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "tone": {"type": "string", "description": "整体基调，200 字以内"},
        "pov": {"type": "string", "enum": list(POV_OPTIONS), "description": "叙事视角"},
        "tense": {"type": "string", "enum": list(TENSE_OPTIONS), "description": "时态"},
        "prose_style": {"type": "string", "description": "文笔风格描述，500 字以内"},
        "forbidden": {
            "type": "array",
            "items": {"type": "string"},
            "description": "需要避免的写法，每条 40 字以内",
        },
        "visual_style": {
            "type": "string",
            "description": "画面风格：具体的场景、光线、色彩、材质与镜头质感，120-300 字，不能为空",
        },
    },
    "required": ["tone", "visual_style"],
}


#: 大纲参数 schema（幕→章→节拍三层结构）
_OUTLINE_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "acts": {
            "type": "array",
            "description": "幕列表，按顺序排列",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "幕标题，80 字以内"},
                    "summary": {"type": "string", "description": "本幕概要，300 字以内"},
                    "chapters": {
                        "type": "array",
                        "description": "本幕章节，按顺序排列",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string", "description": "章节标题，80 字以内"},
                                "summary": {"type": "string", "description": "本章概要，300 字以内"},
                                "beats": {
                                    "type": "array",
                                    "description": "本章节拍，每个元素必须是对象（不要写成字符串）",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "summary": {"type": "string", "description": "节拍内容，200 字以内"},
                                            "character_ids": {
                                                "type": "array",
                                                "items": {"type": "string"},
                                                "description": "参与人物 id，从给定角色表中选取，可为空",
                                            },
                                            "turning_point": {"type": "boolean", "description": "是否为本章转折点"},
                                            "target_words": {"type": "integer", "description": "该节拍目标字数"},
                                            "extra_characters": {
                                                "type": "array",
                                                "items": {"type": "string"},
                                                "description": "只在本章出现、未建卡的龙套姓名（按 minor 处理，不进入角色表）",
                                            },
                                        },
                                        "required": ["summary"],
                                    },
                                },
                            },
                            "required": ["title"],
                        },
                    },
                },
                "required": ["title"],
            },
        }
    },
    "required": ["acts"],
}


#: 本章出场角色（与节拍同一次调用产出，不再单独发角色请求）
_CAST_ITEM_SCHEMA = {
    "type": "object",
    "required": ["name", "beats"],
    "properties": {
        "existing_id": {"type": "string", "description": "复用现有角色时填其 ID；新建角色留空"},
        "name": {"type": "string", "description": "角色姓名"},
        "aliases": {
            "type": "array",
            "items": {"type": "string"},
            "description": "本章出现的称呼/别名",
        },
        "role": {"type": "string", "enum": ["protagonist", "antagonist", "supporting", "minor"]},
        "gender": {"type": "string", "enum": ["male", "female", "other", "unknown"]},
        "age_range": {"type": "string"},
        "personality": {"type": "array", "items": {"type": "string"}},
        "background": {"type": "string", "description": "新建角色必须给，已有角色可留空"},
        "speech_style": {
            "type": "object",
            "properties": {
                "tempo": {"type": "string"},
                "pitch": {"type": "string"},
                "accent": {"type": "string"},
                "catchphrases": {"type": "array", "items": {"type": "string"}},
            },
        },
        "beats": {
            "type": "array",
            "items": {"type": "integer"},
            "description": "该角色出场的节拍序号（从 1 开始），这是绑定依据",
        },
    },
}

#: 单幕参数 schema：复用大纲结构里的 act 对象，外面再包一层 act 键，
#: 让 `emit_act` 一轮只产出一幕（title/summary/chapters[title/summary/beats/cast]）。
_ACT_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "act": copy.deepcopy(_OUTLINE_TOOL_SCHEMA["properties"]["acts"]["items"]),
    },
    "required": ["act"],
}
_ACT_TOOL_SCHEMA["properties"]["act"]["properties"]["chapters"]["items"]["properties"]["cast"] = {
    "type": "array",
    "description": "本章出场角色；与节拍同一次调用产出，每项必须给 beats 绑定依据",
    "items": _CAST_ITEM_SCHEMA,
}


#: 三级分层生成：阶段一幕列表（不塞章节与节拍）
_HIER_ACTS_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "acts": {
            "type": "array",
            "description": "幕列表，按顺序排列",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "幕标题，80 字以内"},
                    "summary": {"type": "string", "description": "本幕目标与概要，300 字以内"},
                    "chapters_count": {"type": "integer", "description": "本幕预计章数"},
                },
                "required": ["title"],
            },
        }
    },
    "required": ["acts"],
}

#: 三级分层生成：阶段二某幕的章节（只出标题与概要）
_HIER_CHAPTERS_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "chapters": {
            "type": "array",
            "description": "本幕章节，按顺序排列",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "章节标题，80 字以内"},
                    "summary": {"type": "string", "description": "本章概要，300 字以内"},
                },
                "required": ["title"],
            },
        }
    },
    "required": ["chapters"],
}

#: 复用大纲结构里的节拍项定义，避免重复维护
_BEAT_ITEM_SCHEMA = _OUTLINE_TOOL_SCHEMA["properties"]["acts"]["items"]["properties"]["chapters"]["items"]["properties"]["beats"]["items"]

#: 三级分层生成：阶段三某章的节拍（一次补齐）
_HIER_BEATS_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "beats": {"type": "array", "description": "本章全部节拍，按顺序排列", "items": _BEAT_ITEM_SCHEMA},
        "cast": {
            "type": "array",
            "description": "本章出场角色；与节拍同一次调用产出，每项必须给 beats 绑定依据",
            "items": _CAST_ITEM_SCHEMA,
        },
        "character_arcs": {
            "type": "array",
            "description": "本章主要角色弧线：角色在本章从什么状态走到什么状态（可选，最多 3 条）",
            "items": {
                "type": "object",
                "properties": {
                    "character_id": {"type": "string", "description": "角色 ID，必须来自给定角色表"},
                    "from_state": {"type": "string", "description": "本章开始时的状态"},
                    "to_state": {"type": "string", "description": "本章结束时的状态"},
                    "note": {"type": "string", "description": "变化原因（可选）"},
                },
                "required": ["character_id", "to_state"],
            },
        },
    },
    "required": ["beats"],
}


def _gen_tool(step: str | None) -> dict | None:
    """按生成步骤返回对应的工具定义（OpenAI tools 单项）。

    参数:
        step: outline|act|premise|style|world|worlds|character|characters。

    返回:
        工具定义 dict；未知步骤返回 None。

    异常:
        无。
    """
    if step == "outline_acts":
        return {
            "type": "function",
            "function": {"name": "emit_acts", "description": "输出幕列表（标题、目标/概要、预计章数）", "parameters": _HIER_ACTS_TOOL_SCHEMA},
        }
    if step == "outline_chapters":
        return {
            "type": "function",
            "function": {"name": "emit_chapters", "description": "输出某一幕的章节列表（标题与概要）", "parameters": _HIER_CHAPTERS_TOOL_SCHEMA},
        }
    if step == "outline_beats":
        return {
            "type": "function",
            "function": {"name": "emit_beats", "description": "输出某一章的全部节拍", "parameters": _HIER_BEATS_TOOL_SCHEMA},
        }
    if step == "outline":
        return {
            "type": "function",
            "function": {"name": "emit_outline", "description": "输出幕/章/节拍大纲", "parameters": _OUTLINE_TOOL_SCHEMA},
        }
    if step == "act":
        return {
            "type": "function",
            "function": {"name": "emit_act", "description": "输出单幕大纲（含本幕章节与节拍）", "parameters": _ACT_TOOL_SCHEMA},
        }
    if step == "premise":
        return {
            "type": "function",
            "function": {"name": "emit_premise", "description": "输出故事梗概设定", "parameters": _PREMISE_TOOL_SCHEMA},
        }
    if step == "style":
        return {
            "type": "function",
            "function": {"name": "emit_style", "description": "输出文风设定", "parameters": _STYLE_TOOL_SCHEMA},
        }
    if step == "worlds":
        return {
            "type": "function",
            "function": {
                "name": "emit_worlds",
                "description": "输出多条世界设定",
                "parameters": {
                    "type": "object",
                    "properties": {"worlds": {"type": "array", "items": _WORLD_TOOL_SCHEMA}},
                    "required": ["worlds"],
                },
            },
        }
    if step == "character":
        return {
            "type": "function",
            "function": {"name": "emit_character", "description": "输出一个完整人物设定", "parameters": _CHARACTER_TOOL_SCHEMA},
        }
    if step == "character_plan":
        return {
            "type": "function",
            "function": {
                "name": "emit_character_plan",
                "description": "输出人物名单：每人姓名、定位与一句话简介，不写详细设定",
                "parameters": _CHARACTER_PLAN_TOOL_SCHEMA,
            },
        }
    if step == "characters":
        return {
            "type": "function",
            "function": {
                "name": "emit_characters",
                "description": "输出人物设定：数量按需求自行判断（至少 1 名主角），不要凑数",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "characters": {"type": "array", "items": _CHARACTER_TOOL_SCHEMA},
                        "stock_characters": {
                            "type": "array",
                            "items": _STOCK_CHARACTER_TOOL_SCHEMA,
                            "description": "常用路人/龙套池，固定 6-10 个职业，供跨章复用音色",
                        },
                        "narrator_profile": {
                            "type": "object",
                            "properties": {"voice_profile": _VOICE_PROFILE_TOOL_SCHEMA},
                            "required": ["voice_profile"],
                            "description": "旁白音色描述（旁白是既有实体，这里只产出描述）",
                        },
                    },
                    "required": ["characters"],
                },
            },
        }
    return None


def _generate_json(adapter, req: TextGenRequest, step: str, on_delta=None, tool: dict | None = None) -> dict:
    """用 LLM 工具调用产出结构化参数（不再解析自由文本）。

    参数:
        adapter: 写作适配器（需实现 generate_tool）。
        req: 文本生成请求。
        step: 生成步骤（premise/style/world/worlds/character/characters），决定工具 schema。
        on_delta: 接收工具调用增量文本的回调（用于页面进度），可为 None。
        tool: 覆盖默认工具定义（扩充/续写会按目标数量加 minItems/maxItems），可为 None。

    返回:
        工具参数解析后的 dict。

    异常:
        ValueError: 未知步骤或适配器不支持工具调用。
        AdapterError: 模型未返回工具调用，或工具参数不合法。
    """
    tool = tool or _gen_tool(step)
    if tool is None:
        raise ValueError(f"未知的生成步骤: {step or '(空)'}")
    # 工具步骤自动归属思考调用点；调用点若已显式标注则不覆盖
    stage = step_stage(step)
    if stage and not (req.metadata or {}).get("thinking_stage"):
        req = replace(req, metadata={**(req.metadata or {}), "thinking_stage": stage})
    generate_tool = getattr(adapter, "generate_tool", None)
    if generate_tool is None:
        raise ValueError(f"写作适配器不支持工具调用: {type(adapter).__name__}")
    if on_delta is None:
        return generate_tool(req, tool)
    try:
        accepts = "on_delta" in inspect.signature(generate_tool).parameters
    except (TypeError, ValueError):
        accepts = False
    if accepts:
        return generate_tool(req, tool, on_delta=on_delta)
    return generate_tool(req, tool)


def _progress(on_progress, event: dict) -> None:
    """安全地上报生成进度（进度通道异常不影响生成本身）。

    参数:
        on_progress: 进度回调，可为 None。
        event: 事件字典，`type` 取 batch/chars。

    返回:
        无。
    """
    if on_progress is None:
        return
    try:
        on_progress(event)
    except Exception:  # 进度是旁路信息，不允许打断生成
        return


def _char_delta(state: dict, on_progress, index: int, total: int):
    """构造统计接收字数的增量回调。

    参数:
        state: 累积状态 `{"chars": n}`。
        on_progress: 进度回调。
        index: 当前批次序号（从 1 开始）。
        total: 批次总数。

    返回:
        `(text) -> None` 回调。
    """

    def on_delta(text):
        """累计增量并上报 chars 事件（附带一小段最新原文，供界面显示实时输出）。"""
        if not text:
            return
        state["chars"] = int(state.get("chars") or 0) + len(str(text))
        state["tail"] = (str(state.get("tail") or "") + str(text))[-200:]
        _progress(
            on_progress,
            {
                "type": "chars",
                "chars": state["chars"],
                "sample": state["tail"],
                "batch": index,
                "total": total,
            },
        )

    return on_delta


def _check_text(value, limit: int, label: str, required: bool = False) -> str:
    """校验并清洗字符串字段。

    参数:
        value: 原始值。
        limit: 最大长度（schema 上限）。
        label: 字段中文名，用于错误提示。
        required: 是否允许为空。

    返回:
        去空白后的字符串。

    异常:
        ValueError: 必填为空或超长。
    """
    text = str(value or "").strip()
    if required and not text:
        raise ValueError(f"{label}不能为空")
    if len(text) > limit:
        raise ValueError(f"{label}超过 {limit} 字上限（当前 {len(text)} 字）")
    return text


def _normalize_choice(value, options: tuple[str, ...]) -> str | None:
    """把枚举值归一成英文选项（兼容模型输出的中文别名）。

    参数:
        value: 原始值。
        options: 允许的取值。

    返回:
        归一后的枚举值；无法识别时返回 None。
    """
    text = str(value or "").strip()
    if text in options:
        return text
    # 模型可能输出「中长篇小说，约15-20万字」这类中文，按最长别名命中，避免「中长篇」被「长篇」抢走
    for alias, canonical in sorted(CHOICE_ALIASES.items(), key=lambda item: -len(item[0])):
        if alias in text and canonical in options:
            return canonical
    return None


def _check_choice(value, options: tuple[str, ...], label: str) -> str:
    """校验枚举字段。

    参数:
        value: 原始值。
        options: 允许的取值。
        label: 字段中文名。

    返回:
        归一后的枚举值。

    异常:
        ValueError: 取值不在枚举内。
    """
    normalized = _normalize_choice(value, options)
    if normalized is None:
        text = str(value or "").strip()
        raise ValueError(f"{label}取值非法: {text or '(空)'}（可选 {'/'.join(options)}）")
    return normalized


def _clean_str_list(rows, item_limit: int, label: str) -> list[str]:
    """清洗字符串数组（允许单个字符串自动成组）。

    参数:
        rows: 原始数组或字符串。
        item_limit: 单项最大长度。
        label: 字段中文名。

    返回:
        去空后的字符串列表。

    异常:
        ValueError: 类型错误或单项超长。
    """
    if rows is None:
        return []
    if isinstance(rows, str):
        # 模型有时把数组直接写成 JSON 字符串：先解析，避免整段 JSON 被当成一条规则
        text = rows.strip()
        if text[:1] in ("[", "{"):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, (list, dict)):
                return _clean_str_list(parsed, item_limit, label)
        rows = [rows]
    if isinstance(rows, dict):
        # 模型可能把规则/势力返回成单个对象或「名称→说明」映射，统一转成字符串数组
        if any(key in rows for key in ("name", "title", "description", "desc", "summary")):
            rows = [rows]
        else:
            rows = [
                f"{key}：{value}" if not isinstance(value, (dict, list)) and str(value).strip() else str(key)
                for key, value in rows.items()
            ]
    if not isinstance(rows, list):
        raise ValueError(f"{label}必须是字符串数组（实际收到 {type(rows).__name__}）")
    result = []
    for index, item in enumerate(rows):
        if isinstance(item, dict):
            # 对象项拼成「名称：说明」，不直接报错
            name = str(item.get("name") or item.get("title") or "").strip()
            note = str(item.get("description") or item.get("desc") or item.get("summary") or "").strip()
            item = f"{name}：{note}" if name and note else (name or note or "、".join(
                str(value).strip() for value in item.values() if not isinstance(value, (dict, list)) and str(value).strip()
            ))
        text = _check_text(item, item_limit, f"{label}第 {index + 1} 项")
        if text:
            result.append(text)
    return result


def _clean_setting_fields(step: str, payload: dict) -> dict:
    """按步骤清洗并校验生成字段。

    参数:
        step: premise / style。
        payload: 模型返回的 JSON 对象。

    返回:
        只包含该步骤允许字段的清洗结果。

    异常:
        ValueError: 字段类型、枚举或长度不合法。
    """
    if step == "premise":
        if "logline" not in payload:
            raise ValueError("logline 不能为空")
        fields = {"logline": _check_text(payload.get("logline"), 500, "logline", required=True)}
        for key in ("theme", "core_conflict", "ending_intent", "target_audience"):
            if key in payload:
                fields[key] = _check_text(payload.get(key), SCALAR_LIMITS[key], key)
        if "target_length" in payload:
            fields["target_length"] = _check_choice(payload.get("target_length"), LENGTH_OPTIONS, "target_length")
        return fields

    if step == "style":
        if "tone" not in payload:
            raise ValueError("tone 不能为空")
        fields = {"tone": _check_text(payload.get("tone"), 300, "tone", required=True)}
        if "pov" in payload:
            fields["pov"] = _check_choice(payload.get("pov"), POV_OPTIONS, "pov")
        if "tense" in payload:
            fields["tense"] = _check_choice(payload.get("tense"), TENSE_OPTIONS, "tense")
        if "prose_style" in payload:
            fields["prose_style"] = _check_text(payload.get("prose_style"), 1000, "prose_style")
        if "forbidden" in payload:
            fields["forbidden"] = _clean_str_list(payload.get("forbidden"), 200, "forbidden")
        if "visual_style" in payload:
            visual = _check_text(payload.get("visual_style"), 500, "visual_style")
            # 空串不写入，交给 _ensure_visual_style 补一次，避免覆盖/落空
            if visual:
                fields["visual_style"] = visual
        return fields

    raise ValueError(f"不支持的设定生成步骤: {step}")


def _bible_digest(bible: dict) -> dict:
    """整理写作设定（故事层）摘要，供提示词与增量生成使用。

    世界观与地点已归世界库，这里只保留故事内核与文风。

    参数:
        bible: 设定库 dict（可为空）。

    返回:
        {logline, theme, core_conflict, target_length, target_words, tone, pov}。
    """
    premise = bible.get("premise") or {}
    style = bible.get("style") or {}
    return {
        "logline": premise.get("logline"),
        "theme": premise.get("theme"),
        "core_conflict": premise.get("core_conflict"),
        "target_length": premise.get("target_length"),
        "target_words": premise.get("target_words"),
        "tone": style.get("tone"),
        "pov": style.get("pov"),
    }


def _project_context(
    work: dict,
    bible: dict,
    characters: list[dict] | None = None,
    worlds: list[dict] | None = None,
) -> str:
    """把项目基础信息拼成提示词上下文，保证生成内容不脱离小说设定。

    参数:
        work: 作品信息（title/author/genre）。
        bible: 设定库（premise/style）。
        characters: 已有角色表，用于避免重名并呼应既有关系。
        worlds: 世界库（每套含世界观总述与分类条目）。

    返回:
        多行上下文文本；一条信息都没有时返回空串。
    """
    premise = bible.get("premise") or {}
    style = bible.get("style") or {}
    lines: list[str] = []

    title = work.get("title") or "未命名"
    genres = "、".join(str(item) for item in (work.get("genre") or []) if item)
    lines.append(f"作品：{title}" + (f"（题材：{genres}）" if genres else ""))
    if premise.get("logline"):
        lines.append(f"一句话故事：{premise['logline']}")
    if premise.get("theme"):
        lines.append(f"主题：{premise['theme']}")
    if premise.get("core_conflict"):
        lines.append(f"核心冲突：{premise['core_conflict']}")
    if premise.get("ending_intent"):
        lines.append(f"结局意图：{premise['ending_intent']}")
    if premise.get("target_audience"):
        lines.append(f"目标读者：{premise['target_audience']}")
    if style.get("tone"):
        lines.append(f"叙事基调：{style['tone']}")
    if style.get("prose_style"):
        lines.append(f"文笔风格：{str(style['prose_style'])[:200]}")

    world = next((item for item in (worlds or []) if isinstance(item, dict)), None) or {}
    if world:
        if world.get("era"):
            lines.append(f"时代与舞台：{world['era']}")
        if world.get("summary"):
            lines.append(f"世界观总述：{str(world['summary'])[:600]}")
        rules = [str(item) for item in (world.get("rules") or []) if str(item).strip()]
        if rules:
            lines.append("世界规则：" + "；".join(rules[:6]))

        def _names(key: str, label: str, name_key: str) -> None:
            """把某一类条目压成名称列表追加到上下文。"""
            values = [
                str(item.get(name_key)).strip()
                for item in (world.get(key) or [])
                if isinstance(item, dict) and str(item.get(name_key) or "").strip()
            ]
            if values:
                lines.append(f"{label}：" + "、".join(values[:8]))

        _names("factions", "主要势力", "name")
        _names("locations", "关键地点", "name")
        _names("glossary", "专有名词", "term")
        _names("others", "其它设定", "name")

    names = [str(item.get("name")) for item in (characters or []) if item.get("name") and item.get("role") != "narrator"]
    if names:
        lines.append("已有角色（不要重名，可呼应既有关系）：" + "、".join(names[:20]))
    return "\n".join([line for line in lines if line.strip()])


def _worlds_digest(worlds: list[dict], ids: list[str]) -> str | None:
    """把选中世界条目拼成章节上下文文本（委托 writing/worlds.worlds_digest）。

    参数:
        worlds: 世界条目列表。
        ids: 本章要注入的世界 ID 列表。

    返回:
        多行上下文文本；ids 为空或一个都没命中时返回 None。
    """
    return worlds_digest(worlds, ids)


def _is_meaningful(current, baseline, key: str = "") -> bool:
    """判断已有值是否算「用户真实内容」（骨架占位值视为空）。

    只有 logline / era / tone 这类骨架占位字段会与默认值比较，
    其余字段只要非空就保留（避免未经用户确认被生成结果覆盖）。

    参数:
        current: 设定库里的当前值。
        baseline: 设定库骨架里的默认值。
        key: 字段名。

    返回:
        True 表示应当保留，不被生成结果覆盖。
    """
    if current is None:
        return False
    if isinstance(current, list):
        return len(current) > 0
    if isinstance(current, dict):
        return bool(current)
    text = str(current).strip()
    if not text:
        return False
    if key in ("logline", "era", "tone"):
        if "待填写" in text:
            return False
        if baseline is not None and text == str(baseline).strip():
            return False
    return True


def generate_worlds(
    store: ProjectStore,
    config: AppConfig,
    count: int | None = None,
    overwrite: bool = False,
    brief: str | None = None,
    batch_size: int | None = None,
    on_progress=None,
) -> dict:
    """一次生成 1..6 条世界条目（只返回预览，不落盘）。

    参数:
        store: 工程。
        config: 本机配置。
        count: 期望世界条数（1..6）；为空时由适配器决定。
        overwrite: True 表示调用方打算覆盖已有世界；本函数仍只预览，仅在返回值里标记。
        brief: 用户补充要求（拼进提示词与请求元信息）。
        batch_size: 每批条数；None 时取 `writing.multi_turn.batch_size_worlds`（默认 2）。
        on_progress: 进度回调（batch/chars 事件）；仅页面流式接口传入。

    返回:
        {"step": "worlds", "worlds": [...], "count", "brief", "overwrite", "provider", "batches"}；
        worlds 为清洗后通过 world schema 的完整条目（可直接交给 WorldStore.add）。

    异常:
        ValueError: count 越界、模型输出不是合法 JSON 或世界字段不合法。
    """
    if count is not None:
        try:
            wanted = int(count)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"世界数量必须是 1..6 的整数：{count}") from exc
        if wanted < 1 or wanted > 6:
            raise ValueError(f"世界数量必须在 1..6 之间：{wanted}")
    else:
        wanted = None

    settings_store = SettingsStore(store.root)
    bible = settings_store.load_or_none() or {}
    project = store.load_project()
    work = project.get("work") or {}
    adapter = build_writing_adapter(config, bible)
    brief_text = str(brief or "").strip()

    world_store = WorldStore(store.root)
    world_items = world_store.list_worlds() if world_store.exists() else []
    existing = [{"name": item.get("name"), "era": item.get("era")} for item in world_items]
    ask = "生成世界设定列表，每条包含 name/era/summary/rules/factions/locations/glossary/others/visual_style；locations/glossary/others 必须是数组，元素为对象，不要用字符串包裹。summary 写世界观总述，factions/locations/glossary 是总述之下的分类条目。"
    if wanted:
        ask += f"共 {wanted} 条世界。"
    if brief_text:
        ask += f"补充要求：{brief_text}。"
    # 批量世界同样要贴合作品既有梗概、文风与已建世界，避免自造独立宇宙
    context = _project_context(work, bible, None, world_items)
    prompt = f"{context}\n\n任务：{ask}" if context else ask
    req = TextGenRequest(
        task="setting",
        system="你是中文小说的世界观编辑，世界设定必须与给定的作品设定一致，只按工具参数输出，不要解释。",
        messages=[Message("user", prompt)],
        stream=True,
        temperature=resolve_temperature(config, "writing"),
        metadata={
            "step": "worlds",
            "work": {"title": work.get("title"), "author": work.get("author"), "genre": work.get("genre") or []},
            "existing": _bible_digest(bible),
            "existing_worlds": existing,
            "count": wanted,
            "brief": brief_text,
            "overwrite": bool(overwrite),
            "seed": work.get("id"),
        },
    )
    mt = config.get("writing.multi_turn") or {}
    try:
        batch = int(batch_size or mt.get("batch_size_worlds") or 2)
    except (TypeError, ValueError):
        batch = 2
    batch = max(1, batch)

    rows: list = []
    batches = 1
    progress_state = {"chars": 0}
    if wanted and wanted > batch:
        # 数量超过单批上限：按批生成，每批在固定前缀后追加本批要求与已生成名单
        batches = (wanted + batch - 1) // batch
        names: list[str] = []
        for index in range(batches):
            take = min(batch, wanted - len(rows))
            if take <= 0:
                break
            progress_index = index + 1
            _progress(on_progress, {"type": "batch", "index": progress_index, "total": batches, "unit": "batch"})
            lines = [f"本批生成 {take} 条世界（第 {index + 1}/{batches} 批）。"]
            if names:
                lines.append("已生成世界（不要重复名称）：" + "、".join(names))
            batch_req = replace(req, messages=list(req.messages) + [Message("user", "\n".join(lines))])
            batch_req.metadata = dict(req.metadata or {}, count=take)
            batch_payload = _generate_json(
                adapter,
                batch_req,
                "worlds",
                on_delta=_char_delta(progress_state, on_progress, progress_index, batches),
            )
            batch_rows = batch_payload.get("worlds")
            if isinstance(batch_rows, dict):
                batch_rows = [batch_rows]
            if not isinstance(batch_rows, list):
                continue
            for row in batch_rows:
                if not isinstance(row, dict):
                    continue
                name = str(row.get("name") or "").strip()
                if name and name in names:
                    continue
                if name:
                    names.append(name)
                rows.append(row)
    else:
        _progress(on_progress, {"type": "batch", "index": 1, "total": 1})
        payload = _generate_json(
            adapter,
            req,
            "worlds",
            on_delta=_char_delta(progress_state, on_progress, 1, 1),
        )
        rows = payload.get("worlds")
        if isinstance(rows, dict):
            rows = [rows]
    if not isinstance(rows, list) or not rows:
        raise ValueError("LLM 返回的世界列表为空")
    if wanted:
        rows = rows[:wanted]
    worlds = [clean_world_entry(row) for row in rows]
    return {
        "step": "worlds",
        "worlds": worlds,
        "count": len(worlds),
        "brief": brief_text or None,
        "overwrite": bool(overwrite),
        "provider": str(config.get("adapters.writing") or config.get("providers.writing") or "offline"),
        "batches": batches,
    }


def _ensure_visual_style(adapter, req: TextGenRequest, fields: dict) -> None:
    """确保文风字段 visual_style 有实际内容。

    模型在工具调用里常省略非必填的 visual_style，导致前端「视觉风格」显示空白。
    这里先做一次只补该字段的定向生成；仍失败时用已生成的基调/文笔拼出可读内容兜底。

    参数:
        adapter: 写作适配器。
        req: 原始文风生成请求（复用上下文与元信息）。
        fields: 已清洗的文风字段（就地修改）。

    返回:
        无。
    """
    if str(fields.get("visual_style") or "").strip():
        return
    ask = (
        "只补充 visual_style：用 120-300 字写可拍摄的画面风格，必须覆盖场景、光线、色彩、"
        "材质与镜头质感；不要留空，不要写「暂无」「略」这类占位内容。"
    )
    try:
        retry = replace(req, messages=list(req.messages) + [Message("user", ask)])
        payload = _generate_json(adapter, retry, "style")
        if isinstance(payload.get("style"), dict):
            payload = payload["style"]
        visual = _check_text(payload.get("visual_style"), 500, "visual_style")
        if visual:
            fields["visual_style"] = visual
            return
    except Exception:  # noqa: BLE001 - 定向补一次失败时走兜底，不影响整次生成
        pass
    fallback = "；".join(
        part for part in (str(fields.get("tone") or "").strip(), str(fields.get("prose_style") or "").strip()) if part
    )
    if fallback:
        fields["visual_style"] = fallback[:500]


def generate_settings(store: ProjectStore, config: AppConfig, step: str, overwrite: bool = False, brief: str | None = None, seed: int | None = None) -> dict:
    """生成梗概 / 文风设定（只返回结果，不写盘）。

    参数:
        store: 工程。
        config: 本机配置。
        step: premise|style。
        overwrite: True 时用生成值整体替换该分区；False 时保留已有非空值并记入 skipped。
        brief: 可选的用户补充要求（拼进提示词与请求元信息）。
        seed: 显式固定随机种子；None 表示每次由服务端随机采样（默认）。

    返回:
        {"step", "fields", "skipped"}。

    异常:
        ValueError: step 非法、模型输出不是合法 JSON 或字段校验失败。
    """
    if step not in SETTING_STEPS:
        raise ValueError(f"未知设定步骤: {step}（可选 premise / style）")
    settings_store = SettingsStore(store.root)
    bible = settings_store.load_or_none() or {}
    project = store.load_project()
    work = project.get("work") or {}
    skeleton = settings_store.build_skeleton(work)
    adapter = build_writing_adapter(config, bible)

    label = {"premise": "故事梗概", "style": "文风设定"}[step]
    allowed = {"premise": PREMISE_FIELDS, "style": STYLE_FIELDS}[step]
    target_length = (bible.get("premise") or {}).get("target_length") or (skeleton.get("premise") or {}).get("target_length")
    brief_text = str(brief or "").strip()
    ask = f"为《{work.get('title') or '未命名'}》生成{label}，字段：{', '.join(allowed)}。"
    if step == "style":
        # visual_style 常被模型省略：明确要求写具体、非空，避免前端回显空白
        ask += "其中 visual_style 必填：写 120-300 字具体画面风格（场景、光线、色彩、材质与镜头质感），不能留空。"
    if brief_text:
        ask += f"补充要求：{brief_text}。"
    # 生成某个分区时，把其他已完成的分区与已有世界作为上下文，避免设定互相矛盾
    context_bible = dict(bible)
    context_bible[step] = {}
    world_store = WorldStore(store.root)
    world_items = world_store.list_worlds() if world_store.exists() else []
    context = _project_context(work, context_bible, None, world_items)
    prompt = f"{context}\n\n任务：{ask}" if context else ask
    metadata = {
        "step": step,
        "work": {"title": work.get("title"), "author": work.get("author"), "genre": work.get("genre") or []},
        "existing": _bible_digest(bible),
        "target_length": target_length,
        "brief": brief_text,
        "work_id": work.get("id"),
    }
    # 只有用户显式固定种子时才下发，默认交给服务端随机采样（同一设定重复生成应有差异）
    if seed is not None:
        metadata["extra_body"] = {"seed": int(seed)}
    req = TextGenRequest(
        task="setting",
        system="你是中文小说的设定编辑，设定必须与给定的作品信息一致，只按工具参数输出，不要解释。",
        messages=[Message("user", prompt)],
        stream=True,
        temperature=resolve_temperature(config, "writing"),
        metadata=metadata,
    )
    payload = _generate_json(adapter, req, step)
    if isinstance(payload.get(step), dict):
        payload = payload[step]
    fields = _clean_setting_fields(step, payload)
    if step == "style":
        # 「只补缺失」且已有非空视觉风格时无需额外补写，避免多一次模型调用
        existing_visual = str((bible.get("style") or {}).get("visual_style") or "").strip()
        if overwrite or not existing_visual:
            _ensure_visual_style(adapter, req, fields)

    existing_section = bible.get(step) or {}
    baseline_section = skeleton.get(step) or {}
    final: dict = {}
    skipped: list[str] = []
    for key, value in fields.items():
        current = existing_section.get(key)
        if not overwrite and _is_meaningful(current, baseline_section.get(key), key):
            final[key] = current
            skipped.append(key)
        else:
            final[key] = value
    return {"step": step, "fields": final, "skipped": skipped}


def generate_characters(
    store: ProjectStore,
    config: AppConfig,
    count: int | None = None,
    brief: str | None = None,
    batch_size: int | None = None,
    on_progress=None,
) -> dict:
    """生成人物设定（只返回结果，不写盘）。

    参数:
        store: 工程。
        config: 本机配置。
        count: 期望人物总数；为空时由模型按需求自行判断需要几个角色。
        brief: 用户补充要求（拼进提示词，离线实现会写进人物小传/性格）。
        batch_size: 每批条数；None 时取 `writing.multi_turn.batch_size_characters`（默认 5）。
        on_progress: 进度回调（batch/chars 事件）；仅页面流式接口传入。

    返回:
        {"characters": [{name, role, gender, age_range, personality, background,
        appearance_prompt, negative_prompt, relationships}], "batches": N}。

    异常:
        ValueError: 模型输出不是合法 JSON 或重名。
    """
    settings_store = SettingsStore(store.root)
    bible = settings_store.load_or_none() or {}
    project = store.load_project()
    work = project.get("work") or {}
    existing_characters = store.load_characters()["characters"]
    world_store = WorldStore(store.root)
    worlds = world_store.list_worlds() if world_store.exists() else []
    adapter = build_writing_adapter(config, bible)
    brief_text = str(brief or "").strip()

    ask = f"为《{work.get('title') or '未命名'}》生成人物设定：根据需求自行判断需要几个角色（至少 1 名主角，其余按剧情需要决定数量与定位），不要凑数。"
    if count:
        ask += f"本次共生成 {count} 个角色。"
    if brief_text:
        ask += f"补充要求：{brief_text}。"
    ask += "\n" + _CHARACTER_DESC_RULES
    # 无论需求怎么写，都先把作品梗概/文风/世界/已有角色交给模型，避免脱离设定
    context = _project_context(work, bible, existing_characters, worlds)
    prompt = f"{context}\n\n任务：{ask}" if context else ask
    mt = config.get("writing.multi_turn") or {}
    try:
        batch = int(batch_size or mt.get("batch_size_characters") or 5)
        total = int(count) if count else 0
    except (TypeError, ValueError):
        batch = 5
        total = 0
    batch = max(1, batch)

    req = TextGenRequest(
        task="character",
        system="你是中文小说的人物编辑，人物必须与给定的作品设定一致，只按工具参数输出，不要解释。",
        messages=[Message("user", prompt)],
        stream=True,
        temperature=resolve_temperature(config, "writing"),
        metadata={
            "work": {"title": work.get("title"), "author": work.get("author"), "genre": work.get("genre") or []},
            "premise": bible.get("premise") or {},
            "world": _bible_digest(bible),
            "count": count,
            "brief": brief_text,
            "seed": work.get("id"),
        },
    )

    batches = 1
    stock_rows: list[dict] = []
    narrator_profile_raw: dict | None = None
    last_payload: dict = {}
    progress_state = {"chars": 0}
    if total > batch:
        # 数量超过单批上限：按批生成，每批在固定前缀后追加本批要求与已生成名单
        batches = (total + batch - 1) // batch
        rows: list = []
        names: list[str] = []
        for index in range(batches):
            take = min(batch, total - len(rows))
            if take <= 0:
                break
            progress_index = index + 1
            _progress(on_progress, {"type": "batch", "index": progress_index, "total": batches, "unit": "batch"})
            lines = [f"本批生成 {take} 条人物（第 {index + 1}/{batches} 批）。"]
            if names:
                lines.append("已生成人物（不要重复姓名）：" + "、".join(names))
            batch_req = replace(req, messages=list(req.messages) + [Message("user", "\n".join(lines))])
            batch_req.metadata = dict(req.metadata or {}, count=take)
            batch_payload = _generate_json(
                adapter,
                batch_req,
                "characters",
                on_delta=_char_delta(progress_state, on_progress, progress_index, batches),
            )
            last_payload = batch_payload
            if not stock_rows and isinstance(batch_payload.get("stock_characters"), list):
                stock_rows = [row for row in batch_payload["stock_characters"] if isinstance(row, dict)]
            if narrator_profile_raw is None and isinstance(batch_payload.get("narrator_profile"), dict):
                narrator_profile_raw = batch_payload["narrator_profile"]
            batch_rows = batch_payload.get("characters")
            if not isinstance(batch_rows, list):
                continue
            for row in batch_rows:
                if not isinstance(row, dict):
                    continue
                name = str(row.get("name") or "").strip()
                if name and name in names:
                    continue
                if name:
                    names.append(name)
                rows.append(row)
    else:
        _progress(on_progress, {"type": "batch", "index": 1, "total": 1})
        payload = _generate_json(
            adapter,
            req,
            "characters",
            on_delta=_char_delta(progress_state, on_progress, 1, 1),
        )
        last_payload = payload
        rows = payload.get("characters")
        if isinstance(payload.get("stock_characters"), list):
            stock_rows = [row for row in payload["stock_characters"] if isinstance(row, dict)]
        if isinstance(payload.get("narrator_profile"), dict):
            narrator_profile_raw = payload["narrator_profile"]
    if not isinstance(rows, list) or not rows:
        # 带上响应顶层字段与摘录，便于区分「模型不支持 tools / 字段名不对 / 数组为空」
        keys = "、".join(sorted(str(key) for key in last_payload.keys())) or "（空对象）"
        excerpt = json.dumps(last_payload, ensure_ascii=False)[:300]
        raise ValueError(
            f"LLM 返回的人物列表为空：响应顶层字段为 {keys}，未包含非空的 characters 数组；"
            f"响应摘录：{excerpt}"
        )

    characters: list[dict] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        entry = _clean_character_row(row, index)
        name = entry["name"]
        if name in seen:
            raise ValueError(f"人物姓名重复: {name}")
        seen.add(name)
        characters.append(entry)

    stock_characters = _build_stock_characters(stock_rows, seen)
    narrator_profile = None
    if narrator_profile_raw:
        profile = _clean_voice_profile(narrator_profile_raw.get("voice_profile"))
        if profile:
            narrator_profile = {"voice_profile": _attach_profile_source({"name": "旁白"}, profile)}
    if not stock_characters:
        stock_characters = _default_stock_characters()

    # 角色数量与定位由需求决定，这里只保证至少有一个主角，不再限定配角人数
    if not any(item["role"] == "protagonist" for item in characters):
        raise ValueError("人物里至少需要 1 个 protagonist（主角）")
    return {
        "characters": characters,
        "stock_characters": stock_characters,
        "narrator_profile": narrator_profile,
        "batches": batches,
    }


def _clean_age_range(value, owner: str) -> str | None:
    """清洗年龄段：只接受分档枚举，模型给出的数字区间按中点归入分档。

    参数:
        value: 模型返回的年龄段（可能是「18-24」「30s」「青年」等写法）。
        owner: 角色名的中文描述，用于报错，如「人物 张三」。

    返回:
        合法分档值；未填写返回 None。

    异常:
        ValueError: 无法归入任何分档（消息含可选值）。
    """
    raw = _check_text(value, 40, f"{owner} 的年龄段")
    if not raw:
        return None
    age = AgeRange.normalize(raw)
    if age is None:
        raise ValueError(f"{owner} 的年龄段「{raw}」无法归入分档；可选值：{'/'.join(AgeRange.values())}")
    return age


def _clean_speech_style(value) -> dict | None:
    """清洗人物说话方式（写作时直接注入提示词）。

    参数:
        value: 模型返回的 speech_style 对象，或旧数据。

    返回:
        `{tempo,pitch,accent,catchphrases}`；四项全空时返回 None。
    """
    if not isinstance(value, dict):
        return None
    raw_tempo = _check_text(value.get("tempo"), 40, "说话方式的语速")
    raw_pitch = _check_text(value.get("pitch"), 40, "说话方式的音高")
    tempo = SpeechTempo.normalize(raw_tempo) if raw_tempo else None
    pitch = SpeechPitch.normalize(raw_pitch) if raw_pitch else None
    if raw_tempo and tempo is None:
        raise ValueError(f"说话方式的语速「{raw_tempo}」不在可选范围；只能取 { '/'.join(SpeechTempo.values()) }")
    if raw_pitch and pitch is None:
        raise ValueError(f"说话方式的音高「{raw_pitch}」不在可选范围；只能取 { '/'.join(SpeechPitch.values()) }")
    clean = {
        "tempo": tempo,
        "pitch": pitch,
        "accent": _check_text(value.get("accent"), 40, "说话方式的口音"),
        "catchphrases": _clean_str_list(value.get("catchphrases"), 60, "说话方式的口头禅")[:3],
    }
    if not any([clean["tempo"], clean["pitch"], clean["accent"], clean["catchphrases"]]):
        return None
    return clean


def _clean_character_row(row, index: int) -> dict:
    """清洗单个 LLM 人物条目（重名由调用方判断）。

    参数:
        row: 模型返回的单个角色对象。
        index: 该角色在本次结果中的序号（从 0 开始，用于错误提示）。

    返回:
        清洗后的角色 dict（字段与 characters.json 对齐）。

    异常:
        ValueError: 条目不是对象或必填字段缺失。
    """
    if not isinstance(row, dict):
        raise ValueError(f"第 {index + 1} 个人物必须是对象")
    name = _check_text(row.get("name"), 60, f"第 {index + 1} 个人物姓名", required=True)
    role = _normalize_choice(row.get("role"), ROLE_OPTIONS) or "supporting"
    gender = _normalize_choice(row.get("gender"), GENDER_OPTIONS) or "unknown"
    relationships = row.get("relationships") or {}
    if not isinstance(relationships, dict):
        relationships = {}
    entry = {
        "name": name,
        "role": role,
        "gender": gender,
        "age_range": _clean_age_range(row.get("age_range"), f"人物 {name}"),
        "personality": _clean_str_list(row.get("personality"), 120, f"人物 {name} 的性格"),
        "background": _check_text(row.get("background"), 2000, f"人物 {name} 的背景"),
        "speech_style": _clean_speech_style(row.get("speech_style")),
        "appearance_prompt": _check_text(row.get("appearance_prompt"), 2000, f"人物 {name} 的外貌提示词"),
        "negative_prompt": _check_text(row.get("negative_prompt"), 1000, f"人物 {name} 的负向提示词"),
        "voice_description_zh": _check_text(row.get("voice_description_zh"), 1000, f"人物 {name} 的中文音色描述"),
        "voice_description_en": _check_text(row.get("voice_description_en"), 1000, f"人物 {name} 的英文音色描述"),
        "appearance_desc_zh": _check_text(row.get("appearance_desc_zh"), 1200, f"人物 {name} 的中文外貌描述"),
        "appearance_desc_en": _check_text(row.get("appearance_desc_en"), 1200, f"人物 {name} 的英文外貌描述"),
        "appearance_negative_zh": _check_text(row.get("appearance_negative_zh"), 800, f"人物 {name} 的中文负向说明"),
        "appearance_negative_en": _check_text(row.get("appearance_negative_en"), 800, f"人物 {name} 的英文负向提示词"),
        "relationships": {
            _check_text(key, 60, f"人物 {name} 的关系对象", required=True): _check_text(note, 500, f"人物 {name} 的关系备注")
            for key, note in relationships.items()
        },
    }
    profile = _clean_voice_profile(row.get("voice_profile"))
    if profile:
        entry["voice_profile"] = _attach_profile_source(entry, profile)
    stock = _clean_stock(row.get("stock")) if role == "minor" else None
    if stock:
        entry["stock"] = stock
        entry["name"] = _stock_display_name(entry["name"], stock["label"])
    # 新旧字段兼容：已有消费端只看 appearance_prompt / negative_prompt
    if not entry["appearance_prompt"] and entry["appearance_desc_en"]:
        entry["appearance_prompt"] = entry["appearance_desc_en"]
    if not entry["negative_prompt"] and entry["appearance_negative_en"]:
        entry["negative_prompt"] = entry["appearance_negative_en"]
    return entry


_VOICE_PROFILE_TEXT_LIMITS = {
    "voice_summary": 300,
    "age_range": 40,
    "age_feel": 20,
    "pitch": 20,
    "pace": 20,
    "volume": 20,
    "energy": 30,
    "warmth": 30,
    "articulation": 30,
    "accent": 60,
    "design_prompt": 1200,
}

#: 龙套默认槽位（LLM 未产出时的兜底；职业槽位与音色池分离）
_DEFAULT_STOCK_SLOTS = (
    ("waiter", "店小二", "male_mid"),
    ("guard", "衙役", "male_mid"),
    ("runner", "小厮", "male_young"),
    ("passerby_m", "路人男", "male_mid"),
    ("passerby_f", "路人女", "female_mid"),
    ("elder_m", "老者", "male_elder"),
    ("elder_f", "老妇", "female_elder"),
    ("girl", "少女", "female_young"),
)


def _clean_voice_profile(row) -> dict | None:
    """清洗 LLM 产出的音色描述（只保留已知字段，长度受限）。

    参数:
        row: 模型返回的 voice_profile 对象。

    返回:
        清洗后的 dict；空对象返回 None。
    """
    if not isinstance(row, dict):
        return None
    profile: dict = {}
    for key, limit in _VOICE_PROFILE_TEXT_LIMITS.items():
        value = _check_text(row.get(key), limit, f"音色描述 {key}")
        if value:
            profile[key] = value
    for key in ("timbre", "temperament", "negative_prompt", "evidence"):
        items = _clean_str_list(row.get(key), 60, f"音色描述 {key}")
        if items:
            profile[key] = items
    gender = _normalize_choice(row.get("gender"), GENDER_OPTIONS)
    if gender:
        profile["gender"] = gender
    confidence = row.get("confidence")
    if isinstance(confidence, (int, float)) and 0 <= confidence <= 1:
        profile["confidence"] = float(confidence)
    return profile or None


def _attach_profile_source(character: dict, profile: dict) -> dict:
    """补齐音色描述的元数据（schema/summary/来源哈希/状态）。"""
    from ..store.voice_cast import profile_source_hash

    profile["schema"] = "voice_profile/1"
    profile.setdefault("summary", profile.get("voice_summary"))
    profile["source"] = {"kind": "llm", "at": now_iso(), "from_hash": profile_source_hash(character)}
    profile["status"] = "ready"
    return profile


def _stock_voice_slot(gender: str | None, age_range: str | None) -> str:
    """按性别与年龄感给出音色池槽位。"""
    age = str(age_range or "")
    elder = any(word in age for word in ("60", "70", "80", "老"))
    young = any(word in age for word in ("16", "17", "18", "19", "20", "21", "22", "少年", "少女"))
    if gender == "female":
        return "female_elder" if elder else ("female_young" if young else "female_mid")
    if gender == "male":
        return "male_elder" if elder else ("male_young" if young else "male_mid")
    return "neutral_mid"


def _clean_stock(row) -> dict | None:
    """清洗龙套槽位；非法槽位返回 None（由调用方决定是否降级）。"""
    if not isinstance(row, dict):
        return None
    label = _check_text(row.get("label"), 40, "龙套职业名", required=True)
    slot = str(row.get("slot") or "").strip().lower()
    if not re.match(r"^[a-z][a-z0-9_]{1,39}$", slot):
        return None
    return {
        "slot": slot,
        "voice_slot": str(row.get("voice_slot") or "").strip() or None,
        "label": label,
        "archetype_tags": _clean_str_list(row.get("archetype_tags"), 20, f"龙套 {label} 的标签"),
        "reuse_policy": "reuse",
    }


def _stock_display_name(name: str, label: str) -> str:
    """统一龙套展示名：`龙套·<职业>`。"""
    if name.startswith("龙套"):
        return name
    return f"龙套·{label}"


def _build_stock_characters(rows: list[dict], seen: set[str]) -> list[dict]:
    """把 LLM 的 stock_characters 清洗成角色实体（同槽位只保留第一条）。

    参数:
        rows: 模型返回的龙套条目。
        seen: 已占用的人物姓名集合（会被更新）。

    返回:
        角色 dict 列表。
    """
    built: list[dict] = []
    used_slots: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        stock = _clean_stock(row.get("stock"))
        if not stock or stock["slot"] in used_slots:
            continue
        used_slots.add(stock["slot"])
        name = _stock_display_name(str(row.get("name") or "").strip(), stock["label"])
        if name in seen:
            continue
        seen.add(name)
        gender = _normalize_choice(row.get("gender"), GENDER_OPTIONS) or "unknown"
        stock["voice_slot"] = stock.get("voice_slot") or _stock_voice_slot(gender, row.get("age_range"))
        character = {
            "name": name,
            "role": "minor",
            "gender": gender,
            "age_range": _clean_age_range(row.get("age_range"), f"龙套 {name}"),
            "personality": _clean_str_list(row.get("personality"), 120, f"龙套 {name} 的性格"),
            "background": _check_text(row.get("background"), 1200, f"龙套 {name} 的背景"),
            "appearance_prompt": _check_text(row.get("appearance_prompt"), 1200, f"龙套 {name} 的外貌提示词"),
            "negative_prompt": _check_text(row.get("negative_prompt"), 800, f"龙套 {name} 的负向提示词"),
            "relationships": {},
            "stock": stock,
        }
        profile = _clean_voice_profile(row.get("voice_profile"))
        if profile:
            character["voice_profile"] = _attach_profile_source(character, profile)
        built.append(character)
    return built


def _default_stock_characters() -> list[dict]:
    """LLM 未产出龙套时的默认 8 个职业槽位（无音色描述，由校验提醒补齐）。"""
    return [
        {
            "name": f"龙套·{label}",
            "role": "minor",
            "gender": "female" if voice_slot.startswith("female") else ("male" if voice_slot.startswith("male") else "unknown"),
            "age_range": None,
            "personality": [],
            "background": None,
            "appearance_prompt": None,
            "negative_prompt": None,
            "relationships": {},
            "stock": {
                "slot": slot,
                "voice_slot": voice_slot,
                "label": label,
                "archetype_tags": [],
                "reuse_policy": "reuse",
            },
        }
        for slot, label, voice_slot in _DEFAULT_STOCK_SLOTS
    ]


def _clean_character_entry(row: dict) -> dict:
    """清洗单个完整人物（含配图提示词与关系备注）。

    参数:
        row: 模型返回的人物对象。

    返回:
        {name, role, gender, age_range, personality, background, appearance_prompt,
        negative_prompt, state.relationship_notes}。

    异常:
        ValueError: 类型或字段长度不合法。
    """
    if not isinstance(row, dict):
        raise ValueError("人物必须是 JSON 对象")
    name = _check_text(row.get("name"), 60, "人物姓名", required=True)
    role = _normalize_choice(row.get("role"), ROLE_OPTIONS) or "supporting"
    gender = _normalize_choice(row.get("gender"), GENDER_OPTIONS) or "unknown"
    state = row.get("state") if isinstance(row.get("state"), dict) else {}
    notes = state.get("relationship_notes")
    if not isinstance(notes, dict):
        notes = row.get("relationships") if isinstance(row.get("relationships"), dict) else {}
    entry = {
        "name": name,
        "role": role,
        "gender": gender,
        "age_range": _clean_age_range(row.get("age_range"), f"人物 {name}"),
        "personality": _clean_str_list(row.get("personality"), 120, f"人物 {name} 的性格"),
        "background": _check_text(row.get("background"), 2000, f"人物 {name} 的背景"),
        "speech_style": _clean_speech_style(row.get("speech_style")),
        "appearance_prompt": _check_text(row.get("appearance_prompt"), 2000, f"人物 {name} 的外貌提示词"),
        "negative_prompt": _check_text(row.get("negative_prompt"), 1000, f"人物 {name} 的负向提示词"),
        "voice_description_zh": _check_text(row.get("voice_description_zh"), 1000, f"人物 {name} 的中文音色描述"),
        "voice_description_en": _check_text(row.get("voice_description_en"), 1000, f"人物 {name} 的英文音色描述"),
        "appearance_desc_zh": _check_text(row.get("appearance_desc_zh"), 1200, f"人物 {name} 的中文外貌描述"),
        "appearance_desc_en": _check_text(row.get("appearance_desc_en"), 1200, f"人物 {name} 的英文外貌描述"),
        "appearance_negative_zh": _check_text(row.get("appearance_negative_zh"), 800, f"人物 {name} 的中文负向说明"),
        "appearance_negative_en": _check_text(row.get("appearance_negative_en"), 800, f"人物 {name} 的英文负向提示词"),
        "state": {
            "relationship_notes": {
                _check_text(key, 60, f"人物 {name} 的关系对象", required=True): _check_text(note, 500, f"人物 {name} 的关系备注")
                for key, note in notes.items()
            }
        },
    }
    # 新旧字段兼容：已有消费端只看 appearance_prompt / negative_prompt
    if not entry["appearance_prompt"] and entry["appearance_desc_en"]:
        entry["appearance_prompt"] = entry["appearance_desc_en"]
    if not entry["negative_prompt"] and entry["appearance_negative_en"]:
        entry["negative_prompt"] = entry["appearance_negative_en"]
    return entry


def _section_fields(section: str, generated: dict, bible: dict) -> dict:
    """补齐一次生成区块的全部字段（生成值优先，回退已有值/空值）。

    参数:
        section: premise / style。
        generated: generate_settings 返回的字段。
        bible: 当前设定库。

    返回:
        该区块全部字段的 dict。
    """
    allowed = PREMISE_FIELDS if section == "premise" else STYLE_FIELDS
    existing = bible.get(section) or {}
    fields: dict = {}
    for key in allowed:
        value = generated.get(key)
        if value is None:
            value = existing.get(key)
        if value is None:
            if key == "forbidden":
                value = []
            elif key == "target_length":
                value = "novella"
            else:
                value = ""
        fields[key] = value
    return fields


def generate_section(
    store: ProjectStore,
    config: AppConfig,
    section: str,
    brief: str | None = None,
    target_id: str | None = None,
    seed: int | None = None,
    overwrite: bool = True,
) -> dict:
    """一次生成一个区块的全部字段（梗概 / 文风 / 单条世界 / 单个人物）。

    参数:
        store: 工程。
        config: 本机配置。
        section: premise|style|world|character。
        brief: 用户补充要求。
        target_id: world 用世界 ID、character 用角色 ID；命中时作为改写上下文。
        seed: 显式固定随机种子；None 表示每次随机采样。
        overwrite: True 时重新生成并覆盖已有字段；False 时保留已有非空值（只补缺失）。

    返回:
        {"section", "fields", "target_id"?}；world/character 的 fields 为完整条目。

    异常:
        ValueError: section 非法、目标条目不存在或模型输出不合法。
    """
    key = str(section or "").strip()
    if key not in ("premise", "style", "character"):
        raise ValueError(f"不支持的生成区块: {section}")

    settings_store = SettingsStore(store.root)
    bible = settings_store.load_or_none() or {}
    if key in ("premise", "style"):
        # overwrite=True（重新生成）时关闭「只补缺失」，已有字段也会被新结果覆盖；
        # overwrite=False（只补缺失）时保留已有非空值，模型只补空缺。
        result = generate_settings(store, config, key, overwrite=overwrite, brief=brief, seed=seed)
        return {
            "section": key,
            "fields": _section_fields(key, result.get("fields") or {}, bible),
            "skipped": result.get("skipped") or [],
        }

    project = store.load_project()
    work = project.get("work") or {}
    adapter = build_writing_adapter(config, bible)
    brief_text = str(brief or "").strip()
    work_payload = {"title": work.get("title"), "author": work.get("author"), "genre": work.get("genre") or []}

    characters = store.load_characters()["characters"]
    world_store = WorldStore(store.root)
    worlds = world_store.list_worlds() if world_store.exists() else []
    existing_char = next((item for item in characters if item.get("id") == target_id), None) if target_id else None
    if target_id and existing_char is None:
        raise ValueError(f"角色不存在: {target_id}")
    ask = "生成一个完整人物设定，字段：name/role/gender/age_range/personality/background/appearance_prompt/negative_prompt/state.relationship_notes；personality 写 3-5 个短标签，每个不超过 20 字。\n" + _CHARACTER_DESC_RULES
    if existing_char:
        ask += f"这是对已有角色「{existing_char.get('name')}」的改写，保持与现有设定的连贯。"
    if brief_text:
        ask += f"补充要求：{brief_text}。"
    # 单人物同样注入项目设定，避免脱离小说背景
    context = _project_context(work, bible, characters, worlds)
    prompt = f"{context}\n\n任务：{ask}" if context else ask
    req = TextGenRequest(
        task="character",
        system="你是中文小说的人物编辑，人物必须与给定的作品设定一致，只按工具参数输出，不要解释。",
        messages=[Message("user", prompt)],
        stream=True,
        temperature=resolve_temperature(config, "writing"),
        metadata={
            "mode": "single",
            "work": work_payload,
            "premise": bible.get("premise") or {},
            "world": _bible_digest(bible),
            "characters": _characters_payload(characters),
            "existing_character": existing_char,
            "brief": brief_text,
            "seed": work.get("id"),
        },
    )
    payload = _generate_json(adapter, req, "character")
    raw = payload.get("character")
    if not isinstance(raw, dict):
        rows = payload.get("characters")
        raw = rows[0] if isinstance(rows, list) and rows else None
    if not isinstance(raw, dict) and any(key in payload for key in ("name", "role", "personality")):
        # 模型常把人物字段直接平铺返回，不再包一层 character
        raw = payload
    if not isinstance(raw, dict):
        raise ValueError("LLM 返回的人物不是合法 JSON 对象")
    entry = _clean_character_entry(raw)
    if existing_char:
        entry["id"] = existing_char.get("id") or entry.get("id")
    return {"section": "character", "fields": entry, "target_id": target_id}


def polish_field(
    store: ProjectStore,
    config: AppConfig,
    field: str,
    text: str,
    context: str | None = None,
) -> dict:
    """润色单个可填写字段（只返回文本，不写盘）。

    参数:
        store: 工程。
        config: 本机配置。
        field: 白名单内的字段名（见 POLISH_FIELDS）。
        text: 待润色文本。
        context: 可选上下文（如所属人物、章节摘要）。

    返回:
        {"field", "text"}。

    异常:
        ValueError: 字段不在白名单，或没有可润色的内容。
    """
    key = (field or "").strip()
    # 兼容前端按区块传参的 `premise.*` / `style.*` 前缀写法，归一化后再查白名单
    for prefix in ("premise.", "style."):
        if key.startswith(prefix):
            key = key[len(prefix):]
            break
    if key not in POLISH_FIELDS:
        raise ValueError(f"不支持的润色字段: {key or '(空)'}（可选 {'、'.join(POLISH_FIELDS)}）")
    source = (text or "").strip()
    if not source:
        raise ValueError("没有可润色的内容")

    bible = SettingsStore(store.root).load_or_none() or {}
    work = store.load_project().get("work") or {}
    adapter = build_writing_adapter(config, bible)
    # 润色要贴合作品文风：把项目设定与字段上下文一并交给模型
    prompt_parts: list[str] = []
    project_context = _project_context(work, bible)
    if project_context:
        prompt_parts.append(project_context)
    if context:
        prompt_parts.append(f"字段所在上下文：{context}")
    prompt_parts.append(f"待润色字段 {key}：\n{source}")
    req = TextGenRequest(
        task="polish",
        system="你是中文小说编辑，润色要贴合作品设定与文风，保持原意与信息量，只输出 JSON（键为 text）。",
        messages=[Message("user", "\n\n".join(prompt_parts))],
        stream=False,
        temperature=resolve_temperature(config, "polish"),
        metadata={
            "field": key,
            "text": source,
            "context": context or "",
            "work": {"title": work.get("title"), "genre": work.get("genre") or []},
            "seed": work.get("id"),
            "thinking_stage": "rewrite",
        },
    )
    result = adapter.generate(req)
    polished = (result.text or "").strip()
    try:
        payload = _extract_json_object(polished)
        candidate = str(payload.get("text") or "").strip()
        if candidate:
            polished = candidate
    except ValueError:
        # 模型直接返回纯文本时按原文使用，不强求 JSON
        pass
    if not polished:
        raise ValueError("LLM 返回内容不是合法 JSON")
    limit = SCALAR_LIMITS.get(key)
    if limit and len(polished) > limit:
        polished = polished[:limit]
    return {"field": key, "text": polished}


def bind_outline_context(bible: dict, worlds: list[dict], characters: list[dict]) -> dict:
    """把大纲章节与节拍绑定到世界条目与角色（就地修改 bible）。

    参数:
        bible: 设定库 dict。
        worlds: 世界条目列表；为空时把每章 world_ids 写成空数组。
        characters: 角色表（含旁白，内部会过滤）。

    返回:
        {"chapters": 绑定章节数, "beats": 绑定节拍数, "worlds": 可用世界数}。
    """
    cast = [item for item in characters or [] if item.get("id") and item.get("role") != "narrator" and item.get("id") != entities.NARRATOR_ID]
    # 节拍可以用旁白兜底：旁白是保留实体，允许出现在 character_ids 里
    cast_ids = {item["id"] for item in cast} | {entities.NARRATOR_ID}
    protagonist = next((item for item in cast if item.get("role") == "protagonist"), cast[0] if cast else None)
    order = ([protagonist] + [item for item in cast if item is not protagonist]) if protagonist else []
    world_ids = [item["id"] for item in worlds or [] if item.get("id")]

    chapters_bound = 0
    beats_bound = 0
    index = 0
    for act in (bible.get("outline") or {}).get("acts") or []:
        for chapter in act.get("chapters") or []:
            if world_ids:
                chapter["world_ids"] = [world_ids[index % len(world_ids)]]
            else:
                chapter["world_ids"] = []
            existing_pov = chapter.get("pov_character_id")
            if existing_pov in cast_ids:
                pov = existing_pov
            elif order:
                pov = order[index % len(order)]["id"]
            else:
                pov = None
            chapter["pov_character_id"] = pov
            chapters_bound += 1
            for beat in chapter.get("beats") or []:
                valid = [cid for cid in (beat.get("character_ids") or []) if cid in cast_ids]
                if not valid:
                    # 节拍没有点名任何角色时回退旁白，不再硬绑主角
                    valid = [entities.NARRATOR_ID]
                beat["character_ids"] = valid
                beats_bound += 1
            index += 1
    return {"chapters": chapters_bound, "beats": beats_bound, "worlds": len(world_ids)}


def sanitize_outline_character_ids(bible: dict, characters: list[dict]) -> dict:
    """过滤大纲节拍里的非法角色引用（`binding=none` 专用）。

    与 `bind_outline_context()` 的差异（两者都会丢弃非法引用，但后续处理不同）：
    - `auto`：过滤后若为空，回落到本章 POV，并按幕轮转绑定世界；
    - `none`：只做清洗，**不回落到 POV、不改世界绑定**，保持空数组。

    判定规则：只保留角色表里真实存在的非旁白 ID；人名、空串、重复项一律丢弃。

    参数:
        bible: 设定库 dict（就地修改大纲里的节拍）。
        characters: 角色表（含旁白也可，内部会过滤）。

    返回:
        `{"chapters": 处理章节数, "kept": 保留引用数, "dropped": 丢弃引用数}`。
    """
    cast_ids = {
        str(item["id"])
        for item in characters or []
        if item.get("id") and item.get("role") != "narrator" and item.get("id") != entities.NARRATOR_ID
    }
    # 旁白是保留实体，节拍里的旁白兜底不算非法引用
    cast_ids.add(entities.NARRATOR_ID)
    chapters = 0
    kept = 0
    dropped = 0
    for act in (bible.get("outline") or {}).get("acts") or []:
        for chapter in act.get("chapters") or []:
            chapters += 1
            for beat in chapter.get("beats") or []:
                clean: list[str] = []
                seen: set[str] = set()
                for raw in beat.get("character_ids") or []:
                    text = str(raw or "").strip()
                    # 非 ID（人名/空串）与重复项都丢弃，但计数用于提示
                    if not text or text in seen or text not in cast_ids:
                        dropped += 1
                        continue
                    seen.add(text)
                    clean.append(text)
                kept += len(clean)
                beat["character_ids"] = clean
    return {"chapters": chapters, "kept": kept, "dropped": dropped}


def outline_binding_stats(bible: dict) -> dict:
    """统计大纲里实际生效的角色绑定情况（供完成摘要展示）。

    参数:
        bible: 设定库 dict。

    返回:
        `{"characters": 去重角色数, "refs": 节拍角色引用总数, "chapters": 有角色引用的章节数}`。
    """
    character_ids: set[str] = set()
    refs = 0
    chapters = 0
    for act in (bible.get("outline") or {}).get("acts") or []:
        for chapter in act.get("chapters") or []:
            touched = False
            for beat in chapter.get("beats") or []:
                ids = [str(item) for item in (beat.get("character_ids") or []) if str(item).strip()]
                if not ids:
                    continue
                touched = True
                refs += len(ids)
                character_ids.update(ids)
            if touched:
                chapters += 1
    return {"characters": len(character_ids), "refs": refs, "chapters": chapters}


def bind_outline_characters(store: ProjectStore, bible: dict) -> dict:
    """把大纲章节与节拍绑定到已有世界与人物（兼容旧函数名）。

    读取工程内的世界列表后交给 `bind_outline_context`，每章按幕轮转绑定 1 个世界；
    工程里还没有世界条目时，每章 `world_ids` 写成空数组。

    参数:
        store: 工程（读取 worlds.json 与 characters.json）。
        bible: 设定库 dict。

    返回:
        {"chapters": 绑定章节数, "beats": 绑定节拍数, "worlds": 可用世界数}。
    """
    world_store = WorldStore(store.root)
    worlds = world_store.list_worlds() if world_store.exists() else []
    return bind_outline_context(bible, worlds, store.load_characters()["characters"])


def _effective_pov_mode(entry: dict | None, chapter: dict, style: dict) -> str:
    """计算本章实际生效的视角写法。

    优先级：设定库章节的 pov_mode → 章节副本 pov_mode → 全书 style.pov → 第三人称有限。

    参数:
        entry: 设定库大纲章节；没有绑定时为 None。
        chapter: 章节文件。
        style: 设定库 style。

    返回:
        视角枚举值（first/second/third_limited/third_omniscient）。
    """
    for value in ((entry or {}).get("pov_mode"), chapter.get("pov_mode"), style.get("pov")):
        text = str(value or "").strip()
        if text:
            return text
    return "third_limited"


def sync_chapter_pov(
    store: ProjectStore,
    chapter_id: str,
    pov_character_id: str | None,
    pov_mode: str | None = None,
    update_mode: bool = False,
) -> dict:
    """把本章 POV 同步写进设定库与章节文件（消灭两份死数据）。

    设定库是主源，章节文件只作为副本保存；两处同时写入，页面与生成读取的结果一致。

    参数:
        store: 工程。
        chapter_id: 章节 ID。
        pov_character_id: 视角人物 ID；None 表示不绑定（全知视角常用）。
        pov_mode: 视角写法覆盖；None 表示跟随全书。
        update_mode: 是否改写 pov_mode（False 时只改人物，不动视角写法）。

    返回:
        {"chapter": 章节ID, "pov_character_id", "pov_mode", "settings_revision"}。

    异常:
        ValueError: 角色不存在，或视角写法非法。
    """
    allowed_modes = {"first", "second", "third_limited", "third_omniscient", None, ""}
    if pov_mode not in allowed_modes:
        raise ValueError(f"视角写法非法：{pov_mode}（可选 first/second/third_limited/third_omniscient）")
    settings = SettingsStore(store.root)
    bible = settings.ensure()
    entry = settings.find_outline_chapter(bible, chapter_id)
    cast_ids = {
        str(item.get("id"))
        for item in store.load_characters()["characters"]
        if item.get("id") and item.get("role") != "narrator"
    }
    if pov_character_id and pov_character_id not in cast_ids:
        raise ValueError(f"角色不存在：{pov_character_id}；请先在人物页生成人物")
    if entry is not None:
        entry["pov_character_id"] = pov_character_id or None
        if update_mode:
            entry["pov_mode"] = pov_mode or None
        bible = settings.save(bible)
    chapter = None
    try:
        chapter = store.load_chapter(chapter_id)
    except FileNotFoundError:
        chapter = None
    if chapter is not None:
        chapter["pov_character_id"] = pov_character_id or None
        if update_mode:
            chapter["pov_mode"] = pov_mode or None
        store.save_chapter(chapter)
    return {
        "chapter": chapter_id,
        "pov_character_id": pov_character_id or None,
        "pov_mode": (entry or {}).get("pov_mode") if entry is not None else (chapter or {}).get("pov_mode"),
        "settings_revision": bible.get("settings_revision"),
    }


def bind_beat_characters(store: ProjectStore, chapter_id: str, beat_id: str, character_ids: list[str]) -> dict:
    """把某个节拍的出场角色写入设定库（页面「节拍选角」）。

    只改设定库里的计划选角，不触碰正文与章节文件；返回最新绑定供页面立即刷新。

    参数:
        store: 工程。
        chapter_id: 章节 ID。
        beat_id: 节拍 ID。
        character_ids: 角色 ID 列表（去重，按传入顺序）。

    返回:
        `chapter_binding` 的返回值。

    异常:
        ValueError: 大纲章节/节拍不存在，或角色 ID 非法。
    """
    settings = SettingsStore(store.root)
    bible = settings.ensure()
    entry = settings.find_outline_chapter(bible, chapter_id)
    if entry is None:
        raise ValueError(f"本章还没有大纲条目：{chapter_id}；请先生成或落地大纲")
    cast_ids = {
        str(item.get("id"))
        for item in store.load_characters()["characters"]
        if item.get("id") and item.get("role") != "narrator" and item.get("id") != entities.NARRATOR_ID
    }
    beat = next((item for item in (entry.get("beats") or []) if item.get("id") == beat_id), None)
    if beat is None:
        raise ValueError(f"节拍不存在：{beat_id}")
    clean: list[str] = []
    for raw in character_ids or []:
        value = str(raw or "").strip()
        if not value:
            continue
        if value not in cast_ids:
            raise ValueError(f"角色不存在或不可出场：{value}")
        if value not in clean:
            clean.append(value)
    beat["character_ids"] = clean
    settings.save(bible)
    return chapter_binding(store, chapter_id)


def chapter_binding(store: ProjectStore, chapter_id: str) -> dict:
    """汇总本章的角色绑定、实际出场与对账结果（只读派生，不落盘）。

    实际出场从 `scenes[].character_ids` 与 `lines[].speaker_id` 现算；计划出场只读大纲节拍。

    参数:
        store: 工程。
        chapter_id: 章节 ID。

    返回:
        {
          chapter_id, pov: {id,name}|null, pov_mode, blocking: [...], warnings: [...],
          beats: [...], characters: [...],
          audit: {planned_not_used, unplanned_appeared, pov_not_in_prose, missing_refs, unknown_speakers}
        }。
    """
    settings = SettingsStore(store.root)
    bible = settings.ensure()
    entry = settings.find_outline_chapter(bible, chapter_id)
    try:
        chapter = store.load_chapter(chapter_id)
    except FileNotFoundError:
        # 大纲章还没落地：绑定信息仍要能看，实际出场视为空
        chapter = {"pov_character_id": None, "scenes": []}
    style = bible.get("style") or {}
    characters = [
        item
        for item in store.load_characters()["characters"]
        if item.get("id") and item.get("role") != "narrator"
    ]
    cast_by_id = {str(item["id"]): item for item in characters}
    pov_id = (entry or {}).get("pov_character_id") or chapter.get("pov_character_id")
    if pov_id == entities.NARRATOR_ID:
        pov_id = None
    pov_mode = _effective_pov_mode(entry, chapter, style)
    beats = (entry or {}).get("beats") or []
    planned_counts: dict[str, int] = {}
    beat_rows: list[dict] = []
    for position, beat in enumerate(beats, start=1):
        ids = [str(cid) for cid in (beat.get("character_ids") or []) if str(cid).strip()]
        for cid in ids:
            planned_counts[cid] = planned_counts.get(cid, 0) + 1
        beat_rows.append(
            {
                "beat_id": beat.get("id"),
                "index": position,
                "summary": beat.get("summary") or "",
                "character_ids": ids,
                "characters": [
                    {
                        "id": cid,
                        "name": (cast_by_id.get(cid) or {}).get("name") or "已删除角色",
                        "role": (cast_by_id.get(cid) or {}).get("role"),
                    }
                    for cid in ids
                ],
            }
        )
    # 实际出场：场景标签 + 台词说话人
    lines_by_char: dict[str, int] = {}
    scenes_by_char: dict[str, int] = {}
    unknown: dict[str, dict] = {}
    for scene in chapter.get("scenes") or []:
        for cid in scene.get("character_ids") or []:
            key = str(cid or "").strip()
            if key and key not in (entities.NARRATOR_ID, entities.UNKNOWN_SPEAKER_ID):
                scenes_by_char[key] = scenes_by_char.get(key, 0) + 1
        for line in scene.get("lines") or []:
            key = str(line.get("speaker_id") or "").strip()
            if not key or key == entities.NARRATOR_ID:
                continue
            lines_by_char[key] = lines_by_char.get(key, 0) + 1
            if key == entities.UNKNOWN_SPEAKER_ID or key not in cast_by_id:
                row = unknown.setdefault(key, {"speaker_id": key, "lines": 0, "sample": ""})
                row["lines"] += 1
                if not row["sample"]:
                    row["sample"] = str(line.get("text") or "")[:60]
    actual_ids = {cid for cid, count in lines_by_char.items() if count} | {
        cid for cid, count in scenes_by_char.items() if count
    }
    actual_ids = {cid for cid in actual_ids if cid not in (entities.UNKNOWN_SPEAKER_ID,)}
    rows: list[dict] = []
    for cid in sorted(set(cast_by_id) | set(planned_counts) | actual_ids | ({pov_id} if pov_id else set())):
        if cid in (entities.NARRATOR_ID, entities.UNKNOWN_SPEAKER_ID):
            continue
        item = cast_by_id.get(cid) or {}
        rows.append(
            {
                "id": cid,
                "name": item.get("name") or "已删除角色",
                "role": item.get("role") or "minor",
                "planned": cid in planned_counts,
                "planned_beats": planned_counts.get(cid, 0),
                "lines": lines_by_char.get(cid, 0),
                "scenes": scenes_by_char.get(cid, 0),
                "is_pov": cid == pov_id,
            }
        )
    missing_refs: list[dict] = []
    for cid in planned_counts:
        if cid not in cast_by_id:
            missing_refs.append({"kind": "beat", "id": cid})
    if pov_id and pov_id not in cast_by_id:
        missing_refs.append({"kind": "pov", "id": pov_id})
    blocking: list[dict] = []
    warnings: list[dict] = []
    if pov_mode != "third_omniscient" and not pov_id:
        blocking.append(
            {
                "code": "BINDING_POV_MISSING",
                "message": "本章还没有指定视角人物：请选择本章视角人物，或显式切换为全知视角",
            }
        )
    if not planned_counts and not pov_id:
        blocking.append(
            {
                "code": "BINDING_CHARACTER_MISSING",
                "message": "本章还没有绑定任何角色：请先在大纲里给节拍选择出场角色",
            }
        )
    empty_beats = [row["index"] for row in beat_rows if not row["character_ids"]]
    if empty_beats:
        warnings.append(
            {
                "code": "BINDING_BEAT_EMPTY",
                "message": "以下节拍还没有指定出场角色：第 " + "、".join(str(i) for i in empty_beats) + " 个",
            }
        )
    planned_not_used = [
        {"id": row["id"], "name": row["name"]}
        for row in rows
        if row["planned"] and row["lines"] == 0 and row["scenes"] == 0
    ]
    if planned_not_used:
        warnings.append(
            {
                "code": "PLAN_UNUSED_CHARACTER",
                "message": "计划出场但正文里没有出现：" + "、".join(item["name"] for item in planned_not_used),
            }
        )
    unplanned = [
        {"id": row["id"], "name": row["name"]}
        for row in rows
        if not row["planned"] and (row["lines"] or row["scenes"]) and row["id"] in cast_by_id
    ]
    if unplanned:
        warnings.append(
            {
                "code": "PLAN_UNPLANNED_CHARACTER",
                "message": "正文里出现了计划外角色：" + "、".join(item["name"] for item in unplanned),
            }
        )
    pov_row = next((row for row in rows if row["id"] == pov_id), None) if pov_id else None
    pov_not_in_prose = bool(pov_id and pov_row and pov_row["lines"] == 0 and pov_row["scenes"] == 0)
    if pov_not_in_prose:
        warnings.append(
            {"code": "POV_NOT_IN_PROSE", "message": f"视角人物「{pov_row['name']}」没有在正文场景或台词里出现"}
        )
    if unknown:
        warnings.append(
            {
                "code": "UNKNOWN_SPEAKER",
                "message": f"有 {sum(item['lines'] for item in unknown.values())} 行台词无法对应到角色卡",
            }
        )
    pov_name = (cast_by_id.get(pov_id) or {}).get("name") if pov_id else None
    return {
        "chapter_id": chapter_id,
        "pov": {"id": pov_id, "name": pov_name or ("已删除角色" if pov_id else None)} if pov_id else None,
        "pov_mode": pov_mode,
        "blocking": blocking,
        "warnings": warnings,
        "beats": beat_rows,
        "characters": rows,
        "audit": {
            "planned_not_used": planned_not_used,
            "unplanned_appeared": unplanned,
            "pov_not_in_prose": pov_not_in_prose,
            "missing_refs": missing_refs,
            "unknown_speakers": list(unknown.values()),
        },
    }


def validate_chapter_binding(store: ProjectStore, chapter_id: str) -> dict:
    """校验章节是否已绑定可用的世界、视角人物与角色。

    POV 缺失时阻断生成（除非本章显式设为全知视角）；不再静默套用默认主角。

    参数:
        store: 工程。
        chapter_id: 章节 ID。

    返回:
        {"chapter_id", "source", "world_ids", "pov_character_id", "pov_mode", "character_ids"}。

    异常:
        ValueError: 绑定的世界条目不存在、缺少视角人物或缺少非旁白角色。
    """
    binding = chapter_binding(store, chapter_id)
    characters = [
        item
        for item in store.load_characters()["characters"]
        if item.get("id") and item.get("role") != "narrator" and item.get("id") != entities.NARRATOR_ID
    ]
    cast_ids = {item["id"] for item in characters}
    settings_store = SettingsStore(store.root)
    bible = settings_store.ensure()
    entry = settings_store.find_outline_chapter(bible, chapter_id)
    world_ids = list((entry or {}).get("world_ids") or [])
    if world_ids:
        world_store = WorldStore(store.root)
        known = {item.get("id") for item in (world_store.list_worlds() if world_store.exists() else [])}
        missing = [world_id for world_id in world_ids if world_id not in known]
        if missing:
            raise ValueError(f"本章绑定的世界条目不存在：{'、'.join(missing)}；请先在世界页生成并绑定世界")
    blocking = binding.get("blocking") or []
    if blocking:
        raise ValueError(blocking[0]["message"])
    linked = {row["id"] for row in binding["characters"] if row["planned"] or row["is_pov"]}
    valid = linked & cast_ids
    if not valid:
        raise ValueError("本章还没有绑定角色：请先在人物页生成主要人物并在大纲页绑定")
    return {
        "chapter_id": chapter_id,
        "source": "outline" if entry else "bible",
        "world_ids": world_ids,
        "pov_character_id": binding.get("pov", {}).get("id") if binding.get("pov") else None,
        "pov_mode": binding.get("pov_mode"),
        "character_ids": sorted(valid),
    }


def build_art_prompt(kind: str, entity: dict, style: dict | None = None, width: int | None = None, height: int | None = None) -> dict:
    """拼一条设定图提示词（委托 writing/worlds.build_art_prompt）。

    参数:
        kind: world|closeup|turnaround。
        entity: 世界条目或角色条目。
        style: 文风分区（读取 visual_style，可选 width/height）。
        width: 输出宽度；为空时取 style.width 或 1024。
        height: 输出高度；为空时取 style.height 或 1024。

    返回:
        {"prompt", "negative_prompt", "width", "height"}。

    异常:
        ValueError: kind 非法。
    """
    return _build_art_prompt(kind, entity, style, width=width, height=height)


def _outline_chapters(bible: dict) -> list[dict]:
    """按序号展开设定库里的全部大纲章节。

    参数:
        bible: 设定库 dict。

    返回:
        大纲章节列表（按 index 升序）。
    """
    rows: list[dict] = []
    for act in (bible.get("outline") or {}).get("acts") or []:
        rows.extend(act.get("chapters") or [])
    return sorted(rows, key=lambda item: int(item.get("index") or 0))


def _golden_issue(
    chapter_index: int, chapter: dict | None, problem: str, hint: str, focus: str | None = None
) -> dict:
    """构造一条黄金三章问题（带可定位的章节信息）。

    参数:
        chapter_index: 章节序号（从 1 起，用于缺章时的兜底展示）。
        chapter: 对应的大纲章节 dict；章节不存在时为 None。
        problem: 未通过的具体项（一句话，不含章节号）。
        hint: 为什么没过 + 怎么改。
        focus: 界面定位用的落点标记；目前仅 `beat_turning_point`（需标记转折的节拍）。

    返回:
        {"chapter_index", "chapter_id", "chapter_title", "problem", "hint", "focus"}。
    """
    chapter = chapter or {}
    return {
        "chapter_index": int(chapter_index),
        "chapter_id": chapter.get("id") or None,
        "chapter_title": str(chapter.get("title") or ""),
        "problem": problem,
        "hint": hint,
        "focus": focus,
    }


def check_golden_three(bible: dict) -> dict:
    """检查前三章是否满足「黄金三章」约束（不阻断落盘）。

    规则:
        第 1 章首个节拍摘要非空（钩子）；
        第 2 章节拍 ≥ 2，或章节摘要含冲突/对抗语义；
        第 3 章存在 turning_point=true 的节拍。

    参数:
        bible: 设定库 dict。

    返回:
        {"passed": bool, "issues": [{"chapter_index", "chapter_id", "chapter_title", "problem",
        "hint", "focus"}]}；focus 供界面定位（如 beat_turning_point 表示该章需要标转折）。
    """
    chapters = _outline_chapters(bible)[:3]
    issues: list[dict] = []

    if not chapters:
        issues.append(
            _golden_issue(1, None, "还没有大纲", "设定库里没有任何幕/章；先执行生成大纲（ndw outline generate）")
        )
        return {"passed": False, "issues": issues}

    def index_of(position: int, chapter: dict | None = None) -> int:
        """取章节在整本书里的实际序号，缺失章节按已有最大序号顺延。"""
        if chapter is not None:
            return int(chapter.get("index") or position)
        tail = chapters[-1] if chapters else {}
        return int(tail.get("index") or len(chapters)) + (1 if tail else 0)

    first = chapters[0]
    first_index = index_of(1, first)
    first_beats = first.get("beats") or []
    if not (first_beats and str((first_beats[0] or {}).get("summary") or "").strip()):
        issues.append(
            _golden_issue(
                first_index,
                first,
                "开篇钩子缺失",
                f"第 {first_index} 章首个节拍的摘要是空的；要写清悬念/冲突/能力亮相，前 300 字抓住读者",
            )
        )

    if len(chapters) < 2:
        missing_index = index_of(2)
        issues.append(
            _golden_issue(missing_index, None, f"缺少第 {missing_index} 章", "第 2 章需要确立核心矛盾与对抗关系")
        )
    else:
        second = chapters[1]
        second_index = index_of(2, second)
        second_beats = second.get("beats") or []
        summary = str(second.get("summary") or "")
        has_conflict = len(second_beats) >= 2 or any(word in summary for word in CONFLICT_KEYWORDS)
        if not has_conflict:
            issues.append(
                _golden_issue(
                    second_index,
                    second,
                    "没有确立核心矛盾",
                    f"第 {second_index} 章只有 {len(second_beats)} 个节拍，摘要里也没有冲突/对抗语义；"
                    "把冲突写进章节摘要，或拆成至少 2 个节拍呈现对抗",
                )
            )

    if len(chapters) < 3:
        missing_index = index_of(3)
        issues.append(
            _golden_issue(
                missing_index, None, f"缺少第 {missing_index} 章", "第 3 章需要出现第一次小高潮，让读者确认「这本书能看」"
            )
        )
    else:
        third = chapters[2]
        third_index = index_of(3, third)
        turned = any(bool((beat or {}).get("turning_point")) for beat in (third.get("beats") or []))
        if not turned:
            issues.append(
                _golden_issue(
                    third_index,
                    third,
                    "缺少第一次小高潮",
                    f"第 {third_index} 章没有任何节拍标记 turning_point=true；把其中一个节拍标记为转折，形成阶段性反转",
                    focus="beat_turning_point",
                )
            )

    return {"passed": not issues, "issues": issues}
