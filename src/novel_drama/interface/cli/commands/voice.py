"""音色库与导演脚本 CLI。"""

from __future__ import annotations

import json
from pathlib import Path

from novel_drama.application.script.apply_script import apply_script_to_chapter
from novel_drama.composition import (
    ProjectStore,
    find_variant,
    find_voice,
    validate,
    variant_ref,
    voice_binding_summary,
)
from novel_drama.infrastructure.config import AppConfig
from novel_drama.models import entities
from novel_drama.models.ids import now_iso

from ..common import (
    EXIT_FAILED,
    EXIT_OK,
    PreconditionError,
    _emit,
    _resolve_store,
    _tts_service,
)

# ---------------- 音色库变体管理 ----------------


def _build_variant(args, store: ProjectStore, name: str) -> dict:
    """由命令行参数构造一个音色变体（含校验与参考音频入库）。

    参数:
        args: 已解析参数。
        store: 工程。
        name: 变体名称。

    返回:
        符合 `voice.schema.json` 的变体 dict。
    """
    import shutil

    from novel_drama.composition import probe_duration_ms

    source = args.source
    tags = args.emotion_tag or ["default"]
    variant = {
        "id": entities.new_id("vav"),
        "name": name,
        "source": source,
        "emotion_tags": tags,
        "status": args.source_status or "ready",
        "created_at": now_iso(),
    }
    if source == "preset":
        if not args.preset:
            raise PreconditionError("预设音色必须指定 --preset（Vivian/Serena/Uncle_Fu/Dylan/Eric/Ryan/Aiden/Ono_Anna/Sohee）")
        variant["preset_name"] = args.preset
    else:
        if not args.audio:
            raise PreconditionError("克隆/录制音色必须指定 --audio（参考音频）")
        if not args.consent:
            raise PreconditionError("使用真人参考音频必须确认授权：加 --consent（本人录音或已获书面授权）")
        source_path = Path(args.audio)
        if not source_path.exists():
            raise PreconditionError(f"参考音频不存在: {source_path}")
        try:
            relative = str(source_path.resolve().relative_to(store.root.resolve()))
        except ValueError:
            ref_dir = store.voice_asset_dir("ref")
            ref_dir.mkdir(parents=True, exist_ok=True)
            dest = ref_dir / f"{variant['id']}{source_path.suffix or '.wav'}"
            shutil.copy2(source_path, dest)
            relative = str(dest.relative_to(store.root))
        duration = args.duration_ms or probe_duration_ms(source_path, "ffprobe") or 0
        if not 2000 <= duration <= 20000:
            raise PreconditionError(f"参考音频时长 {duration}ms 不在 2000~20000ms 范围（建议 5~15 秒）")
        variant["reference"] = {"audio_path": relative, "duration_ms": duration, "text": args.ref_text}
        variant["consent"] = {"confirmed": True, "note": args.consent_note}
    if args.provider:
        variant["provider_availability"] = [
            {
                "provider_id": args.provider,
                "status": "ready",
                "clone_supported": source != "preset",
                "checked_at": now_iso(),
            }
        ]
    return variant


def cmd_voice_builtin(args, config: AppConfig) -> int:
    """内置音色：列出清单、下载到外置盘、导入当前工程。

    参数:
        args: 已解析参数（action=list/install/import，key 可重复，force 可选）。
        config: 本机配置。

    返回:
        退出码。
    """
    from novel_drama.tts import builtin_voices

    action = args.action
    if action == "list":
        result = builtin_voices.rows(config)
        root = builtin_voices.builtin_root(config)
        print(f"内置音色目录: {root}")
        for item in result:
            state = "已下载" if item["present"] else "未下载"
            print(f"{item['key']:<9} {item['name']:<14} {item['gender']:<7} {item['duration_ms']}ms  {state}")
        return EXIT_OK
    if action == "install":
        result = builtin_voices.install(config, keys=args.key, force=args.force)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return EXIT_OK if result["ok"] else EXIT_FAILED
    if action == "import":
        store = _resolve_store(args, config)
        result = builtin_voices.import_into_project(store, config, keys=args.key)
        print(
            f"新增 {len(result['added'])} 个，更新 {len(result['updated'])} 个，"
            f"跳过 {len(result['skipped'])} 个。"
        )
        for item in result["added"]:
            print(f"  + {item['name']}（{item['voice_id']}）")
        for item in result["updated"]:
            print(f"  ~ {item['name']}（{item['voice_id']}，已修正）")
        for item in result["skipped"]:
            print(f"  - {item['key']}: {item['reason']}")
        return EXIT_OK
    raise PreconditionError(f"未知操作: {action}")


