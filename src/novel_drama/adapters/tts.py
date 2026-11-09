"""多角色 TTS 适配（本轮占位）。

对接约定：
- 必须以单行为单位合成，禁止整章拼接后切分；
- `duration_ms` 必须实测（解码音频或读容器时长），禁止返回估算值；
- 不支持情绪/克隆时保留字段并给出告警，不得静默丢弃。
"""

from __future__ import annotations

import tempfile

from .base import (
    AdapterError,
    ErrorCode,
    HealthStatus,
    TtsRequest,
    TtsResult,
    Voice,
    not_implemented,
)

# TTS 错误码 → 适配层错误码
_ERROR_MAP = {
    "NOT_IMPLEMENTED": ErrorCode.NOT_IMPLEMENTED,
    "PROVIDER_TIMEOUT": ErrorCode.TIMEOUT,
    "PROVIDER_OOM": ErrorCode.UNAVAILABLE,
    "PROVIDER_UNREACHABLE": ErrorCode.UNAVAILABLE,
    "PROVIDER_DISABLED": ErrorCode.UNAVAILABLE,
    "MODEL_MISSING": ErrorCode.UNAVAILABLE,
    "BAD_OUTPUT": ErrorCode.UPSTREAM_ERROR,
}


class ProviderTtsAdapter:
    """把多 provider 配音层（src/novel_drama/tts）桥接到旧 TtsAdapter 契约。

    `provider_id` 传 "auto" 时由自动路由逐句决定；传具体 id 等价于全局锁定。
    """

    def __init__(self, config, provider_id: str = "auto") -> None:
        """初始化并构建配音服务。

        参数:
            config: AppConfig。
            provider_id: 具体 provider（全局锁定）或 "auto"。
        """
        from ..tts import TtsService

        self.service = TtsService(config)
        self.provider_id = provider_id

    def list_voices(self) -> list[Voice]:
        """返回配置里的默认音色（本轮只有一个参考音色）。"""
        voice = self.service.default_voice
        if not voice.ref_audio:
            raise not_implemented("音色库", "在 config/config.yaml 配置 tts.default_voice.ref_audio")
        return [
            Voice(
                voice_id=voice.variant_id or "default",
                name="默认参考音色",
                language="zh-CN",
                adapter=self.provider_id,
                extra={"ref_audio": voice.ref_audio, "ref_text": voice.ref_text},
            )
        ]

    def synthesize(self, req: TtsRequest) -> TtsResult:
        """按单行请求合成，返回实测时长。

        参数:
            req: 单行配音请求。

        返回:
            TtsResult（sample_rate 为模型实际输出，未做重采样）。
        """
        from ..tts import Requirement, VoiceRef
        from ..tts.errors import TtsError

        voice = self.service.default_voice
        requirement = Requirement(
            text=req.text,
            emotion=req.emotion,
            emotion_intensity=max(0, min(3, round(float(req.emotion_intensity) * 3))),
        )
        ref = VoiceRef(
            variant_id=req.voice_id,
            ref_audio=req.reference_audio or voice.ref_audio,
            ref_text=voice.ref_text,
        )
        out = req.output_path or str(tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name)
        locked = None if self.provider_id == "auto" else self.provider_id
        try:
            payload = self.service.synthesize(
                req.text,
                out,
                requirement=requirement,
                voice=ref,
                instruct=req.delivery_note,
                line_provider_id=locked,
            )
        except TtsError as exc:
            raise AdapterError(
                _ERROR_MAP.get(exc.code, ErrorCode.UPSTREAM_ERROR),
                f"[{exc.code}] {exc.message}",
                retryable=exc.retryable,
                detail={"hint": exc.hint, "provider_id": exc.provider_id},
            ) from exc
        return TtsResult(
            audio_path=payload["audio_path"],
            duration_ms=payload["duration_ms"],
            sample_rate=payload["sample_rate"],
            channels=1,
            voice_id=req.voice_id,
            model=f"{payload['model']}({payload['model_version']})",
            raw=payload,
        )

    def health(self) -> HealthStatus:
        """任意 provider 可用即视为可用。"""
        rows = self.service.doctor()
        available = [row for row in rows if row["available"]]
        if available:
            detail = "、".join(f"{row['provider_id']}({row['model_version']})" for row in available)
            return HealthStatus(ok=True, detail=f"可用 provider：{detail}", extra={"providers": rows})
        detail = "；".join(f"{row['provider_id']}: {row['code']}" for row in rows)
        return HealthStatus(ok=False, detail=f"无可用 provider：{detail}", extra={"providers": rows})


class PlaceholderTtsAdapter:
    """未对接的 TTS 实现：所有能力明确报错，避免前端误以为可用。"""

    def __init__(self, settings: dict | None = None):
        """绑定 TTS 配置。

        参数:
            settings: {adapter, default_voice_id, sample_rate}。
        """
        self.settings = settings or {}

    def list_voices(self) -> list[Voice]:
        """列出音色。

        返回:
            空列表不可接受，故直接抛 NOT_IMPLEMENTED。

        异常:
            AdapterError(NOT_IMPLEMENTED): 始终抛出。
        """
        raise not_implemented(
            "TTS 适配器",
            "实现 TtsAdapter 后在此返回 list[Voice]（voice_id/name/language/gender），供角色音色库下拉使用",
        )

    def synthesize(self, req: TtsRequest) -> TtsResult:
        """合成单行音频。

        参数:
            req: 单行配音请求。

        返回:
            TtsResult（audio_path 为工程内相对路径，duration_ms 为实测值）。

        异常:
            AdapterError(NOT_IMPLEMENTED): 始终抛出。
        """
        raise not_implemented(
            "TTS 适配器",
            "实现 TtsAdapter.synthesize：按 output_path 写 wav，返回实测 duration_ms/sample_rate/channels，失败抛 UNAVAILABLE/TIMEOUT 等统一错误码",
        )

    def health(self) -> HealthStatus:
        """检查 TTS 可用性。"""
        return HealthStatus(ok=False, detail="TTS 适配器未对接（本轮为骨架）")
