"""`ndw models`：模型清单、校验与导入（不下载）。

职责边界：
- 只管本机已有模型：列清单、验完整性、把已有目录纳入模型根目录；
- 下载器（`models install`）另做，本模块不联网；
- 输出里的路径一律来自 `paths.models_root`，与 provider 解析同源。
"""

from __future__ import annotations

from .cli_common import EXIT_FAILED, EXIT_OK, EXIT_PRECONDITION, EXIT_USAGE, PreconditionError, _emit
from .config import AppConfig
from .tts import model_store


def _human_size(num: int) -> str:
    """字节数转人读体积。

    参数:
        num: 字节数。

    返回:
        形如 `1.3G` 的字符串。
    """
    size = float(num)
    for unit in ("B", "K", "M", "G", "T"):
        if size < 1024 or unit == "T":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}T"


def cmd_models_list(args, config: AppConfig) -> int:
    """列出模型清单与就位状态。

    参数:
        args: 已解析参数（`--json`、`--present`）。
        config: 本机配置。
    """
    rows = model_store.scan(config)
    if getattr(args, "present", False):
        rows = [row for row in rows if row["present"]]
    root = str(model_store.models_root(config))
    if getattr(args, "json", False):
        _emit(args, payload={"root": root, "models": rows})
        return EXIT_OK
    print(f"模型根目录 {root}")
    print(f"{'目录键':<20}{'就位':<6}{'完整':<6}{'体积':<10}{'许可':<18}{'商用':<6}provider")
    for row in rows:
        print(
            f"{row['key']:<20}"
            f"{'是' if row['present'] else '否':<6}"
            f"{'是' if row['complete'] else '否':<6}"
            f"{_human_size(row['bytes']):<10}"
            f"{row['license']:<18}"
            f"{'是' if row['commercial_use'] else '否':<6}"
            f"{row['provider_id']}"
        )
    ready = [row for row in rows if row["complete"]]
    print(f"\n已就位 {len(ready)} 项，未下载/不完整 {len(rows) - len(ready)} 项")
    return EXIT_OK


def cmd_models_verify(args, config: AppConfig) -> int:
    """校验模型文件完整性（可选真加载）。

    参数:
        args: 已解析参数（`--json`、`--key`、`--load`）。
        config: 本机配置。

    返回:
        0 全部通过；5 有缺失/加载失败。
    """
    keys = list(getattr(args, "key", None) or []) or None
    result = model_store.verify(config, keys=keys, load=bool(getattr(args, "load", False)))
    if getattr(args, "json", False):
        _emit(args, payload=result)
        return EXIT_OK if result["ok"] else EXIT_FAILED
    print(f"模型根目录 {result['root']}")
    for item in result["items"]:
        if not item["present"]:
            print(f"- {item['key']:<20} 未下载（{item['path']}）")
            continue
        state = "完整" if item["complete"] else f"缺失：{', '.join(item['missing'])}"
        load = item.get("load")
        extra = ""
        if load:
            extra = f"｜加载：{'成功' if load['ok'] else load['detail']}（{load['seconds']}s）"
        print(f"- {item['key']:<20} {state}｜{_human_size(item['bytes'])}｜{item['path']}{extra}")
    print("校验通过" if result["ok"] else "校验未通过")
    return EXIT_OK if result["ok"] else EXIT_FAILED


def cmd_models_install(args, config: AppConfig) -> int:
    """从 hf-mirror 下载模型：先报体积/许可，再落暂存校验后 mv 就位。

    参数:
        args: 已解析参数（`--key`、`--yes`、`--accept-license`、`--dry-run`、`--json`）。
        config: 本机配置。

    返回:
        0 已就位/已存在/仅计划；4 需先确认（体积或许可）；5 下载或校验失败；2 参数错误。
    """
    key = getattr(args, "key", None)
    if not key:
        raise PreconditionError("缺少参数：ndw models install --key <目录键>（如 cosyvoice3/8bit）")
    result = model_store.install(
        config,
        key,
        yes=bool(getattr(args, "yes", False)),
        accept_license=bool(getattr(args, "accept_license", False)),
        dry_run=bool(getattr(args, "dry_run", False)),
    )
    status = result["status"]
    if getattr(args, "json", False):
        _emit(args, payload=result)
    else:
        _print_install(result)
    if status in ("installed", "already", "plan"):
        return EXIT_OK
    if status in ("need_confirm", "need_license"):
        return EXIT_PRECONDITION
    if status == "unknown_key":
        return EXIT_USAGE
    return EXIT_FAILED


