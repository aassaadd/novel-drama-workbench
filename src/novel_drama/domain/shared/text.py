"""文本工具：姓名归一化、字数统计、文本哈希。

语义与现有实现逐字对齐（`writing/cast_sync._norm`、`writing/service._count_words`），
因此迁移后同一份数据算出的结果不变。

- 归一化：NFKC 统一全半角 → 去掉全部空白 → casefold 小写。用于角色名/别名查重；
- 字数：非空白字符数。用于章节 `word_count`；
- 哈希：`sha256:<64 位小写十六进制>`，与 schema 里的 `text_hash`/`content_hash` 一致。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

#: 全部空白字符（含全角空格）
_SPACE_RE = re.compile(r"\s+")


def normalize_name(value: str | None) -> str:
    """归一化姓名或别名，用于确定性比对与去重。

    参数:
        value: 原始姓名或别名。

    返回:
        归一化字符串；无效输入返回空串。
    """
    text = unicodedata.normalize("NFKC", str(value or ""))
    return _SPACE_RE.sub("", text).casefold()


def count_words(text: str | None) -> int:
    """统计正文字数（去掉空白后的字符数）。

    参数:
        text: 正文文本。

    返回:
        字数；None 视为 0。
    """
    return sum(1 for char in (text or "") if not char.isspace())


def text_hash(text: str | None) -> str:
    """计算文本哈希。

    参数:
        text: 待哈希文本；None 视为空串。

    返回:
        形如 `sha256:ab12...` 的字符串。
    """
    digest = hashlib.sha256((text or "").encode("utf-8")).hexdigest()
    return f"sha256:{digest}"
