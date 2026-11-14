"""段落（台词行）级语音缓存：按「文本 + 说话人 + 音色 + 合成参数」复用已合成音频。

设计要点：
- 一条缓存对应一句台词（配音链路的最小可复用单元），音频本体仍在
  `assets/voice/lines/` 下；缓存只记签名与产物位置，文件被删即视为未命中；
- 签名覆盖所有会影响音频产物的字段：台词文本、说话人、音色条目/变体/参考音频内容、
  provider 与模型版本、情绪与强度、语速、风格指令、语言、切分与接缝裁切参数、
  目标时长、时长对齐配置；任一变化都会得到不同的键；
- 按 `line_id` 留一条索引，用于解释「这次为什么复用 / 为什么重新合成」；
- 旧结构（schema 1）不再读取，直接按空缓存处理（项目未上线，不做兼容）。

本模块只负责签名、读写与差异说明；「哪些行要跑、命中还是重合成」由 `tts.reuse` 决定。
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from .fingerprint import normalize_value

# 缓存结构版本：签名规则或存储结构变化时 +1，旧文件按空缓存处理
CACHE_SCHEMA_VERSION = 3

# 参与签名的参数白名单（全部来自侧车 `params` 的同名字段）
SIGNATURE_PARAM_FIELDS: tuple[str, ...] = (
    "text",
    "speaker_id",
    "voice_id",
    "variant_id",
    "emotion",
    "emotion_intensity",
    "speed",
    "instruct",
    "language",
    "provider_id",
    "model_version",
    "chunk_limit",
    "chunk_silence_ms",
    "edge_trim_ms",
    "edge_trim_max_ms",
    "keep_min_ms",
    "silence_threshold_dbfs",
    "hard_split_ratio",
)

# 签名差异的展示名与分组（顺序即输出顺序）
SIGNATURE_LABELS: dict[str, tuple[str, str]] = {
    "text": ("文本", "文本"),
    "speaker_id": ("说话人", "音色"),
    "voice_id": ("音色", "音色"),
    "variant_id": ("音色变体", "音色"),
    "ref_sha256": ("参考音频", "音色"),
    "ref_text": ("参考文本", "音色"),
    "emotion": ("情绪", "演绎"),
    "emotion_intensity": ("强度", "演绎"),
    "speed": ("语速", "演绎"),
    "instruct": ("风格指令", "演绎"),
    "language": ("语言", "演绎"),
    "target_duration_ms": ("目标时长", "时长"),
    "provider_id": ("引擎", "引擎"),
    "model_version": ("模型版本", "引擎"),
    "chunk_limit": ("切分上限", "切分"),
    "chunk_silence_ms": ("接缝静音", "切分"),
    "edge_trim_ms": ("接缝裁切", "切分"),
    "edge_trim_max_ms": ("裁切上限", "切分"),
    "keep_min_ms": ("保留气口", "切分"),
    "silence_threshold_dbfs": ("静音阈值", "切分"),
    "hard_split_ratio": ("硬切折扣", "切分"),
    "alignment": ("对齐参数", "时长"),
}

# 参考音频内容哈希的进程内缓存：键为「路径@mtime@大小」
_REF_HASH_CACHE: dict[str, str] = {}


def _now() -> str:
    """当前 UTC 时间（ISO 8601）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def cache_path(root: str | Path) -> Path:
    """缓存文件路径（`.ndw/tts_cache.json`）。

    参数:
        root: 工程根目录。

    返回:
        缓存文件路径。
    """
    return Path(root) / ".ndw" / "tts_cache.json"


def ref_hash(ref_audio: str | None) -> str:
    """参考音频内容哈希（带进程内缓存）。

    参数:
        ref_audio: 参考音频路径；缺失时返回空串，文件不存在返回 `missing`。

    返回:
        `sha256` 十六进制串、`missing` 或空串。
    """
    if not ref_audio:
        return ""
    path = Path(ref_audio)
    try:
        stat = path.stat()
    except OSError:
        return "missing"
    key = f"{path}@{int(stat.st_mtime)}@{stat.st_size}"
    cached = _REF_HASH_CACHE.get(key)
    if cached is not None:
        return cached
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


