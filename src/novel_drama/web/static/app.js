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
  flowData: { worlds: 0, characters: 0 },  // 流程条「世界 / 人物」完成数，后续任务填数，缺省按 0
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

/* toast / api / qs / stateBadge / kindLabel / fmt / escapeHtml 已下沉到 core.js，
 * 这里直接用全局同名函数，行为与原来逐字一致。 */

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
  const duration = row.duration_ms ? Math.round(row.duration_ms) : "—";
  // 已就绪且有音频的行：时长格里换成试听按钮，时长放在按钮文字里
  const playable = row.state === "ready" && row.audio;
  const durationCell = playable
    ? `<span class="c-num c-play"><button class="play-btn" type="button" data-play="${row.line_id}" title="试听">▶</button>${duration}</span>`
    : `<span class="c-num">${duration}</span>`;
  return (
    `<div class="row ${selected}" data-line="${row.line_id}" style="position:absolute;top:${top}px;left:0;right:0">` +
    `<span class="c-index">${row.index ?? ""}</span>` +
    `<span class="c-kind">${kindLabel(row.kind)}</span>` +
    `<span class="c-speaker ${unknown}">${escapeHtml(row.speaker || "")}</span>` +
    `<span class="c-text" title="${text}">${text}</span>` +
    `<span class="c-num">${escapeHtml(emotionLabel(row.emotion))}</span>` +
    `<span class="c-num">${fmt(row.emotion_intensity)}</span>` +
    `<span class="c-num">${fmt(row.speed)}</span>` +
    `<span class="c-state">${stateBadge(row.state)}</span>` +
    durationCell +
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

/* kindLabel / fmt / escapeHtml 已下沉到 core.js（本页仍按同名全局函数调用）。 */

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

/* 读章节列表。本地开发服务在并发请求下偶发串响应（拿到别的接口的 body），
 * 不是数组就重试一次，避免整页被一次错响应带空。
 * 参数：path 工程目录。
 * 返回：Promise<章节数组>（两次都拿不到时返回空数组）。
 */
async function fetchChapters(path) {
  for (let attempt = 0; attempt < 2; attempt += 1) {
    const data = await api(`/api/ui/chapters?${qs({ path })}`);
    if (Array.isArray(data)) return data;
  }
  return [];
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
    const chapters = await fetchChapters(path);
    $("chapterSelect").innerHTML = chapters
      .map((item) => `<option value="${item.id}">${item.index}. ${escapeHtml(item.title)}</option>`)
      .join("");
    if (!chapters.length) {
      $("hint").textContent = "工程里还没有章节";
      /* 新工程没有章节：必须清掉上一本的当前章节，否则时间轴会拿旧 chapter 去请求并报错 */
      state.chapter = "";
      if ($("chapterSelect")) $("chapterSelect").value = "";
      state.rows = [];
      state.counts = {};
      renderBadges();
      renderTable();
      $("runInfo").textContent = "本次将生成 0 行";
      $("runProgress").textContent = "";
      $("btnStart").disabled = true;
      $("btnStop").hidden = true;
      NW.notifyProject();
      NW.notifyChapter();
      return;
    }
    const keep = chapters.some((item) => item.id === state.chapter);
    state.chapter = keep ? state.chapter : chapters[0].id;
    $("chapterSelect").value = state.chapter;
    await loadChapter();
    NW.notifyProject();
    NW.notifyChapter();
  } catch (error) {
    toast(`打开工程失败：${error.message}`);
  }
}

/* 切章的唯一落点：顶栏下拉、左栏树、右栏动作都只发 `chapter` 事件到这里，
 * 由它同步 #chapterSelect、清空当前选中行并重载，避免各处互相触发成环。
 * 参数：chapterId 目标章节 ID。
 * 返回：Promise，无。
 */
