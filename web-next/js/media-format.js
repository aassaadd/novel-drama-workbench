/**
 * 媒体总线的纯展示层：中文状态映射、进度/错误归一与统一任务卡片 HTML。
 *
 * 这里只放不依赖 DOM 的纯函数，便于 Node 单测直接导入；
 * `media-bus.js` 负责确认、启动、轮询与进度卡，并把这批函数原样再导出。
 */

/** 媒体任务状态 → 中文文案（与后端 `UiJobStatus` 一一对应） */
export const MEDIA_STATUS = {
  queued: '排队',
  running: '进行中',
  succeeded: '已完成',
  partial: '部分完成',
  failed: '失败',
  canceled: '已取消',
  interrupted: '已中断',
};

/** 媒体通道 → 中文文案 */
export const MEDIA_CHANNELS = {
  audio: '声音',
  image: '图片',
  video: '视频',
  llm: '模型',
  other: '其他',
};

/**
 * 媒体任务类型的兜底中文名（后端未带 title 时使用）。
 * 只覆盖媒体与编排类任务；其余类型交给调用方的标题，绝不显示英文原值。
 */
const KIND_LABELS = {
  'tts.line': '单行重录',
  'tts.chapter': '本章批量配音',
  'tts.book': '全书批量配音',
  'voice.design': '音色设计',
  'voice.preview': '角色试听',
  sfx: '音效',
  bgm: '配乐',
  image: '配图',
  video: '视频',
  anim: '图生视频',
  produce: '全链路编排',
};

/**
 * HTML 转义。
 * 与 `dom.js` 的 `esc` 同口径；本模块刻意不导入 dom.js（它加载时会访问 document），
 * 以保证纯函数可以在 Node 单测里直接使用。
 * @param {*} value 原始值
 * @returns {string} 转义后的文本
 */
