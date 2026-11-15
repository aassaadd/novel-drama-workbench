"""全剧配音表补全：音色描述与龙套槽位（默认只预览，不写盘）。"""

from __future__ import annotations

import hashlib
import re

from ...adapters.persistence import ProjectStore
from ...composition import SettingsStore, profile_source_hash
from ...models.ids import now_iso


def _slug(text: str) -> str:
    """把中文职业名转成稳定的英文风格槽位键（中文按 hash 后缀兜底）。"""
    cleaned = re.sub(r"[^a-z0-9_]+", "_", (text or "").lower()).strip("_")
    if cleaned:
        return cleaned[:40]
    return "minor_" + hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:8]


def _gender_voice_slot(gender: str | None, age_range: str | None) -> str:
    """按性别与年龄感给出默认音色池槽位。"""
    age = str(age_range or "")
    elder = any(word in age for word in ("60", "70", "80", "老"))
    young = any(word in age for word in ("16", "17", "18", "19", "20", "21", "22", "少年", "少女"))
    if gender == "female":
        return "female_elder" if elder else ("female_young" if young else "female_mid")
    if gender == "male":
        return "male_elder" if elder else ("male_young" if young else "male_mid")
    return "neutral_mid"


def backfill_cast(
    store: ProjectStore,
    config,
    apply: bool = False,
    force: bool = False,
    profiles: bool = False,
    stock: bool = False,
    all: bool = False,
) -> dict:
    """给存量工程补音色描述 / 龙套槽位。

    参数:
        store: 工程。
        config: 本机配置。
        apply: 是否写盘（默认只预览）。
        force: 覆盖已有描述与人工改过的字段。
        profiles: 只补音色描述；不传 `stock` 时默认启用。
        stock: 只补龙套槽位。
        all: 已有非 draft 描述也重新生成。

    返回:
        `{"changed": 变更数, "applied": 是否写盘, "profiles": 逐角色预览, "stock": 逐角色槽位}`。
    """
    from ...tts.voice_design import build_profile
    from ..support.llm_calls import build_writing_adapter

    table = store.load_characters()
    want_profiles = profiles or not stock
    want_stock = stock
    changed = 0
    profile_rows: list[dict] = []
    stock_rows: list[dict] = []

    if want_profiles:
        adapter = build_writing_adapter(config, SettingsStore(store.root).load_or_none() or {})
        for character in table.get("characters") or []:
            profile = character.get("voice_profile") or {}
            # status=draft 表示上次只回落到规则模板，默认允许重试补齐，不要求用户加 --force
            if profile and profile.get("status") != "draft" and not (force or all):
                continue
            if (profile.get("source") or {}).get("override_fields") and not force:
                continue
            built = build_profile(adapter, character, config)
            new_profile = dict(built["profile"])
            source = {
                "kind": built.get("source") or "rule",
                "at": now_iso(),
                "from_hash": profile_source_hash(character),
            }
            if built.get("note"):
                # 回落原因写进来源，页面与后续 cast check 可据此解释 draft
                source["note"] = str(built["note"])[:200]
            new_profile.update(
                {
                    "schema": "voice_profile/1",
                    "summary": new_profile.get("voice_summary"),
                    "source": source,
                    "status": "ready" if built.get("source") == "llm" else "draft",
                }
            )
            character["voice_profile"] = new_profile
            changed += 1
            profile_rows.append(
                {
                    "name": character.get("name"),
                    "design_prompt": (new_profile.get("design_prompt") or "")[:80],
                    "note": built.get("note"),
                }
            )

    if want_stock:
        for index, character in enumerate(table.get("characters") or []):
            if character.get("role") != "minor" or (character.get("stock") or {}).get("slot"):
                continue
            label = re.sub(r"^龙套[·.・]\s*", "", character.get("name") or "") or f"龙套{index}"
            character["stock"] = {
                "slot": _slug(label) or f"minor_{index:02d}",
                "voice_slot": _gender_voice_slot(character.get("gender"), character.get("age_range")),
                "label": label[:40],
                "archetype_tags": list(character.get("personality") or [])[:3],
                "reuse_policy": "reuse",
            }
            changed += 1
            stock_rows.append({"name": character.get("name"), "slot": character["stock"]["slot"]})

    if apply and changed:
        store.save_characters(table)
    return {"changed": changed, "applied": apply, "profiles": profile_rows, "stock": stock_rows}
