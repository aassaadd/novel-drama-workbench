"""Stable Audio 3 音效 provider（官方 PyTorch + MPS，跑在独立 Python 环境）。

与 `tts/providers/indextts25.py` 同构：本模块只做「组参数 → 起子进程 → 收结果」，
模型常驻在官方仓库自己的 venv 里（torch 2.7.1 + transformers 5.8），
因为工作台 venv 的 torch 版本与之不兼容。

实测（M 系列 Mac，MPS，8 步 cfg 1.0）：2 秒音效热推理约 0.7s，
稳态进程峰值内存约 5.2GB；输出 44.1kHz 立体声。
"""

from __future__ import annotations

import json
import os
import queue
import random
import subprocess
import threading
import time
from pathlib import Path

from ..base import BaseSfxProvider
from ..types import (
    BAD_OUTPUT,
    BAD_REQUEST,
    MODEL_MISSING,
    PROVIDER_DISABLED,
    PROVIDER_TIMEOUT,
    PROVIDER_UNREACHABLE,
    SfxError,
    SfxHealth,
    SfxRequest,
    SfxResult,
)

# worker 与 provider 同目录，随包分发
WORKER_PATH = Path(__file__).with_name("stable_audio3_worker.py")
DEFAULT_TIMEOUT_S = 900
DEFAULT_IDLE_TIMEOUT_S = 300
DEFAULT_REQUEST_TIMEOUT_S = 300
DEFAULT_MAX_RESTARTS = 2


class _WorkerTimeout(RuntimeError):
    """单条请求等待 worker 响应超时。"""


class _WorkerGone(RuntimeError):
    """worker 进程提前退出或管道断开。"""


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


