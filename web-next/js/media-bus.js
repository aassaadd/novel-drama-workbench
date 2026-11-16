/**
 * 统一媒体生成总线（前端唯一入口）。
 *
 * 声音、图片、视频、音效、配乐的生成都走这里：确认计划 → 启动任务 → 统一进度卡 →
 * 轮询终态 → 取消 / 重试 / 完成后刷新。页面不再各自轮询 job 接口。
 *
 * 后端接口（Task 2 已实现）：
 * - `GET  /api/ui/media/plan`          计划与事实清单
 * - `POST /api/ui/media/run`           启动任务，立即返回统一快照
 * - `GET  /api/ui/media/job/{job_id}`  读取统一快照
 * - `POST /api/ui/media/cancel`        按 job_id 协作式取消（幂等）
 */
import { esc, openDialog, closeDialog } from './dom.js';
import { getJSON, postJSON } from './api.js';
import { openProgress } from './progress.js';
import {
  MEDIA_STATUS,
  MEDIA_CHANNELS,
  mediaStatusLabel,
  mediaChannelLabel,
  jobProgressText,
  jobErrorSummary,
  mediaPlanHTML,
  mediaJobCardHTML,
  mediaJobRowsHTML,
} from './media-format.js';

// 展示层纯函数统一从总线出口提供，页面只依赖 media-bus.js
export {
  MEDIA_STATUS,
  MEDIA_CHANNELS,
  mediaStatusLabel,
  mediaChannelLabel,
  jobProgressText,
  jobErrorSummary,
  mediaPlanHTML,
  mediaJobCardHTML,
  mediaJobRowsHTML,
};

/** 轮询间隔离（毫秒） */
const RUN_POLL_MS = 1200;
const WATCH_POLL_MS = 1500;
/** 单次读取任务快照的超时（毫秒）：进度接口卡住时及时重试，不把界面拖死 */
const JOB_TIMEOUT_MS = 8000;
/** 连续读取失败多少次后收尾 */
const MAX_POLL_ERRORS = 3;

/**
 * 等待若干毫秒。
 * @param {number} ms 毫秒
 * @returns {Promise<void>} 到时 resolve
 */
function wait(ms) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

/**
 * 从快照里取百分比（无有效数字时返回 null，交给进度卡显示「进行中」）。
 * @param {object} job 任务快照
 * @returns {number|null} 0-100
 */
function percentOf(job) {
  const raw = job?.percent;
  if (raw === null || raw === undefined || raw === '') return null;
  const value = Number(raw);
  return Number.isFinite(value) ? value : null;
}

/**
 * 产物数量。
 * @param {object} job 任务快照
 * @returns {number} 条数
 */
function artifactCount(job) {
  return Array.isArray(job?.artifacts) ? job.artifacts.length : 0;
}

/**
 * 终态归类。
 * @param {string} status 任务状态值
 * @returns {'done'|'cancel'|'fail'|''} 终态类型；运行中 / 排队返回空串
 */
function terminalKind(status) {
  const value = String(status || '');
  if (value === 'succeeded' || value === 'partial') return 'done';
  if (value === 'canceled') return 'cancel';
  if (value === 'failed' || value === 'interrupted') return 'fail';
  return '';
}

/**
 * 终态标题文案。
 * @param {object} job 任务快照
 * @returns {string} 成功 / 取消 / 失败时的中文摘要
 */
function doneMessage(job) {
  const current = String(job?.current || '').trim();
  if (current) return current;
  return String(job?.status || '') === 'partial' ? '部分完成' : '已完成';
}

/**
 * 失败标题文案：中断单独给出原因，避免用户误以为是 provider 报错。
 * @param {object} job 任务快照
 * @returns {string} 中文失败原因
 */
function failMessage(job) {
  if (String(job?.status || '') === 'interrupted') return '任务已中断（页面进程重启）';
  return jobErrorSummary(job) || '生成失败';
}

/**
 * 取一次媒体动作的事实清单。
 * @param {object} options
 * @param {string} options.action 动作键（与后端登记表一致）
 * @param {object} [options.params] 作用范围与生成参数（path 可空）
 * @returns {Promise<object>} 后端返回的计划
 */
export async function fetchMediaPlan({ action, params = {} } = {}) {
  const query = new URLSearchParams();
  Object.entries({ action, ...params }).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') query.set(key, String(value));
  });
  return getJSON(`/api/ui/media/plan?${query.toString()}`);
}

/**
 * 弹出统一确认弹窗并等待用户选择。
 *
 * ```js
 * const check = await confirmMedia({ action: 'sfx.item', params: { path, chapter, sfx_id } });
 * if (!check.ok) return;
 * ```
 *
 * @param {object} options
 * @param {string} options.action 动作键
 * @param {object} [options.params] 作用范围与生成参数
 * @param {string} [options.title] 自定义标题
 * @param {string} [options.extraHTML] 额外说明 HTML
 * @param {string} [options.confirmLabel] 主按钮文案
 * @param {object|null} [options.plan] 已取好的计划（避免重复请求）
 * @returns {Promise<{ok: boolean, plan: object|null, error?: Error}>} 用户是否确认
 */
