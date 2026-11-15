/**
 * 后端接口薄封装。
 * 只做「拼查询串 / 解析错误 / 同源 fetch」，不缓存、不推导业务状态。
 */

/** 普通接口默认超时（毫秒）：超过后中止请求，避免界面一直停在加载态 */
export const DEFAULT_TIMEOUT_MS = 12000;

/**
 * 生成一条可识别超时的中文错误。
 *
 * 只用于非 LLM 的普通接口：LLM 调用不设总超时（`timeout: 0`），
 * 用户通过进度卡「取消」终止；超时仅在普通接口上出现。
 *
 * @param {number} ms 超时毫秒数
 * @returns {Error} 带 timeout 标记的错误
 */
function timeoutError(ms) {
  const seconds = Math.round(ms / 1000);
  const error = new Error(
    `请求超时（${seconds} 秒内没有响应）：接口可能过载或卡住；` +
      '若其它普通接口也超时，再检查后端服务是否正常。'
  );
  error.timeout = true;
  error.timeoutMs = ms;
  return error;
}

/**
 * 生成一条可识别的取消错误（用户点进度卡「取消」或页面主动断开）。
 * @param {string} [message] 中文提示
 * @returns {Error} 带 canceled 标记的错误
 */
export function canceledError(message = '已取消本次请求') {
  const error = new Error(message);
  error.canceled = true;
  return error;
}

/**
 * 把外部 AbortSignal 接到内部控制器上，并区分「超时」与「用户取消」。
 * @param {AbortController} controller 内部控制器
 * @param {AbortSignal|null|undefined} signal 外部信号
 * @returns {() => void} 清理监听
 */
function linkAbortSignal(controller, signal) {
  if (!signal) return () => {};
  const onAbort = () => controller.abort();
  if (signal.aborted) controller.abort();
  else signal.addEventListener('abort', onAbort, { once: true });
  return () => signal.removeEventListener('abort', onAbort);
}

/**
 * 组装请求头；LLM 调用统一携带 call_id，后端据此注册取消令牌。
 * @param {Record<string,string>} base 基础头
 * @param {{callId?: string, json?: boolean}} [options] 选项
 * @returns {Record<string,string>} 请求头
 */
function buildHeaders(base, options = {}) {
  const headers = { ...base };
  if (options.callId) headers['X-LLM-Call-Id'] = String(options.callId);
  return headers;
}

/**
 * 把网络异常统一成中文提示（超时已在调用处单独处理）。
 * @param {unknown} error 原始异常
 * @returns {Error}
 */
function networkError(error) {
  if (error?.name === 'AbortError') return error;
  const message =
    error instanceof TypeError
      ? '无法连接本地服务（后端可能未启动或端口不通），请确认后端已启动后重试'
      : error?.message || '网络异常';
  const wrapped = new Error(message);
  wrapped.cause = error;
  return wrapped;
}

/** 把后端 detail 归一成一句中文提示 */
function extractMessage(detail, status, payload) {
  if (typeof detail === 'string' && detail.trim()) return detail;
  if (detail && typeof detail === 'object') {
    if (typeof detail.message === 'string' && detail.message) return detail.message;
    if (Array.isArray(detail) && detail.length) {
      const first = detail[0] || {};
      const loc = Array.isArray(first.loc) ? first.loc.join('.') : '';
      return `${loc} ${first.msg || '参数不合法'}`.trim();
    }
  }
  if (payload && typeof payload === 'object' && typeof payload.message === 'string') return payload.message;
  if (status === 412) return '前置条件不满足（模型未就绪或存在阻断项）';
  if (status === 409) return '已有同名任务在运行';
  if (status === 404) return '资源不存在（工程或章节可能已被移动）';
  return `请求失败（HTTP ${status}）`;
}

/**
 * GET 请求。
 * @param {string} url 接口路径
 * @param {Record<string, string|number|undefined|null>} [params] 查询参数
 * @param {{ timeout?: number, signal?: AbortSignal, callId?: string }} [options]
 *   timeout 毫秒；传 0 表示不设置超时（LLM 默认）。signal 用于用户取消。
 * @returns {Promise<any>}
 */
export async function getJSON(url, params = {}, options = {}) {
  const query = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') query.set(key, String(value));
  });
  const target = query.toString() ? `${url}?${query.toString()}` : url;
  const timeout = options.timeout === undefined ? DEFAULT_TIMEOUT_MS : Number(options.timeout) || 0;
  const controller = new AbortController();
  const unlink = linkAbortSignal(controller, options.signal);
  const timer = timeout > 0 ? window.setTimeout(() => controller.abort(), timeout) : null;
  let response;
  try {
    response = await fetch(target, {
      headers: buildHeaders({ Accept: 'application/json' }, options),
      signal: controller.signal,
    });
  } catch (error) {
    if (error?.name === 'AbortError') {
      throw options.signal?.aborted ? canceledError() : timeoutError(timeout);
    }
    throw networkError(error);
  } finally {
    if (timer) window.clearTimeout(timer);
    unlink();
  }
  const text = await response.text();
  let payload = null;
  try {
    payload = text ? JSON.parse(text) : null;
  } catch {
    payload = null;
  }
  if (!response.ok) {
    const error = new Error(extractMessage(payload?.detail, response.status, payload));
    error.status = response.status;
    error.payload = payload;
    throw error;
  }
  return payload;
}

