"""正文反向总结：从已生成正文重新总结章节标题、摘要、大纲与节拍。

与 `generate_chapter` 的方向相反：这里以正文为事实来源，回写章节文件与设定库大纲，
让目录、后续章节上下文与正文保持一致。默认只预览，`apply=True` 才落盘。
"""

from __future__ import annotations

from ..adapters.base import AdapterError, ErrorCode, Message, TextGenRequest
from ..config import AppConfig
from ..models.ids import new_id
from ..store import ProjectStore
from ..store.settings_store import SettingsStore
from ..text.context import POV_LABELS
from .service import _extract_json_object, build_chapter_context

#: 章节字段上限（与 `schema/chapter.schema.json` 保持一致）
TITLE_LIMIT = 200
SYNOPSIS_LIMIT = 4000
OUTLINE_LIMIT = 8000
#: 设定库大纲章节摘要上限（`schema/setting-bible.schema.json`）
BIBLE_SUMMARY_LIMIT = 2000
#: 单条节拍摘要上限（设定库 beat.summary）
BEAT_SUMMARY_LIMIT = 1000
#: 节拍数量范围
BEATS_MIN = 1
BEATS_MAX = 8
#: 送入模型的正文开头 / 结尾字数上限，超出部分标注省略
PROMPT_HEAD_CHARS = 12000
PROMPT_TAIL_CHARS = 6000
#: 给模型的输出约束
OUTPUT_RULE = "只输出一个 JSON 对象，不要代码块、不要解释、不要额外文本"
#: 模型输出的 JSON 结构示例
JSON_SHAPE = '{"title": "…", "synopsis": "…", "outline": "…", "beats": [{"summary": "…", "turning_point": false}]}'


def _content_excerpt(content: str) -> tuple[str, bool]:
    """截取正文开头与结尾，供超长正文送入模型。

    参数:
        content: 章节正文。

    返回:
        (送入模型的文本, 是否发生截断)；截断处用「中间省略 N 字」标注。
    """
    limit = PROMPT_HEAD_CHARS + PROMPT_TAIL_CHARS
    if len(content) <= limit:
        return content, False
    omitted = len(content) - limit
    head = content[:PROMPT_HEAD_CHARS]
    tail = content[-PROMPT_TAIL_CHARS:]
    return f"{head}\n\n……（中间省略 {omitted} 字）……\n\n{tail}", True


def _character_digest(characters: list[dict]) -> str:
    """整理主要角色名，保证模型总结时沿用既有专有名词。

    参数:
        characters: 全量角色表。

    返回:
        形如「沈砚（protagonist，别名：沈队）；林见夏（supporting）」的单行文本。
    """
    parts: list[str] = []
    for item in characters:
        name = str(item.get("name") or "").strip()
        if not name or item.get("role") == "narrator":
            continue
        detail = str(item.get("role") or "").strip()
        aliases = [str(alias).strip() for alias in (item.get("aliases") or []) if str(alias).strip()]
        if aliases:
            detail = f"{detail}，别名：{'、'.join(aliases)}" if detail else f"别名：{'、'.join(aliases)}"
        parts.append(f"{name}（{detail}）" if detail else name)
        if len(parts) >= 20:
            break
    return "；".join(parts) or "（无角色设定）"


def _style_digest(style: dict) -> str:
    """把作品文风整理成提示词里的一行说明。

    参数:
        style: 设定库 style 字段。

    返回:
        基调 / 视角 / 句式偏好 / 禁止项的中文说明。
    """
    pov = str(style.get("pov") or "").strip()
    parts = [
        f"基调：{style.get('tone') or '未设定'}",
        f"视角：{POV_LABELS.get(pov, pov or '未设定')}",
    ]
    if style.get("prose_style"):
        parts.append(f"句式偏好：{style['prose_style']}")
    forbidden = [str(item) for item in (style.get("forbidden") or []) if str(item).strip()]
    if forbidden:
        parts.append(f"禁止项：{'、'.join(forbidden)}")
    return "；".join(parts)


def _current_beats_text(beats: list[dict]) -> str:
    """把设定库现有节拍渲染成提示词里的编号清单。

    参数:
        beats: 当前章节节拍列表。

    返回:
        多行文本；为空时返回「（无）」。
    """
    lines = []
    for index, beat in enumerate(beats, start=1):
        summary = str(beat.get("summary") or "").strip()[:200]
        flag = "（转折点）" if beat.get("turning_point") else ""
        lines.append(f"{index}. {summary}{flag}")
    return "\n".join(lines) or "（无）"


