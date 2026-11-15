/**
 * 统一的「模型任务」确认组件。
 *
 * 所有会调用大模型的前端操作都先调用 `confirmLlm()`：它从后端
 * `/api/ui/llm/plan` 取次数、阶段、思考与取消能力，再按固定结构渲染，
 * 页面不再各自拼文案或猜测调用次数。
 */
import { esc, openDialog, closeDialog } from './dom.js';
import { getJSON } from './api.js';

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
    ['开启思考', plan.thinking_text || '关闭'],
    ['中途取消', plan.cancel_text || (plan.cancelable ? '可以中途停止' : '不能中途取消')],
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
 * 把后端报错转成用户能读懂的文案（供调用方 toast）。
 * @param {object} result `confirmLlm` 的返回
 * @param {string} [fallback] 兜底文案
 * @returns {string} 文案
 */
export function llmErrorText(result, fallback = '读取模型任务信息失败') {
  return result?.error?.message ? `${fallback}：${result.error.message}` : fallback;
}
