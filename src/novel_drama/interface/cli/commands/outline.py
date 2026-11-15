"""大纲检查与绑定 CLI：`ndw outline check` / `ndw outline bind`。"""

from __future__ import annotations

import re

from novel_drama.application.story.outline_bind import update_chapter_binding
from novel_drama.application.story.queries import golden_check
from novel_drama.domain.shared.errors import TaskFailedError
from novel_drama.infrastructure.config import AppConfig
from novel_drama.interface.cli.commands.continuation import cmd_outline_continue
from novel_drama.interface.cli.commands.writing import cmd_outline

from ..common import EXIT_FAILED, _emit, _resolve_store


def cmd_outline_check(args, config: AppConfig) -> int:
    """检查大纲前三章是否满足「黄金三章」要求。

    参数:
        args: 已解析参数（json）。
        config: 本机配置。

    返回:
        进程退出码（检查结果放在 passed 字段里，不阻断流程）。
    """
    store = _resolve_store(args, config)
    result = golden_check(store)
    if result["passed"]:
        text = "黄金三章检查通过"
    else:
        lines = [f"黄金三章检查未通过：{len(result['issues'])} 项"]
        for issue in result["issues"]:
            title = f"《{issue['chapter_title']}》" if issue.get("chapter_title") else ""
            lines.append(f"  第 {issue['chapter_index']} 章{title}：{issue['problem']}——{issue['hint']}")
        text = "\n".join(lines)
    _emit(args, text, result)
    return 0


def _split_ids(raw: str) -> list[str]:
    """把逗号/顿号分隔的 ID 串拆成去空列表。

    参数:
        raw: 命令行原始文本；空串表示清空绑定。

    返回:
        去空白后的 ID 列表。
    """
    return [part.strip() for part in re.split(r"[、,，;；\s]+", raw or "") if part.strip()]


def cmd_outline_bind(args, config: AppConfig) -> int:
    """按参数更新某一章的 world_ids / pov_character_id。

    参数:
        args: 已解析参数（chapter / worlds / pov / json）。
        config: 本机配置。

    返回:
        进程退出码（世界或角色 ID 不存在时返回 EXIT_FAILED）。

    异常:
        无：错误以返回值 + 中文提示表达。
    """
    store = _resolve_store(args, config)
    raw_worlds = getattr(args, "worlds", None)
    raw_pov = getattr(args, "pov", None)
    raw_pov_mode = getattr(args, "pov_mode", None)
    try:
        payload = update_chapter_binding(
            store,
            args.chapter,
            world_ids=_split_ids(raw_worlds) if raw_worlds is not None else None,
            pov_character_id=raw_pov if raw_pov is not None else None,
            pov_mode=raw_pov_mode if raw_pov_mode is not None else None,
        )
    except TaskFailedError as exc:
        text = str(exc)
        if text == "大纲章节不存在":
            text = f"大纲章节不存在: {args.chapter}（先用 ndw outline generate / apply 生成大纲）"
        _emit(args, text, {"chapter": args.chapter, "message": str(exc)})
        return EXIT_FAILED
    except ValueError:
        _emit(
            args,
            "至少给 --worlds / --pov / --pov-mode 之一",
            {"chapter": args.chapter, "message": "缺少绑定参数"},
        )
        return 2
    _emit(
        args,
        f"已更新章节绑定 {args.chapter}：世界 {len(payload['world_ids'])} 个，POV {payload['pov_character_id'] or '未设置'}",
        payload,
    )
    return 0


def add_parser(sub) -> None:
    """注册 `outline` 命令组。"""
    p = sub.add_parser("outline", help="大纲（幕/章/节拍）")
    p_sub = p.add_subparsers(dest="action", required=True)
    p_sub.add_parser("show", help="查看大纲").set_defaults(func=cmd_outline)
    q = p_sub.add_parser("generate", help="生成大纲并写入设定库")
    q.add_argument("--acts", type=int, default=3)
    q.add_argument("--chapters-per-act", dest="chapters_per_act", type=int, default=3)
    q.add_argument("--beats-per-chapter", dest="beats_per_chapter", type=int, default=3)
    q.add_argument("--words", type=int, default=1200, help="单章目标字数")
    q.add_argument("--binding", choices=["auto", "none"], default="auto", help="auto=绑定到已有人物（非法引用过滤后回落到 POV）；none=只过滤非法角色引用，不回落、不改世界绑定")
    q.add_argument("--dry-run", action="store_true")
    q.set_defaults(func=cmd_outline)
    q = p_sub.add_parser("apply", help="把大纲落地成章节文件")
    q.add_argument("--dry-run", action="store_true")
    q.set_defaults(func=cmd_outline)
    p_sub.add_parser("check", help="黄金三章检查（不阻断）").set_defaults(func=cmd_outline_check)
    q = p_sub.add_parser("bind", help="更新章节的世界 / POV 绑定")
    q.add_argument("--chapter", required=True)
    q.add_argument("--worlds", help="世界 ID，逗号分隔；传空串表示清空")
    q.add_argument("--pov", help="POV 角色 ID；传空串表示清空")
    q.add_argument(
        "--pov-mode",
        dest="pov_mode",
        choices=["first", "second", "third_limited", "third_omniscient"],
        help="本章视角覆盖；不传则跟随全书写法，传 third_omniscient 可显式使用全知视角",
    )
    q.set_defaults(func=cmd_outline_bind)
    q = p_sub.add_parser("continue", help="在已有大纲之后追加新章（默认预览，--apply 才写设定库）")
    q.add_argument("--chapters", type=int, help="本次追加章数（默认取配置或 3）；与 --acts 同时给出时以本参数为准")
    q.add_argument("--acts", type=int, help="本次追加幕数；章数 = 幕数 × 每幕章数")
    q.add_argument("--chapters-per-act", dest="chapters_per_act", type=int, default=3)
    q.add_argument("--beats-per-chapter", dest="beats_per_chapter", type=int, default=3)
    q.add_argument("--words", type=int, default=1200, help="单章目标字数")
    q.add_argument("--brief", help="补充要求，如「进入第二卷，节奏加快」")
    q.add_argument("--intent", help="续写需求：接下来要写什么（最高优先级，可留空）")
    outline_cont_mode = q.add_mutually_exclusive_group()
    outline_cont_mode.add_argument("--dry-run", action="store_true", help="只预览（默认行为）")
    outline_cont_mode.add_argument("--apply", action="store_true", help="写入设定库大纲（逐幕落盘）")
    q.set_defaults(func=cmd_outline_continue)
