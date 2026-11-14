/**
 * 素材视图（V5）：按音效 / 配图 / 视频 / BGM 浏览本章已有素材。
 * 支持生成本章待生成素材（音效可逐条看进度），BGM 可手填描述或按计划生成。
 */
import { esc, icon, toast, fmtDuration, openDialog, closeDialog } from '../dom.js';
import { getJSON, postJSON, fileUrl } from '../api.js';
import { labelOf } from '../enums.js';
import { openProgress } from '../progress.js';
import { modeSwitchHTML, bindModeSwitch, getGenMode } from '../gen-mode.js';
import { state, resetProject } from '../store.js';
import { railbar, topbar, chapterCtx, goChapter, goProjects, emptyProjectHTML } from '../shell.js';

const TABS = [
  ['sfx', '音效'],
  ['image', '配图'],
  ['video', '视频'],
  ['bgm', 'BGM'],
];

const STATUS_LABEL = {
  ready: '已生成',
  pending: '待生成',
  running: '生成中',
  failed: '失败',
  stale: '已过期',
};

let payload = { sfx: [], image: [], video: [] };
/** 章节 BGM 数据（cue / 生成记录 / 计划 / 档位） */
let bgm = null;
let tab = 'sfx';
let statusFilter = 'all';
let keyword = '';
/** 浏览形态：list 列表 / grid 网格（参考素材库右上角切换） */
let layout = 'list';
let bound = false;

const TAB_LABEL = { sfx: '音效', image: '配图', video: '视频', bgm: 'BGM' };

/** 当前 tab 的条目 */
function rowsOf() {
  return payload[tab] || [];
}

/** 过滤后的条目 */
function visibleRows() {
  const word = keyword.trim().toLowerCase();
  return rowsOf().filter((item) => {
    if (statusFilter !== 'all' && item.status !== statusFilter) return false;
    if (!word) return true;
    return `${item.prompt || ''} ${item.prompt_en || ''}`.toLowerCase().includes(word);
  });
}

/** 状态徽标语义色 */
function badgeClass(name) {
  if (name === 'ready') return 'ok';
  if (name === 'failed') return 'bad';
  if (name === 'running') return 'run';
  if (name === 'stale') return 'warn';
  return 'mute';
}

/** 条目副标题 */
function metaOf(item) {
  const parts = [`场景 ${item.scene_id ? item.scene_id.slice(-6) : '—'}`, `#${item.index ?? '—'}`];
  if (item.duration_ms) parts.push(fmtDuration(item.duration_ms));
  if (item.width) parts.push(`${item.actual_width || item.width}×${item.actual_height || item.height}`);
  if (item.template) parts.push(item.template);
  return parts.join(' · ');
}

/** 条目行 */
function rowHTML(item) {
  const ready = item.status === 'ready' && item.asset_id;
  const actions = [];
  if (tab === 'sfx') {
    actions.push(`<button class="btn sm" data-act="sfx-generate" data-id="${esc(item.id || '')}"
      title="${ready ? '重新生成这段音效' : '生成这段音效（后台任务，按钮上显示进度）'}">${ready ? '重生成' : '生成'}</button>`);
  }
  if (tab === 'sfx' && ready) actions.push(`<button class="btn sm" data-act="play" data-asset="${esc(item.asset_id)}"
    title="试听这段音效">试听</button>`);
  if (tab === 'image' && ready) actions.push(`<a class="btn sm" href="${fileUrl(item.asset_id, state.path)}" target="_blank"
    rel="noopener" title="在新标签页查看原图">查看</a>`);
  if (tab === 'video' && ready) actions.push(`<a class="btn sm" href="${fileUrl(item.asset_id, state.path)}" target="_blank"
    rel="noopener" title="在新标签页播放视频">播放</a>`);
  actions.push(`<button class="btn sm" data-act="copy-path" data-asset="${esc(item.asset_id || '')}"
    ${item.asset_id ? '' : 'disabled'} title="${item.asset_id ? '复制产物相对路径到剪贴板' : '这一项还没有产物文件'}">复制路径</button>`);
  const mismatch = item.size_mismatch
    ? `<span class="badge warn" title="产物尺寸 ${item.actual_width}×${item.actual_height} 与工程画幅 ${
        item.width
      }×${item.height} 不一致">尺寸不符</span>`
    : '';
  return `
    <div class="summary-line" style="align-items:flex-start">
      <span style="min-width:0;flex:1">
        <span style="color:var(--text)">${esc(item.prompt || '(无提示词)')}</span>
        <span style="display:block;color:var(--text-4);font-size:11px;margin-top:2px">${esc(metaOf(item))}</span>
        ${item.error ? `<span style="display:block;color:var(--bad);font-size:11px;margin-top:2px">${esc(item.error)}</span>` : ''}
      </span>
      <span class="right" style="display:flex;gap:6px;align-items:center;flex:0 0 auto">
        ${mismatch}
        <span class="badge ${badgeClass(item.status)}">${esc(STATUS_LABEL[item.status] || item.status)}</span>
        ${actions.join('')}
      </span>
    </div>`;
}

