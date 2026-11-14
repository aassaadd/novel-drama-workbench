/**
 * 任务视图（V6）：本章配音进度、失败行重试、任务队列。
 * 数据来自批量进度文件与队列接口；运行中每 3 秒刷新一次，离开页面即停轮询。
 */
import { esc, icon, toast, openDialog, closeDialog, fmtDuration, impactListHTML } from '../dom.js';
import { getJSON, postJSON } from '../api.js';
import { state, resetProject } from '../store.js';
import { railbar, topbar, chapterCtx, goChapter, goProjects, checkConsistency, resplitBannerHTML, onResplit } from '../shell.js';

const QUEUE_LABEL = {
  tts: '配音',
  sfx: '音效',
  image: '配图',
  video: '视频',
  render: '渲染',
};

const STATUS_LABEL = {
  queued: '排队',
  running: '运行中',
  succeeded: '成功',
  failed: '失败',
  canceled: '已取消',
  pending: '待生成',
  ready: '就绪',
};

/** 全链路阶段（与 `ndw produce` 的 STAGES 一致） */
const PRODUCE_STAGES = [
  ['setting', '设定库'],
  ['outline', '大纲'],
  ['chapter', '章节正文'],
  ['scenes', '场景切分'],
  ['lines', '台词切分'],
  ['script', '导演标注'],
  ['tts', '配音合成'],
  ['sfx', '音效'],
  ['image', '配图'],
  ['video', '视频位'],
  ['timeline', '时间轴'],
  ['concat', '配音拼接'],
  ['mix', '混音'],
  ['mux', '混流'],
  ['export', '导出'],
];

const PRODUCE_STATUS = {
  pending: '待跑',
  running: '运行中',
  succeeded: '成功',
  failed: '失败',
  skipped: '跳过',
};

let progress = null;
let queue = [];
let readiness = null;
/** 最近一次全链路编排记录（`run show`） */
let produceRun = null;
/** GPU 锁与并发明细（锁文件持有人、队列计数） */
let gpu = null;
/** 配音批次是否已暂停 */
let paused = false;
/** 当前章节正文与切分是否一致 */
let consistency = null;
let queueFilter = 'all';
let timer = null;
let bound = false;

/** 状态徽标语义色 */
function badgeClass(name) {
  if (name === 'succeeded' || name === 'ready') return 'ok';
  if (name === 'failed') return 'bad';
  if (name === 'running') return 'run';
  if (name === 'queued' || name === 'pending') return 'mute';
  return '';
}

/** 过滤队列 */
function visibleQueue() {
  if (queueFilter === 'all') return queue;
  if (queueFilter === 'active') return queue.filter((item) => ['queued', 'running'].includes(item.status));
  return queue.filter((item) => item.status === queueFilter);
}

/** 失败行文案 */
function failedRowHTML(row) {
  const lineId = row.line_id || row.line || '';
  const label = row.text || row.speaker || lineId.slice(-6) || '未命名行';
  return `
    <div class="summary-line">
      <span style="min-width:0">${esc(label)}
        <span style="color:var(--text-4);font-size:11px"> · ${esc(row.error || '生成失败')}</span></span>
      <span class="right"><button class="btn sm" data-act="retry" data-line="${esc(lineId)}"
        title="只重新生成这一行的配音，旧版本保留">重试</button></span>
    </div>`;
}

