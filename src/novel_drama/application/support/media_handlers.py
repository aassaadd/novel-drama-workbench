"""媒体生成执行器：把登记表里的 12 个动作接到统一媒体总线上。

每个执行器都遵守同一份约定：
- 关键节点上报中文阶段与进度；
- 定期检查取消，取消时尽快返回 `canceled=True`，已落盘产物保留；
- 成功时写入产物相对路径；
- 失败抛异常，异常文案是中文可操作原因（总线会拼成「标题失败：…」）。

底层能力全部复用既有实现：配音走 `tts_run` / `tts_batch_run`，音色走
`tts.voice_design` / `tts.voice_batch`，音效与视觉走 `adapters.media`，
配乐走 `bgm_generate`，本模块只负责编排、进度与取消。
"""

from __future__ import annotations

import time
from pathlib import Path

from ...adapters.media import sfx as sfx_adapter
from ...adapters.persistence import ProjectStore
from ...domain.story.rules.chapter_rules import bump_revision
from ...infrastructure.config import AppConfig
from ...sfx import SfxError
from ...tts import voice_batch, voice_design
from ..production import bgm_generate, media_ops, tts_batch_run, tts_run
from ..production.timeline import build_chapter_timeline
from ..production.tts_errors import error_text
from ..production.voice_preview import synthesize_preview
from .media_bus import MediaJobHandle, register_handler, registered_handlers

#: 是否已注册过执行器（模块级幂等标记）
_REGISTERED = False

#: 轮询批次状态的间隔（秒）
_POLL_S = 0.5

#: 媒体类型 → (中文名, 量词)
_MEDIA_LABELS = {"sfx": ("音效", "条"), "image": ("图片", "张"), "video": ("视频", "条")}


def _require(params: dict, *keys: str) -> None:
    """校验必填参数。

    参数:
        params: 请求参数。
        *keys: 必填字段名。

    异常:
        ValueError: 有字段缺失或为空（中文列出缺哪些）。
    """
    missing = [key for key in keys if str(params.get(key) or "").strip() == ""]
    if missing:
        raise ValueError(f"缺少必填参数：{'、'.join(missing)}")


def _find_nested(chapter: dict, key: str, item_id: str) -> dict | None:
    """在章节的场景内嵌列表里按 ID 找条目。

    参数:
        chapter: 章节 dict。
        key: `sfx_events` / `key_images` / `video_segments`。
        item_id: 条目 ID。

    返回:
        条目 dict；找不到时返回 None。
    """
    for scene in chapter.get("scenes") or []:
        for item in scene.get(key) or []:
            if item.get("id") == item_id:
                return item
    return None


def _find_line(chapter: dict, line_id: str) -> dict | None:
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


def _rebuild_timeline(store: ProjectStore, chapter_id: str) -> bool:
    """录音 / 版本变化后重建章节时间轴。

    参数:
        store: 工程。
        chapter_id: 章节 ID。

    返回:
        True 表示重建成功；工程还没有时间轴或重建失败时为 False（不影响本次操作结果）。
    """
    if not store.timeline_path(chapter_id).exists():
        return False
    try:
        build_chapter_timeline(store, chapter_id)
        return True
    except Exception:  # noqa: BLE001 - 重建失败不阻断正在完成的录音/版本操作
        return False


