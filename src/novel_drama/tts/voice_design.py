"""角色设定 → 音色描述 → VoxCPM2 抽卡 → 入库绑定。

设计文档：`docs/spec-r17-voice-design.md`。

要点：
- 描述优先走写作适配器的工具调用（结构化 JSON），失败回退规则模板；
- 生成走独立 venv 的一次性子进程（`voxcpm2_design_worker.py`），NDJSON 汇报进度；
- 候选落在 `assets/voice/candidates/<batch_id>/`，默认保留 7 天；
- 选中后复制为 `assets/voice/ref/<variant_id>.wav`，记录描述、引擎、种子、sha256。
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import shutil
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..adapters.base import Message, TextGenRequest
from ..config import AppConfig
from ..models import entities

WORKER_PATH = Path(__file__).resolve().parent / "providers" / "voxcpm2_design_worker.py"

_THREADS: dict[str, threading.Thread] = {}
_THREADS_LOCK = threading.Lock()

#: 运行中的批次：batch_id -> {"cancel": Event, "proc": Popen|None}
_RUNNING: dict[str, dict] = {}
_RUNNING_LOCK = threading.Lock()

#: SSE 订阅者：batch_id -> 队列集合；进程内广播，页面断线只退订不取消任务
_SUBSCRIBERS: dict[str, set[queue.Queue]] = {}
_SUB_LOCK = threading.Lock()

#: 本批脚本的字数（用于「已接收 X 字」进度：每个候选完成即累计一次）
_SCRIPT_CHARS_KEY = "script_chars"


_PROFILE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["voice_summary", "gender", "age_range", "pitch", "timbre", "pace", "temperament", "accent", "design_prompt"],
    "properties": {
        "voice_summary": {"type": "string", "maxLength": 200},
        "gender": {"type": "string", "enum": ["male", "female", "other", "unknown"]},
        "age_range": {"type": "string", "maxLength": 20},
        "pitch": {"type": "string", "enum": ["low", "mid-low", "mid", "mid-high", "high"]},
        "timbre": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 4},
        "pace": {"type": "string", "enum": ["slow", "medium", "fast"]},
        "temperament": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 4},
        "accent": {"type": "string", "maxLength": 40},
        "design_prompt": {"type": "string", "maxLength": 600},
        "negative_prompt": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "evidence": {"type": "array", "items": {"type": "string"}, "maxItems": 6},
    },
}

_PROFILE_TOOL = {
    "type": "function",
    "function": {
        "name": "emit_voice_profile",
        "description": "根据人物设定产出一个可直接用于文字造音色的音色描述",
        "parameters": _PROFILE_SCHEMA,
    },
}

_SYSTEM_PROMPT = """你是拟音导演。根据人物设定，设计一条能用于 TTS 文字造音色的音色描述。

