"""台词行参数指纹：把「会影响音频产物」的参数归一化后哈希。

用途（三者共用同一份结果）：
1. 过期判定：当前参数指纹 vs 生成时快照指纹；
2. 「改 N」变更说明：逐字段比对时以快照为基准；
3. 缓存与幂等键的辅助标识。

三条硬规则：
- **不含 `revision`**：撤回后必须能重新命中原快照，revision 只做并发锁；
- **浮点归一到 3 位**：`0.95` 与 `0.9500001` 视为同一参数，避免数字漂移造成整篇误判；
- **字段白名单 + 键排序**：哈希与字典插入顺序、多余字段无关。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

# 指纹算法版本：字段增删或归一规则变化时必须 +1，否则新旧指纹会被误判为只差参数
FP_VERSION = 2

# 参与指纹的参数白名单（顺序即文档顺序，实际哈希前会排序）
# 不含 revision / latency_ms / routing / created_at：它们不是用户意图
FP_FIELDS: tuple[str, ...] = (
    "text",
    "emotion",
    "emotion_intensity",
    "speed",
    "delivery_note",
    "voice_id",
    "variant_id",
    "instruct",
    "chunk_limit",
    "chunk_silence_ms",
    "edge_trim_ms",
    "edge_trim_max_ms",
    "keep_min_ms",
    "silence_threshold_dbfs",
    "hard_split_ratio",
    "provider_id",
    "model_version",
)

# 浮点保留位数
_FLOAT_DIGITS = 3


def normalize_value(value: Any) -> Any:
    """归一单个参数值，保证同一语义得到同一表示。

    参数:
        value: 任意参数值，通常为 str/int/float/bool/None，也允许 list/tuple/dict。

    返回:
        归一后的值：float 保留 3 位小数；list/tuple 逐项归一；dict 按键名排序后逐项归一；
        其他类型原样返回。
    """
    if isinstance(value, bool):
        # bool 是 int 的子类，必须先于数值判断，避免被 round 成 0/1
        return value
    if isinstance(value, float):
        return round(value, _FLOAT_DIGITS)
    if isinstance(value, (list, tuple)):
        return [normalize_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): normalize_value(value[key]) for key in sorted(value, key=str)}
    return value


def canonical(params: dict) -> str:
    """把参数集序列化成稳定字符串，作为哈希输入。

    参数:
        params: 参数字典，键名不必齐全，缺失的 `FP_FIELDS` 会补 None。

    返回:
        紧凑、键有序的 JSON 字符串（含 `fp_version`，版本变化必然改变哈希）。
    """
    payload = {
        "fp_version": FP_VERSION,
        "params": {field: normalize_value(params.get(field)) for field in FP_FIELDS},
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def line_fingerprint(params: dict) -> str:
    """计算台词行的参数指纹。

    参数:
        params: 参数字典（见 `FP_FIELDS`）；多余键会被忽略。

    返回:
        `sha256:<64 位十六进制>` 形式的指纹字符串。
    """
    digest = hashlib.sha256(canonical(params).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"
