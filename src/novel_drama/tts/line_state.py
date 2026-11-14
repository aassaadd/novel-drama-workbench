"""台词行状态解析、「改 N」差异说明与行级撤回。

三者共用同一份真相：音频侧车里的参数快照（v2）。状态判定回答「这条还要不要重跑」，
差异说明回答「用户到底改了哪几项」，撤回回答「怎么把参数退回上一次生成时的值」。
设计依据见 `docs/tts-line-state-and-seams.md` §2~§4。

三条原则：
- `stale` / `skipped` 是**派生态**，不落盘，每次读取时现算，因此不需要维护「何时改回去」；
- 当前参数以侧车快照为底稿，只覆盖「能从行上重建」的字段，避免路由决策
  （provider 选择、模型版本、切分参数）复现不出来就整库误判过期；
- `revision` 退为兜底：旧侧车（v1，无 `params/fingerprint`）仍按 revision 比对，行为与改造前一致。
"""

from __future__ import annotations

import difflib
from pathlib import Path

from ..models import entities
from . import sidecar
from .fingerprint import line_fingerprint, normalize_value

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
_LINE_FIELDS: tuple[str, ...] = ("text", "emotion", "emotion_intensity", "speed", "delivery_note")

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


def _instruct_for(line: dict) -> str | None:
    """把交付说明/情绪收敛成风格指令。

    与 `tts/batch.py` 的 `_instruct_for` 必须完全一致，故延迟导入复用同一份实现
    （模块级导入会与 `batch → pipeline → line_state` 形成循环）。

    参数:
        line: 台词行。

    返回:
        风格指令；既无交付说明也无情绪时返回 None。
    """
    from .batch import _instruct_for as batch_instruct

    return batch_instruct(line)


def _iter_lines(chapter: dict):
    """按场景顺序遍历章节内的所有台词行。

    参数:
        chapter: 章节 dict。

    生成:
        台词行 dict（保持章内原顺序）。
    """
    for scene in chapter.get("scenes") or []:
        for line in scene.get("lines") or []:
            yield line


def _asset_path(root, asset_id: str | None) -> Path | None:
    """把行上的相对产物路径解析成绝对路径。

    参数:
        root: 工程根目录。
        asset_id: 行上的 `tts.asset_id`。

    返回:
        绝对路径；`asset_id` 为空时返回 None。
    """
    if not asset_id:
        return None
    path = Path(asset_id)
    if path.is_absolute():
        return path
    if root is None:
        return None
    return Path(root) / asset_id


def load_snapshots(root, chapter: dict) -> dict[str, dict]:
    """读取本章所有行的侧车快照。

    每行只读一个小文件，不整表扫描，因此一本书 500 行也只有 500 次小读。

    参数:
        root: 工程根目录。
        chapter: 章节 dict。

    返回:
        `{line_id: snapshot}`；没有侧车、旧版（v1）侧车的行不出现在结果里。
    """
    index: dict[str, dict] = {}
    for line in _iter_lines(chapter):
        path = _asset_path(root, (line.get("tts") or {}).get("asset_id"))
        if path is None:
            continue
        snap = sidecar.snapshot(sidecar.load(path))
        if snap is not None:
            index[line.get("id")] = snap
    return index


def live_line_ids(store) -> set[str]:
    """当前队列里仍在排队/执行中的台词行 ID，用于僵尸态判定。

    参数:
        store: ProjectStore。

    返回:
        行 ID 集合；账号无队列文件时为空集合。
    """
    from ..store import QueueStore

    ids: set[str] = set()
    for job in QueueStore(store).load():
        if job.get("status") not in ("queued", "running"):
            continue
        line_id = (job.get("target") or {}).get("line_id")
        if line_id:
            ids.add(line_id)
    return ids


def snapshot_for_line(root, line: dict) -> dict | None:
    """读取单行的侧车快照（只读一个小文件，不扫全章）。

    参数:
        root: 工程根目录。
        line: 台词行。

    返回:
        侧车快照；无产物、产物文件不存在或旧版（v1）侧车时返回 None。
    """
    path = _asset_path(root, (line.get("tts") or {}).get("asset_id"))
    if path is None or not path.exists():
        return None
    return sidecar.snapshot(sidecar.load(path))


