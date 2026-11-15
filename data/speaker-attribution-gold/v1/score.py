#!/usr/bin/env python3
"""对白说话人归属金标评测脚本。

读取任意预测结果（JSONL），输出四项指标：
1. 非未知行准确率（只在金标为卡内角色的行上计算）
2. 整体准确率（全部金标行，一对一精确匹配）
3. unknown 精确率（预测为 unknown 的行里，金标确实是 unknown 的比例）
4. unknown 预测数（模型一共预测了多少行 unknown）

预测文件格式：每行一个 JSON，至少包含 id（或 source_uid）与说话人字段。
说话人字段可用 speaker / speaker_id / pred / prediction 之一，值可以是：
- 角色 ID（如 chr_linjiujin）
- 角色姓名或别名（如 九斤、苏姐、老板娘）
- 龙套写法 extra:司仪
- unknown / chr_unknown / 无法判定 / null / 空串

用法：
    python score.py --gold gold.jsonl --pred pred.jsonl
    python score.py --gold gold.jsonl --pred pred.jsonl --extra-as-unknown
    python score.py --gold gold.jsonl --pred pred.jsonl --json
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import sys

SPEAKER_KEYS = ("speaker", "speaker_id", "pred", "prediction", "gold_speaker")
UNKNOWN_VALUES = {"", "unknown", "chr_unknown", "无法判定", "none", "null", "-"}


def load_jsonl(path: str) -> list[dict]:
    """读取 JSONL 文件，跳过空行。

    参数:
        path: 文件路径。

    返回:
        记录列表。
    """
    rows = []
    with open(path, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"{path}:{lineno} 不是合法 JSON：{exc}") from exc
    return rows


def load_aliases(path: str) -> dict[str, str]:
    """从角色卡构建「姓名/别名 → 角色 ID」映射。

    参数:
        path: characters.eval.json 路径。

    返回:
        别名映射；文件不存在时返回空映射。
    """
    try:
        data = json.load(open(path, encoding="utf-8"))
    except FileNotFoundError:
        return {}
    aliases: dict[str, str] = {}
    for character in data.get("characters", []):
        cid = character["id"]
        for name in [character.get("name") or ""] + list(character.get("aliases") or []):
            if name:
                aliases[name] = cid
                aliases[cid] = cid
    return aliases


def normalize(value, aliases: dict[str, str]) -> str:
    """把预测值归一成金标使用的写法。

    参数:
        value: 预测值，可能是 ID、姓名、别名、unknown 或 None。
        aliases: 别名映射。

    返回:
        角色 ID / extra:名称 / unknown / missing / invalid:原值。
    """
    if value is None:
        return "unknown"
    text = str(value).strip()
    if text.lower() in UNKNOWN_VALUES:
        return "unknown"
    if text.startswith("extra:"):
        return text
    if text in aliases:
        return aliases[text]
    # 卡外姓名按龙套处理，名称仍需与金标一致才算对
    return f"extra:{text}"


def pick_prediction(record: dict) -> object:
    """从预测记录中取出说话人字段。

    参数:
        record: 单行预测。

    返回:
        原始说话人值；不存在时返回 None。
    """
    for key in SPEAKER_KEYS:
        if key in record:
            return record[key]
    return None


def ratio(hit: int, total: int) -> float | None:
    """计算比例，分母为 0 时返回 None。

    参数:
        hit: 命中数。
        total: 总数。

    返回:
        0-1 的比例或 None。
    """
    return hit / total if total else None


def fmt(value: float | None) -> str:
    """把比例格式化成百分比文本。

    参数:
        value: 比例或 None。

    返回:
        百分比字符串；None 显示为 n/a。
    """
    return "n/a" if value is None else f"{value * 100:.1f}%"


def main() -> None:
    """解析参数、对齐预测、计算并打印指标。"""
    parser = argparse.ArgumentParser(description="对白说话人归属金标评测")
    parser.add_argument("--gold", required=True, help="金标 JSONL 路径")
    parser.add_argument("--pred", required=True, help="预测结果 JSONL 路径")
    parser.add_argument(
        "--characters",
        default=None,
        help="角色卡路径，默认取 gold 同目录的 characters.eval.json",
    )
    parser.add_argument(
        "--extra-as-unknown",
        action="store_true",
        help="把龙套金标视为 unknown 计分（适合只输出卡内角色的模型）",
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    args = parser.parse_args()

    gold_rows = load_jsonl(args.gold)
    if not gold_rows:
        raise SystemExit("金标为空")
    card_path = args.characters or os.path.join(os.path.dirname(os.path.abspath(args.gold)), "characters.eval.json")
    aliases = load_aliases(card_path)
    card_ids = set(aliases.values())

    predictions: dict[str, str] = {}
    duplicates = 0
    for record in load_jsonl(args.pred):
        key = record.get("id") or record.get("source_uid") or record.get("uid")
        if not key:
            continue
        if key in predictions:
            duplicates += 1
        predictions[key] = normalize(pick_prediction(record), aliases)

    gold_ids = set()
    for row in gold_rows:
        gold_ids.add(row["id"])
        if row.get("source_uid"):
            gold_ids.add(row["source_uid"])

    stats = {
        "total": 0,
        "coverage": 0,
        "card_total": 0,
        "card_hit": 0,
        "overall_hit": 0,
        "unknown_pred": 0,
        "unknown_hit": 0,
        "gold_unknown": 0,
        "extra_total": 0,
        "extra_hit": 0,
        "missing": 0,
        "invalid": 0,
        "per_stratum": collections.defaultdict(lambda: {"total": 0, "hit": 0}),
        "per_evidence": collections.defaultdict(lambda: {"total": 0, "hit": 0}),
        "unmatched_predictions": 0,
    }

    for row in gold_rows:
        gold = row["gold_speaker"]
        key = row["id"] if row["id"] in predictions else row.get("source_uid")
        pred = predictions.get(key) if key else None
        if pred is None:
            pred = "missing"
            stats["missing"] += 1
        elif pred.startswith("invalid:"):
            stats["invalid"] += 1
        else:
            stats["coverage"] += 1

        gold_for_score = gold
        if args.extra_as_unknown and gold.startswith("extra:"):
            gold_for_score = "unknown"
        correct = pred == gold_for_score

        stats["total"] += 1
        stats["overall_hit"] += int(correct)
        stats["per_stratum"][row["stratum"]]["total"] += 1
        stats["per_stratum"][row["stratum"]]["hit"] += int(correct)
        stats["per_evidence"][row["evidence"]]["total"] += 1
        stats["per_evidence"][row["evidence"]]["hit"] += int(correct)

        if gold in card_ids or (not card_ids and gold not in ("unknown",) and not gold.startswith("extra:")):
            stats["card_total"] += 1
            stats["card_hit"] += int(correct)
        if gold.startswith("extra:"):
            stats["extra_total"] += 1
            stats["extra_hit"] += int(correct)
        if gold == "unknown":
            stats["gold_unknown"] += 1
        if pred == "unknown":
            stats["unknown_pred"] += 1
            stats["unknown_hit"] += int(gold == "unknown")

    stats["unmatched_predictions"] = len(set(predictions) - gold_ids)
    result = {
        "total": stats["total"],
        "coverage": stats["coverage"],
        "non_unknown_accuracy": ratio(stats["card_hit"], stats["card_total"]),
        "non_unknown_correct": stats["card_hit"],
        "non_unknown_total": stats["card_total"],
        "overall_accuracy": ratio(stats["overall_hit"], stats["total"]),
        "overall_correct": stats["overall_hit"],
        "unknown_precision": ratio(stats["unknown_hit"], stats["unknown_pred"]),
        "unknown_correct": stats["unknown_hit"],
        "unknown_predictions": stats["unknown_pred"],
        "unknown_recall": ratio(stats["unknown_hit"], stats["gold_unknown"]),
        "gold_unknown": stats["gold_unknown"],
        "extra_accuracy": ratio(stats["extra_hit"], stats["extra_total"]),
        "extra_correct": stats["extra_hit"],
        "extra_total": stats["extra_total"],
        "missing": stats["missing"],
        "invalid": stats["invalid"],
        "duplicates": duplicates,
        "unmatched_predictions": stats["unmatched_predictions"],
        "extra_as_unknown": args.extra_as_unknown,
        "per_stratum": {
            key: {"total": value["total"], "correct": value["hit"], "accuracy": ratio(value["hit"], value["total"])}
            for key, value in sorted(stats["per_stratum"].items())
        },
        "per_evidence": {
            key: {"total": value["total"], "correct": value["hit"], "accuracy": ratio(value["hit"], value["total"])}
            for key, value in sorted(stats["per_evidence"].items())
        },
    }

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    print(f"样本数: {result['total']}")
    print(f"覆盖率: {result['coverage']}/{result['total']} (缺失 {result['missing']}，非法 {result['invalid']})")
    print(f"非未知行准确率: {fmt(result['non_unknown_accuracy'])} ({result['non_unknown_correct']}/{result['non_unknown_total']})")
    print(f"整体准确率: {fmt(result['overall_accuracy'])} ({result['overall_correct']}/{result['total']})")
    print(f"unknown 精确率: {fmt(result['unknown_precision'])} ({result['unknown_correct']}/{result['unknown_predictions']})")
    print(f"unknown 预测数: {result['unknown_predictions']}")
    print(f"unknown 召回率: {fmt(result['unknown_recall'])} (金标 unknown {result['gold_unknown']} 条)")
    print(f"龙套行准确率: {fmt(result['extra_accuracy'])} ({result['extra_correct']}/{result['extra_total']})")
    if result["duplicates"] or result["unmatched_predictions"]:
        print(f"提示: 重复预测 {result['duplicates']} 条，无法对齐到金标的预测 {result['unmatched_predictions']} 条")
    print("\n按困难类型：")
    for key, value in result["per_stratum"].items():
        print(f"  {key:20s} {value['correct']:4d}/{value['total']:<4d} {fmt(value['accuracy'])}")
    print("\n按依据类型：")
    for key, value in result["per_evidence"].items():
        print(f"  {key:20s} {value['correct']:4d}/{value['total']:<4d} {fmt(value['accuracy'])}")


if __name__ == "__main__":
    sys.exit(main())
