"""台词行派生状态判定（七态模型）。

`stale` / `skipped` 是**派生态**，不落盘，每次读取时现算。判定需要的三样外部事实由
调用方传入，本模块只做纯判断：

- 产物是否存在（`missing_asset`）；
- 队列里是否还有这条（`live`）；
- 说话人展示名与风格指令（`speaker_name` / `instruct`），避免 domain 依赖展示层与提示词拼装。
"""

from __future__ import annotations

from collections.abc import Callable

from novel_drama.domain.speech.rules.params import (
    FIELD_LABELS,
    current_params,
    diff_params,
    format_value,
    line_fingerprint,
)


def resolve_line_state(
    line: dict,
    characters: dict[str, dict] | None = None,
    snapshot: dict | None = None,
    *,
    live: bool = False,
    missing_asset: bool = False,
    engine: dict | None = None,
    instruct: str | None = None,
    skip_kinds: tuple[str, ...] = (),
    speaker_name: Callable[[str | None], str] | None = None,
) -> dict:
    """计算一条台词的展示状态（七态模型）。

    参数:
        line: 台词行。
        characters: `{角色 ID: 角色}`；用于音色绑定变更判定。
        snapshot: 该行的侧车快照（`sidecar.snapshot` 的结果），没有则传 None。
        live: 该行是否仍在队列里排队/执行中（僵尸态判定）。
        missing_asset: 行上的音频产物是否已丢失（已检查且不存在时为 True）。
        engine: 当前配置导出的引擎侧参数；改裁切参数后据此判 stale。
        instruct: 当前行的风格指令（交付说明/情绪收敛），用于重建当前参数。
        skip_kinds: 不参与配音的行类型，默认空。
        speaker_name: 说话人 ID → 展示名的函数；不传时直接显示 ID。

    返回:
        `{"state", "persisted", "reason", "changes"}`：
        `state` 为七态之一，`persisted` 为落盘值，`changes` 为 `diff_params` 的结果。
    """
    tts = line.get("tts") or {}
    asked = tts.get("status") or "pending"
    text = (line.get("text") or "").strip()
    name_of = speaker_name or (lambda speaker_id: str(speaker_id or ""))

    if not text or (skip_kinds and line.get("kind") in skip_kinds):
        return {"state": "skipped", "persisted": asked, "reason": "empty_text", "changes": []}

    if asked in ("queued", "running"):
        # 僵尸态降级：队列里已经没有这条了，说明上次进程被中断
        if live:
            return {"state": asked, "persisted": asked, "reason": "active", "changes": []}
        return {"state": "pending", "persisted": asked, "reason": "zombie", "changes": []}

    if asked == "failed":
        return {"state": "failed", "persisted": asked, "reason": "last_failed", "changes": []}

    if asked != "ready":
        return {"state": "pending", "persisted": asked, "reason": "not_generated", "changes": []}

    if missing_asset:
        return {"state": "pending", "persisted": asked, "reason": "asset_missing", "changes": []}

    if snapshot is not None:
        current = current_params(line, snapshot.get("params"), engine, instruct)
        if line_fingerprint(current) != snapshot.get("fingerprint"):
            return {
                "state": "stale",
                "persisted": asked,
                "reason": "param_changed",
                "changes": diff_params(snapshot.get("params") or {}, current),
            }
    elif tts.get("synthesized_revision") != line.get("revision"):
        # 旧侧车（无指纹）走 revision 兜底，与改造前行为一致
        return {"state": "stale", "persisted": asked, "reason": "revision_changed", "changes": []}

    # 真人录音不依赖 TTS 音色/侧车参数：正文 revision 已在上方校验，直接算就绪。
    if tts.get("adapter") == "human":
        return {"state": "ready", "persisted": asked, "reason": "human_take", "changes": []}

    # 说话人是否变过：换了人就把音频标为过期，避免留着旧音色的音频还显示「就绪」。
    # 新侧车直接记 speaker_id；旧侧车没有该字段时，按音频音色反推归属者兜底。
    current_speaker = line.get("speaker_id")
    snapshot_speaker = (snapshot.get("params") or {}).get("speaker_id") if snapshot else None
    used_voice = tts.get("voice_id")
    if snapshot is not None and not snapshot_speaker and used_voice:
        owners = [
            character_id
            for character_id, character in (characters or {}).items()
            if ((character or {}).get("voice_binding") or {}).get("voice_id") == used_voice
        ]
        if owners and current_speaker not in owners:
            snapshot_speaker = owners[0]
    if snapshot_speaker and current_speaker != snapshot_speaker:
        before_name = name_of(snapshot_speaker)
        after_name = name_of(current_speaker)
        return {
            "state": "stale",
            "persisted": asked,
            "reason": "speaker_changed",
            "changes": [
                {
                    "group": "音色",
                    "field": "speaker_id",
                    "label": FIELD_LABELS["speaker_id"],
                    "from": snapshot_speaker,
                    "to": current_speaker,
                    "text": f"说话人 {before_name} → {after_name}",
                }
            ],
        }

    binding = ((characters or {}).get(line.get("speaker_id")) or {}).get("voice_binding") or {}
    if binding.get("voice_id") and tts.get("voice_id") != binding.get("voice_id"):
        return {
            "state": "stale",
            "persisted": asked,
            "reason": "voice_rebound",
            "changes": [
                {
                    "group": "音色",
                    "field": "voice_id",
                    "label": FIELD_LABELS["voice_id"],
                    "from": tts.get("voice_id"),
                    "to": binding.get("voice_id"),
                    "text": f"音色 {format_value('voice_id', tts.get('voice_id'))} → "
                    f"{format_value('voice_id', binding.get('voice_id'))}",
                }
            ],
        }

    return {"state": "ready", "persisted": asked, "reason": "matched", "changes": []}
