"""全链路编排：`ndw produce`。

一条命令从设定库跑到交付包，阶段顺序见 `STAGES`。每个阶段的执行结果写入
`.ndw/runs/<run_id>.json`，`--resume` 会跳过已成功的阶段，便于失败后从断点继续。

设计要点：
- 阶段之间只通过落盘产物与章节数据传递，不共享内存状态，可单阶段重跑；
- 失败即停（默认），保留已完成阶段的产物与「失败阶段 + 原始错误」；
- `--dry-run` 只体检前置条件，不写任何文件。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from ..config import AppConfig
from ..media import concat_voice, export_bundle, plan_concat, plan_mix, plan_mux, render_mix, run_media
from ..media.comfy_media import load_manifest, templates_dir
from ..media.mux import mux as run_mux
from ..media.timeline_builder import build_timeline, summarize_timeline
from ..models.ids import new_id, now_iso
from ..store import ProjectStore, validate
from ..store.settings_store import SettingsStore
from ..writing import apply_outline, generate_chapter, generate_outline, suggest_script, summarize_chapter

# 阶段顺序（也是依赖顺序）
STAGES = (
    "setting",
    "outline",
    "chapter",
    "scenes",
    "lines",
    "script",
    "tts",
    "sfx",
    "image",
    "video",
    "timeline",
    "concat",
    "mix",
    "mux",
    "export",
)
# 阶段中文名（CLI/Web 展示用）
STAGE_LABELS = {
    "setting": "设定库",
    "outline": "大纲",
    "chapter": "章节正文",
    "scenes": "场景切分",
    "lines": "台词切分",
    "script": "导演标注",
    "tts": "配音合成",
    "sfx": "音效",
    "image": "配图",
    "video": "视频位",
    "timeline": "时间轴",
    "concat": "配音拼接",
    "mix": "混音",
    "mux": "混流",
    "export": "导出",
}

#: 工程级阶段：不依赖章节，整个工程只跑一次（设定库、大纲）
PROJECT_STAGES = ("setting", "outline")
#: 章节级阶段：依赖章节；未指定章节时按全书逐章执行
CHAPTER_STAGES = tuple(stage for stage in STAGES if stage not in PROJECT_STAGES)


def stages_needing_chapter(from_stage: str, to_stage: str) -> list[str]:
    """列出区间内需要章节的阶段。

    参数:
        from_stage: 起始阶段 key。
        to_stage: 结束阶段 key。

    返回:
        阶段 key 列表（按执行顺序）；区间非法时返回空列表。
    """
    if from_stage not in STAGES or to_stage not in STAGES:
        return []
    index_from, index_to = STAGES.index(from_stage), STAGES.index(to_stage)
    return [stage for stage in STAGES[index_from : index_to + 1] if stage in CHAPTER_STAGES]


def runs_dir(store: ProjectStore) -> Path:
    """编排记录目录。

    参数:
        store: 工程。

    返回:
        `.ndw/runs` 路径。
    """
    path = store.root / ".ndw" / "runs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_run(store: ProjectStore, run: dict) -> Path:
    """写入编排记录（原子替换，避免并发读到写了一半的 JSON）。

    参数:
        store: 工程。
        run: 记录 dict。

    返回:
        记录文件路径。
    """
    run["updated_at"] = now_iso()
    path = runs_dir(store) / f"{run['run_id']}.json"
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    tmp.write_text(json.dumps(run, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def load_run(store: ProjectStore, run_id: str) -> dict:
    """读取编排记录。

    参数:
        store: 工程。
        run_id: 记录 ID。

    返回:
        记录 dict。

    异常:
        FileNotFoundError: 记录不存在。
    """
    path = runs_dir(store) / f"{run_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"编排记录不存在: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def latest_run(store: ProjectStore) -> dict | None:
    """取最近一次编排记录。

    参数:
        store: 工程。

    返回:
        记录 dict；没有记录时返回 None。
    """
    files = sorted(runs_dir(store).glob("run_*.json"), key=lambda item: item.stat().st_mtime)
    # 从新到旧找第一份能解析的记录：避免读到正在写的临时内容或损坏文件直接抛错
    for item in reversed(files):
        try:
            return json.loads(item.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
    return None


def _artifact(kind: str, path: str | Path) -> dict:
    """构造阶段产物记录。

    参数:
        kind: 产物类型。
        path: 路径。

    返回:
        {kind, path}。
    """
    return {"kind": kind, "path": str(path)}


# ---------------- 各阶段 ----------------


def _stage_setting(ctx: dict) -> tuple[list[dict], str]:
    """确保设定库存在。"""
    bible = SettingsStore(ctx["store"].root).ensure()
    return [_artifact("json", ctx["store"].root / "settings" / "bible.json")], f"设定库 r{bible.get('settings_revision')}"


def _stage_outline(ctx: dict) -> tuple[list[dict], str]:
    """大纲存在则补齐章节，不存在则先生成再落地。"""
    store = ctx["store"]
    settings = SettingsStore(store.root)
    bible = settings.ensure()
    if not settings.all_outline_chapters(bible):
        result = generate_outline(store, ctx["config"], acts=3, chapters_per_act=3, beats_per_chapter=3)
        bible = settings.load()
        note = f"已生成 {result['chapters']} 章大纲"
    else:
        note = f"沿用已有大纲 {len(settings.all_outline_chapters(bible))} 章"
    applied = apply_outline(store)
    return [_artifact("json", store.root / "settings" / "bible.json")], f"{note}，落地新建 {len(applied['created'])} 章"


def _stage_chapter(ctx: dict) -> tuple[list[dict], str]:
    """确保章节有正文，没有则生成，生成成功后自动反向总结大纲与目录。

    参数:
        ctx: 编排上下文，含 store / config / chapter_id / force。

    返回:
        (阶段产物列表, 阶段说明)；反向总结的告警追加在说明末尾，不影响阶段成功状态。
    """
    store = ctx["store"]
    chapter = store.load_chapter(ctx["chapter_id"])
    if (chapter.get("content") or "").strip() and not ctx["force"]:
        return [], f"沿用已有正文 {chapter.get('word_count') or 0} 字"
    result = generate_chapter(store, ctx["config"], ctx["chapter_id"], regen=True)
    note = f"生成正文 {result['words']} 字"
    # 正文已落盘，以正文为准反写章节标题/摘要/大纲与设定库节拍；失败只告警，不阻断后续阶段
    try:
        summary = summarize_chapter(store, ctx["config"], chapter["id"], apply=True)
        changed = summary.get("changed_fields") or []
        note += f"，已同步大纲与目录（{'、'.join(changed)}）" if changed else "，大纲与目录无变化"
        warnings = list(summary.get("warnings") or [])
    except Exception as exc:  # 任意异常都不应让整体编排失败
        warnings = [f"反向总结失败：{type(exc).__name__}: {exc}"]
    if warnings:
        note += "；告警：" + "；".join(warnings)
    return [_artifact("json", store.chapter_path(ctx["chapter_id"]))], note


def _stage_scenes(ctx: dict) -> list[dict]:
    """切分场景（已有场景时跳过）。"""
    from .pipeline import split_chapter_scenes

    store = ctx["store"]
    chapter = store.load_chapter(ctx["chapter_id"])
    if chapter.get("scenes") and not ctx["force"]:
        return [], f"沿用已有场景 {len(chapter['scenes'])} 个"
    created = split_chapter_scenes(store, chapter, force=ctx["force"])
    return [_artifact("json", store.chapter_path(ctx["chapter_id"]))], f"切分场景 {len(created)} 个"


def _stage_lines(ctx: dict) -> list[dict]:
    """切分台词（已有台词时跳过）。"""
    from .pipeline import split_scene_lines

    store = ctx["store"]
    chapter = store.load_chapter(ctx["chapter_id"])
    total = sum(len(scene.get("lines") or []) for scene in chapter.get("scenes") or [])
    if total and not ctx["force"]:
        return [], f"沿用已有台词 {total} 行"
    characters = store.load_characters()["characters"]
    result = split_scene_lines(store, chapter, characters, force=ctx["force"])
    return [_artifact("json", store.chapter_path(ctx["chapter_id"]))], f"切分台词 {result.get('lines', 0)} 行（未识别 {result.get('unknown', 0)}）"


def _stage_script(ctx: dict) -> list[dict]:
    """生成导演标注并物化到场景（已有脚本时跳过）。"""
    from ..cli_voice import _apply_script_to_chapter

    store = ctx["store"]
    path = store.script_path(ctx["chapter_id"])
    if path.exists() and not ctx["force"]:
        return [_artifact("json", path)], "沿用已有导演脚本"
    result = suggest_script(store, ctx["config"], ctx["chapter_id"])
    markup_path = store.root / "scripts" / f"{ctx['chapter_id']}.markup.txt"
    markup_path.parent.mkdir(parents=True, exist_ok=True)
    markup_path.write_text(result["markup"] + "\n", encoding="utf-8")
    script = result["script"]
    validate(script, "director-script.schema.json")
    store.save_script(script)
    chapter = store.load_chapter(ctx["chapter_id"])
    summary = _apply_script_to_chapter(store, chapter, script)
    return (
        [_artifact("json", path), _artifact("text", markup_path)],
        f"标注 {result['shots']} 镜头 / 台词 {result['lines']} 行，音效 {summary['sfx_added']} 条、画面 {summary['images_added']} 张、视频位 {summary['videos_added']} 段",
    )


def _stage_tts(ctx: dict) -> list[dict]:
    """批量配音（只补未就绪行）。"""
    from ..cli_common import _tts_service
    from ..tts.batch import run_chapters

    store = ctx["store"]
    chapter = store.load_chapter(ctx["chapter_id"])
    service = _tts_service(_Namespace(project=str(store.root)), ctx["config"])
    try:
        report = run_chapters(
            service,
            store,
            [chapter],
            batch_size=int(ctx["config"].get("tts.batch_size", 5)),
            dry_run=False,
            scene_ids=None,
            include_ready=False,
            limit=None,
            line_ids=None,
        )
    finally:
        # 阶段结束回收常驻 worker；后续媒体阶段不需要它
        service.close()
    stats = report.get("stats") or {}
    if int(stats.get("failed") or 0):
        raise RuntimeError(f"配音有 {stats['failed']} 行失败（成功 {stats.get('succeeded', 0)} 行），详见 {report.get('report_path')}")
    return [_artifact("json", store.chapter_path(ctx["chapter_id"]))], f"合成 {stats.get('succeeded', 0)} 行"


def _media_stage(kind: str):
    """构造某类媒体的阶段函数。

    参数:
        kind: sfx/image/video。

    返回:
        阶段函数。
    """

    def _runner(ctx: dict) -> tuple[list[dict], str]:
        """执行媒体阶段。"""
        store = ctx["store"]
        chapter = store.load_chapter(ctx["chapter_id"])
        result = run_media(store, ctx["config"], chapter, kind, force=ctx["force"])
        artifacts = [_artifact(kind, item["path"]) for item in result["items"] if item.get("path")]
        return artifacts, f"就绪 {result['ready']} / 跳过 {result['skipped']} / 失败 {result['failed']}"

    return _runner


def _timeline_state(store: ProjectStore, chapter_id: str) -> dict:
    """读取时间轴（不存在则构建）。

    参数:
        store: 工程。
        chapter_id: 章节 ID。

    返回:
        时间轴 dict。
    """
    from ..cli_media import _silence_events

    path = store.timeline_path(chapter_id)
    chapter = store.load_chapter(chapter_id)
    existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    timeline = build_timeline(chapter, store.settings(), existing=existing, silence_events=_silence_events(store, chapter_id))
    timeline["built_at"] = now_iso()
    validate(timeline, "timeline.schema.json")
    store._write_json(path, timeline)
    return timeline


def _stage_timeline(ctx: dict) -> tuple[list[dict], str]:
    """构建时间轴。"""
    timeline = _timeline_state(ctx["store"], ctx["chapter_id"])
    return [_artifact("json", ctx["store"].timeline_path(ctx["chapter_id"]))], summarize_timeline(timeline)


def _stage_concat(ctx: dict) -> tuple[list[dict], str]:
    """拼接配音轨。"""
    store = ctx["store"]
    timeline = json.loads(store.timeline_path(ctx["chapter_id"]).read_text(encoding="utf-8"))
    plan = plan_concat(timeline, store.root)
    if plan.get("missing"):
        raise RuntimeError(f"{len(plan['missing'])} 条配音产物缺失：{plan['missing'][:3]}")
    result = concat_voice(plan, binary=ctx["config"].get("ffmpeg.binary", "ffmpeg"))
    return [_artifact("wav", result["path"])], f"{result['duration_ms']}ms"


def _stage_mix(ctx: dict) -> tuple[list[dict], str]:
    """混音与响度规整。"""
    store = ctx["store"]
    timeline = json.loads(store.timeline_path(ctx["chapter_id"]).read_text(encoding="utf-8"))
    plan = plan_mix(timeline, store.root)
    result = render_mix(
        plan,
        binary=ctx["config"].get("ffmpeg.binary", "ffmpeg"),
        audio_cfg=ctx["config"].get("audio"),
        loudness_target=ctx["config"].get("output.loudness_target_lufs", -16),
    )
    return (
        [_artifact("wav", result["normalized"]["path"])] + [_artifact("wav", stem["path"]) for stem in result["stems"]],
        f"峰值 {result['mix']['peak_db']}dBFS，规整后 {result['normalized']['after'].get('lufs')}LUFS",
    )


def _stage_mux(ctx: dict) -> tuple[list[dict], str]:
    """音画混流。"""
    store = ctx["store"]
    timeline = json.loads(store.timeline_path(ctx["chapter_id"]).read_text(encoding="utf-8"))
    plan = plan_mux(
        timeline,
        store.root,
        plan_mix(timeline, store.root),
        settings=store.settings(),
        manifest=load_manifest(templates_dir(ctx["config"])),
    )
    plan["binary"] = ctx["config"].get("ffmpeg.binary", "ffmpeg")
    if plan["blockers"]:
        raise RuntimeError("；".join(plan["blockers"]))
    result = run_mux(plan)
    return [_artifact("mp4", result["path"])], f"{result['duration_ms']}ms / {result['clips']} 个片段"


def _stage_export(ctx: dict) -> tuple[list[dict], str]:
    """导出交付包。"""
    from ..cli_media import _silence_events

    store = ctx["store"]
    chapter = store.load_chapter(ctx["chapter_id"])
    timeline = json.loads(store.timeline_path(ctx["chapter_id"]).read_text(encoding="utf-8"))
    characters = {item["id"]: item for item in store.load_characters()["characters"]}
    result = export_bundle(
        store,
        chapter,
        timeline,
        characters,
        _silence_events(store, ctx["chapter_id"]),
        binary=ctx["config"].get("ffmpeg.binary", "ffmpeg"),
    )
    artifacts = [_artifact(item["kind"], item["path"]) for item in result["artifacts"]]
    artifacts.append(_artifact("json", result["bundle"]))
    artifacts.append(_artifact("text", result["readme"]))
    return artifacts, f"产物 {len(result['artifacts'])} 个" + (f"，缺失 {len(result['missing'])} 项" if result["missing"] else "")


# 阶段 → 执行函数
STAGE_RUNNERS = {
    "setting": _stage_setting,
    "outline": _stage_outline,
    "chapter": _stage_chapter,
    "scenes": _stage_scenes,
    "lines": _stage_lines,
    "script": _stage_script,
    "tts": _stage_tts,
    "sfx": _media_stage("sfx"),
    "image": _media_stage("image"),
    "video": _media_stage("video"),
    "timeline": _stage_timeline,
    "concat": _stage_concat,
    "mix": _stage_mix,
    "mux": _stage_mux,
    "export": _stage_export,
}


class _Namespace:
    """最小参数壳：给复用 CLI 内部函数提供 args 形态。"""

    def __init__(self, **kwargs):
        """按关键字挂属性。"""
        self.__dict__.update(kwargs)


def produce(
    store: ProjectStore,
    config: AppConfig,
    chapter_id: str = "",
    from_stage: str = "setting",
    to_stage: str = "export",
    resume: bool = True,
    dry_run: bool = False,
    force: bool = False,
    keep_going: bool = False,
) -> dict:
    """执行全链路编排。

    工程级阶段（设定库、大纲）只跑一次；章节级阶段在 chapter_id 为空时
    按全书逐章执行，指定 chapter_id 时只跑该章。

    参数:
        store: 工程。
        config: 本机配置。
        chapter_id: 目标章节；为空表示全书（章节级阶段逐章执行）。
        from_stage: 起始阶段（含）。
        to_stage: 结束阶段（含）。
        resume: 跳过上一次已成功的阶段。
        dry_run: 只体检不执行。
        force: 强制重跑已有产物（覆盖正文、媒体、脚本）。
        keep_going: 阶段失败后是否继续后续阶段。

    返回:
        {"run_id", "status", "stages": [...], "path"}。

    异常:
        ValueError: 阶段名非法、区间不成立、需要章节但工程没有章节。
    """
    if from_stage not in STAGES:
        raise ValueError(f"未知阶段: {from_stage}（可选 {', '.join(STAGES)}）")
    if to_stage not in STAGES:
        raise ValueError(f"未知阶段: {to_stage}（可选 {', '.join(STAGES)}）")
    index_from, index_to = STAGES.index(from_stage), STAGES.index(to_stage)
    if index_from > index_to:
        raise ValueError(f"--from {from_stage} 在 --to {to_stage} 之后，顺序不成立")

    selected = STAGES[index_from : index_to + 1]
    need_chapters = [stage for stage in selected if stage in CHAPTER_STAGES]
    if need_chapters and chapter_id:
        store.load_chapter(chapter_id)  # 章节不存在直接报错
    elif need_chapters and not store.list_chapters():
        labels = "、".join(STAGE_LABELS.get(stage, stage) for stage in need_chapters)
        raise ValueError(
            f"所选区间包含需要章节的阶段：{labels}。工程还没有章节：请先落地章节或导入正文，"
            "或只选择工程级阶段（设定库 / 大纲）"
        )

    # 章节目标延迟解析：大纲阶段可能新建章节，等真正跑到章节级阶段再取全书章节
    chapter_targets: list[str] | None = None
    chapter_titles: dict[str, str] = {}

    def resolve_chapters() -> list[str]:
        """返回章节级阶段要遍历的章节 ID（结果缓存）。"""
        nonlocal chapter_targets
        if chapter_targets is None:
            if chapter_id:
                chapter_targets = [chapter_id]
                chapter_titles[chapter_id] = store.load_chapter(chapter_id).get("title") or chapter_id[-6:]
            else:
                rows = store.list_chapters()
                chapter_targets = [row["id"] for row in rows]
                chapter_titles.update(
                    {row["id"]: row.get("title") or f"第 {row.get('index')} 章" for row in rows}
                )
        return chapter_targets

    previous = latest_run(store) if resume else None
    done = {
        (stage.get("stage"), stage.get("chapter_id") or "")
        for stage in (previous or {}).get("stages", [])
        if stage.get("status") == "succeeded"
    }

    run = {
        "run_id": new_id("run"),
        "chapter_id": chapter_id,
        "scope": "project" if not need_chapters else ("chapter" if chapter_id else "book"),
        "chapters": [],
        "started_at": now_iso(),
        "from_stage": from_stage,
        "to_stage": to_stage,
        "dry_run": dry_run,
        "stages": [],
        "status": "running",
    }
    # 先落一条 running 记录：Web 端在首个阶段结束前也能看到「正在跑哪一段」
    save_run(store, run)
    ctx = {"store": store, "config": config, "chapter_id": "", "force": force}
    aborted = False

    for stage in selected:
        targets = [""] if stage in PROJECT_STAGES else resolve_chapters()
        multi = len(targets) > 1
        base_label = STAGE_LABELS.get(stage, stage)
        if stage not in PROJECT_STAGES:
            run["chapters"] = list(targets)
        for target in targets:
            title = chapter_titles.get(target) or ""
            record = {
                "stage": stage,
                "label": f"{base_label} · {title}" if multi and title else base_label,
                "chapter_id": target,
                "status": "pending",
                "started_at": now_iso(),
                "finished_at": None,
                "artifacts": [],
                "note": "",
                "error": None,
            }
            if dry_run:
                record["note"] = "dry-run：未执行"
                record["status"] = "skipped"
                run["stages"].append(record)
                continue
            if aborted:
                record["status"] = "skipped"
                record["note"] = "前序阶段失败"
                run["stages"].append(record)
                continue
            if resume and (stage, target) in done:
                record["status"] = "skipped"
                record["note"] = "续跑：上次已成功"
                run["stages"].append(record)
                continue
            ctx["chapter_id"] = target
            try:
                artifacts, note = STAGE_RUNNERS[stage](ctx)
                record.update({"status": "succeeded", "artifacts": artifacts, "note": note})
            except Exception as exc:  # 记录原始错误，按策略决定是否继续
                record.update({"status": "failed", "error": f"{type(exc).__name__}: {exc}"})
                aborted = not keep_going
            record["finished_at"] = now_iso()
            run["stages"].append(record)
            save_run(store, run)

    failed = [item for item in run["stages"] if item["status"] == "failed"]
    run["status"] = "failed" if failed else ("dry_run" if dry_run else "succeeded")
    run["finished_at"] = now_iso()
    path = save_run(store, run)
    return {"run_id": run["run_id"], "status": run["status"], "stages": run["stages"], "path": str(path)}