def _print_install(result: dict) -> None:
    """打印下载结果（体积、许可、缺文件、下一步提示）。

    参数:
        result: `model_store.install` 的返回值。
    """
    status = result["status"]
    name = result.get("display_name") or result.get("key")
    if status == "unknown_key":
        print(f"未知模型目录键。可选：{', '.join(result['available'])}")
        return
    if status == "retired":
        print(result["message"])
        return
    if status == "already":
        print(f"{name} 已就位（{_human_size(result['bytes'])}）：{result['path']}，不覆盖")
        return
    if status == "target_incomplete":
        print(f"目标目录已存在但不完整，不覆盖：{result['path']}")
        print(f"  缺少：{', '.join(result['missing'])}。确认后可先删除该目录再重试")
        return
    if status == "need_license":
        print(f"{name} 的许可为 {result['license']}，商用许可：{'是' if result['commercial_use'] else '否'}")
        if result.get("note"):
            print(f"  说明：{result['note']}")
        print("确认理解许可限制后，加 --accept-license 再执行")
        return
    if status in ("plan", "need_confirm", "installed", "incomplete", "error"):
        if result.get("repo"):
            print(f"仓库 {result['repo']}（镜像 {model_store.HF_MIRROR}）")
        if result.get("total_bytes") is not None:
            total = _human_size(result["total_bytes"])
            extra = "，含未知体积文件" if result.get("unknown") else ""
            print(f"体积 {total}{extra}（超过 15G 需 --yes 确认）")
            for item in result.get("files") or []:
                print(f"  {_human_size(item['bytes']):>8}  {item['name']}")
        if result.get("staging"):
            print(f"暂存 {result['staging']} → 目标 {result['target']}")
    if status == "need_confirm":
        print("体积超过阈值或未知：先确认，加 --yes 再执行（不会直接写目标目录）")
    elif status == "plan":
        print("仅计划：未下载。去掉 --dry-run 即开始下载")
    elif status == "installed":
        print(f"已就位（{_human_size(result['bytes'])}）：{result['path']}，完整性校验通过")
    elif status == "incomplete":
        print(f"校验未通过，缺少：{', '.join(result.get('missing') or [])}")
        if result.get("staging"):
            print(f"  暂存目录保留，可重跑续传：{result['staging']}")
    elif status == "error":
        print(f"下载失败：{result.get('error')}")


def cmd_models_import(args, config: AppConfig) -> int:
    """把已有权重目录纳入模型根目录（同卷 rename，跨卷暂存后 rename）。

    参数:
        args: 已解析参数（`--from`、`--key`、`--keep-source`、`--json`）。
        config: 本机配置。

    返回:
        0 成功；2 参数不合法；5 迁移失败。
    """
    src = getattr(args, "src", None)
    key = getattr(args, "key", None)
    if not src or not key:
        raise PreconditionError("缺少参数：ndw models import --from <目录> --key <目录键>（如 cosyvoice3/8bit）")
    try:
        result = model_store.import_dir(config, src, key, keep_source=bool(getattr(args, "keep_source", False)))
    except ValueError as exc:
        payload = {"ok": False, "error": str(exc)}
        if getattr(args, "json", False):
            _emit(args, payload=payload)
        else:
            print(f"导入失败：{exc}")
        return EXIT_USAGE
    if getattr(args, "json", False):
        _emit(args, payload={"ok": True, **result})
        return EXIT_OK
    mode = "同卷 rename" if result["same_volume"] else "跨卷暂存后 rename"
    print(f"已导入 {result['key']}（{mode}，{_human_size(result['bytes'])}）")
    print(f"  源 {result['src']} → 目标 {result['target']}")
    print(f"  源目录{'已删除' if result['source_removed'] else '保留（--keep-source）'}")
    return EXIT_OK
