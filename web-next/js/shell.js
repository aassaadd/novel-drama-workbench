/**
 * 应用外壳：顶栏、56px 图标栏、360px 章节目录面板，以及跨页跳转。
 * 页面只组合这些片段，尺寸统一由 app.css 的骨架类控制。
 */
import { esc, icon, openDialog, closeDialog, charCount, toast, openMenu } from './dom.js';
import { getJSON, postJSON, fileUrl } from './api.js';
import { state, lastProject, rememberProject } from './store.js';

/** 章节状态 → 目录圆环颜色 */
export function chapterDot(chapter) {
  if (!chapter) return '';
  if (chapter.status === 'draft') return '';
  if (chapter.status === 'split') return 'warn';
  return 'ok';
}

/** 行状态 → 中文标签 */
export const STATE_LABEL = {
  ready: '已配',
  pending: '待配',
  stale: '已过期',
  failed: '失败',
  unknown: '未识别',
};

/** 行状态 → 徽标语义色 */
export function stateClass(name) {
  if (name === 'ready') return 'ok';
  if (name === 'failed') return 'bad';
  if (name === 'stale') return 'warn';
  if (name === 'pending') return 'mute';
  return '';
}

/**
 * 渲染 56px 图标栏。
 * 第一阶段只保留参考产品存在的入口（目录 / 角色 / 素材 / 任务 / 设置），
 * 本系统独有的导出、工程切换不作为导航项。
 * @param {'projects'|'chapter'|'characters'|'voices'|'media'|'tasks'|'settings'|'logs'} active 当前阶段
 * @param {{chapters?:number, ready?:number, total?:number}} counts 角标
 */
export function railbar(active, counts = {}) {
  const path = encodeURIComponent(state.path);
  const chapterId = state.chapterId;
  const hasChapter = Boolean(state.path && chapterId);
  const toNovel = path ? `#/p/${path}/novel` : '#/projects';
  const toCharacters = path ? `#/p/${path}/characters` : '#/projects';
  const toVoices = path ? `#/p/${path}/voices` : '#/projects';
  const toMedia = path ? `#/p/${path}/media` : '#/projects';
  const toTasks = path ? `#/p/${path}/tasks` : '#/projects';
  // 设置是全局视图：不带任何工程参数；工程侧设置走 #/p/<路径>/settings
  const toSettings = '#/settings';
  const items = [
    { key: 'projects', label: '项目', href: '#/projects', icon: 'folder', n: '' },
    { key: 'novel', label: '小说', href: toNovel, icon: 'book', n: '' },
    // 无章节时「目录」不再是链接：不回工程列表、也不进空选择页，
    // 改成按钮，由 need-chapter 引导当前页给出「写小说 / 导入正文」两条入口
    hasChapter
      ? {
          key: 'chapter',
          label: '目录',
          href: `#/p/${path}/c/${chapterId}`,
          icon: 'chapter',
          n: counts.chapters ? String(counts.chapters) : '',
        }
      : state.path
        ? { key: 'chapter', label: '目录', act: 'need-chapter', icon: 'chapter', n: '' }
        : { key: 'chapter', label: '目录', href: '#/projects', icon: 'chapter', n: '' },
    { key: 'characters', label: '角色', href: toCharacters, icon: 'user', n: '' },
    { key: 'voices', label: '音色', href: toVoices, icon: 'audio', n: '' },
    { key: 'media', label: '素材', href: toMedia, icon: 'image', n: '' },
    { key: 'tasks', label: '任务', href: toTasks, icon: 'task', n: '' },
  ];
  return `
    <nav class="railbar" aria-label="制作阶段">
      ${items
        .map((item) =>
          item.act
            ? `
        <button class="rail-item ${active === item.key ? 'active' : ''}" data-act="${item.act}"
          title="${item.label}：进入画本编辑；没有章节时给出写小说 / 导入入口">
          <span class="rail-ic">${icon(item.icon, 18)}</span>${item.label}
        </button>`
            : `
        <a class="rail-item ${active === item.key ? 'active' : ''}" href="${item.href}" title="${item.label}">
          <span class="rail-ic">${icon(item.icon, 18)}</span>${item.label}
          ${item.n ? `<span class="rail-n">${esc(item.n)}</span>` : ''}
        </a>`
        )
        .join('')}
      <div class="rail-foot">
        <button class="rail-item" data-act="global-search" title="全局搜索（⌘/Ctrl + K）">
          <span class="rail-ic">${icon('search', 18)}</span>搜索
        </button>
        <a class="rail-item ${active === 'logs' ? 'active' : ''}" href="#/logs"
          title="全平台日志：操作记录与大模型调用（含完整提示词）">
          <span class="rail-ic">${icon('log', 18)}</span>日志
        </a>
        <a class="rail-item ${active === 'settings' ? 'active' : ''}" href="${toSettings}"
          title="设置：全局与本机环境（不需要先打开工程）">
          <span class="rail-ic">${icon('gear', 18)}</span>设置
        </a>
      </div>
    </nav>`;
}

/**
 * 顶栏。
 * @param {object} options
 * @param {string} options.left 左侧 HTML
 * @param {string} options.crumbs 面包屑 HTML
 * @param {string} [options.state] 保存态文本
 * @param {string} [options.stateClass] 保存态语义色
 * @param {string} options.right 右侧操作 HTML
 */
export function topbar({ left, crumbs, stateText = '', stateClass = '', right = '' }) {
  return `
    <header class="topbar">
      <div class="tb-left">${left}</div>
      <div class="crumbs">${crumbs}</div>
      ${stateText ? `<span class="save-state ${stateClass}">${icon('check', 12)}${esc(stateText)}</span>` : ''}
      <div class="tb-right">${right}</div>
    </header>`;
}

/**
 * 章节目录（360px 上下文面板）。
 * @param {object} options
 * @param {string} [options.search] 搜索词
 * @param {string} [options.mode] 当前阶段（决定高亮样式）
 */
/** 目录排序与显示开关（与参考的排序菜单一致） */
const chapterView = { sort: 'asc', showStatus: true, showWords: true, batch: false };
/** 批量操作时选中的章节 */
const chapterSelected = new Set();
/** 目录三组筛选：场景 / 角色 / 状态（状态值带分组前缀，如 creative:voice） */
const chapterFilters = { scene: '', character: '', status: '' };
/** 三组筛选的候选值与每章维度（来自 /api/ui/chapters/facets） */
const chapterFacetCache = { path: '', data: null, loading: false };
/** 正文搜索命中：关键词 → 章节命中数（与标题匹配取并集） */
const chapterBodyHits = { q: '', path: '', hits: new Map() };
let chapterSearchTimer = null;

/**
 * 节流拉取正文命中；返回后重画目录。
 * 关键词少于 2 个字时只走标题匹配，避免每敲一个字都扫全书。
 * @param {string} keyword 搜索词
 */
function scheduleChapterBodySearch(keyword) {
  const key = keyword.trim();
  if (chapterBodyHits.path !== state.path) {
    chapterBodyHits.path = state.path;
    chapterBodyHits.q = '';
    chapterBodyHits.hits.clear();
  }
  if (key.length < 2) {
    chapterBodyHits.q = '';
    chapterBodyHits.hits.clear();
    return;
  }
  if (chapterBodyHits.q === key) return;
  window.clearTimeout(chapterSearchTimer);
  chapterSearchTimer = window.setTimeout(async () => {
    try {
      const data = await getJSON('/api/ui/chapters/search', { path: state.path, q: key });
      if (state.chapterSearch.trim() !== key) return;
      chapterBodyHits.q = key;
      chapterBodyHits.path = state.path;
      chapterBodyHits.hits = new Map((data.hits || []).map((item) => [item.id, item]));
      repaintChapterCtx(key);
    } catch {
      // 正文检索失败时保留标题匹配结果，不打断输入
    }
  }, 250);
}

/** 当前搜索词的正文命中表（关键词或工程不一致时视为无命中） */
function bodyHitsFor(search) {
  const key = search.trim();
  if (!key || chapterBodyHits.q !== key || chapterBodyHits.path !== state.path) return null;
  return chapterBodyHits.hits;
}

