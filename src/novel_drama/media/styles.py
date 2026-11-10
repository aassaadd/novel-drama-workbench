"""画面风格预设：写实 / 动漫 / 古风等固定模板，图片与视频共用。

风格词只存一份：项目设定库 `style.visual_style`。页面下拉选中预设后写入该字段，
渲染图片与视频时统一把风格词拼进提示词，保证跨镜头、跨类型风格一致。

风格清单固定在 `config/comfy/styles.json`，与 ComfyUI 模板同级维护。
"""

from __future__ import annotations

import json
from pathlib import Path


def load_styles(templates_dir: Path) -> list[dict]:
    """读取固定风格预设清单。

    参数:
        templates_dir: ComfyUI 模板目录（`config/comfy`），同目录下取 `styles.json`。

    返回:
        `[{"id", "label", "prompt"}]`；文件缺失或格式不对时返回空列表（页面退化为自由输入）。
    """
    path = Path(templates_dir) / "styles.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    items = data.get("styles") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    styles: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        style_id = str(item.get("id") or "").strip()
        label = str(item.get("label") or "").strip()
        prompt = str(item.get("prompt") or "").strip()
        if style_id and label and prompt:
            styles.append({"id": style_id, "label": label, "prompt": prompt})
    return styles


def with_style(text: str, settings: dict) -> str:
    """把项目统一的画面风格词拼到提示词末尾（已包含时不重复拼）。

    参数:
        text: 原始提示词（分镜 / 镜头描述）。
        settings: 工程设置，取 `style.visual_style`。

    返回:
        追加了风格词的提示词；未设置风格时原样返回。
    """
    visual = str(((settings or {}).get("style") or {}).get("visual_style") or "").strip()
    base = str(text or "").strip()
    if not visual or visual in base:
        return base
    if not base:
        return visual
    return f"{base}，画面风格：{visual}"
