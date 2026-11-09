"""虚拟分组：按查询条件批量改行参数，物化写入。

存 `.ndw/groups/<chapter_id>.json`，**不进章节 JSON**：
分组是编辑期辅助数据，不是作品内容，随时可删可重建，也不该触发 chapter schema 变更。
"""

from __future__ import annotations

import json
from pathlib import Path

from ..models.ids import now_iso

# 允许分组覆盖的字段（与 docs §8.1 一致；不开放 text/speaker_id，避免批量改错文本）
GROUP_FIELDS: tuple[str, ...] = ("emotion", "emotion_intensity", "speed", "pause_after_ms")

# 查询条件里可用的键
QUERY_KEYS: tuple[str, ...] = ("speaker_ids", "kinds", "scene_ids", "line_ids")


class GroupsStore:
    """`.ndw/groups/` 的读写入口。"""

    def __init__(self, root: str | Path):
        """绑定工程根目录。

        参数:
            root: 工程目录。
        """
        self.root = Path(root).expanduser().resolve()

    @property
    def directory(self) -> Path:
        """分组目录。"""
        return self.root / ".ndw" / "groups"

    def path(self, chapter_id: str) -> Path:
        """某章的分组文件路径。

        参数:
            chapter_id: 章节 ID。

        返回:
            `.ndw/groups/<chapter_id>.json`。
        """
        return self.directory / f"{chapter_id}.json"

    def load(self, chapter_id: str) -> dict:
        """读某章的分组文件。

        参数:
            chapter_id: 章节 ID。

        返回:
            `{"version": 1, "groups": [...]}`；文件不存在时返回空表（等价「无分组」）。
        """
        path = self.path(chapter_id)
        if not path.exists():
            return {"version": 1, "groups": []}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"version": 1, "groups": []}
        if not isinstance(data, dict) or not isinstance(data.get("groups"), list):
            return {"version": 1, "groups": []}
        data.setdefault("version", 1)
        return data

    def save(self, chapter_id: str, data: dict) -> None:
        """写某章的分组文件（原子写）。

        参数:
            chapter_id: 章节 ID。
            data: 全量分组数据。
        """
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.path(chapter_id)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def find(self, chapter_id: str, group_id: str) -> dict:
        """按 ID 取分组定义。

        参数:
            chapter_id: 章节 ID。
            group_id: 分组 ID。

        返回:
            分组 dict。

        异常:
            ValueError: 分组不存在（CLI 转退出码 2）。
        """
        data = self.load(chapter_id)
        for group in data["groups"]:
            if group.get("id") == group_id:
                return group
        known = ", ".join(item.get("id", "?") for item in data["groups"]) or "（无）"
        raise ValueError(f"分组不存在: {group_id}（本章已有 {known}）")

    def mark_applied(self, chapter_id: str, group_id: str, line_ids: list[str]) -> dict:
        """记下某分组这次覆盖了哪些行（用于「该行已被 grp_x 覆盖过」提示）。

        参数:
            chapter_id: 章节 ID。
            group_id: 分组 ID。
            line_ids: 本次实际改动的行。

        返回:
            更新后的分组定义。
        """
        data = self.load(chapter_id)
        for group in data["groups"]:
            if group.get("id") == group_id:
                group["applied"] = {"at": now_iso(), "lines": sorted(set(line_ids))}
                self.save(chapter_id, data)
                return group
        raise ValueError(f"分组不存在: {group_id}")


def validate_group(group: dict) -> dict:
    """校验分组定义（查询键与覆盖字段都必须在白名单内）。

    参数:
        group: 分组 dict。

    返回:
        归一化后的 `{"query", "overrides"}`。

    异常:
        ValueError: 出现未知查询键或未知覆盖字段（防止拼写错误被静默忽略）。
    """
    query = group.get("query") or {}
    overrides = group.get("overrides") or {}
    unknown_query = [key for key in query if key not in QUERY_KEYS]
    if unknown_query:
        raise ValueError(f"分组 {group.get('id')} 的查询条件含未知键: {', '.join(unknown_query)}")
    unknown_fields = [key for key in overrides if key not in GROUP_FIELDS]
    if unknown_fields:
        raise ValueError(
            f"分组 {group.get('id')} 试图覆盖不允许的字段: {', '.join(unknown_fields)}"
            f"（可选 {', '.join(GROUP_FIELDS)}）"
        )
    return {"query": query, "overrides": overrides}


