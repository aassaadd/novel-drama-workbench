"""导演脚本：由人工标注文本解析成 `director-script.schema.json` 结构（不调用 LLM）。

标注语法（逐行，缩进无关，空行与 `#` 开头行忽略）：

    @shot 1 书房夜雨 location=书房 time=night mood=压抑 goal=等一个解释
    旁白：夜色渐深，雨点敲在窗上。
    林晚|angry：你为什么要骗我？
    林晚|whisper|inner_monologue：别出声。
    !sfx ambience 8000 after-line=2 雨声敲窗 || Gentle rain tapping a window, close perspective
    !bgm mood=tense energy=0.4 name=夜雨独行 style=低音弦乐+稀疏钢琴 en=tense+low+strings+sparse+piano
    !silence 800  after-line=2  情绪留白
    !visual 书房内景，暖黄台灯，雨夜，人物背对镜头
    !anim i2v 5000 slow_push_in 镜头缓慢推近，雨丝划过窗

约定：
- 台词行格式 `说话人|情绪|类型：文本`，后两项可省略；没有冒号视为旁白；
- `!sfx` 必须用 `||` 在中文描述后附英文提示词；英文与中文同源产出，生成时只补时长后缀；
- `!bgm` 必须带 `en=` 英文提示词（空格用 `+` 代替），缺失直接报错；
- `before-line=N` / `after-line=N` 的 N 是当前镜头内的台词序号（从 1 开始）；
- 锚点按正文段落块规则计算：先按游标顺序精确匹配，找不到时在全章找同文（就近），
  再按标点/空白归一化匹配；能校正的记 `shifted`，只有仍找不到才记 `orphan` 并告警；
- 同文本的章节台词行会被自动挂接，配音状态随之带入。
"""

from __future__ import annotations

import bisect
import hashlib
import re

from ..config import load_config
from ..models import new_id
from ..models.entities import EMOTIONS, NARRATOR_ID, UNKNOWN_SPEAKER_ID
from ..models.ids import now_iso
from ..text.names import match_speaker, normalize_speaker_name

#: 信息级提示前缀：可自动修复的问题归到这里，页面折叠显示「已自动修正 N 项」
INFO_PREFIX = "【已自动修正】"

#: 模型常见的音效类型别写，自动归到合法枚举，避免整章解析失败
SFX_KIND_ALIASES = {
    "silence": "ambience",
    "none": "ambience",
    "ambient": "ambience",
    "effect": "foley",
    "effects": "foley",
    "sfx": "foley",
    "bgm": "music_sting",
    "music": "music_sting",
    "whoosh": "transition",
}

TIMES = ("dawn", "morning", "day", "dusk", "night", "unknown")
SFX_KINDS = ("ambience", "foley", "impact", "transition", "music_sting")
BGM_MOODS = ("calm", "tense", "sad", "romantic", "mysterious", "action", "comedic", "epic", "horror")
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


def _normalize_for_match(text: str) -> str:
    """匹配用归一化：只保留文字与数字，忽略空白、标点、引号与符号差异。

    参数:
        text: 原始文本。

    返回:
        归一化后的匹配串。
    """
    return "".join(ch for ch in str(text or "") if ch.isalnum())


def _normalized_map(content: str) -> tuple[str, list[int]]:
    """生成归一化正文与「归一化字符 → 原文下标」的映射。

    参数:
        content: 章节正文。

    返回:
        (归一化正文, 每个字符对应的原文下标列表)。
    """
    chars: list[str] = []
    index_map: list[int] = []
    for index, ch in enumerate(content or ""):
        if ch.isalnum():
            chars.append(ch)
            index_map.append(index)
    return "".join(chars), index_map


