"""配音复用计划：决定每句台词「沿用已有音频 / 复用缓存 / 重新合成 / 跳过」。

与 `tts.batch` 的关系：
- `prepare_line` 统一解析一行所需的音色、需求向量、provider、参数与缓存签名，
  批量合成与只读预检共用同一份结果，避免两处口径不一致；
- `plan_reuse` 在真正合成前给出逐行决策与节省估算，供 CLI dry-run、Web 预检与弹窗展示；
- `voice_changed_checker` 用于把「音色变体 / 参考音频变了但行状态仍 ready」的行
  重新纳入本次计划——缓存键会随之失效，从而只重跑受影响的行。

范围说明：本模块只服务配音与音频合成链路，不参与正文生成，也不改 chapter 的 revision 判定。
"""

from __future__ import annotations

from ..models import entities
from . import alignment, cache, line_state
from .types import Requirement, VoiceRef


def line_voice(store, characters_table: dict, line: dict, service, chapter: dict | None = None, voices: dict | None = None) -> dict:
    """解析一行该用的音色参考。

    参数:
        store: ProjectStore。
        characters_table: 角色表。
        line: 台词行。
        service: TtsService（提供 default_voice 兜底）。
        chapter: 所在章节（章节覆盖与旁白切换点需要）；缺省按无覆盖处理。
        voices: 已加载的音色库文档，可复用避免逐行读盘。

    返回:
        `{"voice_id", "variant_id", "ref_audio", "ref_text", "source"}`。
    """
    from ..store.voice_cast import resolve_cast_voice

    resolved = resolve_cast_voice(store, characters_table, chapter or {}, line, voices=voices)
    if resolved.get("ref_audio"):
        return resolved
    if resolved.get("voice_id"):
        # 角色已绑定音色但没有可用参考音频：保持原样，让这一行报 VOICE_MISSING
        return resolved
    default = service.default_voice
    return {
        "voice_id": None,
        "variant_id": default.variant_id,
        "ref_audio": default.ref_audio,
        "ref_text": default.ref_text,
        "source": default.source,
    }


def line_params(
    line: dict,
    requirement: Requirement,
    voice: VoiceRef,
    instruct: str | None,
    voice_id: str | None,
    *,
    ref_audio: str | None = None,
    ref_text: str | None = None,
    language: str | None = None,
) -> dict:
    """组装写进侧车并参与签名的行级参数。

    参数:
        line: 台词行（读取文本、情绪、强度、交付说明、语速）。
        requirement: 需求向量；行上没有情绪时用它的值兜底。
        voice: 音色参考（提供变体 id）。
        instruct: 风格指令。
        voice_id: 音色库条目 id。
        ref_audio: 参考音频路径（用于记录内容哈希，供音色变更判定）。
        ref_text: 参考音频对应文本。
        language: 合成语言。

    返回:
        参数字典；缺失字段留空。
    """
    return {
        "text": line.get("text"),
        "speaker_id": line.get("speaker_id"),
        "emotion": line.get("emotion") or requirement.emotion,
        "emotion_intensity": line.get("emotion_intensity"),
        "delivery_note": line.get("delivery_note"),
        "speed": line.get("speed"),
        "target_duration_ms": line.get("target_duration_ms"),
        "voice_id": voice_id,
        "variant_id": voice.variant_id,
        "instruct": instruct,
        "ref_sha256": cache.ref_hash(ref_audio),
        "ref_text": ref_text,
        "language": language,
    }


def engine_params(service) -> dict:
    """取参与签名的引擎侧参数（切分与接缝裁切）。

    参数:
        service: TtsService。

    返回:
        与侧车 `params` 同名的参数字典。
    """
    return {
        "chunk_limit": service.chunk_limit,
        "chunk_silence_ms": service.chunk_silence_ms,
        "edge_trim_ms": service.edge_trim_ms,
        "edge_trim_max_ms": service.edge_trim_max_ms,
        "keep_min_ms": service.keep_min_ms,
        "silence_threshold_dbfs": service.silence_threshold_dbfs,
        "hard_split_ratio": service.hard_split_ratio,
    }


