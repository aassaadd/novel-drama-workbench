"""工程数据层：工程目录的创建、JSON 读写与实体校验。"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from ...domain.story.rules.chapter_rules import should_archive_version, version_id_of
from ...models import entities
from ...models.enum_check import (
    check_chapter,
    check_character_table,
    check_project,
    check_script,
    check_voice_table,
)
from ...models.ids import now_iso
from .schema_validator import SchemaValidationError, validate

WORKSPACE_INDEX = Path.home() / ".ndw" / "workspaces.json"

#: 已删除的工程级设置键（不做旧工程兼容）：读工程时直接报可读错误，不静默忽略
_REMOVED_SETTING_KEYS = {
    "omlx.max_tokens": "输出侧已取消 max_tokens 设置，请求输出由上下文剩余空间决定",
}

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
    ".ndw/versions",
)

#: 版本文件名只允许这些字符，避免路径穿越
_VERSION_ID_PATTERN = re.compile(r"^[0-9A-Za-z_-]+$")

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
    "production": {
        "silences_ms": {
            "start": 1000,
            "title_to_body": 800,
            "narration_to_dialogue": 400,
            "narration_to_sfx": 400,
            "narration_to_narration": 300,
            "dialogue_to_narration": 300,
            "dialogue_to_sfx": 300,
            "dialogue_to_dialogue": 250,
            "sfx_to_dialogue": 300,
            "sfx_to_narration": 300,
            "sfx_to_sfx": 200,
            "end": 1000,
        },
        "audio": {"format": "mp3", "sample_rate": 48000, "bitrate_kbps": 192},
    },
    "gpu": {"lock_path": "/Volumes/other/gpu.lock", "max_concurrent": 1},
}


def _reject_removed_settings(project: dict) -> None:
    """检查工程是否仍带已删除的设置键，命中时给出可读中文错误。

    参数:
        project: project.json 内容。

    返回:
        无。

    异常:
        SchemaValidationError: 出现已删除的设置键。
    """
    settings = project.get("settings") or {}
    for dotted, reason in _REMOVED_SETTING_KEYS.items():
        section, key = dotted.split(".", 1)
        block = settings.get(section)
        if isinstance(block, dict) and key in block:
            raise SchemaValidationError(
                f"旧工程包含已删除的设置项 settings/{dotted.replace('.', '/')}"
                f"（{reason}）；请从 project.json 删除该键后重新打开"
            )


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

    def save_timeline(self, chapter_id: str, timeline: dict) -> None:
        """写入时间轴文件（派生物，不走章节 schema 校验）。

        参数:
            chapter_id: 章节 ID。
            timeline: 完整时间轴 dict（调用方负责先做 schema 校验）。

        返回:
            无。
        """
        self._write_json(self.timeline_path(chapter_id), timeline)

    def chapter_versions_dir(self, chapter_id: str) -> Path:
        """章节历史版本目录。

        参数:
            chapter_id: 章节 ID。

        返回:
            .ndw/versions/<chapterId> 路径（目录可能还不存在）。
        """
        return self.root / ".ndw" / "versions" / chapter_id

    @property
    def rolling_summary_file(self) -> Path:
        """全书滚动摘要文件路径（.ndw/rolling_summary.json）。"""
        return self.root / ".ndw" / "rolling_summary.json"

    # ---------- 通用 IO ----------

    def exists(self) -> bool:
        """工程是否已初始化（存在 project.json）。"""
        return self.project_file.exists()

    @staticmethod
    def _read_json(path: Path) -> dict:
        """读取 JSON 文件（UTF-8）。"""
        return json.loads(path.read_text(encoding="utf-8"))

    def _baseline(self, path: Path) -> dict | None:
        """读取磁盘上的旧文档，用于放行原样未改的历史枚举值。

        参数:
            path: 目标文件。

        返回:
            旧文档；文件不存在或损坏时返回 None（按严格校验处理）。
        """
        if not path.exists():
            return None
        try:
            return self._read_json(path)
        except (OSError, ValueError):
            return None

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
        """读取 project.json；旧工程里的已删除设置键直接报可读错误。"""
        project = self._read_json(self.project_file)
        _reject_removed_settings(project)
        return project

    def save_project(self, project: dict) -> None:
        """保存 project.json（先校验）。

        参数:
            project: 完整工程文档。
        """
        project.setdefault("work", {})["updated_at"] = now_iso()
        baseline = self._baseline(self.project_file)
        check_project(project, baseline=baseline)
        validate(project, "project.schema.json", baseline=baseline)
        self._write_json(self.project_file, project)

    def settings(self) -> dict:
        """返回工程级设置（缺失时回退到默认值）。

        旧工程没有 `production` 时按默认制作要求补齐，保证时间轴与导出行为一致。

        返回:
            设置 dict（含 production）。
        """
        settings = self.load_project().get("settings") or json.loads(json.dumps(_DEFAULT_SETTINGS))
        settings.setdefault("production", self.production())
        return settings

    def production(self) -> dict:
        """读取项目级制作要求（缺失字段逐项回退默认值）。

        返回:
            `{"silences_ms": {...}, "audio": {...}}`。
        """
        merged = json.loads(json.dumps(_DEFAULT_SETTINGS["production"]))
        current = (self.load_project().get("settings") or {}).get("production") or {}
        for section in ("silences_ms", "audio"):
            merged[section].update(current.get(section) or {})
        return merged

    def save_production(self, payload: dict) -> dict:
        """保存项目级制作要求，并同步 `output.sample_rate`。

        参数:
            payload: 允许只带 `silences_ms` 或 `audio` 的部分字段。

        返回:
            保存后的完整制作要求 dict。
        """
        project = self.load_project()
        settings = project.setdefault("settings", {})
        merged = self.production()
        for section in ("silences_ms", "audio"):
            if isinstance(payload.get(section), dict):
                merged[section].update(payload[section])
        settings["production"] = merged
        # 采样率是全局输出参数：同步到 output，混音与时间轴沿用同一份
        settings.setdefault("output", {})["sample_rate"] = int(merged["audio"]["sample_rate"])
        self.save_project(project)
        return merged

    # ---------- 角色 ----------

    def load_characters(self) -> dict:
        """读取角色表。"""
        if not self.characters_file.exists():
            return {"schema_version": 1, "characters": [entities.new_narrator()]}
        return self._read_json(self.characters_file)

    def save_characters(self, table: dict, baseline: dict | None = None) -> None:
        """保存角色表（先校验枚举与 schema）。

        参数:
            table: 角色表文档。
            baseline: 校验用的旧文档；默认读磁盘。删除列表中间的角色后，后面角色的
                下标会整体前移，按旧下标比对会把「历史非法枚举值」误判成本次新写入；
                调用方可在删除后传入「已去掉该角色」的旧文档，让历史值仍按同一角色放行。

        异常:
            EnumValueError: 写入了未定义的枚举值（消息含字段名与可选值）。
        """
        baseline = baseline if baseline is not None else self._baseline(self.characters_file)
        check_character_table(table, baseline=baseline)
        validate(table, "character.schema.json", baseline=baseline)
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
        """保存工程音色库（先校验枚举与 schema）。

        参数:
            table: 音色库文档。
        """
        baseline = self._baseline(self.voices_file)
        check_voice_table(table, baseline=baseline)
        validate(table, "voice.schema.json", baseline=baseline)
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
        """保存导演脚本（先校验枚举与 schema，再原子写入）。

        参数:
            script: 导演脚本文档。
        """
        path = self.script_path(script["chapter_id"])
        baseline = self._baseline(path)
        check_script(script, baseline=baseline)
        validate(script, "director-script.schema.json", baseline=baseline)
        self._write_json(path, script)

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
                    "synopsis": data.get("synopsis"),
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

        正文发生变化时，先把当前磁盘上的旧版本完整存档到
        `.ndw/versions/<章节ID>/<旧 revision>.json`（同 revision 冲突时追加 `-2`、`-3`）。
        只改标题/摘要等不影响正文的字段时不产生新版本，避免版本列表被无关写入刷屏。

        参数:
            chapter: 章节 dict。
        """
        chapter["updated_at"] = now_iso()
        path = self.chapter_path(chapter["id"])
        current: dict = {}
        if path.exists():
            try:
                current = self._read_json(path)
            except (OSError, ValueError):
                current = {}
        baseline = current or None
        check_chapter(chapter, baseline=baseline)
        validate(chapter, "chapter.schema.json", baseline=baseline)
        # 旧版没有正文时不存档（新建章节的首次生成不产生无意义的 0 字版本）
        if should_archive_version(current, chapter):
            self._archive_chapter(chapter["id"], current)
        self._write_json(path, chapter)

    def _archive_chapter(self, chapter_id: str, chapter: dict) -> None:
        """把一份完整章节快照写入历史版本目录。

        参数:
            chapter_id: 章节 ID。
            chapter: 待存档的旧章节（原样保存，不修改字段）。

        返回:
            无。
        """
        directory = self.chapter_versions_dir(chapter_id)
        directory.mkdir(parents=True, exist_ok=True)
        revision = version_id_of(chapter)
        target = directory / f"{revision}.json"
        suffix = 2
        while target.exists():
            target = directory / f"{revision}-{suffix}.json"
            suffix += 1
        self._write_json(target, chapter)

    def list_chapter_versions(self, chapter_id: str) -> list[dict]:
        """列出章节的历史版本（新版本在前，只返回展示所需字段）。

        参数:
            chapter_id: 章节 ID。

        返回:
            [{version_id, revision, title, word_count, updated_at, content_chars}]；
            没有历史版本时返回空列表。
        """
        directory = self.chapter_versions_dir(chapter_id)
        if not directory.exists():
            return []
        versions: list[dict] = []
        for path in directory.glob("*.json"):
            try:
                data = self._read_json(path)
            except (OSError, ValueError):
                continue
            content = str(data.get("content") or "")
            versions.append(
                {
                    "version_id": path.stem,
                    "revision": data.get("revision"),
                    "title": data.get("title"),
                    "word_count": data.get("word_count"),
                    "updated_at": data.get("updated_at"),
                    "content_chars": len(content),
                }
            )
        versions.sort(key=lambda item: (int(item.get("revision") or 0), str(item.get("updated_at") or "")), reverse=True)
        return versions

    def load_chapter_version(self, chapter_id: str, version_id: str) -> dict:
        """读取某个历史版本的完整章节快照。

        参数:
            chapter_id: 章节 ID。
            version_id: 版本文件名（不含 .json，如 `3` 或 `3-2`）。

        返回:
            旧章节完整 dict。

        异常:
            ValueError: version_id 非法。
            FileNotFoundError: 版本不存在。
        """
        if not _VERSION_ID_PATTERN.match(str(version_id or "")):
            raise ValueError(f"非法的版本号: {version_id}")
        path = self.chapter_versions_dir(chapter_id) / f"{version_id}.json"
        if not path.exists():
            raise FileNotFoundError(f"章节版本不存在: {chapter_id}/{version_id}")
        return self._read_json(path)

    # ---------- 滚动摘要 ----------

    def load_rolling_summary(self) -> dict:
        """读取全书滚动摘要。

        返回:
            {book_summary, through_index, through_chapter_id, updated_at}；
            文件不存在或损坏时返回空 dict。
        """
        path = self.rolling_summary_file
        if not path.exists():
            return {}
        try:
            data = self._read_json(path)
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def save_rolling_summary(self, data: dict) -> None:
        """原子写入全书滚动摘要。

        参数:
            data: {book_summary, through_index, through_chapter_id, updated_at}。
        """
        self._write_json(self.rolling_summary_file, data)

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
