"""音色库、内置音色、设计批次与绑定。"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from fastapi import HTTPException, Query

from novel_drama.composition import ProjectStore
from novel_drama.infrastructure.config import load_config
from novel_drama.models.ids import now_iso

logger = logging.getLogger(__name__)

from novel_drama.interface.http.deps import store_for
from novel_drama.interface.http.routers.common import (
    _resolve_voice_binding,
    _voice_binding_index,
    _watch_background_job,
)


def register_voice_voices_routes(app, project_root: str | None = None, config_path: str | None = None) -> None:
    """注册「音色库、内置音色、设计批次与绑定」相关路由。

    参数:
        app: FastAPI 实例。
        project_root: 默认工程目录（请求未带 path 时使用）。
        config_path: 本机配置文件路径（与 serve 同源）。

    返回:
        无。
    """
    _store = store_for(project_root)

    def _tts_config():
        """读取本机配置（与 serve 同源），供音色相关接口使用。"""
        return load_config(config_path) if config_path else load_config()

    def _voice_board(path: str) -> dict:
        """汇总音色库、人物绑定与内置音色状态。

        参数:
            path: 工程目录。

        返回:
            `{voices, characters, builtin, denoise_config}`。
        """
        from novel_drama.tts import builtin_voices

        store = _store(path)
        cfg = _tts_config()
        denoise_conf = cfg.get("voice.denoise", {}) or {}
        characters = store.load_characters()["characters"]
        voices = store.load_voices()["voices"]
        binding_index = _voice_binding_index(voices)
        return {
            "voices": voices,
            "characters": [
                {
                    "id": item["id"],
                    "name": item["name"],
                    "role": item.get("role"),
                    "gender": item.get("gender"),
                    # 供「按角色设定生成音色」面板带入描述，只读展示，不参与写回
                    "age_range": item.get("age_range"),
                    "personality": item.get("personality") or [],
                    "background": item.get("background"),
                    "speech_style": item.get("speech_style") or {},
                    # 兼容 v2 voice_library_id、v1 voice_id、变体 ID 与旧版内置名称
                    "voice_id": _resolve_voice_binding(item.get("voice_binding"), binding_index),
                }
                for item in characters
            ],
            "builtin": {
                "root": str(builtin_voices.builtin_root(cfg)),
                "items": builtin_voices.rows(cfg),
            },
            "denoise_config": {
                "default_enabled": bool(denoise_conf.get("default_enabled", True)),
                "engine": denoise_conf.get("engine"),
            },
        }

    def _decode_audio(store: ProjectStore, body: dict, variant_id: str, cfg) -> tuple[Path, int, dict]:
        """把前端上传/录音的 base64 音频落盘，转成 24k 单声道 wav，并按需降噪。

        参数:
            store: 工程。
            body: 含 audio_base64 / audio_filename / denoise。
            variant_id: 变体 ID，用作文件名。
            cfg: 本机配置（读 `voice.denoise` 与 `ffmpeg.binary`）。

        返回:
            (wav 绝对路径, 时长毫秒, 降噪记录)。

        异常:
            HTTPException 400: 音频为空、过大、转码失败或时长越界。
        """
        import base64
        import binascii

        from novel_drama.composition import binary_available, probe_duration_ms, run

        raw = str(body.get("audio_base64") or "").strip()
        if "," in raw and raw.startswith("data:"):
            raw = raw.split(",", 1)[1]
        if not raw:
            raise HTTPException(status_code=400, detail={"code": 2, "message": "缺少音频数据"})
        try:
            data = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError):
            raise HTTPException(status_code=400, detail={"code": 2, "message": "音频数据不是合法 base64"})
        if len(data) < 2000:
            raise HTTPException(status_code=400, detail={"code": 2, "message": "录音太短，请至少录制 2 秒"})
        if len(data) > 20 * 1024 * 1024:
            raise HTTPException(status_code=400, detail={"code": 2, "message": "音频超过 20MB"})
        if not binary_available("ffmpeg"):
            raise HTTPException(status_code=500, detail={"code": 4, "message": "未找到 ffmpeg，无法转码录音"})

        suffix = Path(str(body.get("audio_filename") or "recording.webm")).suffix or ".webm"
        ref_dir = store.voice_asset_dir("ref")
        ref_dir.mkdir(parents=True, exist_ok=True)
        upload = ref_dir / f".upload-{variant_id}{suffix}"
        raw_wav = ref_dir / f".raw-{variant_id}.wav"
        out = ref_dir / f"{variant_id}.wav"
        upload.write_bytes(data)
        try:
            run(["ffmpeg", "-v", "error", "-y", "-i", str(upload), "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", str(raw_wav)])
        except Exception as exc:
            upload.unlink(missing_ok=True)
            raw_wav.unlink(missing_ok=True)
            out.unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail={"code": 2, "message": f"录音转码失败: {exc}"})
        upload.unlink(missing_ok=True)

        conf = cfg.get("voice.denoise", {}) or {}
        wanted = body.get("denoise")
        if wanted is None:
            wanted = bool(conf.get("default_enabled", True))
        denoise_info = {
            "enabled": False,
            "engine": conf.get("engine"),
            "original_audio_path": None,
            "attenuation_db": None,
            "applied_at": None,
            "note": "未勾选降噪",
        }
        original = ref_dir / f"{variant_id}.orig.wav"
        if wanted:
            from novel_drama.composition import denoise_wav

            shutil.copyfile(raw_wav, original)
            result = denoise_wav(raw_wav, out, conf, cfg.get("ffmpeg.binary", "ffmpeg"))
            if result.get("enabled"):
                denoise_info = {
                    "enabled": True,
                    "engine": result.get("engine"),
                    "original_audio_path": str(original.relative_to(store.root)),
                    "attenuation_db": result.get("attenuation_db"),
                    "applied_at": now_iso(),
                    "note": result.get("note"),
                }
            else:
                # 降噪失败或过度抑制：丢弃副本，直接用原音作为正式参考音频
                original.unlink(missing_ok=True)
                denoise_info["note"] = result.get("note")
                os.replace(raw_wav, out)
        else:
            os.replace(raw_wav, out)
        raw_wav.unlink(missing_ok=True)

        duration = probe_duration_ms(out)
        if duration < 2000 or duration > 20000:
            out.unlink(missing_ok=True)
            original.unlink(missing_ok=True)
            raise HTTPException(
                status_code=400,
                detail={"code": 2, "message": f"参考音频需 2~20 秒，当前 {duration / 1000:.1f} 秒"},
            )
        return out, duration, denoise_info

    def _variant_from_upload(store: ProjectStore, body: dict, cfg) -> tuple[dict, Path]:
        """构造一个「真人/上传」音色变体并落盘。

        参数:
            store: 工程。
            body: 含 name / gender / ref_text / consent / recorded / denoise 等。
            cfg: 本机配置（透传给转码与降噪）。

        返回:
            (变体 dict, wav 路径)。

        异常:
            HTTPException 400: 未确认授权或参数非法。
        """
        from novel_drama.models import entities

        if not body.get("consent"):
            raise HTTPException(status_code=400, detail={"code": 2, "message": "请先确认你拥有这段录音的使用授权"})
        variant_id = entities.new_id("vav")
        out, duration, denoise_info = _decode_audio(store, body, variant_id, cfg)
        variant = {
            "id": variant_id,
            "name": str(body.get("variant_name") or "默认"),
            "source": "recorded" if body.get("recorded") else "cloned",
            "emotion_tags": ["default"],
            "reference": {
                "audio_path": str(out.relative_to(store.root)),
                "duration_ms": int(duration),
                "text": (body.get("ref_text") or "").strip() or None,
                "denoise": denoise_info,
            },
            "consent": {
                "confirmed": True,
                "note": str(body.get("consent_note") or "Web 音色库上传/录音，用户确认已获授权"),
            },
            "provider_availability": [
                {
                    "provider_id": "indextts_25",
                    "status": "ready",
                    "clone_supported": True,
                    "checked_at": now_iso(),
                }
            ],
            "status": "ready",
            "created_at": now_iso(),
        }
        return variant, out

    @app.get("/api/ui/voices")
    def ui_voices(path: str = Query(...)) -> dict:
        """返回工程音色库、人物绑定与内置音色清单。"""
        return _voice_board(path)

    @app.post("/api/ui/voices/builtin/install")
    def ui_voices_builtin_install(body: dict) -> dict:
        """下载内置音色到外置盘。

        参数:
            body: `{keys?: [...], force?: bool}`。

        返回:
            `{result, items}`。
        """
        from novel_drama.tts import builtin_voices

        cfg = _tts_config()
        keys = body.get("keys") or None
        result = builtin_voices.install(cfg, keys=keys, force=bool(body.get("force")))
        return {"result": result, "items": builtin_voices.rows(cfg)}

    @app.post("/api/ui/voices/builtin/import")
    def ui_voices_builtin_import(body: dict) -> dict:
        """把内置音色导入工程音色库（按名称/路径迁移旧条目，可重复执行）。

        参数:
            body: `{path, keys?: [...]}`。

        返回:
            `{added, updated, skipped, voices, items}`。
        """
        from novel_drama.tts import builtin_voices

        path = body.get("path")
        store = _store(path)
        cfg = _tts_config()
        result = builtin_voices.import_into_project(store, cfg, keys=body.get("keys") or None)
        return {
            "added": result["added"],
            "updated": result["updated"],
            "skipped": result["skipped"],
            "voices": result["voices"],
            "items": builtin_voices.rows(cfg),
        }

    @app.get("/api/ui/voices/builtin/audio")
    def ui_voices_builtin_audio(key: str = Query(...)):
        """返回内置音色的参考音频，供页面试听。

        参数:
            key: 内置音色键（如 `voice_05`）。

        异常:
            HTTPException 404: 未下载或键不存在。
        """
        from fastapi.responses import FileResponse

        from novel_drama.tts import builtin_voices

        target = builtin_voices.audio_path(_tts_config(), key)
        if target is None:
            raise HTTPException(status_code=404, detail={"code": 3, "message": "内置音色未下载", "key": key})
        return FileResponse(target)

    @app.get("/api/ui/voices/audio")
    def ui_voices_audio(path: str = Query(...), voice_id: str = Query(...), variant_id: str = Query("")):
        """返回工程音色的参考音频，供页面试听（支持内置音色的绝对路径）。

        参数:
            path: 工程目录。
            voice_id: 音色 ID。
            variant_id: 变体 ID；为空时用默认变体。

        异常:
            HTTPException 404: 音色/变体/参考音频不存在。
        """
        from fastapi.responses import FileResponse

        from novel_drama.composition import find_variant, find_voice, variant_ref

        store = _store(path)
        voice = find_voice(store.load_voices(), voice_id)
        if voice is None:
            raise HTTPException(status_code=404, detail={"code": 3, "message": "音色不存在", "voice_id": voice_id})
        variant = find_variant(voice, variant_id) if variant_id else find_variant(voice, voice.get("default_variant_id") or "")
        if variant is None:
            raise HTTPException(status_code=404, detail={"code": 3, "message": "变体不存在", "variant_id": variant_id})
        ref, _ = variant_ref(store, variant)
        if not ref or not Path(ref).is_file():
            raise HTTPException(status_code=404, detail={"code": 3, "message": "参考音频不存在"})
        return FileResponse(ref)

    @app.post("/api/ui/voices")
    def ui_voices_add(body: dict) -> dict:
        """上传/录制参考音频，新建音色或给已有音色追加变体。

        参数:
            body: `{path, name?, gender?, voice_id?, audio_base64, audio_filename?,
                    ref_text?, consent, recorded?, variant_name?}`。

        返回:
            `{voice, variant_id, denoise, voices, characters, builtin}`。
        """
        from novel_drama.models import entities

        path = body.get("path")
        store = _store(path)
        table = store.load_voices()
        variant, out = _variant_from_upload(store, body, _tts_config())
        voice_id = body.get("voice_id")
        if voice_id:
            voice = next((item for item in table["voices"] if item["id"] == voice_id), None)
            if voice is None:
                out.unlink(missing_ok=True)
                raise HTTPException(status_code=404, detail={"code": 3, "message": "音色不存在", "voice_id": voice_id})
            voice["variants"].append(variant)
            if not voice.get("default_variant_id"):
                voice["default_variant_id"] = variant["id"]
            voice["updated_at"] = now_iso()
        else:
            name = str(body.get("name") or "").strip()
            if not name:
                out.unlink(missing_ok=True)
                raise HTTPException(status_code=400, detail={"code": 2, "message": "缺少音色名称"})
            gender = body.get("gender")
            voice = {
                "id": entities.new_id("voc"),
                "name": name[:120],
                "engine": None,
                "default_provider_id": None,
                "language": "zh-CN",
                "gender": gender if gender in {"male", "female", "other", "unknown"} else "unknown",
                "tags": ["自建", "浏览器录音" if body.get("recorded") else "上传"],
                "default_variant_id": variant["id"],
                "variants": [variant],
                "status": "ready",
                "revision": 1,
                "created_at": now_iso(),
                "updated_at": now_iso(),
            }
            table["voices"].append(voice)
        try:
            store.save_voices(table)
        except Exception:
            out.unlink(missing_ok=True)
            raise
        board = _voice_board(path)
        return {"voice": voice, "variant_id": variant["id"], "denoise": variant["reference"].get("denoise"), **board}

    @app.post("/api/ui/voices/bind")
    def ui_voices_bind(body: dict) -> dict:
        """把音色绑定到人物（写入 v2 voice_binding）。

        参数:
            body: `{path, character_id, voice_id}`；voice_id 为空表示解绑。

        异常:
            HTTPException 404: 人物或音色不存在。
        """
        path = body.get("path")
        store = _store(path)
        character_id = body.get("character_id")
        voice_id = body.get("voice_id") or None
        characters = store.load_characters()
        target = next((item for item in characters["characters"] if item["id"] == character_id), None)
        if target is None:
            raise HTTPException(status_code=404, detail={"code": 3, "message": "人物不存在", "character_id": character_id})
        if voice_id is None:
            target["voice_binding"] = None
        else:
            voice = next((item for item in store.load_voices()["voices"] if item["id"] == voice_id), None)
            if voice is None:
                raise HTTPException(status_code=404, detail={"code": 3, "message": "音色不存在", "voice_id": voice_id})
            binding = target.get("voice_binding") or {}
            target["voice_binding"] = {
                "adapter": binding.get("adapter") or "placeholder",
                "voice_id": voice["id"],
                "voice_library_id": voice["id"],
                "default_variant_id": voice["default_variant_id"],
                "emotion_variant_map": binding.get("emotion_variant_map") or {},
            }
        target["updated_at"] = now_iso()
        store.save_characters(characters)
        return {"character": target, "characters": _voice_board(path)["characters"]}

    @app.post("/api/ui/voices/design/preview")
    def ui_voices_design_preview(body: dict) -> dict:
        """根据人物档案生成音色描述，供用户确认后再抽卡。

        参数:
            body: `{path, character_id, prompt?}`；prompt 非空表示用户手动改写。

        返回:
            `{profile, source, note, prompt, script, candidates}`。

        异常:
            HTTPException 400/404: 功能未启用或人物不存在。
        """
        from novel_drama.tts import voice_design

        store = _store(body.get("path"))
        cfg = _tts_config()
        design = cfg.get("voice.design") or {}
        if not design.get("enabled", True):
            raise HTTPException(status_code=400, detail={"code": 2, "message": "音色设计未启用（voice.design.enabled=false）"})
        characters = store.load_characters()["characters"]
        character = next((item for item in characters if item["id"] == body.get("character_id")), None)
        if character is None:
            raise HTTPException(status_code=404, detail={"code": 3, "message": "人物不存在", "character_id": body.get("character_id")})
        manual = str(body.get("prompt") or "").strip()
        if manual:
            has_cjk = any("\u4e00" <= char <= "\u9fff" for char in manual)
            result = {
                "profile": None,
                "source": "manual",
                "note": None,
                "prompt": manual,
                "prompt_zh": manual if has_cjk else "",
            }
        else:
            from novel_drama.application.support.llm_calls import build_writing_adapter

            adapter = build_writing_adapter(cfg, store.settings(), action="voice.design.preview")
            built = voice_design.build_profile(adapter, character, cfg)
            result = {
                **built,
                "prompt": built["profile"]["design_prompt"],
                "prompt_zh": built["profile"].get("design_prompt_zh") or "",
            }
        return {
            **result,
            "script": design.get("script"),
            "candidates": int(design.get("candidates", 4)),
            "max_keep_days": int(design.get("max_keep_days", 7)),
        }

    @app.post("/api/ui/voices/design/translate")
    def ui_voices_design_translate(body: dict) -> dict:
        """把中文音色描述翻译成英文，供 VoxCPM2 抽卡使用。

        参数:
            body: `{path, text}`；text 为中文音色描述。

        返回:
            `{english, source}`。

        异常:
            HTTPException 400: 功能未启用、文本为空或模型翻译失败。
        """
        from novel_drama.tts import voice_design

        store = _store(body.get("path"))
        cfg = _tts_config()
        design = cfg.get("voice.design") or {}
        if not design.get("enabled", True):
            raise HTTPException(status_code=400, detail={"code": 2, "message": "音色设计未启用"})
        text = str(body.get("text") or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail={"code": 2, "message": "缺少中文音色描述"})
        from novel_drama.application.support.llm_calls import build_writing_adapter

        adapter = build_writing_adapter(cfg, store.settings(), action="voice.design.translate")
        try:
            english = voice_design.translate_to_english(adapter, text)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"code": 2, "message": str(exc)}) from exc
        return {"english": english, "source": "llm"}

    @app.post("/api/ui/voices/design/batches")
    def ui_voices_design_start(body: dict) -> dict:
        """启动一次音色抽卡批次（后台线程运行，接口立即返回批次 ID）。

        参数:
            body: `{path, character_id, prompt, profile?, count?}`。

        返回:
            批次元数据。

        异常:
            HTTPException 400/404: 参数缺失、环境未就绪或人物不存在。
        """
        from novel_drama.tts import voice_design

        store = _store(body.get("path"))
        cfg = _tts_config()
        design = cfg.get("voice.design") or {}
        if not design.get("enabled", True):
            raise HTTPException(status_code=400, detail={"code": 2, "message": "音色设计未启用"})
        python = str(design.get("python") or "")
        model_dir = str(design.get("model_dir") or "")
        if not Path(python).is_file():
            raise HTTPException(status_code=400, detail={"code": 2, "message": f"VoxCPM2 运行环境不存在：{python}"})
        if not Path(model_dir).is_dir():
            raise HTTPException(status_code=400, detail={"code": 2, "message": f"VoxCPM2 权重目录不存在：{model_dir}"})
        characters = store.load_characters()["characters"]
        character = next((item for item in characters if item["id"] == body.get("character_id")), None)
        if character is None:
            raise HTTPException(status_code=404, detail={"code": 3, "message": "人物不存在", "character_id": body.get("character_id")})
        prompt = str(body.get("prompt") or "").strip()
        if not prompt:
            raise HTTPException(status_code=400, detail={"code": 2, "message": "缺少音色描述"})
        count = int(body.get("count") or design.get("candidates", 4))
        batch = voice_design.create_batch(store, cfg, character, prompt, count, body.get("profile"))

        def read_batch() -> dict:
            """读取抽卡批次并换算候选进度。"""
            meta = voice_design.read_batch(store, batch["batch_id"])
            candidates = meta.get("candidates") or []
            finished = sum(1 for item in candidates if item.get("status") in ("ready", "failed", "done"))
            return {
                "status": meta.get("status"),
                "stage": f"抽卡 · {character.get('name') or '角色'}",
                "progress": {"index": finished, "total": len(candidates) or count},
                "note": meta.get("note"),
            }

        _watch_background_job(
            store,
            "voice",
            f"音色抽卡 · {character.get('name') or '角色'}",
            read_batch,
        )
        return batch

    @app.get("/api/ui/voices/design/batches/{batch_id}")
    def ui_voices_design_status(batch_id: str, path: str = Query(...)) -> dict:
        """轮询批次进度与候选清单。

        参数:
            batch_id: 批次 ID。
            path: 工程目录。

        异常:
            HTTPException 404: 批次不存在。
        """
        from novel_drama.tts import voice_design

        meta = voice_design.read_batch(_store(path), batch_id)
        if not meta:
            raise HTTPException(status_code=404, detail={"code": 3, "message": "批次不存在", "batch_id": batch_id})
        return meta

    @app.post("/api/ui/voices/design/batches/{batch_id}/cancel")
    def ui_voices_design_cancel(batch_id: str, body: dict) -> dict:
        """停止批次后续候选：保留已完成候选，返回 stopped 状态。

        参数:
            batch_id: 批次 ID。
            body: `{path}`。

        异常:
            HTTPException 404: 批次不存在。
        """
        from novel_drama.tts import voice_design

        meta = voice_design.cancel_batch(_store(body.get("path")), batch_id)
        if not meta:
            raise HTTPException(status_code=404, detail={"code": 3, "message": "批次不存在", "batch_id": batch_id})
        return {"batch": meta}

    @app.post("/api/ui/voices/design/batches/{batch_id}/retry")
    def ui_voices_design_retry(batch_id: str, body: dict) -> dict:
        """单条重试：重新生成指定序号的失败/缺失候选。

        参数:
            batch_id: 批次 ID。
            body: `{path, index}`。

        异常:
            HTTPException 400: 批次仍在生成或该候选不需要重试。
        """
        from novel_drama.tts import voice_design

        store = _store(body.get("path"))
        try:
            return voice_design.retry_candidate(store, _tts_config(), batch_id, int(body.get("index") or 0))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"code": 2, "message": str(exc)}) from exc

    @app.post("/api/ui/voices/design/batch/plan")
    def ui_voices_batch_plan(body: dict) -> dict:
        """预览批量生成范围：待生成 N 个、跳过 M 个及原因，附带可选章节。

        参数:
            body: `{path, scope, chapter_id?}`。
        """
        from novel_drama.tts import voice_batch

        store = _store(body.get("path"))
        try:
            return voice_batch.plan(store, str(body.get("scope") or "unbound"), body.get("chapter_id"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"code": 2, "message": str(exc)}) from exc

    @app.post("/api/ui/voices/design/batch")
    def ui_voices_batch_start(body: dict) -> dict:
        """启动一键批量生成音色（逐角色串行）。

        参数:
            body: `{path, scope, chapter_id?, count?}`。
        """
        from novel_drama.tts import voice_batch

        store = _store(body.get("path"))
        scope = str(body.get("scope") or "unbound")
        try:
            job = voice_batch.start_job(
                store,
                _tts_config(),
                scope,
                body.get("chapter_id"),
                body.get("count"),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"code": 2, "message": str(exc)}) from exc

        def read_batch() -> dict:
            """读取批量任务并按条目数换算进度。"""
            latest = voice_batch.read_job(store, job["job_id"])
            items = latest.get("items") or []
            return {
                "status": latest.get("status"),
                "stage": latest.get("current") or "批量生成音色",
                "progress": {"index": latest.get("done") or 0, "total": len(items)},
                "note": latest.get("note"),
            }

        _watch_background_job(store, "voice", "批量抽取音色", read_batch)
        return job

    @app.get("/api/ui/voices/design/batch/latest")
    def ui_voices_batch_latest(path: str | None = None) -> dict:
        """最近一次批量任务：页面刷新后据状态展示或续跑。"""
        from novel_drama.tts import voice_batch

        return {"job": voice_batch.latest_job(_store(path))}

    @app.post("/api/ui/voices/design/batch/{job_id}/cancel")
    def ui_voices_batch_cancel(job_id: str, body: dict) -> dict:
        """停止批量任务：当前角色停止，已完成角色保留。"""
        from novel_drama.tts import voice_batch

        job = voice_batch.cancel_job(_store(body.get("path")), job_id)
        if not job:
            raise HTTPException(status_code=404, detail={"code": 3, "message": "批量任务不存在"})
        return {"job": job}

    @app.post("/api/ui/voices/design/batch/{job_id}/resume")
    def ui_voices_batch_resume(job_id: str, body: dict) -> dict:
        """续跑或重试批量任务：只处理未完成与失败的角色。

        参数:
            body: `{path, character_ids?}`；character_ids 为空时重试全部失败项。
        """
        from novel_drama.tts import voice_batch

        try:
            job = voice_batch.resume_job(
                _store(body.get("path")), _tts_config(), job_id, body.get("character_ids")
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"code": 2, "message": str(exc)}) from exc
        return {"job": job}

    @app.get("/api/ui/voices/casting/overview")
    def ui_voices_casting_overview(
        path: str = Query(...), q: str = "", filter_name: str = Query("need", alias="filter")
    ) -> dict:
        """音色编排总览：默认只返回需要处理的角色，正常角色只给摘要。

        参数:
            path: 工程目录。
            q: 搜索词（角色名或当前音色名）。
            filter_name: need/unbound/chapter/duplicate/all。
        """
        from novel_drama.tts import voice_casting

        store = _store(path)
        data = voice_casting.overview(store, q, filter_name)
        data["undo"] = voice_casting.undo_state(store)
        return data

    @app.post("/api/ui/voices/casting/preview")
    def ui_voices_casting_preview(body: dict) -> dict:
        """自动编排预览：每个角色匹配到哪个音色、为什么，以及需对齐的章节覆盖。"""
        from novel_drama.tts import voice_casting

        return voice_casting.preview(_store(body.get("path")))

    @app.post("/api/ui/voices/casting/apply")
    def ui_voices_casting_apply(body: dict) -> dict:
        """应用自动编排建议并写撤销日志。

        参数:
            body: `{path, character_ids?}`；为空时应用全部有变化的角色。
        """
        from novel_drama.tts import voice_casting

        store = _store(body.get("path"))
        result = voice_casting.apply(store, body.get("character_ids"))
        return {
            **result,
            "overview": voice_casting.overview(store, "", "need"),
            "undo": voice_casting.undo_state(store),
        }

    @app.post("/api/ui/voices/casting/bind")
    def ui_voices_casting_bind(body: dict) -> dict:
        """单行替换角色音色（voice_id 为空表示解绑），并写撤销日志。

        异常:
            HTTPException 400/404: 角色或音色不存在 / 没有可用变体。
        """
        from novel_drama.tts import voice_casting

        store = _store(body.get("path"))
        try:
            result = voice_casting.bind_one(store, body.get("character_id"), body.get("voice_id"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"code": 2, "message": str(exc)}) from exc
        return {
            **result,
            "overview": voice_casting.overview(store, "", "need"),
            "undo": voice_casting.undo_state(store),
        }

    @app.post("/api/ui/voices/undo/last")
    def ui_voices_undo_last(body: dict) -> dict:
        """撤销最近一次自动编排 / 自动生成 / 单行替换。

        参数:
            body: `{path, kind?}`；kind 为空时撤销最近一条记录。
        """
        from novel_drama.tts import voice_casting

        store = _store(body.get("path"))
        try:
            result = voice_casting.undo_last(store, body.get("kind") or None)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"code": 2, "message": str(exc)}) from exc
        # 撤销已经完成；章节文件损坏时概览可能读不出来，不能因此把整次撤销打成 500
        overview_error = None
        try:
            overview = voice_casting.overview(store, "", "need")
        except Exception as exc:  # 概览失败只影响展示
            logger.warning("撤销后音色概览读取失败：%s", exc, exc_info=True)
            overview = None
            overview_error = f"音色编排概览读取失败：{exc}；撤销结果已保存，可修复损坏章节后刷新"
        return {
            **result,
            "overview": overview,
            "overview_error": overview_error,
            "undo": voice_casting.undo_state(store),
        }

    @app.get("/api/ui/voices/design/history")
    def ui_voices_design_history(path: str = Query(...), character_id: str = Query(...)) -> dict:
        """从磁盘恢复某角色的历史候选批次（页面刷新后使用）。

        参数:
            path: 工程目录。
            character_id: 人物 ID。

        返回:
            `{batches: [...]}`，倒序；含批次 ID、生成时间、候选是否仍在、已采用序号。
        """
        from novel_drama.tts import voice_design

        return {"batches": voice_design.list_batches(_store(path), character_id)}

    @app.get("/api/ui/voices/design/audio")
    def ui_voices_design_audio(path: str = Query(...), batch_id: str = Query(...), index: int = Query(...)):
        """返回候选音频，供页面逐条试听。

        异常:
            HTTPException 404: 批次/候选/文件不存在。
        """
        from fastapi.responses import FileResponse

        from novel_drama.tts import voice_design

        store = _store(path)
        meta = voice_design.read_batch(store, batch_id)
        candidate = next((item for item in meta.get("candidates") or [] if int(item.get("index")) == index), None)
        if candidate is None:
            raise HTTPException(status_code=404, detail={"code": 3, "message": "候选不存在", "index": index})
        target = voice_design.batch_dir(store, batch_id) / candidate["file"]
        if not target.is_file():
            raise HTTPException(status_code=404, detail={"code": 3, "message": "候选音频已过期"})
        return FileResponse(target)

    @app.post("/api/ui/voices/design/select")
    def ui_voices_design_select(body: dict) -> dict:
        """采用某条候选：固化参考音并（可选）绑定人物。

        参数:
            body: `{path, batch_id, index, name, gender, gender_source, bind, voice_id?}`。

        异常:
            HTTPException 400/404: 批次未就绪或参数非法。
        """
        from novel_drama.tts import voice_design

        path = body.get("path")
        store = _store(path)
        try:
            result = voice_design.select_candidate(
                store,
                str(body.get("batch_id") or ""),
                int(body.get("index") or 0),
                str(body.get("name") or ""),
                str(body.get("gender") or "unknown"),
                str(body.get("gender_source") or "manual"),
                bool(body.get("bind")),
                body.get("voice_id"),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"code": 2, "message": str(exc)}) from exc
        return {**result, **_voice_board(path)}

    @app.delete("/api/ui/voices/design/batches/{batch_id}")
    def ui_voices_design_delete(batch_id: str, path: str = Query(...)) -> dict:
        """手动清理一个候选批次。"""
        from novel_drama.tts import voice_design

        return {"deleted": voice_design.delete_batch(_store(path), batch_id)}

    @app.post("/api/ui/voices/delete")
    def ui_voices_delete(body: dict) -> dict:
        """删除音色，并解绑引用它的角色。

        参数:
            body: `{path, voice_id}`。

        异常:
            HTTPException 404: 音色不存在。
        """
        path = body.get("path")
        store = _store(path)
        voice_id = body.get("voice_id")
        table = store.load_voices()
        before = len(table["voices"])
        table["voices"] = [item for item in table["voices"] if item["id"] != voice_id]
        if len(table["voices"]) == before:
            raise HTTPException(status_code=404, detail={"code": 3, "message": "音色不存在", "voice_id": voice_id})
        store.save_voices(table)
        characters = store.load_characters()
        changed = False
        for item in characters["characters"]:
            binding = item.get("voice_binding") or {}
            if voice_id in (binding.get("voice_library_id"), binding.get("voice_id")):
                item["voice_binding"] = None
                item["updated_at"] = now_iso()
                changed = True
        if changed:
            store.save_characters(characters)
        return _voice_board(path)
