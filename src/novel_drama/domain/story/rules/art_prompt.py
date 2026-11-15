"""设定图提示词拼装：把世界 / 角色条目与画风拼成一条出图提示词。

纯规则，不读写文件、不调用适配器；渲染与落盘由 `adapters/media/art.py` 负责。
"""

from __future__ import annotations


def build_art_prompt(
    kind: str,
    entity: dict,
    style: dict | None = None,
    width: int | None = None,
    height: int | None = None,
) -> dict:
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
