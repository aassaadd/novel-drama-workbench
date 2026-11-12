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
  pending: "待生成", queued: "排队", running: "运行", ready: "就绪",
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
  // 最近若干条成功任务的耗时（毫秒），用于估算剩余时间
  samples: [],
};

// 预计剩余：至少 2 条成功样本才显示，滑动平均只取最近 8 条
const ETA_MIN_SAMPLES = 2;
const ETA_SAMPLE_MAX = 8;

const $ = (id) => document.getElementById(id);

/* toast / api / qs / stateBadge / kindLabel / fmt / escapeHtml 已下沉到 core.js，
 * 这里直接用全局同名函数，行为与原来逐字一致。 */

/* 行内「重录」按钮：只重跑这一行，旧版本文件保留，可在右栏对比试听。
 * 参数：lineId 台词行 ID。
 * 返回：无（结果通过 toast 与刷新行体现）。
 */
async function rerunLine(lineId) {
  if (!state.path || !state.chapter) return;
  if (rerunning.has(lineId)) return;
  rerunning.add(lineId);
  const button = $("rows").querySelector(`.row[data-line="${CSS.escape(lineId)}"] .row-rerun`);
  if (button) button.disabled = true;
  try {
    const result = await api("/api/ui/line/rerun", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: state.path, chapter: state.chapter, line: lineId }),
      busy: "正在重录这一行…",
      busyDetail: lineId,
    });
    await loadLines();
    if (state.selected === lineId) await loadVersions(lineId);
    toast(
      `已重录 ${lineId}：r${result.previous_revision} → r${result.revision}` +
        (result.timeline_rebuilt ? "（时间轴已重建）" : ""),
      true
    );
  } catch (error) {
    // 后端已经把 provider 错误翻译成中文；这里不吞错，原样展示
    toast(`重录失败：${error.message}`);
    await loadLines();
  } finally {
    rerunning.delete(lineId);
  }
}

/* 读一行的录音版本列表并渲染到右栏。 */
async function loadVersions(lineId) {
  const list = $("versionList");
  const active = $("activeVersion");
  if (!list) return;
  list.innerHTML = '<div class="muted">读取版本…</div>';
  try {
    const data = await api(`/api/ui/line/versions?${qs({ path: state.path, chapter: state.chapter, line: lineId })}`);
    const versions = data.versions || [];
    if (active) active.textContent = data.revision ? `当前生效 r${data.revision}` : "";
    if (!versions.length) {
      list.innerHTML = '<div class="muted">还没有生成过音频</div>';
      return;
    }
    list.innerHTML = versions
      .map(
        (item) =>
          `<div class="version-row ${item.active ? "active" : ""}">` +
          `<span class="tag ${item.active ? "on" : ""}">r${item.revision}${item.active ? " 当前生效" : " 旧版"}</span>` +
          `<button class="btn small" type="button" data-version-play="${escapeHtml(item.path)}">${
            item.active ? "听新版" : "听旧版"
          }</button>` +
          `<span class="muted">${Math.round((item.size || 0) / 1024)} KB</span>` +
          `</div>`
      )
      .join("");
  } catch (error) {
    list.innerHTML = `<div class="muted">读取版本失败：${escapeHtml(error.message)}</div>`;
  }
}

/* 播放指定工程内音频（版本对比也走这一个 Audio，避免叠音）。 */
function playFile(rel) {
  if (!rel) return;
  if (playback.audio) playback.audio.pause();
  const audio = new Audio(uiFileUrl(rel, state.path));
  playback.audio = audio;
  playback.line = "";
  audio.play().catch((error) => toast(`试听失败：${error.message}`));
}

/* 注入右栏「录音版本」区块（不改 index.html，避免与并行改动冲突）。 */
function injectVersionPanel() {
  const form = $("form");
  if (!form || document.getElementById("versionList")) return;
  const box = document.createElement("div");
  box.className = "versions";
  box.innerHTML = `
    <div class="diff-title">录音版本 <span class="muted" id="activeVersion"></span></div>
    <div class="diff-body" id="versionList"><div class="muted">选中一行后显示</div></div>`;
  form.appendChild(box);
  box.addEventListener("click", (event) => {
    const button = event.target.closest("[data-version-play]");
    if (button) playFile(button.getAttribute("data-version-play"));
  });
}

