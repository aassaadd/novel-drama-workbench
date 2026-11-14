"""重新切分场景 / 台词的影响分析与人工修改保护。

设计原则：
- 影响清单直接复用 `text/splitter.py` 的规则切分结果，不另起一套估算；
- 「人工修改」判定：与规则切分结果相比，台词文本或说话人不同（含标注阶段改动）；
- 保留模式下，同文本旧行原样复用（保留 id / 配音 / 情绪等），匹配不到的旧行按原位置插回；
- 只读统计，不写盘、不删除任何已有音频与媒体文件。
"""

from __future__ import annotations

import copy

from ..models import entities
from ..text.splitter import split_lines, split_scenes

#: 影响清单里最多列出的明细条数（完整数量在计数里，明细只用于让用户点名确认）
_MAX_SAMPLES = 10


def _clean(text: str | None) -> str:
    """规整文本用于比对（去首尾空白，兼容 None）。"""
    return (text or "").strip()


def _usable(line: dict) -> bool:
    """判断旧台词行能否参与复用与插回。

    文本含换行的行无法在正文里按原文定位（历史切分把两个自然段并成了一行），
    保留它会让「行内改词」永久报「正文已变更」，因此重切时直接丢弃。
    """
    return "\n" not in (line.get("text") or "")


def line_has_audio(line: dict) -> bool:
    """判断一条台词是否已有配音产物（有资产或状态为 ready）。"""
    tts = line.get("tts") or {}
    return bool(tts.get("asset_id")) or tts.get("status") == "ready"


def classify_lines(existing: list[dict], drafts: list[dict]) -> tuple[list[tuple[dict, dict]], list[tuple[dict, str]]]:
    """把旧台词行按「能否被规则结果覆盖」分类。

    参数:
        existing: 场景里已有的台词行（可能被人工改过）。
        drafts: 规则切分出的台词草案。

    返回:
        (matched, manual)：
        - matched: [(旧行, 对应草案)]，文本完全相同，可原样复用；
        - manual: [(旧行, 中文原因)]，文本对不上或说话人与规则不同。
    """
    pool = [row for row in existing if _usable(row)]
    matched: list[tuple[dict, dict]] = []
    for draft in drafts:
        for index, row in enumerate(pool):
            if _clean(row.get("text")) == _clean(draft.get("text")):
                matched.append((pool.pop(index), draft))
                break
    manual: list[tuple[dict, str]] = []
    for row in pool:
        manual.append((row, "台词文本与规则切分结果不一致（人工改写或新增）"))
    for old, draft in matched:
        if old.get("speaker_id") != draft.get("speaker_id"):
            manual.append((old, "说话人与规则识别结果不一致（人工或标注改动）"))
    return matched, manual


def merge_manual_lines(existing: list[dict], drafts: list[dict], scene_id: str) -> list[dict]:
    """按「保留人工修改」重切：同文本旧行复用，剩余旧行插回原位。

    参数:
        existing: 场景已有台词行。
        drafts: 规则切分草案。
        scene_id: 目标场景 ID（旧行可能来自另一个场景，需要改写归属）。

    返回:
        合并后的台词行列表，index 已重排为 0..N-1。
    """
    remaining = [copy.deepcopy(item) for item in existing if _usable(item)]
    merged: list[dict] = []
    for draft in drafts:
        picked = None
        for index, row in enumerate(remaining):
            if _clean(row.get("text")) == _clean(draft.get("text")):
                picked = remaining.pop(index)
                break
        if picked is None:
            picked = entities.new_line(
                scene_id,
                len(merged),
                draft["text"],
                kind=draft["kind"],
                speaker_id=draft["speaker_id"],
                confidence=draft.get("confidence"),
            )
        picked["scene_id"] = scene_id
        merged.append(picked)
    # 规则结果里没有的旧行（人工改写 / 人工新增）按原位置插回，避免静默丢失
    for row in remaining:
        row["scene_id"] = scene_id
        position = max(0, min(int(row.get("index") or 0), len(merged)))
        merged.insert(position, row)
    for index, row in enumerate(merged):
        row["index"] = index
    return merged


def _script_info(store, chapter: dict) -> dict:
    """读取本章导演标注状态（只读，失败按无标注处理）。"""
    try:
        path = store.script_path(chapter["id"])
    except Exception:
        return {"exists": False, "shots": 0}
    if not path.exists():
        return {"exists": False, "shots": 0}
    try:
        script = store.load_script(chapter["id"])
        return {"exists": True, "shots": len(script.get("shots") or [])}
    except Exception:
        return {"exists": True, "shots": 0}