/** 网格卡（配图给缩略图，音效/视频给类型块与操作） */
function cardHTML(item) {
  const ready = item.status === 'ready' && item.asset_id;
  const thumb =
    ready && tab === 'image'
      ? `<a href="${fileUrl(item.asset_id, state.path)}" target="_blank" rel="noopener" title="打开原图"
           style="display:block;height:120px;border-radius:6px;overflow:hidden;background:var(--bg-2)">
           <img src="${fileUrl(item.asset_id, state.path)}" alt="" loading="lazy"
             style="width:100%;height:100%;object-fit:cover;display:block" />
         </a>`
      : `<div style="height:120px;border-radius:6px;background:var(--bg-2);display:flex;align-items:center;
           justify-content:center;color:var(--text-4)">${icon(tab === 'sfx' ? 'audio' : 'image', 22)}</div>`;
  return `
    <div class="card" style="padding:10px;display:flex;flex-direction:column;gap:8px">
      ${thumb}
      <div style="font-size:12px;color:var(--text);min-height:32px">${esc(item.prompt || '(无提示词)')}</div>
      <div style="font-size:10.5px;color:var(--text-4)">${esc(metaOf(item))}</div>
      <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">
        <span class="badge ${badgeClass(item.status)}">${esc(STATUS_LABEL[item.status] || item.status)}</span>
        ${
          ready && tab === 'sfx'
            ? `<button class="btn sm" data-act="play" data-asset="${esc(item.asset_id)}" title="试听这段音效">试听</button>`
            : ''
        }
        ${
          ready && tab === 'image'
            ? `<a class="btn sm" href="${fileUrl(item.asset_id, state.path)}" target="_blank" rel="noopener"
                 title="在新标签页查看原图">查看</a>`
            : ''
        }
        ${
          ready && tab === 'video'
            ? `<a class="btn sm" href="${fileUrl(item.asset_id, state.path)}" target="_blank" rel="noopener"
                 title="在新标签页播放视频">播放</a>`
            : ''
        }
        ${
          tab === 'sfx'
            ? `<button class="btn sm" data-act="sfx-generate" data-id="${esc(item.id || '')}"
                 title="${ready ? '重新生成这段音效' : '生成这段音效'}">${ready ? '重生成' : '生成'}</button>`
            : ''
        }
        <button class="btn sm" data-act="copy-path" data-asset="${esc(item.asset_id || '')}"
          ${item.asset_id ? '' : 'disabled'} title="${item.asset_id ? '复制产物相对路径到剪贴板' : '这一项还没有产物文件'}">路径</button>
      </div>
    </div>`;
}

