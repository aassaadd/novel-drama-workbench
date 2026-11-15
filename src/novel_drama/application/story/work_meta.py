"""作品元信息：新建工程、读取与写回书名 / 作者 / 类型标签。

页面与 CLI 共用这一份用例，避免设置页与终端对同一份 `project.json`
各写一套校验规则。
"""

from __future__ import annotations

from ...adapters.persistence import ProjectStore


def init_project(
    root: str,
    title: str,
    author: str = "",
    language: str = "zh-CN",
    genre: list[str] | None = None,
) -> dict:
    """新建工程目录与初始文件。

    参数:
        root: 工程目录。
        title: 作品名。
        author: 作者。
        language: 语言标签，默认 `zh-CN`。
        genre: 类型标签列表。

    返回:
        `project.json` 内容（字段与 `init --json` 一致）。

    异常:
        FileExistsError: 目录里已经有工程（不覆盖）。
    """
    store = ProjectStore(root)
    return store.create(title, author=author, language=language, genre=genre)


def work_view(store: ProjectStore) -> dict:
    """读取作品元信息（书名 / 作者 / 类型标签）。

    参数:
        store: 工程。

    返回:
        `{"title", "author", "genres"}`；缺字段时给空值。

    异常:
        FileNotFoundError: 工程未初始化。
    """
    work = store.load_project().get("work") or {}
    return {
        "title": work.get("title") or "",
        "author": work.get("author") or "",
        "genres": work.get("genre") or [],
    }


def update_work(
    store: ProjectStore,
    title: str | None = None,
    author: str | None = None,
    genres: list[str] | None = None,
) -> dict:
    """写回作品元信息；只改传入的字段。

    参数:
        store: 工程。
        title: 新书名；为空串时拒绝（不能把书名改成空）。
        author: 新作者；空串表示清空。
        genres: 新类型标签；空列表表示清空，None 表示不改。

    返回:
        更新后的 `{"title", "author", "genres"}`。

    异常:
        ValueError: 一个字段都没给，或书名为空。
        FileNotFoundError: 工程未初始化。
    """
    if title is None and author is None and genres is None:
        raise ValueError("没有要修改的字段：至少给一个（--title / --author / --genre）")
    project = store.load_project()
    work = project.setdefault("work", {})
    if title is not None:
        text = title.strip()
        if not text:
            raise ValueError("书名不能为空")
        work["title"] = text
    if author is not None:
        work["author"] = author.strip()
    if genres is not None:
        work["genre"] = [item.strip() for item in genres if item and item.strip()]
    store.save_project(project)
    return {
        "title": work.get("title") or "",
        "author": work.get("author") or "",
        "genres": work.get("genre") or [],
    }
