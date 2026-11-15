"""分层守卫脚本的单元测试。

覆盖三件事：
1. 相对导入还原成绝对模块路径（含越级写法）；
2. 五层目录的允许/禁止依赖判定；
3. 超 1500 行文件在新层内被计为违规。

用例全部在临时目录里造文件，不依赖仓库实际代码结构。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_layering.py"


def _load_guard():
    """按文件路径加载守卫脚本（scripts/ 不是包）。

    dataclasses 会通过 `sys.modules[模块名]` 反查命名空间，因此加载后必须登记。
    """
    spec = importlib.util.spec_from_file_location("check_layering", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["check_layering"] = module
    spec.loader.exec_module(module)
    return module


guard = _load_guard()


def test_absolute_module_relative_levels():
    """相对导入按层级还原：level=1 指当前包，level=2 指上一级。"""
    package = ("novel_drama", "domain", "shared")
    assert guard.absolute_module(package, "enums", 1) == "novel_drama.domain.shared.enums"
    assert guard.absolute_module(package, "story", 2) == "novel_drama.domain.story"
    assert guard.absolute_module(package, None, 2) == "novel_drama.domain"
    assert guard.absolute_module(("novel_drama", "adapters"), "store", 2) == "novel_drama.store"
    assert guard.absolute_module(package, "novel_drama.x", 0) == "novel_drama.x"


def test_classify_target():
    """层名 / legacy / 外部依赖三种归属。"""
    assert guard.classify_target("novel_drama.domain.story") == "domain"
    assert guard.classify_target("novel_drama.web.app") == "legacy"
    assert guard.classify_target("fastapi") is None


def test_scan_flags_forbidden_dependencies(tmp_path: Path):
    """domain 依赖 adapters、adapters 依赖 application 都必须报出。"""
    src = tmp_path / "src"
    files = {
        "novel_drama/domain/bad.py": "from ..adapters import tts\n",
        "novel_drama/domain/legacy_bad.py": "from ..store import ProjectStore\n",
        "novel_drama/domain/kernel_ok.py": "from novel_drama.models.enums import ChapterStatus\n",
        "novel_drama/domain/kernel_bad.py": "from novel_drama.models.ids import new_id\n",
        "novel_drama/application/ok.py": "from ..store import ProjectStore\nfrom ..domain.shared import ids\n",
        "novel_drama/adapters/bad.py": "from ..application.ok import run\n",
        "novel_drama/interface/bad.py": "from ..adapters import tts\n",
        "novel_drama/infrastructure/bad.py": "from ..domain.shared import ids\n",
        "novel_drama/infrastructure/ok.py": "from ..infrastructure import config\n",
    }
    for rel, content in files.items():
        path = src / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    violations, checked, _ = guard.scan(src)
    got = {(item.path, item.importer, item.target) for item in violations}

    assert checked == len(files)
    assert ("novel_drama/domain/bad.py", "domain", "adapters") in got
    assert ("novel_drama/domain/legacy_bad.py", "domain", "legacy") in got
    assert ("novel_drama/domain/kernel_bad.py", "domain", "legacy") in got
    assert ("novel_drama/adapters/bad.py", "adapters", "application") in got
    assert ("novel_drama/interface/bad.py", "interface", "adapters") in got
    assert ("novel_drama/infrastructure/bad.py", "infrastructure", "domain") in got
    assert len(violations) == 6


def test_shared_kernel_enum_module_is_the_only_domain_exception():
    """domain 允许依赖 models.enums（共享内核），但不允许依赖 models 下其他模块。"""
    assert "novel_drama.models.enums" in guard.SHARED_KERNEL
    assert guard.SHARED_KERNEL == frozenset({"novel_drama.models.enums"})


def test_scan_reports_oversize_new_layer_file(tmp_path: Path):
    """新层内超过 1500 行的文件计入超限清单。"""
    src = tmp_path / "src"
    path = src / "novel_drama" / "domain" / "huge.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n" * (guard.MAX_LINES + 5), encoding="utf-8")

    violations, checked, oversize = guard.scan(src)
    assert checked == 1
    assert violations == []
    assert oversize and oversize[0][1] > guard.MAX_LINES


def test_repo_has_no_layering_violation():
    """仓库自身（迁移期）必须零违规：新骨架不允许依赖上层。"""
    root = _SCRIPT.resolve().parents[1] / "src"
    violations, checked, oversize = guard.scan(root)
    assert checked >= 4
    assert violations == [], "\n".join(str(item) for item in violations)
    assert oversize == [], f"新层出现超限文件：{oversize}"