/** 进度卡 */
function progressHTML() {
  const data = progress || {};
  const total = Number(data.total || 0);
  const done = Number(data.done || 0);
  const failed = Number(data.failed || 0);
  const ratio = total ? Math.round((done / total) * 100) : 0;
  const running = Boolean(data.running);
  const failedRows = (data.rows || []).slice(0, 20);
  return `
    <div class="card" style="margin:14px 18px 0">
      <div class="card-head"><span class="ct">本章配音进度</span>
        <div class="right">
          <span class="badge ${running ? 'run' : total ? 'ok' : 'mute'}">${
            running ? `生成中 ${done}/${total}` : total ? (data.finished ? '本轮已完成' : '未在运行') : '还没有批量任务'
          }</span>
        </div>
      </div>
      <div class="card-body">
        <div class="bar"><span style="width:${ratio}%"></span></div>
        <div style="display:flex;justify-content:space-between;font-size:11px;color:var(--text-3);margin-top:6px">
          <span>完成 ${done} / ${total}${failed ? ` · 失败 ${failed}` : ''}${
            data.skipped ? ` · 跳过 ${data.skipped}` : ''
          }</span>
          <span class="mono">${ratio}%${
            running && data.eta_ms ? ` · 预计剩余 ${fmtDuration(data.eta_ms)}` : ''
          }</span>
        </div>
        <div style="display:flex;gap:8px;margin-top:10px;flex-wrap:wrap">
          <button class="btn primary" data-act="batch-plan"
            title="先看影响清单：本次生成多少行、跳过多少行、预计耗时">${icon('mic')} 补齐缺失配音…</button>
          ${
            running
              ? `<button class="btn danger" data-act="stop" title="停止当前批量配音任务">${icon('stop')} 停止生成</button>
                 <button class="btn" data-act="pause" title="暂停后当前行会跑完，继续后从下一行接着配">${
                   paused ? `${icon('play')} 继续生成` : `${icon('pause')} 暂停`
                 }</button>`
              : ''
          }
          <button class="btn" data-act="refresh" title="立即刷新进度与队列">${icon('refresh')} 刷新</button>
        </div>
        ${
          total
            ? ''
            : `<div class="hint" style="margin-top:8px">本章还没有批量配音任务。点击「补齐缺失配音…」会先列出影响清单，再开始生成。</div>`
        }
        ${
          failedRows.length
            ? `<div style="margin-top:12px">
                 <div class="hint" style="margin-bottom:6px">失败行（可逐条重试）</div>
                 ${failedRows.map(failedRowHTML).join('')}
               </div>`
            : ''
        }
      </div>
    </div>`;
}

/** 全链路编排卡：阶段区间 + 最近一次运行明细 */
function produceHTML() {
  const run = produceRun;
  const options = (selected) =>
    PRODUCE_STAGES.map(
      ([key, label]) => `<option value="${key}" ${key === selected ? 'selected' : ''}>${label}</option>`
    ).join('');
  const stages = run?.stages || [];
  const doneCount = stages.filter((item) => ['succeeded', 'skipped'].includes(item.status)).length;
  const rows = stages
    .map((item) => {
      const status = PRODUCE_STATUS[item.status] || item.status || '';
      const ok = item.status === 'succeeded';
      const bad = item.status === 'failed';
      const skip = item.status === 'skipped';
      return `
        <tr>
          <td>${esc(item.label || item.stage || '')}</td>
          <td><span class="badge ${ok ? 'ok' : bad ? 'bad' : skip ? 'mute' : item.status === 'running' ? 'run' : 'mute'}">${esc(status)}</span></td>
          <td class="mono">${(item.artifacts || []).length}</td>
          <td style="color:${bad ? 'var(--bad)' : 'var(--text-3)'};font-size:11.5px">${esc(item.error || item.note || '')}</td>
        </tr>`;
    })
    .join('');
  return `
    <div class="card" style="margin:14px 18px 0">
      <div class="card-head"><span class="ct">全链路编排</span>
        <div class="right">${
          run
            ? `<span class="badge ${run.status === 'succeeded' ? 'ok' : run.status === 'failed' ? 'bad' : 'run'}">
                 ${esc(run.status)} · ${doneCount}/${stages.length}${run.run_id ? ` · ${esc(String(run.run_id).slice(-6))}` : ''}
               </span>`
            : '<span class="badge mute">还没有编排记录</span>'
        }</div>
      </div>
      <div class="card-body">
        <div style="display:grid;grid-template-columns:1fr 1fr auto auto;gap:10px;align-items:end">
          <div class="field"><label>从</label>
            <select class="select" id="prod-from">${options('setting')}</select></div>
          <div class="field"><label>到</label>
            <select class="select" id="prod-to">${options('export')}</select></div>
          <label style="display:flex;gap:6px;align-items:center;font-size:11.5px;color:var(--text-3);padding-bottom:7px">
            <input type="checkbox" id="prod-force" /> 强制重跑</label>
          <button class="btn primary" data-act="produce-start"
            title="后台启动 ndw produce，按记录续跑已完成的阶段">启动编排</button>
        </div>
        <div class="hint" style="margin-top:6px">
          默认续跑：已完成的阶段会跳过；勾选「强制重跑」会忽略已有产物。运行期间本卡片随任务轮询一起刷新。
        </div>
        ${
          stages.length
            ? `<table class="files" style="margin-top:10px">
                 <thead><tr><th>阶段</th><th>状态</th><th>产物</th><th>说明</th></tr></thead>
                 <tbody>${rows}</tbody>
               </table>`
            : ''
        }
      </div>
    </div>`;
}