/** BGM 面板：生成表单 + 生成记录 + 已插入 cue */
function bgmHTML() {
  const data = bgm || {};
  const plans = data.plans || [];
  const modes = data.modes || {};
  const records = [...(data.records || [])].reverse();
  const cues = data.cues || [];
  const defaults = data.defaults || {};
  const providers = data.providers || [];
  const planOptions = plans
    .map(
      (plan) => `<option value="${esc(plan.id)}" data-duration="${Number(plan.duration_s || 0)}"
        data-name="${esc(plan.name)}" data-prompt-en="${esc(plan.prompt_en || '')}">${esc(plan.name)} · ${Number(
        plan.duration_s || 0
      )}s · ${esc(plan.mode_label || '')}</option>`
    )
    .join('');
  const modeOptions = Object.entries(modes)
    .map(([key, label]) => `<option value="${esc(key)}">${esc(label)}</option>`)
    .join('');
  return `
    <div class="card" style="margin-bottom:14px">
      <div class="card-head"><span class="ct">生成 BGM</span>
        <div class="right"><span class="hint">默认 ${esc(defaults.provider || 'local')} · ${
          providers
            .map((item) => `${item.label}${item.available ? '' : `（不可用：${item.detail || item.code || ''}）`}`)
            .join(' / ')
        }</span></div>
      </div>
      <div class="card-body">
        ${modeSwitchHTML('media.bgm', { fallback: 'fill' })}
        <div style="display:grid;grid-template-columns:2fr 120px 140px;gap:10px">
          <div class="field"><label>关联计划</label>
            <select class="select" id="bgm-plan"><option value="">不关联（手填描述）</option>${planOptions}</select></div>
          <div class="field"><label>时长（秒）</label>
            <input class="input" id="bgm-duration" type="number" min="5" max="300" value="40" /></div>
          <div class="field"><label>用途</label>
            <select class="select" id="bgm-mode" data-enum="bgm_mode" title="音乐用途，只能从列表选择"><option value="auto">自动判断</option>${modeOptions}</select></div>
        </div>
        <div class="field" style="margin-top:8px"><label>音乐描述（中文，留空时用计划里的英文提示）</label>
          <textarea class="textarea" id="bgm-prompt" rows="2"
            placeholder="例如：夜色下的低音弦乐与稀疏钢琴，缓慢推进，克制而紧张"></textarea></div>
        <div class="field" style="margin-top:8px"><label>英文提示词（不选计划时必填）</label>
          <input class="input" id="bgm-prompt-en"
            placeholder="例如：warm kitchen ambience, light acoustic guitar, slice of life" /></div>
        <div style="display:flex;gap:10px;align-items:center;margin-top:8px">
          <button class="btn primary" data-act="bgm-generate">生成 BGM</button>
          <span class="hint" id="bgm-hint"></span>
        </div>
      </div>
    </div>
    <div class="card" style="margin-bottom:14px">
      <div class="card-head"><span class="ct">生成记录</span>
        <div class="right"><span class="badge mute">${records.length} 条</span></div></div>
      <div class="card-body" style="padding:0">
        ${
          records
            .map(
              (item) => `
          <div class="summary-line">
            <span style="min-width:0">
              <span style="color:var(--text)">${esc(item.prompt || '(无描述)')}</span>
              <span style="display:block;color:var(--text-4);font-size:11px;margin-top:2px">
                ${esc(item.created_at || '')} · ${esc(item.mode_label || labelOf('bgm_mode', item.mode, '未标注用途'))} ·
                ${Number(item.duration_s || 0)}s · ${esc(item.provider || '')}
              </span>
            </span>
            <span class="right" style="display:flex;gap:6px;align-items:center">
              ${
                item.asset_id
                  ? `<button class="btn sm" data-act="play" data-asset="${esc(item.asset_id)}"
                       title="试听这条 BGM">试听</button>
                     <button class="btn sm" data-act="copy-path" data-asset="${esc(item.asset_id)}"
                       title="复制产物相对路径">路径</button>`
                  : '<span class="badge warn">缺产物</span>'
              }
            </span>
          </div>`
            )
            .join('') || '<div class="empty" style="padding:16px"><span>本章还没有 BGM 生成记录</span></div>'
        }
      </div>
    </div>
    <div class="card">
      <div class="card-head"><span class="ct">已插入的 BGM</span>
        <div class="right"><span class="badge mute">${cues.length} 条</span></div></div>
      <div class="card-body" style="padding:0">
        ${
          cues
            .map(
              (cue) => `
          <div class="summary-line">
            <span style="min-width:0">${esc(cue.name || cue.id)}
              <span style="display:block;color:var(--text-4);font-size:11px;margin-top:2px">
                ${fmtDuration(cue.start_ms)} 起 · ${cue.loop ? '循环' : '单次'} ·
                ${esc(labelOf('bgm_mode', cue.mode, '未标注用途'))}</span>
            </span>
            <span class="right" style="display:flex;gap:6px">
              ${
                cue.asset_id
                  ? `<button class="btn sm" data-act="play" data-asset="${esc(cue.asset_id)}">试听</button>`
                  : '<span class="badge warn">缺素材</span>'
              }
            </span>
          </div>`
            )
            .join('') || '<div class="empty" style="padding:16px"><span>还没有 BGM cue；生成后可在后期制作里使用</span></div>'
        }
      </div>
    </div>`;
}