def cmd_voice(args, config: AppConfig) -> int:
    """音色库：列表、新增音色、变体增删改、绑定角色、试听。"""
    store = _resolve_store(args, config)
    table = store.load_voices()
    action = args.action

    if action == "list":
        characters = store.load_characters()["characters"]
        bindings = voice_binding_summary(store, {"characters": characters})
        payload = {"voices": table["voices"], "characters": bindings}
        if getattr(args, "json", False):
            _emit(args, payload=payload)
            return EXIT_OK
        if not table["voices"]:
            print("音色库为空：ndw voice add --name 林晚 --preset Serena")
        for voice in table["voices"]:
            print(f"{voice['id']}  {voice['name']:<10} {voice['status']:<8} 变体 {len(voice['variants'])}  默认 {voice['default_variant_id']}")
        for item in bindings:
            print(f"  角色 {item['name']:<8} → {item['voice_name'] or '未绑定'}（{item['variants']} 个变体）")
        return EXIT_OK

    if action == "show":
        voice = find_voice(table, args.voice)
        if voice is None:
            raise PreconditionError(f"音色不存在: {args.voice}")
        _emit(args, json.dumps(voice, ensure_ascii=False, indent=2), voice)
        return EXIT_OK

    if action == "add":
        if find_voice(table, args.name) is not None:
            raise PreconditionError(f"音色已存在: {args.name}")
        variant = _build_variant(args, store, args.variant_name or "默认")
        voice = {
            "id": entities.new_id("voc"),
            "name": args.name,
            "engine": None,
            "default_provider_id": args.provider,
            "language": args.language,
            "gender": args.gender,
            "tags": args.tag or [],
            "default_variant_id": variant["id"],
            "variants": [variant],
            "status": "ready",
            "revision": 1,
            "created_at": now_iso(),
            "updated_at": now_iso(),
        }
        table["voices"].append(voice)
        store.save_voices(table)
        _emit(args, f"音色已创建: {voice['id']} {voice['name']}（变体 {variant['id']} {variant['name']}）", voice)
        return EXIT_OK

    voice = find_voice(table, args.voice)
    if voice is None:
        raise PreconditionError(f"音色不存在: {args.voice}")

    if action == "variant-add":
        if find_variant(voice, args.variant_name) is not None:
            raise PreconditionError(f"变体名称重复: {args.variant_name}")
        variant = _build_variant(args, store, args.variant_name)
        voice["variants"].append(variant)
        if len(voice["variants"]) == 1:
            voice["default_variant_id"] = variant["id"]
        voice["revision"] += 1
        voice["updated_at"] = now_iso()
        store.save_voices(table)
        _emit(args, f"已新增变体 {variant['id']} {variant['name']}（{voice['name']}）", variant)
        return EXIT_OK

    variant = None
    if action in ("variant-update", "variant-remove", "set-default", "preview"):
        variant = find_variant(voice, getattr(args, "variant", None))
        if variant is None:
            raise PreconditionError(f"变体不存在: {getattr(args, 'variant', None)}")

    if action == "variant-update":
        if args.variant_name:
            variant["name"] = args.variant_name
        if args.emotion_tag:
            variant["emotion_tags"] = args.emotion_tag
        if args.source_status:
            variant["status"] = args.source_status
        if args.preset:
            variant["preset_name"] = args.preset
        if args.provider:
            entry = next((item for item in variant.get("provider_availability") or [] if item["provider_id"] == args.provider), None)
            if entry is None:
                variant.setdefault("provider_availability", []).append(
                    {"provider_id": args.provider, "status": args.provider_status or "ready", "checked_at": now_iso()}
                )
            elif args.provider_status:
                entry["status"] = args.provider_status
                entry["checked_at"] = now_iso()
        voice["revision"] += 1
        voice["updated_at"] = now_iso()
        store.save_voices(table)
        _emit(args, f"变体已更新: {variant['id']} {variant['name']}", variant)
        return EXIT_OK

    if action == "variant-remove":
        if voice["default_variant_id"] == variant["id"]:
            raise PreconditionError("不能删除默认变体：先 voice set-default 切到其它变体")
        if len(voice["variants"]) <= 1:
            raise PreconditionError("音色至少保留一个变体")
        voice["variants"] = [item for item in voice["variants"] if item["id"] != variant["id"]]
        voice["revision"] += 1
        voice["updated_at"] = now_iso()
        store.save_voices(table)
        _emit(args, f"已删除变体: {variant['id']} {variant['name']}", {"removed": variant["id"]})
        return EXIT_OK

    if action == "set-default":
        voice["default_variant_id"] = variant["id"]
        voice["revision"] += 1
        voice["updated_at"] = now_iso()
        store.save_voices(table)
        _emit(args, f"{voice['name']} 默认变体 → {variant['id']}（{variant['name']}）", voice)
        return EXIT_OK

    if action == "bind":
        characters = store.load_characters()
        target = next(
            (item for item in characters["characters"] if item["id"] == args.character or item["name"] == args.character),
            None,
        )
        if target is None:
            raise PreconditionError(f"角色不存在: {args.character}")
        binding = target.get("voice_binding") or {}
        target["voice_binding"] = {
            "adapter": args.adapter or binding.get("adapter") or "placeholder",
            "voice_id": voice["id"],
            "voice_library_id": voice["id"],
            "default_variant_id": voice["default_variant_id"],
            "emotion_variant_map": binding.get("emotion_variant_map") or {},
        }
        target["updated_at"] = now_iso()
        store.save_characters(characters)
        _emit(args, f"已绑定 {target['name']} → {voice['name']}（{voice['id']}）", target)
        return EXIT_OK

    if action == "preview":
        ref_audio, ref_text = variant_ref(store, variant)
        if not ref_audio:
            raise PreconditionError("该变体没有参考音频（预设音色无法在当前克隆 provider 上试听）")
        from novel_drama.tts import Requirement, VoiceRef

        service = _tts_service(args, config)
        text = args.text or "这是一段音色试听，用来确认音色与情绪是否符合角色设定。"
        out = store.voice_asset_dir("preview") / f"{variant['id']}_{now_iso().replace(':', '').replace('-', '')[:15]}.wav"
        out.parent.mkdir(parents=True, exist_ok=True)
        payload = service.synthesize(
            text,
            out,
            requirement=Requirement(text=text, emotion=args.emotion or "neutral", clone_required=True),
            voice=VoiceRef(variant_id=variant["id"], ref_audio=ref_audio, ref_text=ref_text, source=variant["source"]),
            instruct=args.instruct,
        )
        relative = str(out.relative_to(store.root))
        entries = variant.setdefault("provider_availability", [])
        entry = next((item for item in entries if item["provider_id"] == payload["provider_id"]), None)
        if entry is None:
            entries.append(
                {
                    "provider_id": payload["provider_id"],
                    "status": "ready",
                    "clone_supported": True,
                    "preview_asset_id": relative,
                    "checked_at": now_iso(),
                }
            )
        else:
            entry.update({"status": "ready", "preview_asset_id": relative, "checked_at": now_iso()})
        voice["revision"] += 1
        voice["updated_at"] = now_iso()
        store.save_voices(table)
        _emit(
            args,
            f"试听产物 {relative}\nprovider {payload['provider_id']}  时长 {payload['duration_ms']}ms  合成 {payload['latency_ms']}ms",
            payload,
        )
        return EXIT_OK

    raise PreconditionError(f"未知的 voice 操作: {action}")