def _anchor(
    content: str,
    blocks: list[dict],
    text: str,
    search_from: int,
    normalized: tuple[str, list[int]] | None = None,
) -> tuple[dict, int, bool]:
    """为一段文本计算锚点。

    匹配顺序：游标后精确匹配 → 全章同文（就近）→ 标点/空白归一化后匹配（就近）。
    后两种记为 shifted（可自动修正），只有归一化后仍找不到才记 orphan。

    参数:
        content: 章节正文。
        blocks: 段落块列表。
        text: 待定位文本。
        search_from: 从正文哪个字符位置开始搜索。
        normalized: `_normalized_map(content)` 的结果，省略时不做归一化匹配。

    返回:
        (anchor, 新的搜索起点, 是否精确命中)。
    """
    if not text:
        return _orphan_anchor(blocks, text), search_from, False

    position = content.find(text, search_from)
    end = position + len(text) if position >= 0 else 0
    exact = position >= 0

    if position < 0:
        # 同文可能出现在正文别处（重复短句、镜头顺序与正文不完全一致），取离游标最近的一处
        positions: list[int] = []
        cursor = 0
        while True:
            found = content.find(text, cursor)
            if found < 0:
                break
            positions.append(found)
            cursor = found + 1
        if positions:
            forward = [pos for pos in positions if pos >= search_from]
            position = min(forward) if forward else min(positions, key=lambda pos: abs(pos - search_from))
            end = position + len(text)

    if position < 0 and normalized is not None:
        norm_content, index_map = normalized
        norm_text = _normalize_for_match(text)
        # 太短的串归一化后容易误命中，只对 ≥4 字的片段做归一化匹配
        if len(norm_text) >= 4:
            norm_start = bisect.bisect_left(index_map, search_from)
            found = norm_content.find(norm_text, norm_start)
            if found < 0:
                found = norm_content.find(norm_text)
            if found >= 0:
                position = index_map[found]
                end = index_map[found + len(norm_text) - 1] + 1

    if position < 0:
        return _orphan_anchor(blocks, text), search_from, False

    block = next((item for item in blocks if item["char_start"] <= position < item["char_end"]), blocks[0])
    anchor = {
        "block_id": block["id"],
        "char_start": position,
        "char_end": end,
        "text_hash": _sha(text),
        "line_id": None,
        "state": "exact" if exact else "shifted",
    }
    # 过短的碎片（如模型写出的「*」）不推进游标，避免把后续镜头带偏
    next_from = end if len(_normalize_for_match(text)) >= 3 else search_from
    return anchor, next_from, exact


def _orphan_anchor(blocks: list[dict], text: str) -> dict:
    """构造 orphan 锚点。

    参数:
        blocks: 段落块列表。
        text: 未定位到的文本。

    返回:
        锚点 dict。
    """
    block = blocks[0]
    return {
        "block_id": block["id"],
        "char_start": 0,
        "char_end": 0,
        "text_hash": _sha(text),
        "line_id": None,
        "state": "orphan",
    }


def _parse_kv(tokens: list[str]) -> dict:
    """从空白分隔的 token 中取出 key=value。"""
    result = {}
    for token in tokens:
        if "=" in token:
            key, value = token.split("=", 1)
            result[key.strip()] = value.strip()
    return result


def _resolve_speaker(name: str, characters: list[dict]) -> tuple[str, float]:
    """把标注里的说话人解析成角色 ID（先归一化再匹配）。

    参数:
        name: 标注里的名字（可为空，表示旁白）。
        characters: 角色表。

    返回:
        (角色 ID, 置信度)；未识别返回 chr_unknown 与 0.0。
    """
    key = normalize_speaker_name(name)
    if not key or key in ("旁白", "narrator", "narration"):
        return NARRATOR_ID, 1.0
    matched = match_speaker(name, characters)
    if matched is not None:
        return matched
    return UNKNOWN_SPEAKER_ID, 0.0


def _snippet(text: str, limit: int = 60) -> str:
    """截取用于展示的台词片段。

    参数:
        text: 台词原文。
        limit: 最大字符数（超出时补省略号）。

    返回:
        片段文本；空文本返回空串。
    """
    value = str(text or "").strip()
    if len(value) <= limit:
        return value
    return value[:limit] + "…"


