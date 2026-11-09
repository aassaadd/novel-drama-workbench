"""模型仓库：清单、路径解析、校验与导入。

设计依据（用户要求）：
- 模型权重只放外置盘，路径、清单、下载、校验全部由本项目自己管；
- provider 只从 `paths.models_root`（默认 `/Volumes/other/models/ndw`）解析权重，
  不再扫描全局 HuggingFace 缓存，避免「文件在别处也能跑」的隐性依赖；
- 模型缺失必须报明确错误，不允许静默降级成 placeholder；
- 许可不合规的模型直接拦在下载/启用之前，不靠使用者自觉。
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime
from pathlib import Path

#: 默认模型根目录（外置盘，绝不落系统盘）
DEFAULT_MODELS_ROOT = "/Volumes/other/models/ndw"

#: 下载统一走镜像（直连 HF 在这台机器上不可达）
HF_MIRROR = "https://hf-mirror.com"

#: 单项超过这个体积必须先报体积、等人工确认（字节）
BIG_MODEL_BYTES = 15 * 1024**3

#: 判定「模型就位」的最小文件集（权重 + 结构配置）；有下载清单时以清单为准
REQUIRED_FILES = ("config.json", "model.safetensors")

#: 下载时写入的文件清单，verify 按它逐文件核对（适配多子模型仓库）
MANIFEST_NAME = ".ndw-model.json"

#: 许可门禁取值
GATE_NONE = "none"  # 许可干净，可直接下载/启用
GATE_APPROVAL = "approval_required"  # 需取得授权后才能商用，默认拦下载
GATE_BLOCK = "block"  # 明确禁止商用（或非商用），默认拦下载
GATE_RETIRED = "retired"  # 已退役，不再提供

#: 已知模型清单。`key` 是 models_root 下的相对目录，也是 provider 的唯一解析依据。
CATALOG: list[dict] = [
    {
        "key": "cosyvoice3/8bit",
        "provider_id": "cosyvoice3_mlx",
        "display_name": "Fun-CosyVoice3-0.5B (MLX 8bit)",
        "hf_repo": "FunAudioLLM/Fun-CosyVoice3-0.5B-2512",
        "license": "Apache-2.0",
        "commercial_use": True,
        "license_gate": GATE_NONE,
        "note": "当前默认 provider；输出 24kHz 单声道，零样本克隆需参考音频",
    },
    {
        "key": "cosyvoice3/fp16",
        "provider_id": "cosyvoice3_mlx",
        "display_name": "Fun-CosyVoice3-0.5B (MLX fp16)",
        "hf_repo": "FunAudioLLM/Fun-CosyVoice3-0.5B-2512",
        "license": "Apache-2.0",
        "commercial_use": True,
        "license_gate": GATE_NONE,
        "note": "精度更高、显存占用更大；A/B 用",
    },
    {
        "key": "cosyvoice3/4bit",
        "provider_id": "cosyvoice3_mlx",
        "display_name": "Fun-CosyVoice3-0.5B (MLX 4bit)",
        "hf_repo": "FunAudioLLM/Fun-CosyVoice3-0.5B-2512",
        "license": "Apache-2.0",
        "commercial_use": True,
        "license_gate": GATE_NONE,
        "note": "显存最省；音质略降，适合批量粗跑",
    },
    {
        "key": "step_audio_editx",
        "provider_id": "step_audio_editx",
        "display_name": "Step-Audio-EditX",
        "hf_repo": "stepfun-ai/Step-Audio-EditX",
        "license": "Apache-2.0",
        "commercial_use": True,
        "license_gate": GATE_NONE,
        "note": "未接入；带原生副语言控制（笑/叹）",
    },
    {
        "key": "glm_tts",
        "provider_id": "glm_tts",
        "display_name": "GLM-TTS",
        "hf_repo": "zai-org/GLM-TTS",
        "license": "Apache-2.0",
        "commercial_use": True,
        "license_gate": GATE_NONE,
        "note": "未接入",
    },
    {
        "key": "qwen3_tts",
        "provider_id": "qwen3_tts",
        "display_name": "Qwen3-TTS",
        "hf_repo": "Qwen/Qwen3-TTS-12Hz-1.7B-Base",
        "license": "Apache-2.0",
        "commercial_use": True,
        "license_gate": GATE_NONE,
        "note": "未接入（用户暂停安装）；repo id 已按镜像元数据核实，同族另有 0.6B-Base / 1.7B-CustomVoice / VoiceDesign / Tokenizer-12Hz",
    },
    {
        "key": "gpt_sovits",
        "provider_id": "gpt_sovits",
        "display_name": "GPT-SoVITS",
        "hf_repo": "lj1995/GPT-SoVITS",
        "license": "MIT",
        "commercial_use": True,
        "license_gate": GATE_NONE,
        "note": "未接入；少量参考音频即可克隆",
    },
    {
        "key": "indextts_25",
        "provider_id": "indextts_25",
        "display_name": "IndexTTS 2.5",
        "hf_repo": "IndexTeam/IndexTTS-2.5",
        "license": "bilibili-model-license",
        "commercial_use": True,
        "license_gate": GATE_NONE,
        "note": (
            "允许商用且免授权费；需保留版权声明与协议副本、不得用于改进其他商用 AI 模型、"
            "分发衍生作品需附免责声明；月活>1亿或年收入>10亿元需单独授权；禁高风险场景"
        ),
    },
    {
        "key": "fish_s1_mini",
        "provider_id": "fish_s1_mini",
        "display_name": "Fish S1-mini",
        "hf_repo": "fishaudio/openaudio-s1-mini",
        "license": "CC-BY-NC-SA-4.0",
        "commercial_use": False,
        "license_gate": GATE_BLOCK,
        "note": "非商用（NC）；对外发布内容禁止使用",
    },
    {
        "key": "fish_s2_pro",
        "provider_id": "fish_s2_pro",
        "display_name": "Fish S2 Pro",
        "hf_repo": "fishaudio/fish-speech-1.5",
        "license": "proprietary",
        "commercial_use": False,
        "license_gate": GATE_APPROVAL,
        "note": "商用需单独授权",
    },
    {
        "key": "f5_tts",
        "provider_id": "f5_tts",
        "display_name": "F5-TTS",
        "hf_repo": "SWivid/F5-TTS",
        "license": "CC-BY-NC-4.0",
        "commercial_use": False,
        "license_gate": GATE_BLOCK,
        "note": "非商用许可",
    },
    {
        "key": "spark_tts",
        "provider_id": "spark_tts",
        "display_name": "Spark-TTS",
        "hf_repo": "SparkAudio/Spark-TTS-0.5B",
        "license": "CC-BY-NC-SA-4.0",
        "commercial_use": False,
        "license_gate": GATE_BLOCK,
        "note": "非商用许可",
    },
    {
        "key": "higgs_audio",
        "provider_id": "higgs_audio",
        "display_name": "Higgs Audio",
        "hf_repo": "boson-ai/higgs-audio-v2",
        "license": "proprietary",
        "commercial_use": False,
        "license_gate": GATE_APPROVAL,
        "note": "商用需授权",
    },
    {
        "key": "chatterbox",
        "provider_id": "chatterbox",
        "display_name": "Chatterbox",
        "hf_repo": "ResembleAI/chatterbox",
        "license": "retired",
        "commercial_use": False,
        "license_gate": GATE_RETIRED,
        "note": "已退役，不再提供下载",
    },
]


def _now() -> str:
    """当前本地时间（用于暂存目录命名）。"""
    return datetime.now().strftime("%Y%m%dT%H%M%S")


def models_root(config) -> Path:
    """返回模型根目录。

    参数:
        config: AppConfig（读 `paths.models_root`）。

    返回:
        绝对路径；配置为相对路径时按仓库根解析。
    """
    from ..config import REPO_ROOT

    raw = config.get("paths.models_root") or DEFAULT_MODELS_ROOT
    path = Path(str(raw)).expanduser()
    if not path.is_absolute():
        path = REPO_ROOT / path
    return path


def catalog_entry(key: str) -> dict | None:
    """按目录键查清单项。

    参数:
        key: 相对 models_root 的目录（如 `cosyvoice3/8bit`）。

    返回:
    清单项或 None。
    """
    for item in CATALOG:
        if item["key"] == key:
            return item
    return None


def default_key_for(provider_id: str) -> str | None:
    """给定 provider 的默认权重目录键（清单里第一条非退役项）。

    参数:
        provider_id: provider 标识。

    返回:
        目录键或 None（清单里没有这个 provider）。
    """
    for item in CATALOG:
        if item["provider_id"] == provider_id and item["license_gate"] != GATE_RETIRED:
            return item["key"]
    return None


def resolve_weights(config, provider_id: str, raw: str | None) -> str | None:
    """把配置里的 weights_path 解析成 models_root 下的绝对路径。

    规则：
    - 相对路径 → 拼到 models_root 下；
    - 绝对路径必须位于 models_root 内，否则报错（不允许 provider 从任意位置加载）；
    - 未配置 → 用清单里该 provider 的默认目录。

    参数:
        config: AppConfig。
        provider_id: provider 标识。
        raw: 配置里的 weights_path 原值。

    返回:
        绝对路径字符串；该 provider 在清单里没有默认目录且未配置时返回 None。

    异常:
        ValueError: 配置的路径在 models_root 之外。
    """
    root = models_root(config)
    if not raw:
        key = default_key_for(provider_id)
        return str(root / key) if key else None
    path = Path(str(raw)).expanduser()
    if not path.is_absolute():
        return str(root / path)
    resolved = path.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(
            f"provider {provider_id} 的 weights_path 必须在模型根目录内：{resolved} 不在 {root} 下。"
            f"请把权重移入 {root}（可用 `ndw models import`），或改 config 的 paths.models_root"
        ) from exc
    return str(resolved)


def dir_size(path: Path) -> int:
    """统计目录内所有文件字节数。

    参数:
        path: 目录。

    返回:
        总字节数；目录不存在时为 0。
    """
    total = 0
    if not path.exists():
        return 0
    for file in path.rglob("*"):
        if file.is_file():
            try:
                total += file.stat().st_size
            except OSError:
                continue
    return total


def read_manifest(path: Path) -> dict | None:
    """读取下载清单（`.ndw-model.json`）。

    参数:
        path: 权重目录。

    返回:
        清单 dict；不存在或损坏时返回 None（回落到默认必需文件）。
    """
    file = path / MANIFEST_NAME
    if not file.exists():
        return None
    try:
        return json.loads(file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def write_manifest(path: Path, key: str, repo: str) -> dict:
    """把下载到的文件与大小写成清单，供 verify 逐文件核对。

    参数:
        path: 已下载完成的目录。
        key: 目录键。
        repo: 来源仓库。

    返回:
        写入的清单 dict。
    """
    files = []
    for file in sorted(path.rglob("*")):
        if not file.is_file() or file.name == MANIFEST_NAME:
            continue
        rel = file.relative_to(path).as_posix()
        if rel.startswith(".cache/"):
            continue
        files.append({"name": rel, "bytes": file.stat().st_size})
    manifest = {"key": key, "repo": repo, "downloaded_at": _now(), "files": files}
    (path / MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


def required_missing(path: Path) -> list[str]:
    """列出缺失/不完整的必需文件。

    有下载清单时按清单逐文件核对（存在 + 大小一致），否则只查 `REQUIRED_FILES`。

    参数:
        path: 权重目录。

    返回:
        问题列表（缺失或大小不符）；空列表表示齐全。
    """
    manifest = read_manifest(path)
    if manifest:
        problems = []
        for item in manifest.get("files") or []:
            file = path / item["name"]
            if not file.exists():
                problems.append(item["name"])
                continue
            expected = int(item.get("bytes") or 0)
            if expected and file.stat().st_size != expected:
                problems.append(f"{item['name']}（大小不符：{file.stat().st_size}/{expected}）")
        return problems
    missing = []
    for name in REQUIRED_FILES:
        file = path / name
        if not file.exists() or file.stat().st_size <= 0:
            missing.append(name)
    return missing


def scan(config) -> list[dict]:
    """扫描模型根目录，生成清单视图（含未下载项）。

    参数:
        config: AppConfig。

    返回:
        每个清单项一行：key/provider_id/path/present/bytes/complete/missing/license 等。
    """
    root = models_root(config)
    rows = []
    for item in CATALOG:
        path = root / item["key"]
        present = path.is_dir()
        missing = required_missing(path) if present else list(REQUIRED_FILES)
        rows.append(
            {
                "key": item["key"],
                "provider_id": item["provider_id"],
                "display_name": item["display_name"],
                "path": str(path),
                "present": present,
                "complete": present and not missing,
                "bytes": dir_size(path) if present else 0,
                "missing": missing,
                "license": item["license"],
                "commercial_use": item["commercial_use"],
                "license_gate": item["license_gate"],
                "note": item.get("note"),
            }
        )
    return rows


def verify(config, keys: list[str] | None = None, load: bool = False) -> dict:
    """校验模型文件完整（可选：真加载一次）。

    参数:
        config: AppConfig。
        keys: 只验这些目录键；None 表示验全部已就位项。
        load: 是否调用 mlx 真加载（耗时约 1~2 分钟/个）。

    返回:
        `{"ok": bool, "items": [...]}`，每项含 key/path/present/complete/missing/bytes/load。
    """
    root = models_root(config)
    rows = scan(config)
    if keys:
        rows = [row for row in rows if row["key"] in keys]
    items = []
    for row in rows:
        entry = {
            "key": row["key"],
            "path": row["path"],
            "present": row["present"],
            "complete": row["complete"],
            "missing": row["missing"],
            "bytes": row["bytes"],
            "load": None,
        }
        if load and row["complete"]:
            entry["load"] = _load_check(Path(row["path"]))
        items.append(entry)
    ok = all(item["present"] and item["complete"] and (item["load"] in (None, True)) for item in items)
    return {"ok": ok, "root": str(root), "items": items}


def _load_check(path: Path) -> dict:
    """真加载一次权重，确认不是「文件在但读不了」。

    参数:
        path: 权重目录。

    返回:
        `{"ok": bool, "detail": str, "seconds": float}`。
    """
    import time

    started = time.perf_counter()
    try:
        from mlx_audio.tts.utils import load_model
    except Exception as exc:  # 依赖未装
        return {"ok": False, "detail": f"mlx-audio-plus 未安装：{exc}", "seconds": 0.0}
    try:
        load_model(path)
        detail = "加载成功"
        ok = True
    except Exception as exc:
        detail = f"加载失败：{exc}"
        ok = False
    return {"ok": ok, "detail": detail, "seconds": round(time.perf_counter() - started, 1)}


def import_dir(config, src: str, key: str, keep_source: bool = False) -> dict:
    """把已有权重目录纳入模型根目录（不重下）。

    同卷直接 `rename`（原子、不占额外空间）；跨卷先复制到 models_root 下的暂存目录，
    逐文件核对大小后 `rename` 就位，再删除源目录——保证任何时刻只有一份完整副本。

    参数:
        config: AppConfig。
        src: 源目录。
        key: 目标目录键（相对 models_root，如 `cosyvoice3/8bit`）。
        keep_source: 跨卷迁移后是否保留源目录（默认删）。

    返回:
        `{"key","src","target","bytes","same_volume","source_removed"}`。

    异常:
        ValueError: 源目录不合法、目标已存在或复制后大小不一致。
    """
    source = Path(src).expanduser().resolve()
    if not source.is_dir():
        raise ValueError(f"源目录不存在：{source}")
    missing = required_missing(source)
    if missing:
        raise ValueError(f"源目录不完整，缺少：{', '.join(missing)}（{source}）")
    root = models_root(config)
    root.mkdir(parents=True, exist_ok=True)
    target = root / key
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and any(target.iterdir()):
        raise ValueError(f"目标已存在且非空：{target}")

    size = dir_size(source)
    same_volume = _same_volume(source, root)
    if same_volume:
        source.rename(target)
        source_removed = True
    else:
        staging = root / f".staging-{key.replace('/', '_')}-{_now()}"
        shutil.copytree(source, staging)
        if dir_size(staging) != size:
            shutil.rmtree(staging, ignore_errors=True)
            raise ValueError(f"复制后体积不一致（源 {size} B），已清除暂存目录 {staging}")
        staging.rename(target)
        source_removed = False
        if not keep_source:
            shutil.rmtree(source)
            source_removed = True
    return {
        "key": key,
        "src": str(source),
        "target": str(target),
        "bytes": dir_size(target),
        "same_volume": same_volume,
        "source_removed": source_removed,
    }


def _same_volume(a: Path, b: Path) -> bool:
    """判断两个路径是否在同一卷（同卷 rename 才是原子且不占空间）。

    参数:
        a: 路径一。
        b: 路径二。

    返回:
        True 表示同卷。
    """
    try:
        return os.stat(a).st_dev == os.stat(b).st_dev
    except OSError:
        return False


def _hub_api():
    """返回指向镜像的 HfApi（只走 hf-mirror，不走直连）。

    返回:
        `huggingface_hub.HfApi` 实例。
    """
    os.environ["HF_ENDPOINT"] = HF_MIRROR
    from huggingface_hub import HfApi

    return HfApi(endpoint=HF_MIRROR)


def remote_size(key: str) -> dict:
    """查询清单项在镜像上的体积（下载前必须先报体积）。

    参数:
        key: 目录键。

    返回:
        `{"ok","repo","total_bytes","unknown","files":[{"name","bytes"}]}`；
        仓库不存在或网络失败时 `ok=False` 并带 `error`。

    异常:
        ValueError: 目录键不在清单里。
    """
    entry = catalog_entry(key)
    if entry is None:
        raise ValueError(f"未知模型目录键：{key}。用 `ndw models list` 查看可选键")
    repo = entry.get("hf_repo")
    try:
        info = _hub_api().model_info(repo, files_metadata=True)
    except Exception as exc:
        return {"ok": False, "repo": repo, "error": f"无法获取仓库信息：{exc}"}
    files = []
    total = 0
    unknown = False
    for sibling in getattr(info, "siblings", None) or []:
        size = getattr(sibling, "size", None)
        name = getattr(sibling, "rfilename", "")
        if size is None:
            unknown = True
            continue
        total += int(size)
        files.append({"name": name, "bytes": int(size)})
    files.sort(key=lambda item: item["bytes"], reverse=True)
    return {"ok": True, "repo": repo, "total_bytes": total, "unknown": unknown, "files": files}


def install(
    config,
    key: str,
    yes: bool = False,
    accept_license: bool = False,
    dry_run: bool = False,
) -> dict:
    """下载一个模型到模型根目录（先落暂存，校验通过再 mv）。

    流程：查清单 → 目标已就位则直接返回 → 许可门禁 → 报体积（>15GB 需确认）
    → 下载到 `<models_root>/.staging-<key>` → 校验必需文件 → `rename` 到目标 → 再校验一次。

    参数:
        config: AppConfig。
        key: 目录键（如 `cosyvoice3/8bit`）。
        yes: 已确认体积（超过 15GB 或体积未知时必须为 True）。
        accept_license: 已确认非商用/需授权许可。
        dry_run: 只报体积与许可门禁，不下载。

    返回:
        `{"status": ..., ...}`；status 取值：
        `unknown_key` / `retired` / `already` / `target_incomplete` / `need_license` /
        `need_confirm` / `plan`（dry-run）/ `incomplete` / `error` / `installed`。
    """
    entry = catalog_entry(key)
    if entry is None:
        return {"status": "unknown_key", "key": key, "available": [item["key"] for item in CATALOG]}
    if entry["license_gate"] == GATE_RETIRED:
        return {"status": "retired", "key": key, "message": f"{entry['display_name']} 已退役，不再提供下载"}

    root = models_root(config)
    target = root / key
    if target.exists() and any(target.iterdir()):
        missing = required_missing(target)
        if missing:
            return {"status": "target_incomplete", "key": key, "path": str(target), "missing": missing}
        return {"status": "already", "key": key, "path": str(target), "bytes": dir_size(target)}

    info = {"display_name": entry["display_name"], "key": key, "license": entry["license"],
            "commercial_use": entry["commercial_use"], "license_gate": entry["license_gate"],
            "note": entry.get("note")}
    if entry["license_gate"] in (GATE_BLOCK, GATE_APPROVAL) and not accept_license:
        return {"status": "need_license", **info}

    size = remote_size(key)
    if not size.get("ok"):
        return {"status": "error", "key": key, "repo": entry.get("hf_repo"), "error": size.get("error")}
    over_limit = size["total_bytes"] > BIG_MODEL_BYTES or size["unknown"]
    plan = {
        **info,
        "repo": size["repo"],
        "total_bytes": size["total_bytes"],
        "unknown": size["unknown"],
        "files": size["files"][:5],
        "staging": str(root / f".staging-{key.replace('/', '_')}"),
        "target": str(target),
        "over_limit": over_limit,
    }
    if dry_run:
        return {"status": "plan", **plan}
    if over_limit and not yes:
        return {"status": "need_confirm", **plan, "limit_bytes": BIG_MODEL_BYTES}

    staging = root / f".staging-{key.replace('/', '_')}"
    root.mkdir(parents=True, exist_ok=True)
    staging.mkdir(parents=True, exist_ok=True)
    try:
        os.environ["HF_ENDPOINT"] = HF_MIRROR
        from huggingface_hub import snapshot_download

        snapshot_download(repo_id=size["repo"], local_dir=str(staging))
    except Exception as exc:
        return {"status": "error", "key": key, "repo": size["repo"], "error": f"下载失败：{exc}", "staging": str(staging)}

    hub_cache = staging / ".cache"
    if hub_cache.exists():
        shutil.rmtree(hub_cache, ignore_errors=True)
    # 落清单：多子模型仓库（如 llm/ + speech_tokenizer/）没有顶层 model.safetensors，
    # 按实际下载内容逐文件记账，verify 才能准确报出缺哪个文件
    manifest = write_manifest(staging, key, size["repo"])
    if not manifest["files"]:
        return {"status": "error", "key": key, "repo": size["repo"], "error": "下载后目录为空", "staging": str(staging)}
    missing = required_missing(staging)
    if missing:
        return {
            "status": "incomplete",
            "key": key,
            "staging": str(staging),
            "missing": missing,
            "bytes": dir_size(staging),
            "message": f"下载内容不完整，缺少 {', '.join(missing)}；暂存目录保留以便重试",
        }

    _replace_empty_dir(staging, target)
    result = verify(config, keys=[key])
    item = (result.get("items") or [{}])[0]
    if not item.get("complete"):
        return {"status": "incomplete", "key": key, "path": str(target), "missing": item.get("missing")}
    return {"status": "installed", "key": key, "path": str(target), "bytes": dir_size(target), "repo": size["repo"]}


def _replace_empty_dir(staging: Path, target: Path) -> None:
    """把暂存目录 rename 成目标目录（目标若是空目录先删掉）。

    参数:
        staging: 已下载并校验通过的暂存目录（与目标同卷）。
        target: 目标目录。
    """
    if target.exists():
        if any(target.iterdir()):
            raise ValueError(f"目标目录已存在且非空，拒绝覆盖：{target}")
        target.rmdir()
    target.parent.mkdir(parents=True, exist_ok=True)
    staging.rename(target)


def dump(value) -> str:
    """统一 JSON 输出格式（供 CLI `--json` 使用）。

    参数:
        value: 任意可序列化对象。

    返回:
        缩进 JSON 文本。
    """
    return json.dumps(value, ensure_ascii=False, indent=2)
