"""章节正文生成的断点存档。

一章中途取消 / 失败 / 服务重启时，已经写完的轮次不再作废：
每写完一轮就把「已写正文 + 轮次指针 + 版本指针」写入
`.ndw/checkpoints/<章节ID>.chapter.json`，下次生成本章时自动从这里接上。

存档只保存生成过程的中间态，不写章节文件；正文落盘成功后由调用方删除。
校验口径与一次跑完保持一致：章节版本、节拍分组、每轮节拍数、目标字数
任一变化都视为旧断点失效，直接作废并从头生成（并给出可读原因）。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from ...adapters.persistence import ProjectStore
from ...models.ids import now_iso

#: 存档结构版本：字段不兼容时递增，旧版本按失效处理
STATE_VERSION = 1


def checkpoint_path(store: ProjectStore, chapter_id: str) -> Path:
    """返回章节正文断点存档的文件路径。

    参数:
        store: 工程。
        chapter_id: 章节 ID。

    返回:
        `.ndw/checkpoints/<章节ID>.chapter.json` 的 Path。
    """
    return store.root / ".ndw" / "checkpoints" / f"{chapter_id}.chapter.json"


def _write(path: Path, data: dict) -> None:
    """原子写入断点存档（先写临时文件再替换，避免中途退出留下半截 JSON）。

    参数:
        path: 目标文件。
        data: 存档内容。

    返回:
        无。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def load(store: ProjectStore, chapter_id: str) -> dict | None:
    """读取章节正文断点存档。

    参数:
        store: 工程。
        chapter_id: 章节 ID。

    返回:
        存档 dict；不存在、损坏或版本不兼容时返回 None。
    """
    path = checkpoint_path(store, chapter_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("chapter_id") != chapter_id:
        return None
    if int(data.get("state_version") or 0) != STATE_VERSION:
        return None
    if not str(data.get("content") or "").strip():
        return None
    try:
        rounds_done = int(data.get("rounds_done") or 0)
    except (TypeError, ValueError):
        return None
    if rounds_done < 1:
        return None
    data["rounds_done"] = rounds_done
    return data


def save(store: ProjectStore, state: dict) -> str:
    """写入章节正文断点存档（每轮写完后调用）。

    参数:
        store: 工程。
        state: 存档内容；`chapter_id` 必填。

    返回:
        存档文件路径字符串。
    """
    payload = dict(state)
    payload["state_version"] = STATE_VERSION
    payload["updated_at"] = now_iso()
    payload.setdefault("created_at", payload["updated_at"])
    path = checkpoint_path(store, str(payload["chapter_id"]))
    _write(path, payload)
    return str(path)


def mark(store: ProjectStore, chapter_id: str, status: str, error: str | None = None) -> dict | None:
    """更新已有断点的状态（取消 / 失败），不改动已完成的轮次内容。

    参数:
        store: 工程。
        chapter_id: 章节 ID。
        status: running / canceled / failed。
        error: 中文原因（可为空）。

    返回:
        更新后的存档；没有断点时返回 None。
    """
    state = load(store, chapter_id)
    if state is None:
        return None
    state["status"] = status
    if error:
        state["error"] = error
    save(store, state)
    return state


def clear(store: ProjectStore, chapter_id: str) -> None:
    """删除章节正文断点存档（正文与摘要都落盘成功后调用）。

    参数:
        store: 工程。
        chapter_id: 章节 ID。

    返回:
        无。
    """
    path = checkpoint_path(store, chapter_id)
    if path.exists():
        path.unlink()


def fingerprint(signature: dict) -> str:
    """计算生成上下文的指纹（用于判断断点是否还能复用）。

    参数:
        signature: `{base_revision, chapter_words, total_beats, per_round, beats}`。

    返回:
        sha256 十六进制字符串。
    """
    raw = json.dumps(signature, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def mismatch_reason(state: dict, signature: dict) -> str | None:
    """判断断点与当前生成上下文是否一致。

    参数:
        state: 已读取的断点存档。
        signature: 本次生成刚算出的上下文签名。

    返回:
        不一致时的中文原因；一致时返回 None。
    """
    stored = state.get("signature") or {}
    if not isinstance(stored, dict):
        return "断点缺少上下文签名"
    if fingerprint(stored) != fingerprint(signature):
        # 逐项给出最具体的原因，避免只报「指纹不一致」
        if int(stored.get("base_revision") or 0) != int(signature.get("base_revision") or 0):
            return (
                f"章节版本已变化（断点基于 revision {stored.get('base_revision')}，"
                f"当前 revision {signature.get('base_revision')}）"
            )
        if int(stored.get("chapter_words") or 0) != int(signature.get("chapter_words") or 0):
            return f"本章目标字数已变化（{stored.get('chapter_words')} → {signature.get('chapter_words')}）"
        if int(stored.get("total_beats") or 0) != int(signature.get("total_beats") or 0):
            return f"本章节拍数已变化（{stored.get('total_beats')} → {signature.get('total_beats')}）"
        if int(stored.get("per_round") or 0) != int(signature.get("per_round") or 0):
            return f"每轮节拍数已变化（{stored.get('per_round')} → {signature.get('per_round')}）"
        return "本章节拍内容已变化"
    return None


def resume_info(state: dict, path: str | None = None) -> dict:
    """把断点整理成结果里可直接展示的「续写来源」信息。

    参数:
        state: 断点存档。
        path: 断点文件路径；None 时省略。

    返回:
        `{resumed, from_round, reused_rounds, status, updated_at, checkpoint}`。
    """
    rounds_done = int(state.get("rounds_done") or 0)
    info = {
        "resumed": True,
        "from_round": rounds_done + 1,
        "reused_rounds": rounds_done,
        "status": str(state.get("status") or ""),
        "updated_at": str(state.get("updated_at") or ""),
    }
    if path:
        info["checkpoint"] = path
    return info
