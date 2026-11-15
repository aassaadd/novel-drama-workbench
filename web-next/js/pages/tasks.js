/**
 * 任务视图（工程级）：任务队列、全链路编排、GPU 与并发、写作模型。
 *
 * 本页不绑定任何章节：不显示章节标题、不渲染章节目录、不出现「本章配音进度」这类按章统计；
 * 章节内的配音进度与失败行重试放在工作室的章节侧视图（画本右栏「本章任务」、录音 / 后期视图）。
 *
 * 数据来自队列与编排接口；运行中每 3 秒刷新一次，只替换内容有变化的卡片，
 * 不整页重绘，避免任务列表闪动和丢失滚动位置。离开页面即停轮询。
 */
import { esc, icon, toast, openDialog, closeDialog } from '../dom.js';
import { getJSON, postJSON } from '../api.js';
import { openProgress } from '../progress.js';
import { confirmLlm, llmErrorText } from '../llm.js';
import { modeSwitchHTML, bindModeSwitch, getGenMode } from '../gen-mode.js';
import { state, resetProject } from '../store.js';
import { railbar, topbar, emptyProjectHTML, goProjects } from '../shell.js';

const QUEUE_LABEL = {
  tts: '配音',
  sfx: '音效',
  image: '配图',
  video: '视频',
  render: '渲染',
  chapter: '章节正文',
  act: '整幕正文',
  'chapter.continue': '正文续写',
  world: '世界观',
  outline: '大纲',
  setting: '写作设定',
  character: '角色',
  'character.desc': '角色描述',
  script: '导演标注',
  voice: '音色',
  'voice.fill': '补齐音色',
  produce: '全链路编排',
};

