"""台词行的统一读入口，以及「场景 ↔ 台本」的无损换算（阶段 6-3 步骤 1）。

口径（已确认）：
- 内容层字段（台词文本、说话人、情绪、语速、停顿等）归台本 `scripts/<chapterId>.json`；
- 生产态 `tts` 与媒体（音效/图/视频）继续留在章节场景，派生时按行 ID / 条目 ID 原样保留；
- 旧工程没有 `scripts/` 时，`script_from_scenes` 在内存里引导出台本，行 ID 不变；
- `scenes_from_script` 是派生产出：深拷贝旧场景后只覆盖内容字段，未知字段与生产态不丢。

本模块目前只提供只读换算与读入口，不接任何写路径；写点切换（只写台本、scenes 派生）
在步骤 3 落地。开关 `NDW_LINES_SOURCE=scenes|script` 用于逐步切换与差异对照，默认 `scenes`。
"""

from __future__ import annotations

import copy
import hashlib
import os

from ...adapters.persistence import ProjectStore
from ...models import entities

#: 台本行承接的内容层字段（生产态 `tts`、`anchor` 与 `confidence` 不在此列）
CONTENT_LINE_FIELDS = (
    "index",
    "kind",
    "speaker_id",
    "text",
    "emotion",
    "emotion_intensity",
    "speed",
    "pause_before_ms",
    "pause_after_ms",
    "delivery_note",
    "paralinguistic",
    "target_duration_ms",
    "voice",
    "inferred",
)

#: 场景描述字段：按镜头覆盖，其余场景字段（id/summary/source_text/status/媒体）沿用旧场景
SCENE_DESCRIPTIVE_FIELDS = ("location", "time_of_day", "mood", "goal")

#: 空章节内容的占位哈希输入（保证 `content_hash` 始终是合法 sha256）
_EMPTY_TIME = "1970-01-01T00:00:00Z"


def _digest(value: str) -> str:
    """把任意字符串压成稳定的 24 位十六进制串（用于确定性 ID 与锚点块号）。

    参数:
        value: 参与哈希的文本。

    返回:
        小写十六进制摘要前 24 位。
    """
    return hashlib.sha1(str(value).encode("utf-8")).hexdigest()[:24]


def _text_hash(text: str) -> str:
    """计算正文片段的 `sha256:` 哈希（锚点必填字段）。

    参数:
        text: 片段文本。

    返回:
        `sha256:<64 位十六进制>`。
    """
    return "sha256:" + hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def _anchor_for(text: str, seed: str) -> dict:
    """为没有锚点的旧数据合成一个确定性锚点。

    旧工程的行与场景都没有 `anchor`（回指正文段落块），而台本 schema 要求镜头行必带。
    这里按「文本 + 稳定种子」合成：同输入永远得到同一锚点，保证换算幂等。

    参数:
        text: 锚定的正文片段（场景用场景原文/标题，行用台词文本）。
        seed: 稳定种子（章节 ID + 序号或行 ID）。

    返回:
        符合 `director-script.schema.json#/$defs/anchor` 的锚点 dict。
    """
    value = str(text or "")
    return {
        "block_id": f"blk_{_digest(seed)}",
        "char_start": 0,
        "char_end": len(value),
        "text_hash": _text_hash(value),
    }


def _stable_id(prefix: str, seed: str) -> str:
    """按种子生成确定性实体 ID（同输入同 ID，便于重复执行幂等）。

    参数:
        prefix: 实体前缀，如 `scr` / `sht`。
        seed: 稳定种子。

    返回:
        `<prefix>_<24 位十六进制>`。
    """
    return f"{prefix}_{_digest(seed)}"


def lines_source() -> str:
    """返回当前台词读取来源。

    返回:
        `script` 或 `scenes`；非法值按 `scenes` 处理，默认也是 `scenes`。
    """
    value = os.environ.get("NDW_LINES_SOURCE", "scenes").strip().lower()
    return "script" if value == "script" else "scenes"