/** 状态筛选分组（与参考的「创作状态 / 成品音频 / 工单」一致） */
const STATUS_GROUPS = [
  [
    '创作状态',
    [
      ['creative:script', '画本中'],
      ['creative:voice', '配音中'],
      ['creative:post', '后期中'],
      ['creative:review', '审听中'],
      ['creative:done', '创作完成'],
    ],
  ],
  [
    '成品音频',
    [
      ['audio:yes', '有成品音频'],
      ['audio:no', '无成品音频'],
    ],
  ],
  [
    '工单',
    [
      ['job:yes', '有工单'],
      ['job:no', '无工单'],
    ],
  ],
];

/**
 * 拉取目录筛选维度；同一工程只拉一次，切工程后重新拉。
 * 失败时保持无筛选候选，不影响目录本身的展示与操作。
 */
async function ensureChapterFacets() {
  if (!state.path || chapterFacetCache.loading) return;
  if (chapterFacetCache.path === state.path && chapterFacetCache.data) return;
  chapterFacetCache.loading = true;
  chapterFacetCache.path = state.path;
  try {
    chapterFacetCache.data = await getJSON('/api/ui/chapters/facets', { path: state.path });
    repaintChapterCtx();
  } catch {
    chapterFacetCache.data = null;
  } finally {
    chapterFacetCache.loading = false;
  }
}

/** 目录过滤器候选值 */
function facetData() {
  return chapterFacetCache.data || { characters: [], scenes: [], chapters: {} };
}

/**
 * 单章是否命中当前三组筛选。
 * @param {object} chapter 章节元信息
 * @returns {boolean} 是否显示
 */
function matchChapterFilters(chapter) {
  const info = facetData().chapters?.[chapter.id];
  if (chapterFilters.scene && !(info?.scene_titles || []).includes(chapterFilters.scene)) return false;
  if (chapterFilters.character && !(info?.character_ids || []).includes(chapterFilters.character)) return false;
  if (chapterFilters.status) {
    const [group, value] = chapterFilters.status.split(':');
    if (group === 'creative' && info?.creative !== value) return false;
    if (group === 'audio' && Boolean(info?.has_audio) !== (value === 'yes')) return false;
    if (group === 'job' && Boolean(info?.has_job) !== (value === 'yes')) return false;
  }
  return true;
}

/** 目录三组筛选下拉（全部场景 / 全部角色 / 全部状态） */
function chapterFiltersHTML() {
  const data = facetData();
  const sceneOptions = data.scenes
    .map((title) => `<option value="${esc(title)}" ${chapterFilters.scene === title ? 'selected' : ''}>${esc(title)}</option>`)
    .join('');
  const charOptions = data.characters
    .map(
      (item) =>
        `<option value="${esc(item.id)}" ${chapterFilters.character === item.id ? 'selected' : ''}>${esc(item.name)}</option>`
    )
    .join('');
  const statusOptions = STATUS_GROUPS.map(
    ([label, rows]) =>
      `<optgroup label="${esc(label)}">${rows
        .map(
          ([key, text]) =>
            `<option value="${key}" ${chapterFilters.status === key ? 'selected' : ''}>${esc(text)}</option>`
        )
        .join('')}</optgroup>`
  ).join('');
  return `
    <div class="toolrow" style="gap:6px">
      <select class="select" data-chap-filter="scene" style="flex:1;height:26px;font-size:11px" title="按场景筛选">
        <option value="">全部场景</option>${sceneOptions}
      </select>
      <select class="select" data-chap-filter="character" style="flex:1;height:26px;font-size:11px" title="按角色筛选">
        <option value="">全部角色</option>${charOptions}
      </select>
      <select class="select" data-chap-filter="status" style="flex:1;height:26px;font-size:11px" title="按状态筛选">
        <option value="">全部状态</option>${statusOptions}
      </select>
    </div>`;
}


/** 读写目录排序与批量状态 */
export function chapterViewState() {
  return chapterView;
}

/** 批量选择 */
export function chapterSelection() {
  return chapterSelected;
}

/**
 * 设定批量模式；关闭时清空选择。
 * @param {boolean} on 是否进入批量模式
 */
export function setChapterBatch(on) {
  chapterView.batch = Boolean(on);
  if (!on) chapterSelected.clear();
}

/**
 * 目录行排序后的章节列表（先应用场景 / 角色 / 状态三组筛选，再排序）。
 * @param {string} search 章节搜索词
 * @returns {Array<object>} 章节列表
 */
export function sortedChapters(search = '') {
  const hits = bodyHitsFor(search);
  const rows = state.chapters.filter((chapter) => {
    if (!matchChapterFilters(chapter)) return false;
    if (!search.trim()) return true;
    if (`${chapter.title || ''}`.toLowerCase().includes(search.trim().toLowerCase())) return true;
    return Boolean(hits?.has(chapter.id));
  });
  return chapterView.sort === 'desc' ? [...rows].reverse() : rows;
}

export function chapterCtx({ search = '' } = {}) {
  // 目录首次渲染时补拉筛选维度；数据回来后自动重画一次
  void ensureChapterFacets();
  scheduleChapterBodySearch(search);
  const hits = bodyHitsFor(search);
  const rows = sortedChapters(search)
    .map((chapter) => {
      const on = chapter.id === state.chapterId ? 'on' : '';
      const picked = chapterSelected.has(chapter.id);
      const hit = hits?.get(chapter.id);
      const stale = Number(facetData().chapters?.[chapter.id]?.stale || 0);
      return `
      <div class="chap-row ${on} ${picked ? 'picked' : ''}" data-chapter="${esc(chapter.id)}">
        ${chapterView.batch ? `<input type="checkbox" class="chap-check" ${picked ? 'checked' : ''} />` : ''}
        <span class="cn">${esc(chapter.title || `第 ${chapter.index} 章`)}</span>
        ${hit ? `<span class="badge accent" title="${esc(hit.snippet || '')}">正文 ${hit.count}</span>` : ''}
        ${
          stale
            ? `<button class="badge warn" data-act="chapter-stale" data-chapter="${esc(chapter.id)}"
                 title="${stale} 行台词已过期，点击查看汇总">过期 ${stale}</button>`
            : ''
        }
        ${chapterView.showStatus ? `<span class="dot ${chapterDot(chapter)}"></span>` : ''}
        ${chapterView.showWords ? `<span class="cw">${chapter.word_count ? `${chapter.word_count}字` : '未写'}</span>` : ''}
        <button class="btn icon sm chap-more" data-act="chapter-menu" data-chapter="${esc(chapter.id)}"
          title="章节操作">${icon('more', 12)}</button>
      </div>`;
    })
    .join('');
  const empty =
    state.chapters.length === 0
      ? `<div class="empty"><b>还没有章节</b><span>先在「小说」视图落地章节，或导入已有正文</span></div>`
      : `<div class="empty"><b>没有匹配的章节</b><span>换个关键词，或清空搜索与筛选</span></div>`;
  return `
    <section class="ctx" aria-label="章节目录">
      <div class="ctx-head">
        <span class="ct">章节目录</span>
        <span class="badge mute">${state.chapters.length} 章</span>
        <div class="right">
          <button class="btn icon sm" data-act="chapter-sort" title="排序与显示">${icon('list', 13)}</button>
          <button class="btn icon sm" data-act="new-chapter" title="新建章节">${icon('plus')}</button>
          <button class="btn icon sm" data-act="toggle-ctx" title="收起目录">${icon('collapse')}</button>
        </div>
      </div>
      <div class="ctx-tools">
        ${chapterFiltersHTML()}
        <div class="toolrow" style="margin-top:6px">
          <div class="search">
            ${icon('search', 14)}
            <input type="search" id="chapter-search" placeholder="搜索章节" value="${esc(search)}" />
          </div>
        </div>
      </div>
      ${
        chapterView.batch
          ? `<div class="chap-batch">
              <span>已选 ${chapterSelected.size} 章</span>
              <button class="btn sm" data-act="chapter-batch-all">全选</button>
              <button class="btn sm" data-act="chapter-batch-resplit">重新切分</button>
              <button class="btn sm danger" data-act="chapter-batch-delete">删除</button>
              <button class="btn sm" data-act="chapter-batch-cancel">取消</button>
            </div>`
          : ''
      }
      <div class="chap-list" id="chapter-list">${rows || empty}</div>
      <div class="hint" style="padding:9px 14px;border-top:1px solid var(--line);font-size:10.5px">
        共 ${state.chapters.length} 章 · 点击切换${chapterView.batch ? ' · 点击勾选' : ''}
      </div>
    </section>`;
}

