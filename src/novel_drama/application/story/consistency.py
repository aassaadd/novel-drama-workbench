"""生成后的角色一致性检查：只产出问题清单，不改正文、不改角色卡。

检查分两类：
1. 派生检查（不依赖模型）：计划未出场、计划外出场、POV 未在正文出现、未知说话人；
2. 模型检查：口吻偏离角色卡、说出未知信息、关系言行矛盾。
结果落到 `.ndw/consistency/<章节>.json`，页面逐条展示。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ...adapters.llm.contracts import Message, TextGenRequest
from ...adapters.persistence import ProjectStore
from ...adapters.persistence.settings_store import SettingsStore
from ...infrastructure.config import AppConfig, resolve_temperature
from ...models.ids import now_iso
from ..support.llm_calls import build_writing_adapter
from .outline_bind import chapter_binding

#: 问题类型（与页面展示的中文标签对应）
ISSUE_KINDS = ("speech", "knowledge", "relation", "unplanned", "pov", "other")

#: 问题级别
LEVELS = ("high", "medium", "low")


def consistency_dir(store: ProjectStore) -> Path:
    """返回一致性检查结果目录（不存在时创建）。

    参数:
        store: 工程。

    返回:
        `.ndw/consistency` 目录。
    """
    path = store.root / ".ndw" / "consistency"
    path.mkdir(parents=True, exist_ok=True)
    return path


def consistency_file(store: ProjectStore, chapter_id: str) -> Path:
    """返回某章的一致性检查结果文件。

    参数:
        store: 工程。
        chapter_id: 章节 ID。

    返回:
        结果 JSON 路径。
    """
    return consistency_dir(store) / f"{chapter_id}.json"


def _parse_json(text: str) -> dict:
    """从模型输出中解析 JSON（容忍代码块与前后说明）。

    参数:
        text: 模型原始输出。

    返回:
        解析后的 dict；失败时返回空 dict。
    """
    raw = str(text or "").strip()
    match = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw)
    if match:
        raw = match.group(1).strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        value = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _derived_issues(binding: dict) -> list[dict]:
    """把绑定对账结果转成一致性问题（不依赖模型，确定可复现）。

    参数:
        binding: `chapter_binding` 的返回值。

    返回:
        问题列表。
    """
    issues: list[dict] = []
    for item in (binding.get("audit") or {}).get("planned_not_used") or []:
        issues.append(
            {
                "kind": "unplanned",
                "level": "medium",
                "character_id": item.get("id"),
                "name": item.get("name"),
                "message": f"计划出场的「{item.get('name')}」在本章正文里没有出现",
                "evidence": "",
                "source": "derived",
            }
        )
    for item in (binding.get("audit") or {}).get("unplanned_appeared") or []:
        issues.append(
            {
                "kind": "unplanned",
                "level": "low",
                "character_id": item.get("id"),
                "name": item.get("name"),
                "message": f"「{item.get('name')}」出现在正文里，但本章计划没有安排该角色",
                "evidence": "",
                "source": "derived",
            }
        )
    if (binding.get("audit") or {}).get("pov_not_in_prose"):
        pov = binding.get("pov") or {}
        issues.append(
            {
                "kind": "pov",
                "level": "high",
                "character_id": pov.get("id"),
                "name": pov.get("name"),
                "message": f"视角人物「{pov.get('name')}」没有在正文场景或台词里出现，视角可能被写丢",
                "evidence": "",
                "source": "derived",
            }
        )
    for item in (binding.get("audit") or {}).get("unknown_speakers") or []:
        issues.append(
            {
                "kind": "other",
                "level": "medium",
                "character_id": item.get("speaker_id"),
                "name": item.get("speaker_id"),
                "message": f"有 {item.get('lines')} 行台词无法对应到角色卡",
                "evidence": item.get("sample") or "",
                "source": "derived",
            }
        )
    if (binding.get("audit") or {}).get("missing_refs"):
        for item in binding["audit"]["missing_refs"]:
            issues.append(
                {
                    "kind": "other",
                    "level": "high",
                    "character_id": item.get("id"),
                    "name": item.get("id"),
                    "message": "绑定里引用了不存在的角色（角色可能已被删除）",
                    "evidence": "",
                    "source": "derived",
                }
            )
    return issues


def check_chapter_consistency(
    store: ProjectStore,
    config: AppConfig,
    chapter_id: str,
    excerpt_chars: int = 6000,
) -> dict:
    """检查本章角色一致性并保存结果（不改正文、不改角色卡）。

    参数:
        store: 工程。
        config: 本机配置。
        chapter_id: 章节 ID。
        excerpt_chars: 正文最多读取的字符数。

    返回:
        `{chapter_id, created_at, issues, counts}`，并写入结果文件。

    异常:
        ValueError: 本章正文为空。
    """
    chapter = store.load_chapter(chapter_id)
    content = str(chapter.get("content") or "").strip()
    if not content:
        raise ValueError("本章还没有正文：无法做角色一致性检查")
    binding = chapter_binding(store, chapter_id)
    derived = _derived_issues(binding)
    characters = [
        item
        for item in store.load_characters()["characters"]
        if item.get("role") != "narrator"
    ]
    cast_ids = {str(item.get("id")) for item in characters if item.get("id")}
    focus_ids = {row["id"] for row in binding.get("characters") or [] if row.get("planned") or row.get("is_pov")}
    focus = [item for item in characters if str(item.get("id")) in focus_ids] or characters
    brief = [
        {
            "character_id": item.get("id"),
            "name": item.get("name"),
            "personality": item.get("personality") or [],
            "speech_style": item.get("speech_style") or {},
            "situation": (item.get("state") or {}).get("situation"),
            "knowledge": (item.get("state") or {}).get("knowledge") or [],
            "relationships": (item.get("state") or {}).get("relationship_notes") or {},
        }
        for item in focus
    ]
    pov_mode = binding.get("pov_mode")
    pov = binding.get("pov") or {}
    prompt = (
        "请对照角色卡检查本章正文是否有角色一致性问题。只输出 JSON，不要改正文：\n"
        '{"issues":[{"character_id":"chr_xxx","kind":"speech|knowledge|relation|other",'
        '"level":"high|medium|low","message":"问题描述","evidence":"正文原句（不超过60字）"}]}\n'
        "检查点：\n"
        "1) 说话方式：语速、用词、口音、口头禅是否明显偏离角色卡；不同角色口吻是否雷同；\n"
        "2) 已知信息：角色是否说出自己不可能知道的事实；\n"
        "3) 关系：言行是否与关系备注矛盾（盟友互相出卖、仇敌突然交心都要指出）；\n"
        "4) 视角：是否越界写了非视角人物的内心。\n"
        "只使用下面给出的 character_id；没有问题时输出 {\"issues\":[]}。\n\n"
        f"本章视角：{pov_mode}；视角人物：{pov.get('name') or '未绑定'}\n"
        f"角色卡：{json.dumps(brief, ensure_ascii=False)}\n\n"
        f"本章正文：\n{content[:excerpt_chars]}"
    )
    adapter = build_writing_adapter(config, SettingsStore(store.root).ensure())
    req = TextGenRequest(
        task="consistency",
        system="你是小说角色一致性审校，只输出问题 JSON，不改写正文。",
        messages=[Message("user", prompt)],
        stream=False,
        temperature=resolve_temperature(config, "writing"),
        metadata={
            "chapter_id": chapter_id,
            "characters": brief,
            "content": content[:excerpt_chars],
            "pov_name": pov.get("name"),
            "pov_mode": pov_mode,
            "thinking_stage": "consistency",
        },
    )
    result = adapter.generate(req)
    payload = _parse_json(result.text)
    issues = list(derived)
    for raw in payload.get("issues") or []:
        if not isinstance(raw, dict):
            continue
        character_id = str(raw.get("character_id") or "").strip()
        if character_id and character_id not in cast_ids:
            continue
        kind = str(raw.get("kind") or "other").strip()
        level = str(raw.get("level") or "medium").strip()
        message = str(raw.get("message") or "").strip()[:300]
        if not message:
            continue
        issues.append(
            {
                "kind": kind if kind in ISSUE_KINDS else "other",
                "level": level if level in LEVELS else "medium",
                "character_id": character_id or None,
                "name": next(
                    (item.get("name") for item in characters if item.get("id") == character_id), ""
                ),
                "message": message,
                "evidence": str(raw.get("evidence") or "").strip()[:200],
                "source": "model",
            }
        )
    counts = {
        "high": sum(1 for item in issues if item["level"] == "high"),
        "medium": sum(1 for item in issues if item["level"] == "medium"),
        "low": sum(1 for item in issues if item["level"] == "low"),
        "total": len(issues),
    }
    record = {
        "chapter_id": chapter_id,
        "created_at": now_iso(),
        "model": result.model,
        "issues": issues,
        "counts": counts,
    }
    consistency_file(store, chapter_id).write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return record


def load_consistency(store: ProjectStore, chapter_id: str) -> dict:
    """读取某章最近一次一致性检查结果。

    参数:
        store: 工程。
        chapter_id: 章节 ID。

    返回:
        结果记录；没有时返回 `{chapter_id, issues: [], counts: {...}, exists: false}`。
    """
    path = consistency_file(store, chapter_id)
    if not path.exists():
        return {
            "chapter_id": chapter_id,
            "issues": [],
            "counts": {"high": 0, "medium": 0, "low": 0, "total": 0},
            "exists": False,
        }
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "chapter_id": chapter_id,
            "issues": [],
            "counts": {"high": 0, "medium": 0, "low": 0, "total": 0},
            "exists": False,
        }
    record["exists"] = True
    return record
