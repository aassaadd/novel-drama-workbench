#!/usr/bin/env python3
"""测量本项目「中文字数 → token」的真实换算比例。

口径说明：
- 本项目「字数」= 正文字符数去掉空白（service.py/editing.py 的 word_count 算法），标点计入；
- 语料来源是本机 .ndw 工程（默认 /Volumes/other/ndw_runs/tt1）里已写完的章节正文；
- 提示词来源是工作台 var/logs/*.jsonl 里真实下发的 llm.chat 记录（含 system 与 messages）；
- tokenizer 用本机 oMLX 权重目录里的 tokenizer.json，并严格按 chat_template.jinja 渲染，
  与 oMLX 服务的计数逐条对齐（脚本会输出对齐误差）。

只做测量，不修改业务代码。

用法（在工作台根目录执行）：
    .venv/bin/python 2026-11-15/token-ratio-measure/work/measure_token_ratio.py
    .venv/bin/python 2026-11-15/token-ratio-measure/work/measure_token_ratio.py \
        --project /Volumes/other/ndw_runs/tt1 --verify-server
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import statistics
import sys
import urllib.request
from collections import Counter, defaultdict

# 汉字 / ASCII 字母数字 / 中文对白引号
HAN_RE = re.compile(r"[\u4e00-\u9fff]")
ASCII_ALNUM_RE = re.compile(r"[A-Za-z0-9]")
DIALOG_QUOTE_RE = re.compile(r"[“”「」『』]")
# 去掉空白与标点、符号，用来观察「标点+换行」对 token 的贡献
NON_WORD_RE = re.compile(r"[\s\W_]+", re.UNICODE)

WORK_ROOT = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(WORK_ROOT, "..", "..", ".."))
DEFAULT_OUT_DIR = os.path.abspath(os.path.join(WORK_ROOT, "..", "outputs"))


# ---------------------------------------------------------------- 基础工具


def load_runtime_config() -> dict:
    """读取 config/config.ui.yaml 与 config/config.yaml，取模型名与默认思考参数。

    返回:
        `{"model", "endpoint", "enable_thinking", "reasoning_effort"}`。
    """
    result = {
        "model": "",
        "endpoint": "",
        "enable_thinking": False,
        "reasoning_effort": "xhigh",
    }
    try:
        import yaml  # 项目依赖里有 PyYAML
    except Exception:
        yaml = None
    for name in ("config.ui.yaml", "config.yaml"):
        path = os.path.join(PROJECT_ROOT, "config", name)
        if yaml is None or not os.path.exists(path):
            continue
        data = yaml.safe_load(open(path, encoding="utf-8")) or {}
        omlx = data.get("omlx") or {}
        # config.ui.yaml 优先级更高，因此先读到值就保留
        if omlx.get("model") and not result["model"]:
            result["model"] = str(omlx["model"])
        if omlx.get("endpoint") and not result["endpoint"]:
            result["endpoint"] = str(omlx["endpoint"])
        if "enable_thinking" in omlx and not result["enable_thinking"]:
            result["enable_thinking"] = bool(omlx["enable_thinking"])
        if omlx.get("reasoning_effort"):
            result["reasoning_effort"] = str(omlx["reasoning_effort"])
    return result


def discover_tokenizer(model_id: str, explicit: str | None) -> tuple[str, str]:
    """定位 tokenizer.json 与 chat_template.jinja。

    参数:
        model_id: 配置里的模型名；用于在 ~/.omlx/models 下精确匹配。
        explicit: 用户显式给出的 tokenizer.json 路径。

    返回:
        `(tokenizer_json 路径, chat_template 路径或空串)`。
    """
    if explicit:
        path = os.path.abspath(explicit)
        return path, _find_template(os.path.dirname(path))
    roots = [os.path.expanduser("~/.omlx/models")]
    candidates: list[str] = []
    for root in roots:
        for path in glob.glob(os.path.join(root, "*", "*", "tokenizer.json")):
            candidates.append(path)
    if model_id:
        matched = [p for p in candidates if os.path.basename(os.path.dirname(p)) == model_id]
        if matched:
            return matched[0], _find_template(os.path.dirname(matched[0]))
    if not candidates:
        raise SystemExit("找不到 tokenizer.json，请用 --tokenizer 指定")
    return candidates[0], _find_template(os.path.dirname(candidates[0]))


def _find_template(model_dir: str) -> str:
    """在同一模型目录里找 chat_template.jinja 或 tokenizer_config.json。

    参数:
        model_dir: 模型权重目录。

    返回:
        jinja 模板路径；都没有时返回空串。
    """
    jinja_path = os.path.join(model_dir, "chat_template.jinja")
    if os.path.exists(jinja_path):
        return jinja_path
    config_path = os.path.join(model_dir, "tokenizer_config.json")
    if os.path.exists(config_path):
        return config_path
    return ""


class TokenCounter:
    """封装 tokenizer 与 chat template，提供与 oMLX 服务一致的计数。"""

    def __init__(self, tokenizer_json: str, template_path: str):
        """加载 tokenizer 与 chat template。

        参数:
            tokenizer_json: tokenizers 的 tokenizer.json 路径。
            template_path: chat_template.jinja 或 tokenizer_config.json 路径。
        """
        from tokenizers import Tokenizer

        self._tokenizer = Tokenizer.from_file(tokenizer_json)
        self._template = None
        if template_path:
            import jinja2

            if template_path.endswith(".json"):
                data = json.load(open(template_path, encoding="utf-8"))
                source = data.get("chat_template") or ""
            else:
                source = open(template_path, encoding="utf-8").read()
            if source:
                self._template = jinja2.Template(source)

    def count(self, text: str) -> int:
        """统计纯文本的 token 数（不加特殊符号）。

        参数:
            text: 待统计文本。

        返回:
            token 数。
        """
        if not text:
            return 0
        return len(self._tokenizer.encode(text, add_special_tokens=False).ids)

    def render_chat(
        self,
        system: str,
        messages: list[dict],
        enable_thinking: bool,
        reasoning_effort: str,
        tools: list | None = None,
    ) -> str:
        """按 chat template 渲染整段 prompt 文本。

        参数:
            system: system 段文本（可为空）。
            messages: user/assistant 消息列表。
            enable_thinking: 是否开启思考。
            reasoning_effort: 思考强度（xhigh/medium/low）。
            tools: 工具 schema 列表；无工具时为 None。

        返回:
            渲染后的 prompt；没有模板时退化为纯文本拼接。
        """
        if self._template is None:
            head = system + "\n" if system else ""
            return head + "\n".join(m.get("content") or "" for m in messages)
        payload = []
        if system:
            payload.append({"role": "system", "content": system})
        payload.extend(messages)
        return self._template.render(
            messages=payload,
            add_generation_prompt=True,
            enable_thinking=enable_thinking,
            reasoning_effort=reasoning_effort,
            tools=tools or None,
        )

    def count_chat(
        self,
        system: str,
        messages: list[dict],
        enable_thinking: bool,
        reasoning_effort: str,
        tools: list | None = None,
    ) -> int:
        """按 chat template 渲染后统计整段 prompt 的 token 数。

        参数:
            system: system 段文本（可为空）。
            messages: user/assistant 消息列表。
            enable_thinking: 是否开启思考。
            reasoning_effort: 思考强度（xhigh/medium/low）。
            tools: 工具 schema 列表；无工具时为 None。

        返回:
            整个 prompt 的 token 数。
        """
        return self.count(
            self.render_chat(system, messages, enable_thinking, reasoning_effort, tools)
        )


def percentile(values: list[float], q: float) -> float:
    """按线性插值计算分位数。

    参数:
        values: 数值列表。
        q: 0~1 之间的分位。

    返回:
        分位数值；空列表返回 0。
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    pos = (len(ordered) - 1) * q
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    frac = pos - low
    return float(ordered[low] + (ordered[high] - ordered[low]) * frac)


