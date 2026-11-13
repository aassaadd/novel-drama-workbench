"""标题序号剥离：模型常把「第 3 幕」「第三章」写进标题里。

系统渲染时会统一加序号（如「第 2 幕 · 标题」「3. 标题」），模型标题自带序号就会叠成
「第 2 幕 · 第 3 幕：码头证人」。这里只剥掉标题**开头**的序号前缀，正文中间的不动。

支持写法：
- `第3幕` / `第 3 幕` / `第三幕` / `第2章` / `第三章`
- 序号与标题之间可用中英文冒号、空格、顿号、点号、破折号等分隔
- 外层可包一层括号：`【第3幕】`、`（第三章）`、`(第 2 章)`、`[第3幕]`
- 纯数字前缀：`3. 码头证人`、`12 码头证人`

剥离后为空（标题只有一个序号）时返回空串，由调用方回落到默认标题。
"""

from __future__ import annotations

import re

#: 开头的「第 N 幕 / 第 N 章」前缀（可带括号包裹；必须跟括号、分隔符、空白或结尾）
_INDEX_PREFIX = re.compile(
    r"^\s*(?:[【\[（(]\s*)?第\s*[0-9０-９零〇一二三四五六七八九十百两]+\s*[幕章]"
    r"(?:[】\]）)]\s*|[：:·・、,，.．\-—~～]\s*|\s+|$)"
)

#: 开头的纯数字前缀（必须带分隔符或空白，避免误伤「1984」这类标题）
_NUMBER_PREFIX = re.compile(r"^\s*[0-9０-９]{1,3}\s*(?:[.．、:：)）\]】]\s*|\s+)")


def strip_index_prefix(title: str) -> str:
    """剥掉标题开头的幕/章序号前缀。

    参数:
        title: 原始标题。

    返回:
        去掉开头序号后的标题；只剥开头，正文中的「第 N 章」不受影响。
    """
    value = str(title or "").strip()
    # 最多剥两层，兼容「第3幕 第三章：码头证人」这种重复前缀
    for _ in range(2):
        cleaned = _INDEX_PREFIX.sub("", value, count=1)
        if cleaned == value:
            cleaned = _NUMBER_PREFIX.sub("", value, count=1)
        cleaned = cleaned.strip()
        if cleaned == value:
            break
        value = cleaned
        if not value:
            break
    return value
