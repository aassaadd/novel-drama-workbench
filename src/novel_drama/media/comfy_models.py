"""ComfyUI 本地模型清单：目录探测、扫描、线上地址反查、下载与校验。

设计依据（Spec「本地模型即清单」「本地模型反查线上地址」「图像/视频模型一键下载」）：
- 清单以本机 ComfyUI models 目录里的真实文件为准，不内置虚构模型；
- 模板依赖的 9 个文件写死相对路径，扫描只记录本地状态与实际体积；
- 线上地址按「手工固定 → ComfyUI-Manager 清单 → sidecar → 可信仓库候选 → HF 镜像搜索」
  顺序反查，结果缓存在用户级 `config/comfy/models.local.json`（不入库）；
- 下载先落 `<models_dir>/.ndw-downloads/`，校验体积后再原子改名到目标相对路径，
  临时文件保留以便续传，目标路径绝不出现半截文件；
- 许可需要授权/禁止商用/许可未知的模型，下载前必须显式确认。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from ..config import REPO_ROOT

#: 仓库内模型清单（提交入库，由 scan 生成/更新）
MODELS_JSON = REPO_ROOT / "config" / "comfy" / "models.json"

#: 用户级解析缓存（不入库）
MODELS_LOCAL_JSON = REPO_ROOT / "config" / "comfy" / "models.local.json"

#: 镜像链：国内优先，国外降级
DEFAULT_MIRRORS = ("https://hf-mirror.com", "https://huggingface.co")

#: 找不到任何配置/日志/常见路径时的回落目录（本机共享模型盘）
DEFAULT_MODELS_DIR = "/Volumes/other/ComfyUI-Shared/models"

#: 视为模型权重的后缀
WEIGHT_SUFFIXES = (".safetensors", ".gguf", ".ckpt", ".pt", ".pth")

#: 下载暂存目录名（位于 models_dir 下）
DOWNLOAD_DIR_NAME = ".ndw-downloads"

#: 单项超过这个体积必须先报体积、等人工确认（字节）
BIG_MODEL_BYTES = 15 * 1024**3

#: 可信仓库候选（按优先级；未知文件的逐仓库 sibling 精确匹配也用它）
CANDIDATE_REPOS = (
    "city96/Qwen-Image-gguf",
    "Comfy-Org/Qwen-Image_ComfyUI",
    "lightx2v/Qwen-Image-Lightning",
    "Comfy-Org/MiniMax-H3",
    "Abiray/MiniMax-H3-Pruned-GGUF",
)

#: 许可门禁取值（与 tts/model_store.py 保持一致）
GATE_NONE = "none"
GATE_APPROVAL = "approval_required"
GATE_BLOCK = "block"

#: 日志里定位共享模型目录的行格式：`Adding extra search path <类别> <绝对路径>`
_SEARCH_PATH_RE = re.compile(r"Adding extra search path\s+(\S+)\s+(.+?)\s*$")

#: 从 HF 下载直链里拆出 (repo, 仓库内文件路径)
_HF_URL_RE = re.compile(r"https?://[^/]+/([^/]+/[^/]+)/resolve/[^/]+/(.+)$")


#: 模板依赖的本地模型清单。`key` 是相对 models_dir 的路径，也是下载键。
REQUIRED_MODELS: list[dict] = [
    {
        "key": "diffusion_models/qwen-image-Q4_K_S.gguf",
        "kind": "diffusion",
        "templates": ["qwen_image_accel"],
        "license": "Apache-2.0",
        "license_gate": GATE_NONE,
        "commercial_use": True,
        "note": "Qwen-Image 的 Q4_K_S GGUF 量化（city96 转换），首图模板的扩散模型",
    },
    {
        "key": "text_encoders/qwen_2.5_vl_7b_fp8_scaled.safetensors",
        "kind": "text_encoder",
        "templates": ["qwen_image_accel"],
        "license": "Apache-2.0",
        "license_gate": GATE_NONE,
        "commercial_use": True,
        "note": "Qwen2.5-VL-7B 文本编码器（fp8 scaled），随 Qwen-Image 一起使用",
    },
    {
        "key": "vae/qwen_image_vae.safetensors",
        "kind": "vae",
        "templates": ["qwen_image_accel"],
        "license": "Apache-2.0",
        "license_gate": GATE_NONE,
        "commercial_use": True,
        "note": "Qwen-Image 的 VAE",
    },
    {
        "key": "loras/Qwen-Image-Lightning-4steps-V2.0-bf16.safetensors",
        "kind": "lora",
        "templates": ["qwen_image_accel"],
        "license": "Apache-2.0",
        "license_gate": GATE_NONE,
        "commercial_use": True,
        "note": "Lightning 4 步加速 LoRA（bf16），模板里强度固定 1.0",
    },
    {
        "key": "diffusion_models/MiniMax-H3-FL2VA-Pruned-Q4_K_M.gguf",
        "kind": "diffusion",
        "templates": ["minimax_h3_accel", "minimax_h3_accel_t2v"],
        "license": "other",
        "license_gate": GATE_APPROVAL,
        "commercial_use": False,
        "note": "MiniMax-H3 FL2VA 剪枝版 Q4_K_M GGUF；仓库许可标记为 other（自定义），商用需确认授权",
    },
    {
        "key": "loras/minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors",
        "kind": "lora",
        "templates": ["minimax_h3_accel", "minimax_h3_accel_t2v"],
        "license": "other",
        "license_gate": GATE_APPROVAL,
        "commercial_use": False,
        "note": "H3 turbo 8 步 LoRA（ComfyUI bf16）；许可随 H3 仓库，商用需确认",
    },
    {
        "key": "text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
        "kind": "text_encoder",
        "templates": ["minimax_h3_accel", "minimax_h3_accel_t2v"],
        "license": "other",
        "license_gate": GATE_APPROVAL,
        "commercial_use": False,
        "note": "H3 专用的 Qwen3-VL-32B 文本编码器（nvfp4 awq 量化）；许可随 H3 仓库",
    },
    {
        "key": "vae/minimax_h3_video_vae_fp16.safetensors",
        "kind": "vae",
        "templates": ["minimax_h3_accel", "minimax_h3_accel_t2v"],
        "license": "other",
        "license_gate": GATE_APPROVAL,
        "commercial_use": False,
        "note": "H3 视频 VAE（fp16）；许可随 H3 仓库",
    },
    {
        "key": "vae/minimax_h3_audio_vae_fp32.safetensors",
        "kind": "vae",
        "templates": ["minimax_h3_accel", "minimax_h3_accel_t2v"],
        "license": "other",
        "license_gate": GATE_APPROVAL,
        "commercial_use": False,
        "note": "H3 音频 VAE（fp32），视频模板里的音轨解码用；许可随 H3 仓库",
    },
]


def _now() -> str:
    """当前本地时间字符串（写入清单与缓存用）。

    返回:
        形如 `2026-11-10T22:30:00` 的本地时间。
    """
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _read_json(path: Path) -> dict | None:
    """读取 JSON 文件。

    参数:
        path: 文件路径。

    返回:
        解析出的字典；文件不存在、内容为空或损坏时返回 None。
    """
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _write_json(path: Path, data: dict) -> None:
    """把字典写成带缩进的 UTF-8 JSON（父目录自动创建）。

    参数:
        path: 目标文件路径。
        data: 要写入的内容。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _file_bytes(path: Path) -> int:
    """安全读取文件体积。

    参数:
        path: 文件路径。

    返回:
        字节数；文件不存在或不可读时返回 0。
    """
    try:
        if path.is_file():
            return path.stat().st_size
    except OSError:
        pass
    return 0


