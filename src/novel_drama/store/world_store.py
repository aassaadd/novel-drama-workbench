"""世界条目库（worlds.json）读写。

一本书可记录 1..n 条世界条目（主世界 / 平行世界 / 回忆世界等），
落盘契约见 `schema/world.schema.json`。本模块只做读写与从设定库的迁移，
业务语义（章节挂哪个世界、按世界拼装提示词）由上层服务负责。
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from ..models.ids import new_id, now_iso
from .project_store import ProjectStore
from .schema_validator import validate

# 设定库相对路径（迁移主世界的数据来源）
BIBLE_RELPATH = Path("settings") / "bible.json"

# 设定库未填写时代背景时的占位值，不视为可迁移的世界
_PENDING_ERA = "待填写"

# update() 允许修改的字段（id 与时间戳不接受外部覆盖）
_EDITABLE_FIELDS = (
    "name",
    "era",
    "summary",
    "rules",
    "factions",
    "locations",
    "glossary",
    "visual_style",
    "cover_asset",
)


class WorldStore:
    """单个工程的世界条目库读写入口。"""

    def __init__(self, project_root: str | Path):
        """绑定工程根目录。

        参数:
            project_root: 工程目录（含 project.json）。
        """
        self.project = ProjectStore(project_root)
        self.path = self.project.root / "worlds.json"

    # ---------- 读 ----------

    def exists(self) -> bool:
        """世界条目库文件是否存在。

        返回:
            True 表示 worlds.json 已存在。
        """
        return self.path.exists()

    def load(self) -> dict:
        """读取世界条目库。

        返回:
            {"schema_version": 1, "worlds": [...]} 形式的 dict。

        异常:
            FileNotFoundError: 尚未初始化 world 库。
        """
        if not self.exists():
            raise FileNotFoundError(f"世界条目库不存在: {self.path}（先执行 ensure）")
        return json.loads(self.path.read_text(encoding="utf-8"))

    def list_worlds(self) -> list[dict]:
        """列出全部世界条目。

        返回:
            世界条目列表（按文件内顺序）。
        """
        return self.load().get("worlds") or []

    def get(self, world_id: str) -> dict | None:
        """按 ID 查找世界条目。

        参数:
            world_id: 世界 ID（wld_ 前缀）。

        返回:
            世界条目 dict；未命中返回 None。
        """
        for world in self.list_worlds():
            if world.get("id") == world_id:
                return world
        return None

    def find_many(self, ids: list[str]) -> list[dict]:
        """按给定 ID 顺序批量取世界条目，忽略未知 ID。

        参数:
            ids: 世界 ID 列表。

        返回:
            存在的世界条目列表，顺序与入参一致。
        """
        by_id = {world.get("id"): world for world in self.list_worlds()}
        return [by_id[world_id] for world_id in ids if world_id in by_id]

    # ---------- 写 ----------

    def save(self, table: dict) -> dict:
        """校验并原子写入世界条目库。

        参数:
            table: 完整世界条目库 dict。

        返回:
            写入后的 dict（原样返回）。
        """
        validate(table, "world.schema.json")
        self._write(table)
        return table

    def ensure(self) -> dict:
        """确保 world 库存在；不存在时尝试从设定库迁移主世界。

        返回:
            世界条目库 dict（已存在时原样返回，不覆盖已有数据）。
        """
        if self.exists():
            return self.load()
        table = {"schema_version": 1, "worlds": self._migrate_from_bible()}
        return self.save(table)

    def add(self, world: dict) -> dict:
        """新增一条世界条目并落盘。

        参数:
            world: 世界条目 dict（可缺 id / 时间戳，由本方法补齐）。

        返回:
            写入后的世界条目 dict。
        """
        table = self.load()
        entry = dict(world)
        if not entry.get("id"):
            entry["id"] = new_id("wld")
        ts = now_iso()
        entry.setdefault("created_at", ts)
        entry["updated_at"] = ts
        table.setdefault("worlds", []).append(entry)
        self.save(table)
        return entry

    def update(self, world_id: str, patch: dict) -> dict:
        """更新世界条目的允许字段并刷新 updated_at。

        参数:
            world_id: 世界 ID。
            patch: 待更新字段（仅 _EDITABLE_FIELDS 生效，其余忽略）。

        返回:
            更新后的世界条目 dict。

        异常:
            KeyError: 世界条目不存在。
        """
        table = self.load()
        for entry in table.get("worlds") or []:
            if entry.get("id") != world_id:
                continue
            for field in _EDITABLE_FIELDS:
                if field in patch:
                    entry[field] = patch[field]
            entry["updated_at"] = now_iso()
            self.save(table)
            return entry
        raise KeyError(f"世界条目不存在: {world_id}")

    def remove(self, world_id: str) -> bool:
        """删除一条世界条目并落盘。

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
        """原子写入世界条目库文件。

        临时文件名带 pid + 随机后缀，避免并发写时互相覆盖 replace 源文件。

        参数:
            table: 世界条目库 dict。
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
        factions: list[str] | None = None,
        locations: list[dict] | None = None,
        glossary: list[dict] | None = None,
        visual_style: str = "",
        cover_asset: str | None = None,
    ) -> dict:
        """构造一条世界条目（含 id 与时间戳）。

        参数:
            name: 世界名称。
            era: 时代背景。
            summary: 世界概述。
            rules: 世界硬规则。
            factions: 势力列表。
            locations: 地点列表（元素结构同设定库 world.locations）。
            glossary: 名词表（元素结构同设定库 world.glossary）。
            visual_style: 该世界统一画面风格。
            cover_asset: 封面图相对路径；为空表示未生成。

        返回:
            世界条目 dict。
        """
        ts = now_iso()
        return {
            "id": new_id("wld"),
            "name": name,
            "era": era,
            "summary": summary,
            "rules": list(rules or []),
            "factions": list(factions or []),
            "locations": list(locations or []),
            "glossary": list(glossary or []),
            "visual_style": visual_style,
            "cover_asset": cover_asset,
            "created_at": ts,
            "updated_at": ts,
        }

    def _migrate_from_bible(self) -> list[dict]:
        """从 settings/bible.json 提取旧设定库的 world，构造迁移用的世界条目。

        返回:
            世界条目列表；设定库缺失、无 world 或时代背景仍为占位值时返回空列表。
        """
        bible_path = self.project.root / BIBLE_RELPATH
        if not bible_path.exists():
            return []
        bible = json.loads(bible_path.read_text(encoding="utf-8"))
        world = bible.get("world") or {}
        era = (world.get("era") or "").strip()
        if not era or era == _PENDING_ERA:
            return []
        try:
            title = (self.project.load_project().get("work") or {}).get("title") or ""
        except (FileNotFoundError, json.JSONDecodeError):
            title = ""
        return [
            self.new_world(
                name=f"{title}·主世界" if title else "主世界",
                era=era,
                summary=(bible.get("premise") or {}).get("logline") or "",
                rules=world.get("rules") or [],
                factions=world.get("factions") or [],
                locations=world.get("locations") or [],
                glossary=world.get("glossary") or [],
                visual_style=(bible.get("style") or {}).get("visual_style") or "",
            )
        ]