def _build_request(context: dict, chapter: dict, excerpt: str) -> TextGenRequest:
    """组装反向总结用的文本生成请求。

    参数:
        context: `build_chapter_context` 的返回。
        chapter: 章节 dict。
        excerpt: 可能被截断的正文。

    返回:
        TextGenRequest（task=summarize，metadata 供离线适配器确定性复现）。
    """
    work = context["work"]
    style = context["style"]
    instruction = "\n".join(
        [
            "请根据下面这章正文，反向总结章节标题、摘要、大纲与节拍。",
            (
                f"作品：《{work.get('title') or '未命名'}》；类型：{'、'.join(work.get('genre') or []) or '未设定'}；"
                f"一句话简介：{work.get('logline') or '未设定'}"
            ),
            f"文风：{_style_digest(style)}",
            f"主要角色：{_character_digest(context['characters'])}",
            f"当前章节标题：{chapter.get('title') or ''}",
            f"当前大纲：{(chapter.get('outline') or '').strip() or '（无）'}",
            "当前节拍：",
            _current_beats_text(context["beats"]),
            "正文：",
            excerpt,
            "输出 JSON 结构：",
            JSON_SHAPE,
            "约束：",
            f"1. title 为字符串，1–{TITLE_LIMIT} 字；",
            f"2. synopsis 为字符串，不超过 {SYNOPSIS_LIMIT} 字，概括本章发生了什么；",
            f"3. outline 为字符串，不超过 {OUTLINE_LIMIT} 字，可用换行分条；",
            (
                f"4. beats 为 1–{BEATS_MAX} 条数组，每条含字符串 summary（不超过 {BEAT_SUMMARY_LIMIT} 字）"
                "与布尔 turning_point；"
            ),
            "5. 人物、地点、术语必须沿用上面的既有名称，不要新造专有名词；",
            f"6. {OUTPUT_RULE}。",
        ]
    )
    return TextGenRequest(
        task="summarize",
        system="你是长篇小说的结构编辑，擅长从既有正文反推大纲。只输出 JSON，不要解释。",
        messages=[Message("user", instruction)],
        stream=False,
        metadata={
            "title": chapter.get("title"),
            "content": excerpt,
            "beats": context["beats"],
            "characters": context["character_payload"],
            "style": style,
            "tone": style.get("tone"),
            "pov": style.get("pov"),
        },
    )


def _require_text(payload: dict, key: str, limit: int, label: str, required: bool = True) -> str:
    """校验并清洗模型输出中的字符串字段。

    参数:
        payload: 模型输出的 JSON dict。
        key: 字段名。
        limit: 最大长度（schema 上限）。
        label: 中文字段名，用于错误提示。
        required: 是否拒绝空字符串。

    返回:
        去空白后的字符串。

    异常:
        AdapterError: 字段缺失、类型错误、为空或超长（中文消息）。
    """
    if key not in payload:
        raise AdapterError(ErrorCode.UPSTREAM_ERROR, f"模型输出缺少字段：{key}", retryable=True)
    value = payload.get(key)
    if not isinstance(value, str):
        raise AdapterError(ErrorCode.UPSTREAM_ERROR, f"模型输出字段 {key} 必须是字符串", retryable=True)
    text = value.strip()
    if required and not text:
        raise AdapterError(ErrorCode.UPSTREAM_ERROR, f"模型输出的{label}不能为空", retryable=True)
    if len(text) > limit:
        raise AdapterError(
            ErrorCode.UPSTREAM_ERROR,
            f"模型输出的{label}超过 {limit} 字上限（实际 {len(text)} 字）",
            retryable=True,
        )
    return text


