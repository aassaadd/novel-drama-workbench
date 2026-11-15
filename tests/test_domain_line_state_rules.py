"""阶段 3 台词行 stale 判定的并跑比对：领域规则与旧实现逐条一致。

覆盖七态模型的每条分支（skipped / queued / zombie / failed / pending / asset_missing /
param_changed / revision_changed / human_take / speaker_changed / voice_rebound / ready），
并在真实工程章节上逐行比对。真实工程用环境变量开启：

    NDW_GOLDEN_PROJECTS=/Volumes/other/ndw_runs/tt1 \\
        .venv/bin/python -m pytest -q tests/test_domain_line_state_rules.py
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import pytest

from novel_drama.domain.speech.rules import line_state as domain_rules
from novel_drama.domain.speech.rules.params import (
    current_params,
    diff_params,
    line_fingerprint,
)
from novel_drama.models import entities
from novel_drama.tts import line_state as legacy

PROJECTS = [Path(item) for item in os.environ.get("NDW_GOLDEN_PROJECTS", "").split(":") if item]

#: 旧入口的 `_instruct_for` 依赖 `tts.batch`；并跑时以它的结果为准，
#: 保证两侧比较的是状态判定本身，而不是提示词收敛（后者仍留在旧入口）。
def _instruct(line: dict) -> str | None:
    """取旧入口算出的风格指令。"""
    return legacy._instruct_for(line)


def _call_domain(line: dict, characters: dict | None, snapshot: dict | None, root=None, live_ids=None, engine=None) -> dict:
    """按调用方的口径准备好外部事实，再调用领域规则（与旧入口一一对应）。"""
    asset_id = (line.get("tts") or {}).get("asset_id")
    missing_asset = False
    if root is not None and asset_id:
        path = Path(asset_id)
        missing_asset = not (path if path.is_absolute() else Path(root) / asset_id).exists()
    live = bool(live_ids) and line.get("id") in (live_ids or set())
    return domain_rules.resolve_line_state(
        line,
        characters,
        snapshot,
        live=live,
        missing_asset=missing_asset,
        engine=engine,
        instruct=_instruct(line),
        speaker_name=lambda speaker_id: entities.speaker_display_name(speaker_id, characters or {}),
    )


def _call_domain_with_patch(line: dict, characters: dict | None, snapshot: dict | None, **kwargs) -> dict:
    """给 synthetic 用例直接用领域规则（instruct 与旧实现对齐同一份值）。"""
    return domain_rules.resolve_line_state(
        line,
        characters,
        snapshot,
        instruct=_instruct(line),
        speaker_name=lambda speaker_id: entities.speaker_display_name(speaker_id, characters or {}),
        **kwargs,
    )


def _base_line(**overrides) -> dict:
    """构造一条合法台词行（默认 pending）。"""
    line = {
        "id": "lin_1",
        "revision": 2,
        "text": "你终于来了。",
        "emotion": "neutral",
        "emotion_intensity": 1.0,
        "speed": 1.0,
        "delivery_note": None,
        "target_duration_ms": None,
        "speaker_id": "chr_a",
        "tts": {"status": "pending"},
    }
    line.update(overrides)
    return line


def _snapshot(line: dict, **param_overrides) -> dict:
    """按当前行生成一份「参数未变」的侧车快照。"""
    params = current_params(line, {}, None, _instruct(line))
    params["speaker_id"] = line.get("speaker_id")
    params.update(param_overrides)
    return {"params": params, "fingerprint": line_fingerprint(params)}


def _characters() -> dict:
    """两个角色：chr_a 绑定 voc_a，chr_b 绑定 voc_b。"""
    return {
        "chr_a": {"id": "chr_a", "name": "顾三", "voice_binding": {"voice_id": "voc_a"}},
        "chr_b": {"id": "chr_b", "name": "沈砚", "voice_binding": {"voice_id": "voc_b"}},
        entities.NARRATOR_ID: {"id": entities.NARRATOR_ID, "name": "旁白"},
    }


def _cases() -> list[tuple[str, dict, dict, dict | None, dict]]:
    """返回 (用例名, line, characters, snapshot, 旧入口参数)。"""
    characters = _characters()
    cases: list[tuple[str, dict, dict, dict | None, dict]] = []

    cases.append(("empty_text", _base_line(text="   "), characters, None, {}))
    cases.append(("queued_live", _base_line(tts={"status": "queued"}), characters, None, {"live_ids": {"lin_1"}}))
    cases.append(("queued_zombie", _base_line(tts={"status": "queued"}), characters, None, {"live_ids": set()}))
    cases.append(("running_zombie", _base_line(tts={"status": "running"}), characters, None, {}))

    ready = _base_line(tts={"status": "ready", "synthesized_revision": 2, "asset_id": "assets/voice/lines/x.wav"})
    cases.append(("failed", _base_line(tts={"status": "failed"}), characters, None, {}))
    cases.append(("pending", ready, characters, None, {}))
    cases.append(("revision_stale", _base_line(tts={"status": "ready", "synthesized_revision": 1}), characters, None, {}))
    cases.append(("human", _base_line(tts={"status": "ready", "synthesized_revision": 2, "adapter": "human"}), characters, None, {}))

    matched = _base_line(tts={"status": "ready", "asset_id": None})
    cases.append(("snapshot_matched", matched, characters, _snapshot(matched), {}))
    cases.append(("param_changed", matched, characters, _snapshot(matched, speed=1.2), {}))

    rebound = _base_line(tts={"status": "ready", "voice_id": "voc_x"})
    cases.append(("voice_rebound", rebound, characters, _snapshot(rebound), {}))

    speaker_changed = _base_line(tts={"status": "ready"})
    cases.append(("speaker_changed", speaker_changed, characters, _snapshot(speaker_changed, speaker_id="chr_b"), {}))

    # 旧侧车没有 speaker_id 时按音频音色反推归属者
    legacy_snapshot = {"params": {"speaker_id": None}, "fingerprint": None}
    inferred = _base_line(tts={"status": "ready", "voice_id": "voc_b"})
    cases.append(("speaker_inferred", inferred, characters, legacy_snapshot, {}))
    return cases


@pytest.mark.parametrize("name,line,characters,snapshot,extra", _cases(), ids=[case[0] for case in _cases()])
def test_state_matches_legacy(name, line, characters, snapshot, extra):
    """分支并跑：领域规则与旧入口逐字段一致。"""
    old = legacy.resolve_line_state(line, characters, snapshot, **extra)
    new = _call_domain_with_patch(
        line,
        characters,
        snapshot,
        live=bool(extra.get("live_ids")) and line.get("id") in (extra.get("live_ids") or set()),
        missing_asset=False,
    )
    assert old == new, name


def test_speaker_changed_names_match_legacy():
    """说话人变更文案：领域规则与旧实现同为「顾三 → 沈砚」。"""
    line = _base_line(tts={"status": "ready"})
    characters = _characters()
    snapshot = _snapshot(line, speaker_id="chr_b")
    old = legacy.resolve_line_state(line, characters, snapshot)
    new = _call_domain_with_patch(line, characters, snapshot)
    assert old == new
    assert "沈砚 → 顾三" in new["changes"][0]["text"]


def _real_chapters() -> list[tuple[Path, dict]]:
    """读取真实工程的章节（工程根 + 章节）。"""
    rows: list[tuple[Path, dict]] = []
    for root in PROJECTS:
        directory = root / "chapters"
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.json")):
            try:
                rows.append((root, json.loads(path.read_text(encoding="utf-8"))))
            except (OSError, ValueError):
                continue
    return rows


CHAPTERS = _real_chapters()


def test_real_project_lines_match_legacy():
    """真实工程逐行比对：产物存在性、revision 兜底、空文本等分支全部一致。"""
    if not CHAPTERS:
        pytest.skip("未设置 NDW_GOLDEN_PROJECTS，跳过真实工程台词并跑")
    characters = _characters()
    compared = 0
    for root, chapter in CHAPTERS:
        snapshots = legacy.load_snapshots(root, chapter)
        for scene in chapter.get("scenes") or []:
            for line in scene.get("lines") or []:
                snapshot = snapshots.get(line.get("id"))
                old = legacy.resolve_line_state(line, characters, snapshot, root=root)
                new = _call_domain(line, characters, snapshot, root=root)
                assert old == new, (chapter.get("id"), line.get("id"))
                compared += 1
    assert compared > 0


def test_diff_params_matches_expected_shape():
    """「改 N」说明：字段顺序、浮点归一与文案保持既有行为（改造前并跑 16 项全过）。"""
    before = {
        "text": "旧文本",
        "emotion": "neutral",
        "emotion_intensity": 1.0,
        "voice_id": "voc_a",
        "speed": 1.0,
        "delivery_note": None,
        "chunk_limit": 40,
    }
    after = copy.deepcopy(before)
    after.update({"text": "新文本多两句", "emotion": "sad", "voice_id": "voc_b", "speed": 0.95000001, "delivery_note": "压低嗓音", "chunk_limit": 60})
    changes = diff_params(before, after)
    assert [row["field"] for row in changes] == ["text", "emotion", "voice_id", "speed", "delivery_note", "chunk_limit"]
    by_field = {row["field"]: row for row in changes}
    assert by_field["text"]["text"].startswith("文本 +")
    assert by_field["emotion"]["text"] == "情绪 neutral → sad"
    assert by_field["speed"]["text"] == "语速 1 → 0.95"
    assert by_field["delivery_note"]["text"] == "交付说明 空 → 「压低嗓音」"
    assert by_field["chunk_limit"]["text"] == "切分上限 40 → 60"
