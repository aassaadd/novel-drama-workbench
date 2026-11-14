/**
 * 全平台日志视图：操作日志 + LLM 调用日志（自查问题用的调试入口）。
 *
 * 交互：
 * - 左侧筛选：类型（操作 / LLM 调用）、工程、时间范围、关键字；
 * - 主列表：最新在前，点一行打开详情；
 * - 详情按「请求 → 中间步骤 → 响应 → 错误」分段，长文本可折叠、可一键复制；
 * - 顶部可手动 / 自动刷新，也可清空日志（清空为不可恢复操作，需二次确认）。
 */
import { esc, icon, toast, openDialog, closeDialog, fmtBytes, fmtDuration } from '../dom.js';
import { getJSON, postJSON } from '../api.js';
import { railbar, topbar, goProjects } from '../shell.js';

/** 列表与筛选状态（模块级，切页返回时保留筛选条件） */
const view = {
  kind: '',
  project: '',
  since: '',
  until: '',
  q: '',
  trace: '',
  items: [],
  matched: 0,
  hasMore: false,
  offset: 0,
  loading: false,
};
/** 自动刷新定时器（离开页面必须清掉） */
let timer = null;
/** 根节点事件是否已绑定 */
let bound = false;
/** 详情里可复制的文本（按按钮顺序存放，避免把长文本塞进 HTML 属性） */
let copyTexts = [];
/** 最近一次打开详情时的记录（复制全部用） */
let currentRecord = null;

/** 类型徽标 */
const KIND_LABEL = { op: '操作', llm: 'LLM 调用' };

/** 状态徽标语义色 */
function statusClass(status) {
  if (status === 'ok') return 'ok';
  if (status === 'error') return 'bad';
  return 'warn';
}

/** 时间戳 → `11-14 16:05:21` */
function shortTime(value) {
  const text = String(value || '');
  return text.length >= 19 ? `${text.slice(5, 10)} ${text.slice(11, 19)}` : text;
}

/** 工程路径 → 短名（悬停显示完整路径） */
function projectName(path) {
  if (!path) return '全局';
  return String(path).split('/').filter(Boolean).pop() || path;
}

/** 把毫秒转成简短耗时 */
function durationText(ms) {
  const value = Number(ms) || 0;
  if (value < 1000) return `${value}ms`;
  if (value < 60000) return `${(value / 1000).toFixed(1)}s`;
  return fmtDuration(value);
}

