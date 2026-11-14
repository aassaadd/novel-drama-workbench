"""多世界条目清洗：把模型返回的单条世界 JSON 归一化为可落盘的 world 条目。

本模块只做「字段补齐 / 结构清洗 / schema 校验」，不读写文件、不调用适配器；
章节绑定与落盘由 `writing/service.py` 负责。
"""

from __future__ import annotations

import re

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
    if isinstance(rows, dict):
        # 模型有时把规则/势力返回成单个对象或「名称→说明」映射
        if any(key in rows for key in ("name", "title", "description", "desc", "summary")):
            rows = [rows]
        else:
            rows = [
                f"{_text(key)}：{_one_line(value)}" if _one_line(value) else _text(key)
                for key, value in rows.items()
            ]
    if not isinstance(rows, list):
        raise ValueError(f"世界字段必须是字符串数组（实际收到 {type(rows).__name__}）")
    result: list[str] = []
    for item in rows:
        if isinstance(item, dict):
            # 模型有时把规则/势力写成对象，拼成「名称：说明」而不是直接报错
            name = _text(item.get("name") or item.get("title"))
            note = _text(item.get("description") or item.get("desc") or item.get("summary"))
            text = f"{name}：{note}" if name and note else (name or note or "、".join(_text(value) for value in item.values()))
        elif isinstance(item, list):
            text = "、".join(_text(value) for value in item if not isinstance(value, (dict, list)))
        else:
            text = _text(item)
        if text:
            result.append(text[:item_limit])
    return result


def _pick_text(item: dict, keys: tuple[str, ...]) -> str:
    """按候选键顺序取第一个非空文本值。

    参数:
        item: 原始对象。
        keys: 候选键（中英文别名）。

    返回:
        去空白文本；都没命中返回空串。
    """
    for key in keys:
        value = item.get(key)
        if isinstance(value, (dict, list)):
            continue
        text = _text(value)
        if text:
            return text
    return ""


def _one_line(value) -> str:
    """把任意值压成一行文本（对象按键值拼接，数组用顿号连接）。

    参数:
        value: 原始值。

    返回:
        单行文本；无法提取时返回空串。
    """
    if isinstance(value, dict):
        return "；".join(f"{_text(key)}：{_one_line(item)}" for key, item in value.items() if _one_line(item))
    if isinstance(value, list):
        return "、".join(part for part in (_one_line(item) for item in value) if part)
    return _text(value)


def normalize_locations(rows) -> list[dict]:
    """把模型返回的地点字段归一化成对象数组。

    模型可能返回数组、单个对象、`名称→描述` 映射或整段字符串，这里统一转换：
    对象转数组、单对象包成单元素数组、字符串按行/分号拆成地点记录，尽量保留内容。

    参数:
        rows: 原始地点字段。

    返回:
        [{name, description, atmosphere}]（不含 id）。

    异常:
        ValueError: 顶层类型无法识别（错误信息带实际类型）。
    """
    if rows is None:
        return []
    if isinstance(rows, str):
        parts = [part.strip() for part in rows.replace("；", "\n").replace(";", "\n").splitlines() if part.strip()]
        rows = parts
    elif isinstance(rows, dict):
        name_keys = ("name", "名称", "地名", "地点", "名字", "title", "label")
        if any(key in rows for key in name_keys) or all(
            key in ("description", "desc", "detail", "summary", "intro", "atmosphere", "氛围", "描述", "简介")
            for key in rows
        ):
            rows = [rows]
        else:
            # 「名称→描述」映射：键作为地名，值作为描述 / 补充字段
            mapped = []
            for key, value in rows.items():
                if isinstance(value, dict):
                    item = dict(value)
                    item.setdefault("name", key)
                elif isinstance(value, list):
                    item = {"name": key, "description": _one_line(value)}
                else:
                    item = {"name": key, "description": _text(value)}
                mapped.append(item)
            rows = mapped
    if not isinstance(rows, list):
        raise ValueError(f"locations 必须是数组或对象（实际收到 {type(rows).__name__}）")

    name_keys = ("name", "名称", "地名", "地点", "名字", "title", "label")
    desc_keys = ("description", "desc", "detail", "summary", "intro", "描述", "简介", "说明")
    atmos_keys = ("atmosphere", "氛围", "气氛", "调性", "mood")
    result: list[dict] = []
    for index, item in enumerate(rows):
        if isinstance(item, str):
            # 兼容「名称 | 描述 | 氛围」写法
            parts = [part.strip() for part in item.split("|")]
            item = {
                "name": parts[0] if parts else "",
                "description": parts[1] if len(parts) > 1 else "",
                "atmosphere": parts[2] if len(parts) > 2 else "",
            }
        elif isinstance(item, (int, float, bool)):
            item = {"name": _text(item)}
        elif isinstance(item, list):
            item = {"name": _one_line(item)}
        if not isinstance(item, dict):
            raise ValueError(f"locations 第 {index + 1} 项类型无法识别（实际收到 {type(item).__name__}）")
        name = _pick_text(item, name_keys)
        description = _pick_text(item, desc_keys)
        atmosphere = _pick_text(item, atmos_keys)
        if not name:
            # 没有显式名称时，用第一个标量值/第一个键兜底，避免直接丢掉这条地点
            scalars = [value for value in item.values() if not isinstance(value, (dict, list)) and _text(value)]
            name = _text(scalars[0]) if scalars else _text(next(iter(item.keys()), ""))
            description = description or _one_line({key: value for key, value in item.items() if _text(value) != name})
        if not name:
            continue
        result.append({"name": name, "description": description, "atmosphere": atmosphere})
    return result