def script_from_scenes(chapter: dict) -> dict:
    """把旧工程的章节场景内存引导成导演脚本（不写盘、行 ID 原样保留）。

    该函数是迁移旧工程的唯一入口：三套语料都没有 `scripts/`，首次需要台本时由它引导。
    镜头 ID / 脚本 ID 由章节内序号确定性生成，重复调用结果完全一致。

    参数:
        chapter: 章节 dict（需含 `scenes`）。

    返回:
        导演脚本 dict（形状对齐 `director-script.schema.json`；镜头行不带生产态 `tts`）。
    """
    chapter_id = str(chapter.get("id") or "")
    timestamp = str(chapter.get("created_at") or chapter.get("updated_at") or _EMPTY_TIME)
    content = str(chapter.get("content") or "")
    scenes = sorted(chapter.get("scenes") or [], key=lambda item: item.get("index") or 0)
    shots = []
    for scene in scenes:
        shot_index = int(scene.get("index") or 0) + 1
        anchor_text = str(scene.get("source_text") or scene.get("title") or "")
        shot = {
            "id": _stable_id("sht", f"{chapter_id}:{shot_index}"),
            "index": shot_index,
            "scene_label": str(scene.get("title") or f"场景 {shot_index}"),
            "location": scene.get("location"),
            "time_of_day": scene.get("time_of_day") or "unknown",
            "mood": scene.get("mood"),
            "goal": scene.get("goal"),
            "anchor": _anchor_for(anchor_text, f"{chapter_id}:{shot_index}"),
            "status": "ready" if scene.get("lines") else "draft",
            "lines": [_line_from_scene_line(line) for line in scene.get("lines") or []],
            "revision": int(scene.get("revision") or 1),
        }
        if scene.get("bgm"):
            shot["bgm"] = copy.deepcopy(scene["bgm"])
        shots.append(shot)
    return {
        "schema_version": 1,
        "id": _stable_id("scr", chapter_id),
        "chapter_id": chapter_id,
        "work_id": str(chapter.get("work_id") or ""),
        "source": {
            "chapter_revision": int(chapter.get("revision") or 1),
            "settings_revision": 1,
            "content_hash": _text_hash(content),
        },
        "status": "ready" if shots else "draft",
        "shots": shots,
        "created_at": timestamp,
        "updated_at": timestamp,
    }


def _line_from_scene_line(line: dict) -> dict:
    """把场景台词行投影成台本镜头行（只取内容层字段）。

    参数:
        line: 场景内的台词行 dict。

    返回:
        台本行 dict；不带 `tts`（生产态归场景投影）。
    """
    text = str(line.get("text") or "")
    shot_line = {
        "id": line.get("id"),
        "index": int(line.get("index") or 0),
        "kind": line.get("kind") or "dialogue",
        "speaker_id": line.get("speaker_id") or entities.UNKNOWN_SPEAKER_ID,
        "text": text,
        "emotion": line.get("emotion") or "neutral",
        "revision": int(line.get("revision") or 1),
        # 旧数据没有锚点，这里合成；派生回场景时不会把合成锚点写进旧行（见 scenes_from_script）
        "anchor": copy.deepcopy(line["anchor"]) if line.get("anchor") else _anchor_for(text, str(line.get("id") or "")),
    }
    for field in CONTENT_LINE_FIELDS:
        if field in line:
            shot_line[field] = copy.deepcopy(line[field])
    return shot_line


def scenes_from_script(chapter: dict, script: dict) -> list[dict]:
    """由导演脚本派生章节场景（纯函数、幂等，保留生产态与媒体）。

    派生规则：
    1. 场景身份按 `index` 对齐旧场景：`id/chapter_id/summary/source_text/status/character_ids`
       与媒体数组沿用旧场景，镜头重排视为重新标注（不搬旧生产态到新位置）；
    2. 描述字段取镜头（`title/location/time_of_day/mood/goal/bgm`）；
    3. 台词行按镜头行重建，除内容层字段外一律沿用旧行（`tts`、`confidence`、媒体引用等）；
    4. 内容无变化时沿用旧 `revision`，有变化才 +1，保证同一命令连跑两次第二次 0 文件变化。

    参数:
        chapter: 章节 dict，提供旧场景与章节身份。
        script: 导演脚本 dict。

    返回:
        新的场景列表（不修改入参）。
    """
    chapter_id = str(chapter.get("id") or "")
    old_by_index = {int(scene.get("index") or 0): scene for scene in chapter.get("scenes") or []}
    derived = []
    for shot in sorted(script.get("shots") or [], key=lambda item: item.get("index") or 0):
        scene_index = int(shot.get("index") or 1) - 1
        old = old_by_index.get(scene_index)
        if old is not None:
            scene = copy.deepcopy(old)
        else:
            scene = entities.new_scene(chapter_id, scene_index, str(shot.get("scene_label") or ""))
        scene["chapter_id"] = chapter_id
        scene["index"] = scene_index
        label = str(shot.get("scene_label") or "")
        default_label = f"场景 {scene_index + 1}"
        if old is not None and not old.get("title") and label == default_label:
            # 旧场景标题为空、台本只是占位默认值时不凭空补标题，保证换算可逆
            scene["title"] = old.get("title")
        else:
            scene["title"] = label or scene.get("title") or default_label
        for field in SCENE_DESCRIPTIVE_FIELDS:
            scene[field] = copy.deepcopy(shot.get(field)) if field in shot else scene.get(field)
        if "time_of_day" in shot and not shot.get("time_of_day"):
            scene["time_of_day"] = "unknown"
        if "bgm" in shot:
            if shot.get("bgm") is None:
                scene.pop("bgm", None)
            else:
                scene["bgm"] = copy.deepcopy(shot["bgm"])
        scene["lines"] = _lines_from_shot(scene, old, shot)
        if old is None:
            scene["revision"] = max(int(shot.get("revision") or 1), 1)
        else:
            unchanged = _scene_without_revision(old) == _scene_without_revision(scene)
            scene["revision"] = int(old.get("revision") or 1) if unchanged else int(old.get("revision") or 1) + 1
        derived.append(scene)
    return derived