规则：
1. 只描述「能发出声音的生理与语言习惯」：性别、年龄感、音区、音色质感、语速、气质、口音。
2. 不写外貌形容词，不写剧情，不写真实人名、明星或主播。
3. design_prompt 用英文写，一句话，包含性别、年龄、音区、质感、语速、气质、口音；不要出现负面词。
4. 性别必须服从人物设定的 gender；设定为 unknown 时按背景合理推断为 male 或 female，不要返回 other。
5. timbre 用 2~4 个中文质感词（如：低沉、略带沙哑、清亮）。"""


def _now() -> str:
    """当前 UTC 时间（ISO 8601）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_time(value: str | None) -> datetime | None:
    """解析 ISO 时间；失败返回 None。"""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _write_json(path: Path, payload: dict) -> None:
    """原子写 JSON（先写临时名再替换），避免轮询读到半截内容。"""
    tmp = path.with_suffix(path.suffix + f".tmp-{os.getpid()}")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> dict:
    """读 JSON；文件不存在或损坏时返回空 dict。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _subscribe(batch_id: str) -> queue.Queue:
    """订阅批次事件，返回只属于该连接的队列。

    参数:
        batch_id: 批次 ID。

    返回:
        queue.Queue；调用方结束时必须 `_unsubscribe`。
    """
    channel: queue.Queue = queue.Queue()
    with _SUB_LOCK:
        _SUBSCRIBERS.setdefault(batch_id, set()).add(channel)
    return channel


def _unsubscribe(batch_id: str, channel: queue.Queue) -> None:
    """退订批次事件；最后一个订阅者离开时清理空集合。"""
    with _SUB_LOCK:
        items = _SUBSCRIBERS.get(batch_id)
        if not items:
            return
        items.discard(channel)
        if not items:
            _SUBSCRIBERS.pop(batch_id, None)


def _publish(batch_id: str, event: dict) -> None:
    """向该批次的所有订阅者广播一条事件（无订阅者时直接丢弃）。"""
    with _SUB_LOCK:
        items = list(_SUBSCRIBERS.get(batch_id) or ())
    for channel in items:
        channel.put(event)


def _character_brief(character: dict) -> str:
    """把人物档案压成给 LLM 的简报（只取与音色相关的字段）。"""
    style = character.get("speech_style") or {}
    parts = [
        f"姓名：{character.get('name') or '未命名'}",
        f"角色定位：{character.get('role') or 'unknown'}",
        f"性别：{character.get('gender') or 'unknown'}",
        f"年龄：{character.get('age_range') or '未标注'}",
        f"性格：{'、'.join(character.get('personality') or []) or '未标注'}",
        f"背景：{(character.get('background') or '')[:600] or '未标注'}",
        f"语速：{style.get('tempo') or '未标注'}",
        f"音区：{style.get('pitch') or '未标注'}",
        f"口音：{style.get('accent') or '未标注'}",
    ]
    return "\n".join(parts)


def _rule_profile(character: dict) -> dict:
    """LLM 不可用时的规则模板（保证功能可用）。"""
    gender = character.get("gender") if character.get("gender") in {"male", "female", "other"} else "unknown"
    style = character.get("speech_style") or {}
    pitch_raw = (style.get("pitch") or "").strip()
    pitch = "low" if any(word in pitch_raw for word in ("低", "沉")) else "high" if any(word in pitch_raw for word in ("高", "尖")) else "mid"
    tempo_raw = (style.get("tempo") or "").strip()
    pace = "slow" if any(word in tempo_raw for word in ("慢", "缓")) else "fast" if any(word in tempo_raw for word in ("快", "急")) else "medium"
    gender_en = {"male": "male", "female": "female"}.get(gender, "neutral")
    age = character.get("age_range") or "30s"
    accent = (style.get("accent") or "").strip() or "standard Mandarin"
    prompt = f"A {pitch}-pitched {gender_en} voice in {age}, clear natural timbre, {pace} pace, {accent}, restrained and believable."
    return {
        "voice_summary": f"{age}{'男' if gender == 'male' else '女' if gender == 'female' else ''}声，{pitch_raw or '中音区'}，{tempo_raw or '中速'}",
        "gender": gender,
        "age_range": age,
        "pitch": pitch,
        "timbre": ["自然", "清晰"],
        "pace": pace,
        "temperament": list(character.get("personality") or ["沉稳"])[:2] or ["沉稳"],
        "accent": accent,
        "design_prompt": prompt,
        "negative_prompt": ["童声", "播音腔"],
        "confidence": 0.4,
        "evidence": ["规则模板：LLM 不可用，未做语义推断"],
    }


def build_profile(adapter, character: dict) -> dict:
    """用 LLM 生成音色描述；失败回退规则模板。

    参数:
        adapter: 写作适配器（需实现 generate_tool）。
        character: 人物档案 dict。

    返回:
        `{"profile": ..., "source": "llm"|"rule", "note": 可选提示}`。
    """
    req = TextGenRequest(
        task="freeform",
        system=_SYSTEM_PROMPT,
        messages=[Message(role="user", content=_character_brief(character))],
        max_tokens=900,
        temperature=0.6,
        stream=False,
        metadata={"purpose": "voice_design_profile"},
    )
    try:
        raw = adapter.generate_tool(req, _PROFILE_TOOL, timeout_s=90)
        profile, gender_note = _normalize_profile(raw, character)
        return {"profile": profile, "source": "llm", "note": gender_note}
    except Exception as exc:  # noqa: BLE001 - 任何失败都回退模板，不阻断功能
        return {
            "profile": _rule_profile(character),
            "source": "rule",
            "note": f"LLM 描述生成失败，已用规则模板：{exc}",
        }


def _resolve_gender(raw_gender: str | None, character: dict) -> tuple[str, str | None]:
    """确定音色标签性别：优先角色档案，绝不把未知/不一致兜底成「其他」。

    参数:
        raw_gender: 模型返回或人工选择的性别。
        character: 角色档案。

    返回:
        `(gender, note)`；note 非空时由调用方提示用户。
    """
    archive = character.get("gender") if character.get("gender") in {"male", "female", "other"} else None
    raw = raw_gender if raw_gender in {"male", "female", "other", "unknown"} else None
    if archive in {"male", "female"}:
        note = None
        if raw not in (None, archive, "unknown"):
            note = "模型推断性别与角色档案不一致，已沿用档案性别"
        return archive, note
    if archive == "other":
        note = None
        if raw not in (None, "other", "unknown"):
            note = "模型推断性别与角色档案不一致，已沿用档案性别「其他」"
        return "other", note
    # 档案未标注或无法识别：只接受明确的男/女，其余保留为未知
    if raw in {"male", "female"}:
        return raw, None
    if raw == "other":
        return "unknown", "模型返回「其他」，已保留为未知，请人工确认"
    return "unknown", None


def _normalize_profile(raw: dict, character: dict) -> tuple[dict, str | None]:
    """裁剪并补全 LLM 输出，保证字段完整、长度可控。

    返回:
        `(profile, note)`；性别只取角色档案或明确枚举，映射不到时保留未知并给出提示。
    """
    gender, note = _resolve_gender(raw.get("gender"), character)
    profile = {
        "voice_summary": str(raw.get("voice_summary") or "")[:200],
        "gender": gender,
        "age_range": str(raw.get("age_range") or character.get("age_range") or "")[:20],
        "pitch": raw.get("pitch") if raw.get("pitch") in {"low", "mid-low", "mid", "mid-high", "high"} else "mid",
        "timbre": [str(item)[:20] for item in (raw.get("timbre") or [])][:4] or ["自然"],
        "pace": raw.get("pace") if raw.get("pace") in {"slow", "medium", "fast"} else "medium",
        "temperament": [str(item)[:20] for item in (raw.get("temperament") or [])][:4] or ["沉稳"],
        "accent": str(raw.get("accent") or "")[:40],
        "design_prompt": str(raw.get("design_prompt") or "").strip()[:600],
        "negative_prompt": [str(item)[:20] for item in (raw.get("negative_prompt") or [])][:5],
        "confidence": max(0.0, min(1.0, float(raw.get("confidence") or 0.6))),
        "evidence": [str(item)[:80] for item in (raw.get("evidence") or [])][:6],
    }
    if not profile["design_prompt"]:
        raise ValueError("模型未返回 design_prompt")
    return profile, note


def batch_dir(store, batch_id: str) -> Path:
    """候选批次目录（相对工程，可整体迁移）。"""
    return store.voice_asset_dir("candidates") / batch_id


def cleanup_expired(store, days: int) -> int:
    """删除超过保留期的候选批次；返回删除数量。"""
    root = store.voice_asset_dir("candidates")
    if not root.exists():
        return 0
    deadline = datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))
    removed = 0
    for item in root.iterdir():
        if not item.is_dir():
            continue
        meta = _read_json(item / "batch.json")
        created = _parse_time(meta.get("created_at"))
        if created is None:
            created = datetime.fromtimestamp(item.stat().st_mtime, tz=timezone.utc)
        if created < deadline:
            shutil.rmtree(item, ignore_errors=True)
            removed += 1
    return removed


def create_batch(store, cfg: AppConfig, character: dict, prompt: str, count: int, profile: dict | None = None) -> dict:
    """创建抽卡批次并在后台线程启动 worker。

    参数:
        store: 工程。
        cfg: 本机配置（读取 voice.design.*）。
        character: 人物档案。
        prompt: 已确认（可编辑）的英文音色描述。
        count: 候选条数。
        profile: 结构化描述；为空时只记录 prompt。

    返回:
        批次元数据（含 batch_id）。
    """
    design = cfg.get("voice.design") or {}
    cleanup_expired(store, int(design.get("max_keep_days", 7)))
    count = max(1, min(6, int(count)))
    batch_id = entities.new_id("vdb")
    target = batch_dir(store, batch_id)
    target.mkdir(parents=True, exist_ok=True)
    meta = {
        "batch_id": batch_id,
        "status": "running",
        "stage": "queued",
        "done": 0,
        "total": count,
        "current_index": 0,
        "character_id": character.get("id"),
        "character_name": character.get("name"),
        "profile": profile,
        "prompt": prompt,
        "engine": "voxcpm2",
        "model_id": "openbmb/VoxCPM2",
        "params": {
            "cfg_value": float(design.get("cfg_value", 2.0)),
            "inference_timesteps": int(design.get("inference_timesteps", 10)),
        },
        "candidates": [],
        "failures": [],
        "error": None,
        "note": None,
        "created_at": _now(),
    }
    _write_json(target / "batch.json", meta)
    with _RUNNING_LOCK:
        _RUNNING[batch_id] = {"cancel": threading.Event(), "proc": None}
    thread = threading.Thread(target=_run_batch, args=(store, dict(design), target, batch_id, prompt, count), daemon=True)
    with _THREADS_LOCK:
        _THREADS[batch_id] = thread
    thread.start()
    return meta


def _design_script(design: dict) -> str:
    """取本批试听脚本文本（空时用默认句）。"""
    return str(design.get("script") or "你好，这是一段用于试听的样音。").strip()


def _upsert_candidate(meta: dict, event: dict) -> dict:
    """把一条候选写入元数据，按序号去重并清掉同序号失败记录。"""
    index = int(event.get("index") or 0)
    item = {
        "index": index,
        "file": str(event.get("file") or f"cand-{index:02d}.wav"),
        "seed": event.get("seed"),
        "duration_ms": int(event.get("duration_ms") or 0),
    }
    candidates = [row for row in meta.get("candidates") or [] if int(row.get("index") or 0) != index]
    candidates.append(item)
    meta["candidates"] = sorted(candidates, key=lambda row: int(row.get("index") or 0))
    meta["failures"] = [row for row in meta.get("failures") or [] if int(row.get("index") or 0) != index]
    meta["done"] = len(meta["candidates"])
    return item


def _mark_failure(meta: dict, index: int, message: str) -> dict:
    """记录一条候选失败（同序号覆盖），不影响其它候选。"""
    failures = [row for row in meta.get("failures") or [] if int(row.get("index") or 0) != int(index)]
    item = {"index": int(index), "message": str(message or "生成失败")}
    failures.append(item)
    meta["failures"] = sorted(failures, key=lambda row: int(row.get("index") or 0))
    return item


def _fill_missing_failures(meta: dict, reason: str) -> None:
    """把既没有候选也没有失败记录的序号补成失败（worker 异常退出时使用）。"""
    total = int(meta.get("total") or 0)
    done = {int(row.get("index") or 0) for row in meta.get("candidates") or []}
    failed = {int(row.get("index") or 0) for row in meta.get("failures") or []}
    failures = list(meta.get("failures") or [])
    for index in range(1, total + 1):
        if index in done or index in failed:
            continue
        failures.append({"index": index, "message": reason})
    meta["failures"] = sorted(failures, key=lambda row: int(row.get("index") or 0))


def _finish_meta(meta: dict, target: Path, missing_reason: str | None = None) -> dict:
    """按候选与失败清单收敛批次状态，并给新候选补基频提示。

    参数:
        meta: 批次元数据（就地修改）。
        target: 批次目录。
        missing_reason: 非空时把缺失序号补成失败（worker 异常退出）。

    返回:
        更新后的元数据。
    """
    if missing_reason:
        _fill_missing_failures(meta, missing_reason)
    total = int(meta.get("total") or 0)
    candidates = sorted(meta.get("candidates") or [], key=lambda row: int(row.get("index") or 0))
    done = {int(row.get("index") or 0) for row in candidates}
    failures = sorted(
        [row for row in meta.get("failures") or [] if int(row.get("index") or 0) not in done],
        key=lambda row: int(row.get("index") or 0),
    )
    gender_hint = str((meta.get("profile") or {}).get("gender") or "unknown")
    for item in candidates:
        if not item.get("hint"):
            item["hint"] = _f0_hint(target / str(item.get("file") or ""), gender_hint)
    meta["candidates"] = candidates
    meta["failures"] = failures
    meta["done"] = len(candidates)
    meta["stage"] = "done"
    if len(candidates) >= total and not failures:
        meta["status"] = "ready"
        meta["error"] = None
        meta["note"] = None
        meta["generated_at"] = _now()
    elif candidates:
        meta["status"] = "partial"
        meta["error"] = None
        meta["note"] = f"{len(failures)} 条候选生成失败，可单条重试"
        meta["generated_at"] = _now()
    else:
        meta["status"] = "failed"
        meta["error"] = failures[0]["message"] if failures else "全部候选生成失败"
        meta["note"] = None
    return meta


def _probe_ms(path: Path) -> int:
    """探测音频时长（毫秒）；失败返回 0。"""
    try:
        from ..media.ffmpeg import probe_duration_ms

        return int(probe_duration_ms(path, "ffprobe") or 0)
    except Exception:  # noqa: BLE001 - 时长只用于展示，失败不影响主流程
        return 0


def _reconcile_candidates(meta: dict, target: Path) -> None:
    """中断收尾时按磁盘上的 cand-*.wav 补齐候选记录，避免丢掉刚写完的文件。"""
    known = {int(row.get("index") or 0) for row in meta.get("candidates") or []}
    for path in sorted(target.glob("cand-*.wav")):
        try:
            index = int(path.stem.split("-")[1])
        except (IndexError, ValueError):
            continue
        if index in known:
            continue
        meta.setdefault("candidates", []).append(
            {"index": index, "file": path.name, "seed": 1000 + index, "duration_ms": _probe_ms(path)}
        )
    meta["candidates"] = sorted(meta.get("candidates") or [], key=lambda row: int(row.get("index") or 0))


def _finish_stopped(meta: dict, target: Path) -> dict:
    """中断收尾：保留已完成候选，标记 stopped，不删除已落盘音频。"""
    _reconcile_candidates(meta, target)
    total = int(meta.get("total") or 0)
    if len(meta.get("candidates") or []) >= total:
        # 中断时整批其实已经生成完：按正常完成处理，避免误报「已停止」
        return _finish_meta(meta, target)
    meta["status"] = "stopped"
    meta["stage"] = "stopped"
    meta["error"] = None
    meta["done"] = len(meta.get("candidates") or [])
    meta["note"] = "已停止生成，后续候选未生成"
    return meta


def _run_batch(
    store, design: dict, target: Path, batch_id: str, prompt: str, count: int, indices: list[int] | None = None
) -> None:
    """执行一次抽卡：写临时输入 → 起 worker → 逐条广播进度 → 收尾。

    参数:
        store: 工程。
        design: `voice.design` 配置。
        target: 批次目录。
        batch_id: 批次 ID。
        prompt: 音色描述。
        count: 计划候选总数（重试/续跑时取批次原总数）。
        indices: 只生成这些序号（单条重试或中断续跑）；为空时生成整批。
    """
    meta_path = target / "batch.json"
    meta = _read_json(meta_path)
    script = _design_script(design)
    script_chars = len(script)
    prompt_file = target / "prompt.txt"
    script_file = target / "script.txt"
    log_path = target / "worker.log"
    prompt_file.write_text(prompt, encoding="utf-8")
    script_file.write_text(script, encoding="utf-8")
    meta[_SCRIPT_CHARS_KEY] = script_chars
    with _RUNNING_LOCK:
        state = _RUNNING.setdefault(batch_id, {"cancel": threading.Event(), "proc": None})
    cancel = state["cancel"]
    cmd = [
        str(design.get("python") or ""),
        str(WORKER_PATH),
        "--model-dir",
        str(design.get("model_dir") or ""),
        "--out-dir",
        str(target),
        "--prompt-file",
        str(prompt_file),
        "--script-file",
        str(script_file),
        "--count",
        str(count),
        "--cfg",
        str(float(design.get("cfg_value", 2.0))),
        "--timesteps",
        str(int(design.get("inference_timesteps", 10))),
        "--device",
        str(design.get("device") or "auto"),
    ]
    if indices:
        cmd.extend(["--indices", ",".join(str(int(item)) for item in indices)])
    timeout_s = float(design.get("timeout_s", 900))
    killed = {"value": False}
    proc = None
    try:
        with log_path.open("w", encoding="utf-8") as log:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=log, text=True, bufsize=1, start_new_session=True)
            with _RUNNING_LOCK:
                state["proc"] = proc
            if cancel.is_set():
                _kill(proc)

            def watchdog() -> None:
                """超时后终止整个 worker 进程组。"""
                try:
                    proc.wait(timeout=timeout_s)
                except subprocess.TimeoutExpired:
                    killed["value"] = True
                    _kill(proc)

            threading.Thread(target=watchdog, daemon=True).start()
            for line in proc.stdout or []:
                if cancel.is_set():
                    _kill(proc)
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                stage = event.get("stage")
                if stage in ("loading", "generating"):
                    meta["stage"] = stage
                    if stage == "generating":
                        meta["current_index"] = int(event.get("index") or 0)
                        meta["done"] = len(meta.get("candidates") or [])
                    _write_json(meta_path, meta)
                    if stage == "loading":
                        _publish(batch_id, {"type": "stage", "stage": "loading"})
                    else:
                        _publish(
                            batch_id,
                            {
                                "type": "batch",
                                "index": int(event.get("index") or 0),
                                "total": int(meta.get("total") or count),
                                "unit": "candidate",
                            },
                        )
                elif stage == "candidate":
                    item = _upsert_candidate(meta, event)
                    _write_json(meta_path, meta)
                    _publish(batch_id, {"type": "candidate", **item, "done": meta["done"]})
                    _publish(
                        batch_id,
                        {
                            "type": "chars",
                            "chars": script_chars * meta["done"],
                            "index": item["index"],
                            "total": int(meta.get("total") or count),
                            "unit": "candidate",
                        },
                    )
                elif stage == "candidate_error":
                    item = _mark_failure(meta, int(event.get("index") or 0), str(event.get("message") or "生成失败"))
                    _write_json(meta_path, meta)
                    _publish(batch_id, {"type": "candidate_error", **item})
                elif stage == "error":
                    meta["status"] = "failed"
                    meta["error"] = str(event.get("message") or "生成失败")
                    meta["stage"] = "done"
                    _write_json(meta_path, meta)
                    _publish(batch_id, {"type": "failed", "message": meta["error"]})
            code = proc.wait(timeout=30)
        if cancel.is_set():
            meta = _finish_stopped(meta, target)
            _write_json(meta_path, meta)
        elif meta.get("status") == "failed":
            # 模型加载失败等全局错误：没有可用候选，清掉半成品
            _cleanup_partial(target)
            _write_json(meta_path, meta)
        elif killed["value"]:
            meta = _finish_meta(meta, target, f"生成超时（>{int(timeout_s)} 秒）")
            _write_json(meta_path, meta)
            if not meta.get("candidates"):
                _cleanup_partial(target)
        elif code != 0:
            # worker 非正常退出：保留已完成候选，缺失序号标为失败，可单条重试
            meta = _finish_meta(meta, target, f"worker 退出码 {code}，详见 worker.log")
            _write_json(meta_path, meta)
        else:
            meta = _finish_meta(meta, target)
            _write_json(meta_path, meta)
    except Exception as exc:  # noqa: BLE001 - 任何失败都收敛为批次状态，不抛到线程外
        if proc is not None and proc.poll() is None:
            _kill(proc)
        if cancel.is_set():
            meta = _finish_stopped(meta, target)
        else:
            meta["status"] = "failed"
            meta["error"] = str(exc)
            meta["stage"] = "done"
        _write_json(meta_path, meta)
        if not cancel.is_set() and not meta.get("candidates"):
            _cleanup_partial(target)
    finally:
        _cleanup_temp(target)
        with _RUNNING_LOCK:
            _RUNNING.pop(batch_id, None)
        with _THREADS_LOCK:
            _THREADS.pop(batch_id, None)
        _publish(batch_id, {"type": "finished", "status": meta.get("status"), "meta": meta})


def _kill(proc: subprocess.Popen) -> None:
    """终止 worker 进程组（含其子进程）。"""
    try:
        os.killpg(os.getpgid(proc.pid), 15)
        proc.wait(timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        try:
            os.killpg(os.getpgid(proc.pid), 9)
        except OSError:
            pass


def _cleanup_temp(target: Path) -> None:
    """清理临时输入与半截音频，不动已落盘的 cand-*.wav。"""
    for pattern in (".cand-*.tmp*", "prompt.txt", "script.txt"):
        for item in target.glob(pattern):
            item.unlink(missing_ok=True)


def _cleanup_partial(target: Path) -> None:
    """批次整体失败时删除半成品候选与临时文件，只保留 batch.json 与日志。"""
    for pattern in ("cand-*.wav", ".cand-*.tmp*", "prompt.txt", "script.txt"):
        for item in target.glob(pattern):
            item.unlink(missing_ok=True)


def is_running(batch_id: str) -> bool:
    """批次当前是否有后台线程在生成（用于识别服务重启后的残留 running）。"""
    with _RUNNING_LOCK:
        return batch_id in _RUNNING


def cancel_batch(store, batch_id: str) -> dict:
    """停止批次后续候选：终止 worker，保留已完成候选并标记 stopped。

    参数:
        store: 工程。
        batch_id: 批次 ID。

    返回:
        收尾后的批次元数据（不存在时为空 dict）。
    """
    with _RUNNING_LOCK:
        state = _RUNNING.get(batch_id)
        if state:
            state["cancel"].set()
        proc = (state or {}).get("proc")
    if proc is not None and proc.poll() is None:
        _kill(proc)
    with _THREADS_LOCK:
        thread = _THREADS.get(batch_id)
    if thread is not None and thread.is_alive():
        thread.join(timeout=30)
    meta = read_batch(store, batch_id)
    if meta.get("status") == "running":
        target = batch_dir(store, batch_id)
        meta = _finish_stopped(meta, target)
        _write_json(target / "batch.json", meta)
        _publish(batch_id, {"type": "finished", "status": meta.get("status"), "meta": meta})
    return meta


def retry_candidate(store, cfg: AppConfig, batch_id: str, index: int) -> dict:
    """单条重试：只重新生成指定序号的候选。

    参数:
        store: 工程。
        cfg: 本机配置（读取 voice.design.*）。
        batch_id: 批次 ID。
        index: 候选序号（从 1 开始）。

    返回:
        更新后的批次元数据。

    异常:
        ValueError: 批次不存在、仍在生成中或该候选不需要重试。
    """
    meta = read_batch(store, batch_id)
    if not meta:
        raise ValueError("批次不存在")
    if meta.get("status") == "running":
        raise ValueError("批次仍在生成中，请稍后再试")
    index = int(index)
    total = int(meta.get("total") or 0)
    if index < 1 or index > total:
        raise ValueError("候选序号超出范围")
    target = batch_dir(store, batch_id)
    candidates = meta.get("candidates") or []
    candidate = next((row for row in candidates if int(row.get("index") or 0) == index), None)
    file_missing = candidate is not None and not (target / str(candidate.get("file") or "")).is_file()
    needs_retry = file_missing or any(int(row.get("index") or 0) == index for row in meta.get("failures") or [])
    if not needs_retry:
        raise ValueError("该候选不需要重试")
    meta["candidates"] = [row for row in candidates if int(row.get("index") or 0) != index]
    meta["failures"] = [row for row in meta.get("failures") or [] if int(row.get("index") or 0) != index]
    meta["status"] = "running"
    meta["stage"] = "queued"
    meta["current_index"] = index
    meta["done"] = len(meta["candidates"])
    meta["error"] = None
    meta["note"] = None
    _write_json(target / "batch.json", meta)
    design = dict(cfg.get("voice.design") or {})
    with _RUNNING_LOCK:
        _RUNNING[batch_id] = {"cancel": threading.Event(), "proc": None}
    thread = threading.Thread(
        target=_run_batch,
        args=(store, design, target, batch_id, str(meta.get("prompt") or ""), total),
        kwargs={"indices": [index]},
        daemon=True,
    )
    with _THREADS_LOCK:
        _THREADS[batch_id] = thread
    thread.start()
    _publish(batch_id, {"type": "status", "status": "running", "index": index})
    return meta


def resume_batch(store, cfg: AppConfig, batch_id: str) -> dict:
    """中断/部分失败后续跑：只补生成缺失的候选序号，已完成的不重跑。

    参数:
        store: 工程。
        cfg: 本机配置（读取 voice.design.*）。
        batch_id: 批次 ID。

    返回:
        更新后的批次元数据。

    异常:
        ValueError: 批次不存在、仍在生成中或已生成完整。
    """
    meta = read_batch(store, batch_id)
    if not meta:
        raise ValueError("批次不存在")
    if meta.get("status") == "running":
        raise ValueError("批次仍在生成中，请稍后再试")
    total = int(meta.get("total") or 0)
    done = {int(row.get("index") or 0) for row in meta.get("candidates") or []}
    missing = [index for index in range(1, total + 1) if index not in done]
    if not missing:
        raise ValueError("批次已生成完整")
    target = batch_dir(store, batch_id)
    meta["status"] = "running"
    meta["stage"] = "queued"
    meta["current_index"] = missing[0]
    meta["done"] = len(done)
    meta["error"] = None
    meta["note"] = None
    _write_json(target / "batch.json", meta)
    design = dict(cfg.get("voice.design") or {})
    with _RUNNING_LOCK:
        _RUNNING[batch_id] = {"cancel": threading.Event(), "proc": None}
    thread = threading.Thread(
        target=_run_batch,
        args=(store, design, target, batch_id, str(meta.get("prompt") or ""), total),
        kwargs={"indices": missing},
        daemon=True,
    )
    with _THREADS_LOCK:
        _THREADS[batch_id] = thread
    thread.start()
    _publish(batch_id, {"type": "status", "status": "running", "index": missing[0]})
    return meta


def _attach_adopted(store, meta: dict) -> dict:
    """给批次元数据补 `adopted_indices`，供页面显示「已自动采用候选 N」。"""
    if not meta:
        return meta
    payload = dict(meta)
    payload["adopted_indices"] = _adopted_indices(
        store, payload, batch_dir(store, str(payload.get("batch_id") or ""))
    )
    return payload


def batch_events(store, batch_id: str):
    """产出批次事件序列：先回放磁盘状态，再转入实时广播。

    参数:
        store: 工程。
        batch_id: 批次 ID。

    产出:
        `(event_name, payload)`；event_name 为 stage/batch/candidate/candidate_error/chars/ping/done。
        调用方负责编码成 SSE；页面断开只会退订，不会取消生成。

    异常:
        ValueError: 批次不存在。
    """
    meta = read_batch(store, batch_id)
    if not meta:
        raise ValueError("批次不存在")
    channel = _subscribe(batch_id)
    try:
        # 先订阅再重新读盘：回放期间到达的事件留在队列里，不会丢
        meta = read_batch(store, batch_id)
        total = int(meta.get("total") or 0)
        if meta.get("status") == "running":
            yield ("stage", {"stage": meta.get("stage") or "queued"})
            yield (
                "batch",
                {
                    "type": "batch",
                    "index": int(meta.get("current_index") or (len(meta.get("candidates") or []) + 1)),
                    "total": total,
                    "unit": "candidate",
                },
            )
        for item in meta.get("candidates") or []:
            yield ("candidate", item)
        for item in meta.get("failures") or []:
            yield ("candidate_error", item)
        if meta.get("status") != "running":
            yield ("done", {"result": _attach_adopted(store, meta)})
            return
        while True:
            try:
                event = channel.get(timeout=15)
            except queue.Empty:
                latest = read_batch(store, batch_id)
                if latest.get("status") != "running":
                    yield ("done", {"result": _attach_adopted(store, latest)})
                    return
                # 心跳：保持连接，页面忽略 ping
                yield ("ping", {})
                continue
            kind = str(event.get("type") or "")
            if kind == "finished":
                yield ("done", {"result": _attach_adopted(store, read_batch(store, batch_id) or event.get("meta") or {})})
                return
            if kind == "candidate":
                yield ("candidate", {key: value for key, value in event.items() if key != "type"})
            elif kind == "candidate_error":
                yield ("candidate_error", {"index": event.get("index"), "message": event.get("message")})
            elif kind in ("batch", "chars"):
                yield (kind, event)
            elif kind == "stage":
                yield ("stage", {"stage": event.get("stage")})
    finally:
        _unsubscribe(batch_id, channel)


def read_batch(store, batch_id: str) -> dict:
    """读取批次状态；不存在返回空 dict。"""
    return _read_json(batch_dir(store, batch_id) / "batch.json")


def delete_batch(store, batch_id: str) -> bool:
    """删除整个候选批次目录（运行中的批次先停止，避免后台线程继续写盘）。"""
    target = batch_dir(store, batch_id)
    if not target.exists():
        return False
    with _RUNNING_LOCK:
        running = batch_id in _RUNNING
    if running:
        cancel_batch(store, batch_id)
    shutil.rmtree(target, ignore_errors=True)
    return True


def _adopted_indices(store, meta: dict, target: Path) -> list[int]:
    """找出该批次里已被采用（已固化为正式参考音）的候选序号。

    参数:
        store: 工程。
        meta: 批次元数据。
        target: 批次目录。

    返回:
        候选序号列表；旧数据没有 candidate_index 时按参考音 sha256 回退匹配。
    """
    batch_id = meta.get("batch_id")
    variants = []
    for voice in store.load_voices().get("voices") or []:
        for variant in voice.get("variants") or []:
            if (variant.get("generation") or {}).get("batch_id") == batch_id:
                variants.append(variant)
    adopted: set[int] = set()
    for variant in variants:
        index = (variant.get("generation") or {}).get("candidate_index")
        if index:
            adopted.add(int(index))
    if adopted:
        return sorted(adopted)
    # 兼容旧批次：参考音与候选文件逐字节一致即视为已采用
    ref_hashes = {
        (variant.get("reference") or {}).get("sha256")
        for variant in variants
        if (variant.get("reference") or {}).get("sha256")
    }
    if not ref_hashes:
        return []
    for candidate in meta.get("candidates") or []:
        path = target / str(candidate.get("file") or "")
        if path.is_file() and _sha256(path) in ref_hashes:
            adopted.add(int(candidate.get("index") or 0))
    return sorted(index for index in adopted if index > 0)


def list_batches(store, character_id: str | None = None) -> list[dict]:
    """列出磁盘上仍可用的候选批次，供页面刷新后恢复。

    参数:
        store: 工程。
        character_id: 只返回该角色的批次；为空时返回全部。

    返回:
        批次摘要列表（按生成时间倒序），含候选文件是否仍在、哪些候选已被采用。
    """
    root = store.voice_asset_dir("candidates")
    if not root.exists():
        return []
    items: list[dict] = []
    for target in root.iterdir():
        if not target.is_dir():
            continue
        meta = _read_json(target / "batch.json")
        if not meta.get("batch_id"):
            continue
        if character_id and meta.get("character_id") != character_id:
            continue
        candidates = []
        for candidate in meta.get("candidates") or []:
            path = target / str(candidate.get("file") or "")
            candidates.append({**candidate, "available": path.is_file()})
        if meta.get("status") == "ready" and not any(item["available"] for item in candidates):
            # 候选文件已被手动清理：不把空壳批次恢复回页面
            continue
        adopted = _adopted_indices(store, meta, target)
        created = meta.get("created_at") or datetime.fromtimestamp(
            target.stat().st_mtime, tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        items.append(
            {
                "batch_id": meta.get("batch_id"),
                "status": meta.get("status"),
                "stage": meta.get("stage"),
                "total": int(meta.get("total") or 0),
                "done": int(meta.get("done") or 0),
                "current_index": int(meta.get("current_index") or 0),
                "character_id": meta.get("character_id"),
                "character_name": meta.get("character_name"),
                "prompt": meta.get("prompt"),
                "created_at": created,
                "generated_at": meta.get("generated_at"),
                "error": meta.get("error"),
                "note": meta.get("note"),
                "candidates": candidates,
                "failures": meta.get("failures") or [],
                "adopted": bool(adopted),
                "adopted_indices": adopted,
            }
        )
    items.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return items


def _sha256(path: Path) -> str:
    """计算文件 sha256。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _f0_hint(path: Path, gender: str) -> dict:
    """估计基频中位数，给出与设定性别的冲突提示（只提示，不改标签）。"""
    try:
        import librosa

        audio, sr = librosa.load(str(path), sr=16000, mono=True)
        f0, voiced, _ = librosa.pyin(audio, fmin=60, fmax=500, sr=sr)
        import numpy as np

        flat = f0[voiced] if voiced is not None else f0
        values = flat[~np.isnan(flat)] if flat is not None else []
        if len(values) == 0:
            return {"f0_median": None, "mismatch": False}
        median = float(np.median(values))
        mismatch = (gender == "male" and median > 210) or (gender == "female" and median < 120)
        return {"f0_median": round(median, 1), "mismatch": bool(mismatch)}
    except Exception:  # noqa: BLE001 - 提示属于增强信息，失败不影响主流程
        return {"f0_median": None, "mismatch": False}


