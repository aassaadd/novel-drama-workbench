"""流水线步骤：把「数据操作」与「模型任务」分开。

- 数据步骤（拆场景、切台词、建时间轴）本轮即可运行；
- 模型步骤（配音、音效、配图、视频、混音、导出）只负责「规划 + 入队」，
  真正执行由队列里的适配器完成，本轮适配器均为未对接实现。
"""

from __future__ import annotations

import copy

from ..adapters.base import not_implemented
from ..models import entities
from ..store import ProjectStore
from ..text.splitter import split_lines, split_scenes
from ..tts.line_state import resolve_line_state
from .jobs import build_idempotency_key
from .resplit import merge_manual_lines


def split_chapter_scenes(
    store: ProjectStore,
    chapter: dict,
    max_chars: int = 1200,
    force: bool = False,
    dry_run: bool = False,
    keep_lines: bool = False,
) -> list[dict]:
    """把章节正文拆成场景并落盘（不切台词）。

    参数:
        store: 工程读写入口。
        chapter: 章节 dict。
        max_chars: 单场景软上限。
        force: 已有场景时是否覆盖。
        dry_run: 只返回草案，不写入。
        keep_lines: 保留模式；新场景正文与旧场景完全一致时，沿用旧场景（含台词与媒体引用）。

    返回:
        场景草案列表 [{title, content}] 或写入后的场景列表。

    异常:
        ValueError: 已有场景且未指定 force。
    """
    drafts = split_scenes(chapter.get("content") or "", max_chars=max_chars)
    if dry_run:
        return drafts
    if chapter.get("scenes") and not force:
        raise ValueError("章节已有场景，若确认覆盖请加 --force")

    # 保留模式：按正文完全相同匹配旧场景，命中就整块沿用（场景/台词 ID 不变，时间轴与配音仍有效）
    pool = sorted(chapter.get("scenes") or [], key=lambda item: item.get("index") or 0) if keep_lines else []
    scenes = []
    for index, draft in enumerate(drafts):
        reused = None
        for at, old in enumerate(pool):
            if (old.get("source_text") or "").strip() == draft["content"].strip():
                reused = pool.pop(at)
                break
        if reused is not None:
            scene = copy.deepcopy(reused)
            scene["index"] = index
            scene["title"] = scene.get("title") or draft["title"]
            scene["source_text"] = draft["content"]
            for line_index, line in enumerate(scene.get("lines") or []):
                line["index"] = line_index
                line["scene_id"] = scene["id"]
            scenes.append(scene)
            continue
        scene = entities.new_scene(chapter["id"], index, draft["title"])
        scene["source_text"] = draft["content"]
        scenes.append(scene)

    chapter["scenes"] = scenes
    chapter["status"] = "split"
    chapter["revision"] = int(chapter.get("revision") or 1) + 1
    store.save_chapter(chapter)
    return scenes


def split_scene_lines(
    store: ProjectStore,
    chapter: dict,
    characters: list[dict],
    scene_ids: list[str] | None = None,
    force: bool = False,
    dry_run: bool = False,
    keep_manual: bool = False,
) -> dict:
    """把场景正文切成台词行。

    参数:
        store: 工程读写入口。
        chapter: 章节 dict。
        characters: 角色表。
        scene_ids: 指定场景；为空则处理全部场景。
        force: 已有台词行时是否覆盖。
        dry_run: 只统计，不写入。
        keep_manual: 保留模式；同文本旧行复用，人工改写/新增的行按原位置插回。

    返回:
        {"scenes": 处理场景数, "lines": 新增台词行数, "unknown": 未识别说话人行数}。

    异常:
        ValueError: 场景已有台词行且未指定 force。
    """
    total_lines = 0
    unknown = 0
    scene_count = 0
    for scene in chapter.get("scenes") or []:
        if scene_ids and scene.get("id") not in scene_ids:
            continue
        if scene.get("lines") and not force:
            raise ValueError(f"场景 {scene.get('id')} 已有台词行，若确认覆盖请加 --force")
        drafts = split_lines(scene.get("source_text") or "", characters)
        if dry_run:
            scene_count += 1
            total_lines += len(drafts)
            unknown += sum(1 for item in drafts if item["speaker_id"] == entities.UNKNOWN_SPEAKER_ID)
            continue
        if keep_manual and scene.get("lines"):
            lines = merge_manual_lines(scene["lines"], drafts, scene["id"])
        else:
            lines = [
                entities.new_line(scene["id"], index, item["text"], kind=item["kind"], speaker_id=item["speaker_id"], confidence=item["confidence"])
                for index, item in enumerate(drafts)
            ]
        scene["lines"] = lines
        scene["character_ids"] = sorted({line["speaker_id"] for line in lines if line["speaker_id"] not in (entities.NARRATOR_ID, entities.UNKNOWN_SPEAKER_ID)})
        scene["revision"] = int(scene.get("revision") or 1) + 1
        total_lines += len(lines)
        unknown += sum(1 for line in lines if line["speaker_id"] == entities.UNKNOWN_SPEAKER_ID)
        scene_count += 1

    if not dry_run and scene_count:
        chapter["revision"] = int(chapter.get("revision") or 1) + 1
        store.save_chapter(chapter)
    return {"scenes": scene_count, "lines": total_lines, "unknown": unknown}


