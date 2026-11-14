#!/usr/bin/env python3
"""枚举字段审计脚本（真实浏览器）。

目的：
1. 遍历主要视图与弹窗，确认每个枚举字段的编辑控件都是真正的下拉（非自由输入）；
2. 统计枚举字段数、已改下拉数、缺失数（缺失必须为 0）；
3. 检查展示态不出现 novella / male_mid 这类裸值；
4. 实际保存一次「目标篇幅 + 目标总字数 + 视角 + 时态」，刷新后核对是否生效，然后还原。

用法：
    .venv/bin/python scripts/audit_enum_fields.py
环境变量：
    NDW_BASE     服务地址，默认 http://127.0.0.1:8790
    NDW_PROJECT  测试工程路径，默认 /tmp/ndw-proj
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = os.environ.get("NDW_BASE", "http://127.0.0.1:8790").rstrip("/")
PROJECT = os.environ.get("NDW_PROJECT", "/tmp/ndw-proj")
SHOTS = Path("docs/screenshots")
SHOTS.mkdir(parents=True, exist_ok=True)

#: 明确属于内部枚举值的裸串（展示态出现即算缺失）
RAW_TOKENS = [
    "novella",
    "third_limited",
    "third_omniscient",
    "inner_monologue",
    "per_scene",
    "male_mid",
    "female_mid",
    "music_sting",
    "slow_push_in",
    "candidates_ready",
    "asset_ready",
]
RAW_RE = re.compile(r"(?<![A-Za-z0-9_])(" + "|".join(RAW_TOKENS) + r")(?![A-Za-z0-9_])")

#: 历史实现里曾用自由输入框承载枚举的控件 id（现在必须不存在或已是 select）
LEGACY_FREE_IDS = [
    "ns-length",
    "ns-pov",
    "ns-tense",
    "ch-role",
    "ch-gender",
    "ch-age",
    "nc-role",
    "le-emotion",
    "bgm-mode",
]

#: 页面内采集脚本：返回 data-enum 控件、旧式自由输入、可见文本
COLLECT_JS = """
() => {
  const controls = [...document.querySelectorAll('[data-enum]')].map((node) => ({
    key: node.dataset.enum,
    tag: node.tagName.toLowerCase(),
    id: node.id || '',
    selected: (node.selectedOptions && node.selectedOptions[0]
      ? node.selectedOptions[0].textContent
      : node.textContent || '').trim(),
  }));
  const legacy = %s
    .map((id) => document.getElementById(id))
    .filter((node) => node && node.tagName !== 'SELECT')
    .map((node) => `${node.tagName.toLowerCase()}#${node.id}`);
  const filters = [...document.querySelectorAll('[data-gender],[data-role]')].map((node) => ({
    key: node.dataset.gender ? 'gender' : 'role',
    tag: node.tagName.toLowerCase(),
    text: (node.textContent || '').trim(),
  }));
  return { controls, legacy, filters, text: document.body.innerText || '' };
}
""" % json.dumps(LEGACY_FREE_IDS)


def visit(page, name: str, hash_path: str, wait_for: str, extra=None, shot: str = ""):
    """打开一个视图，可选执行额外交互，采集枚举控件与裸值。
    
    参数:
        page: Playwright 页面。
        name: 视图名称（报告用）。
        hash_path: 路由 hash。
        wait_for: 等待出现的选择器。
        extra: 额外交互函数（接收 page）。
        shot: 截图文件名（空则不截图）。
    返回:
        采集结果 dict。
    """
    page.goto(f"{BASE}/#{hash_path}", wait_until="domcontentloaded")
    page.wait_for_selector(wait_for, timeout=15000)
    page.wait_for_timeout(700)
    if extra:
        extra(page)
        page.wait_for_timeout(500)
    data = page.evaluate(COLLECT_JS)
    data["name"] = name
    data["raw_hits"] = sorted(set(RAW_RE.findall(data["text"])))
    if shot:
        page.screenshot(path=str(SHOTS / shot), full_page=True)
    return data


def open_new_character_dialog(page):
    """打开「新增角色」弹窗，验证弹窗内枚举同样是下拉。"""
    page.click('[data-act="new-character"]')
    page.wait_for_selector("#nc-role", timeout=8000)


def open_line_editor(page):
    """展开第一条台词的行内编辑，验证情绪为下拉。"""
    page.click(".seg-row")
    page.wait_for_selector("#le-emotion", timeout=8000)


def open_bgm_tab(page):
    """切到 BGM 页签，验证用途下拉。"""
    page.click('[data-tab="bgm"]')
    page.wait_for_selector("#bgm-mode", timeout=8000)


def close_dialog(page):
    """关闭当前弹窗（用遮罩上的关闭按钮）。"""
    try:
        page.click("[data-dlg-close]", timeout=3000)
        page.wait_for_timeout(250)
    except Exception:
        page.keyboard.press("Escape")


def verify_save(page, results):
    """在小说视图实际保存枚举与数字字段，刷新后核对，再还原。"""
    page.goto(f"{BASE}/#/p/{PROJECT.replace('/', '%2F')}/novel", wait_until="domcontentloaded")
    page.wait_for_selector("#ns-length", timeout=15000)
    page.wait_for_timeout(700)
    before = page.evaluate(
        """() => ({
          length: document.getElementById('ns-length').value,
          words: document.getElementById('ns-words').value,
          pov: document.getElementById('ns-pov').value,
          tense: document.getElementById('ns-tense').value,
        })"""
    )
    page.select_option("#ns-length", "series")
    page.fill("#ns-words", "150000")
    page.click('[data-act="novel-setting-save"][data-section="premise"]')
    page.wait_for_timeout(1200)
    # 保存某个分区会重画整页，风格分区必须在重画之后再改
    page.select_option("#ns-pov", "third_limited")
    page.select_option("#ns-tense", "present")
    page.click('[data-act="novel-setting-save"][data-section="style"]')
    page.wait_for_timeout(1200)
    page.reload(wait_until="domcontentloaded")
    page.wait_for_selector("#ns-length", timeout=15000)
    page.wait_for_timeout(700)
    after = page.evaluate(
        """() => ({
          length: document.getElementById('ns-length').value,
          words: document.getElementById('ns-words').value,
          pov: document.getElementById('ns-pov').value,
          tense: document.getElementById('ns-tense').value,
        })"""
    )
    ok = after["length"] == "series" and after["words"] == "150000" and after["pov"] == "third_limited" and after["tense"] == "present"
    results.append(
        {
            "name": "保存生效（目标篇幅/总字数/视角/时态）",
            "before": before,
            "after": after,
            "ok": ok,
        }
    )
    # 还原，避免污染测试工程
    page.select_option("#ns-length", before["length"] or "novella")
    page.fill("#ns-words", before["words"])
    page.click('[data-act="novel-setting-save"][data-section="premise"]')
    page.wait_for_timeout(900)
    page.select_option("#ns-pov", before["pov"] or "third_limited")
    page.select_option("#ns-tense", before["tense"] or "past")
    page.click('[data-act="novel-setting-save"][data-section="style"]')
    page.wait_for_timeout(900)


def first_chapter() -> str:
    """从接口取测试工程的第一章，避免把章节 ID 写死在脚本里。"""
    url = f"{BASE}/api/ui/chapters?path={urllib.parse.quote(PROJECT)}"
    with urllib.request.urlopen(url, timeout=10) as response:
        chapters = json.load(response)
    if not chapters:
        raise SystemExit(f"测试工程没有章节：{PROJECT}")
    return chapters[0]["id"]


def main() -> int:
    """执行审计并输出统计，缺失数非 0 时返回 1。"""
    encoded = PROJECT.replace("/", "%2F")
    chapter = first_chapter()
    views = [
        ("工程列表", "/projects", ".page-title, .pc-title", None, "20261114-enum-projects.png"),
        ("小说 / 写作设定", f"p/{encoded}/novel", "#ns-length", None, "20261114-enum-novel.png"),
        ("角色档案", f"p/{encoded}/characters", "#ch-age", None, "20261114-enum-characters.png"),
        ("新增角色弹窗", f"p/{encoded}/characters", "#ch-age", open_new_character_dialog, ""),
        ("画本 / 行内编辑", f"p/{encoded}/c/{chapter}", ".seg-row", open_line_editor, "20261114-enum-editor.png"),
        ("音色", f"p/{encoded}/voices", ".ctx", None, ""),
        ("素材 / BGM", f"p/{encoded}/media", "[data-tab]", open_bgm_tab, ""),
        ("任务", f"p/{encoded}/tasks", ".page-title, .card-head", None, ""),
        ("全局设置 / 连接配置", "/settings?section=conn", ".rail-link", None, ""),
        ("工程设置", f"p/{encoded}/settings", "#set-genres", None, "20261114-enum-project-settings.png"),
        ("导出", f"p/{encoded}/export/{chapter}", ".export-page", None, ""),
    ]

    reports = []
    results = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport={"width": 1500, "height": 980})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        for name, hash_path, wait_for, extra, shot in views:
            try:
                reports.append(visit(page, name, hash_path, wait_for, extra, shot))
            except Exception as error:  # 单个视图失败不中断其余视图
                reports.append({"name": name, "error": str(error), "controls": [], "legacy": [], "raw_hits": []})
            finally:
                if extra is open_new_character_dialog:
                    close_dialog(page)
        verify_save(page, results)
        browser.close()

    total_fields = 0
    total_selects = 0
    total_filters = 0
    missing = []
    keys = set()
    cjk = re.compile(r"[\u4e00-\u9fff]")
    for item in reports:
        if item.get("error"):
            missing.append((item["name"], "视图打开失败", item["error"]))
            continue
        for control in item["controls"]:
            total_fields += 1
            keys.add(control["key"])
            if control["tag"] == "select":
                total_selects += 1
                if not control["selected"] or re.fullmatch(r"[a-z_]+", control["selected"]):
                    missing.append((item["name"], f"下拉 {control['key']}", f"选中项不是中文：{control['selected']}"))
            else:
                missing.append((item["name"], f"控件 {control['key']}", f"不是下拉：<{control['tag']}>"))
        # 筛选器必须是有中文标签的分段按钮，不能是自由输入
        for chip in item.get("filters", []):
            total_filters += 1
            if chip["tag"] != "button" or not cjk.search(chip["text"]):
                missing.append((item["name"], f"筛选器 {chip['key']}", f"不是中文分段按钮：<{chip['tag']}>{chip['text']}"))
        for legacy in item["legacy"]:
            missing.append((item["name"], legacy, "枚举字段仍是自由输入"))
        for token in item["raw_hits"]:
            missing.append((item["name"], "展示裸值", token))
    for result in results:
        if not result["ok"]:
            missing.append((result["name"], "保存未生效", json.dumps(result["after"], ensure_ascii=False)))

    print("视图                     枚举控件  下拉  裸值")
    for item in reports:
        if item.get("error"):
            print(f"{item['name']:<22} 打开失败：{item['error']}")
            continue
        print(f"{item['name']:<22} {len(item['controls']):>6}  {len([c for c in item['controls'] if c['tag'] == 'select']):>4}  {len(item['raw_hits']):>3}")
    print()
    print(f"枚举字段数：{total_fields}")
    print(f"已改下拉数：{total_selects}")
    print(f"枚举筛选器：{total_filters}（分段按钮 + 中文标签）")
    print(f"缺失数：{len(missing)}（必须为 0）")
    print(f"覆盖枚举键：{', '.join(sorted(keys))}")
    for row in missing:
        print("  - " + " | ".join(row))
    print(f"页面 JS 异常：{len(errors)}")
    for error in errors:
        print("  ! " + error)
    for result in results:
        print(f"保存校验：{result['name']} → {'PASS' if result['ok'] else 'FAIL'} {result['after']}")
    return 0 if not missing and not errors else 1


if __name__ == "__main__":
    sys.exit(main())
