/* 台词校对页（P4）。
 *
 * 取数纪律：
 *   - `line list`（整表）只在三种时机拉：1) 打开/切换章节；2) 点「刷新」；3) 写操作（保存/回滚）完成。
 *   - 队列进度只轮询 `queue list`，绝不重拉 `line list`；某行完成后只打 `line list --line` 取那一行。
 *   - 轮询间隔：有任务在跑 2 秒，空闲 4 秒；队列全空即停止轮询。
 * 状态徽标的筛选在前端对已加载的行做，不额外发请求，但状态值本身完全来自后端。
 */

const ROW_H = 46;
const OVERSCAN = 6;
const POLL_ACTIVE_MS = 2000;
const POLL_IDLE_MS = 4000;
const START_GRACE_MS = 30000; // 点了开始但 worker 还没入队时的等待上限
const TAIL_GRACE_MS = 6000; // 队列转空后再等一小会儿，避免漏掉尾巴上的任务

const EMOTIONS = [
  ["neutral", "中性"], ["joy", "喜悦"], ["sad", "悲伤"], ["angry", "愤怒"],
  ["fear", "恐惧"], ["surprise", "惊讶"], ["tender", "温柔"],
  ["excited", "激动"], ["whisper", "低语"], ["serious", "严肃"],
];

const STATE_ORDER = ["pending", "queued", "running", "ready", "failed", "stale", "skipped"];
const STATE_LABEL = {
  pending: "待生成", queued: "排队", running: "运行中", ready: "就绪",
  failed: "失败", stale: "过期", skipped: "跳过",
};

const state = {
  path: localStorage.getItem("ndw.path") || "",
  chapter: "",
  rows: [],
  counts: {},
  filter: new Set(),
  selected: null,
  scrollTop: 0,
  start: 0,
  scenes: {},
  ready: true,        // 配音链是否可开工（模型就位 + provider 可用）
  readyReason: "",
};

/* 生成会话：只记本次点「开始」之后的队列状态，避免旧任务混进进度。 */
const run = {
  timer: null,
  polling: false,
  since: null,
  total: 0,
  seenJobs: false,
  idleSince: 0,
  patched: new Set(),
  done: new Set(),
  failed: new Set(),
};

const $ = (id) => document.getElementById(id);

function toast(message, ok = false) {
  const el = $("toast");
  el.textContent = message;
  el.className = ok ? "toast ok" : "toast";
  el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { el.hidden = true; }, 4000);
}

async function api(url, options) {
  const response = await fetch(url, options);
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
}

function qs(params) {
  const search = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== "") search.set(key, value);
  });
  return search.toString();
}

function stateBadge(name) {
  const color = `s-${name}`;
  return `<span class="state-badge ${color}"><span class="dot"></span>${name}</span>`;
}

function renderBadges() {
  const total = state.rows.length;
  const parts = [`<button class="badge ${state.filter.size ? "" : "active"}" data-state="">全部 <span class="num">${total}</span></button>`];
  STATE_ORDER.forEach((name) => {
    const count = state.counts[name] ?? 0;
    const active = state.filter.has(name) ? "active" : "";
    parts.push(
      `<button class="badge ${active}" data-state="${name}">` +
      `<span class="dot s-${name}" style="background:currentColor"></span>${STATE_LABEL[name]}` +
      ` <span class="num">${count}</span></button>`
    );
  });
  $("badges").innerHTML = parts.join("");
}

function visibleRows() {
  if (!state.filter.size) return state.rows;
  return state.rows.filter((row) => state.filter.has(row.state));
}

function rowHtml(row, top) {
  const selected = state.selected === row.line_id ? "selected" : "";
  const unknown = row.speaker_id === "chr_unknown" || !row.speaker_id ? "unknown" : "";
  const text = escapeHtml(row.text || "");
  return (
    `<div class="row ${selected}" data-line="${row.line_id}" style="position:absolute;top:${top}px;left:0;right:0">` +
    `<span class="c-index">${row.index ?? ""}</span>` +
    `<span class="c-kind">${kindLabel(row.kind)}</span>` +
    `<span class="c-speaker ${unknown}">${escapeHtml(row.speaker || "")}</span>` +
    `<span class="c-text" title="${text}">${text}</span>` +
    `<span class="c-num">${row.emotion || ""}</span>` +
    `<span class="c-num">${fmt(row.emotion_intensity)}</span>` +
    `<span class="c-num">${fmt(row.speed)}</span>` +
    `<span class="c-state">${stateBadge(row.state)}</span>` +
    `<span class="c-num">${row.duration_ms ? Math.round(row.duration_ms) : "—"}</span>` +
    `<span class="c-num">${row.pause_before_ms ?? ""}</span>` +
    `<span class="c-num">${row.pause_after_ms ?? ""}</span>` +
    `</div>`
  );
}

