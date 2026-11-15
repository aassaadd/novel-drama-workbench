"""分层依赖守卫：按 import 矩阵检查五层目录的跨层引用。

设计依据见 `docs/architecture-domain-model.md` §5.2。规则：

| 被检查层 | 允许依赖 |
| --- | --- |
| domain | domain |
| application | domain、application、adapters、infrastructure |
| adapters | domain、adapters、infrastructure |
| interface | domain、application、interface、infrastructure |
| infrastructure | infrastructure |

迁移期约定：`novel_drama` 下不属于上面五层的旧模块（models/store/writing/tts/web/...）
统一记为 `legacy`。`application`、`adapters`、`interface` 在迁移期允许依赖 legacy，
`domain` 与 `infrastructure` **不允许**，保证新骨架从第一天就是干净的。

用法：
    python scripts/check_layering.py                 # 检查仓库，退出码 0/1
    python scripts/check_layering.py --root DIR      # 检查指定 src 目录（测试用）
    python scripts/check_layering.py --baseline-lines # 额外列出超过 1500 行的旧文件

退出码：
    0 通过；1 存在违规（含新层文件超过 1500 行）。
"""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass
from pathlib import Path

#: 五层目录名（与 docs/architecture-domain-model.md §6 一致）
LAYERS: tuple[str, ...] = ("domain", "application", "adapters", "interface", "infrastructure")

#: 每层允许依赖的层（含自身）
ALLOWED: dict[str, frozenset[str]] = {
    "domain": frozenset({"domain"}),
    "application": frozenset({"domain", "application", "adapters", "infrastructure"}),
    "adapters": frozenset({"domain", "adapters", "infrastructure"}),
    "interface": frozenset({"domain", "application", "interface", "infrastructure"}),
    "infrastructure": frozenset({"infrastructure"}),
}

#: 迁移期允许依赖 legacy 旧模块的层（domain 与 infrastructure 不在此列）
LEGACY_EXEMPT: frozenset[str] = frozenset({"application", "adapters", "interface"})

#: 共享内核：迁移期唯一允许 domain 依赖的旧模块。
#: 枚举是领域概念，当前由 `models/enums.py` 单点维护（枚举类型化改造的产物，
#: 前端 `web-next/js/enums.js` 与之对应）。阶段 6 该模块整体迁入 domain/shared 后，
#: 必须删除本例外，domain 恢复「只依赖自己」。
SHARED_KERNEL: frozenset[str] = frozenset({"novel_drama.models.enums"})

#: 单文件行数上限
MAX_LINES = 1500

#: 迁移期超限豁免：这些文件在旧结构里就已超限，阶段 6 拆分后必须删除对应条目。
#: 只允许登记 `adapters/` 下的历史文件；新写的文件不得进入本表。
OVERSIZE_DEBT: frozenset[str] = frozenset()

#: 项目包名
PACKAGE = "novel_drama"

#: 已退役的 CLI 模块：界面层不得再用 subprocess 拼命令串调用它
CLI_MODULE = "novel_drama.cli"


@dataclass(frozen=True)
class Violation:
    """一处跨层违规。

    属性:
        path: 违规文件（相对检查根目录的 POSIX 路径）。
        line: 违规 import 的行号。
        importer: 发起依赖的层名。
        target: 被依赖的层名，legacy 表示旧模块。
        reason: 中文说明。
    """

    path: str
    line: int
    importer: str
    target: str
    reason: str

    def __str__(self) -> str:
        """渲染成一行可读报告。"""
        return f"{self.path}:{self.line}  {self.reason}"


def layer_of_package(parts: tuple[str, ...]) -> str | None:
    """判断模块路径属于哪一层。

    参数:
        parts: 模块路径分段，如 ("novel_drama", "domain", "story")。

    返回:
        层名；不属于五层时返回 None。
    """
    if len(parts) >= 2 and parts[0] == PACKAGE and parts[1] in LAYERS:
        return parts[1]
    return None


def absolute_module(package: tuple[str, ...], module: str | None, level: int) -> str:
    """把 import 语句还原成绝对模块路径。

    参数:
        package: 当前文件的所在包，如 ("novel_drama", "domain", "shared")。
        module: `from X import ...` 里的 X；`import X` 时为完整模块名。
        level: 相对导入层级；0 表示绝对导入。

    返回:
        绝对模块路径（可能为空字符串）。

    说明:
        level=1 指当前包，level=2 指上一级包，依此类推。
    """
    if level == 0:
        return module or ""
    keep = len(package) - (level - 1)
    if keep <= 0:
        # 越界的相对导入由解释器自己报错，这里原样返回便于人工定位
        return module or ""
    base = package[:keep]
    return ".".join((*base, module) if module else base)


def classify_target(module: str) -> str | None:
    """判断被依赖模块的归属。

    参数:
        module: 绝对模块路径。

    返回:
        层名 / "legacy" / None（None 表示项目外部依赖，如标准库、fastapi）。
    """
    parts = tuple(module.split("."))
    if not parts or parts[0] != PACKAGE:
        return None
    layer = layer_of_package(parts)
    return layer or "legacy"


