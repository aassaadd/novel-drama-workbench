"""枚举校验：Pydantic 模型声明字段类型，落盘前统一拦截非法枚举值。

分层：
- `schema_validator` 仍负责结构校验（必填、字段白名单、长度、跨文件 $ref）；
- 本模块只负责「枚举字段必须是已定义值」，用 Pydantic 模型把字段声明成枚举类型，
  因此 Python 侧也不存在「随便塞个字符串就能落盘」的路径。
- 历史数据里已经存在的非法值（如旧工程的 `30-40`）只要原样不动就放行，
  由界面给出修复提示；一旦被改动成新的非法值立刻拒绝，报错带字段中文名与可选值。

用法：
    check_character_table(table)                    # 严格校验
    check_character_table(table, baseline=old)      # 兼容历史值（原样不动才放行）
"""

from __future__ import annotations

import types
from typing import Any, get_args, get_origin

from pydantic import BaseModel, ConfigDict, ValidationError

from . import enums as E


class EnumValueError(ValueError):
    """枚举取值非法，消息为中文并含字段名与可选值。"""


class _Doc(BaseModel):
    """枚举视图模型基类：只声明枚举字段，其余字段交给 JSON Schema。"""

    model_config = ConfigDict(extra="allow")


class SpeechStyleDoc(_Doc):
    """角色说话方式。"""

    tempo: E.SpeechTempo | None = None
    pitch: E.SpeechPitch | None = None


class RelationDoc(_Doc):
    """角色关系。"""

    sentiment: E.RelationSentiment | None = None


class CharacterStateDoc(_Doc):
    """角色状态卡。"""

    relations: list[RelationDoc] | None = None


class VoiceProfileDoc(_Doc):
    """音色描述。"""

    gender: E.CharacterGender | None = None
    gender_source: E.GenderSource | None = None
    age_feel: E.AgeFeel | None = None
    status: E.VoiceProfileStatus | None = None


class StockDoc(_Doc):
    """龙套槽位。"""

    reuse_policy: E.StockReusePolicy | None = None


class CharacterDoc(_Doc):
    """单个角色（只列枚举字段）。"""

    role: E.CharacterRole | None = None
    gender: E.CharacterGender | None = None
    age_range: E.AgeRange | None = None
    speech_style: SpeechStyleDoc | None = None
    state: CharacterStateDoc | None = None
    voice_profile: VoiceProfileDoc | None = None
    stock: StockDoc | None = None


class CharacterTableDoc(_Doc):
    """角色表。"""

    characters: list[CharacterDoc] = []


class TtsDoc(_Doc):
    """台词行配音状态。"""

    status: E.LineTtsStatus | None = None


class LineDoc(_Doc):
    """台词行。"""

    kind: E.LineKind | None = None
    emotion: E.Emotion | None = None
    paralinguistic: list[E.Paralinguistic] | None = None
    tts: TtsDoc | None = None


class SfxAssetDoc(_Doc):
    """音效产物。"""

    status: E.AssetStatus | None = None


class SfxPositionDoc(_Doc):
    """音效锚点。"""

    mode: E.SfxPositionMode | None = None


class SfxDoc(_Doc):
    """音效事件。"""

    kind: E.SfxKind | None = None
    position: SfxPositionDoc | None = None
    asset: SfxAssetDoc | None = None


class MediaAssetDoc(_Doc):
    """配图 / 视频产物。"""

    status: E.AssetStatus | None = None


class KeyImageDoc(_Doc):
    """关键图。"""

    shot_type: E.ShotType | None = None
    status: E.KeyImageStatus | None = None
    prompt_source: E.PromptSource | None = None
    aspect_ratio: E.AspectRatio | None = None
    asset: MediaAssetDoc | None = None


class VideoDoc(_Doc):
    """短视频段落。"""

    status: E.VideoStatus | None = None
    mode: E.VideoMode | None = None
    camera_move: E.CameraMove | None = None
    audio_policy: E.AudioPolicy | None = None
    aspect_ratio: E.AspectRatio | None = None
    asset: MediaAssetDoc | None = None


class BgmCueDoc(_Doc):
    """BGM cue。"""

    mode: E.BgmMode | None = None
    status: E.BgmStatus | None = None