/* 只重画某一行：滚动时虚拟列表会自然重绘，这里仅在行处于视口内时替换那一个节点。 */
function updateRowDom(lineId) {
  const rows = visibleRows();
  const index = rows.findIndex((row) => row.line_id === lineId);
  if (index < 0) return;
  const node = $("rows").querySelector(`.row[data-line="${CSS.escape(lineId)}"]`);
  if (!node) return;
  node.outerHTML = rowHtml(rows[index], (index - state.start) * ROW_H);
}

function renderTable() {
  const rows = visibleRows();
  $("empty").hidden = rows.length > 0;
  $("spacer").style.height = `${rows.length * ROW_H}px`;
  const viewport = $("viewport");
  const start = Math.max(0, Math.floor(state.scrollTop / ROW_H) - OVERSCAN);
  const end = Math.min(rows.length, start + Math.ceil(viewport.clientHeight / ROW_H) + OVERSCAN * 2);
  const layer = $("rows");
  state.start = start;
  layer.style.transform = `translateY(${start * ROW_H}px)`;
  const html = [];
  for (let i = start; i < end; i += 1) {
    html.push(rowHtml(rows[i], (i - start) * ROW_H));
  }
  layer.innerHTML = html.join("");
  layer.style.height = `${(end - start) * ROW_H}px`;
}

function kindLabel(kind) {
  return { dialogue: "对白", narration: "旁白", inner_monologue: "独白" }[kind] || kind || "";
}

function fmt(value) {
  return value === null || value === undefined ? "" : Number(value).toFixed(2).replace(/0$/, "");
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
}

function fillEmotions() {
  $("fEmotion").innerHTML = EMOTIONS.map(([value, label]) => `<option value="${value}">${label}</option>`).join("");
}

async function loadProjects() {
  try {
    const projects = await api("/api/projects");
    $("projectList").innerHTML = projects
      .map((item) => `<option value="${escapeHtml(item.path)}">${escapeHtml(item.title || item.path)}</option>`)
      .join("");
    if (!state.path && projects.length) state.path = projects[0].path;
  } catch (error) {
    toast(`读取工程列表失败：${error.message}`);
  }
  $("projectPath").value = state.path;
}

async function openProject() {
  const path = $("projectPath").value.trim();
  if (!path) {
    toast("先填工程目录（或用 --project 建过工程）");
    return;
  }
  state.path = path;
  localStorage.setItem("ndw.path", path);
  try {
    const chapters = await api(`/api/ui/chapters?${qs({ path })}`);
    $("chapterSelect").innerHTML = chapters
      .map((item) => `<option value="${item.id}">${item.index}. ${escapeHtml(item.title)}</option>`)
      .join("");
    if (!chapters.length) {
      $("hint").textContent = "工程里还没有章节";
      state.rows = [];
      state.counts = {};
      renderBadges();
      renderTable();
      $("runInfo").textContent = "本次将生成 0 行";
      $("runProgress").textContent = "";
      $("btnStart").disabled = true;
      return;
    }
    const keep = chapters.some((item) => item.id === state.chapter);
    state.chapter = keep ? state.chapter : chapters[0].id;
    $("chapterSelect").value = state.chapter;
    await loadChapter();
  } catch (error) {
    toast(`打开工程失败：${error.message}`);
  }
}

async function loadChapter() {
  if (!state.path || !state.chapter) return;
  state.scenes = {};
  try {
    const detail = await api(`/api/ui/chapter?${qs({ path: state.path, chapter: state.chapter })}`);
    (detail.scenes || []).forEach((scene, index) => {
      state.scenes[scene.id] = `${index + 1}. ${scene.title || scene.location || "场景"}`;
    });
    $("hint").textContent = `${(detail.scenes || []).length} 个场景`;
  } catch (error) {
    // 场景名只是展示用；拿不到也要把台词表渲染出来
    toast(`读取场景失败：${error.message}`);
  }
  await loadLines();
  await loadPlan();
}

