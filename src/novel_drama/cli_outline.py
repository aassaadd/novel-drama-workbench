"""大纲检查 CLI：`ndw outline check`（黄金三章）。"""

from __future__ import annotations

from .cli_common import _emit, _resolve_store
from .config import AppConfig
from .store.settings_store import SettingsStore
from .writing import check_golden_three


def cmd_outline_check(args, config: AppConfig) -> int:
    """检查大纲前三章是否满足「黄金三章」要求。

    参数:
        args: 已解析参数（json）。
        config: 本机配置。

    返回:
        进程退出码（检查结果放在 passed 字段里，不阻断流程）。
    """
    store = _resolve_store(args, config)
    bible = SettingsStore(store.root).ensure()
    result = check_golden_three(bible)
    if result["passed"]:
        text = "黄金三章检查通过"
    else:
        lines = [f"黄金三章检查未通过：{len(result['issues'])} 项"]
        for issue in result["issues"]:
            lines.append(f"  第 {issue['chapter']} 章：{issue['problem']}——{issue['hint']}")
        text = "\n".join(lines)
    _emit(args, text, {**result, "settings_revision": bible.get("settings_revision")})
    return 0
