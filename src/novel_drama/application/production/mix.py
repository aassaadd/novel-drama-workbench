"""章节混音：按时间轴分轨，再混音并做响度规整。

页面与 CLI 共用这一份用例；页面把它丢进独立子进程执行（见
`interface/http/executor.py`），避免合成期间阻塞其它接口。
"""

from __future__ import annotations

import json

from ...adapters.media.bgm import load_cues
from ...adapters.media.mixer import plan_mix, render_mix
from ...adapters.persistence import ProjectStore
from ...domain.shared.errors import PreconditionError
from ...infrastructure.config import load_config


def mix_chapter(
    root: str,
    chapter_id: str,
    config_path: str | None = None,
    render: bool = True,
) -> dict:
    """执行（或只规划）一章的混音。

    参数:
        root: 工程目录。
        chapter_id: 章节 ID。
        config_path: 本机配置文件路径（取 ffmpeg 与响度参数）。
        render: False 时只返回混音计划，不做任何渲染。

    返回:
        render 为 True 时为 `render_mix` 的结果（分轨 / 混音 / 规整三段的路径与指标），
        否则为混音计划 dict。

    异常:
        PreconditionError: 时间轴尚未构建。
        AdapterError: ffmpeg 缺失或渲染失败。
    """
    store = ProjectStore(root)
    path = store.timeline_path(chapter_id)
    if not path.exists():
        raise PreconditionError(f"时间轴不存在: {path}（先执行 ndw timeline build）")
    timeline = json.loads(path.read_text(encoding="utf-8"))
    plan = plan_mix(timeline, store.root, bgm_cues=load_cues(store.root, chapter_id))
    if not render:
        return plan
    config = load_config(config_path)
    return render_mix(
        plan,
        binary=config.get("ffmpeg.binary", "ffmpeg"),
        audio_cfg=config.get("audio"),
        loudness_target=config.get("output.loudness_target_lufs", -16),
    )