def build_signature(
    params: dict,
    *,
    ref_audio: str | None = None,
    ref_text: str | None = None,
    target_duration_ms: int | None = None,
    alignment: dict | None = None,
) -> dict:
    """按参数集构造段落签名。

    参数:
        params: 参与签名的参数字典（见 `SIGNATURE_PARAM_FIELDS`）；多余键忽略，
            缺失键按 None 处理。
        ref_audio: 参考音频路径，按文件内容哈希参与签名。
        ref_text: 参考音频对应文本（部分克隆实现会用它，改文本需重算）。
        target_duration_ms: 行目标时长；会影响补静音与对齐结果。
        alignment: 时长对齐配置（`alignment.resolve_config` 的结果）；只取会影响产物的字段。

    返回:
        归一化后的签名字典（浮点保留 3 位，字段固定、可直接做差）。
    """
    signature = {field: normalize_value(params.get(field)) for field in SIGNATURE_PARAM_FIELDS}
    signature.update(
        {
            "cache_version": CACHE_SCHEMA_VERSION,
            "ref_sha256": ref_hash(ref_audio),
            "ref_text": normalize_value(ref_text),
            "target_duration_ms": normalize_value(target_duration_ms),
            "alignment": normalize_value(_alignment_subset(alignment)),
        }
    )
    return signature


def _alignment_subset(alignment: dict | None) -> dict:
    """取对齐配置里会影响成片的字段。

    参数:
        alignment: 完整对齐配置；为空时按空字典处理。

    返回:
        `{enabled, max_error_ms, max_speed_change, on_exceed, max_regenerate}`。
    """
    raw = alignment or {}
    return {
        "enabled": bool(raw.get("enabled", True)),
        "max_error_ms": int(raw.get("max_error_ms", 250)),
        "max_speed_change": float(raw.get("max_speed_change", 0.05)),
        "on_exceed": str(raw.get("on_exceed") or "regenerate"),
        "max_regenerate": int(raw.get("max_regenerate", 1)),
    }


def canonical(signature: dict) -> str:
    """把签名序列化成稳定字符串（键排序、紧凑分隔符）。

    参数:
        signature: 签名字典。

    返回:
        可直接做哈希的 JSON 字符串。
    """
    payload = {str(key): normalize_value(signature[key]) for key in sorted(signature, key=str)}
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def signature_key(signature: dict) -> str:
    """计算签名对应的缓存键。

    参数:
        signature: `build_signature` 的结果。

    返回:
        `seg:<64 位十六进制>`。
    """
    digest = hashlib.sha256(canonical(signature).encode("utf-8")).hexdigest()
    return f"seg:{digest}"


def with_provider(signature: dict, provider_id: str | None, model_version: str | None) -> dict:
    """替换签名里的 provider 与模型版本，返回新签名。

    说明:
        真实合成可能发生 provider 回落；产物必须按「实际 provider」归档，
        否则下次仍按首选 provider 查键，永远命中不到。

    参数:
        signature: 原签名。
        provider_id: 实际使用的 provider。
        model_version: 实际模型版本。

    返回:
        新签名字典（不修改入参）。
    """
    updated = dict(signature)
    updated["provider_id"] = provider_id
    updated["model_version"] = model_version
    return updated


def _text_delta(old: str | None, new: str | None) -> str:
    """统计两段文本的增删字数。

    参数:
        old: 生成时的文本。
        new: 当前文本。

    返回:
        形如 `+12/−3 字` 的摘要。
    """
    matcher = difflib.SequenceMatcher(None, old or "", new or "")
    added = removed = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "insert":
            added += j2 - j1
        elif tag == "delete":
            removed += i2 - i1
        elif tag == "replace":
            added += j2 - j1
            removed += i2 - i1
    return f"+{added}/−{removed} 字"


