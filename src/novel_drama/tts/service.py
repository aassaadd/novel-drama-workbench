"""TTS 服务层：把「一句台词」跑完整链路。

链路：需求向量 → 路由 → 逐块合成 → 落盘 → 记录实际 provider 与版本。
对比试听复用同一套 provider，只是对同一句依次换 provider。
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from . import sidecar
from .errors import BAD_OUTPUT, TtsError
from .fingerprint import line_fingerprint
from .registry import build_providers
from .routing import fallback_chain, resolve_lock, route
from .types import Requirement, SynthesisRequest, VoiceRef

# 切分上限：单块越短，同句多次生成的时长离散越小（实测见 docs/tts-stability.md）
DEFAULT_CHUNK_CHARS = 24
# 兼容旧引用
CHUNK_CHARS = DEFAULT_CHUNK_CHARS
# 块间静音：可听净间隔（头尾保留的气口会从插入量里扣掉）
DEFAULT_CHUNK_SILENCE_MS = 180
CHUNK_SILENCE_MS = DEFAULT_CHUNK_SILENCE_MS

# 参数/契约类错误：换 provider 也解决不了（甚至会用另一家静默顶替掉用户要的参数），
# 因此不进入回落链，直接上抛
NO_FALLBACK_CODES = {
    "UNSUPPORTED_EMOTION",
    "UNSUPPORTED_VOICE",
    "UNSUPPORTED_LANGUAGE",
    "UNSUPPORTED_NONVERBAL",
    "UNSUPPORTED_STREAMING",
    "UNSUPPORTED_DURATION",
    "VOICE_MISSING",
    "MODEL_MISSING",
    "PROVIDER_DISABLED",
    "LICENSE_BLOCKED",
}

# 句末标点：优先在此处断开
_SENTENCE_END = re.compile(r"(?<=[。！？；…!?;])")
# 次级停顿标点：句子仍超长时在此处断开
_CLAUSE_END = re.compile(r"(?<=[，、：,])")


def _now() -> str:
    """当前 UTC 时间。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _split_units(text: str, limit: int) -> list[str]:
    """把文本拆成不超过 limit 字的语义单元（句末 → 次级标点 → 硬切）。

    参数:
        text: 原始文本。
        limit: 单元最大字符数。

    返回:
        语义单元列表，保持原文顺序，不做跨单元合并。
    """
    units: list[str] = []
    for sentence in _SENTENCE_END.split(text):
        if not sentence:
            continue
        if len(sentence) <= limit:
            units.append(sentence)
            continue
        for clause in _CLAUSE_END.split(sentence):
            if not clause:
                continue
            if len(clause) <= limit:
                units.append(clause)
                continue
            # 仍超长（无标点的长串）才硬切
            for offset in range(0, len(clause), limit):
                units.append(clause[offset : offset + limit])
    return units


def split_text(text: str, limit: int = DEFAULT_CHUNK_CHARS) -> list[str]:
    """按标点把长句切成不超过 limit 字的块。

    参数:
        text: 原始文本。
        limit: 每块最大字符数（默认 24，可由 `tts.segmentation.max_chars` 配置）。

    返回:
        切分后的块列表；优先在句末断开，其次在次级标点断开，最后才硬切。
    """
    text = (text or "").strip()
    if not text:
        return []
    limit = max(4, int(limit))
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    buf = ""
    for unit in _split_units(text, limit):
        if len(buf) + len(unit) <= limit:
            buf += unit
            continue
        if buf:
            chunks.append(buf)
        buf = unit
    if buf:
        chunks.append(buf)
    return chunks or [text]


# 句末标点：块尾带这些才算「自然断句」，否则视为被字数硬切开
_SENTENCE_TAIL_CHARS = "。！？；!?;…"
# 紧跟句末标点的收尾符号，判断断句时先剥掉
_CLOSING_CHARS = "”’」』）)》"