/** GPU 锁与并发卡：锁持有人 + 队列计数 */
function gpuHTML() {
  if (!gpu) return '';
  const lock = gpu.lock || {};
  const counts = gpu.queue || {};
  const held = Boolean(lock.exists && lock.alive);
  const stale = Boolean(lock.exists && !lock.alive);
  const holder = lock.holder || {};
  const lockBadge = held
    ? '<span class="badge run">锁被占用</span>'
    : stale
      ? '<span class="badge warn">僵死锁（可自动接管）</span>'
      : '<span class="badge ok">GPU 空闲</span>';
  return `
    <div class="card" style="margin:14px 18px 0">
      <div class="card-head"><span class="ct">GPU 与并发</span>
        <div class="right">${lockBadge}${
          gpu.paused ? '<span class="badge warn">队列已暂停</span>' : ''
        }</div>
      </div>
      <div class="card-body">
        <div class="summary-line"><span>锁文件</span>
          <span class="right mono" style="max-width:70%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
            title="${esc(lock.path || '未配置 gpu.lock_path')}">${esc(lock.path || '未配置')}</span></div>
        ${
          held || stale
            ? `<div class="summary-line"><span>持有人</span>
                 <span class="right mono">pid ${esc(String(holder.pid || '?'))}${
                   holder.job_id ? ` · 任务 ${esc(String(holder.job_id).slice(-8))}` : ''
                 }${holder.started_at ? ` · ${esc(holder.started_at)}` : ''}</span></div>`
            : ''
        }
        <div class="summary-line"><span>队列</span>
          <span class="right">排队 <b class="mono">${Number(counts.queued || 0)}</b>
            · 运行 <b class="mono">${Number(counts.running || 0)}</b>
            · 失败 <b class="mono" style="color:var(--bad)">${Number(counts.failed || 0)}</b>
            · 成功 <b class="mono">${Number(counts.succeeded || 0)}</b></span></div>
        <div class="hint" style="margin-top:6px">
          同时最多跑 ${Number(gpu.max_concurrent || 1)} 个 GPU 任务，抢锁轮询 ${Number(
            gpu.poll_interval_s || 0
          )} 秒；与 ComfyUI 共用同一把锁。
        </div>
      </div>
    </div>`;
}

/** 队列卡 */
function queueHTML() {
  const rows = visibleQueue();
  const countOf = (key) => {
    if (key === 'all') return queue.length;
    if (key === 'active') return queue.filter((item) => ['queued', 'running'].includes(item.status)).length;
    return queue.filter((item) => item.status === key).length;
  };
  const emptyText = {
    all: '任务队列是空的：配音、音效、配图任务开始后会出现在这里。',
    active: '没有排队或运行中的任务。',
    failed: '没有失败的任务。',
    succeeded: '还没有成功的任务记录。',
  }[queueFilter];
  return `
    <div class="card" style="margin:14px 18px 18px">
      <div class="card-head"><span class="ct">任务队列</span>
        <div class="right" style="display:flex;gap:4px">
          ${[
            ['all', '全部'],
            ['active', '进行中'],
            ['failed', '失败'],
            ['succeeded', '成功'],
          ]
            .map(
              ([key, label]) =>
                `<button class="chip sm ${queueFilter === key ? 'on' : ''}" data-qfilter="${key}"
                  title="按任务状态筛选队列">${label}<span class="n">${countOf(key)}</span></button>`
            )
            .join('')}
        </div>
      </div>
      <div class="card-body" style="padding:0">
        <table class="files">
          <thead><tr><th>类型</th><th>目标</th><th>状态</th><th>尝试</th><th>产物</th><th>错误</th></tr></thead>
          <tbody>
            ${
              rows.length
                ? rows
                    .slice(0, 200)
                    .map(
                      (item) => `<tr>
                        <td>${esc(QUEUE_LABEL[item.kind] || item.kind || '')}</td>
                        <td class="mono">${esc((item.target?.line_id || item.target?.scene_id || '').slice(-8) || '—')}</td>
                        <td><span class="badge ${badgeClass(item.status)}">${esc(
                          STATUS_LABEL[item.status] || item.status
                        )}</span></td>
                        <td class="mono">${Number(item.attempts || 0)}</td>
                        <td class="mono">${(item.artifacts || []).length}</td>
                        <td style="color:var(--bad)">${esc(item.error || '')}</td>
                      </tr>`
                    )
                    .join('')
                : `<tr><td colspan="6" style="color:var(--text-4);padding:16px 12px">${esc(emptyText)}</td></tr>`
            }
          </tbody>
        </table>
      </div>
    </div>`;
}