def prepare_line(service, store, characters_table: dict, chapter: dict, line: dict, *, voices: dict | None = None) -> dict:
    """解析一行的合成上下文与缓存签名。

    参数:
        service: TtsService。
        store: ProjectStore。
        characters_table: 角色表。
        chapter: 章节 dict。
        line: 台词行。
        voices: 已加载的音色库文档，可复用避免逐行读盘。

    返回:
        `{voice_info, requirement, voice, line_provider, character_provider, routing,
          provider_id, model_version, params, signature, key}`；
        没解析到参考音频时额外带 `error`，调用方据此报 VOICE_MISSING。
    """
    from .batch import _instruct_for

    voice_info = line_voice(store, characters_table, line, service, chapter, voices=voices)
    if not voice_info.get("ref_audio"):
        return {"voice_info": voice_info, "error": {"code": "VOICE_MISSING", "message": "未解析到参考音频"}}

    character = next(
        (item for item in characters_table.get("characters") or [] if item.get("id") == line.get("speaker_id")),
        None,
    )
    binding = (character or {}).get("voice_binding") or {}
    adapter = binding.get("adapter")
    character_provider = adapter if adapter and adapter != "placeholder" else None
    instruct = _instruct_for(line)
    requirement = Requirement(
        text=line.get("text") or "",
        emotion=line.get("emotion"),
        emotion_intensity=max(0, min(3, round(float(line.get("emotion_intensity") or 0) * 3))),
        clone_required=True,
        target_duration_ms=line.get("target_duration_ms"),
        character_id=line.get("speaker_id"),
        line_id=line.get("id"),
    )
    voice = VoiceRef(
        variant_id=voice_info.get("variant_id"),
        ref_audio=voice_info.get("ref_audio"),
        ref_text=voice_info.get("ref_text"),
        source=voice_info.get("source") or "preset",
    )
    line_provider = (line.get("voice") or {}).get("provider_id")
    try:
        routing = service.decide(requirement, line_provider, character_provider)
    except Exception as exc:  # noqa: BLE001 - 没有可用 provider 时也要能出预检，而不是整页 500
        code = getattr(exc, "code", None) or "ROUTING_NO_CANDIDATE"
        message = getattr(exc, "message", None) or str(exc) or "没有可用的 TTS provider"
        return {"voice_info": voice_info, "error": {"code": code, "message": message}}
    provider_id = routing["selected"]["provider_id"]
    provider = service.providers.get(provider_id)

    params = line_params(
        line,
        requirement,
        voice,
        instruct,
        voice_info.get("voice_id"),
        ref_audio=voice_info.get("ref_audio"),
        ref_text=voice_info.get("ref_text"),
        language=requirement.language,
    )
    params.update(engine_params(service))
    params.update(
        {
            "provider_id": provider_id,
            "model_version": provider.spec.model_version if provider else None,
        }
    )
    signature = cache.build_signature(
        params,
        ref_audio=voice_info.get("ref_audio"),
        ref_text=voice_info.get("ref_text"),
        target_duration_ms=line.get("target_duration_ms"),
        alignment=alignment.resolve_config(getattr(service, "alignment_cfg", None)),
    )
    return {
        "voice_info": voice_info,
        "requirement": requirement,
        "voice": voice,
        "line_provider": line_provider,
        "character_provider": character_provider,
        "routing": routing,
        "provider_id": provider_id,
        "model_version": provider.spec.model_version if provider else None,
        "params": params,
        "signature": signature,
        "key": cache.signature_key(signature),
    }


def changed_checker(service, store, characters_table: dict, chapter: dict, *, voices: dict | None = None):
    """构造「音色或引擎是否变过」判定函数，供批量规划把 ready 行重新纳入。

    判定口径（命中任一即视为变化）：
    1. 当前解析出的 `voice_id` / `variant_id` 与侧车快照不同；
    2. 侧车记录了参考音频哈希，且当前参考音频内容哈希不同；
    3. 当前路由出的 provider / model 与侧车记录不同（换引擎或模型版本要重配）。

    参数:
        service: TtsService。
        store: ProjectStore。
        characters_table: 角色表。
        chapter: 章节 dict。
        voices: 已加载的音色库文档。

    返回:
        `checker(line, snapshot) -> bool`；无法判定时返回 False（不误伤已就绪行）。
    """

    def check(line: dict, snapshot: dict | None) -> bool:
        """判断单行音色或引擎是否变化。"""
        params = (snapshot or {}).get("params") or {}
        if not params:
            return False
        context = prepare_line(service, store, characters_table, chapter, line, voices=voices)
        if context.get("error"):
            # 当前没有可用 provider 时不主动判失效，避免一次性把整章打成待合成
            return False
        info = context["voice_info"]
        if not info.get("ref_audio"):
            return False
        if str(info.get("voice_id") or "") != str(params.get("voice_id") or ""):
            return True
        if str(info.get("variant_id") or "") != str(params.get("variant_id") or ""):
            return True
        recorded = params.get("ref_sha256")
        if recorded and cache.ref_hash(info.get("ref_audio")) != recorded:
            return True
        if str(params.get("provider_id") or "") != str(context.get("provider_id") or ""):
            return True
        if str(params.get("model_version") or "") != str(context.get("model_version") or ""):
            return True
        return False

    return check


def _locator(chapter: dict) -> dict[str, tuple[int, int, str]]:
    """建立「行 ID → (场景序号, 行号, 场景标题)」，序号均从 1 开始。

    参数:
        chapter: 章节 dict。

    返回:
        定位映射。
    """
    mapping: dict[str, tuple[int, int, str]] = {}
    scenes = sorted(chapter.get("scenes") or [], key=lambda item: item.get("index") or 0)
    for scene_no, scene in enumerate(scenes, start=1):
        title = scene.get("title") or f"第 {scene_no} 场"
        for line_no, line in enumerate(sorted(scene.get("lines") or [], key=lambda item: item.get("index") or 0), start=1):
            if line.get("id"):
                mapping[str(line["id"])] = (scene_no, line_no, title)
    return mapping


