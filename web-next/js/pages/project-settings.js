/**
 * 工程设置视图：作品信息 + 制作要求 + 制作概况（只作用于当前工程）。
 * 与全局设置（#/settings）完全分开：这里所有字段都写入当前工程，页面不再出现全局项。
 */
import { esc, icon, toast } from '../dom.js';
import { getJSON, postJSON } from '../api.js';
import { state, resetProject } from '../store.js';
import { railbar, topbar, goChapter, goProjects } from '../shell.js';

/** 当前工程的根目录 */
let projectPath = '';
/** 工程 project.json 的作品信息 */
let work = null;
/** `/api/project/info` 的返回（章节列表等） */
let info = null;
/** 工程级制作要求（静音间隔 + 音频导出参数） */
let production = null;
/** 作品信息自动保存定时器 */
let timer = null;
/** 制作要求自动保存定时器 */
let prodTimer = null;
/** 是否有尚未落盘的作品信息改动 */
let dirty = false;
let bound = false;
let loadError = '';

/** 静音间隔字段：键、中文名 */
const SILENCE_FIELDS = [
  ['start', '起始留白'],
  ['title_to_body', '标题⇀正文'],
  ['narration_to_dialogue', '旁白⇀对话'],
  ['narration_to_sfx', '旁白⇀音效'],
  ['narration_to_narration', '旁白⇀旁白'],
  ['dialogue_to_narration', '对话⇀旁白'],
  ['dialogue_to_sfx', '对话⇀音效'],
  ['dialogue_to_dialogue', '对话⇀对话'],
  ['sfx_to_dialogue', '音效⇀对白'],
  ['sfx_to_narration', '音效⇀旁白'],
  ['sfx_to_sfx', '音效⇀音效'],
  ['end', '结束留白'],
];

/**
 * 更新顶栏保存态与「保存」按钮可用性。
 * @param {string} kind 状态类型（saving / ok / bad）
 * @param {string} text 显示文案
 */
function setSaveState(kind, text) {
  const node = document.getElementById('settings-save');
  if (node) {
    node.className = `save-state ${kind}`;
    node.innerHTML = `${icon(kind === 'saving' ? 'refresh' : 'check', 12)}${esc(text)}`;
  }
  const button = document.getElementById('settings-save-now');
  if (button) {
    button.disabled = !dirty;
    button.title = dirty ? '立即保存作品信息（⌘S）' : '没有未保存的改动';
  }
}

/**
 * 目标时长格式化。
 * @param {number} ms 毫秒
 * @returns {string} 中文时长
 */
function durationText(ms) {
  if (!ms) return '未设置';
  return `${Math.round(Number(ms) / 60000)} 分钟`;
}

/** 制作要求卡片：12 个静音间隔 + 音频格式参数 */
function productionHTML() {
  const silences = production?.silences_ms || {};
  const audio = production?.audio || {};
  const fields = SILENCE_FIELDS.map(
    ([key, label]) => `
      <div class="field prod-field">
        <label>${esc(label)}</label>
        <div class="prod-input">
          <input class="input" type="number" min="0" step="0.1" data-prod-silence="${key}"
            value="${(Number(silences[key] ?? 0) / 1000).toFixed(1)}"
            title="${esc(label)} 静音时长，单位秒" />
          <span class="unit">s</span>
        </div>
      </div>`
  ).join('');
  const options = (values, current, unit) =>
    values
      .map((item) => `<option value="${item}" ${Number(current) === Number(item) ? 'selected' : ''}>${item}${unit}</option>`)
      .join('');
  return `
    <div class="card" style="margin-top:14px">
      <div class="card-head"><span class="ct">制作要求（当前工程）</span>
        <span class="hint">只写入当前工程；静音间隔作用于时间轴拼接，音频参数作用于交付导出</span></div>
      <div class="card-body">
        <div class="prod-group">
          <h4>静音间隔设置</h4>
          <div class="prod-grid">${fields}</div>
        </div>
        <div class="prod-group" style="margin-top:16px">
          <h4>音频格式参数</h4>
          <div class="prod-grid">
            <div class="field prod-field"><label>音频格式</label>
              <select class="select" data-prod-audio="format" title="交付包音频格式">
                ${['mp3', 'wav', 'm4a']
                  .map((item) => `<option value="${item}" ${audio.format === item ? 'selected' : ''}>${item}</option>`)
                  .join('')}
              </select></div>
            <div class="field prod-field"><label>采样率</label>
              <select class="select" data-prod-audio="sample_rate" title="导出与混音采样率">
                ${options([44100, 48000], audio.sample_rate || 48000, 'hz')}</select></div>
            <div class="field prod-field"><label>目标码率</label>
              <select class="select" data-prod-audio="bitrate_kbps" title="有损格式的目标码率；wav 忽略">
                ${options([192, 256], audio.bitrate_kbps || 192, 'kbps')}</select></div>
          </div>
        </div>
        <div class="hint" style="margin-top:12px">改动静音间隔后，已有时间轴需重建一次才会生效；新导出的交付包立即使用新的音频参数。</div>
      </div>
    </div>`;
}

