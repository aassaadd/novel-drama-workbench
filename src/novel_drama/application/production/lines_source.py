"""台词行的统一读入口，以及「场景 ↔ 台本」的无损换算（阶段 6-3 步骤 1）。

口径（已确认）：
- 内容层字段（台词文本、说话人、情绪、语速、停顿等）归台本 `scripts/<chapterId>.json`；
- 生产态 `tts` 与媒体（音效/图/视频）继续留在章节场景，派生时按行 ID / 条目 ID 原样保留；
- 旧工程没有 `scripts/` 时，`script_from_scenes` 在内存里引导出台本，行 ID 不变；
- `scenes_from_script` 是派生产出：深拷贝旧场景后只覆盖内容字段，未知字段与生产态不丢。

本模块提供只读换算与读入口，以及步骤 3 的唯一内容写点 `save_lines`（只写台本、
场景由台本派生）。开关 `NDW_LINES_SOURCE=scenes|script` 用于切换与差异对照，
默认 `script`；设成 `scenes` 可回退到「直读落盘场景」的旧行为。
"""

from __future__ import annotations

import copy
import hashlib
import os

from ...adapters.persistence import ProjectStore
from ...domain.production.line_fields import CONTENT_LINE_FIELDS, SCENE_DESCRIPTIVE_FIELDS
from ...models import entities
from ...models.ids import now_iso

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
        `script` 或 `scenes`；默认 `script`（台本是内容唯一可写源），
        非法值按 `script` 处理。设置 `NDW_LINES_SOURCE=scenes` 可回退到旧的落盘场景。
    """
    value = os.environ.get("NDW_LINES_SOURCE", "script").strip().lower()
    return "scenes" if value == "scenes" else "script"


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
    shots = [_shot_for_scene(chapter, scene) for scene in scenes]
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


def _shot_for_scene(chapter: dict, scene: dict) -> dict:
    """按场景字段新建一个镜头（引导台本用；ID 与锚点按章节 + 序号确定性生成）。

    参数:
        chapter: 章节 dict（取章节 ID）。
        scene: 场景 dict。

    返回:
        符合 `director-script.schema.json#/$defs/shot` 的镜头 dict。
    """
    chapter_id = str(chapter.get("id") or "")
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
    return shot


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


# ---------------- 写点：台本唯一可写，场景由台本派生 ----------------


def ensure_script(store: ProjectStore, chapter: dict) -> dict:
    """取本章导演脚本；没有台本时先用当前场景引导并落盘（幂等）。

    只在写路径调用：旧工程第一次发生内容写入时补出台本，之后一律以台本为准。
    读路径不调用本函数（读不写，见 `_script_for`）。

    参数:
        store: 工程。
        chapter: 章节 dict（需含 `scenes`，用于首次引导）。

    返回:
        导演脚本 dict。
    """
    try:
        return store.load_script(str(chapter.get("id") or ""))
    except FileNotFoundError:
        script = script_from_scenes(chapter)
        store.save_script(script)
        return script


def save_lines(store: ProjectStore, chapter: dict) -> dict:
    """唯一内容写点：把场景里的内容改动同步进台本，再由台本派生场景落盘。

    调用约定：
    1. 行编辑类用例先就地改好 `chapter["scenes"]` 里的台词行（与改造前完全一致）；
    2. 再调用本函数，由它把内容层字段同步进台本、派生 scenes、按「有变化才写」落盘。

    同步只覆盖内容层字段与场景描述字段；`tts` / `confidence` / 媒体条目 / 场景身份
    一律不写进台本，派生时按行 ID、条目 ID 从旧场景原样保留。

    参数:
        store: 工程。
        chapter: 章节 dict（已就地改好场景）。

    返回:
        `{"script_saved": 台本是否写盘, "chapter_saved": 章节是否写盘}`。
    """
    chapter_id = str(chapter.get("id") or "")
    try:
        script = store.load_script(chapter_id)
        before_script = copy.deepcopy(script)
    except FileNotFoundError:
        # 旧工程首写：内存里的场景已经改过，引导台本与归档快照都要按磁盘上的「改动前」状态来
        try:
            seed = store.load_chapter(chapter_id)
        except FileNotFoundError:
            seed = chapter
        before_script = script_from_scenes(seed)
        script = copy.deepcopy(before_script)
        store.save_script(script)
    script_changed = _sync_script_from_scenes(chapter, script)
    scenes = scenes_from_script(chapter, script)
    scenes_changed = scenes != (chapter.get("scenes") or [])
    if scenes_changed:
        chapter["scenes"] = scenes
    if script_changed:
        script["updated_at"] = now_iso()
        store.save_script(script)
    chapter_changed = scenes_changed or _chapter_changed_on_disk(store, chapter)
    if chapter_changed:
        # 归档要存「改动前」的台本，此时磁盘上的台本已经是新内容
        store.save_chapter(chapter, archive_script=before_script if script_changed else None)
    return {"script_saved": script_changed, "chapter_saved": chapter_changed}


def _chapter_changed_on_disk(store: ProjectStore, chapter: dict) -> bool:
    """判断章节除 `updated_at` 外是否与磁盘不一致（用于避免无改动的二次写盘）。

    参数:
        store: 工程。
        chapter: 内存中的章节 dict。

    返回:
        与磁盘不同（或磁盘尚无该章）时为 True。
    """
    try:
        disk = store.load_chapter(str(chapter.get("id") or ""))
    except FileNotFoundError:
        return True
    disk.pop("updated_at", None)
    current = {key: value for key, value in chapter.items() if key != "updated_at"}
    return disk != current


def _sync_script_from_scenes(chapter: dict, script: dict) -> bool:
    """把场景里的内容层改动同步回台本镜头（就地改 `script`）。

    对齐规则：优先按行 ID 重合度匹配镜头，匹配不到时按 `index`（场景序号 = 镜头序号 - 1）；
    镜头里台本自有字段（锚点、音效/画面/动画、过渡等）原样保留，只覆盖内容与描述字段。

    参数:
        chapter: 已改好场景的章节 dict。
        script: 导演脚本 dict（就地修改）。

    返回:
        台本是否发生变化。
    """
    scenes = list(chapter.get("scenes") or [])
    old_shots = list(script.get("shots") or [])
    remaining = list(old_shots)
    matched: dict[int, dict] = {}
    for position, scene in enumerate(scenes):
        shot = _match_shot(scene, remaining)
        if shot is not None:
            remaining.remove(shot)
            matched[position] = shot
    new_shots = [
        _shot_from_scene(chapter, scene, matched.get(position)) for position, scene in enumerate(scenes)
    ]
    if new_shots == old_shots:
        return False
    script["shots"] = new_shots
    script["status"] = "ready" if new_shots else "draft"
    return True


def _match_shot(scene: dict, remaining: list[dict]) -> dict | None:
    """为一个场景挑选可复用的旧镜头（行 ID 重合优先，其次 index）。

    参数:
        scene: 场景 dict。
        remaining: 尚未匹配的旧镜头列表。

    返回:
        匹配到的镜头；没有可复用时返回 None。
    """
    line_ids = {line.get("id") for line in scene.get("lines") or [] if line.get("id")}
    if line_ids:
        best: dict | None = None
        best_hit = 0
        for shot in remaining:
            hit = len(line_ids & {line.get("id") for line in shot.get("lines") or []})
            if hit > best_hit:
                best, best_hit = shot, hit
        if best is not None:
            return best
    index = int(scene.get("index") or 0) + 1
    for shot in remaining:
        if int(shot.get("index") or 0) == index:
            return shot
    return None


def _shot_from_scene(chapter: dict, scene: dict, shot: dict | None) -> dict:
    """用场景的当前内容生成镜头（旧镜头存在时保留其台本自有字段）。

    参数:
        chapter: 章节 dict。
        scene: 场景 dict。
        shot: 可复用的旧镜头，可为空（新场景）。

    返回:
        镜头 dict。
    """
    if shot is None:
        return _shot_for_scene(chapter, scene)
    updated = copy.deepcopy(shot)
    updated["index"] = int(scene.get("index") or 0) + 1
    title = str(scene.get("title") or "")
    if title:
        # 场景标题为空时不把镜头名写入，保证「引导 → 派生」不凭空补标题
        updated["scene_label"] = title
    updated.setdefault("scene_label", f"场景 {updated['index']}")
    for field in SCENE_DESCRIPTIVE_FIELDS:
        if field in scene:
            updated[field] = copy.deepcopy(scene[field])
    if "time_of_day" in updated and not updated["time_of_day"]:
        updated["time_of_day"] = "unknown"
    if scene.get("bgm") is None:
        updated.pop("bgm", None)
    else:
        updated["bgm"] = copy.deepcopy(scene["bgm"])
    updated["lines"] = _shot_lines_from_scene(scene, shot)
    updated["revision"] = int(scene.get("revision") or updated.get("revision") or 1)
    return updated


def _shot_lines_from_scene(scene: dict, shot: dict) -> list[dict]:
    """按场景台词行生成镜头行（保留旧镜头行的锚点等台本自有字段）。

    参数:
        scene: 场景 dict。
        shot: 旧镜头 dict（提供可复用行）。

    返回:
        镜头行列表，顺序与场景行一致。
    """
    old_lines = {line.get("id"): line for line in shot.get("lines") or []}
    result = []
    for source in scene.get("lines") or []:
        old = old_lines.get(source.get("id"))
        entry = copy.deepcopy(old) if old is not None else _line_from_scene_line(source)
        entry["id"] = source.get("id")
        entry["index"] = int(source.get("index") or 0)
        entry["revision"] = int(source.get("revision") or entry.get("revision") or 1)
        for field in CONTENT_LINE_FIELDS:
            if field == "index":
                continue
            if field in source:
                entry[field] = copy.deepcopy(source[field])
        if entry.get("anchor") is None:
            # 台本 schema 要求镜头行必带锚点：新行按原文合成，旧行沿用原锚点
            entry["anchor"] = _anchor_for(str(source.get("text") or ""), str(source.get("id") or ""))
        result.append(entry)
    return result


def read_scenes(store: ProjectStore | None, chapter: dict) -> list[dict]:
    """统一读入口：返回章节的场景列表（含台词行）。

    `NDW_LINES_SOURCE=script`（默认）时读取台本并派生（无台本则内存引导）；
    设成 `scenes` 时直接返回落盘的场景，用于回退对照。

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

    `NDW_LINES_SOURCE=script`（默认）时返回浅拷贝，`scenes` 换成由台本派生的场景；
    设成 `scenes` 时原样返回入参章节，行为与改造前一致。

    约定：读路径拿到的派生视图不得回写章节；内容写点统一走 `save_lines`。

    参数:
        store: 工程，可为空（只做纯换算时不必传）。
        chapter: 从工程读出的章节 dict。

    返回:
        章节 dict；`scenes` 源下与入参是同一对象。
    """
    if lines_source() != "script":
        return chapter
    view = dict(chapter)
    view["scenes"] = read_scenes(store, chapter)
    return view


def chapter_views(store: ProjectStore | None, chapter_ids: list[str] | None = None) -> list[dict]:
    """批量返回章节视图，供只能拿数据、不能回读工程的 adapter 使用。

    参数:
        store: 工程。
        chapter_ids: 章节 ID 列表；为空表示全部章节（按工程章节顺序）。

    返回:
        章节视图列表；默认源下元素就是原始的章节 dict。
    """
    if chapter_ids:
        chapters = [store.load_chapter(chapter_id) for chapter_id in chapter_ids]
    else:
        chapters = [store.load_chapter(item["id"]) for item in store.list_chapters()]
    return [chapter_view(store, chapter) for chapter in chapters]


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