// ---------------- 目录排序 / 批量 / 章节操作 ----------------

/** 只重画左侧目录，保留搜索词与焦点 */
export function repaintChapterCtx(search = '') {
  const list = document.getElementById('chapter-list');
  const ctx = list?.closest('.ctx');
  if (!ctx) return;
  const input = document.getElementById('chapter-search');
  const focused = document.activeElement === input;
  const value = search || input?.value || state.chapterSearch || '';
  ctx.outerHTML = chapterCtx({ search: value });
  if (focused) {
    // 边输入边重画时把焦点和光标放回搜索框，避免每敲一个字就丢焦点
    const next = document.getElementById('chapter-search');
    if (next) {
      next.focus();
      next.setSelectionRange(next.value.length, next.value.length);
    }
  }
}

// 目录搜索：输入即筛（标题同步匹配，正文命中走 250ms 节流）
document.addEventListener('input', (event) => {
  if (event.target.id !== 'chapter-search') return;
  state.chapterSearch = event.target.value;
  repaintChapterCtx(event.target.value);
});

/** 重新拉取章节列表并重画目录 */
async function reloadChapters() {
  try {
    state.chapters = await getJSON('/api/project/chapters', { path: state.path });
    // 目录内容变了，筛选维度（角色/场景/状态）也可能变，下次渲染重新拉
    chapterFacetCache.data = null;
    chapterFacetCache.path = '';
  } catch {
    // 列表刷新失败时保留旧数据，不打断当前操作
  }
  repaintChapterCtx();
}

/** 通知外部页面章节信息发生变化（重命名 / 删除 / 批量切分后） */
function notifyChaptersChanged(chapterId = '') {
  document.dispatchEvent(new CustomEvent('ndw:chapters-changed', { detail: { chapterId } }));
}

/** 在目录中勾选/取消某个章节 */
function toggleChapterPick(chapterId) {
  if (chapterSelected.has(chapterId)) chapterSelected.delete(chapterId);
  else chapterSelected.add(chapterId);
  repaintChapterCtx();
}

/** 排序与显示设置弹窗（与参考的「排序与显示」菜单一致） */
function openChapterSort() {
  const mask = openDialog({
    title: '排序与显示',
    body: `<div class="dlg-note">只影响左侧目录的显示，不改动章节顺序与内容。</div>
      <div class="radio-line"><label><input type="radio" name="chap-sort" value="asc" ${
        chapterView.sort === 'asc' ? 'checked' : ''
      } /> 按章节 ID 升序</label></div>
      <div class="radio-line"><label><input type="radio" name="chap-sort" value="desc" ${
        chapterView.sort === 'desc' ? 'checked' : ''
      } /> 按章节 ID 降序</label></div>
      <div class="check-line"><label><input type="checkbox" id="chap-show-status" ${
        chapterView.showStatus ? 'checked' : ''
      } /> 显示创作状态</label></div>
      <div class="check-line"><label><input type="checkbox" id="chap-show-words" ${
        chapterView.showWords ? 'checked' : ''
      } /> 显示章节字数</label></div>`,
    foot: `<div class="right"><button class="btn primary" data-dlg-close>完成</button></div>`,
  });
  mask.addEventListener('change', () => {
    const sort = mask.querySelector('input[name="chap-sort"]:checked')?.value;
    chapterView.sort = sort === 'desc' ? 'desc' : 'asc';
    chapterView.showStatus = mask.querySelector('#chap-show-status').checked;
    chapterView.showWords = mask.querySelector('#chap-show-words').checked;
    repaintChapterCtx();
  });
}

/**
 * 章节级过期汇总：列出本章过期/失败的台词行，按场景分组。
 * @param {string} chapterId 章节 ID
 */
async function openChapterStale(chapterId) {
  let data;
  try {
    data = await getJSON('/api/ui/chapters/stale', { path: state.path, chapter: chapterId });
  } catch (error) {
    toast(error.message, 'bad');
    return;
  }
  const items = data?.items || [];
  const groups = [];
  for (const item of items) {
    const key = item.scene_id || 'scene';
    let group = groups.find((entry) => entry.key === key);
    if (!group) {
      group = { key, title: item.scene_title || '未命名场景', rows: [] };
      groups.push(group);
    }
    group.rows.push(item);
  }
  const counts = data?.counts || {};
  const mask = openDialog({
    title: `${data?.title || '本章'} · 下游过期汇总`,
    wide: true,
    body: items.length
      ? `<div class="dlg-note warn">${items.length} 行台词已过期或失败，对应配音需要重新生成；正文改动前生成的音频仍保留在磁盘。</div>
        <div class="summary-line"><span>章节状态</span><span class="right">已配 ${counts.ready || 0} · 过期 ${
          counts.stale || 0
        } · 失败 ${counts.failed || 0} · 待配 ${counts.pending || 0}</span></div>
        <div class="impact">
          ${groups
            .map(
              (group) => `<div class="hint" style="margin:8px 0 2px">${esc(group.title)}（${group.rows.length} 行）</div>
                ${group.rows
                  .map(
                    (item) => `<div class="imp-row">
                      <span style="min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
                        ${esc(item.speaker || '')} · ${esc((item.text || '').slice(0, 40))}
                      </span>
                      <span class="badge ${item.state === 'failed' ? 'bad' : 'warn'}">${
                        item.state === 'failed' ? '失败' : '已过期'
                      }</span>
                    </div>`
                  )
                  .join('')}`
            )
            .join('')}
        </div>`
      : `<div class="empty"><b>没有过期或失败的台词</b><span>本章所有行都与正文一致</span></div>`,
    foot: `<div class="right"><button class="btn" data-dlg-close>关闭</button>
      ${items.length ? '<button class="btn primary" id="stale-go">去处理</button>' : ''}</div>`,
  });
  mask.querySelector('#stale-go')?.addEventListener('click', () => {
    closeDialog();
    goChapter(chapterId);
    toast('可在右栏「本章任务」补齐缺失配音，或按行重跑', 'ok', 3500);
  });
}

/** 当前章节对象 */
function chapterOf(chapterId) {
  return state.chapters.find((item) => item.id === chapterId);
}

/** 章节状态中文名 */
function chapterStatusLabel(status) {
  return { draft: '草稿', split: '已切分', ready: '已配音' }[status] || '未开始';
}

/** 绝对路径转工程内相对路径 */
function relToProject(target) {
  const root = String(state.path || '').replace(/\/+$/, '');
  const value = String(target || '');
  if (root && value.startsWith(`${root}/`)) return value.slice(root.length + 1);
  // macOS 的 /tmp 与 /private/tmp 指向同一目录，前缀可能不同：退回到 renders/ 之后
  const at = value.indexOf('/renders/');
  return at >= 0 ? value.slice(at + 1) : value;
}

/** 执行本章混音，返回结果 */
async function runChapterMix(chapter) {
  const result = await postJSON('/api/ui/chapter/mix', { path: state.path, chapter: chapter.id });
  pushNotice('混音完成', `${chapter.title || chapter.id}${result?.duration_ms ? ` · ${Math.round(result.duration_ms / 1000)}s` : ''}`);
  return result;
}