def _entry(key: str) -> dict | None:
    """按相对路径查模板依赖清单项。

    参数:
        key: 相对 models_dir 的路径（如 `vae/qwen_image_vae.safetensors`）。

    返回:
        清单项或 None。
    """
    for item in REQUIRED_MODELS:
        if item["key"] == key:
            return item
    return None


def _comfy_log_files() -> list[Path]:
    """列出本机 ComfyUI 启动日志（新的在前）。

    同时覆盖 `~/ComfyUI-Installs/*/logs/*.log`，本机实际命中的是
    `/Users/zhaohaochen/ComfyUI-Installs/ComfyUI (1)/logs/comfyui.log`。

    返回:
        按修改时间倒序排列的日志文件；目录不存在时返回空列表。
    """
    base = Path("~/ComfyUI-Installs").expanduser()
    logs: list[Path] = []
    for pattern in ("*/logs/*.log", "*/ComfyUI/user/comfyui.log"):
        try:
            logs.extend(base.glob(pattern))
        except OSError:
            continue
    def _mtime(path: Path) -> float:
        """取文件修改时间（异常时排到最后）。

        参数:
            path: 日志文件。

        返回:
            mtime 秒数。
        """
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    return sorted(set(logs), key=_mtime, reverse=True)


def _log_search_paths(log: Path) -> list[str]:
    """从一条 ComfyUI 启动日志里取出 `Adding extra search path` 的绝对路径。

    路径可能含中文与空格，所以按「类别 + 剩余整行」解析；也兼容带引号的写法。

    参数:
        log: 日志文件路径。

    返回:
        去重后的路径字符串列表（按出现顺序）。
    """
    found: list[str] = []
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return found
    for line in text.splitlines():
        match = _SEARCH_PATH_RE.search(line)
        if not match:
            continue
        raw = match.group(2).strip().strip('"').strip("'")
        if raw and raw not in found:
            found.append(raw)
    return found


def _models_root_from_paths(paths: list[str]) -> Path | None:
    """把若干子目录（如 `.../models/vae`）归并成 models 根目录。

    参数:
        paths: 日志里的搜索路径列表。

    返回:
        存在且包含常见模型子目录的 models 根目录；无法确定时返回 None。
    """
    counts: dict[Path, int] = {}
    for raw in paths:
        path = Path(raw).expanduser()
        if not path.is_dir():
            continue
        root = path.parent
        try:
            if not root.is_dir():
                continue
        except OSError:
            continue
        counts[root] = counts.get(root, 0) + 1
    if not counts:
        return None
    ordered = sorted(counts.items(), key=lambda item: item[1], reverse=True)
    for root, _count in ordered:
        if any((root / name).is_dir() for name in ("diffusion_models", "loras", "vae", "text_encoders")):
            return root.resolve()
    return ordered[0][0].resolve()


