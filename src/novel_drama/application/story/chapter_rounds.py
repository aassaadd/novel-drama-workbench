"""章节生成的内部分轮机制：单轮校验、末轮收口、断点文件读写。"""

from __future__ import annotations

import json
import logging
from dataclasses import replace

from ...adapters.errors import AdapterError, ErrorCode
from ...adapters.llm.contracts import Message, TextGenRequest, TextGenResult
from ...adapters.persistence import ProjectStore
from ...domain.production.rules.watchdog import describe as describe_watchdog
from ...models.ids import now_iso
from ...oplog import set_llm_call_reason
from .chapter_context import stream_text
from .chapter_limits import MAX_LOCAL_CONTINUATIONS
from .chapter_text import (
    _append_segment,
    _beats_round_instruction,
    _count_words,
    _group_words,
)
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
from .recap import (
    split_final_output,
)

logger = logging.getLogger(__name__)


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
    再失败才报错且不落盘。判定阈值见 `application/story/degeneration.py`，不是长度上限。

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

#: 末轮重试时补一句摘要格式要求，避免模型在严格约束下丢掉摘要块
_RETRY_SUMMARY_SUFFIX = (
    "\n重试时仍按本轮要求：正文写完后另起一行输出分隔标记 <<<CHAPTER_SUMMARY_JSON>>>，"
    "再输出 chapter_summary / book_summary 的 JSON。"
)
