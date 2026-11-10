"""工程数据层：工程目录的创建、JSON 读写与实体校验。"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from ..models import entities
from ..models.ids import now_iso
from .schema_validator import validate

WORKSPACE_INDEX = Path.home() / ".ndw" / "workspaces.json"

# 工程内固定目录
_SUBDIRS = (
    "chapters",
    "timelines",
    "scripts",
    "settings",
    "assets/voice",
    "assets/voice/lines",
    "assets/voice/ref",
    "assets/voice/preview",
    "assets/sfx",
    "assets/image",
    "assets/video",
    "assets/audio/bgm",
    "assets/audio/sfx",
    "bgm",
    "renders",
    "exports",
    ".ndw/logs",
    ".ndw/locks",
)

# 新建工程时的默认全局设置（不含任何模型路径）
# endpoint / model 留 None 表示「继承本机 config.yaml」，工程不需要重复记录模型信息
_DEFAULT_SETTINGS = {
    "writing": {
        "adapter": "omlx",
        "protocol": None,
        "endpoint": None,
        "cli_path": None,
        "model": None,
        "context_window": None,
        "stop": [],
        "temperature": 0.8,
        "stream": True,
    },
    "tts": {"adapter": "placeholder", "default_voice_id": None, "sample_rate": 48000},
    "sfx": {"adapter": "placeholder", "default_gain_db": -12.0},
    "comfy": {
        "base_url": "http://127.0.0.1:8188",
        "poll_interval_ms": 1000,
        "request_timeout_ms": 30000,
        "job_timeout_ms": 1800000,
        "status_timeout_ms": 10000,
        "templates": {"image": "qwen_image_accel", "video_t2v": "minimax_h3_accel_t2v", "video_i2v": "minimax_h3_accel"},
    },
    "output": {
        "sample_rate": 48000,
        "channels": 2,
        "loudness_target_lufs": -16,
        "video_max_ms": 5000,
        "aspect_ratio": "1:1",
        "image_width": 1328,
        "image_height": 1328,
    },
    "gpu": {"lock_path": "/Volumes/other/gpu.lock", "max_concurrent": 1},
}


class ProjectStore:
    """单个作品工程目录的读写入口。所有写操作使用原子替换，避免半截文件。"""

    def __init__(self, root: str | Path):
        """绑定工程根目录。

        参数:
            root: 工程目录（含 project.json）。
        """
        self.root = Path(root).expanduser().resolve()

    # ---------- 路径 ----------

    @property
    def project_file(self) -> Path:
        """project.json 路径。"""
        return self.root / "project.json"

    @property
    def characters_file(self) -> Path:
        """characters.json 路径。"""
        return self.root / "characters.json"

    @property
    def voices_file(self) -> Path:
        """voices.json（工程级音色库）路径。"""
        return self.root / "voices.json"

    @property
    def queue_file(self) -> Path:
        """串行队列快照路径。"""
        return self.root / ".ndw" / "queue.json"

    def script_path(self, chapter_id: str) -> Path:
        """导演脚本路径。

        参数:
            chapter_id: 章节 ID。

        返回:
            scripts/<chapterId>.json 路径。
        """
        return self.root / "scripts" / f"{chapter_id}.json"

    def voice_asset_dir(self, kind: str) -> Path:
        """音色素材目录（ref/preview）。

        参数:
            kind: ref（参考音频）或 preview（试听产物）。
        """
        return self.root / "assets" / "voice" / kind

    def chapter_path(self, chapter_id: str) -> Path:
        """章节文件路径。

        参数:
            chapter_id: 章节 ID。

        返回:
            chapters/<chapterId>.json 路径。
        """
        return self.root / "chapters" / f"{chapter_id}.json"

    def timeline_path(self, chapter_id: str) -> Path:
        """时间轴文件路径（派生物）。

        参数:
            chapter_id: 章节 ID。

        返回:
            timelines/<chapterId>.timeline.json 路径。
        """
        return self.root / "timelines" / f"{chapter_id}.timeline.json"

    # ---------- 通用 IO ----------

    def exists(self) -> bool:
        """工程是否已初始化（存在 project.json）。"""
        return self.project_file.exists()

    @staticmethod
    def _read_json(path: Path) -> dict:
        """读取 JSON 文件（UTF-8）。"""
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _write_json(path: Path, data: dict) -> None:
        """原子写入 JSON 文件：先写临时文件再替换。

        参数:
            path: 目标文件。
            data: 待写入 dict。
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)

    # ---------- 工程 ----------

    def create(self, title: str, author: str = "", language: str = "zh-CN", genre: list[str] | None = None) -> dict:
        """新建工程目录与初始文件。

        参数:
            title: 作品名。
            author: 作者。
            language: 语言标签。
            genre: 类型标签。

        返回:
            新建的 project.json 内容。
        """
        if self.exists():
            raise FileExistsError(f"工程已存在: {self.project_file}")
        self.root.mkdir(parents=True, exist_ok=True)
        for sub in _SUBDIRS:
            (self.root / sub).mkdir(parents=True, exist_ok=True)

        project = {
            "schema_version": 1,
            "work": entities.new_work(title, author=author, language=language, genre=genre),
            "settings": json.loads(json.dumps(_DEFAULT_SETTINGS)),
        }
        validate(project, "project.schema.json")
        self._write_json(self.project_file, project)

        table = {"schema_version": 1, "characters": [entities.new_narrator()]}
        validate(table, "character.schema.json")
        self._write_json(self.characters_file, table)

        self._write_json(self.queue_file, {"schema_version": 1, "jobs": []})
        self.register_workspace()
        return project

    def load_project(self) -> dict:
        """读取 project.json。"""
        return self._read_json(self.project_file)

    def save_project(self, project: dict) -> None:
        """保存 project.json（先校验）。

        参数:
            project: 完整工程文档。
        """
        project.setdefault("work", {})["updated_at"] = now_iso()
        validate(project, "project.schema.json")
        self._write_json(self.project_file, project)

    def settings(self) -> dict:
        """返回工程级设置（缺失时回退到默认值）。"""
        return self.load_project().get("settings") or json.loads(json.dumps(_DEFAULT_SETTINGS))

    # ---------- 角色 ----------

    def load_characters(self) -> dict:
        """读取角色表。"""
        if not self.characters_file.exists():
            return {"schema_version": 1, "characters": [entities.new_narrator()]}
        return self._read_json(self.characters_file)

    def save_characters(self, table: dict) -> None:
        """保存角色表（先校验）。

        参数:
            table: 角色表文档。
        """
        validate(table, "character.schema.json")
        self._write_json(self.characters_file, table)

    # ---------- 音色库 ----------

    def load_voices(self) -> dict:
        """读取工程音色库（缺失时返回空库）。

        返回:
            {"schema_version": 1, "voices": []} 或已有内容。
        """
        if not self.voices_file.exists():
            return {"schema_version": 1, "voices": []}
        return self._read_json(self.voices_file)

    def save_voices(self, table: dict) -> None:
        """保存工程音色库（先校验）。

        参数:
            table: 音色库文档。
        """
        validate(table, "voice.schema.json")
        self._write_json(self.voices_file, table)

    # ---------- 导演脚本 ----------

    def load_script(self, chapter_id: str) -> dict:
        """读取导演脚本（缺失时报错）。

        参数:
            chapter_id: 章节 ID。

        异常:
            FileNotFoundError: 脚本不存在。
        """
        path = self.script_path(chapter_id)
        if not path.exists():
            raise FileNotFoundError(f"导演脚本不存在: {path}（先执行 ndw script parse）")
        return self._read_json(path)

    def save_script(self, script: dict) -> None:
        """保存导演脚本（先校验，再原子写入）。

        参数:
            script: 导演脚本文档。
        """
        validate(script, "director-script.schema.json")
        self._write_json(self.script_path(script["chapter_id"]), script)

    # ---------- 章节 ----------

    def list_chapters(self) -> list[dict]:
        """列出全部章节（按 index 升序，只读取必要字段）。

        返回:
            章节元信息列表。
        """
        chapters = []
        for path in (self.root / "chapters").glob("*.json"):
            data = self._read_json(path)
            chapters.append(
                {
                    "id": data.get("id"),
                    "index": data.get("index"),
                    "title": data.get("title"),
                    "status": data.get("status"),
                    "word_count": data.get("word_count"),
                    "scene_count": len(data.get("scenes") or []),
                    "revision": data.get("revision"),
                }
            )
        return sorted(chapters, key=lambda item: (item.get("index") or 0))

    def next_chapter_index(self) -> int:
        """返回下一个可用章节序号（从 1 开始）。"""
        indexes = [item.get("index") or 0 for item in self.list_chapters()]
        return (max(indexes) + 1) if indexes else 1

    def load_chapter(self, chapter_id: str) -> dict:
        """读取章节（含场景、台词、音效、图、视频）。

        参数:
            chapter_id: 章节 ID。

        返回:
            章节 dict。
        """
        path = self.chapter_path(chapter_id)
        if not path.exists():
            raise FileNotFoundError(f"章节不存在: {chapter_id}")
        return self._read_json(path)

    def save_chapter(self, chapter: dict) -> None:
        """保存章节（先校验，再原子写入）。

        参数:
            chapter: 章节 dict。
        """
        chapter["updated_at"] = now_iso()
        validate(chapter, "chapter.schema.json")
        self._write_json(self.chapter_path(chapter["id"]), chapter)

    def create_chapter(self, title: str, content: str = "", outline: str = "", index: int | None = None) -> dict:
        """新建章节并落盘。

        参数:
            title: 章节标题。
            content: 正文。
            outline: 大纲。
            index: 序号，默认自动取下一个。

        返回:
            新章节 dict。
        """
        work_id = self.load_project()["work"]["id"]
        chapter = entities.new_chapter(work_id, index or self.next_chapter_index(), title, content=content, outline=outline)
        self.save_chapter(chapter)
        return chapter

    def find_line(self, chapter: dict, line_id: str) -> tuple[dict, dict, dict]:
        """在章节中定位台词行。

        参数:
            chapter: 章节 dict。
            line_id: 台词行 ID。

        返回:
            (场景, 台词行, 台词行所在列表)。

        异常:
            KeyError: 找不到该行。
        """
        for scene in chapter.get("scenes") or []:
            for line in scene.get("lines") or []:
                if line.get("id") == line_id:
                    return scene, line, scene["lines"]
        raise KeyError(f"台词行不存在: {line_id}")

    # ---------- 工作区索引 ----------

    def register_workspace(self) -> None:
        """把当前工程登记到 ~/.ndw/workspaces.json（工程列表页数据源）。

        索引只是「最近工程」的便利数据，写失败（无权限 / 只读沙箱 / 磁盘异常）
        不应让已经创建好的工程变成失败，因此这里吞掉 OSError 并留一行提示。
        """
        try:
            WORKSPACE_INDEX.parent.mkdir(parents=True, exist_ok=True)
            data = {"projects": []}
            if WORKSPACE_INDEX.exists():
                try:
                    data = json.loads(WORKSPACE_INDEX.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    data = {"projects": []}
            projects = [item for item in data.get("projects", []) if item.get("path") != str(self.root)]
            projects.append({"path": str(self.root), "last_opened_at": now_iso()})
            WORKSPACE_INDEX.write_text(
                json.dumps({"projects": projects}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        except OSError as exc:
            print(f"[WARN] 工程索引写入失败（不影响本工程）：{exc}", file=sys.stderr)


def list_workspaces() -> list[dict]:
    """读取本机工程索引。

    返回:
        [{path, last_opened_at, title?}]，按最近打开排序；已不存在的目录会被过滤。
    """
    if not WORKSPACE_INDEX.exists():
        return []
    try:
        data = json.loads(WORKSPACE_INDEX.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    result = []
    for item in data.get("projects", []):
        root = Path(item.get("path", ""))
        if not (root / "project.json").exists():
            continue
        try:
            project = json.loads((root / "project.json").read_text(encoding="utf-8"))
            title = project.get("work", {}).get("title")
        except (json.JSONDecodeError, OSError):
            title = None
        result.append({"path": str(root), "title": title, "last_opened_at": item.get("last_opened_at")})
    return sorted(result, key=lambda item: item.get("last_opened_at") or "", reverse=True)
