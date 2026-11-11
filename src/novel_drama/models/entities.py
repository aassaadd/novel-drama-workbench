"""实体工厂：以纯 dict 表示实体（与 JSON Schema 一一对应），避免额外映射层。

所有工厂只负责补齐默认值与必填字段，不做业务校验；业务校验由 schema 校验器完成。
"""

from __future__ import annotations

from .duration import estimate_duration_ms
from .ids import new_id, now_iso

# 统一情绪枚举（TTS 适配器负责映射到具体引擎标签）
EMOTIONS = ("neutral", "joy", "sad", "angry", "fear", "surprise", "tender", "excited", "whisper", "serious")

# 旁白保留 ID
NARRATOR_ID = "chr_narrator"

# 未识别说话人
UNKNOWN_SPEAKER_ID = "chr_unknown"

# 虚拟角色的固定中文显示名：旁白与「未识别说话人」不写进 characters.json，只在展示层使用
VIRTUAL_SPEAKER_NAMES = {
    NARRATOR_ID: "旁白",
    UNKNOWN_SPEAKER_ID: "未知角色",
}


def speaker_display_name(speaker_id: str | None, characters: dict[str, dict] | None = None) -> str:
    """把说话人 ID 解析成可展示的中文名（台词表、时间轴、导出等展示层统一入口）。

    参数:
        speaker_id: 说话人角色 ID，可能为空。
        characters: 角色表（角色 ID → 角色 dict），可选。

    返回:
        人物姓名；旁白 / 未识别说话人返回固定中文名；其余解析不到时返回「未知角色」。
        任何情况下都不会把 `chr_xxx` 内部 ID 当作名字返回。
    """
    key = str(speaker_id or "")
    if not key:
        return "未知角色"
    virtual = VIRTUAL_SPEAKER_NAMES.get(key)
    if virtual:
        return virtual
    name = ((characters or {}).get(key) or {}).get("name")
    return name or "未知角色"


def new_work(title: str, author: str = "", language: str = "zh-CN", genre: list[str] | None = None) -> dict:
    """构造作品元数据。

    参数:
        title: 作品名。
        author: 作者。
        language: 语言标签，如 zh-CN。
        genre: 类型标签列表。

    返回:
        作品 dict（对应 project.schema.json 的 work 字段）。
    """
    ts = now_iso()
    return {
        "id": new_id("wrk"),
        "title": title,
        "author": author,
        "genre": genre or [],
        "logline": "",
        "language": language,
        "style_guide": "",
        "target_duration_ms": 0,
        "created_at": ts,
        "updated_at": ts,
    }


def new_narrator() -> dict:
    """构造旁白角色（保留 ID，音色可单独绑定）。"""
    ts = now_iso()
    return {
        "id": NARRATOR_ID,
        "name": "旁白",
        "aliases": [],
        "role": "narrator",
        "gender": "unknown",
        "age_range": None,
        "personality": [],
        "background": "",
        "speech_style": {"tempo": None, "pitch": None, "accent": None, "catchphrases": []},
        "voice_binding": None,
        "appearance_prompt": "",
        "negative_prompt": "",
        "portrait_asset": None,
        "created_at": ts,
        "updated_at": ts,
    }


def new_character(name: str, role: str = "supporting", aliases: list[str] | None = None) -> dict:
    """构造角色。

    参数:
        name: 角色正式名。
        role: protagonist/antagonist/supporting/minor。
        aliases: 别名列表，用于台词说话人识别。

    返回:
        角色 dict。
    """
    ts = now_iso()
    return {
        "id": new_id("chr"),
        "name": name,
        "aliases": aliases or [],
        "role": role,
        "gender": "unknown",
        "age_range": None,
        "personality": [],
        "background": "",
        "speech_style": {"tempo": None, "pitch": None, "accent": None, "catchphrases": []},
        "voice_binding": None,
        "appearance_prompt": "",
        "negative_prompt": "",
        "portrait_asset": None,
        "created_at": ts,
        "updated_at": ts,
    }


