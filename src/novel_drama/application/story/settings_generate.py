"""用例：生成设定分区、世界条目与字段润色。"""

from __future__ import annotations

from dataclasses import replace

from ...adapters.llm.contracts import Message, TextGenRequest
from ...adapters.persistence import ProjectStore
from ...adapters.persistence.settings_store import SettingsStore
from ...adapters.persistence.world_store import WorldStore
from ...application.story.world import clean_world_entry
from ...infrastructure.config import AppConfig, resolve_temperature
from ..cast.character_fields import _CHARACTER_DESC_RULES, _clean_character_entry
from ..support.llm_calls import (
    _char_delta,
    _characters_payload,
    _check_choice,
    _check_text,
    _clean_str_list,
    _extract_json_object,
    _generate_json,
    _progress,
    build_writing_adapter,
)
from .chapter_context import _bible_digest, _project_context
from .setting_spec import (
    LENGTH_OPTIONS,
    POLISH_FIELDS,
    POV_OPTIONS,
    PREMISE_FIELDS,
    SCALAR_LIMITS,
    SETTING_STEPS,
    STYLE_FIELDS,
    TENSE_OPTIONS,
)


def generate_worlds(
    store: ProjectStore,
    config: AppConfig,
    count: int | None = None,
    overwrite: bool = False,
    brief: str | None = None,
    batch_size: int | None = None,
    on_progress=None,
) -> dict:
    """一次生成 1..6 条世界条目（只返回预览，不落盘）。

    参数:
        store: 工程。
        config: 本机配置。
        count: 期望世界条数（1..6）；为空时由适配器决定。
        overwrite: True 表示调用方打算覆盖已有世界；本函数仍只预览，仅在返回值里标记。
        brief: 用户补充要求（拼进提示词与请求元信息）。
        batch_size: 每批条数；None 时取 `writing.multi_turn.batch_size_worlds`（默认 2）。
        on_progress: 进度回调（batch/chars 事件）；仅页面流式接口传入。

    返回:
        {"step": "worlds", "worlds": [...], "count", "brief", "overwrite", "provider", "batches"}；
        worlds 为清洗后通过 world schema 的完整条目（可直接交给 WorldStore.add）。

    异常:
        ValueError: count 越界、模型输出不是合法 JSON 或世界字段不合法。
    """
    if count is not None:
        try:
            wanted = int(count)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"世界数量必须是 1..6 的整数：{count}") from exc
        if wanted < 1 or wanted > 6:
            raise ValueError(f"世界数量必须在 1..6 之间：{wanted}")
    else:
        wanted = None

    settings_store = SettingsStore(store.root)
    bible = settings_store.load_or_none() or {}
    project = store.load_project()
    work = project.get("work") or {}
    adapter = build_writing_adapter(config, bible)
    brief_text = str(brief or "").strip()

    world_store = WorldStore(store.root)
    world_items = world_store.list_worlds() if world_store.exists() else []
    existing = [{"name": item.get("name"), "era": item.get("era")} for item in world_items]
    ask = "生成世界设定列表，每条包含 name/era/summary/rules/factions/locations/glossary/others/visual_style；locations/glossary/others 必须是数组，元素为对象，不要用字符串包裹。summary 写世界观总述，factions/locations/glossary 是总述之下的分类条目。"
    if wanted:
        ask += f"共 {wanted} 条世界。"
    if brief_text:
        ask += f"补充要求：{brief_text}。"
    # 批量世界同样要贴合作品既有梗概、文风与已建世界，避免自造独立宇宙
    context = _project_context(work, bible, None, world_items)
    prompt = f"{context}\n\n任务：{ask}" if context else ask
    req = TextGenRequest(
        task="setting",
        system="你是中文小说的世界观编辑，世界设定必须与给定的作品设定一致，只按工具参数输出，不要解释。",
        messages=[Message("user", prompt)],
        stream=True,
        temperature=resolve_temperature(config, "writing"),
        metadata={
            "step": "worlds",
            "work": {"title": work.get("title"), "author": work.get("author"), "genre": work.get("genre") or []},
            "existing": _bible_digest(bible),
            "existing_worlds": existing,
            "count": wanted,
            "brief": brief_text,
            "overwrite": bool(overwrite),
            "seed": work.get("id"),
        },
    )
    mt = config.get("writing.multi_turn") or {}
    try:
        batch = int(batch_size or mt.get("batch_size_worlds") or 2)
    except (TypeError, ValueError):
        batch = 2
    batch = max(1, batch)

    rows: list = []
    batches = 1
    progress_state = {"chars": 0}
    if wanted and wanted > batch:
        # 数量超过单批上限：按批生成，每批在固定前缀后追加本批要求与已生成名单
        batches = (wanted + batch - 1) // batch
        names: list[str] = []
        for index in range(batches):
            take = min(batch, wanted - len(rows))
            if take <= 0:
                break
            progress_index = index + 1
            _progress(on_progress, {"type": "batch", "index": progress_index, "total": batches, "unit": "batch"})
            lines = [f"本批生成 {take} 条世界（第 {index + 1}/{batches} 批）。"]
            if names:
                lines.append("已生成世界（不要重复名称）：" + "、".join(names))
            batch_req = replace(req, messages=list(req.messages) + [Message("user", "\n".join(lines))])
            batch_req.metadata = dict(req.metadata or {}, count=take)
            batch_payload = _generate_json(
                adapter,
                batch_req,
                "worlds",
                on_delta=_char_delta(progress_state, on_progress, progress_index, batches),
            )
            batch_rows = batch_payload.get("worlds")
            if isinstance(batch_rows, dict):
                batch_rows = [batch_rows]
            if not isinstance(batch_rows, list):
                continue
            for row in batch_rows:
                if not isinstance(row, dict):
                    continue
                name = str(row.get("name") or "").strip()
                if name and name in names:
                    continue
                if name:
                    names.append(name)
                rows.append(row)
    else:
        _progress(on_progress, {"type": "batch", "index": 1, "total": 1})
        payload = _generate_json(
            adapter,
            req,
            "worlds",
            on_delta=_char_delta(progress_state, on_progress, 1, 1),
        )
        rows = payload.get("worlds")
        if isinstance(rows, dict):
            rows = [rows]
    if not isinstance(rows, list) or not rows:
        raise ValueError("LLM 返回的世界列表为空")
    if wanted:
        rows = rows[:wanted]
    worlds = [clean_world_entry(row) for row in rows]
    return {
        "step": "worlds",
        "worlds": worlds,
        "count": len(worlds),
        "brief": brief_text or None,
        "overwrite": bool(overwrite),
        "provider": str(config.get("adapters.writing") or config.get("providers.writing") or "offline"),
        "batches": batches,
    }

