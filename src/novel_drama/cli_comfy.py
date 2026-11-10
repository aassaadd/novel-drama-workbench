"""`ndw comfy` 子命令：模板清单、健康检查、模型扫描/反查/下载与模板导入（Spec Task 10）。

输出约定与其它 CLI 一致：`--json` 时 stdout 只输出可解析 JSON，普通文本走 print。
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from .adapters.base import AdapterError
from .adapters.comfyui import LocalComfyGateway
from .cli_common import (
    EXIT_FAILED,
    EXIT_NOT_IMPLEMENTED,
    EXIT_OK,
    EXIT_PRECONDITION,
    PreconditionError,
    _emit,
)
from .config import AppConfig
from .media import comfy_media, comfy_models
from .media.comfy_workflow import ComfyWorkflowError, convert_ui_workflow

#: 导入模板时必须出现的占位符（按用途）
_IMPORT_REQUIRED = {
    "image": ["prompt", "width", "height", "seed", "filename_prefix"],
    "video": ["prompt", "width", "height", "seed", "filename_prefix"],
}


def _gateway(config: AppConfig) -> LocalComfyGateway:
    """按配置构造本机 ComfyUI 网关。

    参数:
        config: 本机配置。

    返回:
        LocalComfyGateway。
    """
    return LocalComfyGateway(config.get("comfyui", {}) or {}, comfy_media.templates_dir(config))


def _models_dir(config: AppConfig) -> Path | None:
    """定位模型目录；失败返回 None（由调用方给出中文提示）。

    参数:
        config: 本机配置。

    返回:
        models 目录路径或 None。
    """
    try:
        return comfy_models.resolve_models_dir(config)
    except ValueError:
        return None


def _model_rows(config: AppConfig, keys: list[str]) -> list[dict]:
    """检查模板依赖模型是否就位。

    参数:
        config: 本机配置。
        keys: 相对 models 目录的模型键。

    返回:
        `[{"key","present"}]`；目录不可定位时全部 present=False。
    """
    root = _models_dir(config)
    rows = []
    for key in keys:
        present = bool(root and (root / key).is_file() and (root / key).stat().st_size > 0)
        rows.append({"key": key, "present": present})
    return rows


def cmd_comfy_list(args, config: AppConfig) -> int:
    """`ndw comfy list [--json]`：列出清单模板与校验结果。"""
    gateway = _gateway(config)
    try:
        manifest = comfy_media.load_manifest(comfy_media.templates_dir(config))
    except AdapterError as exc:
        _emit(args, f"✗ {exc.message}", {"ok": False, "error": exc.message, "templates": [], "invalid": []})
        return EXIT_FAILED

    validations = {item.template_id: item for item in gateway.list_templates()}
    rows = []
    invalid = []
    for entry in comfy_media.manifest_entries(manifest):
        template_id = str(entry.get("id") or entry.get("template_id"))
        summary = comfy_media.fixed_summary(manifest, template_id)
        check = validations.get(template_id)
        row = {
            "template_id": template_id,
            "kind": summary["kind"],
            "display_name": summary["display_name"],
            "file": summary["file"],
            "required_placeholders": summary["required_placeholders"],
            "fixed": summary["fixed"],
            "size_map": summary["size_map"],
            "models": _model_rows(config, summary["models"]),
            "valid": bool(check.valid) if check else False,
            "error": check.error if check else "模板文件不存在或未登记",
        }
        rows.append(row)
        if not row["valid"]:
            invalid.append({"template_id": template_id, "error": row["error"]})

    lines = [f"ComfyUI 模板清单：{len(rows)} 条（有效 {len(rows) - len(invalid)} / 异常 {len(invalid)}）"]
    for row in rows:
        missing = [item["key"] for item in row["models"] if not item["present"]]
        lines.append(
            f"- {row['template_id']} [{row['kind']}] 文件 {row['file']}；"
            f"必需占位符 {', '.join(row['required_placeholders']) or '无'}；"
            f"固定参数 {json.dumps(row['fixed'], ensure_ascii=False)}；"
            f"依赖模型 {'全部就位' if not missing else '缺失 ' + ', '.join(missing)}"
            + (f"；✗ {row['error']}" if not row["valid"] else "")
        )
    if invalid:
        lines.append("模板校验失败：")
        lines.extend(f"- {item['template_id']}：{item['error']}" for item in invalid)
    _emit(args, "\n".join(lines), {"ok": not invalid, "templates": rows, "invalid": invalid})
    return EXIT_OK


def cmd_comfy_health(args, config: AppConfig) -> int:
    """`ndw comfy health [--json]`：连通性 + 模板校验 + 依赖模型缺失。"""
    gateway = _gateway(config)
    health = gateway.health()
    templates = gateway.list_templates()
    invalid = [{"template_id": item.template_id, "error": item.error} for item in templates if not item.valid]
    manifest_models: list[str] = []
    try:
        manifest = comfy_media.load_manifest(comfy_media.templates_dir(config))
        for entry in comfy_media.manifest_entries(manifest):
            for key in entry.get("models") or []:
                if str(key) not in manifest_models:
                    manifest_models.append(str(key))
    except AdapterError as exc:
        invalid.append({"template_id": "manifest", "error": exc.message})
    missing = [row["key"] for row in _model_rows(config, manifest_models) if not row["present"]]
    queue = (health.extra or {}).get("queue") or {}

    lines = [
        ("✓ " if health.ok else "✗ ") + health.detail,
        f"版本：{(health.extra or {}).get('version') or '未知'}；"
        f"队列：运行 {queue.get('running')} / 等待 {queue.get('pending')}；延迟 {health.latency_ms}ms",
        f"模板：{len(templates)} 条，校验失败 {len(invalid)} 条",
        f"依赖模型缺失：{', '.join(missing) if missing else '无'}",
    ]
    lines.extend(f"- 模板异常 {item['template_id']}：{item['error']}" for item in invalid)
    _emit(
        args,
        "\n".join(lines),
        {
            "ok": bool(health.ok),
            "detail": health.detail,
            "version": (health.extra or {}).get("version"),
            "latency_ms": health.latency_ms,
            "queue": queue,
            "templates": [{"template_id": item.template_id, "valid": item.valid, "error": item.error} for item in templates],
            "missing_models": missing,
        },
    )
    return EXIT_OK if health.ok else EXIT_NOT_IMPLEMENTED


def cmd_comfy_scan(args, config: AppConfig) -> int:
    """`ndw comfy scan [--json]`：扫描模型目录并写回 models.json。"""
    try:
        result = comfy_models.scan(config, write=True)
    except ValueError as exc:
        _emit(args, f"✗ {exc}", {"ok": False, "error": str(exc)})
        return EXIT_FAILED
    rows = result.get("models") or []
    unreferenced = result.get("unreferenced") or []
    lines = [
        f"模型目录：{result.get('models_dir')}",
        f"模板依赖模型：{len(rows)} 个（就位 {sum(1 for item in rows if item.get('present'))} / 缺失 {sum(1 for item in rows if not item.get('present'))}）",
        f"未引用权重：{len(unreferenced)} 个",
    ]
    for item in rows:
        lines.append(f"- {item.get('key')}  {'就位' if item.get('present') else '缺失'}  {item.get('bytes', 0)} bytes")
    _emit(args, "\n".join(lines), result)
    return EXIT_OK


def cmd_comfy_resolve(args, config: AppConfig) -> int:
    """`ndw comfy resolve [--json] [--refresh]`：反查模型下载地址。"""
    try:
        result = comfy_models.resolve(config, refresh=bool(getattr(args, "refresh", False)))
    except ValueError as exc:
        _emit(args, f"✗ {exc}", {"ok": False, "error": str(exc)})
        return EXIT_FAILED
    items = result.get("items") or {}
    lines = [f"模型目录：{result.get('models_dir')}（解析时间 {result.get('resolved_at')}）"]
    for key, item in items.items():
        lines.append(
            f"- {key}：repo={item.get('repo') or '-'}；file={item.get('hf_filename') or '-'}；"
            f"source={item.get('source') or '-'}；url={item.get('url') or '-'}"
            + (f"；✗ {item.get('error')}" if item.get("error") else "")
        )
    _emit(args, "\n".join(lines), result)
    return EXIT_OK


def cmd_comfy_models(args, config: AppConfig) -> int:
    """`ndw comfy models list|verify|install`：转调 media.comfy_models。"""
    action = getattr(args, "models_action", "list")
    if action == "list":
        try:
            result = comfy_models.scan(config, write=False)
        except ValueError as exc:
            _emit(args, f"✗ {exc}", {"ok": False, "error": str(exc)})
            return EXIT_FAILED
        lines = [f"模型目录：{result.get('models_dir')}"]
        for item in result.get("models") or []:
            lines.append(f"- {item.get('key')}  {'就位' if item.get('present') else '缺失'}  {item.get('bytes', 0)} bytes")
        _emit(args, "\n".join(lines), result)
        return EXIT_OK

    if action == "verify":
        try:
            result = comfy_models.verify(config)
        except ValueError as exc:
            _emit(args, f"✗ {exc}", {"ok": False, "error": str(exc)})
            return EXIT_FAILED
        lines = [f"模型目录：{result.get('models_dir')}（{'全部就位' if result.get('ok') else '存在缺失/体积不一致'}）"]
        for item in result.get("models") or []:
            state = "✓" if item.get("ok") else "✗"
            lines.append(f"{state} {item.get('key')}  {item.get('bytes', 0)}/{item.get('expected', 0)} bytes")
        _emit(args, "\n".join(lines), result)
        return EXIT_OK if result.get("ok") else EXIT_FAILED

    if action != "install":
        _emit(args, f"✗ 未知子命令 comfy models {action}", {"ok": False, "error": "unknown_action"})
        return EXIT_PRECONDITION

    mirrors = [str(item) for item in (getattr(args, "mirror", None) or [])]
    try:
        result = comfy_models.install(
            config,
            key=args.key,
            yes=bool(getattr(args, "yes", False)),
            accept_license=bool(getattr(args, "accept_license", False)),
            dry_run=bool(getattr(args, "dry_run", False)),
            mirrors=mirrors or None,
        )
    except ValueError as exc:
        _emit(args, f"✗ {exc}", {"ok": False, "error": str(exc)})
        return EXIT_FAILED
    status = result.get("status")
    lines = [f"{result.get('key')}：{status}"]
    for field in ("target", "bytes", "expected", "mirror_used", "message", "error", "license", "url"):
        if result.get(field) is not None:
            lines.append(f"  {field}: {result.get(field)}")
    attempts = result.get("attempts") or []
    lines.extend(f"  尝试 {item.get('mirror')} → {'成功' if item.get('ok') else item.get('error')}" for item in attempts)
    _emit(args, "\n".join(lines), result)
    ok_status = {"installed", "already", "plan"}
    if status in ok_status:
        return EXIT_OK
    if status in ("need_license", "need_confirm"):
        return EXIT_PRECONDITION
    return EXIT_FAILED


def _validate_api_prompt(data: object) -> list[str]:
    """校验 API 格式工作流：节点结构合法、连线指向已存在的节点。

    参数:
        data: 解析后的 JSON。

    返回:
        错误列表；为空表示通过。
    """
    errors: list[str] = []
    if not isinstance(data, dict) or not data:
        return ["工作流必须是非空 JSON 对象（ComfyUI API 格式）"]
    for node_id, node in data.items():
        if not isinstance(node, dict) or not node.get("class_type") or not isinstance(node.get("inputs"), dict):
            errors.append(f"节点 {node_id} 缺少 class_type 或 inputs")
            continue
        for name, value in node["inputs"].items():
            if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str) and value[0].isdigit():
                if value[0] not in data:
                    errors.append(f"节点 {node_id}.{name} 连线指向不存在的节点 {value[0]}")
    return errors


def _required_placeholders(template_id: str, kind: str, config: AppConfig) -> list[str]:
    """取导入模板的必需占位符：清单有登记就用登记值，否则用类型默认值。

    参数:
        template_id: 模板 ID。
        kind: image / video。
        config: 本机配置。

    返回:
        占位符名列表。
    """
    try:
        manifest = comfy_media.load_manifest(comfy_media.templates_dir(config))
        entry = comfy_media.template_by_id(manifest, template_id)
        required = entry.get("required_placeholders") or entry.get("placeholders")
        if required:
            return list(required.keys()) if isinstance(required, dict) else [str(item) for item in required]
    except AdapterError:
        pass
    return list(_IMPORT_REQUIRED.get(kind, []))


def cmd_comfy_import(args, config: AppConfig) -> int:
    """`ndw comfy import`：把 UI/API 工作流导入为 API 格式模板并校验占位符。"""
    directory = comfy_media.templates_dir(config)
    target = directory / f"{args.id}.json"
    if target.exists() and not getattr(args, "force", False):
        raise PreconditionError(f"模板已存在：{target}；如需覆盖请加 --force")

    ui_path = getattr(args, "workflow", None)
    api_path = getattr(args, "file", None)
    if bool(ui_path) == bool(api_path):
        raise PreconditionError("--workflow（UI 格式）与 --file（API 格式）必须且只能指定一个")

    warnings: list[str] = []
    source_copy: Path | None = None
    if ui_path:
        origin = Path(ui_path).expanduser()
        if not origin.is_file():
            raise PreconditionError(f"UI 工作流文件不存在：{origin}")
        try:
            raw = json.loads(origin.read_text(encoding="utf-8"))
            prompt = convert_ui_workflow(
                raw,
                base_url=str(config.get("comfyui.base_url") or "http://127.0.0.1:8188"),
                warnings=warnings,
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise PreconditionError(f"UI 工作流读取/解析失败：{exc}") from exc
        except ComfyWorkflowError as exc:
            raise PreconditionError(f"UI 工作流转换失败：{exc}") from exc
        source_copy = origin
    else:
        origin = Path(api_path).expanduser()
        if not origin.is_file():
            raise PreconditionError(f"API 工作流文件不存在：{origin}")
        try:
            prompt = json.loads(origin.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PreconditionError(f"API 工作流读取/解析失败：{exc}") from exc

    errors = _validate_api_prompt(prompt)
    if errors:
        raise PreconditionError("工作流校验失败：" + "；".join(errors))

    text = json.dumps(prompt, ensure_ascii=False, indent=2)
    required = _required_placeholders(args.id, args.kind, config)
    missing = [name for name in required if f"{{{{{name}}}}}" not in text]
    if missing:
        raise PreconditionError(
            "模板缺少必需占位符：" + ", ".join("{{" + name + "}}" for name in missing)
            + "；请在导出的 API JSON 里把对应值改成占位符后重试"
        )

    directory.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.tmp")
    try:
        tmp.write_text(text + "\n", encoding="utf-8")
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)

    copied = None
    if source_copy is not None:
        source_dir = directory / "source"
        source_dir.mkdir(parents=True, exist_ok=True)
        copied = source_dir / source_copy.name
        shutil.copy2(source_copy, copied)

    lines = [f"✓ 已导入模板 {args.id}（{args.kind}）→ {target}"]
    if copied:
        lines.append(f"  原 UI 工作流已备份：{copied}")
    lines.extend(f"  {note}" for note in warnings)
    _emit(
        args,
        "\n".join(lines),
        {"ok": True, "template_id": args.id, "kind": args.kind, "target": str(target), "source": str(copied) if copied else None, "warnings": warnings},
    )
    return EXIT_OK


def cmd_comfy(args, config: AppConfig) -> int:
    """`ndw comfy` 入口：按 action 分发到各子命令。

    参数:
        args: 已解析参数（`action` 为二级子命令）。
        config: 本机配置。

    返回:
        退出码。
    """
    action = getattr(args, "action", None) or "health"
    if action in ("list", "templates"):
        return cmd_comfy_list(args, config)
    if action == "health":
        return cmd_comfy_health(args, config)
    if action == "scan":
        return cmd_comfy_scan(args, config)
    if action == "resolve":
        return cmd_comfy_resolve(args, config)
    if action == "models":
        return cmd_comfy_models(args, config)
    if action == "import":
        return cmd_comfy_import(args, config)
    _emit(args, f"✗ 未知子命令 comfy {action}", {"ok": False, "error": "unknown_action"})
    return EXIT_PRECONDITION
