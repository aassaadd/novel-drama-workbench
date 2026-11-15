"""`ndw tts`：行级规划、入队执行与单句试音/路由/对比。"""

from __future__ import annotations

import json
import time
from pathlib import Path

from novel_drama.infrastructure.config import AppConfig
from novel_drama.interface.cli.commands.media import cmd_tts_batch
from novel_drama.interface.cli.common import (
    EXIT_OK,
    PreconditionError,
    _emit,
    _resolve_store,
    _tts_service,
)
from novel_drama.interface.cli.helpers import (
    _add_scope_args,
    _drain,
    _make_queue,
    _queue_service,
    _sort_jobs_by_group,
)
from novel_drama.models.ids import now_iso
from novel_drama.orchestration.pipeline import build_tts_jobs
from novel_drama.tts import line_state


def cmd_tts(args, config: AppConfig) -> int:
    """配音：plan 只规划，run 入队执行；两者共用同一套范围选择。

    范围与状态筛选走 `line_state`，与 `line list --status` 同源，
    因此 plan 的「待合成」计数必然等于 list 筛出来的行数。
    """
    store = _resolve_store(args, config)
    chapter = store.load_chapter(args.chapter)
    characters = store.load_characters()["characters"]
    adapter = config.adapters.get("tts", "placeholder")
    snapshots = line_state.load_snapshots(store.root, chapter)
    live_ids = line_state.live_line_ids(store)
    engine = line_state.engine_params(config)
    # force：忽略状态筛选（含已就绪行），强制重新生成
    force = bool(getattr(args, "force", False))
    statuses = () if force else line_state.parse_states(args.status)
    # 默认范围：给了 --line 就是「选中」；给了 --scene 就是「场景」；否则「待处理」
    if args.scope:
        scope = args.scope
    elif args.line:
        scope = "selected"
    elif args.scene:
        scope = "scene"
    else:
        scope = "chapter" if force else "pending"
    states = line_state.chapter_states(
        chapter, {item["id"]: item for item in characters}, snapshots, root=store.root, live_ids=live_ids, engine=engine
    )
    selected = line_state.select_states(
        states, scope=scope, line_ids=args.line, scene_ids=args.scene, statuses=statuses
    )
    counts = line_state.state_counts(selected)
    if force:
        # 强制重生成：范围内所有行都入队（含 ready）
        todo_ids = {row["line"]["id"] for row in selected}
        run_nonce = f"regen-{time.time_ns()}"
    else:
        todo_ids = {row["line"]["id"] for row in selected if row["state"] in line_state.PENDING_STATES}
        run_nonce = None
    jobs = build_tts_jobs(
        chapter,
        characters,
        adapter=adapter,
        snapshots=snapshots,
        engine=engine,
        root=store.root,
        live_ids=live_ids,
        line_ids=todo_ids,
        force=force,
        run_nonce=run_nonce,
    )
    if args.action == "plan":
        summary = (
            f"待合成台词行: {len(jobs)} 条（范围 {scope}，适配器 {adapter}）"
            f"｜未生成 {counts.get('pending', 0)} / 过期 {counts.get('stale', 0)}"
            f" / 失败 {counts.get('failed', 0)} / 已完成 {counts.get('ready', 0)}"
        )
        _emit(
            args,
            summary,
            {
                "scope": scope,
                "selected": len(selected),
                "counts": counts,
                "jobs": len(jobs),
                "adapter": adapter,
            },
        )
        return EXIT_OK
    if not jobs:
        _emit(args, "没有需要合成的台词行", {"jobs": 0, "scope": scope, "counts": counts})
        return EXIT_OK
    queue = _make_queue(store, config)
    # 入队前按 worker 复用键稳定排序：只改执行顺序，产物/章节顺序不变
    service = _queue_service(queue)
    if service is not None:
        jobs = _sort_jobs_by_group(jobs, chapter, characters, service, store)
    try:
        created = sum(1 for job in jobs if queue.enqueue(job)[1])
        print(f"已入队 {created} 条配音任务（去重后）")
        if getattr(args, "wait", False):
            return _drain(queue)
        return EXIT_OK
    finally:
        # 整批结束（含异常/停止）都要回收常驻 worker，避免留下孤儿进程
        if service is not None:
            service.close()