def _ensure_visual_style(adapter, req: TextGenRequest, fields: dict) -> None:
    """确保文风字段 visual_style 有实际内容。

    模型在工具调用里常省略非必填的 visual_style，导致前端「视觉风格」显示空白。
    这里先做一次只补该字段的定向生成；仍失败时用已生成的基调/文笔拼出可读内容兜底。

    参数:
        adapter: 写作适配器。
        req: 原始文风生成请求（复用上下文与元信息）。
        fields: 已清洗的文风字段（就地修改）。

    返回:
        无。
    """
    if str(fields.get("visual_style") or "").strip():
        return
    ask = (
        "只补充 visual_style：用 120-300 字写可拍摄的画面风格，必须覆盖场景、光线、色彩、"
        "材质与镜头质感；不要留空，不要写「暂无」「略」这类占位内容。"
    )
    try:
        retry = replace(req, messages=list(req.messages) + [Message("user", ask)])
        payload = _generate_json(adapter, retry, "style")
        if isinstance(payload.get("style"), dict):
            payload = payload["style"]
        visual = _check_text(payload.get("visual_style"), 500, "visual_style")
        if visual:
            fields["visual_style"] = visual
            return
    except Exception:  # noqa: BLE001 - 定向补一次失败时走兜底，不影响整次生成
        pass
    fallback = "；".join(
        part for part in (str(fields.get("tone") or "").strip(), str(fields.get("prose_style") or "").strip()) if part
    )
    if fallback:
        fields["visual_style"] = fallback[:500]