function esc(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

/**
 * 状态中文文案。
 * @param {string} status 任务状态值
 * @returns {string} 中文文案；未识别时返回「状态未知」
 */
export function mediaStatusLabel(status) {
  return MEDIA_STATUS[String(status || '')] || '状态未知';
}

/**
 * 通道中文文案。
 * @param {string} channel 通道值（audio/image/video/llm/other）
 * @returns {string} 中文文案；未识别时归入「其他」
 */
export function mediaChannelLabel(channel) {
  return MEDIA_CHANNELS[String(channel || '')] || MEDIA_CHANNELS.other;
}

/**
 * 任务类型兜底中文名。
 * @param {string} kind 任务类型值
 * @returns {string} 中文名；未登记时返回空串（由调用方再兜底）
 */
function mediaKindLabel(kind) {
  return KIND_LABELS[String(kind || '')] || '';
}

/**
 * 从任务快照里取百分比（无有效数字时返回 null）。
 * @param {object} job 任务快照
 * @returns {number|null} 0-100 的数值
 */
function percentOf(job) {
  const raw = job?.percent ?? job?.progress?.percent;
  if (raw === null || raw === undefined || raw === '') return null;
  const value = Number(raw);
  return Number.isFinite(value) ? value : null;
}

/**
 * 产物数量（没有产物时返回 0）。
 * @param {object} job 任务快照
 * @returns {number} 产物条数
 */
function artifactCount(job) {
  return Array.isArray(job?.artifacts) ? job.artifacts.length : 0;
}

/**
 * 把后端错误（字符串或对象）转成可读中文文本。
 * @param {*} value 错误值
 * @returns {string} 错误文本；没有错误时返回空串
 */
function errorText(value) {
  if (!value) return '';
  if (typeof value === 'string') return value.trim();
  if (typeof value === 'object') {
    return (
      String(value.message || value.detail?.message || value.detail || value.reason || '').trim() ||
      '生成失败（后端未提供原因）'
    );
  }
  return String(value);
}

/**
 * 进度文案：百分比 + 当前项；没有百分比时退回阶段文案。
 * @param {object} job 任务快照
 * @returns {string} 例如「42% · 第 3 行」或「合成语音」
 */
export function jobProgressText(job) {
  const percent = percentOf(job);
  const current = String(job?.current || job?.progress?.current || '').trim();
  const stage = String(job?.stage || '').trim();
  if (percent !== null) {
    const head = `${Math.round(percent)}%`;
    return current ? `${head} · ${current}` : head;
  }
  return current || stage || '—';
}

/**
 * 错误摘要：只保留前两行，避免多行堆栈把卡片撑破。
 * @param {object} job 任务快照
 * @returns {string} 两行以内的错误摘要；没有错误时返回空串
 */
export function jobErrorSummary(job) {
  const text = errorText(job?.error);
  if (!text) return '';
  const rows = text
    .split('\n')
    .map((row) => row.trim())
    .filter(Boolean);
  return rows.slice(0, 2).join(' / ');
}

/**
 * 状态徽标语义色（复用任务页的 ok / run / bad / warn / mute）。
 * @param {string} status 任务状态值
 * @returns {string} 徽标类名
 */
function statusBadgeClass(status) {
  const value = String(status || '');
  if (value === 'succeeded') return 'ok';
  if (value === 'partial') return 'warn';
  if (value === 'failed' || value === 'interrupted') return 'bad';
  if (value === 'running') return 'run';
  return 'mute';
}

/**
 * 媒体任务确认弹窗的正文 HTML（只展示后端计划，不编造数据）。
 * @param {object} plan `fetchMediaPlan` 的返回值
 * @returns {string} HTML 片段
 */
export function mediaPlanHTML(plan = {}) {
  const count = plan?.count;
  const countText =
    count === null || count === undefined
      ? '视范围而定'
      : Number(count) > 0
        ? `${Number(count)} 个`
        : '无需生成';
  const stages = Array.isArray(plan?.stages) && plan.stages.length ? plan.stages.join(' → ') : '准备中';
  const queue = plan?.queue ?? plan?.gpu;
  const rows = [
    ['这次做什么', plan?.title || '媒体生成'],
    ['通道', plan?.channel_label || mediaChannelLabel(plan?.channel)],
    ['阶段', stages],
    ['预计数量', countText],
    ['是否排队', queue ? '需要排队（占用 GPU，按队列顺序执行）' : '不排队，立即执行'],
    ['取消方式', plan?.cancel_text || (plan?.cancelable === false ? '不可取消' : '可随时取消')],
    ['需要参数', plan?.usage || '—'],
  ];
  return `<div class="media-notice">
    <div class="banner" style="margin-bottom:8px">这是一次媒体生成任务，会在后台执行，完成后自动刷新。</div>
    ${rows
      .map(
        ([label, value]) =>
          `<div class="summary-line" style="margin-bottom:6px"><span>${esc(label)}</span><span class="right" style="font-family:inherit;color:var(--text)">${esc(
            value
          )}</span></div>`
      )
      .join('')}
  </div>`;
}

/**
 * 单条媒体任务卡片 HTML。
 *
 * 运行中且可取消时才渲染「取消」按钮（`data-media-cancel` 供页面绑定），
 * 已结束或不可取消的任务不出现操作按钮。
 *
 * @param {object} job 统一任务快照（`/api/ui/media/job/{id}` 或 `/api/ui/queue` 的行）
 * @returns {string} HTML 片段
 */
export function mediaJobCardHTML(job = {}) {
  const id = String(job?.id || job?.job_id || '');
  const title = String(job?.title || '').trim() || mediaKindLabel(job?.kind) || '媒体任务';
  const status = String(job?.status || '');
  const active = ['queued', 'running'].includes(status);
  const cancelable = job?.cancelable !== false;
  const progress = jobProgressText(job);
  const errorLine = jobErrorSummary(job);
  return `<div class="media-job"${id ? ` data-job="${esc(id)}"` : ''}
    style="display:flex;flex-direction:column;gap:4px;padding:8px 10px;border:1px solid var(--line);border-radius:6px;background:var(--bg-1)">
    <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">
      <span style="font-size:12.5px;color:var(--text)">${esc(title)}</span>
      <span class="badge mute">${esc(mediaChannelLabel(job?.channel))}</span>
      <span class="badge ${statusBadgeClass(status)}">${esc(mediaStatusLabel(status))}</span>
      <span class="hint mono" style="margin-left:auto" title="本次任务已生成的产物数量">产物 ${artifactCount(job)}</span>
    </div>
    <div class="hint mono" title="${esc(progress)}">${esc(progress)}</div>
    ${
      errorLine
        ? `<div style="font-size:11px;color:var(--bad);white-space:pre-line" title="${esc(errorLine)}">${esc(
            errorLine
          )}</div>`
        : ''
    }
    ${
      active && cancelable && id
        ? `<div style="display:flex;justify-content:flex-end">
             <button class="btn sm danger" type="button" data-media-cancel="${esc(id)}"
               title="停止这条媒体任务，已完成的产物保留">取消</button>
           </div>`
        : ''
    }
  </div>`;
}

/**
 * 媒体任务列表 HTML：运行中优先，同组按创建时间倒序。
 * @param {Array<object>} jobs 任务快照列表
 * @param {{emptyText?: string}} [options] 空列表文案
 * @returns {string} HTML 片段
 */
export function mediaJobRowsHTML(jobs, { emptyText = '暂无媒体任务' } = {}) {
  const list = Array.isArray(jobs) ? jobs.slice() : [];
  const isActive = (job) => ['queued', 'running'].includes(String(job?.status || ''));
  const createdMs = (job) => {
    const value = Date.parse(String(job?.created_at || ''));
    return Number.isFinite(value) ? value : 0;
  };
  list.sort((left, right) => {
    const activeDiff = (isActive(right) ? 1 : 0) - (isActive(left) ? 1 : 0);
    if (activeDiff) return activeDiff;
    return createdMs(right) - createdMs(left);
  });
  if (!list.length) return `<div class="empty" style="padding:16px 12px">${esc(emptyText)}</div>`;
  return `<div class="media-jobs" style="display:flex;flex-direction:column;gap:6px">${list
    .map(mediaJobCardHTML)
    .join('')}</div>`;
}
