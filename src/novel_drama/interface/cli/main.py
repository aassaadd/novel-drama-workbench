"""命令行入口：装配各域子命令、初始化操作日志并映射退出码。

退出码：0 成功 / 1 拒绝执行（无参数快照等，未做修改）/ 2 参数或配置错误 / 3 适配器未对接 /
4 前置条件缺失 / 5 任务失败 / 6 schema 校验失败。
"""

from __future__ import annotations

import argparse
import os
import sys

from novel_drama import __version__
from novel_drama.composition import AdapterError, ErrorCode, SchemaValidationError
from novel_drama.domain.shared.errors import RefusedError
from novel_drama.infrastructure.config import load_config
from novel_drama.interface.cli.commands import (
    cast,
    chapter,
    character,
    comfy,
    group,
    line,
    media,
    models,
    music,
    outline,
    pipeline,
    produce,
    project,
    queue,
    script,
    setting,
    tts,
    voice,
    world,
)
from novel_drama.interface.cli.common import (
    EXIT_FAILED,
    EXIT_NOT_IMPLEMENTED,
    EXIT_PRECONDITION,
    EXIT_REJECTED,
    EXIT_SCHEMA,
    EXIT_USAGE,
    PreconditionError,
    TaskFailedError,
)
from novel_drama.tts.errors import TtsError


def build_parser() -> argparse.ArgumentParser:
    """构建命令行解析器。

    注册顺序即 `--help` 的列表顺序，必须与拆分前一致；各域实现放在
    `commands/` 下，这里只按域调用它们的 `add_parser`。
    """
    parser = argparse.ArgumentParser(prog="ndw", description="小说有声剧工作台：文本 → 配音/音效 → 时间轴 → 成片")
    parser.add_argument("--version", action="version", version=f"ndw {__version__}")
    parser.add_argument("--project", "-p", help="工程目录（也可用 NDW_PROJECT 环境变量）")
    parser.add_argument("--config", help="本机配置文件路径，默认 config/config.yaml")
    parser.add_argument("--json", action="store_true", help="输出 JSON，便于脚本消费")
    sub = parser.add_subparsers(dest="command", required=True)

    for register in (
        project.add_parser,
        chapter.add_parser,
        setting.add_parser,
        outline.add_parser,
        world.add_parser,
        character.add_parser,
        cast.add_parser,
        voice.add_parser,
        script.add_parser,
        line.add_parser,
        group.add_parser,
        tts.add_parser,
        media.add_parser,
        music.add_parser,
        media.add_export_parser,
        comfy.add_parser,
        queue.add_parser,
        pipeline.add_parser,
        models.add_parser,
        produce.add_parser,
        produce.add_run_parser,
        pipeline.add_serve_parser,
    ):
        register(sub)
    return parser


def _init_oplog(config, args) -> None:
    """初始化操作日志并给 LLM 适配器装采集钩子。

    子进程（页面触发的长命令 / 编排）也必须初始化，否则模型调用只会在
    Web 主进程里被记录，子进程里的调用全部丢失。

    参数:
        config: 本机配置。
        args: argparse 解析结果（取 project 与子命令名）。

    返回:
        无。
    """
    try:
        from novel_drama.composition import observability

        observability.configure(config)
        observability.install_llm_hooks()
        if not os.environ.get("NDW_LOG_TRACE"):
            os.environ["NDW_LOG_TRACE"] = observability.new_trace_id()
        project = str(getattr(args, "project", "") or "")
        if project and not os.environ.get("NDW_LOG_PROJECT"):
            os.environ["NDW_LOG_PROJECT"] = project
        if not os.environ.get("NDW_LOG_ACTION"):
            func_name = getattr(getattr(args, "func", None), "__name__", "cli")
            os.environ["NDW_LOG_ACTION"] = f"CLI {func_name}"
    except Exception:  # noqa: BLE001 - 日志初始化失败不影响命令执行
        pass


def main(argv: list[str] | None = None) -> int:
    """命令行主入口。

    参数:
        argv: 参数列表（默认取 sys.argv）。

    返回:
        进程退出码。
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        _init_oplog(config, args)
        return args.func(args, config)
    except AdapterError as exc:
        print(f"[{exc.code.value}] {exc.message}", file=sys.stderr)
        return EXIT_NOT_IMPLEMENTED if exc.code == ErrorCode.NOT_IMPLEMENTED else EXIT_FAILED
    except TtsError as exc:
        print(f"[{exc.code}] {exc.message}", file=sys.stderr)
        if exc.hint:
            print(f"  提示：{exc.hint}", file=sys.stderr)
        return EXIT_NOT_IMPLEMENTED if exc.code == "NOT_IMPLEMENTED" else EXIT_FAILED
    except SchemaValidationError as exc:
        print(f"[SCHEMA] {exc}", file=sys.stderr)
        return EXIT_SCHEMA
    except TaskFailedError as exc:
        print(f"[FAILED] {exc}", file=sys.stderr)
        return EXIT_FAILED
    except PreconditionError as exc:
        print(f"[PRECONDITION] {exc}", file=sys.stderr)
        return EXIT_PRECONDITION
    except RefusedError as exc:
        print(f"[{exc.code}] {exc.message}", file=sys.stderr)
        return EXIT_REJECTED
    except FileExistsError as exc:
        print(f"[EXISTS] {exc}", file=sys.stderr)
        return EXIT_REJECTED
    except (FileNotFoundError, KeyError) as exc:
        print(f"[NOT_FOUND] {exc}", file=sys.stderr)
        return EXIT_PRECONDITION
    except ValueError as exc:
        print(f"[USAGE] {exc}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
