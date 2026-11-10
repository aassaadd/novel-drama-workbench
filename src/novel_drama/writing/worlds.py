"""多世界条目清洗：把模型返回的单条世界 JSON 归一化为可落盘的 world 条目。

本模块只做「字段补齐 / 结构清洗 / schema 校验」，不读写文件、不调用适配器；
章节绑定与落盘由 `writing/service.py` 负责。
"""

from __future__ import annotations

from ..models.ids import new_id
from ..store.schema_validator import validate
from ..store.world_store import WorldStore


def _text(value) -> str:
    """把任意值转成去空白字符串。

    参数:
        value: 原始值。

    返回:
        去空白后的字符串；None 返回空串。
    """
    return str(value or "").strip()


def _str_list(rows, item_limit: int) -> list[str]:
    """清洗字符串数组（允许单个字符串自动成组，去掉空项）。

    参数:
        rows: 原始数组或字符串。
        item_limit: 单项最大长度（超出截断）。

    返回:
        去空后的字符串列表。

    异常:
        ValueError: 类型不合法。
    """
    if rows is None:
        return []
    if isinstance(rows, str):
        rows = [rows]
    if not isinstance(rows, list):
        raise ValueError("世界字段必须是字符串数组")
    result: list[str] = []
    for item in rows:
        if isinstance(item, (dict, list)):
            raise ValueError("世界字段的数组项必须是字符串")
        text = _text(item)
        if text:
            result.append(text[:item_limit])
    return result


def _locations(rows) -> list[dict]:
    """清洗地点数组，补齐 loc_ ID 与固定结构并去掉空项。

    参数:
        rows: 原始地点数组（元素可为对象或地点名字符串）。

    返回:
        [{id, name, description, atmosphere}]。

    异常:
        ValueError: 类型不合法。
    """
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise ValueError("locations 必须是数组")
    result: list[dict] = []
    for index, item in enumerate(rows):
        if isinstance(item, str):
            item = {"name": item}
        if not isinstance(item, dict):
            raise ValueError(f"locations 第 {index + 1} 项必须是对象")
        name = _text(item.get("name"))
        if not name:
            continue
        result.append(
            {
                "id": new_id("loc"),
                "name": name[:120],
                "description": _text(item.get("description"))[:1000],
                "atmosphere": _text(item.get("atmosphere"))[:300],
            }
        )
    return result


def _glossary(rows) -> list[dict]:
    """清洗名词表数组，补齐固定结构并去掉空项。

    参数:
        rows: 原始词条数组。

    返回:
        [{term, meaning, aliases?}]。

    异常:
        ValueError: 类型不合法或词条缺少释义。
    """
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise ValueError("glossary 必须是数组")
    result: list[dict] = []
    for index, item in enumerate(rows):
        if not isinstance(item, dict):
            raise ValueError(f"glossary 第 {index + 1} 项必须是对象")
        term = _text(item.get("term"))
        if not term:
            continue
        meaning = _text(item.get("meaning"))
        if not meaning:
            raise ValueError(f"glossary 第 {index + 1} 项「{term}」缺少释义")
        entry = {"term": term[:120], "meaning": meaning[:1000]}
        aliases = _str_list(item.get("aliases"), 120)
        if aliases:
            entry["aliases"] = aliases
        result.append(entry)
    return result


def clean_world_entry(raw: dict) -> dict:
    """把模型返回的一条世界 JSON 清洗为通过 schema 校验的世界条目。

    参数:
        raw: 模型返回的世界对象，可含 name/era/summary/rules/factions/locations/glossary/visual_style。

    返回:
        含 id / 时间戳 / cover_asset 的完整世界条目 dict（可直接交给 WorldStore.add）。

    异常:
        ValueError: 缺少名称或时代背景、字段类型不合法。
        SchemaValidationError: 清洗后仍不符合 world.schema.json。
    """
    if not isinstance(raw, dict):
        raise ValueError("世界条目必须是 JSON 对象")
    name = _text(raw.get("name"))
    era = _text(raw.get("era"))
    if not name:
        raise ValueError("世界名称不能为空")
    if not era:
        raise ValueError("世界时代背景不能为空")
    entry = WorldStore.new_world(
        name=name[:120],
        era=era[:200],
        summary=_text(raw.get("summary"))[:2000],
        rules=_str_list(raw.get("rules"), 500),
        factions=_str_list(raw.get("factions"), 200),
        locations=_locations(raw.get("locations")),
        glossary=_glossary(raw.get("glossary")),
        visual_style=_text(raw.get("visual_style"))[:500],
    )
    validate({"schema_version": 1, "worlds": [entry]}, "world.schema.json")
    return entry