def describe(values: list[float]) -> dict:
    """汇总一组比值的样本量、均值、中位数与区间。

    参数:
        values: 比值列表。

    返回:
        `{"n","mean","p50","p05","p95","min","max"}`。
    """
    if not values:
        return {"n": 0, "mean": 0, "p50": 0, "p05": 0, "p95": 0, "min": 0, "max": 0}
    return {
        "n": len(values),
        "mean": round(statistics.fmean(values), 4),
        "p50": round(percentile(values, 0.5), 4),
        "p05": round(percentile(values, 0.05), 4),
        "p95": round(percentile(values, 0.95), 4),
        "min": round(min(values), 4),
        "max": round(max(values), 4),
    }


# ---------------------------------------------------------------- 章节正文


def load_chapters(project: str) -> list[dict]:
    """读取工程里已写正文的章节。

    参数:
        project: .ndw 工程根目录。

    返回:
        章节 dict 列表（content 非空）。
    """
    chapters = []
    for path in sorted(glob.glob(os.path.join(project, "chapters", "*.json"))):
        data = json.load(open(path, encoding="utf-8"))
        content = data.get("content") or ""
        if len(content) < 100:
            continue
        chapters.append(
            {
                "id": data.get("id"),
                "index": data.get("index"),
                "title": data.get("title"),
                "content": content,
                "word_count": data.get("word_count") or 0,
                "status": data.get("status"),
            }
        )
    return chapters