/** 列表区（列表 / 网格 / 空状态） */
function bodyHTML() {
  if (tab === 'bgm') return bgmHTML();
  const rows = visibleRows();
  if (!rows.length) {
    return rowsOf().length === 0
      ? `<div class="empty"><b>本章还没有${TAB_LABEL[tab]}</b><span>切分台词后，${TAB_LABEL[tab]}会按场景出现在这里</span></div>`
      : `<div class="empty"><b>没有匹配的${TAB_LABEL[tab]}</b><span>换个筛选条件或搜索词</span></div>`;
  }
  return layout === 'grid'
    ? `<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:10px">${rows
        .map(cardHTML)
        .join('')}</div>`
    : rows.map(rowHTML).join('');
}

/** 主区 */
function mainHTML() {
  const chapter = state.chapters.find((item) => item.id === state.chapterId);
  const counts = Object.fromEntries(
    TABS.map(([key]) => [key, key === 'bgm' ? (bgm?.records || []).length : (payload[key] || []).length])
  );
  const statusCounts = { all: rowsOf().length };
  for (const key of ['ready', 'pending', 'running', 'failed', 'stale']) {
    statusCounts[key] = rowsOf().filter((item) => item.status === key).length;
  }
  return `
    <div class="page-head">
      <div class="ph">
        <div class="eyebrow"><span class="ln"></span>素材</div>
        <h1 class="page-title">${esc(chapter?.title || '本章素材')}</h1>
        <div class="page-sub">音效 ${counts.sfx} · 配图 ${counts.image} · 视频 ${counts.video} · BGM ${counts.bgm}</div>
      </div>
      <div class="ph-actions">
        ${
          tab === 'bgm'
            ? ''
            : `<button class="btn" data-act="run-kind" title="生成前可先选择只补缺失或全部重生成">
                 ${icon('spark')} 生成本章${TAB_LABEL[tab]}</button>`
        }
        <button class="btn" data-act="refresh" title="重新读取本章素材与 BGM">${icon('refresh')} 刷新</button>
        <button class="btn" data-act="reveal" title="在访达中定位工程目录">${icon('folder')} 在访达中打开工程</button>
      </div>
    </div>
    <div class="ed-filters" style="height:44px">
      ${TABS.map(
        ([key, label]) =>
          `<button class="chip ${tab === key ? 'on' : ''}" data-tab="${key}" title="查看本章${label}">${label}<span class="n">${counts[key]}</span></button>`
      ).join('')}
      <div class="grow"></div>
      ${
        tab === 'bgm'
          ? `<span class="hint" style="padding-right:6px">按生成记录管理，不参与状态筛选</span>`
          : `<div style="display:flex;gap:2px" role="group" aria-label="浏览形态">
               <button class="btn icon sm ${layout === 'list' ? 'on' : ''}" data-layout="list" title="列表视图">${icon('list', 14)}</button>
               <button class="btn icon sm ${layout === 'grid' ? 'on' : ''}" data-layout="grid" title="网格视图">${icon('grid', 14)}</button>
             </div>
             <div class="search" style="width:200px">${icon('search', 14)}
               <input type="search" id="media-search" placeholder="搜索提示词" value="${esc(keyword)}" title="按提示词或英文提示词搜索" />
             </div>`
      }
    </div>
    <div id="media-list" style="overflow:auto;flex:1;padding:12px 18px 18px;display:flex;flex-direction:column;gap:6px">
      ${bodyHTML()}
    </div>`;
}