def _scene_without_revision(scene: dict) -> dict:
    """返回剔除 `revision` 的场景视图，用于判断内容是否真的变化。

    参数:
        scene: 场景 dict。

    返回:
        不含 `revision` 的浅拷贝 dict。
    """
    return {key: value for key, value in scene.items() if key != "revision"}


def _lines_from_shot(scene: dict, old_scene: dict | None, shot: dict) -> list[dict]:
    """按镜头行重建场景台词行。

    参数:
        scene: 当前派生中的场景（取 `id` 作为行所属场景）。
        old_scene: 同 index 的旧场景，可为空（新镜头）。
        shot: 镜头 dict。

    返回:
        台词行列表，顺序与镜头行一致。
    """
    old_lines = {line.get("id"): line for line in (old_scene or {}).get("lines") or []}
    result = []
    for position, source in enumerate(shot.get("lines") or []):
        line_id = source.get("id")
        old_line = old_lines.get(line_id)
        index = int(source.get("index") if source.get("index") is not None else position)
        if old_line is not None:
            line = copy.deepcopy(old_line)
        else:
            line = entities.new_line(
                scene["id"],
                index,
                str(source.get("text") or ""),
                kind=source.get("kind") or "dialogue",
                speaker_id=source.get("speaker_id") or entities.UNKNOWN_SPEAKER_ID,
                emotion=source.get("emotion") or "neutral",
            )
        line["id"] = line_id
        line["scene_id"] = scene["id"]
        line["index"] = index
        line["revision"] = int(source.get("revision") or line.get("revision") or 1)
        for field in CONTENT_LINE_FIELDS:
            if field == "index":
                continue
            if field in source:
                line[field] = copy.deepcopy(source[field])
            else:
                line.pop(field, None)
        # 旧数据没有锚点：派生时不凭空补锚，避免「引导 → 派生」把无锚行变成有锚行；
        # 旧行本来有锚点或属于新行时，才同步台本锚点。
        if old_line is None or old_line.get("anchor") is not None:
            if source.get("anchor") is not None:
                line["anchor"] = copy.deepcopy(source["anchor"])
            else:
                line.pop("anchor", None)
        result.append(line)
    return result


def _script_for(store: ProjectStore | None, chapter: dict) -> dict:
    """取章节的导演脚本：优先读盘，缺失时从场景内存引导。

    参数:
        store: 工程，可为空（只做纯换算时不必传）。
        chapter: 章节 dict。

    返回:
        导演脚本 dict。
    """
    if store is not None:
        try:
            return store.load_script(str(chapter.get("id") or ""))
        except FileNotFoundError:
            pass
    return script_from_scenes(chapter)


def read_scenes(store: ProjectStore | None, chapter: dict) -> list[dict]:
    """统一读入口：返回章节的场景列表（含台词行）。

    `NDW_LINES_SOURCE=scenes`（默认）时直接返回落盘的场景；
    切到 `script` 时读取台本并派生（无台本则内存引导），供逐步对照。

    参数:
        store: 工程，可为空（只在 script 模式下用于读台本）。
        chapter: 章节 dict。

    返回:
        场景列表；script 模式下是派生副本，调用方不要直接回写章节。
    """
    if lines_source() == "script":
        return scenes_from_script(chapter, _script_for(store, chapter))
    return list(chapter.get("scenes") or [])


def chapter_view(store: ProjectStore | None, chapter: dict) -> dict:
    """返回用于读取的章节视图（adapter 与展示层只认这一份场景数据）。

    `NDW_LINES_SOURCE=scenes`（默认）时原样返回入参章节，行为与改造前完全一致；
    切到 `script` 时返回浅拷贝，`scenes` 换成由台本派生的场景，其余字段共享。

    约定：视图只用于读路径，调用方不得把视图回写章节（内容写点统一走步骤 3 的
    `save_lines`）；写路径在步骤 3 之前继续直接使用 `store.load_chapter`。

    参数:
        store: 工程，可为空（只做纯换算时不必传）。
        chapter: 从工程读出的章节 dict。

    返回:
        章节 dict；默认源下与入参是同一对象。
    """
    if lines_source() != "script":
        return chapter
    view = dict(chapter)
    view["scenes"] = read_scenes(store, chapter)
    return view


def read_line(store: ProjectStore | None, chapter: dict, line_id: str) -> tuple[dict, dict]:
    """统一读入口：按行 ID 定位台词行。

    参数:
        store: 工程，可为空。
        chapter: 章节 dict。
        line_id: 台词行 ID。

    返回:
        `(scene, line)`。

    异常:
        ValueError: 行 ID 不在本章内。
    """
    for scene in read_scenes(store, chapter):
        for line in scene.get("lines") or []:
            if line.get("id") == line_id:
                return scene, line
    raise ValueError(
        f"台词行不存在: {line_id}（用 line list --chapter {chapter['id']} 查看可用行 ID）"
    )
