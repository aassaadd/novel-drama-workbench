"""世界库（worlds.json）读写。

一套世界 = 顶层世界观总述（名称 / 时代 / 总述 / 规则 / 画面风格）+ 下层分类条目
（地点 / 势力 / 术语 / 其它），落盘契约见 `schema/world.schema.json`。
写作设定不再保存世界观，世界文本只在本文件存一份。
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from ..models.ids import new_id, now_iso
from .project_store import ProjectStore
from .schema_validator import validate

# update() 允许修改的字段（id 与时间戳不接受外部覆盖）
_EDITABLE_FIELDS = (
    "name",
    "era",
    "summary",
    "rules",
    "factions",
    "locations",
    "glossary",
    "others",
    "visual_style",
    "cover_asset",
)

# 分类条目的 ID 前缀与列表字段
CATEGORY_FIELDS = {
    "location": ("locations", "loc"),
    "faction": ("factions", "fac"),
    "term": ("glossary", "glo"),
    "other": ("others", "oth"),
}


class WorldStore:
    """单个工程的世界库读写入口。"""

    def __init__(self, project_root: str | Path):
        """绑定工程根目录。

        参数:
            project_root: 工程目录（含 project.json）。
        """
        self.project = ProjectStore(project_root)
        self.path = self.project.root / "worlds.json"

    # ---------- 读 ----------

    def exists(self) -> bool:
        """世界库文件是否存在。

        返回:
            True 表示 worlds.json 已存在。
        """
        return self.path.exists()

    def load(self) -> dict:
        """读取世界库。

        返回:
            {"schema_version": 1, "worlds": [...]} 形式的 dict。

        异常:
            FileNotFoundError: 尚未初始化世界库。
        """
        if not self.exists():
            raise FileNotFoundError(f"世界库不存在: {self.path}（先执行 ensure）")
        return json.loads(self.path.read_text(encoding="utf-8"))

    def list_worlds(self) -> list[dict]:
        """列出全部世界（每套含总述与分类条目）。

        返回:
            世界条目列表（按文件内顺序）。
        """
        return self.load().get("worlds") or []

    def get(self, world_id: str) -> dict | None:
        """按 ID 查找世界。

        参数:
            world_id: 世界 ID（wld_ 前缀）。

        返回:
            世界 dict；未命中返回 None。
        """
        for world in self.list_worlds():
            if world.get("id") == world_id:
                return world
        return None

    def find_many(self, ids: list[str]) -> list[dict]:
        """按给定 ID 顺序批量取世界，忽略未知 ID。

        参数:
            ids: 世界 ID 列表。

        返回:
            存在的世界列表，顺序与入参一致。
        """
        by_id = {world.get("id"): world for world in self.list_worlds()}
        return [by_id[world_id] for world_id in ids if world_id in by_id]

    # ---------- 写 ----------

    def save(self, table: dict) -> dict:
        """校验并原子写入世界库。

        参数:
            table: 完整世界库 dict。

        返回:
            写入后的 dict（原样返回）。

        异常:
            SchemaValidationError: 结构不符合 world.schema.json。
        """
        for world in table.get("worlds") or []:
            normalize_world(world)
        validate(table, "world.schema.json")
        self._write(table)
        return table

    def ensure(self) -> dict:
        """确保世界库存在；不存在时写入空库。

        返回:
            世界库 dict（已存在时原样返回，不覆盖已有数据）。
        """
        if self.exists():
            return self.load()
        return self.save({"schema_version": 1, "worlds": []})

    def add(self, world: dict) -> dict:
        """新增一套世界并落盘。

        参数:
            world: 世界 dict（可缺 id / 时间戳，由本方法补齐）。

        返回:
            写入后的世界 dict。
        """
        table = self.load()
        entry = normalize_world(dict(world))
        if not entry.get("id"):
            entry["id"] = new_id("wld")
        ts = now_iso()
        entry.setdefault("created_at", ts)
        entry["updated_at"] = ts
        table.setdefault("worlds", []).append(entry)
        self.save(table)
        return entry

    def update(self, world_id: str, patch: dict) -> dict:
        """更新世界的允许字段并刷新 updated_at。

        参数:
            world_id: 世界 ID。
            patch: 待更新字段（仅 _EDITABLE_FIELDS 生效，其余忽略）。

        返回:
            更新后的世界 dict。

        异常:
            KeyError: 世界不存在。
        """
        table = self.load()
        for entry in table.get("worlds") or []:
            if entry.get("id") != world_id:
                continue
            for field in _EDITABLE_FIELDS:
                if field in patch:
                    entry[field] = patch[field]
            normalize_world(entry)
            entry["updated_at"] = now_iso()
            self.save(table)
            return entry
        raise KeyError(f"世界不存在: {world_id}")

    def remove(self, world_id: str) -> bool:
        """删除一套世界并落盘。

        参数:
            world_id: 世界 ID。

        返回:
            True 表示已删除；False 表示该 ID 不存在。
        """
        table = self.load()
        worlds = table.get("worlds") or []
        remaining = [entry for entry in worlds if entry.get("id") != world_id]
        if len(remaining) == len(worlds):
            return False
        table["worlds"] = remaining
        self.save(table)
        return True

    def _write(self, table: dict) -> None:
        """原子写入世界库文件。

        临时文件名带 pid + 随机后缀，避免并发写时互相覆盖 replace 源文件。

        参数:
            table: 世界库 dict。
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
        tmp.write_text(json.dumps(table, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)

    # ---------- 构造 ----------

    @staticmethod
    def new_world(
        name: str,
        era: str,
        summary: str = "",
        rules: list[str] | None = None,
        factions: list[dict] | None = None,
        locations: list[dict] | None = None,
        glossary: list[dict] | None = None,
        others: list[dict] | None = None,
        visual_style: str = "",
        cover_asset: str | None = None,
    ) -> dict:
        """构造一套世界（含总述与分类条目、id 与时间戳）。

        参数:
            name: 世界名称。
            era: 时代与舞台。
            summary: 世界观总述。
            rules: 世界硬规则。
            factions: 势力条目（{id,name,description,source_chapters}）。
            locations: 地点条目（{id,name,description,atmosphere,source_chapters}）。
            glossary: 术语条目（{id,term,meaning,aliases,source_chapters}）。
            others: 其它条目（{id,name,description,source_chapters}）。
            visual_style: 该世界统一画面风格。
            cover_asset: 封面图相对路径；为空表示未生成。

        返回:
            世界 dict。
        """
        ts = now_iso()
        return normalize_world(
            {
                "id": new_id("wld"),
                "name": name,
                "era": era,
                "summary": summary,
                "rules": list(rules or []),
                "factions": list(factions or []),
                "locations": list(locations or []),
                "glossary": list(glossary or []),
                "others": list(others or []),
                "visual_style": visual_style,
                "cover_asset": cover_asset,
                "created_at": ts,
                "updated_at": ts,
            }
        )


def _category_id(category: str, item: dict) -> str:
    """给分类条目补齐稳定 ID。

    参数:
        category: location / faction / term / other。
        item: 条目 dict。

    返回:
        条目 ID（缺失时按分类前缀新生成）。
    """
    _, prefix = CATEGORY_FIELDS[category]
    current = str(item.get("id") or "").strip()
    if current:
        return current
    return new_id(prefix)


def normalize_world(world: dict) -> dict:
    """把一套世界补齐为可落盘结构：分类列表存在、条目有 ID、来源章节是数组。

    说明：不做旧数据迁移；这里只保证新结构自身完整，未知字段原样保留。

    参数:
        world: 世界 dict（会被原地修改）。

    返回:
        同一个 dict。
    """
    for key in ("rules", "factions", "locations", "glossary", "others"):
        value = world.get(key)
        world[key] = list(value) if isinstance(value, list) else []
    for category, (field, prefix) in CATEGORY_FIELDS.items():
        rows = world[field]
        for index, item in enumerate(rows):
            if not isinstance(item, dict):
                # 兼容模型/旧导入把势力写成纯字符串：转成条目对象
                rows[index] = item = {"name": str(item or "").strip()}
            item["id"] = _category_id(category, item)
            sources = item.get("source_chapters")
            item["source_chapters"] = [str(value) for value in sources if str(value).strip()] if isinstance(sources, list) else []
            if category == "term":
                item.setdefault("term", item.get("name") or "")
                item.setdefault("meaning", item.get("description") or "")
                aliases = item.get("aliases")
                item["aliases"] = [str(value) for value in aliases if str(value).strip()] if isinstance(aliases, list) else []
            else:
                item.setdefault("name", "")
                item.setdefault("description", "")
    world.setdefault("visual_style", "")
    world.setdefault("cover_asset", None)
    return world
