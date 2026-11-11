"""场景与台词行 CLI：场景切分、台词切分、状态列表、diff / revert / set。

从 `cli.py` 拆出，纯搬迁：命令名、参数、输出格式、退出码一律未改动。
"""

from __future__ import annotations

from .cli_common import EXIT_OK, EXIT_REJECTED, _emit, _resolve_store
from .config import AppConfig
from .models import entities
from .orchestration.pipeline import split_chapter_scenes, split_scene_lines
from .tts import line_state


# ---------------- scene / line ----------------


def cmd_scene_split(args, config: AppConfig) -> int:
    """拆分场景。"""
    store = _resolve_store(args, config)
    chapter = store.load_chapter(args.chapter)
    scenes = split_chapter_scenes(store, chapter, max_chars=args.max_chars, force=args.force, dry_run=args.dry_run)
    payload = {"dry_run": args.dry_run, "scenes": scenes}
    if args.dry_run:
        text = "\n".join(f"{index + 1}. {item['title']}（{len(item['content'])} 字）" for index, item in enumerate(scenes)) or "正文为空，未产生场景"
    else:
        text = f"已拆分 {len(scenes)} 个场景（章节已保存）"
    _emit(args, text, payload)
    return EXIT_OK


def cmd_line_split(args, config: AppConfig) -> int:
    """切分台词行。"""
    store = _resolve_store(args, config)
    chapter = store.load_chapter(args.chapter)
    characters = store.load_characters()["characters"]
    result = split_scene_lines(store, chapter, characters, scene_ids=args.scene, force=args.force, dry_run=args.dry_run)
    _emit(
        args,
        f"{'预览' if args.dry_run else '已切分'}：场景 {result['scenes']} 个，台词行 {result['lines']} 行，未识别说话人 {result['unknown']} 行",
        result,
    )
    return EXIT_OK


def cmd_line_list(args, config: AppConfig) -> int:
    """列出台词行（可按派生状态与行 ID 筛选）。

    状态一律按「产物 + 侧车快照」现算（七态模型），不使用落盘的 `tts.status` 直接展示，
    因此过期（stale）不需要写盘，僵尸 running 也会在这里降级成 pending。
    筛选与 `tts plan` 共用 `line_state` 的同一套判定，计数天然一致。
    """
    store = _resolve_store(args, config)
    chapter = store.load_chapter(args.chapter)
    characters = {item["id"]: item for item in store.load_characters()["characters"]}
    snapshots = line_state.load_snapshots(store.root, chapter)
    live_ids = line_state.live_line_ids(store)
    engine = line_state.engine_params(config)
    statuses = line_state.parse_states(args.status)
    only_unknown = "unknown" in statuses
    states = line_state.chapter_states(
        chapter, characters, snapshots, root=store.root, live_ids=live_ids, engine=engine
    )
    picked = line_state.select_states(
        states,
        scope="selected" if args.line else "chapter",
        line_ids=args.line,
        statuses=tuple(name for name in statuses if name != "unknown"),
    )
    if only_unknown:
        picked = [row for row in picked if row["line"].get("speaker_id") == entities.UNKNOWN_SPEAKER_ID]
    rows = []
    for row in picked:
        line = row["line"]
        rows.append(
            {
                "scene_id": row["scene_id"],
                "line_id": line["id"],
                "index": line.get("index"),
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
                # 已生成音频的相对路径，供页面直接试听
                "audio": (line.get("tts") or {}).get("asset_id"),
            }
        )
    if getattr(args, "json", False):
        _emit(args, payload={"chapter": chapter["id"], "counts": line_state.state_counts(picked), "lines": rows})
        return EXIT_OK
    if not rows:
        print("没有匹配的台词行")
    for row in rows:
        summary = "；".join(item["text"] for item in row["changes"][:5])
        if len(row["changes"]) > 5:
            summary += f"；等 {len(row['changes'])} 项"
        tail = f"  {summary}" if summary else ""
        print(
            f"{row['line_id']}  {row['state']:<8} {row['kind']:<15} {row['speaker']:<8} "
            f"{row['emotion']:<8} {(row['text'] or '')[:24]}{tail}"
        )
    return EXIT_OK


def _find_line(store, chapter: dict, line_id: str):
    """按行 ID 定位台词行，找不到时给出可执行提示。

    参数:
        store: 工程。
        chapter: 章节 dict。
        line_id: 台词行 ID。

    返回:
        `(scene, line)`。

    异常:
        ValueError: 行 ID 不在本章内（CLI 转成退出码 2）。
    """
    for scene in chapter.get("scenes") or []:
        for line in scene.get("lines") or []:
            if line.get("id") == line_id:
                return scene, line
    raise ValueError(f"台词行不存在: {line_id}（用 line list --chapter {chapter['id']} 查看可用行 ID）")


