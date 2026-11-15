"""前端 LLM 进度与取消守卫。

覆盖：
1. 页面不得直连模型端点：出现 LLM 端点字面量或 `streamJSON(` 的文件必须引入统一封装；
2. 页面里写出的 `action` 必须在后端 `llm_plan.ACTIONS` 登记，避免拼错导致没有进度卡；
3. 现有用户可见动作都有 `runLlm` / `runLlmStream` 入口；
4. LLM 不设额外总超时，取消按钮与「已取消」收尾由统一浮层提供。
"""

from __future__ import annotations

import re
from pathlib import Path

_PAGES = Path(__file__).resolve().parents[1] / "web-next" / "js" / "pages"
_JS = Path(__file__).resolve().parents[1] / "web-next" / "js"

#: 会调用写作模型的接口：页面只能通过 runLlm / runLlmStream 触达
_LLM_ENDPOINTS = (
    "/api/ui/act/generate/stream",
    "/api/ui/cast/backfill",
    "/api/ui/cast/capture/start",
    "/api/ui/chapter/continue/stream",
    "/api/ui/chapter/generate/stream",
    "/api/ui/chapters/character-updates",
    "/api/ui/chapters/consistency",
    "/api/ui/characters/descriptions/fill/stream",
    "/api/ui/characters/generate/stream",
    "/api/ui/outline/expand/stream",
    "/api/ui/outline/hierarchical/stream",
    "/api/ui/produce",
    "/api/ui/script/suggest/stream",
    "/api/ui/setting/generate",
    "/api/ui/sfx/generate",
    "/api/ui/voices/design/preview",
    "/api/ui/worlds/generate/stream",
)

#: 当前界面里用户可触发的模型动作：每个都要有统一入口
_UI_ACTIONS = (
    "setting.section",
    "world.generate",
    "outline.generate",
    "outline.expand",
    "chapter.generate",
    "chapter.continue",
    "chapter.compress",
    "chapter.rewrite",
    "chapter.supplement",
    "act.generate",
    "characters.generate",
    "characters.describe",
    "cast.backfill",
    "cast.capture",
    "consistency",
    "character.updates",
    "script.suggest",
    "voice.design.preview",
    "media.sfx",
    "produce.run",
)


def _page_files() -> list[Path]:
    """列出页面脚本。"""
    return sorted(_PAGES.glob("*.js"))


def test_pages_do_not_bypass_llm_wrappers():
    """出现模型端点或 streamJSON 的页面必须引入统一封装。"""
    offenders: list[str] = []
    for path in _page_files():
        text = path.read_text(encoding="utf-8")
        hit = any(endpoint in text for endpoint in _LLM_ENDPOINTS) or "streamJSON(" in text
        if not hit:
            continue
        if "runLlm(" not in text and "runLlmStream(" not in text:
            offenders.append(path.name)
    assert offenders == [], "这些页面绕过了统一 LLM 封装：" + "、".join(offenders)


def test_stream_pages_use_run_llm_stream():
    """使用 SSE 的页面必须通过 `runLlmStream`，由它负责进度卡与取消。"""
    offenders: list[str] = []
    for path in _page_files():
        text = path.read_text(encoding="utf-8")
        if "streamJSON(" in text and "runLlmStream(" not in text:
            offenders.append(path.name)
    assert offenders == [], "这些页面直接调用 streamJSON：" + "、".join(offenders)


def test_page_actions_are_registered():
    """页面里出现的 `action` 字面量必须是后端登记过的动作。"""
    from novel_drama.application.support.llm_plan import ACTIONS

    used: dict[str, str] = {}
    for path in _page_files():
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"action:\s*'([^']+)'", text):
            used.setdefault(match.group(1), path.name)
    unknown = {key: name for key, name in used.items() if key not in ACTIONS}
    assert unknown == {}, f"页面使用了未登记的模型动作：{unknown}"


def test_every_ui_action_has_unified_entry():
    """用户可见的模型动作都要能在页面里找到统一入口。

    允许 `action` 由变量拼装（例如正文生成的压缩/重写/补写共用入口），
    因此这里按「动作键字符串是否出现在页面里」判断。
    """
    text = "\n".join(path.read_text(encoding="utf-8") for path in _page_files())
    missing = [key for key in _UI_ACTIONS if f"'{key}'" not in text]
    assert missing == [], "这些动作还没有 runLlm / runLlmStream 入口：" + "、".join(missing)


def test_llm_requests_have_no_total_timeout():
    """LLM 请求统一不设总超时，只靠进度卡取消。"""
    text = (_JS / "llm.js").read_text(encoding="utf-8")
    assert "timeout: 0" in text
    assert "timeout_ms" not in text
    assert "TIMEOUT_" not in text
    # 两个统一入口都必须把取消回调交给进度浮层
    assert text.count("onStop:") >= 2


def test_progress_card_provides_cancel():
    """进度浮层提供「取消」按钮与「已取消」收尾，不使用「停止」措辞。"""
    text = (_JS / "progress.js").read_text(encoding="utf-8")
    assert "data-prog-stop" in text
    assert ">取消</button>" in text or "取消</button>" in text
    assert "正在取消…" in text
    assert "cancel(result = {})" in text
    assert "已取消" in text
    # 用户明确要求：停止和取消是一个意思，界面统一用「取消」
    assert ">停止</button>" not in text


def test_plain_api_keeps_default_timeout():
    """非 LLM 普通接口仍保留 12 秒默认超时。"""
    text = (_JS / "api.js").read_text(encoding="utf-8")
    assert "DEFAULT_TIMEOUT_MS = 12000" in text
    assert "canceledError" in text
    assert "X-LLM-Call-Id" in text