def _tts_line(handle: MediaJobHandle, store: ProjectStore, config: AppConfig, params: dict) -> dict:
    """单行重录：revision +1 → 合成该行 → 校验产物 → 重建时间轴。

    参数:
        handle: 任务句柄。
        store: 工程。
        config: 本机配置。
        params: `{path, chapter, line}`。

    返回:
        `{message, line_id, revision, active, duration_ms, timeline_rebuilt}`；
        取消时返回 `{message, canceled: True}`。

    异常:
        ValueError: 缺参数或台词行不存在。
        RuntimeError: 合成失败（中文原因，旧版仍生效）。
    """
    handle.stage("载入")
    chapter_id = str(params.get("chapter") or "")
    line_id = str(params.get("line") or params.get("line_id") or "")
    if not chapter_id or not line_id:
        raise ValueError("缺少必填参数：chapter（章节 ID）与 line（台词行 ID）")
    chapter = store.load_chapter(chapter_id)
    target = _find_line(chapter, line_id)
    if target is None:
        raise ValueError(f"台词行不存在：{line_id}")
    previous = {"revision": int(target.get("revision") or 1), "tts": dict(target.get("tts") or {})}
    new_revision = previous["revision"] + 1
    target["revision"] = new_revision
    store.save_chapter(chapter)
    if handle.cancel_requested():
        target["revision"] = previous["revision"]
        target["tts"] = previous["tts"]
        store.save_chapter(chapter)
        return {"message": "已停止单行重录，未生成新音频", "canceled": True}

    handle.stage("合成语音")
    handle.progress(f"正在合成第 {new_revision} 版", 20)
    result = tts_run.run_chapter_tts(
        store,
        config,
        chapter_id,
        scope="selected",
        line_ids=[line_id],
        force=True,
        wait=True,
    )
    code = int((result or {}).get("exit_code") or 0)
    fresh = store.load_chapter(chapter_id)
    fresh_line = _find_line(fresh, line_id)
    tts = (fresh_line or {}).get("tts") or {}
    expected = f"assets/voice/lines/{line_id}_r{new_revision}.wav"
    ok = code == 0 and tts.get("status") == "ready" and tts.get("asset_id") == expected
    if not ok:
        # 失败：恢复旧 revision 与旧 tts 记录，保证「旧版仍生效」，同时把原因原样带回
        target["revision"] = previous["revision"]
        target["tts"] = previous["tts"]
        store.save_chapter(chapter)
        reason = error_text(tts.get("error"), f"重录进程退出码 {code}")
        raise RuntimeError(f"重录失败：{reason}（旧版 r{previous['revision']} 仍生效）")

    active = str(tts.get("asset_id") or expected)
    handle.artifacts([active])
    if handle.cancel_requested():
        return {"message": "已停止单行重录，新音频已保留", "canceled": True, "line_id": line_id, "revision": new_revision}
    handle.stage("写入产物")
    timeline_rebuilt = _rebuild_timeline(store, chapter_id)
    handle.progress("重录完成", 100, stage="完成")
    return {
        "message": f"已重录第 {new_revision} 版",
        "line_id": line_id,
        "revision": new_revision,
        "active": active,
        "duration_ms": tts.get("duration_ms"),
        "timeline_rebuilt": timeline_rebuilt,
    }


def _tts_batch(
    handle: MediaJobHandle,
    store: ProjectStore,
    config: AppConfig,
    params: dict,
    *,
    all_chapters: bool,
) -> dict:
    """批量配音（本章 / 全书）：用 `run_batch` 执行并把进度转成进度卡。

    参数:
        handle: 任务句柄。
        store: 工程。
        config: 本机配置。
        params: `{chapter?, force?, batch_size?, skip_unknown?}`。
        all_chapters: True 表示全书批量。

    返回:
        `{message, stats}`；取消时由总线落成已取消。

    异常:
        ValueError: 缺章节参数。
        RuntimeError: 有失败行（中文提示可逐条重录）。
    """
    handle.stage("载入")
    chapter = None if all_chapters else str(params.get("chapter") or "")
    if not all_chapters and not chapter:
        raise ValueError("缺少必填参数：chapter（章节 ID）")

    def on_progress(progress: dict) -> None:
        """把批次进度转成「当前章 + 完成/失败行数 + 百分比」。"""
        total = int(progress.get("total") or 0)
        done = int(progress.get("done") or 0)
        failed = int(progress.get("failed") or 0)
        shown = min(done, total) if total else done
        text = f"第 {shown}/{total} 行" if total else f"已完成 {done} 行"
        if failed:
            text += f"，失败 {failed} 行"
        current_chapter = str(progress.get("current_chapter") or "")
        if current_chapter:
            text += f"（当前章节 {current_chapter}）"
        handle.progress(text, (done / total * 100) if total else None, stage="逐行合成")

    def should_cancel() -> bool:
        """批次间检查取消与暂停标记。

        暂停时原地等待（阶段显示「已暂停」），点继续后自动恢复；
        用户取消时立即返回 True，由 run_batch 抛取消异常收尾。

        返回:
            True 表示用户已请求取消。
        """
        marker = store.root / ".ndw" / "tts.paused"
        while not handle.cancel_requested() and marker.exists():
            handle.stage("已暂停（在章节右栏点继续）")
            time.sleep(0.5)
        return handle.cancel_requested()

    options = tts_batch_run.BatchOptions(
        chapter=chapter,
        all_chapters=all_chapters,
        include_ready=bool(params.get("force")),
        skip_unknown_speakers=bool(params.get("skip_unknown", True)),
        batch_size=int(params.get("batch_size") or 5),
        human_summary=False,
        on_progress=on_progress,
        should_cancel=should_cancel,
    )
    handle.stage("逐章筛选" if all_chapters else "筛选台词")
    handle.progress("开始逐行合成", 0)
    code, report = tts_batch_run.run_batch(store, config, options)
    stats = dict((report or {}).get("stats") or {})
    reuse = (report or {}).get("reuse") or {}
    failed = int(stats.get("failed") or 0)
    if code != 0:
        raise RuntimeError(f"有 {failed} 行合成失败，可在章节页逐条重录")
    handle.stage("汇总结果")
    handle.progress("批量配音完成", 100, stage="完成")
    succeeded = int(stats.get("succeeded") or 0)
    reused = int(reuse.get("reused") or 0)
    skipped = int(stats.get("skipped") or 0)
    return {"message": f"已完成 {succeeded} 行（复用 {reused}，跳过 {skipped}）", "stats": stats}


