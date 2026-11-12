"""Stable Audio 3 small-sfx 推理 worker（在官方独立环境里执行）。

工作台进程（Python 3.12）通过子进程调用本脚本，两种入口：

- 一次性：stdin 读一个 JSON，stdout 最后一行回 `NDW_RESULT {json}` 后退出；
- 常驻：`--serve` 起 NDJSON 循环，同一进程连续处理多行请求，模型只加载一次。
  协议行写到独立 fd，官方库日志全部改道 stderr，避免污染响应。

之所以独立成脚本：官方仓库要求 torch==2.7.1 / transformers>=5.8，与工作台
venv 的 torch 2.15 不兼容，只能分环境跑。

请求字段：
    prompt            必填，英文描述（中文改写由上层适配层完成）
    negative_prompt   可选，负面描述
    duration          必填，秒（0.5~120）
    out_path          必填，wav 输出路径（常驻模式传临时名，由上层原子改名）
    model_dir         必填，权重目录（model_config.json + model.safetensors + t5gemma-b-b-ul2/）
    steps / cfg       可选，缺省用 8 步 / cfg 1.0（官方默认）
    seed              可选，-1 表示随机（worker 会先摇成具体值再回报）
    device            可选，mps（默认）/ cpu

响应字段（成功）：
    {"ok": true, "out_path", "duration_s", "sample_rate", "channels",
     "infer_s", "load_s", "peak_mib", "steps", "cfg", "seed"}
"""

from __future__ import annotations

import argparse
import json
import os
import random
import resource
import select
import signal
import sys
import time
import traceback

RESULT_PREFIX = "NDW_RESULT "
DEFAULT_IDLE_TIMEOUT_S = 300

#: 当前正在写的临时音频；收到 SIGTERM 时先删掉它再退出
_current_tmp: dict = {"path": None}


def _emit(payload: dict, stream=None) -> None:
    """打印一行 JSON（一次性入口带 `NDW_RESULT` 前缀，常驻入口不带前缀）。

    参数:
        payload: 要回给上层的字典。
        stream: 输出流；缺省为标准输出。
    """
    out = stream or sys.stdout
    line = json.dumps(payload, ensure_ascii=False)
    if stream is None:
        line = RESULT_PREFIX + line
    print(line, file=out, flush=True)


def _cleanup(path: str | None) -> None:
    """删除临时文件（不存在或失败都忽略）。

    参数:
        path: 文件路径，可为空。
    """
    if not path:
        return
    try:
        os.unlink(path)
    except OSError:
        pass


def _peak_mib() -> float:
    """返回当前进程生命周期内的峰值常驻内存（MiB）。"""
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024)


def _on_signal(signum, frame) -> None:
    """收到终止信号时先清理临时文件再立即退出。"""
    _cleanup(_current_tmp.get("path"))
    os._exit(0)


def _install_signal_handlers() -> None:
    """安装 SIGTERM / SIGINT 处理，确保被停止时清理临时文件。"""
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _on_signal)
        except (ValueError, OSError):
            pass


class _LineReader:
    """按行读 stdin，并支持空闲超时（避免与官方库争用缓冲）。"""

    def __init__(self) -> None:
        """初始化内部字节缓冲。"""
        self._buf = b""

    def readline(self, timeout: float | None = None) -> bytes | None:
        """读一行；超时返回 None，EOF 返回空字节串。

        参数:
            timeout: 等待上限（秒）；None 表示一直等。

        返回:
            一行字节（不含换行）或 None（超时）或 b""（EOF）。
        """
        while b"\n" not in self._buf:
            try:
                ready, _, _ = select.select([0], [], [], timeout)
            except InterruptedError:
                return None
            if not ready:
                return None
            chunk = os.read(0, 65536)
            if not chunk:
                if self._buf:
                    line, self._buf = self._buf, b""
                    return line
                return b""
            self._buf += chunk
        line, _, self._buf = self._buf.partition(b"\n")
        return line


