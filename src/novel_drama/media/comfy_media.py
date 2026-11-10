"""ComfyUI 模板加载、参数填充与画幅解析。

职责边界（Spec Task 5）：
- 模板与清单只读：本模块从不修改 `config/comfy/` 下的模板文件；
- 按 manifest 的 `bindings`（节点 ID + 输入名）写入 prompt / negative_prompt /
  width / height / seed / filename_prefix / input_image，数值保持 int；
- 模板里只允许白名单占位符，填完后仍残留 `{{...}}` 直接 `INVALID_INPUT`；
- 画幅按「条目 > 工程 > 1:1」解析，再经模板 `size_map` 映射为宽高；
- 视频只支持 9:16 / 16:9 / 1:1，其它画幅取比例最接近的一档并返回中文降级说明；
- seed 与产物前缀由「条目 ID + revision」稳定派生，保证同一 revision 可复现。
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path

from ..adapters.base import AdapterError, ErrorCode
from ..config import REPO_ROOT, AppConfig

#: 模板目录缺省值（相对仓库根目录）
DEFAULT_TEMPLATES_DIR = "config/comfy"

#: 占位符：只允许字母数字下划线
PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")

#: 路径不安全字符（用于产物前缀）
_UNSAFE_PATH_RE = re.compile(r"[^A-Za-z0-9._-]")

#: 各用途的内置模板（manifest 里的模板 ID 不存在时回落到这里）
_BUILTIN_TEMPLATES = {
    "image": ["qwen_image_accel"],
    "video": ["minimax_h3_accel", "minimax_h3_accel_t2v"],
    "video_i2v": ["minimax_h3_accel"],
    "video_t2v": ["minimax_h3_accel_t2v"],
}

#: 视频允许的画幅（其余画幅取最接近的一档）
VIDEO_RATIOS = ("9:16", "16:9", "1:1")

#: 模板缺少清单登记时，按类型要求的必需占位符
_DEFAULT_REQUIRED = {
    "image": ["prompt", "width", "height", "seed", "filename_prefix"],
    "video": ["prompt", "width", "height", "seed", "filename_prefix"],
    "video_i2v": ["prompt", "width", "height", "seed", "filename_prefix", "input_image"],
    "video_t2v": ["prompt", "width", "height", "seed", "filename_prefix"],
}


def templates_dir(config: AppConfig | None = None, override: str | Path | None = None) -> Path:
    """解析模板目录（含 `manifest.json`）。

    参数:
        config: 本机配置；读取 `comfyui.templates_dir`。
        override: 直接指定的目录（优先于配置）。

    返回:
        绝对路径；相对路径按仓库根目录解析。
    """
    if override is not None:
        path = Path(override).expanduser()
    else:
        raw = config.get("comfyui.templates_dir", DEFAULT_TEMPLATES_DIR) if config is not None else DEFAULT_TEMPLATES_DIR
        path = Path(str(raw or DEFAULT_TEMPLATES_DIR)).expanduser()
    return path if path.is_absolute() else (REPO_ROOT / path)


def load_manifest(templates_dir: str | Path | None = None) -> dict:
    """读取 `config/comfy/manifest.json`。

    参数:
        templates_dir: 模板目录；为空时用仓库内置 `config/comfy`。

    返回:
        清单 dict（含 `templates` 数组）。

    异常:
        AdapterError(INVALID_INPUT): 清单缺失、无法读取或结构不是 JSON 对象。
    """
    root = Path(templates_dir).expanduser() if templates_dir else REPO_ROOT / DEFAULT_TEMPLATES_DIR
    path = root / "manifest.json"
    if not path.exists():
        raise AdapterError(
            ErrorCode.INVALID_INPUT,
            f"未找到 ComfyUI 模板清单：{path}（请确认 comfyui.templates_dir 指向含 manifest.json 的目录）",
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AdapterError(ErrorCode.INVALID_INPUT, f"模板清单读取/解析失败：{path}（{exc}）") from exc
    if not isinstance(data, dict):
        raise AdapterError(ErrorCode.INVALID_INPUT, f"模板清单格式错误：{path}（必须是 JSON 对象）")
    return data


def manifest_entries(manifest: dict) -> list[dict]:
    """把清单登记统一成 dict 列表（兼容 templates 为数组或对象）。

    参数:
        manifest: 清单 dict。

    返回:
        模板登记列表，每项至少含 `id`。
    """
    raw = manifest.get("templates") if manifest.get("templates") is not None else manifest.get("workflows")
    if isinstance(raw, dict):
        result = []
        for key, value in raw.items():
            entry = dict(value) if isinstance(value, dict) else {}
            entry.setdefault("id", str(key))
            result.append(entry)
        return result
    if isinstance(raw, list):
        return [dict(item) for item in raw if isinstance(item, dict)]
    return []


def template_by_id(manifest: dict, template_id: str) -> dict:
    """按模板 ID 取清单登记。

    参数:
        manifest: 清单 dict。
        template_id: 模板 ID。

    返回:
        模板登记 dict。

    异常:
        AdapterError(INVALID_INPUT): 清单里没有该模板。
    """
    entries = manifest_entries(manifest)
    for entry in entries:
        if str(entry.get("id") or entry.get("template_id")) == str(template_id):
            return entry
    known = "、".join(str(entry.get("id") or entry.get("template_id")) for entry in entries) or "（空）"
    raise AdapterError(ErrorCode.INVALID_INPUT, f"模板 {template_id} 不在清单中（可选：{known}）")


def builtin_template_ids(kind: str) -> list[str]:
    """返回某类用途的内置模板 ID 列表（第一个为默认）。

    参数:
        kind: image / video / video_i2v / video_t2v。

    返回:
        模板 ID 列表。

    异常:
        AdapterError(INVALID_INPUT): kind 非法。
    """
    ids = _BUILTIN_TEMPLATES.get(str(kind))
    if not ids:
        raise AdapterError(ErrorCode.INVALID_INPUT, f"未知模板用途：{kind}（可选 image/video/video_i2v/video_t2v）")
    return list(ids)


def resolve_template(template_id: str | None, kind: str, manifest: dict | None = None) -> dict:
    """解析要用的模板；不存在时回落到对应内置 ID（Task 4.3）。

    参数:
        template_id: 工程/配置里的模板 ID（可为空）。
        kind: image / video_i2v / video_t2v。
        manifest: 已加载的清单；为空时按仓库默认目录读取。

    返回:
        `{"template_id": 实际使用的 ID, "template": 清单登记, "warning": 中文提示或 None}`。
    """
    data = manifest if manifest is not None else load_manifest()
    available = {str(entry.get("id") or entry.get("template_id")) for entry in manifest_entries(data)}
    if template_id and str(template_id) in available:
        return {"template_id": str(template_id), "template": template_by_id(data, str(template_id)), "warning": None}
    fallback = builtin_template_ids(kind)[0]
    warning = None
    if template_id:
        warning = f"模板 {template_id} 不存在，已回落 {fallback}"
    return {"template_id": fallback, "template": template_by_id(data, fallback), "warning": warning}


def _ratio_value(ratio: str) -> float | None:
    """把 `宽:高` 解析成浮点比例。

    参数:
        ratio: 形如 `9:16` 的画幅字符串。

    返回:
        宽高比（浮点）；无法解析时返回 None。
    """
    text = str(ratio or "").strip()
    if ":" not in text:
        return None
    left, right = text.split(":", 1)
    try:
        width, height = float(left), float(right)
    except ValueError:
        return None
    if width <= 0 or height <= 0:
        return None
    return width / height


def _pick_ratio(ratio: str, allowed: tuple[str, ...] | list[str], size_map: dict) -> tuple[str, bool, str | None]:
    """在允许的画幅里选出实际使用的一档。

    参数:
        ratio: 请求的画幅。
        allowed: 允许的画幅列表。
        size_map: 模板的画幅 → [宽, 高] 映射。

    返回:
        `(实际画幅, 是否降级, 中文说明或 None)`；都用浮点比例距离找最接近档。
    """
    candidates = [item for item in allowed if item in size_map]
    if not candidates:
        raise AdapterError(ErrorCode.INVALID_INPUT, f"模板清单缺少 size_map 画幅映射（可用：{'、'.join(size_map) or '空'}）")
    if ratio in candidates:
        return ratio, False, None
    target = _ratio_value(ratio)
    if target is None:
        chosen = candidates[0]
        width, height = size_map[chosen]
        return chosen, True, f"画幅 {ratio} 无法解析，已按 {chosen}（{width}×{height}）生成"
    chosen = min(candidates, key=lambda item: (abs((_ratio_value(item) or 0) - target), candidates.index(item)))
    width, height = size_map[chosen]
    return chosen, True, f"画幅 {ratio} 不支持，已按最接近的 {chosen}（{width}×{height}）生成"


def _size_map_for(kind: str, manifest: dict | None = None) -> dict:
    """取某类用途默认模板的画幅映射。

    参数:
        kind: image / video_i2v / video_t2v / video。
        manifest: 已加载的清单。

    返回:
        `{画幅: [宽, 高]}`。

    异常:
        AdapterError(INVALID_INPUT): 模板未配置 size_map。
    """
    data = manifest if manifest is not None else load_manifest()
    entry = template_by_id(data, builtin_template_ids(kind)[0])
    size_map = entry.get("size_map")
    if not isinstance(size_map, dict) or not size_map:
        raise AdapterError(ErrorCode.INVALID_INPUT, f"模板 {entry.get('id')} 缺少 size_map 画幅映射")
    return size_map


def resolve_aspect(item: dict, project_settings: dict, kind: str) -> dict:
    """解析条目画幅与宽高。

    优先级：`item["aspect_ratio"]` > `project_settings["output"]["aspect_ratio"]` > `1:1`。
    视频只允许 9:16 / 16:9 / 1:1，其余画幅按浮点比例距离取最接近的一档。

    参数:
        item: 关键图或视频条目。
        project_settings: 工程设置（读 `output.aspect_ratio`）。
        kind: image / video。

    返回:
        `{"ratio","width","height","fallback","note"}`；`fallback=True` 表示发生了降级。
    """
    output = project_settings.get("output") if isinstance(project_settings, dict) else None
    ratio = str(item.get("aspect_ratio") or "").strip()
    if not ratio:
        ratio = str((output or {}).get("aspect_ratio") or "").strip()
    if not ratio:
        ratio = "1:1"
    size_map = _size_map_for("video" if kind == "video" else "image")
    allowed = VIDEO_RATIOS if kind == "video" else tuple(size_map.keys())
    chosen, fallback, note = _pick_ratio(ratio, allowed, size_map)
    width, height = size_map[chosen]
    return {"ratio": chosen, "width": int(width), "height": int(height), "fallback": fallback, "note": note}


def seed_for(item_id: str, revision: int) -> int:
    """按「条目 ID + revision」派生稳定 seed。

    参数:
        item_id: 条目 ID。
        revision: 条目版本号。

    返回:
        0 ~ 2^32-1 的整数（sha256 前 8 位十六进制）。
    """
    digest = hashlib.sha256(f"{item_id}:{int(revision)}".encode("utf-8")).hexdigest()
    return int(digest[:8], 16)


def output_prefix(item_id: str, revision: int) -> str:
    """生成 ComfyUI 产物前缀 `ndw/<条目ID>_r<revision>`。

    参数:
        item_id: 条目 ID。
        revision: 条目版本号。

    返回:
        前缀字符串，路径不安全字符已替换为 `_`。
    """
    safe_id = _UNSAFE_PATH_RE.sub("_", str(item_id))
    return f"ndw/{safe_id}_r{int(revision)}"


def _require_binding(template: dict, template_id: str, name: str, spec: object) -> tuple[dict, str]:
    """定位绑定指向的节点与输入名。

    参数:
        template: API 格式模板（会被就地修改）。
        template_id: 模板 ID（用于报错）。
        name: 参数名。
        spec: 清单里的绑定 `{"node": "...", "input": "..."}`。

    返回:
        `(节点 dict, 输入名)`。

    异常:
        AdapterError(INVALID_INPUT): 绑定缺少节点/输入名，或节点不存在。
    """
    if not isinstance(spec, dict) or not spec.get("node") or not spec.get("input"):
        raise AdapterError(ErrorCode.INVALID_INPUT, f"模板 {template_id} 的参数 {name} 绑定格式错误（需 node/input）")
    node_id = str(spec["node"])
    node = template.get(node_id)
    if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
        raise AdapterError(ErrorCode.INVALID_INPUT, f"模板 {template_id} 的参数 {name} 指向不存在的节点 {node_id}")
    return node, str(spec["input"])


def _leftover_placeholders(node: object, found: set[str]) -> set[str]:
    """递归收集模板中残留的占位符名。

    参数:
        node: 模板节点/值。
        found: 收集集合（就地追加）。

    返回:
        占位符名集合。
    """
    if isinstance(node, dict):
        for value in node.values():
            _leftover_placeholders(value, found)
    elif isinstance(node, list):
        for value in node:
            _leftover_placeholders(value, found)
    elif isinstance(node, str):
        found.update(PLACEHOLDER_RE.findall(node))
    return found


def build_prompt(
    config: AppConfig,
    template_id: str,
    kind: str,
    *,
    prompt: str,
    negative_prompt: str = "",
    aspect_ratio: str = "1:1",
    item_id: str,
    revision: int,
    input_image: str | None = None,
    seed: int | None = None,
) -> dict:
    """按清单填参，生成可直接提交的 API 格式工作流。

    参数:
        config: 本机配置（读 `comfyui.templates_dir`）。
        template_id: 模板 ID；不存在时回落到内置模板并返回 warning。
        kind: image / video_i2v / video_t2v。
        prompt: 正向提示词。
        negative_prompt: 负向提示词（模板没有该占位符时忽略）。
        aspect_ratio: 请求的画幅。
        item_id: 条目 ID（用于 seed 与产物前缀）。
        revision: 条目版本号。
        input_image: ComfyUI 输入图片名（i2v 首帧）。
        seed: 指定 seed；为空时按「条目 ID + revision」派生。

    返回:
        `{"template_id","kind","prompt","width","height","aspect_ratio","seed",
        "filename_prefix","warning"}`。

    异常:
        AdapterError(INVALID_INPUT): 模板缺失/损坏、缺必需值、占位符未解析。
    """
    directory = templates_dir(config)
    manifest = load_manifest(directory)
    resolved = resolve_template(template_id, kind, manifest)
    actual_id = resolved["template_id"]
    entry = resolved["template"]
    warning = resolved["warning"]

    file_name = str(entry.get("file") or f"{actual_id}.json")
    path = Path(file_name).expanduser()
    if not path.is_absolute():
        path = directory / path
    if not path.exists():
        raise AdapterError(ErrorCode.INVALID_INPUT, f"未找到模板文件 {actual_id}：{path}（执行 ndw comfy import 生成）")
    try:
        template = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AdapterError(ErrorCode.INVALID_INPUT, f"模板 {actual_id} 读取/解析失败：{exc}") from exc
    if not isinstance(template, dict) or not template:
        raise AdapterError(ErrorCode.INVALID_INPUT, f"模板 {actual_id} 必须是 API 格式的非空 JSON 对象")

    size_map = entry.get("size_map") if isinstance(entry.get("size_map"), dict) else {}
    allowed = VIDEO_RATIOS if kind.startswith("video") else tuple(size_map.keys())
    ratio, _fallback, aspect_note = _pick_ratio(str(aspect_ratio or "1:1"), allowed, size_map)
    width, height = size_map[ratio]
    actual_seed = int(seed) if seed is not None else seed_for(item_id, revision)
    prefix = output_prefix(item_id, revision)
    values = {
        "prompt": prompt or "",
        "negative_prompt": negative_prompt or "",
        "width": int(width),
        "height": int(height),
        "seed": actual_seed,
        "filename_prefix": prefix,
        "input_image": input_image,
    }

    required = entry.get("required_placeholders") or _DEFAULT_REQUIRED.get(kind, [])
    missing = [name for name in required if values.get(name) in (None, "")]
    if missing:
        raise AdapterError(
            ErrorCode.INVALID_INPUT,
            f"模板 {actual_id} 缺少必需参数：{', '.join(missing)}" + ("（提示词不能为空）" if "prompt" in missing else ""),
        )

    bindings = entry.get("bindings") if isinstance(entry.get("bindings"), dict) else {}
    for name, spec in bindings.items():
        value = values.get(name)
        if value is None:
            continue
        node, input_name = _require_binding(template, actual_id, name, spec)
        node["inputs"][input_name] = copy.deepcopy(value)

    leftover = _leftover_placeholders(template, set())
    if leftover:
        raise AdapterError(
            ErrorCode.INVALID_INPUT,
            f"模板 {actual_id} 仍存在未解析占位符：{', '.join('{{' + name + '}}' for name in sorted(leftover))}",
        )

    note = warning
    if aspect_note:
        note = f"{note}；{aspect_note}" if note else aspect_note
    return {
        "template_id": actual_id,
        "kind": kind,
        "prompt": template,
        "width": int(width),
        "height": int(height),
        "aspect_ratio": ratio,
        "seed": actual_seed,
        "filename_prefix": prefix,
        "warning": note,
    }


def fixed_summary(manifest: dict, template_id: str) -> dict:
    """返回模板的只读固定参数摘要（供 CLI / Web 展示）。

    参数:
        manifest: 清单 dict。
        template_id: 模板 ID。

    返回:
        `{"template_id","kind","display_name","file","source","fixed","size_map",
        "models","required_placeholders"}`。
    """
    entry = template_by_id(manifest, template_id)
    return {
        "template_id": str(entry.get("id") or template_id),
        "kind": entry.get("kind"),
        "display_name": entry.get("display_name"),
        "file": entry.get("file"),
        "source": entry.get("source"),
        "fixed": entry.get("fixed") or {},
        "size_map": entry.get("size_map") or {},
        "models": list(entry.get("models") or []),
        "required_placeholders": list(entry.get("required_placeholders") or entry.get("placeholders") or []),
    }
