/**
 * 后端接口薄封装。
 * 只做「拼查询串 / 解析错误 / 同源 fetch」，不缓存、不推导业务状态。
 */

/** 普通接口默认超时（毫秒）：超过后中止请求，避免界面一直停在加载态 */
export const DEFAULT_TIMEOUT_MS = 12000;

/**
 * 生成一条可识别超时的中文错误。
 * @param {number} ms 超时毫秒数
 * @returns {Error} 带 timeout 标记的错误
 */
function timeoutError(ms) {
  const error = new Error(`请求超时（${Math.round(ms / 1000)} 秒内没有响应），请确认后端服务正常后重试`);
  error.timeout = true;
  return error;
}

/**
 * 把网络异常统一成中文提示（超时已在调用处单独处理）。
 * @param {unknown} error 原始异常
 * @returns {Error}
 */
function networkError(error) {
  if (error?.name === 'AbortError') return error;
  const message = error instanceof TypeError ? '无法连接本地服务，请确认后端已启动' : error?.message || '网络异常';
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
 * @param {{ timeout?: number }} [options] timeout 毫秒；传 0 表示不设置超时
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
  const timer = timeout > 0 ? window.setTimeout(() => controller.abort(), timeout) : null;
  let response;
  try {
    response = await fetch(target, { headers: { Accept: 'application/json' }, signal: controller.signal });
  } catch (error) {
    if (error?.name === 'AbortError') throw timeoutError(timeout);
    throw networkError(error);
  } finally {
    if (timer) window.clearTimeout(timer);
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
 * @param {{ timeout?: number }} [options] timeout 毫秒；传 0 表示不设置超时（长任务可放宽）
 * @returns {Promise<any>}
 */
export async function sendJSON(method, url, body = {}, options = {}) {
  const timeout = options.timeout === undefined ? DEFAULT_TIMEOUT_MS : Number(options.timeout) || 0;
  const controller = new AbortController();
  const timer = timeout > 0 ? window.setTimeout(() => controller.abort(), timeout) : null;
  let response;
  try {
    response = await fetch(url, {
      method,
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify(body),
      signal: controller.signal,
    });
  } catch (error) {
    if (error?.name === 'AbortError') throw timeoutError(timeout);
    throw networkError(error);
  } finally {
    if (timer) window.clearTimeout(timer);
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
 * @param {{ timeout?: number }} [options] 超时配置
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
 * @returns {Promise<void>}
 */
export async function streamJSON(url, body, onEvent) {
  const response = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
    body: JSON.stringify(body),
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
