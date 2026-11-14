"""竞品视图控件采集（只读）。

用途：对一个视图抓取可交互控件的去重清单，并留 1440 / 1280 两张对照截图。
不做点击、不提交表单、不修改竞品数据。

用法：
    .venv/bin/python tools/parity/collect.py --view V02 --name script \
        --url https://www.audimind.com/studio/52181776

输出：
    design/parity/ref/<date>-<view>-<name>-1440.png
    design/parity/ref/<date>-<view>-<name>-1280.png
    design/parity/ref/<date>-<view>-<name>.json
"""

import argparse
import datetime
import json
import os
import sys

from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT_DIR = os.path.join(ROOT, "design", "parity", "ref")

# 采集范围：可点击元素 + 带提示的图标 + 表单控件
SELECTOR = (
    "button, a[href], [role=button], [role=menuitem], [role=tab], [role=switch], "
    "input, select, textarea, [title], [aria-label]"
)

# 在页面内按可视位置归类区域，并按「标签 + 名称」去重
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
    if (r.bottom < 0 || r.top > vh) continue;
    const text = (n.innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 40);
    const aria = n.getAttribute('aria-label') || '';
    const title = n.getAttribute('title') || '';
    const placeholder = n.getAttribute('placeholder') || '';
    const name = text || aria || title || placeholder;
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
    const row = {
      tag: n.tagName.toLowerCase(),
      type: n.type || '',
      role: n.getAttribute('role') || '',
      region: region,
      name: name,
      text: text,
      aria: aria,
      title: title,
      placeholder: placeholder,
      disabled: Boolean(n.disabled) || n.getAttribute('aria-disabled') === 'true',
      checked: n.checked === true,
      classes: (n.className || '').toString().slice(0, 60),
    };
    if (n.tagName === 'SELECT') {
      row.options = Array.from(n.options).map((o) => ({ value: o.value, label: o.text }));
    }
    if (n.tagName === 'INPUT') {
      row.constraints = {
        min: n.getAttribute('min'),
        max: n.getAttribute('max'),
        step: n.getAttribute('step'),
        maxlength: n.getAttribute('maxlength'),
      };
    }
    rows.push(row);
  }
  return rows;
}
"""


def capture(page, view, name, stamp):
    """抓取当前视口下的控件清单与截图，返回本次控件数。"""
    controls = page.evaluate(EXTRACT_JS, SELECTOR)
    by_region = {}
    for item in controls:
        by_region[item["region"]] = by_region.get(item["region"], 0) + 1
    payload = {
        "view": view,
        "name": name,
        "url": page.url,
        "captured_at": stamp,
        "viewport": page.viewport_size,
        "stats": {"total": len(controls), "by_region": by_region},
        "controls": controls,
    }
    base = os.path.join(OUT_DIR, f"{stamp}-{view}-{name}")
    with open(base + ".json", "w", encoding="utf-8") as fp:
        json.dump(payload, fp, ensure_ascii=False, indent=2)
    page.screenshot(path=base + "-1440.png")
    page.set_viewport_size({"width": 1280, "height": 800})
    page.wait_for_timeout(400)
    page.screenshot(path=base + "-1280.png")
    page.set_viewport_size({"width": 1440, "height": 900})
    page.wait_for_timeout(300)
    print(f"{view} {name}: {len(controls)} controls -> {base}.json")
    return len(controls)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--view", required=True, help="视图编号，如 V02")
    parser.add_argument("--name", required=True, help="视图短名，如 script")
    parser.add_argument("--url", required=True)
    parser.add_argument("--wait", default="", help="等待出现的文本（可选）")
    parser.add_argument("--storage", default="", help="登录态 storage_state.json（可选）")
    args = parser.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = datetime.date.today().strftime("%Y%m%d")

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            storage_state=args.storage or None,
            locale="zh-CN",
        )
        page = context.new_page()
        page.goto(args.url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(2500)
        if args.wait:
            try:
                page.wait_for_selector(f"text={args.wait}", timeout=15000)
            except Exception:
                print(f"警告：等待「{args.wait}」超时，按当前页面采集", file=sys.stderr)
        else:
            page.wait_for_timeout(1500)
        capture(page, args.view, args.name, stamp)
        browser.close()


if __name__ == "__main__":
    main()
