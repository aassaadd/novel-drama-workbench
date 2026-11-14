"""原文快照存储与对照（`.ndw/imports/`）。

导入时的原文独立成文件落盘，章节 JSON 只记引用与哈希，避免把大段正文塞进章节 JSON。
快照只读、不参与任何 schema 校验，唯一用途是回答「这段原文去哪了」。
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
from pathlib import Path

from ..models.ids import now_iso

# 快照文件后缀
_SUFFIX = ".txt"


def digest(text: str) -> str:
    """算原文的 sha256（带 `sha256:` 前缀，与指纹格式一致）。

    参数:
        text: 原文。

    返回:
        `sha256:<64 位小写十六进制>`。
    """
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _atomic_write(path: Path, payload: str) -> None:
    """原子写文本文件（先写临时文件再替换，避免断电留半截）。

    参数:
        path: 目标文件。
        payload: 文本内容。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, path)


def split_paragraphs(text: str) -> list[dict]:
    """按空行切段，并保留原文偏移。

    参数:
        text: 原文。

    返回:
        `[{index, char_start, char_end, text, key, sha256}]`；`key` 是去掉所有空白后的文本，
        用于比对点/缩进/换行差异免疫；`index` 从 0 开始。
    """
    spans: list[tuple[int, int]] = []
    start: int | None = None
    end = 0
    offset = 0
    for raw in text.splitlines(keepends=True):
        if raw.strip():
            if start is None:
                start = offset
            end = offset + len(raw)
        elif start is not None:
            spans.append((start, end))
            start = None
        offset += len(raw)
    if start is not None:
        spans.append((start, end))

    items = []
    for index, (char_start, char_end) in enumerate(spans):
        body = text[char_start:char_end]
        key = "".join(body.split())
        items.append(
            {
                "index": index,
                "char_start": char_start,
                "char_end": char_end,
                "text": body.strip(),
                "key": key,
                "sha256": digest(key),
            }
        )
    return items


def preview(text: str, limit: int = 40) -> str:
    """把段落压成一行预览（供 CLI 与 JSON 共用）。

    参数:
        text: 段落文本。
        limit: 最多保留多少字。

    返回:
        超长时带省略号的单行文本。
    """
    clean = re.sub(r"\s+", " ", text).strip()
    return clean if len(clean) <= limit else clean[:limit] + "…"


def compare_texts(source_text: str, current_text: str) -> dict:
    """按段落对齐原文与当前文本。

    参数:
        source_text: 导入快照原文。
        current_text: 当前章节正文。

    返回:
        `{"paragraphs": 原文段数, "current_paragraphs": 当前段数,
          "summary": {"missing"/"added"/"modified"/"matched": 计数},
          "items": [差异条目], "pairs": {原文段序号: 当前段序号}}`。
    """
    source = split_paragraphs(source_text)
    current = split_paragraphs(current_text)
    matcher = difflib.SequenceMatcher(a=[item["key"] for item in source], b=[item["key"] for item in current], autojunk=False)
    items: list[dict] = []
    pairs: dict[int, int] = {}
    counts = {"missing": 0, "added": 0, "modified": 0, "matched": 0}

    def add_missing(src_index: int) -> None:
        """登记一条「原文有、当前无」。"""
        row = source[src_index]
        items.append(
            {
                "status": "missing",
                "paragraph": src_index + 1,
                "char_start": row["char_start"],
                "char_end": row["char_end"],
                "preview": preview(row["text"]),
                "current_paragraph": None,
                "current_char_start": None,
                "current_char_end": None,
            }
        )
        counts["missing"] += 1

    def add_added(cur_index: int) -> None:
        """登记一条「当前有、原文无」。"""
        row = current[cur_index]
        items.append(
            {
                "status": "added",
                "paragraph": None,
                "char_start": None,
                "char_end": None,
                "preview": preview(row["text"]),
                "current_paragraph": cur_index + 1,
                "current_char_start": row["char_start"],
                "current_char_end": row["char_end"],
            }
        )
        counts["added"] += 1

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for offset in range(i2 - i1):
                pairs[i1 + offset] = j1 + offset
            counts["matched"] += i2 - i1
            continue
        if tag == "delete":
            for src_index in range(i1, i2):
                add_missing(src_index)
            continue
        if tag == "insert":
            for cur_index in range(j1, j2):
                add_added(cur_index)
            continue
        paired = min(i2 - i1, j2 - j1)
        for offset in range(paired):
            src_index, cur_index = i1 + offset, j1 + offset
            src_row, cur_row = source[src_index], current[cur_index]
            pairs[src_index] = cur_index
            items.append(
                {
                    "status": "modified",
                    "paragraph": src_index + 1,
                    "char_start": src_row["char_start"],
                    "char_end": src_row["char_end"],
                    "preview": preview(src_row["text"]),
                    "current_paragraph": cur_index + 1,
                    "current_char_start": cur_row["char_start"],
                    "current_char_end": cur_row["char_end"],
                }
            )
            counts["modified"] += 1
        for src_index in range(i1 + paired, i2):
            add_missing(src_index)
        for cur_index in range(j1 + paired, j2):
            add_added(cur_index)

    return {
        "paragraphs": len(source),
        "current_paragraphs": len(current),
        "summary": counts,
        "items": items,
        "pairs": pairs,
    }