/** 主区 */
function mainHTML() {
  const chapter = state.chapters.find((item) => item.id === state.chapterId);
  const ready = readiness?.ready;
  return `
    ${resplitBannerHTML(consistency)}
    <div class="page-head">
      <div class="ph">
        <div class="eyebrow"><span class="ln"></span>任务</div>
        <h1 class="page-title">${esc(chapter?.title || '本章任务')}</h1>
        <div class="page-sub">批量进度、失败重试与队列状态。运行中每 3 秒自动刷新。</div>
      </div>
      <div class="ph-actions">
        <span class="badge ${ready ? 'ok' : 'bad'}">${ready ? '配音引擎就绪' : esc(readiness?.reason || '引擎不可用')}</span>
      </div>
    </div>
    ${progressHTML()}
    ${produceHTML()}
    ${gpuHTML()}
    ${queueHTML()}`;
}

/** 重画 */
function repaint() {
  const main = document.getElementById('task-main');
  if (main) main.innerHTML = mainHTML();
}

/** 检查本章正文与切分是否一致（只读，不阻塞任务刷新） */
async function refreshConsistency() {
  consistency = await checkConsistency(state.path, state.chapterId);
}

/** 拉取数据 */
async function refresh({ silent = true } = {}) {
  try {
    const [progressData, queueData, pauseData, gpuData, produceData] = await Promise.all([
      getJSON('/api/ui/tts/batch-all/progress', { path: state.path }),
      getJSON('/api/ui/queue', { path: state.path }),
      getJSON('/api/ui/tts/pause', { path: state.path }),
      getJSON('/api/ui/gpu/status', { path: state.path }).catch(() => null),
      getJSON('/api/ui/produce/latest', { path: state.path }).catch(() => null),
    ]);
    progress = progressData;
    queue = Array.isArray(queueData) ? queueData : queueData?.jobs || [];
    paused = Boolean(pauseData?.paused);
    gpu = gpuData;
    produceRun = produceData?.run || null;
    repaint();
    // 运行中加快刷新，停止后回到 8 秒
    window.clearTimeout(timer);
    timer = window.setTimeout(
      () => refresh({ silent: true }),
      progress?.running ? 3000 : 8000
    );
    if (!silent) toast('已刷新', 'ok');
  } catch (error) {
    if (!silent) toast(error.message, 'bad');
    window.clearTimeout(timer);
    timer = window.setTimeout(() => refresh({ silent: true }), 8000);
  }
}

/** 停止生成 */
async function stopAll() {
  try {
    const result = await postJSON('/api/ui/stop', { path: state.path });
    toast(result?.stopped ? '已停止生成' : '当前没有在跑的任务', result?.stopped ? 'ok' : 'info');
    await refresh();
  } catch (error) {
    toast(error.message, 'bad');
  }
}

/** 暂停 / 继续生成 */
async function togglePause() {
  try {
    await postJSON('/api/ui/tts/pause', { path: state.path, paused: !paused });
    paused = !paused;
    toast(paused ? '已暂停：当前行完成后续跑' : '已继续生成', 'ok');
    await refresh();
  } catch (error) {
    toast(error.message, 'bad');
  }
}