def _tts_requirement(args, text: str):
    """把命令行参数翻译成路由需求向量。

    参数:
        args: 已解析参数。
        text: 台词文本。

    返回:
        Requirement。
    """
    from novel_drama.tts import Requirement

    paral = []
    for item in getattr(args, "paralinguistic", None) or []:
        paral.extend([tag.strip() for tag in item.split(",") if tag.strip()])
    return Requirement(
        text=text,
        emotion=getattr(args, "emotion", None),
        emotion_intensity=int(getattr(args, "emotion_intensity", 0) or 0),
        paralinguistic=paral,
        clone_required=not getattr(args, "no_clone", False),
        language=getattr(args, "language", None) or "zh-CN",
        duration_mode="target" if getattr(args, "target_duration_ms", None) else "none",
        target_duration_ms=getattr(args, "target_duration_ms", None),
        usage=getattr(args, "usage", None) or "personal",
    )


def _tts_voice(args, service):
    """构造音色参考；命令行未指定时沿用配置默认值。

    参数:
        args: 已解析参数。
        service: TtsService（读取 default_voice）。
    """
    from novel_drama.tts import VoiceRef

    ref_audio = getattr(args, "ref_audio", None) or service.default_voice.ref_audio
    ref_text = getattr(args, "ref_text", None) or service.default_voice.ref_text
    emo_audio = getattr(args, "emo_audio_prompt", None)
    return VoiceRef(
        variant_id=getattr(args, "voice_id", None),
        ref_audio=ref_audio,
        ref_text=ref_text,
        emotion_audio=emo_audio,
        source="emotion_ref" if emo_audio else "preset",
    )


def _tts_instruct(args) -> str | None:
    """取显式风格指令；不再把情绪自动改写成指令文本。

    参数:
        args: 已解析参数。

    说明:
        情绪到指令的自动改写会和 IndexTTS 的 8 维向量通路撞车（三条情绪通路
        互斥），因此只保留用户显式给的 `--emo-text` / `--instruct`；
        需要「情绪→指令」的实现（如 CosyVoice3）在 provider 内部自行改写。
    """
    return getattr(args, "emo_text", None) or getattr(args, "instruct", None)


def _read_text(args) -> str:
    """读取台词文本（--text 或 --text-file）。

    参数:
        args: 已解析参数。
    """
    if getattr(args, "text_file", None):
        return Path(args.text_file).read_text(encoding="utf-8").strip()
    return (getattr(args, "text", None) or "").strip()


def _rss_mb() -> float:
    """当前进程峰值内存（MB）；macOS 的 ru_maxrss 单位是字节。"""
    import resource

    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 / 1024, 1)


def cmd_tts_providers(args, config: AppConfig) -> int:
    """列出全部 provider 的可用性、许可与自动选中资格。"""
    service = _tts_service(args, config)
    rows = service.doctor()
    if getattr(args, "json", False):
        print(json.dumps({"providers": rows}, ensure_ascii=False, indent=2))
        return EXIT_OK
    print(f"{'provider_id':<20}{'可用':<6}{'启用':<6}{'自动':<6}{'许可':<20}{'版本':<12}说明")
    for row in rows:
        print(
            f"{row['provider_id']:<20}"
            f"{'是' if row['available'] else '否':<6}"
            f"{'是' if row['enabled'] else '否':<6}"
            f"{'是' if row['auto_select'] else '否':<6}"
            f"{str(row['license_gate']):<20}"
            f"{str(row['model_version']):<12}"
            f"{row['detail']}"
        )
    return EXIT_OK


def cmd_tts_route(args, config: AppConfig) -> int:
    """只做路由决策，不合成：解释选中理由与被淘汰项。"""
    text = _read_text(args)
    if not text:
        raise PreconditionError("缺少台词文本：用 --text 或 --text-file 指定")
    service = _tts_service(args, config)
    requirement = _tts_requirement(args, text)
    decision = service.decide(
        requirement,
        line_provider_id=getattr(args, "provider", None),
        character_provider_id=getattr(args, "character_provider", None),
    )
    if getattr(args, "json", False):
        print(json.dumps(decision, ensure_ascii=False, indent=2))
        return EXIT_OK
    selected = decision["selected"]
    print(f"路由模式 {decision['mode']}  选中 {selected['provider_id']}  得分 {selected['score']}")
    for reason in selected["reasons"]:
        print(f"  + {reason}")
    for item in decision["degradations"]:
        print(f"  ! {item['kind']}: {item.get('detail')}")
    for item in decision["rejected"]:
        print(f"  - {item['provider_id']}  [{item['stage']}/{item['reason']}] {item.get('detail')}")
    print(f"input_hash {decision['input_hash']}")
    return EXIT_OK