class ImportsStore:
    """`.ndw/imports/` 的读写入口。"""

    def __init__(self, root: str | Path):
        """绑定工程根目录。

        参数:
            root: 工程目录。
        """
        self.root = Path(root).expanduser().resolve()

    @property
    def directory(self) -> Path:
        """快照目录。"""
        return self.root / ".ndw" / "imports"

    @property
    def index_path(self) -> Path:
        """索引文件路径。"""
        return self.directory / "index.json"

    def blob_path(self, sha256_value: str) -> Path:
        """按内容哈希定位快照正文（内容寻址，同一段原文只存一份）。

        参数:
            sha256_value: `sha256:<hex>` 或纯 hex。

        返回:
            `.ndw/imports/<hex>.txt`。
        """
        hex_value = sha256_value.split(":", 1)[-1]
        return self.directory / f"{hex_value}{_SUFFIX}"

    def _read_index(self) -> dict:
        """读索引；文件缺失或损坏时返回空表（快照是附加信息，不该阻塞主流程）。"""
        if not self.index_path.exists():
            return {}
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write_index(self, data: dict) -> None:
        """写索引。

        参数:
            data: 全量索引。
        """
        _atomic_write(self.index_path, json.dumps(data, ensure_ascii=False, indent=2))

    def meta(self, chapter_id: str) -> dict | None:
        """读某章的快照元信息。

        参数:
            chapter_id: 章节 ID。

        返回:
            索引条目；无快照时 None。
        """
        return self._read_index().get(chapter_id)

    def save(self, chapter_id: str, text: str, source_path: str | None = None, tool: str = "novel import") -> dict:
        """落一份原文快照（按内容哈希去重，同一原文不重复落盘）。

        参数:
            chapter_id: 章节 ID。
            text: 清洗前的原文。
            source_path: 来源文件路径（便于溯源）。
            tool: 触发导入的命令名。

        返回:
            `{"snapshot_id", "file", "sha256", "chars", "imported_at", "source_path", "tool", "deduplicated"}`。
            `deduplicated=True` 表示同内容已存在，本次没有新增文件。
        """
        sha = digest(text)
        index = self._read_index()
        existing = index.get(chapter_id)
        path = self.blob_path(sha)
        reused = path.exists()
        if not reused:
            _atomic_write(path, text)
        same_snapshot = existing is not None and existing.get("sha256") == sha
        entry = dict(existing) if same_snapshot else {}
        entry.update(
            {
                "snapshot_id": sha.split(":", 1)[-1][:32],
                "file": f".ndw/imports/{path.name}",
                "sha256": sha,
                "chars": len(text),
                "imported_at": entry.get("imported_at") or now_iso(),
                "source_path": source_path,
                "tool": tool,
            }
        )
        index[chapter_id] = entry
        self._write_index(index)
        return {**entry, "deduplicated": reused}

    def load(self, chapter_id: str) -> tuple[str | None, dict | None]:
        """读某章的原文快照。

        参数:
            chapter_id: 章节 ID。

        返回:
            `(原文, 元信息)`；索引或正文缺失时返回 `(None, None)`。
        """
        meta = self.meta(chapter_id)
        if meta is None:
            return None, None
        path = self.blob_path(meta.get("sha256", ""))
        if not path.exists():
            return None, None
        return path.read_text(encoding="utf-8"), meta

    def index(self) -> dict:
        """读全量索引。"""
        return self._read_index()