def worlds_digest(worlds: list[dict], ids: list[str]) -> str | None:
    """把选中世界条目拼成章节上下文文本。

    参数:
        worlds: 世界条目列表（WorldStore 读取结果）。
        ids: 本章要注入的世界 ID 列表。

    返回:
        多行上下文文本；ids 为空或一个都没命中时返回 None。
    """
    if not ids:
        return None
    by_id = {item.get("id"): item for item in worlds or [] if item.get("id")}
    blocks: list[str] = []
    for world_id in ids:
        world = by_id.get(world_id)
        if not world:
            continue
        lines = [f"【{world.get('name') or '未命名世界'}】时代：{world.get('era') or '未注明'}"]
        if world.get("summary"):
            lines.append(f"概述：{world['summary']}")
        if world.get("visual_style"):
            lines.append(f"画面风格：{world['visual_style']}")
        for label, key in (("硬规则", "rules"), ("势力", "factions")):
            values = [str(item) for item in (world.get(key) or []) if str(item).strip()]
            if values:
                lines.append(f"{label}：{'；'.join(values)}")
        locations = [item for item in (world.get("locations") or []) if item.get("name")]
        if locations:
            briefs = []
            for item in locations:
                description = str(item.get("description") or "").strip()
                briefs.append(f"{item['name']}（{description}）" if description else str(item["name"]))
            lines.append(f"地点：{'；'.join(briefs)}")
        glossary = [item for item in (world.get("glossary") or []) if item.get("term")]
        if glossary:
            terms = "；".join("{}：{}".format(item.get("term"), item.get("meaning") or "") for item in glossary)
            lines.append(f"术语：{terms}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) or None


def build_art_prompt(kind: str, entity: dict, style: dict | None = None, width: int | None = None, height: int | None = None) -> dict:
    """拼一条设定图提示词（世界封面 / 角色特写 / 角色三视图）。

    参数:
        kind: world|closeup|turnaround。
        entity: 世界条目或角色条目。
        style: 文风分区（读取 visual_style，可选 width/height）。
        width: 输出宽度；为空时取 style.width 或 1024。
        height: 输出高度；为空时取 style.height 或 1024。

    返回:
        {"prompt", "negative_prompt", "width", "height"}。

    异常:
        ValueError: kind 非法或提示词为空。
    """
    style = style or {}
    visual = str(style.get("visual_style") or "").strip()
    if kind == "world":
        parts = [entity.get("name"), entity.get("era"), entity.get("summary"), visual, "世界场景概念图，广角，电影感"]
        negative = str(entity.get("negative_prompt") or "").strip() or "低质, 水印, 文字, 多余肢体"
    elif kind == "closeup":
        parts = ["角色脸部特写，半身，干净背景", entity.get("appearance_prompt"), visual]
        negative = str(entity.get("negative_prompt") or "").strip() or "低质, 多余手指, 水印, 文字"
    elif kind == "turnaround":
        parts = ["角色三视图，正面/侧面/背面，全身，白底", entity.get("appearance_prompt"), visual]
        negative = str(entity.get("negative_prompt") or "").strip() or "低质, 多余手指, 水印, 文字"
    else:
        raise ValueError(f"不支持的配图类型: {kind}（可选 world / closeup / turnaround）")

    prompt = "，".join(str(part).strip() for part in parts if str(part or "").strip())
    if not prompt:
        raise ValueError("配图提示词为空：世界缺少名称/概述，或角色缺少外貌提示词")
    try:
        out_width = int(width or style.get("width") or 1024)
        out_height = int(height or style.get("height") or 1024)
    except (TypeError, ValueError):
        out_width, out_height = 1024, 1024
    return {"prompt": prompt, "negative_prompt": negative, "width": out_width, "height": out_height}