def _format_value(value) -> str:
    """把签名里的一个值格式化成给人看的样子。

    参数:
        value: 任意签名值。

    返回:
        人类可读文本；None/空串显示为「空」。
    """
    if value is None or value == "":
        return "空"
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def describe_changes(before: dict | None, after: dict) -> list[dict]:
    """比对两份签名，输出「为什么重新合成」的逐字段说明。

    参数:
        before: 同一行上一次生成时的签名；没有历史时传 None。
        after: 当前签名。

    返回:
        `[{group, field, label, from, to, text}]`；无历史或无差异时为空列表。
    """
    if not before:
        return []
    changes: list[dict] = []
    for field in SIGNATURE_LABELS:
        if field not in after and field not in before:
            continue
        old = normalize_value(before.get(field))
        new = normalize_value(after.get(field))
        if old == new:
            continue
        label, group = SIGNATURE_LABELS[field]
        if field == "text":
            summary = f"文本 {_text_delta(old, new)}"
        else:
            summary = f"{label} {_format_value(old)} → {_format_value(new)}"
        changes.append(
            {
                "group": group,
                "field": field,
                "label": label,
                "from": old,
                "to": new,
                "text": summary,
            }
        )
    return changes


def load(root: str | Path) -> dict:
    """读取缓存文件；不存在、损坏或版本不符时返回空缓存。

    参数:
        root: 工程根目录。

    返回:
        `{"schema_version": 3, "entries": {key: entry}, "lines": {line_id: key}}`。
    """
    empty = {"schema_version": CACHE_SCHEMA_VERSION, "entries": {}, "lines": {}}
    path = cache_path(root)
    if not path.exists():
        return empty
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return empty
    if not isinstance(data, dict) or int(data.get("schema_version") or 0) != CACHE_SCHEMA_VERSION:
        return empty
    if not isinstance(data.get("entries"), dict):
        data["entries"] = {}
    if not isinstance(data.get("lines"), dict):
        data["lines"] = {}
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
        root: 工程根目录（用于解析相对的 asset_id）。
        key: 缓存键。
        table: 已加载的缓存表；批量路径在章开始时读一次、章内复用，避免逐行读盘。

    返回:
        命中的条目，否则 None。
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


def entry_for_line(table: dict | None, line_id: str | None) -> dict | None:
    """取某条台词最近一次写入缓存的条目（不校验文件是否还在）。

    参数:
        table: 已加载的缓存表。
        line_id: 台词行 ID。

    返回:
        缓存条目；没有行索引时返回 None。用于解释本次变化。
    """
    if not table or not line_id:
        return None
    key = (table.get("lines") or {}).get(str(line_id))
    if not key:
        return None
    return (table.get("entries") or {}).get(key)


def put_many(root: str | Path, entries: dict[str, dict]) -> int:
    """批量写入缓存记录：只读一次、只写一次。

    参数:
        root: 工程根目录。
        entries: `{缓存键: 条目}`；条目需含 `signature`、`asset_id`、`line_id`。

    返回:
        实际写入条数；`entries` 为空时直接返回 0，不读写文件。
    """
    if not entries:
        return 0
    data = load(root)
    bucket = data.setdefault("entries", {})
    lines = data.setdefault("lines", {})
    stamp = _now()
    for key, entry in entries.items():
        record = dict(entry)
        record.setdefault("created_at", stamp)
        record["updated_at"] = stamp
        asset = record.get("asset_id")
        if asset and record.get("bytes") is None:
            record["bytes"] = _file_size(root, asset)
        bucket[key] = record
        if record.get("line_id"):
            lines[str(record["line_id"])] = key
    save(root, data)
    return len(entries)


def update_line_index(table: dict, key: str, entry: dict) -> None:
    """把一条记录同步进内存缓存表（同章重复台词可立刻命中，不必等落盘）。

    参数:
        table: 内存中的缓存表（`load` 的结果）。
        key: 缓存键。
        entry: 缓存条目。
    """
    table.setdefault("entries", {})[key] = entry
    if entry.get("line_id"):
        table.setdefault("lines", {})[str(entry["line_id"])] = key


def _file_size(root: str | Path, asset_id: str) -> int | None:
    """取产物文件字节数（取不到时返回 None）。

    参数:
        root: 工程根目录。
        asset_id: 相对或绝对产物路径。

    返回:
        文件大小；不存在或读取失败时返回 None。
    """
    path = Path(asset_id)
    if not path.is_absolute():
        path = Path(root) / asset_id
    try:
        return path.stat().st_size
    except OSError:
        return None