def _line_diagnostic(
    kind: str,
    message: str,
    shot: dict,
    line_index: int,
    line_id: str,
    text: str,
    anchor: dict,
    speaker: str | None = None,
) -> dict:
    """构造一条台词级告警的结构化定位信息。

    参数:
        kind: 告警类型（orphan / speaker_unknown）。
        message: 与 `warnings` 中一致的告警文本（前端按此匹配）。
        shot: 所属镜头。
        line_index: 镜头内行序号（从 1 开始）。
        line_id: 台词行 ID（可能尚未写入章节）。
        text: 台词原文。
        anchor: 该行的锚点。
        speaker: 未识别的说话人名字（仅 speaker_unknown）。

    返回:
        供页面定位使用的 dict。
    """
    return {
        "kind": kind,
        "message": message,
        "chapter_id": None,  # 由 parse_markup 统一补全
        "shot_index": shot.get("index"),
        "scene_index": (int(shot.get("index") or 1) - 1),
        "scene_label": shot.get("scene_label"),
        "line_id": line_id,
        "line_index": line_index,
        "text": str(text or "")[:200],
        "snippet": _snippet(text),
        "speaker": speaker,
        "anchor_state": anchor.get("state"),
        "char_start": anchor.get("char_start"),
        "char_end": anchor.get("char_end"),
    }


def _parse_position(
    token: str,
    line_ids: list[str],
    warnings: list[str],
    where: str,
    info_counts: dict[tuple[str, str], int] | None = None,
    diagnostics: list[dict] | None = None,
    shot_index: int | None = None,
) -> dict:
    """解析位置标记：before-line=N / after-line=N / during-line=N / shot-start / shot-end。

    序号越界（正数）时自动夹紧到当前镜头有效范围并累计到信息级合并计数；
    序号为 0/负数属于数据问题，保留告警。

    参数:
        token: 位置标记。
        line_ids: 当前镜头的台词行 ID（按镜头内顺序）。
        warnings: 告警列表（原地追加）。
        where: 定位前缀（如「镜头 3 音效」）。
        info_counts: 信息级合并计数，键为 (定位, 原因)。
        diagnostics: 硬告警的结构化定位列表（原地追加，可为 None）。
        shot_index: 所属镜头序号（供定位用）。

    返回:
        位置 dict。
    """
    token = token.strip()
    if token in ("shot-start", "shot_start"):
        return {"mode": "shot_start", "line_id": line_ids[0] if line_ids else None, "offset_ms": 0}
    if token in ("shot-end", "shot_end"):
        return {"mode": "shot_end", "line_id": line_ids[-1] if line_ids else None, "offset_ms": 0}
    match = re.match(r"^(before|after|during)[_-]line=(-?\d+)$", token)
    if not match:
        warnings.append(INFO_PREFIX + f"{where} 位置标记无法识别：{token}（已按 after_line 处理）")
        return {"mode": "after_line", "line_id": line_ids[-1] if line_ids else None, "offset_ms": 0}
    mode, number = match.group(1) + "_line", int(match.group(2))
    if number < 1:
        message = f"{where} 台词序号 {number} 无效（必须从 1 开始），已挂到末行"
        warnings.append(message)
        if diagnostics is not None:
            diagnostics.append(
                {
                    "kind": "sfx_position",
                    "message": message,
                    "chapter_id": None,
                    "shot_index": shot_index,
                    "scene_index": (int(shot_index) - 1) if shot_index is not None else None,
                    "scene_label": None,
                    "line_id": None,
                    "line_index": None,
                    "text": None,
                    "snippet": None,
                    "speaker": None,
                    "anchor_state": None,
                    "char_start": None,
                    "char_end": None,
                    "detail": token,
                }
            )
        return {"mode": mode, "line_id": line_ids[-1] if line_ids else None, "offset_ms": 0}
    if number <= len(line_ids):
        return {"mode": mode, "line_id": line_ids[number - 1], "offset_ms": 0}
    if info_counts is not None:
        key = (where, "台词序号越界")
        info_counts[key] = info_counts.get(key, 0) + 1
    return {"mode": mode, "line_id": line_ids[-1] if line_ids else None, "offset_ms": 0}


def _flush_info_counts(info_counts: dict[tuple[str, str], int], warnings: list[str]) -> None:
    """把自动修复的合并计数写成信息级提示（同一镜头同一原因只留一条）。

    参数:
        info_counts: {(定位, 原因): 次数}。
        warnings: 告警列表（原地追加）。

    返回:
        无。
    """
    for (where, reason), count in info_counts.items():
        if reason == "台词序号越界":
            warnings.append(f"{INFO_PREFIX}{where}台词序号越界，已夹紧到本镜头有效范围（共 {count} 处）")
        elif reason == "台词锚点按同文就近校正":
            warnings.append(f"{INFO_PREFIX}{where}有 {count} 行台词锚点按同文就近校正")
        elif reason.endswith("非法") or "缺少" in reason:
            warnings.append(f"{INFO_PREFIX}{where}出现「{reason}」（共 {count} 处），已自动处理")
        else:
            warnings.append(f"{INFO_PREFIX}{where}有 {count} 行{reason}，已标为 chr_unknown 待校对")


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


