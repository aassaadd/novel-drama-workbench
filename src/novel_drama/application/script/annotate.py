"""用例：生成导演标注建议（镜头 / 台词 / 音效 / 画面）。"""

from __future__ import annotations

from ...adapters.base import (
    AdapterError,
    ErrorCode,
    Message,
    TextGenRequest,
)
from ...adapters.persistence import ProjectStore
from ...adapters.persistence.settings_store import SettingsStore
from ...director.markup import parse_markup
from ...infrastructure.config import AppConfig
from ...models.ids import now_iso
from ..story.chapter_context import _project_context
from ..story.chapter_rounds import (
    _script_work_load,
    _script_work_path,
    _script_work_save,
)
from ..support.llm_calls import _generate_text, build_writing_adapter


def _scene_block(scene: dict, prefix: str = "") -> str:
    """把一个场景渲染成提示词片段。

    参数:
        scene: 场景负载。
        prefix: 展示前缀（如「参考·前」）。

    返回:
        多行文本。
    """
    title = f"【{prefix}镜头 {scene['index'] + 1}】" if prefix else f"【镜头 {scene['index'] + 1}】"
    lines = [f"{title}{scene['title']}｜地点：{scene['location']}｜时间：{scene['time_of_day']}｜情绪：{scene['mood']}"]
    for line in scene["lines"]:
        label = line["speaker_name"] or ("旁白" if line.get("kind") == "narration" else "（未标注）")
        lines.append(f"{label}：{line['text']}")
    return "\n".join(lines)

