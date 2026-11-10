"""设定库（settings/bible.json）读写。

设定库是「世界观 / 大纲 / 文风 / 连续性」的唯一事实来源，落盘契约见
`schema/setting-bible.schema.json`。本模块只做读写、版本号与默认骨架，
业务语义（生成、落地成章节）放在 `writing/service.py`。
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from ..models.ids import new_id, now_iso
from .project_store import ProjectStore
from .schema_validator import validate

# 设定库相对路径（相对工程根目录）
BIBLE_RELPATH = Path("settings") / "bible.json"


class SettingsStore:
    """单个工程的设定库读写入口。"""

    def __init__(self, project_root: str | Path):
        """绑定工程根目录。

        参数:
            project_root: 工程目录（含 project.json）。
        """
        self.project = ProjectStore(project_root)
        self.path = self.project.root / BIBLE_RELPATH

    # ---------- 读 ----------

    def exists(self) -> bool:
        """设定库文件是否存在。"""
        return self.path.exists()

    def load(self) -> dict:
        """读取设定库。

        返回:
            设定库 dict。

        异常:
            FileNotFoundError: 尚未初始化设定库。
        """
        if not self.exists():
            raise FileNotFoundError(f"设定库不存在: {self.path}（先执行 ndw setting init）")
        return json.loads(self.path.read_text(encoding="utf-8"))

    def load_or_none(self) -> dict | None:
        """读取设定库；不存在时返回 None（供可选读取场景使用）。"""
        return self.load() if self.exists() else None

    # ---------- 写 ----------

    def ensure(self) -> dict:
        """确保设定库存在，不存在则按工程元信息生成骨架。

        返回:
            设定库 dict（已存在时原样返回）。
        """
        if self.exists():
            return self.load()
        project = self.project.load_project()
        work = project.get("work") or {}
        bible = self.build_skeleton(work)
        self._write(bible)
        return bible

    def build_skeleton(self, work: dict) -> dict:
        """按作品元信息构造空设定库骨架。

        参数:
            work: project.json 的 work 字段。

        返回:
            通过 schema 校验的设定库 dict。
        """
        ts = now_iso()
        genre = "、".join(work.get("genre") or [])
        return {
            "schema_version": 1,
            "settings_revision": 1,
            "work_id": work["id"],
            "premise": {
                "logline": work.get("logline") or f"《{work.get('title') or '未命名'}》的一句话故事待填写",
                "theme": "",
                "core_conflict": "",
                "ending_intent": "",
                "target_audience": "",
                "target_length": "novella",
            },
            "world": {"era": "待填写", "locations": [], "rules": [], "factions": [], "glossary": []},
            "outline": {"acts": []},
            "style": {
                "tone": genre or "克制",
                "pov": "third_limited",
                "tense": "past",
                "prose_style": work.get("style_guide") or "",
                "forbidden": [],
                "visual_style": "",
            },
            "continuity": {"timeline": [], "open_threads": []},
            "characters_ref": "characters.json",
            "voices_ref": "voices.json",
            "created_at": ts,
            "updated_at": ts,
        }

    def save(self, bible: dict) -> dict:
        """保存设定库：校验 → 版本号 +1 → 原子写入。

        参数:
            bible: 完整设定库 dict。

        返回:
            写入后的 dict（settings_revision 已递增）。
        """
        validate(bible, "setting-bible.schema.json")
        bible["settings_revision"] = int(bible.get("settings_revision") or 1) + 1
        bible["updated_at"] = now_iso()
        bible.setdefault("created_at", bible["updated_at"])
        self._write(bible)
        return bible

    def validate(self, bible: dict) -> None:
        """只校验不写盘。

        参数:
            bible: 设定库 dict。

        异常:
            SchemaValidationError: 不符合设定库 schema。
        """
        validate(bible, "setting-bible.schema.json")

    def _write(self, bible: dict) -> None:
        """原子写入设定库文件。

        临时文件名带 pid + 随机后缀：新建工程后页面会并发拉取设定库，
        固定的 `.json.tmp` 会让后到的写线程在 replace 时找不到源文件。

        参数:
            bible: 设定库 dict。
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
        tmp.write_text(json.dumps(bible, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)

    # ---------- 大纲工具 ----------

    @staticmethod
    def new_act(title: str, summary: str = "") -> dict:
        """构造一幕。

        参数:
            title: 幕标题。
            summary: 幕摘要。

        返回:
            幕 dict（chapters 为空数组）。
        """
        return {"id": new_id("act"), "title": title, "summary": summary, "chapters": []}

    @staticmethod
    def new_outline_chapter(chapter_id: str, index: int, title: str, summary: str = "") -> dict:
        """构造大纲章节条目（ID 复用真实章节 ID）。

        参数:
            chapter_id: 章节 ID（chp_ 前缀）。
            index: 全书序号，从 1 开始。
            title: 章节标题。
            summary: 章节摘要。

        返回:
            大纲章节 dict。
        """
        return {"id": chapter_id, "index": index, "title": title, "summary": summary, "beats": []}

    @staticmethod
    def new_beat(summary: str, character_ids: list[str] | None = None, turning_point: bool = False, target_words: int = 0) -> dict:
        """构造节拍。

        参数:
            summary: 节拍摘要（要发生什么）。
            character_ids: 涉及角色 ID。
            turning_point: 是否关键转折。
            target_words: 该节拍目标字数。

        返回:
            节拍 dict。
        """
        return {
            "id": new_id("bea"),
            "summary": summary,
            "character_ids": character_ids or [],
            "turning_point": turning_point,
            "target_words": target_words,
        }

    def find_outline_chapter(self, bible: dict, chapter_id: str) -> dict | None:
        """在大纲中按章节 ID 查找条目。

        参数:
            bible: 设定库 dict。
            chapter_id: 章节 ID。

        返回:
            大纲章节条目；找不到返回 None。
        """
        for act in (bible.get("outline") or {}).get("acts") or []:
            for item in act.get("chapters") or []:
                if item.get("id") == chapter_id:
                    return item
        return None

    def all_outline_chapters(self, bible: dict) -> list[dict]:
        """按幕顺序展开全部大纲章节。

        参数:
            bible: 设定库 dict。

        返回:
            大纲章节条目列表。
        """
        result = []
        for act in (bible.get("outline") or {}).get("acts") or []:
            result.extend(act.get("chapters") or [])
        return result