/** 拼顶部骨架（筛选面板 + 列表容器） */
function pageHTML() {
  const projects = (view.projects || [])
    .map(
      (item) =>
        `<option value="${esc(item.path)}" ${view.project === item.path ? 'selected' : ''}>${esc(
          item.name
        )}（${item.count}）</option>`
    )
    .join('');
  return `
    ${topbar({
      left: `<button class="btn icon" data-act="lg-back" title="回工程列表">${icon('back')}</button>
             <div class="brand"><span class="brand-mark"></span><span style="font-size:12.5px">日志<small>调试</small></span></div>`,
      crumbs: `<a href="#/projects">工程列表</a><span class="sep">/</span><span class="cur">全平台日志</span>`,
      right: `<button class="btn" data-act="lg-refresh" title="重新读取日志（⌘/Ctrl + R 亦可）">${icon('refresh', 13)} 刷新</button>
              <button class="btn danger" data-act="lg-clear" title="删除全部本地日志文件（不可恢复）">${icon('trash', 13)} 清空日志</button>`,
    })}
    <div class="studio no-right">
      ${railbar('logs')}
      <aside class="ctx">
        <div class="ctx-head"><span class="ct">日志筛选</span>
          <div class="right"><span class="hint" id="lg-total">…</span></div>
        </div>
        <div class="ctx-tools">
          <div class="toolrow" id="lg-kinds">
            <button class="chip ${view.kind === '' ? 'on' : ''}" data-act="lg-kind" data-kind="">全部</button>
            <button class="chip ${view.kind === 'op' ? 'on' : ''}" data-act="lg-kind" data-kind="op">操作</button>
            <button class="chip ${view.kind === 'llm' ? 'on' : ''}" data-act="lg-kind" data-kind="llm">LLM 调用</button>
          </div>
          <div class="field"><label>工程</label>
            <select class="select" id="lg-project">
              <option value="">全部工程</option>${projects}
            </select>
          </div>
          <div class="field"><label>起始时间</label>
            <input class="input" type="datetime-local" id="lg-since" value="${esc(view.since)}" />
          </div>
          <div class="field"><label>结束时间</label>
            <input class="input" type="datetime-local" id="lg-until" value="${esc(view.until)}" />
          </div>
          <div class="field"><label>关键字（章节名 / 提示词 / 错误 / trace）</label>
            <div class="search">${icon('search', 14)}
              <input id="lg-q" placeholder="输入后回车或点应用" value="${esc(view.q)}" />
            </div>
          </div>
          <div class="toolrow">
            <button class="btn sm primary" data-act="lg-apply">应用筛选</button>
            <button class="btn sm" data-act="lg-reset">重置</button>
          </div>
          ${view.trace ? `<div class="dlg-note">正在看单次操作：<span class="mono">${esc(view.trace)}</span>
            <button class="btn sm ghost" data-act="lg-reset" style="margin-left:6px">看全部</button></div>` : ''}
          <div class="hint" id="lg-disk">正在读取磁盘占用…</div>
          <div class="hint">日志只在本机落盘，按天滚动；API Key 等敏感值写入前已脱敏。</div>
        </div>
      </aside>
      <section class="editor">
        <div class="lg-main">
          <div class="lg-toolbar">
            <span class="hint" id="lg-count">正在加载…</span>
            <div class="right">
              <label class="hint" title="每 6 秒自动刷新列表"><input type="checkbox" id="lg-auto" checked /> 自动刷新</label>
              <button class="btn sm" data-act="lg-refresh">${icon('refresh', 12)} 刷新</button>
            </div>
          </div>
          <div id="lg-list" class="lg-list"></div>
          <div id="lg-more" class="lg-more"></div>
        </div>
      </section>
    </div>`;
}

/** 列表行 HTML */
function rowHTML(item) {
  const kindClass = item.kind === 'llm' ? 'run' : 'mute';
  const sub = item.error
    ? `<span class="lg-error">${esc(item.error.split('\n')[0].slice(0, 120))}</span>`
    : `<span class="lg-sub">${esc(item.target || item.summary || item.action || '')}</span>`;
  return `
    <div class="lg-row" data-act="lg-row" data-file="${esc(item.file)}" data-line="${item.line}">
      <span class="lg-time" title="${esc(item.ts || '')}">${esc(shortTime(item.ts))}</span>
      <span><span class="badge ${kindClass}">${esc(KIND_LABEL[item.kind] || item.kind || '')}</span></span>
      <span style="min-width:0">
        <span class="lg-label">${esc(item.label || item.action || '')}
          <span class="badge ${statusClass(item.status)}">${
            item.status === 'ok' ? '成功' : item.status === 'error' ? '失败' : '中断'
          }</span>
          ${item.project ? `<span class="lg-proj" title="${esc(item.project)}">${esc(projectName(item.project))}</span>` : ''}
        </span>
        ${sub}
      </span>
      <span class="lg-trace" title="${esc(item.trace_id || '')}">${esc((item.trace_id || '').slice(3, 11))}</span>
      <span class="lg-dur">${esc(durationText(item.duration_ms))}</span>
    </div>`;
}

/** 重画列表 */
function paintList() {
  const host = document.getElementById('lg-list');
  if (!host) return;
  if (!view.items.length) {
    host.innerHTML = `<div class="empty" style="padding:48px 20px">
      <b>没有匹配的日志</b><span>换个类型或时间范围试试；日志只记录有副作用的操作与大模型调用。</span>
    </div>`;
  } else {
    host.innerHTML = view.items.map(rowHTML).join('');
  }
  const count = document.getElementById('lg-count');
  if (count) {
    count.textContent = view.hasMore
      ? `已显示 ${view.items.length} 条，还有更多${view.q ? `（关键字：${view.q}）` : ''}`
      : `共 ${view.matched} 条匹配${view.q ? `（关键字：${view.q}）` : ''}`;
  }
  const more = document.getElementById('lg-more');
  if (more) {
    more.innerHTML = view.hasMore
      ? `<button class="btn sm" data-act="lg-more">加载更多</button>`
      : '';
  }
  const total = document.getElementById('lg-total');
  if (total) total.textContent = view.hasMore ? `${view.items.length}+ 条` : `${view.matched} 条`;
}