class _Engine:
    """模型持有者：按 (model_dir, device) 复用，首次推理时懒加载。"""

    def __init__(self) -> None:
        """初始化空引擎。"""
        self.sam = None
        self.key: tuple | None = None
        self.sample_rate = 44100
        self.channels = 2
        self.load_s = 0.0

    def ensure(self, model_dir: str, device: str) -> None:
        """确保模型已按当前复用键加载。

        参数:
            model_dir: 权重目录。
            device: mps / cpu。
        """
        key = (model_dir, device)
        if self.sam is not None and self.key == key:
            return
        self.sam = None
        self.key = None
        started = time.time()

        import torch
        from stable_audio_3.loading_utils import load_diffusion_cond
        from stable_audio_3.model import StableAudioModel

        with open(os.path.join(model_dir, "model_config.json"), encoding="utf-8") as f:
            cfg = json.load(f)
        self.sample_rate = int(cfg.get("sample_rate") or 44100)
        self.channels = int(cfg.get("audio_channels") or 2)

        # 文本编码器指向权重目录内部，保证离线可加载（不依赖 HF 缓存或受限仓库）
        text_dir = os.path.join(model_dir, "t5gemma-b-b-ul2")
        for cond in cfg.get("model", {}).get("conditioning", {}).get("configs", []):
            if cond.get("type") == "t5gemma":
                conf = cond.setdefault("config", {})
                conf.pop("repo_id", None)
                conf.pop("subfolder", None)
                conf["model_path"] = text_dir

        model = load_diffusion_cond(
            cfg,
            os.path.join(model_dir, "model.safetensors"),
            device=device,
            model_half=False,
        )
        self.sam = StableAudioModel(model, cfg, torch.device(device), False)
        self.key = key
        self.load_s = round(time.time() - started, 2)
        print(f"[ndw-sfx-worker] model loaded: {model_dir} device={device} {self.load_s}s", file=sys.stderr, flush=True)

    def synth(self, req: dict) -> dict:
        """按一条请求生成音频并写到 `out_path`，返回结果字典。

        参数:
            req: 请求字典。

        返回:
            成功结果；失败时抛异常，由调用方翻译成错误响应。
        """
        out_path = req["out_path"]
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        self.ensure(req["model_dir"], req.get("device") or "mps")

        duration = max(1.0, min(120.0, float(req["duration"])))
        steps = int(req.get("steps") or 8)
        cfg = float(req.get("cfg") if req.get("cfg") is not None else 1.0)
        seed = int(req.get("seed") if req.get("seed") is not None else -1)
        if seed < 0:
            seed = random.randint(0, 99999)

        import torchaudio

        started = time.time()
        audio = self.sam.generate(
            prompt=req["prompt"],
            negative_prompt=req.get("negative_prompt"),
            duration=duration,
            steps=steps,
            cfg_scale=cfg,
            seed=seed,
        )
        infer_s = round(time.time() - started, 3)
        audio = audio.detach().to("cpu")
        torchaudio.save(out_path, audio[0], self.sample_rate)
        return {
            "ok": True,
            "out_path": out_path,
            "duration_s": round(audio.shape[-1] / self.sample_rate, 3),
            "sample_rate": self.sample_rate,
            "channels": int(audio.shape[1]),
            "infer_s": infer_s,
            "load_s": self.load_s,
            "peak_mib": round(_peak_mib(), 1),
            "steps": steps,
            "cfg": cfg,
            "seed": seed,
        }


def _handle_synth(engine: _Engine, req: dict) -> dict:
    """处理一条 synth 请求，把异常翻译成错误响应。

    参数:
        engine: 模型持有者。
        req: 请求字典。

    返回:
        响应字典（含 id）。
    """
    req_id = req.get("id")
    tmp = req.get("out_path")
    _current_tmp["path"] = tmp
    try:
        data = engine.synth(req)
        data["id"] = req_id
        return data
    except Exception as exc:  # noqa: BLE001 - worker 边界，必须把错误原文回给上层
        _cleanup(tmp)
        return {
            "id": req_id,
            "ok": False,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "traceback": traceback.format_exc()[-4000:],
        }
    finally:
        _current_tmp["path"] = None


def serve(idle_timeout_s: float = DEFAULT_IDLE_TIMEOUT_S) -> int:
    """常驻模式：逐行读 NDJSON 请求，按 id 回包，空闲超时或 shutdown 后退出。

    参数:
        idle_timeout_s: 空闲多久自动退出（秒）。

    返回:
        进程退出码。
    """
    # 协议行独占原始 stdout 的副本；把 fd 1 改道 stderr，官方库日志不再污染响应
    proto = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1)
    os.dup2(2, 1)
    _install_signal_handlers()
    engine = _Engine()
    reader = _LineReader()
    print(f"[ndw-sfx-worker] serving pid={os.getpid()} idle={idle_timeout_s}s", file=sys.stderr, flush=True)

    while True:
        line = reader.readline(timeout=idle_timeout_s)
        if line is None:
            print("[ndw-sfx-worker] idle timeout, exit", file=sys.stderr, flush=True)
            return 0
        if line == b"":
            print("[ndw-sfx-worker] stdin closed, exit", file=sys.stderr, flush=True)
            return 0
        try:
            req = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue

        cmd = req.get("cmd") or "synth"
        if cmd == "ping":
            _emit(
                {
                    "id": req.get("id"),
                    "ok": True,
                    "pong": True,
                    "pid": os.getpid(),
                    "loaded": engine.sam is not None,
                    "model_dir": (engine.key or (None, None))[0],
                },
                proto,
            )
            continue
        if cmd == "shutdown":
            _emit({"id": req.get("id"), "ok": True, "bye": True}, proto)
            print("[ndw-sfx-worker] shutdown requested", file=sys.stderr, flush=True)
            return 0
        if cmd != "synth":
            _emit({"id": req.get("id"), "ok": False, "error_type": "ValueError", "error": f"unknown cmd: {cmd}"}, proto)
            continue
        _emit(_handle_synth(engine, req), proto)


def main() -> int:
    """入口：`--serve` 走常驻模式，否则一次性执行一条请求。返回进程退出码。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--idle-timeout", type=float, default=DEFAULT_IDLE_TIMEOUT_S)
    args = parser.parse_args()

    # 权重与文本编码器全部来自本地目录，禁止 worker 联网兜底
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

    if args.serve:
        return serve(args.idle_timeout)

    req = json.loads(sys.stdin.read() or "{}")
    _install_signal_handlers()
    t0 = time.time()
    engine = _Engine()
    result = _handle_synth(engine, req)
    result.setdefault("total_s", round(time.time() - t0, 3))
    _emit(result)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
