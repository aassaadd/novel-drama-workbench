"""配音批次的阻断原因汇总：逐章/逐行说明为什么还不能开跑。"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException

from novel_drama.composition import ProjectStore
from novel_drama.web.actions import fail_body, run

#: 单行重录串行锁：同一时间只允许一个重录，避免两个进程同时改同一章
_RERUN_LOCK = threading.Lock()

#: Casting 试听保留条数（超出后删最旧的）
PREVIEW_KEEP = 12

#: 批量进度 / pid / 日志文件名（都放在工程内 `.ndw/`）
PROGRESS_NAME = "tts_batch_progress.json"
BATCH_PID_NAME = "web-tts-batch.pid"
RERUN_PID_NAME = "web-tts.pid"


def _now() -> str:
    """当前 UTC 时间（ISO 8601），落进进度文件用。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _cli(config_path: str | None, *argv: str):
    """按 `serve --config` 的同一份配置执行 CLI。

    参数:
        config_path: 本机配置文件路径；为空时读默认配置。
        argv: 不含 `--config` 的参数串。

    返回:
        `actions.CliResult`。
    """
    if config_path and "--config" not in argv:
        return run("--config", config_path, *argv)
    return run(*argv)


def _respond(result):
    """把 CLI 结果转成 HTTP 响应；失败时抛带中文说明的 HTTPException。

    参数:
        result: `actions.CliResult`。

    返回:
        CLI 的 JSON 负载。

    异常:
        HTTPException: CLI 退出码非 0。
    """
    if result.code != 0:
        raise HTTPException(status_code=result.status, detail=fail_body(result))
    return result.payload if result.payload is not None else {"text": result.text}


def _store(path: str | None) -> ProjectStore:
    """定位工程，未初始化时返回 404。

    参数:
        path: 工程根目录。

    返回:
        ProjectStore。
    """
    if not path:
        raise HTTPException(status_code=400, detail="缺少 path 参数（工程目录）")
    store = ProjectStore(path)
    if not store.exists():
        raise HTTPException(status_code=404, detail=f"工程不存在: {store.root}")
    return store


def _find_line(store: ProjectStore, chapter_id: str, line_id: str) -> dict:
    """按章节 + 行 ID 取台词行。

    参数:
        store: 工程。
        chapter_id: 章节 ID。
        line_id: 台词行 ID。

    返回:
        台词行 dict。

    异常:
        HTTPException 404: 章节或台词行不存在。
    """
    try:
        chapter = store.load_chapter(chapter_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"章节不存在: {chapter_id}") from exc
    for scene in chapter.get("scenes") or []:
        for line in scene.get("lines") or []:
            if line.get("id") == line_id:
                return line
    raise HTTPException(status_code=404, detail=f"台词行不存在: {line_id}")


def _takes_index_path(store: ProjectStore) -> Path:
    """真人录音的元数据文件路径（记录哪一版是实录、时长等）。

    参数:
        store: 工程。

    返回:
        `.ndw/line_takes.json` 的绝对路径。
    """
    return store.root / ".ndw" / "line_takes.json"


def _load_takes(store: ProjectStore) -> dict:
    """读取真人录音元数据，文件缺失或损坏时返回空字典。

    参数:
        store: 工程。

    返回:
        `{行ID_r版本: {source, duration_ms, created_at}}`。
    """
    path = _takes_index_path(store)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_takes(store: ProjectStore, data: dict) -> None:
    """写回真人录音元数据（失败不影响主流程）。

    参数:
        store: 工程。
        data: 完整元数据字典。
    """
    path = _takes_index_path(store)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def _safe_duration(path: Path) -> int:
    """读取音频时长（毫秒），ffprobe 缺失或解析失败时返回 0。

    参数:
        path: 音频文件。

    返回:
        时长毫秒。
    """
    try:
        from novel_drama.composition import probe_duration_ms

        return int(probe_duration_ms(path) or 0)
    except Exception:  # noqa: BLE001 - 探测失败不应阻断版本列表
        return 0