/** 读取筛选控件的当前值 */
function readFilters() {
  const value = (id) => document.getElementById(id)?.value || '';
  view.project = value('lg-project');
  view.since = value('lg-since');
  view.until = value('lg-until');
  view.q = (value('lg-q') || '').trim();
}

/** 查询参数 */
function queryParams(offset) {
  return {
    kind: view.kind,
    project: view.project,
    q: view.q,
    since: view.since,
    until: view.until,
    trace: view.trace,
    limit: 100,
    offset,
  };
}

/**
 * 拉取日志列表。
 * @param {{append?: boolean}} options append=true 时追加到已有列表（加载更多）
 */
async function loadList({ append = false } = {}) {
  if (view.loading) return;
  view.loading = true;
  try {
    const offset = append ? view.offset : 0;
    const data = await getJSON('/api/logs', queryParams(offset));
    const items = data.items || [];
    view.items = append ? view.items.concat(items) : items;
    view.matched = Number(data.matched) || 0;
    view.hasMore = Boolean(data.has_more);
    view.offset = offset + items.length;
    paintList();
  } catch (error) {
    toast(error.message, 'bad', 4800);
  } finally {
    view.loading = false;
  }
}

/** 刷新磁盘占用与工程下拉候选 */
async function loadMeta() {
  try {
    const [stats, facets] = await Promise.all([getJSON('/api/logs/stats'), getJSON('/api/logs/facets')]);
    view.projects = facets.projects || [];
    const select = document.getElementById('lg-project');
    if (select) {
      const current = view.project;
      select.innerHTML =
        `<option value="">全部工程</option>` +
        view.projects
          .map(
            (item) =>
              `<option value="${esc(item.path)}">${esc(item.name)}（${item.count}）</option>`
          )
          .join('');
      select.value = current;
    }
    const disk = document.getElementById('lg-disk');
    if (disk) {
      const limits = stats.limits || {};
      const percent = limits.max_total_bytes ? Math.round((stats.total_bytes / limits.max_total_bytes) * 100) : 0;
      disk.innerHTML = `磁盘占用 ${esc(fmtBytes(stats.total_bytes))} / ${esc(
        fmtBytes(limits.max_total_bytes)
      )}（${percent}%，${stats.file_count} 个分卷）<br/>目录：<span class="mono">${esc(stats.dir)}</span>`;
    }
  } catch {
    // 统计失败不打断列表
  }
}

/** 打开详情弹窗 */
async function openDetail(item) {
  try {
    const record = await getJSON('/api/logs/record', { file: item.file, line: item.line }, { timeout: 20000 });
    renderDetail(record);
  } catch (error) {
    toast(error.message, 'bad', 4800);
  }
}

/**
 * 复制文本（优先 Clipboard API，失败时退回 selection）。
 * @param {string} text 要复制的文本
 */
async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast('已复制', 'ok', 1800);
  } catch {
    const area = document.createElement('textarea');
    area.value = text;
    area.style.position = 'fixed';
    area.style.opacity = '0';
    document.body.appendChild(area);
    area.select();
    try {
      document.execCommand('copy');
      toast('已复制', 'ok', 1800);
    } catch {
      toast('复制失败，请手动选中文本复制', 'warn', 3600);
    }
    area.remove();
  }
}

/** 登记一段可复制文本，返回按钮 HTML */
function copyButton(text, label = '复制') {
  copyTexts.push(text || '');
  return `<button class="btn sm ghost" data-act="lg-copy" data-index="${copyTexts.length - 1}">${esc(label)}</button>`;
}

/** 分段卡片 HTML */
function sectionHTML(title, body, extra = '') {
  if (!body) return '';
  return `<section class="lg-sec">
    <div class="lg-sec-head"><span class="lg-sec-title">${esc(title)}</span>${extra}</div>
    ${body}
  </section>`;
}