def split_text_chunks(text: str, limit: int = DEFAULT_CHUNK_CHARS) -> list[tuple[str, bool]]:
    """切分文本并标注每块是否「硬切」。

    参数:
        text: 原始文本。
        limit: 每块最大字符数。

    返回:
        `[(块文本, 是否硬切)]`；硬切指块尾没有句末标点（被字数硬断开），
        接缝处需要打折处理，避免听感上出现长停顿。
    """
    chunks: list[tuple[str, bool]] = []
    for chunk in split_text(text, limit):
        tail = chunk.rstrip().rstrip(_CLOSING_CHARS).rstrip()
        chunks.append((chunk, not tail.endswith(tuple(_SENTENCE_TAIL_CHARS))))
    return chunks


def silence_edges(data, sample_rate: int, threshold_dbfs: float, max_ms: int) -> tuple[float, float]:
    """测量一块音频首尾「低于阈值」的静音时长。

    参数:
        data: 音频样本（一维或二维，二维按帧取各声道最大值）。
        sample_rate: 采样率。
        threshold_dbfs: 静音阈值（dBFS），低于它算静音。
        max_ms: 测量上限（毫秒），超过即截断，省去长尾扫描。

    返回:
        `(头部静音毫秒, 尾部静音毫秒)`。
    """
    import numpy as np

    if len(data) == 0:
        return 0.0, 0.0
    frames = np.abs(data)
    if frames.ndim > 1:
        frames = frames.max(axis=1)
    threshold = 10 ** (float(threshold_dbfs) / 20)
    loud = np.flatnonzero(frames > threshold)
    limit_frames = max(1, int(sample_rate * max_ms / 1000))
    if loud.size == 0:
        span = min(len(frames), limit_frames) / sample_rate * 1000
        return float(span), float(span)
    head = min(int(loud[0]), limit_frames) / sample_rate * 1000
    tail = min(len(frames) - 1 - int(loud[-1]), limit_frames) / sample_rate * 1000
    return float(head), float(tail)


def trim_edges(
    data,
    sample_rate: int,
    edge_trim_max_ms: int,
    silence_threshold_dbfs: float,
    keep_min_ms: int,
) -> dict:
    """裁掉一块音频头尾的多余静音（保留 `keep_min_ms` 防切掉气口）。

    裁切量按硬上限 `edge_trim_max_ms` 计算，目标值 `edge_trim_ms` 只在接缝补偿时作为基准，
    这样 200ms+ 的边缘静音能真正被裁到只剩气口。

    参数:
        data: 音频样本。
        sample_rate: 采样率。
        edge_trim_max_ms: 每端硬上限（防止吃掉弱起音）。
        silence_threshold_dbfs: 静音阈值。
        keep_min_ms: 每端保留量。

    返回:
        `{"audio", "head_ms", "tail_ms", "cut_head_ms", "cut_tail_ms"}`。
    """
    import numpy as np

    head_ms, tail_ms = silence_edges(data, sample_rate, silence_threshold_dbfs, edge_trim_max_ms)
    cut_head = max(0.0, min(head_ms, edge_trim_max_ms) - keep_min_ms)
    cut_tail = max(0.0, min(tail_ms, edge_trim_max_ms) - keep_min_ms)
    start = int(cut_head / 1000 * sample_rate)
    end = len(data) - int(cut_tail / 1000 * sample_rate)
    audio = data[start:end] if end > start else np.zeros(0, dtype=data.dtype)
    return {
        "audio": audio,
        "head_ms": round(head_ms, 1),
        "tail_ms": round(tail_ms, 1),
        "cut_head_ms": round(cut_head, 1),
        "cut_tail_ms": round(cut_tail, 1),
    }


def seam_insert_ms(chunk_silence_ms: int, kept_ms: float, hard_split: bool, hard_split_ratio: float) -> int:
    """计算接缝处需要插入的静音量。

    语义：`chunk_silence_ms` 是**可听净间隔**。决定接缝长度的是两侧实际保留的气口，
    因此插入量 = 目标值 − 已保留的气口；硬切处再按比例打折，避免听感上断句过度。

    参数:
        chunk_silence_ms: 目标净间隔（毫秒）。
        kept_ms: 该接缝两侧被保留下来的气口总量（前块尾 + 后块头）。
        hard_split: 该接缝是否为硬切（前块末尾无句末标点）。
        hard_split_ratio: 硬切折扣，默认 0.5。

    返回:
        需要插入的静音毫秒数（非负整数）。
    """
    value = max(0.0, float(chunk_silence_ms) - float(kept_ms))
    if hard_split:
        value *= float(hard_split_ratio)
    return max(0, int(round(value)))