def generate_settings(store: ProjectStore, config: AppConfig, step: str, overwrite: bool = False, brief: str | None = None, seed: int | None = None) -> dict:
    """生成梗概 / 文风设定（只返回结果，不写盘）。

    参数:
        store: 工程。
        config: 本机配置。
        step: premise|style。
        overwrite: True 时用生成值整体替换该分区；False 时保留已有非空值并记入 skipped。
        brief: 可选的用户补充要求（拼进提示词与请求元信息）。
        seed: 显式固定随机种子；None 表示每次由服务端随机采样（默认）。

    返回:
        {"step", "fields", "skipped"}。

    异常:
        ValueError: step 非法、模型输出不是合法 JSON 或字段校验失败。
    """
    if step not in SETTING_STEPS:
        raise ValueError(f"未知设定步骤: {step}（可选 premise / style）")
    settings_store = SettingsStore(store.root)
    bible = settings_store.load_or_none() or {}
    project = store.load_project()
    work = project.get("work") or {}
    skeleton = settings_store.build_skeleton(work)
    adapter = build_writing_adapter(config, bible)

    label = {"premise": "故事梗概", "style": "文风设定"}[step]
    allowed = {"premise": PREMISE_FIELDS, "style": STYLE_FIELDS}[step]
    target_length = (bible.get("premise") or {}).get("target_length") or (skeleton.get("premise") or {}).get("target_length")
    brief_text = str(brief or "").strip()
    ask = f"为《{work.get('title') or '未命名'}》生成{label}，字段：{', '.join(allowed)}。"
    if step == "style":
        # visual_style 常被模型省略：明确要求写具体、非空，避免前端回显空白
        ask += "其中 visual_style 必填：写 120-300 字具体画面风格（场景、光线、色彩、材质与镜头质感），不能留空。"
    if brief_text:
        ask += f"补充要求：{brief_text}。"
    # 生成某个分区时，把其他已完成的分区与已有世界作为上下文，避免设定互相矛盾
    context_bible = dict(bible)
    context_bible[step] = {}
    world_store = WorldStore(store.root)
    world_items = world_store.list_worlds() if world_store.exists() else []
    context = _project_context(work, context_bible, None, world_items)
    prompt = f"{context}\n\n任务：{ask}" if context else ask
    metadata = {
        "step": step,
        "work": {"title": work.get("title"), "author": work.get("author"), "genre": work.get("genre") or []},
        "existing": _bible_digest(bible),
        "target_length": target_length,
        "brief": brief_text,
        "work_id": work.get("id"),
    }
    # 只有用户显式固定种子时才下发，默认交给服务端随机采样（同一设定重复生成应有差异）
    if seed is not None:
        metadata["extra_body"] = {"seed": int(seed)}
    req = TextGenRequest(
        task="setting",
        system="你是中文小说的设定编辑，设定必须与给定的作品信息一致，只按工具参数输出，不要解释。",
        messages=[Message("user", prompt)],
        stream=True,
        temperature=resolve_temperature(config, "writing"),
        metadata=metadata,
    )
    payload = _generate_json(adapter, req, step)
    if isinstance(payload.get(step), dict):
        payload = payload[step]
    fields = _clean_setting_fields(step, payload)
    if step == "style":
        # 「只补缺失」且已有非空视觉风格时无需额外补写，避免多一次模型调用
        existing_visual = str((bible.get("style") or {}).get("visual_style") or "").strip()
        if overwrite or not existing_visual:
            _ensure_visual_style(adapter, req, fields)

    existing_section = bible.get(step) or {}
    baseline_section = skeleton.get(step) or {}
    final: dict = {}
    skipped: list[str] = []
    for key, value in fields.items():
        current = existing_section.get(key)
        if not overwrite and _is_meaningful(current, baseline_section.get(key), key):
            final[key] = current
            skipped.append(key)
        else:
            final[key] = value
    return {"step": step, "fields": final, "skipped": skipped}

def _clean_setting_fields(step: str, payload: dict) -> dict:
    """按步骤清洗并校验生成字段。

    参数:
        step: premise / style。
        payload: 模型返回的 JSON 对象。

    返回:
        只包含该步骤允许字段的清洗结果。

    异常:
        ValueError: 字段类型、枚举或长度不合法。
    """
    if step == "premise":
        if "logline" not in payload:
            raise ValueError("logline 不能为空")
        fields = {"logline": _check_text(payload.get("logline"), 500, "logline", required=True)}
        for key in ("theme", "core_conflict", "ending_intent", "target_audience"):
            if key in payload:
                fields[key] = _check_text(payload.get(key), SCALAR_LIMITS[key], key)
        if "target_length" in payload:
            fields["target_length"] = _check_choice(payload.get("target_length"), LENGTH_OPTIONS, "target_length")
        return fields

    if step == "style":
        if "tone" not in payload:
            raise ValueError("tone 不能为空")
        fields = {"tone": _check_text(payload.get("tone"), 300, "tone", required=True)}
        if "pov" in payload:
            fields["pov"] = _check_choice(payload.get("pov"), POV_OPTIONS, "pov")
        if "tense" in payload:
            fields["tense"] = _check_choice(payload.get("tense"), TENSE_OPTIONS, "tense")
        if "prose_style" in payload:
            fields["prose_style"] = _check_text(payload.get("prose_style"), 1000, "prose_style")
        if "forbidden" in payload:
            fields["forbidden"] = _clean_str_list(payload.get("forbidden"), 200, "forbidden")
        if "visual_style" in payload:
            visual = _check_text(payload.get("visual_style"), 500, "visual_style")
            # 空串不写入，交给 _ensure_visual_style 补一次，避免覆盖/落空
            if visual:
                fields["visual_style"] = visual
        return fields

    raise ValueError(f"不支持的设定生成步骤: {step}")