/** 作品信息卡片 */
function projectInfoHTML(item) {
  return `
    <div class="card">
      <div class="card-head"><span class="ct">作品信息</span>
        <span class="hint">写入当前工程的 project.json</span></div>
      <div class="card-body">
        <div style="display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px">
          <div class="field"><label>书名</label>
            <input class="input" id="set-title" value="${esc(item.title || '')}" title="作品名，显示在顶栏与工程列表" /></div>
          <div class="field"><label>作者</label>
            <input class="input" id="set-author" value="${esc(item.author || '')}" placeholder="未填写"
              title="作者署名，可留空" /></div>
          <div class="field" style="grid-column:1/-1"><label>题材（逗号分隔）</label>
            <input class="input" id="set-genres" value="${esc((item.genres || []).join('，'))}" placeholder="例如：悬疑，都市"
              title="题材关键词，多个用逗号分隔" /></div>
          <div class="field"><label>语言</label>
            <div class="input" style="display:flex;align-items:center;color:var(--text-3)"
              title="语言在导入阶段确定，本地版暂不支持在这里修改">${esc(item.language || 'zh-CN')}</div></div>
          <div class="field"><label>目标时长</label>
            <div class="input" style="display:flex;align-items:center;color:var(--text-3)"
              title="目标时长在导入阶段确定，本地版暂不支持在这里修改">${durationText(item.target_duration_ms)}</div></div>
        </div>
        <div class="hint">改动会自动保存；语言与目标时长本阶段只读展示。</div>
      </div>
    </div>`;
}

/** 制作概况卡片 */
function summaryHTML() {
  const totalWords = (info?.chapters || []).reduce((sum, chapter) => sum + Number(chapter.word_count || 0), 0);
  return `
    <div class="card" style="margin-top:14px">
      <div class="card-head"><span class="ct">制作概况</span></div>
      <div class="card-body">
        <div class="summary-line"><span>章节数</span><span class="right">${(info?.chapters || []).length} 章</span></div>
        <div class="summary-line"><span>正文字数</span><span class="right">${totalWords} 字</span></div>
        <div class="summary-line"><span>创建时间</span><span class="right mono">${esc(work?.created_at || '—')}</span></div>
        <div class="summary-line"><span>最近更新</span><span class="right mono">${esc(work?.updated_at || '—')}</span></div>
      </div>
    </div>`;
}

/** 主区：加载失败给可读错误态，加载成功给三块卡片 */
function mainHTML() {
  if (loadError) {
    return `<div class="card" style="margin-top:14px"><div class="card-body">
      <div class="empty" style="padding:18px">${icon('warn', 20)}
        <b>工程设置读取失败</b><span>${esc(loadError)}</span></div>
    </div></div>`;
  }
  if (!work) {
    return `<div class="card" style="margin-top:14px"><div class="card-body">
      <div class="empty" style="padding:18px"><span>正在读取工程设置…</span></div></div></div>`;
  }
  return `${projectInfoHTML(work)}${productionHTML()}${summaryHTML()}`;
}

/** 左侧章节目录（点击回到该章画本） */
function ctxHTML() {
  const rows = (state.chapters || [])
    .map(
      (chapter) => `
      <div class="chap-row" data-chapter="${esc(chapter.id)}"
           title="${esc(chapter.title || `第 ${chapter.index} 章`)} · 回到画本">
        <span class="cn">${esc(chapter.title || `第 ${chapter.index} 章`)}</span>
        <span class="dot ${chapter.status === 'split' ? 'warn' : ''}"></span>
        <span class="cw">${chapter.word_count ? `${chapter.word_count}字` : '未写'}</span>
      </div>`
    )
    .join('');
  return `
    <section class="ctx" aria-label="章节目录">
      <div class="ctx-head"><span class="ct">章节目录</span>
        <span class="badge mute">${(state.chapters || []).length} 章</span>
      </div>
      <div class="chap-list" id="ctx-list">${rows || '<div class="empty" style="padding:18px 12px"><span>正在读取章节…</span></div>'}</div>
    </section>`;
}

