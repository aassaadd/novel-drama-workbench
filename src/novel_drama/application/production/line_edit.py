"""台词行写操作：改参、就地编辑、合并、批量合并旁白、撤回、拆分与重切。

页面与 CLI 共用这一份实现：所有判定（正文是否对位、是否相邻、是否有参数快照）
都在这里完成，CLI 只负责解析参数与打印，接口只负责翻译异常与回传结果。
"""

from __future__ import annotations

import copy

from ...adapters.persistence import ProjectStore
from ...domain.story.rules.chapter_rules import bump_revision, count_words
from ...models import entities
from ...models.ids import new_id
from ...orchestration.pipeline import split_scene_lines
from ...orchestration.resplit import analyze_line_split
from ...tts import line_state
from .line_query import find_line
from .lines_source import chapter_view, save_lines

#: 正文与切分数据对不上时的统一提示：宁可拒绝写盘，也不静默改到错误位置
CONTENT_MISMATCH = "正文已变更，请先重新切分"


def ordered_lines(chapter: dict) -> list[dict]:
    """按章节里场景与台词的出现顺序摊平所有台词行。

    参数:
        chapter: 章节 dict。

    返回:
        台词行列表，顺序即正文中的先后顺序。
    """
    return [line for scene in chapter.get("scenes") or [] for line in scene.get("lines") or []]


def locate_lines(
    content: str, lines: list[dict], cursor: int = 0
) -> tuple[dict[str, tuple[int, int]], list[str]]:
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
    lines = ordered_lines(chapter)
    anchors, _ = locate_lines(chapter.get("content") or "", lines)
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


def set_lines(
    store: ProjectStore,
    chapter_id: str,
    line_ids: list[str],
    updates: dict,
    batch: bool,
) -> dict:
    """修改台词行的说话人/情绪/强度/交付说明/语速/停顿/文本。

    批量时说话人变更会同步台词类型（旁白 ↔ 对白，内心独白保持不变），
    与章节页行内编辑保持一致。

    参数:
        store: 工程。
        chapter_id: 章节 ID。
        line_ids: 要修改的行 ID（单行时长度为 1）。
        updates: `{字段名: 新值}`，只放实际给出的字段。
        batch: 是否为批量入口（决定返回结构与文案）。

    返回:
        `{"text": 终端文案, "payload": 结构化结果}`。

    异常:
        FileNotFoundError: 章节不存在。
    """
    chapter = chapter_view(store, store.load_chapter(chapter_id))
    updated: list[str] = []
    not_found: list[dict] = []
    last_line: dict | None = None
    for line_id in line_ids:
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
        return {"text": "没有字段变化", "payload": {"changed": False}}
    if updated:
        save_lines(store, chapter)

    if batch:
        text = f"已批量更新 {len(updated)} 行"
        if not_found:
            text += f"，{len(not_found)} 行不存在：{', '.join(item['line_id'] for item in not_found[:5])}"
        return {
            "text": text,
            "payload": {
                "changed": bool(updated),
                "updated": updated,
                "not_found": not_found,
                "counts": {"updated": len(updated), "missing": len(not_found)},
            },
        }
    if last_line is None:
        return {"text": "没有字段变化", "payload": {"changed": False}}
    return {
        "text": f"已更新 {last_line['id']}（revision={last_line['revision']}，读取时按参数指纹判定是否过期）",
        "payload": last_line,
    }


