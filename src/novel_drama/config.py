"""配置加载：本机 `config/config.yaml` + 工程 `project.json` 的两层合并。"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "config.yaml"
# 页面「设置」写出的覆盖层：与手写的 config.yaml 分开存，避免页面保存时抹掉注释
DEFAULT_UI_CONFIG_PATH = REPO_ROOT / "config" / "config.ui.yaml"

# 模型下载镜像链：国内优先，国外降级（音频与 ComfyUI 模型下载共用）
_DEFAULT_DOWNLOAD_MIRRORS = ["https://hf-mirror.com", "https://huggingface.co"]

#: 启动时必须可写的配置路径：(配置键, 中文名称)
#: 这些位置会在运行期写入锁文件 / 缓存 / 下载音色；不可写时直接报错，
#: 不静默改用 /tmp 等其它目录，避免出现「看起来能跑、实际状态分裂」。
REQUIRED_WRITABLE_PATHS = (
    ("gpu.lock_path", "GPU 锁文件"),
    ("tts.hf_home", "HF 缓存目录"),
    ("tts.builtin_voices.root", "内置音色目录"),
)

# 无配置文件时的最小默认值，保证 doctor / init 可用
_FALLBACK = {
    # 模型权重只放外置盘；provider 只从这个目录解析，不扫全局 HF 缓存
    "paths": {"models_root": "/Volumes/other/models/ndw"},
    "adapters": {"writing": "omlx", "tts": "auto", "sfx": "placeholder", "comfyui": "local"},
    "omlx": {
        "protocol": "openai_compatible",
        "endpoint": "",
        "api_key": None,
        "model": "",
        "available_models": [],
        "context_window": None,
        "memory_ceiling_gb": 29.48,
        "idle_timeout_s": None,
        "max_concurrency": 8,
        "request_timeout_s": 300,
        "connect_timeout_s": 5,
        # 流式超时：首字等待、分片间隔、整次总时长（null=不限）
        "first_token_timeout_s": 60,
        "stream_idle_timeout_s": 60,
        "total_timeout_s": None,
        "max_retries": 2,
        "temperature": 0.8,
        "max_tokens": None,
        "stop": [],
        "enable_thinking": False,
        # 思考强度：low / medium / high；仅在 enable_thinking 打开时随请求下发
        "reasoning_effort": "medium",
        "unload": {"enabled": True, "path": "/v1/models/{model_id}/unload", "policy": "after_text_task"},
    },
    "comfyui": {
        "base_url": "http://127.0.0.1:8188",
        "poll_interval_ms": 1000,
        "request_timeout_ms": 30000,
        "job_timeout_ms": 1800000,
        "status_timeout_ms": 10000,
        "templates_dir": "config/comfy",
        # 空字符串表示自动探测（由 media.comfy_models 扫描 ComfyUI 日志与常见安装路径）
        "models_dir": "",
        "templates": {"image": "qwen_image_accel", "video_t2v": "minimax_h3_accel_t2v", "video_i2v": "minimax_h3_accel"},
    },
    "models": {"download_mirrors": list(_DEFAULT_DOWNLOAD_MIRRORS)},
    "gpu": {"lock_path": "/Volumes/other/gpu.lock", "max_concurrent": 1, "poll_interval_s": 2},
    # 音效：本地 Stable Audio 3（独立 venv + 常驻 worker），与 TTS 同构
    "sfx": {
        "default_provider_id": "stable_audio3_small_sfx",
        "providers": [],
        # 常驻 worker 空闲多久自动退出并释放模型内存（秒）；provider.runtime 可单独覆盖
        "idle_timeout_s": 300,
        # auto：LLM 可达就改写，失败降级词典；llm：只用 LLM；dict：只用词典
        "prompt": {"rewrite": "auto", "llm_timeout_s": 20},
    },
    # 配乐：默认项目内 Stable Audio Open 1.0，可切回 ComfyUI / ACE-Step
    "bgm": {
        "provider": "local",
        "default_provider_id": "stable_audio_open_10",
        "providers": [],
        "idle_timeout_s": 300,
        "normalize_narrative": True,
        "trim_narrative_edges": True,
        "prompt": {"rewrite": "auto", "llm_timeout_s": 20},
    },
    "tts": {
        "segmentation": {
            "max_chars": 24,
            "chunk_silence_ms": 180,
            "edge_trim_ms": 100,
            "edge_trim_max_ms": 400,
            "silence_threshold_dbfs": -45,
            "keep_min_ms": 30,
            "hard_split_ratio": 0.5,
            "trim_single_chunk": False,
        },
        "alignment": {
            "enabled": True,
            "max_error_ms": 250,
            "max_speed_change": 0.05,
            "on_exceed": "regenerate",
            "max_regenerate": 1,
        },
    },
    "output": {"sample_rate": 48000, "channels": 2, "loudness_target_lufs": -16, "video_max_ms": 5000, "image_width": 1328, "image_height": 1328},
    "voice": {
        "denoise": {
            "default_enabled": True,
            "engine": "ffmpeg_afftdn",
            "noise_reduction_db": 12,
            "noise_floor_db": -50,
            "max_attenuation_db": 20,
        },
        "design": {
            "enabled": True,
            "engine": "voxcpm2",
            "python": "/Users/zhaohaochen/git/voxcpm-venv/bin/python",
            "model_dir": "/Volumes/other/models/ndw/voxcpm2",
            "candidates": 4,
            "max_keep_days": 7,
            "cfg_value": 2.0,
            "inference_timesteps": 10,
            "timeout_s": 900,
            "script": "我用了一段时间才明白，有些话不说出口，反而更清楚。你愿意听我讲完吗？",
        },
    },
    "audio": {
        "highpass_hz": 80,
        "high_shelf_gain_db": 1.5,
        "high_shelf_hz": 9000,
        "true_peak_db": -1.5,
        "lra": 11,
    },
    "ffmpeg": {"binary": "ffmpeg", "probe_binary": "ffprobe"},
}


def _deep_merge(base: dict, extra: dict) -> dict:
    """递归合并两层配置（extra 覆盖 base）。

    参数:
        base: 默认配置。
        extra: 用户配置。

    返回:
        合并后的新 dict。
    """
    result = dict(base)
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


@dataclass
class AppConfig:
    """本机配置访问器。"""

    raw: dict
    path: Path | None = None
    project_root: Path | None = None
    warnings: list[str] = field(default_factory=list)
    # 页面覆盖层（config/config.ui.yaml）：只记录被页面改过的键，用于前端标「来源」
    ui_values: dict = field(default_factory=dict)
    ui_path: Path | None = None

    def get(self, dotted: str, default=None):
        """按 `a.b.c` 读取配置项。

        参数:
            dotted: 点号分隔的键路径。
            default: 缺失时的返回值。

        返回:
            配置值或默认值。
        """
        node = self.raw
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    @property
    def adapters(self) -> dict:
        """适配器装配表。"""
        return self.raw.get("adapters", {})

    @property
    def models_dir(self) -> Path | None:
        """ComfyUI 模型根目录（`comfyui.models_dir`）。

        参数:
            无。

        返回:
            绝对化后的 Path；配置为空时返回 None，由 comfy_models 自动探测。
        """
        value = self.get("comfyui.models_dir")
        if not value or not str(value).strip():
            return None
        path = Path(str(value).strip()).expanduser()
        return path if path.is_absolute() else (REPO_ROOT / path)

    @property
    def download_mirrors(self) -> list[str]:
        """模型下载镜像链（`models.download_mirrors`）。

        参数:
            无。

        返回:
            镜像地址列表；配置缺失或为空时返回内置默认（国内优先、国外降级）。
        """
        value = self.get("models.download_mirrors")
        if isinstance(value, list):
            mirrors = [str(item).strip().rstrip("/") for item in value if str(item).strip()]
            if mirrors:
                return mirrors
        return list(_DEFAULT_DOWNLOAD_MIRRORS)


def _get_dotted(raw: dict, dotted: str):
    """按 `a.b.c` 从字典里取值；任一层缺失返回 None。

    参数:
        raw: 已合并的配置字典。
        dotted: 点号分隔的键路径。

    返回:
        配置值或 None。
    """
    node = raw
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _ensure_writable(value, dotted: str, label: str) -> None:
    """校验配置里的写入路径是否可写；不可写直接抛中文错误。

    参数:
        value: 配置值（路径字符串，可为空）。
        dotted: 配置键，用于错误提示。
        label: 中文名称，用于错误提示。

    返回:
        无。

    异常:
        ValueError: 路径（或其最近存在的父目录）不可写时抛出，提示里带具体路径。
    """
    if value is None or not str(value).strip():
        return
    target = Path(str(value).strip()).expanduser()
    # 文件名（如 gpu.lock）看父目录；目录（如 hf-cache）看自身
    probe = target.parent if target.suffix else target
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    reason = ""
    if not probe.exists():
        reason = "目录不存在"
    else:
        # 只读挂载下 os.access 仍可能返回可写，必须真正写一个临时文件才能确认
        try:
            with tempfile.NamedTemporaryFile(dir=probe, prefix=".ndw-write-check-", delete=True):
                pass
        except OSError as exc:
            reason = f"写入测试失败：{exc.strerror or exc}"
    if reason:
        raise ValueError(
            f"配置项 {dotted}（{label}）指向的路径不可写：{target}。"
            f"最近存在的目录是 {probe}（{reason}）。"
            f"请把 {label} 改到可写位置（例如外置盘的可写目录）后重新启动；程序不会改用其它目录。"
        )


def load_config(path: str | Path | None = None, ui_path: str | Path | None = None) -> AppConfig:
    """加载本机配置。

    参数:
        path: 配置文件路径；默认 `config/config.yaml`，不存在时用内置默认值。
        ui_path: 页面覆盖层路径；默认 `config/config.ui.yaml`，不存在则跳过。

    返回:
        AppConfig。缺少 PyYAML 时记录 warning 并使用默认值。
    """
    warnings: list[str] = []
    config_path = Path(path).expanduser() if path else DEFAULT_CONFIG_PATH
    raw: dict = {}
    if config_path.exists():
        try:
            import yaml

            raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        except ImportError:
            warnings.append("缺少依赖 PyYAML，已使用内置默认配置；请 pip install pyyaml")
        except Exception as exc:  # yaml 语法错误直接暴露，避免带错运行
            raise ValueError(f"配置文件解析失败 {config_path}: {exc}") from exc
    else:
        warnings.append(f"未找到配置文件 {config_path}，使用内置默认配置")

    ui_values = _read_yaml(Path(ui_path).expanduser() if ui_path else DEFAULT_UI_CONFIG_PATH, warnings)

    # 三层：内置默认 < 手写 config.yaml < 页面覆盖层
    merged = _deep_merge(_deep_merge(_FALLBACK, raw if isinstance(raw, dict) else {}), ui_values)
    # 启动即校验写入路径：不可写就明确报中文错误并指出路径，不回退到其它目录
    for dotted, label in REQUIRED_WRITABLE_PATHS:
        _ensure_writable(_get_dotted(merged, dotted), dotted, label)
    project_root = os.environ.get("NDW_PROJECT") or merged.get("project_root")
    return AppConfig(
        raw=merged,
        path=config_path if config_path.exists() else None,
        project_root=Path(project_root).expanduser() if project_root else None,
        warnings=warnings,
        ui_values=ui_values,
        ui_path=(Path(ui_path).expanduser() if ui_path else DEFAULT_UI_CONFIG_PATH),
    )


def save_ui_config(updates: dict, path: str | Path | None = None) -> Path:
    """把设置页的连接配置写入覆盖层 `config/config.ui.yaml`。

    只写覆盖层、不改手写的 `config.yaml`，因此不会抹掉主配置里的注释；
    `load_config` 每次请求都会重新读取覆盖层，保存后立即生效、无需重启。

    参数:
        updates: 需要覆盖的配置片段（调用方负责白名单校验）。
        path: 覆盖层路径；默认 `config/config.ui.yaml`。

    返回:
        实际写入的覆盖层路径。
    """
    import yaml

    target = Path(path).expanduser() if path else DEFAULT_UI_CONFIG_PATH
    current = _read_yaml(target, [])
    merged = _deep_merge(current, updates)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    header = (
        "# 由 Web「设置」页写入，只覆盖 config.yaml 里的同名键。\n"
        "# 删掉本文件（或删掉某个键）即回落到 config.yaml 的取值。\n"
    )
    tmp.write_text(header + yaml.safe_dump(merged, allow_unicode=True, sort_keys=False), encoding="utf-8")
    os.replace(tmp, target)
    return target


def _read_yaml(path: Path, warnings: list[str]) -> dict:
    """读取 YAML 文件；不存在或为空返回空字典。

    参数:
        path: 文件路径。
        warnings: 解析告警收集列表（就地追加）。

    返回:
        解析出的字典；文件缺失、内容为空或解析失败时返回 {}。
    """
    if not path.exists():
        return {}
    try:
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # 页面覆盖层写坏了也不能拖垮 CLI 启动
        warnings.append(f"页面覆盖层解析失败，已忽略 {path}: {exc}")
        return {}
    return data if isinstance(data, dict) else {}