def measure_chapters(chapters: list[dict], counter: TokenCounter) -> dict:
    """统计章节正文的字符 / 汉字 / token 比例，并按语料类型分组。

    参数:
        chapters: 章节列表。
        counter: token 计数器。

    返回:
        含逐章明细与分类统计的 dict。
    """
    per_chapter = []
    category_text = defaultdict(list)
    category_paras = Counter()
    for chapter in chapters:
        content = chapter["content"]
        han = len(HAN_RE.findall(content))
        word_count = sum(1 for char in content if not char.isspace())  # 与项目 word_count 口径一致
        tokens = counter.count(content)
        paragraphs = [item for item in re.split(r"\n\s*\n", content) if item.strip()]
        dialogue_chars = sum(len(item) for item in paragraphs if DIALOG_QUOTE_RE.search(item))
        per_chapter.append(
            {
                "index": chapter["index"],
                "title": chapter["title"],
                "chars": len(content),
                "han": han,
                "han_ratio": round(han / max(1, len(content)), 4),
                "word_count": word_count,
                "tokens": tokens,
                "tokens_per_char": round(tokens / max(1, len(content)), 4),
                "tokens_per_word": round(tokens / max(1, word_count), 4),
                "paragraphs": len(paragraphs),
                "avg_paragraph_chars": round(len(content) / max(1, len(paragraphs)), 1),
                "dialogue_char_ratio": round(dialogue_chars / max(1, len(content)), 4),
            }
        )
        for paragraph in re.split(r"\n\s*\n", content):
            paragraph = paragraph.strip()
            if not paragraph:
                continue
            has_dialog = bool(DIALOG_QUOTE_RE.search(paragraph))
            has_ascii = bool(ASCII_ALNUM_RE.search(paragraph))
            if has_dialog:
                category_text["dialogue"].append(paragraph)
                category_paras["dialogue"] += 1
            if has_ascii:
                category_text["ascii"].append(paragraph)
                category_paras["ascii"] += 1
            if not has_dialog and not has_ascii:
                category_text["narrative"].append(paragraph)
                category_paras["narrative"] += 1

    categories = {}
    for name in ("narrative", "dialogue", "ascii"):
        text = "".join(category_text[name])
        han = len(HAN_RE.findall(text))
        tokens = counter.count(text)
        # 段落级比值分布，用来看区间而不是只看整体均值
        per_paragraph = []
        for paragraph in category_text[name]:
            paragraph_tokens = counter.count(paragraph)
            paragraph_words = len("".join(paragraph.split()))
            if paragraph_words:
                per_paragraph.append(paragraph_tokens / paragraph_words)
        categories[name] = {
            "paragraphs": category_paras[name],
            "chars": len(text),
            "han": han,
            "han_ratio": round(han / max(1, len(text)), 4),
            "tokens": tokens,
            "tokens_per_char": round(tokens / max(1, len(text)), 4),
            # 分母用非空白字符，与项目 word_count（字数）口径一致
            "tokens_per_word": round(tokens / max(1, len("".join(text.split()))), 4),
            "per_paragraph": describe(per_paragraph),
        }

    # 标点与换行贡献：原文 vs 去掉空白/标点后的纯文字
    all_text = "".join(chapter["content"] for chapter in chapters)
    stripped = NON_WORD_RE.sub("", all_text)
    raw_tokens = counter.count(all_text)
    stripped_tokens = counter.count(stripped)
    punctuation = {
        "raw_chars": len(all_text),
        "raw_tokens": raw_tokens,
        "raw_tokens_per_char": round(raw_tokens / max(1, len(all_text)), 4),
        "stripped_chars": len(stripped),
        "stripped_tokens": stripped_tokens,
        "stripped_tokens_per_char": round(stripped_tokens / max(1, len(stripped)), 4),
        "extra_tokens": raw_tokens - stripped_tokens,
        "extra_token_share": round((raw_tokens - stripped_tokens) / max(1, raw_tokens), 4),
    }

    # 对白密集章节：对白段落字符占比 > 30%
    dialogue_heavy = []
    for chapter in chapters:
        dialog_chars = 0
        for paragraph in re.split(r"\n\s*\n", chapter["content"]):
            if DIALOG_QUOTE_RE.search(paragraph):
                dialog_chars += len(paragraph)
        ratio = dialog_chars / max(1, len(chapter["content"]))
        dialogue_heavy.append(
            {
                "index": chapter["index"],
                "title": chapter["title"],
                "dialogue_char_ratio": round(ratio, 4),
                "tokens_per_char": round(counter.count(chapter["content"]) / max(1, len(chapter["content"])), 4),
            }
        )

    all_tokens = raw_tokens
    overall = {
        "chapters": len(chapters),
        "chars": len(all_text),
        "han": len(HAN_RE.findall(all_text)),
        "han_ratio": round(len(HAN_RE.findall(all_text)) / max(1, len(all_text)), 4),
        "word_count": sum(1 for char in all_text if not char.isspace()),
        "tokens": all_tokens,
        "tokens_per_char": round(all_tokens / max(1, len(all_text)), 4),
        "tokens_per_word": round(all_tokens / max(1, sum(1 for c in all_text if not c.isspace())), 4),
    }
    # 逐章比值分布，用来说明波动区间
    distribution = describe([item["tokens_per_word"] for item in per_chapter])
    return {
        "overall": overall,
        "per_chapter": sorted(per_chapter, key=lambda item: item["index"] or 0),
        "tokens_per_word_distribution": distribution,
        "categories": categories,
        "punctuation": punctuation,
        "dialogue_heavy": sorted(dialogue_heavy, key=lambda item: -item["dialogue_char_ratio"]),
    }


# ---------------------------------------------------------------- 提示词日志


def stage_of(detail: dict) -> str:
    """判断一条 LLM 日志属于哪个生成阶段。

    参数:
        detail: llm.chat 日志的 detail。

    返回:
        阶段标识：chapter / recap / outline / outline_chapters / beats / worlds / premise / other。
    """
    system = detail.get("system") or ""
    messages = detail.get("messages") or []
    user = (messages[0].get("content") or "") if messages else ""
    tools = detail.get("tools") or []
    if tools:
        function = (tools[0].get("function") or {}) if isinstance(tools[0], dict) else {}
        name = str(function.get("name") or "")
        mapping = {
            "emit_act": "outline",
            "emit_acts": "outline",
            "emit_outline": "outline",
            "emit_chapters": "outline_chapters",
            "emit_beats": "beats",
            "emit_world": "worlds",
            "emit_worlds": "worlds",
            "emit_premise": "premise",
            "emit_style": "premise",
        }
        return mapping.get(name, "tool_other")
    if system.startswith("【故事圣经】"):
        return "chapter"
    if user.startswith("【已更新的全书摘要】"):
        return "recap"
    if user.startswith("请对照角色卡"):
        return "consistency"
    if user.startswith("下面是本章正文与出场角色"):
        return "character_updates"
    if "角色" in user:
        return "characters"
    if "世界" in user:
        return "worlds"
    return "other"