class SceneDoc(_Doc):
    """场景（章节内嵌）。"""

    status: E.SceneStatus | None = None
    time_of_day: E.TimeOfDay | None = None
    mood: E.SceneMood | None = None
    lines: list[LineDoc] | None = None
    sfx_events: list[SfxDoc] | None = None
    key_images: list[KeyImageDoc] | None = None
    video_segments: list[VideoDoc] | None = None
    bgm: BgmCueDoc | None = None


class BlockDoc(_Doc):
    """正文块。"""

    kind: E.BlockKind | None = None


class GenerationDoc(_Doc):
    """正文生成记录。"""

    mode: E.GenerationMode | None = None
    status: E.GenerationStatus | None = None


class ChapterDoc(_Doc):
    """章节。"""

    status: E.ChapterStatus | None = None
    pov_mode: E.PovMode | None = None
    blocks: list[BlockDoc] | None = None
    content_generation: GenerationDoc | None = None
    scenes: list[SceneDoc] | None = None


class ShotDoc(_Doc):
    """导演脚本镜头。"""

    status: E.SceneStatus | None = None
    time_of_day: E.TimeOfDay | None = None
    lines: list[LineDoc] | None = None
    sfx_events: list[SfxDoc] | None = None


class ScriptDoc(_Doc):
    """导演脚本。"""

    status: E.ScriptStatus | None = None
    shots: list[ShotDoc] | None = None


class VoiceVariantDoc(_Doc):
    """音色变体。"""

    emotion_tags: list[E.VoiceVariantEmotion] | None = None
    gender_source: E.VoiceGenderSource | None = None


class VoiceDoc(_Doc):
    """音色条目。"""

    gender: E.VoiceGender | None = None
    status: E.VoiceStatus | None = None
    source: E.VoiceSource | None = None
    status_check: E.VoiceCheckStatus | None = None
    variants: list[VoiceVariantDoc] | None = None


class VoiceTableDoc(_Doc):
    """音色库。"""

    voices: list[VoiceDoc] = []


class WorkDoc(_Doc):
    """作品元数据。"""

    usage: E.WorkUsage | None = None


class AudioDoc(_Doc):
    """导出音频参数。"""

    format: E.AudioFormat | None = None


class OutputDoc(_Doc):
    """输出设置。"""

    aspect_ratio: E.AspectRatio | None = None


class RoutingDoc(_Doc):
    """TTS 路由设置。"""

    mode: E.RoutingMode | None = None


class NarratorSwitchDoc(_Doc):
    """旁白切换点。"""

    scope: E.NarratorSwitchScope | None = None


class CastDoc(_Doc):
    """全剧音色编排。"""

    narrator_switches: list[NarratorSwitchDoc] | None = None


class TtsSettingsDoc(_Doc):
    """配音设置。"""

    routing: RoutingDoc | None = None
    cast: CastDoc | None = None


class UnloadDoc(_Doc):
    """模型卸载设置。"""

    policy: E.UnloadPolicy | None = None


class WritingDoc(_Doc):
    """写作设置。"""

    unload: UnloadDoc | None = None


class ProductionDoc(_Doc):
    """制作要求。"""

    audio: AudioDoc | None = None


class GpuDoc(_Doc):
    """GPU 设置。"""

    unload_on_switch: E.GpuUnloadMode | None = None


class SettingsDoc(_Doc):
    """工程设置。"""

    writing: WritingDoc | None = None
    tts: TtsSettingsDoc | None = None
    output: OutputDoc | None = None
    production: ProductionDoc | None = None
    gpu: GpuDoc | None = None


class ProjectDoc(_Doc):
    """工程文档。"""

    work: WorkDoc | None = None
    settings: SettingsDoc | None = None


class JobDoc(_Doc):
    """渲染任务。"""

    kind: E.JobKind | None = None
    status: E.JobStatus | None = None


class QueueDoc(_Doc):
    """任务队列。"""

    jobs: list[JobDoc] = []


class BgmTableDoc(_Doc):
    """BGM cue 表。"""

    cues: list[BgmCueDoc] = []


class UiJobDoc(_Doc):
    """界面生成任务。"""

    kind: E.UiJobKind | None = None
    status: E.UiJobStatus | None = None


class UiJobTableDoc(_Doc):
    """界面生成任务列表。"""

    jobs: list[UiJobDoc] = []


_MODEL_INDEX_CACHE: dict[type[BaseModel], dict[tuple[str, ...], type[E.ZhEnum]]] = {}


