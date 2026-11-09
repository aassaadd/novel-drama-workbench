"""离线占位配音 provider。

用途只有一个：**不装任何模型**也能把「建项目 → 出成品」整条链路跑通。
它按文本字数生成一段真实可播放的 WAV（低频正弦 + 淡入淡出），
时长、采样率、落盘路径都按真实 provider 的契约返回，
因此批次、时间轴、拼接、混音、混流、导出全部走真实代码路径。

它**不会**被自动路由选中（配置里 `allow_auto_select: false`），
只有显式锁定 provider 或专门用测试配置时才生效，不影响 IndexTTS 真出音。
"""

from __future__ import annotations

import math
import struct
import time
import wave
from pathlib import Path

from ..base import BaseProvider
from ..types import HealthStatus, SynthesisRequest, SynthesisResult

# 每个字符对应的占位时长（毫秒），贴近中文朗读语速
MS_PER_CHAR = 190
# 最短时长，避免一个字的语气词时长过短
MIN_MS = 320
# 占位音振幅（约 -22 dBFS），保证后续 loudnorm 有可用信号
AMPLITUDE = 0.08
# 淡入淡出长度，消除首尾爆音
FADE_MS = 12


class OfflineDubProvider(BaseProvider):
    """离线占位配音：确定性生成真实音频文件，不加载任何模型。"""

    def health(self) -> HealthStatus:
        """永远可用：只依赖标准库。

        返回:
            HealthStatus：ok=True，detail 说明这是占位音轨。
        """
        return HealthStatus(self.spec.provider_id, True, "离线占位配音：按字数生成可播放音轨，不依赖模型与权重")

    def synthesize(self, request: SynthesisRequest) -> SynthesisResult:
        """按文本生成一段占位 WAV。

        参数:
            request: 中立合成请求；只用到 text、output_path、sample_rate。

        返回:
            SynthesisResult：含落盘路径、采样率、实际时长与耗时。

        异常:
            OSError：输出目录不可写时直接抛出，由批次层记录失败。
        """
        started = time.time()
        sample_rate = int(request.sample_rate or 48000)
        duration_ms = max(MIN_MS, len(request.text.strip()) * MS_PER_CHAR)
        output = Path(request.output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        self._write_wav(output, request.text, duration_ms, sample_rate)
        return SynthesisResult(
            audio_path=str(output),
            sample_rate=sample_rate,
            duration_ms=duration_ms,
            provider_id=self.spec.provider_id,
            model=self.spec.model,
            model_version=self.spec.model_version,
            latency_ms=int((time.time() - started) * 1000),
            variant_id=request.voice.variant_id,
        )

    def _write_wav(self, output: Path, text: str, duration_ms: int, sample_rate: int) -> None:
        """写单声道 16bit WAV：逐字符换音高，带淡入淡出。

        参数:
            output: 输出文件路径。
            text: 原始文本，用来决定每个字符的音高。
            duration_ms: 目标时长。
            sample_rate: 采样率。
        """
        total_frames = max(1, int(sample_rate * duration_ms / 1000))
        chars = [ch for ch in text if not ch.isspace()] or ["占"]
        fade_frames = max(1, int(sample_rate * FADE_MS / 1000))
        frames = bytearray()
        for index in range(total_frames):
            ch = chars[min(len(chars) - 1, index * len(chars) // total_frames)]
            # 音高随字符码点变化，听感上能区分「不同句子」
            freq = 180.0 + (ord(ch) % 12) * 22.0
            value = AMPLITUDE * math.sin(2 * math.pi * freq * index / sample_rate)
            # 首尾淡入淡出，避免爆音
            if index < fade_frames:
                value *= index / fade_frames
            elif index > total_frames - fade_frames:
                value *= max(0.0, (total_frames - index) / fade_frames)
            frames += struct.pack("<h", int(max(-1.0, min(1.0, value)) * 32767))
        with wave.open(str(output), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(sample_rate)
            handle.writeframes(bytes(frames))
