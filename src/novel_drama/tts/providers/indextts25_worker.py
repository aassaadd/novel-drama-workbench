"""IndexTTS 2.5 推理 worker（在官方 3.11 环境里执行，只依赖标准库 + indextts）。

工作台进程（Python 3.12）通过子进程调用本脚本，支持两种入口：

- 一次性：stdin 读一个 JSON，stdout 最后一行回 `NDW_RESULT {json}` 后退出（旧链路，保留）；
- 常驻：`--serve` 起 NDJSON 循环，同一进程连续处理多行请求，模型只加载一次。
  协议行写到独立 fd，官方库日志全部改道 stderr，避免污染响应。

之所以独立成脚本：官方 pyproject 要求 >=3.10,<3.12 且钉 torch/torchaudio 2.8.*，
与工作台 venv(3.12.9, torch 2.15.0) 不兼容，只能分环境跑。

输入字段（除 model_dir/text/out_path 外都可缺省）：
    text              必填，台词文本
    out_path          必填，wav 输出路径（常驻模式传临时名，由上层 os.replace 改名）
    model_dir         必填，权重目录（含 config.yaml / gpt.pth）
    ref_audio         参考音频（音色克隆），对应官方 spk_audio_prompt
    ref_fingerprint   参考音频指纹，用于同路径文件被覆盖时让官方缓存失效
    lang              语言，默认 ZH
    emo_vector        8 维情绪向量，顺序见 EMO_ORDER
    emo_alpha         情绪强度 0~1
    emo_text          文字情绪（需 use_qwen_emo=True 构造，本脚本自动打开）
    emo_audio_prompt  情绪参考音频（与 emo_vector/emo_text 互斥，官方会清空它）
    duration_factor   时长缩放因子

一次性输出：成功 {"ok": true, "out_path", "duration_s", "sample_rate", "elapsed_s", "peak_rss_mib"}
            失败 {"ok": false, "error_type", "error", "traceback"}
"""

from __future__ import annotations

import json
import os
import resource
import select
import signal
import sys
import time
import traceback

RESULT_PREFIX = "NDW_RESULT "
# 官方 README.md:87-95 给出的 8 维情绪顺序
EMO_ORDER = ["happy", "angry", "sad", "afraid", "disgusted", "melancholic", "surprised", "calm"]
DEFAULT_IDLE_TIMEOUT_S = 300

#: 当前正在写的临时音频；收到 SIGTERM 时先删掉它再退出，避免留下半截 wav
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
    """删除一个临时文件（不存在或删除失败都忽略）。

    参数:
        path: 文件路径，可为空。
    """
    if not path:
        return
    try:
        os.unlink(path)
    except OSError:
        pass