class TtsService:
    """配音服务：路由 + 合成 + 记录。"""

    def __init__(self, config, project_root: str | Path | None = None) -> None:
        """初始化。

        参数:
            config: AppConfig。
            project_root: 工程根目录；用于记录同角色历史，可为空。
        """
        self.config = config
        self.providers = build_providers(config)
        self.provider_order = list(config.get("tts.provider_order") or [])
        if not self.provider_order:
            self.provider_order = [pid for pid, p in self.providers.items() if p.spec.enabled]
        self.routing_cfg = config.get("tts.routing") or {}
        # 主力 provider：自动路由时只要它满足条件就优先选它，避免被别家的高分抢走
        self.default_provider_id = config.get("tts.default_provider_id")
        default_voice = config.get("tts.default_voice") or {}
        self.default_voice = VoiceRef(
            ref_audio=default_voice.get("ref_audio"),
            ref_text=default_voice.get("ref_text"),
            source="preset",
        )
        segmentation = config.get("tts.segmentation") or {}
        self.chunk_limit = max(4, int(segmentation.get("max_chars") or DEFAULT_CHUNK_CHARS))
        self.chunk_silence_ms = max(0, int(segmentation.get("chunk_silence_ms") or DEFAULT_CHUNK_SILENCE_MS))
        # 接缝裁切参数：头尾各裁多少静音、静音阈值、硬切折扣（见 docs/tts-line-state-and-seams.md §7）
        self.edge_trim_ms = max(0, int(segmentation.get("edge_trim_ms", 100)))
        self.edge_trim_max_ms = max(self.edge_trim_ms, int(segmentation.get("edge_trim_max_ms", 400)))
        self.silence_threshold_dbfs = float(segmentation.get("silence_threshold_dbfs", -45))
        self.keep_min_ms = max(0, int(segmentation.get("keep_min_ms", 30)))
        self.hard_split_ratio = min(1.0, max(0.0, float(segmentation.get("hard_split_ratio", 0.5))))
        self.trim_single_chunk = bool(segmentation.get("trim_single_chunk", False))
        self.alignment_cfg = dict(config.get("tts.alignment") or {})
        self.project_root = Path(project_root) if project_root else None

    # ---------- 历史（同角色一致性） ----------

    def _history_path(self) -> Path | None:
        """同角色历史文件路径；无工程时为 None。"""
        if not self.project_root:
            return None
        return self.project_root / ".ndw" / "tts_history.json"

    def _history_provider(self, character_id: str | None) -> str | None:
        """读取该角色上次成功使用的 provider。"""
        path = self._history_path()
        if not character_id or not path or not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        return (data.get(character_id) or {}).get("provider_id")

    def _record_history(self, character_id: str | None, provider_id: str, variant_id: str | None) -> None:
        """记录该角色本次使用的 provider。"""
        path = self._history_path()
        if not character_id or not path:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        data[character_id] = {"provider_id": provider_id, "variant_id": variant_id, "updated_at": _now()}
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---------- 路由 ----------

    def decide(
        self,
        requirement: Requirement,
        line_provider_id: str | None = None,
        character_provider_id: str | None = None,
    ) -> dict:
        """解析锁定并做路由决策。

        参数:
            requirement: 需求向量。
            line_provider_id: 行级锁定。
            character_provider_id: 角色级锁定。
        """
        project_lock = self.routing_cfg.get("lock_provider_id")
        lock_id, lock_mode = resolve_lock(line_provider_id, character_provider_id, project_lock)
        history_provider = self._history_provider(requirement.character_id)
        return route(
            requirement,
            self.providers,
            self.provider_order,
            self.routing_cfg,
            lock_provider_id=lock_id,
            lock_mode=lock_mode,
            consistency_provider_id=history_provider,
            preferred_provider_id=self.default_provider_id,
        )

    # ---------- 合成 ----------

    def _synthesize_chunks(
        self,
        provider_id: str,
        text: str,
        voice: VoiceRef,
        emotion: str | None,
        instruct: str | None,
        out_path: Path,
        target_duration_ms: int | None,
        chunk_limit: int | None = None,
        chunk_silence_ms: int | None = None,
        emotion_intensity: int = 0,
        language: str | None = None,
    ) -> dict:
        """逐块合成并拼接为单个 wav。

        参数:
            provider_id: 已选 provider。
            text: 完整文本。
            voice: 音色参考。
            emotion: 情绪（目前由 instruct 承载）。
            instruct: 风格指令。
            out_path: 输出文件。
            target_duration_ms: 期望时长。
            chunk_limit: 单块最大字数；缺省用配置。
            chunk_silence_ms: 块间静音；缺省用配置。
            emotion_intensity: 情绪强度 0~3，透传给 provider 推导情绪权重。

        返回:
            含路径、采样率、时长、耗时、块数的字典。
        """
        provider = self.providers[provider_id]
        limit = self.chunk_limit if chunk_limit is None else max(4, int(chunk_limit))
        silence_ms = self.chunk_silence_ms if chunk_silence_ms is None else max(0, int(chunk_silence_ms))
        chunks = split_text_chunks(text, limit) or [(text, False)]
        import numpy as np
        import soundfile as sf

        started = time.perf_counter()
        trimmed: list[dict] = []
        trims: list[dict] = []
        sample_rate = None
        chunk_dir = out_path.parent / f"{out_path.stem}_chunks"
        chunk_dir.mkdir(parents=True, exist_ok=True)
        # 单块默认不裁（trim_single_chunk=false），保证与改造前逐样本一致
        may_trim = len(chunks) > 1 or self.trim_single_chunk
        for idx, (chunk, hard_split) in enumerate(chunks):
            req = SynthesisRequest(
                text=chunk,
                output_path=str(chunk_dir / f"{idx:03d}.wav"),
                voice=voice,
                emotion=emotion,
                instruct=instruct,
                emotion_intensity=emotion_intensity,
                language=language,
            )
            result = provider.synthesize(req)
            data, sr = sf.read(result.audio_path)
            sample_rate = sample_rate or sr
            if may_trim:
                info = trim_edges(
                    data,
                    sr,
                    self.edge_trim_max_ms,
                    self.silence_threshold_dbfs,
                    self.keep_min_ms,
                )
            else:
                info = {"audio": data, "head_ms": 0.0, "tail_ms": 0.0, "cut_head_ms": 0.0, "cut_tail_ms": 0.0}
            trimmed.append(info["audio"])
            trims.append(
                {
                    "index": idx,
                    "hard_split": hard_split,
                    "head_ms": info["head_ms"],
                    "tail_ms": info["tail_ms"],
                    "cut_head_ms": info["cut_head_ms"],
                    "cut_tail_ms": info["cut_tail_ms"],
                }
            )

        # 拼接：接缝静音按「目标净间隔」计算，裁掉多少补回多少，硬切处打折
        pieces = []
        seams: list[dict] = []
        for idx, piece in enumerate(trimmed):
            pieces.append(piece)
            if idx == len(trimmed) - 1:
                continue
            removed = trims[idx]["cut_tail_ms"] + trims[idx + 1]["cut_head_ms"]
            kept = (trims[idx]["tail_ms"] - trims[idx]["cut_tail_ms"]) + (
                trims[idx + 1]["head_ms"] - trims[idx + 1]["cut_head_ms"]
            )
            insert = seam_insert_ms(
                silence_ms,
                kept,
                trims[idx]["hard_split"],
                self.hard_split_ratio,
            )
            pieces.append(np.zeros(int(sample_rate * insert / 1000), dtype=piece.dtype))
            seams.append(
                {
                    "after_index": idx,
                    "insert_ms": insert,
                    "kept_ms": round(kept, 1),
                    "trimmed_ms": round(removed, 1),
                    "hard_split": trims[idx]["hard_split"],
                }
            )
        audio = np.concatenate(pieces) if len(pieces) > 1 else pieces[0]
        duration_ms = int(len(audio) / sample_rate * 1000)

        compensation = None
        if target_duration_ms:
            delta = target_duration_ms - duration_ms
            if delta > 0:
                pad = np.zeros(int(sample_rate * delta / 1000), dtype=audio.dtype)
                audio = np.concatenate([audio, pad])
                duration_ms = target_duration_ms
                compensation = {"kind": "silence_pad", "pad_ms": delta, "rate_delta": 0.0}
            elif delta < 0:
                ratio = duration_ms / target_duration_ms
                compensation = {
                    "kind": "tempo_needed",
                    "rate_delta": round(ratio - 1, 3),
                    "note": "超出目标时长，需 atempo 或接受超时；对口型段落禁止变速",
                }

        sf.write(str(out_path), audio, sample_rate)
        return {
            "audio_path": str(out_path),
            "sample_rate": sample_rate,
            "duration_ms": duration_ms,
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "chunks": len(chunks),
            "chunk_limit": limit,
            "chunk_silence_ms": silence_ms,
            "edge_trim_ms": self.edge_trim_ms,
            "edge_trim_max_ms": self.edge_trim_max_ms,
            "keep_min_ms": self.keep_min_ms,
            "silence_threshold_dbfs": self.silence_threshold_dbfs,
            "hard_split_ratio": self.hard_split_ratio,
            "trims": trims,
            "seams": seams,
            "compensation": compensation,
        }

    def synthesize(
        self,
        text: str,
        out_path: str | Path,
        requirement: Requirement | None = None,
        voice: VoiceRef | None = None,
        instruct: str | None = None,
        line_provider_id: str | None = None,
        character_provider_id: str | None = None,
        chunk_limit: int | None = None,
        params_extra: dict | None = None,
    ) -> dict:
        """合成一句并写出音频与溯源侧车文件。

        参数:
            text: 台词文本。
            out_path: 输出 wav 路径。
            requirement: 需求向量；缺省按文本与默认情绪构造。
            voice: 音色参考；缺省用配置里的默认参考。
            instruct: 风格指令。
            line_provider_id: 行级锁定。
            character_provider_id: 角色级锁定。
            chunk_limit: 单块最大字数；缺省用配置（用于 A/B 对比切分粒度）。
            params_extra: 行级参数补充（音色 id、交付说明、语速、裁切参数等），
                会并入侧车 `params` 并参与指纹；未提供的字段按 None 处理。
        """
        requirement = requirement or Requirement(text=text, emotion="neutral")
        voice = voice or self.default_voice
        if not voice.ref_audio:
            raise TtsError("VOICE_MISSING", "未配置默认参考音频（tts.default_voice.ref_audio）")

        decision = self.decide(requirement, line_provider_id, character_provider_id)
        selected = decision["selected"]["provider_id"]
        # 行级锁定是显式意图：锁定的 provider 失败就报错，不静默换人顶替
        if line_provider_id:
            order = [selected]
        else:
            order = [selected] + fallback_chain(selected, self.provider_order, self.providers)

        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        errors: list[dict] = []
        for pid in order:
            try:
                info = self._synthesize_chunks(
                    pid,
                    text,
                    voice,
                    requirement.emotion,
                    instruct,
                    out,
                    requirement.target_duration_ms,
                    chunk_limit=chunk_limit,
                    emotion_intensity=requirement.emotion_intensity,
                    language=requirement.language,
                )
            except TtsError as exc:
                errors.append(exc.to_dict())
                # 参数/契约类错误不换 provider 顶替：换一家能跑不代表用户要的参数被尊重
                if exc.code in NO_FALLBACK_CODES:
                    raise
                if pid != order[-1]:
                    decision["degradations"].append(
                        {
                            "kind": "provider_fallback",
                            "from": pid,
                            "to": order[order.index(pid) + 1],
                            "reason": exc.code,
                            "detail": exc.message,
                        }
                    )
                continue
            provider = self.providers[pid]
            # 侧车参数：影响音频产物的字段全集，行级值由 params_extra 覆盖
            params = {
                "text": text,
                "emotion": requirement.emotion,
                "emotion_intensity": requirement.emotion_intensity,
                "variant_id": voice.variant_id,
                "instruct": instruct,
                "provider_id": pid,
                "model_version": provider.spec.model_version,
                "chunk_limit": info.get("chunk_limit"),
                "chunk_silence_ms": info.get("chunk_silence_ms"),
                "edge_trim_ms": info.get("edge_trim_ms"),
                "edge_trim_max_ms": info.get("edge_trim_max_ms"),
                "keep_min_ms": info.get("keep_min_ms"),
                "silence_threshold_dbfs": info.get("silence_threshold_dbfs"),
                "hard_split_ratio": info.get("hard_split_ratio"),
            }
            params.update(params_extra or {})
            payload = {
                "payload_version": sidecar.SIDECAR_VERSION,
                "text": text,
                "provider_id": pid,
                "model": provider.spec.model,
                "model_version": provider.spec.model_version,
                "variant_id": voice.variant_id,
                "emotion": requirement.emotion,
                "routing": decision,
                "errors": errors,
                "created_at": _now(),
                "params": params,
                "fingerprint": line_fingerprint(params),
                "dimensions": {
                    "sample_rate": info.get("sample_rate"),
                    "duration_ms": info.get("duration_ms"),
                },
                **info,
            }
            sidecar.dump(out, payload)
            self._record_history(requirement.character_id, pid, voice.variant_id)
            return payload

        raise TtsError(
            BAD_OUTPUT,
            f"全部候选 provider 均失败：{[e['code'] for e in errors]}",
            hint="查看输出里的 errors 与各自的 hint",
        )

    # ---------- 对比试听 ----------

    def compare(
        self,
        text: str,
        provider_ids: list[str],
        out_dir: str | Path,
        requirement: Requirement | None = None,
        voice: VoiceRef | None = None,
    ) -> dict:
        """同一句依次用多个 provider 合成并留档。

        参数:
            text: 台词文本。
            provider_ids: 参与对比的 provider。
            out_dir: 对比产物目录。
            requirement: 需求向量。
            voice: 音色参考。
        """
        requirement = requirement or Requirement(text=text, emotion="neutral")
        voice = voice or self.default_voice
        folder = Path(out_dir)
        folder.mkdir(parents=True, exist_ok=True)
        batch_id = "cmpb_" + datetime.now(timezone.utc).strftime("%y%m%d%H%M%S")
        items: list[dict] = []
        for pid in provider_ids:
            provider = self.providers.get(pid)
            asset = folder / f"{pid}.wav"
            if provider is None:
                items.append({"provider_id": pid, "status": "failed", "code": "PROVIDER_DISABLED", "message": "未配置"})
                continue
            try:
                info = self._synthesize_chunks(
                    pid,
                    text,
                    voice,
                    requirement.emotion,
                    None,
                    asset,
                    requirement.target_duration_ms,
                )
                items.append(
                    {
                        "provider_id": pid,
                        "model_version": provider.spec.model_version,
                        "status": "ready",
                        "asset_id": str(asset),
                        "duration_ms": info["duration_ms"],
                        "latency_ms": info["latency_ms"],
                        "applied": {
                            "emotion": bool(requirement.emotion and requirement.emotion != "neutral"),
                            "speed": False,
                            "ssml": False,
                            "nonverbal": bool(requirement.paralinguistic),
                        },
                    }
                )
            except TtsError as exc:
                items.append(
                    {
                        "provider_id": pid,
                        "status": "failed",
                        "code": exc.code,
                        "message": exc.message,
                    }
                )
        record = {
            "batch_id": batch_id,
            "text": text,
            "emotion": requirement.emotion,
            "variant_id": voice.variant_id,
            "items": items,
            "winner_provider_id": None,
            "created_at": _now(),
        }
        record_path = folder / f"{batch_id}.json"
        record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        record["record_path"] = str(record_path)
        return record

    # ---------- 健康报告 ----------

    def doctor(self) -> list[dict]:
        """返回所有 provider 的可用性与原因。"""
        rows = []
        for pid, provider in self.providers.items():
            status = provider.health()
            rows.append(
                {
                    "provider_id": pid,
                    "model": provider.spec.model,
                    "model_version": provider.spec.model_version,
                    "backend": provider.spec.backend,
                    "enabled": provider.spec.enabled,
                    "available": status.ok,
                    "code": status.code,
                    "detail": status.detail,
                    "weights_path": provider.spec.weights_path,
                    "vram_estimate_gb": provider.spec.vram_estimate_gb,
                    "license_gate": provider.spec.capabilities.get("license_gate"),
                    "commercial_use": provider.spec.capabilities.get("commercial_use"),
                    "auto_select": provider.spec.capabilities.get("allow_auto_select"),
                }
            )
        return rows
