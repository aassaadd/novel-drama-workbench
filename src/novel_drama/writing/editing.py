"""章节正文编辑：手工保存，以及按提示词让 LLM 整章 / 选区改写。

与 `generate_chapter` 的区别：这里不重新构思剧情，只在既有正文上做改写；
上下文素材（设定、角色、前情、大纲）统一由 `service.build_chapter_context` 准备，
真正进入 prompt 的文本仍由 `text.context.ContextAssembler` 装配。
"""

from __future__ import annotations

import re

from ..adapters.base import AdapterError, ErrorCode
from ..config import AppConfig
from ..models.ids import new_id, now_iso
from ..store import ProjectStore
from ..text.context import EXCERPT_CHARS, ContextAssembler
from .service import build_chapter_context

#: 已有下游产物时给用户的提示：正文变了，场景与台词需要重新切分
STALE_WARNING = "正文已变更，需重新切分场景与台词"
#: 整章改写时给模型的输出约束
WHOLE_OUTPUT_RULE = "只输出改写后的完整正文，不要解释、不要标题"
#: 选区改写时给模型的输出约束
SELECTION_OUTPUT_RULE = "只输出用于替换选中片段的新文本，不要解释、不要重复前文"
#: continuation.instruction 的 schema 长度上限
INSTRUCTION_LIMIT = 4000
#: 段落切分规则：与前端 page-chapter.js 的 paragraphsOf 一致（按空行）
_PARAGRAPH_PATTERN = re.compile(r"[^\n]+(?:\n(?!\s*\n)[^\n]+)*")


class _EditContextAssembler(ContextAssembler):
    """编辑用装配器：允许指定自定义前文摘录块（选区改写场景）。"""

    def __init__(self, *args, excerpt_blocks: list[dict] | None = None, **kwargs):
        """构造装配器。

        参数:
            excerpt_blocks: 自定义摘录块；为 None 时完全沿用父类行为。
            *args: 透传给 `ContextAssembler`。
            **kwargs: 透传给 `ContextAssembler`。
        """
        super().__init__(*args, **kwargs)
        self.excerpt_blocks = excerpt_blocks

    def _excerpt_blocks(self) -> list[dict]:
        """返回前文摘录块。

        返回:
            指定了 `excerpt_blocks` 时返回其副本，否则沿用父类的「上一章结尾 / 本章结尾」。
        """
        if self.excerpt_blocks is not None:
            return [dict(block) for block in self.excerpt_blocks]
        return super()._excerpt_blocks()


def _paragraph_spans(text: str) -> list[tuple[int, int]]:
    """按空行切分正文，返回每段的字符起止偏移。

    参数:
        text: 章节正文。

    返回:
        [(start, end)]，只包含非空白段落，起始偏移 0 起算。
    """
    spans: list[tuple[int, int]] = []
    for match in _PARAGRAPH_PATTERN.finditer(text or ""):
        if match.group(0).strip():
            spans.append((match.start(), match.end()))
    return spans


def _selection_excerpt(content: str, start: int, end: int) -> list[dict]:
    """取选区前后各 1 段作为前文摘录。

    参数:
        content: 章节正文。
        start: 选区起始偏移（0 起算）。
        end: 选区结束偏移（左闭右开）。

    返回:
        摘录块列表，元素为 {kind, title, text}；没有相邻段落时返回空列表。
    """
    spans = _paragraph_spans(content)
    before = next((span for span in reversed(spans) if span[1] <= start), None)
    after = next((span for span in spans if span[0] >= end), None)
    blocks: list[dict] = []
    if before:
        blocks.append({"kind": "previous", "title": "【选区前一段】", "text": content[before[0]:before[1]][-EXCERPT_CHARS:]})
    if after:
        blocks.append({"kind": "current", "title": "【选区后一段】", "text": content[after[0]:after[1]][:EXCERPT_CHARS]})
    return blocks


def _resolve_range(char_range, content: str) -> tuple[int, int] | None:
    """校验并规范化选区范围。

    参数:
        char_range: None 表示整章；否则为 (start, end) 或 [start, end]。
        content: 章节正文（用于越界校验）。

    返回:
        (start, end) 或 None（整章）。

    异常:
        ValueError: 范围格式错误或越界，消息为中文。
    """
    if char_range is None:
        return None
    if not isinstance(char_range, (tuple, list)) or len(char_range) != 2:
        raise ValueError("选区范围无效：需要 start:end 两个整数")
    try:
        start = int(char_range[0])
        end = int(char_range[1])
    except (TypeError, ValueError) as exc:
        raise ValueError("选区范围无效：start 与 end 必须是整数") from exc
    if start < 0 or end > len(content) or start >= end:
        raise ValueError(f"选区范围无效：需满足 0 <= start < end <= {len(content)}")
    return start, end


