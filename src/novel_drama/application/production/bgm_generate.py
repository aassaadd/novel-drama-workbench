"""配乐生成用例：项目内 Stable Audio Open / ComfyUI ACE-Step 两档。

从路由层迁出，媒体总线与页面共用同一份业务：
校验档位与时长 → 选择 provider（超本地上限自动改走 ACE-Step）→ 调用
`media.music.render_bgm` → 在结果里注明请求的 provider 与是否自动切换。
"""

from __future__ import annotations

from ...adapters.errors import AdapterError
from ...adapters.media.music import MODE_BED, MODE_NARRATIVE, render_bgm
from ...adapters.persistence import ProjectStore
from ...bgm.registry import build_providers, pick_provider
from ...infrastructure.config import AppConfig

#: ComfyUI / ACE-Step 的时长边界，与 `media.music` 的校验保持一致
COMFY_MIN_SECONDS = 10
COMFY_MAX_SECONDS = 180


def local_status(config: AppConfig) -> dict:
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


def generate_bgm(store: ProjectStore, config: AppConfig | None, params: dict) -> dict:
    """生成一段配乐并按档位处理、记账、写 cue。

    参数:
        store: 工程。
        config: 本机配置。
        params: `{chapter, prompt?, prompt_en?, duration?, mode?, plan_id?, seed?,
                 name?, start_ms?, normalize?, trim_edges?, provider?}`。

    返回:
        `render_bgm` 的结果，额外带 `provider_requested` 与 `auto_switched`。

    异常:
        ValueError: 缺少参数、模式非法，或上游参数校验失败（中文原因）。
        RuntimeError: 配乐生成失败（上游中文原因原样透出）。
    """
    chapter_id = str(params.get("chapter") or "").strip()
    if not chapter_id:
        raise ValueError("缺少必填参数：chapter（章节 ID）")
    if config is None:
        raise ValueError("缺少本机配置，无法生成配乐")
    mode = str(params.get("mode") or "auto")
    if mode not in ("auto", MODE_NARRATIVE, MODE_BED):
        raise ValueError("模式只能是 自动 / 叙事 / 铺底")
    duration = params.get("duration")
    requested = str(params.get("provider") or "").strip().lower()
    provider = requested if requested in ("local", "comfyui") else str(config.get("bgm.provider") or "local").strip().lower()
    auto_switched = False
    # 超过本地模型上限时自动改走 ACE-Step，并在结果里说明，避免用户以为走了本地
    local_max = local_status(config)["max_seconds"]
    if provider == "local" and isinstance(duration, (int, float)) and duration > local_max:
        provider = "comfyui"
        auto_switched = True
    try:
        result = render_bgm(
            store,
            config,
            chapter_id,
            str(params.get("prompt") or ""),
            duration_s=duration,
            requested_mode=mode,
            requested_normalize=params.get("normalize") if isinstance(params.get("normalize"), bool) else None,
            requested_trim=params.get("trim_edges") if isinstance(params.get("trim_edges"), bool) else None,
            plan_id=params.get("plan_id"),
            prompt_en=params.get("prompt_en"),
            seed=params.get("seed"),
            name=params.get("name"),
            start_ms=int(params.get("start_ms") or 0),
            provider=provider,
        )
    except AdapterError as exc:
        raise RuntimeError(exc.message) from exc
    result["provider_requested"] = requested or provider
    result["auto_switched"] = auto_switched
    return result