def _scene_lines(scene: dict) -> list[dict]:
    """取场景台词行列表。"""
    return list(scene.get("lines") or [])


def _sample(scene_index: int, line: dict, reason: str) -> dict:
    """构造一条影响明细（用于页面点名人工修改的行）。"""
    return {
        "scene": scene_index + 1,
        "line_id": line.get("id"),
        "text": (line.get("text") or "")[:40],
        "speaker_id": line.get("speaker_id"),
        "has_audio": line_has_audio(line),
        "reason": reason,
    }


def analyze_scene_split(store, chapter: dict, characters: list[dict], max_chars: int = 1200, keep_lines: bool = True) -> dict:
    """统计「重新切分场景」的影响清单。

    参数:
        store: 工程读写入口（只读脚本状态）。
        chapter: 章节 dict。
        characters: 角色表（用于判断说话人是否被改过）。
        max_chars: 单场景软上限，与 `scene split --max-chars` 一致。
        keep_lines: 是否保留「正文未变化场景」的台词与媒体引用。

    返回:
        dict：结构化计数 + `text`（中文影响清单）。
    """
    drafts = split_scenes(chapter.get("content") or "", max_chars=max_chars)
    old_scenes = sorted(chapter.get("scenes") or [], key=lambda item: item.get("index") or 0)
    pool = list(old_scenes) if keep_lines else []
    reused: list[tuple[dict, dict]] = []
    for draft in drafts:
        for index, scene in enumerate(pool):
            if _clean(scene.get("source_text")) == _clean(draft["content"]):
                reused.append((pool.pop(index), draft))
                break
    # 不保留时所有旧场景都按「会被重建」处理；保留时只有没匹配上的场景会丢
    lost = list(pool) if keep_lines else list(old_scenes)

    total_lines = sum(len(_scene_lines(scene)) for scene in old_scenes)
    total_audio = sum(1 for scene in old_scenes for line in _scene_lines(scene) if line_has_audio(line))
    total_manual = 0
    for scene in old_scenes:
        _, manual = classify_lines(_scene_lines(scene), split_lines(scene.get("source_text") or "", characters))
        total_manual += len(manual)

    reuse_lines = sum(len(_scene_lines(scene)) for scene, _ in reused) if keep_lines else 0
    reuse_audio = (
        sum(1 for scene, _ in reused for line in _scene_lines(scene) if line_has_audio(line)) if keep_lines else 0
    )

    lost_lines = lost_audio = lost_manual = 0
    samples: list[dict] = []
    for scene in lost:
        lines = _scene_lines(scene)
        _, manual = classify_lines(lines, split_lines(scene.get("source_text") or "", characters))
        lost_lines += len(lines)
        lost_audio += sum(1 for line in lines if line_has_audio(line))
        lost_manual += len(manual)
        for line, reason in manual:
            if len(samples) < _MAX_SAMPLES:
                samples.append(_sample(int(scene.get("index") or 0), line, reason))

    script = _script_info(store, chapter)
    created = len(drafts) - len(reused) if keep_lines else len(drafts)
    # 页面「拆场景预览」需要逐段展示「原文段落 → 归属场景」，这里直接给出规则切分结果
    reused_ids = {id(scene) for scene, _ in reused}
    preview = {
        "scenes": [
            {
                "index": index,
                "title": draft.get("title") or f"场景 {index + 1}",
                "chars": len(draft.get("content") or ""),
                "excerpt": (draft.get("content") or "").strip()[:60],
            }
            for index, draft in enumerate(drafts)
        ],
        "reused": [
            {"index": int(scene.get("index") or 0), "title": scene.get("title") or "", "lines": len(_scene_lines(scene))}
            for scene in old_scenes
            if id(scene) in reused_ids
        ],
        "rebuilt": [
            {"index": int(scene.get("index") or 0), "title": scene.get("title") or "", "lines": len(_scene_lines(scene))}
            for scene in old_scenes
            if id(scene) not in reused_ids
        ],
    }

    text = [
        f"重新切分场景影响：已有 {len(old_scenes)} 个场景 / {total_lines} 行台词，规则将切出 {len(drafts)} 个场景。",
        (
            f"保留人工修改（推荐）：{len(reused)} 个场景正文未变化，可沿用 {reuse_lines} 行台词（其中已有配音 {reuse_audio} 条）；"
            f"另有 {len(lost)} 个场景会重建并丢失 {lost_lines} 行台词。"
            if keep_lines
            else f"全部按规则重切：{len(old_scenes)} 个场景全部重建，{total_lines} 行台词全部丢失。"
        ),
        f"丢失台词中人工/标注改动 {lost_manual} 行，已有配音 {lost_audio} 条。",
        ("本章已有导演标注：是（" + str(script["shots"]) + " 个镜头），场景重切后必须重新生成标注。") if script["exists"] else "本章暂无导演标注。",
        "场景重切后需要重新切分台词；已有音频/图片/视频文件不会删除，但会失去引用，需要重新生成。",
    ]
    if samples:
        text.append("点名的改动行：")
        text.extend(f"- 场景 {item['scene']} / {item['reason']}：{item['text']}" for item in samples)

    return {
        "mode": "scene",
        "keep": bool(keep_lines),
        "existing": {
            "scenes": len(old_scenes),
            "lines": total_lines,
            "audio": total_audio,
            "manual": total_manual,
            "script_shots": script["shots"],
        },
        "planned": {"scenes": len(drafts)},
        "reuse": {"scenes": len(reused), "lines": reuse_lines, "audio": reuse_audio},
        "lost": {
            "scenes": len(lost),
            "lines": lost_lines,
            "manual": lost_manual,
            "audio": lost_audio,
            "samples": samples,
        },
        "created": created,
        "need_audio": lost_lines,
        "script": script,
        "preview": preview,
        "text": "\n".join(text),
    }


