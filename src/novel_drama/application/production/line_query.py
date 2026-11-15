"""台词表读取：派生状态、展示序号与单行差异。

状态一律按「产物 + 侧车快照」现算（七态模型），不直接展示落盘的 `tts.status`；
页面与 CLI 共用这一份，保证两边看到的行数、状态、变更项完全一致。
"""

from __future__ import annotations

from ...adapters.persistence import ProjectStore
from ...models import entities
from ...tts import line_state


def normalize_chapter_indexes(store: ProjectStore, chapter: dict) -> bool:
    """把场景/台词的 index 按当前顺序重排为连续序号，必要时回写一次。

    `index` 的语义不变：场景 index 是章内场景序号（0 基），台词 index 是场景内行号（0 基）。
    这里只修负数、重复、跳号这类历史脏数据；不改 line_id、不改 tts/音频引用，
    因此已配音行不会失联，重编号也不会触发重新合成。

    参数:
        store: 工程（用于回写）。
        chapter: 章节 dict（就地修改）。

    返回:
        是否发生了改动并已回写。
    """
    changed = False
    for scene_no, scene in enumerate(chapter.get("scenes") or []):
        if scene.get("index") != scene_no:
            scene["index"] = scene_no
            changed = True
        for line_no, line in enumerate(scene.get("lines") or []):
            if line.get("index") != line_no:
                line["index"] = line_no
                changed = True
    if changed:
        # 只重排序号，不改其它字段；save_chapter 会校验 schema 并原子写盘
        store.save_chapter(chapter)
    return changed


def ordinal_map(chapter: dict) -> dict[str, tuple[int, int, int]]:
    """建立「行 ID → (全章序号, 场景序号, 场景内行号)」，三个序号均从 1 开始。

    参数:
        chapter: 章节 dict。

    返回:
        行 ID 到三段序号的映射；供台词表展示列使用（不落盘、不进 schema）。
    """
    mapping: dict[str, tuple[int, int, int]] = {}
    ordinal = 0
    for scene_no, scene in enumerate(chapter.get("scenes") or [], start=1):
        for line_no, line in enumerate(scene.get("lines") or [], start=1):
            line_id = line.get("id")
            if not line_id:
                continue
            ordinal += 1
            mapping[str(line_id)] = (ordinal, scene_no, line_no)
    return mapping


def find_line(store: ProjectStore, chapter: dict, line_id: str) -> tuple[dict, dict]:
    """按行 ID 定位台词行。

    参数:
        store: 工程。
        chapter: 章节 dict。
        line_id: 台词行 ID。

    返回:
        `(scene, line)`。

    异常:
        ValueError: 行 ID 不在本章内。
    """
    for scene in chapter.get("scenes") or []:
        for line in scene.get("lines") or []:
            if line.get("id") == line_id:
                return scene, line
    raise ValueError(
        f"台词行不存在: {line_id}（用 line list --chapter {chapter['id']} 查看可用行 ID）"
    )


def chapter_lines(
    store: ProjectStore,
    chapter_id: str,
    config,
    statuses_raw: str | None = None,
    line_ids: list[str] | None = None,
) -> dict:
    """列出台词行（可按派生状态与行 ID 筛选）。

    参数:
        store: 工程。
        chapter_id: 章节 ID。
        config: 本机配置（取引擎参数）。
        statuses_raw: 逗号分隔的状态筛选，如 `pending,stale,unknown`。
        line_ids: 只看指定行。

    返回:
        `{"chapter": 章节 ID, "counts": 状态计数, "lines": 行列表}`。

    异常:
        FileNotFoundError: 章节不存在。
    """
    chapter = store.load_chapter(chapter_id)
    normalize_chapter_indexes(store, chapter)
    ordinals = ordinal_map(chapter)
    characters = {item["id"]: item for item in store.load_characters()["characters"]}
    snapshots = line_state.load_snapshots(store.root, chapter)
    live_ids = line_state.live_line_ids(store)
    engine = line_state.engine_params(config)
    statuses = line_state.parse_states(statuses_raw)
    only_unknown = "unknown" in statuses
    states = line_state.chapter_states(
        chapter, characters, snapshots, root=store.root, live_ids=live_ids, engine=engine
    )
    picked = line_state.select_states(
        states,
        scope="selected" if line_ids else "chapter",
        line_ids=line_ids,
        statuses=tuple(name for name in statuses if name != "unknown"),
    )
    if only_unknown:
        picked = [
            row for row in picked if row["line"].get("speaker_id") == entities.UNKNOWN_SPEAKER_ID
        ]
    rows = []
    for row in picked:
        line = row["line"]
        ordinal, scene_ordinal, scene_line_ordinal = ordinals.get(str(line.get("id")), (0, 0, 0))
        rows.append(
            {
                "scene_id": row["scene_id"],
                "line_id": line["id"],
                "index": line.get("index"),
                # 展示用派生字段：全章连续序号与「第 N 场 · 第 M 行」，不落盘、不进 schema
                "ordinal": ordinal,
                "scene_ordinal": scene_ordinal,
                "scene_line_ordinal": scene_line_ordinal,
                "kind": line.get("kind"),
                "speaker": entities.speaker_display_name(line.get("speaker_id"), characters),
                "speaker_id": line.get("speaker_id"),
                "text": line.get("text"),
                "emotion": line.get("emotion"),
                "emotion_intensity": line.get("emotion_intensity"),
                "speed": line.get("speed"),
                "pause_before_ms": line.get("pause_before_ms"),
                "pause_after_ms": line.get("pause_after_ms"),
                "delivery_note": line.get("delivery_note"),
                "state": row["state"],
                "state_reason": row["reason"],
                "tts_status": row["persisted"],
                "changes": row["changes"],
                "duration_ms": (line.get("tts") or {}).get("duration_ms"),
                # 上次失败的结构化原因（code/message/at），供行内直接展示与重试
                "error": (line.get("tts") or {}).get("error"),
                # 已生成音频的相对路径，供页面直接试听
                "audio": (line.get("tts") or {}).get("asset_id"),
            }
        )
    return {"chapter": chapter["id"], "counts": line_state.state_counts(picked), "lines": rows}


def line_diff(store: ProjectStore, chapter_id: str, line_id: str, config) -> dict:
    """单行参数差异（相对上次生成时的快照）。

    参数:
        store: 工程。
        chapter_id: 章节 ID。
        line_id: 台词行 ID。
        config: 本机配置。

    返回:
        `{"line_id", "snapshot_available", "state"?, "state_reason"?, "changes"}`。
        没有快照时 `snapshot_available=False`、`changes=[]`。

    异常:
        FileNotFoundError: 章节不存在。
        ValueError: 行 ID 不在本章内。
    """
    chapter = store.load_chapter(chapter_id)
    _, line = find_line(store, chapter, line_id)
    snapshot = line_state.snapshot_for_line(store.root, line)
    if snapshot is None:
        return {"line_id": line_id, "snapshot_available": False, "changes": []}
    characters = {item["id"]: item for item in store.load_characters()["characters"]}
    live_ids = line_state.live_line_ids(store)
    engine = line_state.engine_params(config)
    state = line_state.resolve_line_state(
        line, characters, snapshot, root=store.root, live_ids=live_ids, engine=engine
    )
    return {
        "line_id": line["id"],
        "snapshot_available": True,
        "state": state["state"],
        "state_reason": state["reason"],
        "changes": state["changes"],
    }