# ---------------- 导演脚本（人工标注解析） ----------------


def cmd_script(args, config: AppConfig) -> int:
    """导演脚本：语法说明、解析标注、同步到章节、校验。"""
    from novel_drama.director import parse_markup

    if args.action == "syntax":
        from novel_drama.director import markup as markup_module

        print(markup_module.__doc__)
        return EXIT_OK

    store = _resolve_store(args, config)
    if args.action == "show":
        script = store.load_script(args.chapter)
        summary = {
            "chapter_id": script["chapter_id"],
            "script_id": script["id"],
            "status": script["status"],
            "shots": len(script["shots"]),
            "lines": sum(len(shot["lines"]) for shot in script["shots"]),
            "sfx": sum(len(shot["sfx_events"]) for shot in script["shots"]),
            "silence": sum(len(shot["silence_events"]) for shot in script["shots"]),
            "warnings": script.get("warnings") or [],
        }
        _emit(
            args,
            f"脚本 {summary['script_id']}  状态 {summary['status']}  镜头 {summary['shots']}  台词 {summary['lines']}  "
            f"音效 {summary['sfx']}  静音 {summary['silence']}\n" + "\n".join(summary["warnings"]),
            script,
        )
        return EXIT_OK

    if args.action == "validate":
        script = store.load_script(args.chapter)
        validate(script, "director-script.schema.json")
        warnings = script.get("warnings") or []
        _emit(args, f"脚本校验通过；告警 {len(warnings)} 条" + ("\n" + "\n".join(warnings) if warnings else ""), {"warnings": warnings})
        return EXIT_OK

    chapter = store.load_chapter(args.chapter)
    characters = store.load_characters()["characters"]

    if args.action == "parse":
        markup_text = Path(args.file).read_text(encoding="utf-8")
        script = parse_markup(markup_text, chapter, characters, store.settings())
        validate(script, "director-script.schema.json")
        if args.dry_run:
            _emit(args, f"[dry-run] 解析成功：镜头 {len(script['shots'])}，告警 {len(script['warnings'])} 条", script)
            return EXIT_OK
        store.save_script(script)
        warnings = script["warnings"]
        _emit(
            args,
            f"导演脚本已写入 {store.script_path(chapter['id'])}：镜头 {len(script['shots'])}，状态 {script['status']}"
            + ("\n" + "\n".join(warnings) if warnings else ""),
            script,
        )
        return EXIT_OK

    if args.action == "apply":
        script = store.load_script(args.chapter)
        summary = apply_script_to_chapter(store, chapter, script)
        _emit(
            args,
            f"已同步到章节：新建场景 {summary['scenes_created']}，新增台词 {summary['lines_added']}，更新台词 {summary['lines_updated']}",
            summary,
        )
        return EXIT_OK

    raise PreconditionError(f"未知的 script 操作: {args.action}")