def _validate_payload(payload: dict) -> dict:
    """逐字段校验模型输出，返回可用于回写的规范化结果。

    参数:
        payload: 模型输出的 JSON dict。

    返回:
        {title, synopsis, outline, beats: [{summary, turning_point}]}。

    异常:
        AdapterError: 字段缺失、类型错误、数量越界或超长（中文消息）。
    """
    title = _require_text(payload, "title", TITLE_LIMIT, "标题")
    synopsis = _require_text(payload, "synopsis", SYNOPSIS_LIMIT, "摘要", required=False)
    outline = _require_text(payload, "outline", OUTLINE_LIMIT, "大纲", required=False)

    if "beats" not in payload:
        raise AdapterError(ErrorCode.UPSTREAM_ERROR, "模型输出缺少字段：beats", retryable=True)
    raw_beats = payload.get("beats")
    if not isinstance(raw_beats, list):
        raise AdapterError(ErrorCode.UPSTREAM_ERROR, "模型输出字段 beats 必须是数组", retryable=True)
    if not BEATS_MIN <= len(raw_beats) <= BEATS_MAX:
        raise AdapterError(
            ErrorCode.UPSTREAM_ERROR,
            f"模型输出的节拍数量需在 {BEATS_MIN}–{BEATS_MAX} 条之间（实际 {len(raw_beats)} 条）",
            retryable=True,
        )

    beats: list[dict] = []
    for index, item in enumerate(raw_beats, start=1):
        if not isinstance(item, dict):
            raise AdapterError(ErrorCode.UPSTREAM_ERROR, f"模型输出的第 {index} 条节拍必须是对象", retryable=True)
        summary = _require_text(item, "summary", BEAT_SUMMARY_LIMIT, f"第 {index} 条节拍摘要")
        turning_point = item.get("turning_point")
        if not isinstance(turning_point, bool):
            raise AdapterError(
                ErrorCode.UPSTREAM_ERROR,
                f"模型输出的第 {index} 条节拍 turning_point 必须是布尔值",
                retryable=True,
            )
        beats.append({"summary": summary, "turning_point": turning_point})
    return {"title": title, "synopsis": synopsis, "outline": outline, "beats": beats}


def _beats_equal(old_beats: list[dict], new_beats: list[dict]) -> bool:
    """判断新旧节拍在语义字段上是否一致（忽略 ID / 角色绑定）。

    参数:
        old_beats: 设定库现有节拍。
        new_beats: 模型总结出的节拍。

    返回:
        True 表示摘要与转折标记完全一致。
    """
    if len(old_beats) != len(new_beats):
        return False
    for old, new in zip(old_beats, new_beats):
        if str(old.get("summary") or "").strip() != new["summary"]:
            return False
        if bool(old.get("turning_point")) != new["turning_point"]:
            return False
    return True


def _diff_fields(chapter: dict, old_beats: list[dict], preview: dict, has_entry: bool) -> list[str]:
    """列出将被变更的字段。

    参数:
        chapter: 当前章节。
        old_beats: 设定库现有节拍（没有大纲条目时为空）。
        preview: 校验后的总结结果。
        has_entry: 设定库是否存在本章大纲条目。

    返回:
        字段名列表，顺序固定为 title / synopsis / outline / beats。
    """
    changes = []
    if preview["title"] != (chapter.get("title") or ""):
        changes.append("title")
    if preview["synopsis"] != (chapter.get("synopsis") or "").strip():
        changes.append("synopsis")
    if preview["outline"] != (chapter.get("outline") or "").strip():
        changes.append("outline")
    if has_entry and not _beats_equal(old_beats, preview["beats"]):
        changes.append("beats")
    return changes


def _sync_beats(old_beats: list[dict], new_beats: list[dict], pov_id: str | None) -> list[dict]:
    """按位置复用旧节拍 ID，生成新的设定库节拍列表。

    - 不足的部分新建 ID；
    - 多余旧节拍直接丢弃；
    - `character_ids` 无法从正文可靠推断，沿用同位置旧值，新增节拍用本章 POV 角色；
    - `target_words` 沿用同位置旧值，新增节拍为 0。

    参数:
        old_beats: 设定库现有节拍。
        new_beats: 模型总结出的节拍。
        pov_id: 本章 POV 角色 ID。

    返回:
        新的节拍列表。
    """
    result: list[dict] = []
    for index, beat in enumerate(new_beats):
        old = old_beats[index] if index < len(old_beats) else None
        if old and old.get("id"):
            beat_id = old["id"]
        else:
            beat_id = new_id("bea")
        character_ids = list((old or {}).get("character_ids") or [])
        if old is None and pov_id:
            character_ids = [pov_id]
        result.append(
            {
                "id": beat_id,
                "summary": beat["summary"],
                "character_ids": character_ids,
                "turning_point": beat["turning_point"],
                "target_words": int((old or {}).get("target_words") or 0),
            }
        )
    return result


