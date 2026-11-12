"""VoxCPM2 音色设计 worker：读参数、生成若干候选、按 NDJSON 汇报进度。

该脚本运行在独立的 voxcpm venv（torch/transformers 与主工程隔离），
由主进程一次性拉起；每次生成完一条候选立即落盘（先写临时名再 os.replace），
收到 SIGTERM 时删除未完成的临时文件后退出，避免留下半截 wav 或残留进程。

协议（stdout，每行一个 JSON）：
    {"stage": "loading"}
    {"stage": "generating", "index": 1, "total": 4}
    {"stage": "candidate", "index": 1, "file": "cand-01.wav", "seed": 101, "duration_ms": 8123}
    {"stage": "done", "files": [...]}
    {"stage": "error", "message": "..."}
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
from pathlib import Path

_current_tmp: Path | None = None


def _emit(payload: dict) -> None:
    """向主进程输出一条 JSON 进度（stdout 只用于协议）。"""
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _cleanup() -> None:
    """删除未完成的临时文件（幂等）。"""
    if _current_tmp is not None:
        try:
            _current_tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _on_signal(signum, _frame) -> None:
    """收到终止信号时清理临时文件并立即退出。"""
    _cleanup()
    os._exit(128 + int(signum))


def _parse_args() -> argparse.Namespace:
    """解析命令行参数（由主进程组装）。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--prompt-file", required=True)
    parser.add_argument("--script-file", required=True)
    parser.add_argument("--count", type=int, default=4)
    parser.add_argument("--cfg", type=float, default=2.0)
    parser.add_argument("--timesteps", type=int, default=10)
    parser.add_argument("--device", default="auto")
    parser.add_argument("--seed-base", type=int, default=1000)
    return parser.parse_args()


def main() -> int:
    """加载模型、逐条生成候选音频并汇报进度。

    返回:
        0 表示全部成功；2 表示生成异常（主进程据此回退与清理）。
    """
    global _current_tmp
    args = _parse_args()
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    import librosa
    import numpy as np
    import soundfile as sf
    import torch
    from voxcpm import VoxCPM

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    prompt = (Path(args.prompt_file).read_text(encoding="utf-8").strip()).strip("()")
    script = Path(args.script_file).read_text(encoding="utf-8").strip()

    _emit({"stage": "loading"})
    try:
        model = VoxCPM.from_pretrained(
            args.model_dir,
            load_denoiser=False,
            local_files_only=True,
            device=None if args.device in ("", "auto") else args.device,
        )
    except Exception as exc:  # noqa: BLE001 - 统一转成协议错误，由主进程提示
        _emit({"stage": "error", "message": f"模型加载失败：{exc}"})
        return 2

    sample_rate = int(getattr(model.tts_model, "sample_rate", 48000) or 48000)
    files: list[dict] = []
    for index in range(1, args.count + 1):
        seed = args.seed_base + index
        _emit({"stage": "generating", "index": index, "total": args.count})
        torch.manual_seed(seed)
        text = f"({prompt}) {script}"
        try:
            wav = model.generate(
                text=text,
                cfg_value=args.cfg,
                inference_timesteps=args.timesteps,
                normalize=True,
            )
        except Exception as exc:  # noqa: BLE001 - 单条失败不打断整批
            _emit({"stage": "error", "message": f"第 {index} 条生成失败：{exc}"})
            return 2

        audio = np.asarray(wav, dtype=np.float32).reshape(-1)
        if sample_rate != 24000:
            # 参考音统一 24k 单声道，避免下游在错误采样率上做处理
            audio = librosa.resample(audio, orig_sr=sample_rate, target_sr=24000)
        audio = np.clip(audio, -1.0, 1.0)
        name = f"cand-{index:02d}.wav"
        final = out_dir / name
        _current_tmp = out_dir / f".{name}.tmp.wav"
        sf.write(str(_current_tmp), audio, 24000, subtype="PCM_16")
        os.replace(_current_tmp, final)
        _current_tmp = None
        duration_ms = round(len(audio) / 24000 * 1000)
        files.append({"index": index, "file": name, "seed": seed, "duration_ms": duration_ms})
        _emit({"stage": "candidate", "index": index, "file": name, "seed": seed, "duration_ms": duration_ms})

    _emit({"stage": "done", "files": files, "sample_rate": 24000})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