def _locations(rows) -> list[dict]:
    """清洗地点数组，补齐 loc_ ID 与固定结构并去掉空项。

    参数:
        rows: 原始地点数组（元素可为对象、字符串或名称映射）。

    返回:
        [{id, name, description, atmosphere}]。

    异常:
        ValueError: 顶层类型无法识别。
    """
    result: list[dict] = []
    for item in normalize_locations(rows):
        result.append(
            {
                "id": new_id("loc"),
                "name": item["name"][:120],
                "description": _text(item.get("description"))[:1000],
                "atmosphere": _text(item.get("atmosphere"))[:300],
            }
        )
    return result


def normalize_glossary(rows) -> list[dict]:
    """把模型返回的名词表归一化成对象数组。

    支持数组、单个词条对象、「术语→释义」映射与整段字符串；释义可能是对象/数组，
    别名可能写成顿号/逗号分隔的字符串，这里一并展开，尽量保留内容。

    参数:
        rows: 原始词条字段。

    返回:
        [{term, meaning, aliases?}]。

    异常:
        ValueError: 顶层类型无法识别（错误信息带实际类型）。
    """
    if rows is None:
        return []
    if isinstance(rows, str):
        # 模型有时把词条写成整段文本，按换行/分号拆成多项
        rows = [part.strip() for part in rows.replace("；", "\n").replace(";", "\n").splitlines() if part.strip()]
    if isinstance(rows, dict):
        # 单个词条对象，或「术语→释义」映射，统一成数组；释义可能是对象/数组
        if any(key in rows for key in ("term", "name", "word")):
            rows = [rows]
        else:
            mapped = []
            for key, value in rows.items():
                if isinstance(value, dict):
                    # 值本身是词条对象：保留 meaning/aliases 等字段，只补 term
                    item = {sub_key: sub_value for sub_key, sub_value in value.items() if sub_key != "term"}
                    item["term"] = key
                    mapped.append(item)
                else:
                    mapped.append({"term": key, "meaning": _one_line(value)})
            rows = mapped
    if not isinstance(rows, list):
        raise ValueError(f"glossary 必须是数组或对象（实际收到 {type(rows).__name__}）")
    result: list[dict] = []
    for item in rows:
        if isinstance(item, str):
            # 「术语：释义」字符串拆成对象；拆不出释义的跳过
            text = item.strip()
            for sep in ("：", ":", "——", " - ", "—"):
                if sep in text:
                    head, tail = text.split(sep, 1)
                    item = {"term": head.strip(), "meaning": tail.strip()}
                    break
            else:
                continue
        if not isinstance(item, dict):
            continue
        term = _text(item.get("term") or item.get("name") or item.get("word"))
        meaning = _text(
            item.get("meaning") or item.get("definition") or item.get("description") or item.get("desc")
        )
        if not term or not meaning:
            # 缺术语或释义的条目直接跳过，不让整次生成失败
            continue
        entry = {"term": term[:120], "meaning": meaning[:1000]}
        aliases = _str_list(item.get("aliases"), 120)
        if aliases:
            # 模型有时把多个别名写成一个字符串（顿号/逗号分隔），拆开保留
            expanded = []
            for alias in aliases:
                expanded.extend(part.strip() for part in re.split(r"[、,，]", alias) if part.strip())
            entry["aliases"] = [alias[:120] for alias in expanded]
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
        glossary=normalize_glossary(raw.get("glossary")),
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
    """拼一条设定图提示词（世界封面 / 角色全身像 / 头像 / 三视图）。

    参数:
        kind: world|fullbody|closeup|turnaround。
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
    elif kind == "fullbody":
        parts = ["角色全身像，正面站立，完整全身入镜，浅灰纯色背景，柔和棚拍光", entity.get("appearance_prompt"), visual]
        negative = str(entity.get("negative_prompt") or "").strip() or "低质, 多余手指, 水印, 文字, 裁掉头部, 只有半身"
    elif kind == "closeup":
        parts = [
            "角色半身脸部特写，与参考图完全同一个人，面容/发型/服装严格一致，浅灰纯色背景",
            entity.get("appearance_prompt"),
            visual,
        ]
        negative = str(entity.get("negative_prompt") or "").strip() or "不同的人, 换脸, 面部不一致, 风格不一致, 低质, 多余手指, 水印, 文字"
    elif kind == "turnaround":
        parts = [
            "角色三视图设定稿，正面/侧面/背面并排，全身，与参考图完全同一个人，面容/发型/服装严格一致，浅灰纯色背景",
            entity.get("appearance_prompt"),
            visual,
        ]
        negative = str(entity.get("negative_prompt") or "").strip() or "不同的人, 换脸, 面部不一致, 风格不一致, 多余人物, 低质, 多余手指, 水印, 文字"
    else:
        raise ValueError(f"不支持的配图类型: {kind}（可选 world / fullbody / closeup / turnaround）")

    prompt = "，".join(str(part).strip() for part in parts if str(part or "").strip())
    if not prompt:
        raise ValueError("配图提示词为空：世界缺少名称/概述，或角色缺少外貌提示词")
    try:
        out_width = int(width or style.get("width") or 1024)
        out_height = int(height or style.get("height") or 1024)
    except (TypeError, ValueError):
        out_width, out_height = 1024, 1024
    return {"prompt": prompt, "negative_prompt": negative, "width": out_width, "height": out_height}
