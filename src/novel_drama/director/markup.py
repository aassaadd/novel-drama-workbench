"""导演脚本：由人工标注文本解析成 `director-script.schema.json` 结构（不调用 LLM）。

标注语法（逐行，缩进无关，空行与 `#` 开头行忽略）：

    @shot 1 书房夜雨 location=书房 time=night mood=压抑 goal=等一个解释
    旁白：夜色渐深，雨点敲在窗上。
    林晚|angry：你为什么要骗我？
    林晚|whisper|inner_monologue：别出声。
    !sfx ambience 8000 after-line=2 雨声敲窗
    !silence 800  after-line=2  情绪留白
    !visual 3:4 书房内景，暖黄台灯，雨夜，人物背对镜头
    !anim i2v 5000 slow_push_in 镜头缓慢推近，雨丝划过窗

约定：
- 台词行格式 `说话人|情绪|类型：文本`，后两项可省略；没有冒号视为旁白；
- `before-line=N` / `after-line=N` 的 N 是当前镜头内的台词序号（从 1 开始）；
- 锚点按正文段落块规则计算，文本在正文中找不到时标记 `orphan` 并告警；
- 同文本的章节台词行会被自动挂接，配音状态随之带入。
"""

from __future__ import annotations

import hashlib
import re

from ..config import load_config
from ..models import new_id
from ..models.entities import EMOTIONS, NARRATOR_ID, UNKNOWN_SPEAKER_ID
from ..models.ids import now_iso

ASPECT_SIZES = {
    "1:1": (1024, 1024),
    "3:4": (1152, 1536),
    "4:3": (1536, 1152),
    "9:16": (720, 1280),
    "16:9": (1280, 720),
}
TIMES = ("dawn", "morning", "day", "dusk", "night", "unknown")
SFX_KINDS = ("ambience", "foley", "impact", "transition", "music_sting")
CAMERA_MOVES = ("static", "slow_push_in", "pan_left", "pan_right", "handheld")

# 内置模板 ID：工程与本机配置都取不到时的兜底（与 config.py 的 _FALLBACK 保持一致）
_BUILTIN_TEMPLATES = {
    "image": "qwen_image_accel",
    "video_t2v": "minimax_h3_accel_t2v",
    "video_i2v": "minimax_h3_accel",
}

_LINE_RE = re.compile(r"^(?P<head>[^：:]{0,80})[：:](?P<body>.+)$")


def _sha(text: str) -> str:
    """计算 sha256 文本哈希。"""
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _block_index(content: str) -> list[dict]:
    """把正文按空行拆成段落块，并记录字符起止。

    参数:
        content: 章节正文。

    返回:
        [{id, text, char_start, char_end}]；正文为空时返回一个占位块。
    """
    blocks: list[dict] = []
    cursor = 0
    for match in re.finditer(r"[^\n]+(?:\n(?!\s*\n)[^\n]+)*", content or ""):
        text = match.group(0)
        if not text.strip():
            cursor = match.end()
            continue
        block_id = "blk_" + hashlib.sha1(f"{len(blocks)}:{text}".encode("utf-8")).hexdigest()[:16]
        blocks.append({"id": block_id, "text": text, "char_start": match.start(), "char_end": match.end()})
        cursor = match.end()
    if not blocks:
        blocks.append(
            {
                "id": "blk_" + hashlib.sha1(b"empty").hexdigest()[:16],
                "text": "",
                "char_start": 0,
                "char_end": 0,
            }
        )
    return blocks


def _anchor(content: str, blocks: list[dict], text: str, search_from: int) -> tuple[dict, int, bool]:
    """为一段文本计算锚点。

    参数:
        content: 章节正文。
        blocks: 段落块列表。
        text: 待定位文本。
        search_from: 从正文哪个字符位置开始搜索。

    返回:
        (anchor, 新的搜索起点, 是否精确命中)。
    """
    position = content.find(text, search_from) if text else -1
    if position < 0:
        block = blocks[0]
        return (
            {
                "block_id": block["id"],
                "char_start": 0,
                "char_end": 0,
                "text_hash": _sha(text),
                "line_id": None,
                "state": "orphan",
            },
            search_from,
            False,
        )
    end = position + len(text)
    block = next((item for item in blocks if item["char_start"] <= position < item["char_end"]), blocks[0])
    return (
        {
            "block_id": block["id"],
            "char_start": position,
            "char_end": end,
            "text_hash": _sha(text),
            "line_id": None,
            "state": "exact",
        },
        end,
        True,
    )