def edit_line_text(store: ProjectStore, chapter_id: str, line_id: str, text: str) -> dict:
    """就地编辑一条台词文本，并把同一处文本同步回章节正文。

    定位方式（不新增字段、不改切分）：按「场景 → 台词」顺序在正文里做单调游标原文匹配。
    保存前先用新正文重算一遍：目标行必须仍落在原位置，且前面与后面的行都能定位；
    任一行对不上就拒绝写盘，避免把正文改到错误位置。

    参数:
        store: 工程。
        chapter_id: 章节 ID。
        line_id: 台词行 ID。
        text: 新文本。

    返回:
        `{"text": 终端文案, "payload": 结构化结果}`。

    异常:
        ValueError: 文本为空/含换行/超长，或正文与切分结果已不一致。
        KeyError: 台词行不存在。
    """
    chapter = chapter_view(store, store.load_chapter(chapter_id))
    _, line, _ = store.find_line(chapter, line_id)

    new_text = str(text or "").strip()
    if not new_text:
        raise ValueError("台词内容不能为空")
    if "\n" in new_text or "\r" in new_text:
        raise ValueError("台词不能包含换行")
    if len(new_text) > 5000:
        raise ValueError("台词过长（最多 5000 字）")

    old_text = line.get("text") or ""
    if new_text == old_text.strip():
        return {
            "text": f"{line['id']} 文本没有变化",
            "payload": {
                "changed": False,
                "line_id": line["id"],
                "text": old_text,
                "revision": line.get("revision"),
            },
        }

    lines = ordered_lines(chapter)
    anchors, missing = locate_lines(chapter.get("content") or "", lines)
    if missing or line["id"] not in anchors:
        # 正文与切分数据已经对不上，拒绝写盘
        raise ValueError(CONTENT_MISMATCH)

    start, end = anchors[line["id"]]
    updated = (chapter.get("content") or "")[:start] + new_text + (chapter.get("content") or "")[end:]

    # 校验：目标行之前的部分不受影响；目标行之后的行在新正文里仍能按序定位
    target_index = next(index for index, item in enumerate(lines) if item.get("id") == line["id"])
    _, prefix_missing = locate_lines(updated[:start], lines[:target_index])
    _, suffix_missing = locate_lines(updated, lines[target_index + 1:], cursor=start + len(new_text))
    if prefix_missing or suffix_missing:
        raise ValueError(CONTENT_MISMATCH)

    line["text"] = new_text
    line["revision"] = int(line.get("revision") or 1) + 1
    chapter["content"] = updated
    chapter["word_count"] = count_words(updated)
    bump_revision(chapter)
    save_lines(store, chapter)
    return {
        "text": f"已更新 {line['id']}（revision={line['revision']}），正文已同步",
        "payload": {
            "changed": True,
            "line_id": line["id"],
            "text": new_text,
            "revision": line["revision"],
            "chapter_revision": chapter["revision"],
            "start": start,
            "end": start + len(new_text),
        },
    }


# ---------------- 合并相邻行 / 批量合并旁白 ----------------
# 合并只做结构收敛：新行文本取正文里「首行开头 → 末行结尾」的原文片段，
# 因此正文与切分仍然对位，不写正文、不删除任何音频文件。