const STATUS_LABEL = {
  queued: '排队',
  running: '运行中',
  succeeded: '成功',
  partial: '部分完成',
  failed: '失败',
  canceled: '已取消',
  interrupted: '已中断',
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

/** 工程级阶段：不需要章节，整本工程只跑一次（与后端 PROJECT_STAGES 一致） */
const PROJECT_STAGES = new Set(['setting', 'outline']);
/** 阶段中文名 */
const PRODUCE_LABEL = Object.fromEntries(PRODUCE_STAGES);

/**
 * 阶段区间（含两端）内的阶段 key 列表。
 * @param {string} from 起始阶段
 * @param {string} to 结束阶段
 * @returns {string[]} 阶段 key
 */
function stageRange(from, to) {
  const order = PRODUCE_STAGES.map(([key]) => key);
  const start = Math.max(0, order.indexOf(from));
  const end = order.indexOf(to);
  return end < start ? [] : order.slice(start, end + 1);
}

let queue = [];
let readiness = null;
/** 最近一次全链路编排记录（`run show`） */
let produceRun = null;
/** GPU 锁与并发明细（锁文件持有人、队列计数） */
let gpu = null;
let queueFilter = 'all';
let timer = null;
let bound = false;
/** 写作模型自查状态（进入任务页加载一次；点「检测连接」重新探测，不参与 3 秒轮询） */
let llmStatus = null;
let llmChecking = false;
/** 各卡片上次渲染的 HTML：内容没变就不动 DOM，避免轮询时列表闪动 */
const lastRegion = {};

/** 状态徽标语义色 */
function badgeClass(name) {
  if (name === 'succeeded' || name === 'ready') return 'ok';
  if (name === 'partial') return 'warn';
  if (name === 'failed') return 'bad';
  if (name === 'running') return 'run';
  if (name === 'queued' || name === 'pending' || name === 'interrupted' || name === 'canceled') return 'mute';
  return '';
}

/**
 * 把后端错误统一转成可读文本（错误可能是字符串，也可能是 {message} 对象）。
 * @param {any} value 错误值
 * @returns {string} 中文错误文本
 */
function errorText(value) {
  if (!value) return '';
  if (typeof value === 'string') return value;
  if (typeof value === 'object') {
    return String(value.message || value.detail?.message || value.detail || value.reason || '').trim() ||
      JSON.stringify(value);
  }
  return String(value);
}

/**
 * 队列错误列的简短文本：多行错误（退出码 + 日志尾部）只留前两行。
 * @param {any} value 错误值
 * @returns {string} 两行以内的错误摘要
 */
function errorSummary(value) {
  const text = errorText(value);
  const rows = text.split('\n').map((row) => row.trim()).filter(Boolean);
  return rows.slice(0, 2).join(' / ');
}

/** 过滤队列 */
function visibleQueue() {
  if (queueFilter === 'all') return queue;
  if (queueFilter === 'active') return queue.filter((item) => ['queued', 'running'].includes(item.status));
  return queue.filter((item) => item.status === queueFilter);
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
          <td style="color:${bad ? 'var(--bad)' : 'var(--text-3)'};font-size:11.5px">${esc(
            errorText(item.error) || item.note || ''
          )}</td>
        </tr>`;
    })
    .join('');
  return `
    <div class="card" style="margin:14px 18px 0">
      <div class="card-head"><span class="ct">全链路编排</span>
        <div class="right">${
          run
            ? `<span class="badge ${run.status === 'succeeded' ? 'ok' : run.status === 'failed' ? 'bad' : 'run'}">
                 ${esc(PRODUCE_STATUS[run.status] || '未识别')} · ${doneCount}/${stages.length}${
                   run.scope === 'book'
                     ? ` · 全书 ${(run.chapters || []).length} 章`
                     : run.scope === 'chapter'
                       ? ' · 指定章节'
                       : ' · 工程级'
                 }${run.run_id ? ` · ${esc(String(run.run_id).slice(-6))}` : ''}
               </span>`
            : '<span class="badge mute">还没有编排记录</span>'
        }</div>
      </div>
      <div class="card-body">
        <div style="display:grid;grid-template-columns:1fr 1fr auto auto;gap:10px;align-items:end">
          <div class="field"><label>从</label>
            <select class="select" id="prod-from" data-keep="prod-from" data-enum="produce_stage"
              title="起始阶段，只能从列表选择">${options('setting')}</select></div>
          <div class="field"><label>到</label>
            <select class="select" id="prod-to" data-keep="prod-to" data-enum="produce_stage"
              title="结束阶段，只能从列表选择">${options('export')}</select></div>
          ${modeSwitchHTML('produce', { fallback: 'fill' })}
          <button class="btn primary" data-act="produce-start"
            title="后台启动 ndw produce，按记录续跑已完成的阶段">启动编排</button>
        </div>
        <div class="hint" style="margin-top:6px">
          ${
            state.chapters.length
              ? '只补缺失：已完成的阶段会跳过；重新生成：忽略已有产物并强制重跑全部阶段。'
              : '工程还没有章节：设定库 / 大纲可以直接跑；正文及之后的阶段需要先在小说视图落地章节。'
          }运行期间本卡片随任务轮询一起刷新。
        </div>
        ${
          stages.length
            ? `<table class="files" style="margin-top:10px">
                 <thead><tr><th>阶段</th><th>状态</th><th>产物</th><th>说明</th></tr></thead>
                 <tbody>${rows}</tbody>
               </table>`
            : '<div class="empty" style="padding:18px 12px"><b>还没有编排记录</b><span>选择阶段区间后点「启动编排」，这里会逐阶段显示状态与产物数量。</span></div>'
        }
      </div>
    </div>`;
}

/** GPU 锁与并发卡：锁持有人 + 队列计数；读不到状态时也给可读空态，不整块隐藏 */
function gpuHTML() {
  if (!gpu) {
    return `
      <div class="card" style="margin:14px 18px 0">
        <div class="card-head"><span class="ct">GPU 与并发</span>
          <div class="right"><span class="badge mute">读取中</span></div>
        </div>
        <div class="card-body">
          <div class="empty" style="padding:18px 12px"><b>暂时读不到 GPU 状态</b>
            <span>接口不可用或工程还没初始化；恢复后本卡片会随轮询自动刷新。</span></div>
        </div>
      </div>`;
  }
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
          ${esc(lock.note || '与 ComfyUI 共用同一把锁。')}
        </div>
        <div class="hint" style="margin-top:4px">
          同时最多跑 ${Number(gpu.max_concurrent || 1)} 个 GPU 任务，抢锁轮询 ${Number(
            gpu.poll_interval_s || 0
          )} 秒；与 ComfyUI 共用同一把锁。
        </div>
        ${
          stale
            ? `<div style="margin-top:10px">
                 <button class="btn danger" data-act="gpu-release"
                   title="只会删除持锁进程已不存在的锁；活锁会被拒绝">确认僵死并释放锁</button>
               </div>`
            : ''
        }
      </div>
    </div>`;
}

/** 队列卡：工程内所有任务的运行中 / 排队 / 完成 / 失败状态与产物 */
function queueHTML() {
  const rows = visibleQueue();
  const countOf = (key) => {
    if (key === 'all') return queue.length;
    if (key === 'active') return queue.filter((item) => ['queued', 'running'].includes(item.status)).length;
    return queue.filter((item) => item.status === key).length;
  };
  const emptyText = {
    all: '任务队列是空的：大纲、正文、世界、角色、标注、素材、配音等任务开始后会出现在这里。',
    active: '没有排队或运行中的任务。',
    failed: '没有失败的任务。',
    partial: '没有部分完成的任务。',
    succeeded: '还没有成功的任务记录。',
  }[queueFilter];

  /**
   * 队列目标：优先显示章节标题 / 自定义标签，其次才是行、场景 ID。
   * @param {object} item 队列任务
   */
  const targetText = (item) => {
    const target = item.target || {};
    if (target.chapter_title) return target.chapter_title;
    if (target.label) return String(target.label);
    const id = target.line_id || target.scene_id || target.chapter_id || '';
    return id ? id.slice(-8) : '—';
  };

  /**
   * 进度列：有百分比显示百分比 + 当前项，没有则显示当前阶段。
   * @param {object} item 队列任务
   */
  const progressText = (item) => {
    const info = item.progress || {};
    const percent = info.percent;
    if (typeof percent === 'number') {
      return `${Math.round(percent)}%${info.current ? ` · ${info.current}` : ''}`;
    }
    return info.current || '—';
  };
  const terminalCount = queue.filter((item) =>
    ['succeeded', 'partial', 'failed', 'canceled', 'interrupted'].includes(item.status)
  ).length;
  return `
    <div class="card" style="margin:14px 18px 0">
      <div class="card-head"><span class="ct">任务队列</span>
        <div class="right" style="display:flex;gap:4px;align-items:center">
          ${[
            ['all', '全部'],
            ['active', '进行中'],
            ['failed', '失败'],
            ['partial', '部分完成'],
            ['succeeded', '成功'],
          ]
            .map(
              ([key, label]) =>
                `<button class="chip sm ${queueFilter === key ? 'on' : ''}" data-qfilter="${key}"
                  title="按任务状态筛选队列">${label}<span class="n">${countOf(key)}</span></button>`
            )
            .join('')}
          ${
            terminalCount
              ? `<button class="btn sm" data-act="queue-clear"
                   title="只删除已结束的任务记录，不会删除正文、音频等产物">清理已结束（${terminalCount}）</button>`
              : ''
          }
        </div>
      </div>
      <div class="card-body" style="padding:0">
        <table class="files">
          <thead><tr><th>类型</th><th>目标</th><th>状态</th><th>进度</th><th>尝试</th><th>产物</th><th>错误</th><th>操作</th></tr></thead>
          <tbody>
            ${
              rows.length
                ? rows
                    .slice(0, 200)
                    .map((item) => {
                      // 只有界面发起、后端能停的任务才给停止按钮（GPU 队列任务不在本进程）
                      const stoppable = Boolean(item.ui) && ['running', 'queued'].includes(item.status);
                      return `<tr>
                        <td>${esc(QUEUE_LABEL[item.kind] || item.kind || '')}</td>
                        <td title="${esc(targetText(item))}">${esc(targetText(item))}</td>
                        <td><span class="badge ${badgeClass(item.status)}">${esc(
                          STATUS_LABEL[item.status] || item.status
                        )}</span></td>
                        <td style="font-size:11.5px;color:var(--text-3)">${esc(progressText(item))}</td>
                        <td class="mono">${Number(item.attempts || 0)}</td>
                        <td class="mono">${(item.artifacts || []).length}</td>
                        <td style="color:var(--bad);white-space:pre-line;max-width:340px"
                          title="${esc(errorText(item.error))}">${esc(errorSummary(item.error))}</td>
                        <td>${
                          stoppable
                            ? `<button class="btn sm danger" data-act="job-stop" data-job="${esc(item.id)}"
                                 title="停止这条运行中的任务，并把状态落为已取消">停止</button>`
                            : ''
                        }</td>
                      </tr>`;
                    })
                    .join('')
                : `<tr><td colspan="8" style="color:var(--text-4);padding:16px 12px">${esc(emptyText)}</td></tr>`
            }
          </tbody>
        </table>
      </div>
    </div>`;
}

/**
 * 写作模型自查卡：现在生效的 provider / 地址 / 模型、是否连通、最近一次真实调用的证据。
 * @returns {string} 卡片 HTML
 */
function llmHTML() {
  const data = llmStatus;
  const last = (data && data.last_call) || null;
  let badge = '<span class="badge mute">未检测</span>';
  if (llmChecking) {
    badge = '<span class="badge run">检测中…</span>';
  } else if (data) {
    if (!data.connected) badge = '<span class="badge bad">连不上</span>';
    else if (!data.model_found) badge = '<span class="badge bad">已连通 · 模型不可用</span>';
    else badge = `<span class="badge ok">已连通${data.latency_ms != null ? ` · ${data.latency_ms}ms` : ''}</span>`;
  }
  const line = (label, value, mono = false) => `
        <div style="display:flex;gap:8px;font-size:12px;line-height:1.9">
          <span style="color:var(--text-4);min-width:76px">${label}</span>
          <span class="${mono ? 'mono' : ''}" style="word-break:break-all">${esc(value || '—')}</span>
        </div>`;
  const lastText = last
    ? `${last.at || '—'} · HTTP ${last.status ?? '—'} · ${last.elapsed_ms ?? '—'}ms · ${
        last.ok ? '成功' : '失败'
      } · tool_calls=${last.tool_calls ?? 0}${last.finish_reason ? ` · finish=${last.finish_reason}` : ''}`
    : '';
  const lastSnippet = last && (last.snippet || last.error) ? (last.snippet || last.error) : '';
  return `
    <div class="card" style="margin:14px 18px 18px">
      <div class="card-head"><span class="ct">写作模型</span>
        <div class="right" style="display:flex;gap:8px;align-items:center">
          ${badge}
          <button class="btn sm" data-act="llm-check" ${llmChecking ? 'disabled' : ''}
            title="探测当前配置的服务地址与模型，并读取最近一次真实调用记录">检测连接</button>
        </div>
      </div>
      <div class="card-body" style="padding:10px 14px">
        ${line('服务提供方', data ? data.provider || '未配置' : '加载中…')}
        ${line('服务地址', data ? data.endpoint : '', true)}
        ${line('使用模型', data ? data.model : '', true)}
        ${line('检测结论', data ? data.message : '进入页面后自动检测一次', true)}
        ${line('最近调用', lastText || '本次进程还没有发出过写作请求', true)}
        ${
          last
            ? `${line('请求地址', last.url, true)}${
                lastSnippet
                  ? `<div class="mono" style="font-size:11.5px;color:var(--text-3);white-space:pre-wrap;word-break:break-all;margin-top:4px;max-height:72px;overflow:auto">${esc(
                      String(lastSnippet).slice(0, 300)
                    )}</div>`
                  : ''
              }`
            : ''
        }
      </div>
    </div>`;
}

/**
 * 探测写作模型（POST /api/ui/llm/status）。
 * @returns {Promise<void>}
 */
async function refreshLlm() {
  if (llmChecking) return;
  llmChecking = true;
  paintRegions(['llm']);
  try {
    llmStatus = await postJSON('/api/ui/llm/status', {});
  } catch (error) {
    llmStatus = { ok: false, connected: false, message: `检测失败：${error.message}` };
  } finally {
    llmChecking = false;
  }
  paintRegions(['llm']);
}

/** 主区：工程级标题 + 四块卡片容器 */
function mainHTML() {
  const ready = readiness?.ready;
  return `
    ${state.chapters.length ? '' : emptyProjectHTML(state.path, { compact: true })}
    <div class="page-head" style="margin:14px 18px 0">
      <div class="ph">
        <div class="eyebrow"><span class="ln"></span>工程任务</div>
        <h1 class="page-title">任务中心</h1>
        <div class="page-sub">任务队列、全链路编排、GPU 与并发、写作模型。运行中每 3 秒自动刷新，只更新变化的卡片。</div>
      </div>
      <div class="ph-actions">
        <span class="badge ${ready ? 'ok' : readiness ? 'bad' : 'mute'}">${
          readiness ? (ready ? '配音引擎就绪' : esc(readiness.reason || '引擎不可用')) : '正在读取配音引擎状态…'
        }</span>
      </div>
    </div>
    <div id="task-queue">${queueHTML()}</div>
    <div id="task-produce">${produceHTML()}</div>
    <div id="task-gpu">${gpuHTML()}</div>
    <div id="task-llm">${llmHTML()}</div>`;
}

/** 卡片构建器：key → HTML 生成函数 */
const REGION_BUILDERS = {
  queue: queueHTML,
  produce: produceHTML,
  gpu: gpuHTML,
  llm: llmHTML,
};

/**
 * 只重画内容有变化的卡片。
 * 内容一致的卡片保持原 DOM，轮询刷新时不会整页重绘，任务列表不闪、滚动位置不丢。
 * 编排卡里的阶段下拉是用户选择，重画后要恢复，避免被默认值覆盖。
 * @param {string[]} [force] 强制重画的卡片 key
 */
function paintRegions(force = []) {
  for (const [key, build] of Object.entries(REGION_BUILDERS)) {
    const node = document.getElementById(`task-${key}`);
    if (!node) continue;
    const html = build();
    if (!force.includes(key) && lastRegion[key] === html) continue;
    const keep = {};
    node.querySelectorAll('select[data-keep]').forEach((sel) => {
      keep[sel.dataset.keep] = sel.value;
    });
    node.innerHTML = html;
    node.querySelectorAll('select[data-keep]').forEach((sel) => {
      if (keep[sel.dataset.keep] !== undefined) sel.value = keep[sel.dataset.keep];
    });
    lastRegion[key] = html;
    if (key === 'produce') bindProduceMode();
  }
}

/**
 * 全链路编排卡每次重画后重新绑定生成方式（分段按钮 + 启动按钮文案）。
 * 生成方式存在 localStorage，轮询重画不会丢。
 */
function bindProduceMode() {
  const main = document.getElementById('task-main');
  if (!main) return;
  bindModeSwitch(main, 'produce', {
    fallback: 'fill',
    action: main.querySelector('[data-act="produce-start"]'),
    labels: { fill: '只补缺失阶段', regen: '强制重跑全部' },
  });
}

/** 队列里是否还有运行中 / 排队中的任务（决定刷新频率） */
function hasActiveJobs() {
  return queue.some((item) => ['queued', 'running'].includes(item.status)) || produceRun?.status === 'running';
}

/** 拉取数据 */
async function refresh({ silent = true } = {}) {
  // 已离开任务页（或请求在离开后才返回）时不再轮询，避免后台持续打接口
  if (!document.getElementById('task-main')) return;
  try {
    const [queueData, gpuData, produceData] = await Promise.all([
      getJSON('/api/ui/queue', { path: state.path }),
      getJSON('/api/ui/gpu/status', { path: state.path }).catch(() => null),
      getJSON('/api/ui/produce/latest', { path: state.path }).catch(() => null),
    ]);
    queue = Array.isArray(queueData) ? queueData : queueData?.jobs || [];
    gpu = gpuData;
    produceRun = produceData?.run || null;
    paintRegions();
    // 运行中加快刷新，空闲时放慢
    window.clearTimeout(timer);
    timer = window.setTimeout(() => refresh({ silent: true }), hasActiveJobs() ? 3000 : 8000);
    if (!silent) toast('已刷新', 'ok');
  } catch (error) {
    if (!silent) toast(error.message, 'bad');
    paintRegions();
    window.clearTimeout(timer);
    timer = window.setTimeout(() => refresh({ silent: true }), 8000);
  }
}

/**
 * 停止一条运行中的任务。
 * 生成线程会协作取消（不再写进度/产物）；编排任务直接杀子进程；
 * 后端已无对应进程的僵尸任务可以强制标记为已取消。
 * @param {string} jobId 任务 ID
 * @param {HTMLButtonElement} [button] 触发的按钮
 */
async function stopJob(jobId, button) {
  const item = queue.find((row) => row.id === jobId) || {};
  const kind = QUEUE_LABEL[item.kind] || item.kind || '任务';
  const info = item.progress || {};
  const progressLabel = `${typeof info.percent === 'number' ? `${Math.round(info.percent)}% · ` : ''}${
    info.current || '—'
  }`;
  const mask = openDialog({
    title: '停止任务',
    body: `
      <div class="summary-line"><span>任务</span>
        <span class="right">${esc(kind)} · <span class="mono">${esc(jobId)}</span></span></div>
      <div class="summary-line"><span>当前进度</span>
        <span class="right">${esc(progressLabel)}</span></div>
      <div class="dlg-note">停止后会中断后台生成（编排类任务会杀掉子进程），任务落「已取消」；
        已接收的字数、已完成批次与产物都会保留，停止后不会再写入或覆盖已有结果。
        如果后端已经没有对应进程（僵尸任务），请用「强制标记为已取消」。</div>`,
    foot: `<div class="right"><button class="btn" data-dlg-close>返回</button>
      <button class="btn" id="job-stop-force">强制标记为已取消</button>
      <button class="btn danger" id="job-stop-ok">确认停止</button></div>`,
  });
  /**
   * 提交停止请求并给出进行中 / 结果反馈。
   * @param {boolean} force true=不等生成线程，直接落取消
   * @param {HTMLButtonElement} target 当前点击的按钮
   */
  const submit = async (force, target) => {
    const buttons = [...mask.querySelectorAll('button')];
    const label = target.textContent;
    buttons.forEach((node) => (node.disabled = true));
    target.textContent = force ? '正在强制取消…' : '正在停止…';
    try {
      const result = await postJSON('/api/ui/jobs/cancel', { path: state.path, id: jobId, force });
      closeDialog();
      toast(result.message || (force ? '已强制标记为已取消' : '已停止任务'), result.canceled === false ? 'info' : 'ok', 4800);
    } catch (error) {
      // 失败保留弹窗，恢复按钮，让用户可以重试或改用强制取消
      buttons.forEach((node) => (node.disabled = false));
      target.textContent = label;
      toast(error.message, 'bad', 5600);
    }
    if (button) button.disabled = false;
    await refresh({ silent: true });
  };
  mask.querySelector('#job-stop-ok').addEventListener('click', (event) => submit(false, event.currentTarget));
  mask.querySelector('#job-stop-force').addEventListener('click', (event) => submit(true, event.currentTarget));
}

/** 清理已结束的队列行（不动正文/音频产物） */
async function clearQueue(button) {
  button.disabled = true;
  try {
    const result = await postJSON('/api/ui/jobs/clear', { path: state.path });
    toast(result.message || '已清理', 'ok');
  } catch (error) {
    toast(error.message, 'bad', 5200);
  }
  button.disabled = false;
  await refresh({ silent: true });
}

/** 释放僵死的 GPU 锁（持锁进程仍存活时后端会拒绝） */
async function releaseGpuLock(button) {
  const lock = gpu?.lock || {};
  const holder = lock.holder || {};
  const mask = openDialog({
    title: '释放僵死的 GPU 锁',
    body: `
      <div class="summary-line"><span>锁文件</span>
        <span class="right mono" style="max-width:70%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
          title="${esc(lock.path || '')}">${esc(lock.path || '未配置')}</span></div>
      <div class="summary-line"><span>锁内记录</span>
        <span class="right mono">pid ${esc(String(holder.pid || '?'))}${
          holder.job_id ? ` · 任务 ${esc(String(holder.job_id).slice(-8))}` : ''
        }${holder.started_at ? ` · ${esc(holder.started_at)}` : ''}</span></div>
      <div class="dlg-note">只有确认持锁进程已经退出时才需要手动释放；如果它还在运行，后端会拒绝并说明原因。正常情况下一次 GPU 任务开始时会自动接管僵死锁。</div>`,
    foot: `<div class="right"><button class="btn" data-dlg-close>取消</button>
      <button class="btn danger" id="gpu-release-ok">确认释放</button></div>`,
  });
  mask.querySelector('#gpu-release-ok').addEventListener('click', async (event) => {
    event.currentTarget.disabled = true;
    try {
      const result = await postJSON(
        `/api/ui/gpu/lock/release?path=${encodeURIComponent(state.path)}`,
        {}
      );
      closeDialog();
      toast(result.message || '已释放僵死锁', 'ok');
      await refresh({ silent: true });
    } catch (error) {
      closeDialog();
      toast(error.message, 'bad', 5200);
      button.disabled = false;
    }
  });
}

/** 点击 */
async function onRootClick(event) {
  // 监听绑在共享根节点上：离开任务页后不再处理，避免抢走其它页面的同名操作
  if (!document.getElementById('task-main')) return;
  const filterButton = event.target.closest('[data-qfilter]');
  if (filterButton) {
    queueFilter = filterButton.dataset.qfilter;
    paintRegions(['queue']);
    return;
  }
  const button = event.target.closest('[data-act]');
  if (!button) return;
  const act = button.dataset.act;
  if (act === 'back') return goProjects();
  if (act === 'refresh') return refresh({ silent: false });
  if (act === 'llm-check') return refreshLlm();
  if (act === 'gpu-release') return releaseGpuLock(button);
  if (act === 'job-stop') return stopJob(button.dataset.job, button);
  if (act === 'queue-clear') return clearQueue(button);
  if (act === 'produce-start') {
    const from = document.getElementById('prod-from')?.value || 'setting';
    const to = document.getElementById('prod-to')?.value || 'export';
    // 只有章节级阶段需要章节；工程级阶段（设定库 / 大纲）空工程也能跑
    const needChapterStages = stageRange(from, to).filter((key) => !PROJECT_STAGES.has(key));
    if (needChapterStages.length && !state.chapters.length) {
      return toast(
        `所选区间包含需要章节的阶段：${needChapterStages.map((key) => PRODUCE_LABEL[key] || key).join('、')}。` +
          '工程还没有章节：请先落地章节或导入正文，或只选「设定库 → 大纲」等工程级阶段',
        'warn',
        5600
      );
    }
    // 生成方式：只补缺失阶段（续跑）或强制重跑全部阶段
    const mode = getGenMode('produce', 'fill');
    const regen = mode === 'regen';
    const force = regen;
    const noResume = regen;
    const order = PRODUCE_STAGES.map(([key]) => key);
    if (order.indexOf(from) > order.indexOf(to)) {
      return toast('「从」阶段必须排在「到」阶段之前', 'warn');
    }
    const check = await confirmLlm({
      action: 'produce.run',
      params: { path: state.path, from_stage: from, to_stage: to },
    });
    if (!check.ok) {
      if (check.error) toast(llmErrorText(check), 'bad', 3600);
      return;
    }
    button.disabled = true;
    try {
      const result = await postJSON('/api/ui/produce', {
        path: state.path,
        chapter: '',
        from,
        to,
        force,
        no_resume: noResume,
      });
      toast(`编排已启动（pid ${result.pid || '—'}），进度见浮层`, 'ok', 4200);
      watchProduceProgress(from, to, mode);
      window.setTimeout(() => refresh({ silent: true }), 1500);
    } catch (error) {
      toast(error.message, 'bad', 4800);
      button.disabled = false;
    }
  }
}

/**
 * 轮询全链路编排记录并用浮层展示当前阶段与进度。
 * @param {string} from 起始阶段 key
 * @param {string} to 结束阶段 key
 * @param {'fill'|'regen'} [mode] fill 只补缺失阶段；regen 强制重跑全部
 */
function watchProduceProgress(from, to, mode = 'fill') {
  const order = PRODUCE_STAGES.map(([key]) => key);
  const labelOf = (key) => PRODUCE_STAGES.find(([item]) => item === key)?.[1] || key;
  const scope = order.slice(Math.max(0, order.indexOf(from)), order.indexOf(to) + 1);
  const regen = mode === 'regen';
  const panel = openProgress({
    title: regen ? '正在强制重跑全链路编排…' : '正在补齐缺失阶段的编排…',
    hint: `${labelOf(from)} → ${labelOf(to)}${regen ? ' · 忽略已有产物' : ' · 已完成阶段会跳过'}`,
    stage: '后台进程已启动，正在读取编排记录…',
  });
  panel.update({ percent: 0 });
  const startedMs = Date.now();
  let stopped = false;
  let timer = null;
  let errors = 0;
  let ticks = 0;

  /** 读取一次 run show */
  const tick = async () => {
    if (stopped) return;
    ticks += 1;
    try {
      const data = await getJSON('/api/ui/produce/latest', { path: state.path });
      errors = 0;
      const run = data?.run;
      if (!run) {
        // 进程可能已快速失败：从任务队列取本次启动的 produce 任务，展示退出码与日志尾部
        if (ticks >= 2) {
          const jobs = await getJSON('/api/ui/queue', { path: state.path }).catch(() => null);
          const mine = (jobs || [])
            .filter(
              (item) =>
                item.kind === 'produce' &&
                item.created_at &&
                Date.parse(item.created_at) >= startedMs - 3000
            )
            .sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at))[0];
          if (mine && ['failed', 'canceled', 'interrupted'].includes(mine.status)) {
            stopped = true;
            if (timer) window.clearTimeout(timer);
            panel.fail({
              message: '编排没有启动成功',
              detail: errorText(mine.error) || '可到本页「任务队列」查看这条任务。',
              retry: () => watchProduceProgress(from, to, mode),
            });
            void refresh({ silent: true });
            return;
          }
        }
        if (ticks > 6) {
          stopped = true;
          panel.fail({
            message: '没有找到编排记录，可能启动失败',
            detail: '可到本页「任务队列」或日志页查看。',
            retry: () => watchProduceProgress(from, to, mode),
          });
          return;
        }
      } else {
        const stages = run.stages || [];
        const finishedKeys = stages.map((item) => item.stage);
        const failed = stages.filter((item) => item.status === 'failed').length;
        const done = scope.filter((key) => finishedKeys.includes(key)).length;
        const total = scope.length || 1;
        if (run.status === 'running') {
          const nextKey = scope.find((key) => !finishedKeys.includes(key));
          panel.update({
            stage: nextKey ? `正在执行：${labelOf(nextKey)}` : '正在收尾…',
            percent: Math.round((done / total) * 100),
            current: `已完成 ${done}/${total} 个阶段${failed ? ` · 失败 ${failed}` : ''}`,
          });
        } else {
          stopped = true;
          if (timer) window.clearTimeout(timer);
          if (run.status === 'failed' || failed) {
            const bad = stages.find((item) => item.status === 'failed');
            panel.fail({
              message: `编排结束：${failed} 个阶段失败`,
              detail: errorText(bad?.error) || '失败原因可在本页「全链路编排」里逐阶段查看。',
              retry: () => watchProduceProgress(from, to, mode),
            });
          } else {
            const lastStage = stages[stages.length - 1];
            panel.note(
              lastStage ? `最后一个阶段：${lastStage.label || labelOf(lastStage.stage)}` : ''
            );
            panel.finish({
              message: `编排完成：${done}/${total} 个阶段`,
              detail: run.status === 'dry_run' ? '本次是 dry-run，没有执行实际生成。' : '产物已写入工程目录。',
              actions: [{ label: '刷新本页', primary: true, onClick: () => void refresh() }],
            });
          }
          void refresh({ silent: true });
          return;
        }
      }
    } catch (error) {
      errors += 1;
      if (errors >= 3) {
        stopped = true;
        panel.fail({
          message: `读取编排进度失败：${error.message}`,
          retry: () => watchProduceProgress(from, to, mode),
        });
        return;
      }
    }
    if (!stopped) timer = window.setTimeout(tick, 2500);
  };
  void tick();
}

