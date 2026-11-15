"""用例：生成角色（分批调用模型，清洗后写入角色表）。"""

from __future__ import annotations

import json
from dataclasses import replace

from ...adapters.llm.contracts import Message, TextGenRequest
from ...adapters.persistence import ProjectStore
from ...adapters.persistence.settings_store import SettingsStore
from ...adapters.persistence.world_store import WorldStore
from ...infrastructure.config import AppConfig, resolve_temperature
from ..story.chapter_context import _bible_digest, _project_context
from ..support.llm_calls import (
    _char_delta,
    _generate_json,
    _progress,
    build_writing_adapter,
)
from .character_fields import (
    _CHARACTER_DESC_RULES,
    _attach_profile_source,
    _build_stock_characters,
    _clean_character_row,
    _clean_voice_profile,
    _default_stock_characters,
)


def generate_characters(
    store: ProjectStore,
    config: AppConfig,
    count: int | None = None,
    brief: str | None = None,
    batch_size: int | None = None,
    on_progress=None,
) -> dict:
    """生成人物设定（只返回结果，不写盘）。

    参数:
        store: 工程。
        config: 本机配置。
        count: 期望人物总数；为空时由模型按需求自行判断需要几个角色。
        brief: 用户补充要求（拼进提示词，离线实现会写进人物小传/性格）。
        batch_size: 每批条数；None 时取 `writing.multi_turn.batch_size_characters`（默认 5）。
        on_progress: 进度回调（batch/chars 事件）；仅页面流式接口传入。

    返回:
        {"characters": [{name, role, gender, age_range, personality, background,
        appearance_prompt, negative_prompt, relationships}], "batches": N}。

    异常:
        ValueError: 模型输出不是合法 JSON 或重名。
    """
    settings_store = SettingsStore(store.root)
    bible = settings_store.load_or_none() or {}
    project = store.load_project()
    work = project.get("work") or {}
    existing_characters = store.load_characters()["characters"]
    world_store = WorldStore(store.root)
    worlds = world_store.list_worlds() if world_store.exists() else []
    adapter = build_writing_adapter(config, bible)
    brief_text = str(brief or "").strip()

    ask = f"为《{work.get('title') or '未命名'}》生成人物设定：根据需求自行判断需要几个角色（至少 1 名主角，其余按剧情需要决定数量与定位），不要凑数。"
    if count:
        ask += f"本次共生成 {count} 个角色。"
    if brief_text:
        ask += f"补充要求：{brief_text}。"
    ask += "\n" + _CHARACTER_DESC_RULES
    # 无论需求怎么写，都先把作品梗概/文风/世界/已有角色交给模型，避免脱离设定
    context = _project_context(work, bible, existing_characters, worlds)
    prompt = f"{context}\n\n任务：{ask}" if context else ask
    mt = config.get("writing.multi_turn") or {}
    try:
        batch = int(batch_size or mt.get("batch_size_characters") or 5)
        total = int(count) if count else 0
    except (TypeError, ValueError):
        batch = 5
        total = 0
    batch = max(1, batch)

    req = TextGenRequest(
        task="character",
        system="你是中文小说的人物编辑，人物必须与给定的作品设定一致，只按工具参数输出，不要解释。",
        messages=[Message("user", prompt)],
        stream=True,
        temperature=resolve_temperature(config, "writing"),
        metadata={
            "work": {"title": work.get("title"), "author": work.get("author"), "genre": work.get("genre") or []},
            "premise": bible.get("premise") or {},
            "world": _bible_digest(bible),
            "count": count,
            "brief": brief_text,
            "seed": work.get("id"),
        },
    )

    batches = 1
    stock_rows: list[dict] = []
    narrator_profile_raw: dict | None = None
    last_payload: dict = {}
    progress_state = {"chars": 0}
    if total > batch:
        # 数量超过单批上限：按批生成，每批在固定前缀后追加本批要求与已生成名单
        batches = (total + batch - 1) // batch
        rows: list = []
        names: list[str] = []
        for index in range(batches):
            take = min(batch, total - len(rows))
            if take <= 0:
                break
            progress_index = index + 1
            _progress(on_progress, {"type": "batch", "index": progress_index, "total": batches, "unit": "batch"})
            lines = [f"本批生成 {take} 条人物（第 {index + 1}/{batches} 批）。"]
            if names:
                lines.append("已生成人物（不要重复姓名）：" + "、".join(names))
            batch_req = replace(req, messages=list(req.messages) + [Message("user", "\n".join(lines))])
            batch_req.metadata = dict(req.metadata or {}, count=take)
            batch_payload = _generate_json(
                adapter,
                batch_req,
                "characters",
                on_delta=_char_delta(progress_state, on_progress, progress_index, batches),
            )
            last_payload = batch_payload
            if not stock_rows and isinstance(batch_payload.get("stock_characters"), list):
                stock_rows = [row for row in batch_payload["stock_characters"] if isinstance(row, dict)]
            if narrator_profile_raw is None and isinstance(batch_payload.get("narrator_profile"), dict):
                narrator_profile_raw = batch_payload["narrator_profile"]
            batch_rows = batch_payload.get("characters")
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
            "characters",
            on_delta=_char_delta(progress_state, on_progress, 1, 1),
        )
        last_payload = payload
        rows = payload.get("characters")
        if isinstance(payload.get("stock_characters"), list):
            stock_rows = [row for row in payload["stock_characters"] if isinstance(row, dict)]
        if isinstance(payload.get("narrator_profile"), dict):
            narrator_profile_raw = payload["narrator_profile"]
    if not isinstance(rows, list) or not rows:
        # 带上响应顶层字段与摘录，便于区分「模型不支持 tools / 字段名不对 / 数组为空」
        keys = "、".join(sorted(str(key) for key in last_payload.keys())) or "（空对象）"
        excerpt = json.dumps(last_payload, ensure_ascii=False)[:300]
        raise ValueError(
            f"LLM 返回的人物列表为空：响应顶层字段为 {keys}，未包含非空的 characters 数组；"
            f"响应摘录：{excerpt}"
        )

    characters: list[dict] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        entry = _clean_character_row(row, index)
        name = entry["name"]
        if name in seen:
            raise ValueError(f"人物姓名重复: {name}")
        seen.add(name)
        characters.append(entry)

    stock_characters = _build_stock_characters(stock_rows, seen)
    narrator_profile = None
    if narrator_profile_raw:
        profile = _clean_voice_profile(narrator_profile_raw.get("voice_profile"))
        if profile:
            narrator_profile = {"voice_profile": _attach_profile_source({"name": "旁白"}, profile)}
    if not stock_characters:
        stock_characters = _default_stock_characters()

    # 角色数量与定位由需求决定，这里只保证至少有一个主角，不再限定配角人数
    if not any(item["role"] == "protagonist" for item in characters):
        raise ValueError("人物里至少需要 1 个 protagonist（主角）")
    return {
        "characters": characters,
        "stock_characters": stock_characters,
        "narrator_profile": narrator_profile,
        "batches": batches,
    }