/** 读取作品信息表单 */
function collect() {
  return {
    title: document.getElementById('set-title')?.value.trim() || '',
    author: document.getElementById('set-author')?.value.trim() || '',
    genres: (document.getElementById('set-genres')?.value || '')
      .split(/[,，、;；]/)
      .map((text) => text.trim())
      .filter(Boolean),
  };
}

/**
 * 保存作品信息到当前工程。
 * @param {{silent?: boolean}} options silent=true 时不在成功后弹提示（自动保存用）
 */
async function saveWork({ silent = false } = {}) {
  if (!work) return;
  const body = collect();
  setSaveState('saving', '保存中…');
  try {
    await postJSON('/api/ui/project/work', { path: projectPath, ...body });
    work = { ...work, ...body, updated_at: new Date().toISOString() };
    dirty = false;
    setSaveState('ok', '已自动保存');
    if (!silent) toast('工程信息已保存', 'ok');
  } catch (error) {
    setSaveState('bad', '保存失败');
    toast(error.message, 'bad');
  }
}

/** 作品信息延迟保存 */
function scheduleSave() {
  dirty = true;
  setSaveState('saving', '待保存…');
  if (timer) window.clearTimeout(timer);
  timer = window.setTimeout(() => {
    timer = null;
    saveWork({ silent: true });
  }, 800);
}

/** 读取制作要求表单 */
function collectProduction() {
  const silences = {};
  document.querySelectorAll('[data-prod-silence]').forEach((node) => {
    const seconds = Number(node.value || 0);
    silences[node.dataset.prodSilence] = Math.max(0, Math.min(10000, Math.round(seconds * 1000)));
  });
  return {
    path: projectPath,
    production: {
      silences_ms: silences,
      audio: {
        format: document.querySelector('[data-prod-audio="format"]')?.value || 'mp3',
        sample_rate: Number(document.querySelector('[data-prod-audio="sample_rate"]')?.value || 48000),
        bitrate_kbps: Number(document.querySelector('[data-prod-audio="bitrate_kbps"]')?.value || 192),
      },
    },
  };
}

/** 保存制作要求（自动保存入口） */
async function saveProduction() {
  if (!production) return;
  const payload = collectProduction();
  setSaveState('saving', '保存中…');
  try {
    const result = await postJSON('/api/ui/project/production', payload);
    production = result.production || payload.production;
    setSaveState('ok', '已自动保存');
  } catch (error) {
    setSaveState('bad', '保存失败');
    toast(error.message, 'bad');
  }
}

/** 制作要求延迟保存 */
function scheduleProductionSave() {
  if (prodTimer) window.clearTimeout(prodTimer);
  setSaveState('saving', '待保存…');
  prodTimer = window.setTimeout(() => {
    prodTimer = null;
    saveProduction();
  }, 500);
}

/** 读取工程数据并渲染主区 */
async function loadProject() {
  if (!projectPath) return;
  try {
    const [infoPayload, workPayload, productionPayload] = await Promise.all([
      getJSON('/api/project/info', { path: projectPath }),
      getJSON('/api/ui/project/work', { path: projectPath }),
      getJSON('/api/ui/project/production', { path: projectPath }),
    ]);
    info = infoPayload;
    work = workPayload || {};
    production = productionPayload?.production || null;
    state.info = info;
    state.chapters = info?.chapters || [];
    if (!state.chapters.some((item) => item.id === state.chapterId)) {
      state.chapterId = state.chapters[0]?.id || '';
    }
    loadError = '';
  } catch (error) {
    loadError = error.message;
    work = null;
    production = null;
  }
  const body = document.getElementById('settings-body');
  if (body) body.innerHTML = mainHTML();
  const list = document.getElementById('ctx-list');
  if (list) list.innerHTML = ctxHTML();
  const crumb = document.getElementById('set-crumb');
  if (crumb) crumb.textContent = work?.title || '当前工程';
}