def plan_reuse(
    service,
    store,
    chapter: dict,
    rows: list[dict],
    *,
    characters_table: dict,
    snapshots: dict | None = None,
    engine: dict | None = None,
    live_ids: set[str] | None = None,
    states: list[dict] | None = None,
    cache_table: dict | None = None,
    avg_ms_per_line: int | None = None,
    voices: dict | None = None,
) -> dict:
    """为本章生成「沿用 / 复用 / 重合成 / 跳过」的逐行计划。

    参数:
        service: TtsService。
        store: ProjectStore。
        chapter: 章节 dict。
        rows: `plan_batch` 的结果（本次真正要处理的行）。
        characters_table: 角色表。
        snapshots: `{line_id: 侧车快照}`；缺省时现读。
        engine: 当前配置导出的引擎侧参数；缺省时按章节现算。
        live_ids: 队列里活跃的行 ID。
        states: 已算好的 `line_state.chapter_states` 结果；可复用避免重复计算。
        cache_table: 已加载的缓存表；缺省时现读。
        avg_ms_per_line: 单行平均合成耗时（毫秒），用于估算节省时间；为空时不给估算。
        voices: 已加载的音色库文档。

    返回:
        `{keep, reuse, regenerate, blocked, skipped, reused_audio_ms, saved_ms_estimate,
          avg_ms_per_line, items}`；`items` 保持章内顺序。
    """
    index = {item["id"]: item for item in characters_table.get("characters") or []}
    if states is None:
        states = line_state.chapter_states(
            chapter,
            index,
            snapshots if snapshots is not None else line_state.load_snapshots(store.root, chapter),
            root=store.root,
            live_ids=live_ids,
            engine=engine,
        )
    if cache_table is None:
        cache_table = cache.load(store.root)
    locator = _locator(chapter)
    planned = {row["line"].get("id"): row for row in rows}
    counts = {"keep": 0, "reuse": 0, "regenerate": 0, "blocked": 0, "skipped": 0}
    reused_audio_ms = 0
    items: list[dict] = []

    for state_row in states:
        line = state_row.get("line") or {}
        line_id = line.get("id")
        scene_no, line_no, scene_title = locator.get(str(line_id), (0, 0, ""))
        state = state_row.get("state")
        base = {
            "chapter_id": chapter.get("id"),
            "chapter_title": chapter.get("title"),
            "scene_id": state_row.get("scene_id"),
            "scene_index": scene_no,
            "scene_title": scene_title,
            "line_id": line_id,
            "line_index": line_no,
            "speaker_id": line.get("speaker_id"),
            "speaker_name": entities.speaker_display_name(line.get("speaker_id"), index),
            "text": line.get("text"),
            "has_audio": bool((line.get("tts") or {}).get("asset_id")),
            "state": state,
        }
        if state == "skipped":
            counts["skipped"] += 1
            items.append({**base, "decision": "skip", "reason": "skipped", "changes": []})
            continue
        if line_id not in planned:
            if state == "ready":
                # 已就绪且参数、音色都没变：本次不动它，音频原样沿用
                counts["keep"] += 1
                items.append({**base, "decision": "keep", "reason": "unchanged", "changes": []})
            else:
                # 未进入本次计划（显式跳过、超出 limit 等）：保持原样，不算沿用
                counts["skipped"] += 1
                items.append({**base, "decision": "skip", "reason": "out_of_scope", "changes": []})
            continue

        context = prepare_line(service, store, characters_table, chapter, line, voices=voices)
        if context.get("error"):
            counts["blocked"] += 1
            items.append(
                {
                    **base,
                    "decision": "blocked",
                    "reason": context["error"]["code"],
                    "message": context["error"].get("message"),
                    "changes": [],
                }
            )
            continue
        hit = cache.lookup(store.root, context["key"], table=cache_table)
        if hit:
            counts["reuse"] += 1
            reused_audio_ms += int(hit.get("duration_ms") or 0)
            items.append(
                {
                    **base,
                    "decision": "reuse",
                    "reason": "matched",
                    "cache_key": context["key"],
                    "duration_ms": hit.get("duration_ms"),
                    "changes": [],
                }
            )
            continue
        counts["regenerate"] += 1
        previous = cache.entry_for_line(cache_table, line_id)
        changes = cache.describe_changes(previous.get("signature") if previous else None, context["signature"])
        items.append(
            {
                **base,
                "decision": "synth",
                "reason": "params_changed" if changes else ("first" if not previous else "cache_cleared"),
                "changes": changes,
            }
        )

    saved = 0
    if avg_ms_per_line and counts["reuse"]:
        saved = counts["reuse"] * int(avg_ms_per_line)
    return {
        **counts,
        "reused_audio_ms": reused_audio_ms,
        "saved_ms_estimate": saved,
        "avg_ms_per_line": avg_ms_per_line,
        "items": items,
    }