def cmd_voice_design(args, config: AppConfig) -> int:
    """按人物设定生成音色候选（本地 VoxCPM2），可同步等待并直接采用。

    参数:
        args: `{character, count, prompt, select, name, gender, no_bind}`。
        config: 本机配置（读取 voice.design.*）。

    返回:
        退出码：成功 EXIT_OK；生成失败 EXIT_FAILED。
    """
    import time

    from novel_drama.composition import build_adapters
    from novel_drama.tts import voice_design

    store = _resolve_store(args, config)
    design = config.get("voice.design") or {}
    if not design.get("enabled", True):
        raise PreconditionError("音色设计未启用：voice.design.enabled=false")
    characters = store.load_characters()["characters"]
    character = next((item for item in characters if item["id"] == args.character or item["name"] == args.character), None)
    if character is None:
        raise PreconditionError(f"人物不存在: {args.character}")

    prompt = args.prompt
    if not prompt:
        adapters = build_adapters(config, store.settings())
        built = voice_design.build_profile(adapters.writing, character, config)
        prompt = built["profile"]["design_prompt"]
        if built.get("note"):
            print(f"[warn] {built['note']}")
        print(f"音色描述: {prompt}")

    count = int(args.count or design.get("candidates", 4))
    meta = voice_design.create_batch(store, config, character, prompt, count, None)
    batch_id = meta["batch_id"]
    print(f"批次 {batch_id} 已提交，共 {count} 条候选…")
    deadline = time.time() + float(design.get("timeout_s", 900)) + 60
    while time.time() < deadline:
        meta = voice_design.read_batch(store, batch_id)
        if meta.get("status") != "running":
            break
        stage = meta.get("stage")
        if stage == "loading":
            print("  加载模型…")
        elif stage == "generating":
            print(f"  生成中 {meta.get('done')}/{meta.get('total')}")
        time.sleep(3)
    if meta.get("status") not in ("ready", "partial"):
        print(f"[failed] {meta.get('error') or '生成超时'}")
        return EXIT_FAILED

    for item in meta.get("candidates") or []:
        print(f"  候选 {item['index']}: {item['duration_ms'] / 1000:.1f}s  seed={item.get('seed')}")
    for item in meta.get("failures") or []:
        print(f"  [失败] 候选 {item.get('index')}: {item.get('message')}")
    print(f"候选目录: {voice_design.batch_dir(store, batch_id)}")
    if not args.select:
        print(f"未指定 --select；候选保留 {design.get('max_keep_days', 7)} 天，可稍后在页面采用。")
        return EXIT_OK

    gender = args.gender or (meta.get("profile") or {}).get("gender") or "unknown"
    result = voice_design.select_candidate(
        store,
        batch_id,
        int(args.select),
        args.name or character.get("name") or "生成音色",
        gender,
        "manual",
        not args.no_bind,
    )
    print(f"已入库: {result['voice']['id']} / 变体 {result['variant_id']}")
    return EXIT_OK


