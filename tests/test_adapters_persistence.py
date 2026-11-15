"""章节/角色仓储（实体契约）的测试。

验收口径（阶段 2）：真实工程数据「读 → 写 → 再读」后，除 `updated_at`（现有 `save_chapter`
每次保存都会刷新）外逐字段一致，且不产生额外历史版本、不多出字段。

所有写操作都在临时目录里的工程副本上执行，绝不触碰真实工程目录。
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from novel_drama.adapters.persistence.cast_store import CastRepository
from novel_drama.adapters.persistence.chapter_store import ChapterRepository
from novel_drama.models.enum_check import EnumValueError
from novel_drama.store.project_store import ProjectStore

FIXTURES = Path(__file__).parent / "fixtures" / "domain"
PROJECTS = [Path(item) for item in os.environ.get("NDW_GOLDEN_PROJECTS", "").split(":") if item]


def _copy_project(tmp_path: Path, source: Path) -> ProjectStore:
    """把真实工程的章节与角色表复制到临时目录，返回临时工程的仓储。"""
    root = tmp_path / "proj"
    (root / "chapters").mkdir(parents=True)
    for path in (source / "chapters").glob("*.json"):
        shutil.copy2(path, root / "chapters" / path.name)
    characters = source / "characters.json"
    if characters.exists():
        shutil.copy2(characters, root / "characters.json")
    return ProjectStore(root)


def _fixture_project(tmp_path: Path, names: list[str], characters: str | None) -> ProjectStore:
    """用仓库里的黄金样本搭一个临时工程（章节文件名必须是 `<章节ID>.json`）。"""
    root = tmp_path / "proj"
    (root / "chapters").mkdir(parents=True)
    for name in names:
        data = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
        target = root / "chapters" / f"{data['id']}.json"
        target.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if characters:
        shutil.copy2(FIXTURES / characters, root / "characters.json")
    return ProjectStore(root)


def _diff(before: dict, after: dict) -> dict[str, tuple]:
    """两个字典的字段差异。"""
    return {
        key: (before.get(key), after.get(key))
        for key in set(before) | set(after)
        if before.get(key) != after.get(key)
    }


def test_chapter_save_keeps_every_field_except_updated_at(tmp_path: Path):
    """读 → 写 → 再读：除 updated_at 外逐字段一致，且不产生历史版本。"""
    store = _fixture_project(tmp_path, ["chapter_written_real.json"], None)
    repo = ChapterRepository(store)
    chapter_id = "chp_01M7CY5ZD2WVTNMNKW0VB2CZR0"
    path = store.chapter_path(chapter_id)
    before = json.loads(path.read_text(encoding="utf-8"))

    chapter = repo.load(chapter_id)
    assert chapter.to_dict() == before, "只读不应改变任何字段"

    repo.save(chapter)
    written = json.loads(path.read_text(encoding="utf-8"))
    diff = _diff(before, written)
    assert set(diff) == {"updated_at"}, f"意外字段变化：{diff}"

    reread = repo.load(chapter_id)
    assert {k: v for k, v in reread.to_dict().items() if k != "updated_at"} == {
        k: v for k, v in chapter.to_dict().items() if k != "updated_at"
    }
    assert not store.chapter_versions_dir(chapter_id).exists(), "正文没变不应产生历史版本"


def test_chapter_content_change_archives_old_version(tmp_path: Path):
    """正文变化时仍按现有语义归档旧版本，并保留版本号自增。"""
    store = _fixture_project(tmp_path, ["chapter_written_real.json"], None)
    repo = ChapterRepository(store)
    chapter_id = "chp_01M7CY5ZD2WVTNMNKW0VB2CZR0"
    path = store.chapter_path(chapter_id)
    before = json.loads(path.read_text(encoding="utf-8"))

    chapter = repo.load(chapter_id)
    chapter.rewrite_content("改写后的正文，只有一句。")
    repo.save(chapter)

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["revision"] == before["revision"] + 1
    assert saved["content"] == "改写后的正文，只有一句。"
    assert saved["word_count"] == 12
    archived = sorted(store.chapter_versions_dir(chapter_id).glob("*.json"))
    assert len(archived) == 1
    assert json.loads(archived[0].read_text(encoding="utf-8"))["content"] == before["content"]


def test_cast_save_is_byte_stable(tmp_path: Path):
    """角色表读 → 写后与磁盘内容完全一致（含历史非法枚举值）。"""
    store = _fixture_project(tmp_path, [], "characters_real.json")
    repo = CastRepository(store)
    raw = json.loads(store.characters_file.read_text(encoding="utf-8"))

    cast = repo.load()
    assert cast.to_dict() == raw
    repo.save(cast)
    assert json.loads(store.characters_file.read_text(encoding="utf-8")) == raw


def test_repository_still_rejects_invalid_enum(tmp_path: Path):
    """仓储仍然走 schema 与枚举校验，写非法值必须被拒（不是绕开校验的旁路）。"""
    store = _fixture_project(tmp_path, [], "characters_real.json")
    repo = CastRepository(store)
    cast = repo.load()
    cast.characters[1].speech_style.tempo = "乱写"
    with pytest.raises(EnumValueError):
        repo.save(cast)


@pytest.mark.skipif(not PROJECTS, reason="未设置 NDW_GOLDEN_PROJECTS，跳过真实工程写回验收")
@pytest.mark.parametrize("source", PROJECTS, ids=[path.name for path in PROJECTS])
def test_real_project_write_cycle(tmp_path: Path, source: Path):
    """真实工程副本：全部章节与角色表读 → 写 → 再读，逐字段零差异（updated_at 除外）。"""
    store = _copy_project(tmp_path, source)
    chapters = ChapterRepository(store)
    problems: list[str] = []

    for path in sorted((store.root / "chapters").glob("*.json")):
        before = json.loads(path.read_text(encoding="utf-8"))
        chapter = chapters.load(before["id"])
        chapters.save(chapter)
        written = json.loads(path.read_text(encoding="utf-8"))
        diff = _diff(before, written)
        diff.pop("updated_at", None)
        if diff:
            problems.append(f"{path.name}: {diff}")
        again = chapters.load(before["id"])
        if again.to_dict() != written:
            problems.append(f"{path.name}: 再读结果与写入文件不一致")

    if store.characters_file.exists():
        raw = json.loads(store.characters_file.read_text(encoding="utf-8"))
        cast = CastRepository(store)
        cast.save(cast.load())
        if json.loads(store.characters_file.read_text(encoding="utf-8")) != raw:
            problems.append("characters.json: 往返后不一致")

    assert problems == [], "\n".join(problems)
