"""配乐生成接口：ACE-Step 两档（叙事 / 铺底）+ 章节 cue 与生成记录查询。

业务规则在 `media.music`，这里只做 HTTP 包装，错误统一翻成中文。
"""

from __future__ import annotations

from fastapi import HTTPException

from ..adapters.base import AdapterError, ErrorCode
from ..config import load_config
from ..media.bgm import load_cues
from ..media.music import (
    LONG_BED_SECONDS,
    MODE_BED,
    MODE_LABELS,
    MODE_NARRATIVE,
    load_generation_records,
    render_bgm,
    resolve_mode,
)
from ..store import ProjectStore


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


def _plans(store: ProjectStore, chapter_id: str) -> list[dict]:
    """列出本章导演脚本里的 BGM 计划，并给出推断档位供页面预选。

    参数:
        store: 工程。
        chapter_id: 章节 ID。

    返回:
        计划列表 `[{id, name, duration_s, has_dialogue, mode, mode_label, reason}]`。
    """
    try:
        script = store.load_script(chapter_id)
    except FileNotFoundError:
        return []
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
                "duration_s": round(duration, 1),
                "has_dialogue": has_dialogue,
                "mode": decision["mode"],
                "mode_label": decision["label"],
                "reason": decision["reason"],
            }
        )
    return plans


def register_bgm_routes(app, config_path: str | None = None) -> None:
    """挂载配乐生成相关接口。

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
            `{cues, records, plans, modes, long_bed_seconds}`。
        """
        store = _store(path)
        return {
            "cues": load_cues(store.root, chapter),
            "records": load_generation_records(store.root, chapter),
            "plans": _plans(store, chapter),
            "modes": MODE_LABELS,
            "long_bed_seconds": LONG_BED_SECONDS,
        }

    @app.post("/api/ui/bgm/generate")
    def ui_bgm_generate(body: dict) -> dict:
        """生成一段配乐并按档位处理。

        参数:
            body: `{path, chapter, prompt, duration?, mode?, plan_id?, seed?, name?, start_ms?}`；
            mode 取值 auto / narrative / bed。

        返回:
            `render_bgm` 的结果（含 mode / 两段耗时 / 响度 / cue / 记录路径）。
        """
        store = _store(body.get("path"))
        chapter_id = str(body.get("chapter") or "").strip()
        if not chapter_id:
            raise HTTPException(status_code=400, detail="缺少 chapter 参数（章节 ID）")
        mode = str(body.get("mode") or "auto")
        if mode not in ("auto", MODE_NARRATIVE, MODE_BED):
            raise HTTPException(status_code=400, detail="模式只能是 自动 / 叙事 / 铺底")
        config = load_config(config_path)
        try:
            return render_bgm(
                store,
                config,
                chapter_id,
                str(body.get("prompt") or ""),
                duration_s=body.get("duration"),
                requested_mode=mode,
                requested_normalize=body.get("normalize") if isinstance(body.get("normalize"), bool) else None,
                plan_id=body.get("plan_id"),
                seed=body.get("seed"),
                name=body.get("name"),
                start_ms=int(body.get("start_ms") or 0),
            )
        except AdapterError as exc:
            status = 400 if exc.code == ErrorCode.INVALID_INPUT else 503 if exc.code == ErrorCode.UNAVAILABLE else 502
            raise HTTPException(status_code=status, detail=f"配乐生成失败：{exc.message}") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
