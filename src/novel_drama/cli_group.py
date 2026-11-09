"""虚拟分组 CLI：list / diff / apply。

分组存 `.ndw/groups/<chapter>.json`，**不进章节 JSON**（见 docs §8.2）。
`apply` 只把参数物化写入行，是否过期由读取时按参数指纹派生，不直接改 `tts.status`。
"""

from __future__ import annotations

import copy

from .cli_common import EXIT_OK, _emit, _resolve_store
from .store.groups_store import (
    GroupsStore,
    apply_group,
    build_diff,
    covering_groups,
    detect,
)
from .tts import line_state
from .tts.fingerprint import FP_FIELDS


def _derive_states(store, config, chapter: dict) -> dict[str, str]:
    """按当前产物与侧车算一遍逐行派生状态。

    参数:
        store: 工程。
        config: 本机配置。
        chapter: 章节 dict。

    返回:
        `{line_id: 派生状态}`。
    """
    characters = {item["id"]: item for item in store.load_characters()["characters"]}
    snapshots = line_state.load_snapshots(store.root, chapter)
    rows = line_state.chapter_states(
        chapter,
        characters,
        snapshots,
        root=store.root,
        live_ids=line_state.live_line_ids(store),
        engine=line_state.engine_params(config),
    )
    return {row["line"]["id"]: row["state"] for row in rows}


def _query_summary(query: dict) -> str:
    """把查询条件压成一行可读文本。

    参数:
        query: 分组查询条件。

    返回:
        形如 `speaker_ids=chr_li, kinds=dialogue` 的摘要；无条件是「整章」。
    """
    parts = []
    for key, value in (query or {}).items():
        values = value if isinstance(value, list) else [value]
        if values:
            parts.append(f"{key}={'/'.join(str(item) for item in values)}")
    return "，".join(parts) or "整章"


def _override_summary(overrides: dict) -> str:
    """把覆盖字段压成一行可读文本。

    参数:
        overrides: 字段 → 目标值。

    返回:
        形如 `emotion=sad, speed=0.95` 的摘要。
    """
    return "，".join(f"{key}={value}" for key, value in (overrides or {}).items()) or "（无覆盖字段）"


def _field_text(fields: dict) -> str:
    """渲染一条「旧值 → 新值」变更文本。

    参数:
        fields: `{字段: {"from", "to"}}`。

    返回:
        以「；」连接的变更描述。
    """
    return "；".join(f"{name} {item['from']} → {item['to']}" for name, item in fields.items())


def cmd_group_list(args, config) -> int:
    """列出本章分组，并报告未分组与重复分组的行。

    参数:
        args: 含 chapter。
        config: 本机配置。

    返回:
        退出码 0。
    """
    store = _resolve_store(args, config)
    chapter = store.load_chapter(args.chapter)
    data = GroupsStore(store.root).load(chapter["id"])
    groups = data["groups"]
    report = detect(chapter, groups)
    rows = [
        {
            "id": group.get("id"),
            "name": group.get("name"),
            "query": group.get("query") or {},
            "overrides": group.get("overrides") or {},
            "applied_lines": len((group.get("applied") or {}).get("lines") or []),
        }
        for group in groups
    ]
    payload = {"chapter": chapter["id"], "groups": rows, "detect": report}
    if getattr(args, "json", False):
        _emit(args, payload=payload)
        return EXIT_OK
    if not rows:
        print(f"{chapter['id']} 暂无分组（{GroupsStore(store.root).path(chapter['id'])} 不存在即「无分组」）")
    for row in rows:
        applied = f"  已覆盖 {row['applied_lines']} 行" if row["applied_lines"] else ""
        print(f"{row['id']}  {row['name']}  [{_query_summary(row['query'])}] → {_override_summary(row['overrides'])}{applied}")
    print(f"行合计 {report['total']}：未分组 {len(report['ungrouped'])} 行，重复分组 {len(report['duplicated'])} 行")
    if report["ungrouped"]:
        print(f"  未分组: {', '.join(report['ungrouped'])}")
    for line_id, group_ids in report["duplicated"].items():
        print(f"  重复分组: {line_id} ← {', '.join(group_ids)}（后 apply 的覆盖先 apply 的）")
    return EXIT_OK


