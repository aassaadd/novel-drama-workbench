"""本地视图控件采集（只读，用于与竞品逐控件对标）。

用途：打开本地工作台某一视图，抓取可交互控件去重清单与 1440x900 截图，
并单独列出「纯图标按钮」及其 title/aria（本地提示用原生 title 属性）。

用法：
    .venv/bin/python tools/parity/collect_local.py --view V02 --name script \
        --hash '#/p/%2FVolumes%2Fother%2Fndw_runs%2Ftt1/c/chp_xxx'

输出：
    design/parity/local/<date>-<view>-<name>.json
    design/parity/local/<date>-<view>-<name>.png
"""

import argparse
import datetime
import json
import os

from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT_DIR = os.path.join(ROOT, "design", "parity", "local")

SELECTOR = (
    "button, a[href], [role=button], [role=menuitem], [role=tab], [role=switch], "
    "input, select, textarea, [title], [aria-label]"
)

EXTRACT_JS = """
(selector) => {
  const vw = document.documentElement.clientWidth;
  const vh = window.innerHeight;
  const rows = [];
  const seen = new Set();
  const nodes = document.querySelectorAll(selector);
  for (const n of nodes) {
    const cs = getComputedStyle(n);
    if (cs.display === 'none' || cs.visibility === 'hidden' || cs.opacity === '0') continue;
    const r = n.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) continue;
    // 不限视口：滚动区与折叠区里的控件同样计入 N，保证计数闭环不因截图高度而漏项
    const text = (n.innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 40);
    const aria = n.getAttribute('aria-label') || '';
    const title = n.getAttribute('title') || '';
    const placeholder = n.getAttribute('placeholder') || '';
    const value = n.tagName === 'INPUT' || n.tagName === 'SELECT' || n.tagName === 'TEXTAREA'
      ? String(n.value || '').slice(0, 24) : '';
    const name = text || aria || title || placeholder || (value ? '值=' + value : '');
    if (!name) continue;
    let region = '主区';
    if (r.top < 64) region = '顶栏';
    else if (r.bottom > vh - 72) region = '底栏';
    else if (r.left < 64) region = '左栏';
    else if (r.left < 400) region = '目录面板';
    else if (r.right > vw - 560) region = '右栏';
    const key = [n.tagName, n.type || '', name, aria, title, placeholder].join('|');
    if (seen.has(key)) continue;
    seen.add(key);
    rows.push({
      tag: n.tagName.toLowerCase(),
      type: n.type || '',
      role: n.getAttribute('role') || '',
      region: region,
      name: name,
      text: text,
      aria: aria,
      title: title,
      placeholder: placeholder,
      iconOnly: !text,
      disabled: Boolean(n.disabled) || n.getAttribute('aria-disabled') === 'true',
      href: n.getAttribute('href') || '',
      act: n.getAttribute('data-act') || '',
      view: n.getAttribute('data-view') || '',
      classes: (n.className || '').toString().slice(0, 80),
    });
  }
  return rows;
}
"""


def capture(page, view, name, stamp):
    """采集当前视图控件清单 + 截图，返回控件数与纯图标按钮数。"""
    controls = page.evaluate(EXTRACT_JS, SELECTOR)
    by_region = {}
    for item in controls:
        by_region[item["region"]] = by_region.get(item["region"], 0) + 1
    icon_only = [c for c in controls if c["iconOnly"]]
    payload = {
        "view": view,
        "name": name,
        "hash": page.url,
        "captured_at": stamp,
        "viewport": page.viewport_size,
        "stats": {
            "total": len(controls),
            "icon_only": len(icon_only),
            "by_region": by_region,
            "icon_only_missing_tip": len([c for c in icon_only if not (c["title"] or c["aria"])]),
        },
        "controls": controls,
    }
    base = os.path.join(OUT_DIR, f"{stamp}-{view}-{name}")
    with open(base + ".json", "w", encoding="utf-8") as fp:
        json.dump(payload, fp, ensure_ascii=False, indent=2)
    page.screenshot(path=base + ".png")
    print(f"{view} {name}: {len(controls)} controls, {len(icon_only)} icon-only -> {base}.json")
    return payload["stats"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--view", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--hash", required=True, help="如 '#/projects'")
    parser.add_argument("--click", default="", help="进入视图前先点击的选择器（可选）")
    parser.add_argument("--click2", default="", help="第一次点击后再点击的选择器（可选，菜单项用）")
    parser.add_argument("--wait-sel", default="", help="等待出现的 CSS 选择器（可选）")
    parser.add_argument("--base", default="http://127.0.0.1:8790/")
    parser.add_argument("--wait", default="", help="等待出现的文本（可选）")
    args = parser.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = datetime.date.today().strftime("%Y%m%d")

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 900}, locale="zh-CN")
        page = context.new_page()
        page.goto(args.base + args.hash, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2500)
        if args.wait:
            try:
                page.wait_for_selector(f"text={args.wait}", timeout=15000)
            except Exception:
                print(f"警告：等待「{args.wait}」超时，按当前页面采集")
        if args.click:
            page.evaluate("(sel) => document.querySelector(sel)?.click()", args.click)
            page.wait_for_timeout(1800)
        if args.click2:
            page.evaluate("(sel) => document.querySelector(sel)?.click()", args.click2)
            page.wait_for_timeout(2200)
        if args.wait_sel:
            try:
                page.wait_for_selector(args.wait_sel, timeout=15000)
            except Exception:
                print(f"警告：等待选择器「{args.wait_sel}」超时，按当前页面采集")
        capture(page, args.view, args.name, stamp)
        browser.close()


if __name__ == "__main__":
    main()
