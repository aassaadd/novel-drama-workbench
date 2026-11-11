"""时间轴构建：由台词行、音效、关键图、短视频计算各轨绝对起止时间。

规则：
- 语音轨按行顺序累加：start = 上一行结束 + pause_before，end = start + 时长(实测优先)，之后加 pause_after；
- 音效按 position 解析为绝对时间（锚定行 / 场景起点）；
- 关键图以锚点行起点出现，默认持续 3 秒或被下一个画面截断；
- 短视频替换同时间窗的画面，失败时使用 fallback 图；
- 未就绪的产物不进入时间轴，但必须写入 warnings。
"""

from __future__ import annotations

import hashlib
import json

from ..models import new_id
from ..models.entities import provisional_line_duration_ms, speaker_display_name

# 关键图默认展示时长
DEFAULT_IMAGE_HOLD_MS = 3000


def compute_source_hash(chapter: dict, silence_events: list[dict] | None = None) -> str:
    """计算时间轴输入哈希（行 revision、实测时长、音效/图/视频 revision、静音事件）。

    参数:
        chapter: 章节 dict。
        silence_events: 导演脚本静音事件（影响行间停顿）。

    返回:
        `sha256:<hex>`，用于判断时间轴是否需要重建。
    """
    payload = []
    for scene in chapter.get("scenes") or []:
        for line in scene.get("lines") or []:
            payload.append(["line", line.get("id"), line.get("revision"), (line.get("tts") or {}).get("duration_ms")])
        for sfx in scene.get("sfx_events") or []:
            payload.append(["sfx", sfx.get("id"), sfx.get("revision")])
        for image in scene.get("key_images") or []:
            payload.append(["image", image.get("id"), image.get("revision")])
        for video in scene.get("video_segments") or []:
            payload.append(["video", video.get("id"), video.get("revision")])
    for event in silence_events or []:
        payload.append(["silence", event.get("id"), event.get("duration_ms"), (event.get("position") or {})])
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _ordered_lines(chapter: dict) -> list[dict]:
    """按场景与行序号返回整章台词行。"""
    lines = []
    for scene in sorted(chapter.get("scenes") or [], key=lambda item: item.get("index") or 0):
        lines.extend(sorted(scene.get("lines") or [], key=lambda item: item.get("index") or 0))
    return lines


def apply_silences(silence_events: list[dict] | None, chapter: dict) -> tuple[dict, dict, list[str]]:
    """把导演脚本的静音事件折算成行前/行后额外停顿。

    参数:
        silence_events: `director-script` 的 `silence_events`。
        chapter: 章节 dict（用于定位首行/末行）。

    返回:
        (before, after, warnings)；before/after 为 {line_id: 毫秒}。
    """
    before: dict[str, int] = {}
    after: dict[str, int] = {}
    warnings: list[str] = []
    lines = _ordered_lines(chapter)
    if not lines or not silence_events:
        return before, after, warnings
    first_id, last_id = lines[0].get("id"), lines[-1].get("id")
    for event in silence_events:
        position = event.get("position") or {}
        mode = position.get("mode") or "after_line"
        line_id = position.get("line_id")
        duration = int(event.get("duration_ms") or 0)
        if duration <= 0:
            continue
        if mode in ("before_line", "shot_start"):
            key = line_id or first_id
            if key not in {item.get("id") for item in lines}:
                warnings.append(f"{event.get('id')} 静音事件锚定的台词行不存在，已挂到首行")
                key = first_id
            before[key] = before.get(key, 0) + duration
        else:  # after_line / during_line / shot_end
            key = line_id or last_id
            if key not in {item.get("id") for item in lines}:
                warnings.append(f"{event.get('id')} 静音事件锚定的台词行不存在，已挂到末行")
                key = last_id
            after[key] = after.get(key, 0) + duration
    return before, after, warnings