def parse_markup(
    markup: str,
    chapter: dict,
    characters: list[dict],
    settings: dict | None = None,
    diagnostics: list[dict] | None = None,
) -> dict:
    """把标注文本解析成导演脚本。

    参数:
        markup: 标注文本。
        chapter: 章节 dict（提供正文、revision 与已有台词行）。
        characters: 角色表。
        settings: 工程设置（读取 comfy 模板与输出规格）。
        diagnostics: 可选的硬告警结构化定位列表；传入时原地追加
            `{kind, message, chapter_id, shot_index, scene_index, line_id,
            line_index, text, snippet, ...}`，供页面点击告警直接定位。
            信息级（【已自动修正】）提示不会写入该列表。

    返回:
        符合 `director-script.schema.json` 的脚本 dict（含 warnings）。

    异常:
        ValueError: 语法错误（缺镜头、缺文本、非法枚举等）。
    """
    settings = settings or {}
    content = chapter.get("content") or ""
    blocks = _block_index(content)
    search_from = 0
    normalized = _normalized_map(content)
    warnings: list[str] = []
    #: 可自动修复问题的合并计数：{(定位, 原因): 次数}
    info_counts: dict[tuple[str, str], int] = {}
    templates = _template_ids(settings)
    template_image = templates["image"]
    from ..media.comfy_media import resolve_aspect

    # 画幅与宽高统一按工程 output.aspect_ratio 解析（模板 size_map 是宽高唯一来源）
    aspect = resolve_aspect(settings, "image")
    output_aspect = aspect["ratio"]
    output_width = int(aspect["width"])
    output_height = int(aspect["height"])

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
                warnings.append(INFO_PREFIX + f"镜头 {index} 的 time={time_of_day} 非法，已改为 unknown")
                time_of_day = "unknown"
            anchor, search_from, _ = _anchor(content, blocks, " ".join(tokens[2:]), search_from, normalized)
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
                # 双语语法：`!sfx kind 时长 位置 中文描述 || English prompt`
                # 英文提示词由标注阶段与中文同源产出，缺失直接报错，不做运行时翻译兜底。
                zh_payload, _, en_payload = payload.partition("||")
                parts = zh_payload.split()
                if len(parts) < 4:
                    raise ValueError(f"音效需要「kind 时长 位置 中文描述 || 英文提示词」：{raw}")
                kind, duration = parts[0], int(parts[1])
                if kind not in SFX_KINDS and kind in SFX_KIND_ALIASES:
                    key = (f"镜头 {shot['index']} 音效", f"音效类型 {kind} 非法")
                    info_counts[key] = info_counts.get(key, 0) + 1
                    kind = SFX_KIND_ALIASES[kind]
                if kind not in SFX_KINDS:
                    raise ValueError(f"音效 kind 非法：{kind}（可选 {', '.join(SFX_KINDS)}）")
                prompt_zh = " ".join(parts[3:]).strip()
                prompt_en = en_payload.strip()
                if not prompt_en:
                    raise ValueError(
                        f"音效缺少英文提示词（!sfx ... 中文描述 || English prompt）：{raw}；"
                        "请重新生成标注描述"
                    )
                line_ids = [item["id"] for item in shot["lines"]]
                position = _parse_position(
                    parts[2],
                    line_ids,
                    warnings,
                    f"镜头 {shot['index']} 音效",
                    info_counts,
                    diagnostics=diagnostics,
                    shot_index=shot["index"],
                )
                anchor = shot["anchor"]
                shot["sfx_events"].append(
                    {
                        "id": new_id("sfx"),
                        "index": len(shot["sfx_events"]) + 1,
                        "kind": kind,
                        "prompt": prompt_zh,
                        "prompt_en": prompt_en,
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
            if head in ("bgm", "bgm-seg"):
                options = _parse_kv(parts)
                mood = (options.get("mood") or "calm").strip()
                if mood not in BGM_MOODS:
                    warnings.append(INFO_PREFIX + f"镜头 {shot['index']} 的 BGM mood={mood} 非法，已改为 calm")
                    mood = "calm"

                def opt_number(key: str, default=None):
                    """读取数值型选项；非法值回退默认并告警。"""
                    raw_value = options.get(key)
                    if raw_value in (None, ""):
                        return default
                    try:
                        return float(raw_value)
                    except ValueError:
                        warnings.append(INFO_PREFIX + f"镜头 {shot['index']} 的 BGM {key}={raw_value} 不是数字，已忽略")
                        return default

                if head == "bgm":
                    prompt_en = (options.get("en") or "").replace("+", " ").strip()
                    if not prompt_en:
                        raise ValueError(
                            f"配乐缺少英文提示词（!bgm ... en=english+prompt）：{raw}；请重新生成标注描述"
                        )
                    shot["bgm"] = {
                        "id": new_id("bgp"),
                        "scope": "shot",
                        "name": (options.get("name") or f"镜头{shot['index']} BGM").replace("+", " ")[:200],
                        "prompt_en": prompt_en[:1000],
                        "narrative_function": (options.get("function") or "").replace("+", " ")[:300] or None,
                        "mood_family": mood,
                        "mood_text": (options.get("mood_text") or "").replace("+", " ")[:200] or None,
                        "energy": opt_number("energy", 0.4),
                        "intensity": opt_number("intensity", 0.5),
                        "style": (options.get("style") or "").replace("+", " ")[:500] or None,
                        "instrumentation": [
                            item.replace("+", " ") for item in (options.get("inst") or "").split(",") if item
                        ],
                        "tempo_bpm": int(opt_number("bpm", 0)) or None,
                        "duration_hint_ms": int(opt_number("dur", 0)) or None,
                        "loop_required": options.get("loop", "1") not in ("0", "false", "no"),
                        "loop_point_hint": (options.get("loop_hint") or "").replace("+", " ")[:200] or None,
                        "gain_db_hint": opt_number("gain", -20.0),
                        "fade_in_ms": int(opt_number("fade_in", 1500)),
                        "fade_out_ms": int(opt_number("fade_out", 2000)),
                        "duck_under_dialogue": options.get("duck", "1") not in ("0", "false", "no"),
                        "source": "manual",
                        "status": "planned",
                        "segments": [],
                        "revision": 1,
                    }
                    continue
                plan = shot.get("bgm")
                if not plan:
                    raise ValueError(f"!bgm-seg 必须在本镜头先声明 !bgm：{raw}")
                anchor_raw = (options.get("anchor") or "last").strip()
                anchor_index = len(shot["lines"]) if anchor_raw == "last" else int(anchor_raw)
                plan["segments"].append(
                    {
                        "index": len(plan["segments"]) + 1,
                        "anchor_line_id": None,
                        "_anchor_index": anchor_index,
                        "narrative_function": (options.get("function") or "").replace("+", " ")[:300] or None,
                        "mood_family": mood,
                        "energy": opt_number("energy", plan.get("energy")),
                        "style": (options.get("style") or "").replace("+", " ")[:500] or None,
                        "duration_hint_ms": int(opt_number("dur", 0)) or None,
                        "transition_in": options.get("in") or "fade",
                        "transition_out": options.get("out") or "fade",
                        "note": (options.get("note") or "").replace("+", " ")[:300] or None,
                    }
                )
                continue
            if head == "silence":
                if len(parts) < 2:
                    raise ValueError(f"静音需要「时长 位置 [原因]」：{raw}")
                duration = int(parts[0])
                line_ids = [item["id"] for item in shot["lines"]]
                position = _parse_position(
                    parts[1],
                    line_ids,
                    warnings,
                    f"镜头 {shot['index']} 静音",
                    diagnostics=diagnostics,
                    shot_index=shot["index"],
                )
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
                tokens = list(parts)
                # 兼容旧脚本：形如 3:4 的比例令牌直接忽略，画幅统一以工程 output.aspect_ratio 为唯一来源
                if tokens and re.match(r"^\d+:\d+$", tokens[0]):
                    tokens = tokens[1:]
                if not tokens:
                    raise ValueError(f"画面需要提示词：{raw}")
                shot["visuals"].append(
                    {
                        "id": new_id("img"),
                        "index": len(shot["visuals"]) + 1,
                        "prompt": " ".join(tokens),
                        "aspect_ratio": output_aspect,
                        "width": output_width,
                        "height": output_height,
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
                    # 没有关键图时退化为纯文生视频，避免整章解析失败
                    key = (f"镜头 {shot['index']} 动画", "i2v 缺少关键图")
                    info_counts[key] = info_counts.get(key, 0) + 1
                    mode = "t2v"
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
        explicit_prefix = bool(match)
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
        if explicit_prefix and not normalize_speaker_name(speaker_name):
            # 模型按要求把无法判断的说话人留空：标为待校对，但不报硬告警
            speaker_id, confidence = UNKNOWN_SPEAKER_ID, 0.0
            key = (f"镜头 {shot['index']}", "未标注说话人")
            info_counts[key] = info_counts.get(key, 0) + 1
        else:
            speaker_id, confidence = _resolve_speaker(speaker_name, characters)
        if emotion not in EMOTIONS:
            warnings.append(INFO_PREFIX + f"镜头 {shot['index']} 情绪 {emotion} 非法，已改为 neutral")
            emotion = "neutral"
        if not kind:
            kind = "dialogue" if speaker_id != NARRATOR_ID else "narration"
        if kind not in ("dialogue", "narration", "inner_monologue"):
            raise ValueError(f"台词类型非法：{kind}")
        speaker_message = None
        if speaker_id == UNKNOWN_SPEAKER_ID and speaker_name and explicit_prefix and normalize_speaker_name(speaker_name):
            cleaned = normalize_speaker_name(speaker_name) or speaker_name[:20]
            speaker_message = f"镜头 {shot['index']} 说话人未识别：{cleaned}，标记为 chr_unknown"
            warnings.append(speaker_message)

        anchor, search_from, exact = _anchor(content, blocks, text, search_from, normalized)
        orphan_message = None
        if anchor["state"] == "orphan":
            orphan_message = f"镜头 {shot['index']} 的台词未在正文中找到，锚点为 orphan：{text[:24]}"
            warnings.append(orphan_message)
        elif not exact:
            key = (f"镜头 {shot['index']}", "台词锚点按同文就近校正")
            info_counts[key] = info_counts.get(key, 0) + 1
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
        if diagnostics is not None:
            line_index = len(shot["lines"]) + 1
            if speaker_message:
                diagnostics.append(
                    _line_diagnostic(
                        "speaker_unknown",
                        speaker_message,
                        shot,
                        line_index,
                        line_id,
                        text,
                        anchor,
                        speaker=cleaned,
                    )
                )
            if orphan_message:
                diagnostics.append(
                    _line_diagnostic("orphan", orphan_message, shot, line_index, line_id, text, anchor)
                )
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
        plan = shot.get("bgm")
        if plan:
            for segment in plan.get("segments") or []:
                anchor_index = int(segment.pop("_anchor_index", len(shot["lines"])))
                if 1 <= anchor_index <= len(shot["lines"]):
                    segment["anchor_line_id"] = shot["lines"][anchor_index - 1]["id"]
                else:
                    warnings.append(INFO_PREFIX + f"镜头 {shot['index']} 的 BGM 段落锚点 {anchor_index} 超出范围，已改到镜首")
                    segment["anchor_line_id"] = shot["lines"][0]["id"]

    _flush_info_counts(info_counts, warnings)
    if diagnostics is not None:
        # 统一补全章节 ID 与镜头名：_parse_position 等内部构造点拿不到这些上下文
        chapter_id = chapter.get("id")
        scene_labels = {shot.get("index"): shot.get("scene_label") for shot in shots}
        for item in diagnostics:
            if not item.get("chapter_id"):
                item["chapter_id"] = chapter_id
            if not item.get("scene_label") and item.get("shot_index") in scene_labels:
                item["scene_label"] = scene_labels[item.get("shot_index")]
    hard_warnings = [item for item in warnings if not item.startswith(INFO_PREFIX)]
    for shot in shots:
        shot["status"] = "ready" if not hard_warnings else "draft"

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
        "status": "draft" if hard_warnings else "ready",
        "shots": shots,
        "warnings": warnings,
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }
    return script