def add_parser(sub) -> None:
    """注册 `voice` 命令组。"""
    p = sub.add_parser("voice", help="音色库（角色 → 多变体）")
    p_sub = p.add_subparsers(dest="action", required=True)
    p_sub.add_parser("list").set_defaults(func=cmd_voice)
    q = p_sub.add_parser("show")
    q.add_argument("--voice", required=True)
    q.set_defaults(func=cmd_voice)

    q = p_sub.add_parser("builtin", help="内置音色：list / install / import")
    q.add_argument("action", nargs="?", choices=["list", "install", "import"], default="list")
    q.add_argument("--key", action="append", help="只处理指定内置音色键，可重复")
    q.add_argument("--force", action="store_true", help="install 时强制重新下载")
    q.set_defaults(func=cmd_voice_builtin)

    q = p_sub.add_parser("design", help="按人物设定生成音色候选（本地 VoxCPM2）")
    q.add_argument("--character", required=True, help="人物 ID 或姓名")
    q.add_argument("--count", type=int, default=0, help="候选条数；0 表示用配置默认值")
    q.add_argument("--prompt", help="手写英文音色描述；省略则先由 LLM 生成")
    q.add_argument("--select", type=int, help="生成完成后直接采用第几条候选")
    q.add_argument("--name", help="入库音色名；默认用人物名")
    q.add_argument("--gender", choices=["male", "female", "other", "unknown"])
    q.add_argument("--no-bind", action="store_true", help="入库时不自动绑定到人物")
    q.set_defaults(func=cmd_voice_design)

    def _add_variant_args(q, *, need_name: bool = False) -> None:
        """为音色/变体命令挂上素材参数。"""
        if need_name:
            q.add_argument("--variant-name", required=True, dest="variant_name", help="变体名称，如「默认」「愤怒」")
        q.add_argument("--source", choices=["preset", "cloned", "recorded"], default="preset")
        q.add_argument(
            "--preset",
            choices=["Vivian", "Serena", "Uncle_Fu", "Dylan", "Eric", "Ryan", "Aiden", "Ono_Anna", "Sohee"],
            help="预设音色（source=preset 时必填）",
        )
        q.add_argument("--audio", help="参考音频路径（source=cloned/recorded 时必填）")
        q.add_argument("--duration-ms", type=int, dest="duration_ms", help="参考音频时长；缺省用 ffprobe 探测")
        q.add_argument("--ref-text", dest="ref_text", default=None, help="参考音频对应文本")
        q.add_argument("--consent", action="store_true", help="确认已获授权（真人参考音频必填）")
        q.add_argument("--consent-note", dest="consent_note", default=None, help="授权说明")
        q.add_argument("--emotion-tag", action="append", dest="emotion_tag", help="适用情绪，可重复；默认 default")
        q.add_argument("--source-status", choices=["ready", "pending", "unusable"], dest="source_status")
        q.add_argument("--provider", help="记录 provider 可用性（可选）")

    q = p_sub.add_parser("add")
    q.add_argument("--name", required=True)
    q.add_argument("--gender", choices=["male", "female", "other", "unknown"], default="unknown")
    q.add_argument("--language", default="zh-CN")
    q.add_argument("--tag", action="append")
    q.add_argument("--variant-name", dest="variant_name", default="默认")
    _add_variant_args(q)
    q.set_defaults(func=cmd_voice)

    q = p_sub.add_parser("variant-add")
    q.add_argument("--voice", required=True)
    _add_variant_args(q, need_name=True)
    q.set_defaults(func=cmd_voice)

    q = p_sub.add_parser("variant-update")
    q.add_argument("--voice", required=True)
    q.add_argument("--variant", required=True)
    q.add_argument("--name", dest="variant_name")
    q.add_argument("--emotion-tag", action="append", dest="emotion_tag")
    q.add_argument("--status", dest="source_status", choices=["ready", "pending", "unusable"])
    q.add_argument(
        "--preset",
        choices=["Vivian", "Serena", "Uncle_Fu", "Dylan", "Eric", "Ryan", "Aiden", "Ono_Anna", "Sohee"],
    )
    q.add_argument("--provider")
    q.add_argument("--provider-status", dest="provider_status", choices=["ready", "pending", "unusable"])
    q.set_defaults(func=cmd_voice)

    q = p_sub.add_parser("variant-remove")
    q.add_argument("--voice", required=True)
    q.add_argument("--variant", required=True)
    q.set_defaults(func=cmd_voice)

    q = p_sub.add_parser("set-default")
    q.add_argument("--voice", required=True)
    q.add_argument("--variant", required=True)
    q.set_defaults(func=cmd_voice)

    q = p_sub.add_parser("bind")
    q.add_argument("--character", required=True)
    q.add_argument("--voice", required=True)
    q.add_argument("--adapter", default=None)
    q.set_defaults(func=cmd_voice)

    q = p_sub.add_parser("preview")
    q.add_argument("--voice", required=True)
    q.add_argument("--variant", required=True)
    q.add_argument("--text")
    q.add_argument("--emotion", default="neutral")
    q.add_argument("--instruct")
    q.set_defaults(func=cmd_voice)
