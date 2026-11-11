/* 共享层：视图路由。
 *
 * 每个视图是一个 `<section id="view-xxx">`，页面逻辑分散在 page-*.js，
 * 各自注册到 `window.Pages`，激活时才取数（避免一进页面就把所有接口打一遍）。
 * 业务判定仍然只在 CLI 里：页面拿到的都是 `--json` 原样输出。
 */

window.Pages = window.Pages || {};

const VIEWS = ["project", "worlds", "characters", "outline", "chapter", "script", "voice", "media", "export", "settings"];

/* 当前 hash 对应的视图名；未知或空 hash 回落到台词校对页（保持原有入口行为）。 */
function currentView() {
  const raw = (location.hash || "").replace(/^#\/?/, "").trim();
  return VIEWS.includes(raw) ? raw : "voice";
}

/* 切换视图：显隐 section、点亮页签、调用页面 activate，并广播 view 事件。
 * 参数：name 视图名（VIEWS 之一）。
 * 返回：无。
 */
function showView(name) {
  VIEWS.forEach((view) => {
    const section = document.getElementById(`view-${view}`);
    if (section) section.hidden = view !== name;
  });
  document.querySelectorAll("#tabs a").forEach((link) => {
    link.classList.toggle("active", link.dataset.view === name);
  });
  const page = window.Pages[name];
  if (page && page.activate) page.activate();
  // 右栏 Inspector 等订阅者据此切换形态（配音视图 = 编辑表单，其余 = 读取型）
  if (window.NW && window.NW.emit) window.NW.emit("view", name);
}

window.Router = {
  init() {
    window.addEventListener("hashchange", () => showView(currentView()));
    showView(currentView());
  },
  show: showView,
};

/* 通用小工具：与台词页保持同一套（页面之间不要各写一份）。 */

/* 把对象拼成查询串，空值自动省略。 */
function buildQuery(params) {
  const search = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== "") search.set(key, value);
  });
  return search.toString();
}

/* HTML 转义，渲染用户文本前必调。 */
function esc(text) {
  return String(text ?? "").replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
}

