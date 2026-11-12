"""Stable Audio Open 1.0 推理 worker（在独立环境里执行）。

工作台进程通过子进程调用本脚本，两种入口：

- 一次性：stdin 读一个 JSON，stdout 最后一行回 `NDW_RESULT {json}` 后退出；
- 常驻：`--serve` 起 NDJSON 循环，同一进程连续处理多行请求，模型只加载一次。
  协议行写到独立 fd，三方库日志全部改道 stderr，避免污染响应。

请求字段：
    model_dir     必填，权重目录（model_config.json + model.safetensors + t5-base/）
    prompt        必填，英文描述（中文改写由上层适配层完成）
    duration      必填，秒（模型训练窗口 2097152/44100 ≈ 47.55s，超出直接报错）
    out_path      必填，wav 输出路径（常驻模式传临时名，由上层原子改名）
    steps / cfg   可选，缺省 100 步 / cfg 7.0（官方推荐区间）
    seed          可选，-1 表示随机（worker 会先摇成具体值再回报）
    device        可选，mps（默认）/ cpu

响应字段（成功）：
    {"ok": true, "out_path", "duration_s", "sample_rate", "channels",
     "infer_s", "load_s", "peak_mib", "steps", "cfg", "seed", "apg_scale"}
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
    """按行读 stdin，并支持空闲超时（避免与三方库争用缓冲）。"""

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
        self.model = None
        self.config: dict | None = None
        self.key: tuple | None = None
        self.sample_rate = 44100
        self.channels = 2
        self.max_seconds = 47.0
        self.load_s = 0.0

    def ensure(self, model_dir: str, device: str) -> None:
        """确保模型已按当前复用键加载。

        参数:
            model_dir: 权重目录。
            device: mps / cpu。
        """
        key = (model_dir, device)
        if self.model is not None and self.key == key:
            return
        self.model = None
        self.key = None
        started = time.time()

        import torch
        from stable_audio_tools.models.factory import create_model_from_config
        from stable_audio_tools.models.utils import load_ckpt_state_dict

        config_path = os.path.join(model_dir, "model_config.json")
        with open(config_path, encoding="utf-8") as f:
            cfg = json.load(f)
        self.sample_rate = int(cfg.get("sample_rate") or 44100)
        self.channels = int(cfg.get("audio_channels") or 2)
        max_samples = int(cfg.get("sample_size") or 0)
        if max_samples > 0:
            self.max_seconds = max_samples / float(self.sample_rate)

        # 文本编码器指向权重目录内部，保证离线可加载（不依赖 HF 缓存或受限仓库）
        t5_dir = os.path.join(model_dir, "t5-base")
        for cond in cfg.get("model", {}).get("conditioning", {}).get("configs", []):
            if cond.get("type") == "t5":
                conf = cond.setdefault("config", {})
                conf.pop("repo_id", None)
                conf.pop("subfolder", None)
                conf["model_path"] = t5_dir

        model = create_model_from_config(cfg)
        weights = os.path.join(model_dir, "model.safetensors")
        model.load_state_dict(load_ckpt_state_dict(weights))
        model = model.to(device).eval()
        if device == "mps":
            # 统一内存：先触发一次极小前向，把权重页真正落到设备上
            torch.mps.synchronize()

        self.model = model
        self.config = cfg
        self.key = key
        self.load_s = time.time() - started

    def synth(self, req: dict) -> dict:
        """按请求生成一段配乐并写成 wav。

        参数:
            req: 请求字典（见模块文档）。

        返回:
            成功响应字典。

        异常:
            ValueError: 参数非法（时长越界、目录缺失等）。
        """
        model_dir = str(req.get("model_dir") or "")
        prompt = str(req.get("prompt") or "").strip()
        duration = float(req.get("duration") or 0)
        out_path = str(req.get("out_path") or "")
        if not model_dir or not os.path.isdir(model_dir):
            raise ValueError(f"权重目录不存在：{model_dir}")
        if not prompt:
            raise ValueError("配乐描述不能为空")
        if not out_path:
            raise ValueError("缺少 out_path")
        if duration <= 0:
            raise ValueError(f"时长必须为正数：{duration}")
        device = str(req.get("device") or "mps")

        self.ensure(model_dir, device)
        if duration > self.max_seconds + 1e-6:
            raise ValueError(
                f"Stable Audio Open 1.0 最长 {self.max_seconds:.1f} 秒，请求 {duration:g} 秒"
            )

        import soundfile as sf
        import torch
        from stable_audio_tools.inference.generation import generate_diffusion_cond

        steps = int(req.get("steps") or 100)
        cfg_scale = float(req.get("cfg") if req.get("cfg") is not None else 7.0)
        seed = int(req.get("seed") if req.get("seed") is not None else -1)
        if seed < 0:
            # 随机种子先摇成具体值，便于把可复现的 seed 回报给上层
            seed = random.randint(0, 2**31 - 1)
        # MPS 不支持 float64，APG 投影会强制转 double 而报错；统一走 vanilla CFG
        apg_scale = 0.0

        conditioning = [
            {
                "prompt": prompt,
                "seconds_start": 0,
                "seconds_total": duration,
            }
        ]
        sample_size = int(self.sample_rate * duration)
        started = time.time()
        with torch.inference_mode():
            audio = generate_diffusion_cond(
                self.model,
                steps=steps,
                cfg_scale=cfg_scale,
                conditioning=conditioning,
                sample_size=sample_size,
                seed=seed,
                device=device,
                batch_size=1,
                apg_scale=apg_scale,
                disable_tqdm=True,
            )
        infer_s = time.time() - started

        audio = audio.to(torch.float32).cpu().clamp(-1, 1)
        if audio.dim() == 3:
            audio = audio[0]
        samples = audio.shape[-1]
        # soundfile 需要 [frames, channels]
        sf.write(out_path, audio.transpose(0, 1).numpy(), self.sample_rate, subtype="PCM_24")
        return {
            "ok": True,
            "out_path": out_path,
            "duration_s": round(samples / self.sample_rate, 3),
            "sample_rate": self.sample_rate,
            "channels": int(audio.shape[0]),
            "infer_s": round(infer_s, 2),
            "load_s": round(self.load_s, 2),
            "peak_mib": round(_peak_mib(), 1),
            "steps": steps,
            "cfg": cfg_scale,
            "seed": seed,
            "apg_scale": apg_scale,
            "max_seconds": round(self.max_seconds, 2),
        }


def _handle_synth(engine: _Engine, req: dict) -> dict:
    """执行一条合成请求，把异常收进响应字典。

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
    # 协议行独占原始 stdout 的副本；把 fd 1 改道 stderr，三方库日志不再污染响应
    proto = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1)
    os.dup2(2, 1)
    _install_signal_handlers()
    engine = _Engine()
    reader = _LineReader()
    print(f"[ndw-bgm-worker] serving pid={os.getpid()} idle={idle_timeout_s}s", file=sys.stderr, flush=True)

    while True:
        line = reader.readline(timeout=idle_timeout_s)
        if line is None:
            print("[ndw-bgm-worker] idle timeout, exit", file=sys.stderr, flush=True)
            return 0
        if line == b"":
            print("[ndw-bgm-worker] stdin closed, exit", file=sys.stderr, flush=True)
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
                    "loaded": engine.model is not None,
                    "model_dir": (engine.key or (None, None))[0],
                },
                proto,
            )
            continue
        if cmd == "shutdown":
            _emit({"id": req.get("id"), "ok": True, "bye": True}, proto)
            print("[ndw-bgm-worker] shutdown requested", file=sys.stderr, flush=True)
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
    os.environ.setdefault("TQDM_DISABLE", "1")

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
