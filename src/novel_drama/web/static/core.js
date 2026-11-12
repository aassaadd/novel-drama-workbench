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
  draft: "草稿",
  done: "已完成",
};

/* 状态枚举 → 中文：章节 draft/done、媒体 pending/ready/failed 等共用。
 * 参数：status 状态枚举。
 * 返回：中文字符串；未登记的值原样返回。
 */
function statusLabel(status) {
  return STATE_LABELS[status] || status || "";
}

/* 配音情绪的中文名：情绪值是 schema 枚举，界面显示中文、写回仍是英文。 */
const EMOTION_LABELS = {
  neutral: "中性",
  joy: "喜悦",
  sad: "悲伤",
  angry: "愤怒",
  fear: "恐惧",
  surprise: "惊讶",
  tender: "温柔",
  excited: "激动",
  whisper: "低语",
  serious: "严肃",
  calm: "冷静",
};

/* 情绪枚举 → 中文；未登记的值原样返回，避免静默丢信息。
 * 参数：emotion 情绪枚举。
 * 返回：中文字符串。
 */
function emotionLabel(emotion) {
  return EMOTION_LABELS[emotion] || emotion || "";
}

/* 角色定位的中文名：取值与 schema/character.schema.json 的 role 枚举一一对应。
 * 界面只做展示翻译，写回 characters.json 的仍是英文枚举。 */
const ROLE_LABELS = {
  protagonist: "主角",
  antagonist: "反派",
  supporting: "配角",
  narrator: "旁白",
  minor: "龙套",
};

/* 角色定位枚举 → 中文。
 * 参数：role 角色定位（protagonist/antagonist/supporting/narrator/minor）。
 * 返回：中文字符串；未登记的值原样返回，避免静默丢信息。
 */
function roleLabel(role) {
  return ROLE_LABELS[role] || role || "";
}

/* 虚拟角色 ID → 固定中文名：旁白与「未识别说话人」不在人物表里，不能直接展示 ID。 */
const VIRTUAL_SPEAKER_LABELS = { chr_narrator: "旁白", chr_unknown: "未知角色" };

/* 说话人展示名：后端已解析成姓名时直接用；拿到的仍是内部 ID 或为空时按虚拟角色兜底，
 * 保证界面上不会出现 chr_xxx 这类内部 ID。
 * 参数：speaker 后端给的说话人展示名（可能是 ID）；speakerId 说话人角色 ID。
 * 返回：中文名字符串；解析不到时为「未知角色」。
 */
function speakerLabel(speaker, speakerId) {
  const name = String(speaker || "").trim();
  const virtual = VIRTUAL_SPEAKER_LABELS[speakerId] || VIRTUAL_SPEAKER_LABELS[name];
  if (virtual) return virtual;
  if (!name || /^chr_[0-9A-Za-z]+$/.test(name)) return "未知角色";
  return name;
}

/* 媒体/标注里的英文枚举 → 中文：只影响展示，写回与解析仍用英文原值。 */
const TIME_LABELS = { dawn: "黎明", morning: "上午", day: "白天", dusk: "黄昏", night: "夜晚", unknown: "未知" };
const MEDIA_KIND_LABELS = { sfx: "音效", image: "配图", video: "视频" };
const SFX_KIND_LABELS = { ambience: "环境音", foley: "拟音", impact: "冲击音", transition: "转场音", music_sting: "音乐点" };
const ANIM_MODE_LABELS = { i2v: "图生视频", t2v: "文生视频" };
const CAMERA_LABELS = { static: "固定", slow_push_in: "缓慢推近", pan_left: "向左摇镜", pan_right: "向右摇镜", handheld: "手持" };

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

/* 更新生成蒙版的明细文案（主文案与嵌套计数不变）。
 * 参数：text 明细文案（如「第 2/3 批 · 已接收 1.2k 字」）。
 * 返回：无。
 */
function busyDetail(text) {
  busyState.baseDetail = text || "";
  busyRender();
}

/* 字数紧凑展示：1000 以上按 k、10000 以上按「万」，各保留一位小数。
 * 参数：n 字数。
 * 返回：展示字符串。
 */
function formatChars(n) {
  const value = Math.max(0, Number(n) || 0);
  if (value < 1000) return String(value);
  if (value < 10000) return `${(value / 1000).toFixed(1).replace(/\.0$/, "")}k`;
  return `${(value / 10000).toFixed(1).replace(/\.0$/, "")}万`;
}