/* 字节数转成人读体积。 */
function humanSize(bytes) {
  const value = Number(bytes || 0);
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(2)} MB`;
}

/* 毫秒转秒（保留一位小数）。 */
function msText(ms) {
  if (ms === null || ms === undefined) return "—";
  return `${(Number(ms) / 1000).toFixed(1)}s`;
}

/* 与 esc 等价的别名：台词校对页历史上用 escapeHtml，其余页面用 esc，统一实现只留一份。 */
function escapeHtml(text) {
  return esc(text);
}

/* 台词类型的中文名（对白 / 旁白 / 独白）。
 * 参数：kind 行类型。
 * 返回：中文字符串；未知类型原样返回。
 */
function kindLabel(kind) {
  return { dialogue: "对白", narration: "旁白", inner_monologue: "独白" }[kind] || kind || "";
}

/* 数值保留两位并把结尾的 0 去掉（情绪强度 / 语速用）。
 * 参数：value 任意值。
 * 返回：字符串；空值返回空串。
 */
function fmt(value) {
  return value === null || value === undefined ? "" : Number(value).toFixed(2).replace(/0$/, "");
}

/* 行状态的中文名：状态值是 schema 枚举，界面只显示中文。 */
const STATE_LABELS = {
  pending: "待处理",
  queued: "排队中",
  running: "生成中",
  ready: "已就绪",
  failed: "失败",
  stale: "已过期",
  skipped: "已跳过",
};

/* 行状态徽标（台词校对页与右栏共用同一套 class）。
 * 参数：name 状态名（pending / queued / running / ready / failed / stale / skipped）。
 * 返回：HTML 字符串；未知状态原样显示。
 */
function stateBadge(name) {
  const text = STATE_LABELS[name] || name || "";
  return `<span class="state-badge s-${name}"><span class="dot"></span>${text}</span>`;
}

/* 轻提示：复用全站唯一的 #toast 节点。
 * 参数：message 文本；ok 是否为成功样式（默认失败样式）。
 * 返回：无。
 */
function toast(message, ok = false) {
  const node = document.getElementById("toast");
  if (!node) return;
  node.textContent = message;
  node.className = ok ? "toast ok" : "toast";
  node.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { node.hidden = true; }, 4000);
}

/* ---------------- 生成中蒙版 ---------------- */

/* 蒙版状态：depth 支持嵌套调用，内层结束不会提前关掉外层。 */
const busyState = { depth: 0, title: "", baseDetail: "", startedAt: 0, timer: null };

/* 刷新蒙版文案：主文案 + 明细 + 已用时。参数：无。返回：无。 */
function busyRender() {
  const title = document.getElementById("busyTitle");
  const detail = document.getElementById("busyDetail");
  if (!title || !detail) return;
  title.textContent = busyState.title || "正在生成…";
  const elapsed = Math.floor((Date.now() - busyState.startedAt) / 1000);
  const parts = [];
  if (busyState.baseDetail) parts.push(busyState.baseDetail);
  if (elapsed > 0) parts.push(`已用时 ${elapsed}s`);
  detail.textContent = parts.join(" · ");
}

/* 显示生成蒙版；重复调用叠加计数并更新文案，计数归零才隐藏。
 * 参数：text 主文案；detail 明细文案（可空）。
 * 返回：无。
 */
function busyShow(text, detail) {
  busyState.depth += 1;
  busyState.title = text || "正在生成…";
  busyState.baseDetail = detail || "";
  if (busyState.depth === 1) {
    busyState.startedAt = Date.now();
    const mask = document.getElementById("busyMask");
    if (mask) mask.hidden = false;
    clearInterval(busyState.timer);
    busyState.timer = setInterval(busyRender, 1000);
  }
  busyRender();
}

/* 关闭生成蒙版；嵌套调用时只减计数。参数：无。返回：无。 */
function busyHide() {
  if (busyState.depth === 0) return;
  busyState.depth -= 1;
  if (busyState.depth > 0) return;
  clearInterval(busyState.timer);
  busyState.timer = null;
  const mask = document.getElementById("busyMask");
  if (mask) mask.hidden = true;
}

/* 统一取数：把非 2xx 的 detail 翻译成人能读的文本，并带上接口给的退出码。
 * 参数：url 请求地址；options fetch 选项，额外支持 busy（主文案）与 busyDetail（明细）。
 *       传了 busy 时，请求超过 200ms 仍未返回就弹出全屏生成蒙版，结束后自动收起。
 * 返回：Promise<解析后的 JSON>；失败抛 Error（error.code = 接口退出码或 HTTP 状态）。
 */
async function api(url, options) {
  const opts = options || {};
  const busyText = opts.busy;
  let busyTimer = null;
  let busyVisible = false;
  if (busyText) {
    busyTimer = setTimeout(() => {
      busyVisible = true;
      busyShow(busyText, opts.busyDetail);
    }, 200);
  }
  try {
    const rest = { ...opts };
    delete rest.busy;
    delete rest.busyDetail;
    const response = await fetch(url, rest);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = data.detail;
      let text;
      if (typeof detail === "string") {
        text = detail;
      } else if (Array.isArray(detail)) {
        // FastAPI 自身的参数校验错误（缺 chapter 之类）
        text = detail.map((item) => `${(item.loc || []).slice(1).join(".")}: ${item.msg}`).join("；");
      } else if (detail && detail.message) {
        text = detail.message;
      } else {
        text = `HTTP ${response.status}`;
      }
      const error = new Error(text);
      error.code = detail && detail.code !== undefined ? detail.code : response.status;
      throw error;
    }
    return data;
  } finally {
    if (busyTimer) clearTimeout(busyTimer);
    if (busyVisible) busyHide();
  }
}

/* 查询串构造（与 buildQuery 同实现，保留旧名给各页面复用）。 */
function qs(params) {
  return buildQuery(params);
}

/* 工程内相对路径 → 只读文件接口 URL。媒体预览、右栏播放器、底部时间线共用这一套。
 * 参数：rel 工程内相对路径（如 assets/sfx/xxx.wav）；project 工程根目录，缺省取当前工程。
 * 返回：URL 字符串；工程或路径缺失时返回空串（调用方据此隐藏播放器，不伪造 URL）。
 */
function uiFileUrl(rel, project) {
  const root = project || ((window.NW && NW.state && NW.state.path) || "");
  const path = String(rel || "");
  if (!root || !path) return "";
  return `/api/ui/file?${new URLSearchParams({ path, project: root }).toString()}`;
}