def parse_states(raw: str | None) -> tuple[str, ...]:
    """解析 `--status` 的逗号列表。

    参数:
        raw: 原始参数值，如 `pending,stale`；中英文逗号都认。

    返回:
        去重后的状态元组；未给出时返回空元组。

    异常:
        ValueError: 出现未知状态名（由 CLI 转成退出码 2）。
    """
    if not raw:
        return ()
    picked: list[str] = []
    for chunk in raw.replace("，", ",").split(","):
        name = chunk.strip()
        if not name:
            continue
        if name not in STATUS_FILTER_ALIASES:
            raise ValueError(f"未知状态: {name}（可选 {', '.join(STATUS_FILTER_ALIASES)}）")
        if name not in picked:
            picked.append(name)
    return tuple(picked)


def chapter_states(
    chapter: dict,
    characters: dict[str, dict] | None = None,
    snapshots: dict[str, dict] | None = None,
    root=None,
    live_ids: set[str] | None = None,
    engine: dict | None = None,
) -> list[dict]:
    """逐行算出派生状态，并带上场景与行引用。

    `line list`、`line diff`、`tts plan`、`tts batch` 都从这里取状态，
    保证「筛选出来的行」与「计划重跑的行」是同一份判定结果。

    参数:
        chapter: 章节 dict。
        characters: `{角色 ID: 角色}`。
        snapshots: `{line_id: 侧车快照}`。
        root: 工程根目录；给出时会检查产物是否还在。
        live_ids: 当前队列里活跃的行 ID。
        engine: 当前配置导出的引擎侧参数。

    返回:
        `[{scene_id, scene_index, line, state, persisted, reason, changes}]`，保持章内顺序。
    """
    index = snapshots or {}
    rows: list[dict] = []
    for scene in chapter.get("scenes") or []:
        for line in scene.get("lines") or []:
            state = resolve_line_state(
                line, characters, index.get(line.get("id")), root=root, live_ids=live_ids, engine=engine
            )
            rows.append({"scene_id": scene.get("id"), "scene_index": scene.get("index"), "line": line, **state})
    return rows


def select_states(
    rows: list[dict],
    scope: str = "chapter",
    line_ids: list[str] | None = None,
    scene_ids: list[str] | None = None,
    statuses: tuple[str, ...] = (),
) -> list[dict]:
    """按生成范围与派生状态筛选行。

    参数:
        rows: `chapter_states` 的结果。
        scope: `selected`（配 `line_ids`）/ `pending`（固定三态）/ `scene`（配 `scene_ids`）/ `chapter`。
        line_ids: 选中的台词行 ID。
        scene_ids: 选中的场景 ID。
        statuses: 额外状态筛选；仅允许 `SELECTABLE_STATES` 内的派生状态。

    返回:
        命中的行记录；保持入参顺序。

    异常:
        ValueError: 范围缺参、行 ID 不存在、或 `scope=pending` 又给了 `--status`。
    """
    if scope not in SCOPES:
        raise ValueError(f"未知范围: {scope}（可选 {', '.join(SCOPES)}）")
    picked = rows
    if scope == "selected":
        wanted = list(dict.fromkeys(line_ids or []))
        if not wanted:
            raise ValueError("--scope selected 必须用 --line 指定至少一条台词行")
        known = {row["line"].get("id") for row in picked}
        missing = [item for item in wanted if item not in known]
        if missing:
            raise ValueError(f"台词行不存在: {', '.join(missing)}（用 line list 查看本章可用行 ID）")
        wanted_set = set(wanted)
        picked = [row for row in picked if row["line"].get("id") in wanted_set]
    elif scope == "scene":
        wanted_scenes = set(scene_ids or [])
        if not wanted_scenes:
            raise ValueError("--scope scene 必须用 --scene 指定至少一个场景")
        picked = [row for row in picked if row["scene_id"] in wanted_scenes]
    elif scope == "pending":
        if statuses:
            raise ValueError("--scope pending 与 --status 互斥：pending 已固定为 pending+stale+failed")
        statuses = PENDING_STATES
    if statuses:
        allowed = set(statuses)
        picked = [row for row in picked if row["state"] in allowed]
    return picked


def state_counts(rows: list[dict]) -> dict[str, int]:
    """统计各派生状态的行数。

    参数:
        rows: `chapter_states` 或 `select_states` 的结果。

    返回:
        `{状态: 行数}`；四个可筛选状态恒存在（无行时为 0），`skipped` 出现时才补键。
    """
    counts: dict[str, int] = {name: 0 for name in SELECTABLE_STATES}
    for row in rows:
        counts[row["state"]] = counts.get(row["state"], 0) + 1
    return counts