def is_line_stale(
    line: dict,
    characters: dict[str, dict],
    snapshot: dict | None = None,
    root=None,
    live_ids: set[str] | None = None,
    engine: dict | None = None,
) -> bool:
    """判断台词行是否需要重跑配音。

    参数:
        line: 台词行。
        characters: {角色 ID: 角色}。
        snapshot: 该行的侧车快照；有指纹时按指纹判定，为 None 时回退到 revision 比对。
        root: 工程根目录；给出时会检查音频产物是否还在。
        live_ids: 当前队列里活跃的行 ID；用于僵尸态降级。
        engine: 当前配置导出的引擎侧参数；改接缝裁切配置后据此判 stale。

    返回:
        True 表示需要重跑（待生成、参数过期或上次失败）。
    """
    state = resolve_line_state(line, characters, snapshot, root=root, live_ids=live_ids, engine=engine)["state"]
    return state in ("pending", "stale", "failed")


def build_tts_jobs(
    chapter: dict,
    characters: list[dict],
    adapter: str = "placeholder",
    snapshots: dict[str, dict] | None = None,
    engine: dict | None = None,
    root=None,
    live_ids: set[str] | None = None,
    line_ids: set[str] | None = None,
    force: bool = False,
    run_nonce: str | None = None,
) -> list[dict]:
    """规划整章配音任务（默认只针对过期/失败行）。

    参数:
        chapter: 章节 dict。
        characters: 角色表。
        adapter: TTS 适配器名称（参与幂等键）。
        snapshots: `{line_id: 侧车快照}`；给出时按参数指纹判定过期。
        engine: 当前配置导出的引擎侧参数；改接缝裁切配置后据此判过期。
        root: 工程根目录；给出时产物丢失的行会重新排队。
        live_ids: 当前队列里活跃的行 ID；用于僵尸态降级。
        line_ids: 只保留这些行（范围选择器的结果）；为空表示整章。
        force: 强制重新生成；忽略过期判定（已就绪行也入队）。
        run_nonce: 本次重生成的批次标识；写进幂等键，避免与历史任务去重。

    返回:
        渲染任务列表（未入队）。
    """
    char_index = {character["id"]: character for character in characters}
    snapshot_index = snapshots or {}
    jobs = []
    for scene in chapter.get("scenes") or []:
        for line in scene.get("lines") or []:
            if line_ids is not None and line.get("id") not in line_ids:
                continue
            if not force and not is_line_stale(
                line,
                char_index,
                snapshot_index.get(line.get("id")),
                root=root,
                live_ids=live_ids,
                engine=engine,
            ):
                continue
            target = {"chapter_id": chapter["id"], "scene_id": scene["id"], "line_id": line["id"]}
            params = {"voice": line.get("speaker_id"), "speed": line.get("speed"), "emotion": line.get("emotion")}
            if run_nonce:
                params["run"] = run_nonce
            key = build_idempotency_key("tts", target, line.get("revision"), adapter, params)
            job = entities.new_job("tts", target, key, priority=100, adapter=adapter)
            job["params_hash"] = key
            if force:
                # 标记强制重生成：执行时跳过产物缓存，真正重新合成
                job["force"] = True
            jobs.append(job)
    return jobs


def build_media_jobs(chapter: dict, kind: str, adapter: str = "local") -> list[dict]:
    """规划配图/音效/视频任务（只针对未就绪项）。

    参数:
        chapter: 章节 dict。
        kind: sfx/image/video。
        adapter: 适配器名称。

    返回:
        渲染任务列表（未入队）。
    """
    priority = {"sfx": 120, "image": 140, "video": 160}.get(kind, 140)
    key_name = {"sfx": "sfx_id", "image": "key_image_id", "video": "video_segment_id"}.get(kind)
    if key_name is None:
        raise ValueError(f"未知媒体类型: {kind}")
    jobs = []
    for scene in chapter.get("scenes") or []:
        if kind == "sfx":
            items = scene.get("sfx_events") or []
        elif kind == "image":
            items = scene.get("key_images") or []
        else:
            items = scene.get("video_segments") or []
        for item in items:
            if (item.get("asset") or {}).get("status") == "ready":
                continue
            target = {"chapter_id": chapter["id"], "scene_id": scene["id"], key_name: item["id"]}
            key = build_idempotency_key(kind, target, item.get("revision"), adapter, item.get("params") or {})
            jobs.append(entities.new_job(kind, target, key, priority=priority, adapter=adapter))
    return jobs


def require_writing(adapters) -> None:
    """检查写作适配器是否可用；不可用时抛出明确错误。

    参数:
        adapters: 已装配适配器。

    异常:
        AdapterError(NOT_IMPLEMENTED): omlx 协议未确认或实现未落地。
    """
    status = adapters.writing.health()
    if not status.ok:
        raise not_implemented("写作能力", status.detail)
