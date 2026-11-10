"""内置参考音色：清单、按需下载、导入工程音色库。

来源是 IndexTTS 官方 Demo Space 的示例音频（voice_01~voice_12，voice_10 缺失）。
这些音频只作为本地创作的起步音色，不随仓库分发，默认下载到外置盘：
`tts.builtin_voices.root`（缺省 `/Volumes/other/models/voice_refs/builtin`）。

授权说明：示例音频随 IndexTTS 模型许可提供；对外发布的成品建议替换为
自有录音或已获书面授权的素材，`consent.note` 会原样写入音色库，便于追溯。
"""

from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path

from ..models import entities
from ..models.ids import now_iso
from .model_store import dir_size

#: 默认下载目录（外置盘；目录不存在时首次安装自动创建）
DEFAULT_ROOT = "/Volumes/other/models/voice_refs/builtin"

#: 默认下载源（国内镜像优先）
DEFAULT_SOURCE = "https://hf-mirror.com/spaces/IndexTeam/IndexTTS-2-Demo/resolve/main/examples"

#: 下载清单文件名
MANIFEST_NAME = ".ndw-voices.json"

#: 音色库里的授权说明
CONSENT_NOTE = "IndexTTS 官方 Demo 示例音频（随模型许可）；对外商用请替换为自有授权录音"

#: 内置音色清单。
#: f0_hz 为实测基频中位数（仅用于大致区分男女声与音区，不是声学标注）；
#: duration_ms 为实测时长，安装后会再用 ffprobe 校正。
CATALOG: list[dict] = [
    {
        "key": "voice_12",
        "file": "voice_12.wav",
        "name": "内置男声·低沉",
        "gender": "male",
        "f0_hz": 107,
        "duration_ms": 2673,
    },
    {
        "key": "voice_05",
        "file": "voice_05.wav",
        "name": "内置男声·中音",
        "gender": "male",
        "f0_hz": 147,
        "duration_ms": 8406,
    },
    {
        "key": "voice_07",
        "file": "voice_07.wav",
        "name": "内置女声·中低 A",
        "gender": "female",
        "f0_hz": 170,
        "duration_ms": 2036,
    },
    {
        "key": "voice_06",
        "file": "voice_06.wav",
        "name": "内置女声·中低 B",
        "gender": "female",
        "f0_hz": 176,
        "duration_ms": 6229,
    },
    {
        "key": "voice_09",
        "file": "voice_09.wav",
        "name": "内置女声·中音 A",
        "gender": "female",
        "f0_hz": 219,
        "duration_ms": 10194,
    },
    {
        "key": "voice_04",
        "file": "voice_04.wav",
        "name": "内置女声·中音 B",
        "gender": "female",
        "f0_hz": 222,
        "duration_ms": 2346,
    },
    {
        "key": "voice_11",
        "file": "voice_11.wav",
        "name": "内置女声·中高 A",
        "gender": "female",
        "f0_hz": 258,
        "duration_ms": 7862,
    },
    {
        "key": "voice_02",
        "file": "voice_02.wav",
        "name": "内置女声·中高 B",
        "gender": "female",
        "f0_hz": 262,
        "duration_ms": 2927,
    },
    {
        "key": "voice_01",
        "file": "voice_01.wav",
        "name": "内置女声·高音 A",
        "gender": "female",
        "f0_hz": 296,
        "duration_ms": 2439,
    },
    {
        "key": "voice_03",
        "file": "voice_03.wav",
        "name": "内置女声·高音 B",
        "gender": "female",
        "f0_hz": 308,
        "duration_ms": 2111,
    },
]


def builtin_root(config) -> Path:
    """返回内置音色下载目录。

    参数:
        config: AppConfig（读 `tts.builtin_voices.root`）。

    返回:
        目录路径；相对路径按仓库根解析。
    """
    from ..config import REPO_ROOT

    raw = config.get("tts.builtin_voices.root") if config is not None else None
    path = Path(str(raw or DEFAULT_ROOT)).expanduser()
    if not path.is_absolute():
        path = REPO_ROOT / path
    return path