def _ordered_group(store: ProjectStore, chapter: dict, line_ids: list[str]) -> tuple[dict, list[dict]]:
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
        anchors: `locate_lines` 得到的行 ID → 正文字符区间。

    返回:
        合并后的台词行。

    异常:
        ValueError: 正文对不上、合并后为空或超长。
    """
    if any(line["id"] not in anchors for line in group):
        raise ValueError(CONTENT_MISMATCH)
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


def merge_lines(store: ProjectStore, chapter_id: str, line_ids: list[str]) -> dict:
    """合并同一场景内相邻的台词行（选中行 → 合并）。

    参数:
        store: 工程。
        chapter_id: 章节 ID。
        line_ids: 待合并的行 ID 列表。

    返回:
        `{"text": 终端文案, "payload": 结构化结果}`。

    异常:
        ValueError: 行不足两行、跨场景、不相邻或正文对不上。
    """
    chapter = chapter_view(store, store.load_chapter(chapter_id))
    if len(line_ids) < 2:
        raise ValueError("至少选择两行才能合并")
    scene, group = _ordered_group(store, chapter, line_ids)
    anchors, _ = locate_lines(chapter.get("content") or "", ordered_lines(chapter))
    lost = _lost_audio(group)
    merged = _merge_group(scene, group, chapter.get("content") or "", anchors)
    bump_revision(chapter)
    save_lines(store, chapter)
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
    return {"text": text, "payload": payload}


def plan_narration_groups(
    chapter: dict, scene_ids: list[str] | None = None, max_chars: int = 120
) -> list[tuple[dict, list[dict]]]:
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

        def flush(scene: dict = scene) -> None:
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
    anchors, _ = locate_lines(chapter.get("content") or "", ordered_lines(chapter))
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


def merge_narrations(
    store: ProjectStore,
    chapter_id: str,
    scene_ids: list[str] | None = None,
    max_chars: int = 120,
    dry_run: bool = False,
) -> dict:
    """按期望字数批量合并相邻旁白；`dry_run` 只输出预览。

    参数:
        store: 工程。
        chapter_id: 章节 ID。
        scene_ids: 只处理这些场景；为空表示全部。
        max_chars: 单组目标字数上限。
        dry_run: 为 True 时只返回预览，不写盘。

    返回:
        `{"text": 终端文案, "payload": 结构化结果}`。
    """
    chapter = chapter_view(store, store.load_chapter(chapter_id))
    groups = plan_narration_groups(chapter, scene_ids=scene_ids, max_chars=max_chars)
    preview = _narration_preview(groups, chapter)
    before = sum(len(group) for _, group in groups)
    counts = {"groups": len(groups), "lines_before": before, "lines_after": len(groups)}
    if dry_run:
        text = (
            f"预览：{before} 段相邻旁白可合并为 {len(groups)} 段"
            if groups
            else "没有可合并的相邻旁白（需要同场景内连续两段以上旁白）"
        )
        return {
            "text": text,
            "payload": {"mode": "narration", "dry_run": True, "groups": preview, "counts": counts},
        }
    if not groups:
        return {
            "text": "没有可合并的相邻旁白",
            "payload": {"changed": False, "groups": [], "counts": counts},
        }

    anchors, _ = locate_lines(chapter.get("content") or "", ordered_lines(chapter))
    content = chapter.get("content") or ""
    lost = 0
    for scene, group in groups:
        lost += _lost_audio(group)
        _merge_group(scene, group, content, anchors)
    bump_revision(chapter)
    save_lines(store, chapter)
    text = f"已合并 {before} 段相邻旁白为 {len(groups)} 段"
    if lost:
        text += f"；{lost} 条已有配音失去引用（文件仍在磁盘）"
    return {
        "text": text,
        "payload": {
            "changed": True,
            "mode": "narration",
            "dry_run": False,
            "groups": preview,
            "counts": counts,
            "lost_audio": lost,
            "chapter_revision": chapter["revision"],
        },
    }


def revert_line_params(store: ProjectStore, chapter_id: str, line_id: str, config) -> dict:
    """把一条台词的情绪/强度/语速/交付说明回滚到上次生成时的值（文本不回滚）。

    参数:
        store: 工程。
        chapter_id: 章节 ID。
        line_id: 台词行 ID。
        config: 本机配置（取引擎参数）。

    返回:
        `{"text": 终端文案, "payload": 结构化结果, "snapshot_available": 是否有快照}`；
        没有快照时 `snapshot_available=False`，调用方按「拒绝执行」处理。
    """
    chapter = chapter_view(store, store.load_chapter(chapter_id))
    _, line = find_line(store, chapter, line_id)
    snapshot = line_state.snapshot_for_line(store.root, line)
    if snapshot is None:
        return {
            "text": f"{line_id} 没有参数快照，无法撤回；先执行 tts run 生成一次",
            "snapshot_available": False,
            "payload": {"line_id": line_id, "snapshot_available": False, "changed": []},
        }
    characters = {item["id"]: item for item in store.load_characters()["characters"]}
    engine = line_state.engine_params(config)
    result = line_state.revert_line(
        store, chapter, line, snapshot, characters=characters, root=store.root, engine=engine
    )
    payload = {"line_id": line["id"], "snapshot_available": True, **(result or {})}
    if not result or not result["changed"]:
        return {
            "text": f"{line['id']} 当前参数已与快照一致，无需撤回（state={payload.get('state')}）",
            "snapshot_available": True,
            "payload": payload,
        }
    text = f"已撤回 {line['id']}：{', '.join(result['changed'])} → 上次生成值；当前状态 {result['state']}"
    if result["text_changed"]:
        text += "\n注意：文本与快照不同，按约定不回滚（文本属于创作成果）"
    return {"text": text, "snapshot_available": True, "payload": payload}


def split_line_at(store: ProjectStore, chapter_id: str, line_id: str, offset: int) -> dict:
    """在指定字符位置把一条台词拆成两行（对应参考的「光标处拆分」）。

    只改切分结构：两段文本拼起来等于原文本，正文不动，因此正文/切分仍然对位。
    原行保留前半段与已有音频（revision 递增后自动变「已过期」），新行没有配音。

    参数:
        store: 工程。
        chapter_id: 章节 ID。
        line_id: 台词行 ID。
        offset: 0 基字符位置。

    返回:
        `{"text": 终端文案, "payload": 结构化结果}`。

    异常:
        ValueError: 行不存在、位置不在台词中间、正文对不上。
    """
    chapter = chapter_view(store, store.load_chapter(chapter_id))
    scene, line = find_line(store, chapter, line_id)
    text = str(line.get("text") or "")
    offset = int(offset)
    if offset <= 0 or offset >= len(text):
        raise ValueError("拆分位置必须落在台词中间（不能是开头或结尾）")
    first_text = text[:offset].strip()
    second_text = text[offset:].strip()
    if not first_text or not second_text:
        raise ValueError("拆分后有一段为空，请换个位置")
    anchors, _ = locate_lines(chapter.get("content") or "", ordered_lines(chapter))
    if line["id"] not in anchors:
        raise ValueError(CONTENT_MISMATCH)

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
    bump_revision(chapter)
    save_lines(store, chapter)
    return {
        "text": f"已在第 {offset} 字处拆分 {line['id']} → {line['id']} + {second['id']}",
        "payload": {
            "changed": True,
            "line_id": line["id"],
            "new_line_id": second["id"],
            "first_text": first_text,
            "second_text": second_text,
            "chapter_revision": chapter["revision"],
        },
    }


def split_lines(
    store: ProjectStore,
    chapter_id: str,
    scene_ids: list[str] | None = None,
    force: bool = False,
    dry_run: bool = False,
    keep_manual: bool = False,
) -> dict:
    """重新切分章节台词行。

    参数:
        store: 工程。
        chapter_id: 章节 ID。
        scene_ids: 只切分这些场景；为空表示全部。
        force: 覆盖已有切分结果。
        dry_run: 只预览，不写盘。
        keep_manual: 保留人工改过/新增的台词行。

    返回:
        `{"text": 终端文案, "payload": 结构化结果}`。
    """
    chapter = chapter_view(store, store.load_chapter(chapter_id))
    characters = store.load_characters()["characters"]
    result = split_scene_lines(
        store,
        chapter,
        characters,
        scene_ids=scene_ids,
        force=force,
        dry_run=dry_run,
        keep_manual=keep_manual,
    )
    text = (
        f"{'预览' if dry_run else '已切分'}：场景 {result['scenes']} 个，台词行 {result['lines']} 行，"
        f"未识别说话人 {result['unknown']} 行"
    )
    return {"text": text, "payload": result}


def line_split_impact(
    store: ProjectStore,
    chapter_id: str,
    scene_ids: list[str] | None = None,
    keep_manual: bool = False,
) -> dict:
    """重切台词的影响清单（只读，不写盘）。

    参数:
        store: 工程。
        chapter_id: 章节 ID。
        scene_ids: 只检查这些场景；为空表示全部。
        keep_manual: 是否按「保留人工修改」口径评估。

    返回:
        `{"text": 终端文案, "payload": 影响清单}`。
    """
    chapter = chapter_view(store, store.load_chapter(chapter_id))
    characters = store.load_characters()["characters"]
    report = analyze_line_split(store, chapter, characters, scene_ids=scene_ids, keep_manual=keep_manual)
    return {"text": report["text"], "payload": report}


def replace_line_texts(store: ProjectStore, chapter_id: str, find: str, replacement: str) -> dict:
    """在本章台词里做查找替换（逐行同步正文），返回替换统计。

    参数:
        store: 工程。
        chapter_id: 章节 ID。
        find: 待查找文本（非空）。
        replacement: 替换成的新文本。

    返回:
        `{"replaced": 成功数, "failed": 失败数, "total_matched": 命中数}`。

    异常:
        FileNotFoundError: 章节不存在。
    """
    payload = store.load_chapter(chapter_id)
    replaced = 0
    failed = 0
    matched = 0
    for scene in payload.get("scenes") or []:
        for line in scene.get("lines") or []:
            text = str(line.get("text") or "")
            if find not in text:
                continue
            matched += 1
            new_text = text.replace(find, replacement)
            try:
                edit_line_text(store, chapter_id, line.get("id"), new_text)
            except (FileNotFoundError, KeyError, ValueError):
                # 与旧实现一致：单行失败只计数，继续处理其余命中行
                failed += 1
                continue
            replaced += 1
    return {"replaced": replaced, "failed": failed, "total_matched": matched}
