"""`ndw script`：导演脚本的查看、解析、同步与标注建议。"""

from __future__ import annotations

from novel_drama.interface.cli.commands.voice import cmd_script
from novel_drama.interface.cli.commands.writing import cmd_script_suggest


def add_parser(sub) -> None:
    """注册 `script` 命令组。"""
    p = sub.add_parser("script", help="导演脚本（人工标注，规则解析）")
    p_sub = p.add_subparsers(dest="action", required=True)
    p_sub.add_parser("syntax", help="打印标注语法").set_defaults(func=cmd_script)
    q = p_sub.add_parser("suggest", help="按场景与台词生成标注建议")
    q.add_argument("--chapter", required=True)
    q.add_argument("--apply", action="store_true", help="解析并同步进章节（含音效/画面/视频位）")
    q.add_argument("--dry-run", action="store_true", help="只预览，不写盘")
    q.add_argument("--out", help="标注文本输出路径，默认 scripts/<chapter>.markup.txt")
    q.add_argument("--scenes-per-round", dest="scenes_per_round", type=int, help="每批镜头数（默认取配置 script_scenes_per_round）")
    q.add_argument("--resume", action="store_true", help="复用上次结果，只重试失败的标注批次")
    q.set_defaults(func=cmd_script_suggest)
    q = p_sub.add_parser("parse")
    q.add_argument("--chapter", required=True)
    q.add_argument("--file", required=True)
    q.add_argument("--dry-run", action="store_true")
    q.set_defaults(func=cmd_script)
    q = p_sub.add_parser("apply", help="把脚本台词同步进章节")
    q.add_argument("--chapter", required=True)
    q.set_defaults(func=cmd_script)
    q = p_sub.add_parser("show")
    q.add_argument("--chapter", required=True)
    q.set_defaults(func=cmd_script)
    q = p_sub.add_parser("validate")
    q.add_argument("--chapter", required=True)
    q.set_defaults(func=cmd_script)