def match_lines(group: dict, chapter: dict) -> list[dict]:
    """按分组查询条件命中台词行（各条件 AND，空条件不限制）。

    参数:
        group: 分组 dict。
        chapter: 章节 dict。

    返回:
        `[{scene_id, line}]`，保持章内顺序。
    """
    query = group.get("query") or {}
    speakers = set(query.get("speaker_ids") or [])
    kinds = set(query.get("kinds") or [])
    scene_ids = set(query.get("scene_ids") or [])
    line_ids = set(query.get("line_ids") or [])
    rows: list[dict] = []
    for scene in chapter.get("scenes") or []:
        if scene_ids and scene.get("id") not in scene_ids:
            continue
        for line in scene.get("lines") or []:
            if speakers and line.get("speaker_id") not in speakers:
                continue
            if kinds and line.get("kind") not in kinds:
                continue
            if line_ids and line.get("id") not in line_ids:
                continue
            rows.append({"scene_id": scene.get("id"), "line": line})
    return rows


def build_diff(group: dict, chapter: dict) -> dict:
    """算「apply 会发生什么」，不落盘。

    参数:
        group: 分组 dict。
        chapter: 章节 dict。

    返回:
        `{"matched": 命中行数, "changes": [{line_id, scene_id, fields}], "unchanged": 无需改动的行数}`，
        `fields` 形如 `{字段: {"from": 旧值, "to": 新值}}`。
    """
    spec = validate_group(group)
    overrides = spec["overrides"]
    rows = match_lines(group, chapter)
    changes: list[dict] = []
    unchanged = 0
    for row in rows:
        line = row["line"]
        fields = {}
        for name, value in overrides.items():
            if line.get(name) != value:
                fields[name] = {"from": line.get(name), "to": value}
        if not fields:
            unchanged += 1
            continue
        changes.append({"line_id": line["id"], "scene_id": row["scene_id"], "fields": fields})
    return {"matched": len(rows), "changes": changes, "unchanged": unchanged}


def detect(chapter: dict, groups: list[dict]) -> dict:
    """检测未分组与重复分组的行。

    参数:
        chapter: 章节 dict。
        groups: 分组定义列表。

    返回:
        `{"total": 章节行数, "ungrouped": [line_id], "duplicated": {line_id: [group_id]}}`；
        `duplicated` 只收被 ≥2 个分组命中的行。
    """
    hits: dict[str, list[str]] = {}
    total = 0
    for group in groups:
        for row in match_lines(group, chapter):
            hits.setdefault(row["line"]["id"], [])
            if group.get("id") not in hits[row["line"]["id"]]:
                hits[row["line"]["id"]].append(group.get("id"))
    all_ids = [line["id"] for scene in chapter.get("scenes") or [] for line in scene.get("lines") or []]
    total = len(all_ids)
    ungrouped = [line_id for line_id in all_ids if line_id not in hits]
    duplicated = {line_id: ids for line_id, ids in hits.items() if len(ids) > 1}
    return {"total": total, "ungrouped": ungrouped, "duplicated": duplicated}


def covering_groups(groups: list[dict], line_id: str, exclude: str | None = None) -> list[str]:
    """找出已经 apply 过、且覆盖过某行的其他分组。

    参数:
        groups: 分组定义列表。
        line_id: 台词行 ID。
        exclude: 要排除的分组（通常是当前分组自己）。

    返回:
        分组 ID 列表，按 apply 时间先后。
    """
    found = []
    for group in groups:
        if group.get("id") == exclude:
            continue
        applied = group.get("applied") or {}
        if line_id in (applied.get("lines") or []):
            found.append(group.get("id"))
    return found


def apply_group(store, chapter: dict, group: dict) -> dict:
    """把分组物化写入行参数（只改参数，不碰 tts.status）。

    参数:
        store: 工程（用于保存章节）。
        chapter: 章节 dict（就地修改）。
        group: 分组 dict。

    返回:
        `{"changed_lines": [line_id], "changed": {line_id: {字段: {"from","to"}}}, "unchanged": n}`。
    """
    spec = validate_group(group)
    overrides = spec["overrides"]
    rows = match_lines(group, chapter)
    changed: dict[str, dict] = {}
    for row in rows:
        line = row["line"]
        fields = {}
        for name, value in overrides.items():
            if line.get(name) != value:
                fields[name] = {"from": line.get(name), "to": value}
                line[name] = value
        if not fields:
            continue
        # revision 只做并发锁；过期与否由读取时按参数指纹派生（见 docs §4.3）
        line["revision"] = int(line.get("revision") or 1) + 1
        changed[line["id"]] = fields
    if changed:
        store.save_chapter(chapter)
    return {
        "changed_lines": list(changed),
        "changed": changed,
        "unchanged": len(rows) - len(changed),
    }