/** 点击：保存、回画本、章节跳转 */
function onRootClick(event) {
  const chapterRow = event.target.closest('[data-chapter]');
  if (chapterRow) return goChapter(projectPath, chapterRow.dataset.chapter);
  const button = event.target.closest('[data-act]');
  if (!button) return;
  if (button.dataset.act === 'back') return goProjects();
  if (button.dataset.act === 'to-editor') return goChapter(projectPath, state.chapterId);
  if (button.dataset.act === 'save-now') {
    if (timer) {
      window.clearTimeout(timer);
      timer = null;
    }
    saveWork();
  }
}

/** 输入：作品信息防抖保存，制作要求单独防抖 */
function onRootInput(event) {
  const target = event.target;
  if (target.closest('#settings-main') && !target.dataset.prodSilence) {
    if (target.id === 'set-title' || target.id === 'set-author' || target.id === 'set-genres') scheduleSave();
    return;
  }
  if (target.dataset && target.dataset.prodSilence) {
    scheduleProductionSave();
    return;
  }
  if (target.dataset && target.dataset.prodAudio) scheduleProductionSave();
}

/** 下拉变更：立即保存制作要求 */
function onRootChange(event) {
  const target = event.target;
  if (target.dataset && target.dataset.prodAudio) scheduleProductionSave();
}

/** 失焦立即落盘，避免切页丢改动 */
function onRootFocusOut(event) {
  const target = event.target;
  if (!target.id && !target.dataset?.prodSilence) return;
  if (timer) {
    window.clearTimeout(timer);
    timer = null;
    saveWork({ silent: true });
  }
  if (prodTimer) {
    window.clearTimeout(prodTimer);
    prodTimer = null;
    saveProduction();
  }
}

/** 快捷键：⌘S 立即保存 */
function onKeydown(event) {
  if (!(event.metaKey || event.ctrlKey) || event.key.toLowerCase() !== 's') return;
  event.preventDefault();
  if (timer) {
    window.clearTimeout(timer);
    timer = null;
  }
  saveWork();
}

/** 进入工程设置 */
export async function renderProjectSettings(root, { path = '' } = {}) {
  if (timer) window.clearTimeout(timer);
  if (prodTimer) window.clearTimeout(prodTimer);
  timer = null;
  prodTimer = null;
  dirty = false;
  work = null;
  info = null;
  production = null;
  loadError = '';
  projectPath = path;
  if (path && state.path !== path) resetProject(path);
  if (!bound) {
    root.addEventListener('click', onRootClick);
    root.addEventListener('input', onRootInput);
    root.addEventListener('change', onRootChange);
    root.addEventListener('focusout', onRootFocusOut);
    document.addEventListener('keydown', onKeydown);
    bound = true;
  }
  root.innerHTML = `
    ${topbar({
      left: `<button class="btn icon" data-act="back" title="回工程列表">${icon('back')}</button>
             <div class="brand"><span class="brand-mark"></span><span style="font-size:12.5px">工程设置<small>当前工程</small></span></div>`,
      crumbs: `<a href="#/projects">工程列表</a><span class="sep">/</span>
               <span class="cur" id="set-crumb">${esc(work?.title || '当前工程')}</span>`,
      right: `<span class="save-state ok" id="settings-save">${icon('check', 12)}已自动保存</span>
              <button class="btn sm" id="settings-save-now" data-act="save-now" disabled
                title="没有未保存的改动">保存</button>
              <button class="btn" data-act="to-editor">${icon('chapter')} 回画本</button>`,
    })}
    <div class="studio" id="studio">
      ${railbar('settings')}
      ${ctxHTML()}
      <section class="editor">
        <div id="settings-main" style="display:flex;flex-direction:column;min-height:0;flex:1">
          <div class="page-head" style="padding:18px 18px 0">
            <div class="ph">
              <div class="eyebrow"><span class="ln"></span>工程设置</div>
              <h1 class="page-title">工程设置</h1>
              <div class="page-sub">这里的字段只作用于当前工程；全局设置请到「设置」页。</div>
            </div>
          </div>
          <div style="padding:0 18px 18px;overflow:auto;flex:1" id="settings-body">${mainHTML()}</div>
        </div>
      </section>
      <aside class="studio-right"></aside>
    </div>`;
  await loadProject();
}
