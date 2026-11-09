/* 共享层：视图路由。
 *
 * 每个视图是一个 `<section id="view-xxx">`，页面逻辑分散在 page-*.js，
 * 各自注册到 `window.Pages`，激活时才取数（避免一进页面就把所有接口打一遍）。
 * 业务判定仍然只在 CLI 里：页面拿到的都是 `--json` 原样输出。
 */

window.Pages = window.Pages || {};

const VIEWS = ["project", "outline", "chapter", "script", "voice", "media", "export"];

/* 当前 hash 对应的视图名；未知或空 hash 回落到台词校对页（保持原有入口行为）。 */
function currentView() {
  const raw = (location.hash || "").replace(/^#\/?/, "").trim();
  return VIEWS.includes(raw) ? raw : "voice";
}

/* 切换视图：显隐 section、点亮页签、调用页面 activate。 */
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
