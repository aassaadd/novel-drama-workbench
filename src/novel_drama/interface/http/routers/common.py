"""路由层共用后台/绑定辅助：跨多个路由模块使用，集中一份实现。"""

from __future__ import annotations

import logging
import os
import re
import signal
import subprocess
import threading
import time

from fastapi import HTTPException

from novel_drama.application.production.job_tracker import UiJob
from novel_drama.composition import ProjectStore, QueueStore
from novel_drama.interface.http.cli_bridge import fail_body
from novel_drama.models.ids import now_iso

logger = logging.getLogger(__name__)

_ACTIVE_TTS_PROJECTS: set[str] = set()


def _require(body: dict, usage: str, **labels: str) -> None:
    """校验请求体必填字段，缺失时按「参数错误」返回 400。

    参数:
        body: 请求体。
        usage: 本接口需要哪些参数的说明，直接拼进错误消息。
        **labels: `字段名=中文名`，如 `path="工程目录"`。

    返回:
        无。

    异常:
        HTTPException: 任一字段缺失或为空字符串时返回 400（中文说明缺哪个、接口要什么）。
    """
    missing = [
        f"{name}（{label}）"
        for name, label in labels.items()
        if body.get(name) is None or str(body.get(name)).strip() == ""
    ]
    if missing:
        raise HTTPException(
            status_code=400,
            detail={
                "code": 2,
                "message": f"缺少必填参数：{'、'.join(missing)}；{usage}",
            },
        )

def _business_error(result):
    """把新接口的 CLI 失败结果转成中文说明 + 分级状态码。

    参数:
        result: `actions.CliResult`。

    返回:
        带 detail 的 HTTPException（409 引用冲突 / 404 不存在 / 422 校验失败）。

    异常:
        无。
    """
    body = fail_body(result)
    payload = result.payload if isinstance(result.payload, dict) else {}
    raw_message = str(payload.get("message") or body.get("message") or "")
    message = re.sub(r"^\[[A-Z_]+\]\s*", "", raw_message)
    body = {**body, "message": message}
    if result.code == 1:
        detail = dict(body)
        if payload.get("chapters"):
            detail["chapters"] = payload["chapters"]
        return HTTPException(status_code=409, detail=detail)
    if result.code == 4 and "不存在" in message:
        return HTTPException(status_code=404, detail=body)
    if result.code in (5, 6):
        return HTTPException(status_code=422, detail=body)
    return HTTPException(status_code=result.status, detail=body)

def _is_tts_process(pid: int) -> bool:
    """判断 pid 是否为本工程启动的配音消费进程。

    说明:
        pid 文件可能在进程自然退出后残留；系统复用 pid 时直接 killpg 会误杀无关进程，
        因此先用 ps 确认命令行里确实是 `novel_drama.cli ... tts run` 或 `tts batch`。

    参数:
        pid: 待确认的进程号。

    返回:
        True 表示可以安全终止。
    """
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    command = (result.stdout or "").strip()
    return "novel_drama.cli" in command and ("tts run" in command or "tts batch" in command)

def _resolve_voice_binding(binding: dict | None, index: dict) -> str | None:
    """把人物绑定解析成音色库里的音色 ID。

    参数:
        binding: `character.voice_binding`。
        index: `_voice_binding_index` 生成的索引。

    返回:
        匹配到的音色 ID；无法解析时返回 None。
    """
    if not binding:
        return None
    for key in ("voice_library_id", "voice_id", "default_variant_id"):
        raw = binding.get(key)
        if not raw:
            continue
        resolved = index.get(str(raw))
        if resolved:
            return resolved
    return None

def _respond(result):
    """把 CLI 结果转成 HTTP 响应。

    参数:
        result: `actions.CliResult`。

    返回:
        CLI 的 JSON 负载。

    异常:
        HTTPException: CLI 退出码非 0 时按映射返回状态码与中文说明。
    """
    if result.code != 0:
        raise HTTPException(status_code=result.status, detail=fail_body(result))
    return result.payload if result.payload is not None else {"text": result.text}

def _respond_business(result):
    """成功返回 JSON 负载，失败按业务语义映射状态码。

    参数:
        result: `actions.CliResult`。

    返回:
        CLI 的 JSON 负载。

    异常:
        HTTPException: 退出码非 0。
    """
    if result.code != 0:
        raise _business_error(result)
    return result.payload if result.payload is not None else {"text": result.text}

