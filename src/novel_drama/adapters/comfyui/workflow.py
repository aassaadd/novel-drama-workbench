"""ComfyUI UI 工作流（nodes/links）→ API prompt（class_type/inputs）转换。

ComfyUI 界面保存的工作流是「画布图」格式：`nodes` 里带坐标、控件值（`widgets_values`）
与备注节点，`links` 单独存放连线；而 `/prompt` 接口要的是 API 格式：

    {"<节点ID>": {"class_type": "KSampler", "inputs": {...}}}

本模块只做一次性转换（供模板生成与 `ndw comfy import` 使用）：

- 先向运行中的 ComfyUI 请求 `/object_info/<class_type>`，按 `required` + `optional`
  的声明顺序筛出「控件型」输入，建立「`widgets_values` 顺序 → 输入名」映射；
- UI 节点自带的 `inputs[].widget.name` 更贴近前端实际渲染顺序，存在时优先使用；
- `control_after_generate`、`upload` 这类前端附加值不进入 API，只保留真实输入；
- `links` 解析成 `[上游节点ID, 输出槽位]`；
- 跳过静音（mode=2）/ 旁路（mode=4）节点与备注类非可执行节点，仅保留从输出节点
  可达的子图（与 ComfyUI「导出（API）」的行为一致）。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Mapping

#: 控件型输入类型（值来自 widgets_values，而不是连线）
_WIDGET_TYPES = frozenset({"INT", "FLOAT", "STRING", "BOOLEAN", "COMBO", "MULTISELECT"})

#: 纯画布节点：只存在于前端，不可提交给 /prompt
_NON_EXECUTABLE_TYPES = frozenset({"MarkdownNote", "Note", "Reroute", "PrimitiveNode"})

#: `control_after_generate` 等前端附加控件的取值
_CONTROL_VALUES = frozenset({"fixed", "increment", "decrement", "randomize"})

#: 静音 / 旁路 模式
_SKIP_MODES = frozenset({2, 4})


class ComfyWorkflowError(ValueError):
    """工作流转换失败（连线缺失、引用不存在等）。"""


def load_object_info(
    base_url: str,
    class_types: list[str] | None = None,
    timeout: float = 10.0,
) -> dict[str, dict[str, Any]]:
    """向运行中的 ComfyUI 查询节点定义。

    参数:
        base_url: ComfyUI 根地址，如 `http://127.0.0.1:8188`。
        class_types: 需要查询的节点类型；为空时抓取 `/object_info` 全量。
        timeout: 单次请求超时秒数。

    返回:
        `{class_type: object_info}`；未注册的类型不会出现在结果里。

    异常:
        ComfyWorkflowError: 地址不可达、超时或返回非法 JSON。
    """
    base = base_url.rstrip("/")
    if class_types:
        result: dict[str, dict[str, Any]] = {}
        for class_type in class_types:
            url = f"{base}/object_info/{class_type}"
            try:
                with urllib.request.urlopen(url, timeout=timeout) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                if exc.code == 404:  # 未注册的自定义节点
                    continue
                raise ComfyWorkflowError(f"查询 {url} 失败：HTTP {exc.code}") from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                raise ComfyWorkflowError(f"无法连接 ComfyUI（{base}）：{exc}") from exc
            except json.JSONDecodeError as exc:
                raise ComfyWorkflowError(f"{url} 返回非法 JSON：{exc}") from exc
            if isinstance(payload, dict):
                result.update(payload)
        return result

    url = f"{base}/object_info"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ComfyWorkflowError(f"无法连接 ComfyUI（{base}）：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise ComfyWorkflowError(f"{url} 返回非法 JSON：{exc}") from exc
    if not isinstance(payload, dict):
        raise ComfyWorkflowError(f"{url} 返回结构异常")
    return payload


def convert_ui_workflow(
    workflow: Mapping[str, Any],
    *,
    object_info: Mapping[str, Mapping[str, Any]] | None = None,
    base_url: str | None = None,
    timeout: float = 10.0,
    warnings: list[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """把 ComfyUI UI 工作流转成 API prompt。

    参数:
        workflow: UI 工作流字典（含 `nodes` / `links`）。
        object_info: 已抓取的 `/object_info` 结果；为空时按 `base_url` 现场抓取。
        base_url: ComfyUI 地址；为空且没有 `object_info` 时退化为「只按 UI 声明转换」。
        timeout: 抓取 `/object_info` 的超时秒数。
        warnings: 可选列表，用于收集被跳过的节点、被丢弃的附加值等非致命提示。

    返回:
        `{节点ID字符串: {"class_type": 类型, "inputs": {...}}}`。

    异常:
        ComfyWorkflowError: 缺少 `nodes`、连线引用不存在或没有可提交的节点。
    """
    notes = warnings if warnings is not None else []
    raw_nodes = workflow.get("nodes")
    if not isinstance(raw_nodes, list):
        raise ComfyWorkflowError("工作流缺少 nodes 数组，可能不是 ComfyUI UI 格式")

    nodes: dict[int, Mapping[str, Any]] = {}
    for node in raw_nodes:
        if not isinstance(node, Mapping) or "id" not in node:
            continue
        nodes[int(node["id"])] = node
    if not nodes:
        raise ComfyWorkflowError("工作流 nodes 为空")

    links = _link_map(workflow)

    info: dict[str, dict[str, Any]] = {str(k): dict(v) for k, v in (object_info or {}).items()}
    if not info and base_url:
        types = sorted({str(node.get("type")) for node in nodes.values()})
        try:
            info = load_object_info(base_url, types, timeout)
        except ComfyWorkflowError as exc:
            notes.append(f"读取 ComfyUI /object_info 失败（{exc}），改为只按 UI 声明的 widget 名转换")
    has_info = bool(info)

    skipped: dict[int, str] = {}
    for node_id, node in nodes.items():
        class_type = str(node.get("type"))
        mode = int(node.get("mode") or 0)
        if mode in _SKIP_MODES:
            skipped[node_id] = f"mode={mode}（静音/旁路）"
        elif class_type in _NON_EXECUTABLE_TYPES:
            skipped[node_id] = "前端节点，不可提交"
        elif has_info and class_type not in info:
            skipped[node_id] = "未在 /object_info 注册"

    kept = _reachable_from_outputs(nodes, links, info if has_info else None)
    if kept is None:
        kept = {node_id for node_id in nodes if node_id not in skipped}
    for node_id in nodes:
        if node_id not in kept and node_id not in skipped:
            skipped[node_id] = "未被任何输出节点引用"
            notes.append(f"跳过节点 {node_id}（{nodes[node_id].get('type')}）：未被任何输出节点引用")

    prompt: dict[str, dict[str, Any]] = {}
    for node_id in sorted(kept - set(skipped)):
        node = nodes[node_id]
        class_type = str(node.get("type"))
        inputs = _build_inputs(
            node_id,
            node,
            info.get(class_type) or {},
            links,
            skipped,
            notes,
        )
        if not inputs and not (info.get(class_type) or {}).get("input"):
            notes.append(f"节点 {node_id}（{class_type}）没有可提交的输入")
        prompt[str(node_id)] = {"class_type": class_type, "inputs": inputs}

    if not prompt:
        raise ComfyWorkflowError("工作流里没有可提交给 /prompt 的节点")
    return prompt


def _link_map(workflow: Mapping[str, Any]) -> dict[int, tuple[int, int]]:
    """建立「连线ID → (上游节点ID, 输出槽位)」映射。

    参数:
        workflow: UI 工作流字典。

    返回:
        连线索引表；兼容 `[id, origin_id, origin_slot, target_id, target_slot, type]`
        与 `{"id": ..., "origin_id": ...}` 两种写法。
    """
    result: dict[int, tuple[int, int]] = {}
    for link in workflow.get("links") or []:
        if isinstance(link, Mapping):
            link_id = link.get("id")
            origin = link.get("origin_id", link.get("origin"))
            slot = link.get("origin_slot", 0)
        elif isinstance(link, (list, tuple)) and len(link) >= 3:
            link_id, origin, slot = link[0], link[1], link[2]
        else:
            continue
        if link_id is None or origin is None:
            continue
        result[int(link_id)] = (int(origin), int(slot))
    return result


def _input_specs(node_info: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """按 required + optional 的声明顺序展开输入规格。

    参数:
        node_info: 单个节点的 object_info。

    返回:
        `{输入名: 规格选项字典}`，顺序即声明顺序。
    """
    specs: dict[str, dict[str, Any]] = {}
    raw = node_info.get("input") if isinstance(node_info, Mapping) else None
    if not isinstance(raw, Mapping):
        return specs
    for section in ("required", "optional"):
        entries = raw.get(section)
        if not isinstance(entries, Mapping):
            continue
        for name, spec in entries.items():
            options = {}
            if isinstance(spec, (list, tuple)) and len(spec) >= 2 and isinstance(spec[1], Mapping):
                options = dict(spec[1])
            specs[str(name)] = options
    return specs


def _spec_type(node_info: Mapping[str, Any], name: str) -> str | None:
    """取出某个输入的类型描述。

    参数:
        node_info: 单个节点的 object_info。
        name: 输入名。

    返回:
        类型字符串；缺失时返回 None（对象数组形式的 COMBO 返回 `"COMBO"`）。
    """
    raw = node_info.get("input") if isinstance(node_info, Mapping) else None
    if not isinstance(raw, Mapping):
        return None
    for section in ("required", "optional"):
        entries = raw.get(section)
        if not isinstance(entries, Mapping) or name not in entries:
            continue
        spec = entries[name]
        if isinstance(spec, (list, tuple)) and spec:
            head = spec[0]
            if isinstance(head, list):
                return "COMBO"
            if isinstance(head, str):
                return head
        return None
    return None


def _is_widget_type(type_name: str | None) -> bool:
    """判断输入类型是否由前端控件赋值。

    参数:
        type_name: `/object_info` 里的类型名。

    返回:
        是控件型（INT/FLOAT/STRING/BOOLEAN/COMBO/COMFY_*）返回 True。
    """
    if not type_name:
        return False
    if type_name in _WIDGET_TYPES:
        return True
    return type_name.startswith("COMFY_")


def _widget_names_from_info(node_info: Mapping[str, Any]) -> list[str]:
    """按对象定义顺序列出「控件型」输入名（对应 widgets_values 顺序）。

    参数:
        node_info: 单个节点的 object_info。

    返回:
        控件输入名列表。
    """
    names: list[str] = []
    for name in _input_specs(node_info):
        if _is_widget_type(_spec_type(node_info, name)):
            names.append(name)
    return names


def _widget_names_from_ui(node: Mapping[str, Any]) -> list[str]:
    """从 UI 节点的 `inputs[].widget.name` 取控件名（前端实际渲染顺序）。

    参数:
        node: UI 工作流里的单个节点。

    返回:
        控件输入名列表；节点未声明时返回空列表。
    """
    names: list[str] = []
    for item in node.get("inputs") or []:
        if not isinstance(item, Mapping):
            continue
        widget = item.get("widget")
        if isinstance(widget, Mapping) and widget.get("name"):
            names.append(str(widget["name"]))
    return names


def _build_inputs(
    node_id: int,
    node: Mapping[str, Any],
    node_info: Mapping[str, Any],
    links: Mapping[int, tuple[int, int]],
    skipped: Mapping[int, str],
    notes: list[str],
) -> dict[str, Any]:
    """拼装单个 API 节点的 inputs。

    参数:
        node_id: 节点 ID。
        node: UI 工作流里的节点。
        node_info: 该节点类型的 object_info（可能为空）。
        links: 连线索引表。
        skipped: 被跳过节点及原因。
        notes: 非致命提示收集列表。

    返回:
        `{输入名: 值或 [上游节点ID, 输出槽位]}`。

    异常:
        ComfyWorkflowError: 输入引用的连线在工作流里不存在。
    """
    specs = _input_specs(node_info)
    widget_names = _widget_names_from_ui(node) or _widget_names_from_info(node_info)
    values = node.get("widgets_values")
    value_list = list(values) if isinstance(values, list) else []

    inputs: dict[str, Any] = {}
    index = 0
    for name in widget_names:
        options = specs.get(name, {})
        # 没有 object_info 时只能靠取值猜：数值控件后面跟 randomize/fixed 等即为 control_after_generate
        while (
            not specs
            and index < len(value_list)
            and isinstance(value_list[index], str)
            and value_list[index] in _CONTROL_VALUES
        ):
            index += 1
        if index >= len(value_list):
            break
        inputs[name] = value_list[index]
        index += 1
        if options.get("control_after_generate") and index < len(value_list):
            index += 1  # 丢弃 control_after_generate 的附加值
    if index < len(value_list):
        notes.append(
            f"节点 {node_id}（{node.get('type')}）有 {len(value_list) - index} 个前端附加值未映射，已丢弃"
        )

    for item in node.get("inputs") or []:
        if not isinstance(item, Mapping):
            continue
        link_id = item.get("link")
        if link_id is None:
            continue
        name = str(item.get("name") or "")
        if not name:
            continue
        if int(link_id) not in links:
            raise ComfyWorkflowError(f"节点 {node_id} 的输入 {name} 引用了不存在的连线 {link_id}")
        origin, slot = links[int(link_id)]
        if origin in skipped:
            inputs.pop(name, None)
            notes.append(
                f"节点 {node_id} 的输入 {name} 来自被跳过的节点 {origin}（{skipped[origin]}），已断开"
            )
            continue
        inputs[name] = [str(origin), slot]
    return inputs


def _reachable_from_outputs(
    nodes: Mapping[int, Mapping[str, Any]],
    links: Mapping[int, tuple[int, int]],
    info: Mapping[str, Mapping[str, Any]] | None,
) -> set[int] | None:
    """从输出节点（`output_node=true`）反向收集可达节点。

    参数:
        nodes: 节点表。
        links: 连线索引表。
        info: `/object_info` 结果；为空表示无法判断输出节点。

    返回:
        需要提交的节点 ID 集合；`info` 为空时返回 None 表示不裁剪。
    """
    if not info:
        return None
    output_ids = [
        node_id
        for node_id, node in nodes.items()
        if (info.get(str(node.get("type"))) or {}).get("output_node")
    ]
    if not output_ids:
        return None
    seen: set[int] = set()
    stack = list(output_ids)
    while stack:
        node_id = stack.pop()
        if node_id in seen or node_id not in nodes:
            continue
        seen.add(node_id)
        for item in nodes[node_id].get("inputs") or []:
            if not isinstance(item, Mapping):
                continue
            link_id = item.get("link")
            if link_id is None or int(link_id) not in links:
                continue
            stack.append(links[int(link_id)][0])
    return seen
