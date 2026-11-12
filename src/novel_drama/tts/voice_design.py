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
import shutil
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..adapters.base import Message, TextGenRequest
from ..models import entities

WORKER_PATH = Path(__file__).resolve().parent / "providers" / "voxcpm2_design_worker.py"

_THREADS: dict[str, threading.Thread] = {}
_THREADS_LOCK = threading.Lock()

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
4. 性别必须服从人物设定的 gender；设定为 unknown 时按背景合理推断并在 evidence 里说明。
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
    gender = character.get("gender") if character.get("gender") in {"male", "female"} else "unknown"
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
        profile = _normalize_profile(raw, character)
        return {"profile": profile, "source": "llm", "note": None}
    except Exception as exc:  # noqa: BLE001 - 任何失败都回退模板，不阻断功能
        return {"profile": _rule_profile(character), "source": "rule", "note": f"LLM 描述生成失败，已用规则模板：{exc}"}


def _normalize_profile(raw: dict, character: dict) -> dict:
    """裁剪并补全 LLM 输出，保证字段完整、长度可控。"""
    gender = raw.get("gender") if raw.get("gender") in {"male", "female", "other", "unknown"} else (character.get("gender") or "unknown")
    if gender not in {"male", "female", "other", "unknown"}:
        gender = "unknown"
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
    return profile


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


def create_batch(store, cfg: dict, character: dict, prompt: str, count: int, profile: dict | None = None) -> dict:
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
        "error": None,
        "note": None,
        "created_at": _now(),
    }
    _write_json(target / "batch.json", meta)
    thread = threading.Thread(target=_run_batch, args=(store, dict(design), target, batch_id, prompt, count), daemon=True)
    with _THREADS_LOCK:
        _THREADS[batch_id] = thread
    thread.start()
    return meta


def _run_batch(store, design: dict, target: Path, batch_id: str, prompt: str, count: int) -> None:
    """执行一次抽卡：写临时输入 → 起 worker → 逐行解析进度 → 收尾。"""
    meta_path = target / "batch.json"
    meta = _read_json(meta_path)
    prompt_file = target / "prompt.txt"
    script_file = target / "script.txt"
    log_path = target / "worker.log"
    prompt_file.write_text(prompt, encoding="utf-8")
    script_file.write_text(str(design.get("script") or "你好，这是一段用于试听的样音。"), encoding="utf-8")
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
    timeout_s = float(design.get("timeout_s", 900))
    killed = {"value": False}
    proc = None
    try:
        with log_path.open("w", encoding="utf-8") as log:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=log, text=True, bufsize=1, start_new_session=True)

            def watchdog() -> None:
                """超时后终止整个 worker 进程组。"""
                try:
                    proc.wait(timeout=timeout_s)
                except subprocess.TimeoutExpired:
                    killed["value"] = True
                    _kill(proc)

            threading.Thread(target=watchdog, daemon=True).start()
            for line in proc.stdout or []:
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
                    meta["done"] = int(event.get("index", 0)) - 1 if stage == "generating" else meta.get("done", 0)
                    _write_json(meta_path, meta)
                elif stage == "candidate":
                    meta["done"] = int(event.get("index", meta["done"]))
                    meta["candidates"].append(
                        {
                            "index": int(event["index"]),
                            "file": event["file"],
                            "seed": event.get("seed"),
                            "duration_ms": int(event.get("duration_ms") or 0),
                        }
                    )
                    _write_json(meta_path, meta)
                elif stage == "error":
                    meta["status"] = "failed"
                    meta["error"] = str(event.get("message") or "生成失败")
                    _write_json(meta_path, meta)
            code = proc.wait(timeout=30)
        if meta.get("status") == "failed":
            _cleanup_partial(target)
            return
        if killed["value"]:
            raise TimeoutError(f"生成超时（>{int(timeout_s)} 秒）")
        if code != 0:
            raise RuntimeError(f"worker 退出码 {code}，详见 worker.log")
        if len(meta.get("candidates") or []) < count:
            raise RuntimeError("候选数量不足，详见 worker.log")
        meta["status"] = "ready"
        meta["stage"] = "done"
        meta["generated_at"] = _now()
        # 入库前给每条候选补基频提示；只做提醒，不自动改性别标签
        gender_hint = str((meta.get("profile") or {}).get("gender") or "unknown")
        for item in meta.get("candidates") or []:
            item["hint"] = _f0_hint(target / item["file"], gender_hint)
        _write_json(meta_path, meta)
    except Exception as exc:  # noqa: BLE001 - 任何失败都收敛为批次失败，不抛到线程外
        if proc is not None and proc.poll() is None:
            _kill(proc)
        meta["status"] = "failed"
        meta["error"] = str(exc)
        _write_json(meta_path, meta)
        _cleanup_partial(target)
    finally:
        for item in (prompt_file, script_file):
            item.unlink(missing_ok=True)
        with _THREADS_LOCK:
            _THREADS.pop(batch_id, None)


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


def _cleanup_partial(target: Path) -> None:
    """失败/中断时删除半成品候选与临时文件，只保留 batch.json 与日志。"""
    for pattern in ("cand-*.wav", ".cand-*.tmp*", "prompt.txt", "script.txt"):
        for item in target.glob(pattern):
            item.unlink(missing_ok=True)


def read_batch(store, batch_id: str) -> dict:
    """读取批次状态；不存在返回空 dict。"""
    return _read_json(batch_dir(store, batch_id) / "batch.json")


def delete_batch(store, batch_id: str) -> bool:
    """删除整个候选批次目录。"""
    target = batch_dir(store, batch_id)
    if not target.exists():
        return False
    shutil.rmtree(target, ignore_errors=True)
    return True


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


def select_candidate(store, batch_id: str, index: int, name: str, gender: str, gender_source: str, bind, voice_id: str | None = None) -> dict:
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

    返回:
        `{voice, variant_id, character?}`。

    异常:
        ValueError: 批次未就绪、候选不存在或参数非法。
    """
    meta = read_batch(store, batch_id)
    if meta.get("status") != "ready":
        raise ValueError("批次尚未生成完成")
    candidate = next((item for item in meta.get("candidates") or [] if int(item.get("index")) == int(index)), None)
    if candidate is None:
        raise ValueError("候选不存在")
    if gender not in {"male", "female", "other", "unknown"}:
        raise ValueError("性别标签不合法")
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
    return {"voice": voice, "variant_id": variant["id"], "character": character}