/** 生成参数 / 影响清单弹窗 */
function openPlanDialog(plan) {
  const total = Number(plan?.total || 0);
  const unknownRows = plan?.unknown_rows || [];
  const blockers = plan?.blockers || [];
  const mask = openDialog({
    title: '补齐缺失配音',
    body: `
      <div class="summary-line"><span>本次生成</span><span class="right">${total} 行</span></div>
      <div class="summary-line"><span>跳过（已配 / 未识别）</span><span class="right">${Number(plan?.skipped || 0)} 行</span></div>
      <div class="summary-line"><span>预计耗时</span><span class="right">${plan?.eta_ms ? fmtDuration(plan.eta_ms) : '未知'}</span></div>
      ${impactListHTML(plan, state.characters)}
      ${
        unknownRows.length
          ? `<div class="dlg-note warn">未识别角色 ${unknownRows.length} 行，会被跳过：<br />${unknownRows
              .slice(0, 5)
              .map((row) => `· ${esc(row.chapter_title || '')} ${esc((row.line_id || '').slice(-6))}`)
              .join('<br />')}${unknownRows.length > 5 ? '<br />…' : ''}</div>`
          : ''
      }
      ${blockers.length ? `<div class="dlg-note bad">阻塞：${blockers.map((item) => esc(item.message || item)).join('；')}</div>` : ''}`,
    foot: `<div class="right"><button class="btn" data-dlg-close>取消</button>
      <button class="btn primary" id="plan-ok" ${total ? '' : 'disabled'}>开始生成 ${total} 行</button></div>`,
  });
  mask.querySelector('#plan-ok').addEventListener('click', async (event) => {
    event.currentTarget.disabled = true;
    try {
      await postJSON('/api/ui/tts/batch-all', { path: state.path, scope: 'chapter', chapter: state.chapterId });
      closeDialog();
      toast(`已开始补齐 ${total} 行`, 'ok');
      await refresh();
    } catch (error) {
      toast(error.message, 'bad');
      event.currentTarget.disabled = false;
    }
  });
}

/** 点击 */
async function onRootClick(event) {
  // 监听绑在共享根节点上：离开任务页后不再处理，避免抢走其它页面的同名操作
  if (!document.getElementById('task-main')) return;
  const chapterRow = event.target.closest('[data-chapter]');
  if (chapterRow) {
    state.chapterId = chapterRow.dataset.chapter;
    const ctx = document.querySelector('.ctx');
    if (ctx) ctx.outerHTML = chapterCtx({ search: state.chapterSearch });
    await refreshConsistency();
    await refresh();
    return;
  }
  const filterButton = event.target.closest('[data-qfilter]');
  if (filterButton) {
    queueFilter = filterButton.dataset.qfilter;
    repaint();
    return;
  }
  const button = event.target.closest('[data-act]');
  if (!button) return;
  const act = button.dataset.act;
  if (act === 'back') return goProjects();
  if (act === 'to-editor') return goChapter(state.path, state.chapterId);
  if (act === 'refresh') return refresh({ silent: false });
  if (act === 'stop') return stopAll();
  if (act === 'pause') return togglePause();
  if (act === 'batch-plan') {
    button.disabled = true;
    try {
      const plan = await getJSON('/api/ui/tts/batch-all/plan', {
        path: state.path,
        scope: 'chapter',
        chapter: state.chapterId,
      });
      openPlanDialog(plan);
    } catch (error) {
      toast(error.message, 'bad');
    } finally {
      button.disabled = false;
    }
    return;
  }
  if (act === 'retry') {
    const lineId = button.dataset.line;
    if (!lineId) return toast('这一行缺少 ID，无法重试', 'bad');
    button.disabled = true;
    try {
      await postJSON('/api/ui/line/rerun', { path: state.path, chapter: state.chapterId, line: lineId });
      toast('这一行已重新生成', 'ok');
      await refresh();
    } catch (error) {
      toast(error.message, 'bad');
      button.disabled = false;
    }
    return;
  }
  if (act === 'produce-start') {
    const from = document.getElementById('prod-from')?.value || 'setting';
    const to = document.getElementById('prod-to')?.value || 'export';
    const force = Boolean(document.getElementById('prod-force')?.checked);
    const order = PRODUCE_STAGES.map(([key]) => key);
    if (order.indexOf(from) > order.indexOf(to)) {
      return toast('「从」阶段必须排在「到」阶段之前', 'warn');
    }
    button.disabled = true;
    try {
      const result = await postJSON('/api/ui/produce', {
        path: state.path,
        chapter: state.chapterId,
        from,
        to,
        force,
      });
      toast(`编排已启动（pid ${result.pid || '—'}），进度会自动刷新`, 'ok', 4200);
      window.setTimeout(() => refresh({ silent: true }), 1500);
    } catch (error) {
      toast(error.message, 'bad', 4800);
      button.disabled = false;
    }
  }
}

