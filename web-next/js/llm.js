/**
 * 统一的「模型任务」确认组件。
 *
 * 所有会调用大模型的前端操作都先调用 `confirmLlm()`：它从后端
 * `/api/ui/llm/plan` 取次数、阶段、思考与取消能力，再按固定结构渲染，
 * 页面不再各自拼文案或猜测调用次数。
 */
import { esc, openDialog, closeDialog } from './dom.js';
import { getJSON, postJSON } from './api.js';
import { openProgress } from './progress.js';

/**
 * 取一个模型动作的事实清单。
 * @param {object} params `action` + 作用范围（path/chapter/act/count 等）
 * @returns {Promise<object>} 后端返回的 plan
 */
export async function fetchLlmPlan(params) {
  return getJSON('/api/ui/llm/plan', params);
}

/**
 * 生成统一的提示内容（供确认弹窗使用，也可嵌入其他弹窗）。
 * @param {object} plan 后端 plan
 * @param {string} [extraHTML] 额外说明（如待处理章节清单）
 * @returns {string} HTML
 */
export function llmNoticeHTML(plan = {}, extraHTML = '') {
  const rows = [
    ['这次做什么', plan.what || plan.title || '调用模型处理当前内容'],
    ['模型调用', plan.calls_text || `${Number(plan.calls || 0)} 次`],
    ['预计耗时', plan.seconds_text || '视内容长度而定（估计）'],
    // LLM 不设额外总超时：进度卡上随时可取消
    ['取消方式', plan.cancel_text || '可随时取消'],
    ['开启思考', plan.thinking_text || '关闭'],
  ];
  return `<div class="llm-notice">
    <div class="banner" style="margin-bottom:8px">${esc(
      plan.banner || '这是一次模型任务，会占用模型调用时间。'
    )}</div>
    ${rows
      .map(
        ([label, value]) =>
          `<div class="summary-line" style="margin-bottom:6px"><span>${esc(label)}</span><span class="right" style="font-family:inherit;color:var(--text)">${esc(
            value
          )}</span></div>`
      )
      .join('')}
    ${plan.estimate_note ? `<div class="hint" style="margin-top:6px">${esc(plan.estimate_note)}</div>` : ''}
    ${extraHTML ? `<div style="margin-top:10px">${extraHTML}</div>` : ''}
  </div>`;
}

/**
 * 弹出统一确认弹窗并等待用户选择。
 *
 * 用法：
 * ```js
 * const check = await confirmLlm({ action: 'chapter.generate', params: { path, chapter } });
 * if (!check.ok) return;
 * ```
 *
 * @param {object} options
 * @param {string} options.action 动作键（与后端 ACTIONS 一致）
 * @param {object} [options.params] 作用范围与生成参数
 * @param {string} [options.title] 自定义标题
 * @param {string} [options.extraHTML] 额外说明 HTML
 * @param {string} [options.confirmLabel] 主按钮文案
 * @param {object} [options.plan] 已提前取好的 plan（避免重复请求）
 * @returns {Promise<{ok: boolean, plan: object|null, error?: Error}>}
 */
export async function confirmLlm({
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
      facts = await fetchLlmPlan({ action, ...params });
    } catch (error) {
      return { ok: false, plan: null, error };
    }
  }
  // 次数为 0 表示后端判定这次不会调用模型（例如只补缺失但已有正文），不弹假提示
  if (Number(facts?.calls || 0) <= 0) return { ok: true, plan: facts };
  return new Promise((resolve) => {
    let settled = false;
    /** 只结算一次：确认、取消、Esc、点遮罩都走这里 */
    const finish = (ok) => {
      if (settled) return;
      settled = true;
      resolve({ ok, plan: facts });
    };
    openDialog({
      title: title || facts?.title || '模型任务确认',
      body: llmNoticeHTML(facts, extraHTML),
      foot: `<div class="right"><button class="btn" data-dlg-close>取消</button>
        <button class="btn primary" data-llm-go>${esc(confirmLabel)}</button></div>`,
      stacked: true,
      onClose: () => finish(false),
      onMount: (mask) => {
        mask.querySelector('[data-llm-go]').addEventListener('click', () => {
          finish(true);
          closeDialog();
        });
      },
    });
  });
}

/**
 * 生成一个调用标识。
 * @returns {string} `llm_<随机串>`
 */
export function newLlmCallId() {
  const random =
    typeof crypto !== 'undefined' && crypto.randomUUID
      ? crypto.randomUUID().replace(/-/g, '')
      : `${Date.now().toString(36)}${Math.random().toString(36).slice(2)}`;
  return `llm_${random}`;
}

/**
 * 通知后端取消一次模型调用（幂等，失败不打扰用户）。
 * @param {string} callId 调用标识
 * @returns {Promise<void>}
 */
async function cancelLlmCall(callId) {
  try {
    await postJSON('/api/ui/llm/cancel', { call_id: callId }, { timeout: 5000 });
  } catch {
    // 取消失败不改变本地已经中止请求的事实；后端会在连接断开/分片检查时自然停止
  }
}

/**
 * 兼容旧调用方的请求选项。
 *
 * LLM 不再推导等待上限：`timeout: 0` 表示不设总超时，由进度卡「取消」控制。
 * 新代码请直接使用 `runLlm` / `runLlmStream`。
 *
 * @param {object} [_check] 旧的确认结果（保留参数以兼容）
 * @param {object} [extra] 额外请求选项
 * @returns {{timeout: number}}
 */
export function llmRequestOptions(_check, extra = {}) {
  return { timeout: 0, ...extra };
}