/** 长文本块（默认展开，可折叠，带复制） */
function preHTML(text, { title = '', open = true, lang = '' } = {}) {
  if (!text) return '';
  const body = `<pre class="lg-pre ${lang}">${esc(text)}</pre>`;
  return `<details class="lg-fold" ${open ? 'open' : ''}>
    <summary>${esc(title || `文本（${String(text).length} 字）`)}</summary>
    ${body}
  </details>`;
}

/** 键值对行 */
function kvHTML(label, value) {
  if (value === undefined || value === null || value === '') return '';
  return `<div class="summary-line lg-kv"><span>${esc(label)}</span><span class="right">${esc(String(value))}</span></div>`;
}

/** 渲染 LLM 调用详情 */
function llmDetailHTML(detail) {
  const http = detail.http || {};
  const params = detail.params || {};
  const response = detail.response || {};
  const reqBody = [
    kvHTML('模型', detail.model),
    kvHTML('流式', detail.stream ? '是' : '否'),
    kvHTML('请求地址', http.url || ''),
    kvHTML('HTTP 状态', http.status || ''),
    kvHTML('结束原因', response.finish_reason || ''),
    kvHTML('耗时', response.duration_ms != null ? durationText(response.duration_ms) : ''),
    kvHTML('首字耗时', response.first_token_ms != null ? `${response.first_token_ms}ms` : ''),
    kvHTML('token 用量', JSON.stringify(response.usage || {}) === '{}' ? response.usage_note || '未返回' : JSON.stringify(response.usage)),
    kvHTML('触发操作', detail.trigger || ''),
    preHTML(JSON.stringify(params, null, 2), { title: `请求参数（${Object.keys(params).length} 项）`, lang: 'mono' }),
  ].join('');
  const promptBody = [
    detail.system ? preHTML(detail.system, { title: `system 提示词（${detail.system.length} 字）` }) : '',
    ...(detail.messages || []).map((msg, index) =>
      preHTML(msg.content || '', { title: `${msg.role || 'user'} 消息 #${index + 1}（${String(msg.content || '').length} 字）` })
    ),
    (detail.tools || []).length
      ? preHTML(JSON.stringify(detail.tools, null, 2), { title: `工具定义（${detail.tools.length} 个）`, open: false, lang: 'mono' })
      : '',
    preHTML(JSON.stringify(detail.payload || {}, null, 2), { title: '完整原始请求体', open: false, lang: 'mono' }),
  ].join('');
  const respBody = [
    preHTML(response.text || '', { title: `返回文本（${String(response.text || '').length} 字）` }),
    response.reasoning ? preHTML(response.reasoning, { title: `思考过程（${response.reasoning.length} 字）`, open: false }) : '',
    response.tool_arguments
      ? preHTML(response.tool_arguments, { title: '工具调用参数', open: true, lang: 'mono' })
      : '',
    response.raw
      ? preHTML(JSON.stringify(response.raw, null, 2), { title: '原始返回报文 / SSE 事件', open: false, lang: 'mono' })
      : '',
  ].join('');
  const stepBody = (detail.steps || []).length
    ? `<div class="lg-steps">${detail.steps
        .map(
          (step) =>
            `<div class="lg-step"><span class="mono">${esc(step.at || '')}</span>
             <span>${esc(step.label || '')}</span>
             ${step.detail ? `<span class="hint">${esc(step.detail)}</span>` : ''}</div>`
        )
        .join('')}</div>`
    : '';
  return (
    sectionHTML('请求', reqBody + promptBody, copyButton(detail.system || '', '复制 system')) +
    sectionHTML('中间步骤', stepBody, '') +
    sectionHTML('响应', respBody, copyButton(response.text || '', '复制返回文本')) +
    errorSectionHTML(detail.error || (detail.response || {}).error)
  );
}