/** 章节行「⋯」菜单（批量操作 / 混音 / 下载 / 工单 / 信息 / 重命名 / 删除） */
function openChapterMenu(chapterId, anchor) {
  const chapter = chapterOf(chapterId);
  if (!chapter) return;
  openMenu(
    anchor,
    [
      { key: 'batch', label: '批量操作' },
      { key: 'mix', label: '混音合成' },
      { key: 'download', label: '下载音频…' },
      { key: 'jobs', label: '查看工单' },
      { key: 'info', label: '章节信息' },
      { key: 'rename', label: '重命名' },
      { key: 'delete', label: '删除章节', danger: true },
    ],
    (key) => {
      if (key === 'batch') {
        setChapterBatch(true);
        chapterSelected.add(chapterId);
        repaintChapterCtx();
        return;
      }
      if (key === 'mix') {
        toast('正在混音…', 'info', 1500);
        runChapterMix(chapter)
          .then(() => toast('混音完成，可从「下载音频」获取', 'ok'))
          .catch((error) => toast(`混音失败：${error.message}`, 'bad', 4000));
        return;
      }
      if (key === 'download') return openChapterDownload(chapter);
      if (key === 'jobs') {
        location.hash = `#/p/${encodeURIComponent(state.path)}/tasks`;
        return;
      }
      if (key === 'info') return openChapterInfo(chapter);
      if (key === 'rename') return openChapterRename(chapter);
      if (key === 'delete') confirmChapterDelete(chapter);
    }
  );
}

/** 下载音频弹窗：整章混音或去导出页 */
function openChapterDownload(chapter) {
  const mask = openDialog({
    title: `下载音频 · ${chapter.title || '本章'}`,
    body: `<div class="dlg-note">整章混音会先执行分轨、混音与响度规整，完成后直接下载 WAV；
      分轨文件与工程包在导出页。</div>`,
    foot: `<div class="right"><button class="btn" data-dlg-close>取消</button>
      <button class="btn" id="dl-stems">分轨与工程包</button>
      <button class="btn primary" id="dl-mix">整章混音（WAV）</button></div>`,
  });
  mask.querySelector('#dl-stems').addEventListener('click', () => {
    closeDialog();
    location.hash = `#/p/${encodeURIComponent(state.path)}/export/${encodeURIComponent(chapter.id)}`;
  });
  mask.querySelector('#dl-mix').addEventListener('click', async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    button.textContent = '混音中…';
    try {
      const result = await runChapterMix(chapter);
      const rel = relToProject(result?.normalized?.path || result?.mix?.path || '');
      if (!rel) throw new Error('没有返回混音文件路径');
      const link = document.createElement('a');
      link.href = fileUrl(rel, state.path);
      link.download = `${chapter.title || chapter.id}.wav`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      closeDialog();
      toast('已开始下载整章混音', 'ok');
    } catch (error) {
      toast(`下载失败：${error.message}`, 'bad', 4000);
      button.disabled = false;
      button.textContent = '整章混音（WAV）';
    }
  });
}

/** 章节信息弹窗 */
function openChapterInfo(chapter) {
  openDialog({
    title: `章节信息 · ${chapter.title || '本章'}`,
    body: `<div class="summary-line"><span>章节 ID</span><span class="right mono">${esc(chapter.id)}</span></div>
      <div class="summary-line"><span>序号</span><span class="right">第 ${esc(chapter.index ?? '—')} 章</span></div>
      <div class="summary-line"><span>状态</span><span class="right">${esc(chapterStatusLabel(chapter.status))}</span></div>
      <div class="summary-line"><span>字数</span><span class="right">${chapter.word_count ? `${chapter.word_count} 字` : '未写'}</span></div>
      <div class="summary-line"><span>场景</span><span class="right">${chapter.scene_count ?? '—'} 个</span></div>`,
    foot: `<div class="right"><button class="btn primary" data-dlg-close>关闭</button></div>`,
  });
}

/** 重命名章节 */
function openChapterRename(chapter) {
  const mask = openDialog({
    title: '重命名章节',
    body: `<div class="field"><label>章节标题</label>
      <input class="input" id="cr-title" value="${esc(chapter.title || '')}" /></div>`,
    foot: `<div class="right"><button class="btn" data-dlg-close>取消</button>
      <button class="btn primary" id="cr-ok">保存</button></div>`,
  });
  const input = mask.querySelector('#cr-title');
  input.focus();
  input.select();
  mask.querySelector('#cr-ok').addEventListener('click', async (event) => {
    const title = input.value.trim();
    if (!title) return toast('章节标题不能为空', 'warn');
    event.currentTarget.disabled = true;
    try {
      await postJSON('/api/ui/chapter/rename', { path: state.path, chapter: chapter.id, title });
      chapter.title = title;
      closeDialog();
      repaintChapterCtx();
      notifyChaptersChanged(chapter.id);
      toast('章节已重命名', 'ok');
    } catch (error) {
      toast(error.message, 'bad');
      event.currentTarget.disabled = false;
    }
  });
}

/** 删除章节（二次确认，至少保留一章） */
function confirmChapterDelete(chapter) {
  const mask = openDialog({
    title: '删除章节',
    body: `<div class="dlg-note warn">会删除「${esc(chapter.title || '本章')}」的正文、台词、时间轴与脚本；
      已生成的配音文件保留在磁盘上，但不再被引用。</div>`,
    foot: `<div class="right"><button class="btn" data-dlg-close>取消</button>
      <button class="btn danger" id="cd-ok">删除章节</button></div>`,
  });
  mask.querySelector('#cd-ok').addEventListener('click', async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    button.textContent = '删除中…';
    try {
      await deleteChapter(chapter.id);
      closeDialog();
    } catch (error) {
      toast(`删除失败：${error.message}`, 'bad', 4000);
      button.disabled = false;
      button.textContent = '删除章节';
    }
  });
}

/** 删除单个章节并刷新目录；删的是当前章时跳到剩余第一章 */
async function deleteChapter(chapterId) {
  // 删除需要同时清理大纲条目与派生物，给足超时，避免界面无限等待
  await postJSON('/api/ui/chapter/delete', { path: state.path, chapter: chapterId }, { timeout: 30000 });
  const wasCurrent = chapterId === state.chapterId;
  await reloadChapters();
  notifyChaptersChanged(chapterId);
  toast('章节已删除', 'ok');
  if (!wasCurrent) return;
  if (state.chapters.length) {
    goChapter(state.path, state.chapters[0].id);
    return;
  }
  // 章节被删空：清掉当前选中，回到小说视图，避免保留悬空引用
  state.chapterId = '';
  location.hash = `#/p/${encodeURIComponent(state.path)}/novel`;
}

/** 批量操作条上的动作 */
async function onChapterBatch(act) {
  if (act === 'chapter-batch-cancel') {
    setChapterBatch(false);
    repaintChapterCtx();
    return;
  }
  if (act === 'chapter-batch-all') {
    const ids = sortedChapters(state.chapterSearch).map((chapter) => chapter.id);
    const allOn = ids.length > 0 && ids.every((id) => chapterSelected.has(id));
    chapterSelected.clear();
    if (!allOn) ids.forEach((id) => chapterSelected.add(id));
    repaintChapterCtx();
    return;
  }
  const ids = [...chapterSelected];
  if (!ids.length) return toast('先勾选章节', 'warn');
  if (act === 'chapter-batch-delete') {
    const mask = openDialog({
      title: '批量删除章节',
      body: `<div class="dlg-note warn">将删除选中的 ${ids.length} 章（正文、台词、时间轴与脚本）。
        至少需要保留一章；已生成的配音文件不从磁盘删除。</div>`,
      foot: `<div class="right"><button class="btn" data-dlg-close>取消</button>
        <button class="btn danger" id="cb-del">删除 ${ids.length} 章</button></div>`,
    });
    mask.querySelector('#cb-del').addEventListener('click', async (event) => {
      const button = event.currentTarget;
      button.disabled = true;
      let done = 0;
      for (const id of ids) {
        button.textContent = `删除中 ${done + 1}/${ids.length}`;
        try {
          await postJSON('/api/ui/chapter/delete', { path: state.path, chapter: id }, { timeout: 30000 });
          done += 1;
        } catch (error) {
          toast(`${chapterOf(id)?.title || id}：${error.message}`, 'warn', 4000);
        }
      }
      const deletedCurrent = ids.includes(state.chapterId);
      setChapterBatch(false);
      await reloadChapters();
      notifyChaptersChanged(state.chapterId);
      closeDialog();
      toast(`已删除 ${done} 章`, done ? 'ok' : 'warn');
      if (deletedCurrent && state.chapters.length) goChapter(state.path, state.chapters[0].id);
    });
    return;
  }
  if (act === 'chapter-batch-resplit') {
    const mask = openDialog({
      title: '批量重新切分',
      body: `<div class="dlg-note">对选中的 ${ids.length} 章按当前正文重新切分场景与台词。
        会保留人工改过的台词与已有配音（与单章「重新切分本章」一致）。</div>`,
      foot: `<div class="right"><button class="btn" data-dlg-close>取消</button>
        <button class="btn primary" id="cb-split">开始切分</button></div>`,
    });
    mask.querySelector('#cb-split').addEventListener('click', async (event) => {
      const button = event.currentTarget;
      button.disabled = true;
      let done = 0;
      for (const id of ids) {
        button.textContent = `切分中 ${done + 1}/${ids.length}`;
        try {
          await postJSON('/api/ui/scene/split', { path: state.path, chapter: id, force: true, keep_lines: true });
          await postJSON('/api/ui/line/split', { path: state.path, chapter: id, force: true, keep_manual: true });
          done += 1;
        } catch (error) {
          toast(`${chapterOf(id)?.title || id}：${error.message}`, 'warn', 4000);
        }
      }
      setChapterBatch(false);
      await reloadChapters();
      notifyChaptersChanged(state.chapterId);
      closeDialog();
      pushNotice('批量重新切分完成', `${done}/${ids.length} 章已重切`);
      toast(`已重新切分 ${done} 章`, done ? 'ok' : 'warn', 3000);
    });
  }
}