/** 重画 */
function repaint() {
  const studio = document.getElementById('studio');
  if (!studio) return;
  const old = studio.querySelector('.ctx');
  if (old) old.outerHTML = chapterCtx({ search: state.chapterSearch });
  const main = document.getElementById('media-main');
  if (main) main.innerHTML = mainHTML();
  bindBgmMode();
}

/**
 * BGM 表单每次重画后重新绑定生成方式（分段按钮 + 「生成 BGM」按钮文案）。
 * 生成方式本身存在 localStorage，重画不会丢。
 */
function bindBgmMode() {
  const main = document.getElementById('media-main');
  if (!main) return;
  bindModeSwitch(main, 'media.bgm', {
    fallback: 'fill',
    action: main.querySelector('[data-act="bgm-generate"]'),
    labels: { fill: '补缺失', regen: '重新生成' },
  });
}

/** 读取本章素材与 BGM（BGM 失败不阻断素材浏览） */
async function loadChapter(path, chapterId) {
  const [data, bgmData] = await Promise.all([
    getJSON('/api/ui/media', { path, chapter: chapterId }),
    getJSON('/api/ui/bgm', { path, chapter: chapterId }).catch(() => null),
  ]);
  payload = { sfx: data?.sfx || [], image: data?.image || [], video: data?.video || [] };
  bgm = bgmData || { cues: [], records: [], plans: [], modes: {}, providers: [] };
}

/** 只刷新 BGM 数据 */
async function loadBgm() {
  try {
    bgm = await getJSON('/api/ui/bgm', { path: state.path, chapter: state.chapterId });
  } catch (error) {
    bgm = { cues: [], records: [], plans: [], modes: {}, providers: [] };
    toast(`BGM 数据读取失败：${error.message}`, 'bad', 4600);
  }
}

/**
 * 「生成本章X」前的确认弹窗：先选生成方式（只补缺失 / 重新生成），再提交。
 * @param {string} kind sfx / image / video
 * @param {HTMLElement} button 入口按钮（用于忙碌态与结束后恢复文案）
 */
function openRunKindDialog(kind, button) {
  const label = TAB_LABEL[kind] || kind;
  const key = `media.${kind}`;
  const mask = openDialog({
    title: `生成本章${label}`,
    body: `${modeSwitchHTML(key, { fallback: 'fill' })}
      <div class="dlg-note">只补缺失=只生成待生成 / 失败的条目；重新生成=覆盖本章同类型全部条目。</div>`,
    foot: `<div class="right"><button class="btn" data-dlg-close>取消</button>
      <button class="btn primary" id="media-run-ok">生成缺失条目</button></div>`,
  });
  const action = mask.querySelector('#media-run-ok');
  const readMode = bindModeSwitch(mask, key, {
    fallback: 'fill',
    action,
    labels: { fill: '生成缺失条目', regen: '重新生成全部' },
  });
  action.addEventListener('click', () => {
    const mode = readMode();
    closeDialog();
    void runMedia(kind, mode === 'regen', button);
  });
}

/**
 * 生成一类本章素材（音效 / 配图 / 视频）。
 * @param {string} kind sfx / image / video
 * @param {boolean} force 是否覆盖已生成条目
 * @param {HTMLElement} button 触发的按钮，用于展示忙碌态
 */
