"""写作适配器唯一构造入口。

应用层的 `LlmBus` 与适配器装配 `build_adapters` 都从这里取写作实现，
避免「有的模块直接 new、有的模块走另一套工厂」导致超时/思考覆盖不一致。
"""

from __future__ import annotations

from ...domain.production.rules.thinking_policy import collect_overrides
from ...infrastructure.config import AppConfig
from ..errors import AdapterError, ErrorCode
from .offline import OfflineWritingAdapter
from .omlx import OmlxWritingAdapter
from .unavailable import UnavailableWritingAdapter


def build_writing_adapter(config: AppConfig, settings: dict | None = None):
    """按配置装配写作适配器。

    参数:
        config: 本机配置。
        settings: 工程设置（可选）；其中 `writing` 子项会覆盖本机 omlx 参数，
            空值 / 空串 / 空列表表示「未定制」，继承本机配置。

    返回:
        实现 `WritingAdapter` 协议的实例。

    异常:
        AdapterError(INVALID_INPUT): 配置名未知或未配置写作模型。
    """
    # 配置里适配器写在 adapters.writing；providers.writing 是旧键，保留兼容
    name = str(config.get("adapters.writing") or config.get("providers.writing") or "").strip()
    if not name:
        # 不能默认回落到离线假数据：配置缺失必须明确报错，避免假结果写进正式产物
        raise AdapterError(
            ErrorCode.INVALID_INPUT,
            "未配置写作模型（adapters.writing）：请在设置页选择 omlx 并填写真实服务地址与模型；"
            "离线适配器只能显式配置为 offline 时使用",
        )
    if name in ("offline", "offline-writing"):
        return OfflineWritingAdapter(config.get("writing.offline") or settings or {})
    if name == "omlx":
        omlx_settings = dict(config.get("omlx") or {})
        project_writing = {
            key: value
            for key, value in ((settings or {}).get("writing") or {}).items()
            if value not in (None, "", [])
        }
        omlx_settings.update(project_writing)
        # 思考：全局开关 + 强度 + 设置页按调用点覆盖（没有覆盖项即系统自动）
        omlx_settings["thinking_stages"] = collect_overrides(config)
        return OmlxWritingAdapter(omlx_settings)
    raise AdapterError(ErrorCode.INVALID_INPUT, f"未知写作适配器: {name}（可选 offline / omlx）")


def build_omlx_writing_adapter(settings: dict):
    """用一份 omlx 设置直接构造写作适配器（供无 AppConfig 的调试入口使用）。

    参数:
        settings: 已合并好的 omlx 设置（endpoint/model/温度等）。

    返回:
        `OmlxWritingAdapter`。

    异常:
        AdapterError(INVALID_INPUT): 设置里没有 endpoint 或 model。
    """
    if not str((settings or {}).get("endpoint") or "").strip() or not str((settings or {}).get("model") or "").strip():
        raise AdapterError(ErrorCode.INVALID_INPUT, "omlx 未配置 endpoint/model")
    return OmlxWritingAdapter(dict(settings or {}))


def build_unavailable_writing_adapter(reason: str):
    """构造不可用兜底写作适配器（仅用于诊断页展示，不参与生成）。

    参数:
        reason: 不可用原因。

    返回:
        `UnavailableWritingAdapter`。
    """
    return UnavailableWritingAdapter(reason)


__all__ = [
    "build_writing_adapter",
    "build_omlx_writing_adapter",
    "build_unavailable_writing_adapter",
]
