/**
 * 全站统一轻提示。
 *
 * 约定：按钮 / 图标 / 可点击控件的说明统一写在 `title`（或 `data-tip`）里，
 * 本模块负责把它们收编成同一套自绘浮层：首次悬停时把原生 `title` 转存为
 * `data-tip` 并移除，避免浏览器再弹一份原生提示，造成两种体验。
 *
 * 覆盖范围：鼠标悬停、键盘 Tab 聚焦（仅 :focus-visible）、禁用控件
 * （通过坐标命中，规避禁用元素不派发鼠标事件）、弹层内元素。
 */

/** 浮层状态：节点、当前目标、延时器、是否可见 */
const tip = { el: null, host: null, timer: 0, visible: false };

/** 创建（或复用）唯一的提示浮层 */
function bubble() {
  if (tip.el && tip.el.isConnected) return tip.el;
  const node = document.createElement('div');
  node.className = 'tip-bubble';
  node.setAttribute('role', 'tooltip');
  document.body.appendChild(node);
  tip.el = node;
  return node;
}

/**
 * 从事件目标解析出提示文案。
 * 优先 data-tip，其次原生 title；title 会被搬到 data-tip 上，避免双提示。
 * @param {Element|null} target 事件目标或坐标命中的元素
 * @returns {{host: Element, text: string}|null} 提示宿主与文案
 */
function resolve(target) {
  const el = target instanceof Element ? target : null;
  if (!el || el.closest('.tip-bubble')) return null;
  const host = el.closest('[data-tip], [title], [aria-label]');
  if (!host) return null;
  if (host.hasAttribute('title')) {
    const native = host.getAttribute('title');
    if (!host.hasAttribute('data-tip')) host.setAttribute('data-tip', native || '');
    host.removeAttribute('title');
  }
  const text = String(host.getAttribute('data-tip') || host.getAttribute('aria-label') || '').trim();
  return text ? { host, text } : null;
}

/**
 * 把浮层放到宿主上方（放不下时放下方），并夹在视口内。
 * @param {Element} host 提示宿主
 */
function place(host) {
  const node = bubble();
  const rect = host.getBoundingClientRect();
  const box = node.getBoundingClientRect();
  const gap = 8;
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  const left = Math.max(gap, Math.min(rect.left + rect.width / 2 - box.width / 2, vw - box.width - gap));
  let top = rect.top - box.height - gap;
  let below = false;
  if (top < gap) {
    top = rect.bottom + gap;
    below = true;
  }
  if (top + box.height > vh - gap) top = Math.max(gap, vh - box.height - gap);
  node.style.left = `${Math.round(left)}px`;
  node.style.top = `${Math.round(top)}px`;
  node.classList.toggle('below', below);
}

/**
 * 显示提示。
 * @param {{host: Element, text: string}} info 提示信息
 * @param {boolean} [immediate] 是否跳过悬停延时（键盘聚焦时用）
 */
function show(info, immediate = false) {
  window.clearTimeout(tip.timer);
  tip.timer = 0;
  tip.host = info.host;
  const run = () => {
    tip.timer = 0;
    if (!info.host.isConnected || tip.host !== info.host) return;
    const node = bubble();
    node.textContent = info.text;
    node.classList.add('on');
    tip.visible = true;
    place(info.host);
  };
  if (immediate) run();
  else tip.timer = window.setTimeout(run, 160);
}

/** 立即收起提示并清掉待执行延时 */
function hide() {
  window.clearTimeout(tip.timer);
  tip.timer = 0;
  tip.host = null;
  tip.visible = false;
  tip.el?.classList.remove('on');
}

/** mousemove 节流标记，避免每像素都做 DOM 查询 */
let pendingMove = null;

document.addEventListener(
  'mousemove',
  (event) => {
    if (pendingMove) return;
    pendingMove = window.requestAnimationFrame(() => {
      pendingMove = null;
      // 禁用元素不会成为事件目标：改成按坐标取最上层元素，保证提示仍能出现
      const hit = document.elementFromPoint(event.clientX, event.clientY);
      const info = resolve(hit);
      if (!info) {
        hide();
        return;
      }
      if (tip.host === info.host) return;
      hide();
      show(info);
    });
  },
  { passive: true }
);

// 指针离开文档时收起
document.addEventListener('mouseleave', hide);

// 键盘聚焦：只在 :focus-visible（键盘操作）时展示，鼠标点击不弹
document.addEventListener('focusin', (event) => {
  const el = event.target;
  if (!(el instanceof Element) || !el.matches(':focus-visible')) return;
  const info = resolve(el);
  if (info) show(info, true);
});
document.addEventListener('focusout', hide);

// 点击、滚动、缩放、按 Esc 都收起，避免浮层悬在原地
document.addEventListener('mousedown', hide, true);
document.addEventListener('scroll', hide, true);
window.addEventListener('resize', hide);
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') hide();
});

// 页面重绘（路由切换 / 弹窗关闭）后宿主可能已脱离文档，直接收起
new MutationObserver(() => {
  if (tip.visible && !tip.host?.isConnected) hide();
}).observe(document.body, { childList: true, subtree: true });

// 供调试或特殊场景主动收起（例如打开全屏浮层前）
export { hide as hideTip };