def _on_signal(signum, frame) -> None:
    """收到终止信号时先清理临时文件再立即退出。

    说明:
        MPS 推理无法在句子中途安全中断，因此不等待当前 infer 结束；
        直接删掉临时名可保证最终路径永远不会出现半截文件。
    """
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
    """模型持有者：按 (model_dir, use_qwen_emo) 复用，参考音频变化只失效缓存。"""

    def __init__(self) -> None:
        """初始化空引擎。"""
        self.tts = None
        self.key: tuple | None = None
        self._ref_fps: dict[str, str] = {}

    def ensure(self, model_dir: str, use_qwen_emo: bool) -> None:
        """确保模型已按当前复用键加载。

        参数:
            model_dir: 权重目录。
            use_qwen_emo: 是否启用 qwen 文字情绪模型。
        """
        key = (model_dir, bool(use_qwen_emo))
        if self.tts is not None and self.key == key:
            return
        # 换模型/换情绪模式：释放旧实例再加载，避免两份权重同时占内存
        self.tts = None
        self.key = None
        self._ref_fps = {}
        from indextts.infer_v2_5 import IndexTTS2

        # use_qwen_emo 必须在构造时打开，否则 use_emo_text 会抛 RuntimeError（infer_v2_5.py:588-600）
        # device/use_bf16 交给官方 MPS 分支自己决定，不手动改成 bf16（infer_v2_5.py:107-110）
        self.tts = IndexTTS2(
            cfg_path=os.path.join(model_dir, "config.yaml"),
            model_dir=model_dir,
            use_bf16=False,
            device="mps",
            use_qwen_emo=bool(use_qwen_emo),
        )
        self.key = key
        print(f"[ndw-worker] model loaded: {model_dir} use_qwen_emo={bool(use_qwen_emo)}", file=sys.stderr, flush=True)

    def _invalidate_ref_cache(self) -> None:
        """清空官方按路径字符串缓存的参考音频结果。

        说明:
            官方只比较路径字符串（infer_v2_5.py:620），同名文件被覆盖时不会重新编码；
            指纹变化时显式清空 `cache_*` 字段，强制下一次推理重新编码。
        """
        if self.tts is None:
            return
        for name in list(vars(self.tts).keys()):
            if name.startswith("cache_"):
                try:
                    setattr(self.tts, name, None)
                except Exception:  # noqa: BLE001, S110 - 官方字段可能只读，忽略即可
                    pass

    def synth(self, req: dict) -> dict:
        """按一条请求合成到 `out_path`，返回结果字典。

        参数:
            req: 请求字典。

        返回:
            成功结果；失败时抛出异常，由调用方翻译成错误响应。
        """
        out_path = req["out_path"]
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        self.ensure(req["model_dir"], bool(req.get("emo_text")))

        # 同路径参考音频被覆盖：指纹变化时让官方缓存失效，否则会沿用旧音色
        fp = req.get("ref_fingerprint")
        ref = req["ref_audio"]
        prev = self._ref_fps.get(ref)
        if fp and prev is not None and prev != fp:
            self._invalidate_ref_cache()
        if fp:
            self._ref_fps[ref] = fp

        kwargs: dict = {
            "spk_audio_prompt": ref,
            "text": req["text"],
            "output_path": out_path,
            "lang": req.get("lang") or "ZH",
            "verbose": False,
        }
        if req.get("emo_vector"):
            kwargs["emo_vector"] = req["emo_vector"]
            kwargs["emo_alpha"] = float(req.get("emo_alpha") or 1.0)
        if req.get("emo_text"):
            kwargs["use_emo_text"] = True
            kwargs["emo_text"] = req["emo_text"]
            kwargs["emo_alpha"] = float(req.get("emo_alpha") or 1.0)
        if req.get("emo_audio_prompt"):
            kwargs["emo_audio_prompt"] = req["emo_audio_prompt"]
            kwargs["emo_alpha"] = float(req.get("emo_alpha") or 1.0)
        if req.get("duration_factor"):
            kwargs["duration_factor"] = float(req["duration_factor"])

        started = time.time()
        self.tts.infer(**kwargs)
        infer_s = round(time.time() - started, 2)

        import soundfile as sf  # 官方依赖里已有 soundfile

        info = sf.info(out_path)
        return {
            "ok": True,
            "out_path": out_path,
            "duration_s": round(info.frames / info.samplerate, 3),
            "sample_rate": info.samplerate,
            "infer_s": infer_s,
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
    # 协议行独占原始 stdout 的副本；把 fd 1 改道 stderr，官方库的 print 不再污染响应
    proto = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1)
    os.dup2(2, 1)
    _install_signal_handlers()
    engine = _Engine()
    reader = _LineReader()
    print(f"[ndw-worker] serving pid={os.getpid()} idle={idle_timeout_s}s", file=sys.stderr, flush=True)

    while True:
        line = reader.readline(timeout=idle_timeout_s)
        if line is None:
            print("[ndw-worker] idle timeout, exit", file=sys.stderr, flush=True)
            return 0
        if line == b"":
            print("[ndw-worker] stdin closed, exit", file=sys.stderr, flush=True)
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
                    "loaded": engine.tts is not None,
                    "model_dir": (engine.key or (None, None))[0],
                    "use_qwen_emo": bool((engine.key or (None, None))[1]),
                },
                proto,
            )
            continue
        if cmd == "shutdown":
            _emit({"id": req.get("id"), "ok": True, "bye": True}, proto)
            print("[ndw-worker] shutdown requested", file=sys.stderr, flush=True)
            return 0
        if cmd != "synth":
            _emit({"id": req.get("id"), "ok": False, "error_type": "ValueError", "error": f"unknown cmd: {cmd}"}, proto)
            continue
        _emit(_handle_synth(engine, req), proto)


def main() -> int:
    """一次性入口：读 stdin、跑一次推理、回一行结果 JSON。返回进程退出码。"""
    t0 = time.time()
    req = json.loads(sys.stdin.read() or "{}")
    _install_signal_handlers()
    engine = _Engine()
    _current_tmp["path"] = req.get("out_path")
    try:
        data = engine.synth(req)
    except Exception as exc:  # noqa: BLE001 - worker 边界，必须把错误原文回给上层
        _cleanup(req.get("out_path"))
        _emit(
            {
                "ok": False,
                "error_type": type(exc).__name__,
                "error": str(exc),
                "traceback": traceback.format_exc()[-4000:],
            }
        )
        return 1
    finally:
        _current_tmp["path"] = None
    data["elapsed_s"] = round(time.time() - t0, 2)
    data["peak_rss_mib"] = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 / 1024, 0)
    _emit(data)
    return 0


def _parse_argv(argv: list[str]) -> tuple[bool, float]:
    """解析命令行：`--serve` 与 `--idle-timeout`。

    参数:
        argv: 去掉脚本名后的参数列表。

    返回:
        (是否常驻, 空闲超时秒数)。
    """
    serve_mode = "--serve" in argv
    idle = DEFAULT_IDLE_TIMEOUT_S
    if "--idle-timeout" in argv:
        try:
            idle = max(1.0, float(argv[argv.index("--idle-timeout") + 1]))
        except (IndexError, ValueError):
            idle = DEFAULT_IDLE_TIMEOUT_S
    return serve_mode, idle


if __name__ == "__main__":
    serve_mode, idle_timeout = _parse_argv(sys.argv[1:])
    try:
        sys.exit(serve(idle_timeout) if serve_mode else main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - 兜底：至少把错误写进日志
        print(f"[ndw-worker] fatal: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        traceback.print_exc()
        sys.exit(1)