def resolve_models_dir(config) -> Path:
    """定位 ComfyUI 的 models 目录。

    顺序：配置 `comfyui.models_dir` → ComfyUI 启动日志的 `Adding extra search path`
    → 常见安装路径（`~/ComfyUI/models`、`~/ComfyUI-Installs/*/ComfyUI/models`）
    → 回落 `/Volumes/other/ComfyUI-Shared/models`。

    参数:
        config: AppConfig（读 `comfyui.models_dir`）。

    返回:
        models 目录的绝对路径。

    异常:
        ValueError: 所有方式都找不到可用目录时抛出（中文提示如何修复）。
    """
    configured = config.get("comfyui.models_dir") if config is not None else None
    if configured and str(configured).strip():
        path = Path(str(configured)).expanduser()
        if not path.is_absolute():
            path = REPO_ROOT / path
        return path.resolve()

    for log in _comfy_log_files():
        root = _models_root_from_paths(_log_search_paths(log))
        if root is not None:
            return root

    common = [Path("~/ComfyUI/models").expanduser()]
    try:
        common.extend(sorted(Path("~/ComfyUI-Installs").expanduser().glob("*/ComfyUI/models")))
    except OSError:
        pass
    for candidate in common:
        if candidate.is_dir():
            return candidate.resolve()

    fallback = Path(DEFAULT_MODELS_DIR)
    if fallback.is_dir():
        return fallback.resolve()

    raise ValueError(
        "未找到 ComfyUI 模型目录：请在配置里设置 comfyui.models_dir，"
        "或确认 ComfyUI 启动日志里存在 `Adding extra search path ...`，"
        f"也可以放到常见路径 ~/ComfyUI/models 或 {DEFAULT_MODELS_DIR}"
    )


def _weight_files(root: Path) -> list[Path]:
    """递归列出 models 目录里的所有权重文件（跳过下载暂存目录）。

    参数:
        root: models 根目录。

    返回:
        权重文件路径列表（已排序）。
    """
    result: list[Path] = []
    if not root.is_dir():
        return result
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        if rel.parts and rel.parts[0] == DOWNLOAD_DIR_NAME:
            continue
        if path.suffix.lower() in WEIGHT_SUFFIXES:
            result.append(path)
    return sorted(result)


def _manual_hints() -> dict[str, dict]:
    """读取 models.json 里手工固定的 `repo` / `hf_filename`。

    返回:
        `{key: {"repo":..., "hf_filename":...}}`；文件缺失时为空字典。
    """
    data = _read_json(MODELS_JSON) or {}
    hints: dict[str, dict] = {}
    for item in data.get("models") or []:
        if not isinstance(item, dict) or not item.get("key"):
            continue
        if item.get("repo") or item.get("hf_filename"):
            hints[item["key"]] = {"repo": item.get("repo"), "hf_filename": item.get("hf_filename")}
    return hints


def cached_resolution(config=None) -> dict:
    """读取线上地址解析缓存（只读、不联网）。

    供 Web 接口展示「仓库 / 文件名 / 直链 / 解析失败原因」，绝不触发网络请求。

    参数:
        config: AppConfig（当前实现不读配置，保留参数以与其它函数签名一致）。

    返回:
        `{"resolved_at": 字符串或 None, "items": {key: {repo,hf_filename,url,source,size,error}}}`；
        缓存文件缺失或损坏时 `items` 为空字典。
    """
    data = _read_json(MODELS_LOCAL_JSON) or {}
    items = data.get("items")
    return {
        "resolved_at": data.get("resolved_at"),
        "items": dict(items) if isinstance(items, dict) else {},
    }


def scan(config, write: bool = True) -> dict:
    """扫描模板依赖的模型与目录内的其它权重，生成/更新 `config/comfy/models.json`。

    以本地文件为准：缺失项 `present=false`、`bytes=0`；被模板引用之外的文件只作为
    `unreferenced` 返回，不写进清单的 models 数组。models.json 里已有的手工
    `repo` / `hf_filename` 会保留，避免每次扫描丢掉人工修正。

    参数:
        config: AppConfig。
        write: 是否写回 `config/comfy/models.json`。

    返回:
        `{"ok","models_dir","models":[...],"unreferenced":[...],"written"}`。
    """
    root = resolve_models_dir(config)
    manual = _manual_hints()
    referenced: set[str] = set()
    models: list[dict] = []
    for entry in REQUIRED_MODELS:
        key = entry["key"]
        referenced.add(key)
        path = root / key
        present = path.is_file()
        hint = manual.get(key, {})
        models.append(
            {
                "key": key,
                "kind": entry["kind"],
                "path": key,
                "bytes": _file_bytes(path) if present else 0,
                "present": present,
                "templates": list(entry["templates"]),
                "license": entry["license"],
                "license_gate": entry["license_gate"],
                "commercial_use": entry["commercial_use"],
                "note": entry["note"],
                "repo": hint.get("repo"),
                "hf_filename": hint.get("hf_filename"),
            }
        )

    unreferenced = []
    for path in _weight_files(root):
        rel = path.relative_to(root).as_posix()
        if rel in referenced:
            continue
        unreferenced.append({"key": rel, "bytes": _file_bytes(path)})

    payload = {
        "version": 1,
        "models_dir": str(root),
        "models": models,
    }
    written = None
    if write:
        _write_json(MODELS_JSON, payload)
        written = str(MODELS_JSON)
    return {
        "ok": True,
        "models_dir": str(root),
        "models": models,
        "unreferenced": unreferenced,
        "written": written,
    }


