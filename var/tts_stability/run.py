"""TTS 语速稳定性实测：同一批句子 × 多次生成 × 两种切分粒度，统计时长离散度。

用法（在仓库根目录）：
    PYTHONPATH=src .venv/bin/python var/tts_stability/run.py \
        --config config/config.yaml \
        --out-dir /Volumes/other/ndw_stability \
        --repeats 3 --limits 80,24

产物：
    <out-dir>/stability_<时间戳>.json  完整数据（每句每次的时长、块数、耗时）
    <out-dir>/audio/                   每次生成的 wav（便于人耳复核）

约束：单并发、逐次生成、不并发、不跑图像/视频模型。
"""

from __future__ import annotations

import argparse
import json
import resource
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

from novel_drama.config import load_config
from novel_drama.tts import TtsService
from novel_drama.tts.service import split_text

# 覆盖「极短 / 用户实测 13 字 / 中等 / 较长」四档，全部带标点
SENTENCES = [
    "别动。",
    "你到底想要我怎么做才满意？",
    "她说这句话的时候，声音很轻，像是怕惊动了什么。",
    "收到好友从远方寄来的生日礼物，那份意外的惊喜让我开心得不得了。",
]


def peak_rss_mb() -> float:
    """当前进程峰值内存（MB，macOS 的 ru_maxrss 为字节）。"""
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 / 1024, 1)


def summarize(durations: list[int]) -> dict:
    """统计一组时长的离散程度。

    参数:
        durations: 同一句同一粒度下多次生成的时长（毫秒）。

    返回:
        {min, max, mean, stdev, cv, range_ratio, spread_ms}。
    """
    if not durations:
        return {}
    mean = statistics.fmean(durations)
    stdev = statistics.pstdev(durations) if len(durations) > 1 else 0.0
    return {
        "min_ms": min(durations),
        "max_ms": max(durations),
        "mean_ms": round(mean, 1),
        "stdev_ms": round(stdev, 1),
        "cv": round(stdev / mean, 4) if mean else None,
        "range_ratio": round(max(durations) / min(durations), 3) if min(durations) else None,
        "spread_ms": max(durations) - min(durations),
    }


def main() -> int:
    """执行实测并输出 JSON + 终端表格。"""
    parser = argparse.ArgumentParser(description="TTS 语速稳定性实测")
    parser.add_argument("--config", default="config/config.yaml")
    parser.add_argument("--out-dir", default="/Volumes/other/ndw_stability")
    parser.add_argument("--repeats", type=int, default=3, help="每句每种粒度生成次数")
    parser.add_argument("--limits", default="80,24", help="逗号分隔的切分上限（字）")
    parser.add_argument("--ref-audio", help="覆盖参考音频路径")
    parser.add_argument("--ref-text", help="覆盖参考音频对应文本")
    args = parser.parse_args()

    config = load_config(args.config)
    service = TtsService(config)  # 不挂工程，避免污染工程历史
    limits = [max(4, int(item)) for item in args.limits.split(",") if item.strip()]

    ref = service.default_voice
    if args.ref_audio:
        from novel_drama.tts.types import VoiceRef

        ref = VoiceRef(ref_audio=args.ref_audio, ref_text=args.ref_text or ref.ref_text, source="preset")

    out_dir = Path(args.out_dir)
    audio_dir = out_dir / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    started = time.perf_counter()
    results = []
    for sentence in SENTENCES:
        for limit in limits:
            run_label = f"{stamp}_L{limit}_{abs(hash(sentence)) % 10**6:06d}"
            runs = []
            for repeat in range(max(1, args.repeats)):
                out_path = audio_dir / f"{run_label}_{repeat + 1}.wav"
                payload = service.synthesize(sentence, out_path, voice=ref, chunk_limit=limit)
                runs.append(
                    {
                        "repeat": repeat + 1,
                        "duration_ms": payload["duration_ms"],
                        "chunks": payload["chunks"],
                        "latency_ms": payload["latency_ms"],
                        "sample_rate": payload["sample_rate"],
                    }
                )
                print(
                    f"[{limit:>2}字] {sentence[:12]:<12} #{repeat + 1} "
                    f"时长 {payload['duration_ms']}ms 块 {payload['chunks']} 耗时 {payload['latency_ms']}ms",
                    flush=True,
                )
            durations = [item["duration_ms"] for item in runs]
            results.append(
                {
                    "sentence": sentence,
                    "chars": len(sentence),
                    "limit": limit,
                    "planned_chunks": len(split_text(sentence, limit)),
                    "runs": runs,
                    "stats": summarize(durations),
                }
            )

    by_limit = {}
    for limit in limits:
        rows = [item for item in results if item["limit"] == limit]
        cvs = [item["stats"]["cv"] for item in rows if item["stats"].get("cv") is not None]
        ratios = [item["stats"]["range_ratio"] for item in rows if item["stats"].get("range_ratio")]
        by_limit[str(limit)] = {
            "sentences": len(rows),
            "mean_cv": round(statistics.fmean(cvs), 4) if cvs else None,
            "max_range_ratio": max(ratios) if ratios else None,
        }

    report = {
        "created_at": stamp,
        "config": args.config,
        "ref_audio": ref.ref_audio,
        "repeats": args.repeats,
        "limits": limits,
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
        "peak_rss_mb": peak_rss_mb(),
        "summary": {"by_limit": by_limit},
        "results": results,
    }
    json_path = out_dir / f"stability_{stamp}.json"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print("\n粒度  平均CV    最大极差比")
    for limit in limits:
        item = by_limit[str(limit)]
        print(f"{limit:>4}  {item['mean_cv']}  {item['max_range_ratio']}")
    print(f"\n总耗时 {report['elapsed_ms'] / 1000:.1f}s  峰值内存 {report['peak_rss_mb']}MB")
    print(f"报告 {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