def _annotation_enums(annotation) -> list[tuple[str, Any]]:
    """展开类型注解，找出其中的枚举类与嵌套模型。

    参数:
        annotation: 字段类型注解（可能是 Optional / list / Union 组合）。

    返回:
        [(kind, 目标)]，kind 为 "enum" 或 "model"。
    """
    found: list[tuple[str, Any]] = []
    origin = get_origin(annotation)
    if origin is not None:
        args = get_args(annotation)
        if origin in (list, tuple, set):
            for arg in args:
                found.extend(_annotation_enums(arg))
        elif origin in (types.UnionType,):
            for arg in args:
                found.extend(_annotation_enums(arg))
        elif str(origin).endswith("Union"):
            for arg in args:
                found.extend(_annotation_enums(arg))
        return found
    if isinstance(annotation, type):
        if issubclass(annotation, E.ZhEnum):
            found.append(("enum", annotation))
        elif issubclass(annotation, BaseModel):
            found.append(("model", annotation))
    return found


def _enum_index(model_cls: type[BaseModel], prefix: tuple[str, ...] = ()) -> dict[tuple[str, ...], type[E.ZhEnum]]:
    """递归建立「字段路径 → 枚举类」索引（列表下标会被忽略）。

    参数:
        model_cls: 模型类。
        prefix: 当前路径前缀。

    返回:
        {字段路径: 枚举类}。
    """
    cached = _MODEL_INDEX_CACHE.get(model_cls)
    if cached is not None and not prefix:
        return cached
    index: dict[tuple[str, ...], type[E.ZhEnum]] = {}
    for name, field in model_cls.model_fields.items():
        for kind, target in _annotation_enums(field.annotation):
            if kind == "enum":
                index[prefix + (name,)] = target
            else:
                index.update(_enum_index(target, prefix + (name,)))
    if not prefix:
        _MODEL_INDEX_CACHE[model_cls] = index
    return index


def _value_at(doc: Any, loc: tuple) -> Any:
    """按 Pydantic 错误路径取值（不存在时返回哨兵）。

    参数:
        doc: 原文档。
        loc: Pydantic 错误位置。

    返回:
        该位置的值；路径不通时返回 `_MISSING`。
    """
    current = doc
    for part in loc:
        if isinstance(part, int):
            if not isinstance(current, list) or part >= len(current):
                return _MISSING
            current = current[part]
        else:
            if not isinstance(current, dict) or part not in current:
                return _MISSING
            current = current[part]
    return current


_MISSING = object()


def _same(left: Any, right: Any) -> bool:
    """判断两个值是否完全相同（用于历史值放行）。

    参数:
        left: 原文档中的值。
        right: 本次待写入的值。

    返回:
        True 表示未发生变化。
    """
    if left is _MISSING:
        return False
    return left == right


def _loc_text(loc: tuple, context: str) -> str:
    """把错误位置渲染成「上下文中第 N 项」。

    参数:
        loc: Pydantic 错误位置。
        context: 文档中文名，如「角色档案」。

    返回:
        中文位置描述。
    """
    numbers = [part for part in loc if isinstance(part, int)]
    if numbers:
        return f"{context}第 {numbers[0] + 1} 项"
    return context


def _error_message(err: dict, enum_cls: type[E.ZhEnum], context: str) -> str:
    """构造中文枚举错误信息。

    参数:
        err: Pydantic 错误条目。
        enum_cls: 对应枚举类。
        context: 文档中文名。

    返回:
        含字段名与可选值的中文错误。
    """
    label = E.field_label(enum_cls)
    value = err.get("input")
    return f"{_loc_text(tuple(err.get('loc') or ()), context)}的「{label}」不能是「{value}」；可选值：{E.choices_text(enum_cls)}"


def _issue_of(err: dict, enum_cls: type[E.ZhEnum]) -> dict:
    """把历史遗留的非法值整理成界面可展示的修复提示。

    参数:
        err: Pydantic 错误条目。
        enum_cls: 对应枚举类。

    返回:
        `{field, path, value, allowed, suggestion}`。
    """
    value = err.get("input")
    suggestion = enum_cls.normalize(value)
    return {
        "field": E.field_label(enum_cls),
        "path": "/".join(str(part) for part in err.get("loc") or ()),
        "value": value if isinstance(value, (str, int, float)) else str(value),
        "allowed": [{"value": member.value, "label": member.label} for member in enum_cls],
        "suggestion": {"value": suggestion, "label": enum_cls.label_of(suggestion)} if suggestion else None,
    }


