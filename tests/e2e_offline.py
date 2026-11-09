"""端到端离线验收：不接任何模型，从建项目一路跑到导出交付包。

用法：`.venv/bin/python3.12 tests/e2e_offline.py`
（默认工程建在 `/Volumes/other/ndw_runs/e2e_offline/proj_<时间戳>`，
可用环境变量 `NDW_E2E_DIR` 改根目录。）

链路：init → setting → outline → chapter → scene/line split → script suggest --apply
      → media sfx/image/video → tts batch（离线占位配音）→ timeline → concat
      → mix → mux → export bundle

每一步都走真实 CLI，只把「模型调用」换成离线实现；
全部产物落在工程目录内，脚本最后逐项检查文件是否存在且非空。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "tests" / "fixtures" / "e2e_offline.yaml"
BASE = Path(os.environ.get("NDW_E2E_DIR") or "/Volumes/other/ndw_runs/e2e_offline")

PROJECT = BASE / f"proj_{time.strftime('%Y%m%d_%H%M%S')}"
#: 运行时配置：由 fixture 复制并注入本机参考音频路径（仓库里不存绝对路径）
RUNTIME_CONFIG = BASE / "e2e_offline.runtime.yaml"
chapter_id = ""


def log(message: str) -> None:
    """打印带前缀的进度行，便于日志里按步骤定位。"""
    print(f"[e2e] {message}", flush=True)


def cli(*args: str) -> subprocess.CompletedProcess:
    """执行一次 CLI（统带 --project/--config/--json）。

    参数:
        args: 子命令及其参数。

    返回:
        CompletedProcess；退出码非 0 时由调用方决定是否断言失败。
    """
    command = [
        sys.executable,
        "-m",
        "novel_drama.cli",
        "--project",
        str(PROJECT),
        "--config",
        str(RUNTIME_CONFIG),
        "--json",
        *args,
    ]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src") + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return subprocess.run(command, cwd=str(ROOT), env=env, capture_output=True, text=True)


def run_json(*args: str) -> dict:
    """执行 CLI 并解析 JSON 输出；失败直接抛错（带 stderr 摘要）。

    参数:
        args: 子命令及其参数。

    返回:
        解析后的 JSON 对象（列表会被包成 {"items": [...]}）。
    """
    result = cli(*args)
    if result.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} 失败（exit={result.returncode}）：{result.stderr.strip() or result.stdout.strip()}")
    text = (result.stdout or "").strip()
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.strip() in ("{", "["):
            # `--json` 输出是缩进多行 JSON，从这一行起整体解析
            data = json.loads("\n".join(lines[index:]))
            return {"items": data} if isinstance(data, list) else data
    raise RuntimeError(f"{' '.join(args)} 没有 JSON 输出：{text[:300]}")


def require(path: Path, min_bytes: int = 1) -> None:
    """断言文件存在且不小于给定体积。

    参数:
        path: 被检查的文件。
        min_bytes: 最小体积（字节）。
    """
    if not path.exists():
        raise AssertionError(f"缺少产物：{path}")
    size = path.stat().st_size
    if size < min_bytes:
        raise AssertionError(f"产物过小：{path}（{size} < {min_bytes}）")
    log(f"OK  {path.relative_to(PROJECT)}  {size / 1024:.1f} KB")


def ensure_ref_audio() -> Path:
    """确保验收配置里的参考音频存在：生成 1 秒静音 WAV。

    返回:
        参考音频路径（与 `tests/fixtures/e2e_offline.yaml` 中一致）。
    """
    import wave

    path = BASE / "ref_narrator.wav"
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(48000)
        handle.writeframes(b"\x00\x00" * 48000)
    return path


def build_runtime_config() -> Path:
    """生成运行时配置：复制 fixture 并注入本机参考音频路径。

    仓库里的 fixture 不含任何本机绝对路径；每次运行按当前机器写到验收根目录。

    返回:
        运行时配置文件路径。
    """
    ref_audio = ensure_ref_audio()
    text = CONFIG.read_text(encoding="utf-8")
    inject = (
        "  # 以下由 tests/e2e_offline.py 运行时注入（不随仓库提交）\n"
        "  default_voice:\n"
        f"    ref_audio: {ref_audio}\n"
        "    ref_text: 离线验收占位参考音频\n"
    )
    marker = "  providers:\n"
    if marker not in text:
        raise RuntimeError("fixture 配置缺少 providers 段，无法注入参考音频")
    RUNTIME_CONFIG.write_text(text.replace(marker, inject + marker, 1), encoding="utf-8")
    return RUNTIME_CONFIG


def main() -> int:
    """跑完整条离线链路并校验产物，返回进程退出码。"""
    global chapter_id
    PROJECT.parent.mkdir(parents=True, exist_ok=True)
    build_runtime_config()

    log(f"工程目录 {PROJECT}")
    run_json("init", str(PROJECT), "--title", "离线验收", "--author", "ndw", "--genre", "悬疑")

    log("设定库")
    run_json(
        "setting",
        "set",
        "--logline",
        "一名电台主播收到自己在未来发出的求救电话。",
        "--theme",
        "记忆与自我欺骗",
        "--tone",
        "冷峻",
    )
    run_json(
        "setting",
        "world",
        "--add-rule",
        "凌晨三点后不能接听陌生来电",
        "--add-location",
        "旧电台|城北废弃广播楼|潮湿压抑",
        "--add-glossary",
        "回响|能听见过去声音的异常现象",
    )

    log("大纲")
    outline = run_json("outline", "generate", "--acts", "1", "--chapters-per-act", "1", "--beats-per-chapter", "2", "--words", "600")
    log(f"生成 {outline['chapters']} 章 / {outline['beats']} 个节拍（provider={outline['provider']}）")
    applied = run_json("outline", "apply")
    if not applied["created"]:
        raise AssertionError(f"大纲没有落地出章节：{applied}")
    chapter_id = applied["created"][0]
    log(f"章节 {chapter_id}")

    log("章节正文")
    generated = run_json("chapter", "generate", "--chapter", chapter_id)
    log(f"{generated['words']} 字（provider={generated['provider']}）")

    log("场景 / 台词切分")
    scenes = run_json("scene", "split", "--chapter", chapter_id)
    lines = run_json("line", "split", "--chapter", chapter_id)
    log(f"场景 {len(scenes['scenes'])} 个，台词 {lines['lines']} 行")
    if not lines["lines"]:
        raise AssertionError("台词切分结果为空")

    log("导演标注（并应用）")
    script = run_json("script", "suggest", "--chapter", chapter_id, "--apply")
    log(f"镜头 {script['shots']} 个，告警 {len(script['warnings'])} 条")

    log("音效 / 配图 / 视频位")
    for kind in ("sfx", "image", "video"):
        result = run_json(kind, "run", "--chapter", chapter_id)
        log(f"{kind}: 就绪 {result['ready']} / 跳过 {result['skipped']} / 失败 {result['failed']}")
        if result["failed"]:
            raise AssertionError(f"{kind} 有失败项：{result['items']}")

    log("配音（离线占位 provider）")
    audio = run_json("tts", "batch", "--chapter", chapter_id)
    log(f"配音：成功 {audio['stats']['succeeded']} / 失败 {audio['stats']['failed']} / 跳过 {audio['stats']['skipped']}")
    if audio["stats"]["failed"]:
        raise AssertionError(f"配音失败：{audio['stats']}")

    log("时间轴 / 拼接 / 混音")
    run_json("timeline", "build", "--chapter", chapter_id)
    run_json("concat", "--chapter", chapter_id)
    run_json("mix", "--chapter", chapter_id, "--render")

    log("混流")
    mux = run_json("mux", "--chapter", chapter_id, "--render")
    log(f"成片 {mux['path']}（{mux['duration_ms']} ms）")

    log("导出交付包")
    bundle = run_json("export", "--chapter", chapter_id, "--format", "bundle")
    log(f"交付包 {bundle['bundle']}")

    log("产物校验")
    renders = PROJECT / "renders"
    exports = PROJECT / "exports"
    require(renders / f"{chapter_id}.mp4", 10_000)
    require(exports / f"{chapter_id}.mp3", 2_000)
    require(exports / f"{chapter_id}.mp4", 10_000)
    require(exports / f"{chapter_id}.srt", 20)
    require(exports / f"{chapter_id}.bundle.json", 100)
    require(exports / "README.txt", 20)
    log(f"完成：{PROJECT}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 - 验收脚本需要把失败原因原样打出来
        log(f"FAIL {type(exc).__name__}: {exc}")
        sys.exit(1)
