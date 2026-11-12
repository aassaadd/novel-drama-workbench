"""背景音乐生成：项目内 Stable Audio Open 1.0 / ComfyUI 的 ACE-Step 1.5。

两条路径的差异只在「怎么拿到一条原始 wav」：
- `local`（默认）：项目内 provider（独立 venv + 常驻 worker，MPS），最长约 47 秒；
- `comfyui`：ACE-Step 1.5，支持 10~180 秒长曲。

两档（叙事 / 铺底）、响度归一、首尾裁剪、循环填充与生成记录完全共用，
见 `render_bgm` 里的后处理段；provider 选择读 `bgm.provider`，CLI 可显式覆盖。
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from ..adapters.base import AdapterError, ErrorCode
from ..config import AppConfig
from ..models.ids import now_iso
from ..store import ProjectStore
from .bgm import add_cue, load_cues, save_cues
from .comfy_media import load_manifest, resolve_template, seed_for, templates_dir
from .ffmpeg import probe_duration_ms, run as ffmpeg_run
from .normalize import measure_loudness
from .runner import _check_models, _comfy_gateway, _release_comfy, _submit_and_fetch

#: 两档用途（对外文案固定为「叙事 / 铺底」，不暴露 audio codes 实现词）
MODE_NARRATIVE = "narrative"
MODE_BED = "bed"
MODE_AUTO = "auto"
MODE_LABELS = {MODE_NARRATIVE: "叙事", MODE_BED: "铺底"}
MODE_CHOICES = (MODE_AUTO, MODE_NARRATIVE, MODE_BED)

#: 铺底后处理参数（可按工程需要调整，保持集中定义便于阅读）
BED_SILENCE_THRESHOLD_DBFS = -50.0  # 低于该电平视为静音
BED_LEAD_KEEP_S = 0.25  # 裁掉开头静音后至少保留的起点长度
BED_FADE_IN_S = 0.8  # 铺底淡入时长
LOUDNESS_TARGET_LUFS = -16.0
TRUE_PEAK_DBTP = -1.5

#: 长镜头阈值：没有台词但时长达到该值，同样按铺底处理
LONG_BED_SECONDS = 60.0

#: 叙事档首尾处理参数（只处理静音与断点，不改变中段节奏）
NARRATIVE_TRIM_THRESHOLD_DBFS = -50.0  # 低于该电平判为近静音
NARRATIVE_TRIM_KEEP_HEAD_S = 0.15  # 开头保留的自然起音/混响
NARRATIVE_TRIM_KEEP_TAIL_S = 0.2  # 结尾保留的余韵
NARRATIVE_TAIL_FADE_S = 0.4  # 结尾短淡出，避免循环/衔接断感

_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")
_RECORD_LIMIT = 200


def resolve_mode(
    item_mode: str | None,
    requested_mode: str | None,
    has_dialogue: bool | None,
    duration_s: float,
) -> dict:
    """按优先级决定本次生成用哪一档。

    参数:
        item_mode: 条目（cue / BGM 计划）上显式指定的模式，最高优先级。
        requested_mode: 页面或 CLI 传入的模式；`auto` 视为未指定。
        has_dialogue: 该段是否有台词；None 表示拿不到场景信息。
        duration_s: 本次生成时长（秒），用于长镜头推断。

    返回:
        `{"mode", "label", "source", "reason"}`；
        source 取值 item / request / inferred，reason 为可读中文说明。

    推断规则（只在两级显式值都缺省时启用）：
    1. 有台词的段落优先铺底——对白需要垫底，不抢语音频段；
    2. 没有台词但时长 ≥ LONG_BED_SECONDS 的长镜头也按铺底，避免长时间叙事音乐疲劳；
    3. 其余无台词的情绪段按叙事处理，保留层次与推进。
    """
    if item_mode in MODE_LABELS:
        return {"mode": item_mode, "label": MODE_LABELS[item_mode], "source": "item", "reason": "条目已指定档位"}
    if requested_mode in MODE_LABELS:
        return {
            "mode": requested_mode,
            "label": MODE_LABELS[requested_mode],
            "source": "request",
            "reason": "按页面/CLI 传参",
        }
    if has_dialogue:
        return {
            "mode": MODE_BED,
            "label": MODE_LABELS[MODE_BED],
            "source": "inferred",
            "reason": "该段有台词，按铺底处理",
        }
    if float(duration_s or 0) >= LONG_BED_SECONDS:
        return {
            "mode": MODE_BED,
            "label": MODE_LABELS[MODE_BED],
            "source": "inferred",
            "reason": f"无台词但时长 ≥ {LONG_BED_SECONDS:g} 秒的长镜头，按铺底处理",
        }
    return {
        "mode": MODE_NARRATIVE,
        "label": MODE_LABELS[MODE_NARRATIVE],
        "source": "inferred",
        "reason": "无台词的情绪段落，按叙事处理",
    }


def plan_context(store: ProjectStore, chapter_id: str, plan_id: str | None) -> dict | None:
    """读取某条 BGM 计划所在的镜头信息，用于自动推断档位。

    参数:
        store: 工程。
        chapter_id: 章节 ID。
        plan_id: BGM 计划 ID（`bgp_...`），可为空。

    返回:
        `{"plan", "shot", "has_dialogue", "duration_s"}`；找不到时返回 None。
    """
    if not plan_id:
        return None
    try:
        script = store.load_script(chapter_id)
    except FileNotFoundError:
        return None
    for shot in script.get("shots") or []:
        plan = shot.get("bgm")
        if not isinstance(plan, dict) or plan.get("id") != plan_id:
            continue
        has_dialogue = any(str((line or {}).get("text") or "").strip() for line in shot.get("lines") or [])
        return {
            "plan": plan,
            "shot": shot,
            "has_dialogue": has_dialogue,
            "duration_s": float(plan.get("duration_hint_ms") or 0) / 1000 if plan.get("duration_hint_ms") else None,
        }
    return None


def _fill_template(value: object, params: dict) -> object:
    """递归把模板里的 {{name}} 替换成实际参数（保留数字类型）。"""
    if isinstance(value, dict):
        return {key: _fill_template(item, params) for key, item in value.items()}
    if isinstance(value, list):
        return [_fill_template(item, params) for item in value]
    if isinstance(value, str):
        match = _PLACEHOLDER.fullmatch(value)
        if match:
            return params.get(match.group(1), value)
        return _PLACEHOLDER.sub(lambda found: str(params.get(found.group(1), found.group(0))), value)
    return value


def _music_workflow(config: AppConfig, template_id: str, params: dict, codes: bool) -> tuple[dict, str]:
    """读取音乐模板并填参，同时按档位切换 audio codes。

    参数:
        config: 本机配置。
        template_id: 模板 ID。
        params: 占位符参数。
        codes: 是否开启 audio codes（叙事 True / 铺底 False）。

    返回:
        `(API 格式工作流, 实际模板 ID)`。

    异常:
        AdapterError(INVALID_INPUT): 模板缺失、损坏或占位符未解析。
    """
    directory = templates_dir(config)
    manifest = load_manifest(directory)
    resolved = resolve_template(template_id, "music", manifest)
    actual_id = resolved["template_id"]
    entry = resolved["template"]
    path = Path(str(entry.get("file") or f"{actual_id}.json"))
    if not path.is_absolute():
        path = directory / path
    if not path.exists():
        raise AdapterError(ErrorCode.INVALID_INPUT, f"未找到音乐模板 {actual_id}：{path}（执行 ndw comfy import 生成）")
    try:
        template = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AdapterError(ErrorCode.INVALID_INPUT, f"音乐模板 {actual_id} 读取失败：{exc}") from exc
    if not isinstance(template, dict) or not template:
        raise AdapterError(ErrorCode.INVALID_INPUT, f"音乐模板 {actual_id} 必须是 API 格式的非空 JSON 对象")

    filled = _fill_template(template, params)
    # 档位开关落在文本编码节点上：叙事开 codes，铺底关 codes
    for node in filled.values():
        if not isinstance(node, dict):
            continue
        class_type = str(node.get("class_type") or "")
        if class_type.startswith("TextEncodeAceStep"):
            node.setdefault("inputs", {})["generate_audio_codes"] = bool(codes)
    leftover = sorted({match.group(1) for match in _PLACEHOLDER.finditer(json.dumps(filled, ensure_ascii=False))})
    if leftover:
        raise AdapterError(ErrorCode.INVALID_INPUT, f"音乐模板 {actual_id} 仍存在未解析占位符：{', '.join(leftover)}")
    return filled, actual_id


def _convert_wav(src: Path, dst: Path) -> None:
    """把模型输出（flac）转成 48kHz / 24bit / 立体声 wav。"""
    ffmpeg_run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
            "-i", str(src), "-ar", "48000", "-ac", "2", "-c:a", "pcm_s24le", str(dst),
        ],
        timeout_s=600,
    )


def _parse_loudnorm(stderr: str) -> dict:
    """从 ffmpeg stderr 截出 loudnorm 的 JSON 测量块。"""
    start = stderr.rfind("{")
    end = stderr.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        return json.loads(stderr[start : end + 1])
    except json.JSONDecodeError:
        return {}


def _loudnorm_filter(measured: dict) -> str:
    """拼 loudnorm 第二遍滤镜：有首遍测量值时走线性增益，否则退回动态模式。"""
    norm = f"loudnorm=I={LOUDNESS_TARGET_LUFS}:TP={TRUE_PEAK_DBTP}:LRA=11"
    if measured.get("input_i") not in (None, "", "-inf"):
        norm += (
            f":measured_I={measured.get('input_i')}:measured_TP={measured.get('input_tp')}"
            f":measured_LRA={measured.get('input_lra')}:measured_thresh={measured.get('input_thresh')}"
            f":offset={measured.get('target_offset')}:linear=true"
        )
    return norm


def _apply_loudnorm(src: Path, dst: Path, prefix: str) -> dict:
    """两遍法 loudnorm，prefix 为归一前可选的处理链（如铺底的裁静音 + 淡入）。

    参数:
        src: 输入音频。
        dst: 输出 wav。
        prefix: 前置滤镜链；为空表示只做响度归一。

    返回:
        `{"measured_lufs", "target_lufs", "target_true_peak"}`。
    """
    base = f"{prefix}," if prefix else ""
    measure_filter = f"{base}loudnorm=I={LOUDNESS_TARGET_LUFS}:TP={TRUE_PEAK_DBTP}:LRA=11:print_format=json"
    first = ffmpeg_run(
        ["ffmpeg", "-hide_banner", "-nostats", "-nostdin", "-i", str(src), "-af", measure_filter, "-f", "null", "-"],
        timeout_s=900,
    )
    measured = _parse_loudnorm(first.stderr)
    ffmpeg_run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
            "-i", str(src), "-af", f"{base}{_loudnorm_filter(measured)}",
            "-ar", "48000", "-ac", "2", "-c:a", "pcm_s24le", str(dst),
        ],
        timeout_s=900,
    )
    # 线性增益受真峰值限制时可能到不了 -16 LUFS；差超过 0.3dB 时用动态 loudnorm 补，最多两遍
    for _ in range(2):
        actual = measure_loudness(dst).get("lufs")
        if not isinstance(actual, (int, float)) or LOUDNESS_TARGET_LUFS - actual <= 0.3:
            break
        retry = dst.with_name(dst.stem + ".dyn.wav")
        ffmpeg_run(
            [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
                "-i", str(dst), "-af",
                f"loudnorm=I={LOUDNESS_TARGET_LUFS}:TP={TRUE_PEAK_DBTP}:LRA=11",
                "-ar", "48000", "-ac", "2", "-c:a", "pcm_s24le", str(retry),
            ],
            timeout_s=900,
        )
        os.replace(retry, dst)
    return {
        "measured_lufs": measured.get("input_i"),
        "target_lufs": LOUDNESS_TARGET_LUFS,
        "target_true_peak": TRUE_PEAK_DBTP,
    }


def _bed_postprocess(src: Path, dst: Path) -> dict:
    """铺底后处理：裁开头静音 → 淡入 → 两遍 loudnorm（这套处理只属于铺底档）。

    参数:
        src: 原始 wav。
        dst: 处理后 wav。

    返回:
        `{"trim_keep_s", "fade_in_s", "threshold_dbfs", "target_lufs"}`。
    """
    prefix = (
        "silenceremove=start_periods=1:start_duration=0.05"
        f":start_threshold={BED_SILENCE_THRESHOLD_DBFS}dB:start_silence={BED_LEAD_KEEP_S},"
        f"afade=t=in:st=0:d={BED_FADE_IN_S}"
    )
    info = _apply_loudnorm(src, dst, prefix)
    return {
        "trim_keep_s": BED_LEAD_KEEP_S,
        "fade_in_s": BED_FADE_IN_S,
        "threshold_dbfs": BED_SILENCE_THRESHOLD_DBFS,
        "target_lufs": LOUDNESS_TARGET_LUFS,
        "measured_lufs": info.get("measured_lufs"),
    }


def _narrative_normalize(src: Path, dst: Path) -> dict:
    """叙事档响度归一：只做 loudnorm，首尾处理由 `_trim_narrative_edges` 单独完成。

    参数:
        src: 输入 wav。
        dst: 归一后 wav。

    返回:
        `{"target_lufs", "target_true_peak", "measured_lufs"}`。
    """
    return _apply_loudnorm(src, dst, "")


def _silence_profile(path: Path) -> dict:
    """测量音频首尾近静音时长与有效时长（阈值 -50 dBFS）。

    参数:
        path: 音频文件。

    返回:
        `{"duration_s", "head_silence_s", "tail_silence_s", "effective_s"}`；
        无法解析时各项为 None。
    """
    result = ffmpeg_run(
        [
            "ffmpeg", "-hide_banner", "-nostats", "-nostdin", "-i", str(path),
            "-af", f"silencedetect=noise={NARRATIVE_TRIM_THRESHOLD_DBFS}dB:d=0.05",
            "-f", "null", "-",
        ],
        timeout_s=300,
    )
    duration = probe_duration_ms(path) / 1000
    head = 0.0
    tail = 0.0
    current_start = None
    for line in result.stderr.splitlines():
        if "silence_start:" in line:
            current_start = float(line.split("silence_start:")[1].strip())
        elif "silence_end:" in line and current_start is not None:
            end = float(line.split("silence_end:")[1].split("|")[0].strip())
            if current_start <= 0.01:
                head = end
            if end >= duration - 0.05:
                tail = max(0.0, duration - current_start)
            current_start = None
    return {
        "duration_s": round(duration, 2),
        "head_silence_s": round(head, 2),
        "tail_silence_s": round(tail, 2),
        "effective_s": round(max(0.0, duration - head - tail), 2),
    }


def _trim_narrative_edges(src: Path, dst: Path) -> dict:
    """叙事档首尾处理：裁近静音（保留极短起音/余韵）+ 结尾短淡出。

    结尾选择短淡出而不是与开头环形交叉淡化，原因：
    环形交叉淡化会叠加开头素材、改变长度和听感，只适合确定要循环的片段；
    短淡出对循环和普通衔接都安全，也不改动中段节奏。

    参数:
        src: 输入 wav。
        dst: 处理后 wav。

    返回:
        处理参数（阈值、保留时长、淡出时长）。
    """
    trim = (
        f"silenceremove=start_periods=1:start_duration=0.05"
        f":start_threshold={NARRATIVE_TRIM_THRESHOLD_DBFS}dB:start_silence={NARRATIVE_TRIM_KEEP_HEAD_S},"
        "areverse,"
        f"silenceremove=start_periods=1:start_duration=0.05"
        f":start_threshold={NARRATIVE_TRIM_THRESHOLD_DBFS}dB:start_silence={NARRATIVE_TRIM_KEEP_TAIL_S},"
        "areverse,"
        f"areverse,afade=t=in:st=0:d={NARRATIVE_TAIL_FADE_S},areverse"
    )
    ffmpeg_run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
            "-i", str(src), "-af", trim, "-ar", "48000", "-ac", "2", "-c:a", "pcm_s24le", str(dst),
        ],
        timeout_s=600,
    )
    return {
        "method": "trim_silence+fade_out",
        "threshold_dbfs": NARRATIVE_TRIM_THRESHOLD_DBFS,
        "keep_head_s": NARRATIVE_TRIM_KEEP_HEAD_S,
        "keep_tail_s": NARRATIVE_TRIM_KEEP_TAIL_S,
        "tail_fade_s": NARRATIVE_TAIL_FADE_S,
    }


def _records_path(root: str | Path, chapter_id: str) -> Path:
    """生成记录文件路径（无 schema 约束，便于直接追加复盘信息）。"""
    return Path(root) / "bgm" / f"{chapter_id}.records.json"


def load_generation_records(root: str | Path, chapter_id: str) -> list[dict]:
    """读取某章的历史生成记录；缺失或损坏时返回空列表。

    参数:
        root: 工程根目录。
        chapter_id: 章节 ID。

    返回:
        记录列表（旧记录在前）。
    """
    path = _records_path(root, chapter_id)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    records = data.get("records") if isinstance(data, dict) else None
    return records if isinstance(records, list) else []


def _append_record(root: str | Path, chapter_id: str, record: dict) -> Path:
    """原子追加一条生成记录，只保留最近 `_RECORD_LIMIT` 条。"""
    path = _records_path(root, chapter_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    records = load_generation_records(root, chapter_id) + [record]
    payload = {"schema_version": 1, "chapter_id": chapter_id, "records": records[-_RECORD_LIMIT:]}
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def _cue_mode(cue: dict | None) -> str | None:
    """取 cue 上显式档位；兼容空值与未知值。"""
    if not isinstance(cue, dict):
        return None
    mode = cue.get("mode")
    return mode if mode in MODE_LABELS else None


def _find_plan_cue(cues: list[dict], plan_id: str | None) -> dict | None:
    """找某条计划对应的主 cue（segment_index 为空视为主段落）。"""
    if not plan_id:
        return None
    for cue in cues:
        ref = cue.get("plan_ref") or {}
        if ref.get("plan_id") == plan_id and int(ref.get("segment_index") or 0) == 0:
            return cue
    return None


def _upsert_cue(
    root: str | Path,
    chapter_id: str,
    plan_id: str | None,
    asset_id: str,
    mode: str,
    name: str,
    start_ms: int,
    normalize: bool,
    trim_edges: bool,
    prompt_en: str | None = None,
) -> dict:
    """把生成结果写回 cue：有计划时更新原 cue，否则追加一条手工 cue。

    `normalize` 与 `trim_edges` 一并落盘，作为下次同一条目生成的条目级开关；
    关联计划时把英文提示词也记到 cue 上，便于追溯。
    """
    cues = load_cues(root, chapter_id)
    existing = _find_plan_cue(cues, plan_id)
    if existing is not None:
        existing.update(
            {
                "asset_id": asset_id,
                "mode": mode,
                "status": "asset_ready",
                "normalize": normalize,
                "trim_edges": trim_edges,
            }
        )
        if prompt_en:
            existing["prompt_en"] = prompt_en
        if name:
            existing["name"] = name
        save_cues(root, chapter_id, cues)
        return existing
    cue = add_cue(root, chapter_id, asset_id, start_ms=start_ms, mode=mode, name=name)
    cues = load_cues(root, chapter_id)
    for item in cues:
        if item.get("id") != cue["id"]:
            continue
        item["normalize"] = normalize
        item["trim_edges"] = trim_edges
        if prompt_en:
            item["prompt_en"] = prompt_en
        if plan_id:
            # 关联计划的 cue 必须带 plan_ref，后续 sync 才会按计划更新而不是再建一条
            item["plan_ref"] = {"plan_id": plan_id, "shot_id": None, "segment_index": None}
        break
    save_cues(root, chapter_id, cues)
    return next(item for item in cues if item.get("id") == cue["id"])


def _generate_comfy(
    config: AppConfig,
    chapter_id: str,
    mode: str,
    text: str,
    duration: float,
    seed: int,
    item_id: str,
    bgm_dir: Path,
) -> tuple[Path, dict]:
    """走 ComfyUI / ACE-Step 生成一条原始音频。

    参数:
        config: 本机配置。
        chapter_id: 章节 ID。
        mode: 叙事 / 铺底。
        text: 英文音乐提示词（标注阶段产出）。
        duration: 时长（秒）。
        seed: 实际种子。
        item_id: 文件前缀。
        bgm_dir: 输出目录。

    返回:
        `(原始音频路径, 生成元信息)`。

    异常:
        AdapterError: ComfyUI 不可达、模板/模型缺失或生成失败。
    """
    gateway = _comfy_gateway(config)
    status = gateway.health()
    if not status.ok:
        raise AdapterError(
            ErrorCode.UNAVAILABLE,
            f"ComfyUI 未连接：{status.detail or '请确认服务已启动、地址正确'}",
            retryable=True,
        )

    template_id = str(config.get("comfyui.templates.music") or "ace_step_1_5")
    manifest = load_manifest(templates_dir(config))
    resolved = resolve_template(template_id, "music", manifest)
    _check_models(config, resolved["template"], resolved["template_id"])

    params = {
        "prompt": text,
        "lyrics": "",
        "duration": duration,
        "bpm": 70,
        "seed": seed,
        "timesignature": "4",
        "language": "en",
        "keyscale": "D minor",
        "filename_prefix": f"ndw_bgm/{item_id}",
    }
    workflow, actual_template = _music_workflow(config, template_id, params, mode == MODE_NARRATIVE)

    started = time.perf_counter()
    try:
        produced, prompt_id = _submit_and_fetch(gateway, config, "audio", workflow, item_id, 1, bgm_dir)
    finally:
        # 音乐生成结束（含失败）都走官方 /free 卸载模型，避免 ACE-Step 权重常驻
        _release_comfy(gateway, config, f"music-{chapter_id}")
    return produced, {
        "provider": "comfyui",
        "model": "ACE-Step 1.5",
        "template_id": actual_template,
        "prompt_id": prompt_id,
        "seed": seed,
        "steps": None,
        "cfg": None,
        "latency_ms": None,
        "load_s": None,
        "peak_mib": None,
        "generation_s": time.perf_counter() - started,
    }


def _generate_local(
    config: AppConfig,
    mode: str,
    prompt_en: str,
    duration: float,
    seed: int,
    item_id: str,
    bgm_dir: Path,
    *,
    steps: int | None,
    cfg: float | None,
) -> tuple[Path, dict]:
    """走项目内 Stable Audio Open 1.0 生成一条原始音频。

    运行时只读取已存英文提示词（标注阶段产出），不再做中→英改写。
    产物落到 `<item_id>_s<seed>.src.wav`，后处理阶段会把它转成最终 wav 并删除。

    参数:
        config: 本机配置。
        mode: 叙事 / 铺底（当前只影响后处理，提示词相同）。
        prompt_en: 英文音乐提示词（已由生成阶段落盘）。
        duration: 时长（秒）。
        seed: 实际种子。
        item_id: 文件前缀。
        bgm_dir: 输出目录。
        steps: 采样步数；None 用配置默认。
        cfg: cfg；None 用配置默认。

    返回:
        `(原始音频路径, 生成元信息)`。

    异常:
        AdapterError: provider 不可用、模型缺失、时长越界或生成失败。
    """
    from ..bgm.registry import build_providers, pick_provider
    from ..bgm.types import BAD_REQUEST as BGM_BAD_REQUEST
    from ..bgm.types import BgmError, BgmRequest

    providers = build_providers(config)
    pid, provider = pick_provider(config, providers)
    health = provider.health()
    if not health.ok:
        raise AdapterError(ErrorCode.UNAVAILABLE, f"本地配乐 provider 不可用：{health.detail}", retryable=False)

    source = bgm_dir / f"{item_id}_s{seed}.src.wav"
    started = time.perf_counter()
    try:
        result = provider.generate(
            BgmRequest(
                prompt=prompt_en,
                output_path=str(source),
                duration_s=duration,
                steps=steps,
                cfg=cfg,
                seed=seed,
            )
        )
    except BgmError as exc:
        code = ErrorCode.INVALID_INPUT if exc.code == BGM_BAD_REQUEST else ErrorCode.UNAVAILABLE
        message = f"本地配乐生成失败：{exc.message}"
        if exc.hint:
            message += f"（{exc.hint}）"
        raise AdapterError(code, message, retryable=code != ErrorCode.INVALID_INPUT) from exc
    finally:
        provider.close()

    return source, {
        "provider": "local",
        "provider_id": pid,
        "model": result.model,
        "prompt_en": prompt_en,
        "rewrite_mode": None,
        "rewrite_note": None,
        "seed": result.seed,
        "steps": result.steps,
        "cfg": result.cfg,
        "latency_ms": result.latency_ms,
        "load_s": result.load_s,
        "peak_mib": result.peak_mib,
        "sample_rate": result.sample_rate,
        "channels": result.channels,
        "generation_s": time.perf_counter() - started,
    }


def render_bgm(
    store: ProjectStore,
    config: AppConfig,
    chapter_id: str,
    prompt: str,
    *,
    duration_s: float | None = None,
    requested_mode: str | None = None,
    item_mode: str | None = None,
    requested_normalize: bool | None = None,
    item_normalize: bool | None = None,
    requested_trim: bool | None = None,
    item_trim: bool | None = None,
    plan_id: str | None = None,
    seed: int | None = None,
    name: str | None = None,
    start_ms: int = 0,
    add_to_cue: bool = True,
    provider: str | None = None,
    prompt_en: str | None = None,
    allow_llm: bool = True,
    allow_rewrite: bool = False,
    steps: int | None = None,
    cfg: float | None = None,
) -> dict:
    """生成一段配乐并按档位处理、记账、写 cue。

    参数:
        store: 工程。
        config: 本机配置。
        chapter_id: 章节 ID。
        prompt: 中文音乐描述（仅用于记录/展示；生成只用英文提示词）。
        duration_s: 生成时长（秒）；local 5~47，comfyui 10~180。
        requested_mode: 页面/CLI 传入的档位（auto 视为未指定）。
        item_mode: 条目上显式指定的档位（优先级最高）。
        requested_normalize: 页面/CLI 传入的归一开关；None 表示未指定。
        item_normalize: 条目（cue）上保存的归一开关；只在未显式传参时生效。
        requested_trim: 页面/CLI 传入的首尾处理开关；None 表示未指定。
        item_trim: 条目（cue）上保存的首尾处理开关；只在未显式传参时生效。
        plan_id: 关联的 BGM 计划 ID（用于自动推断与回写 cue）。
        seed: 指定随机种子；为空时按章节 + 档位派生，保证同参数可复现。
        name: cue 名称；为空时取计划名称或描述前 16 字。
        start_ms: 新 cue 的起始时间。
        add_to_cue: 是否写回章节 cue 表。
        provider: `local` / `comfyui`；为空时读 `bgm.provider`，再缺省 `local`。
        prompt_en: 页面手填的英文提示词；为空时读关联计划的 `prompt_en`。
        allow_llm: 调试改写时是否允许调用 LLM。
        allow_rewrite: 调试开关：缺英文时临时把中文改写成英文（不参与常规链路）。
        steps: 本地 provider 的采样步数；为空用配置默认。
        cfg: 本地 provider 的 cfg；为空用配置默认。

    返回:
        `{"asset_id","mode","mode_label","mode_source","mode_reason","provider",
        "generation_s","postprocess_s","lufs","true_peak","cue","record",...}`。

    异常:
        AdapterError: 参数非法、缺英文提示词、provider 不可用、模型缺失或生成失败。
    """
    context = plan_context(store, chapter_id, plan_id)
    plan = (context or {}).get("plan") or {}
    text = str(prompt or "").strip()
    if not text:
        # 只给计划时，用计划里的中文语义字段作记录描述
        text = str(plan.get("narrative_function") or plan.get("name") or "").strip()
    stored_en = str(prompt_en or "").strip() or str(plan.get("prompt_en") or "").strip()
    rewrite_used = False
    rewrite_note: str | None = None
    if not stored_en:
        if not allow_rewrite:
            raise AdapterError(
                ErrorCode.INVALID_INPUT,
                "缺少英文提示词（prompt_en）：请重新生成标注描述，或在页面填写英文提示词",
            )
        if not text:
            raise AdapterError(ErrorCode.INVALID_INPUT, "配乐描述不能为空")
        # 显式调试选项：临时把中文改写成英文；不参与页面与常规链路
        from ..bgm.prompting import rewrite_prompt

        outcome = rewrite_prompt(text, config=config, allow_llm=allow_llm)
        stored_en = outcome.prompt
        rewrite_used = True
        rewrite_note = outcome.note
    if not text:
        text = stored_en
    if duration_s is None and context and context.get("duration_s"):
        # 未显式给时长时，优先采用 BGM 计划里的时长提示
        duration = float(context["duration_s"])
    else:
        try:
            duration = float(duration_s if duration_s is not None else 40.0)
        except (TypeError, ValueError):
            duration = 40.0
    provider_mode = str(provider or config.get("bgm.provider") or "local").strip().lower()
    if provider_mode not in ("local", "comfyui"):
        raise AdapterError(ErrorCode.INVALID_INPUT, f"未知配乐 provider：{provider_mode}（可选 local / comfyui）")
    if provider_mode == "comfyui":
        if duration < 10 or duration > 180:
            raise AdapterError(ErrorCode.INVALID_INPUT, "ACE-Step 配乐时长需在 10~180 秒之间")
    elif duration < 5 or duration > 47:
        raise AdapterError(
            ErrorCode.INVALID_INPUT,
            f"本地 Stable Audio Open 1.0 最长约 47 秒、最短 5 秒，请求 {duration:g} 秒；"
            "超过 47 秒请改用 --provider comfyui（ACE-Step）生成更长配乐",
        )
    has_dialogue = context.get("has_dialogue") if context else None
    decision = resolve_mode(item_mode, requested_mode, has_dialogue, duration)
    mode = decision["mode"]

    # 归一开关优先级：CLI/页面显式传参 > 条目已保存值 > 全局配置；铺底档始终归一
    existing_cue = _find_plan_cue(load_cues(store.root, chapter_id), plan_id)
    if item_normalize is None and isinstance((existing_cue or {}).get("normalize"), bool):
        item_normalize = existing_cue["normalize"]
    if mode == MODE_BED:
        do_normalize = True
    elif requested_normalize is not None:
        do_normalize = bool(requested_normalize)
    elif item_normalize is not None:
        do_normalize = bool(item_normalize)
    else:
        do_normalize = bool(config.get("bgm.normalize_narrative", True))

    # 首尾处理只属于叙事档（铺底已有裁静音 + 淡入），优先级与归一一致
    if item_trim is None and isinstance((existing_cue or {}).get("trim_edges"), bool):
        item_trim = existing_cue["trim_edges"]
    if requested_trim is not None:
        do_trim = bool(requested_trim)
    elif item_trim is not None:
        do_trim = bool(item_trim)
    else:
        do_trim = bool(config.get("bgm.trim_narrative_edges", True))
    do_trim = do_trim and mode == MODE_NARRATIVE

    actual_seed = int(seed) if seed is not None else seed_for(f"bgm_{chapter_id}_{mode}", 1)
    item_id = f"bgm_{chapter_id}_{mode}"

    bgm_dir = store.root / "assets" / "audio" / "bgm"
    bgm_dir.mkdir(parents=True, exist_ok=True)
    if provider_mode == "local":
        produced, gen_meta = _generate_local(
            config,
            mode,
            stored_en,
            duration,
            actual_seed,
            item_id,
            bgm_dir,
            steps=steps,
            cfg=cfg,
        )
    else:
        produced, gen_meta = _generate_comfy(config, chapter_id, mode, stored_en, duration, actual_seed, item_id, bgm_dir)
    generation_s = gen_meta["generation_s"]

    post_started = time.perf_counter()
    final_path = bgm_dir / f"{item_id}_s{actual_seed}.wav"
    ffmpeg_binary = str(config.get("ffmpeg.binary", "ffmpeg"))
    normalize_info: dict | None = None
    edge_trim_info: dict | None = None
    notes: list[str] = []
    if mode == MODE_BED:
        pre_loudness = measure_loudness(produced, ffmpeg_binary)
        _bed_postprocess(produced, final_path)
        produced.unlink(missing_ok=True)
        normalize_info = {"target_lufs": LOUDNESS_TARGET_LUFS, "target_true_peak": TRUE_PEAK_DBTP}
        notes.append(
            f"裁静音（阈值 {BED_SILENCE_THRESHOLD_DBFS:g} dBFS，保留 {BED_LEAD_KEEP_S:g}s）"
            f"+ 淡入 {BED_FADE_IN_S:g}s + loudnorm {LOUDNESS_TARGET_LUFS:g} LUFS"
        )
    else:
        _convert_wav(produced, final_path)
        produced.unlink(missing_ok=True)
        pre_loudness = measure_loudness(final_path, ffmpeg_binary)
        pre_silence = _silence_profile(final_path)
        trim_settings: dict | None = None
        if do_trim:
            temp_trim = final_path.with_name(final_path.stem + ".trim.wav")
            trim_settings = _trim_narrative_edges(final_path, temp_trim)
            os.replace(temp_trim, final_path)
            notes.append(
                f"裁首尾近静音（阈值 {NARRATIVE_TRIM_THRESHOLD_DBFS:g} dBFS，保留 {NARRATIVE_TRIM_KEEP_HEAD_S:g}s 起音）"
                f"+ 结尾淡出 {NARRATIVE_TAIL_FADE_S:g}s"
            )
        if do_normalize:
            temp_norm = final_path.with_name(final_path.stem + ".norm.wav")
            info = _narrative_normalize(final_path, temp_norm)
            os.replace(temp_norm, final_path)
            normalize_info = {"target_lufs": info["target_lufs"], "target_true_peak": info["target_true_peak"]}
            notes.append(f"loudnorm {LOUDNESS_TARGET_LUFS:g} LUFS / {TRUE_PEAK_DBTP:g} dBTP")
        if not do_normalize:
            notes.append("已关闭响度归一，保留模型原生动态")
        edge_trim_info = {
            "enabled": bool(do_trim),
            "method": "trim_silence+fade_out",
            "settings": trim_settings,
            "before": pre_silence,
            "after": _silence_profile(final_path),
        }
    postprocess_s = time.perf_counter() - post_started
    post_note = "；".join(notes) or "无后处理"

    asset_id = str(final_path.relative_to(store.root))
    loudness = measure_loudness(final_path, ffmpeg_binary)
    cue_name = name or (context or {}).get("plan", {}).get("name") or text[:16]
    cue = None
    if add_to_cue:
        cue = _upsert_cue(
            store.root,
            chapter_id,
            plan_id,
            asset_id,
            mode,
            cue_name,
            int(start_ms or 0),
            do_normalize,
            do_trim,
            stored_en,
        )

    record = {
        "created_at": now_iso(),
        "provider": gen_meta["provider"],
        "provider_id": gen_meta.get("provider_id"),
        "model": gen_meta.get("model"),
        "mode": mode,
        "mode_label": decision["label"],
        "mode_source": decision["source"],
        "mode_reason": decision["reason"],
        "generation_s": round(generation_s, 2),
        "postprocess_s": round(postprocess_s, 2),
        "normalize": bool(do_normalize),
        "normalize_target": normalize_info,
        "trim_edges": bool(do_trim),
        "edge_trim": edge_trim_info,
        "pre_lufs": (pre_loudness or {}).get("lufs"),
        "pre_true_peak": (pre_loudness or {}).get("true_peak"),
        "lufs": loudness.get("lufs"),
        "true_peak": loudness.get("true_peak"),
        "duration_s": round(probe_duration_ms(final_path) / 1000, 2),
        "prompt": text,
        "prompt_en": gen_meta.get("prompt_en") or stored_en,
        "prompt_source": "debug-rewrite" if rewrite_used else ("manual" if str(prompt_en or "").strip() else "plan"),
        "rewrite_mode": "debug" if rewrite_used else None,
        "rewrite_note": rewrite_note,
        "seed": gen_meta.get("seed", actual_seed),
        "steps": gen_meta.get("steps"),
        "cfg": gen_meta.get("cfg"),
        "latency_ms": gen_meta.get("latency_ms"),
        "load_s": gen_meta.get("load_s"),
        "peak_mib": gen_meta.get("peak_mib"),
        "template_id": gen_meta.get("template_id"),
        "prompt_id": gen_meta.get("prompt_id"),
        "asset_id": asset_id,
        "postprocess": post_note,
        "cue_id": (cue or {}).get("id"),
        "plan_id": plan_id,
    }

    # 同名侧车：把「用的什么描述、什么参数、实测指标」和音频绑在一起，便于追溯
    sidecar = final_path.with_suffix(".json")
    sidecar_tmp = sidecar.with_name(sidecar.name + ".tmp")
    sidecar_tmp.write_text(
        json.dumps({**record, "audio_file": final_path.name}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(sidecar_tmp, sidecar)

    record_path = _append_record(store.root, chapter_id, record)
    return {
        "asset_id": asset_id,
        "provider": record["provider"],
        "provider_id": record["provider_id"],
        "model": record["model"],
        "mode": mode,
        "mode_label": decision["label"],
        "mode_source": decision["source"],
        "mode_reason": decision["reason"],
        "generation_s": record["generation_s"],
        "postprocess_s": record["postprocess_s"],
        "normalize": record["normalize"],
        "normalize_target": record["normalize_target"],
        "trim_edges": record["trim_edges"],
        "edge_trim": record["edge_trim"],
        "pre_lufs": record["pre_lufs"],
        "pre_true_peak": record["pre_true_peak"],
        "lufs": record["lufs"],
        "true_peak": record["true_peak"],
        "duration_s": record["duration_s"],
        "steps": record["steps"],
        "cfg": record["cfg"],
        "load_s": record["load_s"],
        "peak_mib": record["peak_mib"],
        "prompt_en": record["prompt_en"],
        "sidecar": str(sidecar),
        "cue": cue,
        "record": record,
        "record_path": str(record_path),
        "template_id": record["template_id"],
        "prompt_id": record["prompt_id"],
    }
