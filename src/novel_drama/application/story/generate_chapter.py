"""用例：按大纲节拍生成章节正文（含分轮、断点续跑、退化熔断、摘要合并）。"""

from __future__ import annotations

import logging
import math

from ...adapters.errors import AdapterError, ErrorCode
from ...adapters.persistence import ProjectStore
from ...cancel import GenerationCanceled
from ...domain.production.rules.token_budget import context_budget_tokens
from ...domain.shared.errors import TaskFailedError
from ...domain.story.rules.chapter_rules import bump_revision
from ...infrastructure.config import AppConfig
from ...models.ids import now_iso
from ...text.context import POV_LABELS, ContextAssembler
from ...text.dialogue import normalize_dialogue
from . import chapter_checkpoint
from .adjust_chapter import compress_chapter, supplement_chapter
from .chapter_context import build_chapter_context
from .chapter_limits import OVERSHOOT_ACTION_RATIO
from .chapter_rounds import _generate_chapter_rounds, _generate_final_round
from .chapter_text import _count_words, _group_words, _word_deviation
from .content_guard import check_chapter_content
from .length_plan import length_instruction, length_plan
from .outline_bind import validate_chapter_binding
from .recap import (
    final_round_summary_instruction,
    save_summary,
)

logger = logging.getLogger(__name__)


def generate_chapter_checked(
    store: ProjectStore,
    config: AppConfig,
    chapter_id: str,
    regen: bool = False,
    dry_run: bool = False,
    multi_turn: bool | None = None,
    beats_per_round: int | None = None,
    max_rounds: int | None = None,
) -> dict:
    """先校验章节的世界/角色绑定，再生成正文（CLI 与页面共用同一入口）。

    参数:
        store: 工程。
        config: 本机配置。
        chapter_id: 章节 ID。
        regen: 已有正文时是否覆盖重写。
        dry_run: 只生成不落盘。
        multi_turn: 显式分轮开关；None 时按配置判断。
        beats_per_round: 每轮写几个节拍。
        max_rounds: 最大轮数。

    返回:
        `generate_chapter` 的结构化结果。

    异常:
        TaskFailedError: 章节世界/角色绑定不完整。
    """
    try:
        validate_chapter_binding(store, chapter_id)
    except ValueError as error:
        raise TaskFailedError(str(error)) from error
    return generate_chapter(
        store,
        config,
        chapter_id,
        regen=regen,
        dry_run=dry_run,
        multi_turn=multi_turn,
        beats_per_round=beats_per_round,
        max_rounds=max_rounds,
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
        from ...application.cast.capture_cast import ensure_chapter_cast

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
