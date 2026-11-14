/**
 * 通用 DOM / 格式化 / 弹窗 / toast 工具。
 * 新版前端不引入任何框架，这里只放与业务无关的小函数。
 */

/** HTML 转义，所有插入模板的用户文本都必须过一遍 */
export function esc(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

/** 毫秒 → 00:00.0 */
export function fmtDuration(ms) {
  const total = Math.max(0, Math.round(Number(ms) || 0));
  const min = Math.floor(total / 60000);
  const sec = Math.floor((total % 60000) / 1000);
  const tenth = Math.floor((total % 1000) / 100);
  return `${String(min).padStart(2, '0')}:${String(sec).padStart(2, '0')}.${tenth}`;
}

/** 毫秒 → 00:00（长时长也用分:秒，不引入小时） */
export function fmtClock(ms) {
  const total = Math.max(0, Math.round(Number(ms) || 0));
  const min = Math.floor(total / 60000);
  const sec = Math.floor((total % 60000) / 1000);
  return `${String(min).padStart(2, '0')}:${String(sec).padStart(2, '0')}`;
}

/**
 * 生成影响清单：按「章节 · 场景」分组列出本次会生成/替换的台词行。
 * @param {object} plan `/api/ui/tts/batch-all/plan` 返回值（需含 items）
 * @param {Array<{id:string,name:string}>} [characters] 角色表，用于把 speaker_id 显示成名字
 * @returns {string} HTML 片段；没有清单时返回空串
 */
export function impactListHTML(plan, characters = []) {
  const items = plan?.items || [];
  if (!items.length) return '';
  const shown = items.slice(0, 60);
  const groups = [];
  for (const item of shown) {
    const key = `${item.chapter_id || ''}|${item.scene_index || 0}`;
    let group = groups.find((entry) => entry.key === key);
    if (!group) {
      group = { key, title: `${item.chapter_title || '本章'} · 第 ${item.scene_index || '?'} 场`, rows: [] };
      groups.push(group);
    }
    group.rows.push(item);
  }
  const nameOf = (id) =>
    characters.find((item) => item.id === id)?.name || (id === 'chr_narrator' ? '旁白' : id || '未识别');
  return `
    <details class="impact-block" open style="margin-top:10px">
      <summary>影响清单（${Number(plan.total || items.length)} 行${
        plan.assets ? ` · ${plan.assets} 条已有音频会被替换` : ''
      }）</summary>
      <div class="impact" style="margin-top:8px">
        ${groups
          .map(
            (group) => `
          <div class="hint" style="margin:6px 0 2px">${esc(group.title)}（${group.rows.length} 行）</div>
          ${group.rows
            .map(
              (item) => `<div class="imp-row">
                <span style="min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
                  ${esc(nameOf(item.speaker_id))} · ${esc((item.text || '').slice(0, 42))}
                </span>
                <span class="badge ${item.has_audio ? 'warn' : 'mute'}">${item.has_audio ? '替换音频' : '待生成'}</span>
              </div>`
            )
            .join('')}`
          )
          .join('')}
        ${
          plan.items_truncated || items.length > 60
            ? '<div class="hint">清单过长，只展示前 60 行。</div>'
            : ''
        }
      </div>
    </details>`;
}

/** 字节 → 可读体积 */
export function fmtBytes(size) {
  const n = Number(size) || 0;
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

/** 字符数（按 Unicode 码点，中文一个字算一个） */
export function charCount(text) {
  return Array.from(String(text || '')).length;
}

const TOAST_KIND = { info: '', ok: 'ok', bad: 'bad', warn: 'warn' };

/**
 * 轻提示。
 * @param {string} message 文案
 * @param {'info'|'ok'|'bad'|'warn'} kind 语义色
 * @param {number} timeout 自动消失毫秒数
 */
export function toast(message, kind = 'info', timeout = 3200) {
  const host = document.getElementById('toast-host');
  if (!host) return;
  const node = document.createElement('div');
  node.className = `toast ${TOAST_KIND[kind] || ''}`;
  node.textContent = String(message ?? '');
  host.appendChild(node);
  window.setTimeout(() => node.remove(), timeout);
}

let activeDialog = null;

/**
 * 打开弹窗。
 * @param {object} options
 * @param {string} options.title 标题
 * @param {string} options.body 内容 HTML
 * @param {string} options.foot 底部 HTML（按钮带 data-dlg-close 可自动关闭）
 * @param {boolean} [options.wide] 宽版
 * @param {() => void} [options.onMount] 挂载后回调
 * @returns {HTMLElement} 弹窗根节点
 */
export function openDialog({ title, body, foot = '', wide = false, onMount }) {
  closeDialog();
  const mask = document.createElement('div');
  mask.className = 'dlg-mask';
  mask.innerHTML = `
    <div class="dlg ${wide ? 'wide' : ''}" role="dialog" aria-modal="true">
      <div class="dlg-head">
        <span class="dt">${esc(title)}</span>
        <div class="right"><button class="btn icon sm" data-dlg-close title="关闭">${icon('x')}</button></div>
      </div>
      <div class="dlg-body">${body}</div>
      ${foot ? `<div class="dlg-foot">${foot}</div>` : ''}
    </div>`;
  mask.addEventListener('mousedown', (event) => {
    if (event.target === mask) closeDialog();
  });
  mask.addEventListener('click', (event) => {
    if (event.target.closest('[data-dlg-close]')) closeDialog();
  });
  document.body.appendChild(mask);
  activeDialog = mask;
  onMount?.(mask);
  return mask;
}

/** 关闭当前弹窗 */
export function closeDialog() {
  if (activeDialog) {
    activeDialog.remove();
    activeDialog = null;
  }
}

// Esc 统一关弹窗，业务层不用各自监听
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') {
    closeDialog();
    closeMenu();
  }
});

