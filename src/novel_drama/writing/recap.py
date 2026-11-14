"""章节摘要与全书滚动摘要。

每次章节正文生成完成后调用一次模型：
- 产出「本章摘要」写入 `chapter.synopsis`，供后续章节上下文与人工查阅；
- 同时把旧的全书摘要与本章合并成新的「全书滚动摘要」，持久化到
  `.ndw/rolling_summary.json`，长书不必只依赖最近几章。

模型不可用或输出不合法时抛 `AdapterError`，由调用方明确提示，不做假数据兜底。
"""

from __future__ import annotations

from ..adapters.base import AdapterError, ErrorCode, Message, TextGenRequest
from ..config import AppConfig
from ..models.ids import now_iso
from ..store import ProjectStore

#: 送入摘要模型的正文字数：开头 + 结尾，中间省略（长章节也能覆盖起承转合）
RECAP_HEAD_CHARS = 3000
RECAP_TAIL_CHARS = 9000
#: 本章摘要写入 synopsis 的长度上限（与 chapter.schema.json 的 4000 对齐）
CHAPTER_SUMMARY_LIMIT = 4000
#: 全书摘要落盘长度上限
BOOK_SUMMARY_LIMIT = 8000
#: 摘要调用的输出预算
RECAP_MAX_TOKENS = 1600


def _chapter_excerpt(content: str) -> str:
    """截取正文开头与结尾，供摘要模型阅读。

    参数:
        content: 章节正文。

    返回:
        不超过「开头 + 结尾」上限的正文；短正文原样返回。
    """
    text = (content or "").strip()
    limit = RECAP_HEAD_CHARS + RECAP_TAIL_CHARS
    if len(text) <= limit:
        return text
    return text[:RECAP_HEAD_CHARS] + "\n\n……（中间省略）……\n\n" + text[-RECAP_TAIL_CHARS:]


def _recap_prompt(chapter: dict, content: str, book_summary: str) -> str:
    """拼装一次摘要调用的 user 提示词。

    参数:
        chapter: 章节 dict（读取 index/title/outline）。
        content: 已截取的正文。
        book_summary: 上一版全书摘要（可能为空）。

    返回:
        中文提示词。
    """
    index = chapter.get("index") or ""
    title = chapter.get("title") or "未命名"
    outline = str(chapter.get("outline") or "").strip()
    lines = [
        "【已更新的全书摘要】",
        book_summary or "（还没有，本章是第一次生成摘要）",
        "",
        "【本章信息】",
        f"第 {index} 章《{title}》",
    ]
    if outline:
        lines.append(f"大纲：{outline}")
    lines += [
        "",
        "【本章正文】",
        content,
        "",
        "请输出一个 JSON 对象，不要解释：",
        '{"chapter_summary": "本章发生了什么，150-300 字，保留关键人物、事件、转折与结果", '
        '"book_summary": "把旧全书摘要与本章合并后的新全书摘要，500-800 字，按时间顺序保留主线、人物关系变化与未解决悬念，不要逐章罗列"}',
    ]
    return "\n".join(lines)


def update_rolling_summary(
    store: ProjectStore,
    config: AppConfig,
    chapter: dict,
    adapter=None,
) -> dict:
    """生成并持久化本章摘要与全书滚动摘要。

    参数:
        store: 工程。
        config: 本机配置（适配器未传入时用来装配写作适配器）。
        chapter: 已写入正文的章节 dict；本函数会更新其 synopsis 并落盘。
        adapter: 可复用的写作适配器；None 时按配置新建。

    返回:
        {"chapter_id", "chapter_summary", "book_summary_chars", "through_index", "updated_at", "provider"}。

    异常:
        ValueError: 正文为空。
        AdapterError: 模型调用失败或返回内容不是合法 JSON / 缺少必需字段。
    """
    content = str(chapter.get("content") or "").strip()
    if not content:
        raise ValueError("正文为空，无法生成摘要")

    # 延迟导入：service 在生成成功后调用本模块，避免循环依赖
    from .service import _extract_json_object, build_writing_adapter

    rolling = store.load_rolling_summary()
    previous_book = str(rolling.get("book_summary") or "").strip()
    if adapter is None:
        adapter = build_writing_adapter(config)

    excerpt = _chapter_excerpt(content)
    req = TextGenRequest(
        task="recap",
        system="你是长篇小说的责任编辑，负责维护章节摘要与全书滚动摘要。只输出 JSON，不要解释。",
        messages=[Message("user", _recap_prompt(chapter, excerpt, previous_book))],
        stream=False,
        max_tokens=RECAP_MAX_TOKENS,
        temperature=0.3,
        metadata={
            "index": chapter.get("index"),
            "title": chapter.get("title"),
            "content": excerpt,
            "book_summary": previous_book,
        },
    )
    try:
        result = adapter.generate(req)
    except AdapterError:
        raise
    except Exception as exc:  # 适配器内部异常统一转成中文可读错误
        raise AdapterError(ErrorCode.UPSTREAM_ERROR, f"摘要模型调用失败：{exc}", retryable=True) from exc

    try:
        payload = _extract_json_object(result.text or "")
    except ValueError as exc:
        raise AdapterError(ErrorCode.UPSTREAM_ERROR, f"摘要模型返回内容不是合法 JSON：{exc}", retryable=True) from exc

    chapter_summary = str(payload.get("chapter_summary") or "").strip()
    book_summary = str(payload.get("book_summary") or "").strip()
    if not chapter_summary or not book_summary:
        raise AdapterError(
            ErrorCode.UPSTREAM_ERROR,
            "摘要模型返回缺少 chapter_summary 或 book_summary，本次摘要未写入",
            retryable=True,
        )

    chapter_summary = chapter_summary[:CHAPTER_SUMMARY_LIMIT]
    book_summary = book_summary[:BOOK_SUMMARY_LIMIT]
    chapter["synopsis"] = chapter_summary
    store.save_chapter(chapter)

    updated_at = now_iso()
    try:
        previous_through = int(rolling.get("through_index") or 0)
    except (TypeError, ValueError):
        previous_through = 0
    through_index = max(previous_through, int(chapter.get("index") or 0))
    store.save_rolling_summary(
        {
            "book_summary": book_summary,
            "through_index": through_index,
            "through_chapter_id": chapter.get("id"),
            "updated_at": updated_at,
        }
    )

    return {
        "chapter_id": chapter.get("id"),
        "chapter_summary": chapter_summary,
        "book_summary_chars": len(book_summary),
        "through_index": through_index,
        "updated_at": updated_at,
        "provider": result.model,
    }