/* 重录请求的并发去重：同一行重复点击不再发第二次。 */
const rerunning = new Set();

/* ---------------- 补齐缺失（轻量增量批量） ---------------- */

/* 批量界面状态：面板 DOM 由脚本注入，避免改 index.html。
 * 只补缺失/失败行，已有音频跳过；scope=chapter 默认只看当前章。 */
const batch = { timer: null, polling: false, plan: null, scope: "chapter" };

/* 打开/收起整本批量面板；打开时先读预计条数与阻断项。 */
async function toggleBatchPanel() {
  const panel = $("batchPanel");
  if (!panel) return;
  if (!panel.hidden) {
    panel.hidden = true;
    return;
  }
  panel.hidden = false;
  const scopeSelect = $("batchScope");
  if (scopeSelect) scopeSelect.value = batch.scope;
  await refreshBatch();
}

/* 读取补齐计划：跳过多少、实际要补多少、按历史均速预计多久。 */
async function refreshBatch() {
  if (!state.path) {
    $("batchPlanInfo").textContent = "先打开工程";
    return;
  }
  if (batch.scope === "chapter" && !state.chapter) {
    $("batchPlanInfo").textContent = "先选一个章节";
    $("batchStart").disabled = true;
    return;
  }
  const progress = await readBatchProgress(false);
  if (progress && progress.running) {
    renderBatchProgress(progress);
    startBatchPolling();
    return;
  }
  try {
    const query = { path: state.path, scope: batch.scope };
    if (batch.scope === "chapter") query.chapter = state.chapter;
    const plan = await api(`/api/ui/tts/batch-all/plan?${qs(query)}`);
    batch.plan = plan;
    const parts = [`跳过 ${plan.skipped || 0} 行`, `待补 ${plan.total} 行`];
    if (plan.total > 0) {
      parts.push(
        plan.eta_ms
          ? `预计约 ${formatEta(plan.eta_ms)}（按 ${Math.round((plan.avg_ms_per_line || 0) / 1000)} 秒/行）`
          : "首次运行，暂无速度可估算"
      );
    }
    if (plan.blockers && plan.blockers.length) parts.push(`阻断项 ${plan.blockers.length} 个`);
    if (!plan.ready) parts.push(plan.ready_reason || "provider 不可用");
    $("batchPlanInfo").textContent = plan.total > 0 || plan.blockers?.length ? parts.join(" · ") : "当前范围没有缺失，无需补齐";
    $("batchStart").disabled = !plan.ready || plan.total <= 0 || (plan.blockers || []).length > 0;
    $("batchProgress").textContent = (plan.blockers || []).length
      ? `先修正音色编排：${plan.blockers.slice(0, 3).join("；")}`
      : "";
    $("batchFailures").innerHTML = "";
    renderBatchFill(0, plan.total);
    $("batchEta").textContent = "";
  } catch (error) {
    $("batchPlanInfo").textContent = `读取失败：${error.message}`;
    $("batchStart").disabled = true;
  }
}