def select_candidate(
    store,
    batch_id: str,
    index: int,
    name: str,
    gender: str,
    gender_source: str,
    bind,
    voice_id: str | None = None,
    record_undo: bool = True,
) -> dict:
    """把候选音频固化为正式参考音，并（可选）绑定角色。

    参数:
        store: 工程。
        batch_id: 批次 ID。
        index: 候选序号（从 1 开始）。
        name: 音色名称（新建音色时必填）。
        gender: 人工确认后的性别标签。
        gender_source: character | manual。
        bind: 是否绑定到批次对应角色。
        voice_id: 追加到已有音色时传入；为空则新建音色。
        record_undo: 绑定前是否写撤销快照（批量任务自行写整批日志时传 False）。

    返回:
        `{voice, variant_id, character?}`。

    异常:
        ValueError: 批次未就绪、候选不存在或参数非法。
    """
    meta = read_batch(store, batch_id)
    if meta.get("status") == "running":
        raise ValueError("批次仍在生成中")
    candidate = next((item for item in meta.get("candidates") or [] if int(item.get("index")) == int(index)), None)
    if candidate is None:
        raise ValueError("候选不存在")
    if gender not in {"male", "female", "other", "unknown"}:
        raise ValueError("性别标签不合法")
    # 性别兜底：只有角色档案明确为「其他」时才保留 other，否则沿用档案或记为未知
    gender_note = None
    archive_character = None
    if meta.get("character_id"):
        archive_character = next(
            (item for item in store.load_characters()["characters"] if item["id"] == meta["character_id"]),
            None,
        )
    archive_gender = (archive_character or {}).get("gender")
    if gender == "other":
        if archive_gender in {"male", "female", "other"}:
            gender = archive_gender
            gender_source = "character"
            if archive_gender != "other":
                gender_note = "已按角色档案把音色性别记为「男声/女声」，未使用「其他」"
        else:
            gender = "unknown"
            gender_note = "角色档案未标注可用的性别，音色标签已记为「未知」，未使用「其他」"
    source_path = batch_dir(store, batch_id) / candidate["file"]
    if not source_path.exists():
        raise ValueError("候选音频已过期或被清理")
    duration_ms = int(candidate.get("duration_ms") or 0)
    if duration_ms < 2000 or duration_ms > 20000:
        raise ValueError(f"候选音频时长 {duration_ms}ms 不在 2000~20000ms 范围，建议重新抽卡")
    variant_id = entities.new_id("vav")
    ref_dir = store.voice_asset_dir("ref")
    ref_dir.mkdir(parents=True, exist_ok=True)
    final = ref_dir / f"{variant_id}.wav"
    shutil.copy2(source_path, final)
    sha = _sha256(final)
    variant = {
        "id": variant_id,
        "name": str(name or "默认")[:60],
        "source": "designed",
        "emotion_tags": ["default"],
        "reference": {
            "audio_path": str(final.relative_to(store.root)),
            "duration_ms": duration_ms,
            "text": None,
            "sha256": sha,
            "denoise": {"enabled": False, "engine": None, "original_audio_path": None, "applied_at": None, "note": "模型生成样本，默认不降噪"},
        },
        "consent": {"confirmed": True, "note": "AI 生成音色（VoxCPM2），非真人录音"},
        "generation": {
            "engine": "voxcpm2",
            "model_id": meta.get("model_id") or "openbmb/VoxCPM2",
            "model_version": None,
            "prompt": str(meta.get("prompt") or "")[:2000],
            "profile": meta.get("profile"),
            "seed": candidate.get("seed"),
            "candidate_index": int(index),
            "params": meta.get("params") or {},
            "batch_id": batch_id,
            "license": "Apache-2.0",
            "generated_at": meta.get("generated_at") or _now(),
        },
        "gender_source": gender_source,
        "provider_availability": [
            {"provider_id": "indextts_25", "status": "ready", "clone_supported": True, "checked_at": _now()}
        ],
        "status": "ready",
        "created_at": _now(),
    }
    table = store.load_voices()
    if voice_id:
        voice = next((item for item in table["voices"] if item["id"] == voice_id), None)
        if voice is None:
            final.unlink(missing_ok=True)
            raise ValueError("音色不存在")
        voice["variants"].append(variant)
        voice["updated_at"] = _now()
    else:
        voice = {
            "id": entities.new_id("voc"),
            "name": str(name or meta.get("character_name") or "生成音色")[:120],
            "engine": None,
            "default_provider_id": None,
            "language": "zh-CN",
            "gender": gender,
            "tags": ["AI生成", "音色设计"],
            "default_variant_id": variant["id"],
            "variants": [variant],
            "status": "ready",
            "revision": 1,
            "created_at": _now(),
            "updated_at": _now(),
        }
        table["voices"].append(voice)
    try:
        store.save_voices(table)
    except Exception:
        final.unlink(missing_ok=True)
        raise
    character = None
    if bind and meta.get("character_id"):
        characters = store.load_characters()
        target = next((item for item in characters["characters"] if item["id"] == meta["character_id"]), None)
        if target is not None:
            if record_undo:
                from . import voice_casting

                voice_casting.record_binding_undo(store, target)
            binding = target.get("voice_binding") or {}
            target["voice_binding"] = {
                "adapter": binding.get("adapter") or "placeholder",
                "voice_id": voice["id"],
                "voice_library_id": voice["id"],
                "default_variant_id": voice["default_variant_id"],
                "emotion_variant_map": binding.get("emotion_variant_map") or {},
            }
            target["updated_at"] = _now()
            store.save_characters(characters)
            character = target
    return {"voice": voice, "variant_id": variant["id"], "character": character, "gender": gender, "gender_note": gender_note}