def _count_words(text: str) -> int:
    """统计字数（非空白字符）。

    参数:
        text: 正文文本。

    返回:
        字数。
    """
    return sum(1 for char in text if not char.isspace())


def _usage_payload(result) -> dict | None:
    """把适配器返回的 usage 转成 schema 允许的 dict。

    参数:
        result: `TextGenResult`。

    返回:
        {prompt_tokens, completion_tokens}；没有 usage 时返回 None。
    """
    usage = getattr(result, "usage", None)
    if usage is None:
        return None
    return {
        "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
        "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
    }


def _finish_chapter(chapter: dict, text: str, exact: bool) -> tuple[str, int, str]:
    """按保存规则算出最终正文、字数与状态（不写盘）。

    参数:
        chapter: 章节 dict（读取 scenes / script_path 判断下游产物）。
        text: 模型改写后的文本或拼接后的完整正文。
        exact: True 时保留原文首尾空白（选区改写，保证选区外逐字不变）。

    返回:
        (最终正文, 字数, 状态)。
    """
    final = text if exact else text.strip() + "\n"
    has_downstream = bool(chapter.get("scenes") or chapter.get("script_path"))
    return final, _count_words(final), ("stale" if has_downstream else "written")


def edit_chapter(
    store: ProjectStore,
    config: AppConfig,
    chapter_id: str,
    instruction: str,
    char_range: tuple[int, int] | None = None,
    dry_run: bool = True,
) -> dict:
    """按提示词让 LLM 改写章节正文（整章或选区），默认只预览。

    参数:
        store: 工程。
        config: 本机配置（决定写作适配器与上下文预算）。
        chapter_id: 章节 ID。
        instruction: 用户提示词；strip 后不能为空。
        char_range: (start, end) 0 起算字符偏移，左闭右开；None 表示整章改写。
        dry_run: True 只返回预览，不写盘、不追加 continuations。

    返回:
        {chapter_id, mode, before, after, changed, word_count, revision, status,
         warnings, continuation_id?}；before/after 为完整正文。

    异常:
        ValueError: 提示词为空、正文为空或选区越界（中文消息）。
        AdapterError: 模型调用失败或返回空内容（中文消息，正文不变）。
        FileNotFoundError: 章节不存在。
    """
    prompt = (instruction or "").strip()
    if not prompt:
        raise ValueError("提示词不能为空")

    chapter = store.load_chapter(chapter_id)
    content = chapter.get("content") or ""
    if not content.strip():
        raise ValueError("正文为空，无法编辑")

    selection = _resolve_range(char_range, content)
    context = build_chapter_context(store, config, chapter)
    configured_budget = int(config.get("writing.context_budget_tokens") or 32768)

    if selection is None:
        # 整章改写必须让模型看到全文；在配置预算外预留装配余量，避免摘录被裁掉
        budget = max(configured_budget, min(len(content) + 32768, 262144))
        excerpt_blocks = [{"kind": "current", "title": "【当前正文（全文）】", "text": content}]
        instruction_text = (
            f"按以下提示词改写本章正文。\n提示词：{prompt}\n输出要求：{WHOLE_OUTPUT_RULE}。"
        )
    else:
        start, end = selection
        budget = configured_budget
        excerpt_blocks = _selection_excerpt(content, start, end)
        selected = content[start:end]
        instruction_text = (
            f"按以下提示词改写选中片段。\n提示词：{prompt}\n"
            f"选中片段：\n{selected}\n输出要求：{SELECTION_OUTPUT_RULE}。"
        )

    assembler = _EditContextAssembler(
        work=context["work"],
        bible=context["bible"],
        world_digest=context["world_context"],
        characters=context["characters"],
        chapter=chapter,
        outline_chapter=context["entry"] or {},
        previous_chapters=context["previous_chapters"],
        current_content=content,
        budget_tokens=budget,
        excerpt_blocks=excerpt_blocks,
    )
    req = assembler.build("chapter", instruction_text, context["speaker_ids"])
    # 离线适配器依赖 metadata 产出确定性正文；编辑时沿用同一份，真实模型只用到 prompt
    req.metadata = context["metadata"]

    try:
        result = context["adapter"].generate(req)
    except AdapterError:
        raise
    except Exception as exc:  # 适配器内部异常统一转成中文可读错误
        raise AdapterError(ErrorCode.UPSTREAM_ERROR, f"模型调用失败：{exc}", retryable=True) from exc

    generated = (result.text or "").strip()
    if not generated:
        raise AdapterError(ErrorCode.UPSTREAM_ERROR, "模型未返回可用内容，正文未修改", retryable=True)

    if selection is None:
        final, word_count, status = _finish_chapter(chapter, generated, exact=False)
        produced = {"char_start": 0, "char_end": len(generated)}
    else:
        start, end = selection
        final, word_count, status = _finish_chapter(chapter, content[:start] + generated + content[end:], exact=True)
        produced = {"char_start": start, "char_end": start + len(generated)}

    warnings = list(assembler.warnings)
    has_downstream = bool(chapter.get("scenes") or chapter.get("script_path"))
    if has_downstream:
        warnings.append(STALE_WARNING)

    revision = int(chapter.get("revision") or 1)
    response = {
        "chapter_id": chapter_id,
        "mode": "rewrite_range",
        "before": content,
        "after": final,
        "changed": final != content,
        "word_count": word_count,
        "revision": revision,
        "status": status,
        "warnings": warnings,
    }
    if dry_run:
        return response

    record = {
        "id": new_id("cnt"),
        "mode": "rewrite_range",
        "instruction": prompt[:INSTRUCTION_LIMIT],
        "range": ({"char_start": selection[0], "char_end": selection[1]} if selection else None),
        "context": {
            "include_bible": True,
            "include_previous_summaries": len(context["previous_chapters"]),
            "include_recent_chars": min(len(content), 100000),
            "budget_tokens": max(1024, min(budget, 262144)),
        },
        "status": "done",
        "produced": produced,
        "model": (result.model or "")[:200] or None,
        "usage": _usage_payload(result),
        "created_at": now_iso(),
    }

    chapter["content"] = final
    chapter["word_count"] = word_count
    chapter["status"] = status
    chapter["revision"] = revision + 1
    continuations = chapter.get("continuations")
    if not isinstance(continuations, list):
        continuations = []
    continuations.append(record)
    chapter["continuations"] = continuations
    store.save_chapter(chapter)

    response["revision"] = chapter["revision"]
    response["continuation_id"] = record["id"]
    return response