def _hf_api(endpoint: str):
    """构造指向指定端点的 HfApi。

    参数:
        endpoint: `https://hf-mirror.com` 或 `https://huggingface.co`。

    返回:
        `huggingface_hub.HfApi` 实例。

    异常:
        RuntimeError: 未安装 huggingface_hub。
    """
    try:
        from huggingface_hub import HfApi
    except ImportError as exc:  # 核心功能不依赖它，只有反查/下载需要
        raise RuntimeError(f"未安装 huggingface_hub，无法访问 HF 端点：{exc}") from exc
    return HfApi(endpoint=endpoint)


def _repo_files(repo: str, endpoints: tuple[str, ...], cache: dict) -> tuple[str | None, list[dict], str | None]:
    """获取仓库的 sibling 列表（带进程内缓存，避免同一仓库重复请求）。

    参数:
        repo: 仓库 ID（如 `city96/Qwen-Image-gguf`）。
        endpoints: 依次尝试的端点（国内优先）。
        cache: 复用的缓存字典，键为仓库 ID。

    返回:
        `(成功端点, [{"rfilename","size"}], 错误原因)`；失败时端点为 None、错误原因为
        各端点失败原因拼接。
    """
    if repo in cache:
        return cache[repo]
    errors: list[str] = []
    for endpoint in endpoints:
        try:
            info = _hf_api(endpoint).model_info(repo, files_metadata=True)
        except Exception as exc:  # 网络/仓库不存在都只记原因，不中断解析
            errors.append(f"{endpoint}: {type(exc).__name__}: {str(exc)[:160]}")
            continue
        files = [
            {"rfilename": item.rfilename, "size": getattr(item, "size", None)}
            for item in (getattr(info, "siblings", None) or [])
        ]
        cache[repo] = (endpoint, files, None)
        return cache[repo]
    cache[repo] = (None, [], "；".join(errors) or "未知错误")
    return cache[repo]


def _try_repo(
    repo: str,
    filename: str,
    expected: int,
    endpoints: tuple[str, ...],
    cache: dict,
) -> tuple[dict | None, str]:
    """在指定仓库里按文件名（必要时校验体积）找线上文件。

    优先匹配完整 rfilename，其次按 basename 精确匹配；本地体积已知时要求线上体积一致，
    避免同名的不同版本被误认。

    参数:
        repo: 仓库 ID。
        filename: 本地文件名或仓库内相对路径。
        expected: 本地文件字节数（0 表示未知，跳过体积校验）。
        endpoints: 依次尝试的端点。
        cache: 仓库 sibling 缓存。

    返回:
        `(命中信息 {repo,hf_filename,size,endpoint}, "")` 或 `(None, 失败原因)`。
    """
    endpoint, files, error = _repo_files(repo, endpoints, cache)
    if error:
        return None, f"{repo}: {error}"
    picks = [item for item in files if item["rfilename"] == filename]
    if not picks:
        picks = [item for item in files if Path(item["rfilename"]).name == filename]
    if not picks:
        return None, f"{repo}: 无同名文件 {filename}"
    if expected:
        sized = [item for item in picks if item["size"] == expected]
        if not sized:
            first = picks[0]
            return None, f"{repo}: 体积不一致（线上 {first['size']} / 本地 {expected}）"
        picks = sized
    hit = picks[0]
    return {"repo": repo, "hf_filename": hit["rfilename"], "size": hit["size"], "endpoint": endpoint}, ""


def _manager_index() -> dict[str, list[dict]]:
    """读取 ComfyUI-Manager 的 model-list 缓存，建立「文件名 → 条目」索引。

    返回:
        `{文件名: [条目...]}`；没有缓存时为空字典。
    """
    index: dict[str, list[dict]] = {}
    base = Path("~/ComfyUI-Installs").expanduser()
    try:
        files = sorted(base.glob("*/ComfyUI/user/__manager/cache/*_model-list.json"))
    except OSError:
        return index
    for file in files:
        data = _read_json(file) or {}
        for item in data.get("models") or []:
            if not isinstance(item, dict):
                continue
            name = Path(str(item.get("filename") or "")).name
            url = str(item.get("url") or "")
            if not name or not url:
                continue
            index.setdefault(name, []).append({"url": url, "filename": name})
    return index


def _repo_filename_from_url(url: str) -> tuple[str, str] | None:
    """从 HF 下载直链里拆出 (repo, 仓库内文件路径)。

    参数:
        url: 形如 `https://huggingface.co/<repo>/resolve/main/<path>` 的链接。

    返回:
        `(repo, hf_filename)`；不是可识别的 HF 链接时返回 None。
    """
    match = _HF_URL_RE.search(url)
    if not match:
        return None
    repo = match.group(1)
    filename = urllib.parse.unquote(match.group(2).split("?")[0])
    return repo, filename


def _sidecar_hint(path: Path) -> tuple[str, str] | None:
    """从同目录 sidecar 元数据里找线上仓库/文件名。

    支持 `<file>.metadata.json`、`<file>.civitai.info`：既认显式的
    `repo`/`repo_id`/`hf_repo` + `filename`，也认内嵌的 HF 下载直链。

    参数:
        path: 本地模型文件。

    返回:
        `(repo, hf_filename)` 或 None。
    """
    for suffix in (".metadata.json", ".civitai.info"):
        sidecar = path.with_name(path.name + suffix)
        if not sidecar.exists():
            continue
        data = _read_json(sidecar)
        if not data:
            continue
        repo = data.get("repo") or data.get("repo_id") or data.get("hf_repo")
        filename = data.get("hf_filename") or data.get("filename")
        if isinstance(repo, str) and isinstance(filename, str) and "/" in repo:
            return repo, filename
        parsed = _repo_filename_from_url(json.dumps(data, ensure_ascii=False))
        if parsed:
            return parsed
        for value in data.values():
            if isinstance(value, str):
                parsed = _repo_filename_from_url(value)
                if parsed:
                    return parsed
    return None