def build_schedule(timeline: dict, chapter: dict, characters: dict[str, dict] | None = None, silence_events: list[dict] | None = None) -> list[dict]:
    """由时间轴生成可核对的时间表（含台词行与停顿行）。

    参数:
        timeline: 时间轴 dict。
        chapter: 章节 dict。
        characters: {角色 ID: 角色}，用于显示说话人姓名。
        silence_events: 导演脚本静音事件，用于给停顿行补原因。

    返回:
        [{index, kind, line_id, speaker, text, start_ms, end_ms, duration_ms, estimated, note}]。
    """
    characters = characters or {}
    line_index = {line.get("id"): line for line in _ordered_lines(chapter)}
    voice_track = next((track for track in timeline.get("tracks") or [] if track.get("kind") == "voice"), None)
    items = sorted((voice_track or {}).get("items") or [], key=lambda item: item.get("start_ms") or 0)

    reasons_before: dict[str, str] = {}
    reasons_after: dict[str, str] = {}
    for event in silence_events or []:
        position = event.get("position") or {}
        line_id = position.get("line_id")
        if not line_id:
            continue
        reason = event.get("reason") or "静音事件"
        if (position.get("mode") or "") in ("before_line", "shot_start"):
            reasons_before[line_id] = reason
        else:
            reasons_after[line_id] = reason

    rows: list[dict] = []
    cursor = 0
    for item in items:
        start = int(item.get("start_ms") or 0)
        end = int(item.get("end_ms") or 0)
        if start > cursor:
            prev_id = rows[-1].get("line_id") if rows else None
            note = reasons_after.get(prev_id or "") or reasons_before.get(item.get("source_id") or "") or "停顿/静音填充"
            rows.append(
                {
                    "kind": "gap",
                    "line_id": None,
                    "speaker": "",
                    "text": "",
                    "start_ms": cursor,
                    "end_ms": start,
                    "duration_ms": start - cursor,
                    "estimated": False,
                    "note": note,
                }
            )
        line = line_index.get(item.get("source_id")) or {}
        rows.append(
            {
                "kind": "line",
                "line_id": item.get("source_id"),
                "speaker": speaker_display_name(line.get("speaker_id"), characters),
                "text": line.get("text") or item.get("label") or "",
                "start_ms": start,
                "end_ms": end,
                "duration_ms": end - start,
                "estimated": bool(item.get("estimated")),
                "note": "",
            }
        )
        cursor = max(cursor, end)

    total = int(timeline.get("duration_ms") or 0)
    if total > cursor:
        rows.append(
            {
                "kind": "gap",
                "line_id": None,
                "speaker": "",
                "text": "",
                "start_ms": cursor,
                "end_ms": total,
                "duration_ms": total - cursor,
                "estimated": False,
                "note": "章末静音填充",
            }
        )
    for index, row in enumerate(rows, start=1):
        row["index"] = index
    return rows


def format_schedule_tsv(rows: list[dict]) -> str:
    """把时间表格式化成 TSV（首行表头），便于人工核对。"""
    header = ["序号", "类型", "台词行", "说话人", "开始(ms)", "结束(ms)", "时长(ms)", "文本", "说明"]
    lines = ["\t".join(header)]
    for row in rows:
        text = (row.get("text") or "").replace("\t", " ").replace("\n", " ")
        lines.append(
            "\t".join(
                [
                    str(row.get("index")),
                    "台词" if row.get("kind") == "line" else "停顿",
                    str(row.get("line_id") or ""),
                    str(row.get("speaker") or ""),
                    str(row.get("start_ms")),
                    str(row.get("end_ms")),
                    str(row.get("duration_ms")),
                    text,
                    str(row.get("note") or ("估算" if row.get("estimated") else "")),
                ]
            )
        )
    return "\n".join(lines) + "\n"