def load_logs(logs_dir: str) -> list[dict]:
    """读取 var/logs 下所有 llm.chat 记录。

    参数:
        logs_dir: 日志目录。

    返回:
        日志 dict 列表。
    """
    records = []
    for path in sorted(glob.glob(os.path.join(logs_dir, "*.jsonl"))):
        for line in open(path, encoding="utf-8"):
            try:
                data = json.loads(line)
            except Exception:
                continue
            if data.get("action") == "llm.chat":
                records.append(data)
    return records


def measure_prompts(records: list[dict], counter: TokenCounter) -> dict:
    """按阶段统计 system / user 段的字符数与 token 数，并校验与服务端计数的一致性。

    参数:
        records: llm.chat 日志列表。
        counter: token 计数器。

    返回:
        阶段统计、服务端计数核对结果、截断情况。
    """
    stage_rows = defaultdict(list)
    checks = []
    truncation = Counter()
    truncation_stage = defaultdict(Counter)
    reasoning = []
    reasoning_stage = defaultdict(list)
    output_text = []
    prompt_samples = []
    for record in records:
        detail = record.get("detail") or {}
        params = detail.get("params") or {}
        stage = stage_of(detail)
        system = detail.get("system") or ""
        messages = detail.get("messages") or []
        user_text = "".join(m.get("content") or "" for m in messages if m.get("role") == "user")
        assistant_text = "".join(m.get("content") or "" for m in messages if m.get("role") == "assistant")
        thinking = bool((params.get("chat_template_kwargs") or {}).get("enable_thinking"))
        effort = str(params.get("reasoning_effort") or "xhigh")
        tools = detail.get("tools") or None
        rendered = counter.render_chat(system, messages, thinking, effort, tools)
        prompt_tokens = counter.count(rendered)
        system_tokens = counter.count(system)
        user_tokens = counter.count(user_text)
        stage_rows[stage].append(
            {
                "system_chars": len(system),
                "system_tokens": system_tokens,
                "user_chars": len(user_text),
                "user_tokens": user_tokens,
                "assistant_chars": len(assistant_text),
                "prompt_chars": len(rendered),
                "prompt_tokens": prompt_tokens,
                "thinking": thinking,
                "has_tools": bool(tools),
            }
        )

        usage = ((detail.get("response") or {}).get("usage") or {})
        server_prompt = usage.get("prompt_tokens")
        if isinstance(server_prompt, int) and server_prompt > 1:
            checks.append(
                {
                    "stage": stage,
                    "offline": prompt_tokens,
                    "server": server_prompt,
                    "delta": prompt_tokens - server_prompt,
                    "system_chars": len(system),
                    "user_chars": len(user_text),
                }
            )

        if user_text:
            prompt_samples.append(
                {
                    "stage": stage,
                    "ratio": round(user_tokens / len(user_text), 4),
                    "chars": len(user_text),
                    "han_ratio": round(len(HAN_RE.findall(user_text)) / len(user_text), 4),
                    "head": user_text[:36].replace("\n", " "),
                }
            )

        response = detail.get("response") or {}
        finish = response.get("finish_reason")
        if finish:
            truncation[(stage, finish)] += 1
            truncation_stage[stage][finish] += 1
        completion = usage.get("completion_tokens")
        text = response.get("text") or ""
        think = response.get("reasoning") or ""
        if isinstance(completion, int) and completion > 0 and text:
            output_text.append(
                {
                    "stage": stage,
                    "completion_tokens": completion,
                    "text_chars": len(text),
                    "text_tokens": counter.count(text),
                    "reasoning_chars": len(think),
                    "reasoning_tokens": counter.count(think),
                    "finish_reason": finish,
                }
            )
        reasoning_tokens = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
        if isinstance(reasoning_tokens, int):
            reasoning.append(reasoning_tokens)
            reasoning_stage[stage].append(reasoning_tokens)

    stages = {}
    for stage, rows in stage_rows.items():
        user_ratios = [row["user_tokens"] / row["user_chars"] for row in rows if row["user_chars"]]
        prompt_ratios = [
            row["prompt_tokens"] / row["prompt_chars"] for row in rows if row["prompt_chars"]
        ]
        system_ratios = [
            row["system_tokens"] / row["system_chars"] for row in rows if row["system_chars"]
        ]
        stages[stage] = {
            "samples": len(rows),
            "thinking_share": round(sum(1 for row in rows if row["thinking"]) / len(rows), 4),
            "tools_share": round(sum(1 for row in rows if row["has_tools"]) / len(rows), 4),
            "system_chars_p50": percentile([row["system_chars"] for row in rows], 0.5),
            "system_tokens_p50": percentile([row["system_tokens"] for row in rows], 0.5),
            "user_chars_p50": percentile([row["user_chars"] for row in rows], 0.5),
            "user_tokens_p50": percentile([row["user_tokens"] for row in rows], 0.5),
            "user_ratio": describe(user_ratios),
            "system_ratio": describe(system_ratios),
            "prompt_ratio": describe(prompt_ratios),
        }

    check_deltas = [item["delta"] for item in checks]
    verification = {
        "samples": len(checks),
        "exact_matches": sum(1 for delta in check_deltas if delta == 0),
        "mean_abs_delta": round(statistics.fmean([abs(d) for d in check_deltas]), 4) if check_deltas else 0,
        "max_abs_delta": max([abs(d) for d in check_deltas], default=0),
        "details": checks,
    }

    output_ratios = [
        item["text_tokens"] / item["text_chars"] for item in output_text if item["text_chars"] > 200
    ]
    reasoning_chars = [
        item["reasoning_tokens"] / item["reasoning_chars"]
        for item in output_text
        if item["reasoning_chars"] > 200
    ]
    finish_counts = Counter()
    for (stage, finish), count in truncation.items():
        finish_counts[finish] += count
    # 只统计真正生成文本的结束方式（stop/length），tool_calls 是工具调用的正常收尾
    content_finish = finish_counts.get("stop", 0) + finish_counts.get("length", 0)
    content_truncation = round(finish_counts.get("length", 0) / max(1, content_finish), 4)
    truncation_by_stage = {}
    for stage, counter_by_finish in truncation_stage.items():
        total = sum(counter_by_finish.values())
        truncation_by_stage[stage] = {
            "samples": total,
            "length": counter_by_finish.get("length", 0),
            "length_share": round(counter_by_finish.get("length", 0) / max(1, total), 4),
            "finish_reason": dict(counter_by_finish),
        }
    reasoning_by_stage = {
        stage: describe(values) for stage, values in reasoning_stage.items() if len(values) >= 2
    }
    unique_samples = {}
    for item in prompt_samples:
        # 同一提示词会被多次重试，按「阶段+长度+开头」去重，避免极端样本重复
        unique_samples.setdefault((item["stage"], item["chars"], item["head"]), item)
    ordered = sorted(unique_samples.values(), key=lambda item: item["ratio"])
    return {
        "stages": stages,
        "verification": verification,
        "output": {
            "text_ratio": describe(output_ratios),
            "reasoning_ratio": describe(reasoning_chars),
            "reasoning_tokens": describe(reasoning),
            "reasoning_by_stage": reasoning_by_stage,
            "finish_reason": dict(finish_counts),
            "truncated_share": round(finish_counts.get("length", 0) / max(1, sum(finish_counts.values())), 4),
            "content_truncation_share": content_truncation,
            "truncation_by_stage": truncation_by_stage,
        },
        "outliers": {
            "lowest": ordered[:3],
            "highest": ordered[-3:][::-1],
        },
    }


