"""场景与台词行 CLI：场景切分、台词切分、状态列表、diff / revert / set / 合并。

从 `cli.py` 拆出，纯搬迁：命令名、参数、输出格式、退出码一律未改动。
"""

from __future__ import annotations

import copy

from .cli_common import EXIT_OK, EXIT_REJECTED, _emit, _resolve_store, cast_hint
from .config import AppConfig
from .models import entities
from .models.ids import new_id
from .orchestration.pipeline import split_chapter_scenes, split_scene_lines
from .orchestration.resplit import analyze_line_split, analyze_scene_split
from .tts import line_state

# 正文与切分数据对不上时的统一提示：宁可拒绝写盘，也不静默改到错误位置
_CONTENT_MISMATCH = "正文已变更，请先重新切分"


def normalize_chapter_indexes(store, chapter: dict) -> bool:
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
        # 只重排序号，不动 updated_at 之外的任何字段；save_chapter 会校验 schema 并原子写盘
        store.save_chapter(chapter)
    return changed


def ordinal_map(chapter: dict) -> dict[str, tuple[int, int, int]]:
    """建立「行 ID → (全章序号, 场景序号, 场景内行号)」，三个序号均从 1 开始。

    参数:
        chapter: 章节 dict。

    返回:
        行 ID 到三段序号的映射；供台词表新增的展示列使用（不写入 schema）。
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


# ---------------- scene / line ----------------


def cmd_scene_split(args, config: AppConfig) -> int:
    """拆分场景；`--impact` 时只输出影响清单（不写盘）。

    参数:
        args: 参数（chapter/max-chars/force/dry-run/impact/keep-lines）。
        config: 本机配置。

    返回:
        退出码。
    """
    store = _resolve_store(args, config)
    chapter = store.load_chapter(args.chapter)
    keep_lines = bool(getattr(args, "keep_lines", False))
    if getattr(args, "impact", False):
        characters = store.load_characters()["characters"]
        report = analyze_scene_split(store, chapter, characters, max_chars=args.max_chars, keep_lines=keep_lines)
        _emit(args, report["text"], report)
        return EXIT_OK
    scenes = split_chapter_scenes(
        store,
        chapter,
        max_chars=args.max_chars,
        force=args.force,
        dry_run=args.dry_run,
        keep_lines=keep_lines,
    )
    payload = {"dry_run": args.dry_run, "scenes": scenes}
    if args.dry_run:
        text = "\n".join(f"{index + 1}. {item['title']}（{len(item['content'])} 字）" for index, item in enumerate(scenes)) or "正文为空，未产生场景"
    else:
        text = f"已拆分 {len(scenes)} 个场景（章节已保存）"
    _emit(args, text, payload)
    if not args.dry_run:
        # 切分完成后提示音色/BGM 编排问题，但不阻断切分结果落盘
        cast_hint(store)
    return EXIT_OK


def cmd_line_split(args, config: AppConfig) -> int:
    """切分台词行；`--impact` 时只输出影响清单（不写盘）。

    参数:
        args: 参数（chapter/scene/force/dry-run/impact/keep-manual）。
        config: 本机配置。

    返回:
        退出码。
    """
    store = _resolve_store(args, config)
    chapter = store.load_chapter(args.chapter)
    characters = store.load_characters()["characters"]
    keep_manual = bool(getattr(args, "keep_manual", False))
    if getattr(args, "impact", False):
        report = analyze_line_split(store, chapter, characters, scene_ids=args.scene, keep_manual=keep_manual)
        _emit(args, report["text"], report)
        return EXIT_OK
    result = split_scene_lines(
        store,
        chapter,
        characters,
        scene_ids=args.scene,
        force=args.force,
        dry_run=args.dry_run,
        keep_manual=keep_manual,
    )
    _emit(
        args,
        f"{'预览' if args.dry_run else '已切分'}：场景 {result['scenes']} 个，台词行 {result['lines']} 行，未识别说话人 {result['unknown']} 行",
        result,
    )
    if not args.dry_run:
        # 切分完成后提示音色/BGM 编排问题，但不阻断切分结果落盘
        cast_hint(store)
    return EXIT_OK


def cmd_line_list(args, config: AppConfig) -> int:
    """列出台词行（可按派生状态与行 ID 筛选）。

    状态一律按「产物 + 侧车快照」现算（七态模型），不使用落盘的 `tts.status` 直接展示，
    因此过期（stale）不需要写盘，僵尸 running 也会在这里降级成 pending。
    筛选与 `tts plan` 共用 `line_state` 的同一套判定，计数天然一致。
    """
    store = _resolve_store(args, config)
    chapter = store.load_chapter(args.chapter)
    # 历史数据自愈：按当前场景/行顺序重排 index（脏号只修一次，之后不再写盘）
    normalize_chapter_indexes(store, chapter)
    ordinals = ordinal_map(chapter)
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


def cmd_line_split_at(args, config: AppConfig) -> int:
    """在指定字符位置把一条台词拆成两行（对应参考的「光标处拆分」）。

    只改切分结构：两段文本拼起来等于原文本，正文不动，因此正文/切分仍然对位。
    原行保留前半段与已有音频（revision 递增后自动变「已过期」），新行没有配音。

    参数:
        args: 含 chapter、line、offset（0 基字符位置）。
        config: 本机配置。

    返回:
        退出码。

    异常:
        ValueError: 行不存在、位置不在台词中间、正文对不上。
    """
    store = _resolve_store(args, config)
    chapter = store.load_chapter(args.chapter)
    scene, line = _find_line(store, chapter, args.line)
    text = str(line.get("text") or "")
    offset = int(args.offset)
    if offset <= 0 or offset >= len(text):
        raise ValueError("拆分位置必须落在台词中间（不能是开头或结尾）")
    first_text = text[:offset].strip()
    second_text = text[offset:].strip()
    if not first_text or not second_text:
        raise ValueError("拆分后有一段为空，请换个位置")
    anchors, _ = _locate_lines(chapter.get("content") or "", _ordered_lines(chapter))
    if line["id"] not in anchors:
        raise ValueError(_CONTENT_MISMATCH)

    second = copy.deepcopy(line)
    second["id"] = new_id("lin")
    second["text"] = second_text
    second["revision"] = 1
    second["pause_before_ms"] = 0
    # 新行还没有任何音频，旧行保留自己的 tts（revision 变化后由状态机判为过期）
    second.pop("tts", None)
    second.pop("anchor", None)
    second.pop("target_duration_ms", None)
    second.pop("paralinguistic", None)

    line["text"] = first_text
    line["pause_after_ms"] = 0
    line["revision"] = int(line.get("revision") or 1) + 1
    line.pop("target_duration_ms", None)

    lines = list(scene.get("lines") or [])
    position = next(index for index, item in enumerate(lines) if item.get("id") == line["id"])
    lines.insert(position + 1, second)
    for index, item in enumerate(lines):
        item["index"] = index
        item["scene_id"] = scene.get("id")
    scene["lines"] = lines
    chapter["revision"] = int(chapter.get("revision") or 1) + 1
    store.save_chapter(chapter)
    _emit(
        args,
        f"已在第 {offset} 字处拆分 {line['id']} → {line['id']} + {second['id']}",
        {
            "changed": True,
            "line_id": line["id"],
            "new_line_id": second["id"],
            "first_text": first_text,
            "second_text": second_text,
            "chapter_revision": chapter["revision"],
        },
    )
    return EXIT_OK


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
    """修改台词行的说话人/情绪/强度/交付说明/语速/停顿/文本。

    支持 `--line` 单行与 `--lines a,b,c` 批量；批量时说话人变更会同步台词类型
    （旁白 ↔ 对白，内心独白保持不变），与章节页行内编辑保持一致。

    参数:
        args: 参数（chapter/line/lines/speaker/emotion/...）。
        config: 本机配置。

    返回:
        退出码。
    """
    store = _resolve_store(args, config)
    chapter = store.load_chapter(args.chapter)
    raw_lines = getattr(args, "lines", None)
    if raw_lines:
        targets = [item.strip() for item in str(raw_lines).split(",") if item.strip()]
    elif getattr(args, "line", None):
        targets = [args.line]
    else:
        raise ValueError("请用 --line 指定一行，或用 --lines 批量指定（逗号分隔）")
    if not targets:
        raise ValueError("--lines 里没有有效的行 ID")

    updates: dict = {}
    for field_name, attr in (
        ("speaker_id", "speaker"),
        ("emotion", "emotion"),
        ("text", "text"),
        ("kind", "kind"),
        ("delivery_note", "delivery_note"),
        ("speed", "speed"),
        ("pause_before_ms", "pause_before"),
        ("pause_after_ms", "pause_after"),
        ("emotion_intensity", "emotion_intensity"),
    ):
        value = getattr(args, attr, None)
        if value is not None:
            updates[field_name] = value

    updated: list[str] = []
    not_found: list[dict] = []
    last_line: dict | None = None
    for line_id in targets:
        try:
            _, line, _ = store.find_line(chapter, line_id)
        except (KeyError, ValueError) as error:
            message = error.args[0] if getattr(error, "args", None) else str(error)
            not_found.append({"line_id": line_id, "message": str(message)})
            continue
        changed = False
        for field_name, value in updates.items():
            if line.get(field_name) != value:
                line[field_name] = value
                changed = True
        # 说话人变更时同步类型：旁白 → narration，人物 → dialogue；内心独白保持不动
        if "speaker_id" in updates and "kind" not in updates and line.get("kind") in ("narration", "dialogue"):
            auto_kind = "narration" if updates["speaker_id"] == entities.NARRATOR_ID else "dialogue"
            if line.get("kind") != auto_kind:
                line["kind"] = auto_kind
                changed = True
        if changed:
            line["revision"] = int(line.get("revision") or 1) + 1
            # 不再把 tts.status 打回 pending：过期与否由读取时按参数指纹/说话人比对派生，
            # revision 只承担并发锁职责（见 docs/tts-line-state-and-seams.md §4.3）
            updated.append(line_id)
            last_line = line

    if not updated and not not_found:
        _emit(args, "没有字段变化", {"changed": False})
        return EXIT_OK
    if updated:
        store.save_chapter(chapter)

    if raw_lines:
        _emit(
            args,
            f"已批量更新 {len(updated)} 行"
            + (f"，{len(not_found)} 行不存在：{', '.join(item['line_id'] for item in not_found[:5])}" if not_found else ""),
            {
                "changed": bool(updated),
                "updated": updated,
                "not_found": not_found,
                "counts": {"updated": len(updated), "missing": len(not_found)},
            },
        )
        return EXIT_OK
    if last_line is None:
        _emit(args, "没有字段变化", {"changed": False})
        return EXIT_OK
    _emit(
        args,
        f"已更新 {last_line['id']}（revision={last_line['revision']}，读取时按参数指纹判定是否过期）",
        last_line,
    )
    return EXIT_OK


def _ordered_lines(chapter: dict) -> list[dict]:
    """按章节里场景与台词的出现顺序摊平所有台词行。

    参数:
        chapter: 章节 dict。

    返回:
        台词行列表，顺序即正文中的先后顺序。
    """
    return [line for scene in chapter.get("scenes") or [] for line in scene.get("lines") or []]


def _locate_lines(content: str, lines: list[dict], cursor: int = 0) -> tuple[dict[str, tuple[int, int]], list[str]]:
    """按顺序在正文里定位台词行（原文匹配 + 单调游标，不依赖任何落盘偏移）。

    台词行只存 scene_id/index/text，没有字符区间；切分产出的行文本都是正文原文片段，
    因此从游标处依次向后查找即可还原文中出现位置。

    参数:
        content: 章节正文。
        lines: 按出现顺序排列的台词行。
        cursor: 起始查找位置（用于只校验目标行之后的部分）。

    返回:
        `(锚点, 未命中的行 ID 列表)`；锚点为 `{line_id: (start, end)}`。
    """
    anchors: dict[str, tuple[int, int]] = {}
    missing: list[str] = []
    for line in lines:
        needle = (line.get("text") or "").strip()
        if not needle:
            continue
        start = content.find(needle, cursor)
        if start < 0:
            missing.append(line.get("id"))
            continue
        end = start + len(needle)
        anchors[line.get("id")] = (start, end)
        cursor = end
    return anchors, missing


def chapter_consistency(chapter: dict) -> dict:
    """检查章节正文与已切分台词是否仍能按顺序原样对位。

    与 `line edit` 的定位算法完全一致：任何一行在正文里找不到，就说明正文被改过而
    台词没有重新切分，此时「改台词同步正文」会失败。只读，不写盘。

    参数:
        chapter: 章节 dict。

    返回:
        {"ok": 是否一致, "chapter": 章节 ID, "chapter_title": 标题,
         "total": 有文本的台词行数, "missing_total": 对不上的行数, "missing": 前 20 个行 ID}。
    """
    lines = _ordered_lines(chapter)
    anchors, _ = _locate_lines(chapter.get("content") or "", lines)
    missing = [
        line.get("id")
        for line in lines
        if (line.get("text") or "").strip() and line.get("id") not in anchors
    ]
    return {
        "ok": not missing,
        "chapter": chapter.get("id"),
        "chapter_title": chapter.get("title") or "",
        "total": len(lines),
        "missing_total": len(missing),
        "missing": missing[:20],
    }


def cmd_line_edit(args, config: AppConfig) -> int:
    """就地编辑一条台词文本，并把同一处文本同步回章节正文。

    定位方式（不新增字段、不改切分）：按「场景 → 台词」顺序在正文里做单调游标原文匹配。
    保存前先用新正文重算一遍：目标行必须仍落在原位置，且前面与后面的行都能定位；
    任一行对不上就拒绝写盘，避免把正文改到错误位置。

    参数:
        args: 含 chapter、line、text。
        config: 本机配置。

    返回:
        退出码。

    异常:
        ValueError: 文本为空/含换行/超长，或正文与切分结果已不一致。
        KeyError: 台词行不存在。
    """
    store = _resolve_store(args, config)
    chapter = store.load_chapter(args.chapter)
    _, line, _ = store.find_line(chapter, args.line)

    new_text = str(args.text or "").strip()
    if not new_text:
        raise ValueError("台词内容不能为空")
    if "\n" in new_text or "\r" in new_text:
        raise ValueError("台词不能包含换行")
    if len(new_text) > 5000:
        raise ValueError("台词过长（最多 5000 字）")

    old_text = line.get("text") or ""
    if new_text == old_text.strip():
        _emit(
            args,
            f"{line['id']} 文本没有变化",
            {"changed": False, "line_id": line["id"], "text": old_text, "revision": line.get("revision")},
        )
        return EXIT_OK

    lines = _ordered_lines(chapter)
    anchors, missing = _locate_lines(chapter.get("content") or "", lines)
    if missing or line["id"] not in anchors:
        # 正文与切分数据已经对不上，拒绝写盘
        raise ValueError(_CONTENT_MISMATCH)

    start, end = anchors[line["id"]]
    updated = (chapter.get("content") or "")[:start] + new_text + (chapter.get("content") or "")[end:]

    # 校验：目标行之前的部分不受影响；目标行之后的行在新正文里仍能按序定位
    target_index = next(index for index, item in enumerate(lines) if item.get("id") == line["id"])
    _, prefix_missing = _locate_lines(updated[:start], lines[:target_index])
    _, suffix_missing = _locate_lines(updated, lines[target_index + 1:], cursor=start + len(new_text))
    if prefix_missing or suffix_missing:
        raise ValueError(_CONTENT_MISMATCH)

    line["text"] = new_text
    line["revision"] = int(line.get("revision") or 1) + 1
    chapter["content"] = updated
    chapter["word_count"] = sum(1 for char in updated if not char.isspace())
    chapter["revision"] = int(chapter.get("revision") or 1) + 1
    store.save_chapter(chapter)
    _emit(
        args,
        f"已更新 {line['id']}（revision={line['revision']}），正文已同步",
        {
            "changed": True,
            "line_id": line["id"],
            "text": new_text,
            "revision": line["revision"],
            "chapter_revision": chapter["revision"],
            "start": start,
            "end": start + len(new_text),
        },
    )
    return EXIT_OK


# ---------------- 合并相邻行 / 批量合并旁白 ----------------
# 合并只做结构收敛：新行文本取正文里「首行开头 → 末行结尾」的原文片段，
# 因此正文与切分仍然对位，不写正文、不删除任何音频文件。


def _ordered_group(store, chapter: dict, line_ids: list[str]) -> tuple[dict, list[dict]]:
    """把行 ID 解析成同一场景内按顺序排列的相邻台词行。

    参数:
        store: 工程。
        chapter: 章节 dict。
        line_ids: 待合并的行 ID 列表。

    返回:
        `(场景, 按 index 排序后的行列表)`。

    异常:
        ValueError: 行不存在、跨场景、有重复或不相邻。
    """
    scene_of: dict | None = None
    picked: list[dict] = []
    for line_id in line_ids:
        try:
            scene, line, _ = store.find_line(chapter, line_id)
        except KeyError as error:
            raise ValueError(error.args[0] if error.args else f"台词行不存在: {line_id}") from error
        if scene_of is None:
            scene_of = scene
        elif scene is not scene_of:
            raise ValueError("只能合并同一场景内的台词行")
        picked.append(line)
    picked.sort(key=lambda item: int(item.get("index") or 0))
    indexes = [int(item.get("index") or 0) for item in picked]
    if len(set(indexes)) != len(indexes):
        raise ValueError("合并目标里有重复行")
    if len(indexes) > 1 and indexes != list(range(indexes[0], indexes[0] + len(indexes))):
        raise ValueError("只能合并相邻的台词行")
    return scene_of, picked


def _merge_group(scene: dict, group: list[dict], content: str, anchors: dict) -> dict:
    """把一组相邻台词行合并成一行（就地改写 `scene["lines"]`）。

    参数:
        scene: 目标场景 dict。
        group: 按顺序排列、index 连续的台词行。
        content: 章节正文。
        anchors: `_locate_lines` 得到的行 ID → 正文字符区间。

    返回:
        合并后的台词行。

    异常:
        ValueError: 正文对不上、合并后为空或超长。
    """
    if any(line["id"] not in anchors for line in group):
        raise ValueError(_CONTENT_MISMATCH)
    start = anchors[group[0]["id"]][0]
    end = anchors[group[-1]["id"]][1]
    text = (content[start:end] or "").strip()
    if not text:
        raise ValueError("合并后的台词为空")
    if len(text) > 5000:
        raise ValueError("合并后的台词超过 5000 字，请减少合并行数")

    merged = dict(group[0])
    merged["text"] = text
    merged["pause_after_ms"] = group[-1].get("pause_after_ms", merged.get("pause_after_ms"))
    merged["revision"] = int(group[0].get("revision") or 1) + 1
    # 合并后覆盖多段正文，旧段落锚点与目标时长都不再准确，先清掉避免误导
    merged.pop("anchor", None)
    merged.pop("target_duration_ms", None)

    removed = {line["id"] for line in group[1:]}
    lines: list[dict] = []
    for line in scene.get("lines") or []:
        if line.get("id") == merged["id"]:
            lines.append(merged)
        elif line.get("id") in removed:
            continue
        else:
            lines.append(line)
    for index, line in enumerate(lines):
        line["index"] = index
        line["scene_id"] = scene.get("id")
    scene["lines"] = lines
    return merged


def _lost_audio(group: list[dict]) -> int:
    """统计被合并掉的台词行里已有配音的条数（文件保留，仅失去引用）。"""
    return sum(1 for line in group[1:] if (line.get("tts") or {}).get("asset_id"))


def cmd_line_merge(args, config: AppConfig) -> int:
    """合并同一场景内相邻的台词行（选中行 → 合并）。

    参数:
        args: 含 chapter、lines（逗号分隔的行 ID）。
        config: 本机配置。

    返回:
        退出码。

    异常:
        ValueError: 行不足两行、跨场景、不相邻或正文对不上。
    """
    store = _resolve_store(args, config)
    chapter = store.load_chapter(args.chapter)
    line_ids = [item.strip() for item in str(args.lines or "").split(",") if item.strip()]
    if len(line_ids) < 2:
        raise ValueError("至少选择两行才能合并")
    scene, group = _ordered_group(store, chapter, line_ids)
    anchors, _ = _locate_lines(chapter.get("content") or "", _ordered_lines(chapter))
    lost = _lost_audio(group)
    merged = _merge_group(scene, group, chapter.get("content") or "", anchors)
    chapter["revision"] = int(chapter.get("revision") or 1) + 1
    store.save_chapter(chapter)
    payload = {
        "changed": True,
        "merged": {
            "line_id": merged["id"],
            "text": merged["text"],
            "chars": len(merged["text"]),
            "removed": [line["id"] for line in group[1:]],
            "lost_audio": lost,
        },
        "revision": merged["revision"],
        "chapter_revision": chapter["revision"],
    }
    text = f"已合并 {len(group)} 行为 {merged['id']}（{len(merged['text'])} 字）"
    if lost:
        text += f"；{lost} 条已有配音失去引用（文件仍在磁盘）"
    _emit(args, text, payload)
    return EXIT_OK


def plan_narration_groups(chapter: dict, scene_ids: list[str] | None = None, max_chars: int = 120) -> list[tuple[dict, list[dict]]]:
    """按期望字数规划相邻旁白的合并分组（只读，不写盘）。

    只有「连续旁白」才会被合并；角色台词、内心独白、音效标记都会切断分组。
    单组至少两行才进入结果，避免出现「合并后没有变化」的无效操作。

    参数:
        chapter: 章节 dict。
        scene_ids: 只处理这些场景；为空表示全部。
        max_chars: 单组目标字数上限（达到上限就另起一组）。

    返回:
        `[(场景, 行列表)]`，组内行按 index 升序。
    """
    groups: list[tuple[dict, list[dict]]] = []
    limit = max(1, int(max_chars or 120))
    for scene in chapter.get("scenes") or []:
        if scene_ids and scene.get("id") not in scene_ids:
            continue
        current: list[dict] = []
        size = 0

        def flush() -> None:
            """收尾当前分组：至少两行才算一次合并。"""
            nonlocal current, size
            if len(current) >= 2:
                groups.append((scene, list(current)))
            current = []
            size = 0

        for line in sorted(scene.get("lines") or [], key=lambda item: int(item.get("index") or 0)):
            if line.get("kind") != "narration":
                flush()
                continue
            chars = len((line.get("text") or "").strip())
            if current and size + chars > limit:
                flush()
            current.append(line)
            size += chars
        flush()
    return groups


def _narration_preview(groups: list[tuple[dict, list[dict]]], chapter: dict) -> list[dict]:
    """构造批量合并旁白的预览（含合并后文本与预计时长）。"""
    anchors, _ = _locate_lines(chapter.get("content") or "", _ordered_lines(chapter))
    content = chapter.get("content") or ""
    rows = []
    for scene, group in groups:
        if group[0]["id"] in anchors and group[-1]["id"] in anchors:
            text = content[anchors[group[0]["id"]][0] : anchors[group[-1]["id"]][1]].strip()
        else:
            text = "".join((line.get("text") or "").strip() for line in group)
        duration = sum(int((line.get("tts") or {}).get("duration_ms") or 0) for line in group)
        rows.append(
            {
                "scene_id": scene.get("id"),
                "scene_title": scene.get("title") or "",
                "line_ids": [line["id"] for line in group],
                "text": text,
                "chars": len(text),
                "duration_ms": duration,
                "lost_audio": _lost_audio(group),
            }
        )
    return rows


def cmd_line_merge_narration(args, config: AppConfig) -> int:
    """按期望字数批量合并相邻旁白；`--dry-run` 只输出预览。

    参数:
        args: 含 chapter、scene（可重复）、max_chars、dry_run。
        config: 本机配置。

    返回:
        退出码。
    """
    store = _resolve_store(args, config)
    chapter = store.load_chapter(args.chapter)
    groups = plan_narration_groups(chapter, scene_ids=args.scene, max_chars=args.max_chars)
    preview = _narration_preview(groups, chapter)
    before = sum(len(group) for _, group in groups)
    counts = {"groups": len(groups), "lines_before": before, "lines_after": len(groups)}
    if args.dry_run:
        _emit(
            args,
            f"预览：{before} 段相邻旁白可合并为 {len(groups)} 段"
            if groups
            else "没有可合并的相邻旁白（需要同场景内连续两段以上旁白）",
            {"mode": "narration", "dry_run": True, "groups": preview, "counts": counts},
        )
        return EXIT_OK
    if not groups:
        _emit(args, "没有可合并的相邻旁白", {"changed": False, "groups": [], "counts": counts})
        return EXIT_OK

    anchors, _ = _locate_lines(chapter.get("content") or "", _ordered_lines(chapter))
    content = chapter.get("content") or ""
    lost = 0
    for scene, group in groups:
        lost += _lost_audio(group)
        _merge_group(scene, group, content, anchors)
    chapter["revision"] = int(chapter.get("revision") or 1) + 1
    store.save_chapter(chapter)
    _emit(
        args,
        f"已合并 {before} 段相邻旁白为 {len(groups)} 段"
        + (f"；{lost} 条已有配音失去引用（文件仍在磁盘）" if lost else ""),
        {
            "changed": True,
            "mode": "narration",
            "dry_run": False,
            "groups": preview,
            "counts": counts,
            "lost_audio": lost,
            "chapter_revision": chapter["revision"],
        },
    )
    return EXIT_OK