def engine_params(config) -> dict:
    """从配置里取出进指纹的引擎侧参数（切分 + 接缝裁切）。

    参数:
        config: AppConfig。

    返回:
        与侧车 `params` 同名的参数字典；改配置后据此派生 stale。
    """
    from .service import DEFAULT_CHUNK_CHARS, DEFAULT_CHUNK_SILENCE_MS

    seg = config.get("tts.segmentation") or {}
    edge_trim_ms = max(0, int(seg.get("edge_trim_ms", 100)))
    return {
        "chunk_limit": max(4, int(seg.get("max_chars") or DEFAULT_CHUNK_CHARS)),
        "chunk_silence_ms": max(0, int(seg.get("chunk_silence_ms") or DEFAULT_CHUNK_SILENCE_MS)),
        "edge_trim_ms": edge_trim_ms,
        "edge_trim_max_ms": max(edge_trim_ms, int(seg.get("edge_trim_max_ms", 400))),
        "keep_min_ms": max(0, int(seg.get("keep_min_ms", 30))),
        "silence_threshold_dbfs": float(seg.get("silence_threshold_dbfs", -45)),
        "hard_split_ratio": min(1.0, max(0.0, float(seg.get("hard_split_ratio", 0.5)))),
    }


def current_params(line: dict, snapshot_params: dict | None = None, engine: dict | None = None) -> dict:
    """用行上的当前值重建参数快照，用于与侧车快照比对。

    参数:
        line: 台词行。
        snapshot_params: 侧车里的 `params`，作为底稿。
        engine: 当前配置导出的引擎侧参数（`engine_params`），覆盖底稿同名字段。

    返回:
        参数字典：行级 5 个字段取自当前行，`instruct` 按当前行重新收敛，
        引擎侧字段以当前配置为准，其余（provider/model/音色）沿用底稿。
    """
    params = dict(snapshot_params or {})
    params.update(engine or {})
    for field in _LINE_FIELDS:
        params[field] = line.get(field)
    params["instruct"] = _instruct_for(line)
    return params


def _text_delta(old: str | None, new: str | None) -> str:
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


def _format_value(field: str, value) -> str:
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
                summary = f"文本 {_text_delta(old, new)}"
            else:
                label = FIELD_LABELS.get(field, field)
                summary = f"{label} {_format_value(field, old)} → {_format_value(field, new)}"
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