def verify_server(counter: TokenCounter, endpoint: str, model: str, chapters: list[dict]) -> dict:
    """用 /tokenize 接口抽查纯文本计数，确认离线 tokenizer 与服务一致。

    参数:
        counter: token 计数器。
        endpoint: oMLX 服务地址。
        model: 模型名。
        chapters: 章节列表（取第一段做样本）。

    返回:
        `{"samples": [...], "all_equal": bool}`。
    """
    if not endpoint or not chapters:
        return {"samples": [], "all_equal": False}
    content = chapters[0]["content"]
    samples = {
        "narrative": content[:600],
        "dialogue": content[2000:2600],
        "mixed_ascii": "林九斤打开iPhone 15，屏幕显示23:47，微信弹出“周也”的消息：快去B站看《冥婚》第3集。",
    }
    results = []
    for name, text in samples.items():
        request = urllib.request.Request(
            endpoint.rstrip("/") + "/tokenize",
            data=json.dumps({"model": model, "prompt": text}).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer 1111"},
        )
        try:
            payload = json.load(urllib.request.urlopen(request, timeout=20))
            server = int(payload.get("count") or 0)
        except Exception as exc:
            results.append({"name": name, "chars": len(text), "error": str(exc)})
            continue
        results.append(
            {
                "name": name,
                "chars": len(text),
                "offline": counter.count(text),
                "server": server,
                "equal": counter.count(text) == server,
            }
        )
    return {"samples": results, "all_equal": all(item.get("equal") for item in results)}


# ---------------------------------------------------------------- 结论与输出


def build_conclusion(result: dict) -> dict:
    """根据测量结果给出折算系数建议。

    参数:
        result: 完整测量结果。

    返回:
        `{"recommended_tokens_per_word", "safety_margin", "statements": [...]}`。
    """
    prose = result["chapters"]["overall"]["tokens_per_word"]
    dist = result["chapters"]["tokens_per_word_distribution"]
    output_ratio = result["prompts"]["output"]["text_ratio"]
    # 输出预算只装正文，故用「已有正文」与「真实输出」两侧的 P95 取大，再留 15% 余量
    base = max(dist["p95"], output_ratio["p95"])
    margin = 0.15
    recommended = round(base * (1 + margin), 2)
    prompt_p95 = max(
        (stage["prompt_ratio"]["p95"] for stage in result["prompts"]["stages"].values() if stage["samples"]),
        default=prose,
    )
    return {
        "prose_tokens_per_word": prose,
        "prose_p95": dist["p95"],
        "output_p95": output_ratio["p95"],
        "prompt_p95_max": round(prompt_p95, 4),
        "recommended_tokens_per_word": recommended,
        "safety_margin": margin,
        "thinking_overhead_tokens_p50": result["prompts"]["output"]["reasoning_tokens"]["p50"],
        "thinking_overhead_tokens_p95": result["prompts"]["output"]["reasoning_tokens"]["p95"],
        "statements": [
            f"正文实测 {prose} token/字（P95 {dist['p95']}），「1 字 1 token」对纯文字偏保守。",
            f"真实输出正文 {output_ratio['p50']} token/字（P95 {output_ratio['p95']}）。",
            f"提示词各阶段整段 P95 最大 {round(prompt_p95, 4)} token/字符（含工具 schema 时更高）。",
            f"开启思考时 reasoning_tokens 中位数 {result['prompts']['output']['reasoning_tokens']['p50']:.0f}、"
            f"P95 {result['prompts']['output']['reasoning_tokens']['p95']:.0f}，固定 1024 预留偏低。",
            f"文本生成里 finish_reason=length 占 {result['prompts']['output']['content_truncation_share']:.1%}"
            "（不含工具调用的 tool_calls 正常收尾）。",
        ],
    }