/* 新建一轮生成进度状态（层级批次与已接收字数）。
 * 返回：{batch, total, chars, name, unit, actIndex, actTotal}。
 */
function progressInit() {
  return { batch: 0, total: 0, chars: 0, name: "", unit: "", actIndex: 0, actTotal: 0 };
}

/* 应用一条进度事件并生成文案；不含「已用时」，由蒙版统一追加。
 * 参数：state progressInit() 返回的状态（就地更新）；event 事件对象
 *       （batch: {index,total,name?,unit?,act_index?,act_total?}，
 *        chars: {chars,index|batch,total,name?,unit?,act_index?,act_total?}）。
 * 返回：文案，如「第 2/3 幕 · 第 4/9 章 · 已接收 1.2k 字」「第 3/9 个：林墨 · 已接收 420 字」。
 */
function progressApply(state, event) {
  const data = event || {};
  const current = state || progressInit();
  if (data.type === "batch" || data.type === "chars") {
    if (data.unit) current.unit = String(data.unit);
    if (data.type === "batch") {
      current.batch = Number(data.index || data.batch) || 0;
      current.total = Number(data.total) || 0;
      current.name = data.name ? String(data.name) : "";
    } else {
      current.chars = Number(data.chars) || current.chars;
      if (data.index || data.batch) current.batch = Number(data.index || data.batch) || current.batch;
      if (data.total) current.total = Number(data.total) || current.total;
      if (data.name) current.name = String(data.name);
    }
    if (data.act_index) current.actIndex = Number(data.act_index) || current.actIndex;
    if (data.act_total) current.actTotal = Number(data.act_total) || current.actTotal;
  }
  const parts = [];
  if (current.unit === "chapter" && current.batch && current.total) {
    /* 三级分层：幕与章同时展示，例如「第 2/3 幕 · 第 4/9 章」 */
    parts.push(
      current.actIndex && current.actTotal
        ? `第 ${current.actIndex}/${current.actTotal} 幕 · 第 ${current.batch}/${current.total} 章`
        : `第 ${current.batch}/${current.total} 章`
    );
  } else if (current.unit === "act" && current.batch && current.total) {
    parts.push(`第 ${current.batch}/${current.total} 幕`);
  } else if (current.batch && current.total) {
    parts.push(current.name ? `第 ${current.batch}/${current.total} 个：${current.name}` : `第 ${current.batch}/${current.total} 批`);
  }
  if (current.chars) parts.push(`已接收 ${formatChars(current.chars)} 字`);
  return parts.join(" · ");
}

/* 解析一段 SSE 文本（以空行分帧），按事件名回调。
 * 参数：block 原始块；onEvent(event, data) 回调。
 * 返回：无。
 */
function dispatchSse(block, onEvent) {
  let event = "message";
  const dataLines = [];
  block.split("\n").forEach((line) => {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
  });
  if (!dataLines.length) return;
  let data = null;
  try {
    data = JSON.parse(dataLines.join("\n"));
  } catch (error) {
    return;
  }
  onEvent(event, data || {});
}

/* 用 fetch + ReadableStream 读取 SSE（EventSource 不能带 POST body）。
 * 参数：url 接口地址；body 请求体；onEvent(event, data) 事件回调；signal 取消信号。
 * 返回：Promise，done 事件的 result；error 事件或连接失败时 reject。
 */
async function streamPost(url, body, onEvent, signal) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!response.ok || !response.body) {
    let message = `HTTP ${response.status}`;
    try {
      const detail = await response.json();
      message = (detail && (detail.detail || detail.message)) || message;
    } catch (error) {
      // 非 JSON 错误体：保留 HTTP 状态码
    }
    throw new Error(message);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  let result = null;
  let failure = "";
  const handle = (event, data) => {
    if (event === "error") {
      failure = data.message || "生成失败";
      return;
    }
    if (event === "done") result = data.result || null;
    onEvent(event, data);
  };
  while (true) {
    const chunk = await reader.read();
    if (chunk.done) break;
    buffer += decoder.decode(chunk.value, { stream: true });
    let index = buffer.indexOf("\n\n");
    while (index >= 0) {
      dispatchSse(buffer.slice(0, index), handle);
      buffer = buffer.slice(index + 2);
      index = buffer.indexOf("\n\n");
    }
  }
  if (buffer.trim()) dispatchSse(buffer, handle);
  if (failure) throw new Error(failure);
  if (!result && signal && signal.aborted) throw new Error("已停止生成");
  return result;
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