/** 渲染操作详情 */
function opDetailHTML(detail) {
  const http = detail.http || {};
  const params = detail.params || {};
  const reqBody = [
    kvHTML('方法', http.method),
    kvHTML('接口', http.path),
    kvHTML('状态码', http.status),
    preHTML(JSON.stringify(params, null, 2), { title: `请求参数（${Object.keys(params).length} 项）`, lang: 'mono' }),
  ].join('');
  const steps = detail.steps || [];
  const stepBody = steps.length
    ? `<div class="lg-steps">${steps
        .map(
          (step) =>
            `<div class="lg-step"><span class="mono">${esc(step.at || '')}</span>
             <span>${esc(step.label || '')}</span>
             ${step.detail ? preHTML(step.detail, { title: '步骤详情', open: false, lang: 'mono' }) : ''}</div>`
        )
        .join('')}</div>`
    : '';
  return (
    sectionHTML('请求', reqBody, '') +
    sectionHTML('中间步骤（含 CLI 命令 / stderr）', stepBody, '') +
    sectionHTML('响应', http.status ? kvHTML('接口返回', `HTTP ${http.status}`) : '', '') +
    errorSectionHTML(detail.error)
  );
}

/** 错误分段 */
function errorSectionHTML(error) {
  if (!error) return '';
  if (typeof error === 'string') {
    return sectionHTML('错误', preHTML(error, { title: '错误信息' }), copyButton(error, '复制错误'));
  }
  const message = error.message || '';
  const trace = error.traceback || '';
  return sectionHTML(
    '错误',
    (message ? preHTML(message, { title: '错误信息' }) : '') +
      (trace ? preHTML(trace, { title: '异常堆栈', open: false, lang: 'mono' }) : ''),
    copyButton(trace || message, '复制错误')
  );
}

/** 渲染详情弹窗 */
function renderDetail(record) {
  copyTexts = [];
  currentRecord = record;
  const detail = record.detail || {};
  const isLlm = record.kind === 'llm';
  const head = `
    <div class="lg-detail-head">
      <span class="badge ${record.kind === 'llm' ? 'run' : 'mute'}">${esc(KIND_LABEL[record.kind] || record.kind)}</span>
      <span class="badge ${statusClass(record.status)}">${
        record.status === 'ok' ? '成功' : record.status === 'error' ? '失败' : '中断'
      }</span>
      <b>${esc(record.label || record.action || '')}</b>
      <span class="mono hint">${esc(record.ts || '')}</span>
      <span class="hint">${esc(durationText(record.duration_ms))}</span>
    </div>
    <div class="lg-detail-meta">
      <span>工程：${esc(record.project || '（全局）')}</span>
      <span>目标：${esc(record.target || '—')}</span>
      <span>trace：<span class="mono">${esc(record.trace_id || '')}</span>
        ${record.trace_id ? `<button class="btn sm ghost" data-act="lg-trace" data-trace="${esc(record.trace_id)}">看同一次操作</button>` : ''}</span>
    </div>`;
  const body = isLlm ? llmDetailHTML(detail) : opDetailHTML(detail);
  const mask = openDialog({
    title: isLlm ? 'LLM 调用详情' : '操作详情',
    wide: true,
    body: `<div class="lg-detail">${head}${body || '<div class="hint">没有更多详情</div>'}</div>`,
    foot: `<div class="right">
      ${copyButton(JSON.stringify(record, null, 2), '复制整条 JSON')}
      <button class="btn" data-dlg-close>关闭</button>
    </div>`,
  });
  mask.addEventListener('click', (event) => {
    const copy = event.target.closest('[data-act="lg-copy"]');
    if (copy) copyText(copyTexts[Number(copy.dataset.index)] || '');
    const trace = event.target.closest('[data-act="lg-trace"]');
    if (trace) {
      view.trace = trace.dataset.trace;
      closeDialog();
      void loadList();
      repaintTraceHint();
    }
  });
}

/** 更新「正在看单次操作」提示（筛选区重绘用） */
function repaintTraceHint() {
  const host = document.querySelector('.ctx-tools');
  if (!host) return;
  let note = host.querySelector('.lg-trace-note');
  if (!view.trace) {
    note?.remove();
    return;
  }
  if (!note) {
    note = document.createElement('div');
    note.className = 'dlg-note lg-trace-note';
    host.appendChild(note);
  }
  note.innerHTML = `正在看单次操作：<span class="mono">${esc(view.trace)}</span>
    <button class="btn sm ghost" data-act="lg-reset">看全部</button>`;
}

