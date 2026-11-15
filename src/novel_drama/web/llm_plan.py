"""LLM 任务事实清单。

前端在每次模型调用前请求本模块，拿到「调用几次、属于哪个阶段、是否思考、
能否取消」等事实，再渲染统一确认弹窗；前端不硬编码次数或思考开关。

次数口径：
- 精确可算的动作用 `calls`；
- 只能给下限的动作用 `at_least=True`，文案写成「至少 N 次」；
- 纯规则、不会调用模型的动作不在这里登记，前端也不会弹提示。
"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

from ..store import ProjectStore
from ..store.settings_store import SettingsStore
from ..writing.thinking import collect_overrides, resolve_thinking, stage_label

#: 动作清单：键 -> 说明与次数计算
#: - title/what: 弹窗标题与「这次做什么」
#: - stages: 涉及的思考阶段（可多个）
#: - seconds: 单次调用的大致秒数；None 表示视内容长度而定
#: - cancelable: 是否能中途停止
#: - calls: `(store, config, ctx) -> int`
#: - at_least: 次数只是下限
ACTIONS: dict[str, dict[str, Any]] = {}


def _action(
    key: str,
    *,
    title: str,
    what: str,
    stages: tuple[str, ...],
    seconds: int | None,
    cancelable: bool,
    calls: Callable[[ProjectStore, Any, dict], int],
    at_least: bool = False,
    seconds_text: str = "",
) -> None:
    """登记一个会调用模型的动作。

    参数:
        key: 前端传入的动作键。
        title: 弹窗标题。
        what: 这次做什么（用户能读懂的一句话）。
        stages: 思考阶段键，用于解析是否开启思考。
        seconds: 单次调用大致秒数；None 表示视内容长度而定。
        cancelable: 是否能中途停止。
        calls: 次数计算函数。
        at_least: 次数是否为下限。
        seconds_text: 自定义耗时文案（为空时按 seconds 生成）。

    返回:
        无。
    """
    ACTIONS[key] = {
        "title": title,
        "what": what,
        "stages": stages,
        "seconds": seconds,
        "cancelable": cancelable,
        "calls": calls,
        "at_least": at_least,
        "seconds_text": seconds_text,
    }


def _entry(store: ProjectStore, chapter_id: str) -> dict:
    """按章节 ID 找大纲条目（章节文件缺失时只按 ID 兜底）。

    参数:
        store: 工程。
        chapter_id: 章节 ID。

    返回:
        大纲章节 dict；找不到时为空 dict。
    """
    chapter_id = str(chapter_id or "")
    if not chapter_id:
        return {}
    settings = SettingsStore(store.root)
    bible = settings.ensure()
    index = 0
    try:
        index = int(store.load_chapter(chapter_id).get("index") or 0)
    except (OSError, ValueError, KeyError):
        index = 0
    return settings.find_outline_chapter(bible, chapter_id, index=index or None) or {}


def _beats(store: ProjectStore, chapter_id: str) -> int:
    """取章节的节拍数。"""
    return len(_entry(store, chapter_id).get("beats") or [])


def _has_body(store: ProjectStore, chapter_id: str) -> bool:
    """章节是否已有正文（有正文且不覆盖时不会调用模型）。"""
    try:
        return int(store.load_chapter(chapter_id).get("word_count") or 0) > 0
    except (OSError, ValueError, KeyError):
        return False


def _chapter_calls(store: ProjectStore, chapter_id: str, ctx: dict) -> int:
    """估算单章正文生成的模型调用次数。

    参数:
        store: 工程。
        chapter_id: 章节 ID。
        ctx: 前端传来的参数（multi_turn / beats_per_round / max_rounds）。

    返回:
        调用次数；只补缺失且已有正文时为 0。
    """
    if not ctx.get("regen") and _has_body(store, chapter_id):
        return 0
    if not ctx.get("multi_turn"):
        return 1
    beats = max(1, _beats(store, chapter_id))
    per_round = int(ctx.get("beats_per_round") or 0)
    max_rounds = int(ctx.get("max_rounds") or 0)
    if per_round <= 0:
        # 自动分配时近似每轮 1 个节拍，再用最大轮数兜底
        rounds = beats
        if max_rounds:
            rounds = min(rounds, max_rounds)
        return max(1, rounds)
    rounds = math.ceil(beats / per_round)
    if max_rounds:
        rounds = min(rounds, max_rounds)
    return max(1, rounds)


def _act_entries(store: ProjectStore, act_id: str = "", act_index: int | None = None) -> list[dict]:
    """取某一幕的大纲章节列表。"""
    settings = SettingsStore(store.root)
    bible = settings.ensure()
    rows: list[dict] = []
    for position, act in enumerate((bible.get("outline") or {}).get("acts") or [], start=1):
        if act_id and str(act.get("id") or "") != str(act_id):
            continue
        if act_index and int(act.get("index") or position) != int(act_index):
            continue
        rows.extend(entry for entry in act.get("chapters") or [] if isinstance(entry, dict))
    return rows


def _non_narrator(store: ProjectStore) -> list[dict]:
    """非旁白角色列表。"""
    return [item for item in store.load_characters().get("characters") or [] if item.get("role") != "narrator"]


def _needs_description(character: dict) -> bool:
    """角色是否还缺音色/外貌/负向描述字段。"""
    return not all(
        str(character.get(key) or "").strip()
        for key in ("voice_profile", "appearance_prompt", "negative_prompt")
    )


# ---- 各动作登记 ----

_action(
    "setting.section",
    title="生成故事设定",
    what="按作品信息生成故事内核 / 文风 / 角色设定字段，先生成预览，确认后才写入",
    stages=("story_bible",),
    seconds=35,
    cancelable=False,
    calls=lambda store, config, ctx: 1,
)
_action(
    "setting.step",
    title="生成故事设定",
    what="按作品信息生成故事内核或文风字段，先生成预览，确认后才写入",
    stages=("story_bible",),
    seconds=35,
    cancelable=True,
    calls=lambda store, config, ctx: 1,
)
_action(
    "setting.polish",
    title="润色设定字段",
    what="对单个设定字段做一次润色，不写入设定库",
    stages=("story_bible",),
    seconds=25,
    cancelable=False,
    calls=lambda store, config, ctx: 1,
)
_action(
    "world.generate",
    title="生成世界设定",
    what="按方向描述生成世界总述与分类条目，先生成预览，勾选后才写入",
    stages=("world",),
    seconds=45,
    cancelable=False,
    calls=lambda store, config, ctx: 1,
)
_action(
    "outline.generate",
    title="生成分层大纲",
    what="按幕数、每幕章数、每章节拍生成整份大纲：先规划幕，再逐幕出章，最后逐章拆节拍",
    stages=("outline_acts", "outline_chapters", "outline_beats"),
    seconds=60,
    cancelable=False,
    calls=lambda store, config, ctx: 1
    + max(1, int(ctx.get("acts") or 3))
    + max(1, int(ctx.get("acts") or 3)) * max(1, int(ctx.get("chapters_per_act") or 3)) * 2,
    at_least=True,
    seconds_text="逐幕逐章生成，至少 N 次调用；校验不通过会重写，耗时会增加（估计）",
)
_action(
    "outline.expand",
    title="扩充大纲",
    what="在指定位置新增幕 / 章 / 节拍，只新增不改已有内容",
    stages=("outline_acts", "outline_chapters", "outline_beats"),
    seconds=60,
    cancelable=False,
    calls=lambda store, config, ctx: 1
    + max(1, int(ctx.get("new_acts") or 1))
    + max(1, int(ctx.get("new_acts") or 1))
    * max(1, int(ctx.get("chapters_per_act") or 1))
    * max(1, int(ctx.get("beats_per_chapter") or 1)),
    at_least=True,
    seconds_text="逐幕逐章生成，至少 N 次调用；校验不通过会重写，耗时会增加（估计）",
)
_action(
    "chapter.generate",
    title="生成章节正文",
    what="按本章节拍生成正文；开启分轮时每轮一次模型调用",
    stages=("chapter_round", "chapter_single"),
    seconds=None,
    cancelable=True,
    calls=lambda store, config, ctx: 0
    if str(ctx.get("mode") or "") in ("compress", "rewrite", "supplement")
    else _chapter_calls(store, str(ctx.get("chapter") or ""), ctx),
    seconds_text="视章节长度而定（估计，每次调用约 40–90 秒）",
)
_action(
    "chapter.continue",
    title="续写正文",
    what="从本章末尾继续写一段，只生成草稿，确认后才写入正文",
    stages=("continuation",),
    seconds=None,
    cancelable=True,
    calls=lambda store, config, ctx: 1,
    seconds_text="视续写字数而定（估计）",
)
_action(
    "chapter.compress",
    title="压缩正文",
    what="按目标字数压缩本章，保留全部节拍要点",
    stages=("compress",),
    seconds=None,
    cancelable=True,
    calls=lambda store, config, ctx: 1,
    seconds_text="视章节长度而定（估计）",
)
_action(
    "chapter.rewrite",
    title="重写正文",
    what="按原目标字数整章重写，覆盖现有正文",
    stages=("rewrite",),
    seconds=None,
    cancelable=True,
    calls=lambda store, config, ctx: 1,
    seconds_text="视章节长度而定（估计）",
)
_action(
    "chapter.supplement",
    title="补写正文",
    what="只按缺口字数与未覆盖的情节要点续写，不改写已有正文",
    stages=("supplement",),
    seconds=None,
    cancelable=True,
    calls=lambda store, config, ctx: 1,
    seconds_text="视缺口字数而定（估计）",
)
_action(
    "act.generate",
    title="生成整幕正文",
    what="按章节顺序逐章生成整幕正文；每章内部仍按分轮设置调用模型",
    stages=("chapter_round", "chapter_single"),
    seconds=None,
    cancelable=True,
    calls=lambda store, config, ctx: sum(
        _chapter_calls(store, str(entry.get("id") or ""), ctx)
        for entry in _act_entries(store, str(ctx.get("act") or ""), int(ctx.get("act_index") or 0) or None)
    ),
    seconds_text="视整幕章节数与篇幅而定（估计）",
)
_action(
    "characters.generate",
    title="生成角色名单",
    what="按目标人数分批生成角色档案，先生成预览，勾选后才写入",
    stages=("characters",),
    seconds=35,
    cancelable=True,
    calls=lambda store, config, ctx: _character_batches(config, ctx),
)
_action(
    "characters.describe",
    title="补全角色档案",
    what="补全或重新生成角色的音色描述、外貌描述与负向提示词",
    stages=("characters",),
    seconds=20,
    cancelable=True,
    calls=lambda store, config, ctx: _describe_calls(store, ctx),
)
_action(
    "cast.capture",
    title="补角色识别",
    what="只做角色识别、不写正文：从本章节拍里识别角色并更新角色表与待定队列",
    stages=("characters",),
    seconds=8,
    cancelable=True,
    calls=lambda store, config, ctx: _capture_calls(store, ctx),
)
_action(
    "consistency",
    title="一致性审校",
    what="检查本章正文与设定、角色状态是否一致，只出问题清单",
    stages=("consistency",),
    seconds=30,
    cancelable=False,
    calls=lambda store, config, ctx: 1,
)
_action(
    "character.updates",
    title="角色状态回流",
    what="从本章正文提取角色状态变化，只生成提案，逐条确认后才写入角色卡",
    stages=("character_updates",),
    seconds=25,
    cancelable=False,
    calls=lambda store, config, ctx: 1,
)
_action(
    "script.suggest",
    title="脚本标注",
    what="按镜头批次生成导演标注与台词抽取，先生成预览，确认后才应用",
    stages=("script",),
    seconds=30,
    cancelable=True,
    calls=lambda store, config, ctx: _script_calls(store, config, ctx),
)
_action(
    "voice.design.preview",
    title="生成音色描述",
    what="按角色档案生成一次音色描述，只回填文本框，不自动抽卡",
    stages=("voice_profile",),
    seconds=15,
    cancelable=False,
    calls=lambda store, config, ctx: 1,
)
_action(
    "media.sfx",
    title="生成音效",
    what="按本章场景需要生成音效；缺少描述时会先让模型改写音效提示词",
    stages=("sfx_prompt",),
    seconds=20,
    cancelable=False,
    calls=lambda store, config, ctx: _sfx_calls(config, ctx),
    at_least=True,
    seconds_text="至少 N 次提示词调用；实际按本章缺失音效条数增加，媒体合成时间另计（估计）",
)
_action(
    "produce.run",
    title="启动全链路编排",
    what="按所选阶段区间依次执行；其中设定、大纲、正文、导演标注、音效提示词会调用模型",
    stages=("world", "story_bible", "outline_acts", "outline_chapters", "outline_beats", "chapter_round", "script", "sfx_prompt"),
    seconds=None,
    cancelable=True,
    calls=lambda store, config, ctx: _produce_calls(store, ctx),
    at_least=True,
    seconds_text="至少 N 次模型调用；正文与标注按章节数增加，整条链路耗时较长（估计）",
)


def _sfx_calls(config, ctx: dict) -> int:
    """音效提示词的模型调用次数；规则改写时为 0。"""
    if str(config.get("sfx.prompt.rewrite") or "") != "llm":
        return 0
    return max(1, int(ctx.get("count") or 1))


def _produce_calls(store: ProjectStore, ctx: dict) -> int:
    """全链路编排的模型调用下限（按所选阶段区间里的模型阶段数）。

    参数:
        store: 工程。
        ctx: `from_stage` / `to_stage`。

    返回:
        至少会发生的模型调用次数。
    """
    stages = (
        "setting",
        "outline",
        "chapter",
        "scenes",
        "lines",
        "script",
        "tts",
        "sfx",
        "image",
        "video",
        "timeline",
        "concat",
        "mix",
        "mux",
    )
    model_stages = {"setting", "outline", "chapter", "script", "sfx"}
    start = stages.index(str(ctx.get("from_stage") or "setting")) if str(ctx.get("from_stage") or "") in stages else 0
    end = stages.index(str(ctx.get("to_stage") or "mux")) if str(ctx.get("to_stage") or "") in stages else len(stages) - 1
    end = max(end, start)
    selected = [key for key in stages[start : end + 1] if key in model_stages]
    if "chapter" in selected:
        try:
            acts = (SettingsStore(store.root).ensure().get("outline") or {}).get("acts") or []
            chapters = sum(len(act.get("chapters") or []) for act in acts if isinstance(act, dict))
        except Exception:  # noqa: BLE001 - 仅用于估算，失败时按下限 1 处理
            chapters = 0
        return max(1, len(selected) + max(0, chapters - 1))
    return max(1, len(selected))


def _character_batches(config, ctx: dict) -> int:
    """角色名单的分批调用次数。"""
    count = int(ctx.get("count") or 0)
    if count <= 0:
        return 1
    try:
        batch = int((config.get("writing.multi_turn") or {}).get("batch_size_characters") or 5)
    except (TypeError, ValueError):
        batch = 5
    return max(1, math.ceil(count / max(1, batch)))


def _describe_calls(store: ProjectStore, ctx: dict) -> int:
    """角色档案补全的调用次数（按需补全的角色数）。"""
    characters = _non_narrator(store)
    target = str(ctx.get("character") or "")
    if target:
        hit = next((item for item in characters if str(item.get("id") or "") == target), None)
        if hit is None:
            return 0
        return 1 if ctx.get("regen") or _needs_description(hit) else 0
    if ctx.get("regen"):
        return max(1, len(characters))
    return max(0, sum(1 for item in characters if _needs_description(item)))


def _capture_calls(store: ProjectStore, ctx: dict) -> int:
    """角色识别补跑的调用次数（每章一次）。"""
    from ..writing.cast_capture import cast_capture_plan

    plan = cast_capture_plan(
        store,
        chapter_id=str(ctx.get("chapter") or "") or None,
        act_id=str(ctx.get("act") or "") or None,
        act_index=int(ctx.get("act_index") or 0) or None,
    )
    return int(plan.get("count") or 0)


def _script_calls(store: ProjectStore, config, ctx: dict) -> int:
    """脚本标注的分批调用次数。"""
    chapter_id = str(ctx.get("chapter") or "")
    try:
        scenes = len(store.load_chapter(chapter_id).get("scenes") or [])
    except (OSError, ValueError, KeyError):
        scenes = 0
    try:
        per_round = int((config.get("writing.multi_turn") or {}).get("script_scenes_per_round") or 8)
    except (TypeError, ValueError):
        per_round = 8
    return max(1, math.ceil(max(1, scenes) / max(1, per_round)))


def _thinking_text(config, stages: tuple[str, ...]) -> tuple[bool, str]:
    """解析动作涉及的思考阶段。

    参数:
        config: AppConfig。
        stages: 阶段键。

    返回:
        `(是否有任一步骤开启, 中文文案)`。
    """
    global_enabled = config.get("omlx.enable_thinking")
    overrides = collect_overrides(config)
    values = [resolve_thinking(global_enabled, overrides, stage)[0] for stage in stages]
    if not values:
        return False, "关闭"
    if all(values):
        return True, "开启"
    if any(values):
        return True, "部分步骤开启"
    return False, "关闭"


def _seconds_text(spec: dict, calls: int) -> str:
    """生成耗时文案。

    参数:
        spec: 动作登记项。
        calls: 调用次数。

    返回:
        中文耗时文案。
    """
    if spec.get("seconds_text"):
        return str(spec["seconds_text"]).replace("N", str(calls))
    seconds = spec.get("seconds")
    if not seconds:
        return "视内容长度而定（估计）"
    total = int(seconds) * max(1, calls)
    if total < 90:
        return f"约 {total} 秒（估计）"
    return f"约 {max(1, round(total / 60))} 分钟（估计）"


def llm_plan(store: ProjectStore, config, action: str, ctx: dict | None = None) -> dict:
    """返回一个模型动作的事实清单。

    参数:
        store: 工程。
        config: AppConfig。
        action: 动作键（见 `ACTIONS`）。
        ctx: 前端传入的参数（chapter/act/count 等）。

    返回:
        字段见模块开头；未知动作抛 `ValueError`。

    异常:
        ValueError: 动作未登记。
    """
    ctx = dict(ctx or {})
    spec = ACTIONS.get(str(action or ""))
    if not spec:
        raise ValueError(f"未知的模型动作：{action}")
    calls = max(0, int(spec["calls"](store, config, ctx)))
    thinking, thinking_text = _thinking_text(config, tuple(spec["stages"]))
    cancelable = bool(spec["cancelable"])
    calls_text = "0 次（不会调用模型）" if calls == 0 else f"{'至少 ' if spec.get('at_least') else ''}{calls} 次"
    return {
        "action": action,
        "title": spec["title"],
        "what": spec["what"],
        "stage": " / ".join(stage_label(stage) for stage in spec["stages"]),
        "calls": calls,
        "calls_text": calls_text,
        "at_least": bool(spec.get("at_least")),
        "seconds_text": _seconds_text(spec, calls),
        "thinking": thinking,
        "thinking_text": thinking_text,
        "cancelable": cancelable,
        "cancel_text": (
            "可以中途停止：当前这次模型调用结束后生效，已完成内容保留"
            if cancelable
            else "不能中途取消：需要等待本次模型调用完成"
        ),
        "estimate_note": "次数与耗时是估算；实际会受篇幅、校验重写与网络情况影响。",
    }