/** 离开任务页时停掉轮询 */
export function leaveTasks() {
  window.clearTimeout(timer);
  timer = null;
}

/** 进入任务视图（工程级，不读取也不选中任何章节） */
export async function renderTasks(root, { path }) {
  if (state.path !== path) resetProject(path);
  if (!bound) {
    root.addEventListener('click', onRootClick);
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
    // 章节只用于图标栏计数与「空工程」提示，本页不选中、不展示任何章节上下文
    state.chapters = chapters || [];
    readiness = ready;
  } catch (error) {
    root.innerHTML = `<div class="page"><div class="empty">${icon('warn', 22)}<b>任务加载失败</b>
      <span>${esc(error.message)}</span><a class="btn" href="#/projects" style="margin-top:10px">回工作区</a></div></div>`;
    return;
  }
  Object.keys(lastRegion).forEach((key) => delete lastRegion[key]);
  root.innerHTML = `
    ${topbar({
      left: `<button class="btn icon" data-act="back" title="回工作区">${icon('back')}</button>
             <div class="brand"><span class="brand-mark"></span><span style="font-size:12.5px">任务<small>本地版</small></span></div>`,
      crumbs: `<a href="#/projects">${esc(state.info?.work?.title || '工程')}</a>
               <span class="sep">/</span><span class="cur">任务</span>`,
      right: `<button class="btn" data-act="refresh">${icon('refresh')} 刷新</button>`,
    })}
    <div class="studio no-ctx" id="studio">
      ${railbar('tasks', { chapters: state.chapters.length })}
      <section class="editor">
        <div id="task-main" style="display:flex;flex-direction:column;min-height:0;flex:1;overflow:auto">${mainHTML()}</div>
      </section>
    </div>`;
  bindProduceMode();
  await refresh();
  // 写作模型自查：进入页面探测一次，点「检测连接」可随时复测（不参与 3 秒轮询）
  void refreshLlm();
}
