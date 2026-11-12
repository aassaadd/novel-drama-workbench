"""音效 provider 的数据结构与错误码。

只放上层需要看到的中立字段：描述、时长、采样参数、结果指标。
模型差异（设备、步数、cfg、提示词格式）全部收敛在适配器内部。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 错误码：与 TTS 的码表保持同名，便于上层统一处理
BAD_REQUEST = "BAD_REQUEST"
BAD_OUTPUT = "BAD_OUTPUT"
MODEL_MISSING = "MODEL_MISSING"
PROVIDER_DISABLED = "PROVIDER_DISABLED"
PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
PROVIDER_UNREACHABLE = "PROVIDER_UNREACHABLE"


class SfxError(RuntimeError):
    """音效生成失败（带机器可读错误码与 provider 标识）。"""

    def __init__(self, code: str, message: str, provider_id: str | None = None, hint: str | None = None) -> None:
        """初始化。

        参数:
            code: 错误码，见本模块常量。
            message: 人类可读错误说明。
            provider_id: 出错的 provider；与具体 provider 无关时可为空。
            hint: 可操作的修复提示。
        """
        super().__init__(message)
        self.code = code
        self.message = message
        self.provider_id = provider_id
        self.hint = hint


@dataclass
class SfxSpec:
    """单个音效 provider 的配置项（来自 `sfx.providers[]`）。"""

    provider_id: str
    model: str
    backend: str = "torch-mps"
    model_version: str | None = None
    display_name: str | None = None
    weights_path: str | None = None
    enabled: bool = False
    license: str | None = None
    license_gate: str | None = None
    commercial_use: bool | None = None
    # 生成默认参数，如 {"steps": 8, "cfg": 1.0, "device": "mps"}
    defaults: dict = field(default_factory=dict)
    # 其余字段（runtime / note 等）
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict) -> "SfxSpec":
        """从配置字典构造。

        参数:
            raw: 配置里的单个 provider 对象。

        返回:
            SfxSpec。
        """
        known = {
            "provider_id",
            "model",
            "backend",
            "model_version",
            "display_name",
            "weights_path",
            "enabled",
            "license",
            "license_gate",
            "commercial_use",
            "defaults",
        }
        return cls(
            provider_id=raw["provider_id"],
            model=raw["model"],
            backend=raw.get("backend") or "torch-mps",
            model_version=raw.get("model_version"),
            display_name=raw.get("display_name"),
            weights_path=raw.get("weights_path"),
            enabled=bool(raw.get("enabled")),
            license=raw.get("license"),
            license_gate=raw.get("license_gate"),
            commercial_use=raw.get("commercial_use"),
            defaults=raw.get("defaults") or {},
            extra={k: v for k, v in raw.items() if k not in known},
        )


@dataclass
class SfxRequest:
    """一次音效生成请求（provider 中立）。"""

    prompt: str
    output_path: str
    duration_s: float
    negative_prompt: str | None = None
    steps: int | None = None
    cfg: float | None = None
    seed: int | None = None


@dataclass
class SfxResult:
    """一次音效生成结果。"""

    audio_path: str
    sample_rate: int
    duration_s: float
    channels: int
    provider_id: str
    model: str
    latency_ms: int
    steps: int
    cfg: float
    seed: int
    load_s: float = 0.0
    peak_mib: float = 0.0

    def to_dict(self) -> dict:
        """转成可落盘 / 可打印的结构。"""
        return {
            "audio_path": self.audio_path,
            "sample_rate": self.sample_rate,
            "duration_s": self.duration_s,
            "channels": self.channels,
            "provider_id": self.provider_id,
            "model": self.model,
            "latency_ms": self.latency_ms,
            "steps": self.steps,
            "cfg": self.cfg,
            "seed": self.seed,
            "load_s": self.load_s,
            "peak_mib": self.peak_mib,
        }


@dataclass
class SfxHealth:
    """音效 provider 健康检查结果。"""

    provider_id: str
    ok: bool
    detail: str = ""
    code: str | None = None
    loaded: bool = False

    def to_dict(self) -> dict:
        """转成可打印结构。"""
        return {
            "provider_id": self.provider_id,
            "ok": self.ok,
            "detail": self.detail,
            "code": self.code,
            "loaded": self.loaded,
        }