let activeMenu = null;

/** 关闭行内菜单 */
export function closeMenu() {
  if (activeMenu) {
    activeMenu.remove();
    activeMenu = null;
    document.removeEventListener('mousedown', onDocDown, true);
  }
}

/** 点击菜单外部时关闭（挂在捕获阶段，先于业务点击） */
function onDocDown(event) {
  if (activeMenu && !event.target.closest('.row-menu')) closeMenu();
}

/**
 * 在锚点旁打开行内菜单（参考产品的行尾「⋯」）。
 * @param {HTMLElement} anchor 触发按钮
 * @param {Array<{key:string,label:string,danger?:boolean,disabled?:boolean,sep?:boolean}>} items 菜单项
 * @param {(key:string)=>void} onPick 选中回调
 */
export function openMenu(anchor, items, onPick) {
  closeMenu();
  const menu = document.createElement('div');
  menu.className = 'row-menu';
  menu.innerHTML = items
    .map((item) =>
      item.sep
        ? '<div class="row-menu-sep"></div>'
        : `<button class="row-menu-item ${item.danger ? 'danger' : ''}" data-menu-key="${esc(item.key)}"
             ${item.disabled ? 'disabled' : ''}>${esc(item.label)}</button>`
    )
    .join('');
  document.body.appendChild(menu);
  // 右对齐锚点、向下展开；下方空间不足时改为向上
  const rect = anchor.getBoundingClientRect();
  const width = menu.offsetWidth;
  const height = menu.offsetHeight;
  const left = Math.max(8, Math.min(rect.right - width, window.innerWidth - width - 8));
  const below = rect.bottom + 4;
  const top = below + height > window.innerHeight - 8 ? Math.max(8, rect.top - height - 4) : below;
  menu.style.left = `${left}px`;
  menu.style.top = `${top}px`;
  activeMenu = menu;
  menu.addEventListener('click', (event) => {
    const button = event.target.closest('[data-menu-key]');
    if (!button || button.disabled) return;
    const key = button.dataset.menuKey;
    closeMenu();
    onPick(key);
  });
  document.addEventListener('mousedown', onDocDown, true);
  window.addEventListener('scroll', closeMenu, { once: true, capture: true });
}

