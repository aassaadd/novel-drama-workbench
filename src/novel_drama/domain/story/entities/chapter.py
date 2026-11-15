"""章节实体（Chapter）。

聚合根。字段与 `chapter.schema.json` 一一对应，21 个键全部建模，`to_dict()` 不丢字段。

本阶段落地的规则：
1. `word_count` 由 `content` 派生（非空白字符数），不接受外部赋值；
2. `status` 只接受枚举；历史非法值原样保留（见 `enum_bridge`）；
3. 正文改写时 `revision` 自增、字数重算、旧段落块清空（块将在阶段 5 由标注器重建，
   清空是为了避免锚点指向已经不存在的旧文本）；
4. 章节级音色覆盖必须写原因。

尚未落地的规则（按计划在阶段 3/5 接入，避免本阶段改变现有行为）：
- 正文变更联动台本 `stale`（跨聚合，属应用服务）；
- 段落块重建与锚点复核（依赖导演标注解析）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from novel_drama.domain.shared.enum_bridge import coerce_enum, enum_value
from novel_drama.domain.shared.errors import require, require_len
from novel_drama.domain.shared.ids import has_prefix
from novel_drama.domain.shared.text import count_words
from novel_drama.domain.shared.timestamps import now_iso
from novel_drama.domain.story.value_objects.block import ChapterBlock
from novel_drama.domain.story.value_objects.import_snapshot import ImportSnapshot
from novel_drama.domain.story.value_objects.voice_override import VoiceOverride
from novel_drama.models.enums import ChapterStatus, PovMode

#: 已产出下游产物（台本/配音/画面/成片）的状态；正文再改就整体作废
_DOWNSTREAM_STATUSES = {"split", "scripted", "voiced", "visualized", "composed"}


@dataclass
class Chapter:
    """一章正文。

    属性:
        id: 章节 ID，`chp_` 开头。
        work_id: 所属作品 ID。
        index: 章节序号，从 1 开始，工程内唯一。
        title: 标题，1~200 字。
        status: 章节状态枚举。
        revision: 版本号，≥1，正文或结构变更时自增。
        created_at / updated_at: 时间戳（UTC ISO8601）。
        schema_version: 落盘版本，固定 1。
        outline: 正文反推的大纲，≤8000 字。
        synopsis: 滚动摘要，≤4000 字。
        content: 正文，≤200000 字；新建空正文时存 None，历史 `""` 原样保留不改写。
        pov_character_id / pov_mode: 本章视角。
        voice_overrides: 角色 ID → 章节级音色覆盖。
        word_count: 字数，由正文派生。
        blocks: 段落块列表（持久化，供锚点定位）。
        script_path: 导演脚本相对路径。
        source: 导入快照引用。
        continuations: 续写会话记录（阶段 4 建实体，当前原样透传）。
        scenes: v1 内嵌场景（阶段 5 由 Script.shots 承接，当前原样透传）。
    """

    id: str
    work_id: str
    index: int
    title: str
    status: ChapterStatus | str
    revision: int
    created_at: str
    updated_at: str
    schema_version: int = 1
    outline: str | None = None
    synopsis: str | None = None
    content: str | None = None
    pov_character_id: str | None = None
    pov_mode: PovMode | str | None = None
    voice_overrides: dict[str, VoiceOverride] = field(default_factory=dict)
    word_count: int = 0
    blocks: list[ChapterBlock] = field(default_factory=list)
    script_path: str | None = None
    source: ImportSnapshot | None = None
    continuations: list[dict] = field(default_factory=list)
    scenes: list[dict] = field(default_factory=list)
    #: 载入时源文件里出现过的可选键；用于回写时保持与源文件同样的键集合（不凭空补 null/空数组）
    present: frozenset[str] = field(default_factory=frozenset, repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: dict, strict: bool = False) -> Chapter:
        """从落盘字典构造并校验。

        参数:
            data: 章节字典。
            strict: 枚举非法时是否抛异常（载入历史数据时用 False）。

        返回:
            Chapter。
        """
        content = data.get("content")
        chapter = cls(
            id=str(data.get("id") or ""),
            work_id=str(data.get("work_id") or ""),
            index=int(data.get("index") or 0),
            title=str(data.get("title") or ""),
            status=coerce_enum(ChapterStatus, data.get("status"), strict=strict, field="status"),
            revision=int(data.get("revision") or 0),
            created_at=str(data.get("created_at") or ""),
            updated_at=str(data.get("updated_at") or ""),
            schema_version=int(data.get("schema_version") or 1),
            outline=data.get("outline"),
            synopsis=data.get("synopsis"),
            content=content,
            pov_character_id=data.get("pov_character_id"),
            pov_mode=coerce_enum(PovMode, data.get("pov_mode"), strict=strict, field="pov_mode"),
            voice_overrides={
                str(character_id): VoiceOverride.from_dict(item)
                for character_id, item in (data.get("voice_overrides") or {}).items()
            },
            word_count=count_words(content),
            blocks=[ChapterBlock.from_dict(item, strict=strict) for item in (data.get("blocks") or [])],
            script_path=data.get("script_path"),
            source=ImportSnapshot.from_dict(data.get("source")),
            continuations=list(data.get("continuations") or []),
            scenes=list(data.get("scenes") or []),
            present=frozenset(data.keys()),
        )
        chapter.validate()
        return chapter

    def validate(self) -> None:
        """校验章节不变式。

        异常:
            InvariantViolation: 必填缺失、序号/版本非法、字段超长或覆盖项 ID 非法。
        """
        require(has_prefix(self.id, "chp"), "id", f"章节 ID 必须以 chp_ 开头：{self.id}")
        require(has_prefix(self.work_id, "wrk"), "work_id", f"作品 ID 必须以 wrk_ 开头：{self.work_id}")
        require(self.index >= 1, "index", "章节序号必须从 1 开始")
        require_len(self.title, "title", 200, 1)
        require(self.status is not None, "status", "章节状态不能为空")
        require(self.revision >= 1, "revision", "版本号必须 ≥1")
        require(bool(self.created_at), "created_at", "创建时间不能为空")
        require(bool(self.updated_at), "updated_at", "更新时间不能为空")
        require_len(self.outline, "outline", 8000)
        require_len(self.synopsis, "synopsis", 4000)
        require_len(self.content, "content", 200000)
        require_len(self.script_path, "script_path", 500)
        if self.pov_character_id:
            require(has_prefix(self.pov_character_id, "chr"), "pov_character_id", "必须是 chr_ 开头的角色 ID")
        for character_id in self.voice_overrides:
            require(has_prefix(character_id, "chr"), "voice_overrides", f"非法角色 ID：{character_id}")

    def rewrite_content(self, text: str | None) -> None:
        """改写正文：重算字数、版本自增、作废旧段落块与下游状态。

        参数:
            text: 新正文；空白字符串按「无正文」处理。

        异常:
            InvariantViolation: 正文超过 200000 字。
        """
        normalized = text if (text and text.strip()) else None
        require_len(normalized, "content", 200000)
        self.content = normalized
        self.word_count = count_words(normalized)
        self.revision += 1
        self.updated_at = now_iso()
        # 旧块对应旧正文，留着会让锚点静默指向错位置；阶段 5 由标注器重建
        self.blocks = []
        if enum_value(self.status) in _DOWNSTREAM_STATUSES:
            self.status = ChapterStatus.STALE

    def mark_scripted(self, script_path: str) -> None:
        """标记本章已生成导演脚本。

        参数:
            script_path: 脚本相对路径；空值表示撤销标记。

        异常:
            InvariantViolation: 路径超长。
        """
        require_len(script_path, "script_path", 500)
        self.script_path = script_path or None
        if self.script_path:
            self.status = ChapterStatus.SCRIPTED
        elif enum_value(self.status) == "scripted":
            self.status = ChapterStatus.WRITTEN
        self.updated_at = now_iso()

    def to_dict(self) -> dict[str, Any]:
        """转回落盘字典。

        可选键的写出规则：源文件里出现过、或当前有值时才写，避免「读一次再存」就凭空多出
        `source: null`、`blocks: []` 这类键，让工程目录出现无意义 diff。

        返回:
            章节字典。
        """
        data: dict[str, Any] = {
            "schema_version": self.schema_version,
            "id": self.id,
            "work_id": self.work_id,
            "index": self.index,
            "title": self.title,
            "word_count": self.word_count,
            "status": enum_value(self.status),
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        optional = {
            "outline": self.outline,
            "synopsis": self.synopsis,
            "content": self.content,
            "pov_character_id": self.pov_character_id,
            "pov_mode": enum_value(self.pov_mode),
            "voice_overrides": {
                character_id: override.to_dict()
                for character_id, override in self.voice_overrides.items()
            },
            "blocks": [block.to_dict() for block in self.blocks],
            "script_path": self.script_path,
            "source": self.source.to_dict() if self.source else None,
            "continuations": list(self.continuations),
            "scenes": list(self.scenes),
        }
        for key, value in optional.items():
            empty = value is None or value == [] or value == {}
            if empty and key not in self.present:
                continue
            data[key] = value
        return data