def _section_fields(section: str, generated: dict, bible: dict) -> dict:
    """补齐一次生成区块的全部字段（生成值优先，回退已有值/空值）。

    参数:
        section: premise / style。
        generated: generate_settings 返回的字段。
        bible: 当前设定库。

    返回:
        该区块全部字段的 dict。
    """
    allowed = PREMISE_FIELDS if section == "premise" else STYLE_FIELDS
    existing = bible.get(section) or {}
    fields: dict = {}
    for key in allowed:
        value = generated.get(key)
        if value is None:
            value = existing.get(key)
        if value is None:
            if key == "forbidden":
                value = []
            elif key == "target_length":
                value = "novella"
            else:
                value = ""
        fields[key] = value
    return fields

def generate_section(
    store: ProjectStore,
    config: AppConfig,
    section: str,
    brief: str | None = None,
    target_id: str | None = None,
    seed: int | None = None,
    overwrite: bool = True,
) -> dict:
    """一次生成一个区块的全部字段（梗概 / 文风 / 单条世界 / 单个人物）。

    参数:
        store: 工程。
        config: 本机配置。
        section: premise|style|world|character。
        brief: 用户补充要求。
        target_id: world 用世界 ID、character 用角色 ID；命中时作为改写上下文。
        seed: 显式固定随机种子；None 表示每次随机采样。
        overwrite: True 时重新生成并覆盖已有字段；False 时保留已有非空值（只补缺失）。

    返回:
        {"section", "fields", "target_id"?}；world/character 的 fields 为完整条目。

    异常:
        ValueError: section 非法、目标条目不存在或模型输出不合法。
    """
    key = str(section or "").strip()
    if key not in ("premise", "style", "character"):
        raise ValueError(f"不支持的生成区块: {section}")

    settings_store = SettingsStore(store.root)
    bible = settings_store.load_or_none() or {}
    if key in ("premise", "style"):
        # overwrite=True（重新生成）时关闭「只补缺失」，已有字段也会被新结果覆盖；
        # overwrite=False（只补缺失）时保留已有非空值，模型只补空缺。
        result = generate_settings(store, config, key, overwrite=overwrite, brief=brief, seed=seed)
        return {
            "section": key,
            "fields": _section_fields(key, result.get("fields") or {}, bible),
            "skipped": result.get("skipped") or [],
        }

    project = store.load_project()
    work = project.get("work") or {}
    adapter = build_writing_adapter(config, bible)
    brief_text = str(brief or "").strip()
    work_payload = {"title": work.get("title"), "author": work.get("author"), "genre": work.get("genre") or []}

    characters = store.load_characters()["characters"]
    world_store = WorldStore(store.root)
    worlds = world_store.list_worlds() if world_store.exists() else []
    existing_char = next((item for item in characters if item.get("id") == target_id), None) if target_id else None
    if target_id and existing_char is None:
        raise ValueError(f"角色不存在: {target_id}")
    ask = "生成一个完整人物设定，字段：name/role/gender/age_range/personality/background/appearance_prompt/negative_prompt/state.relationship_notes；personality 写 3-5 个短标签，每个不超过 20 字。\n" + _CHARACTER_DESC_RULES
    if existing_char:
        ask += f"这是对已有角色「{existing_char.get('name')}」的改写，保持与现有设定的连贯。"
    if brief_text:
        ask += f"补充要求：{brief_text}。"
    # 单人物同样注入项目设定，避免脱离小说背景
    context = _project_context(work, bible, characters, worlds)
    prompt = f"{context}\n\n任务：{ask}" if context else ask
    req = TextGenRequest(
        task="character",
        system="你是中文小说的人物编辑，人物必须与给定的作品设定一致，只按工具参数输出，不要解释。",
        messages=[Message("user", prompt)],
        stream=True,
        temperature=resolve_temperature(config, "writing"),
        metadata={
            "mode": "single",
            "work": work_payload,
            "premise": bible.get("premise") or {},
            "world": _bible_digest(bible),
            "characters": _characters_payload(characters),
            "existing_character": existing_char,
            "brief": brief_text,
            "seed": work.get("id"),
        },
    )
    payload = _generate_json(adapter, req, "character")
    raw = payload.get("character")
    if not isinstance(raw, dict):
        rows = payload.get("characters")
        raw = rows[0] if isinstance(rows, list) and rows else None
    if not isinstance(raw, dict) and any(key in payload for key in ("name", "role", "personality")):
        # 模型常把人物字段直接平铺返回，不再包一层 character
        raw = payload
    if not isinstance(raw, dict):
        raise ValueError("LLM 返回的人物不是合法 JSON 对象")
    entry = _clean_character_entry(raw)
    if existing_char:
        entry["id"] = existing_char.get("id") or entry.get("id")
    return {"section": "character", "fields": entry, "target_id": target_id}

