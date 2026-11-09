"""TTS 断点续跑缓存：按「文本 + 音色变体 + 参考音频 + provider 版本 + 参数」命中已合成片段。

缓存只记录片段位置与元信息，音频本体仍在 `assets/voice/lines/` 下，
因此缓存失效（文件被删）时自动视为未命中。
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

_REF_HASH_CACHE: dict[str, str] = {}


def _now() -> str:
    """当前 UTC 时间（ISO 8601）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def cache_path(root: str | Path) -> Path:
    """缓存文件路径（`.ndw/tts_cache.json`）。"""
    return Path(root) / ".ndw" / "tts_cache.json"


def _ref_hash(ref_audio: str | None) -> str:
    """参考音频内容哈希（带进程内缓存），缺失时返回空串。"""
    if not ref_audio:
        return ""
    path = Path(ref_audio)
    if not path.exists():
        return "missing"
    try:
        stat = path.stat()
    except OSError:
        return "missing"
    key = f"{path}@{int(stat.st_mtime)}@{stat.st_size}"
    if key in _REF_HASH_CACHE:
        return _REF_HASH_CACHE[key]
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError:
        return "missing"
    value = digest.hexdigest()
    _REF_HASH_CACHE[key] = value
    return value


def compute_key(
    *,
    text: str,
    variant_id: str | None,
    ref_audio: str | None,
    provider_id: str | None,
    model_version: str | None,
    instruct: str | None,
    emotion: str | None,
    chunk_limit: int,
    chunk_silence_ms: int,
    sample_rate: int,
    edge_trim_ms: int,
    edge_trim_max_ms: int,
    keep_min_ms: int,
    silence_threshold_dbfs: float,
    hard_split_ratio: float,
) -> str:
    """计算缓存键（sha256）。

    接缝裁切参数一并参与：否则改了裁切配置仍会命中旧产物，音频与配置对不上。

    参数:
        text: 台词文本。
        variant_id: 音色变体 ID。
        ref_audio: 参考音频路径（按内容哈希参与）。
        provider_id: 实际 provider。
        model_version: 权重版本。
        instruct: 风格指令。
        emotion: 情绪。
        chunk_limit: 切分上限。
        chunk_silence_ms: 目标净间隔。
        sample_rate: 输出采样率。
        edge_trim_ms: 接缝裁切目标值。
        edge_trim_max_ms: 接缝裁切硬上限。
        keep_min_ms: 每端保留气口。
        silence_threshold_dbfs: 静音阈值。
        hard_split_ratio: 硬切静音折扣。

    返回:
        `sha256:<hex>`。
    """
    payload = {
        "text": text,
        "variant_id": variant_id,
        "ref": _ref_hash(ref_audio),
        "provider_id": provider_id,
        "model_version": model_version,
        "instruct": instruct,
        "emotion": emotion,
        "chunk_limit": int(chunk_limit),
        "chunk_silence_ms": int(chunk_silence_ms),
        "sample_rate": int(sample_rate),
        "edge_trim_ms": int(edge_trim_ms),
        "edge_trim_max_ms": int(edge_trim_max_ms),
        "keep_min_ms": int(keep_min_ms),
        "silence_threshold_dbfs": round(float(silence_threshold_dbfs), 3),
        "hard_split_ratio": round(float(hard_split_ratio), 3),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def load(root: str | Path) -> dict:
    """读取缓存文件；不存在或损坏时返回空缓存。

    返回:
        {"schema_version": 1, "entries": {key: entry}}。
    """
    path = cache_path(root)
    if not path.exists():
        return {"schema_version": 1, "entries": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"schema_version": 1, "entries": {}}
    if not isinstance(data.get("entries"), dict):
        data["entries"] = {}
    return data


def save(root: str | Path, data: dict) -> None:
    """原子写回缓存文件。

    参数:
        root: 工程根目录。
        data: 缓存内容。
    """
    path = cache_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def lookup(root: str | Path, key: str, table: dict | None = None) -> dict | None:
    """查询缓存；音频文件已不存在时视为未命中。

    参数:
        root: 工程根目录（`table` 已给出时仍用来解析相对的 asset_id）。
        key: 缓存键。
        table: 已加载的缓存表。批量路径在章开始时 `load` 一次、章内复用同一份，
            避免每行都整表读盘；交互式单条路径保持传 None，每次现读。

    返回:
        命中的条目（含 asset_id/duration_ms），否则 None。
    """
    data = table if table is not None else load(root)
    entry = (data.get("entries") or {}).get(key)
    if not entry:
        return None
    asset = entry.get("asset_id")
    if not asset:
        return None
    path = Path(asset)
    if not path.is_absolute():
        path = Path(root) / asset
    if not path.exists():
        return None
    return entry


def put(root: str | Path, key: str, entry: dict) -> None:
    """写入一条缓存记录（每次命中/生成后调用）。

    交互式路径用这个；批量路径请用 `put_many`，避免每行都全量读写一次缓存文件。

    参数:
        root: 工程根目录。
        key: 缓存键。
        entry: {asset_id, duration_ms, provider_id, model_version, variant_id}。
    """
    put_many(root, {key: entry})


def put_many(root: str | Path, entries: dict[str, dict]) -> int:
    """批量写入缓存记录：只读一次、只写一次。

    参数:
        root: 工程根目录。
        entries: {缓存键: 条目}。

    返回:
        实际写入的条数；`entries` 为空时直接返回 0，不读写文件。
    """
    if not entries:
        return 0
    data = load(root)
    bucket = data.setdefault("entries", {})
    stamp = _now()
    for key, entry in entries.items():
        bucket[key] = {**entry, "updated_at": stamp}
    save(root, data)
    return len(entries)