/* 启动「补齐缺失」（后台进程），随后进入轮询。 */
async function startBatch() {
  if (!state.path) return;
  $("batchStart").disabled = true;
  try {
    const body = { path: state.path, scope: batch.scope };
    if (batch.scope === "chapter") body.chapter = state.chapter;
    const result = await api("/api/ui/tts/batch-all", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    $("batchProgress").textContent = `已开始补齐：共 ${result.total} 行，等待第一行完成…`;
    $("batchFailures").innerHTML = "";
    startBatchPolling();
  } catch (error) {
    $("batchProgress").textContent = `启动失败：${error.message}`;
    $("batchStart").disabled = false;
  }
}

/* 轮询进度文件；有任务时 2.5 秒一次，结束后停。 */
function startBatchPolling() {
  batch.polling = true;
  clearTimeout(batch.timer);
  scheduleBatchPoll(600);
}

function scheduleBatchPoll(delay) {
  clearTimeout(batch.timer);
  batch.timer = setTimeout(pollBatch, delay);
}

async function pollBatch() {
  if (!batch.polling) return;
  const progress = await readBatchProgress(true);
  if (!progress) {
    scheduleBatchPoll(2500);
    return;
  }
  renderBatchProgress(progress);
  if (progress.running) {
    scheduleBatchPoll(2500);
    return;
  }
  batch.polling = false;
  if (progress.interrupted) {
    // 用户停止或进程崩溃：不重跑已完成行，再点一次会从剩下的行继续
    await loadLines();
    await loadPlan();
    await refreshBatch();
    $("batchProgress").textContent = `已中断：完成 ${progress.done}/${progress.total}，再点「开始补齐」会接着跑`;
    toast(`补齐已中断：完成 ${progress.done}/${progress.total}，已完成的不会重复合成`);
  } else if (progress.finished) {
    await loadLines();
    await loadPlan();
    toast(
      `补齐结束：完成 ${progress.done}/${progress.total}，失败 ${progress.failed}`,
      progress.failed === 0
    );
  }
  $("batchStart").disabled = false;
}

/* 读进度；静默模式下失败返回 null，不打扰用户。 */
async function readBatchProgress(quiet) {
  if (!state.path) return null;
  try {
    return await api(`/api/ui/tts/batch-all/progress?${qs({ path: state.path })}`);
  } catch (error) {
    if (!quiet) $("batchProgress").textContent = `读取进度失败：${error.message}`;
    return null;
  }
}

/* 渲染进度、预计剩余与失败行清单。 */
function renderBatchProgress(progress) {
  const total = progress.total || 0;
  const done = progress.done || 0;
  renderBatchFill(done, total);
  $("batchProgress").textContent =
    `完成 ${done}/${total}` +
    (progress.failed ? `（失败 ${progress.failed}）` : "") +
    (progress.running && progress.current_chapter ? ` · 当前 ${progress.current_chapter}` : "") +
    (progress.finished && !progress.running ? " · 已结束" : "");
  const eta = $("batchEta");
  if (progress.running && progress.eta_ms > 0) {
    eta.textContent = `预计剩余 ${formatEta(progress.eta_ms)}`;
    eta.hidden = false;
  } else {
    eta.textContent = "";
    eta.hidden = true;
  }
  renderBatchFailures(progress.rows || []);
  if (!progress.running && progress.log_tail && done === 0) {
    $("batchProgress").textContent += "（没有产出，见日志）";
  }
}

function renderBatchFill(done, total) {
  const fill = $("batchFill");
  if (fill) fill.style.width = total > 0 ? `${Math.min(100, (done / total) * 100)}%` : "0";
}

/* 失败行清单：可定位到那一行，也可就地单行重录。 */
function renderBatchFailures(rows) {
  const box = $("batchFailures");
  if (!box) return;
  if (!rows.length) {
    box.innerHTML = "";
    return;
  }
  box.innerHTML = rows
    .map((row) => {
      const where = `${row.chapter_title || row.chapter_id || ""} · ${row.line_id || ""}`;
      return (
        `<div class="batch-failure">` +
        `<span class="where">${escapeHtml(where)}</span>` +
        `<span class="why" title="${escapeHtml(row.message || "")}">${escapeHtml(row.message || "未记录原因")}</span>` +
        `<button class="btn small" type="button" data-batch-goto="${escapeHtml(row.chapter_id || "")}" data-batch-line="${escapeHtml(
          row.line_id || ""
        )}">定位</button>` +
        `<button class="btn small primary" type="button" data-batch-retry="${escapeHtml(
          row.chapter_id || ""
        )}" data-batch-line="${escapeHtml(row.line_id || "")}">重录</button>` +
        `</div>`
      );
    })
    .join("");
}

/* 注入「批量补齐缺失」入口与面板（不改 index.html）。
 * 面板挂在顶部徽标下方，打开时一定可见；不放在底部运行栏之后，避免被折叠区挡住。 */
function injectBatchPanel() {
  const view = $("view-voice");
  if (!view || document.getElementById("batchPanel")) return;
  const actions = view.querySelector(".run-actions");
  if (actions) {
    const button = document.createElement("button");
    button.className = "btn";
    button.type = "button";
    button.id = "btnBatchAll";
    button.textContent = "批量补齐缺失";
    actions.insertBefore(button, actions.firstChild);
    button.addEventListener("click", toggleBatchPanel);
  }
  const panel = document.createElement("div");
  panel.className = "batch-panel";
  panel.id = "batchPanel";
  panel.hidden = true;
  panel.innerHTML = `
    <div class="batch-head">
      <b>批量补齐缺失</b>
      <select class="input" id="batchScope">
        <option value="chapter">仅当前章节</option>
        <option value="all">全书缺失</option>
      </select>
      <span class="batch-meta" id="batchPlanInfo">读取中…</span>
      <button class="btn small primary" type="button" id="batchStart" disabled>开始补齐</button>
      <button class="btn small" type="button" id="batchRefresh">刷新</button>
      <button class="btn small" type="button" id="batchClose">收起</button>
    </div>
    <div class="run-meter" style="margin-top:8px">
      <div class="run-track"><div class="run-fill" id="batchFill"></div></div>
      <span class="run-eta" id="batchEta" hidden></span>
    </div>
    <div class="batch-meta" id="batchProgress" style="margin-top:6px"></div>
    <div class="batch-meta" style="margin-top:4px">只补未生成/失败/过期的行，已有音频跳过；中途停止后再点「开始补齐」会接着跑。</div>
    <div class="batch-failures" id="batchFailures"></div>`;
  // 顶部徽标下方：面板打开时不挤在底部运行栏之外
  const badges = view.querySelector(".badges");
  if (badges && badges.parentElement) badges.insertAdjacentElement("afterend", panel);
  else view.appendChild(panel);
  panel.addEventListener("change", (event) => {
    if (event.target.id === "batchScope") {
      batch.scope = event.target.value === "all" ? "all" : "chapter";
      refreshBatch();
    }
  });
  panel.addEventListener("click", async (event) => {
    if (event.target.id === "batchStart") return startBatch();
    if (event.target.id === "batchRefresh") return refreshBatch();
    if (event.target.id === "batchClose") {
      panel.hidden = true;
      return;
    }
    const goto = event.target.closest("[data-batch-goto]");
    const retry = event.target.closest("[data-batch-retry]");
    const target = goto || retry;
    if (!target) return;
    const chapterId = target.getAttribute("data-batch-goto") || target.getAttribute("data-batch-retry");
    const lineId = target.getAttribute("data-batch-line");
    if (!chapterId || !lineId) return;
    if (retry) {
      await NW.openLine(chapterId, lineId);
      await rerunLine(lineId);
    } else {
      panel.hidden = true;
      await NW.openLine(chapterId, lineId);
    }
  });
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
    `<span class="c-speaker ${unknown}">${escapeHtml(speakerLabel(row.speaker, row.speaker_id))}</span>` +
    `<span class="c-text" title="${text}">${text}</span>` +
    `<span class="c-num">${escapeHtml(emotionLabel(row.emotion))}</span>` +
    `<span class="c-num">${fmt(row.emotion_intensity)}</span>` +
    `<span class="c-num">${fmt(row.speed)}</span>` +
    `<span class="c-state">${stateBadge(row.state)}</span>` +
    durationCell +
    `<span class="c-num">${row.pause_before_ms ?? ""}</span>` +
    `<span class="c-num">${row.pause_after_ms ?? ""}</span>` +
    `<span class="c-act"><button class="row-rerun" type="button" data-rerun="${row.line_id}" title="只重录这一行（保留旧版）">⟳</button></span>` +
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
      $("runInfo").textContent = "工程里还没有章节：先到「大纲」页落地成章节";
      $("runProgress").textContent = "";
      $("btnStart").disabled = true;
      $("btnStop").hidden = true;
      $("runEta").hidden = true;
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
    // 工程被删除或移动后 localStorage 仍留着旧路径：清掉失效状态并直接展开工程切换器，
    // 否则页面会一直停在「先打开工程」，用户只能从技术报错里猜下一步。
    const message = String(error.message || "");
    if (/工程不存在|未初始化|PRECONDITION/i.test(message)) {
      state.path = "";
      state.chapter = "";
      state.rows = [];
      state.counts = {};
      localStorage.removeItem("ndw.path");
      $("projectPath").value = "";
      $("chapterSelect").innerHTML = "";
      $("hint").textContent = "工程不可用，请重新选择或新建";
      renderBadges();
      renderTable();
      $("runInfo").textContent = "先选择工程和章节";
      $("btnStart").disabled = true;
      NW.notifyProject();
      NW.notifyChapter();
      if (window.NWLayout && NWLayout.openProjectMenu) NWLayout.openProjectMenu();
      toast("工程目录不存在或已移动：请点左上角工程名，重新打开或新建工程");
      return;
    }
    toast(`打开工程失败：${message}`);
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
  // 切章后提示本章音色/BGM 编排问题，不阻断切换
  void refreshCastHint(chapterId);
  // 面板开着时换章：预计条数会变，跟着刷新（运行中则只刷新进度）
  const batchPanel = $("batchPanel");
  if (batchPanel && !batchPanel.hidden) await refreshBatch();
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
  await loadVersions(lineId);
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
      cont.textContent = `只生成缺失的 ${pendingCount} 行`;
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
    renderEta(0);
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

/* 取一条任务的实际耗时：优先「开始→结束」，缺失开始时间时退回「入队→结束」。
 * 参数：job 队列任务。
 * 返回：毫秒数；无法计算时返回 0。
 */
function jobDurationMs(job) {
  const timings = job.timings || {};
  const end = Date.parse(timings.finished_at || "");
  const start = Date.parse(timings.started_at || job.created_at || "");
  if (!Number.isFinite(end) || !Number.isFinite(start)) return 0;
  const ms = end - start;
  return ms > 0 ? ms : 0;
}

/* 记录一条成功任务的耗时，超出上限时丢掉最旧的样本（滑动平均）。
 * 参数：ms 单条耗时（毫秒）。
 * 返回：无。
 */
function pushEtaSample(ms) {
  if (!(ms > 0)) return;
  run.samples.push(ms);
  if (run.samples.length > ETA_SAMPLE_MAX) run.samples.shift();
}

/* 把剩余毫秒写成人话：秒 / 分秒 / 约 X 小时 Y 分。
 * 参数：ms 剩余毫秒。
 * 返回：中文时长文本。
 */
function formatEta(ms) {
  const total = Math.max(1, Math.round(ms / 1000));
  if (total >= 3600) {
    let hours = Math.floor(total / 3600);
    let minutes = Math.round((total % 3600) / 60);
    if (minutes === 60) {
      hours += 1;
      minutes = 0;
    }
    return minutes ? `约 ${hours} 小时 ${minutes} 分` : `约 ${hours} 小时`;
  }
  if (total >= 60) {
    const minutes = Math.floor(total / 60);
    const seconds = total % 60;
    return seconds ? `${minutes} 分 ${seconds} 秒` : `${minutes} 分`;
  }
  return `${total} 秒`;
}

/* 刷新「预计剩余」：样本不足、没有剩余条数或已不在生成中时隐藏。
 * 参数：remaining 剩余条数。
 * 返回：无。
 */
function renderEta(remaining) {
  const node = $("runEta");
  if (!node) return;
  if (!run.polling || remaining <= 0 || run.samples.length < ETA_MIN_SAMPLES) {
    node.hidden = true;
    node.textContent = "";
    return;
  }
  const avg = run.samples.reduce((sum, ms) => sum + ms, 0) / run.samples.length;
  node.textContent = `预计剩余 ${formatEta(avg * remaining)}`;
  node.hidden = false;
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
    // 新一轮生成：清空上一轮（含失败中断）留下的耗时样本
    run.samples = [];
    renderEta(0);
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
    renderEta(0);
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
      pushEtaSample(jobDurationMs(job));
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
  renderEta(Math.max(0, run.total - run.done.size - run.failed.size));

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
  renderEta(0);
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
    const rerun = event.target.closest(".row-rerun");
    if (rerun) {
      // 重录按钮不触发整行选中
      event.stopPropagation();
      rerunLine(rerun.dataset.rerun);
      return;
    }
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
  // 空态里的下一步：直接去章节页做切分，不再让用户去终端敲命令
  const emptyGo = $("emptyGoChapter");
  if (emptyGo) emptyGo.addEventListener("click", () => { location.hash = "#/chapter"; });
  window.addEventListener("resize", renderTable);
}

/* 音色/BGM 编排提示：切章、保存人物后调用现有检查接口，只提示不阻断。
 * 参数：chapterId 可选章节 ID，空则用当前章节。
 * 返回：Promise，无。
 */
async function refreshCastHint(chapterId) {
  if (!state.path) return;
  const chapter = chapterId || state.chapter || "";
  try {
    const result = await api(`/api/ui/cast/check?${qs({ path: state.path, chapter })}`);
    showCastHintBar(result, chapter ? "本章" : "全剧");
  } catch (error) {
    // 提示性检查失败不影响切章/保存
  }
}

/* 渲染顶部黄条；没有问题时移除黄条。
 * 参数：result 校验结果；scope 范围文案。
 * 返回：无。
 */
function showCastHintBar(result, scope) {
  const findings = (result && result.findings) || [];
  const existing = document.getElementById("castHintBar");
  if (!findings.length) {
    if (existing) existing.remove();
    return;
  }
  const bar = existing || document.createElement("div");
  if (!existing) {
    bar.id = "castHintBar";
    bar.style.cssText = "position:sticky;top:0;z-index:30;margin:8px 12px;padding:10px 12px;border:1px solid #d9a441;background:#fff8e6;color:#7a4d00;border-radius:8px;font-size:13px;display:flex;gap:12px;align-items:flex-start";
    const main = document.querySelector("main") || document.body;
    main.insertBefore(bar, main.firstChild);
  }
  const errors = findings.filter((item) => item.level === "error").length;
  const preview = findings.slice(0, 3).map((item) => item.message).join("；");
  bar.textContent = `${scope}有 ${findings.length} 项音色/BGM 待处理（error ${errors}，不阻断本次操作）：${preview}`;
  const close = document.createElement("button");
  close.type = "button";
  close.textContent = "关闭";
  close.style.cssText = "background:none;border:none;color:#7a4d00;cursor:pointer;font-size:13px;flex:0 0 auto";
  close.addEventListener("click", () => bar.remove());
  bar.appendChild(close);
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
  busyShow,
  busyHide,
  busyDetail,
  streamPost,
  progressInit,
  progressApply,
  escapeHtml,
  fmt,
  kindLabel,
  emotionLabel,
  statusLabel,
  roleLabel,
  speakerLabel,
  openProject,
  loadChapter,
  refreshCastHint,
  showCastHintBar,
  /* 重新拉工程列表（新建工程后刷新顶栏候选，避免要刷新页面才出现） */
  refreshProjects: loadProjects,
  onProject(fn) {
    projectListeners.push(fn);
  },
  /* 跨页定位到「某章某行」：批量失败清单点进来时用。
   * 参数：chapterId 章节 ID；lineId 台词行 ID。
   * 返回：Promise，无。
   */
  async openLine(chapterId, lineId) {
    if (chapterId && chapterId !== state.chapter) await applyChapter(chapterId);
    if (!state.rows.some((row) => row.line_id === lineId)) {
      // 失败行可能被状态筛选藏住了，定位前先清掉筛选
      if (state.filter.size) {
        state.filter.clear();
        renderBadges();
      }
      await loadLines();
    }
    await selectLine(lineId);
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
  // 在既有配音页上补「重录/版本/整本批量」入口，不改 index.html，避免与并行改动冲突
  $("thead").insertAdjacentHTML("beforeend", '<span class="c-act">操作</span>');
  injectVersionPanel();
  injectBatchPanel();
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
