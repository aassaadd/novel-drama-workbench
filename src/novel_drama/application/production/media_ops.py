"""媒体生成与成片导出：页面与 CLI 共用一份用例。

只做「读章节 → 调用适配器/导出器 → 返回结构化结果」，不打印、不拼命令串。
"""

from __future__ import annotations

import json
from pathlib import Path

from ...adapters.persistence import ProjectStore
from ...composition import (
    JianyingExportError,
    check_cast,
    export_bundle,
    export_jianying,
    run_media,
    write_export,
)
from ...domain.shared.errors import PreconditionError
from .lines_source import chapter_view
from .timeline import silence_events


def cast_warning_text(store: ProjectStore, chapter_id: str) -> str | None:
    """生成「音色/BGM 编排提示」的一行文本（与 CLI 打到 stderr 的内容一致）。

    参数:
        store: 工程。
        chapter_id: 章节 ID。

    返回:
        有提示时返回 `[warn] …` 一行；没有问题返回 None。
    """
    warnings = check_cast(store, [chapter_view(store, store.load_chapter(chapter_id))])["findings"]
    if not warnings:
        return None
    detail = "；".join(item["message"] for item in warnings[:5])
    return f"[warn] 音色/BGM 编排提示 {len(warnings)} 条：{detail}"


def run_media_kind(
    store: ProjectStore,
    config,
    chapter_id: str,
    kind: str,
    force: bool = False,
    limit: int | None = None,
) -> dict:
    """执行一类媒体生成（音效 / 关键图 / 短视频）。

    参数:
        store: 工程。
        config: 本机配置。
        chapter_id: 章节 ID。
        kind: `sfx` / `image` / `video`。
        force: 已就绪项也重新生成。
        limit: 本次最多处理条数。

    返回:
        `run_media` 的结构化结果（就绪/跳过/失败与逐条明细）。

    异常:
        FileNotFoundError: 章节不存在。
    """
    chapter = store.load_chapter(chapter_id)
    return run_media(store, config, chapter, kind, force=force, limit=limit)


def export_chapter(
    store: ProjectStore,
    config,
    chapter_id: str,
    fmt: str = "srt",
    out_dir: str | None = None,
) -> dict:
    """导出章节成片（SRT / 剪映草稿 / 交付包）。

    参数:
        store: 工程。
        config: 本机配置。
        chapter_id: 章节 ID。
        fmt: `srt`（默认）/ `jianying` / `bundle`。
        out_dir: 自定义输出目录；为空时按格式落到 `exports/` 下。

    返回:
        各格式的结构化结果（与页面接口原样返回的字段一致）。

    异常:
        PreconditionError: 时间轴不存在，或剪映导出缺少素材。
        FileNotFoundError: 章节不存在。
    """
    path = store.timeline_path(chapter_id)
    if not path.exists():
        raise PreconditionError("时间轴不存在，先执行 ndw timeline build")
    timeline = json.loads(path.read_text(encoding="utf-8"))
    chapter = chapter_view(store, store.load_chapter(chapter_id))
    characters = {item["id"]: item for item in store.load_characters()["characters"]}
    target_dir = Path(out_dir) if out_dir else store.root / "exports"
    # 静音事件只从导演脚本派生，缺失脚本时为空列表
    silences = silence_events(store, chapter_id)
    if fmt == "jianying":
        try:
            return export_jianying(
                store,
                chapter,
                timeline,
                drafts_root=config.get("export.jianying.drafts_root"),
                ffprobe=config.get("ffmpeg.probe_binary", "ffprobe"),
                out_dir=(Path(out_dir) if out_dir else store.root / "exports" / "jianying"),
            )
        except JianyingExportError as error:
            # 素材缺失等属于前置条件未满足：命令行与网页都拿到可读原因，而不是 500
            raise PreconditionError(str(error)) from error
    if fmt == "bundle":
        return export_bundle(
            store,
            chapter,
            timeline,
            characters,
            silences,
            out_dir=target_dir,
            binary=config.get("ffmpeg.binary", "ffmpeg"),
        )
    return write_export(timeline, chapter, characters, target_dir, silences)