def source_base(config) -> str:
    """返回下载源基础地址（末尾不带斜杠）。

    参数:
        config: AppConfig（读 `tts.builtin_voices.source`）。

    返回:
        URL 字符串。
    """
    raw = config.get("tts.builtin_voices.source") if config is not None else None
    return str(raw or DEFAULT_SOURCE).rstrip("/")


def catalog_entry(key: str) -> dict | None:
    """按目录键查内置音色。

    参数:
        key: 清单键（如 `voice_05`）。

    返回:
        清单项或 None。
    """
    for item in CATALOG:
        if item["key"] == key:
            return item
    return None


def rows(config) -> list[dict]:
    """列出内置音色及本地就位状态。

    参数:
        config: AppConfig。

    返回:
        每项含 key/name/gender/f0_hz/duration_ms/path/present/bytes。
    """
    root = builtin_root(config)
    result = []
    for item in CATALOG:
        path = root / item["file"]
        present = path.is_file() and path.stat().st_size > 1000
        result.append(
            {
                "key": item["key"],
                "name": item["name"],
                "gender": item["gender"],
                "f0_hz": item["f0_hz"],
                "duration_ms": item["duration_ms"],
                "path": str(path),
                "present": present,
                "bytes": path.stat().st_size if present else 0,
            }
        )
    return result


def audio_path(config, key: str) -> Path | None:
    """返回某个内置音色的本地文件路径（未下载或不存在时返回 None）。

    参数:
        config: AppConfig。
        key: 清单键。

    返回:
        绝对路径或 None。
    """
    entry = catalog_entry(key)
    if entry is None:
        return None
    path = builtin_root(config) / entry["file"]
    if path.is_file() and path.stat().st_size > 1000:
        return path
    return None


def _download(url: str, target: Path) -> None:
    """下载单个文件到目标路径（先写临时文件再原子替换）。

    参数:
        url: 下载地址。
        target: 目标文件。

    异常:
        RuntimeError: HTTP 失败、内容过小或写入失败。
    """
    request = urllib.request.Request(url, headers={"User-Agent": "novel-drama-workbench"})
    with urllib.request.urlopen(request, timeout=120) as response:
        if response.status < 200 or response.status >= 300:
            raise RuntimeError(f"HTTP {response.status}: {url}")
        data = response.read()
    if len(data) < 1000:
        raise RuntimeError(f"下载内容过小（{len(data)} 字节）：{url}")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, target)


def _normalize(src: Path, target: Path) -> None:
    """把下载到的音频统一转成 24k 单声道 PCM16，便于浏览器试听与克隆。

    参数:
        src: 原始文件。
        target: 目标 wav。

    异常:
        Exception: ffmpeg 缺失或转码失败时抛出，由调用方回退。
    """
    from ..media.ffmpeg import run

    run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", str(target)])
    src.unlink(missing_ok=True)


def _probe_duration_ms(path: Path) -> int:
    """用 ffprobe 读取时长；失败时返回 0（由调用方回退到清单值）。

    参数:
        path: 音频文件。

    返回:
        毫秒数或 0。
    """
    try:
        from ..media.ffmpeg import probe_duration_ms

        return probe_duration_ms(path)
    except Exception:
        return 0