def _stop_tts_project(path: str) -> dict:
    """终止某工程的后台配音批次，并把未完成任务标记为已取消。

    参数:
        path: 工程目录。

    返回:
        {stopped 是否确实停下, killed 是否终止了进程, canceled 被取消的任务数}。
    """
    store = ProjectStore(path)
    killed = False
    # 兼容两种入口：逐行/队列合成写 web-tts.pid，整本批量写 web-tts-batch.pid
    for name in ("web-tts.pid", "web-tts-batch.pid"):
        pid_file = store.root / ".ndw" / name
        if not pid_file.is_file():
            continue
        pid = 0
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
        except ValueError:
            pid = 0
        if pid and _is_tts_process(pid):
            try:
                # 配音 CLI 用 start_new_session 独立进程组，killpg 会同时打到常驻 worker
                os.killpg(os.getpgid(pid), signal.SIGTERM)
                killed = True
            except (ProcessLookupError, PermissionError):
                killed = False
            # 进程收到 SIGTERM 立刻退出；稍等再改队列，避免和它的写入互相覆盖
            for _ in range(20):
                try:
                    os.kill(pid, 0)
                except (ProcessLookupError, PermissionError):
                    break
                time.sleep(0.1)
        pid_file.unlink(missing_ok=True)
    queue = QueueStore(store)
    jobs = queue.load()
    canceled = 0
    for job in jobs:
        if job.get("status") in ("queued", "running"):
            job["status"] = "canceled"
            job["finished_at"] = now_iso()
            job["error"] = {"code": "CANCELED", "message": "用户停止生成"}
            canceled += 1
    if canceled:
        queue.save(jobs)
    return {"stopped": killed or canceled > 0, "killed": killed, "canceled": canceled}

def _voice_binding_index(voices: list[dict]) -> dict:
    """建立「绑定值 → 音色 ID」索引，兼容旧字段、变体 ID 与旧版内置名称。

    参数:
        voices: 工程 `voices.json` 里的音色列表。

    返回:
        字典：音色 ID / 变体 ID / 音色名称 / 旧版内置名称 都映射到音色 ID。
    """
    from novel_drama.tts.builtin_voices import LEGACY_NAME_MAP

    index: dict[str, str] = {}
    names: dict[str, str] = {}
    for voice in voices:
        voice_id = str(voice.get("id") or "")
        if not voice_id:
            continue
        index[voice_id] = voice_id
        name = str(voice.get("name") or "")
        if name:
            names[name] = voice_id
            index[name] = voice_id
        for variant in voice.get("variants") or []:
            variant_id = str(variant.get("id") or "")
            if variant_id:
                index[variant_id] = voice_id
    # 旧版内置音色重命名后，旧名称仍可能留在 characters.json 里
    for old_name, new_name in LEGACY_NAME_MAP.items():
        target = names.get(new_name)
        if target:
            index.setdefault(old_name, target)
    return index

def _watch_background_job(store, kind: str, title: str, reader) -> None:
    """把后台音色/编排任务的进度同步到任务队列（轮询任务文件）。

    参数:
        store: 工程。
        kind: 任务类型（voice / voice.fill / produce）。
        title: 任务标题。
        reader: `reader() -> dict`，返回后台任务快照（含 status / progress / stats）。

    返回:
        无（后台线程随 Web 进程退出）。
    """
    ui = UiJob(store, kind, {"label": title}, title)
    ui.start("已提交生成请求")

    def watch() -> None:
        """每 2 秒同步一次，结束后写成功 / 失败。"""
        deadline = time.time() + 6 * 3600
        while time.time() < deadline:
            time.sleep(2)
            try:
                latest = reader() or {}
            except Exception:  # noqa: BLE001 - 读取失败继续等下一轮
                continue
            status = str(latest.get("status") or "")
            if status in ("running", "queued", "pending"):
                progress = latest.get("progress") or {}
                index = int(progress.get("index") or 0)
                total = int(progress.get("total") or 0)
                stage = str(latest.get("stage") or "生成中")
                if total:
                    ui.progress(f"{stage} · {index}/{total}", index / total * 100)
                else:
                    ui.progress(stage)
                continue
            if status in ("ready", "succeeded", "finished", "done"):
                ui.finish(str(latest.get("note") or "生成完成"))
            elif status:
                ui.fail(str(latest.get("note") or latest.get("error") or "生成未完成"))
            else:
                continue
            return
        ui.fail("任务超时未完成")

    threading.Thread(target=watch, name=f"ndw-job-{kind}", daemon=True).start()
