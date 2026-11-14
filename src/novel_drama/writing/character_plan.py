"""人物批量生成：两阶段（先规划名单，再逐个生成详情）。

阶段 1 `plan_characters`：一次工具调用产出名单（姓名 / 定位 / 一句话 / 是否主要），
并把名单落到 `<工程>/.ndw/character_plan.json`，中断后可继续，也可只重试失败项。

阶段 2 `generate_from_plan`：按名单逐个调用 `emit_character` 生成详情；
单个角色失败只记录该角色，不影响其余角色。

原有「一次调用生成多个」的 `service.generate_characters` 保持不变，供旧接口使用。
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from ..adapters.base import Message, TextGenRequest
from ..config import AppConfig
from ..models.ids import now_iso
from ..store import ProjectStore
from ..store.settings_store import SettingsStore
from ..store.world_store import WorldStore
from .service import (
    _CHARACTER_DESC_RULES,
    _clean_character_row,
    _generate_json,
    _progress,
    _project_context,
    _tool_max_tokens,
    build_writing_adapter,
)

#: 计划文件格式版本
PLAN_VERSION = "character_plan/1"

#: 角色定位的中文名（提示词与摘要用）
_ROLE_TEXT = {"protagonist": "主角", "antagonist": "反派", "supporting": "配角", "minor": "龙套"}


def plan_file(store: ProjectStore) -> Path:
    """计划文件路径：`<工程>/.ndw/character_plan.json`。

    参数:
        store: 工程。

    返回:
        Path。
    """
    return store.root / ".ndw" / "character_plan.json"


def _write_json(path: Path, data: dict) -> None:
    """原子写入 JSON（先写临时文件再替换）。

    参数:
        path: 目标文件。
        data: 待写入内容。

    返回:
        无。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def load_plan(store: ProjectStore) -> dict | None:
    """读取已落盘的人物名单；不存在或格式不对时返回 None。

    参数:
        store: 工程。

    返回:
        计划 dict 或 None。
    """
    path = plan_file(store)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("items"), list) else None


def save_plan(store: ProjectStore, plan: dict) -> None:
    """保存人物名单（含每人的生成状态），供中断后继续。

    参数:
        store: 工程。
        plan: 计划 dict。

    返回:
        无。
    """
    plan["updated_at"] = now_iso()
    _write_json(plan_file(store), plan)


def estimate_character_count(chapters: int, existing: int) -> tuple[int, str]:
    """未指定人数时的估算规则：按章节规模推算，再扣除已有角色。

    规则：`2 + 每 3 章 1 名`，下限 4、上限 12；扣除已有角色数，至少 1 名。

    参数:
        chapters: 当前章节数。
        existing: 已有角色数（不含旁白）。

    返回:
        (本次建议生成人数, 依据说明)。
    """
    if chapters <= 0:
        base = 6
        rule = "尚无章节，按常规中篇规模建议 6 名"
    else:
        base = max(4, min(12, 2 + math.ceil(chapters / 3)))
        rule = f"按 {chapters} 章规模估算（2 + 每 3 章 1 名，范围 4-12）"
    count = max(1, base - max(0, existing))
    return count, f"{rule}，扣除已有 {existing} 名角色后本次生成 {count} 名"


def _outline_lines(chapters: list[dict]) -> list[str]:
    """把章节标题/摘要压成名单规划用的篇幅说明。

    参数:
        chapters: `store.list_chapters()` 的结果。

    返回:
        每章一行文本；无章节时返回空列表。
    """
    lines: list[str] = []
    for item in chapters[:30]:
        title = str(item.get("title") or "").strip()
        outline = str(item.get("outline") or "").strip().replace("\n", " ")[:60]
        if title or outline:
            lines.append(f"{item.get('index')}. {title}" + (f"：{outline}" if outline else ""))
    return lines