async function loadLines() {
  try {
    const status = [...state.filter].join(",");
    const data = await api(`/api/ui/lines?${qs({ path: state.path, chapter: state.chapter, status })}`);
    state.rows = data.lines || [];
    state.counts = data.counts || {};
    if (state.selected && !state.rows.some((row) => row.line_id === state.selected)) state.selected = null;
    state.scrollTop = $("viewport").scrollTop;
    renderBadges();
    renderTable();
    if (state.selected) await selectLine(state.selected, false);
  } catch (error) {
    toast(`读取台词失败：${error.message}`);
  }
}

async function selectLine(lineId, scrollIntoView = true) {
  const row = state.rows.find((item) => item.line_id === lineId);
  if (!row) return;
  state.selected = lineId;
  renderTable();
  $("lineId").textContent = `${row.line_id}  ·  ${state.scenes[row.scene_id] || row.scene_id}`;
  const badge = $("lineState");
  badge.className = `state-badge s-${row.state}`;
  badge.innerHTML = `<span class="dot"></span>${row.state}`;
  $("form").hidden = false;
  $("fText").value = row.text || "";
  $("fKind").value = row.kind || "dialogue";
  $("fEmotion").value = row.emotion || "neutral";
  $("fIntensity").value = row.emotion_intensity ?? 0.5;
  $("oIntensity").textContent = fmt(row.emotion_intensity ?? 0.5);
  $("fSpeed").value = row.speed ?? 1;
  $("fPauseBefore").value = row.pause_before_ms ?? 0;
  $("fPauseAfter").value = row.pause_after_ms ?? 0;
  $("fNote").value = row.delivery_note || "";
  if (scrollIntoView) {
    const index = visibleRows().findIndex((item) => item.line_id === lineId);
    if (index >= 0) {
      const viewport = $("viewport");
      const top = index * ROW_H;
      if (top < viewport.scrollTop || top + ROW_H > viewport.scrollTop + viewport.clientHeight) {
        viewport.scrollTop = Math.max(0, top - viewport.clientHeight / 2);
      }
    }
  }
  await loadDiff(row);
}

async function loadDiff(row) {
  try {
    const data = await api(`/api/ui/line/diff?${qs({ path: state.path, chapter: state.chapter, line: row.line_id })}`);
    renderDiff(data);
  } catch (error) {
    const body = $("diffBody");
    if (error.code === 1) {
      body.innerHTML = `<div class="muted">这一行还没生成过，没有参数快照可比对。生成一次后这里会显示改了哪些项。</div>`;
    } else {
      body.innerHTML = `<div class="muted">${escapeHtml(error.message)}</div>`;
    }
  }
}

function renderDiff(data) {
  const body = $("diffBody");
  if (!data.snapshot_available) {
    body.innerHTML = `<div class="muted">这一行还没生成过，没有参数快照可比对。</div>`;
    return;
  }
  if (!data.changes || !data.changes.length) {
    body.innerHTML = `<div class="muted">与上次生成参数一致（${data.state}）</div>`;
    return;
  }
  body.innerHTML = data.changes.map((item) => `<div class="item">[${escapeHtml(item.group)}] ${escapeHtml(item.text)}</div>`).join("");
}

function collectChanges(row) {
  const next = {
    text: $("fText").value,
    kind: $("fKind").value,
    emotion: $("fEmotion").value,
    emotion_intensity: Number($("fIntensity").value),
    speed: Number($("fSpeed").value),
    pause_before_ms: Number($("fPauseBefore").value),
    pause_after_ms: Number($("fPauseAfter").value),
    delivery_note: $("fNote").value.trim() || null,
  };
  const patch = {};
  Object.entries(next).forEach(([key, value]) => {
    const before = row[key] === undefined ? null : row[key];
    if (String(before ?? "") !== String(value ?? "")) patch[key] = value;
  });
  return patch;
}