// 目录交互挂 document 捕获阶段：先于各页面的章节行点击处理，避免点「⋯」被当成切章
document.addEventListener(
  'click',
  (event) => {
    const menuButton = event.target.closest('[data-act="chapter-menu"]');
    if (menuButton) {
      event.preventDefault();
      event.stopPropagation();
      openChapterMenu(menuButton.dataset.chapter, menuButton);
      return;
    }
    const staleButton = event.target.closest('[data-act="chapter-stale"]');
    if (staleButton) {
      event.preventDefault();
      event.stopPropagation();
      openChapterStale(staleButton.dataset.chapter);
      return;
    }
    const sortButton = event.target.closest('[data-act="chapter-sort"]');
    if (sortButton) {
      event.preventDefault();
      event.stopPropagation();
      openChapterSort();
      return;
    }
    const batchButton = event.target.closest('[data-act^="chapter-batch"]');
    if (batchButton) {
      event.preventDefault();
      event.stopPropagation();
      onChapterBatch(batchButton.dataset.act);
      return;
    }
    if (event.target.closest('.chap-check')) {
      event.stopPropagation();
      return;
    }
    const row = event.target.closest('.chap-row[data-chapter]');
    if (row && chapterView.batch) {
      event.preventDefault();
      event.stopPropagation();
      toggleChapterPick(row.dataset.chapter);
    }
  },
  true
);

// 批量模式的勾选框
document.addEventListener('change', (event) => {
  if (!event.target.classList?.contains('chap-check')) return;
  const row = event.target.closest('.chap-row[data-chapter]');
  if (!row) return;
  if (event.target.checked) chapterSelected.add(row.dataset.chapter);
  else chapterSelected.delete(row.dataset.chapter);
  repaintChapterCtx();
});

// 目录三组筛选：全部场景 / 全部角色 / 全部状态
document.addEventListener('change', (event) => {
  const select = event.target.closest?.('[data-chap-filter]');
  if (!select) return;
  chapterFilters[select.dataset.chapFilter] = select.value;
  repaintChapterCtx();
});

// ---------------- 全局搜索 ----------------

/** 全局搜索运行时：计时器、请求序号、可键盘选择的结果行、当前高亮 */
const globalSearch = { timer: null, seq: 0, rows: [], active: -1, scope: 'all' };

/** 台词行状态 → 中文标签 */
const GS_STATE_LABEL = { ready: '已配', pending: '待配', stale: '已过期', failed: '失败', unknown: '未识别' };

/**
 * 打开全局搜索（⌘/Ctrl + K）。
 * 跨工程搜索章节标题、正文与台词行；选中后直达章节或具体台词行。
 */
export function openGlobalSearch() {
  globalSearch.rows = [];
  globalSearch.active = -1;
  openDialog({
    title: '全局搜索',
    wide: true,
    body: `
      <div class="gs-top">
        <div class="search gs-search">
          ${icon('search', 14)}
          <input id="gs-input" type="search" autocomplete="off"
            placeholder="搜索章节标题、正文与台词行（至少 2 个字）" />
        </div>
        <div class="gs-scope" id="gs-scope">
          <button class="chip ${globalSearch.scope === 'all' ? 'on' : ''}" data-gs-scope="all">全部工程</button>
          <button class="chip ${globalSearch.scope === 'current' ? 'on' : ''}" data-gs-scope="current"
            ${state.path ? '' : 'disabled'}>仅当前工程</button>
        </div>
      </div>
      <div class="gs-meta" id="gs-meta">⌘K 打开 · ↑↓ 选择 · Enter 打开 · Esc 关闭</div>
      <div class="gs-results" id="gs-results">
        <div class="empty"><b>输入至少 2 个字开始搜索</b><span>会同时搜索章节标题、正文与台词行</span></div>
      </div>`,
    foot: `<div class="right"><button class="btn" data-dlg-close>关闭</button></div>`,
    onMount: (node) => {
      const input = node.querySelector('#gs-input');
      input?.focus();
      input?.addEventListener('input', () => scheduleGlobalSearch(input.value));
      input?.addEventListener('keydown', (event) => {
        if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
          event.preventDefault();
          moveGlobalCursor(event.key === 'ArrowDown' ? 1 : -1);
        } else if (event.key === 'Enter') {
          event.preventDefault();
          const row = globalSearch.rows[globalSearch.active];
          if (row) gsJump(row);
        }
      });
      node.querySelector('#gs-scope')?.addEventListener('click', (event) => {
        const button = event.target.closest('[data-gs-scope]');
        if (!button || button.disabled) return;
        globalSearch.scope = button.dataset.gsScope;
        node.querySelectorAll('[data-gs-scope]').forEach((item) => {
          item.classList.toggle('on', item.dataset.gsScope === globalSearch.scope);
        });
        scheduleGlobalSearch(input?.value || '');
      });
      node.querySelector('#gs-results')?.addEventListener('click', (event) => {
        const button = event.target.closest('[data-gs-index]');
        if (!button) return;
        const row = globalSearch.rows[Number(button.dataset.gsIndex)];
        if (row) gsJump(row);
      });
    },
  });
}

/**
 * 防抖搜索：至少 2 个字，280ms 后请求 `/api/ui/search`。
 * 用请求序号丢弃过期响应，避免快速输入时结果倒序。
 * @param {string} value 输入框内容
 */
function scheduleGlobalSearch(value) {
  const keyword = String(value || '').trim();
  const seq = ++globalSearch.seq;
  window.clearTimeout(globalSearch.timer);
  const host = document.getElementById('gs-results');
  const meta = document.getElementById('gs-meta');
  if (keyword.length < 2) {
    globalSearch.rows = [];
    globalSearch.active = -1;
    if (host) {
      host.innerHTML = '<div class="empty"><b>输入至少 2 个字开始搜索</b><span>会同时搜索章节标题、正文与台词行</span></div>';
    }
    if (meta) meta.textContent = '⌘K 打开 · ↑↓ 选择 · Enter 打开 · Esc 关闭';
    return;
  }
  if (host) host.innerHTML = '<div class="empty"><b>搜索中…</b><span>正在读取工程数据</span></div>';
  if (meta) meta.textContent = '搜索中…';
  globalSearch.timer = window.setTimeout(async () => {
    try {
      const data = await getJSON('/api/ui/search', {
        q: keyword,
        path: globalSearch.scope === 'current' ? state.path : undefined,
        limit: 40,
      });
      if (seq !== globalSearch.seq) return;
      renderGlobalResults(data);
    } catch (error) {
      if (seq !== globalSearch.seq) return;
      if (host) host.innerHTML = `<div class="empty"><b>搜索失败</b><span>${esc(error.message)}</span></div>`;
      if (meta) meta.textContent = '搜索失败';
    }
  }, 280);
}

/**
 * 渲染全局搜索结果：按工程分组，工程内再分「章节 / 台词行」。
 * @param {object} data `/api/ui/search` 返回值
 */