def _parse_kv(tokens: list[str]) -> dict:
    """从空白分隔的 token 中取出 key=value。"""
    result = {}
    for token in tokens:
        if "=" in token:
            key, value = token.split("=", 1)
            result[key.strip()] = value.strip()
    return result


def _resolve_speaker(name: str, characters: list[dict]) -> tuple[str, float]:
    """把标注里的说话人解析成角色 ID。

    参数:
        name: 标注里的名字（可为空，表示旁白）。
        characters: 角色表。

    返回:
        (角色 ID, 置信度)；未识别返回 chr_unknown 与 0.0。
    """
    if not name or name.strip() in ("旁白", "narrator"):
        return NARRATOR_ID, 1.0
    key = name.strip()
    for character in characters:
        if character.get("role") == "narrator":
            continue
        names = [character.get("name") or ""] + list(character.get("aliases") or [])
        if key in names:
            return character["id"], 1.0
    return UNKNOWN_SPEAKER_ID, 0.0


def _parse_position(token: str, line_ids: list[str], warnings: list[str], where: str) -> dict:
    """解析位置标记：before-line=N / after-line=N / during-line=N / shot-start / shot-end。"""
    token = token.strip()
    if token in ("shot-start", "shot_start"):
        return {"mode": "shot_start", "line_id": line_ids[0] if line_ids else None, "offset_ms": 0}
    if token in ("shot-end", "shot_end"):
        return {"mode": "shot_end", "line_id": line_ids[-1] if line_ids else None, "offset_ms": 0}
    match = re.match(r"^(before|after|during)[_-]line=(\d+)$", token)
    if not match:
        warnings.append(f"{where} 位置标记无法识别：{token}（按 after_line 处理）")
        return {"mode": "after_line", "line_id": line_ids[-1] if line_ids else None, "offset_ms": 0}
    mode, number = match.group(1) + "_line", int(match.group(2))
    if 1 <= number <= len(line_ids):
        return {"mode": mode, "line_id": line_ids[number - 1], "offset_ms": 0}
    warnings.append(f"{where} 台词序号 {number} 超出当前镜头范围，挂到末行")
    return {"mode": mode, "line_id": line_ids[-1] if line_ids else None, "offset_ms": 0}


def _tts_from_line(line: dict | None) -> dict:
    """从章节台词行复制配音状态（保持 schema 允许的字段）。"""
    if not line:
        return {"status": "pending"}
    tts = line.get("tts") or {}
    state = {
        "status": tts.get("status") or "pending",
        "asset_id": tts.get("asset_id"),
        "duration_ms": tts.get("duration_ms"),
        "synthesized_revision": tts.get("synthesized_revision"),
    }
    for key in ("provider_id", "model_version", "voice_id", "variant_id", "routing", "chunks", "error", "updated_at"):
        if tts.get(key) is not None:
            state[key] = tts[key]
    return state


def _template_ids(settings: dict) -> dict:
    """解析图像/视频工作流模板 ID。

    优先级：工程 settings.comfy.templates > 本机 config.yaml 的 comfyui.templates > 内置默认。

    参数:
        settings: 工程设置（project.settings）。

    返回:
        {"image", "video_t2v", "video_i2v"} 三项完整映射（值保证非空）。
    """
    project_templates = (settings.get("comfy") or {}).get("templates") or {}
    local_templates: dict | None = None
    resolved: dict[str, str] = {}
    for key, fallback in _BUILTIN_TEMPLATES.items():
        value = project_templates.get(key)
        if not value:
            if local_templates is None:
                local_templates = load_config().get("comfyui.templates") or {}
            value = local_templates.get(key)
        resolved[key] = value or fallback
    return resolved