def cmd_tts_say(args, config: AppConfig) -> int:
    """合成一句话并落盘，写出侧车 JSON 记录 provider 与版本。"""
    text = _read_text(args)
    if not text:
        raise PreconditionError("缺少台词文本：用 --text 或 --text-file 指定")
    service = _tts_service(args, config)
    requirement = _tts_requirement(args, text)
    voice = _tts_voice(args, service)

    out = getattr(args, "out", None)
    if not out:
        base = service.project_root if getattr(service, "project_root", None) else Path.cwd()
        stamp = now_iso().replace(":", "").replace("-", "")[:15]
        out = str(Path(base) / "var" / "tts" / f"say_{stamp}.wav")

    import time

    started = time.perf_counter()
    try:
        payload = service.synthesize(
            text,
            out,
            requirement=requirement,
            voice=voice,
            instruct=_tts_instruct(args),
            line_provider_id=getattr(args, "provider", None),
            character_provider_id=getattr(args, "character_provider", None),
        )
    finally:
        # 单句命令也要回收常驻 worker（正常情况下 worker 会因 stdin EOF 自行退出）
        service.close()
    wall_ms = int((time.perf_counter() - started) * 1000)

    if getattr(args, "json", False):
        payload["wall_ms"] = wall_ms
        payload["rss_mb"] = _rss_mb()
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return EXIT_OK
    print(f"音频 {payload['audio_path']}")
    print(f"侧车 {payload['audio_path']}.json")
    print(
        f"provider {payload['provider_id']}  模型 {payload['model']}({payload['model_version']})  "
        f"时长 {payload['duration_ms']}ms  合成 {payload['latency_ms']}ms  总耗时 {wall_ms}ms  "
        f"块数 {payload['chunks']}  内存 {_rss_mb()}MB"
    )
    return EXIT_OK


def cmd_tts_compare(args, config: AppConfig) -> int:
    """同一句台词用多个 provider 合成，留档对比试听。"""
    text = _read_text(args)
    if not text:
        raise PreconditionError("缺少台词文本：用 --text 或 --text-file 指定")
    service = _tts_service(args, config)
    raw = getattr(args, "providers", None)
    if raw:
        provider_ids = [item.strip() for item in raw.split(",") if item.strip()]
    else:
        provider_ids = [
            pid
            for pid, provider in service.providers.items()
            if provider.spec.enabled and provider.spec.capabilities.get("allow_auto_select")
        ]
    out_dir = getattr(args, "out_dir", None) or "var/tts/compare"
    try:
        record = service.compare(
            text,
            provider_ids,
            out_dir,
            requirement=_tts_requirement(args, text),
            voice=_tts_voice(args, service),
        )
    finally:
        service.close()
    if getattr(args, "json", False):
        print(json.dumps(record, ensure_ascii=False, indent=2))
        return EXIT_OK
    print(f"对比批次 {record['batch_id']}  记录 {record['record_path']}")
    for item in record["items"]:
        if item["status"] == "ready":
            print(f"  {item['provider_id']:<20} {item['duration_ms']}ms  {item['asset_id']}")
        else:
            print(f"  {item['provider_id']:<20} 失败 [{item['code']}] {item['message']}")
    return EXIT_OK


