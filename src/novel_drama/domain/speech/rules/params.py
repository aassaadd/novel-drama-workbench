"""台词行参数指纹、「改 N」变更说明与状态枚举。

从 `tts/fingerprint.py` 与 `tts/line_state.py` 收敛出来的纯规则，三条硬规则沿用原文：

- **不含 `revision`**：撤回后必须能重新命中原快照，revision 只做并发锁；
- **浮点归一到 3 位**：`0.95` 与 `0.9500001` 视为同一参数，避免数字漂移造成整篇误判；
- **字段白名单 + 键排序**：哈希与字典插入顺序、多余字段无关。

`current_params` 需要 `instruct`（交付说明/情绪收敛结果），由调用方算好传入，
保证本模块不依赖 `tts.batch` 的提示词拼装。
"""

from __future__ import annotations

import difflib
import hashlib
import json
from typing import Any

# 指纹算法版本：字段增删或归一规则变化时必须 +1，否则新旧指纹会被误判为只差参数
FP_VERSION = 3

# 参与指纹的参数白名单（顺序即文档顺序，实际哈希前会排序）
# 不含 revision / latency_ms / routing / created_at：它们不是用户意图
FP_FIELDS: tuple[str, ...] = (
    "text",
    "emotion",
    "emotion_intensity",
    "speed",
    "delivery_note",
    "target_duration_ms",
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

# 允许落盘的状态；`stale` / `skipped` 是派生态，写盘即视为非法
PERSISTED_STATES: tuple[str, ...] = ("pending", "queued", "running", "ready", "failed")

# 可被 `--status` 筛选的派生状态；`unknown` 是历史遗留的「未识别说话人」伪状态，不算派生状态
SELECTABLE_STATES: tuple[str, ...] = ("pending", "stale", "failed", "ready")
STATUS_FILTER_ALIASES: tuple[str, ...] = SELECTABLE_STATES + ("unknown",)

# 需要重跑的三态：`--scope pending` 与 `line list --status pending,stale,failed`
# 必须共用这一份定义，否则「筛选计数」与「plan 计数」会各算一套
PENDING_STATES: tuple[str, ...] = ("pending", "stale", "failed")

# 生成范围选择器（见 docs/tts-line-state-and-seams.md §5）
SCOPES: tuple[str, ...] = ("selected", "pending", "scene", "chapter")

# 能从行上重建的参数：其余字段（provider_id/model_version/variant_id/切分参数）沿用快照
_LINE_FIELDS: tuple[str, ...] = ("text", "emotion", "emotion_intensity", "speed", "delivery_note", "target_duration_ms")

# 撤回时回滚的字段；text 是创作成果，明确不回滚
REVERT_FIELDS: tuple[str, ...] = ("emotion", "emotion_intensity", "speed", "delivery_note")

# 字段展示名
FIELD_LABELS: dict[str, str] = {
    "text": "文本",
    "emotion": "情绪",
    "emotion_intensity": "强度",
    "voice_id": "音色",
    "variant_id": "音色变体",
    "speaker_id": "说话人",
    "speed": "语速",
    "delivery_note": "交付说明",
    "provider_id": "引擎",
    "model_version": "模型版本",
    "chunk_limit": "切分上限",
    "chunk_silence_ms": "接缝静音",
    "edge_trim_ms": "接缝裁切",
    "edge_trim_max_ms": "裁切上限",
    "keep_min_ms": "保留气口",
    "silence_threshold_dbfs": "静音阈值",
    "hard_split_ratio": "硬切折扣",
}

# 差异分组：(组名, 字段列表)，顺序即输出顺序
# 刻意不含 instruct：它是「交付说明 / 情绪」收敛出来的中间结果，展示它等于把黑盒抛给用户
DIFF_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("文本", ("text",)),
    ("情绪", ("emotion", "emotion_intensity")),
    ("音色", ("voice_id", "variant_id")),
    ("演绎", ("speed", "delivery_note")),
    ("引擎", ("provider_id", "model_version")),
    ("切分", ("chunk_limit", "chunk_silence_ms", "edge_trim_ms", "edge_trim_max_ms", "keep_min_ms", "silence_threshold_dbfs", "hard_split_ratio")),
)


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


def current_params(line: dict, snapshot_params: dict | None = None, engine: dict | None = None, instruct: str | None = None) -> dict:
    """用行上的当前值重建参数快照，用于与侧车快照比对。

    参数:
        line: 台词行。
        snapshot_params: 侧车里的 `params`，作为底稿。
        engine: 当前配置导出的引擎侧参数，覆盖底稿同名字段。
        instruct: 当前行的风格指令（由调用方按交付说明/情绪收敛）。

    返回:
        参数字典：行级 6 个字段取自当前行，`instruct` 取入参，
        引擎侧字段以当前配置为准，其余（provider/model/音色）沿用底稿。
    """
    params = dict(snapshot_params or {})
    params.update(engine or {})
    for field in _LINE_FIELDS:
        params[field] = line.get(field)
    params["instruct"] = instruct
    return params


def text_delta(old: str | None, new: str | None) -> str:
    """统计两段文本的增删字数。

    参数:
        old: 快照里的文本。
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


def format_value(field: str, value) -> str:
    """把一个字段值格式化成给人看的样子。

    参数:
        field: 字段名。
        value: 字段值。

    返回:
        人类可读文本；None 显示为「空」。
    """
    if value is None or value == "":
        return "空"
    if field == "delivery_note":
        return f"「{value}」"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def diff_params(before: dict, after: dict) -> list[dict]:
    """逐字段比对两组参数，输出「改 N」条目。

    参数:
        before: 生成时的参数（侧车快照）。
        after: 当前参数（`current_params` 的结果）。

    返回:
        `[{group, field, label, from, to, text}]`；无差异时为空列表。
        `instruct` 不参与展示：它是交付说明/情绪的收敛结果，展示它会让用户看不出自己改了什么。
    """
    changes: list[dict] = []
    for group, fields in DIFF_GROUPS:
        for field in fields:
            old = before.get(field)
            new = after.get(field)
            if normalize_value(old) == normalize_value(new):
                continue
            if field == "text":
                summary = f"文本 {text_delta(old, new)}"
            else:
                label = FIELD_LABELS.get(field, field)
                summary = f"{label} {format_value(field, old)} → {format_value(field, new)}"
            changes.append(
                {
                    "group": group,
                    "field": field,
                    "label": FIELD_LABELS.get(field, field),
                    "from": old,
                    "to": new,
                    "text": summary,
                }
            )
    return changes