async function saveLine() {
  const row = state.rows.find((item) => item.line_id === state.selected);
  if (!row) return;
  const patch = collectChanges(row);
  if (!Object.keys(patch).length) {
    toast("没有字段变化", true);
    return;
  }
  try {
    await api("/api/ui/line/set", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: state.path, chapter: state.chapter, line: row.line_id, ...patch }),
    });
    await loadLines();  // 写操作完成 → 重拉一次，状态由后端按指纹重新派生
    toast("已保存（读取时按参数指纹判定是否过期）", true);
  } catch (error) {
    toast(`保存失败（退出码 ${error.code}）：${error.message}`);
  }
}

async function revertLine() {
  const row = state.rows.find((item) => item.line_id === state.selected);
  if (!row) return;
  try {
    await api("/api/ui/line/revert", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: state.path, chapter: state.chapter, line: row.line_id }),
    });
    await loadLines();
    toast("已回滚到上次生成时的参数", true);
  } catch (error) {
    toast(`回滚失败（退出码 ${error.code}）：${error.message}`);
  }
}

/* ---------------- 生成与队列进度 ---------------- */

/* 读配音链就绪状态：模型缺失/不可用时禁用「开始」并显示原因，不静默降级。 */
async function loadReadiness() {
  try {
    const data = await api("/api/ui/readiness");
    state.ready = Boolean(data.ready);
    state.readyReason = data.reason || "";
  } catch (error) {
    state.ready = false;
    state.readyReason = `读取配音链状态失败：${error.message}`;
  }
  $("btnStart").title = state.ready ? "" : state.readyReason;
}

/* 读「本次将生成 N 行」：走 `tts plan`，与徽标计数同源。 */
async function loadPlan() {
  if (!state.path || !state.chapter) return;
  await loadReadiness();
  try {
    const data = await api(`/api/ui/plan?${qs({ path: state.path, chapter: state.chapter })}`);
    run.total = data.jobs || 0;
    const counts = data.counts || {};
    $("runInfo").textContent = state.ready
      ? `本次将生成 ${run.total} 行（未生成 ${counts.pending || 0} / 过期 ${counts.stale || 0} / 失败 ${counts.failed || 0}）`
      : `无法开始：${state.readyReason}`;
    $("runProgress").textContent = "";
    renderRunFill(0, run.total);
    $("btnStart").disabled = !state.ready || run.polling || run.total === 0;
  } catch (error) {
    $("runInfo").textContent = `无法读取生成计划：${error.message}`;
    $("btnStart").disabled = true;
  }
}

/* 进度条：只按「已完成 / 总数」推进，不做估算。 */
function renderRunFill(done, total) {
  $("runFill").style.width = total > 0 ? `${Math.min(100, (done / total) * 100)}%` : "0";
}

/* 点「开始生成」：入队 + 后台消费进程，接口立即返回，进度靠 queue list。 */
async function startGenerate() {
  if (!state.path || !state.chapter) return;
  try {
    const result = await api("/api/ui/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: state.path, chapter: state.chapter }),
    });
    run.since = result.since || null;
    run.seenJobs = false;
    run.idleSince = Date.now();
    run.patched = new Set();
    run.done = new Set();
    run.failed = new Set();
    run.polling = true;
    $("btnStart").disabled = true;
    $("runProgress").textContent = result.started ? "已入队，等待队列…" : `已有 ${result.active} 条任务在执行，跟随进度`;
    schedulePoll(600);
  } catch (error) {
    toast(`启动失败（退出码 ${error.code}）：${error.message}`);
  }
}

function schedulePoll(delay) {
  clearTimeout(run.timer);
  run.timer = setTimeout(pollQueue, delay);
}