def module_imports(tree: ast.AST) -> list[tuple[int, str, str | None, int]]:
    """收集一个文件里的全部 import。

    参数:
        tree: 已解析的 AST。

    返回:
        `[(行号, 模块名, from 模块名或 None, 相对层级)]`。
    """
    rows: list[tuple[int, str, str | None, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                rows.append((node.lineno, alias.name, None, 0))
        elif isinstance(node, ast.ImportFrom):
            rows.append((node.lineno, node.module or "", node.module, node.level))
    return rows


def cli_invocations(tree: ast.AST) -> list[tuple[int, str]]:
    """找出界面层里把 `novel_drama.cli` 当命令串用的字面量。

    只看字面量出现的上下文：文档字符串与 `"novel_drama.cli" in command` 这类
    命令行检测不算调用；列表里的 `"-m", "novel_drama.cli"`、f-string 等都算。

    参数:
        tree: 已解析的 AST。

    返回:
        `[(行号, 字面量)]`。
    """
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    rows: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if CLI_MODULE not in node.value:
            continue
        parent = parents.get(node)
        if isinstance(parent, ast.Expr):
            # 模块 / 函数文档字符串：说明现状不构成调用
            continue
        if isinstance(parent, ast.Compare):
            # 进程命令行身份检测（ps 输出里判断是不是配音进程）
            continue
        rows.append((node.lineno, node.value))
    return rows


def scan(root: Path) -> tuple[list[Violation], int, list[tuple[str, int]]]:
    """扫描五层目录的跨层依赖与行数。

    参数:
        root: `src` 目录（其下含 novel_drama 包）。

    返回:
        `(违规列表, 检查文件数, [(超限文件, 行数)])`；超限只统计新层文件。
    """
    violations: list[Violation] = []
    oversize: list[tuple[str, int]] = []
    checked = 0

    for layer in LAYERS:
        layer_dir = root / PACKAGE / layer
        if not layer_dir.is_dir():
            continue
        for path in sorted(layer_dir.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            checked += 1
            rel = path.relative_to(root)
            try:
                source = path.read_text(encoding="utf-8")
                tree = ast.parse(source, filename=str(path))
            except (OSError, SyntaxError) as exc:
                violations.append(
                    Violation(str(rel), 1, layer, "-", f"无法解析文件（{exc}）")
                )
                continue

            lines = source.count("\n") + 1
            rel_text = str(rel).replace("\\", "/")
            if lines > MAX_LINES and rel_text not in OVERSIZE_DEBT:
                oversize.append((rel_text, lines))

            package = tuple(rel.parts[:-1])
            for lineno, module, _, level in module_imports(tree):
                target_module = absolute_module(package, module, level)
                if not target_module.startswith(PACKAGE):
                    continue
                target = classify_target(target_module)
                if target is None:
                    continue
                if layer == "domain" and target_module in SHARED_KERNEL:
                    continue
                allowed = ALLOWED[layer] | ({"legacy"} if layer in LEGACY_EXEMPT else set())
                if target in allowed:
                    continue
                violations.append(
                    Violation(
                        str(rel).replace("\\", "/"),
                        lineno,
                        layer,
                        target,
                        f"{layer} 不允许依赖 {target}：{target_module}",
                    )
                )

            if layer == "interface":
                # 界面层只允许调用 application 入口，不允许再拼 `novel_drama.cli` 命令串
                for lineno, literal in cli_invocations(tree):
                    violations.append(
                        Violation(
                            rel_text,
                            lineno,
                            layer,
                            "cli",
                            f"interface 不允许用 subprocess 调用 {CLI_MODULE}（改用 application 入口）：{literal!r}",
                        )
                    )
    return violations, checked, oversize


def legacy_baseline(root: Path) -> list[tuple[str, int]]:
    """列出全部超过行数上限的源文件（含旧模块），仅用于记录基线。

    参数:
        root: `src` 目录。

    返回:
        `[(相对路径, 行数)]`，按行数降序。
    """
    rows: list[tuple[str, int]] = []
    for path in sorted((root / PACKAGE).rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            lines = path.read_text(encoding="utf-8").count("\n") + 1
        except OSError:
            continue
        if lines > MAX_LINES:
            rows.append((str(path.relative_to(root)).replace("\\", "/"), lines))
    return sorted(rows, key=lambda item: item[1], reverse=True)


def main(argv: list[str] | None = None) -> int:
    """命令行入口。

    参数:
        argv: 参数列表；None 表示取 sys.argv。

    返回:
        退出码：0 通过，1 违规。
    """
    parser = argparse.ArgumentParser(description="分层依赖守卫")
    default_root = Path(__file__).resolve().parents[1] / "src"
    parser.add_argument("--root", default=str(default_root), help="src 目录，默认仓库 src/")
    parser.add_argument("--baseline-lines", action="store_true", help="列出超过 1500 行的旧文件")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    violations, checked, oversize = scan(root)

    if args.baseline_lines:
        rows = legacy_baseline(root)
        print(f"超过 {MAX_LINES} 行的源文件：{len(rows)} 个")
        for rel, lines in rows:
            print(f"  {lines:>6}  {rel}")

    for item in violations:
        print(item)
    if oversize:
        print(f"新层超限文件（>{MAX_LINES} 行）：")
        for rel, lines in oversize:
            print(f"  {lines:>6}  {rel}")
    print(f"分层检查完成：扫描 {checked} 个文件，违规 {len(violations) + len(oversize)} 处")
    return 1 if violations or oversize else 0


if __name__ == "__main__":
    sys.exit(main())