def plan_characters(
    store: ProjectStore,
    config: AppConfig,
    count: int | None = None,
    brief: str | None = None,
) -> dict:
    """阶段 1：一次调用产出人物名单并落盘。

    参数:
        store: 工程。
        config: 本机配置。
        count: 用户指定的人数；为空时按章节规模估算。
        brief: 用户补充要求。

    返回:
        `{version, created_at, title, brief, requested_count, basis, model_basis, items}`，
        items 每项含 `{name, role, one_line, main, status}`，status 取 pending/skipped。

    异常:
        ValueError: 人数非法、模型未返回名单。
    """
    settings_store = SettingsStore(store.root)
    bible = settings_store.load_or_none() or {}
    project = store.load_project()
    work = project.get("work") or {}
    characters = store.load_characters()["characters"]
    existing = [item for item in characters if item.get("role") != "narrator"]
    world_store = WorldStore(store.root)
    worlds = world_store.list_worlds() if world_store.exists() else []
    chapters = store.list_chapters()
    brief_text = str(brief or "").strip()

    wanted = None
    if count not in (None, "", 0):
        try:
            wanted = int(count)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"人物数量必须是整数：{count}") from exc
        if wanted < 1:
            raise ValueError(f"人物数量必须大于 0：{wanted}")
        basis = f"按用户指定生成 {wanted} 名角色"
    else:
        wanted, basis = estimate_character_count(len(chapters), len(existing))

    outline_lines = _outline_lines(chapters)
    ask = (
        f"为《{work.get('title') or '未命名'}》规划人物名单：本次共 {wanted} 名角色。\n"
        "只输出名单，不要写详细设定：每人给出 name（姓名）、role（定型）、"
        "one_line（一句话定位：身份 + 在故事里的作用）、main（是否主要角色）。\n"
        "至少 1 名主角；已有角色不要再出现在名单里；姓名之间不要重复。"
    )
    if outline_lines:
        ask += "\n章节规模：\n" + "\n".join(outline_lines)
    if brief_text:
        ask += f"\n补充要求：{brief_text}。"
    context = _project_context(work, bible, existing, worlds)
    prompt = f"{context}\n\n任务：{ask}" if context else ask

    adapter = build_writing_adapter(config, bible)
    req = TextGenRequest(
        task="character",
        system="你是中文小说的人物编辑，人物必须与给定的作品设定一致，只按工具参数输出名单，不要解释。",
        messages=[Message("user", prompt)],
        stream=True,
        temperature=0.7,
        max_tokens=_tool_max_tokens(config, "character_plan", wanted),
        metadata={
            "work": {"title": work.get("title"), "author": work.get("author"), "genre": work.get("genre") or []},
            "premise": bible.get("premise") or {},
            "count": wanted,
            "brief": brief_text,
            "seed": work.get("id"),
        },
    )
    payload = _generate_json(adapter, req, "character_plan")
    rows = payload.get("characters")
    if not isinstance(rows, list) or not rows:
        keys = "、".join(sorted(str(key) for key in payload.keys())) or "（空对象）"
        raise ValueError(f"人物名单为空：响应顶层字段为 {keys}，未包含非空的 characters 数组")

    existing_names = {str(item.get("name") or "").strip() for item in existing}
    items: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()[:60]
        if not name or name in seen:
            continue
        seen.add(name)
        role = str(row.get("role") or "").strip()
        items.append(
            {
                "name": name,
                "role": role if role in _ROLE_TEXT else "supporting",
                "one_line": str(row.get("one_line") or "").strip()[:120],
                "main": bool(row.get("main")),
                "status": "skipped" if name in existing_names else "pending",
            }
        )
    if not items:
        raise ValueError("人物名单为空：模型返回的条目都没有可用姓名")

    plan = {
        "version": PLAN_VERSION,
        "created_at": now_iso(),
        "title": work.get("title"),
        "brief": brief_text,
        "requested_count": wanted,
        "basis": basis,
        "model_basis": str(payload.get("basis") or "").strip()[:300],
        "items": items,
    }
    save_plan(store, plan)
    return plan


def _character_prompt(
    work: dict,
    context: str,
    item: dict,
    roster: list[dict],
    done_names: list[str],
    brief_text: str,
) -> str:
    """拼单个角色的详情生成提示词。

    参数:
        work: 作品信息。
        context: 项目上下文（题材/设定/已有角色）。
        item: 名单条目。
        roster: 完整名单（用于对齐人物关系）。
        done_names: 已生成的角色姓名。
        brief_text: 用户补充要求。

    返回:
        提示词文本。
    """
    name = item["name"]
    role_text = _ROLE_TEXT.get(item.get("role"), "配角")
    lines: list[str] = []
    if context:
        lines.append(context)
    lines.append(f"任务：为《{work.get('title') or '未命名'}》生成人物「{name}」的完整设定。")
    detail = f"名单定位：{role_text}"
    if item.get("one_line"):
        detail += f"；一句话简介：{item['one_line']}"
    detail += "；主要角色" if item.get("main") else "；次要角色"
    lines.append(detail + "。")
    others = [other for other in roster if other.get("name") != name]
    if others:
        roster_text = "、".join(
            f"{other['name']}（{_ROLE_TEXT.get(other.get('role'), '配角')}，{other.get('one_line') or '无简介'}）"
            for other in others
        )
        lines.append("名单中的其他角色（用于对齐关系，不要写成当前人物的别名）：" + roster_text + "。")
    if done_names:
        lines.append("已生成人物（关系可以引用）：" + "、".join(done_names) + "。")
    if brief_text:
        lines.append(f"补充要求：{brief_text}。")
    lines.append(_CHARACTER_DESC_RULES)
    lines.append(f"只输出「{name}」这一个人物，姓名保持不变。")
    return "\n".join(lines)