/** 输入（章节目录搜索） */
function onRootInput(event) {
  // 同上：离开任务页后不再处理共享根节点上的输入
  if (!document.getElementById('task-main')) return;
  if (event.target.id === 'chapter-search') {
    state.chapterSearch = event.target.value;
    const ctx = document.querySelector('.ctx');
    if (ctx) ctx.outerHTML = chapterCtx({ search: state.chapterSearch });
    const input = document.getElementById('chapter-search');
    if (input) {
      input.focus();
      input.setSelectionRange(input.value.length, input.value.length);
    }
  }
}

/** 离开任务页时停掉轮询 */
export function leaveTasks() {
  window.clearTimeout(timer);
  timer = null;
  onResplit(null);
}

/** 进入任务视图 */
export async function renderTasks(root, { path }) {
  if (state.path !== path) resetProject(path);
  if (!bound) {
    root.addEventListener('click', onRootClick);
    root.addEventListener('input', onRootInput);
    bound = true;
  }
  root.innerHTML = `${topbar({ left: '', crumbs: '<span class="cur">正在加载任务…</span>', right: '' })}`;
  try {
    const [info, chapters, ready] = await Promise.all([
      getJSON('/api/project/info', { path }),
      getJSON('/api/ui/chapters', { path }),
      getJSON('/api/ui/readiness', { path }).catch(() => null),
    ]);
    state.info = info;
    state.chapters = chapters || [];
    readiness = ready;
    if (!state.chapters.some((item) => item.id === state.chapterId)) {
      state.chapterId = state.chapters[0]?.id || '';
    }
    if (!state.chapterId) {
      root.innerHTML = `<div class="page"><div class="empty">${icon('warn', 22)}<b>这个工程还没有章节</b></div></div>`;
      return;
    }
  } catch (error) {
    root.innerHTML = `<div class="page"><div class="empty">${icon('warn', 22)}<b>任务加载失败</b>
      <span>${esc(error.message)}</span><a class="btn" href="#/projects" style="margin-top:10px">回工作区</a></div></div>`;
    return;
  }
  root.innerHTML = `
    ${topbar({
      left: `<button class="btn icon" data-act="back" title="回工作区">${icon('back')}</button>
             <div class="brand"><span class="brand-mark"></span><span style="font-size:12.5px">任务<small>本地版</small></span></div>`,
      crumbs: `<a href="#/projects">${esc(state.info?.work?.title || '工程')}</a>
               <span class="sep">/</span><span class="cur">任务</span>`,
      right: `<button class="btn" data-act="to-editor">${icon('chapter')} 回画本</button>`,
    })}
    <div class="studio" id="studio">
      ${railbar('tasks', { chapters: state.chapters.length })}
      ${chapterCtx({ search: state.chapterSearch })}
      <section class="editor">
        <div id="task-main" style="display:flex;flex-direction:column;min-height:0;flex:1;overflow:auto">${mainHTML()}</div>
      </section>
      <aside class="studio-right"></aside>
    </div>`;
  await refreshConsistency();
  await refresh();
  // 顶部「重新切分本章」：完成后复核一致性并刷新进度
  onResplit(async () => {
    await refreshConsistency();
    await refresh();
  });
}
