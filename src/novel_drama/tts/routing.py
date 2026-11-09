"""自动路由：需求向量 → 硬过滤 → 打分 → 确定性决策。

设计依据：docs/design-v2.md §5.6 / §5.7。约束：
- 只按能力矩阵判断，不按模型名写死分支；
- 同输入同配置必须选出同一个 provider（同分按 provider_order、再按 id 排序）；
- 每条台词记录选中理由与被淘汰项，便于 UI 展开与重算复现。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from .errors import (
    LICENSE_BLOCKED,
    NOT_AUTO_SELECTABLE,
    PROVIDER_DISABLED,
    ROUTING_LOCKED_MISSING,
    ROUTING_NO_CANDIDATE,
    UNSUPPORTED_EMOTION,
    UNSUPPORTED_LANGUAGE,
    UNSUPPORTED_NONVERBAL,
    UNSUPPORTED_STREAMING,
    UNSUPPORTED_VOICE,
    TtsError,
)
from .types import LATENCY_SCORE, QUALITY_SCORE, Requirement

# 默认打分权重（可在 config 的 tts.routing.weights 覆盖）
DEFAULT_WEIGHTS = {
    "capability": 30,
    "quality": 25,
    "consistency": 20,
    "latency": 15,
    "resource": 10,
}


def _now() -> str:
    """当前 UTC 时间（ISO8601）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def resolve_lock(
    line_provider_id: str | None,
    character_provider_id: str | None,
    project_lock_provider_id: str | None,
) -> tuple[str | None, str]:
    """按 行 > 角色 > 工程 的优先级解析锁定 provider。

    参数:
        line_provider_id: 台词行上的 provider_id。
        character_provider_id: 角色绑定上的 provider_id。
        project_lock_provider_id: 工程级锁定的 provider_id。

    返回:
        (锁定的 provider_id 或 None, 模式名)。
    """
    if line_provider_id:
        return line_provider_id, "locked_line"
    if character_provider_id:
        return character_provider_id, "locked_character"
    if project_lock_provider_id:
        return project_lock_provider_id, "locked_project"
    return None, "auto"


def _provider_snapshot(providers: dict) -> dict:
    """用于 input_hash 的 provider 快照（只看决策相关字段）。"""
    snap = {}
    for pid, provider in providers.items():
        caps = provider.spec.capabilities.to_dict()
        snap[pid] = {
            "model": provider.spec.model,
            "model_version": provider.spec.model_version,
            "enabled": provider.spec.enabled,
            "capabilities": caps,
        }
    return snap