async function applyChapter(chapterId) {
  if (!chapterId) return;
  const select = $("chapterSelect");
  if (!select || !Array.from(select.options).some((option) => option.value === chapterId)) return;
  run.polling = false;
  clearTimeout(run.timer);
  state.chapter = chapterId;
  state.selected = null;
  if (select.value !== chapterId) select.value = chapterId; // 静默同步，不再派发 change
  $("form").hidden = true;
  $("lineId").textContent = "未选中行";
  await loadChapter();
  NW.notifyChapter();
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
  badge.innerHTML = `<span class="dot"></span>${STATE_LABELS[row.state] || row.state || ""}`;
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
  // 选中行交给右栏 Inspector（voice 视图下右栏就是本该页的编辑表单）
  NW.emit("line", row);
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
    body.innerHTML = `<div class="muted">与上次生成参数一致（${STATE_LABELS[data.state] || data.state}）</div>`;
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

/* ---------------- 试听与生成控制 ---------------- */

/* 试听状态：全页共用一个 Audio，切行时先停掉上一个。 */
const playback = { audio: null, line: "" };

/* 播放/暂停某行配音。
 * 参数：lineId 台词行 ID。
 * 返回：无。
 */
function togglePlay(lineId) {
  const row = state.rows.find((item) => item.line_id === lineId);
  if (!row || !row.audio) return;
  if (playback.line === lineId && playback.audio) {
    if (playback.audio.paused) playback.audio.play();
    else playback.audio.pause();
    return;
  }
  if (playback.audio) playback.audio.pause();
  const audio = new Audio(uiFileUrl(row.audio, state.path));
  playback.audio = audio;
  playback.line = lineId;
  audio.addEventListener("ended", () => {
    if (playback.audio === audio) playback.line = "";
  });
  audio.play().catch((error) => toast(`试听失败：${error.message}`));
}

/* 询问生成方式：已有配音时让用户选「继续补生成」还是「全部重新生成」。
 * 参数：readyCount 已就绪行数；pendingCount 待补生成行数。
 * 返回：Promise<"continue"|"regenerate"|null>，null 表示取消。
 */
function askGenerateMode(readyCount, pendingCount) {
  return new Promise((resolve) => {
    let done = false;
    const finish = (value) => {
      if (done) return;
      done = true;
      document.removeEventListener("keydown", onKey, true);
      mask.remove();
      resolve(value);
    };
    // Esc 等同于「取消」
    const onKey = (event) => {
      if (event.key === "Escape") finish(null);
    };
    const mask = document.createElement("div");
    mask.className = "modal-mask";
    const dialog = document.createElement("div");
    dialog.className = "modal-editor choice-dialog";
    dialog.setAttribute("role", "dialog");
    dialog.setAttribute("aria-modal", "true");

    const title = document.createElement("h3");
    title.className = "modal-title";
    title.textContent = "选择生成方式";
    dialog.appendChild(title);

    const hint = document.createElement("p");
    hint.className = "modal-hint";
    hint.textContent = pendingCount
      ? `已有 ${readyCount} 行配音，还有 ${pendingCount} 行待生成。`
      : `已有 ${readyCount} 行配音，没有待生成的行。`;
    dialog.appendChild(hint);

    const actions = document.createElement("div");
    actions.className = "choice-actions";
    dialog.appendChild(actions);

    if (pendingCount) {
      const cont = document.createElement("button");
      cont.className = "btn primary";
      cont.type = "button";
      cont.textContent = `继续生成（补 ${pendingCount} 行）`;
      cont.addEventListener("click", () => finish("continue"));
      actions.appendChild(cont);
    }

    const regen = document.createElement("button");
    regen.className = "btn danger";
    regen.type = "button";
    regen.textContent = `全部重新生成（覆盖 ${readyCount + pendingCount} 行）`;
    regen.addEventListener("click", () => finish("regenerate"));
    actions.appendChild(regen);

    const cancel = document.createElement("button");
    cancel.className = "btn";
    cancel.type = "button";
    cancel.textContent = "取消";
    cancel.addEventListener("click", () => finish(null));
    actions.appendChild(cancel);

    mask.appendChild(dialog);
    mask.addEventListener("click", (event) => {
      if (event.target === mask) finish(null);
    });
    document.body.appendChild(mask);
    document.addEventListener("keydown", onKey, true);
  });
}

/* 同步运行区按钮：生成中显示「停止」并禁用「开始」。
 * 参数：无。
 * 返回：无。
 */
function syncRunButtons() {
  const phase = $("btnStop");
  if (phase) phase.hidden = !run.polling;
  const readyCount = state.counts.ready || 0;
  $("btnStart").disabled =
    run.polling || !state.ready || (run.total === 0 && readyCount === 0);
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
    const readyCount = state.counts.ready || 0;
    if (!state.ready) {
      $("runInfo").textContent = `无法开始：${state.readyReason}`;
    } else if (run.total === 0 && readyCount > 0) {
      $("runInfo").textContent = `全部 ${readyCount} 行已生成，再次生成会覆盖现有配音`;
    } else {
      $("runInfo").textContent =
        `本次将生成 ${run.total} 行（未生成 ${counts.pending || 0} / 过期 ${counts.stale || 0} / 失败 ${counts.failed || 0}）`;
    }
    $("runProgress").textContent = "";
    renderRunFill(0, run.total);
    syncRunButtons();
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
  // 已有配音时先问方式：继续补生成 / 全部重新生成
  const readyCount = state.counts.ready || 0;
  let mode = "continue";
  if (readyCount > 0) {
    const picked = await askGenerateMode(readyCount, run.total);
    if (!picked) return;
    mode = picked;
  }
  try {
    const result = await api("/api/ui/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: state.path, chapter: state.chapter, mode }),
    });
    if (mode === "regenerate") run.total = readyCount + run.total;
    run.since = result.since || null;
    run.seenJobs = false;
    run.idleSince = Date.now();
    run.patched = new Set();
    run.done = new Set();
    run.failed = new Set();
    run.polling = true;
    syncRunButtons();
    $("runProgress").textContent = result.started
      ? (mode === "regenerate" ? "已重新入队，等待队列…" : "已入队，等待队列…")
      : `已有 ${result.active} 条任务在执行，跟随进度`;
    schedulePoll(600);
  } catch (error) {
    toast(`启动失败（退出码 ${error.code}）：${error.message}`);
  }
}