def generate_from_plan(
    store: ProjectStore,
    config: AppConfig,
    plan: dict,
    statuses: tuple[str, ...] = ("pending",),
    on_progress=None,
) -> dict:
    """阶段 2：按名单逐个生成人物详情。

    参数:
        store: 工程。
        config: 本机配置。
        plan: `plan_characters` 返回或 `load_plan` 读到的计划。
        statuses: 本轮要生成的状态集合（pending=新计划；failed=只重试失败的；
            pending+failed=中断后继续）。
        on_progress: 进度回调，事件为 `batch{index,total,name}` 与 `chars{...}`。

    返回:
        `{characters, stock_characters, plan, success, failed, skipped, failures, summary}`；
        failures 每项 `{name, message}`。

    异常:
        ValueError: 计划里没有可生成条目。
    """
    items = plan.get("items") or []
    targets = [item for item in items if item.get("status") in statuses]
    if not targets:
        raise ValueError("人物名单里没有待生成的角色（可能已全部完成）")

    settings_store = SettingsStore(store.root)
    bible = settings_store.load_or_none() or {}
    project = store.load_project()
    work = project.get("work") or {}
    world_store = WorldStore(store.root)
    worlds = world_store.list_worlds() if world_store.exists() else []
    adapter = build_writing_adapter(config, bible)
    context = _project_context(work, bible, store.load_characters()["characters"], worlds)
    brief_text = str(plan.get("brief") or "").strip()
    known_names = {str(item.get("name") or "").strip() for item in store.load_characters()["characters"]}

    characters: list[dict] = []
    failures: list[dict] = []
    # 名单里因同名已存在而预先标记为跳过的角色，也计入本次汇总
    skipped: list[str] = [str(item.get("name")) for item in items if item.get("status") == "skipped"]
    done_names: list[str] = []
    total = len(targets)
    for index, item in enumerate(targets, start=1):
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        if name in known_names:
            item["status"] = "skipped"
            skipped.append(name)
            save_plan(store, plan)
            continue
        _progress(on_progress, {"type": "batch", "index": index, "total": total, "name": name})
        state = {"chars": 0}

        def on_delta(text, _item=item, _index=index, _name=name, _state=state):
            """累计本角色的接收长度并上报 chars 事件（附带最新原文片段）。"""
            if not text:
                return
            _state["chars"] += len(str(text))
            _state["tail"] = (str(_state.get("tail") or "") + str(text))[-200:]
            _progress(
                on_progress,
                {
                    "type": "chars",
                    "chars": _state["chars"],
                    "sample": _state["tail"],
                    "index": _index,
                    "total": total,
                    "name": _name,
                },
            )

        prompt = _character_prompt(work, context, item, items, done_names, brief_text)
        req = TextGenRequest(
            task="character",
            system="你是中文小说的人物编辑，人物必须与给定的作品设定一致，只按工具参数输出，不要解释。",
            messages=[Message("user", prompt)],
            stream=True,
            temperature=0.8,
            max_tokens=_tool_max_tokens(config, "character", 1),
            metadata={
                "work": {"title": work.get("title"), "author": work.get("author"), "genre": work.get("genre") or []},
                "count": 1,
                "mode": "single",
                "brief": brief_text,
                "plan": {"name": name, "role": item.get("role"), "one_line": item.get("one_line")},
                "seed": work.get("id"),
            },
        )
        try:
            payload = _generate_json(adapter, req, "character", on_delta=on_delta)
            row = _clean_character_row(payload, index - 1)
            if row["name"] in known_names:
                item["status"] = "skipped"
                skipped.append(row["name"])
            else:
                characters.append(row)
                known_names.add(row["name"])
                done_names.append(row["name"])
                item["status"] = "done"
                item.pop("error", None)
        except Exception as exc:  # 单个角色失败不影响其他角色
            message = getattr(exc, "message", None) or str(exc) or exc.__class__.__name__
            item["status"] = "failed"
            item["error"] = message
            failures.append({"name": name, "message": message})
        save_plan(store, plan)

    failed_text = "；".join(f"{item['name']}：{item['message']}" for item in failures)
    summary = f"成功 {len(characters)}、失败 {len(failures)}"
    if failures:
        summary += f"（{failed_text}）"
    if skipped:
        summary += f"、跳过 {len(skipped)}（{'、'.join(skipped)}）"
    return {
        "characters": characters,
        "stock_characters": [],
        "plan": {
            "basis": plan.get("basis"),
            "model_basis": plan.get("model_basis"),
            "total": len(items),
            "file": str(plan_file(store)),
        },
        "success": len(characters),
        "failed": len(failures),
        "skipped": skipped,
        "failures": failures,
        "summary": summary,
    }


