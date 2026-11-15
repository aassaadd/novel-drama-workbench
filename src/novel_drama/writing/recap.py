"""章节摘要与全书滚动摘要。

两条产出路径：
- **末轮内联**（默认）：正文最后一轮同时产出正文与本章摘要，正文后附
  分隔标记 `ROUND_SUMMARY_MARK` 再输出 JSON；由 `split_final_output` 拆开并
  做结构化校验，校验通过就省掉一次单独的摘要请求。
- **单独调用**（回退）：末轮没有可解析的摘要 JSON 时，回退到
  `update_rolling_summary` 单独发一次摘要请求，并留下可读说明。

两条路径共用 `save_summary` 落盘：本章摘要写入 `chapter.synopsis`，全书滚动
摘要写入 `.ndw/rolling_summary.json`，语义完全一致。模型不可用或输出不合法时
抛 `AdapterError`，由调用方明确提示，不做假数据兜底。
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
#: 正文末轮附带摘要的分隔标记（足够显眼，正常正文不会出现）
ROUND_SUMMARY_MARK = "<<<CHAPTER_SUMMARY_JSON>>>"



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
        (
            '{"chapter_summary": "本章发生了什么，150-300 字，保留关键人物、事件、转折与结果", '
            '"book_summary": "把旧全书摘要与本章合并后的新全书摘要，500-800 字，按时间顺序保留主线、人物关系变化与未解决悬念，不要逐章罗列"}'
        ),
    ]
    return "\n".join(lines)


def update_rolling_summary(
    store: ProjectStore,
    config: AppConfig,
    chapter: dict,
    adapter=None,
) -> dict:
    """单独发一次摘要请求，生成并持久化本章摘要与全书滚动摘要。

    这是末轮内联摘要解析失败时的回退路径：提示词、预算与落盘口径与内联路径
    完全一致，调用方需要把「本次走的是回退」写进结果或日志。

    参数:
        store: 工程。
        config: 本机配置（适配器未传入时用来装配写作适配器）。
        chapter: 已写入正文的章节 dict；本函数会更新其 synopsis 并落盘。
        adapter: 可复用的写作适配器；None 时按配置新建。

    返回:
        {"chapter_id", "chapter_summary", "book_summary_chars", "through_index",
         "updated_at", "provider", "mode"}。

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

    info = save_summary(store, chapter, chapter_summary, book_summary, provider=result.model)
    info["mode"] = "single_call"
    return info


def final_round_summary_instruction(book_summary: str) -> str:
    """拼装正文末轮「正文 + 本章摘要」的输出约定。

    参数:
        book_summary: 生成开始时的全书滚动摘要（可能为空）。

    返回:
        追加在末轮续写指令后的中文说明。
    """
    return "\n".join(
        [
            "",
            "【本轮同时产出本章摘要】",
            f"正文写完后，另起一行输出分隔标记 {ROUND_SUMMARY_MARK}，紧接着输出一个 JSON 对象，不要额外解释：",
            (
                '{"chapter_summary": "本章发生了什么，150-300 字，保留关键人物、事件、转折与结果", '
                '"book_summary": "把下面的旧全书摘要与本章合并后的新全书摘要，500-800 字，'
                '按时间顺序保留主线、人物关系变化与未解决悬念，不要逐章罗列"}'
            ),
            "正文中不得出现该分隔标记；JSON 之后不要再写任何内容。",
            "",
            "【已更新的全书摘要】",
            book_summary or "（还没有，本章是第一次生成摘要）",
        ]
    )


def split_final_output(text: str) -> tuple[str, dict | None, str | None]:
    """把末轮输出拆成「正文」与「摘要 JSON」，并做结构化校验。

    参数:
        text: 末轮模型原始输出（正文 + 分隔标记 + JSON）。

    返回:
        `(body, payload, error)`：
        - body：去掉摘要块后的正文；
        - payload：`{chapter_summary, book_summary}`（已按落盘上限截断）；
        - error：解析失败的中文原因（成功时为 None）。
    """
    raw = (text or "").strip()
    if not raw:
        return "", None, "末轮返回内容为空"

    marker_index = raw.rfind(ROUND_SUMMARY_MARK)
    if marker_index >= 0:
        body = raw[:marker_index].strip()
        tail = raw[marker_index + len(ROUND_SUMMARY_MARK) :].strip()
    else:
        # 模型偶尔会漏掉分隔标记直接写 JSON：按 JSON 键位置切，避免摘要混进正文
        key_index = raw.find('"chapter_summary"')
        if key_index < 0:
            return raw, None, "模型没有按约定输出本章摘要（缺少分隔标记与摘要 JSON）"
        brace_index = raw.rfind("{", 0, key_index)
        body = raw[: brace_index if brace_index >= 0 else key_index].strip()
        tail = raw[brace_index if brace_index >= 0 else key_index :].strip()

    from .service import _extract_json_object

    try:
        payload = _extract_json_object(tail)
    except ValueError as exc:
        return body, None, f"本章摘要 JSON 解析失败：{exc}"

    chapter_summary = str(payload.get("chapter_summary") or "").strip()
    book_summary = str(payload.get("book_summary") or "").strip()
    if not chapter_summary or not book_summary:
        return body, None, "本章摘要 JSON 缺少 chapter_summary 或 book_summary"
    return (
        body,
        {
            "chapter_summary": chapter_summary[:CHAPTER_SUMMARY_LIMIT],
            "book_summary": book_summary[:BOOK_SUMMARY_LIMIT],
        },
        None,
    )


def save_summary(
    store: ProjectStore,
    chapter: dict,
    chapter_summary: str,
    book_summary: str,
    provider: str | None = None,
) -> dict:
    """把本章摘要与全书滚动摘要写入工程（两条产出路径共用）。

    参数:
        store: 工程。
        chapter: 章节 dict；会更新其 synopsis 并落盘。
        chapter_summary: 本章摘要。
        book_summary: 新的全书滚动摘要。
        provider: 产出来源的模型名（可为空）。

    返回:
        {"chapter_id", "chapter_summary", "book_summary_chars", "through_index",
         "updated_at", "provider"}。
    """
    rolling = store.load_rolling_summary()
    chapter_summary = str(chapter_summary or "").strip()[:CHAPTER_SUMMARY_LIMIT]
    book_summary = str(book_summary or "").strip()[:BOOK_SUMMARY_LIMIT]
    if not chapter_summary or not book_summary:
        raise AdapterError(
            ErrorCode.UPSTREAM_ERROR,
            "摘要内容为空（chapter_summary / book_summary 至少一项缺失），本次摘要未写入",
            retryable=True,
        )

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
        "provider": provider or "",
    }