function renderGlobalResults(data) {
  const host = document.getElementById('gs-results');
  const meta = document.getElementById('gs-meta');
  if (!host) return;
  const chapters = data?.chapters || [];
  const lines = data?.lines || [];
  globalSearch.rows = [];
  globalSearch.active = -1;
  if (!chapters.length && !lines.length) {
    host.innerHTML = `<div class="empty"><b>没有匹配结果</b><span>换个关键词试试${data?.q ? `：${esc(data.q)}` : ''}</span></div>`;
    if (meta) meta.textContent = '没有匹配结果';
    return;
  }
  const groups = new Map();
  for (const item of chapters.concat(lines)) {
    if (!groups.has(item.project)) {
      groups.set(item.project, { title: item.project_title, chapters: [], lines: [] });
    }
  }
  for (const item of chapters) groups.get(item.project).chapters.push(item);
  for (const item of lines) groups.get(item.project).lines.push(item);
  let html = '';
  for (const [project, group] of groups) {
    html += `<div class="gs-project">
      <div class="gs-project-head"><b>${esc(group.title || '未命名工程')}</b>
        <span class="mono">${esc(project)}</span></div>`;
    if (group.chapters.length) {
      html += '<div class="gs-group-title">章节</div>';
      for (const item of group.chapters) {
        const index = globalSearch.rows.length;
        globalSearch.rows.push({ project, chapter: item.chapter_id, line: '' });
        html += `<button class="gs-row" data-gs-index="${index}">
          <span class="gs-row-main">
            <b>第 ${esc(item.chapter_index ?? '—')} 章 · ${esc(item.chapter_title || '未命名章节')}</b>
            <span class="gs-snippet">${esc(item.snippet || '标题命中')}</span>
          </span>
          <span class="badge mute">${item.count ? `正文 ${item.count}` : '标题'}</span>
        </button>`;
      }
    }
    if (group.lines.length) {
      html += '<div class="gs-group-title">台词行</div>';
      for (const item of group.lines) {
        const index = globalSearch.rows.length;
        globalSearch.rows.push({ project, chapter: item.chapter_id, line: item.line_id });
        html += `<button class="gs-row" data-gs-index="${index}">
          <span class="gs-row-main">
            <b>${esc(item.speaker || '未知')}</b>
            <span class="gs-text">${esc(item.text)}</span>
            <span class="gs-snippet">${esc(item.chapter_title || '')}${item.scene_title ? ` · ${esc(item.scene_title)}` : ''}</span>
          </span>
          <span class="badge ${item.state === 'failed' ? 'bad' : item.state === 'stale' ? 'warn' : 'mute'}">${
            GS_STATE_LABEL[item.state] || esc(item.state)
          }</span>
        </button>`;
      }
    }
    html += '</div>';
  }
  const counts = data?.counts || {};
  if (data?.truncated) {
    html += '<div class="hint" style="padding:8px 2px 0">结果达到上限，已截断；输入更精确的关键词可缩小范围。</div>';
  }
  host.innerHTML = html;
  if (meta) {
    meta.textContent = `${counts.projects || groups.size} 个工程 · 章节 ${counts.chapters || 0} · 台词 ${counts.lines || 0}`;
  }
}

/**
 * 键盘游标：↑↓ 在结果行间循环移动。
 * @param {number} delta +1 向下 / -1 向上
 */
function moveGlobalCursor(delta) {
  const host = document.getElementById('gs-results');
  const nodes = host ? [...host.querySelectorAll('.gs-row')] : [];
  if (!nodes.length) return;
  globalSearch.active = (globalSearch.active + delta + nodes.length) % nodes.length;
  nodes.forEach((node, index) => node.classList.toggle('on', index === globalSearch.active));
  nodes[globalSearch.active].scrollIntoView({ block: 'nearest' });
}

/**
 * 打开某条搜索结果：跳转到对应章节，台词行带 `?line=` 直达行定位。
 * @param {{project:string, chapter:string, line:string}} target 搜索目标
 */
function gsJump({ project, chapter, line }) {
  if (!project || !chapter) return;
  closeDialog();
  rememberProject(project);
  const base = `#/p/${encodeURIComponent(project)}/c/${encodeURIComponent(chapter)}`;
  const target = line ? `${base}?line=${encodeURIComponent(line)}` : base;
  if (location.hash === target) {
    // 已经在该章：hash 不变不会触发路由，直接通知编辑页定位
    if (line) document.dispatchEvent(new CustomEvent('ndw:focus-line', { detail: { lineId: line } }));
    return;
  }
  location.hash = target;
}

// 图标栏「搜索」按钮
document.addEventListener('click', (event) => {
  if (!event.target.closest('[data-act="global-search"]')) return;
  event.preventDefault();
  openGlobalSearch();
});

// ---------------- 通知与快捷键 ----------------

/** 本地通知（混音、对轨、批量切分等任务事件） */
const notices = [];

/**
 * 追加一条本地通知。
 * @param {string} title 标题
 * @param {string} [detail] 明细
 */
export function pushNotice(title, detail = '') {
  notices.unshift({
    title,
    detail,
    at: new Date().toLocaleTimeString('zh-CN', { hour12: false }),
  });
  if (notices.length > 50) notices.length = 50;
}

/** 通知面板：空态与参考一致，同时承接本地任务事件 */
function openNotices() {
  openDialog({
    title: '通知',
    body: notices.length
      ? `<div class="notice-list">${notices
          .map(
            (item) => `<div class="notice-row">
              <div class="nr-main"><b>${esc(item.title)}</b>${item.detail ? `<span>${esc(item.detail)}</span>` : ''}</div>
              <span class="nr-time">${esc(item.at)}</span>
            </div>`
          )
          .join('')}</div>`
      : `<div class="empty"><b>暂无通知</b><span>混音、智能对轨、批量切分等任务完成后会出现在这里</span></div>`,
    foot: `<div class="right"><button class="btn" data-dlg-close>关闭</button></div>`,
  });
}

/** 全局快捷键面板（与参考的「查看更多快捷键」合并为统一入口） */
export function openShortcuts() {
  const groups = [
    ['全局', [['⌘/Ctrl + K', '全局搜索'], ['?', '打开快捷键面板'], ['Alt + T', '打开通知'], ['⌘/Ctrl + Z', '撤销'], ['⇧ + ⌘/Ctrl + Z', '重做'], ['⌘/Ctrl + F', '查找替换']]],
    ['录音', [['空格', '开始 / 停止录音'], ['↓', '提交并续录下一行'], ['R', '重录当前行']]],
    ['后期制作', [['空格', '播放 / 暂停']]],
  ];
  openDialog({
    title: '快捷键',
    body: groups
      .map(
        ([name, rows]) => `<div class="sc-group"><div class="sc-title">${esc(name)}</div>${rows
          .map(([key, desc]) => `<div class="sc-row"><kbd>${esc(key)}</kbd><span>${esc(desc)}</span></div>`)
          .join('')}</div>`
      )
      .join(''),
    foot: `<div class="right"><button class="btn primary" data-dlg-close>知道了</button></div>`,
  });
}

// 全局键盘：⌘/Ctrl+K 全局搜索、Alt+T 通知、? 快捷键；输入框内不拦截
document.addEventListener('keydown', (event) => {
  const typing = event.target.closest?.('input, textarea, select, [contenteditable="true"]');
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
    event.preventDefault();
    openGlobalSearch();
    return;
  }
  if (event.altKey && event.key.toLowerCase() === 't') {
    event.preventDefault();
    openNotices();
    return;
  }
  if (event.key === '?' && !typing && !event.metaKey && !event.ctrlKey && !event.altKey) {
    event.preventDefault();
    openShortcuts();
  }
});

// ---------------- 正文 / 切分不一致 ----------------

/** 重新切分完成后的回调（由当前页面注册，用于刷新自己的数据） */
let resplitHandler = null;
/** 保留式重切后仍对不上的章节：用于在提示条里追加「按规则重建」的第二步出口 */
let keepFailedFor = '';

/**
 * 注册「重新切分本章」完成后的刷新回调。
 * @param {(() => void)|null} fn 回调；页面离开时传 null 清理
 */
export function onResplit(fn) {
  resplitHandler = fn;
}