/* 轮询队列：只拉 queue list；行状态只按行回填。 */
async function pollQueue() {
  if (!run.polling) return;
  let jobs = [];
  try {
    jobs = (await api(`/api/ui/queue?${qs({ path: state.path })}`)) || [];
  } catch (error) {
    toast(`读取队列失败：${error.message}`);
    schedulePoll(POLL_IDLE_MS);
    return;
  }
  const mine = jobs.filter(
    (job) => job.kind === "tts" && (job.target || {}).chapter_id === state.chapter
  ).filter((job) => !run.since || (job.created_at || "") >= run.since);

  const finished = [];
  mine.forEach((job) => {
    const lineId = job.target.line_id;
    if (run.patched.has(lineId)) return;
    if (job.status === "succeeded") {
      run.patched.add(lineId);
      run.done.add(lineId);
      finished.push(job);
    } else if (job.status === "failed") {
      run.patched.add(lineId);
      run.failed.add(lineId);
      finished.push(job);
    }
  });

  const active = mine.some((job) => job.status === "queued" || job.status === "running");
  if (mine.length) run.seenJobs = true;
  $("runProgress").textContent =
    `完成 ${run.done.size} / ${run.total}` + (run.failed.size ? `（失败 ${run.failed.size}）` : "");
  renderRunFill(run.done.size, run.total);

  // 某行完成 → 只取那一行的状态，只重画那一行
  for (const job of finished) await patchRow(job.target.line_id);

  if (active) {
    run.idleSince = Date.now();
    schedulePoll(POLL_ACTIVE_MS);
    return;
  }
  // 队列暂时空了：worker 可能还没入队，或者还有尾巴，空闲期放宽到 4 秒再看一眼
  const grace = run.seenJobs ? TAIL_GRACE_MS : START_GRACE_MS;
  if (Date.now() - run.idleSince < grace) {
    schedulePoll(POLL_IDLE_MS);
    return;
  }
  run.polling = false;
  $("btnStart").disabled = false;
  await loadPlan();
  toast(`生成结束：成功 ${run.done.size} / 失败 ${run.failed.size}`, run.failed.size === 0);
}

/* 单行回填：拿一行新状态，替换数组里那一行并只重画那一行节点。 */
async function patchRow(lineId) {
  try {
    const data = await api(`/api/ui/line?${qs({ path: state.path, chapter: state.chapter, line: lineId })}`);
    const fresh = (data.lines || [])[0];
    const index = state.rows.findIndex((row) => row.line_id === lineId);
    if (!fresh || index < 0) return;
    const before = state.rows[index];
    state.rows[index] = fresh;
    bumpCount(before.state, fresh.state);
    if (state.filter.size && !state.filter.has(fresh.state)) {
      // 过滤条件下这一行要离开/进入列表，属于结构性变化，只能重排
      renderTable();
      return;
    }
    updateRowDom(lineId);
    if (state.selected === lineId) {
      const badge = $("lineState");
      badge.className = `state-badge s-${fresh.state}`;
      badge.innerHTML = `<span class="dot"></span>${fresh.state}`;
    }
  } catch (error) {
    // 单行回填失败不影响整轮轮询，下一次 poll 会再试
  }
}

/* 本地挪动一个计数：只动「旧状态」与「新状态」两个格子，不重算整表。 */
function bumpCount(from, to) {
  if (from === to) return;
  if (typeof state.counts[from] === "number") state.counts[from] = Math.max(0, state.counts[from] - 1);
  if (typeof state.counts[to] === "number") state.counts[to] += 1;
  else state.counts[to] = 1;
  renderBadges();
}

function bindEvents() {
  $("btnOpen").addEventListener("click", openProject);
  $("btnReload").addEventListener("click", async () => {
    await loadLines();
    toast("已刷新", true);
  });
  $("chapterSelect").addEventListener("change", async (event) => {
    run.polling = false;
    clearTimeout(run.timer);
    state.chapter = event.target.value;
    state.selected = null;
    $("form").hidden = true;
    $("lineId").textContent = "未选中行";
    await loadChapter();
  });
  $("badges").addEventListener("click", (event) => {
    const badge = event.target.closest(".badge");
    if (!badge) return;
    const name = badge.dataset.state;
    if (!name) {
      state.filter.clear();
    } else if (state.filter.has(name)) {
      state.filter.delete(name);
    } else {
      state.filter.add(name);
    }
    renderBadges();
    renderTable();
  });
  $("viewport").addEventListener("scroll", () => {
    state.scrollTop = $("viewport").scrollTop;
    renderTable();
  });
  $("rows").addEventListener("click", (event) => {
    const target = event.target.closest(".row");
    if (target) selectLine(target.dataset.line);
  });
  $("fIntensity").addEventListener("input", () => {
    $("oIntensity").textContent = fmt($("fIntensity").value);
  });
  $("btnSave").addEventListener("click", saveLine);
  $("btnRevert").addEventListener("click", revertLine);
  $("btnStart").addEventListener("click", startGenerate);
  window.addEventListener("resize", renderTable);
}

async function boot() {
  fillEmotions();
  bindEvents();
  await loadProjects();
  if (state.path) await openProject();
}

boot();