async function runMedia(kind, force, button) {
  const oldText = button.textContent;
  const label = TAB_LABEL[kind] || kind;
  button.disabled = true;
  button.textContent = '生成中…';
  const panel = openProgress({
    title: `正在${force ? '重新生成' : '生成'}本章${label}…`,
    hint: force
      ? '忽略已有产物，覆盖本章同类型全部条目'
      : '只生成待生成 / 失败的条目，已完成条目会跳过',
    stage: '已提交生成请求，等待本机模型返回…',
  });
  panel.update({ percent: null });
  try {
    const result = await postJSON(
      '/api/ui/media/run',
      {
        path: state.path,
        chapter: state.chapterId,
        kind,
        force,
      },
      { timeout: 0 }
    );
    const made = (result?.generated || []).length || result?.count || 0;
    await loadChapter(state.path, state.chapterId);
    repaint();
    const message = `${force ? '已重新生成' : '已生成'}本章${label}${made ? ` ${made} 条` : ''}`;
    panel.note(`${made} 条 · ${label}`);
    panel.finish({
      message,
      detail: '素材列表已刷新，可在本页逐条试听或查看。',
      actions: [{ label: '查看列表', primary: true, onClick: () => repaint() }],
    });
    toast(`${message}，列表已刷新`, 'ok', 4200);
  } catch (error) {
    toast(error.message, 'bad', 5200);
    button.disabled = false;
    button.textContent = oldText;
    panel.fail({
      message: `${label}生成失败：${error.message}`,
      retry: () => void runMedia(kind, force, button),
    });
  }
}

/**
 * 生成单条音效：后台任务 + 按钮上显示进度。
 * @param {HTMLElement} button 条目上的生成按钮
 */
async function generateSfx(button) {
  const id = button.dataset.id;
  if (!id) return;
  const oldText = button.textContent;
  const rowTitle =
    button.closest('.summary-line')?.querySelector('span[style*="color:var(--text)"]')?.textContent?.trim() || '';
  button.disabled = true;
  button.textContent = '排队…';
  const panel = openProgress({
    title: '正在生成单条音效…',
    hint: rowTitle || '按这一条的描述调用本机音效模型',
    stage: '任务已排队，等待开始…',
  });
  panel.update({ percent: 0 });
  try {
    const { job_id: jobId } = await postJSON('/api/ui/sfx/generate', {
      path: state.path,
      chapter: state.chapterId,
      id,
    });
    for (let tick = 0; tick < 160; tick += 1) {
      await new Promise((resolve) => window.setTimeout(resolve, 1500));
      const job = await getJSON('/api/ui/sfx/job', { job_id: jobId });
      if (job.status === 'done') {
        await loadChapter(state.path, state.chapterId);
        repaint();
        panel.finish({
          message: '这段音效已生成',
          detail: rowTitle ? `条目：${rowTitle}` : '',
          actions: [{ label: '查看列表', primary: true, onClick: () => repaint() }],
        });
        toast('这段音效已生成', 'ok');
        return;
      }
      if (job.status === 'failed') {
        throw new Error(job.error?.message || '音效生成失败');
      }
      const percent = Number(job.percent || 0);
      button.textContent = `${percent}%`;
      panel.update({
        stage: `正在生成${rowTitle ? `「${rowTitle}」` : ''}…`,
        percent,
        current: job.stage || job.message || `${percent}%`,
      });
    }
    throw new Error('生成超时，请稍后点「刷新」查看结果');
  } catch (error) {
    toast(error.message, 'bad', 5200);
    button.disabled = false;
    button.textContent = oldText;
    panel.fail({ message: `音效生成失败：${error.message}`, retry: () => void generateSfx(button) });
  }
}