/**
 * 执行一次非流式模型调用：确认 → 右上角进度（始终带取消）→ 请求 → 收尾。
 *
 * @param {object} options 配置
 * @param {string} options.action 动作键
 * @param {object} [options.params] 计划参数
 * @param {boolean} [options.confirm] 是否弹确认框，默认 true
 * @param {string} [options.title] 进度卡标题，默认取 plan.title
 * @param {string} [options.hint] 进度卡副标题
 * @param {string} [options.stage] 初始阶段
 * @param {object} [options.plan] 已取好的计划
 * @param {(ctx: {callId: string, signal: AbortSignal, plan: object}) => Promise<any>} options.request
 *   实际发请求的函数；必须把 `callId` 与 `signal` 传给 postJSON。
 * @param {(data: any) => {message?: string, detail?: string, actions?: Array<object>}} [options.finish]
 *   成功收尾文案
 * @returns {Promise<any|null>} 成功返回数据；用户在确认框取消返回 null；失败抛错
 */
export async function runLlm({
  action,
  params = {},
  confirm = true,
  title = '',
  hint = '',
  stage = '已提交模型请求，等待返回…',
  extraHTML = '',
  confirmLabel = '开始',
  plan = null,
  request,
  finish = null,
} = {}) {
  const check = confirm
    ? await confirmLlm({ action, params, title, extraHTML, confirmLabel, plan })
    : { ok: true, plan };
  if (!check.ok) {
    if (check.error) throw check.error;
    return null;
  }
  const facts = check.plan || {};
  const callId = newLlmCallId();
  const controller = new AbortController();
  const panel = openProgress({
    title: title || facts.title || '正在调用模型…',
    hint: hint || `${facts.seconds_text || '视内容长度而定'} · 可随时取消`,
    stage,
    onStop: () => {
      controller.abort();
      void cancelLlmCall(callId);
    },
  });
  panel.update({ percent: null });
  try {
    const data = await request({ callId, signal: controller.signal, plan: facts });
    const summary = finish ? finish(data) : { message: '已完成' };
    panel.finish(summary);
    return data;
  } catch (error) {
    if (error?.canceled || controller.signal.aborted) {
      panel.cancel({
        message: '已取消本次模型调用',
        detail: '未完成的本次调用不会写入任何产物；已完成并落盘的内容保留。',
      });
      return null;
    }
    panel.fail({
      message: `${facts.title || '模型任务'}失败：${error.message}`,
      retry: () =>
        runLlm({
          action,
          params,
          confirm: false,
          title,
          hint,
          stage,
          confirmLabel,
          plan: facts,
          request,
          finish,
        }),
    });
    throw error;
  }
}

/**
 * 执行一次流式模型调用：确认 → 右上角进度（始终带取消）→ SSE → 收尾。
 *
 * @param {object} options 配置
 * @param {string} options.action 动作键
 * @param {object} [options.params] 计划参数
 * @param {boolean} [options.confirm] 是否弹确认框，默认 true
 * @param {string} [options.title] 进度卡标题
 * @param {string} [options.hint] 进度卡副标题
 * @param {string} [options.stage] 初始阶段
 * @param {object} [options.plan] 已取好的计划
 * @param {(ctx: {callId: string, signal: AbortSignal, panel: object, plan: object}) => Promise<any>} options.stream
 *   实际发 SSE 请求的函数；必须把 `callId` 与 `signal` 传给 streamJSON。
 * @param {(data: any) => {message?: string, detail?: string, actions?: Array<object>}} [options.finish]
 *   成功收尾文案
 * @returns {Promise<any|null>} 成功返回数据；确认框取消返回 null；失败抛错
 */
export async function runLlmStream({
  action,
  params = {},
  confirm = true,
  title = '',
  hint = '',
  stage = '已提交模型请求，等待返回…',
  extraHTML = '',
  confirmLabel = '开始',
  plan = null,
  stream,
  finish = null,
} = {}) {
  const check = confirm
    ? await confirmLlm({ action, params, title, extraHTML, confirmLabel, plan })
    : { ok: true, plan };
  if (!check.ok) {
    if (check.error) throw check.error;
    return null;
  }
  const facts = check.plan || {};
  const callId = newLlmCallId();
  const controller = new AbortController();
  const panel = openProgress({
    title: title || facts.title || '正在调用模型…',
    hint: hint || `${facts.seconds_text || '视内容长度而定'} · 可随时取消`,
    stage,
    stream: true,
    onStop: () => {
      controller.abort();
      void cancelLlmCall(callId);
    },
  });
  panel.update({ percent: null });
  try {
    const data = await stream({ callId, signal: controller.signal, panel, plan: facts });
    const summary = finish ? finish(data) : { message: '已完成' };
    panel.finish(summary);
    return data;
  } catch (error) {
    if (error?.canceled || controller.signal.aborted) {
      panel.cancel({
        message: '已取消本次模型调用',
        detail: '已完成并落盘的内容保留；当前未完成的草稿丢弃。',
      });
      return null;
    }
    panel.fail({
      message: `${facts.title || '模型任务'}失败：${error.message}`,
      retry: () =>
        runLlmStream({
          action,
          params,
          confirm: false,
          title,
          hint,
          stage,
          confirmLabel,
          plan: facts,
          stream,
          finish,
        }),
    });
    throw error;
  }
}

/**
 * 把后端报错转成用户能读懂的文案（供调用方 toast）。
 * @param {object} result `confirmLlm` 的返回
 * @param {string} [fallback] 兜底文案
 * @returns {string} 文案
 */
export function llmErrorText(result, fallback = '读取模型任务信息失败') {
  return result?.error?.message ? `${fallback}：${result.error.message}` : fallback;
}