export async function confirmMedia({
  action,
  params = {},
  title = '',
  extraHTML = '',
  confirmLabel = '开始',
  plan = null,
} = {}) {
  let facts = plan;
  if (!facts) {
    try {
      facts = await fetchMediaPlan({ action, params });
    } catch (error) {
      return { ok: false, plan: null, error };
    }
  }
  // 数量为 0：后端判定这次没有可生成的条目，不弹假提示
  if (facts?.count !== null && facts?.count !== undefined && Number(facts.count) === 0) {
    return { ok: true, plan: facts };
  }
  return new Promise((resolve) => {
    let settled = false;
    /** 只结算一次：确认、取消、Esc、点遮罩都走这里 */
    const finish = (ok) => {
      if (settled) return;
      settled = true;
      resolve({ ok, plan: facts });
    };
    openDialog({
      title: title || facts?.title || '媒体生成确认',
      body: `${mediaPlanHTML(facts)}${extraHTML ? `<div style="margin-top:10px">${extraHTML}</div>` : ''}`,
      foot: `<div class="right"><button class="btn" data-dlg-close>取消</button>
        <button class="btn primary" data-media-go>${esc(confirmLabel)}</button></div>`,
      stacked: true,
      onClose: () => finish(false),
      onMount: (mask) => {
        mask.querySelector('[data-media-go]').addEventListener('click', () => {
          finish(true);
          closeDialog();
        });
      },
    });
  });
}

/**
 * 启动并跟踪一次媒体生成：确认 → 启动 → 统一进度卡 → 轮询到终态。
 *
 * 用户中途点取消时只发取消请求，等后端落 `canceled` 再收尾，不擅自判失败。
 *
 * @param {object} options
 * @param {string} options.action 动作键
 * @param {object} [options.params] 作用范围与生成参数（需含 path）
 * @param {string} [options.title] 进度卡标题
 * @param {string} [options.hint] 进度卡副标题
 * @param {boolean} [options.confirm] 是否先弹确认框（重试时传 false）
 * @param {object|null} [options.plan] 已取好的计划
 * @param {(snap: object) => void} [options.onProgress] 每次拿到快照的回调
 * @param {(() => any)|null} [options.refresh] 成功后的刷新函数
 * @returns {Promise<{job: object|null, id: string|null}>} 最终快照与任务 ID；
 *   用户在确认框取消返回 `{job: null, id: null}`；启动失败抛中文错误
 */
export async function runMedia({
  action,
  params = {},
  title = '',
  hint = '',
  confirm = true,
  plan = null,
  onProgress = null,
  refresh = null,
} = {}) {
  let facts = plan;
  if (confirm) {
    const check = await confirmMedia({ action, params, title, plan });
    if (!check.ok) {
      if (check.error) throw new Error(`发起生成失败：${check.error.message}`);
      return { job: null, id: null };
    }
    facts = check.plan;
  }

  const path = String(params?.path || '');
  let first;
  try {
    // 启动接口只负责建任务，不等待生成：不设总超时，避免长任务启动被误杀
    first = await postJSON('/api/ui/media/run', { action, params }, { timeout: 0 });
  } catch (error) {
    throw new Error(`发起生成失败：${error.message}`);
  }
  const jobId = String(first?.job_id || first?.id || '');
  if (!jobId) throw new Error('发起生成失败：后端没有返回任务编号');

  /** 用户是否已点过取消（决定轮询出错时是否继续等后端落库） */
  let cancelRequested = false;
  const panel = openProgress({
    title: title || first?.title || facts?.title || '媒体生成',
    hint: hint || (facts?.usage ? `需要：${facts.usage}` : ''),
    stage: String(first?.stage || '排队中…'),
    onStop: async () => {
      cancelRequested = true;
      await cancelMediaJob(path, jobId);
    },
  });
  let snap = first;
  panel.update({ percent: percentOf(snap), current: String(snap?.current || '') });

  const retry = () =>
    runMedia({ action, params, confirm: false, plan: facts, title, hint, onProgress, refresh });

  let errors = 0;
  for (;;) {
    await wait(RUN_POLL_MS);
    let next;
    try {
      next = await getJSON(
        `/api/ui/media/job/${encodeURIComponent(jobId)}`,
        { path },
        { timeout: JOB_TIMEOUT_MS }
      );
      errors = 0;
    } catch (error) {
      errors += 1;
      // 已点取消：等后端把任务落成 canceled，不因为读不到进度就判失败
      if (cancelRequested) continue;
      if (errors >= MAX_POLL_ERRORS) {
        panel.fail({ message: `读取生成进度失败：${error.message}`, retry });
        return { job: snap, id: jobId };
      }
      panel.update({ stage: `暂时读不到进度（已重试 ${errors}/${MAX_POLL_ERRORS} 次）…` });
      continue;
    }
    if (!next || typeof next !== 'object') continue;

    snap = next;
    panel.update({
      stage: snap.stage || undefined,
      percent: percentOf(snap),
      current: String(snap.current || ''),
    });
    onProgress?.(snap);

    const kind = terminalKind(snap.status);
    if (kind === 'done') {
      const count = artifactCount(snap);
      panel.finish({
        message: doneMessage(snap),
        detail: count ? `产物 ${count} 个` : '',
        actions: refresh ? [{ label: '刷新', primary: true, onClick: () => void refresh() }] : [],
      });
      await refresh?.();
      return { job: snap, id: jobId };
    }
    if (kind === 'cancel') {
      panel.cancel({ message: jobErrorSummary(snap) || '已取消' });
      return { job: snap, id: jobId };
    }
    if (kind === 'fail') {
      panel.fail({ message: failMessage(snap), retry });
      return { job: snap, id: jobId };
    }
  }
}