def _search_repos(filename: str, endpoints: tuple[str, ...]) -> tuple[list[str], list[str]]:
    """用 HF 搜索 API 按文件名主干找候选仓库。

    参数:
        filename: 本地文件名。
        endpoints: 依次尝试的端点。

    返回:
        `(候选仓库 ID 列表, 失败原因列表)`。
    """
    stem = Path(filename).stem
    repos: list[str] = []
    errors: list[str] = []
    for endpoint in endpoints:
        try:
            models = _hf_api(endpoint).list_models(search=stem, limit=8)
        except Exception as exc:  # 镜像 503 / 直连超时都只记原因
            errors.append(f"{endpoint} 搜索失败：{type(exc).__name__}: {str(exc)[:160]}")
            continue
        for model in models:
            model_id = getattr(model, "id", None)
            if model_id and model_id not in repos:
                repos.append(model_id)
    return repos, errors


def _dialect_url(endpoint: str, repo: str, filename: str) -> str:
    """拼出下载直链（文件名按 URL 规则转义，保留仓库内子目录）。

    参数:
        endpoint: 命中的端点。
        repo: 仓库 ID。
        filename: 仓库内相对路径。

    返回:
        形如 `https://hf-mirror.com/<repo>/resolve/main/<path>` 的直链。
    """
    safe = urllib.parse.quote(filename)
    return f"{endpoint.rstrip('/')}/{repo}/resolve/main/{safe}"


def _resolve_one(
    key: str,
    root: Path,
    hint: dict,
    manager: dict[str, list[dict]],
    endpoints: tuple[str, ...],
    cache: dict,
) -> dict:
    """按解析链为单个本地模型反查线上来源。

    顺序：手工固定 → ComfyUI-Manager 清单 → sidecar → 可信仓库候选 → HF 镜像搜索。

    参数:
        key: 相对 models_dir 的本地路径。
        root: models 根目录。
        hint: models.json 里手工固定的 repo/hf_filename。
        manager: ComfyUI-Manager 的文件名索引。
        endpoints: 依次尝试的端点（国内优先）。
        cache: 仓库 sibling 缓存。

    返回:
        `{"repo","hf_filename","url","source","size","resolved_at","error"}`。
    """
    local = root / key
    expected = _file_bytes(local)
    filename = Path(key).name
    errors: list[str] = []

    def _finish(hit: dict, source: str) -> dict:
        """把命中信息整理成缓存条目。

        参数:
            hit: `_try_repo` 的命中结果。
            source: 解析来源标记。

        返回:
            缓存条目字典。
        """
        return {
            "repo": hit["repo"],
            "hf_filename": hit["hf_filename"],
            "url": _dialect_url(hit.get("endpoint") or endpoints[0], hit["repo"], hit["hf_filename"]),
            "source": source,
            "size": hit.get("size"),
            "resolved_at": _now(),
            "error": None,
        }

    # 1) models.json 手工固定的仓库/文件名
    if hint.get("repo") and hint.get("hf_filename"):
        hit, reason = _try_repo(hint["repo"], hint["hf_filename"], expected, endpoints, cache)
        if hit:
            return _finish(hit, "manual")
        errors.append(f"手工固定失败：{reason}")

    # 2) ComfyUI-Manager 清单：文件名精确匹配后，再用仓库 sibling 校验体积
    for item in manager.get(filename, []):
        parsed = _repo_filename_from_url(item["url"])
        if not parsed:
            errors.append(f"Manager 链接无法解析：{item['url']}")
            continue
        repo, hf_filename = parsed
        hit, reason = _try_repo(repo, hf_filename, expected, endpoints, cache)
        if hit:
            return _finish(hit, "comfyui-manager")
        errors.append(f"Manager 候选失败：{reason}")

    # 3) 同目录 sidecar 元数据
    sidecar = _sidecar_hint(local)
    if sidecar:
        repo, hf_filename = sidecar
        hit, reason = _try_repo(repo, hf_filename, expected, endpoints, cache)
        if hit:
            return _finish(hit, "sidecar")
        errors.append(f"sidecar 候选失败：{reason}")

    # 4) 可信仓库候选：逐个仓库做 sibling 精确匹配
    for repo in CANDIDATE_REPOS:
        hit, reason = _try_repo(repo, filename, expected, endpoints, cache)
        if hit:
            return _finish(hit, "candidate")
        errors.append(f"候选失败：{reason}")

    # 5) HF 镜像搜索 API：按文件名主干搜仓库，再精确匹配 sibling
    repos, search_errors = _search_repos(filename, endpoints)
    errors.extend(search_errors)
    for repo in repos:
        hit, reason = _try_repo(repo, filename, expected, endpoints, cache)
        if hit:
            return _finish(hit, "search")
        errors.append(f"搜索候选失败：{reason}")

    return {
        "repo": None,
        "hf_filename": None,
        "url": None,
        "source": None,
        "size": None,
        "resolved_at": _now(),
        "error": "；".join(errors)[-2000:] or "未找到任何线上来源",
    }