def analyze_line_split(store, chapter: dict, characters: list[dict], scene_ids: list[str] | None = None, keep_manual: bool = True) -> dict:
    """统计「重新切分台词」的影响清单。

    参数:
        store: 工程读写入口（只读脚本状态）。
        chapter: 章节 dict。
        characters: 角色表。
        scene_ids: 只统计这些场景；为空表示全部。
        keep_manual: 是否保留人工修改的台词行。

    返回:
        dict：结构化计数 + `text`（中文影响清单）。
    """
    scenes = [scene for scene in chapter.get("scenes") or [] if not scene_ids or scene.get("id") in scene_ids]
    existing_total = draft_total = manual_total = audio_total = new_lines = kept_audio = 0
    samples: list[dict] = []
    for scene in scenes:
        existing = _scene_lines(scene)
        drafts = split_lines(scene.get("source_text") or "", characters)
        matched, manual = classify_lines(existing, drafts)
        existing_total += len(existing)
        draft_total += len(drafts)
        manual_total += len(manual)
        audio_total += sum(1 for line in existing if line_has_audio(line))
        new_lines += len(drafts) - len(matched)
        kept_audio += sum(1 for line, _ in matched if line_has_audio(line))
        if keep_manual:
            # 人工改写/新增的行也会原样插回，它们的配音引用同样保留
            kept_audio += sum(1 for line, _ in manual if line_has_audio(line))
        for line, reason in manual:
            if len(samples) < _MAX_SAMPLES:
                samples.append(_sample(int(scene.get("index") or 0), line, reason))

    script = _script_info(store, chapter)
    need_audio = new_lines + (0 if keep_manual else max(0, manual_total))
    text = [
        f"重新切分台词影响：{len(scenes)} 个场景 / 已有 {existing_total} 行台词，规则将切出 {draft_total} 行。",
        (
            f"保留人工修改（推荐）：{manual_total} 行人工/标注改动会原样保留，沿用已有配音 {kept_audio} 条，"
            f"仍需重新生成配音 {new_lines} 行。"
            if keep_manual
            else f"全部按规则重切：{manual_total} 行人工/标注改动会丢失，{draft_total} 行全部按规则重建。"
        ),
        ("本章已有导演标注：是（" + str(script["shots"]) + " 个镜头），台词重切后锚点会变化，需要重新生成标注。") if script["exists"] else "本章暂无导演标注。",
        "台词重切不影响场景划分；已有音频文件不会删除，但被替换的行会失去引用并需要重新生成。",
    ]
    if samples:
        text.append("点名的改动行：")
        text.extend(f"- 场景 {item['scene']} / {item['reason']}：{item['text']}" for item in samples)

    return {
        "mode": "line",
        "keep": bool(keep_manual),
        "existing": {
            "scenes": len(scenes),
            "lines": existing_total,
            "audio": audio_total,
            "manual": manual_total,
            "script_shots": script["shots"],
        },
        "planned": {"lines": draft_total},
        "reuse": {"lines": existing_total if keep_manual else 0, "audio": kept_audio if keep_manual else 0},
        "lost": {
            "lines": max(0, existing_total - draft_total) if not keep_manual else 0,
            "manual": 0 if keep_manual else manual_total,
            "audio": 0 if keep_manual else audio_total,
            "samples": samples,
        },
        "need_audio": need_audio,
        "script": script,
        "text": "\n".join(text),
    }
