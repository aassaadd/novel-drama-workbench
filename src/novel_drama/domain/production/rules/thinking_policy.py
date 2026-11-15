"""写作调用点的思考策略（三态：系统自动 / 开 / 关）。

内置清单按任务性质给出默认是否思考：
- 默认开启：需要分析推敲（世界设定、故事圣经、大纲三阶段、角色、一致性等）；
- 默认关闭：平铺直叙或纯抽取（正文、续写、摘要、改写、脚本、音色/音效/配乐等）。

调用点可以在 metadata 里传 `thinking_stage` 指定所属阶段；是否思考按以下优先级：
1. 全局总开关 `omlx.enable_thinking` 为 False 时，全部关闭（最高优先级）；
2. 调用点显式 `metadata.enable_thinking`；
3. 设置页覆盖层 `writing.thinking.stages.<stage>`（开 / 关）；
4. 没有覆盖项即「系统自动」，回落到本文件的内置清单。

思考强度不按阶段分档，统一取 `omlx.reasoning_effort`。
"""

from __future__ import annotations

#: 调用点清单：键 -> {label 中文名, default 系统自动时是否思考, note 用途}
STAGES: dict[str, dict] = {
    # ---- 默认开启：需要分析推敲 ----
    "world": {"label": "世界设定", "default": True, "note": "世界观条目与世界模块生成"},
    "story_bible": {"label": "故事圣经", "default": True, "note": "故事前提、主题、核心冲突与文风设定"},
    "outline_acts": {"label": "幕大纲", "default": True, "note": "分幕结构、幕摘要与幕标题"},
    "outline_chapters": {"label": "章节清单", "default": True, "note": "按幕拆分章节清单与章节标题"},
    "outline_beats": {"label": "节拍拆分", "default": True, "note": "节拍拆分，含每拍出场人物与角色弧线"},
    "characters": {"label": "角色设定", "default": True, "note": "角色档案、人物名单与特别角色合并判定"},
    "consistency": {"label": "一致性审校", "default": True, "note": "正文与设定、角色状态的一致性检查"},
    # ---- 默认关闭：平铺直叙或纯抽取 ----
    "chapter_round": {"label": "正文分轮", "default": False, "note": "按节拍分轮的正文生成"},
    "chapter_single": {"label": "正文单轮", "default": False, "note": "不分轮、一次写完整章的正文生成"},
    "continuation": {"label": "续写", "default": False, "note": "从断点继续写正文"},
    "supplement": {"label": "补写", "default": False, "note": "只补齐正文缺失部分"},
    "compress": {"label": "压缩", "default": False, "note": "章节压缩、长文事件要点抽取"},
    "rewrite": {"label": "重写", "default": False, "note": "整章改写、润色与压缩改写"},
    "recap": {"label": "章节摘要 / 滚动摘要", "default": False, "note": "一次调用同时产出本章摘要与全书滚动摘要"},
    "outline_extract": {"label": "正文反推大纲", "default": False, "note": "从既有正文抽取章节大纲与节拍"},
    "character_updates": {"label": "角色状态回流", "default": False, "note": "生成后回填角色状态提案"},
    "script": {"label": "脚本标注", "default": False, "note": "导演标注、分镜与台词抽取（配音相关）"},
    "voice_profile": {"label": "音色描述", "default": False, "note": "为角色生成音色描述"},
    "sfx_prompt": {"label": "音效提示词", "default": False, "note": "音效描述改写"},
    "bgm_prompt": {"label": "配乐提示词", "default": False, "note": "配乐描述改写"},
}

#: 工具步骤 -> 调用点：`_generate_json` 未显式指定阶段时按步骤自动归属
STEP_STAGES: dict[str, str] = {
    "premise": "story_bible",
    "style": "story_bible",
    "world": "world",
    "worlds": "world",
    "character": "characters",
    "characters": "characters",
    "character_plan": "characters",
    "outline": "outline_acts",
    "outline_acts": "outline_acts",
    "act": "outline_acts",
    "outline_chapters": "outline_chapters",
    "outline_beats": "outline_beats",
}

#: 允许写入覆盖层的阶段键（与清单保持一致）
STAGE_KEYS: tuple[str, ...] = tuple(STAGES)

#: 阶段对应的覆盖层配置键前缀
STAGE_CONFIG_PREFIX = "writing.thinking.stages."


def stage_default(stage: str) -> bool:
    """返回某个调用点在「系统自动」下是否思考。

    参数:
        stage: 调用点键；未知键按不思考处理。

    返回:
        是否思考。
    """
    return bool(STAGES.get(str(stage or ""), {}).get("default", False))


def stage_label(stage: str) -> str:
    """返回调用点的中文名；未知键回显键名便于排查。

    参数:
        stage: 调用点键。

    返回:
        中文名或原键。
    """
    return str(STAGES.get(str(stage or ""), {}).get("label") or stage or "")


def step_stage(step: str) -> str:
    """把工具步骤映射为调用点键；没有映射时返回空串。

    参数:
        step: `_generate_json` 的步骤名。

    返回:
        调用点键或空串。
    """
    return STEP_STAGES.get(str(step or ""), "")


def resolve_thinking(
    global_enabled: bool | None,
    overrides: dict | None,
    stage: str,
    explicit: bool | None = None,
) -> tuple[bool, str]:
    """按优先级解析一次请求是否思考。

    参数:
        global_enabled: 全局总开关；False 表示全部关闭，None 视为未关闭。
        overrides: 设置页覆盖层，`{stage: bool}`。
        stage: 调用点键。
        explicit: 调用点显式值；None 表示没有显式覆盖。

    返回:
        `(enabled, source)`；source 取 `global-off` / `call` / `stage` / `auto`。
    """
    if global_enabled is False:
        return False, "global-off"
    if explicit is not None:
        return bool(explicit), "call"
    value = (overrides or {}).get(str(stage or ""))
    if value is not None:
        return bool(value), "stage"
    return stage_default(stage), "auto"


def collect_overrides(config) -> dict:
    """从 AppConfig 收集设置页显式覆盖的调用点。

    参数:
        config: AppConfig（提供 `get(dotted)`）。

    返回:
        `{stage: bool}`，只包含有覆盖项的调用点。
    """
    overrides: dict = {}
    for stage in STAGE_KEYS:
        value = config.get(STAGE_CONFIG_PREFIX + stage)
        if value is not None:
            overrides[stage] = bool(value)
    return overrides