/**
 * 只读检查某章正文与切分是否一致。
 * @param {string} path 工程目录
 * @param {string} chapterId 章节 ID
 * @returns {Promise<object|null>} 检查结果；接口异常时返回 null（页面不误报）
 */
export async function checkConsistency(path, chapterId) {
  if (!path || !chapterId) return null;
  try {
    return await getJSON('/api/ui/chapter/consistency', { path, chapter: chapterId });
  } catch {
    return null;
  }
}

/**
 * 正文与切分不一致时的统一提示条。
 * 说明是哪一章、会影响哪些操作，并提供保留人工改动的「重新切分本章」。
 * @param {object|null} consistency `checkConsistency` / `lines` 接口返回的 consistency
 * @returns {string} HTML；一致或未检查时返回空串
 */
export function resplitBannerHTML(consistency) {
  if (!consistency || consistency.ok !== false) return '';
  const title = consistency.chapter_title || '当前章节';
  const missing = Number(consistency.missing_total || 0);
  const escalated = keepFailedFor === consistency.chapter;
  return `
    <div class="drift-bar" role="alert">
      ${icon('warn', 16)}
      <div class="db-main">
        <b>「${esc(title)}」的正文与切分台词已不一致</b>
        <span>${
          escalated
            ? `保留人工改动重切后仍有 ${missing} 行对不上：这些是人改写/新增、但正文里已找不到原句的行。按规则重建会按正文重新切一遍，这些行的配音文件不会被删除，但会失去引用。`
            : `有 ${missing} 行在正文里定位不到。改台词文本、查找替换这类「同步回正文」的操作会被拒绝；
          改说话人 / 情绪 / 重新生成配音不受影响。重新切分会保留人工改过的行与已有配音。`
        }</span>
      </div>
      ${
        escalated
          ? '<button class="btn sm" data-act="rebuild-lines">按规则重建台词…</button>'
          : ''
      }
      <button class="btn primary sm" data-act="resplit-chapter">重新切分本章</button>
    </div>`;
}

/**
 * 执行「保留人工改动」的重新切分。
 * 第一步重切场景（--keep-lines：正文未变的场景连同台词与配音整块沿用），
 * 第二步重切台词（--keep-manual：同文本旧行复用，人工改写/新增的行按原位置插回）。
 * 若两步后仍有对不上的行，提示条追加「按规则重建台词」的第二步出口。
 * @param {HTMLElement} button 触发按钮
 */
async function runResplit(button) {
  const path = state.path;
  const chapterId = state.chapterId;
  if (!path || !chapterId) return;
  const label = button.textContent;
  button.disabled = true;
  button.textContent = '切分中…';
  try {
    await postJSON('/api/ui/scene/split', { path, chapter: chapterId, force: true, keep_lines: true });
    const result = await postJSON('/api/ui/line/split', {
      path,
      chapter: chapterId,
      force: true,
      keep_manual: true,
    });
    const after = await checkConsistency(path, chapterId);
    keepFailedFor = after?.ok === false ? chapterId : '';
    if (keepFailedFor) {
      toast(
        `已重新切分，但仍有 ${Number(after?.missing_total || 0)} 行对不上，可继续「按规则重建台词」`,
        'warn'
      );
    } else {
      toast(
        `已重新切分：${Number(result?.scenes || 0)} 个场景 / ${Number(result?.lines || 0)} 行，人工改动与配音已保留`,
        'ok'
      );
    }
    await resplitHandler?.();
  } catch (error) {
    toast(error.message, 'bad');
  } finally {
    button.disabled = false;
    button.textContent = label;
  }
}

/**
 * 按规则重建台词（保留式重切仍对不上时的第二步）。
 * 先二次确认：丢失的只是人工改写/新增行的引用，音频文件不删除。
 * @param {HTMLElement} button 触发按钮
 */
function confirmRebuild(button) {
  const chapterId = state.chapterId;
  const mask = openDialog({
    title: '按规则重建台词',
    body: `<div class="dlg-note warn">会按当前正文重新切分台词。与规则结果对不上的人工改写 / 新增行会失去引用
      （音频文件仍保留在磁盘上，可在版本记录里找回），重新生成的台词行需要重新配音。</div>
      <div class="summary-line"><span>范围</span><span class="right">当前章节</span></div>`,
    foot: `<div class="right"><button class="btn" data-dlg-close>取消</button>
      <button class="btn danger" id="rebuild-ok">按规则重建</button></div>`,
  });
  mask.querySelector('#rebuild-ok').addEventListener('click', async (event) => {
    event.currentTarget.disabled = true;
    event.currentTarget.textContent = '重建中…';
    try {
      const result = await postJSON('/api/ui/line/split', {
        path: state.path,
        chapter: chapterId,
        force: true,
      });
      const after = await checkConsistency(state.path, chapterId);
      keepFailedFor = after?.ok === false ? chapterId : '';
      closeDialog();
      toast(`已按规则重建：${Number(result?.lines || 0)} 行`, 'ok');
      await resplitHandler?.();
    } catch (error) {
      toast(error.message, 'bad');
      event.currentTarget.disabled = false;
      event.currentTarget.textContent = '按规则重建';
    }
  });
  button.blur();
}

// 与目录收起一样挂 document：编辑页 / 任务页 / 配音页共用同一个出口
document.addEventListener('click', (event) => {
  const resplit = event.target.closest('[data-act="resplit-chapter"]');
  if (resplit) {
    runResplit(resplit);
    return;
  }
  const rebuild = event.target.closest('[data-act="rebuild-lines"]');
  if (rebuild) confirmRebuild(rebuild);
});

// ---------------- 导航 ----------------

/**
 * 目录收起/展开：目录面板头部的「⇤」与折叠后的窄条共用。
 * 挂 document，避免每个页面的根节点各自实现一遍。
 */
document.addEventListener('click', (event) => {
  if (!event.target.closest('[data-act="toggle-ctx"]')) return;
  const studio = document.getElementById('studio') || document.querySelector('.studio');
  studio?.classList.toggle('ctx-hidden');
});

/** 跳到工程列表 */
export function goProjects() {
  location.hash = '#/projects';
}

/** 跳到某工程的某章；工程还没有章节时留在当前页给引导，不回列表 */
export function goChapter(path, chapterId) {
  if (!path || !chapterId) {
    if (path) showNeedChapter(path);
    else goProjects();
    return;
  }
  location.hash = `#/p/${encodeURIComponent(path)}/c/${encodeURIComponent(chapterId)}`;
}

/** 跳到导出页；没有章节时同样给引导，不落到空选择页 */
export function goExport(path, chapterId) {
  if (!path || !chapterId) {
    if (path) showNeedChapter(path);
    else goProjects();
    return;
  }
  location.hash = `#/p/${encodeURIComponent(path)}/export/${encodeURIComponent(chapterId)}`;
}

/** 路由参数解码 */
export function decodePath(raw) {
  try {
    return decodeURIComponent(raw || '');
  } catch {
    return raw || '';
  }
}

/**
 * 「切换工程」弹窗：列出本机工程并跳转。
 * 以列表形态出现，避免用户为了切工程先退回列表页。
 */
export async function openSwitchProject() {
  const projects = await getJSON('/api/projects');
  const recent = lastProject();
  const body = `
    <div class="hint">选择要打开的工程；未列出的工程可在工程列表页从目录新建。</div>
    <div style="display:flex;flex-direction:column;gap:6px;max-height:340px;overflow:auto">
      ${
        (projects || [])
          .map(
            (item) => `
        <button class="btn" style="height:auto;padding:10px 12px;justify-content:flex-start;text-align:left"
                data-open-project="${esc(item.path)}">
          <span style="display:flex;flex-direction:column;gap:3px;min-width:0">
            <b style="font-size:13px;color:var(--text)">${esc(item.title || '未命名工程')}</b>
            <span class="mono" style="font-size:10.5px;color:var(--text-4);overflow:hidden;text-overflow:ellipsis">${esc(item.path)}</span>
          </span>
          ${item.path === recent ? '<span class="badge mute" style="margin-left:auto">上次</span>' : ''}
        </button>`
          )
          .join('') || '<div class="hint">本机还没有工程</div>'
      }
    </div>`;
  const mask = openDialog({
    title: '切换工程',
    body,
    foot: `<div class="right"><button class="btn" data-dlg-close>取消</button>
      <button class="btn primary" data-goto-projects>去工程列表</button></div>`,
  });
  mask.addEventListener('click', (event) => {
    const button = event.target.closest('[data-open-project]');
    if (button) {
      closeDialog();
      rememberProject(button.dataset.openProject);
      location.hash = '#/projects';
      // 工程列表页会读取上次工程并自动进入第一章；这里直接跳章节更接近参考的两步内切换
      getJSON('/api/project/chapters', { path: button.dataset.openProject })
        .then((chapters) => {
          if (chapters && chapters.length) {
            goChapter(button.dataset.openProject, chapters[0].id);
          }
        })
        .catch(() => {});
      return;
    }
    if (event.target.closest('[data-goto-projects]')) {
      closeDialog();
      goProjects();
    }
  });
}