/* 停止生成：终止后台消费进程，未完成任务由后端标记取消。
 * 参数：无。
 * 返回：Promise，无。
 */
async function stopGenerate() {
  if (!state.path) return;
  $("btnStop").disabled = true;
  try {
    const result = await api("/api/ui/stop", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: state.path }),
    });
    run.polling = false;
    clearTimeout(run.timer);
    await loadLines();
    await loadPlan();
    toast(result.canceled ? `已停止，取消 ${result.canceled} 条任务` : "已停止", true);
  } catch (error) {
    toast(`停止失败：${error.message}`);
  } finally {
    $("btnStop").disabled = false;
    syncRunButtons();
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
    } else if (job.status === "canceled") {
      // 被停止的任务：刷新那一行，让它回到真实状态
      run.patched.add(lineId);
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
  syncRunButtons();
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
      badge.innerHTML = `<span class="dot"></span>${STATE_LABELS[fresh.state] || fresh.state}`;
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
  $("chapterSelect").addEventListener("change", (event) => {
    // 只发事件，真正的切章逻辑收敛在 applyChapter
    NW.emit("chapter", event.target.value);
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
    const play = event.target.closest(".play-btn");
    if (play) {
      // 试听按钮不触发整行选中
      event.stopPropagation();
      togglePlay(play.dataset.play);
      return;
    }
    const target = event.target.closest(".row");
    if (target) selectLine(target.dataset.line);
  });
  $("fIntensity").addEventListener("input", () => {
    $("oIntensity").textContent = fmt($("fIntensity").value);
  });
  $("btnSave").addEventListener("click", saveLine);
  $("btnRevert").addEventListener("click", revertLine);
  $("btnStart").addEventListener("click", startGenerate);
  $("btnStop").addEventListener("click", stopGenerate);
  window.addEventListener("resize", renderTable);
}

/* 对外暴露共享能力：其余页面复用同一套取数与提示，避免各写一份。
 * 页面在 window.Pages 注册，由 core.js 的路由在激活时调用。 */
const projectListeners = [];
const chapterListeners = [];
const busListeners = {}; // 通用事件总线：给左栏树 / 流程条 / 后续阶段订阅用
window.NW = {
  state,
  api,
  toast,
  qs,
  escapeHtml,
  fmt,
  kindLabel,
  emotionLabel,
  statusLabel,
  openProject,
  loadChapter,
  /* 重新拉工程列表（新建工程后刷新顶栏候选，避免要刷新页面才出现） */
  refreshProjects: loadProjects,
  onProject(fn) {
    projectListeners.push(fn);
  },
  onChapter(fn) {
    chapterListeners.push(fn);
  },
  notifyProject() {
    projectListeners.forEach((fn) => fn());
  },
  notifyChapter() {
    chapterListeners.forEach((fn) => fn());
  },
  /* 订阅通用事件。
   * 参数：name 事件名（如 chapter / scene）；fn 回调，收到 payload。
   * 返回：无。
   */
  on(name, fn) {
    if (!busListeners[name]) busListeners[name] = [];
    busListeners[name].push(fn);
  },
  /* 派发通用事件。
   * 参数：name 事件名；payload 负载。
   * 返回：无。
   */
  emit(name, payload) {
    (busListeners[name] || []).forEach((fn) => fn(payload));
  },
};

async function boot() {
  fillEmotions();
  bindEvents();
  // 章节切换只有一个入口：任何地方切章都只发 `chapter` 事件，由 applyChapter 统一落库到界面
  NW.on("chapter", applyChapter);
  // 右栏「在配音页校对」：切到配音视图并选中该行
  NW.on("focusLine", async (lineId) => {
    if (!lineId) return;
    if (location.hash !== "#/voice") location.hash = "#/voice";
    if (!state.rows.some((row) => row.line_id === lineId)) await loadLines();
    await selectLine(lineId);
  });
  // 工程切换时同步顶栏输入框（左树 / 页面里新建或打开的工程都走 notifyProject）
  NW.onProject(() => {
    $("projectPath").value = state.path;
  });
  if (window.Router) window.Router.init();
  await loadProjects();
  if (state.path) await openProject();
}

boot();
