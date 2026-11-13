"""行级批量合成：按章节/场景逐行跑 TTS，单并发、可控批次、失败不中断。

设计要点：
- 每行独立合成并立刻回写章节，进程中断时已完成的行不会丢；
- 逐行失败只记录到该行 `tts.error` 与批次报告，继续下一行；
- 音色解析顺序：角色绑定 → 音色库情绪变体 → 配置默认参考音频；
- 缓存键包含文本、音色变体、参考音频内容、provider 版本与合成参数，命中则复用音频；
- 有目标时长时按 `tts.alignment` 做「微变速 / 重生成 / 换 provider / 接受」；
- 峰值内存用 `ru_maxrss` 记录（macOS 单位为字节）。
"""

from __future__ import annotations

import json
import resource
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from ..orchestration.pipeline import is_line_stale
from . import alignment, cache, line_state, sidecar
from .errors import TtsError
from .fingerprint import line_fingerprint
from .service import split_text


def _now() -> str:
    """当前 UTC 时间（ISO 8601）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def peak_rss_mb() -> float:
    """当前进程峰值内存（MB）；macOS 的 ru_maxrss 单位是字节。"""
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 / 1024, 1)


def plan_batch(
    chapter: dict,
    characters: list[dict],
    scene_ids: list[str] | None = None,
    include_ready: bool = False,
    limit: int | None = None,
    snapshots: dict[str, dict] | None = None,
    root=None,
    engine: dict | None = None,
    live_ids: set[str] | None = None,
    line_ids: set[str] | None = None,
    skip_line_ids: set[str] | None = None,
) -> list[dict]:
    """规划本章需要合成的行。

    参数:
        chapter: 章节 dict。
        characters: 角色表。
        scene_ids: 只处理这些场景；为空处理全部。
        include_ready: 是否连已完成的行一起重跑。
        limit: 最多返回多少行。
        snapshots: `{line_id: 侧车快照}`；给出时按参数指纹判定过期。
        root: 工程根目录；给出时会跳过产物文件已丢失的行。
        engine: 当前配置导出的引擎侧参数；改接缝裁切配置后据此判过期。
        live_ids: 当前队列里活跃的行 ID；用于僵尸态降级。
        line_ids: 只保留这些行（范围选择器的结果）；为空表示整章。
        skip_line_ids: 不参与本次合成的行（说话人未识别且用户选择跳过）；为空表示不跳过。

    返回:
        [{scene_id, scene_index, line}]，保持章内顺序。
    """
    index = {item["id"]: item for item in characters}
    snapshot_index = snapshots or {}
    rows: list[dict] = []
    for scene in sorted(chapter.get("scenes") or [], key=lambda item: item.get("index") or 0):
        if scene_ids and scene.get("id") not in scene_ids:
            continue
        for line in sorted(scene.get("lines") or [], key=lambda item: item.get("index") or 0):
            if line_ids is not None and line.get("id") not in line_ids:
                continue
            if skip_line_ids and line.get("id") in skip_line_ids:
                continue
            if not include_ready and not is_line_stale(
                line, index, snapshot_index.get(line.get("id")), root=root, live_ids=live_ids, engine=engine
            ):
                continue
            if not (line.get("text") or "").strip():
                continue
            rows.append({"scene_id": scene["id"], "scene_index": scene.get("index"), "line": line})
            if limit and len(rows) >= limit:
                return rows
    return rows


def _line_voice(store, characters_table: dict, line: dict, service, chapter: dict | None = None) -> dict:
    """解析一行该用的音色参考。

    参数:
        store: ProjectStore。
        characters_table: 角色表。
        line: 台词行。
        service: TtsService（提供 default_voice 兜底）。
        chapter: 所在章节（R18 章节覆盖与旁白切换点需要）；缺省时按无覆盖处理。

    返回:
        {"voice_id", "variant_id", "ref_audio", "ref_text", "source"}。
    """
    from ..store.voice_cast import resolve_cast_voice

    resolved = resolve_cast_voice(store, characters_table, chapter or {}, line)
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


def line_group_key(line: dict, chapter: dict, characters_table: dict, service, store) -> tuple:
    """计算一行在常驻 worker 上的复用分组键（`tts run` 与 `tts batch` 共用）。

    一级键 `(provider_id, model_dir, use_qwen_emo)` 决定是否重启 worker；
    二级键 `(ref_audio, emo_audio_prompt)` 决定组内聚簇，减少参考音频重复编码。

    参数:
        line: 台词行。
        chapter: 所在章节（章节覆盖/旁白切换点需要）。
        characters_table: `{"characters": [...]}`。
        service: TtsService。
        store: 工程。

    返回:
        排序键元组。
    """
    from .types import Requirement

    voice_info = _line_voice(store, characters_table, line, service, chapter)
    character = next(
        (item for item in characters_table.get("characters") or [] if item.get("id") == line.get("speaker_id")),
        {},
    )
    binding = character.get("voice_binding") or {}
    adapter = binding.get("adapter")
    character_provider = adapter if adapter and adapter != "placeholder" else None
    line_provider = (line.get("voice") or {}).get("provider_id")
    requirement = Requirement(
        text=line.get("text") or "",
        emotion=line.get("emotion"),
        emotion_intensity=max(0, min(3, round(float(line.get("emotion_intensity") or 0) * 3))),
        clone_required=True,
        target_duration_ms=line.get("target_duration_ms"),
        character_id=line.get("speaker_id"),
        line_id=line.get("id"),
    )
    decision = service.decide(requirement, line_provider, character_provider)
    pid = decision["selected"]["provider_id"]
    provider = service.providers.get(pid)
    model_dir = (provider.spec.weights_path if provider else None) or ""
    # 文字情绪需要 qwen，进程内切换模式会重启，故按开关分组
    use_qwen = 1 if (line.get("delivery_note") or "").strip() else 0
    ref_audio = voice_info.get("ref_audio") or ""
    emo_audio = ref_audio if voice_info.get("source") == "emotion_ref" else ""
    return (str(pid), str(model_dir), use_qwen, str(ref_audio), str(emo_audio))


def sort_rows_by_group(rows: list[dict], chapter: dict, characters: list[dict], service, store) -> list[dict]:
    """按 worker 复用键对本章待合成行做稳定排序（只改执行顺序，不改产物与命名）。

    参数:
        rows: `plan_batch` 的结果。
        chapter: 章节 dict。
        characters: 角色列表。
        service: TtsService。
        store: 工程。

    返回:
        排序后的行列表；预解析失败时原样返回，保证不会漏排或错排。
    """
    if len(rows) <= 1:
        return rows
    try:
        characters_table = {"characters": characters}
        return sorted(
            rows,
            key=lambda row: line_group_key(row.get("line") or {}, chapter, characters_table, service, store),
        )
    except Exception as exc:  # noqa: BLE001 - 分组只是优化，失败必须退回原顺序
        print(f"[warn] 配音分组预解析失败，按原顺序执行：{exc}")
        return rows


def _relative(store, path: Path) -> str:
    """把绝对路径转成相对工程根目录的路径（失败则原样返回）。"""
    try:
        return str(path.resolve().relative_to(store.root))
    except ValueError:
        return str(path)


def _chunk_index(store, out_path: Path, text: str, provider_id: str | None, variant_id: str | None, limit: int) -> list[dict]:
    """生成 tts.chunks 记录。

    说明:
        中间分块在合成成功写完成片后会被清理，因此这里通常拿不到分块文件，
        `asset_id` 为空、状态仍记 `ready`（表示该块已随成片合成完成）。
        若分块目录仍存在（旧产物或清理失败），则照常回填相对路径。
    """
    chunk_dir = out_path.parent / f"{out_path.stem}_chunks"
    chunks = []
    for idx, chunk_text in enumerate(split_text(text, limit) or [text]):
        chunk_path = chunk_dir / f"{idx:03d}.wav"
        chunks.append(
            {
                "index": idx,
                "text": chunk_text,
                "asset_id": _relative(store, chunk_path) if chunk_path.exists() else None,
                "duration_ms": None,
                "provider_id": provider_id,
                "variant_id": variant_id,
                "status": "ready",
            }
        )
    return chunks


def _instruct_for(line: dict) -> str | None:
    """取台词行的风格指令：只认显式交付说明。

    说明:
        情绪不再自动改写成指令文本，否则会和 IndexTTS 的 8 维向量通路互斥；
        需要「情绪→指令」的 provider（如 CosyVoice3）在自己的实现里改写。
    """
    return line.get("delivery_note") or None


def _next_provider(service, current: str | None) -> str | None:
    """返回 provider_order 中下一个可用的 provider（用于换 provider 兜底）。"""
    for pid in service.provider_order:
        provider = service.providers.get(pid)
        if pid != current and provider is not None and provider.spec.enabled:
            return pid
    return None


def _cache_key(service, text: str, voice_info: dict, instruct: str | None, emotion: str | None, provider_id: str | None, model_version: str | None) -> str:
    """计算一条台词的缓存键（含接缝裁切参数，保证产物与配置一致）。"""
    return cache.compute_key(
        text=text,
        variant_id=voice_info.get("variant_id"),
        ref_audio=voice_info.get("ref_audio"),
        provider_id=provider_id,
        model_version=model_version,
        instruct=instruct,
        emotion=emotion,
        chunk_limit=service.chunk_limit,
        chunk_silence_ms=service.chunk_silence_ms,
        sample_rate=24000,
        edge_trim_ms=service.edge_trim_ms,
        edge_trim_max_ms=service.edge_trim_max_ms,
        keep_min_ms=service.keep_min_ms,
        silence_threshold_dbfs=service.silence_threshold_dbfs,
        hard_split_ratio=service.hard_split_ratio,
    )


def _line_params(line: dict, requirement, voice, instruct: str | None, voice_id: str | None) -> dict:
    """组装写进侧车并参与指纹的行级参数。

    参数:
        line: 台词行（读取文本、情绪、强度、交付说明、语速）。
        requirement: 需求向量；行上没有情绪时用它的值兜底。
        voice: 音色参考（提供变体 id）。
        instruct: 风格指令。
        voice_id: 音色库条目 id，由调用方解析后传入。

    返回:
        参数字典；缺失字段留空，指纹计算时统一按 None 处理。
    """
    return {
        "text": line.get("text"),
        "emotion": line.get("emotion") or requirement.emotion,
        "emotion_intensity": line.get("emotion_intensity"),
        "delivery_note": line.get("delivery_note"),
        "speed": line.get("speed"),
        "voice_id": voice_id,
        "variant_id": voice.variant_id,
        "instruct": instruct,
    }


def _synthesize_with_alignment(
    service,
    out_path: Path,
    line: dict,
    requirement,
    voice,
    instruct: str | None,
    line_provider: str | None,
    character_provider: str | None,
    cfg: dict,
    voice_id: str | None = None,
) -> dict:
    """合成一句，并按配置做时长对齐（微变速 / 重生成 / 换 provider）。

    参数:
        service: TtsService。
        out_path: 输出路径。
        line: 台词行（读取 target_duration_ms）。
        requirement: 需求向量。
        voice: 音色参考。
        instruct: 风格指令。
        line_provider: 行级 provider 锁定。
        character_provider: 角色级 provider 锁定。
        cfg: 对齐配置。
        voice_id: 音色库条目 id，写入侧车快照。

    返回:
        service.synthesize 的 payload，额外带 `alignment` 字段。
    """
    target_ms = line.get("target_duration_ms")
    attempt = 0
    switch_to = None
    history: list[dict] = []
    while True:
        payload = service.synthesize(
            line["text"],
            out_path,
            requirement=requirement,
            voice=voice,
            instruct=instruct,
            line_provider_id=switch_to or line_provider,
            character_provider_id=character_provider,
            params_extra=_line_params(line, requirement, voice, instruct, voice_id),
        )
        plan = alignment.evaluate(int(payload.get("duration_ms") or 0), target_ms, cfg, attempt)
        if plan["action"] == "tempo":
            info = alignment.apply_tempo(out_path, plan["ratio"])
            payload["duration_ms"] = info["after_ms"] or payload["duration_ms"]
            plan["applied"] = info
            payload["alignment"] = plan
            payload["alignment"]["history"] = history
            return payload
        if plan["action"] in ("regenerate", "provider_switch"):
            history.append(plan)
            if plan["action"] == "provider_switch":
                switch_to = _next_provider(service, payload.get("provider_id"))
                if not switch_to:
                    plan = {**plan, "action": "accept", "reason": plan["reason"] + "；没有其他可用 provider"}
                    payload["alignment"] = plan
                    payload["alignment"]["history"] = history
                    return payload
            attempt += 1
            continue
        plan["history"] = history
        payload["alignment"] = plan
        return payload


def _mark_failed(store, chapter, line, entry, batch_results, stats, error) -> None:
    """统一记录一行失败（行内 + 批次结果 + 统计）。"""
    entry.update({"status": "failed", "error": error})
    line.setdefault("tts", {}).update({"status": "failed", "error": {**error, "at": _now()}})
    line["tts"]["synthesized_revision"] = None
    store.save_chapter(chapter)
    stats["failed"] += 1
    batch_results.append(entry)


def run_batch(
    service,
    store,
    chapter: dict,
    rows: list[dict],
    batch_size: int = 5,
    dry_run: bool = False,
    reporter=None,
    write_report: bool = True,
    batch_id: str | None = None,
    use_cache: bool = True,
) -> dict:
    """逐行合成并回写章节。

    参数:
        service: TtsService。
        store: ProjectStore。
        chapter: 章节 dict（会被就地修改并逐行保存）。
        rows: `plan_batch` 的结果。
        batch_size: 每多少行汇报一次。
        dry_run: 只解析音色与输出路径，不调用模型。
        reporter: 可选回调 `reporter(批次序号, 批次结果列表, 累计统计)`。
        write_report: 是否单独写一份章节批次报告（多章节汇总时置 False）。
        batch_id: 批次 ID；缺省自动生成。
        use_cache: 是否复用产物缓存（只影响读取；新产物仍会写回缓存）。

    返回:
        批次报告 dict（write_report 为真时同时写入 `.ndw/tts_batches/<batch_id>.json`）。
    """
    characters_table = store.load_characters()
    batch_id = batch_id or "btch_" + datetime.now(timezone.utc).strftime("%y%m%d%H%M%S")
    started = time.perf_counter()
    results: list[dict] = []
    stats = {"requested": len(rows), "succeeded": 0, "failed": 0, "skipped": 0, "cache_hits": 0, "audio_ms": 0}
    # 本次批次的待写缓存条目，循环结束后一次性落盘，避免逐行全量读写缓存文件
    pending_cache: dict[str, dict] = {}
    # 章开始时整表读一次，章内所有行复用同一份，避免逐行读盘（读放大）
    cache_table = cache.load(store.root)
    batch_size = max(1, int(batch_size or 1))
    align_cfg = alignment.resolve_config(getattr(service, "alignment_cfg", None))

    for offset in range(0, len(rows), batch_size):
        batch_rows = rows[offset : offset + batch_size]
        batch_results = []
        for row in batch_rows:
            line = row["line"]
            voice_info = _line_voice(store, characters_table, line, service, chapter)
            rel_path = f"assets/voice/lines/{line['id']}_r{line['revision']}.wav"
            out_path = store.root / rel_path
            instruct = _instruct_for(line)
            entry = {
                "line_id": line.get("id"),
                "scene_id": row["scene_id"],
                "speaker_id": line.get("speaker_id"),
                "emotion": line.get("emotion"),
                "voice_id": voice_info.get("voice_id"),
                "variant_id": voice_info.get("variant_id"),
                "voice_source": voice_info.get("source"),
                "audio_path": rel_path,
                "status": "pending",
            }
            if dry_run:
                entry["status"] = "planned"
                batch_results.append(entry)
                stats["skipped"] += 1
                continue
            if not voice_info.get("ref_audio"):
                _mark_failed(
                    store,
                    chapter,
                    line,
                    entry,
                    batch_results,
                    stats,
                    {"code": "VOICE_MISSING", "message": "未解析到参考音频"},
                )
                continue

            from ..tts.types import Requirement, VoiceRef

            character = next(
                (item for item in characters_table.get("characters") or [] if item.get("id") == line.get("speaker_id")),
                None,
            )
            binding = (character or {}).get("voice_binding") or {}
            adapter = binding.get("adapter")
            character_provider = adapter if adapter and adapter != "placeholder" else None
            requirement = Requirement(
                text=line["text"],
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

            # ---- 缓存命中：直接复用，不调用模型 ----
            decision = service.decide(requirement, line_provider, character_provider)
            expected_pid = decision["selected"]["provider_id"]
            expected_spec = service.providers.get(expected_pid)
            key = _cache_key(
                service,
                line["text"],
                voice_info,
                instruct,
                line.get("emotion"),
                expected_pid,
                expected_spec.spec.model_version if expected_spec else None,
            )
            hit = cache.lookup(store.root, key, table=cache_table) if use_cache else None
            if hit:
                try:
                    src_path = Path(hit["asset_id"])
                    if not src_path.is_absolute():
                        src_path = store.root / hit["asset_id"]
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    # 缓存命中的往往就是本行自己的产物，同文件不能 copy2
                    if src_path.resolve() != out_path.resolve():
                        shutil.copy2(str(src_path), str(out_path))
                except OSError as exc:
                    hit = None
                    entry["cache_error"] = str(exc)
            if hit:
                payload = {
                    "provider_id": hit.get("provider_id"),
                    "model_version": hit.get("model_version"),
                    "duration_ms": hit.get("duration_ms"),
                    "cache_hit": True,
                }
                line["tts"] = {
                    "status": "ready",
                    "asset_id": rel_path,
                    "duration_ms": payload["duration_ms"],
                    "adapter": payload["provider_id"],
                    "voice_id": voice_info.get("voice_id"),
                    "variant_id": voice_info.get("variant_id"),
                    "provider_id": payload["provider_id"],
                    "model_version": payload.get("model_version"),
                    "routing": decision,
                    "chunks": [
                        {
                            "index": 0,
                            "text": line["text"],
                            "asset_id": rel_path,
                            "duration_ms": payload["duration_ms"],
                            "provider_id": payload["provider_id"],
                            "variant_id": voice_info.get("variant_id"),
                            "status": "ready",
                        }
                    ],
                    "synthesized_revision": line.get("revision"),
                    "actual_speed": 1.0,
                    "error": None,
                    "updated_at": _now(),
                }
                store.save_chapter(chapter)
                hit_params = _line_params(line, requirement, voice, instruct, voice_info.get("voice_id"))
                hit_params.update(
                    {
                        "provider_id": payload["provider_id"],
                        "model_version": payload.get("model_version"),
                        "chunk_limit": service.chunk_limit,
                        "chunk_silence_ms": service.chunk_silence_ms,
                        "edge_trim_ms": service.edge_trim_ms,
                        "edge_trim_max_ms": service.edge_trim_max_ms,
                        "keep_min_ms": service.keep_min_ms,
                        "silence_threshold_dbfs": service.silence_threshold_dbfs,
                        "hard_split_ratio": service.hard_split_ratio,
                    }
                )
                sidecar.dump(
                    out_path,
                    {
                        "payload_version": sidecar.SIDECAR_VERSION,
                        "text": line["text"],
                        "provider_id": payload["provider_id"],
                        "model_version": payload.get("model_version"),
                        "variant_id": voice_info.get("variant_id"),
                        "cache_hit": True,
                        "duration_ms": payload["duration_ms"],
                        "created_at": _now(),
                        "params": hit_params,
                        "fingerprint": line_fingerprint(hit_params),
                        "dimensions": {"sample_rate": None, "duration_ms": payload["duration_ms"]},
                    },
                )
                entry.update(
                    {
                        "status": "ready",
                        "cache_hit": True,
                        "provider_id": payload["provider_id"],
                        "model_version": payload.get("model_version"),
                        "duration_ms": payload["duration_ms"],
                        "sidecar": f"{rel_path}.json",
                    }
                )
                store.save_chapter(chapter)
                stats["succeeded"] += 1
                stats["cache_hits"] += 1
                stats["audio_ms"] += int(payload["duration_ms"] or 0)
                batch_results.append(entry)
                continue

            # ---- 未命中：真实合成 + 对齐 ----
            try:
                payload = _synthesize_with_alignment(
                    service,
                    out_path,
                    line,
                    requirement,
                    voice,
                    instruct,
                    line_provider,
                    character_provider,
                    align_cfg,
                    voice_id=voice_info.get("voice_id"),
                )
            except TtsError as exc:
                _mark_failed(store, chapter, line, entry, batch_results, stats, exc.to_dict())
                continue

            speed = 1.0
            align_info = payload.get("alignment") or {}
            if align_info.get("action") == "tempo" and align_info.get("applied", {}).get("applied"):
                speed = float(align_info["applied"].get("ratio") or 1.0)

            line["tts"] = {
                "status": "ready",
                "asset_id": rel_path,
                "duration_ms": payload["duration_ms"],
                "adapter": payload["provider_id"],
                "voice_id": voice_info.get("voice_id"),
                "variant_id": voice_info.get("variant_id"),
                "provider_id": payload["provider_id"],
                "model_version": payload.get("model_version"),
                "routing": payload.get("routing"),
                "chunks": _chunk_index(
                    store, out_path, line["text"], payload["provider_id"], voice_info.get("variant_id"), service.chunk_limit
                ),
                "synthesized_revision": line.get("revision"),
                "actual_speed": speed,
                "error": None,
                "updated_at": _now(),
            }
            store.save_chapter(chapter)

            actual_spec = service.providers.get(payload["provider_id"])
            actual_key = _cache_key(
                service,
                line["text"],
                voice_info,
                instruct,
                line.get("emotion"),
                payload["provider_id"],
                payload.get("model_version") or (actual_spec.spec.model_version if actual_spec else None),
            )
            pending_cache[actual_key] = {
                "asset_id": rel_path,
                "duration_ms": payload["duration_ms"],
                "provider_id": payload["provider_id"],
                "model_version": payload.get("model_version"),
                "variant_id": voice_info.get("variant_id"),
            }
            # 同步到章内的缓存表：同一章里重复的台词（相同参数）能立刻命中，不必等落盘
            cache_table.setdefault("entries", {})[actual_key] = pending_cache[actual_key]

            entry.update(
                {
                    "status": "ready",
                    "provider_id": payload["provider_id"],
                    "model_version": payload.get("model_version"),
                    "duration_ms": payload["duration_ms"],
                    "latency_ms": payload.get("latency_ms"),
                    "chunks": payload.get("chunks"),
                    "chunk_limit": payload.get("chunk_limit"),
                    "alignment": align_info,
                    "sidecar": f"{rel_path}.json",
                }
            )
            stats["succeeded"] += 1
            stats["audio_ms"] += int(payload["duration_ms"] or 0)
            batch_results.append(entry)

        results.extend(batch_results)
        if reporter is not None:
            reporter(offset // batch_size + 1, batch_results, stats)

    # 本章所有行处理完后统一落盘一次：run_chapters 逐章调用本函数，
    # 因此等价于「每章 flush 一次」，中断时已完成的章节缓存不会丢
    if pending_cache:
        cache.put_many(store.root, pending_cache)

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    report = {
        "batch_id": batch_id,
        "chapter_id": chapter.get("id"),
        "dry_run": dry_run,
        "batch_size": batch_size,
        "alignment": align_cfg,
        "started_at": _now(),
        "elapsed_ms": elapsed_ms,
        "peak_rss_mb": peak_rss_mb(),
        "stats": stats,
        "rows": results,
    }
    if write_report:
        report_dir = store.root / ".ndw" / "tts_batches"
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / f"{batch_id}.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        report["report_path"] = str(report_path)
    return report


def run_chapters(
    service,
    store,
    chapters: list[dict],
    batch_size: int = 5,
    dry_run: bool = False,
    reporter=None,
    scene_ids: list[str] | None = None,
    include_ready: bool = False,
    limit: int | None = None,
    line_ids: set[str] | None = None,
    skip_line_ids: set[str] | None = None,
) -> dict:
    """跨章节批量合成：逐章调用 `run_batch`，最后汇总成一份报告。

    参数:
        service: TtsService。
        store: ProjectStore。
        chapters: 章节 dict 列表（按顺序处理）。
        batch_size: 每批行数。
        dry_run: 只规划不合成。
        reporter: 进度回调，参数为 (章节序号, 章节, 批次结果, 累计统计)。
        scene_ids: 只处理指定场景。
        include_ready: 连已完成的行一起重跑。
        limit: 每章最多多少行。
        line_ids: 只处理这些行（范围选择器的结果）；为空表示按场景/整章。
        skip_line_ids: 不参与本次合成的行（说话人未识别且用户选择跳过）。

    返回:
        汇总报告（写入 `.ndw/tts_batches/<batch_id>.json`）。
    """
    batch_id = "btch_" + datetime.now(timezone.utc).strftime("%y%m%d%H%M%S")
    started = time.perf_counter()
    totals = {
        "chapters": len(chapters),
        "requested": 0,
        "succeeded": 0,
        "failed": 0,
        "skipped": 0,
        "cache_hits": 0,
        "audio_ms": 0,
    }
    per_chapter: list[dict] = []
    all_rows: list[dict] = []

    for index, chapter in enumerate(chapters, start=1):
        characters = store.load_characters()["characters"]
        snapshots = line_state.load_snapshots(store.root, chapter)
        engine = line_state.engine_params(service.config)
        rows = plan_batch(
            chapter,
            characters,
            scene_ids=scene_ids,
            include_ready=include_ready,
            limit=limit,
            snapshots=snapshots,
            root=store.root,
            engine=engine,
            line_ids=line_ids,
            skip_line_ids=skip_line_ids,
        )
        if not rows:
            per_chapter.append({"chapter_id": chapter.get("id"), "title": chapter.get("title"), "rows": 0, "stats": None})
            continue
        # 与 `tts run` 共用分组逻辑：同章内先按 worker 模式、再按参考音聚簇，减少重启与重复编码
        rows = sort_rows_by_group(rows, chapter, characters, service, store)

        def _cb(batch_no: int, batch_results: list[dict], stats: dict, _chapter=chapter, _index=index) -> None:
            """章节内批次进度回调。"""
            if reporter is not None:
                reporter(_index, _chapter, batch_no, batch_results, stats)

        sub = run_batch(
            service,
            store,
            chapter,
            rows,
            batch_size=batch_size,
            dry_run=dry_run,
            reporter=_cb,
            write_report=False,
            batch_id=batch_id,
        )
        sub_stats = sub["stats"]
        for key in ("requested", "succeeded", "failed", "skipped", "cache_hits", "audio_ms"):
            totals[key] += int(sub_stats.get(key) or 0)
        all_rows.extend([{**row, "chapter_id": chapter.get("id")} for row in sub["rows"]])
        per_chapter.append(
            {
                "chapter_id": chapter.get("id"),
                "title": chapter.get("title"),
                "rows": len(rows),
                "stats": sub_stats,
                "elapsed_ms": sub["elapsed_ms"],
            }
        )

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    report = {
        "batch_id": batch_id,
        "dry_run": dry_run,
        "batch_size": batch_size,
        "started_at": _now(),
        "elapsed_ms": elapsed_ms,
        "peak_rss_mb": peak_rss_mb(),
        "stats": totals,
        "chapters": per_chapter,
        "rows": all_rows,
    }
    report_dir = store.root / ".ndw" / "tts_batches"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"{batch_id}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report["report_path"] = str(report_path)
    return report
