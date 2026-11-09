"""交付包导出：把一章的可交付产物收进 `exports/` 并生成清单。

产物形态（全部可在任意播放器打开）：
- `<chapter>.srt` 字幕、`<chapter>.manifest.json` 时间轴清单、`<chapter>.schedule.tsv` 时间表；
- `<chapter>.mp3` 整章音频（由混音规整轨转码）；
- `<chapter>.mp4` 音画成品（混流结果存在时才收录）；
- `<chapter>.bundle.json` 交付清单（kind/status/path/size/sha256）；
- `README.txt` 一句话交付说明。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from ..models.ids import new_id, now_iso
from ..store import ProjectStore
from .export import write_export


def _sha256(path: Path) -> str:
    """计算文件 sha256。

    参数:
        path: 文件。

    返回:
        `sha256:<hex>`。
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def _artifact(kind: str, path: Path, scope: str = "chapter") -> dict:
    """构造一条交付产物记录（对齐 export-artifact schema）。

    参数:
        kind: mp4/mp3/wav/srt/manifest。
        path: 产物路径。
        scope: chapter/work。

    返回:
        产物记录 dict；文件不存在时状态为 failed。
    """
    exists = path.exists()
    return {
        "id": new_id("exp"),
        "scope": scope,
        "kind": kind,
        "path": str(path),
        "status": "ready" if exists else "failed",
        "size_bytes": path.stat().st_size if exists else 0,
        "sha256": _sha256(path) if exists else None,
        "created_at": now_iso(),
    }


def _to_mp3(source: Path, target: Path, binary: str = "ffmpeg") -> None:
    """把 wav 转成 192kbps MP3。

    参数:
        source: 源 wav。
        target: 目标 mp3。
        binary: ffmpeg。

    异常:
        RuntimeError: 转码失败。
    """
    proc = subprocess.run(
        [binary, "-hide_banner", "-loglevel", "error", "-y", "-i", str(source), "-codec:a", "libmp3lame", "-b:a", "192k", str(target)],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-6:]
        raise RuntimeError(f"MP3 转码失败（退出码 {proc.returncode}）：\n" + "\n".join(tail))


def export_bundle(
    store: ProjectStore,
    chapter: dict,
    timeline: dict,
    characters: dict,
    silence_events: list[dict] | None = None,
    out_dir: Path | None = None,
    binary: str = "ffmpeg",
    mix_path: str | None = None,
    mp4_path: str | None = None,
) -> dict:
    """导出整章交付包。

    参数:
        store: 工程。
        chapter: 章节 dict。
        timeline: 时间轴 dict。
        characters: {角色 ID: 角色}。
        silence_events: 导演脚本里的静音事件。
        out_dir: 输出目录（默认 exports/）。
        binary: ffmpeg。
        mix_path: 混音规整轨；为空时自动找 renders/<chapter>.mix.norm.wav。
        mp4_path: 混流成品；为空时自动找 renders/<chapter>.mp4。

    返回:
        {"dir", "artifacts", "bundle", "readme", "missing"}。
    """
    target_dir = Path(out_dir) if out_dir else store.root / "exports"
    target_dir.mkdir(parents=True, exist_ok=True)
    chapter_id = chapter["id"]

    base = write_export(timeline, chapter, characters, target_dir, silence_events or [])
    artifacts: list[dict] = [
        _artifact("srt", Path(base["srt"])),
        _artifact("manifest", Path(base["manifest"])),
    ]
    missing: list[str] = []

    mix_candidate = Path(mix_path) if mix_path else store.root / "renders" / f"{chapter_id}.mix.norm.wav"
    mp3_path = target_dir / f"{chapter_id}.mp3"
    if mix_candidate.exists():
        _to_mp3(mix_candidate, mp3_path, binary=binary)
        artifacts.insert(0, _artifact("mp3", mp3_path))
    else:
        missing.append(f"混音轨缺失：{mix_candidate}（先执行 ndw mix --render）")

    mp4_candidate = Path(mp4_path) if mp4_path else store.root / "renders" / f"{chapter_id}.mp4"
    mp4_target = target_dir / f"{chapter_id}.mp4"
    if mp4_candidate.exists():
        if mp4_candidate.resolve() != mp4_target.resolve():
            mp4_target.write_bytes(mp4_candidate.read_bytes())
        artifacts.insert(0, _artifact("mp4", mp4_target))
    else:
        missing.append(f"混流成品缺失：{mp4_candidate}（先执行 ndw mux --render）")

    bundle = {
        "schema_version": 1,
        "chapter_id": chapter_id,
        "chapter_title": chapter.get("title"),
        "work_id": chapter.get("work_id"),
        "duration_ms": int(timeline.get("duration_ms") or 0),
        "generated_at": now_iso(),
        "artifacts": artifacts,
        "missing": missing,
    }
    bundle_path = target_dir / f"{chapter_id}.bundle.json"
    bundle_path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    readme = target_dir / "README.txt"
    lines = [
        f"《{chapter.get('title')}》有声剧交付包",
        f"章节：{chapter_id}　时长：{bundle['duration_ms'] / 1000:.1f}s　生成时间：{bundle['generated_at']}",
        "",
        "文件说明：",
    ]
    for item in artifacts:
        lines.append(f"  - {Path(item['path']).name}（{item['kind']}，{item['status']}，{item['size_bytes'] / 1024:.0f}KB）")
    lines.append(f"  - {bundle_path.name}（交付清单）")
    if missing:
        lines.extend(["", "缺失项："] + [f"  ! {item}" for item in missing])
    readme.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return {"dir": str(target_dir), "artifacts": artifacts, "bundle": str(bundle_path), "readme": str(readme), "missing": missing}
