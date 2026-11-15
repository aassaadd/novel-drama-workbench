"""统一 LLM 总线的静态守卫。

覆盖三件事：
1. 写作适配器只能在 `adapters/llm/factory.py` 里实例化，业务层不得自己 new；
2. application 层构造写作适配器时必须声明 `action`，保证动作可校验、可展示、可取消；
3. 总线是唯一入口：`llm_calls.build_writing_adapter` 必须委托 `LlmBus`。

用例只做源码扫描，不发起任何模型请求。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src" / "novel_drama"

#: 允许实例化写作适配器的文件（相对 src/novel_drama）
_ALLOWED_ADAPTER_CTORS = {"adapters/llm/factory.py"}

#: 写作适配器类名；业务层只能拿工厂返回值，不能直接 new
_ADAPTER_CTOR_RE = re.compile(r"\b(Omlx|Offline|Unavailable)WritingAdapter\(")


def _py_files() -> list[Path]:
    """列出源码目录下的全部 Python 文件。"""
    return sorted(_SRC.rglob("*.py"))


def test_writing_adapter_only_constructed_by_factory():
    """写作适配器只能由工厂实例化，避免另一套超时/思考覆盖逻辑。"""
    offenders: list[str] = []
    for path in _py_files():
        rel = path.relative_to(_SRC).as_posix()
        if rel in _ALLOWED_ADAPTER_CTORS:
            continue
        text = path.read_text(encoding="utf-8")
        for match in _ADAPTER_CTOR_RE.finditer(text):
            line_start = text.rfind("\n", 0, match.start()) + 1
            line = text[line_start : match.start()]
            # 跳过 `class XxxWritingAdapter(...)` 定义本身
            if line.lstrip().startswith("class "):
                continue
            lineno = text.count("\n", 0, match.start()) + 1
            offenders.append(f"{rel}:{lineno}")
    assert offenders == [], "写作适配器必须经 adapters/llm/factory.py 构造：" + "、".join(offenders)


def test_build_writing_adapter_calls_declare_action():
    """application 层每个 `build_writing_adapter(...)` 都必须带 `action=`。

    总线自身是唯一例外：它先校验动作，再向工厂要原始适配器。
    """
    missing: list[str] = []
    for path in sorted((_SRC / "application").rglob("*.py")):
        rel = path.relative_to(_SRC).as_posix()
        if rel == "application/support/llm_bus.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            if name != "build_writing_adapter":
                continue
            keywords = {item.arg for item in node.keywords}
            if "action" not in keywords:
                missing.append(f"{path.relative_to(_SRC).as_posix()}:{node.lineno}")
    assert missing == [], "写作适配器调用必须声明 action：" + "、".join(missing)


def test_llm_calls_delegates_to_bus():
    """`llm_calls.build_writing_adapter` 只做总线绑定，不再自己装配适配器。"""
    path = _SRC / "application" / "support" / "llm_calls.py"
    text = path.read_text(encoding="utf-8")
    assert "LlmBus(" in text
    assert ".bind(action)" in text
    # 不允许绕过总线直接构造具体适配器，也不允许在应用层引入适配器工厂
    assert not _ADAPTER_CTOR_RE.search(text)
    assert "adapters.llm.factory" not in text


def test_bus_registers_and_checks_cancel():
    """总线在调用前后都检查取消，并且把 call_id 登记到取消表。"""
    path = _SRC / "application" / "support" / "llm_bus.py"
    text = path.read_text(encoding="utf-8")
    assert "llm_cancel.register(self.call_id)" in text
    assert text.count("self._check()") >= 3
    # 非流式调用也必须强制流式分片，才有取消点
    assert "generate_stream" in text


def test_every_call_releases_its_token():
    """请求结束（成功、异常或流结束）后都要释放取消令牌。"""
    app = (_SRC / "interface" / "http" / "app.py").read_text(encoding="utf-8")
    stream = (_SRC / "interface" / "http" / "routers" / "stream.py").read_text(encoding="utf-8")
    assert "release_call(call_id)" in app
    # 两个 SSE worker（正文流 / 结构化进度流）都要释放
    assert stream.count("release_call(call_id)") == 2