def cmd_group_diff(args, config) -> int:
    """预览一个分组会改动哪些行、每个字段的旧值与新值。

    参数:
        args: 含 chapter、group。
        config: 本机配置。

    返回:
        退出码 0；分组不存在时由 `GroupsStore.find` 抛 ValueError（退出码 2）。
    """
    store = _resolve_store(args, config)
    groups_store = GroupsStore(store.root)
    chapter = store.load_chapter(args.chapter)
    data = groups_store.load(chapter["id"])
    group = groups_store.find(chapter["id"], args.group)
    diff = build_diff(group, chapter)
    covered = {
        item["line_id"]: covering_groups(data["groups"], item["line_id"], exclude=group.get("id"))
        for item in diff["changes"]
    }
    covered = {line_id: ids for line_id, ids in covered.items() if ids}
    payload = {
        "chapter": chapter["id"],
        "group": {"id": group.get("id"), "name": group.get("name")},
        "matched": diff["matched"],
        "unchanged": diff["unchanged"],
        "changes": diff["changes"],
        "covered_by": covered,
        "stale_field_note": _stale_note(diff["changes"]),
    }
    if getattr(args, "json", False):
        _emit(args, payload=payload)
        return EXIT_OK
    print(f"{group.get('id')}  {group.get('name')}  [{_query_summary(group.get('query') or {})}]")
    print(f"命中 {diff['matched']} 行：将改动 {len(diff['changes'])} 行，{diff['unchanged']} 行参数已一致")
    for item in diff["changes"]:
        mark = f"（已被 {', '.join(covered[item['line_id']])} 覆盖过）" if item["line_id"] in covered else ""
        print(f"  {item['line_id']}  {item['scene_id']}  {_field_text(item['fields'])}{mark}")
    if payload["stale_field_note"]:
        print(payload["stale_field_note"])
    return EXIT_OK


def _stale_note(changes: list[dict]) -> str:
    """判断本次改动是否会真的让行派生为 stale。

    只改 `pause_after_ms` 时参数指纹不变（该字段不进 `FP_FIELDS`），行不会变 stale——
    这不是 bug，但必须在 diff 里说清楚，避免用户以为改了却没生效。

    参数:
        changes: `build_diff` 的 changes。

    返回:
        提示文本；不需要提示时返回空串。
    """
    fields = {name for item in changes for name in item["fields"]}
    if not fields or fields & set(FP_FIELDS):
        return ""
    return f"注意：本次只改 {', '.join(sorted(fields))}，该字段不进参数指纹，行不会变 stale（时间轴参数不改配音）"


def cmd_group_apply(args, config) -> int:
    """把分组物化写入行参数（命中行会按指纹变化派生为 stale）。

    参数:
        args: 含 chapter、group。
        config: 本机配置。

    返回:
        退出码 0（无改动也算成功，便于重复执行）。
    """
    store = _resolve_store(args, config)
    groups_store = GroupsStore(store.root)
    chapter = store.load_chapter(args.chapter)
    group = groups_store.find(chapter["id"], args.group)
    before_chapter = copy.deepcopy(chapter)
    before_states = _derive_states(store, config, before_chapter)
    result = apply_group(store, chapter, group)
    if not result["changed_lines"]:
        payload = {
            "chapter": chapter["id"],
            "group": group.get("id"),
            "changed_lines": [],
            "unchanged": result["unchanged"],
            "states": [],
            "others_unchanged": True,
        }
        _emit(args, f"{group.get('id')} 无改动：命中 {result['unchanged']} 行参数已一致（幂等）", payload)
        return EXIT_OK
    groups_store.mark_applied(chapter["id"], group["id"], result["changed_lines"])
    after_states = _derive_states(store, config, chapter)
    hit = set(result["changed_lines"])
    states = [
        {
            "line_id": line_id,
            "before": before_states.get(line_id),
            "after": after_states.get(line_id),
            "changed": result["changed"][line_id],
        }
        for line_id in result["changed_lines"]
    ]
    others = [line_id for line_id in before_states if line_id not in hit and before_states[line_id] != after_states.get(line_id)]
    payload = {
        "chapter": chapter["id"],
        "group": group.get("id"),
        "changed_lines": result["changed_lines"],
        "unchanged": result["unchanged"],
        "states": states,
        "others_unchanged": not others,
        "others_changed": others,
    }
    if getattr(args, "json", False):
        _emit(args, payload=payload)
        return EXIT_OK
    print(f"已应用 {group.get('id')}：改动 {len(result['changed_lines'])} 行，{result['unchanged']} 行已一致")
    for item in states:
        print(f"  {item['line_id']}  {item['before']} → {item['after']}  {_field_text(item['changed'])}")
    print("未命中的行状态未变" if not others else f"异常：未命中行状态发生变化 {', '.join(others)}")
    return EXIT_OK