#: 后续音色 / 出图直接取用的描述性字段
DESCRIPTION_FIELDS = (
    "voice_description_zh",
    "voice_description_en",
    "appearance_desc_zh",
    "appearance_desc_en",
    "appearance_negative_zh",
    "appearance_negative_en",
)

#: 各描述字段的最大长度（与 schema 对齐）
_DESC_LIMITS = {
    "voice_description_zh": 1000,
    "voice_description_en": 1000,
    "appearance_desc_zh": 1200,
    "appearance_desc_en": 1200,
    "appearance_negative_zh": 800,
    "appearance_negative_en": 800,
}


def missing_description_fields(character: dict) -> list[str]:
    """列出角色缺失的描述性字段。

    参数:
        character: 角色档案。

    返回:
        缺失字段名列表（空列表表示已齐全）。
    """
    return [field for field in DESCRIPTION_FIELDS if not str(character.get(field) or "").strip()]


def fill_character_descriptions(
    store: ProjectStore,
    config: AppConfig,
    names: list[str] | None = None,
    on_progress=None,
    overwrite: bool = False,
) -> dict:
    """给已有人物补全或重新生成描述性字段。

    默认只补缺失、不覆盖已有内容；`overwrite=True` 时重新生成全部描述字段并覆盖，
    用于页面上的「重新生成」入口。

    逐个角色调用 `emit_character`；单个失败只记录，不影响其它角色；
    每次成功立即写盘，重跑时会自动跳过已补全的角色（失败项可重试）。

    参数:
        store: 工程。
        config: 本机配置。
        names: 只处理指定角色（ID 或姓名）；为空表示全部非旁白角色。
        on_progress: 进度回调。事件依次为 `total{total,pending,skipped,narrator}`、
            `batch{index,total,name}`、`chars{chars,index,total,name}`、
            `item{status,reason?|message?}`；跳过项也会出现在 batch 与 item 里。
        overwrite: True 时重新生成全部描述字段并覆盖已有内容。

    返回:
        {"total", "updated", "skipped", "failures", "summary", "overwrite"}。
    """
    settings_store = SettingsStore(store.root)
    bible = settings_store.ensure()
    table = store.load_characters()
    characters = table.get("characters") or []
    work = store.load_project().get("work") or {}
    world_store = WorldStore(store.root)
    worlds = world_store.list_worlds() if world_store.exists() else []
    context = _project_context(work, bible, characters, worlds)
    adapter = build_writing_adapter(config, bible)

    wanted: list[dict] = []
    steps: list[dict] = []
    updated: list[str] = []
    skipped: list[str] = []
    failures: list[dict] = []
    keys = {str(item).strip() for item in (names or []) if str(item).strip()}
    for character in characters:
        name = str(character.get("name") or character.get("id") or "")
        if character.get("role") == "narrator":
            # 旁白不补描述，但进度里要体现，避免看起来像被静默漏掉
            steps.append({"character": character, "name": name, "skip": "旁白"})
            continue
        if keys and character.get("id") not in keys and character.get("name") not in keys:
            continue
        if overwrite or missing_description_fields(character):
            wanted.append(character)
            steps.append({"character": character, "name": name, "skip": "", "overwrite": overwrite})
        else:
            skipped.append(name)
            steps.append({"character": character, "name": name, "skip": "已齐全"})

    total = len(wanted)
    progress_total = len(steps)
    emit = on_progress if on_progress is not None else (lambda event: None)
    # 进度口径用「全部待处理角色」（含跳过项），返回值 total 仍只统计需要补全的角色
    emit({
        "type": "total",
        "total": progress_total,
        "pending": total,
        "skipped": len(skipped),
        "narrator": sum(1 for step in steps if step["skip"] == "旁白"),
    })
    for index, step in enumerate(steps, start=1):
        character = step["character"]
        name = step["name"]
        # 直接用 on_progress 上报，让页面断开时的取消异常能中断生成（_progress 会吞异常）
        emit({"type": "batch", "index": index, "total": progress_total, "name": name})
        if step["skip"]:
            emit({
                "type": "item",
                "index": index,
                "total": progress_total,
                "name": name,
                "status": "skipped",
                "reason": step["skip"],
            })
            continue
        missing = missing_description_fields(character)
        # 重新生成模式下要覆盖全部描述字段；只补缺失模式只处理空缺项
        targets = list(DESCRIPTION_FIELDS) if overwrite else missing
        profile = {
            key: value
            for key, value in character.items()
            if key in ("name", "role", "gender", "age_range", "personality", "background",
                       "appearance_prompt", "negative_prompt")
        }
        if overwrite:
            ask = f"重新生成以下字段并覆盖已有内容：{'、'.join(targets)}。\n"
        else:
            ask = f"只补以下缺失字段：{'、'.join(targets)}；已有内容的字段一律不要输出或改写。\n"
        prompt = (
            f"{context}\n\n" if context else ""
        ) + (
            f"已有角色「{name}」的档案（不要改写）：{json.dumps(profile, ensure_ascii=False)}\n"
            f"{ask}"
        ) + _CHARACTER_DESC_RULES + f"\n只输出「{name}」这一个人物，姓名保持不变。"
        req = TextGenRequest(
            task="character",
            system="你是中文小说的人物编辑，只按工具参数输出描述字段，不要解释。",
            messages=[Message("user", prompt)],
            stream=True,
            temperature=0.7,
            max_tokens=_tool_max_tokens(config, "character", 1),
            metadata={
                "mode": "single",
                "work": {"title": work.get("title"), "author": work.get("author"), "genre": work.get("genre") or []},
                "premise": bible.get("premise") or {},
                "characters": [{"id": character.get("id"), "name": name, "role": character.get("role")}],
                "existing_character": character,
                "brief": "重新生成描述性字段并覆盖已有内容" if overwrite else "只补缺失的描述性字段，不覆盖已有内容",
                "overwrite": bool(overwrite),
                "seed": work.get("id"),
            },
        )
        state = {"chars": 0}

        def on_delta(text: str) -> None:
            """累计接收长度并上报（直接调用回调，断开时可中断；附带最新原文片段）。"""
            if not text:
                return
            state["chars"] += len(str(text))
            state["tail"] = (str(state.get("tail") or "") + str(text))[-200:]
            emit({
                "type": "chars",
                "chars": state["chars"],
                "sample": state["tail"],
                "index": index,
                "total": progress_total,
                "name": name,
            })

        try:
            payload = _generate_json(adapter, req, "character", on_delta=on_delta if on_progress else None)
            # 兼容两种工具返回：{"character": {...}} 与 {"characters": [{...}]}（离线适配器走后者）
            raw = payload.get("character")
            if not isinstance(raw, dict):
                rows = payload.get("characters")
                raw = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else payload
            changed = 0
            for field in targets:
                value = str((raw or {}).get(field) or "").strip()
                if value:
                    character[field] = value[: _DESC_LIMITS[field]]
                    changed += 1
            # 旧字段为空时顺带补齐，已有内容绝不覆盖
            if not character.get("appearance_prompt") and character.get("appearance_desc_en"):
                character["appearance_prompt"] = character["appearance_desc_en"]
            if not character.get("negative_prompt") and character.get("appearance_negative_en"):
                character["negative_prompt"] = character["appearance_negative_en"]
            if changed:
                character["updated_at"] = now_iso()
                store.save_characters(table)
                updated.append(name)
                emit({
                    "type": "item",
                    "index": index,
                    "total": progress_total,
                    "name": name,
                    "status": "done",
                    "fields": changed,
                })
            else:
                reason = "模型没有返回描述字段" if overwrite else "模型没有返回缺失字段"
                failures.append({"name": name, "message": reason})
                emit({
                    "type": "item",
                    "index": index,
                    "total": progress_total,
                    "name": name,
                    "status": "failed",
                    "message": reason,
                })
        except Exception as exc:  # 单人失败不影响其他人
            failures.append({"name": name, "message": str(exc) or exc.__class__.__name__})
            emit({
                "type": "item",
                "index": index,
                "total": progress_total,
                "name": name,
                "status": "failed",
                "message": str(exc) or exc.__class__.__name__,
            })
    failed_text = "；".join(f"{item['name']}：{item['message']}" for item in failures)
    summary = f"{'重新生成' if overwrite else '补全'} {len(updated)}、失败 {len(failures)}"
    if failures:
        summary += f"（{failed_text}）"
    if skipped:
        summary += f"、已齐全 {len(skipped)}"
    return {
        "total": total,
        "updated": updated,
        "skipped": skipped,
        "failures": failures,
        "summary": summary,
        "overwrite": bool(overwrite),
    }
