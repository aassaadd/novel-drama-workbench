"""写作模型装配与调用脚手架：适配器装配、JSON 抽取、工具调用、进度回调、字段清洗。"""

from __future__ import annotations

import inspect
import json
import re
from dataclasses import replace

from ...adapters.base import (
    AdapterError,
    ErrorCode,
    TextGenRequest,
)
from ...adapters.offline_writing import OfflineWritingAdapter
from ...domain.production.rules.thinking_policy import collect_overrides, step_stage
from ...infrastructure.config import AppConfig
from ..story.setting_spec import CHOICE_ALIASES
from .llm_schemas import _gen_tool


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
        from ...adapters.writing import OmlxWritingAdapter

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

def _raw_preview(payload) -> str:
    """把工具参数压成一段可写入错误的原文片段。"""
    try:
        text = json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(payload)
    return text[:500] or "（空）"