def new_line(
    scene_id: str,
    index: int,
    text: str,
    kind: str = "dialogue",
    speaker_id: str = UNKNOWN_SPEAKER_ID,
    emotion: str = "neutral",
    confidence: float | None = None,
) -> dict:
    """构造台词行（含旁白）。

    参数:
        scene_id: 所属场景 ID。
        index: 场景内序号。
        text: 要朗读的文本。
        kind: dialogue/narration/inner_monologue。
        speaker_id: 说话角色 ID；旁白为 chr_narrator，未识别为 chr_unknown。
        emotion: 统一情绪枚举。
        confidence: 说话人识别置信度（模型/规则给出）。

    返回:
        台词行 dict，TTS 状态初始为 pending。
    """
    return {
        "id": new_id("lin"),
        "scene_id": scene_id,
        "index": index,
        "kind": kind,
        "speaker_id": speaker_id,
        "text": text,
        "emotion": emotion,
        "emotion_intensity": 0.5,
        "speed": 1.0,
        "pause_before_ms": 0,
        "pause_after_ms": 120 if kind == "dialogue" else 200,
        "delivery_note": None,
        "inferred": True,
        "confidence": confidence,
        "tts": {"status": "pending", "asset_id": None, "duration_ms": None, "adapter": None, "voice_id": None, "synthesized_revision": None, "error": None},
        "revision": 1,
    }


def new_scene(chapter_id: str, index: int, title: str = "") -> dict:
    """构造场景。

    参数:
        chapter_id: 所属章节 ID。
        index: 场景序号。
        title: 场景标题，默认「场景 N」。

    返回:
        场景 dict（台词/音效/图/视频均为空数组）。
    """
    return {
        "id": new_id("scn"),
        "chapter_id": chapter_id,
        "index": index,
        "title": title or f"场景 {index + 1}",
        "location": None,
        "time_of_day": "unknown",
        "mood": None,
        "goal": None,
        "summary": None,
        "source_text": None,
        "character_ids": [],
        "target_duration_ms": None,
        "status": "draft",
        "lines": [],
        "sfx_events": [],
        "key_images": [],
        "video_segments": [],
        "revision": 1,
    }


def new_chapter(work_id: str, index: int, title: str, content: str = "", outline: str = "") -> dict:
    """构造章节。

    参数:
        work_id: 所属作品 ID。
        index: 章节序号（从 1 开始）。
        title: 章节标题。
        content: 章节正文。
        outline: 章节大纲。

    返回:
        章节 dict，状态 draft。
    """
    ts = now_iso()
    return {
        "schema_version": 1,
        "id": new_id("chp"),
        "work_id": work_id,
        "index": index,
        "title": title,
        "outline": outline or None,
        "synopsis": None,
        "content": content or None,
        "pov_character_id": None,
        "word_count": len("".join((content or "").split())),
        "status": "draft",
        "scenes": [],
        "revision": 1,
        "created_at": ts,
        "updated_at": ts,
    }


def new_sfx_event(scene_id: str, index: int, kind: str, prompt: str, duration_ms: int, line_id: str | None = None) -> dict:
    """构造音效事件。

    参数:
        scene_id: 所属场景 ID。
        index: 场景内序号。
        kind: ambience/foley/impact/transition/music_sting。
        prompt: 音效描述。
        duration_ms: 期望时长。
        line_id: 锚定台词行；为空则默认锚定场景起点。

    返回:
        音效事件 dict。
    """
    return {
        "id": new_id("sfx"),
        "scene_id": scene_id,
        "index": index,
        "kind": kind,
        "prompt": prompt,
        "duration_ms": duration_ms,
        "loop": kind == "ambience",
        "gain_db": -12.0,
        "fade_in_ms": 800 if kind == "ambience" else 0,
        "fade_out_ms": 1200 if kind == "ambience" else 0,
        "position": {"mode": "during_line" if line_id else "absolute", "line_id": line_id, "offset_ms": 0},
        "asset": {"status": "pending", "asset_id": None, "actual_duration_ms": None, "adapter": None, "cache_key": None, "error": None},
        "revision": 1,
    }


