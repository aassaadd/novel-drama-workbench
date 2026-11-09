"""IndexTTS 2.5 真实现（官方 PyTorch + MPS，跑在独立的 Python 3.11 环境里）。

为什么不就地加载：官方 `pyproject.toml` 要求 `>=3.10,<3.12` 且钉 `torch==2.8.*` /
`torchaudio==2.8.*`，而工作台 venv 是 3.12.9 + torch 2.15.0，两套依赖不能共存。
因此本 provider 只做「组参数 → 起子进程 → 收结果」，模型常驻在那个 3.11 环境里。

官方接口事实（`indextts/infer_v2_5.py`）：
- 构造：`IndexTTS2(cfg_path, model_dir, use_bf16, device, use_qwen_emo)`，`:107-110` 的
  MPS 分支自己把 `use_bf16` 置 False，本实现不覆盖。
- 推理：`:506-509` 的 `infer()` 收 `spk_audio_prompt` / `emo_vector` / `emo_text` /
  `use_emo_text` / `emo_audio_prompt` / `emo_alpha` / `duration_factor`。
- 互斥：`:583-586` 一旦给了 `emo_vector` 或 `use_emo_text`，官方会把 `emo_audio_prompt`
  清成 None。本实现在参数校验阶段就直接拒绝，不依赖这个隐式覆盖。
- 文字情绪：`:588-600` 要求构造时 `use_qwen_emo=True`，否则 RuntimeError；worker 自动打开。
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from .. import emotion_map
from ..base import BaseProvider
from ..errors import (
    MODEL_MISSING,
    PROVIDER_DISABLED,
    PROVIDER_TIMEOUT,
    PROVIDER_UNREACHABLE,
    UNSUPPORTED_EMOTION,
    VOICE_MISSING,
    TtsError,
)
from ..types import HealthStatus, SynthesisRequest, SynthesisResult

# worker 与 provider 同目录，随包分发
WORKER_PATH = Path(__file__).with_name("indextts25_worker.py")
DEFAULT_TIMEOUT_S = 1800


class IndexTts25Provider(BaseProvider):
    """IndexTTS 2.5：子进程桥接官方 3.11 环境。"""

    def __init__(self, spec) -> None:
        """保存配置，并取出 runtime 段（3.11 解释器 / 官方仓库路径）。

        参数:
            spec: provider 配置，`extra["runtime"]` 里放 python 与 repo 两个路径。
        """
        super().__init__(spec)
        runtime = (spec.extra or {}).get("runtime") or {}
        self.python = runtime.get("python")
        self.repo = runtime.get("repo")
        self.timeout_s = int(runtime.get("timeout_s") or DEFAULT_TIMEOUT_S)

    def health(self) -> HealthStatus:
        """检查 3.11 解释器、官方仓库、权重目录三件事。

        返回:
            HealthStatus：任何一项缺失都返回不可用，并给出具体缺什么。
        """
        pid = self.spec.provider_id
        if not self.python or not Path(self.python).exists():
            return HealthStatus(pid, False, f"3.11 解释器不存在：{self.python}（配置 tts.providers[].runtime.python）", PROVIDER_DISABLED)
        if not self.repo or not (Path(self.repo) / "indextts").is_dir():
            return HealthStatus(pid, False, f"官方 IndexTTS 仓库不存在：{self.repo}", PROVIDER_DISABLED)
        if not self.spec.weights_path:
            return HealthStatus(pid, False, "未配置 weights_path", MODEL_MISSING)
        weights = Path(self.spec.weights_path)
        missing = [n for n in ("config.yaml", "gpt.pth") if not (weights / n).exists()]
        if missing:
            return HealthStatus(pid, False, f"权重不完整，缺 {missing}：{weights}", MODEL_MISSING)
        aux = (weights / "hf_cache").is_dir()
        return HealthStatus(
            pid,
            True,
            f"3.11 环境就绪，权重 {weights}（含首次加载自动生成的 hf_cache：{'有' if aux else '无'}）；"
            f"每次合成需重新加载模型，热启约 30s",
            None,
        )

    def synthesize(self, request: SynthesisRequest) -> SynthesisResult:
        """把中立请求翻成官方 infer 参数，起子进程合成并返回落盘结果。

        参数:
            request: 中立合成请求（文本、参考音频、情绪、风格指令、输出路径）。

        返回:
            SynthesisResult：含音频路径、采样率、时长、耗时。

        异常:
            TtsError：参数冲突（情绪与风格指令同时给）、缺参考音频、子进程超时或失败。
        """
        pid = self.spec.provider_id
        emotion = request.emotion or request.voice.emotion
        instruct = request.instruct or request.voice.instruct
        emo_audio = request.voice.emotion_audio or (
            request.voice.ref_audio if request.voice.source == "emotion_ref" else None
        )
        # 三条情绪通路互斥：官方 infer_v2_5.py:583-586 会静默把 emo_audio_prompt 清掉、
        # 且 emo_vector 与 emo_text 同时给时以向量为准，这里提前拒绝，不依赖隐式覆盖
        chosen = [
            label
            for label, value in (
                ("emo_vector(emotion)", emotion and emotion.strip().lower() != "neutral"),
                ("emo_text(instruct)", instruct),
                ("emo_audio_prompt", emo_audio),
            )
            if value
        ]
        if len(chosen) > 1:
            raise TtsError(
                UNSUPPORTED_EMOTION,
                f"情绪通路互斥，当前同时给了 {' + '.join(chosen)}；一次只能选一条",
                pid,
            )
        if emotion and not emotion_map.is_supported(emotion):
            raise TtsError(
                UNSUPPORTED_EMOTION,
                f"不支持的情绪 {emotion!r}；可用：{', '.join(emotion_map.available_emotions())}",
                pid,
            )
        ref_audio = request.voice.ref_audio
        if not ref_audio or not Path(ref_audio).exists():
            raise TtsError(VOICE_MISSING, f"参考音频不存在：{ref_audio}", pid)
        if not self.spec.weights_path:
            raise TtsError(MODEL_MISSING, "未配置 weights_path", pid)

        payload: dict = {
            "model_dir": self.spec.weights_path,
            "text": request.text,
            "out_path": str(request.output_path),
            "ref_audio": ref_audio,
            "lang": (request.language or "zh-CN").split("-")[0].upper(),
        }
        if emotion:
            # 项目 10 情绪映射到官方 8 维；neutral 不传向量，等于不做情绪控制
            vector = emotion_map.build_vector(emotion, request.emotion_intensity)
            alpha = emotion_map.build_alpha(emotion, request.emotion_intensity)
            if vector is not None and alpha is not None:
                payload["emo_vector"] = vector
                payload["emo_alpha"] = alpha
        if instruct:
            # 文字情绪通路：worker 会以 use_qwen_emo=True 构造模型
            payload["emo_text"] = instruct
            payload["emo_alpha"] = 1.0
        if emo_audio:
            payload["emo_audio_prompt"] = emo_audio

        t0 = time.time()
        data = self._run_worker(payload)
        latency_ms = int((time.time() - t0) * 1000)
        return SynthesisResult(
            audio_path=data["out_path"],
            sample_rate=int(data["sample_rate"]),
            duration_ms=int(round(float(data["duration_s"]) * 1000)),
            provider_id=pid,
            model=self.spec.model,
            model_version=self.spec.model_version,
            latency_ms=latency_ms,
        )

    def _run_worker(self, payload: dict) -> dict:
        """起子进程调 worker，解析最后一行 `NDW_RESULT` JSON。

        参数:
            payload: 传给 worker 的参数（JSON 可序列化）。

        返回:
            worker 回报的成功结果字典。

        异常:
            TtsError：超时、子进程非零退出、结果缺失或 worker 自报失败。
        """
        pid = self.spec.provider_id
        env = dict(os.environ)
        env["PYTHONPATH"] = str(self.repo)
        env.setdefault("PYTHONUNBUFFERED", "1")
        try:
            proc = subprocess.run(
                [self.python, str(WORKER_PATH)],
                input=json.dumps(payload, ensure_ascii=False),
                capture_output=True,
                text=True,
                cwd=str(self.repo),
                env=env,
                timeout=self.timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise TtsError(PROVIDER_TIMEOUT, f"IndexTTS 2.5 合成超时（>{self.timeout_s}s）", pid) from exc

        result = self._parse_result(proc.stdout)
        if result is None:
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-6:]
            raise TtsError(
                PROVIDER_UNREACHABLE,
                f"worker 未返回结果（exit={proc.returncode}）：{' | '.join(tail)}",
                pid,
            )
        if not result.get("ok"):
            raise TtsError(
                PROVIDER_UNREACHABLE,
                f"{result.get('error_type')}: {result.get('error')}",
                pid,
            )
        return result

    @staticmethod
    def _parse_result(stdout: str) -> dict | None:
        """从 worker 的标准输出里取最后一行 `NDW_RESULT` JSON。

        参数:
            stdout: worker 标准输出全文。

        返回:
            解析出的字典；没有该行或解析失败时返回 None。
        """
        for line in reversed((stdout or "").splitlines()):
            if line.startswith("NDW_RESULT "):
                try:
                    return json.loads(line[len("NDW_RESULT "):])
                except json.JSONDecodeError:
                    return None
        return None