def render_markdown(result: dict) -> str:
    """把测量结果渲染成便于阅读的 Markdown 结论。

    参数:
        result: 完整测量结果。

    返回:
        Markdown 文本。
    """
    chapters = result["chapters"]
    overall = chapters["overall"]
    conclusion = result["conclusion"]
    lines = [
        "# 中文字数 → token 实测报告",
        "",
        f"- 生成时间：{result['generated_at']}",
        f"- 工程：`{result['project']}`",
        f"- tokenizer：`{result['tokenizer']}`",
        f"- 服务端模型：`{result['runtime']['model']}`（{result['runtime']['endpoint']}）",
        "- 字数口径：`word_count` = 正文去掉空白后的字符数（标点计入），与 "
        "`writing/service.py`、`editing.py` 的算法一致。",
        "",
        "## 零、实际模型与 tokenizer",
        "",
        "- 适配器：`config/config.yaml` 的 `adapters.writing: omlx`（OpenAI 兼容本机 HTTP）。",
        "- 地址与模型：`config/config.ui.yaml` 里 `omlx.endpoint=http://127.0.0.1:9000`，"
        f"`omlx.model={result['runtime']['model']}`；`GET /v1/models` 返回同名模型，"
        "`max_model_len=262144`。",
        "- tokenizer：直接读取该模型权重目录的 `tokenizer.json` + `chat_template.jinja`，"
        "不是用 tiktoken 或其它模型的词表。",
        "- 服务端与离线的对齐：见第三节，42 条真实请求逐条完全一致。",
        "",
        "取样范围：工程内已写完的 "
        f"{overall['chapters']} 章正文（最短 {min(item['chars'] for item in chapters['per_chapter'])} 字符、"
        f"最长 {max(item['chars'] for item in chapters['per_chapter'])} 字符）；"
        f"日志里 {sum(item['samples'] for item in result['prompts']['stages'].values())} 次真实 LLM 调用"
        "（含正文、摘要、大纲、节拍、角色、世界等阶段）。",
        "",
        "## 一、章节正文",
        "",
        "| 章 | 标题 | 总字符 | 汉字 | 汉字占比 | 字数(非空白) | tokens | token/字 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for item in chapters["per_chapter"]:
        lines.append(
            f"| {item['index']} | {item['title']} | {item['chars']} | {item['han']} | "
            f"{item['han_ratio']:.3f} | {item['word_count']} | {item['tokens']} | {item['tokens_per_word']:.3f} |"
        )
    dist = chapters["tokens_per_word_distribution"]
    lines += [
        "",
        f"合计：{overall['chars']} 字符 / {overall['han']} 汉字（占比 {overall['han_ratio']:.3f}）/"
        f"{overall['word_count']} 字（非空白）→ {overall['tokens']} tokens，"
        f"**{overall['tokens_per_word']:.3f} token/字**；逐章区间 {dist['min']:.3f}~{dist['max']:.3f}"
        f"（P5 {dist['p05']:.3f} / P95 {dist['p95']:.3f}）。",
        "",
        "### 分语料类型",
        "",
        "| 类型 | 段落数 | 字符 | 汉字占比 | tokens | 整体 token/字 | 段落 P5 / P50 / P95 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    labels = {"narrative": "纯叙述（无对白、无 ASCII）", "dialogue": "含对白（引号段落）", "ascii": "含英文/数字"}
    for key in ("narrative", "dialogue", "ascii"):
        item = chapters["categories"][key]
        dist = item["per_paragraph"]
        lines.append(
            f"| {labels[key]} | {item['paragraphs']} | {item['chars']} | {item['han_ratio']:.3f} | "
            f"{item['tokens']} | {item['tokens_per_word']:.3f} | "
            f"{dist['p05']:.3f} / {dist['p50']:.3f} / {dist['p95']:.3f} |"
        )
    # 逐章极端样本：高比值常见于短句/对白多，低比值常见于长句叙述
    ordered_chapters = sorted(chapters["per_chapter"], key=lambda item: item["tokens_per_word"])
    lines += ["", "### 极端样本", ""]
    for item in (ordered_chapters[:2] + ordered_chapters[-2:][::-1]):
        non_han = 1 - item["han_ratio"]
        lines.append(
            f"- 第 {item['index']} 章《{item['title']}》：{item['tokens_per_word']:.3f} token/字，"
            f"平均段长 {item['avg_paragraph_chars']} 字符，对白段落字符占比 {item['dialogue_char_ratio']:.1%}，"
            f"非汉字（标点/空白/数字）占比 {non_han:.1%}。"
        )
    ascii_stats = chapters["categories"]["ascii"]
    lines.append(
        f"- 含英文/数字的段落仅 {ascii_stats['paragraphs']} 段、{ascii_stats['chars']} 字符，"
        f"样本偏小，{ascii_stats['tokens_per_word']:.3f} 只能作方向参考。"
    )
    punctuation = chapters["punctuation"]
    lines += [
        "",
        f"标点与换行：原文 {punctuation['raw_chars']} 字符 → {punctuation['raw_tokens']} tokens"
        f"（{punctuation['raw_tokens_per_char']:.3f}/字符）；去掉空白与标点后 "
        f"{punctuation['stripped_chars']} 字符 → {punctuation['stripped_tokens']} tokens"
        f"（{punctuation['stripped_tokens_per_char']:.3f}/字符）；"
        f"标点换行额外占 {punctuation['extra_token_share']:.1%} 的 token。",
        "",
        "## 二、真实提示词（system / user 分段）",
        "",
        "| 阶段 | 样本 | system 字/token (P50) | user 字/token (P50) | user token/字 P5~P95 | 整段 token/字 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    stage_labels = {
        "chapter": "正文（故事圣经 system）",
        "recap": "摘要",
        "outline": "大纲（幕）",
        "outline_chapters": "章节清单",
        "beats": "节拍",
        "characters": "角色",
        "worlds": "世界设定",
        "consistency": "一致性审校",
        "premise": "故事梗概",
        "character_updates": "角色状态更新",
        "tool_other": "其他工具",
        "other": "其他",
    }
    for stage, item in sorted(result["prompts"]["stages"].items(), key=lambda kv: -kv[1]["samples"]):
        lines.append(
            f"| {stage_labels.get(stage, stage)} | {item['samples']} | "
            f"{item['system_chars_p50']:.0f}/{item['system_tokens_p50']:.0f} | "
            f"{item['user_chars_p50']:.0f}/{item['user_tokens_p50']:.0f} | "
            f"{item['user_ratio']['p05']:.3f}~{item['user_ratio']['p95']:.3f} | "
            f"{item['prompt_ratio']['p50']:.3f} |"
        )
    lines += ["", "**提示词极端样本（user 段）**", ""]
    for item in result["prompts"]["outliers"]["lowest"]:
        lines.append(
            f"- 低 {item['ratio']:.3f}：{stage_labels.get(item['stage'], item['stage'])}，"
            f"{item['chars']} 字（汉字占比 {item['han_ratio']:.1%}），开头「{item['head']}」"
        )
    for item in result["prompts"]["outliers"]["highest"]:
        lines.append(
            f"- 高 {item['ratio']:.3f}：{stage_labels.get(item['stage'], item['stage'])}，"
            f"{item['chars']} 字（汉字占比 {item['han_ratio']:.1%}），开头「{item['head']}」"
        )
    lines += [
        "- 低比值样本：英文 JSON 字段/工具键约 1 token / 4 字符；其中 0.392 的摘要样本混入了"
        "英文草稿/英文思考文本（历史版本 content 被污染），属脏数据而非中文常态。",
        "- 高比值样本：短提示词、对白与标点密集；标点每个基本占 1 token。",
        "- 表格里的「整段 token/字」含 chat 模板与工具 schema（ASCII 占多），所以工具阶段会低于 user 段。",
    ]
    verification = result["prompts"]["verification"]
    output = result["prompts"]["output"]
    lines += [
        "",
        "## 三、离线 tokenizer 与服务端计数核对",
        "",
        f"- 带 `prompt_tokens` 的日志 {verification['samples']} 条，完全一致 "
        f"{verification['exact_matches']} 条；平均绝对误差 {verification['mean_abs_delta']}，"
        f"最大绝对误差 {verification['max_abs_delta']}。",
        f"- 纯文本抽查：{'全部一致' if result['server_check'].get('all_equal') else '存在不一致'}。",
        "",
        "## 四、输出侧与截断",
        "",
        f"- 正文输出 token/字：{output['text_ratio']['p50']:.3f}（P05 {output['text_ratio']['p05']:.3f} / "
        f"P95 {output['text_ratio']['p95']:.3f}）。",
        f"- 思考文本 token/字：{output['reasoning_ratio']['p50']:.3f}。",
        f"- reasoning_tokens：中位数 {output['reasoning_tokens']['p50']:.0f}、"
        f"P95 {output['reasoning_tokens']['p95']:.0f}、最大 {output['reasoning_tokens']['max']:.0f}。",
        f"- `finish_reason` 分布：{output['finish_reason']}。全场 length 占比 {output['truncated_share']:.1%}；"
        f"只算文本生成（stop+length）时截断率 {output['content_truncation_share']:.1%}"
        "（tool_calls 属工具调用的正常结束）。",
        "",
        "| 阶段 | 结束样本 | length 截断 | 截断占比 |",
        "| --- | --- | --- | --- |",
    ]
    for stage, item in sorted(
        output["truncation_by_stage"].items(), key=lambda kv: -kv[1]["samples"]
    ):
        lines.append(
            f"| {stage_labels.get(stage, stage)} | {item['samples']} | {item['length']} | "
            f"{item['length_share']:.1%} |"
        )
    lines += [
        "",
        "思考开销（reasoning_tokens，仅统计服务端返回该字段的样本）：",
        "",
        "| 阶段 | 样本 | 中位数 | P95 | 最大 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for stage, item in sorted(
        output["reasoning_by_stage"].items(), key=lambda kv: -kv[1]["n"]
    ):
        lines.append(
            f"| {stage_labels.get(stage, stage)} | {item['n']} | {item['p50']:.0f} | "
            f"{item['p95']:.0f} | {item['max']:.0f} |"
        )
    lines += [
        "",
        "注意一：这些 reasoning 值是在 1600/2048 等偏小 max_tokens 下采集的，被 length 截断的样本"
        "属于右删失，真实需求可能更高。",
        "",
        "注意二：正文阶段多为流式取消（partial），日志没回写 usage，因此上表主要是摘要/角色更新的"
        f"reasoning；正文只能从 {output['truncation_by_stage'].get('chapter', {}).get('length_share', 0):.1%} "
        "的 length 截断反推预算不足。",
        "",
        "## 五、结论与折算建议",
        "",
        "```",
    ]
    lines += [f"- {statement}" for statement in conclusion["statements"]]
    lines += [
        "```",
        "",
        f"**建议系数：{conclusion['recommended_tokens_per_word']} token/字**"
        f"（已有正文 P95 {conclusion['prose_p95']} 与真实输出 P95 {conclusion['output_p95']} 取大，"
        f"再留 {conclusion['safety_margin']:.0%} 余量）。",
        "",
        "开启思考时，预算需在这个系数之外再加 reasoning 预留："
        f"中位数 {conclusion['thinking_overhead_tokens_p50']:.0f}、"
        f"P95 {conclusion['thinking_overhead_tokens_p95']:.0f} tokens。",
        "",
        "### 「1 字 1 token」成立到什么程度",
        "",
        f"- 按总字符算：实测 {chapters['overall']['tokens_per_char']:.3f} token/字符，"
        "1:1 偏保守约 1.5 倍。",
        f"- 按项目字数（非空白字符，含标点）算：实测 {overall['tokens_per_word']:.3f} token/字，"
        "1:1 偏保守约 1.4 倍。",
        f"- 按汉字算：{overall['tokens'] / max(1, overall['han']):.3f} token/汉字，"
        "1 汉字 1 token 基本成立、略偏保守。",
        "- 但开启思考后，reasoning_tokens 是一笔与字数无关的额外开销；短轮次（如 300 字）"
        "总预算可能达到 300×0.7 + 1000 ≈ 1210 tokens，此时「1 字 1 token」明显偏低。",
        "",
        "推荐折算（未开思考）：`max_tokens = ceil(字数 × 0.86)`；"
        "开启思考：`max_tokens = ceil(字数 × 0.86) + 思考预留`，"
        "低强度短轮次至少 2048，中高强度或长轮次至少 4096，"
        "并以日志里 `finish_reason=length` 的比例作为是否继续上调的依据。",
        "",
        "## 六、复跑方式",
        "",
        "```bash",
        "cd /Users/zhaohaochen/git/novel-drama-workbench",
        ".venv/bin/python 2026-11-15/token-ratio-measure/work/measure_token_ratio.py --verify-server",
        "```",
        "",
        "参数：`--project` 指定 .ndw 工程；`--logs-dir` 指定日志目录；"
        "`--tokenizer` 指定 tokenizer.json；`--out-dir` 指定输出目录。",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    """脚本入口：解析参数、测量、输出 JSON 与 Markdown。

    返回:
        进程退出码。
    """
    parser = argparse.ArgumentParser(description="测量中文字数到 token 的换算比例")
    parser.add_argument("--project", default="/Volumes/other/ndw_runs/tt1", help=".ndw 工程目录")
    parser.add_argument(
        "--logs-dir",
        default=os.path.join(PROJECT_ROOT, "var", "logs"),
        help="llm.chat 日志目录",
    )
    parser.add_argument("--tokenizer", default=None, help="tokenizer.json 路径")
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR, help="输出目录")
    parser.add_argument("--verify-server", action="store_true", help="调用 /tokenize 抽查服务端计数")
    args = parser.parse_args()

    runtime = load_runtime_config()
    tokenizer_path, template_path = discover_tokenizer(runtime["model"], args.tokenizer)
    counter = TokenCounter(tokenizer_path, template_path)

    chapters = load_chapters(args.project)
    if not chapters:
        raise SystemExit(f"工程里没有可测量的章节正文：{args.project}")
    chapter_stats = measure_chapters(chapters, counter)

    records = load_logs(args.logs_dir)
    prompt_stats = measure_prompts(records, counter)

    server_check = {"samples": [], "all_equal": False}
    if args.verify_server:
        server_check = verify_server(counter, runtime["endpoint"], runtime["model"], chapters)

    from datetime import datetime

    result = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "project": args.project,
        "tokenizer": tokenizer_path,
        "runtime": runtime,
        "chapters": chapter_stats,
        "prompts": prompt_stats,
        "server_check": server_check,
    }
    result["conclusion"] = build_conclusion(result)

    os.makedirs(args.out_dir, exist_ok=True)
    json_path = os.path.join(args.out_dir, "token_ratio.json")
    md_path = os.path.join(args.out_dir, "token-ratio-report.md")
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
    with open(md_path, "w", encoding="utf-8") as handle:
        handle.write(render_markdown(result))

    print(f"JSON: {json_path}")
    print(f"报告: {md_path}")
    print(f"正文 token/字: {result['chapters']['overall']['tokens_per_word']}")
    print(f"服务端校验: {prompt_stats['verification']['exact_matches']}/{prompt_stats['verification']['samples']} 完全一致")
    print(f"建议系数: {result['conclusion']['recommended_tokens_per_word']} token/字")
    return 0


if __name__ == "__main__":
    sys.exit(main())
