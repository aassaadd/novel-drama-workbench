/**
 * 后端接口薄封装。
 * 只做「拼查询串 / 解析错误 / 同源 fetch」，不缓存、不推导业务状态。
 */

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
 * @returns {Promise<any>}
 */
export async function getJSON(url, params = {}) {
  const query = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') query.set(key, String(value));
  });
  const target = query.toString() ? `${url}?${query.toString()}` : url;
  const response = await fetch(target, { headers: { Accept: 'application/json' } });
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
 * @returns {Promise<any>}
 */
export async function sendJSON(method, url, body = {}) {
  const response = await fetch(url, {
    method,
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify(body),
  });
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
 * @returns {Promise<any>}
 */
export async function postJSON(url, body = {}) {
  return sendJSON('POST', url, body);
}

/**
 * PATCH JSON 请求。
 * @param {string} url 接口路径
 * @param {object} body 请求体
 * @returns {Promise<any>}
 */
export async function patchJSON(url, body = {}) {
  return sendJSON('PATCH', url, body);
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