/**
 * 带请求体的通用 JSON 请求（POST / PATCH 共用）。
 * @param {string} method HTTP 方法
 * @param {string} url 接口路径
 * @param {object} body 请求体
 * @param {{ timeout?: number, signal?: AbortSignal, callId?: string }} [options]
 *   timeout 毫秒；传 0 表示不设置超时（LLM 默认）。signal 用于用户取消。
 * @returns {Promise<any>}
 */
export async function sendJSON(method, url, body = {}, options = {}) {
  const timeout = options.timeout === undefined ? DEFAULT_TIMEOUT_MS : Number(options.timeout) || 0;
  const controller = new AbortController();
  const unlink = linkAbortSignal(controller, options.signal);
  const timer = timeout > 0 ? window.setTimeout(() => controller.abort(), timeout) : null;
  let response;
  try {
    response = await fetch(url, {
      method,
      headers: buildHeaders(
        { 'Content-Type': 'application/json', Accept: 'application/json' },
        options
      ),
      body: JSON.stringify(body),
      signal: controller.signal,
    });
  } catch (error) {
    if (error?.name === 'AbortError') {
      throw options.signal?.aborted ? canceledError() : timeoutError(timeout);
    }
    throw networkError(error);
  } finally {
    if (timer) window.clearTimeout(timer);
    unlink();
  }
  const text = await response.text();
  let payload = null;
  try {
    payload = text ? JSON.parse(text) : null;
  } catch {
    payload = null;
  }
  if (!response.ok) {
    const error = new Error(extractMessage(payload?.detail, response.status, payload));
    error.status = response.status;
    error.payload = payload;
    throw error;
  }
  return payload;
}

/**
 * POST JSON 请求。
 * @param {string} url 接口路径
 * @param {object} body 请求体
 * @param {{ timeout?: number, signal?: AbortSignal, callId?: string }} [options]
 *   timeout 毫秒；传 0 表示不设置超时（LLM 默认）。signal 用于用户取消。
 * @returns {Promise<any>}
 */
export async function postJSON(url, body = {}, options = {}) {
  return sendJSON('POST', url, body, options);
}

/**
 * PATCH JSON 请求。
 * @param {string} url 接口路径
 * @param {object} body 请求体
 * @param {{ timeout?: number }} [options] 超时配置
 * @returns {Promise<any>}
 */
export async function patchJSON(url, body = {}, options = {}) {
  return sendJSON('PATCH', url, body, options);
}

/**
 * POST 并逐事件读取 SSE（边生成边回调，用于正文生成/续写）。
 * @param {string} url 接口路径
 * @param {object} body 请求体
 * @param {(event: string, data: any) => void} onEvent 事件回调（start / delta / done / error）
 * @param {{signal?: AbortSignal, callId?: string}} [options]
 *   signal 用于页面主动断开长流；callId 透传给后端注册取消令牌
 * @returns {Promise<void>}
 */
export async function streamJSON(url, body, onEvent, options = {}) {
  const response = await fetch(url, {
    method: 'POST',
    headers: buildHeaders(
      { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
      options
    ),
    body: JSON.stringify(body),
    signal: options.signal,
  });
  if (!response.ok) {
    const text = await response.text();
    let payload = null;
    try {
      payload = text ? JSON.parse(text) : null;
    } catch {
      payload = null;
    }
    const error = new Error(extractMessage(payload?.detail, response.status, payload));
    error.status = response.status;
    throw error;
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let index = buffer.indexOf('\n\n');
    while (index >= 0) {
      const chunk = buffer.slice(0, index);
      buffer = buffer.slice(index + 2);
      let event = 'message';
      let data = '';
      for (const line of chunk.split('\n')) {
        if (line.startsWith('event:')) event = line.slice(6).trim();
        else if (line.startsWith('data:')) data += line.slice(5).trim();
      }
      let parsed = data;
      try {
        parsed = data ? JSON.parse(data) : null;
      } catch {
        parsed = data;
      }
      // 进度浮层自动把批次 / 条目事件显示成一行灰色最新内容
      try {
        window.__ndwLiveTap?.(event, parsed);
      } catch {
        // 浮层异常不影响生成流本身
      }
      onEvent(event, parsed);
      index = buffer.indexOf('\n\n');
    }
  }
}

/**
 * 工程内文件的可访问地址（音频 / 图片 / 导出 zip）。
 * @param {string} rel 相对工程根目录的路径
 * @param {string} project 工程根目录
 */
export function fileUrl(rel, project) {
  if (!rel || !project) return '';
  const query = new URLSearchParams({ path: String(rel), project: String(project) });
  return `/api/ui/file?${query.toString()}`;
}