// ---------------- 空工程引导 / 导入正文 ----------------

/**
 * 导入已有正文：选 txt / md 文件或直接粘贴，按标题切章写入。
 * 供工程列表和各视图的空工程引导共用，成功后默认进入第一章。
 * @param {{path: string, title?: string, onDone?: (result: object) => Promise<void>|void}} options
 */
export function openImportDialog({ path, title = '', onDone } = {}) {
  if (!path) return;
  const mask = openDialog({
    title: title ? `导入正文 · ${title}` : '导入正文',
    body: `
      <div class="field"><label>选择 txt / md 文件</label>
        <input type="file" id="imp-file" accept=".txt,.md,text/plain,text/markdown" /></div>
      <div class="field"><label>标题匹配规则</label>
        <input class="input" id="imp-pattern" value="^#{1,3}\\s+(.+)$" /></div>
      <div class="field"><label>重复章节</label>
        <select class="select" id="imp-dup">
          <option value="skip">跳过（默认）</option>
          <option value="replace">覆盖正文</option>
          <option value="new">再建一份</option>
        </select></div>
      <div class="field"><label>正文（选文件后自动填入，可直接编辑）</label>
        <textarea class="textarea" id="imp-text" rows="8" placeholder="粘贴正文，或用上面的文件选择"></textarea></div>
      <div class="hint">导入会按标题切章写入章节文件；纯文本无标题时整篇作为一章。</div>`,
    foot: `<div class="right"><button class="btn" data-dlg-close>取消</button>
      <button class="btn primary" id="imp-ok">开始导入</button></div>`,
  });
  mask.querySelector('#imp-file').addEventListener('change', async (event) => {
    const file = event.target.files?.[0];
    if (!file) return;
    try {
      mask.querySelector('#imp-text').value = await file.text();
    } catch (error) {
      toast(`读取失败：${error.message}`, 'bad');
    }
  });
  mask.querySelector('#imp-ok').addEventListener('click', async (event) => {
    const text = mask.querySelector('#imp-text').value;
    if (!text.trim()) return toast('正文为空', 'warn');
    const button = event.currentTarget;
    button.disabled = true;
    button.textContent = '导入中…';
    try {
      const result = await postJSON('/api/ui/chapter/import', {
        path,
        text,
        pattern: mask.querySelector('#imp-pattern').value.trim(),
        on_duplicate: mask.querySelector('#imp-dup').value,
      });
      closeDialog();
      const created = result?.created ?? result?.imported ?? result?.chapters?.length ?? 0;
      toast(created ? `已导入 ${created} 章` : '导入完成', 'ok');
      rememberProject(path);
      await onDone?.(result);
      const chapters = await getJSON('/api/project/chapters', { path }).catch(() => []);
      if (chapters?.length) goChapter(path, chapters[0].id);
    } catch (error) {
      toast(`导入失败：${error.message}`, 'bad');
      button.disabled = false;
      button.textContent = '开始导入';
    }
  });
}

/**
 * 无章节时的统一引导：不跳空的章节选择页，在当前页给出「写小说 / 导入正文」。
 * 页面上已经有空工程引导时只滚动定位并提示，不重复插入。
 * @param {string} [path] 工程目录，默认当前工程
 */
export async function showNeedChapter(path = state.path) {
  if (!path) {
    goProjects();
    return;
  }
  // 图标栏可能先于页面数据渲染：先确认这个工程到底有没有章节
  if (!state.chapters.length) {
    try {
      const chapters = await getJSON('/api/ui/chapters', { path });
      if (Array.isArray(chapters) && chapters.length) {
        state.chapters = chapters;
        state.chapterId = state.chapterId || chapters[0].id;
        goChapter(path, state.chapterId);
        return;
      }
    } catch {
      // 读取失败按「没有章节」处理，下面给写小说 / 导入两条入口
    }
  }
  if (state.path !== path) return;
  const existing = document.querySelector('.project-empty, .project-empty-banner');
  if (existing) {
    existing.scrollIntoView({ behavior: 'smooth', block: 'center' });
    toast('这个工程还没有章节：先在小说视图写正文，或导入已有正文', 'warn', 4200);
    return;
  }
  document.querySelector('.need-chapter-bar')?.remove();
  const bar = document.createElement('div');
  bar.className = 'need-chapter-bar';
  bar.innerHTML = emptyProjectHTML(path, { compact: true });
  const host = document.getElementById('app') || document.body;
  const topbar = host.querySelector('.topbar');
  if (topbar) topbar.after(bar);
  else host.prepend(bar);
  bar.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  toast('这个工程还没有章节：先在小说视图写正文，或导入已有正文', 'warn', 4200);
}

// 图标栏「目录」在无章节时改为按钮：留在当前页，给写小说 / 导入正文两条入口。
// 图标栏可能先于页面数据渲染，点击时再按已加载的章节纠正一次。
document.addEventListener('click', (event) => {
  if (!event.target.closest('[data-act="need-chapter"]')) return;
  event.preventDefault();
  const current = state.chapterId && state.chapters.some((item) => item.id === state.chapterId);
  if (current) {
    goChapter(state.path, state.chapterId);
    return;
  }
  if (state.chapters.length) {
    goChapter(state.path, state.chapters[0].id);
    return;
  }
  showNeedChapter();
});

// 任意视图的空工程引导都能直接导入正文，不必先回工程列表
document.addEventListener('click', (event) => {
  if (!event.target.closest('[data-act="empty-import"]')) return;
  event.preventDefault();
  if (state.path) openImportDialog({ path: state.path });
});

/**
 * 空工程引导：说明下一步，并给出「从写小说开始 / 导入已有正文」两条入口。
 * compact=true 时输出横幅，用于仍可正常使用的视图（如音色、小说工作台）。
 * @param {string} path 工程目录
 * @param {{compact?: boolean, title?: string}} [options]
 * @returns {string} HTML
 */
export function emptyProjectHTML(path, { compact = false, title = '' } = {}) {
  const href = `#/p/${encodeURIComponent(path)}/novel`;
  if (compact) {
    return `
      <div class="banner project-empty-banner">
        ${icon('book', 13)}
        <div class="pe-main">
          <b>这个工程还没有章节</b>
          <span>先在小说视图写设定与大纲并落地成章节，或导入已有正文；本页的配置会保留。</span>
        </div>
        <div class="right">
          <a class="btn sm" href="${href}">${icon('book', 12)} 从写小说开始</a>
          <button class="btn sm" data-act="empty-import">${icon('arrow', 12)} 导入已有正文</button>
        </div>
      </div>`;
  }
  return `
    <div class="empty project-empty">
      ${icon('book', 22)}
      <b>${esc(title || '这个工程还没有章节')}</b>
      <span>可以从零开始写小说：先写设定与世界观，再生成大纲，落地成章节；也可以直接导入已有正文。</span>
      <div class="pe-acts">
        <a class="btn primary" href="${href}">${icon('book', 13)} 从写小说开始</a>
        <button class="btn" data-act="empty-import">${icon('arrow', 13)} 导入已有正文</button>
      </div>
      <div class="hint">写小说路径：写作设定 / 世界观 → 分层大纲 → 生成正文 → 落地成章节</div>
    </div>`;
}

/** 字符数统计（供目录与标题使用） */
export { charCount };