/**
 * 接管一条已在后台运行的任务：轮询并展示统一进度卡。
 *
 * 供页面刷新后接管已启动的任务（例如整本批量配音）。
 * 每次拿到快照都会回调；终态只回调一次并自动停止轮询。
 *
 * @param {object} options
 * @param {string} options.path 工程目录
 * @param {string} options.jobId 任务 ID
 * @param {string} [options.title] 进度卡标题
 * @param {string} [options.hint] 进度卡副标题
 * @param {(snap: object) => void} [options.onUpdate] 每次拿到快照的回调
 * @param {(snap: object) => void} [options.onDone] 成功 / 部分完成回调
 * @param {(snap: object|null) => void} [options.onFail] 失败、中断或连续读不到进度时的回调
 * @param {(snap: object) => void} [options.onCancel] 已取消回调
 * @returns {{stop: () => void}} 控制器；`stop()` 会清理定时器
 */
export function watchMediaJob({
  path,
  jobId,
  title = '',
  hint = '',
  onUpdate = null,
  onDone = null,
  onFail = null,
  onCancel = null,
} = {}) {
  const id = String(jobId || '');
  const panel = openProgress({
    title: title || '正在读取媒体任务…',
    hint,
    stage: '正在读取任务进度…',
    onStop: id ? () => cancelMediaJob(path, id) : null,
  });

  let stopped = false;
  let timer = null;
  let errors = 0;

  /** 停止轮询并清理定时器 */
  const stop = () => {
    if (stopped) return;
    stopped = true;
    if (timer) {
      window.clearTimeout(timer);
      timer = null;
    }
  };

  /** 安排下一次轮询（已停止则不再安排） */
  const schedule = () => {
    if (!stopped) timer = window.setTimeout(tick, WATCH_POLL_MS);
  };

  /** 读一次快照；终态回调一次后停止 */
  const tick = async () => {
    if (stopped) return;
    timer = null;
    if (!id) {
      stop();
      panel.fail({ message: '缺少任务编号，无法读取生成进度' });
      onFail?.(null);
      return;
    }
    let snap;
    try {
      snap = await getJSON(
        `/api/ui/media/job/${encodeURIComponent(id)}`,
        { path },
        { timeout: JOB_TIMEOUT_MS }
      );
      errors = 0;
    } catch (error) {
      if (stopped) return;
      errors += 1;
      if (errors >= MAX_POLL_ERRORS) {
        stop();
        panel.fail({
          message: `读取生成进度失败：${error.message}`,
          retry: () => watchMediaJob({ path, jobId: id, title, hint, onUpdate, onDone, onFail, onCancel }),
        });
        onFail?.(null);
        return;
      }
      panel.update({ stage: `暂时读不到进度（已重试 ${errors}/${MAX_POLL_ERRORS} 次）…` });
      schedule();
      return;
    }
    if (stopped) return;
    if (!snap || typeof snap !== 'object') {
      schedule();
      return;
    }

    panel.update({
      stage: snap.stage || undefined,
      percent: percentOf(snap),
      current: String(snap.current || ''),
    });
    onUpdate?.(snap);

    const kind = terminalKind(snap.status);
    if (!kind) {
      schedule();
      return;
    }
    stop();
    if (kind === 'done') {
      const count = artifactCount(snap);
      panel.finish({
        message: doneMessage(snap),
        detail: count ? `产物 ${count} 个` : '',
      });
      onDone?.(snap);
    } else if (kind === 'cancel') {
      panel.cancel({ message: jobErrorSummary(snap) || '已取消' });
      onCancel?.(snap);
    } else {
      panel.fail({ message: failMessage(snap) });
      onFail?.(snap);
    }
  };

  void tick();
  return { stop };
}

/**
 * 通知后端取消一条媒体任务（幂等：失败静默返回 null）。
 * @param {string} path 工程目录
 * @param {string} jobId 任务 ID
 * @returns {Promise<object|null>} 后端返回的取消结果；失败返回 null
 */
export async function cancelMediaJob(path, jobId) {
  if (!path || !jobId) return null;
  try {
    return await postJSON(
      '/api/ui/media/cancel',
      { path: String(path), job_id: String(jobId) },
      { timeout: 15000 }
    );
  } catch {
    // 取消是幂等操作：接口失败不打扰用户，任务终态仍以轮询结果为准
    return null;
  }
}