def resolve(config, keys: list[str] | None = None, refresh: bool = False) -> dict:
    """反查本地模型的线上地址，并写入 `config/comfy/models.local.json`。

    未指定 `refresh` 且缓存里已有该键的结果时直接复用；网络失败不抛异常，把原因写进
    条目的 `error`，不影响其它模型。

    参数:
        config: AppConfig。
        keys: 只解析这些下载键；None 表示解析全部模板依赖项。
        refresh: True 时忽略已有缓存重新解析。

    返回:
        `{"ok","models_dir","resolved_at","items": {key: {...}}}`。
    """
    root = resolve_models_dir(config)
    targets = [entry["key"] for entry in REQUIRED_MODELS]
    if keys:
        unknown = [key for key in keys if _entry(key) is None]
        if unknown:
            raise ValueError(f"未知模型键：{', '.join(unknown)}；请先用 scan 生成清单")
        targets = list(keys)

    cache_data = _read_json(MODELS_LOCAL_JSON) or {}
    items: dict[str, dict] = dict(cache_data.get("items") or {})
    manager = _manager_index()
    endpoints = DEFAULT_MIRRORS
    repo_cache: dict = {}
    hints = _manual_hints()

    for key in targets:
        if not refresh and key in items and (items[key].get("repo") or items[key].get("error")):
            continue
        items[key] = _resolve_one(key, root, hints.get(key, {}), manager, endpoints, repo_cache)

    payload = {"version": 1, "resolved_at": _now(), "items": items}
    _write_json(MODELS_LOCAL_JSON, payload)
    resolved = sum(1 for item in items.values() if item.get("repo"))
    return {
        "ok": True,
        "models_dir": str(root),
        "resolved_at": payload["resolved_at"],
        "resolved": resolved,
        "total": len(items),
        "items": items,
        "path": str(MODELS_LOCAL_JSON),
    }


def save_manual(
    config,
    key: str,
    repo: str | None = None,
    hf_filename: str | None = None,
    url: str | None = None,
) -> dict:
    """手工补录某个模型的线上来源，写入 `models.local.json`。

    供设置页「需手工指定」时使用：只要给了 `repo + hf_filename` 或 `url` 之一即可，
    后续 `install` 会优先复用这里的缓存，不再联网反查。

    参数:
        config: AppConfig（保留参数，当前实现只读写仓库内缓存文件）。
        key: 相对 models_dir 的下载键。
        repo: HuggingFace 仓库 ID（如 `city96/Qwen-Image-gguf`）。
        hf_filename: 仓库内文件名；给了 repo 就必须给。
        url: 直接下载地址；与 repo/hf_filename 二选一即可。

    返回:
        `{"ok": True, "key": key, "item": {...}}`。

    异常:
        ValueError: 键未知、缺少地址、或地址格式不合法。
    """
    if _entry(key) is None:
        raise ValueError(f"未知模型键：{key}；只能给模板依赖的模型手工指定来源")

    repo = (repo or "").strip()
    hf_filename = (hf_filename or "").strip()
    url = (url or "").strip()
    if repo and not hf_filename:
        raise ValueError("填了仓库时还必须填仓库内文件名")
    if hf_filename and not repo:
        raise ValueError("填了文件名时还必须填仓库 ID")
    if not repo and not url:
        raise ValueError("请填写仓库 + 文件名，或直接下载地址")
    if url and not url.startswith(("http://", "https://")):
        raise ValueError("直接下载地址必须是 http:// 或 https:// 开头")

    cache_data = _read_json(MODELS_LOCAL_JSON) or {}
    items: dict = dict(cache_data.get("items") or {})
    previous = items.get(key) or {}
    item = {
        "repo": repo or None,
        "hf_filename": hf_filename or None,
        "url": url or None,
        "source": "manual",
        "size": previous.get("size"),
        "resolved_at": _now(),
        "error": None,
    }
    items[key] = item
    _write_json(
        MODELS_LOCAL_JSON,
        {"version": 1, "resolved_at": _now(), "items": items},
    )
    return {"ok": True, "key": key, "item": item}


def _staging_path(root: Path, key: str) -> Path:
    """计算某个下载键的暂存文件路径。

    暂存目录固定 `<models_dir>/.ndw-downloads/`，键里的斜杠替换为 `__`，避免与其它
    模型共享子目录、也避免半截文件出现在 ComfyUI 的扫描路径里。

    参数:
        root: models 根目录。
        key: 相对路径下载键。

    返回:
        暂存目录下与目标同名的文件路径。
    """
    safe = key.replace("/", "__").replace("\\", "__")
    return root / DOWNLOAD_DIR_NAME / safe / Path(key).name


def _size_matches(actual: int, expected: int) -> bool:
    """判断文件体积是否符合预期。

    参数:
        actual: 实际字节数。
        expected: 预期字节数（0 表示未知，视为通过）。

    返回:
        是否符合。
    """
    return expected <= 0 or actual == expected