def polish_field(
    store: ProjectStore,
    config: AppConfig,
    field: str,
    text: str,
    context: str | None = None,
) -> dict:
    """润色单个可填写字段（只返回文本，不写盘）。

    参数:
        store: 工程。
        config: 本机配置。
        field: 白名单内的字段名（见 POLISH_FIELDS）。
        text: 待润色文本。
        context: 可选上下文（如所属人物、章节摘要）。

    返回:
        {"field", "text"}。

    异常:
        ValueError: 字段不在白名单，或没有可润色的内容。
    """
    key = (field or "").strip()
    # 兼容前端按区块传参的 `premise.*` / `style.*` 前缀写法，归一化后再查白名单
    for prefix in ("premise.", "style."):
        if key.startswith(prefix):
            key = key[len(prefix):]
            break
    if key not in POLISH_FIELDS:
        raise ValueError(f"不支持的润色字段: {key or '(空)'}（可选 {'、'.join(POLISH_FIELDS)}）")
    source = (text or "").strip()
    if not source:
        raise ValueError("没有可润色的内容")

    bible = SettingsStore(store.root).load_or_none() or {}
    work = store.load_project().get("work") or {}
    adapter = build_writing_adapter(config, bible)
    # 润色要贴合作品文风：把项目设定与字段上下文一并交给模型
    prompt_parts: list[str] = []
    project_context = _project_context(work, bible)
    if project_context:
        prompt_parts.append(project_context)
    if context:
        prompt_parts.append(f"字段所在上下文：{context}")
    prompt_parts.append(f"待润色字段 {key}：\n{source}")
    req = TextGenRequest(
        task="polish",
        system="你是中文小说编辑，润色要贴合作品设定与文风，保持原意与信息量，只输出 JSON（键为 text）。",
        messages=[Message("user", "\n\n".join(prompt_parts))],
        stream=False,
        temperature=resolve_temperature(config, "polish"),
        metadata={
            "field": key,
            "text": source,
            "context": context or "",
            "work": {"title": work.get("title"), "genre": work.get("genre") or []},
            "seed": work.get("id"),
            "thinking_stage": "rewrite",
        },
    )
    result = adapter.generate(req)
    polished = (result.text or "").strip()
    try:
        payload = _extract_json_object(polished)
        candidate = str(payload.get("text") or "").strip()
        if candidate:
            polished = candidate
    except ValueError:
        # 模型直接返回纯文本时按原文使用，不强求 JSON
        pass
    if not polished:
        raise ValueError("LLM 返回内容不是合法 JSON")
    limit = SCALAR_LIMITS.get(key)
    if limit and len(polished) > limit:
        polished = polished[:limit]
    return {"field": key, "text": polished}

def _is_meaningful(current, baseline, key: str = "") -> bool:
    """判断已有值是否算「用户真实内容」（骨架占位值视为空）。

    只有 logline / era / tone 这类骨架占位字段会与默认值比较，
    其余字段只要非空就保留（避免未经用户确认被生成结果覆盖）。

    参数:
        current: 设定库里的当前值。
        baseline: 设定库骨架里的默认值。
        key: 字段名。

    返回:
        True 表示应当保留，不被生成结果覆盖。
    """
    if current is None:
        return False
    if isinstance(current, list):
        return len(current) > 0
    if isinstance(current, dict):
        return bool(current)
    text = str(current).strip()
    if not text:
        return False
    if key in ("logline", "era", "tone"):
        if "待填写" in text:
            return False
        if baseline is not None and text == str(baseline).strip():
            return False
    return True
