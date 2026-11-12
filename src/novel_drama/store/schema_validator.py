"""schema 校验：以 schema/ 下的 JSON Schema 为准，跨文件 $ref 自动解析。"""

from __future__ import annotations

import json
from pathlib import Path

try:  # 可选依赖：缺失时给出明确提示，不静默跳过
    from jsonschema import Draft202012Validator
except ImportError:  # pragma: no cover
    Draft202012Validator = None

SCHEMA_DIR = Path(__file__).resolve().parents[3] / "schema"


class SchemaValidationError(ValueError):
    """schema 校验失败，消息中带具体字段路径。"""


def _load_schemas(schema_dir: Path) -> dict[str, dict]:
    """读取目录下全部 schema 文件。

    参数:
        schema_dir: schema 目录。

    返回:
        {schema 文件名: schema 文档}。
    """
    return {path.name: json.loads(path.read_text(encoding="utf-8")) for path in schema_dir.glob("*.schema.json")}


def _build_registry(schemas: dict[str, dict]):
    """构建跨文件 $ref 解析用的 Registry；老版本 jsonschema 返回 None。

    参数:
        schemas: 全部 schema 文档。

    返回:
        referencing.Registry 或 None。
    """
    try:
        from referencing import Registry, Resource

        resources = [(item["$id"], Resource.from_contents(item)) for item in schemas.values() if "$id" in item]
        return Registry().with_resources(resources)
    except ImportError:  # pragma: no cover
        return None


def _chinese_message(error) -> str:
    """把常见的 jsonschema 英文错误翻成中文，便于页面直接展示。

    参数:
        error: jsonschema 的 ValidationError。

    返回:
        中文描述；无法识别时原样返回英文消息。
    """
    import re

    message = error.message
    if "Additional properties are not allowed" in message:
        names = re.findall(r"'([^']+)' was unexpected", message)
        if names:
            return "出现 schema 不允许的字段：" + "、".join(f"「{name}」" for name in names)
    required = re.match(r"'(.+?)' is a required property", message)
    if required:
        return f"缺少必填字段「{required.group(1)}」"
    wrong_type = re.match(r"(.+?) is not of type (.+)", message)
    if wrong_type:
        return f"字段 {wrong_type.group(1)} 类型不正确：应为 {wrong_type.group(2)}"
    return message


def validate(doc: dict, schema_name: str, schema_dir: Path | None = None) -> None:
    """校验一个实体文档。

    参数:
        doc: 待校验的 dict。
        schema_name: schema 文件名，如 "line.schema.json"。
        schema_dir: 覆盖默认 schema 目录（测试用）。

    返回:
        无。校验通过即正常返回。

    异常:
        SchemaValidationError: 校验失败，消息含首个错误的字段路径（中文说明）。
    """
    directory = schema_dir or SCHEMA_DIR
    schemas = _load_schemas(directory)
    schema = schemas.get(schema_name)
    if schema is None:
        raise SchemaValidationError(f"未找到 schema: {schema_name}")
    if Draft202012Validator is None:
        raise SchemaValidationError("缺少依赖 jsonschema，请先执行 pip install jsonschema")

    registry = _build_registry(schemas)
    validator = Draft202012Validator(schema, registry=registry) if registry is not None else Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(doc), key=lambda err: list(err.absolute_path))
    if errors:
        first = errors[0]
        location = "/".join(str(part) for part in first.absolute_path) or "<root>"
        chinese = _chinese_message(first)
        detail = chinese if chinese == first.message else f"{chinese}（原文：{first.message}）"
        raise SchemaValidationError(f"{schema_name} 校验失败 @ {location}: {detail}")
