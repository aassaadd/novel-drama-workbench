"""IndexTTS 2.5 推理 worker（在官方 3.11 环境里执行，只依赖标准库 + indextts）。

工作台进程（Python 3.12）通过子进程调用本脚本，stdin 收 JSON、stdout 最后一行回
`NDW_RESULT {json}`。之所以独立成脚本：官方 pyproject 要求 >=3.10,<3.12 且钉
torch/torchaudio 2.8.*，与工作台 venv(3.12.9, torch 2.15.0) 不兼容，只能分环境跑。

输入字段（除 model_dir/text/out_path 外都可缺省）：
    text              必填，台词文本
    out_path          必填，wav 输出路径
    model_dir         必填，权重目录（含 config.yaml / gpt.pth）
    ref_audio         参考音频（音色克隆），对应官方 spk_audio_prompt
    lang              语言，默认 ZH
    emo_vector        8 维情绪向量，顺序见 EMO_ORDER
    emo_alpha         情绪强度 0~1
    emo_text          文字情绪（需 use_qwen_emo=True 构造，本脚本自动打开）
    emo_audio_prompt  情绪参考音频（与 emo_vector/emo_text 互斥，官方会清空它）
    duration_factor   时长缩放因子

输出：成功 {"ok": true, "out_path", "duration_s", "sample_rate", "elapsed_s", "peak_rss_mib"}
      失败 {"ok": false, "error_type", "error", "traceback"}
"""

from __future__ import annotations

import json
import os
import resource
import sys
import time
import traceback

RESULT_PREFIX = "NDW_RESULT "
# 官方 README.md:87-95 给出的 8 维情绪顺序
EMO_ORDER = ["happy", "angry", "sad", "afraid", "disgusted", "melancholic", "surprised", "calm"]


def _emit(payload: dict) -> None:
    """打印一行带前缀的结果 JSON；库自身的日志都在前缀之外，不影响解析。"""
    print(RESULT_PREFIX + json.dumps(payload, ensure_ascii=False), flush=True)


def main() -> int:
    """读 stdin、跑一次推理、回一行结果 JSON。返回进程退出码。"""
    t0 = time.time()
    req = json.loads(sys.stdin.read() or "{}")
    model_dir = req["model_dir"]
    out_path = req["out_path"]
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

    use_emo_text = bool(req.get("emo_text"))
    from indextts.infer_v2_5 import IndexTTS2

    # use_qwen_emo 必须在构造时打开，否则 use_emo_text 会抛 RuntimeError（infer_v2_5.py:588-600）
    # device/use_bf16 交给官方 MPS 分支自己决定，不手动改成 bf16（infer_v2_5.py:107-110）
    tts = IndexTTS2(
        cfg_path=os.path.join(model_dir, "config.yaml"),
        model_dir=model_dir,
        use_bf16=False,
        device="mps",
        use_qwen_emo=use_emo_text,
    )

    kwargs: dict = {
        "spk_audio_prompt": req["ref_audio"],
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

    tts.infer(**kwargs)

    import soundfile as sf  # 官方依赖里已有 soundfile

    info = sf.info(out_path)
    _emit(
        {
            "ok": True,
            "out_path": out_path,
            "duration_s": round(info.frames / info.samplerate, 3),
            "sample_rate": info.samplerate,
            "elapsed_s": round(time.time() - t0, 2),
            "peak_rss_mib": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 / 1024, 0),
        }
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 - worker 边界，必须把错误原文回给上层
        _emit(
            {
                "ok": False,
                "error_type": type(exc).__name__,
                "error": str(exc),
                "traceback": traceback.format_exc()[-4000:],
            }
        )
        sys.exit(1)
