"""角色试听：用本书真实台词合成一段候选音色样音。

业务主体从路由层迁出：媒体总线、页面与后续批处理都调用同一份实现，
产物仍落在 `.ndw/cast_previews/`，不写章节、不入队、不进时间轴。
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from ...adapters.persistence import ProjectStore
from ...composition import find_voice, pick_variant, resolve_for_character, variant_ref
from ...infrastructure.config import AppConfig
from ...tts.errors import TtsError
from ...tts.service import TtsService
from ...tts.types import Requirement, VoiceRef
from ..production.lines_source import chapter_view
from .tts_errors import error_text

#: 试听保留条数（超出后删最旧的）
PREVIEW_KEEP = 12


def _sample_line(store: ProjectStore, character_id: str, prefer_chapter: str | None) -> tuple[dict, dict] | None:
    """给某个角色找一句已有台词（优先当前章节）。

    参数:
        store: 工程。
        character_id: 角色 ID。
        prefer_chapter: 优先章节 ID；为空时按章序找。

    返回:
        `(chapter, line)`；该角色没有任何台词时返回 None。
    """
    chapters = store.list_chapters()
    if prefer_chapter:
        chapters = sorted(chapters, key=lambda item: 0 if item.get("id") == prefer_chapter else 1)
    for item in chapters:
        try:
            chapter = chapter_view(store, store.load_chapter(item["id"]))
        except FileNotFoundError:
            continue
        for scene in chapter.get("scenes") or []:
            for line in scene.get("lines") or []:
                if line.get("speaker_id") == character_id and (line.get("text") or "").strip():
                    return chapter, line
    return None


def _prune_previews(folder: Path, keep: int = PREVIEW_KEEP) -> int:
    """清理试听目录，只保留最近 `keep` 条音频（连同侧车）。

    参数:
        folder: 试听目录。
        keep: 保留条数。

    返回:
        删除的文件数。
    """
    if not folder.is_dir():
        return 0
    files = sorted(
        (item for item in folder.iterdir() if item.is_file() and item.suffix == ".wav"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    removed = 0
    for item in files[keep:]:
        for target in (item, Path(str(item) + ".json")):
            if target.exists():
                try:
                    target.unlink()
                    removed += 1
                except OSError:
                    pass
    return removed


def synthesize_preview(store: ProjectStore, config: AppConfig | None, params: dict) -> dict:
    """用本书真实台词试听某个角色的候选音色。

    参数:
        store: 工程。
        config: 本机配置（透传给 TtsService）；为空时按默认配置读取。
        params: `{character_id, voice_id?, chapter?}`。

    返回:
        `{audio, text, duration_ms, voice_id, voice_name, variant_id, chapter_id,
        chapter_title, line_id, kept}`。

    异常:
        ValueError: 角色/音色不存在、没有台词、参考音失效或合成失败（中文原因）。
    """
    character_id = str(params.get("character_id") or "").strip()
    if not character_id:
        raise ValueError("缺少 character_id 参数（角色 ID）")
    characters = store.load_characters().get("characters") or []
    character = next((item for item in characters if item.get("id") == character_id), None)
    if character is None:
        raise ValueError(f"角色不存在：{character_id}")
    sample = _sample_line(store, character_id, params.get("chapter"))
    if sample is None:
        raise ValueError("这个角色还没有已有台词，无法用本书台词试听")
    chapter, line = sample

    voice_id = str(params.get("voice_id") or "").strip()
    voice = None
    if voice_id:
        table = store.load_voices()
        voice = find_voice(table, voice_id)
        if voice is None:
            raise ValueError(f"音色不存在：{voice_id}")
        variant = pick_variant(voice, line.get("emotion"), require_reference=True)
        if variant is None:
            raise ValueError(
                f"音色「{voice.get('name') or voice_id}」没有可用参考音频（需要 ready 状态且带参考音）"
            )
        ref_audio, ref_text = variant_ref(store, variant)
        resolved = {
            "voice_id": voice.get("id"),
            "variant_id": variant.get("id"),
            "ref_audio": ref_audio,
            "ref_text": ref_text,
            "source": variant.get("source"),
        }
    else:
        resolved = resolve_for_character(store, {"characters": characters}, character_id, line.get("emotion"))
        if not resolved.get("ref_audio"):
            raise ValueError("这个角色当前没有可用音色参考音频（先在音色库绑定并准备参考音）")
        table = store.load_voices()
        voice = find_voice(table, resolved.get("voice_id") or "")
        voice_id = (voice or {}).get("id") or resolved.get("voice_id") or "current"
    if not resolved.get("ref_audio") or not Path(str(resolved["ref_audio"])).exists():
        raise ValueError(f"参考音失效：{resolved.get('ref_audio')}（请重新上传或重新选择音色）")

    folder = store.root / ".ndw" / "cast_previews"
    folder.mkdir(parents=True, exist_ok=True)
    safe_voice = re.sub(r"[^A-Za-z0-9_-]+", "_", str(voice_id))[:40] or "voice"
    out_path = folder / f"{character_id}_{safe_voice}_{int(time.time() * 1000)}.wav"
    requirement = Requirement(
        text=line["text"],
        emotion=line.get("emotion"),
        emotion_intensity=max(0, min(3, round(float(line.get("emotion_intensity") or 0) * 3))),
        clone_required=True,
        character_id=character_id,
        line_id=None,
    )
    voice_ref = VoiceRef(
        variant_id=resolved.get("variant_id"),
        ref_audio=resolved.get("ref_audio"),
        ref_text=resolved.get("ref_text"),
        source=resolved.get("source") or "preset",
    )
    service = TtsService(config, store.root)
    try:
        payload = service.synthesize(line["text"], out_path, requirement=requirement, voice=voice_ref)
    except TtsError as exc:
        raise ValueError(error_text(exc.to_dict())) from exc
    finally:
        try:
            service.close()
        except Exception:  # noqa: BLE001, S110 - 收尾失败不覆盖上面的成功/失败结果
            pass
    _prune_previews(folder)
    return {
        "audio": str(out_path.relative_to(store.root)),
        "text": line["text"],
        "duration_ms": payload.get("duration_ms"),
        "voice_id": voice_id,
        "voice_name": (voice or {}).get("name") or voice_id,
        "variant_id": resolved.get("variant_id"),
        "chapter_id": chapter.get("id"),
        "chapter_title": chapter.get("title"),
        "line_id": line.get("id"),
        "kept": PREVIEW_KEEP,
    }