def build_timeline(chapter: dict, settings: dict | None = None, existing: dict | None = None, silence_events: list[dict] | None = None) -> dict:
    """构建章节时间轴。

    参数:
        chapter: 章节 dict（含场景与全部产物状态）。
        settings: 工程设置（读取 output.sample_rate/channels）。
        existing: 既有时间轴；存在时沿用 ID 并 revision+1（保留人工调整的语义）。
        silence_events: 导演脚本的静音事件；折算成行前/行后额外停顿。

    返回:
        符合 timeline.schema.json 的时间轴 dict。
    """
    output = (settings or {}).get("output") or {}
    sample_rate = int(output.get("sample_rate") or 48000)
    channels = int(output.get("channels") or 2)
    silence_before, silence_after, silence_warnings = apply_silences(silence_events, chapter)

    voice_items: list[dict] = []
    sfx_items: list[dict] = []
    image_items: list[dict] = []
    video_items: list[dict] = []
    markers: list[dict] = []
    warnings: list[str] = []

    cursor = 0
    line_windows: dict[str, tuple[int, int]] = {}

    for scene in sorted(chapter.get("scenes") or [], key=lambda item: item.get("index") or 0):
        scene_start = cursor
        markers.append({"at_ms": scene_start, "label": scene.get("title") or f"场景 {scene.get('index', 0) + 1}"})

        for line in sorted(scene.get("lines") or [], key=lambda item: item.get("index") or 0):
            duration, estimated = provisional_line_duration_ms(line)
            line_id = line.get("id", "")
            start = cursor + int(line.get("pause_before_ms") or 0) + silence_before.get(line_id, 0)
            end = start + duration
            tts = line.get("tts") or {}
            voice_items.append(
                {
                    "source_kind": "line",
                    "source_id": line.get("id"),
                    "asset_id": tts.get("asset_id"),
                    "start_ms": start,
                    "end_ms": end,
                    "gain_db": 0.0,
                    "fade_in_ms": 20,
                    "fade_out_ms": 40,
                    "estimated": estimated,
                    "label": (line.get("text") or "")[:40],
                }
            )
            line_windows[line.get("id", "")] = (start, end)
            if estimated:
                warnings.append(f"{line.get('id')} 使用估算时长（未合成）")
            if line.get("speaker_id") == "chr_unknown":
                warnings.append(f"{line.get('id')} 说话人未识别")
            cursor = end + int(line.get("pause_after_ms") or 0) + silence_after.get(line_id, 0)

        scene_end = cursor

        for sfx in sorted(scene.get("sfx_events") or [], key=lambda item: item.get("index") or 0):
            asset = sfx.get("asset") or {}
            if asset.get("status") != "ready" or not asset.get("asset_id"):
                warnings.append(f"{sfx.get('id')} 音效未就绪，已跳过")
                continue
            position = sfx.get("position") or {}
            line_id = position.get("line_id")
            if position.get("mode") == "absolute" or not line_id:
                start = int(position.get("offset_ms") or 0) + scene_start
            else:
                anchor_start, anchor_end = line_windows.get(line_id, (scene_start, scene_end))
                if position.get("mode") == "before_line":
                    start = anchor_start
                elif position.get("mode") == "after_line":
                    start = anchor_end
                else:  # during_line
                    start = anchor_start + int(position.get("offset_ms") or 0)
            duration = int(asset.get("actual_duration_ms") or sfx.get("duration_ms") or 0)
            end = scene_end if sfx.get("loop") else start + duration
            sfx_items.append(
                {
                    "source_kind": "sfx",
                    "source_id": sfx.get("id"),
                    "asset_id": asset.get("asset_id"),
                    "start_ms": max(0, start),
                    "end_ms": max(start + 100, end),
                    "gain_db": float(sfx.get("gain_db") or -12.0),
                    "fade_in_ms": int(sfx.get("fade_in_ms") or 0),
                    "fade_out_ms": int(sfx.get("fade_out_ms") or 0),
                    "estimated": False,
                    "label": sfx.get("prompt", "")[:40],
                }
            )

        for image in sorted(scene.get("key_images") or [], key=lambda item: item.get("index") or 0):
            asset = image.get("asset") or {}
            if asset.get("status") != "ready" or not asset.get("asset_id"):
                warnings.append(f"{image.get('id')} 关键图未就绪，已跳过")
                continue
            anchor = line_windows.get(image.get("anchor_line_id"), (scene_start, scene_end))
            start = anchor[0]
            image_items.append(
                {
                    "source_kind": "key_image",
                    "source_id": image.get("id"),
                    "asset_id": asset.get("asset_id"),
                    "start_ms": start,
                    "end_ms": min(scene_end, start + DEFAULT_IMAGE_HOLD_MS),
                    "gain_db": 0.0,
                    "fade_in_ms": 200,
                    "fade_out_ms": 200,
                    "estimated": False,
                    "label": (image.get("anchor_desc") or "")[:40],
                }
            )

        for video in sorted(scene.get("video_segments") or [], key=lambda item: item.get("index") or 0):
            asset = video.get("asset") or {}
            if asset.get("status") != "ready" or not asset.get("asset_id"):
                warnings.append(f"{video.get('id')} 短视频未就绪，已跳过")
                continue
            anchor = line_windows.get(video.get("anchor_line_id"), (scene_start, scene_end))
            start = anchor[0] + int(video.get("start_offset_ms") or 0)
            duration = min(5000, int(asset.get("actual_duration_ms") or video.get("duration_ms") or 5000))
            video_items.append(
                {
                    "source_kind": "video_segment",
                    "source_id": video.get("id"),
                    "asset_id": asset.get("asset_id"),
                    "start_ms": start,
                    "end_ms": start + duration,
                    "gain_db": 0.0,
                    "fade_in_ms": 0,
                    "fade_out_ms": 0,
                    "estimated": False,
                    "label": (video.get("prompt") or "")[:40],
                }
            )

    duration_ms = max([item["end_ms"] for item in voice_items + sfx_items + image_items + video_items] or [0])
    warnings.extend(silence_warnings)
    return {
        "schema_version": 1,
        "id": (existing or {}).get("id") or new_id("tml"),
        "chapter_id": chapter.get("id"),
        "revision": int((existing or {}).get("revision") or 0) + 1,
        "duration_ms": duration_ms,
        "sample_rate": sample_rate,
        "channels": channels,
        "built_at": None,
        "source_hash": compute_source_hash(chapter, silence_events),
        "tracks": [
            {"id": "trk_voice", "kind": "voice", "name": "配音", "items": voice_items},
            {"id": "trk_sfx", "kind": "sfx", "name": "音效", "items": sfx_items},
            {"id": "trk_music", "kind": "music", "name": "音乐", "items": []},
            {"id": "trk_video", "kind": "video", "name": "视频", "items": video_items},
            {"id": "trk_image", "kind": "image", "name": "画面", "items": image_items},
        ],
        "markers": markers,
        "warnings": warnings,
    }


def summarize_timeline(timeline: dict) -> str:
    """生成时间轴的一行摘要（CLI 输出用）。

    参数:
        timeline: 时间轴 dict。

    返回:
        形如「时长 03:02 / 配音 42 段 / 音效 6 段 / 画面 3 段 / 视频 1 段 / 警告 2 条」。
    """
    total = int(timeline.get("duration_ms") or 0)
    minutes, seconds = divmod(total // 1000, 60)
    counts = {track["kind"]: len(track.get("items") or []) for track in timeline.get("tracks") or []}
    return (
        f"时长 {minutes:02d}:{seconds:02d} / 配音 {counts.get('voice', 0)} 段 / 音效 {counts.get('sfx', 0)} 段 / "
        f"画面 {counts.get('image', 0)} 段 / 视频 {counts.get('video', 0)} 段 / 警告 {len(timeline.get('warnings') or [])} 条"
    )