def _download_url(url: str, dest: Path, timeout: int = 60) -> int:
    """用 HTTP Range 把直链下载到暂存文件（支持中断续传）。

    参数:
        url: 下载直链。
        dest: 暂存文件路径（已存在的部分内容会被续传）。
        timeout: 单次请求超时秒数。

    返回:
        下载完成后的文件字节数。

    异常:
        OSError / urllib.error.URLError: 网络或磁盘错误，由调用方记录到 attempts。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    offset = dest.stat().st_size if dest.exists() else 0
    request = urllib.request.Request(url, headers={"User-Agent": "ndw/0.1"})
    if offset:
        request.add_header("Range", f"bytes={offset}-")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        mode = "ab" if offset and getattr(response, "status", 200) == 206 else "wb"
        with dest.open(mode) as handle:
            shutil.copyfileobj(response, handle, length=1024 * 1024)
    return dest.stat().st_size


def _move_into_place(src: Path, target: Path) -> None:
    """把校验通过的暂存文件移动到目标相对路径。

    同卷用 `os.replace`（原子），跨卷用 `shutil.move`；目标父目录自动创建。

    参数:
        src: 暂存文件。
        target: 目标文件（`<models_dir>/<key>`）。
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        same_volume = os.stat(src).st_dev == os.stat(target.parent).st_dev
    except OSError:
        same_volume = False
    if same_volume:
        os.replace(src, target)
    else:
        shutil.move(str(src), str(target))


def _cleanup_staging(staging: Path, root: Path) -> None:
    """清理下载完成后留下的暂存目录（每个下载键独占一个目录）。

    HF 仓库内文件可能带子目录（如 `split_files/vae/...`），下载后暂存目录下会残留
    空子目录，直接整目录删除；删完若 `.ndw-downloads/` 已空也一并移除。

    参数:
        staging: 暂存文件路径（可能不存在或位于子目录内）。
        root: models 根目录。
    """
    base = root / DOWNLOAD_DIR_NAME
    safe_dir = staging if staging.is_dir() else staging.parent
    if safe_dir.parent == base:
        shutil.rmtree(safe_dir, ignore_errors=True)
    try:
        base.rmdir()
    except OSError:
        pass


