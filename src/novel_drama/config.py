"""配置加载：本机 `config/config.yaml` + 工程 `project.json` 的两层合并。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "config.yaml"

# 无配置文件时的最小默认值，保证 doctor / init 可用
_FALLBACK = {
    # 模型权重只放外置盘；provider 只从这个目录解析，不扫全局 HF 缓存
    "paths": {"models_root": "/Volumes/other/models/ndw"},
    "adapters": {"writing": "omlx", "tts": "auto", "sfx": "placeholder", "comfyui": "local"},
    "omlx": {
        "protocol": "openai_compatible",
        "endpoint": "http://127.0.0.1:9000",
        "api_key": None,
        "model": "Qwen3.8-27B-oQ4e-mtp",
        "available_models": ["Qwen3.8-27B-oQ4e-mtp", "Qwen3.8-27B-Uncensored-OptiQ-4bit"],
        "context_window": 262144,
        "memory_ceiling_gb": 29.48,
        "idle_timeout_s": None,
        "max_concurrency": 8,
        "request_timeout_s": 300,
        "connect_timeout_s": 5,
        "max_retries": 2,
        "temperature": 0.8,
        "max_tokens": 2048,
        "stop": [],
        "enable_thinking": False,
        "unload": {"enabled": True, "path": "/v1/models/{model_id}/unload", "policy": "after_text_task"},
    },
    "comfyui": {
        "base_url": "http://127.0.0.1:8188",
        "poll_interval_ms": 1000,
        "request_timeout_ms": 30000,
        "job_timeout_ms": 1800000,
        "status_timeout_ms": 10000,
        "templates_dir": "config/comfy",
        "templates": {"image": "qwen_image_2.1_3stage", "video_t2v": "minimax_h3_5s", "video_i2v": "ltx_2.3_i2v"},
    },
    "gpu": {"lock_path": "/Volumes/other/gpu.lock", "max_concurrent": 1, "poll_interval_s": 2},
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


def load_config(path: str | Path | None = None) -> AppConfig:
    """加载本机配置。

    参数:
        path: 配置文件路径；默认 `config/config.yaml`，不存在时用内置默认值。

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

    merged = _deep_merge(_FALLBACK, raw if isinstance(raw, dict) else {})
    project_root = os.environ.get("NDW_PROJECT") or merged.get("project_root")
    return AppConfig(raw=merged, path=config_path if config_path.exists() else None, project_root=Path(project_root).expanduser() if project_root else None, warnings=warnings)