const ICONS = {
  back: '<path d="M9.5 3.5 5 8l4.5 4.5" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/>',
  prev: '<path d="M10 3.5 5.5 8 10 12.5" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/>',
  next: '<path d="M6 3.5 10.5 8 6 12.5" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/>',
  play: '<path d="M5.5 3.4v9.2L12 8z" fill="currentColor"/>',
  pause: '<path d="M5 3.5h2v9H5zm4 0h2v9H9z" fill="currentColor"/>',
  refresh: '<path d="M12.6 8a4.6 4.6 0 1 1-1.4-3.3" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/><path d="M12.8 2.8v2.6h-2.6" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/>',
  more: '<circle cx="3.6" cy="8" r="1.1" fill="currentColor"/><circle cx="8" cy="8" r="1.1" fill="currentColor"/><circle cx="12.4" cy="8" r="1.1" fill="currentColor"/>',
  search: '<circle cx="7" cy="7" r="3.9" fill="none" stroke="currentColor" stroke-width="1.4"/><path d="m10 10 3 3" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/>',
  plus: '<path d="M8 3v10M3 8h10" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/>',
  folder: '<path d="M2 5.2h4l1.3 1.5H14v6.1H2z" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/><path d="M2 6.7V4.2h4.2" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/>',
  chapter: '<path d="M3.2 2.8h9.6v10.4H3.2z" fill="none" stroke="currentColor" stroke-width="1.3"/><path d="M5.4 5.6h5.2M5.4 8h5.2M5.4 10.4h3.2" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/>',
  book: '<path d="M2.4 3.6c1.9-.6 3.8-.6 5.6.4 1.8-1 3.7-1 5.6-.4v8.6c-1.9-.6-3.8-.6-5.6.4-1.8-1-3.7-1-5.6-.4z" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/><path d="M8 4v8.6" stroke="currentColor" stroke-width="1.2"/>',
  mic: '<rect x="6.2" y="2.2" width="3.6" height="7" rx="1.8" fill="none" stroke="currentColor" stroke-width="1.3"/><path d="M4 7.4a4 4 0 0 0 8 0M8 11.4v2.4" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/>',
  export: '<path d="M8 2.6v7.2M5.4 7.4 8 10l2.6-2.6" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/><path d="M2.8 11.4v1.8h10.4v-1.8" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/>',
  task: '<path d="M3 3h10v10H3z" fill="none" stroke="currentColor" stroke-width="1.3"/><path d="M5.4 6.4h5.2M5.4 9h5.2" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/>',
  check: '<path d="m3.4 8.4 3 3L12.8 5" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>',
  x: '<path d="m4 4 8 8M12 4l-8 8" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/>',
  stop: '<rect x="4.4" y="4.4" width="7.2" height="7.2" rx="1" fill="currentColor"/>',
  warn: '<path d="M8 2.8 14 13H2z" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/><path d="M8 6.2v3.4M8 11.2v.5" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/>',
  arrow: '<path d="M3.5 8h9M9 4.5 12.5 8 9 11.5" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/>',
  audio: '<path d="M2.6 6.4h2.2L8 4v8L4.8 9.6H2.6z" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/><path d="M10.4 6a2.8 2.8 0 0 1 0 4" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/>',
  user: '<circle cx="8" cy="5.4" r="2.6" fill="none" stroke="currentColor" stroke-width="1.3"/><path d="M3.2 13.2c.6-2.4 2.5-3.7 4.8-3.7s4.2 1.3 4.8 3.7" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/>',
  image: '<rect x="2.4" y="3.4" width="11.2" height="9.2" rx="1.4" fill="none" stroke="currentColor" stroke-width="1.3"/><circle cx="6" cy="6.6" r="1" fill="currentColor"/><path d="M3.4 11.4l3-3 2.2 2.2 1.6-1.6 2.4 2.4" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round"/>',
  task: '<rect x="3" y="2.6" width="10" height="10.8" rx="1.4" fill="none" stroke="currentColor" stroke-width="1.3"/><path d="M5.4 5.8h5.2M5.4 8h5.2M5.4 10.2h3.4" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/>',
  gear: '<circle cx="8" cy="8" r="2.2" fill="none" stroke="currentColor" stroke-width="1.3"/><path d="M8 1.8v1.6M8 12.6v1.6M1.8 8h1.6M12.6 8h1.6M3.6 3.6l1.1 1.1M11.3 11.3l1.1 1.1M12.4 3.6l-1.1 1.1M4.7 11.3l-1.1 1.1" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/>',
  cloud: '<path d="M4.6 12.2a2.6 2.6 0 0 1-.3-5.2 3.4 3.4 0 0 1 6.5-.7 2.4 2.4 0 0 1-.4 5.9z" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round"/><path d="M7.9 7.4v3.6M6.3 9l1.6-1.6L9.5 9" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round"/>',
  undo: '<path d="M6 4.2L3 7l3 2.8" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/><path d="M3.4 7h5.4a4 4 0 0 1 0 8H7" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/>',
  redo: '<path d="M10 4.2L13 7l-3 2.8" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/><path d="M12.6 7H7.2a4 4 0 0 0 0 8H9" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/>',
  collapse: '<path d="M9.6 3.4v9.2M11 6.2L8.2 8l2.8 1.8" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/><path d="M4.2 3.4v9.2" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/>',
  list: '<circle cx="3.6" cy="4.2" r=".9" fill="currentColor"/><circle cx="3.6" cy="8" r=".9" fill="currentColor"/><circle cx="3.6" cy="11.8" r=".9" fill="currentColor"/><path d="M6 4.2h7M6 8h7M6 11.8h7" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/>',
  grid: '<path d="M3 3h4.2v4.2H3zM8.8 3H13v4.2H8.8zM3 8.8h4.2V13H3zM8.8 8.8H13V13H8.8z" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/>',
  spark: '<path d="M8 2.2l1.3 3.9 3.9 1.3-3.9 1.3L8 12.6 6.7 8.7 2.8 7.4l3.9-1.3z" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round"/><path d="M12.6 11.4l.5 1.4 1.4.5-1.4.5-.5 1.4-.5-1.4-1.4-.5 1.4-.5z" fill="currentColor"/>',
  trash: '<path d="M3.4 4.6h9.2M6 4.6V3.2h4v1.4M4.6 4.6l.6 8.2h5.6l.6-8.2" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/>',
};

/**
 * 取内联 SVG 图标。
 * @param {keyof typeof ICONS} name 图标名
 * @param {number} size 像素尺寸
 */
export function icon(name, size = 16) {
  const body = ICONS[name] || ICONS.more;
  return `<svg class="ico" width="${size}" height="${size}" viewBox="0 0 16 16" aria-hidden="true">${body}</svg>`;
}
