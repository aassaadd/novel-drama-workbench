"""配乐查询接口：章节 cue、生成记录与可关联的 BGM 计划。

生成逻辑已迁到 `application/production/bgm_generate.py`，由统一媒体总线
（action=`bgm`）调度；本模块只提供页面读取与预选所需的数据。
"""

from __future__ import annotations

import logging

from fastapi import HTTPException

from novel_drama.application.production.bgm_generate import (
    COMFY_MAX_SECONDS,
    COMFY_MIN_SECONDS,
    local_status,
)
from novel_drama.composition import (
    LONG_BED_SECONDS,
    MODE_LABELS,
    ProjectStore,
    load_cues,
    load_generation_records,
    resolve_mode,
)
from novel_drama.infrastructure.config import load_config

logger = logging.getLogger(__name__)


def _store(path: str | None) -> ProjectStore:
    """定位工程，未初始化时返回 404。

    参数:
        path: 工程根目录。

    返回:
        ProjectStore。

    异常:
        HTTPException 400/404: 缺少 path 或工程不存在。
    """
    if not path:
        raise HTTPException(status_code=400, detail="缺少 path 参数（工程目录）")
    store = ProjectStore(path)
    if not store.exists():
        raise HTTPException(status_code=404, detail=f"工程不存在: {store.root}")
    return store


def _plans(store: ProjectStore, chapter_id: str) -> tuple[list[dict], str | None]:
    """列出本章导演脚本里的 BGM 计划，并给出推断档位供页面预选。

    参数:
        store: 工程。
        chapter_id: 章节 ID。

    返回:
        `(计划列表, 读取失败说明)`；计划列表元素为
        `{id, name, duration_s, has_dialogue, mode, mode_label, reason}`。
        脚本文件损坏时列表为空，但第二项给出中文原因与文件位置，
        页面据此提示「分镜文件损坏」，不会把它当成「本章没有配乐计划」。
    """
    try:
        script = store.load_script(chapter_id)
    except FileNotFoundError:
        return [], None
    except Exception as exc:  # 损坏的标注文件不能把整页打成 500
        logger.warning("分镜文件损坏（chapter=%s）：%s", chapter_id, exc, exc_info=True)
        try:
            path = store.script_path(chapter_id)
        except Exception:  # noqa: BLE001 - 只是给错误文案找一个可读位置，取不到就用章节 ID
            path = chapter_id
        return [], f"分镜文件损坏：{path}（{exc}），本次无法读取配乐计划"
    plans: list[dict] = []
    for shot in script.get("shots") or []:
        plan = shot.get("bgm")
        if not isinstance(plan, dict) or not plan.get("id"):
            continue
        has_dialogue = any(str((line or {}).get("text") or "").strip() for line in shot.get("lines") or [])
        duration = float(plan.get("duration_hint_ms") or 0) / 1000 if plan.get("duration_hint_ms") else 40.0
        decision = resolve_mode(None, None, has_dialogue, duration)
        plans.append(
            {
                "id": plan.get("id"),
                "name": plan.get("name") or plan.get("id"),
                "prompt_en": plan.get("prompt_en"),
                "duration_s": round(duration, 1),
                "has_dialogue": has_dialogue,
                "mode": decision["mode"],
                "mode_label": decision["label"],
                "reason": decision["reason"],
            }
        )
    return plans, None


def register_bgm_routes(app, config_path: str | None = None) -> None:
    """挂载配乐查询接口。

    参数:
        app: FastAPI 应用。
        config_path: `serve --config` 传入的配置文件路径。

    返回:
        无。
    """

    @app.get("/api/ui/bgm")
    def ui_bgm(path: str, chapter: str) -> dict:
        """查询章节 BGM：cue、生成记录与可关联的计划。

        参数:
            path: 工程根目录。
            chapter: 章节 ID。

        返回:
            `{cues, records, plans, plan_error, modes, long_bed_seconds, defaults, providers}`；
            plan_error 非空表示标注文件损坏、配乐计划读不出来；
            defaults/providers 供页面显示开关来源与后端时长上限。
        """
        store = _store(path)
        config = load_config(config_path)
        local = local_status(config)
        plans, plan_error = _plans(store, chapter)
        # 显式给页面两档来源与开关来源：默认值来自配置，条目/CLI 可在生成时覆盖
        defaults = {
            "provider": str(config.get("bgm.provider") or "local"),
            "normalize_narrative": bool(config.get("bgm.normalize_narrative", True)),
            "trim_narrative_edges": bool(config.get("bgm.trim_narrative_edges", True)),
        }
        return {
            "cues": load_cues(store.root, chapter),
            "records": load_generation_records(store.root, chapter),
            "plans": plans,
            "plan_error": plan_error,
            "modes": MODE_LABELS,
            "long_bed_seconds": LONG_BED_SECONDS,
            "defaults": defaults,
            "providers": [
                {
                    "id": "local",
                    "label": "本地 Stable Audio Open 1.0",
                    "min_seconds": local["min_seconds"],
                    "max_seconds": local["max_seconds"],
                    "available": local["available"],
                    "detail": local["detail"],
                    "code": local["code"],
                    "model": local["model"],
                },
                {
                    "id": "comfyui",
                    "label": "ComfyUI ACE-Step 1.5",
                    "min_seconds": COMFY_MIN_SECONDS,
                    "max_seconds": COMFY_MAX_SECONDS,
                    "available": True,
                    "detail": "经 ComfyUI 生成，支持 60 秒以上的长曲",
                    "code": None,
                    "model": "ace-step-1.5",
                },
            ],
        }