/** 清空日志（二次确认） */
function confirmClear() {
  const mask = openDialog({
    title: '清空全部日志',
    body: `<div class="dlg-note warn">会删除本机全部操作日志与 LLM 调用日志（含历史分卷），无法恢复。
      <br/>如果只是想减少占用，可等磁盘上限自动清理最旧分卷。</div>`,
    foot: `<div class="right"><button class="btn" data-dlg-close>取消</button>
      <button class="btn danger" id="lg-clear-go">确认清空</button></div>`,
  });
  mask.querySelector('#lg-clear-go').addEventListener('click', async () => {
    try {
      const result = await postJSON('/api/logs/clear', {});
      closeDialog();
      toast(`已清空 ${result.removed || 0} 个日志文件`, 'ok');
      view.items = [];
      view.matched = 0;
      view.offset = 0;
      view.hasMore = false;
      paintList();
      void loadMeta();
      void loadList();
    } catch (error) {
      toast(error.message, 'bad', 4800);
    }
  });
}

/** 根节点事件委托 */
function onRootClick(event) {
  const target = event.target.closest('[data-act]');
  if (!target) return;
  const act = target.dataset.act;
  if (act === 'lg-back') {
    goProjects();
  } else if (act === 'lg-refresh') {
    void loadList();
    void loadMeta();
  } else if (act === 'lg-apply') {
    readFilters();
    view.offset = 0;
    void loadList();
  } else if (act === 'lg-reset') {
    view.kind = '';
    view.project = '';
    view.since = '';
    view.until = '';
    view.q = '';
    view.trace = '';
    view.offset = 0;
    const q = document.getElementById('lg-q');
    if (q) q.value = '';
    const since = document.getElementById('lg-since');
    if (since) since.value = '';
    const until = document.getElementById('lg-until');
    if (until) until.value = '';
    const project = document.getElementById('lg-project');
    if (project) project.value = '';
    document.querySelectorAll('#lg-kinds .chip').forEach((chip) => {
      chip.classList.toggle('on', !chip.dataset.kind);
    });
    repaintTraceHint();
    void loadList();
  } else if (act === 'lg-kind') {
    view.kind = target.dataset.kind || '';
    view.offset = 0;
    document.querySelectorAll('#lg-kinds .chip').forEach((chip) => {
      chip.classList.toggle('on', (chip.dataset.kind || '') === view.kind);
    });
    void loadList();
  } else if (act === 'lg-more') {
    void loadList({ append: true });
  } else if (act === 'lg-clear') {
    confirmClear();
  } else if (act === 'lg-row') {
    const item = view.items.find(
      (row) => row.file === target.dataset.file && String(row.line) === String(target.dataset.line)
    );
    if (item) void openDetail(item);
  }
}

/** 根节点输入事件：关键字回车立即查询 */
function onRootKeydown(event) {
  if (event.key === 'Enter' && event.target?.id === 'lg-q') {
    readFilters();
    view.offset = 0;
    void loadList();
  }
}

/** 自动刷新开关 */
function onRootChange(event) {
  if (event.target?.id === 'lg-auto') {
    setupTimer(event.target.checked);
  }
}

/** 建立 / 清除自动刷新定时器 */
function setupTimer(on) {
  if (timer) {
    window.clearInterval(timer);
    timer = null;
  }
  if (!on) return;
  timer = window.setInterval(() => {
    if (document.getElementById('lg-list') && !document.querySelector('.dlg-mask')) {
      void loadList();
    }
  }, 6000);
}

/** 离开日志页：清定时器 */
export function leaveLogs() {
  if (timer) {
    window.clearInterval(timer);
    timer = null;
  }
}

/**
 * 渲染日志页。
 * @param {HTMLElement} root 根节点
 * @param {{trace?: string}} target 路由参数（可直接带 trace 进入单次操作）
 */
export async function renderLogs(root, target = {}) {
  if (timer) {
    window.clearInterval(timer);
    timer = null;
  }
  if (target.trace) view.trace = target.trace;
  if (!bound) {
    root.addEventListener('click', onRootClick);
    root.addEventListener('keydown', onRootKeydown);
    root.addEventListener('change', onRootChange);
    bound = true;
  }
  root.innerHTML = pageHTML();
  setupTimer(document.getElementById('lg-auto')?.checked !== false);
  await Promise.all([loadList(), loadMeta()]);
}