def summarize_chapter(
    store: ProjectStore,
    config: AppConfig,
    chapter_id: str,
    apply: bool = False,
) -> dict:
    """根据已有正文反向总结章节标题、摘要、大纲与节拍，默认只预览。

    参数:
        store: 工程。
        config: 本机配置（决定写作适配器）。
        chapter_id: 章节 ID。
        apply: True 时写回章节文件与设定库；False 只返回预览。

    返回:
        apply=False: {chapter_id, preview, changes, replaced_beats}；
        apply=True: {applied, chapter_id, old_title, new_title, changed_fields,
                      replaced_beats, warnings}。

    异常:
        ValueError: 正文为空（中文消息）。
        FileNotFoundError: 章节不存在。
        AdapterError: 模型调用失败、输出非法 JSON 或字段校验不通过（不写盘）。
    """
    chapter = store.load_chapter(chapter_id)
    content = str(chapter.get("content") or "")
    if not content.strip():
        raise ValueError("正文为空，无法总结")

    context = build_chapter_context(store, config, chapter)
    entry = context["entry"]
    old_beats = list((entry or {}).get("beats") or [])
    excerpt, truncated = _content_excerpt(content)

    req = _build_request(context, chapter, excerpt)
    try:
        result = context["adapter"].generate(req)
    except AdapterError:
        raise
    except Exception as exc:  # 适配器内部异常统一转成中文可读错误
        raise AdapterError(ErrorCode.UPSTREAM_ERROR, f"模型调用失败：{exc}", retryable=True) from exc

    try:
        payload = _extract_json_object(result.text or "")
        preview = _validate_payload(payload)
    except AdapterError:
        raise
    except ValueError as exc:
        raise AdapterError(ErrorCode.UPSTREAM_ERROR, f"模型返回内容不是合法 JSON，未写盘：{exc}", retryable=True) from exc

    changes = _diff_fields(chapter, old_beats, preview, entry is not None)
    replaced_beats = min(len(old_beats), len(preview["beats"])) if "beats" in changes else 0

    if not apply:
        return {
            "chapter_id": chapter_id,
            "preview": preview,
            "changes": changes,
            "replaced_beats": replaced_beats,
        }

    warnings: list[str] = []
    if truncated:
        warnings.append(f"正文过长，仅截取开头 {PROMPT_HEAD_CHARS} 字与结尾 {PROMPT_TAIL_CHARS} 字参与总结")
    if entry is None:
        warnings.append("设定库中没有本章的大纲条目，已跳过节拍同步")

    old_title = str(chapter.get("title") or "")
    changed_fields = list(changes)
    if not changed_fields:
        warnings.append("总结结果与当前内容一致，无需写盘")
        return {
            "applied": False,
            "chapter_id": chapter_id,
            "old_title": old_title,
            "new_title": preview["title"],
            "changed_fields": [],
            "replaced_beats": 0,
            "warnings": warnings,
        }

    # 正文只作为事实来源，绑定的世界 / POV 保持不变
    chapter_changed = any(field in changed_fields for field in ("title", "synopsis", "outline"))
    if chapter_changed:
        chapter["title"] = preview["title"]
        chapter["synopsis"] = preview["synopsis"]
        chapter["outline"] = preview["outline"]
        chapter["revision"] = int(chapter.get("revision") or 1) + 1

    if entry is not None:
        bible = context["bible"]
        bible_summary = preview["synopsis"]
        if len(bible_summary) > BIBLE_SUMMARY_LIMIT:
            bible_summary = bible_summary[:BIBLE_SUMMARY_LIMIT]
            warnings.append(f"摘要超过设定库上限 {BIBLE_SUMMARY_LIMIT} 字，已截断后同步")
        pov_id = entry.get("pov_character_id") or chapter.get("pov_character_id")
        entry["title"] = preview["title"]
        entry["summary"] = bible_summary
        entry["beats"] = _sync_beats(old_beats, preview["beats"], pov_id)
        # 先校验设定库，避免章节已写而设定库写不进去
        settings_store = SettingsStore(store.root)
        settings_store.validate(bible)

    if chapter_changed:
        store.save_chapter(chapter)
    if entry is not None:
        settings_store.save(bible)

    return {
        "applied": True,
        "chapter_id": chapter_id,
        "old_title": old_title,
        "new_title": preview["title"],
        "changed_fields": changed_fields,
        "replaced_beats": replaced_beats,
        "warnings": warnings,
    }