def suggest_script(
    store: ProjectStore,
    config: AppConfig,
    chapter_id: str,
    dry_run: bool = False,
    scenes_per_round: int | None = None,
    resume: bool = False,
    on_progress=None,
) -> dict:
    """生成导演脚本标注建议并试解析（按批流式，单批失败可续跑）。

    参数:
        store: 工程。
        config: 本机配置。
        chapter_id: 章节 ID。
        dry_run: 只解析不落盘（兼容旧参数）。
        scenes_per_round: 每批镜头数；None 时取 `writing.multi_turn.script_scenes_per_round`（默认 8）。
        resume: True 时复用上次中间结果，只重试失败/未完成的批次。
        on_progress: 进度回调；事件 `batch{index,total,start,end}` / `chars{chars,...}`。

    返回:
        {"markup", "script", "warnings", "shots", "lines", "provider", "dry_run",
        "rounds", "failures", "batches", "partial"}。

    异常:
        ValueError: 章节没有场景或台词行。
        AdapterError: 全部批次失败，或模型输出完全无法解析。
    """
    chapter = store.load_chapter(chapter_id)
    scenes = chapter.get("scenes") or []
    if not scenes:
        raise ValueError(f"章节 {chapter_id} 还没有场景：先执行 ndw scene split")
    characters = store.load_characters()["characters"]
    char_names = {item["id"]: item.get("name") or "" for item in characters}
    settings_store = SettingsStore(store.root)
    bible = settings_store.ensure()

    scenes_payload = []
    for scene in sorted(scenes, key=lambda item: item.get("index") or 0):
        lines = []
        for line in scene.get("lines") or []:
            lines.append(
                {
                    "speaker_id": line.get("speaker_id"),
                    "speaker_name": char_names.get(line.get("speaker_id")) or ("旁白" if line.get("kind") == "narration" else ""),
                    "text": line.get("text"),
                    "kind": line.get("kind"),
                    "emotion": line.get("emotion") or "neutral",
                }
            )
        if not lines:
            continue
        scenes_payload.append(
            {
                "index": scene.get("index") or 0,
                "title": scene.get("title") or scene.get("scene_label") or f"场景 {(scene.get('index') or 0) + 1}",
                "location": scene.get("location") or "未标注",
                "time_of_day": scene.get("time_of_day") or "unknown",
                "mood": scene.get("mood") or "克制",
                "lines": lines,
            }
        )
    if not scenes_payload:
        raise ValueError(f"章节 {chapter_id} 的场景里没有任何台词行：先执行 ndw line split")

    adapter = build_writing_adapter(config, bible)
    work_info = store.load_project().get("work") or {}
    context = _project_context(work_info, bible, characters)
    base_parts = [context] if context else []
    base_parts.append(
        f"作品《{chapter.get('title')}》导演标注任务：按镜头输出 NDW 标注文本"
        "（@shot / !sfx / !visual / !anim / !bgm），保留台词原文与顺序，不要解释。\n"
        "严格格式：\n"
        "@shot 1 镜头名 location=验尸房 time=night mood=压抑\n"
        "旁白：夜色渐深。\n"
        "沈砚秋|calm：执照可以收，尸体不能白躺。\n"
        "!sfx ambience 8000 after-line=1 雨声敲窗 || Gentle rain tapping a window, close perspective, wet glass resonance\n"
        "!visual 画面描述\n"
        "!bgm mood=tense energy=0.4 intensity=0.6 loop=1 name=夜雨独行 style=低音弦乐+稀疏钢琴 function=铺垫危机 en=tense+low+strings+sparse+piano+slow+clock+ticks+dark+minor\n"
        "!anim i2v 5000 slow_push_in 镜头运动\n"
        "规则：@shot 后必须是整数序号与镜头名；time 取 dawn/morning/day/dusk/night/unknown；"
        "!sfx 的 kind 只能是 ambience/foley/impact/transition/music_sting，第二个参数是毫秒数，位置写 after-line=N；"
        "!sfx 的中文描述后必须用「||」跟一段英文提示词：写清声源、材质、动作、空间感与音色，不是逐字直译，也不要写时长；"
        "!visual 直接跟画面提示词，不写画幅；"
        "!bgm 每个镜头最多一条，mood 只能是 calm/tense/sad/romantic/mysterious/action/comedic/epic/horror，"
        "energy/intensity 取 0~1，style 与 function 与 en 里的空格用 + 代替；"
        "!bgm 必须带 en= 英文提示词：包含风格、情绪、配器、节奏与制作质感，不是逐字直译；"
        "同一镜头内情绪有明显变化时，追加 !bgm-seg mood=... anchor=镜头内台词序号 style=...；"
        "!silence 写成「!silence 800 after-line=N 原因」；"
        "!anim 的 mode 只能是 i2v/t2v，第二个参数是 500~5000 的毫秒数，第三个参数只能是 "
        "static/slow_push_in/pan_left/pan_right/handheld；每个镜头至少一句台词；台词保留原文。\n"
        "说话人规则：必须原样取自角色表里的名字，不得自造、不得加 *、编号、括号注释或写「未知」；"
        "某行显示为「（未标注）」时，先结合上下文与角色表推断最可能的说话人并只写名字，"
        "确实无法判断时才把说话人留空写成「：台词」（系统会标为待校对）；"
        "旁白固定写「旁白：」。音效位置 after-line=N 必须是本镜头内已出现的台词序号，不要超出。"
    )
    base_prompt = "\n\n".join(base_parts)
    system_prompt = (
        "你是有声剧导演，把给定台词改写成 NDW 标注文本，不要解释、不要 Markdown 代码块。\n"
        "只标注用户指定的本批镜头，参考镜头只用于理解上下文，不要输出。"
    )

    mt = config.get("writing.multi_turn") or {}
    try:
        per_round = int(scenes_per_round or mt.get("script_scenes_per_round") or 8)
    except (TypeError, ValueError):
        per_round = 8
    per_round = max(1, per_round)

    total = len(scenes_payload)
    rounds = (total + per_round - 1) // per_round
    work = _script_work_load(store, chapter_id, per_round, total) if resume else None
    if work is None:
        work = {
            "version": "script_batches/1",
            "chapter": chapter_id,
            "per_round": per_round,
            "total": total,
            "created_at": now_iso(),
            "batches": [
                {
                    "index": index + 1,
                    "start": index * per_round,
                    "end": min(total, index * per_round + per_round),
                    "status": "pending",
                    "markup": "",
                    "error": None,
                }
                for index in range(rounds)
            ],
        }

    # 批次总数以真实批次列表为准：续跑复用的批次可能少于按当前参数重算的轮数
    rounds = len(work["batches"])
    failures: list[dict] = []
    last_result = None
    for batch in work["batches"]:
        if batch.get("markup"):
            continue
        index = int(batch["index"])
        start, end = int(batch["start"]), int(batch["end"])
        subset = scenes_payload[start:end]
        first_no, last_no = subset[0]["index"] + 1, subset[-1]["index"] + 1
        # 直接调用进度回调：页面取消时抛出的异常要能中断本批生成
        if on_progress is not None:
            on_progress({"type": "batch", "index": index, "total": rounds, "start": first_no, "end": last_no})
        body = [f"本批镜头 {first_no}–{last_no}（全章共 {total} 个镜头，编号沿用全局序号）："]
        if start > 0:
            body.append("参考·前一个镜头（只读，不要输出标注）：\n" + _scene_block(scenes_payload[start - 1], "参考·"))
        body.append("\n\n".join(_scene_block(scene) for scene in subset))
        if end < total:
            body.append("参考·后一个镜头（只读，不要输出标注）：\n" + _scene_block(scenes_payload[end], "参考·"))
        body.append(
            f"任务：仅为本批 {len(subset)} 个镜头生成 NDW 标注文本，保留台词原文与顺序；"
            "每条 !sfx 必须带「|| 英文提示词」，不要解释。\n"
            f"编号规则：@shot 序号必须是 {first_no}–{last_no} 内的整数且与本批【镜头 N】一一对应，"
            "不得重号、跳号或新建镜头；参考镜头不要输出。"
        )
        request = TextGenRequest(
            task="script",
            system=system_prompt,
            messages=[Message("user", base_prompt + "\n\n" + "\n\n".join(body))],
            stream=True,
            metadata={"scenes": subset, "style": bible.get("style") or {}, "thinking_stage": "script"},
        )
        state = {"chars": 0}

        def on_delta(text: str) -> None:
            """累计本批接收长度并上报（取消异常需向上传播）。"""
            if not text or on_progress is None:
                return
            state["chars"] += len(str(text))
            state["tail"] = (str(state.get("tail") or "") + str(text))[-200:]
            on_progress(
                {
                    "type": "chars",
                    "chars": state["chars"],
                    "sample": state["tail"],
                    "index": index,
                    "total": rounds,
                }
            )

        try:
            result = _generate_text(adapter, request, on_delta if on_progress else None)
            last_result = result
            if str(getattr(result, "finish_reason", "") or "").lower() == "length":
                raise AdapterError(
                    ErrorCode.UPSTREAM_ERROR,
                    f"第 {first_no}–{last_no} 个镜头（第 {index}/{rounds} 批）标注输出被截断"
                    "（finish_reason=length）：输出侧没有人为上限，"
                    "说明触发了模型自身输出上限或上游异常。请调小每批场景数后重试",
                )
            markup = (result.text or "").strip()
            if not markup:
                raise AdapterError(ErrorCode.INVALID_INPUT, "本批未返回标注文本")
            batch["markup"] = markup
            batch["status"] = "done"
            batch["error"] = None
        except Exception as exc:  # 单批失败保留其它批次
            message = getattr(exc, "message", None) or str(exc) or exc.__class__.__name__
            batch["status"] = "failed"
            batch["error"] = message
            failures.append(
                {"batch": index, "start_scene": first_no, "end_scene": last_no, "message": message}
            )
        _script_work_save(store, work)

    done_batches = [batch for batch in work["batches"] if batch.get("markup")]
    if not done_batches:
        detail = "；".join(
            f"第 {item['start_scene']}–{item['end_scene']} 个镜头：{item['message']}" for item in failures[:3]
        )
        raise AdapterError(
            ErrorCode.UPSTREAM_ERROR,
            f"标注生成失败：全部 {rounds} 批都没有成功（{detail}）。已保留失败批次，可用 --resume 只重试失败批次",
            detail={"failures": failures},
        )
    markup = "\n\n".join(batch["markup"] for batch in done_batches).strip()
    #: 硬告警的结构化定位信息（页面点击告警直接跳转用）；信息级提示不写入
    warning_items: list[dict] = []
    try:
        script = parse_markup(
            markup, chapter, characters, store.settings(), diagnostics=warning_items
        )
    except ValueError as exc:
        raise AdapterError(ErrorCode.INVALID_INPUT, f"导演标注无法解析：{exc}", detail={"raw": markup[:2000]}) from exc
    if not failures:
        try:
            _script_work_path(store, chapter_id).unlink(missing_ok=True)
        except OSError:
            pass
    return {
        "markup": markup,
        "script": script,
        "warnings": script.get("warnings") or [],
        "warning_items": warning_items,
        "shots": len(script.get("shots") or []),
        "lines": sum(len(shot.get("lines") or []) for shot in script.get("shots") or []),
        "provider": getattr(last_result, "model", "") or getattr(adapter, "model", "") or "offline-writing",
        "dry_run": dry_run,
        "rounds": rounds,
        "failures": failures,
        "batches": [
            {"index": batch["index"], "start": batch["start"], "end": batch["end"], "status": batch["status"]}
            for batch in work["batches"]
        ],
        "partial": bool(failures),
    }
