"""配音页的补充能力：单行重录 / 版本对比 / Casting 试听 / 整本批量。

这些接口不重写业务规则：
- 重录仍然走 `tts run --force --scope selected --line`，只把 revision 先加一，保证旧音频不被覆盖；
- 批量仍然走 `tts batch --all-chapters`，进度由 CLI 写进度文件，页面只读不推算；
- Casting 试听直接调用 TtsService，产物只落在 `.ndw/cast_previews/`，不写章节、不入队、不进时间轴。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException

from ..config import load_config
from ..models.ids import now_iso
from ..store import ProjectStore
from .actions import fail_body, run

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


def _line_versions(store: ProjectStore, line_id: str, active: str | None) -> list[dict]:
    """扫描某行已存在的所有音频版本。

    参数:
        store: 工程。
        line_id: 台词行 ID。
        active: 当前生效的相对路径（`line.tts.asset_id`）。

    返回:
        按 revision 倒序排列的版本列表。
    """
    folder = store.root / "assets" / "voice" / "lines"
    if not folder.is_dir():
        return []
    pattern = re.compile(rf"^{re.escape(line_id)}_r(\d+)\.wav$")
    rows: list[dict] = []
    for item in folder.iterdir():
        match = pattern.match(item.name)
        if not match or not item.is_file():
            continue
        rel = str(item.relative_to(store.root))
        rows.append(
            {
                "revision": int(match.group(1)),
                "path": rel,
                "size": item.stat().st_size,
                "active": bool(active) and rel == active,
            }
        )
    rows.sort(key=lambda row: row["revision"], reverse=True)
    return rows


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


def _avg_ms_per_line(store: ProjectStore) -> int | None:
    """从最近的批次报告估算「单行平均合成耗时」。

    参数:
        store: 工程。

    返回:
        毫秒/行；没有可用历史时返回 None。
    """
    folder = store.root / ".ndw" / "tts_batches"
    if not folder.is_dir():
        return None
    reports = sorted(
        (item for item in folder.glob("*.json") if item.is_file()),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    for item in reports[:5]:
        data = _read_json(item) or {}
        stats = data.get("stats") or {}
        elapsed = int(data.get("elapsed_ms") or 0)
        # 失败行也会耗时，分母优先用成功数，避免均速被失败的快速冒烟拉低
        denom = int(stats.get("succeeded") or 0) or int(stats.get("requested") or 0)
        if elapsed > 0 and denom > 0:
            return max(1, round(elapsed / denom))
    return None


def register_voice_tools_routes(app, config_path: str | None = None) -> None:
    """注册单行重录 / 版本 / Casting 试听 / 补齐缺失接口。

    参数:
        app: FastAPI 实例。
        config_path: 本机配置文件路径（与 serve 同源）。

    返回:
        无。
    """

    @app.get("/api/ui/line/versions")
    def ui_line_versions(path: str, chapter: str, line: str) -> dict:
        """列出一行的全部录音版本，并标出当前生效版本。

        参数:
            path: 工程根目录。
            chapter: 章节 ID。
            line: 台词行 ID。

        返回:
            `{line_id, revision, active, versions: [...]}`
        """
        store = _store(path)
        row = _find_line(store, chapter, line)
        active = (row.get("tts") or {}).get("asset_id")
        versions = _line_versions(store, line, active)
        return {
            "line_id": line,
            "revision": int(row.get("revision") or 1),
            "active": active,
            "versions": versions,
        }

    @app.post("/api/ui/line/rerun")
    def ui_line_rerun(body: dict) -> dict:
        """只重录一行：revision 先加一（旧文件保留）→ 合成这一行 → 重建时间轴。

        参数:
            body: `{path, chapter, line}`。

        返回:
            `{ok, line_id, previous_revision, revision, active, duration_ms, versions, timeline_rebuilt}`

        异常:
            HTTPException 409: 已有重录在进行。
            HTTPException 500: 合成失败（中文原因，旧版本仍生效）。
        """
        path = body.get("path")
        chapter_id = body.get("chapter")
        line_id = body.get("line")
        if not line_id:
            raise HTTPException(status_code=400, detail="缺少 line 参数（台词行 ID）")
        store = _store(path)
        row = _find_line(store, chapter_id, line_id)

        if not _RERUN_LOCK.acquire(blocking=False):
            raise HTTPException(status_code=409, detail={"code": 1, "message": "已有重录任务在进行，请等它结束"})
        try:
            previous = {
                "revision": int(row.get("revision") or 1),
                "tts": dict(row.get("tts") or {}),
            }
            new_revision = previous["revision"] + 1
            chapter = store.load_chapter(chapter_id)
            target = None
            for scene in chapter.get("scenes") or []:
                for item in scene.get("lines") or []:
                    if item.get("id") == line_id:
                        target = item
                        break
                if target:
                    break
            if target is None:
                raise HTTPException(status_code=404, detail=f"台词行不存在: {line_id}")
            target["revision"] = new_revision
            store.save_chapter(chapter)

            log_dir = store.root / ".ndw" / "logs"
            log_path = log_dir / f"rerun-{line_id}-{int(time.time())}.log"
            src_dir = Path(__file__).resolve().parents[2]
            argv = [sys.executable, "-m", "novel_drama.cli", "--project", str(store.root)]
            if config_path:
                argv += ["--config", config_path]
            argv += [
                "tts",
                "run",
                "--chapter",
                chapter_id,
                "--force",
                "--scope",
                "selected",
                "--line",
                line_id,
                "--wait",
            ]
            pid_file = store.root / ".ndw" / RERUN_PID_NAME
            process = _spawn(argv, src_dir, log_path)
            pid_file.parent.mkdir(parents=True, exist_ok=True)
            pid_file.write_text(str(process.pid), encoding="utf-8")
            try:
                code = process.wait(timeout=900)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(process.pid), 15)
                except OSError:
                    pass
                code = -1
            finally:
                if pid_file.is_file():
                    try:
                        if int(pid_file.read_text(encoding="utf-8").strip()) == process.pid:
                            pid_file.unlink(missing_ok=True)
                    except (OSError, ValueError):
                        pass

            fresh = store.load_chapter(chapter_id)
            fresh_line = next(
                (item for scene in fresh.get("scenes") or [] for item in scene.get("lines") or [] if item.get("id") == line_id),
                None,
            )
            tts = (fresh_line or {}).get("tts") or {}
            expected = f"assets/voice/lines/{line_id}_r{new_revision}.wav"
            ok = code == 0 and tts.get("status") == "ready" and tts.get("asset_id") == expected
            if not ok:
                # 失败：恢复旧 revision 与旧 tts 记录，保证「旧版仍生效」，同时把原因原样带回
                if target is not None:
                    target["revision"] = previous["revision"]
                    target["tts"] = previous["tts"]
                    store.save_chapter(chapter)
                reason = _error_text(tts.get("error"), _log_tail(log_path) or f"重录进程退出码 {code}")
                raise HTTPException(
                    status_code=500,
                    detail={
                        "code": 5,
                        "message": f"重录失败：{reason}（旧版 r{previous['revision']} 仍生效）",
                    },
                )

            timeline_rebuilt = False
            if store.timeline_path(chapter_id).exists():
                timeline = _cli(config_path, "--project", str(store.root), "--json", "timeline", "build", "--chapter", chapter_id)
                timeline_rebuilt = timeline.code == 0
            active = tts.get("asset_id")
            return {
                "ok": True,
                "line_id": line_id,
                "previous_revision": previous["revision"],
                "revision": new_revision,
                "active": active,
                "duration_ms": tts.get("duration_ms"),
                "timeline_rebuilt": timeline_rebuilt,
                "versions": _line_versions(store, line_id, active),
            }
        finally:
            _RERUN_LOCK.release()

    @app.post("/api/ui/cast/preview")
    def ui_cast_preview(body: dict) -> dict:
        """用本书真实台词试听某个角色的候选音色。

        参数:
            body: `{path, character_id, voice_id?, chapter?}`。

        返回:
            `{audio, text, duration_ms, voice_id, voice_name, variant_id, chapter_id, line_id, kept}`

        异常:
            HTTPException 400: 角色没有台词，或候选音色没有可用参考音频。
            HTTPException 500: 合成失败（中文原因）。
        """
        from ..tts.errors import TtsError
        from ..tts.service import TtsService
        from ..tts.types import Requirement, VoiceRef
        from ..store.voice_store import find_voice, pick_variant, variant_ref, resolve_for_character

        store = _store(body.get("path"))
        character_id = str(body.get("character_id") or "").strip()
        if not character_id:
            raise HTTPException(status_code=400, detail="缺少 character_id 参数")
        characters = store.load_characters().get("characters") or []
        character = next((item for item in characters if item.get("id") == character_id), None)
        if character is None:
            raise HTTPException(status_code=404, detail=f"角色不存在: {character_id}")
        sample = _sample_line(store, character_id, body.get("chapter"))
        if sample is None:
            raise HTTPException(status_code=400, detail="这个角色还没有已有台词，无法用本书台词试听")
        chapter, line = sample

        voice_id = str(body.get("voice_id") or "").strip()
        if voice_id:
            table = store.load_voices()
            voice = find_voice(table, voice_id)
            if voice is None:
                raise HTTPException(status_code=404, detail=f"音色不存在: {voice_id}")
            variant = pick_variant(voice, line.get("emotion"), require_reference=True)
            if variant is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"音色「{voice.get('name') or voice_id}」没有可用参考音频（需要 ready 状态且带参考音）",
                )
            ref_audio, ref_text = variant_ref(store, variant)
            resolved = {
                "voice_id": voice.get("id"),
                "variant_id": variant.get("id"),
                "ref_audio": ref_audio,
                "ref_text": ref_text,
                "source": variant.get("source"),
            }
        else:
            resolved = resolve_for_character(store, {"characters": characters}, character_id, line.get("emotion"))
            if not resolved.get("ref_audio"):
                raise HTTPException(
                    status_code=400,
                    detail="这个角色当前没有可用音色参考音频（先在音色库绑定并准备参考音）",
                )
            table = store.load_voices()
            voice = find_voice(table, resolved.get("voice_id") or "")
            voice_id = (voice or {}).get("id") or resolved.get("voice_id") or "current"
        if not resolved.get("ref_audio") or not Path(str(resolved["ref_audio"])).exists():
            raise HTTPException(
                status_code=400,
                detail=f"参考音失效：{resolved.get('ref_audio')}（请重新上传或重新选择音色）",
            )

        folder = store.root / ".ndw" / "cast_previews"
        folder.mkdir(parents=True, exist_ok=True)
        safe_voice = re.sub(r"[^A-Za-z0-9_-]+", "_", str(voice_id))[:40] or "voice"
        out_path = folder / f"{character_id}_{safe_voice}_{int(time.time() * 1000)}.wav"
        requirement = Requirement(
            text=line["text"],
            emotion=line.get("emotion"),
            emotion_intensity=max(0, min(3, round(float(line.get("emotion_intensity") or 0) * 3))),
            clone_required=True,
            character_id=character_id,
            line_id=None,
        )
        voice_ref = VoiceRef(
            variant_id=resolved.get("variant_id"),
            ref_audio=resolved.get("ref_audio"),
            ref_text=resolved.get("ref_text"),
            source=resolved.get("source") or "preset",
        )
        service = TtsService(load_config(config_path) if config_path else load_config(), store.root)
        try:
            payload = service.synthesize(line["text"], out_path, requirement=requirement, voice=voice_ref)
        except TtsError as exc:
            raise HTTPException(status_code=500, detail={"code": 5, "message": _error_text(exc.to_dict())}) from exc
        finally:
            try:
                service.close()
            except Exception:  # noqa: BLE001, S110 - 收尾失败不覆盖上面的成功/失败结果
                pass
        _prune_previews(folder)
        return {
            "audio": str(out_path.relative_to(store.root)),
            "text": line["text"],
            "duration_ms": payload.get("duration_ms"),
            "voice_id": voice_id,
            "voice_name": (voice or {}).get("name") or voice_id,
            "variant_id": resolved.get("variant_id"),
            "chapter_id": chapter.get("id"),
            "chapter_title": chapter.get("title"),
            "line_id": line.get("id"),
            "kept": PREVIEW_KEEP,
        }

    @app.post("/api/ui/cast/preview/clear")
    def ui_cast_preview_clear(body: dict) -> dict:
        """清空 Casting 试听临时目录。"""
        store = _store(body.get("path"))
        folder = store.root / ".ndw" / "cast_previews"
        removed = 0
        if folder.is_dir():
            for item in folder.iterdir():
                if item.is_file():
                    try:
                        item.unlink()
                        removed += 1
                    except OSError:
                        pass
        return {"removed": removed}

    @app.get("/api/ui/tts/batch-all/plan")
    def ui_batch_all_plan(path: str, scope: str = "chapter", chapter: str | None = None) -> dict:
        """补齐缺失的预计条数、跳过数与前置检查。

        说明:
            只跑缺失/失败/过期行，已有音频一律跳过；scope=chapter 时只看本章，
            scope=all 才跨章（底层仍是 `tts batch --all-chapters`，页面不叫全书重跑）。

        参数:
            path: 工程根目录。
            scope: `chapter`（默认）或 `all`。
            chapter: scope=chapter 时的章节 ID。

        返回:
            `{scope, chapter, total, skipped, lines_total, eta_ms, avg_ms_per_line,
              chapters, per_chapter, blockers, ready, ready_reason}`。
        """
        store = _store(path)
        if scope == "all":
            target_args = ["--all-chapters"]
        else:
            if not chapter:
                raise HTTPException(status_code=400, detail="补齐本章需要 chapter 参数")
            target_args = ["--chapter", chapter]
        result = _cli(
            config_path,
            "--project",
            str(store.root),
            "--json",
            "tts",
            "batch",
            *target_args,
            "--dry-run",
            "--skip-cast-check",
        )
        plan = result.payload if isinstance(result.payload, dict) else {}
        total = int(plan.get("rows") or 0)
        avg = _avg_ms_per_line(store)
        providers = _cli(config_path, "--json", "tts", "providers")
        rows: list[dict] = []
        if providers.code == 0 and isinstance(providers.payload, dict):
            rows = providers.payload.get("providers") or []
        usable = [item for item in rows if item.get("enabled") and item.get("available")]
        ready = bool(usable)
        ready_reason = ""
        if not ready:
            ready_reason = "没有可用的 TTS provider（先运行 ndw models list / verify 确认权重就位）"

        cast = _cli(config_path, "--project", str(store.root), "--json", "cast", "check")
        findings: list[dict] = []
        if isinstance(cast.payload, dict):
            findings = cast.payload.get("findings") or []
        blockers = [
            f"{item.get('code')}: {item.get('message')}"
            for item in findings
            if item.get("level") == "error"
        ]
        return {
            "scope": scope,
            "chapter": chapter,
            "total": total,
            "skipped": int(plan.get("skipped") or 0),
            "lines_total": int(plan.get("lines_total") or 0),
            "eta_ms": total * avg if avg and total else 0,
            "avg_ms_per_line": avg,
            "chapters": plan.get("chapters") or [],
            "per_chapter": plan.get("per_chapter") or [],
            "blockers": blockers,
            "ready": ready,
            "ready_reason": ready_reason,
        }

    @app.post("/api/ui/tts/batch-all")
    def ui_batch_all_start(body: dict) -> dict:
        """启动「补齐缺失」（后台进程），只补未完成行，天然支持中断续跑。

        参数:
            body: `{path, scope?, chapter?, batch_size?}`。

        返回:
            `{started, pid, log, total}`。

        异常:
            HTTPException 409: 已有配音进程在运行。
            HTTPException 412: provider 不可用或有阻断性音色编排错误。
        """
        store = _store(body.get("path"))
        scope = "all" if body.get("scope") == "all" else "chapter"
        chapter = body.get("chapter")
        plan = ui_batch_all_plan(str(store.root), scope, chapter)
        if not plan["ready"]:
            raise HTTPException(status_code=412, detail={"code": 4, "message": plan["ready_reason"]})
        if plan["blockers"]:
            raise HTTPException(
                status_code=412,
                detail={
                    "code": 4,
                    "message": "音色/BGM 编排有阻断项，已取消补齐：\n- " + "\n- ".join(plan["blockers"][:8]),
                },
            )
        if plan["total"] <= 0:
            raise HTTPException(status_code=400, detail={"code": 2, "message": "当前范围没有缺失台词，无需补齐"})

        pid_file = store.root / ".ndw" / BATCH_PID_NAME
        if pid_file.is_file():
            try:
                old_pid = int(pid_file.read_text(encoding="utf-8").strip())
            except ValueError:
                old_pid = 0
            if _cli_process(old_pid) == "batch":
                raise HTTPException(status_code=409, detail={"code": 1, "message": "已有补齐任务在运行"})
        legacy_pid = store.root / ".ndw" / RERUN_PID_NAME
        if legacy_pid.is_file():
            try:
                legacy = int(legacy_pid.read_text(encoding="utf-8").strip())
            except ValueError:
                legacy = 0
            if _cli_process(legacy) == "run":
                raise HTTPException(status_code=409, detail={"code": 1, "message": "已有配音进程在运行，请先等它结束"})

        progress_file = store.root / ".ndw" / PROGRESS_NAME
        if progress_file.is_file():
            progress_file.unlink(missing_ok=True)
        log_dir = store.root / ".ndw" / "logs"
        log_path = log_dir / f"batch-all-{int(time.time())}.log"
        src_dir = Path(__file__).resolve().parents[2]
        argv = [sys.executable, "-m", "novel_drama.cli", "--project", str(store.root)]
        if config_path:
            argv += ["--config", config_path]
        argv += ["tts", "batch"]
        argv += ["--all-chapters"] if scope == "all" else ["--chapter", str(chapter)]
        batch_size = body.get("batch_size")
        if batch_size:
            argv += ["--batch-size", str(int(batch_size))]
        argv += ["--progress-file", str(progress_file)]
        process = _spawn(argv, src_dir, log_path)
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text(str(process.pid), encoding="utf-8")
        (store.root / ".ndw" / "tts_batch_meta.json").write_text(
            json.dumps(
                {"pid": process.pid, "log": str(log_path), "started_at": _now(), "total": plan["total"]},
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        return {"started": True, "pid": process.pid, "log": str(log_path), "total": plan["total"]}

    @app.get("/api/ui/tts/batch-all/progress")
    def ui_batch_all_progress(path: str) -> dict:
        """读取整本批量进度与失败行清单。

        参数:
            path: 工程根目录。

        返回:
            `{running, done, total, failed, finished, rows, chapter, elapsed_ms, eta_ms, log_tail, report_path}`。
        """
        store = _store(path)
        folder = store.root / ".ndw"
        progress = _read_json(folder / PROGRESS_NAME) or {}
        meta = _read_json(folder / "tts_batch_meta.json") or {}
        pid = int(meta.get("pid") or progress.get("pid") or 0)
        # 进程可能还在收尾（回收常驻 worker），但进度已标 finished：此时不再算「运行中」
        running = _cli_process(pid) == "batch" and not progress.get("finished")
        total = int(progress.get("total") or meta.get("total") or 0)
        done = int(progress.get("done") or 0)
        failed = int(progress.get("failed") or 0)
        started = progress.get("started_at") or meta.get("started_at")
        elapsed = 0
        if started:
            try:
                begin = datetime.strptime(started, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                elapsed = int((datetime.now(timezone.utc) - begin).total_seconds() * 1000)
            except ValueError:
                elapsed = 0
        eta = 0
        if running and done > 0 and total > done:
            eta = int(elapsed / done * (total - done))
        finished = bool(progress.get("finished")) or (not running and bool(progress))
        # 中途被停止/崩溃：进度还在、没标 finished，但进程已经没了
        interrupted = bool(progress) and not running and not bool(progress.get("finished"))
        log_path = Path(str(meta.get("log") or "")) if meta.get("log") else None
        return {
            "running": running,
            "done": min(done, total) if total else done,
            "total": total,
            "failed": failed,
            "finished": finished,
            "interrupted": interrupted,
            "rows": progress.get("failed_rows") or [],
            "current_chapter": progress.get("current_chapter"),
            "elapsed_ms": elapsed,
            "eta_ms": eta,
            "log_tail": "" if running else _log_tail(log_path) if log_path else "",
            "report_path": progress.get("report_path"),
        }