def check_enums(
    doc: dict,
    model_cls: type[BaseModel],
    context: str,
    baseline: dict | None = None,
) -> list[dict]:
    """校验文档里的枚举字段。

    参数:
        doc: 待写入的文档。
        model_cls: 枚举视图模型。
        context: 文档中文名，用于报错（如「角色档案」）。
        baseline: 磁盘上的旧文档；提供时，原样未改的非法值视为历史遗留，返回修复提示而不报错。

    返回:
        历史遗留问题列表（`baseline` 为空时必定为空）。

    异常:
        EnumValueError: 存在本次新写入的非法枚举值。
    """
    try:
        model_cls.model_validate(doc)
        return []
    except ValidationError as exc:
        index = _enum_index(model_cls)
        issues: list[dict] = []
        for err in exc.errors():
            loc = tuple(part for part in err.get("loc") or () if not isinstance(part, int))
            enum_cls = index.get(loc)
            if enum_cls is None:
                # 模型未声明的字段出现校验错误属开发期问题，直接暴露
                raise EnumValueError(f"{context} 枚举校验模型缺少字段声明：{'/'.join(loc) or '<root>'}（{err.get('msg')}）")
            if baseline is not None and _same(_value_at(baseline, tuple(err.get("loc") or ())), err.get("input")):
                issues.append(_issue_of(err, enum_cls))
                continue
            raise EnumValueError(_error_message(err, enum_cls, context))
        return issues


def check_character_table(table: dict, baseline: dict | None = None) -> list[dict]:
    """校验角色表枚举字段。

    参数:
        table: characters.json 文档。
        baseline: 磁盘旧文档，用于放行历史值。

    返回:
        历史遗留问题列表。

    异常:
        EnumValueError: 新写入了非法枚举值。
    """
    return check_enums(table, CharacterTableDoc, "角色档案", baseline)


def check_chapter(chapter: dict, baseline: dict | None = None) -> list[dict]:
    """校验章节枚举字段（含场景、台词、素材）。

    参数:
        chapter: 章节文档。
        baseline: 磁盘旧文档。

    返回:
        历史遗留问题列表。
    """
    return check_enums(chapter, ChapterDoc, "章节", baseline)


def check_script(script: dict, baseline: dict | None = None) -> list[dict]:
    """校验导演脚本枚举字段。

    参数:
        script: 导演脚本文档。
        baseline: 磁盘旧文档。

    返回:
        历史遗留问题列表。
    """
    return check_enums(script, ScriptDoc, "导演脚本", baseline)


def check_voice_table(table: dict, baseline: dict | None = None) -> list[dict]:
    """校验音色库枚举字段。

    参数:
        table: voices.json 文档。
        baseline: 磁盘旧文档。

    返回:
        历史遗留问题列表。
    """
    return check_enums(table, VoiceTableDoc, "音色库", baseline)


def check_project(project: dict, baseline: dict | None = None) -> list[dict]:
    """校验工程设置枚举字段。

    参数:
        project: project.json 文档。
        baseline: 磁盘旧文档。

    返回:
        历史遗留问题列表。
    """
    return check_enums(project, ProjectDoc, "工程设置", baseline)


def check_jobs(jobs: list[dict], baseline: list[dict] | None = None) -> list[dict]:
    """校验任务队列枚举字段。

    参数:
        jobs: 任务列表。
        baseline: 磁盘旧任务列表（队列整体覆盖写），用于兼容历史任务。

    返回:
        历史遗留问题列表。
    """
    return check_enums({"jobs": jobs}, QueueDoc, "任务队列", {"jobs": baseline} if baseline else None)


def check_ui_jobs(jobs: list[dict], baseline: list[dict] | None = None) -> list[dict]:
    """校验界面生成任务枚举字段。

    参数:
        jobs: `.ndw/ui_jobs.json` 里的任务列表。
        baseline: 磁盘旧任务列表。

    返回:
        历史遗留问题列表。
    """
    return check_enums({"jobs": jobs}, UiJobTableDoc, "界面任务", {"jobs": baseline} if baseline else None)


def check_bgm(table: dict, baseline: dict | None = None) -> list[dict]:
    """校验 BGM cue 表枚举字段。

    参数:
        table: `bgm/<章节>.json` 文档。
        baseline: 磁盘旧文档。

    返回:
        历史遗留问题列表。
    """
    return check_enums(table, BgmTableDoc, "BGM 轨", baseline)