def parse_markup(markup: str, chapter: dict, characters: list[dict], settings: dict | None = None) -> dict:
    """把标注文本解析成导演脚本。

    参数:
        markup: 标注文本。
        chapter: 章节 dict（提供正文、revision 与已有台词行）。
        characters: 角色表。
        settings: 工程设置（读取 comfy 模板与输出规格）。

    返回:
        符合 `director-script.schema.json` 的脚本 dict（含 warnings）。

    异常:
        ValueError: 语法错误（缺镜头、缺文本、非法枚举等）。
    """
    settings = settings or {}
    content = chapter.get("content") or ""
    blocks = _block_index(content)
    search_from = 0
    warnings: list[str] = []
    templates = _template_ids(settings)
    template_image = templates["image"]

    # 已有台词行：按文本建索引，供自动挂接
    existing: dict[str, list[dict]] = {}
    for scene in chapter.get("scenes") or []:
        for line in scene.get("lines") or []:
            existing.setdefault((line.get("text") or "").strip(), []).append(line)

    shots: list[dict] = []
    current: dict | None = None

    def require_shot(where: str) -> dict:
        """取当前镜头，缺失时报错。"""
        if current is None:
            raise ValueError(f"{where}：请先用 @shot 声明镜头")
        return current

    for raw in markup.splitlines():
        line_text = raw.strip()
        if not line_text or line_text.startswith("#"):
            continue
        if line_text.startswith("@shot"):
            tokens = line_text.split()
            if len(tokens) < 3:
                raise ValueError(f"镜头声明缺少序号或名称：{raw}")
            try:
                index = int(tokens[1])
            except ValueError as exc:
                raise ValueError(f"镜头序号必须是整数：{raw}") from exc
            extras = _parse_kv(tokens[3:])
            time_of_day = extras.get("time") or "unknown"
            if time_of_day not in TIMES:
                warnings.append(f"镜头 {index} 的 time={time_of_day} 非法，已改为 unknown")
                time_of_day = "unknown"
            anchor, search_from, _ = _anchor(content, blocks, " ".join(tokens[2:]), search_from)
            current = {
                "id": new_id("sht"),
                "index": index,
                "scene_label": " ".join(tokens[2:]),
                "location": extras.get("location"),
                "time_of_day": time_of_day,
                "mood": extras.get("mood"),
                "goal": extras.get("goal"),
                "anchor": anchor,
                "transition_in": {"type": extras.get("in") or "cut"},
                "transition_out": {"type": extras.get("out") or "cut"},
                "status": "draft",
                "lines": [],
                "sfx_events": [],
                "visuals": [],
                "animations": [],
                "silence_events": [],
                "revision": 1,
            }
            shots.append(current)
            continue

        if line_text.startswith("!"):
            shot = require_shot(line_text)
            body = line_text[1:].strip()
            head, _, payload = body.partition(" ")
            head = head.lower()
            parts = payload.split()
            if head == "sfx":
                if len(parts) < 4:
                    raise ValueError(f"音效需要「kind 时长 位置 描述」：{raw}")
                kind, duration = parts[0], int(parts[1])
                if kind not in SFX_KINDS:
                    raise ValueError(f"音效 kind 非法：{kind}（可选 {', '.join(SFX_KINDS)}）")
                line_ids = [item["id"] for item in shot["lines"]]
                position = _parse_position(parts[2], line_ids, warnings, f"镜头 {shot['index']} 音效")
                anchor = shot["anchor"]
                shot["sfx_events"].append(
                    {
                        "id": new_id("sfx"),
                        "index": len(shot["sfx_events"]) + 1,
                        "kind": kind,
                        "prompt": " ".join(parts[3:]),
                        "duration_ms": duration,
                        "loop": kind == "ambience",
                        "gain_db": -12.0,
                        "fade_in_ms": 800 if kind == "ambience" else 0,
                        "fade_out_ms": 1200 if kind == "ambience" else 0,
                        "position": position,
                        "anchor": anchor,
                        "asset": {"status": "pending"},
                        "revision": 1,
                    }
                )
                continue
            if head == "silence":
                if len(parts) < 2:
                    raise ValueError(f"静音需要「时长 位置 [原因]」：{raw}")
                duration = int(parts[0])
                line_ids = [item["id"] for item in shot["lines"]]
                position = _parse_position(parts[1], line_ids, warnings, f"镜头 {shot['index']} 静音")
                shot["silence_events"].append(
                    {
                        "id": new_id("sil"),
                        "index": len(shot["silence_events"]) + 1,
                        "duration_ms": duration,
                        "position": position,
                        "anchor": shot["anchor"],
                        "reason": " ".join(parts[2:]) or None,
                    }
                )
                continue
            if head == "visual":
                if len(parts) < 2:
                    raise ValueError(f"画面需要「比例 提示词」：{raw}")
                aspect = parts[0]
                if aspect not in ASPECT_SIZES:
                    raise ValueError(f"画面比例非法：{aspect}（可选 {', '.join(ASPECT_SIZES)}）")
                width, height = ASPECT_SIZES[aspect]
                shot["visuals"].append(
                    {
                        "id": new_id("img"),
                        "index": len(shot["visuals"]) + 1,
                        "prompt": " ".join(parts[1:]),
                        "aspect_ratio": aspect,
                        "width": width,
                        "height": height,
                        "workflow_template_id": template_image,
                        "anchor": shot["anchor"],
                        "status": "pending",
                        "candidates": [],
                        "revision": 1,
                    }
                )
                continue
            if head == "anim":
                if len(parts) < 4:
                    raise ValueError(f"动画需要「mode 时长 镜头运动 提示词」：{raw}")
                mode, duration, camera = parts[0].lower(), int(parts[1]), parts[2]
                if mode not in ("i2v", "t2v"):
                    raise ValueError(f"动画 mode 非法：{mode}")
                if camera not in CAMERA_MOVES:
                    raise ValueError(f"镜头运动非法：{camera}（可选 {', '.join(CAMERA_MOVES)}）")
                if duration < 500 or duration > 5000:
                    raise ValueError(f"动画时长必须在 500~5000ms：{duration}")
                visual_id = shot["visuals"][-1]["id"] if shot["visuals"] else None
                if mode == "i2v" and not visual_id:
                    raise ValueError(f"i2v 动画必须在本镜头先声明 !visual：{raw}")
                shot["animations"].append(
                    {
                        "id": new_id("vid"),
                        "index": len(shot["animations"]) + 1,
                        "visual_id": visual_id if mode == "i2v" else None,
                        "mode": mode,
                        "duration_ms": duration,
                        "camera_move": camera,
                        "prompt": " ".join(parts[3:]),
                        "audio_policy": "mute",
                        "workflow_template_id": templates["video_i2v"] if mode == "i2v" else templates["video_t2v"],
                        "anchor": shot["anchor"],
                        "status": "pending",
                        "candidates": [],
                        "revision": 1,
                    }
                )
                continue
            raise ValueError(f"无法识别的标注指令：{raw}")

        shot = require_shot(line_text)
        match = _LINE_RE.match(line_text)
        if match:
            head, text = match.group("head").strip(), match.group("body").strip()
        else:
            head, text = "", line_text
        if not text:
            raise ValueError(f"台词文本为空：{raw}")
        tokens = [token for token in head.split("|")] if head else []
        speaker_name = tokens[0].strip() if tokens else ""
        emotion = (tokens[1].strip() if len(tokens) > 1 and tokens[1].strip() else "neutral")
        kind = (tokens[2].strip() if len(tokens) > 2 and tokens[2].strip() else "")
        speaker_id, confidence = _resolve_speaker(speaker_name, characters)
        if emotion not in EMOTIONS:
            warnings.append(f"镜头 {shot['index']} 情绪 {emotion} 非法，已改为 neutral")
            emotion = "neutral"
        if not kind:
            kind = "dialogue" if speaker_id != NARRATOR_ID else "narration"
        if kind not in ("dialogue", "narration", "inner_monologue"):
            raise ValueError(f"台词类型非法：{kind}")
        if speaker_id == UNKNOWN_SPEAKER_ID:
            warnings.append(f"镜头 {shot['index']} 说话人未识别：{speaker_name or '(空)'}，标记为 chr_unknown")

        anchor, search_from, exact = _anchor(content, blocks, text, search_from)
        if not exact:
            warnings.append(f"镜头 {shot['index']} 的台词未在正文中找到，锚点为 orphan：{text[:24]}")
        matched = None
        candidates = existing.get(text) or []
        for candidate in candidates:
            if candidate.get("speaker_id") in (speaker_id, UNKNOWN_SPEAKER_ID):
                matched = candidate
                break
        if matched is None and candidates:
            matched = candidates[0]
        line_id = (matched or {}).get("id") or new_id("lin")
        anchor["line_id"] = line_id
        shot["lines"].append(
            {
                "id": line_id,
                "index": len(shot["lines"]),
                "kind": kind,
                "speaker_id": speaker_id,
                "text": text,
                "emotion": emotion,
                "anchor": anchor,
                "tts": _tts_from_line(matched),
                "revision": int((matched or {}).get("revision") or 1),
            }
        )

    if not shots:
        raise ValueError("标注里没有任何 @shot，无法生成导演脚本")
    for shot in shots:
        if not shot["lines"]:
            raise ValueError(f"镜头 {shot['index']} 没有任何台词行")
        shot["status"] = "ready" if not warnings else "draft"

    content_hash = _sha(content)
    script = {
        "schema_version": 1,
        "id": new_id("scr"),
        "chapter_id": chapter["id"],
        "work_id": chapter.get("work_id"),
        "source": {
            "chapter_revision": int(chapter.get("revision") or 1),
            "settings_revision": int((settings.get("settings_revision") or 1)),
            "content_hash": content_hash,
        },
        "status": "draft" if warnings else "ready",
        "shots": shots,
        "warnings": warnings,
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }
    return script