def _take_params(path: Path) -> dict:
    """读取某一版音频的侧车参数（TTS 产物才有）。

    参数:
        path: 音频文件。

    返回:
        侧车里的 `params` 字典；没有侧车或解析失败时返回空字典。
    """
    sidecar = Path(str(path) + ".json")
    if not sidecar.is_file():
        return {}
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    params = data.get("params") if isinstance(data, dict) else None
    return params if isinstance(params, dict) else {}


def _line_versions(store: ProjectStore, line_id: str, active: str | None) -> list[dict]:
    """扫描某行已存在的所有音频版本。

    参数:
        store: 工程。
        line_id: 台词行 ID。
        active: 当前生效的相对路径（`line.tts.asset_id`）。

    返回:
        按 revision 倒序排列的版本列表，含来源（tts / human）与时长。
    """
    folder = store.root / "assets" / "voice" / "lines"
    if not folder.is_dir():
        return []
    takes = _load_takes(store)
    pattern = re.compile(rf"^{re.escape(line_id)}_r(\d+)\.wav$")
    rows: list[dict] = []
    for item in folder.iterdir():
        match = pattern.match(item.name)
        if not match or not item.is_file():
            continue
        revision = int(match.group(1))
        rel = str(item.relative_to(store.root))
        meta = takes.get(f"{line_id}_r{revision}") or {}
        rows.append(
            {
                "revision": revision,
                "path": rel,
                "size": item.stat().st_size,
                "active": bool(active) and rel == active,
                "source": str(meta.get("source") or "tts"),
                "duration_ms": int(meta.get("duration_ms") or _safe_duration(item)),
            }
        )
    rows.sort(key=lambda row: row["revision"], reverse=True)
    return rows


def _find_scene_line(chapter: dict, line_id: str) -> dict | None:
    """在章节里按 ID 找台词行。

    参数:
        chapter: 章节 dict。
        line_id: 台词行 ID。

    返回:
        台词行；找不到时返回 None。
    """
    for scene in chapter.get("scenes") or []:
        for item in scene.get("lines") or []:
            if item.get("id") == line_id:
                return item
    return None



def _error_text(error: dict | None, fallback: str = "") -> str:
    """把 provider / 批次错误翻译成中文可操作提示。

    参数:
        error: `line.tts.error` 或批次结果里的错误 dict。
        fallback: 没有错误 dict 时的兜底文案。

    返回:
        中文原因；不吞错。
    """
    if not error:
        return fallback or "合成失败，但未拿到具体原因（查看 .ndw/logs 下日志）"
    code = str(error.get("code") or "")
    message = str(error.get("message") or "").strip() or "未知错误"
    lower = message.lower()
    if code == "VOICE_MISSING" or "参考音频不存在" in message or "no such file" in lower or "not found" in lower:
        if "不存在" in message or "no such file" in lower or "not found" in lower:
            return f"参考音失效：{message}（请重新上传或重新选择该角色的音色）"
        return f"缺音色或参考音：{message}（检查角色绑定的音色是否有可用参考音频）"
    if code == "MODEL_MISSING":
        return f"模型未就绪：{message}"
    if code == "NOT_IMPLEMENTED":
        return f"当前音色 provider 未实现：{message}"
    return f"合成失败（{code or '未知错误'}）：{message}"