/** 生成 BGM 并刷新记录 */
async function generateBgm(button) {
  const genMode = getGenMode('media.bgm', 'fill');
  const planId = document.getElementById('bgm-plan')?.value || '';
  const prompt = document.getElementById('bgm-prompt')?.value.trim() || '';
  const promptEn = document.getElementById('bgm-prompt-en')?.value.trim() || '';
  const duration = Number(document.getElementById('bgm-duration')?.value || 0);
  const mode = document.getElementById('bgm-mode')?.value || 'auto';
  const hint = document.getElementById('bgm-hint');
  // 只补缺失：本章已有记录且用户没改提示词时不重复生成，避免误覆盖
  const records = bgm?.records || [];
  if (genMode === 'fill' && records.length > 0 && !prompt && !promptEn) {
    return toast(
      `本章已有 ${records.length} 条 BGM 记录，未重新生成；要再生成请选「重新生成」`,
      'info',
      5000
    );
  }
  if (!planId && !promptEn) {
    return toast('不选计划时请填英文提示词（本地模型只认英文提示）', 'warn', 4600);
  }
  button.disabled = true;
  const oldText = button.textContent;
  button.textContent = '生成中…';
  if (hint) hint.textContent = '正在生成，本地模型通常需要几十秒…';
  const panel = openProgress({
    title: genMode === 'regen' ? '正在重新生成 BGM…' : '正在生成 BGM…',
    hint: prompt || promptEn || '按 BGM 计划生成音乐',
    stage: '已提交生成请求，等待本机音乐模型返回…',
  });
  panel.update({ percent: null });
  try {
    const result = await postJSON(
      '/api/ui/bgm/generate',
      {
        path: state.path,
        chapter: state.chapterId,
        prompt,
        prompt_en: promptEn || undefined,
        duration: duration || undefined,
        mode,
        plan_id: planId || undefined,
      },
      { timeout: 0 }
    );
    await loadBgm();
    repaint();
    const message = `已生成 ${Number(result?.duration_s || 0)}s BGM${
      result?.auto_switched ? '（超过本地时长上限，已自动改用 ACE-Step）' : ''
    }`;
    panel.note(message);
    panel.finish({
      message,
      detail: '生成记录已刷新，可在本页试听或插入 cue。',
      actions: [{ label: '查看记录', primary: true, onClick: () => repaint() }],
    });
    toast(message, 'ok', 4600);
  } catch (error) {
    toast(error.message, 'bad', 5200);
    if (hint) hint.textContent = error.message;
    button.disabled = false;
    button.textContent = oldText;
    panel.fail({ message: `BGM 生成失败：${error.message}`, retry: () => void generateBgm(button) });
  }
}

/** 点击 */
async function onRootClick(event) {
  const chapterRow = event.target.closest('[data-chapter]');
  if (chapterRow) {
    state.chapterId = chapterRow.dataset.chapter;
    await loadChapter(state.path, state.chapterId);
    repaint();
    return;
  }
  const tabButton = event.target.closest('[data-tab]');
  if (tabButton) {
    tab = tabButton.dataset.tab;
    if (tab === 'bgm' && !bgm) await loadBgm();
    repaint();
    return;
  }
  const statusButton = event.target.closest('[data-status]');
  if (statusButton) {
    statusFilter = statusButton.dataset.status;
    repaint();
    return;
  }
  const layoutButton = event.target.closest('[data-layout]');
  if (layoutButton) {
    layout = layoutButton.dataset.layout;
    repaint();
    return;
  }
  const button = event.target.closest('[data-act]');
  if (!button) return;
  const act = button.dataset.act;
  if (act === 'back') return goProjects();
  if (act === 'to-editor') return goChapter(state.path, state.chapterId);
  if (act === 'play') {
    const audio = new Audio(fileUrl(button.dataset.asset, state.path));
    audio.play().catch((error) => toast(`播放失败：${error.message}`, 'bad'));
    return;
  }
  if (act === 'copy-path') {
    const rel = button.dataset.asset;
    if (!rel) return toast('这一项还没有产物文件', 'warn');
    try {
      await navigator.clipboard.writeText(rel);
      toast('路径已复制', 'ok');
    } catch {
      toast(`路径：${rel}`, 'info', 5000);
    }
    return;
  }
  if (act === 'refresh') {
    await loadChapter(state.path, state.chapterId);
    repaint();
    return;
  }
  if (act === 'run-kind') {
    return openRunKindDialog(tab, button);
  }
  if (act === 'sfx-generate') return void generateSfx(button);
  if (act === 'bgm-generate') return void generateBgm(button);
  if (act === 'reveal') {
    try {
      await postJSON('/api/project/reveal', { path: state.path });
    } catch (error) {
      toast(error.message, 'bad');
    }
  }
}