def cmd_line_diff(args, config: AppConfig) -> int:
    """查看一条台词相对上次生成参数改了哪些项。

    没有侧车快照时拒绝输出差异（不猜），返回退出码 1。
    """
    store = _resolve_store(args, config)
    chapter = store.load_chapter(args.chapter)
    _, line = _find_line(store, chapter, args.line)
    snapshot = line_state.snapshot_for_line(store.root, line)
    if snapshot is None:
        _emit(
            args,
            f"{args.line} 没有参数快照（未生成过或侧车是旧版），无法比对差异；先执行 tts run 生成一次",
            {"line_id": args.line, "snapshot_available": False, "changes": []},
        )
        return EXIT_REJECTED
    characters = {item["id"]: item for item in store.load_characters()["characters"]}
    live_ids = line_state.live_line_ids(store)
    engine = line_state.engine_params(config)
    state = line_state.resolve_line_state(
        line, characters, snapshot, root=store.root, live_ids=live_ids, engine=engine
    )
    payload = {
        "line_id": line["id"],
        "snapshot_available": True,
        "state": state["state"],
        "state_reason": state["reason"],
        "changes": state["changes"],
    }
    if not state["changes"]:
        _emit(args, f"{line['id']} 与上次生成参数一致（{state['state']}）", payload)
        return EXIT_OK
    text = f"{line['id']}  state={state['state']}  共 {len(state['changes'])} 项变更"
    for item in state["changes"]:
        text += f"\n  [{item['group']}] {item['text']}"
    _emit(args, text, payload)
    return EXIT_OK


def cmd_line_revert(args, config: AppConfig) -> int:
    """把一条台词的情绪/强度/语速/交付说明回滚到上次生成时的值（文本不回滚）。

    无侧车快照时拒绝撤回并返回退出码 1：不基于猜测回滚。
    """
    store = _resolve_store(args, config)
    chapter = store.load_chapter(args.chapter)
    _, line = _find_line(store, chapter, args.line)
    snapshot = line_state.snapshot_for_line(store.root, line)
    if snapshot is None:
        _emit(
            args,
            f"{args.line} 没有参数快照，无法撤回；先执行 tts run 生成一次",
            {"line_id": args.line, "snapshot_available": False, "changed": []},
        )
        return EXIT_REJECTED
    characters = {item["id"]: item for item in store.load_characters()["characters"]}
    engine = line_state.engine_params(config)
    result = line_state.revert_line(
        store, chapter, line, snapshot, characters=characters, root=store.root, engine=engine
    )
    payload = {"line_id": line["id"], "snapshot_available": True, **(result or {})}
    if not result or not result["changed"]:
        _emit(args, f"{line['id']} 当前参数已与快照一致，无需撤回（state={payload.get('state')}）", payload)
        return EXIT_OK
    text = f"已撤回 {line['id']}：{', '.join(result['changed'])} → 上次生成值；当前状态 {result['state']}"
    if result["text_changed"]:
        text += "\n注意：文本与快照不同，按约定不回滚（文本属于创作成果）"
    _emit(args, text, payload)
    return EXIT_OK


def cmd_line_set(args, config: AppConfig) -> int:
    """修改台词行的说话人/情绪/强度/交付说明/语速/停顿/文本。"""
    store = _resolve_store(args, config)
    chapter = store.load_chapter(args.chapter)
    _, line, _ = store.find_line(chapter, args.line)
    changed = False
    for field_name, attr in (
        ("speaker_id", "speaker"),
        ("emotion", "emotion"),
        ("text", "text"),
        ("kind", "kind"),
        ("delivery_note", "delivery_note"),
    ):
        value = getattr(args, attr, None)
        if value is not None and line.get(field_name) != value:
            line[field_name] = value
            changed = True
    for field_name, attr in (
        ("speed", "speed"),
        ("pause_before_ms", "pause_before"),
        ("pause_after_ms", "pause_after"),
        ("emotion_intensity", "emotion_intensity"),
    ):
        value = getattr(args, attr, None)
        if value is not None and line.get(field_name) != value:
            line[field_name] = value
            changed = True
    if not changed:
        _emit(args, "没有字段变化", {"changed": False})
        return EXIT_OK
    line["revision"] = int(line.get("revision") or 1) + 1
    # 不再把 tts.status 打回 pending：过期与否由读取时按参数指纹派生，
    # revision 只承担并发锁职责（见 docs/tts-line-state-and-seams.md §4.3）
    store.save_chapter(chapter)
    _emit(args, f"已更新 {line['id']}（revision={line['revision']}，读取时按参数指纹判定是否过期）", line)
    return EXIT_OK