def compute_input_hash(
    requirement: Requirement,
    providers: dict,
    policy_revision: int,
    weights: dict,
    consistency_provider_id: str | None,
) -> str:
    """计算决策输入哈希；同哈希复用上次决策。

    参数:
        requirement: 需求向量。
        providers: provider 实例表。
        policy_revision: 路由策略版本。
        weights: 打分权重。
        consistency_provider_id: 同角色历史使用的 provider（影响一致性别分）。
    """
    payload = {
        "requirement": requirement.to_dict(),
        "providers": _provider_snapshot(providers),
        "policy_revision": policy_revision,
        "weights": weights,
        "consistency_provider_id": consistency_provider_id,
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return "sha256:" + hashlib.sha256(blob).hexdigest()


def _check(
    provider,
    requirement: Requirement,
) -> tuple[str | None, str | None, dict]:
    """对单个 provider 做硬过滤。

    参数:
        provider: TtsProvider 实例。
        requirement: 需求向量。

    返回:
        (淘汰阶段, 原因码, 附加信息)；全部通过时返回 (None, None, {})。
    """
    spec = provider.spec
    caps = spec.capabilities

    if not spec.enabled:
        return "availability", PROVIDER_DISABLED, {"detail": "配置中 enabled=false"}
    health = provider.health()
    if not health.ok:
        return "availability", health.code or "PROVIDER_UNREACHABLE", {"detail": health.detail}

    if not caps.get("allow_auto_select"):
        return "availability", NOT_AUTO_SELECTABLE, {"detail": "该 provider 不允许自动选中"}

    # 许可硬过滤
    if requirement.usage == "commercial":
        if not caps.get("commercial_use") or caps.get("license_gate") != "none":
            return (
                "license",
                LICENSE_BLOCKED,
                {
                    "detail": f"商用工程不允许：license={caps.get('license')}, gate={caps.get('license_gate')}"
                },
            )
    elif caps.get("license_gate") == "non_commercial":
        return "license", LICENSE_BLOCKED, {"detail": "许可仅限非商用且工程用途不符"}

    # 克隆
    if requirement.clone_required and not caps.get("voice_clone"):
        return "capability", UNSUPPORTED_VOICE, {"detail": "该 provider 不支持克隆音色"}

    # 情绪
    emotion = (requirement.emotion or "").strip()
    if emotion and emotion != "neutral":
        can_instruct = caps.get("emotion_control") == "instruct" and caps.get("emotion_instruction")
        can_reference = caps.get("emotion_control") == "reference" and caps.get("emotion_reference_variants")
        # 第三类通路：IndexTTS 这类用 8 维情绪向量的实现，能控情绪但不是 instruct/reference
        can_vector = caps.get("emotion_control") == "emo_vector_8d"
        if not (can_instruct or can_reference or can_vector):
            return "capability", UNSUPPORTED_EMOTION, {"detail": f"不支持情绪 {emotion}"}

    # 副语言
    for tag in requirement.paralinguistic:
        if caps.get("nonverbal_control") == "none" or not caps.supports_paralinguistic(tag):
            return "capability", UNSUPPORTED_NONVERBAL, {"detail": f"不支持副语言 {tag}"}

    # 语言
    if requirement.language not in (caps.get("languages") or []):
        return "capability", UNSUPPORTED_LANGUAGE, {"detail": f"不支持语言 {requirement.language}"}

    # 流式
    if requirement.streaming_required and not caps.get("streaming"):
        return "capability", UNSUPPORTED_STREAMING, {"detail": "不支持流式输出"}

    # 时长控制：不支持的允许后期补偿，只记标记，不淘汰
    extra = {}
    if requirement.duration_mode != "none" and not caps.get("native_duration_control"):
        extra["requires_duration_compensation"] = True
    return None, None, extra


def _score(provider, requirement: Requirement, weights: dict, consistency_provider_id: str | None) -> tuple[float, list[str], dict]:
    """给通过硬过滤的 provider 打分。

    参数:
        provider: TtsProvider 实例。
        requirement: 需求向量。
        weights: 打分权重。
        consistency_provider_id: 同角色历史 provider。

    返回:
        (总分, 选中理由, 附加标记)。
    """
    caps = provider.spec.capabilities
    reasons: list[str] = []
    flags: dict = {}

    # 能力匹配
    cap_score = float(weights.get("capability", 30))
    if requirement.emotion and requirement.emotion != "neutral":
        if caps.get("emotion_control") == "instruct":
            reasons.append("情绪走指令控制")
        elif caps.get("emotion_control") == "emo_vector_8d":
            reasons.append("情绪走 8 维向量")
        else:
            reasons.append("情绪走参考变体")
    levels = int(caps.get("emotion_intensity_levels") or 0)
    if levels and requirement.emotion_intensity >= levels:
        cap_score -= 6
        flags["intensity_downgrade"] = {"from": requirement.emotion_intensity, "to": levels - 1}
        reasons.append(f"情绪强度降档至 {levels - 1}")
    if requirement.paralinguistic:
        if caps.get("nonverbal_control") == "reference":
            cap_score -= 4
            reasons.append("副语言需切参考变体")
        else:
            reasons.append("副语言原生支持")

    # 质量 / 耗时
    quality = QUALITY_SCORE.get(caps.get("quality_tier"), 18.0)
    latency = LATENCY_SCORE.get(caps.get("latency_tier"), 10.0)
    reasons.append(f"质量档位 {caps.get('quality_tier')}、耗时档位 {caps.get('latency_tier')}")

    # 同角色一致性
    consistency_cfg = caps.get("consistency") or {}
    stability = consistency_cfg.get("stability", "medium")
    if consistency_provider_id and consistency_provider_id == provider.spec.provider_id:
        consistency = float(weights.get("consistency", 20))
        reasons.append("同角色历史一致")
    else:
        base = {"high": 12.0, "medium": 8.0, "low": 4.0}.get(stability, 8.0)
        consistency = base
        if consistency_cfg.get("pin_per_character"):
            consistency += 2
    if not consistency_provider_id:
        reasons.append("同角色暂无历史，按稳定度计分")

    # 资源余量
    vram = provider.spec.vram_estimate_gb
    if vram is None:
        resource = 5.0
    elif vram <= 8:
        resource = 10.0
    elif vram <= 16:
        resource = 7.0
    else:
        resource = 3.0
    if resource >= 10:
        reasons.append("显存占用小")

    total = cap_score + quality + consistency + latency + resource
    return round(total, 2), reasons, flags


def route(
    requirement: Requirement,
    providers: dict,
    provider_order: list[str],
    routing_cfg: dict,
    lock_provider_id: str | None = None,
    lock_mode: str = "auto",
    consistency_provider_id: str | None = None,
    preferred_provider_id: str | None = None,
) -> dict:
    """执行一次路由决策。

    参数:
        requirement: 需求向量。
        providers: provider 实例表。
        provider_order: 回落链顺序。
        routing_cfg: tts.routing 配置（mode/weights/policy_revision）。
        lock_provider_id: 三级锁定解析结果。
        lock_mode: 锁定来源（locked_line/character/project）。
        consistency_provider_id: 同角色历史 provider。
        preferred_provider_id: 配置里的主力 provider，满足条件时优先选中。

    返回:
        决策字典（对应 line.tts.routing）。
    """
    mode_cfg = routing_cfg.get("mode", "auto")
    weights = dict(DEFAULT_WEIGHTS)
    weights.update(routing_cfg.get("weights") or {})
    policy_revision = int(routing_cfg.get("policy_revision") or 1)
    input_hash = compute_input_hash(requirement, providers, policy_revision, weights, consistency_provider_id)

    # 锁定优先：不参与打分
    if lock_provider_id:
        provider = providers.get(lock_provider_id)
        if provider is None or not provider.spec.enabled:
            raise TtsError(
                ROUTING_LOCKED_MISSING,
                f"锁定的 provider 未启用或不存在：{lock_provider_id}",
                lock_provider_id,
            )
        stage, reason, info = _check(provider, requirement)
        if stage == "license":
            raise TtsError(LICENSE_BLOCKED, info.get("detail", "许可不允许当前用途"), lock_provider_id)
        if stage == "availability":
            raise TtsError(reason, info.get("detail", "provider 不可用"), lock_provider_id)
        return {
            "mode": lock_mode,
            "policy_revision": policy_revision,
            "input_hash": input_hash,
            "selected": {
                "provider_id": lock_provider_id,
                "variant_id": None,
                "score": None,
                "reasons": ["已按用户锁定，跳过自动打分"],
            },
            "rejected": [],
            "degradations": [],
            "decided_at": _now(),
        }

    if mode_cfg == "manual":
        raise TtsError(
            ROUTING_NO_CANDIDATE,
            "路由模式为 manual，但没有锁定任何 provider",
        )

    rejected: list[dict] = []
    candidates: list[tuple[float, int, str, list[str], dict]] = []
    order_index = {pid: i for i, pid in enumerate(provider_order)}

    for pid, provider in providers.items():
        stage, reason, info = _check(provider, requirement)
        if stage:
            rejected.append(
                {
                    "provider_id": pid,
                    "stage": stage,
                    "reason": reason,
                    "detail": info.get("detail"),
                }
            )
            continue
        score, reasons, flags = _score(provider, requirement, weights, consistency_provider_id)
        candidates.append((score, order_index.get(pid, 10_000), pid, reasons, flags))

    if not candidates:
        raise TtsError(
            ROUTING_NO_CANDIDATE,
            "没有满足条件（许可/能力/可用性）的 provider",
            hint="检查 config/config.yaml 的 tts.providers 与工程用途",
        )

    # 确定性排序：分数降序 → provider_order 索引升序 → provider_id 字典序
    candidates.sort(key=lambda item: (-item[0], item[1], item[2]))
    preferred = None
    if preferred_provider_id:
        preferred = next((item for item in candidates if item[2] == preferred_provider_id), None)
    if preferred is not None:
        # 主力 provider 只要通过许可/能力/可用性检查就优先，分数不翻盘
        score, _, pid, reasons, flags = preferred
        reasons = list(reasons) + [f"主力 provider 优先（tts.default_provider_id={preferred_provider_id}）"]
    else:
        score, _, pid, reasons, flags = candidates[0]

    degradations: list[dict] = []
    if "intensity_downgrade" in flags:
        item = flags["intensity_downgrade"]
        degradations.append(
            {
                "kind": "emotion_intensity_downgrade",
                "from": item["from"],
                "to": item["to"],
                "detail": "目标 provider 情绪强度档位不足，已向下就近取档",
            }
        )
    if flags.get("requires_duration_compensation"):
        degradations.append(
            {
                "kind": "duration_compensated",
                "from": requirement.target_duration_ms,
                "to": None,
                "detail": "provider 无原生时长控制，改用切分 + 静音填充 + 必要时微变速",
            }
        )

    return {
        "mode": "auto",
        "policy_revision": policy_revision,
        "input_hash": input_hash,
        "selected": {"provider_id": pid, "variant_id": None, "score": score, "reasons": reasons},
        "rejected": rejected,
        "degradations": degradations,
        "decided_at": _now(),
    }


def fallback_chain(selected_provider_id: str, provider_order: list[str], providers: dict) -> list[str]:
    """返回选中项之后的回落链。

    参数:
        selected_provider_id: 当前选中的 provider。
        provider_order: 配置的回落顺序。
        providers: provider 实例表。
    """
    chain: list[str] = []
    if selected_provider_id in provider_order:
        start = provider_order.index(selected_provider_id) + 1
        chain = [pid for pid in provider_order[start:] if pid in providers]
    else:
        chain = [pid for pid in provider_order if pid != selected_provider_id and pid in providers]
    return chain
