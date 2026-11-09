"""混流（音画合成）接口定义：本轮只出计划，执行明确报 NOT_IMPLEMENTED。

输入：混音计划（语音/音效混音结果）+ 时间轴上的视频与关键图；
输出：MP4（H.264 + AAC）。真实执行依赖图像/视频产物与编码资源，等机器空闲后再接。
"""

from __future__ import annotations

from pathlib import Path

from ..adapters.base import not_implemented


def plan_mux(timeline: dict, project_root: str | Path, mix_plan: dict | None = None) -> dict:
    """生成混流计划（不执行）。

    参数:
        timeline: 时间轴 dict。
        project_root: 工程根目录。
        mix_plan: `plan_mix` 的结果；为空时只描述视频/画面输入。

    返回:
        {
          status: "NOT_IMPLEMENTED",
          audio: 混音输出路径或 None,
          video_segments: [...], images: [...],
          output: 目标 MP4 路径,
          codec: {...}, blockers: [...]
        }
    """
    root = Path(project_root)
    videos = []
    images = []
    for track in timeline.get("tracks") or []:
        if track.get("kind") == "video":
            videos = [item for item in track.get("items") or [] if item.get("asset_id")]
        elif track.get("kind") == "image":
            images = [item for item in track.get("items") or [] if item.get("asset_id")]

    blockers = []
    if not (mix_plan or {}).get("output"):
        blockers.append("缺少混音输出：先执行 ndw mix --render（当前未实现）")
    if not videos:
        blockers.append("没有已选中的短视频：MP4 只能由静态图轮播降级生成")

    return {
        "status": "NOT_IMPLEMENTED",
        "reason": "混流（音画合成 → MP4）本轮只定义接口，未接入编码执行",
        "audio": (mix_plan or {}).get("output"),
        "video_segments": videos,
        "images": images,
        "output": str(root / "renders" / f"{timeline.get('chapter_id')}.mp4"),
        "codec": {
            "video": "libx264",
            "audio": "aac",
            "fps": 24,
            "width": 1280,
            "height": 720,
            "pix_fmt": "yuv420p",
        },
        "blockers": blockers,
    }


def mux(plan: dict) -> dict:
    """执行混流（未实现）。

    参数:
        plan: `plan_mux` 的返回。

    异常:
        AdapterError(NOT_IMPLEMENTED): 始终抛出。
    """
    raise not_implemented(
        "混流",
        "M7 实现：按时间轴把视频段/关键图与混音轨合成 MP4（libx264 + AAC），并校验音画同步",
    )