def _tts_chapter(handle: MediaJobHandle, store: ProjectStore, config: AppConfig, params: dict) -> dict:
    """本章批量配音。

    参数:
        handle: 任务句柄。
        store: 工程。
        config: 本机配置。
        params: `{path, chapter, force?, batch_size?, skip_unknown?}`。

    返回:
        见 `_tts_batch`。
    """
    return _tts_batch(handle, store, config, params, all_chapters=False)


def _tts_book(handle: MediaJobHandle, store: ProjectStore, config: AppConfig, params: dict) -> dict:
    """全书批量配音。

    参数:
        handle: 任务句柄。
        store: 工程。
        config: 本机配置。
        params: `{path, force?, batch_size?, skip_unknown?}`。

    返回:
        见 `_tts_batch`。
    """
    return _tts_batch(handle, store, config, params, all_chapters=True)


def _voice_design(handle: MediaJobHandle, store: ProjectStore, config: AppConfig, params: dict) -> dict:
    """音色抽卡：校验环境与人物 → 创建批次 → 轮询候选 → 返回批次摘要。

    参数:
        handle: 任务句柄。
        store: 工程。
        config: 本机配置（读 `voice.design.*`）。
        params: `{character_id, prompt, count?, profile?}`。

    返回:
        `{message, batch}`；取消时返回停止摘要 `{message, canceled: True, batch}`。

    异常:
        ValueError: 功能未启用、环境/权重缺失、人物不存在或缺少描述。
        RuntimeError: 批次失败且没有任何候选。
    """
    handle.stage("准备音色描述")
    design = (config.get("voice.design") or {}) if config is not None else {}
    if not design.get("enabled", True):
        raise ValueError("音色设计未启用（voice.design.enabled=false）")
    python = str(design.get("python") or "")
    model_dir = str(design.get("model_dir") or "")
    if not Path(python).is_file():
        raise ValueError(f"VoxCPM2 运行环境不存在：{python}")
    if not Path(model_dir).is_dir():
        raise ValueError(f"VoxCPM2 权重目录不存在：{model_dir}")
    character_id = str(params.get("character_id") or "").strip()
    if not character_id:
        raise ValueError("缺少必填参数：character_id（角色 ID）")
    characters = store.load_characters().get("characters") or []
    character = next((item for item in characters if item.get("id") == character_id), None)
    if character is None:
        raise ValueError(f"人物不存在：{character_id}")
    prompt = str(params.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("缺少音色描述（prompt）")
    count = int(params.get("count") or design.get("candidates", 4))
    batch = voice_design.create_batch(store, config, character, prompt, count, params.get("profile"))
    batch_id = str(batch.get("batch_id") or "")
    handle.stage("抽卡生成")
    while True:
        if handle.cancel_requested():
            meta = voice_design.cancel_batch(store, batch_id) or {}
            done = len(meta.get("candidates") or [])
            return {"message": f"已停止音色抽卡，已生成 {done} 个候选（保留）", "canceled": True, "batch": meta}
        meta = voice_design.read_batch(store, batch_id) or {}
        candidates = meta.get("candidates") or []
        finished = len(candidates) + len(meta.get("failures") or [])
        total = int(meta.get("total") or count or 0)
        handle.progress(
            f"抽卡 {finished}/{total} · {character.get('name') or '角色'}",
            (finished / total * 100) if total else None,
        )
        if str(meta.get("status") or "") != "running":
            break
        time.sleep(_POLL_S)
    handle.stage("保存候选")
    if str(meta.get("status") or "") == "failed" and not candidates:
        raise RuntimeError(meta.get("error") or "音色抽卡失败，未生成候选（查看候选目录 worker.log）")
    folder = voice_design.batch_dir(store, batch_id)
    artifacts = [
        str((folder / str(item.get("file"))).relative_to(store.root))
        for item in candidates
        if item.get("file") and (folder / str(item.get("file"))).exists()
    ]
    if artifacts:
        handle.artifacts(artifacts)
    handle.progress("抽卡完成", 100, stage="完成")
    return {"message": f"已生成 {len(candidates)} 个候选", "batch": meta}


def _voice_batch(handle: MediaJobHandle, store: ProjectStore, config: AppConfig, params: dict) -> dict:
    """批量生成音色：启动任务后轮询条目完成数，取消时停止当前角色。

    参数:
        handle: 任务句柄。
        store: 工程。
        config: 本机配置。
        params: `{scope?, chapter_id?, count?}`。

    返回:
        `{message, job}`；取消时返回停止摘要 `{message, canceled: True, job}`。

    异常:
        ValueError: 没有需要生成的角色等前置条件不满足。
        RuntimeError: 任务整体失败。
    """
    handle.stage("载入角色")
    job = voice_batch.start_job(
        store,
        config,
        str(params.get("scope") or "unbound"),
        params.get("chapter_id"),
        params.get("count"),
    )
    job_id = str(job.get("job_id") or "")
    handle.stage("逐角色抽卡")
    while True:
        if handle.cancel_requested():
            latest = voice_batch.cancel_job(store, job_id) or {}
            return {"message": latest.get("note") or "已停止批量生成音色", "canceled": True, "job": latest}
        latest = voice_batch.read_job(store, job_id) or {}
        items = latest.get("items") or []
        total = len(items)
        done = sum(1 for item in items if item.get("status") in ("ready", "failed", "skipped"))
        handle.progress(f"已完成 {done}/{total} 个角色", (done / total * 100) if total else None)
        if str(latest.get("status") or "") != "running":
            break
        time.sleep(_POLL_S)
    status = str(latest.get("status") or "")
    if status == "failed":
        raise RuntimeError(latest.get("note") or "批量生成音色失败，可到音色页重试失败角色")
    handle.progress("批量音色完成", 100, stage="完成")
    return {"message": latest.get("note") or "批量生成音色完成", "job": latest}


def _voice_preview(handle: MediaJobHandle, store: ProjectStore, config: AppConfig, params: dict) -> dict:
    """角色试听：用本书真实台词合成一段候选样音。

    参数:
        handle: 任务句柄。
        store: 工程。
        config: 本机配置。
        params: `{character_id, voice_id?, chapter?}`。

    返回:
        试听结果（含 `audio/text/duration_ms/...`）。

    异常:
        ValueError: 角色/音色不存在、没有台词、参考音失效或合成失败（中文原因）。
    """
    handle.stage("准备音色")
    result = synthesize_preview(store, config, params)
    audio = str(result.get("audio") or "")
    if audio:
        handle.artifacts([audio])
    handle.stage("写入试听")
    handle.progress("试听已生成", 100, stage="完成")
    return {"message": "已生成角色试听", **result}


def _sfx_item(handle: MediaJobHandle, store: ProjectStore, config: AppConfig, params: dict) -> dict:
    """单条音效：加载章节 → 生成音效 → 回写条目与章节版本。

    参数:
        handle: 任务句柄。
        store: 工程。
        config: 本机配置。
        params: `{chapter, sfx_id}`（兼容 `id`）。

    返回:
        `{message: "音效已生成", item}`。

    异常:
        ValueError: 缺参数或条目不存在。
        RuntimeError: provider 校验/生成失败（中文原因）。
    """
    handle.stage("准备提示词")
    chapter_id = str(params.get("chapter") or "")
    sfx_id = str(params.get("sfx_id") or params.get("id") or "")
    if not chapter_id or not sfx_id:
        raise ValueError("缺少必填参数：chapter（章节 ID）与 sfx_id（音效条目 ID）")
    try:
        chapter = store.load_chapter(chapter_id)
    except FileNotFoundError as exc:
        raise ValueError(f"章节不存在：{chapter_id}") from exc
    item = _find_nested(chapter, "sfx_events", sfx_id)
    if item is None:
        raise ValueError(f"章节里找不到音效条目：{sfx_id}")
    if handle.cancel_requested():
        return {"message": "已停止音效生成，未生成产物", "canceled": True}
    handle.stage("合成音效")
    handle.progress("生成中", 30)
    try:
        pid, provider = sfx_adapter.get_provider(config)
        produced = sfx_adapter.generate_sfx_item(store, config, item, provider=provider, provider_id=pid)
    except SfxError as exc:
        detail = exc.message + (f"（{exc.hint}）" if exc.hint else "")
        raise RuntimeError(detail) from exc
    bump_revision(chapter)
    store.save_chapter(chapter)
    asset_id = str(produced.get("asset_id") or "")
    if asset_id:
        handle.artifacts([asset_id])
    if handle.cancel_requested():
        return {"message": "已停止音效生成，已生成产物保留", "canceled": True}
    handle.stage("写入产物")
    handle.progress("音效已生成", 100, stage="完成")
    return {"message": "音效已生成", "item": produced}


def _media_kind(
    handle: MediaJobHandle,
    store: ProjectStore,
    config: AppConfig,
    params: dict,
    kind: str,
    item_ids: list[str] | None = None,
) -> dict:
    """执行一类媒体生成（音效 / 图片 / 视频）并汇总产物与结果。

    参数:
        handle: 任务句柄。
        store: 工程。
        config: 本机配置。
        params: `{chapter, force?}`。
        kind: `sfx` / `image` / `video`。
        item_ids: 只处理这些条目 ID；None 表示全部。

    返回:
        `{message, items}`。

    异常:
        ValueError: 缺章节参数或章节不存在。
        RuntimeError: 生成失败且没有任何产物（中文原因）。
    """
    handle.stage("载入")
    chapter_id = str(params.get("chapter") or "")
    if not chapter_id:
        raise ValueError("缺少必填参数：chapter（章节 ID）")
    name, unit = _MEDIA_LABELS[kind]
    if kind in ("image", "video"):
        handle.stage("排队：等待 GPU 锁")
        handle.stage("生成中")
    else:
        handle.stage("逐条生成")
    handle.progress(f"开始生成{name}", 0)
    try:
        result = media_ops.run_media_kind(
            store,
            config,
            chapter_id,
            kind,
            force=bool(params.get("force")),
            item_ids=item_ids,
        )
    except FileNotFoundError as exc:
        raise ValueError(f"章节不存在：{chapter_id}") from exc
    stats = dict(result or {})
    error = str(stats.get("error") or "")
    if error and not int(stats.get("ready") or 0):
        raise RuntimeError(error)
    items = stats.get("items") or []
    artifacts = [str(item.get("path")) for item in items if item.get("status") == "ready" and item.get("path")]
    if artifacts:
        handle.artifacts(artifacts)
    ready = int(stats.get("ready") or 0)
    skipped = int(stats.get("skipped") or 0)
    failed = int(stats.get("failed") or 0)
    if ready == 0 and failed:
        detail = error
        if not detail:
            detail = next(
                (str(item.get("error") or "") for item in items if item.get("status") == "failed"),
                "",
            )
        raise RuntimeError(f"全部 {failed} 项生成失败：{detail or '请检查 provider 与 GPU 服务是否可用'}")
    handle.progress(f"已生成 {ready} {unit}{name}", 100, stage="完成")
    return {
        "message": f"已生成 {ready} {unit}{name}（跳过 {skipped}，失败 {failed}）",
        "items": items,
        "ready": ready,
        "failed": failed,
        "skipped": skipped,
    }


def _sfx_chapter(handle: MediaJobHandle, store: ProjectStore, config: AppConfig, params: dict) -> dict:
    """本章音效批量生成。

    参数:
        handle: 任务句柄。
        store: 工程。
        config: 本机配置。
        params: `{path, chapter, force?}`。

    返回:
        见 `_media_kind`。
    """
    return _media_kind(handle, store, config, params, "sfx")


def _image_generate(handle: MediaJobHandle, store: ProjectStore, config: AppConfig, params: dict) -> dict:
    """本章配图生成（视觉串行由现有 GPU 锁保证）。

    参数:
        handle: 任务句柄。
        store: 工程。
        config: 本机配置。
        params: `{path, chapter, force?}`。

    返回:
        见 `_media_kind`。
    """
    return _media_kind(handle, store, config, params, "image")


def _video_generate(handle: MediaJobHandle, store: ProjectStore, config: AppConfig, params: dict) -> dict:
    """本章视频生成（视觉串行由现有 GPU 锁保证）。

    参数:
        handle: 任务句柄。
        store: 工程。
        config: 本机配置。
        params: `{path, chapter, force?}`。

    返回:
        见 `_media_kind`。
    """
    return _media_kind(handle, store, config, params, "video")


def _anim(handle: MediaJobHandle, store: ProjectStore, config: AppConfig, params: dict) -> dict:
    """图生视频：只处理本章 `mode == "i2v"` 的视频片段。

    参数:
        handle: 任务句柄。
        store: 工程。
        config: 本机配置。
        params: `{path, chapter, force?}`。

    返回:
        见 `_media_kind`。

    异常:
        ValueError: 本章没有图生视频条目。
    """
    handle.stage("排队：等待 GPU 锁")
    chapter_id = str(params.get("chapter") or "")
    if not chapter_id:
        raise ValueError("缺少必填参数：chapter（章节 ID）")
    try:
        chapter = store.load_chapter(chapter_id)
    except FileNotFoundError as exc:
        raise ValueError(f"章节不存在：{chapter_id}") from exc
    item_ids = [
        str(item["id"])
        for scene in chapter.get("scenes") or []
        for item in scene.get("video_segments") or []
        if item.get("mode") == "i2v" and item.get("id")
    ]
    if not item_ids:
        raise ValueError("本章没有图生视频条目：请先生成关键图并切分出图生视频片段")
    handle.stage("读取关键帧")
    handle.progress(f"本次 {len(item_ids)} 条图生视频", 5)
    return _media_kind(handle, store, config, params, "video", item_ids=item_ids)


def _bgm(handle: MediaJobHandle, store: ProjectStore, config: AppConfig, params: dict) -> dict:
    """配乐生成：调用 `bgm_generate.generate_bgm` 并回报产物。

    参数:
        handle: 任务句柄。
        store: 工程。
        config: 本机配置。
        params: `{chapter, prompt?, duration?, mode?, provider?, ...}`。

    返回:
        `{message, ...render_bgm 结果}`。

    异常:
        ValueError: 参数非法。
        RuntimeError: 上游生成失败（中文原因）。
    """
    handle.stage("规划配乐")
    result = bgm_generate.generate_bgm(store, config, params)
    audio = str(result.get("asset_id") or result.get("audio") or result.get("path") or "")
    if audio:
        handle.artifacts([audio])
    handle.stage("写入产物")
    handle.progress("配乐已生成", 100, stage="完成")
    duration = result.get("duration_s")
    message = f"已生成 {duration:g}s BGM" if isinstance(duration, (int, float)) else "配乐已生成"
    return {"message": message, **result}


_HANDLERS = {
    "tts.line": _tts_line,
    "tts.chapter": _tts_chapter,
    "tts.book": _tts_book,
    "voice.design": _voice_design,
    "voice.batch": _voice_batch,
    "voice.preview": _voice_preview,
    "sfx.item": _sfx_item,
    "sfx.chapter": _sfx_chapter,
    "bgm": _bgm,
    "image.generate": _image_generate,
    "video.generate": _video_generate,
    "anim": _anim,
}


def register_all() -> None:
    """为登记表里的全部媒体动作注册执行器（幂等）。

    返回:
        无。
    """
    global _REGISTERED
    if _REGISTERED:
        return
    existing = registered_handlers()
    for action, handler in _HANDLERS.items():
        if action in existing:
            continue
        register_handler(action, handler)
    _REGISTERED = True