def resolve_line_state(
    line: dict,
    characters: dict[str, dict] | None = None,
    snapshot: dict | None = None,
    root=None,
    live_ids: set[str] | None = None,
    engine: dict | None = None,
    skip_kinds: tuple[str, ...] = (),
) -> dict:
    """计算一条台词的展示状态（七态模型）。

    参数:
        line: 台词行。
        characters: `{角色 ID: 角色}`；用于音色绑定变更判定。
        snapshot: 该行的侧车快照（`sidecar.snapshot` 的结果），没有则传 None。
        root: 工程根目录；给出时会检查音频产物是否还存在。
        live_ids: 当前队列里活跃的行 ID 集合；`None` 视为没有活跃批次。
        engine: 当前配置导出的引擎侧参数（`engine_params`）；改裁切参数后据此判 stale。
        skip_kinds: 不参与配音的行类型，默认空（配置项待后续阶段接入）。

    返回:
        `{"state", "persisted", "reason", "changes"}`：
        `state` 为七态之一，`persisted` 为落盘值，`changes` 为 `diff_params` 的结果。
    """
    tts = line.get("tts") or {}
    asked = tts.get("status") or "pending"
    text = (line.get("text") or "").strip()

    if not text or (skip_kinds and line.get("kind") in skip_kinds):
        return {"state": "skipped", "persisted": asked, "reason": "empty_text", "changes": []}

    if asked in ("queued", "running"):
        # 僵尸态降级：队列里已经没有这条了，说明上次进程被中断
        if live_ids and line.get("id") in live_ids:
            return {"state": asked, "persisted": asked, "reason": "active", "changes": []}
        return {"state": "pending", "persisted": asked, "reason": "zombie", "changes": []}

    if asked == "failed":
        return {"state": "failed", "persisted": asked, "reason": "last_failed", "changes": []}

    if asked != "ready":
        return {"state": "pending", "persisted": asked, "reason": "not_generated", "changes": []}

    asset = _asset_path(root, tts.get("asset_id"))
    if root is not None and asset is not None and not asset.exists():
        return {"state": "pending", "persisted": asked, "reason": "asset_missing", "changes": []}

    if snapshot is not None:
        current = current_params(line, snapshot.get("params"), engine)
        if line_fingerprint(current) != snapshot.get("fingerprint"):
            return {
                "state": "stale",
                "persisted": asked,
                "reason": "param_changed",
                "changes": diff_params(snapshot.get("params") or {}, current),
            }
    elif tts.get("synthesized_revision") != line.get("revision"):
        # 旧侧车（无指纹）走 revision 兜底，与改造前行为一致
        return {"state": "stale", "persisted": asked, "reason": "revision_changed", "changes": []}

    # 真人录音不依赖 TTS 音色/侧车参数：正文 revision 已在上方校验，直接算就绪。
    if tts.get("adapter") == "human":
        return {"state": "ready", "persisted": asked, "reason": "human_take", "changes": []}

    # 说话人是否变过：换了人就把音频标为过期，避免留着旧音色的音频还显示「就绪」。
    # 新侧车直接记 speaker_id；旧侧车没有该字段时，按音频音色反推归属者兜底。
    current_speaker = line.get("speaker_id")
    snapshot_speaker = (snapshot.get("params") or {}).get("speaker_id") if snapshot else None
    used_voice = tts.get("voice_id")
    if snapshot is not None and not snapshot_speaker and used_voice:
        owners = [
            character_id
            for character_id, character in (characters or {}).items()
            if ((character or {}).get("voice_binding") or {}).get("voice_id") == used_voice
        ]
        if owners and current_speaker not in owners:
            snapshot_speaker = owners[0]
    if snapshot_speaker and current_speaker != snapshot_speaker:
        before_name = entities.speaker_display_name(snapshot_speaker, characters or {})
        after_name = entities.speaker_display_name(current_speaker, characters or {})
        return {
            "state": "stale",
            "persisted": asked,
            "reason": "speaker_changed",
            "changes": [
                {
                    "group": "音色",
                    "field": "speaker_id",
                    "label": FIELD_LABELS["speaker_id"],
                    "from": snapshot_speaker,
                    "to": current_speaker,
                    "text": f"说话人 {before_name} → {after_name}",
                }
            ],
        }

    binding = ((characters or {}).get(line.get("speaker_id")) or {}).get("voice_binding") or {}
    if binding.get("voice_id") and tts.get("voice_id") != binding.get("voice_id"):
        return {
            "state": "stale",
            "persisted": asked,
            "reason": "voice_rebound",
            "changes": [
                {
                    "group": "音色",
                    "field": "voice_id",
                    "label": FIELD_LABELS["voice_id"],
                    "from": tts.get("voice_id"),
                    "to": binding.get("voice_id"),
                    "text": f"音色 {_format_value('voice_id', tts.get('voice_id'))} → "
                    f"{_format_value('voice_id', binding.get('voice_id'))}",
                }
            ],
        }

    return {"state": "ready", "persisted": asked, "reason": "matched", "changes": []}


def revert_line(
    store,
    chapter: dict,
    line: dict,
    snapshot: dict | None,
    characters=None,
    root=None,
    engine: dict | None = None,
) -> dict | None:
    """把参数类字段写回侧车快照（文本不回滚）。

    参数:
        store: ProjectStore。
        chapter: 章节 dict（会就地修改并保存）。
        line: 台词行。
        snapshot: 该行的侧车快照；为 None 时拒绝撤回（不基于猜测回滚）。
        characters: `{角色 ID: 角色}`，用于撤回后重新算状态。
        root: 工程根目录，用于重新算状态。
        engine: 当前配置导出的引擎侧参数，用于撤回后重新算状态。

    返回:
        `{"changed": [字段], "state", "text_changed"}`；无快照时返回 None。
    """
    if snapshot is None:
        return None
    params = snapshot.get("params") or {}
    changed: list[str] = []
    for field in REVERT_FIELDS:
        old = line.get(field)
        new = params.get(field)
        if normalize_value(old) != normalize_value(new):
            line[field] = new
            changed.append(field)
    if changed:
        # 撤回后指纹能重新对上，因此不需要把 tts.status 改回 pending：
        # revision 只承担并发锁职责，过期与否由读取路径按指纹派生
        line["revision"] = int(line.get("revision") or 1) + 1
        store.save_chapter(chapter)
    state = resolve_line_state(line, characters, snapshot, root=root, engine=engine)["state"]
    text_changed = normalize_value(line.get("text")) != normalize_value(params.get("text"))
    return {"changed": changed, "state": state, "text_changed": text_changed}