def save_chapter_content(store: ProjectStore, chapter_id: str, content: str) -> dict:
    """保存手工编辑后的章节正文。

    处理规则：
    - 正文 strip 后为空直接拒绝；
    - 落盘文本末尾补一个换行，与 `generate_chapter` 保持一致；
    - 字数按非空白字符统计；
    - 没有下游产物时 `status` 置为 `written`；已有 `scenes` 或 `script_path` 时置为 `stale`，
      提醒需要重新切分场景与台词（不删除用户数据），`revision` 加 1，`updated_at` 由 store 负责。

    参数:
        store: 工程。
        chapter_id: 章节 ID。
        content: 新正文（UTF-8 文本，末尾有无换行均可）。

    返回:
        {"chapter_id", "word_count", "revision", "status", "warnings"}。

    异常:
        ValueError: 正文为空或仅含空白字符。
        FileNotFoundError: 章节不存在。
    """
    text = (content or "").strip()
    if not text:
        raise ValueError("正文不能为空")

    chapter = store.load_chapter(chapter_id)
    warnings: list[str] = []
    # 正文变了，已切分的场景与脚本就与正文对不上，标记为过期等待重建
    has_downstream = bool(chapter.get("scenes") or chapter.get("script_path"))
    if has_downstream:
        warnings.append(STALE_WARNING)

    chapter["content"] = text + "\n"
    chapter["word_count"] = sum(1 for char in text if not char.isspace())
    chapter["status"] = "stale" if has_downstream else "written"
    chapter["revision"] = int(chapter.get("revision") or 1) + 1
    store.save_chapter(chapter)

    return {
        "chapter_id": chapter_id,
        "word_count": chapter["word_count"],
        "revision": chapter["revision"],
        "status": chapter["status"],
        "warnings": warnings,
    }
