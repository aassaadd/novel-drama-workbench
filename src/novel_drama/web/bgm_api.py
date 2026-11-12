"""配乐生成接口：项目内 Stable Audio Open / ACE-Step 两档（叙事 / 铺底）。

业务规则在 `media.music`，这里只做 HTTP 包装，错误统一翻成中文并带机器可读 code，
便于页面在「本地 provider 不可用」时直接引导去设置页。
"""

from __future__ import annotations

from fastapi import HTTPException

from ..adapters.base import AdapterError, ErrorCode
from ..bgm.registry import build_providers, pick_provider
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

#: ComfyUI / ACE-Step 的时长边界，与 `media.music` 的校验保持一致
COMFY_MIN_SECONDS = 10
COMFY_MAX_SECONDS = 180


def _local_status(config) -> dict:
    """检查项目内 provider 是否可用（只查解释器与权重文件，不加载模型）。

    参数:
        config: 本机配置。

    返回:
        `{available, detail, code, max_seconds, min_seconds, model}`。
    """
    try:
        providers = build_providers(config)
        _, provider = pick_provider(config, providers)
        health = provider.health()
        return {
            "available": health.ok,
            "detail": health.detail,
            "code": health.code,
            "max_seconds": float(getattr(provider, "max_seconds", 47.0)),
            "min_seconds": float(getattr(provider, "min_seconds", 5.0)),
            "model": provider.spec.model,
        }
    except Exception as exc:  # noqa: BLE001 - 配置缺失也要给页面一个可读原因
        return {
            "available": False,
            "detail": f"本地配乐配置不可用：{exc}",
            "code": "PROVIDER_DISABLED",
            "max_seconds": 47.0,
            "min_seconds": 5.0,
            "model": None,
        }


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
            `{cues, records, plans, modes, long_bed_seconds, defaults, providers}`；
            defaults/providers 供页面显示开关来源与后端时长上限。
        """
        store = _store(path)
        config = load_config(config_path)
        local = _local_status(config)
        # 显式给页面两档来源与开关来源：默认值来自配置，条目/CLI 可在生成时覆盖
        defaults = {
            "provider": str(config.get("bgm.provider") or "local"),
            "normalize_narrative": bool(config.get("bgm.normalize_narrative", True)),
            "trim_narrative_edges": bool(config.get("bgm.trim_narrative_edges", True)),
        }
        return {
            "cues": load_cues(store.root, chapter),
            "records": load_generation_records(store.root, chapter),
            "plans": _plans(store, chapter),
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
        duration = body.get("duration")
        provider = str(body.get("provider") or "").strip().lower()
        if provider not in ("local", "comfyui"):
            provider = str(config.get("bgm.provider") or "local").strip().lower()
        auto_switched = False
        # 超过本地模型上限时自动改走 ACE-Step，并在响应里说明，避免用户以为走了本地
        local_max = _local_status(config)["max_seconds"]
        if provider == "local" and isinstance(duration, (int, float)) and duration > local_max:
            provider = "comfyui"
            auto_switched = True
        try:
            result = render_bgm(
                store,
                config,
                chapter_id,
                str(body.get("prompt") or ""),
                duration_s=duration,
                requested_mode=mode,
                requested_normalize=body.get("normalize") if isinstance(body.get("normalize"), bool) else None,
                requested_trim=body.get("trim_edges") if isinstance(body.get("trim_edges"), bool) else None,
                plan_id=body.get("plan_id"),
                seed=body.get("seed"),
                name=body.get("name"),
                start_ms=int(body.get("start_ms") or 0),
                provider=provider,
            )
            result["provider_requested"] = body.get("provider") or provider
            result["auto_switched"] = auto_switched
            return result
        except AdapterError as exc:
            status = 400 if exc.code == ErrorCode.INVALID_INPUT else 503 if exc.code == ErrorCode.UNAVAILABLE else 502
            raise HTTPException(
                status_code=status,
                detail={"message": f"配乐生成失败：{exc.message}", "code": exc.code, "provider": provider},
            ) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail={"message": str(exc), "code": "BAD_REQUEST", "provider": provider},
            ) from exc
