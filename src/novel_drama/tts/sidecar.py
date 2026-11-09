"""音频侧车文件：记录一次合成实际使用的参数与指纹。

侧车与音频同目录同名（`xxx.wav` → `xxx.wav.json`），是「这条音频还对得上当前参数吗」
的唯一真相。v2 结构与字段含义见 `docs/tts-line-state-and-seams.md` §3.1。

兼容策略：
- v1 侧车没有 `params` / `fingerprint`，`load()` 原样返回并补 `payload_version=1`；
- `snapshot()` 对 v1 返回 None，调用方回退到 `synthesized_revision` 判定。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .fingerprint import line_fingerprint

# 当前侧车结构版本；写入时缺省补这个值
SIDECAR_VERSION = 2

# 没有版本号的侧车按 v1 处理
_LEGACY_VERSION = 1


def sidecar_path(audio_path: str | Path) -> Path:
    """返回音频对应的侧车路径。

    参数:
        audio_path: 音频文件路径，例如 `.../lin_001.wav`。

    返回:
        侧车路径 `.../lin_001.wav.json`。
    """
    return Path(f"{Path(audio_path)}.json")


def dump(audio_path: str | Path, payload: dict) -> Path:
    """原子写入侧车文件。

    参数:
        audio_path: 音频文件路径；侧车写到同级 `音频名.json`。
        payload: 侧车内容。若含 `params` 但缺 `fingerprint`，会按 `params` 现算补上；
                 缺 `payload_version` 时补 `SIDECAR_VERSION`。

    返回:
        实际写入的侧车路径。
    """
    record = dict(payload)
    record.setdefault("payload_version", SIDECAR_VERSION)
    if record.get("fingerprint") is None and isinstance(record.get("params"), dict):
        record["fingerprint"] = line_fingerprint(record["params"])

    target = sidecar_path(audio_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.parent / f"{target.name}.tmp"
    temp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    # 同目录 rename，保证读到的是完整文件而不是写了一半的内容
    os.replace(temp, target)
    return target


def load(audio_path: str | Path) -> dict | None:
    """读取侧车文件。

    参数:
        audio_path: 音频文件路径，或直接传侧车路径。

    返回:
        侧车内容字典；文件不存在、内容不是合法 JSON 对象时返回 None（不抛异常，
        避免批量列表因为一个坏文件整体失败）。
    """
    path = Path(audio_path)
    if path.suffix != ".json":
        path = sidecar_path(path)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    data.setdefault("payload_version", _LEGACY_VERSION)
    return data


def snapshot(record: dict | None) -> dict | None:
    """取出侧车里的「生成时快照」。

    参数:
        record: `load()` 的返回值。

    返回:
        含 `params` 与 `fingerprint` 的字典；v1 侧车或参数缺失时返回 None。
    """
    if not record:
        return None
    params = record.get("params")
    fingerprint = record.get("fingerprint")
    if not isinstance(params, dict) or not fingerprint:
        return None
    return {
        "params": params,
        "fingerprint": fingerprint,
        "text": record.get("text"),
        "dimensions": record.get("dimensions") or {},
        "payload_version": record.get("payload_version", SIDECAR_VERSION),
    }