/** 输入 */
function onRootInput(event) {
  if (event.target.id === 'media-search') {
    keyword = event.target.value;
    const list = document.getElementById('media-list');
    if (list) list.innerHTML = bodyHTML();
    event.target.focus();
    return;
  }
  if (event.target.id === 'chapter-search') {
    state.chapterSearch = event.target.value;
    const list = document.getElementById('chapter-list');
    if (list) {
      const ctx = list.closest('.ctx');
      if (ctx) ctx.outerHTML = chapterCtx({ search: state.chapterSearch });
      const input = document.getElementById('chapter-search');
      if (input) {
        input.focus();
        input.setSelectionRange(input.value.length, input.value.length);
      }
    }
  }
}

/** 下拉变化：选择 BGM 计划时带出时长、名称与英文提示 */
function onRootChange(event) {
  if (event.target.id !== 'bgm-plan') return;
  const option = event.target.selectedOptions?.[0];
  const duration = Number(option?.dataset.duration || 0);
  const durationInput = document.getElementById('bgm-duration');
  if (durationInput && duration) durationInput.value = String(Math.round(duration));
  const promptInput = document.getElementById('bgm-prompt');
  if (promptInput && option?.dataset.name) promptInput.value = String(option.dataset.name);
  const promptEnInput = document.getElementById('bgm-prompt-en');
  if (promptEnInput) promptEnInput.value = String(option?.dataset.promptEn || '');
}

/** 进入素材视图 */
export async function renderMedia(root, { path }) {
  if (state.path !== path) resetProject(path);
  if (!bound) {
    root.addEventListener('click', onRootClick);
    root.addEventListener('input', onRootInput);
    root.addEventListener('change', onRootChange);
    bound = true;
  }
  root.innerHTML = `${topbar({ left: '', crumbs: '<span class="cur">正在加载素材…</span>', right: '' })}`;
  try {
    const [info, chapters] = await Promise.all([
      getJSON('/api/project/info', { path }),
      getJSON('/api/ui/chapters', { path }),
    ]);
    state.info = info;
    state.chapters = chapters || [];
    if (!state.chapters.some((item) => item.id === state.chapterId)) {
      state.chapterId = state.chapters[0]?.id || '';
    }
    if (!state.chapterId) {
      // 空工程：保留素材页骨架，主区引导先去小说视图起稿或导入正文
      root.innerHTML = `
        ${topbar({
          left: `<button class="btn icon" data-act="back" title="回工作区">${icon('back')}</button>
                 <div class="brand"><span class="brand-mark"></span><span style="font-size:12.5px">素材<small>本地版</small></span></div>`,
          crumbs: `<a href="#/projects">${esc(state.info?.work?.title || '工程')}</a>
                   <span class="sep">/</span><span class="cur">素材</span>`,
          right: '',
        })}
        <div class="studio" id="studio">
          ${railbar('media', { chapters: 0 })}
          ${chapterCtx({ search: state.chapterSearch })}
          <section class="editor"><div class="page" style="overflow:auto">${emptyProjectHTML(path)}</div></section>
          <aside class="studio-right"></aside>
        </div>`;
      return;
    }
    await loadChapter(path, state.chapterId);
  } catch (error) {
    root.innerHTML = `<div class="page"><div class="empty">${icon('warn', 22)}<b>素材加载失败</b>
      <span>${esc(error.message)}</span><a class="btn" href="#/projects" style="margin-top:10px">回工作区</a></div></div>`;
    return;
  }
  root.innerHTML = `
    ${topbar({
      left: `<button class="btn icon" data-act="back" title="回工作区">${icon('back')}</button>
             <div class="brand"><span class="brand-mark"></span><span style="font-size:12.5px">素材<small>本地版</small></span></div>`,
      crumbs: `<a href="#/projects">${esc(state.info?.work?.title || '工程')}</a>
               <span class="sep">/</span><span class="cur">素材</span>`,
      right: `<button class="btn" data-act="to-editor">${icon('chapter')} 回画本</button>`,
    })}
    <div class="studio" id="studio">
      ${railbar('media', { chapters: state.chapters.length })}
      ${chapterCtx({ search: state.chapterSearch })}
      <section class="editor">
        <div id="media-main" style="display:flex;flex-direction:column;min-height:0;flex:1">${mainHTML()}</div>
      </section>
      <aside class="studio-right"></aside>
    </div>`;
  bindBgmMode();
}