def write_manifest(root: Path, source: str = DEFAULT_SOURCE) -> dict:
    """把已下载文件与来源写入清单，便于排查与增量核对。

    参数:
        root: 下载目录。
        source: 实际使用的下载源。

    返回:
        清单 dict。
    """
    files = []
    for item in CATALOG:
        path = root / item["file"]
        if path.is_file():
            files.append(
                {
                    "key": item["key"],
                    "file": item["file"],
                    "bytes": path.stat().st_size,
                    "duration_ms": item["duration_ms"],
                }
            )
    manifest = {"downloaded_at": now_iso(), "source": source, "files": files}
    (root / MANIFEST_NAME).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def install(config, keys: list[str] | None = None, force: bool = False) -> dict:
    """按需下载内置音色到外置盘。

    参数:
        config: AppConfig。
        keys: 只安装这些键；None 表示全部。
        force: 已存在也重新下载。

    返回:
        `{"ok": bool, "root": str, "installed": [...], "skipped": [...], "failed": [...]}`。
    """
    root = builtin_root(config)
    root.mkdir(parents=True, exist_ok=True)
    base = source_base(config)
    wanted = [item for item in CATALOG if keys is None or item["key"] in keys]
    installed, skipped, failed = [], [], []
    for item in wanted:
        target = root / item["file"]
        if target.is_file() and target.stat().st_size > 1000 and not force:
            skipped.append(item["key"])
            continue
        try:
            src = target.with_name(target.stem + ".src" + target.suffix)
            _download(f"{base}/{item['file']}", src)
            try:
                _normalize(src, target)
            except Exception:
                # ffmpeg 不可用时保留原始文件，至少保证可克隆
                os.replace(src, target)
        except Exception as exc:
            failed.append({"key": item["key"], "error": str(exc)})
            continue
        actual = _probe_duration_ms(target) or item["duration_ms"]
        item["duration_ms"] = int(actual)
        installed.append({"key": item["key"], "path": str(target), "bytes": target.stat().st_size})
    write_manifest(root, base)
    return {
        "ok": not failed,
        "root": str(root),
        "installed": installed,
        "skipped": skipped,
        "failed": failed,
        "bytes": dir_size(root),
    }


def import_into_project(store, config, keys: list[str] | None = None) -> dict:
    """把内置音色导入工程 `voices.json`（按名称去重，可重复执行）。

    参数:
        store: ProjectStore。
        config: AppConfig。
        keys: 只导入这些键；None 表示全部已下载项。

    返回:
        `{"installed": {...}, "added": [...], "skipped": [...], "voices": [...]}`。
    """
    install_result = install(config, keys=keys)
    table = store.load_voices()
    existing = {voice.get("name") for voice in table.get("voices") or []}
    added, skipped = [], []
    for item in CATALOG:
        if keys is not None and item["key"] not in keys:
            continue
        path = audio_path(config, item["key"])
        if path is None:
            skipped.append({"key": item["key"], "reason": "未下载"})
            continue
        if item["name"] in existing:
            skipped.append({"key": item["key"], "reason": "已存在"})
            continue
        duration = item["duration_ms"]
        variant_id = entities.new_id("vav")
        voice = {
            "id": entities.new_id("voc"),
            "name": item["name"],
            "engine": None,
            "default_provider_id": None,
            "language": "zh-CN",
            "gender": item["gender"],
            "tags": ["内置", "官方示例", "男声" if item["gender"] == "male" else "女声"],
            "default_variant_id": variant_id,
            "variants": [
                {
                    "id": variant_id,
                    "name": "默认",
                    "source": "cloned",
                    "emotion_tags": ["default"],
                    "reference": {
                        "audio_path": str(path),
                        "duration_ms": int(duration),
                        "text": None,
                    },
                    "consent": {"confirmed": True, "note": CONSENT_NOTE},
                    "provider_availability": [
                        {
                            "provider_id": "indextts_25",
                            "status": "ready",
                            "clone_supported": True,
                            "checked_at": now_iso(),
                        }
                    ],
                    "status": "ready",
                    "created_at": now_iso(),
                }
            ],
            "status": "ready",
            "revision": 1,
            "created_at": now_iso(),
            "updated_at": now_iso(),
        }
        table["voices"].append(voice)
        added.append({"key": item["key"], "voice_id": voice["id"], "name": item["name"]})
        existing.add(item["name"])
    if added:
        store.save_voices(table)
    return {
        "installed": install_result,
        "added": added,
        "skipped": skipped,
        "voices": table["voices"],
    }
