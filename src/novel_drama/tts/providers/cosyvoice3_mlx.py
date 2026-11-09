"""Fun-CosyVoice3-0.5B（MLX 8bit）适配器。

模型特性（实测）：
- 输出 24 kHz 单声道；必须有参考音频（零样本克隆），可选参考文本提升对齐；
- 支持 instruct_text 风格指令；
- 无 SSML、无原生时长控制；语速靠后期处理；
- 首次调用含 MLX 图编译，约 80~95 秒；之后稳态 RTF ≈ 0.22（M 系列 GPU）。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from ..base import BaseProvider
from ..errors import BAD_OUTPUT, MODEL_MISSING, PROVIDER_OOM, TtsError
from ..model_store import required_missing
from ..types import HealthStatus, ProviderSpec, SynthesisRequest, SynthesisResult

# 进程内共享已加载的模型，避免每次合成都重新加载
_MODEL_CACHE: dict[str, object] = {}
_AUDIO_CACHE: dict[str, object] = {}


class CosyVoice3MlxProvider(BaseProvider):
    """MLX 后端的 CosyVoice3 适配器。"""

    def __init__(self, spec: ProviderSpec) -> None:
        """初始化并准备 HF 缓存目录。

        参数:
            spec: provider 配置。
        """
        super().__init__(spec)
        hf_home = spec.extra.get("hf_home") or "/Volumes/other/hf-cache"
        os.environ.setdefault("HF_HOME", str(hf_home))

    # ---------- 内部工具 ----------

    def _weights_dir(self) -> Path:
        """返回权重目录，缺失时抛 MODEL_MISSING（带可操作提示）。"""
        hint = "运行 `ndw models list` 查看就位状态；缺失权重用 `ndw models import` 纳入模型根目录（不要放系统盘）"
        if not self.spec.weights_path:
            raise TtsError(MODEL_MISSING, "未配置 weights_path", self.spec.provider_id, hint=hint)
        path = Path(self.spec.weights_path)
        missing = required_missing(path)
        if missing:
            raise TtsError(
                MODEL_MISSING,
                f"权重不完整：{path}（缺少 {', '.join(missing)}）",
                self.spec.provider_id,
                hint=hint,
            )
        return path

    def _load_model(self):
        """加载（或复用）模型对象。"""
        key = str(self._weights_dir())
        if key in _MODEL_CACHE:
            return _MODEL_CACHE[key]
        try:
            from mlx_audio.tts.utils import load_model
        except Exception as exc:  # 依赖未装
            raise TtsError(
                MODEL_MISSING,
                f"mlx-audio-plus 未安装：{exc}",
                self.spec.provider_id,
            ) from exc
        try:
            model = load_model(Path(key))
        except MemoryError as exc:
            raise TtsError(PROVIDER_OOM, "加载模型时内存不足", self.spec.provider_id) from exc
        except Exception as exc:
            raise TtsError(
                MODEL_MISSING,
                f"加载模型失败：{exc}",
                self.spec.provider_id,
            ) from exc
        _MODEL_CACHE[key] = model
        return model

    def _load_ref_audio(self, path: str, sample_rate: int):
        """读取参考音频为模型需要的张量（带缓存）。"""
        key = f"{path}@{sample_rate}"
        if key in _AUDIO_CACHE:
            return _AUDIO_CACHE[key]
        from mlx_audio.tts.generate import load_audio

        try:
            audio = load_audio(path, sample_rate=sample_rate)
        except Exception as exc:
            raise TtsError(
                BAD_OUTPUT,
                f"参考音频读取失败：{path}（{exc}）",
                self.spec.provider_id,
            ) from exc
        _AUDIO_CACHE[key] = audio
        return audio

    # ---------- 接口实现 ----------

    def health(self) -> HealthStatus:
        """检查权重、依赖与导出样例是否就绪。"""
        try:
            self._weights_dir()
        except TtsError as exc:
            return HealthStatus(self.spec.provider_id, False, exc.message, exc.code)
        try:
            import mlx.core  # noqa: F401
            import mlx_audio  # noqa: F401
        except Exception as exc:
            return HealthStatus(self.spec.provider_id, False, f"依赖缺失：{exc}", MODEL_MISSING)
        loaded = any(k.startswith(str(self.spec.weights_path)) for k in _MODEL_CACHE)
        return HealthStatus(
            self.spec.provider_id,
            True,
            f"权重就绪：{self.spec.weights_path}",
            None,
            loaded=loaded,
        )

    def synthesize(self, request: SynthesisRequest) -> SynthesisResult:
        """合成一句文本并写入 wav。

        参数:
            request: 合成请求，必须带 `voice.ref_audio`。

        返回:
            SynthesisResult，含实际 provider/model/version 与耗时。
        """
        if not request.voice.ref_audio:
            raise TtsError(
                "VOICE_MISSING",
                "CosyVoice3 需要参考音频（零样本克隆），未提供 voice.ref_audio",
                self.spec.provider_id,
            )
        model = self._load_model()
        ref_audio = self._load_ref_audio(request.voice.ref_audio, model.sample_rate)

        instruct = request.instruct or request.voice.instruct
        # CosyVoice3 只有指令通路：没显式指令时，把情绪改写成一句风格指令（等价于改造前的行为）
        if not instruct:
            emotion = request.emotion or request.voice.emotion
            if emotion and emotion != "neutral":
                instruct = f"用{emotion}的语气说这句话"
        kwargs = {
            "text": request.text,
            "ref_audio": ref_audio,
            "ref_text": request.voice.ref_text,
            "stt_model": None,
            "verbose": False,
            "max_tokens": 1200,
        }
        if instruct and self.spec.capabilities.get("emotion_instruction"):
            kwargs["instruct_text"] = instruct

        started = time.perf_counter()
        result = None
        try:
            for item in model.generate(**kwargs):
                result = item
        except MemoryError as exc:
            raise TtsError(PROVIDER_OOM, "合成时内存不足", self.spec.provider_id) from exc
        except Exception as exc:
            raise TtsError(BAD_OUTPUT, f"合成失败：{exc}", self.spec.provider_id) from exc
        latency_ms = int((time.perf_counter() - started) * 1000)

        if result is None:
            raise TtsError(BAD_OUTPUT, "模型未返回音频", self.spec.provider_id)

        out = Path(request.output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            import soundfile as sf

            sf.write(str(out), result.audio, result.sample_rate)
        except Exception as exc:
            raise TtsError(BAD_OUTPUT, f"写音频失败：{exc}", self.spec.provider_id) from exc

        duration_ms = int(len(result.audio) / result.sample_rate * 1000)
        return SynthesisResult(
            audio_path=str(out),
            sample_rate=int(result.sample_rate),
            duration_ms=duration_ms,
            provider_id=self.spec.provider_id,
            model=self.spec.model,
            model_version=self.spec.model_version,
            latency_ms=latency_ms,
            variant_id=request.voice.variant_id,
        )