def _spawn(argv: list[str], cwd: Path, log_path: Path) -> subprocess.Popen:
    """以独立进程组启动一个 CLI 子进程。

    参数:
        argv: 完整命令行（含 python 解释器）。
        cwd: 工作目录（仓库 src 目录，保证能 import novel_drama）。
        log_path: 标准输出/错误写入的日志文件。

    返回:
        Popen 实例。
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = str(cwd) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("wb") as log:
        return subprocess.Popen(
            argv,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
        )


def _pid_alive(pid: int) -> bool:
    """判断 pid 是否仍存活（只做存在性检查，不判断身份）。

    参数:
        pid: 进程号。

    返回:
        True 表示存活。
    """
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def _cli_process(pid: int) -> str:
    """返回 pid 对应的命令行；不是本工程配音 CLI 时返回空串。

    说明:
        进程退出后若父进程未 wait，会成为僵尸并被 `os.kill(pid, 0)` 当成存活；
        这里再用 `ps` 核对命令行，避免僵尸 pid 把「补齐」永远挡在 409。

    参数:
        pid: 进程号。

    返回:
        `run` / `batch` 表示对应的配音命令；其他情况返回空串。
    """
    if not _pid_alive(pid):
        return ""
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    command = (result.stdout or "").strip()
    if "novel_drama.cli" not in command:
        return ""
    if "tts batch" in command:
        return "batch"
    if "tts run" in command:
        return "run"
    return ""


def _read_json(path: Path) -> dict | None:
    """读一份 JSON 文件；不存在或损坏时返回 None。"""
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _log_tail(path: Path, limit: int = 20) -> str:
    """读日志末尾若干行，供失败时在页面给出可读原因。"""
    if not path.is_file():
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-limit:])


def _sample_line(store: ProjectStore, character_id: str, prefer_chapter: str | None) -> tuple[dict, dict] | None:
    """给某个角色找一句已有台词（优先当前章节）。

    参数:
        store: 工程。
        character_id: 角色 ID。
        prefer_chapter: 优先章节 ID；为空时按章序找。

    返回:
        `(chapter, line)`；该角色没有任何台词时返回 None。
    """
    chapters = store.list_chapters()
    if prefer_chapter:
        chapters = sorted(chapters, key=lambda item: 0 if item.get("id") == prefer_chapter else 1)
    for item in chapters:
        try:
            chapter = store.load_chapter(item["id"])
        except FileNotFoundError:
            continue
        for scene in chapter.get("scenes") or []:
            for line in scene.get("lines") or []:
                if line.get("speaker_id") == character_id and (line.get("text") or "").strip():
                    return chapter, line
    return None


def _prune_previews(folder: Path, keep: int = PREVIEW_KEEP) -> int:
    """清理试听目录，只保留最近 `keep` 条音频（连同侧车）。

    参数:
        folder: 试听目录。
        keep: 保留条数。

    返回:
        删除的文件数。
    """
    if not folder.is_dir():
        return 0
    files = sorted(
        (item for item in folder.iterdir() if item.is_file() and item.suffix == ".wav"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    removed = 0
    for item in files[keep:]:
        for target in (item, Path(str(item) + ".json")):
            if target.exists():
                try:
                    target.unlink()
                    removed += 1
                except OSError:
                    pass
    return removed


#: 音色校验错误码 → 页面上给用户看的直白说法（不出现错误码等内部术语）


_CAST_BLOCKER_TEXT = {
    "VOICE_PROFILE_MISSING": {
        "label": "还没有音色描述",
        "detail": "人物档案里没有音色描述，系统无法为这个角色准备音色。",
        "action": "去补音色",
    },
    "VOICE_BINDING_UNRESOLVED": {
        "label": "还没有可用音色",
        "detail": "这个角色没有绑定能用的音色，相关台词配音时会失败。",
        "action": "去补音色",
    },
    "VOICE_VARIANT_NOT_READY": {
        "label": "音色还没准备好",
        "detail": "绑定的音色还没生成完成或已经不可用，需要换一个能用的音色。",
        "action": "去换音色",
    },
    "STOCK_SLOT_CONFLICT": {
        "label": "和别的角色共用了同一个龙套名额",
        "detail": "同一个龙套名额只能对应一个角色，否则两个角色会混用同一条音色。",
        "action": "去角色页处理",
    },
    "CHAPTER_OVERRIDE_FORBIDDEN": {
        "label": "单独换了音色",
        "detail": "只有旁白和龙套可以按章换音色，这个角色要跟随全局音色。",
        "action": "去调整音色",
    },
    "NARRATOR_SWITCH_ILLEGAL": {
        "label": "旁白换了音色但没登记切换点",
        "detail": "旁白换声要登记从哪一章开始切换，否则前面的章节也会跟着变。",
        "action": "去登记切换点",
    },
    "NARRATOR_DRIFT": {
        "label": "旁白音色前后不一致",
        "detail": "不同章节的旁白用了不同音色，但没有登记切换点，听感会突然变化。",
        "action": "去登记切换点",
    },
    "CHARACTER_VOICE_DRIFT": {
        "label": "在不同章节用了不同音色",
        "detail": "同一个角色的音色应当全剧统一，这里解析到了两种以上音色。",
        "action": "去核对音色",
    },
    "BGM_PLAN_MISSING": {
        "label": "有镜头没规划 BGM",
        "detail": "工程开启了严格 BGM 检查，有镜头没规划 BGM 会挡住本次配音。",
        "action": "去补 BGM",
        "target": "chapter",
    },
}


def _blocker(
    code: str,
    title: str,
    detail: str,
    action: dict,
    *,
    count: int = 0,
    unit: str = "处",
    chapters: list[str] | None = None,
    skippable: bool = False,
) -> dict:
    """构造一条阻断项（页面直接展示，不暴露内部错误码）。

    参数:
        code: 内部错误码，仅用于排查。
        title: 一行能看懂的结论（名称）。
        detail: 为什么阻断。
        action: 下一步 `{label, kind, chapter_id?, line_id?, character_id?, act?}`。
        count: 影响数量（台词行数 / 处数）。
        unit: 数量单位（「行」或「处」）。
        chapters: 涉及的章节标题。
        skippable: 是否可由用户选择跳过（如未指定说话人的行）。

    返回:
        阻断项 dict。
    """
    return {
        "code": code,
        "title": title,
        "detail": detail,
        "count": int(count or 0),
        "unit": unit,
        "chapters": list(dict.fromkeys([item for item in chapters or [] if item])),
        "action": action,
        "skippable": bool(skippable),
    }


def _chapter_labels(store: ProjectStore) -> dict[str, str]:
    """章节 ID → 页面可读的章节名（`第 3 章《标题》`）。"""
    labels = {}
    for item in store.list_chapters():
        index = item.get("index")
        title = item.get("title") or ""
        prefix = f"第 {index} 章" if index else "章节"
        labels[item.get("id")] = f"{prefix}《{title}》" if title else prefix
    return labels


def _split_blockers(store: ProjectStore, scope: str, chapter_id: str | None, lines_total: int) -> list[dict]:
    """「还没有切分台词」的阻断项：说明为什么没有可配音的行，并给出切分入口。

    参数:
        store: 工程。
        scope: `chapter` 或 `all`。
        chapter_id: scope=chapter 时的章节 ID。
        lines_total: 本次范围内的台词行数。

    返回:
        阻断项列表（有台词行时为空）。
    """
    if scope == "chapter" and chapter_id:
        try:
            chapter = store.load_chapter(chapter_id)
        except FileNotFoundError:
            return []
        lines = sum(
            1
            for scene in chapter.get("scenes") or []
            for line in scene.get("lines") or []
            if (line.get("text") or "").strip()
        )
        if lines:
            return []
        words = int(chapter.get("word_count") or 0)
        scenes = len(chapter.get("scenes") or [])
        if scenes:
            detail = f"正文 {words} 字、场景 {scenes} 个，但一句台词都没切出来，配音没有可处理的内容。"
        else:
            detail = f"正文 {words} 字，但场景和台词都还没切分，配音没有可处理的内容。"
        return [
            _blocker(
                "NO_LINES",
                "本章还没有切分出台词",
                detail,
                {"label": "去画本切分场景与台词", "kind": "chapter", "chapter_id": chapter_id, "act": "split"},
                chapters=[chapter.get("title") or ""],
            )
        ]
    if lines_total <= 0:
        return [
            _blocker(
                "NO_LINES",
                "所选章节都还没有切分出台词",
                "正文保存后会自动切分场景与台词；没有台词就没有可以配音的行。",
                {"label": "去小说页处理", "kind": "novel"},
            )
        ]
    return []


def _cast_blockers(
    store: ProjectStore,
    findings: list[dict],
    unknown_ids: set[str],
    labels: dict[str, str],
    fallback_label: str = "",
) -> list[dict]:
    """把音色/BGM 校验错误翻译成逐条、可操作的阻断项。

    同一角色 / 同一章节的同类问题会按行重复几十条，这里按「错误码 + 角色或章节」
    聚合，页面只出现一条带数量与章节的可读说明。

    参数:
        store: 工程。
        findings: `cast check` 的 findings。
        unknown_ids: 已由用户选择跳过的未识别说话人行 ID。
        labels: 章节 ID → 可读章节名。
        fallback_label: 角色级问题没有章节信息时，补上本次检查的章节名。

    返回:
        阻断项列表。
    """
    characters = {
        item.get("id"): item for item in (store.load_characters().get("characters") or [])
    }
    grouped: dict[tuple, dict] = {}
    for item in findings:
        if item.get("level") != "error" or item.get("line_id") in unknown_ids:
            continue
        code = item.get("code") or ""
        text = _CAST_BLOCKER_TEXT.get(code) or {
            "label": "需要处理",
            "detail": item.get("message") or "配音前的检查没有通过。",
            "action": "去角色页查看",
        }
        character_id = item.get("character_id")
        chapter_id = item.get("chapter_id")
        key = (code, character_id or chapter_id or "")
        bucket = grouped.setdefault(
            key,
            {
                "code": code,
                "text": text,
                "character_id": character_id,
                "chapter_id": chapter_id,
                "count": 0,
                "chapters": [],
            },
        )
        bucket["count"] += 1
        label = labels.get(chapter_id)
        if label:
            bucket["chapters"].append(label)

    result: list[dict] = []
    for bucket in grouped.values():
        text = bucket["text"]
        character_id = bucket["character_id"]
        chapter_id = bucket["chapter_id"]
        if character_id:
            name = (characters.get(character_id) or {}).get("name") or "未命名角色"
            title = f"角色「{name}」{text['label']}"
        else:
            title = f"{labels.get(chapter_id, '章节')}{text['label']}"
        if text.get("target") == "chapter" and chapter_id:
            action = {"label": text["action"], "kind": "chapter", "chapter_id": chapter_id}
        elif character_id:
            action = {"label": text["action"], "kind": "character", "character_id": character_id}
        else:
            action = {"label": text["action"], "kind": "characters"}
        result.append(
            _blocker(
                bucket["code"],
                title,
                text["detail"],
                action,
                count=bucket["count"],
                unit="行" if bucket["code"] in ("VOICE_BINDING_UNRESOLVED", "VOICE_VARIANT_NOT_READY") else "处",
                chapters=bucket["chapters"] or ([fallback_label] if fallback_label else []),
            )
        )
    return result


def _unknown_speaker_blocker(unknown_rows: list[dict], unknown_total: int) -> dict | None:
    """「说话人未识别」的阻断项：给出数量、章节与首个可定位的行。

    参数:
        unknown_rows: CLI 返回的未识别行明细。
        unknown_total: 未识别行数。

    返回:
        阻断项；没有未识别行时返回 None。
    """
    if not unknown_total:
        return None
    first = unknown_rows[0] if unknown_rows else {}
    chapters = [row.get("chapter_title") or "" for row in unknown_rows]
    if first.get("chapter_id"):
        action = {
            "label": "去指定说话人",
            "kind": "chapter",
            "chapter_id": first.get("chapter_id"),
            "line_id": first.get("line_id") or "",
        }
    else:
        action = {"label": "去画本指定说话人", "kind": "characters"}
    return _blocker(
        "UNKNOWN_SPEAKER",
        f"{unknown_total} 行台词没指定说话人",
        "这些台词无法确定用谁的音色；可以先指定说话人，或在补齐时选择跳过这些行。",
        action,
        count=unknown_total,
        unit="行",
        chapters=chapters,
        skippable=True,
    )


def _avg_ms_per_line(store: ProjectStore) -> int | None:
    """从最近的批次报告估算「单行平均合成耗时」。

    参数:
        store: 工程。

    返回:
        毫秒/行；没有可用历史时返回 None。
    """
    from novel_drama.tts.batch import history_avg_ms_per_line

    return history_avg_ms_per_line(store.root)