def install(
    config,
    key: str,
    yes: bool = False,
    accept_license: bool = False,
    dry_run: bool = False,
    mirrors: list[str] | None = None,
) -> dict:
    """下载单个 ComfyUI 模型文件到 `<models_dir>/<key>`。

    流程：目标已存在且体积正确直接返回 → 许可门禁 → 解析线上地址 → 体积确认
    → 逐个镜像下载到 `.ndw-downloads/` → 校验体积 → 原子改名就位。

    参数:
        config: AppConfig。
        key: 相对 models_dir 的下载键（如 `vae/qwen_image_vae.safetensors`）。
        yes: 已确认体积（超过 15GB 或体积未知时必须为 True）。
        accept_license: 已确认需授权/禁止商用的许可限制。
        dry_run: 只返回下载计划，不发起请求。
        mirrors: 自定义镜像链；默认读 `models.download_mirrors`，否则 hf-mirror → huggingface。

    返回:
        `{"status", "key", "target", "bytes", "mirror_used", "attempts", ...}`；
        status 取值：`unknown_key` / `already` / `need_license` / `need_confirm` /
        `unresolved` / `plan` / `incomplete` / `error` / `installed`。
    """
    entry = _entry(key)
    if entry is None:
        return {
            "status": "unknown_key",
            "key": key,
            "available": [item["key"] for item in REQUIRED_MODELS],
        }

    root = resolve_models_dir(config)
    target = root / key
    scan_data = _read_json(MODELS_JSON) or {}
    scanned = next(
        (item for item in scan_data.get("models") or [] if item.get("key") == key),
        {},
    )
    expected = int(scanned.get("bytes") or 0)
    actual = _file_bytes(target)

    if actual and _size_matches(actual, expected):
        return {
            "status": "already",
            "key": key,
            "target": str(target),
            "bytes": actual,
            "mirror_used": None,
            "attempts": [],
        }

    license_info = {
        "license": entry["license"],
        "license_gate": entry["license_gate"],
        "commercial_use": entry["commercial_use"],
        "note": entry["note"],
    }
    if entry["license_gate"] in (GATE_BLOCK, GATE_APPROVAL) and not accept_license:
        return {"status": "need_license", "key": key, "target": str(target), **license_info}

    cache_data = _read_json(MODELS_LOCAL_JSON) or {}
    item = (cache_data.get("items") or {}).get(key)
    if not item or not (item.get("repo") or item.get("url")):
        resolved = resolve(config, keys=[key], refresh=True)
        item = (resolved.get("items") or {}).get(key) or {}
    if not (item.get("repo") and item.get("hf_filename")) and not item.get("url"):
        return {
            "status": "unresolved",
            "key": key,
            "target": str(target),
            "error": item.get("error") or "没有解析到线上地址，请先在 models.local.json 手工补录 repo/hf_filename 或 url",
        }

    expected = expected or int(item.get("size") or 0)
    chain = list(mirrors or config.get("models.download_mirrors") or DEFAULT_MIRRORS)
    plan = {
        "key": key,
        "target": str(target),
        "bytes": expected,
        "repo": item.get("repo"),
        "hf_filename": item.get("hf_filename"),
        "url": item.get("url"),
        "source": item.get("source"),
        "mirrors": chain,
        "staging": str(_staging_path(root, key)),
        **license_info,
    }
    if dry_run:
        return {"status": "plan", **plan}

    over_limit = expected > BIG_MODEL_BYTES or expected <= 0
    if over_limit and not yes:
        return {"status": "need_confirm", "limit_bytes": BIG_MODEL_BYTES, **plan}

    staging = _staging_path(root, key)
    staging.parent.mkdir(parents=True, exist_ok=True)
    attempts: list[dict] = []
    mirror_used: str | None = None
    downloaded: Path | None = None

    if item.get("repo") and item.get("hf_filename"):
        for mirror in chain:
            try:
                from huggingface_hub import hf_hub_download

                path = hf_hub_download(
                    repo_id=item["repo"],
                    filename=item["hf_filename"],
                    local_dir=str(staging.parent),
                    endpoint=mirror,
                )
                downloaded = Path(path)
                mirror_used = mirror
                attempts.append({"mirror": mirror, "ok": True})
                break
            except Exception as exc:  # 单个镜像失败继续下一个
                attempts.append({"mirror": mirror, "ok": False, "error": f"{type(exc).__name__}: {str(exc)[:300]}"})
        if downloaded is not None:
            hub_cache = staging.parent / ".cache"
            if hub_cache.exists():
                shutil.rmtree(hub_cache, ignore_errors=True)
    else:
        url = str(item["url"])
        candidates: list[tuple[str, str]] = []
        seen_urls: set[str] = set()
        for mirror in chain:
            rewritten = url
            for known in DEFAULT_MIRRORS:
                if url.startswith(known):
                    rewritten = mirror.rstrip("/") + url[len(known):]
                    break
            if rewritten in seen_urls:  # 非 HF 直链不随镜像变化，避免重复重试同一地址
                continue
            seen_urls.add(rewritten)
            candidates.append((mirror, rewritten))
        for mirror, candidate_url in candidates:
            try:
                _download_url(candidate_url, staging)
                downloaded = staging
                mirror_used = mirror
                attempts.append({"mirror": mirror, "url": candidate_url, "ok": True})
                break
            except (OSError, urllib.error.URLError) as exc:
                attempts.append({"mirror": mirror, "url": candidate_url, "ok": False, "error": f"{type(exc).__name__}: {str(exc)[:300]}"})

    if downloaded is None or not downloaded.is_file():
        return {
            "status": "error",
            "key": key,
            "target": str(target),
            "bytes": 0,
            "mirror_used": None,
            "attempts": attempts,
            "error": "所有镜像均下载失败：" + "；".join(
                f"{item.get('mirror')}: {item.get('error')}" for item in attempts if not item.get("ok")
            ),
        }

    got = _file_bytes(downloaded)
    if not _size_matches(got, expected):
        return {
            "status": "incomplete",
            "key": key,
            "target": str(target),
            "staging": str(downloaded),
            "bytes": got,
            "expected": expected,
            "mirror_used": mirror_used,
            "attempts": attempts,
            "error": f"下载体积不一致（实际 {got} / 预期 {expected}），暂存文件已保留以便续传",
        }

    if actual and actual == expected:
        # 下载期间目标被其它进程补齐，避免覆盖完整文件
        _cleanup_staging(staging, root)
        return {
            "status": "already",
            "key": key,
            "target": str(target),
            "bytes": actual,
            "mirror_used": mirror_used,
            "attempts": attempts,
        }

    _move_into_place(downloaded, target)
    _cleanup_staging(staging, root)
    return {
        "status": "installed",
        "key": key,
        "target": str(target),
        "bytes": _file_bytes(target),
        "mirror_used": mirror_used,
        "attempts": attempts,
        "message": "已就位；请在 ComfyUI 界面刷新模型列表后生效",
    }


def verify(config, keys: list[str] | None = None) -> dict:
    """核对模型文件是否就位且体积与清单一致。

    参数:
        config: AppConfig。
        keys: 只检查这些下载键；None 表示全部模板依赖项。

    返回:
        `{"ok": bool, "models_dir": str, "models": [{"key","path","present","bytes","expected","ok"}]}`。
    """
    root = resolve_models_dir(config)
    data = _read_json(MODELS_JSON)
    if not data:
        scan(config, write=True)
        data = _read_json(MODELS_JSON) or {}
    scanned = {item.get("key"): item for item in data.get("models") or []}
    targets = list(keys or [entry["key"] for entry in REQUIRED_MODELS])
    rows = []
    for key in targets:
        path = root / key
        present = path.is_file()
        actual = _file_bytes(path) if present else 0
        expected = int((scanned.get(key) or {}).get("bytes") or 0)
        if not expected:
            expected = actual
        rows.append(
            {
                "key": key,
                "path": str(path),
                "present": present,
                "bytes": actual,
                "expected": expected,
                "ok": present and _size_matches(actual, expected),
            }
        )
    return {"ok": all(row["ok"] for row in rows), "models_dir": str(root), "models": rows}


def missing_for_template(config, model_keys: list[str]) -> list[str]:
    """列出模板依赖里本地缺失的模型键（供媒体层提交前检查）。

    参数:
        config: AppConfig。
        model_keys: 模板依赖的相对路径列表（通常来自 manifest）。

    返回:
        缺失（不存在或为空文件）的键列表；全部就位时为空列表。

    异常:
        ValueError: 无法定位 models 目录时抛出（提示需先配置 comfyui.models_dir）。
    """
    root = resolve_models_dir(config)
    missing = []
    for key in model_keys:
        if _file_bytes(root / key) <= 0:
            missing.append(key)
    return missing
