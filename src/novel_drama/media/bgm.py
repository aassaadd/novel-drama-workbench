"""BGM 轨道：章节级背景音乐 cue 表（独立文件，不塞进 timeline schema）。

每个 cue 描述「哪个音频文件、从第几毫秒开始、增益多少、淡入淡出、是否循环」。
音频文件本身存放在 `assets/audio/bgm/`，cue 表在 `bgm/<chapter_id>.json`。
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from ..models import new_id

_TEMPLATE = {"schema_version": 1, "chapter_id": None, "cues": []}


def cue_path(root: str | Path, chapter_id: str) -> Path:
    """cue 表路径。"""
    return Path(root) / "bgm" / f"{chapter_id}.json"


def load_cues(root: str | Path, chapter_id: str) -> list[dict]:
    """读取章节 BGM cue；缺失时返回空列表。

    参数:
        root: 工程根目录。
        chapter_id: 章节 ID。

    返回:
        cue 列表。
    """
    path = cue_path(root, chapter_id)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return data.get("cues") or []


def save_cues(root: str | Path, chapter_id: str, cues: list[dict]) -> Path:
    """原子写入章节 BGM cue 表。

    参数:
        root: 工程根目录。
        chapter_id: 章节 ID。
        cues: cue 列表。

    返回:
        写入路径。
    """
    path = cue_path(root, chapter_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {**_TEMPLATE, "chapter_id": chapter_id, "cues": cues}
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def import_asset(root: str | Path, source: str | Path) -> str:
    """把外部音频复制进工程 `assets/audio/bgm/`，返回相对路径。

    参数:
        root: 工程根目录。
        source: 外部音频文件。

    返回:
        相对工程根目录的路径。
    """
    src = Path(source).expanduser()
    if not src.exists():
        raise FileNotFoundError(f"BGM 文件不存在: {src}")
    folder = Path(root) / "assets" / "audio" / "bgm"
    folder.mkdir(parents=True, exist_ok=True)
    dst = folder / src.name
    if src.resolve() != dst.resolve():
        shutil.copy2(src, dst)
    return str(dst.relative_to(root))


def add_cue(
    root: str | Path,
    chapter_id: str,
    asset_id: str,
    start_ms: int = 0,
    gain_db: float = -18.0,
    fade_in_ms: int = 1500,
    fade_out_ms: int = 2000,
    loop: bool = True,
    loop_until_ms: int | None = None,
) -> dict:
    """向章节追加一条 BGM cue。

    参数:
        root: 工程根目录。
        chapter_id: 章节 ID。
        asset_id: 音频相对路径。
        start_ms: 起始时间。
        gain_db: 增益（通常压低到 -18 dB 左右）。
        fade_in_ms: 淡入时长。
        fade_out_ms: 淡出时长。
        loop: 是否循环铺满。
        loop_until_ms: 循环结束时间；为空表示铺到章末。

    返回:
        新 cue。
    """
    cues = load_cues(root, chapter_id)
    cue = {
        "id": new_id("bgm"),
        "name": Path(asset_id).stem,
        "asset_id": asset_id,
        "start_ms": max(0, int(start_ms)),
        "gain_db": float(gain_db),
        "fade_in_ms": max(0, int(fade_in_ms)),
        "fade_out_ms": max(0, int(fade_out_ms)),
        "loop": bool(loop),
        "loop_until_ms": int(loop_until_ms) if loop_until_ms else None,
    }
    cues.append(cue)
    save_cues(root, chapter_id, cues)
    return cue
