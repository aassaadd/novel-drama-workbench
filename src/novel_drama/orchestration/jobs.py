"""幂等键与任务执行分发。

约定：
- 幂等键 = sha256(kind | target | 输入 revision | adapter | params_hash)；
- 执行前检查产物是否已存在（按 revision 命名），已存在则直接成功；
- 所有适配器异常统一转为任务失败信息，`retryable` 决定是否自动重试。
"""

from __future__ import annotations

import hashlib
import json

from ..adapters import Adapters
from ..adapters.base import AdapterError, ErrorCode
from ..models.ids import now_iso
from ..store import ProjectStore


def build_idempotency_key(kind: str, target: dict, revision: int | str, adapter: str | None, params: dict | None = None) -> str:
    """计算幂等键。

    参数:
        kind: 任务类型。
        target: 作用对象 ID 集合。
        revision: 输入实体版本。
        adapter: 适配器名称。
        params: 影响产物的参数（不得包含时间戳）。

    返回:
        `sha256:<hex>` 形式的幂等键。
    """
    payload = {
        "kind": kind,
        "target": {key: target[key] for key in sorted(target)},
        "revision": revision,
        "adapter": adapter or "",
        "params": params or {},
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def execute_job(job: dict, store: ProjectStore, adapters: Adapters) -> list[dict]:
    """执行一条任务。

    参数:
        job: 渲染任务 dict。
        store: 工程读写入口。
        adapters: 已装配的适配器。

    返回:
        产物列表 [{kind, path, meta}]。

    异常:
        AdapterError: 适配器未对接或执行失败。
        ValueError: 任务参数非法。
    """
    kind = job.get("kind")
    if kind == "tts":
        return _execute_tts_job(job, store, adapters)
    if kind in ("sfx", "image", "video", "writing", "mix", "export"):
        # 这些步骤依赖未对接的能力，统一抛出明确的未实现错误。
        target = job.get("target") or {}
        raise AdapterError(
            ErrorCode.NOT_IMPLEMENTED,
            f"任务 {kind} 尚未对接（目标 {target}）。先完成对应适配器实现，再用同一队列执行",
        )
    raise ValueError(f"未知任务类型: {kind}")


#: TtsError.code → 编排层语义（不可重试：环境/输入问题，重跑也白跑）
_TTS_UNAVAILABLE = {
    "MODEL_MISSING",
    "PROVIDER_DISABLED",
    "PROVIDER_UNREACHABLE",
    "PROVIDER_UNAUTHORIZED",
    "ROUTING_NO_CANDIDATE",
    "ROUTING_LOCKED_MISSING",
    "LICENSE_BLOCKED",
}
_TTS_INVALID = {
    "VOICE_MISSING",
    "UNSUPPORTED_VOICE",
    "UNSUPPORTED_EMOTION",
    "UNSUPPORTED_NONVERBAL",
    "UNSUPPORTED_LANGUAGE",
    "UNSUPPORTED_DURATION",
    "UNSUPPORTED_STREAMING",
}


def _tts_code_error(code: str, message: str, detail: dict) -> AdapterError:
    """把 `TtsError.code` 翻成适配层错误（保留原始 code 便于记账）。

    参数:
        code: `TtsError.code` 原值，如 `MODEL_MISSING`。
        message: 面向用户的说明。
        detail: 附带信息（行 ID、provider 等），只落日志/任务字段。

    返回:
        AdapterError；`code` 原值写进 detail["tts_code"]。
    """
    detail = {**detail, "tts_code": code}
    text = f"{code}: {message}"
    if code in _TTS_UNAVAILABLE:
        return AdapterError(ErrorCode.UNAVAILABLE, text, retryable=False, detail=detail)
    if code in _TTS_INVALID:
        return AdapterError(ErrorCode.INVALID_INPUT, text, retryable=False, detail=detail)
    if code == "PROVIDER_TIMEOUT":
        return AdapterError(ErrorCode.TIMEOUT, text, retryable=True, detail=detail)
    if code in ("PROVIDER_OOM", "BAD_OUTPUT"):
        return AdapterError(ErrorCode.UPSTREAM_ERROR, text, retryable=True, detail=detail)
    from ..tts.errors import RETRYABLE

    return AdapterError(ErrorCode.UPSTREAM_ERROR, text, retryable=code in RETRYABLE, detail=detail)


def _find_line_row(chapter: dict, line_id: str) -> dict | None:
    """在章节里定位一行，返回 `run_batch` 需要的 row 结构。

    参数:
        chapter: 章节 dict。
        line_id: 台词行 ID。

    返回:
        `{"scene_id", "scene_index", "line"}`；找不到返回 None。
    """
    for scene in chapter.get("scenes") or []:
        for line in scene.get("lines") or []:
            if line.get("id") == line_id:
                return {"scene_id": scene["id"], "scene_index": scene.get("index"), "line": line}
    return None


def _execute_tts_job(job: dict, store: ProjectStore, adapters: Adapters) -> list[dict]:
    """执行一条配音任务：一行一 job，复用 `run_batch` 的单行合成与落盘逻辑。

    行为约定：
    - 每次从磁盘重读章节，避免用入队时的旧快照覆盖别人的修改；
    - 单行失败只把错误记到该行与任务上（按 `TtsError.code`），不抛出中断整批；
    - 成功必须真的落盘 wav 才返回产物，否则按 UPSTREAM_ERROR 记账。

    参数:
        job: 任务 dict（target 需含 chapter_id / line_id）。
        store: 工程读写入口。
        adapters: 已装配的适配器（tts 需为真实 provider 适配器）。

    返回:
        产物列表，成功时为 `[{"kind": "audio", "path": 相对路径, "meta": {...}}]`。

    异常:
        AdapterError: 未对接、参数非法或该行合成失败。
    """
    from ..tts.batch import run_batch

    target = job.get("target") or {}
    chapter_id = target.get("chapter_id")
    line_id = target.get("line_id")
    if not chapter_id or not line_id:
        raise AdapterError(
            ErrorCode.INVALID_INPUT,
            f"配音任务缺少 chapter_id/line_id：{target}",
            detail={"target": target},
        )

    service = getattr(adapters.tts, "service", None)
    if service is None:
        raise AdapterError(
            ErrorCode.NOT_IMPLEMENTED,
            "TTS 适配器是 placeholder，无法执行配音任务；请在 config 的 adapters.tts 配成 auto 或具体 provider",
        )

    chapter = store.load_chapter(chapter_id)
    row = _find_line_row(chapter, line_id)
    if row is None:
        raise AdapterError(
            ErrorCode.INVALID_INPUT,
            f"台词行已不存在：{line_id}（章节 {chapter_id}）。重新执行 tts plan 入队",
            detail={"chapter_id": chapter_id, "line_id": line_id},
        )

    report = run_batch(service, store, chapter, [row], batch_size=1, batch_id=f"job_{job.get('id')}")
    entry = (report.get("rows") or [{}])[0]
    if entry.get("status") != "ready":
        error = entry.get("error") or {}
        raise _tts_code_error(
            str(error.get("code") or "UPSTREAM_ERROR"),
            str(error.get("message") or "合成失败（未返回原因）"),
            {"chapter_id": chapter_id, "line_id": line_id, "batch_id": report.get("batch_id")},
        )

    audio_rel = entry.get("audio_path")
    audio_path = store.root / audio_rel if audio_rel else None
    if not audio_path or not audio_path.exists() or audio_path.stat().st_size <= 0:
        raise AdapterError(
            ErrorCode.UPSTREAM_ERROR,
            f"合成报告为成功但音频未落盘：{audio_rel}",
            retryable=True,
            detail={"chapter_id": chapter_id, "line_id": line_id},
        )
    return [
        {
            "kind": "audio",
            "path": audio_rel,
            "meta": {
                "line_id": line_id,
                "scene_id": row["scene_id"],
                "duration_ms": entry.get("duration_ms"),
                "provider_id": entry.get("provider_id"),
                "model_version": entry.get("model_version"),
                "cache_hit": bool(entry.get("cache_hit")),
                "bytes": audio_path.stat().st_size,
                "batch_id": report.get("batch_id"),
            },
        }
    ]


def record_failure(job: dict, error: AdapterError) -> dict:
    """把失败信息写回任务并决定是否回到队列。

    参数:
        job: 任务 dict。
        error: 适配器错误。

    返回:
        修改后的任务 dict（未落盘）。
    """
    job["attempts"] = int(job.get("attempts") or 0) + 1
    job["error"] = {**error.to_dict(), "at": now_iso()}
    job["timings"]["finished_at"] = now_iso()
    can_retry = error.retryable and job["attempts"] < int(job.get("max_attempts") or 1)
    job["status"] = "queued" if can_retry else "failed"
    return job


def mark_success(job: dict, artifacts: list[dict]) -> dict:
    """把成功信息写回任务。

    参数:
        job: 任务 dict。
        artifacts: 产物列表。

    返回:
        修改后的任务 dict（未落盘）。
    """
    job["status"] = "succeeded"
    job["artifacts"] = artifacts
    job["timings"]["finished_at"] = now_iso()
    job["error"] = None
    return job