def new_key_image(scene_id: str, index: int, anchor_line_id: str, prompt: str, width: int = 1152, height: int = 1536) -> dict:
    """构造关键图。

    参数:
        scene_id: 所属场景 ID。
        index: 场景内序号。
        anchor_line_id: 锚定台词行（所属段落）。
        prompt: 正向提示词。
        width: 目标宽。
        height: 目标高。

    返回:
        关键图 dict，产物状态 pending。
    """
    return {
        "id": new_id("img"),
        "scene_id": scene_id,
        "index": index,
        "anchor_line_id": anchor_line_id,
        "anchor_desc": None,
        "prompt": prompt,
        "negative_prompt": "低质, 多余手指, 水印, 文字",
        "prompt_source": "auto",
        "style_ref": None,
        "aspect_ratio": "3:4" if height >= width else "4:3",
        "width": width,
        "height": height,
        "seed": None,
        "workflow_template_id": "qwen_image_accel",
        "params": {},
        "asset": {"status": "pending", "asset_id": None, "job_id": None, "width": None, "height": None, "error": None},
        "revision": 1,
    }


def new_video_segment(scene_id: str, anchor_line_id: str, prompt: str, mode: str = "t2v", source_image_id: str | None = None, duration_ms: int = 5000) -> dict:
    """构造短视频段落。

    参数:
        scene_id: 所属场景 ID。
        anchor_line_id: 锚定台词行。
        prompt: 视频提示词。
        mode: t2v（文生视频）或 i2v（图生视频）。
        source_image_id: i2v 的首帧关键图 ID。
        duration_ms: 时长，硬上限 5000ms。

    返回:
        短视频段落 dict。
    """
    return {
        "id": new_id("vid"),
        "scene_id": scene_id,
        "anchor_line_id": anchor_line_id,
        "start_offset_ms": 0,
        "duration_ms": min(5000, max(1000, duration_ms)),
        "mode": mode,
        "source_image_id": source_image_id,
        "prompt": prompt,
        "negative_prompt": "画面抖动, 变形, 多余肢体",
        "fps": 24,
        "workflow_template_id": "minimax_h3_accel" if mode == "i2v" else "minimax_h3_accel_t2v",
        "params": {},
        "audio_policy": "mute",
        "fallback_image_id": source_image_id,
        "asset": {"status": "pending", "asset_id": None, "job_id": None, "actual_duration_ms": None, "error": None},
        "revision": 1,
    }


def new_job(kind: str, target: dict, idempotency_key: str, priority: int = 100, adapter: str | None = None) -> dict:
    """构造渲染任务。

    参数:
        kind: writing/tts/sfx/image/video/mix/export。
        target: 作用对象 ID 集合（至少一个）。
        idempotency_key: 幂等键（由 orchestration.jobs 计算）。
        priority: 数值越小越先执行。
        adapter: 执行该任务的适配器名称。

    返回:
        渲染任务 dict，状态 queued。
    """
    ts = now_iso()
    return {
        "id": new_id("job"),
        "kind": kind,
        "target": target,
        "status": "queued",
        "priority": priority,
        "idempotency_key": idempotency_key,
        "params_hash": None,
        "attempts": 0,
        "max_attempts": 3,
        "depends_on": [],
        "adapter": adapter,
        "error": None,
        "artifacts": [],
        "timings": {"queued_at": ts, "started_at": None, "finished_at": None, "duration_ms": None},
        "logs_path": None,
        "created_at": ts,
    }


def provisional_line_duration_ms(line: dict) -> tuple[int, bool]:
    """返回台词行的时长与是否为估算值。

    参数:
        line: 台词行 dict。

    返回:
        (时长毫秒, 是否估算)。已合成则用实测 `tts.duration_ms`，否则按文本估算。
    """
    tts = line.get("tts") or {}
    duration = tts.get("duration_ms")
    if tts.get("status") == "ready" and duration:
        return int(duration), False
    return estimate_duration_ms(line.get("text", ""), float(line.get("speed") or 1.0)), True