class StableAudio3Provider(BaseSfxProvider):
    """Stable Audio 3 small-sfx：子进程桥接官方环境。"""

    def __init__(self, spec) -> None:
        """保存配置，并取出 runtime 段（解释器 / 官方仓库 / 常驻参数 / 生成默认值）。

        参数:
            spec: SfxSpec，`extra["runtime"]` 放 python、repo 与 worker 运行参数，
                `defaults` 放 steps / cfg / device。
        """
        super().__init__(spec)
        runtime = (spec.extra or {}).get("runtime") or {}
        self.python = runtime.get("python")
        self.repo = runtime.get("repo")
        self.timeout_s = int(runtime.get("timeout_s") or DEFAULT_TIMEOUT_S)
        self.idle_timeout_s = max(1.0, float(runtime.get("idle_timeout_s") or DEFAULT_IDLE_TIMEOUT_S))
        self.request_timeout_s = max(1.0, float(runtime.get("request_timeout_s") or DEFAULT_REQUEST_TIMEOUT_S))
        self.max_restarts = max(0, int(runtime.get("max_restarts", DEFAULT_MAX_RESTARTS)))
        defaults = spec.defaults or {}
        self.steps = int(defaults.get("steps") or 8)
        self.cfg = float(defaults.get("cfg") if defaults.get("cfg") is not None else 1.0)
        self.device = str(defaults.get("device") or "mps")
        # 常驻 worker 的进程、读线程与响应队列
        self._proc: subprocess.Popen | None = None
        self._responses: queue.Queue = queue.Queue()
        self._reader: threading.Thread | None = None
        self._seq = 0
        self._consecutive_failures = 0
        self._degraded = False
        self._lock = threading.Lock()

    # ---------- 健康检查 ----------

    def health(self) -> SfxHealth:
        """检查解释器、官方仓库、权重三件事。

        返回:
            SfxHealth：任何一项缺失都返回不可用，并给出具体缺什么。
        """
        pid = self.spec.provider_id
        if not self.python or not Path(self.python).exists():
            return SfxHealth(pid, False, f"独立解释器不存在：{self.python}（配置 sfx.providers[].runtime.python）", PROVIDER_DISABLED)
        if not self.repo or not (Path(self.repo) / "stable_audio_3").is_dir():
            return SfxHealth(pid, False, f"官方 stable-audio-3 仓库不存在：{self.repo}", PROVIDER_DISABLED)
        if not self.spec.weights_path:
            return SfxHealth(pid, False, "未配置 weights_path", MODEL_MISSING)
        weights = Path(self.spec.weights_path)
        missing = [
            name
            for name in ("model_config.json", "model.safetensors", "t5gemma-b-b-ul2/model.safetensors")
            if not (weights / name).exists()
        ]
        if missing:
            return SfxHealth(pid, False, f"权重不完整，缺 {missing}：{weights}", MODEL_MISSING)
        return SfxHealth(
            pid,
            True,
            f"环境就绪，权重 {weights}；常驻 worker（空闲 {self.idle_timeout_s:.0f}s 自动退出），"
            f"默认 {self.steps} 步 / cfg {self.cfg} / device {self.device}",
            None,
        )

    # ---------- 生成 ----------

    def generate(self, request: SfxRequest) -> SfxResult:
        """生成一段音效并原子落盘。

        参数:
            request: 请求（英文描述、时长、输出路径、可选步数/cfg/seed）。

        返回:
            SfxResult（含实测时长、采样率、耗时、峰值内存）。

        异常:
            SfxError：参数非法、权重缺失、worker 超时或失败。
        """
        pid = self.spec.provider_id
        if not request.prompt or not request.prompt.strip():
            raise SfxError(BAD_REQUEST, "音效描述不能为空", pid)
        if request.duration_s <= 0:
            raise SfxError(BAD_REQUEST, f"时长必须为正数：{request.duration_s}", pid)
        if not self.spec.weights_path:
            raise SfxError(MODEL_MISSING, "未配置 weights_path", pid)
        if not Path(self.spec.weights_path).is_dir():
            raise SfxError(MODEL_MISSING, f"权重目录不存在：{self.spec.weights_path}", pid)

        payload: dict = {
            "model_dir": self.spec.weights_path,
            "prompt": request.prompt,
            "negative_prompt": request.negative_prompt,
            "duration": float(request.duration_s),
            "out_path": str(request.output_path),
            "steps": int(request.steps or self.steps),
            "cfg": float(request.cfg if request.cfg is not None else self.cfg),
            "seed": int(request.seed if request.seed is not None else -1),
            "device": self.device,
        }

        t0 = time.time()
        data = self._generate_payload(payload)
        latency_ms = int((time.time() - t0) * 1000)
        return SfxResult(
            audio_path=data["out_path"],
            sample_rate=int(data["sample_rate"]),
            duration_s=float(data["duration_s"]),
            channels=int(data["channels"]),
            provider_id=pid,
            model=self.spec.model,
            latency_ms=latency_ms,
            steps=int(data["steps"]),
            cfg=float(data["cfg"]),
            seed=int(data["seed"]),
            load_s=float(data.get("load_s") or 0.0),
            peak_mib=float(data.get("peak_mib") or 0.0),
        )

    def _generate_payload(self, payload: dict) -> dict:
        """按需走常驻 worker，崩溃降级时回退到一次性进程。

        参数:
            payload: 传给 worker 的参数。

        返回:
            worker 回报的成功结果字典。

        异常:
            SfxError：生成失败、超时或 worker 不可用。
        """
        if self._degraded:
            return self._run_worker(payload)
        try:
            return self._resident_generate(payload)
        except SfxError as exc:
            if self._degraded and exc.code == PROVIDER_UNREACHABLE:
                return self._run_worker(payload)
            raise

    def _resident_generate(self, payload: dict) -> dict:
        """常驻 worker 生成一次：临时名落盘 → 收到成功响应 → os.replace 原子改名。

        参数:
            payload: 传给 worker 的参数。

        返回:
            成功结果字典（`out_path` 为最终路径）。

        异常:
            SfxError：请求超时、worker 崩溃或自报失败。
        """
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                try:
                    self._spawn_worker()
                except SfxError:
                    self._note_failure()
                    raise
            out = Path(payload["out_path"])
            out.parent.mkdir(parents=True, exist_ok=True)
            tmp = out.with_name(f"{out.name}.tmp-{os.getpid()}.wav")
            request = dict(payload)
            request["cmd"] = "synth"
            request["out_path"] = str(tmp)
            try:
                data = self._send_locked(request, self.request_timeout_s)
            except _WorkerTimeout as exc:
                self._kill_locked()
                _remove_quietly(tmp)
                self._note_failure()
                raise SfxError(
                    PROVIDER_TIMEOUT,
                    f"音效生成超时（>{self.request_timeout_s:.0f}s）",
                    self.spec.provider_id,
                ) from exc
            except _WorkerGone as exc:
                self._kill_locked()
                _remove_quietly(tmp)
                self._note_failure()
                raise SfxError(PROVIDER_UNREACHABLE, f"常驻 worker 已退出：{exc}", self.spec.provider_id) from exc
            if not data.get("ok"):
                _remove_quietly(tmp)
                raise SfxError(
                    PROVIDER_UNREACHABLE,
                    f"{data.get('error_type')}: {data.get('error')}",
                    self.spec.provider_id,
                )
            try:
                os.replace(tmp, out)
            except OSError as exc:
                _remove_quietly(tmp)
                raise SfxError(BAD_OUTPUT, f"写入音频失败：{exc}", self.spec.provider_id) from exc
            self._consecutive_failures = 0
            result = dict(data)
            result["out_path"] = str(out)
            return result

    def _note_failure(self) -> None:
        """累计一次常驻 worker 失败，达到重启上限后降级为一次性进程。"""
        self._consecutive_failures += 1
        if self._consecutive_failures > max(0, self.max_restarts):
            if not self._degraded:
                print(
                    f"[sfx] {self.spec.provider_id} 常驻 worker 连续失败 {self._consecutive_failures} 次，"
                    "自动降级为逐条起进程",
                    flush=True,
                )
            self._degraded = True

    def _spawn_worker(self) -> None:
        """启动常驻 worker 并做一次握手（此时模型尚未加载）。

        异常:
            SfxError：解释器/仓库缺失或握手失败。
        """
        pid = self.spec.provider_id
        if not self.python or not Path(self.python).exists():
            raise SfxError(PROVIDER_DISABLED, f"独立解释器不存在：{self.python}", pid)
        if not self.repo or not (Path(self.repo) / "stable_audio_3").is_dir():
            raise SfxError(PROVIDER_DISABLED, f"官方 stable-audio-3 仓库不存在：{self.repo}", pid)
        env = dict(os.environ)
        env["PYTHONPATH"] = str(self.repo)
        env.setdefault("PYTHONUNBUFFERED", "1")
        env.setdefault("HF_HUB_OFFLINE", "1")
        env.setdefault("TRANSFORMERS_OFFLINE", "1")
        # 关掉采样进度条，避免 stderr 噪声混进 CLI/批次日志
        env.setdefault("TQDM_DISABLE", "1")
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
            raise SfxError(PROVIDER_UNREACHABLE, f"常驻 worker 握手失败：{exc}", pid) from exc
        if not reply.get("ok"):
            self._kill_locked()
            raise SfxError(PROVIDER_UNREACHABLE, f"常驻 worker 握手返回异常：{reply}", pid)

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
        """起一次性子进程跑 worker，解析最后一行 `NDW_RESULT` JSON。

        参数:
            payload: 传给 worker 的参数。

        返回:
            worker 回报的成功结果字典。

        异常:
            SfxError：超时、非零退出、结果缺失或自报失败。
        """
        pid = self.spec.provider_id
        env = dict(os.environ)
        env["PYTHONPATH"] = str(self.repo)
        env.setdefault("PYTHONUNBUFFERED", "1")
        env.setdefault("HF_HUB_OFFLINE", "1")
        env.setdefault("TRANSFORMERS_OFFLINE", "1")
        env.setdefault("TQDM_DISABLE", "1")
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
            raise SfxError(PROVIDER_TIMEOUT, f"音效生成超时（>{self.timeout_s}s）", pid) from exc

        result = self._parse_result(proc.stdout)
        if result is None:
            _remove_quietly(tmp)
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-6:]
            raise SfxError(
                PROVIDER_UNREACHABLE,
                f"worker 未返回结果（exit={proc.returncode}）：{' | '.join(tail)}",
                pid,
            )
        if not result.get("ok"):
            _remove_quietly(tmp)
            raise SfxError(
                PROVIDER_UNREACHABLE,
                f"{result.get('error_type')}: {result.get('error')}",
                pid,
            )
        try:
            os.replace(tmp, out)
        except OSError as exc:
            _remove_quietly(tmp)
            raise SfxError(BAD_OUTPUT, f"写入音频失败：{exc}", pid) from exc
        result = dict(result)
        result["out_path"] = str(out)
        return result

    @staticmethod
    def _parse_result(stdout: str) -> dict | None:
        """从一次性 worker 的标准输出里取最后一行 `NDW_RESULT` JSON。

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