def add_parser(sub) -> None:
    """注册 `tts` 命令组。"""
    p = sub.add_parser("tts", help="配音")
    p_sub = p.add_subparsers(dest="action", required=True)
    q = p_sub.add_parser("plan", help="规划要合成的行（范围选择器 + 状态筛选）")
    q.add_argument("--chapter", required=True)
    _add_scope_args(q)
    q.set_defaults(func=cmd_tts)
    q = p_sub.add_parser("run", help="按范围入队合成")
    q.add_argument("--chapter", required=True)
    q.add_argument("--wait", action="store_true")
    q.add_argument("--force", action="store_true", help="忽略状态筛选与产物缓存，强制重新生成（含已就绪行）")
    _add_scope_args(q)
    q.set_defaults(func=cmd_tts)
    q = p_sub.add_parser("batch", help="行级批量合成（单并发，失败不中断，支持跨章节与缓存续跑）")
    q.add_argument("--chapter", help="只处理该章节")
    q.add_argument("--chapters", help="逗号分隔的多个章节 ID")
    q.add_argument("--all-chapters", action="store_true", dest="all_chapters", help="批量处理工程内全部章节")
    q.add_argument("--scene", action="append", help="只跑指定场景，可重复")
    q.add_argument("--line", action="append", help="只跑指定行，可重复")
    q.add_argument(
        "--status",
        help="逗号分隔的派生状态：pending,stale,failed,ready（ready 会连已完成行一起重跑）",
    )
    q.add_argument("--limit", type=int, help="每章最多合成多少行")
    q.add_argument("--batch-size", type=int, default=5, dest="batch_size", help="每批行数（默认 5）")
    q.add_argument("--include-ready", action="store_true", dest="include_ready", help="连已完成的行一起重跑（命中缓存则不重新合成）")
    q.add_argument("--dry-run", action="store_true", help="只列出待合成行")
    q.add_argument(
        "--progress-file",
        dest="progress_file",
        help="把「完成 X/Y + 失败行清单」写进该 JSON 文件，供页面轮询（Web 整本批量使用）",
    )
    q.add_argument(
        "--skip-cast-check",
        action="store_true",
        dest="skip_cast_check",
        help="跳过配音前的音色/BGM 一致性门禁（有 error 时默认阻断）",
    )
    q.add_argument(
        "--skip-unknown-speakers",
        action="store_true",
        dest="skip_unknown_speakers",
        help="跳过说话人未识别的行先配其余行（这些行保持原样，不写音频、不改说话人）",
    )
    q.set_defaults(func=cmd_tts_batch)

    p_sub.add_parser("providers", help="列出 provider 可用性与许可").set_defaults(func=cmd_tts_providers)

    # route/say/compare 共享同一组需求参数
    def _add_tts_inputs(q) -> None:
        """为配音子命令挂上通用的文本与路由参数。"""
        q.add_argument("--text", help="台词文本")
        q.add_argument("--text-file", help="从文件读取台词文本")
        q.add_argument("--emotion", help="情绪：neutral/joy/sad/angry/fear/surprise/tender/excited/whisper/serious，也接受中文如 愤怒/悲伤/温柔")
        q.add_argument("--emotion-intensity", type=int, default=0, help="情绪强度 0~3")
        q.add_argument("--paralinguistic", action="append", help="副语言标记，可逗号分隔：laugh,sigh")
        q.add_argument("--instruct", help="自然语言风格指令（交付说明，走 emo_text 通路）")
        q.add_argument("--emo-text", dest="emo_text", help="文字情绪描述，走 emo_text 通路；与 --emotion/--emo-audio-prompt 互斥")
        q.add_argument("--emo-audio-prompt", dest="emo_audio_prompt", help="情绪参考音频，走 emo_audio_prompt 通路；与 --emotion/--emo-text 互斥")
        q.add_argument("--language", default="zh-CN", help="语言标签")
        q.add_argument("--target-duration-ms", type=int, help="期望时长（毫秒），不足补静音")
        q.add_argument("--usage", choices=["personal", "commercial"], default="personal", help="工程用途，影响许可过滤")
        q.add_argument("--provider", help="锁定 provider（行级）")
        q.add_argument("--character-provider", help="角色绑定的 provider（角色级）")
        q.add_argument("--ref-audio", help="参考音频，缺省用配置默认音色")
        q.add_argument("--ref-text", help="参考音频对应的文本")
        q.add_argument("--voice-id", help="音色变体 id，仅用于记录")

    q = p_sub.add_parser("route", help="只做路由决策，不合成")
    _add_tts_inputs(q)
    q.set_defaults(func=cmd_tts_route)

    q = p_sub.add_parser("say", help="合成一句话并落盘")
    _add_tts_inputs(q)
    q.add_argument("--out", help="输出 wav 路径")
    q.set_defaults(func=cmd_tts_say)

    q = p_sub.add_parser("compare", help="同句多 provider 对比试听")
    _add_tts_inputs(q)
    q.add_argument("--providers", help="参与对比的 provider，逗号分隔；缺省为全部可自动选中的")
    q.add_argument("--out-dir", help="对比产物目录")
    q.set_defaults(func=cmd_tts_compare)
