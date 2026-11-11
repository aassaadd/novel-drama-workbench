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
import queue
import subprocess
import threading
import time
from pathlib import Path

from .. import emotion_map
from ..base import BaseProvider
from ..errors import (
    BAD_OUTPUT,
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
# 常驻 worker 默认参数（口径见 docs/spec-r15-resident-tts-worker.md §13）
DEFAULT_IDLE_TIMEOUT_S = 300
DEFAULT_REQUEST_TIMEOUT_S = 300
DEFAULT_MAX_RESTARTS = 2


class _WorkerTimeout(RuntimeError):
    """单条请求等待 worker 响应超时。"""


class _WorkerGone(RuntimeError):
    """worker 进程提前退出或管道断开。"""


def _ref_fingerprint(path: str | None) -> str | None:
    """计算参考音频指纹（绝对路径|mtime_ns|大小）。

    参数:
        path: 参考音频路径。

    返回:
        指纹字符串；路径为空或不可读时返回 None。
    """
    if not path:
        return None
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return f"{os.path.abspath(path)}|{stat.st_mtime_ns}|{stat.st_size}"


def _remove_quietly(path: str | Path | None) -> None:
    """删除临时文件，失败时忽略。

    参数:
        path: 文件路径。
    """
    if not path:
        return
    try:
        Path(path).unlink()
    except OSError:
        pass


class IndexTts25Provider(BaseProvider):
    """IndexTTS 2.5：子进程桥接官方 3.11 环境。"""

    def __init__(self, spec) -> None:
        """保存配置，并取出 runtime 段（3.11 解释器 / 官方仓库路径 / 常驻参数）。

        参数:
            spec: provider 配置，`extra["runtime"]` 里放 python、repo 与 worker 运行参数。
        """
        super().__init__(spec)
        runtime = (spec.extra or {}).get("runtime") or {}
        self.python = runtime.get("python")
        self.repo = runtime.get("repo")
        self.timeout_s = int(runtime.get("timeout_s") or DEFAULT_TIMEOUT_S)
        # resident：整批复用一个常驻 worker；per_call：保留旧的「一行一进程」
        self.worker_mode = str(runtime.get("worker_mode") or "resident").lower()
        self.idle_timeout_s = max(1.0, float(runtime.get("idle_timeout_s") or DEFAULT_IDLE_TIMEOUT_S))
        self.request_timeout_s = max(1.0, float(runtime.get("request_timeout_s") or DEFAULT_REQUEST_TIMEOUT_S))
        self.max_restarts = max(0, int(runtime.get("max_restarts", DEFAULT_MAX_RESTARTS)))
        # 常驻 worker 的进程、读线程与响应队列
        self._proc: subprocess.Popen | None = None
        self._responses: queue.Queue = queue.Queue()
        self._reader: threading.Thread | None = None
        self._seq = 0
        self._consecutive_failures = 0
        # 连续崩溃达到上限后永久降级为逐行起进程，避免无限重启
        self._degraded = False
        self._lock = threading.Lock()

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
        if self.worker_mode == "resident":
            mode_text = f"常驻 worker（空闲 {self.idle_timeout_s:.0f}s 自动退出，模型整批只加载一次）"
        else:
            mode_text = "逐行起进程（每次合成重新加载模型，热启约 30s）"
        return HealthStatus(
            pid,
            True,
            f"3.11 环境就绪，权重 {weights}（含首次加载自动生成的 hf_cache：{'有' if aux else '无'}）；{mode_text}",
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
        data = self._synthesize_payload(payload)
        latency_ms = int((time.time() - t0) * 1000)
        return SynthesisResult(
            audio_path=data["out_path"],
            sample_rate=int(data["sample_rate"]),
            duration_ms=round(float(data["duration_s"]) * 1000),
            provider_id=pid,
            model=self.spec.model,
            model_version=self.spec.model_version,
            latency_ms=latency_ms,
        )

    def _synthesize_payload(self, payload: dict) -> dict:
        """按配置选择常驻 worker 或逐行起进程，并在崩溃降级时自动回退。

        参数:
            payload: 传给 worker 的参数（JSON 可序列化）。

        返回:
            worker 回报的成功结果字典。

        异常:
            TtsError：合成失败、超时或 worker 不可用。
        """
        if self.worker_mode != "resident" or self._degraded:
            return self._run_worker(payload)
        try:
            return self._resident_synth(payload)
        except TtsError as exc:
            # 进程崩溃/EOF 且已触发降级：用旧链路重试当前行一次，保证这一批还能跑完
            if self._degraded and exc.code == PROVIDER_UNREACHABLE:
                return self._run_worker(payload)
            raise

    def _resident_synth(self, payload: dict) -> dict:
        """用常驻 worker 合成一次：临时名落盘 → 收到成功响应 → os.replace 原子改名。

        参数:
            payload: 传给 worker 的参数。

        返回:
            成功结果字典（`out_path` 为最终路径）。

        异常:
            TtsError：请求超时、worker 崩溃或 worker 自报失败。
        """
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                try:
                    self._spawn_worker()
                except TtsError:
                    self._note_failure()
                    raise
            out = Path(payload["out_path"])
            out.parent.mkdir(parents=True, exist_ok=True)
            # 临时名带父进程 pid，失败/被杀时可精确定位并清理
            tmp = out.with_name(f"{out.name}.tmp-{os.getpid()}.wav")
            request = dict(payload)
            request["cmd"] = "synth"
            request["out_path"] = str(tmp)
            request["ref_fingerprint"] = _ref_fingerprint(payload.get("ref_audio"))
            try:
                data = self._send_locked(request, self.request_timeout_s)
            except _WorkerTimeout as exc:
                # 单行卡住：杀掉 worker，本行按超时失败，不自动重试（避免再卡一轮）
                self._kill_locked()
                _remove_quietly(tmp)
                self._note_failure()
                raise TtsError(
                    PROVIDER_TIMEOUT,
                    f"IndexTTS 2.5 常驻 worker 单行超时（>{self.request_timeout_s:.0f}s）",
                    self.spec.provider_id,
                ) from exc
            except _WorkerGone as exc:
                self._kill_locked()
                _remove_quietly(tmp)
                self._note_failure()
                raise TtsError(
                    PROVIDER_UNREACHABLE,
                    f"IndexTTS 2.5 常驻 worker 已退出：{exc}",
                    self.spec.provider_id,
                ) from exc
            if not data.get("ok"):
                # worker 自报失败（参数/模型错误）不是进程崩溃，不计入降级计数
                _remove_quietly(tmp)
                raise TtsError(
                    PROVIDER_UNREACHABLE,
                    f"{data.get('error_type')}: {data.get('error')}",
                    self.spec.provider_id,
                )
            try:
                os.replace(tmp, out)
            except OSError as exc:
                _remove_quietly(tmp)
                raise TtsError(BAD_OUTPUT, f"写入音频失败：{exc}", self.spec.provider_id) from exc
            self._consecutive_failures = 0
            result = dict(data)
            result["out_path"] = str(out)
            return result

    def _note_failure(self) -> None:
        """累计一次常驻 worker 失败，达到重启上限后降级为逐行起进程。"""
        self._consecutive_failures += 1
        if self._consecutive_failures > max(0, self.max_restarts):
            if not self._degraded:
                print(
                    f"[tts] {self.spec.provider_id} 常驻 worker 连续失败 {self._consecutive_failures} 次，"
                    "自动降级为逐行起进程",
                    flush=True,
                )
            self._degraded = True

    def _spawn_worker(self) -> None:
        """启动常驻 worker 并做一次握手（此时模型尚未加载）。

        异常:
            TtsError：解释器缺失或握手失败。
        """
        pid = self.spec.provider_id
        if not self.python or not Path(self.python).exists():
            raise TtsError(PROVIDER_DISABLED, f"3.11 解释器不存在：{self.python}", pid)
        if not self.repo or not (Path(self.repo) / "indextts").is_dir():
            raise TtsError(PROVIDER_DISABLED, f"官方 IndexTTS 仓库不存在：{self.repo}", pid)
        env = dict(os.environ)
        env["PYTHONPATH"] = str(self.repo)
        env.setdefault("PYTHONUNBUFFERED", "1")
        argv = [
            self.python,
            str(WORKER_PATH),
            "--serve",
            "--idle-timeout",
            str(self.idle_timeout_s),
        ]
        # stderr 直通父进程（Web 场景下就是批次日志），stdout 只走 NDJSON 协议
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,
            text=True,
            bufsize=1,
            cwd=str(self.repo),
            env=env,
        )
        self._proc = proc
        self._responses = queue.Queue()
        self._reader = threading.Thread(
            target=self._read_loop,
            args=(proc, self._responses),
            name=f"{pid}-reader",
            daemon=True,
        )
        self._reader.start()
        try:
            reply = self._send_locked({"cmd": "ping"}, timeout=30.0)
        except (_WorkerTimeout, _WorkerGone) as exc:
            self._kill_locked()
            raise TtsError(PROVIDER_UNREACHABLE, f"常驻 worker 握手失败：{exc}", pid) from exc
        if not reply.get("ok"):
            self._kill_locked()
            raise TtsError(PROVIDER_UNREACHABLE, f"常驻 worker 握手返回异常：{reply}", pid)

    @staticmethod
    def _read_loop(proc: subprocess.Popen, responses: queue.Queue) -> None:
        """后台读取 worker 的协议行，按到达顺序放入响应队列；EOF 时放 None 哨兵。

        参数:
            proc: worker 进程。
            responses: 响应队列。
        """
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                text = (line or "").strip()
                if not text:
                    continue
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    # 非协议输出一律忽略（日志已改道 stderr，这里只是兜底）
                    continue
                responses.put(data)
        except (ValueError, OSError):
            # 主进程关闭管道时读线程会收到该异常，属于正常收尾
            pass
        finally:
            responses.put(None)

    def _send_locked(self, request: dict, timeout: float) -> dict:
        """在持锁状态下发一条请求并等待同 id 的响应。

        参数:
            request: 请求字典（会补 id）。
            timeout: 等待上限（秒）。

        返回:
            对应 id 的响应字典。

        异常:
            _WorkerTimeout: 超时。
            _WorkerGone: worker 已退出或管道断开。
        """
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise _WorkerGone("worker 未启动")
        self._seq += 1
        req_id = f"{os.getpid()}-{self._seq}"
        request = {**request, "id": req_id}
        try:
            proc.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
            proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            raise _WorkerGone(str(exc)) from exc
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _WorkerTimeout("等待响应超时")
            try:
                data = self._responses.get(timeout=remaining)
            except queue.Empty as exc:
                raise _WorkerTimeout("等待响应超时") from exc
            if data is None:
                raise _WorkerGone("worker 进程已退出")
            if data.get("id") == req_id:
                return data

    def _drain_responses(self) -> None:
        """清空响应队列，避免旧响应串到下一次请求。"""
        while True:
            try:
                self._responses.get_nowait()
            except queue.Empty:
                return

    def _kill_locked(self) -> None:
        """结束并清理当前 worker 进程（持锁调用）。"""
        proc = self._proc
        self._proc = None
        if proc is not None:
            try:
                if proc.stdin is not None:
                    try:
                        proc.stdin.close()
                    except OSError:
                        pass
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        try:
                            proc.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            pass
            finally:
                try:
                    if proc.stdout is not None:
                        proc.stdout.close()
                except OSError:
                    pass
        self._drain_responses()

    def close(self) -> None:
        """整批结束时优雅关闭常驻 worker：shutdown → 等 5s → terminate → 再 2s → kill。"""
        with self._lock:
            proc = self._proc
            if proc is None:
                self._drain_responses()
                return
            try:
                if proc.poll() is None and proc.stdin is not None:
                    proc.stdin.write(json.dumps({"cmd": "shutdown", "id": "close"}, ensure_ascii=False) + "\n")
                    proc.stdin.flush()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
            except (BrokenPipeError, OSError, ValueError):
                pass
            self._kill_locked()

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
        # 与常驻模式一致：先写临时名，成功后再原子改名，避免半截 wav
        out = Path(payload["out_path"])
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(f"{out.name}.tmp-{os.getpid()}.wav")
        run_payload = dict(payload)
        run_payload["out_path"] = str(tmp)
        try:
            proc = subprocess.run(
                [self.python, str(WORKER_PATH)],
                input=json.dumps(run_payload, ensure_ascii=False),
                capture_output=True,
                text=True,
                cwd=str(self.repo),
                env=env,
                timeout=self.timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            _remove_quietly(tmp)
            raise TtsError(PROVIDER_TIMEOUT, f"IndexTTS 2.5 合成超时（>{self.timeout_s}s）", pid) from exc

        result = self._parse_result(proc.stdout)
        if result is None:
            _remove_quietly(tmp)
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-6:]
            raise TtsError(
                PROVIDER_UNREACHABLE,
                f"worker 未返回结果（exit={proc.returncode}）：{' | '.join(tail)}",
                pid,
            )
        if not result.get("ok"):
            _remove_quietly(tmp)
            raise TtsError(
                PROVIDER_UNREACHABLE,
                f"{result.get('error_type')}: {result.get('error')}",
                pid,
            )
        try:
            os.replace(tmp, out)
        except OSError as exc:
            _remove_quietly(tmp)
            raise TtsError(BAD_OUTPUT, f"写入音频失败：{exc}", pid) from exc
        result = dict(result)
        result["out_path"] = str(out)
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
