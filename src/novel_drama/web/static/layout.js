/* 应用骨架交互：左右面板显隐、窄屏收起、底部栏折叠与高度拖拽、流程条、顶栏工程切换器。
 *
 * 只碰骨架，不碰任何页面业务：
 *   - 面板状态写 localStorage，刷新后保持；
 *   - 流程条（#flowbar）的 done / current / todo 三态由 tree.js 提供的数据快照推导，
 *     本文件不发任何请求，避免与页面取数重复；
 *   - 顶栏工程 popover 的开合、最近工程列表（GET /api/projects）也只在骨架层处理，
 *     「打开工程」仍复用 app.js 里 #btnOpen 的既有逻辑；
 *   - 所有按钮与容器都在 index.html 的 .app-shell 里。
 */

(function () {
  // 待确认删除的工程路径：非空时该行渲染成「移入废纸篓？」确认条
  let pendingDelete = "";
  const STAGE_ORDER = ["project", "outline", "chapter", "script", "voice", "media", "export"];
  const STAGE_LABEL = {
    project: "项目",
    outline: "大纲",
    chapter: "章节",
    script: "标注",
    voice: "配音",
    media: "媒体",
    export: "导出",
  };

  const KEY_TREE = "ndw.layout.tree";
  const KEY_BOTTOM = "ndw.layout.bottom";
  const KEY_INSPECTOR = "ndw.layout.inspector";
  const KEY_BOTTOM_H = "ndw.layout.bottomH";

  const NARROW_PX = 1100; // 窄屏阈值：小于该宽度自动收起左栏
  const BOTTOM_MIN = 90;
  const BOTTOM_MAX = 480;

  let inited = false;
  let narrow = false;

  /* 取元素。 */
  const el = (id) => document.getElementById(id);

  /* 读布尔型偏好。
   * 参数：key localStorage 键；fallback 未存过时的默认值。
   * 返回：布尔值。
   */
  function readFlag(key, fallback) {
    const raw = localStorage.getItem(key);
    if (raw === null) return fallback;
    return raw === "1";
  }

  /* 写布尔型偏好。
   * 参数：key 键名；value 布尔值。
   * 返回：无。
   */
  function writeFlag(key, value) {
    localStorage.setItem(key, value ? "1" : "0");
  }

  /* 设置面板折叠态并同步按钮字形。
   * 参数：panelId 面板元素 id；btnId 切换按钮 id；collapsed 是否收起；
   *       closedGlyph / openGlyph 收起时与展开时按钮上的字符。
   * 返回：无。
   */
  function setPanel(panelId, btnId, collapsed, closedGlyph, openGlyph) {
    const panel = el(panelId);
    const btn = el(btnId);
    if (!panel) return;
    panel.dataset.collapsed = collapsed ? "true" : "false";
    if (btn) {
      btn.textContent = collapsed ? closedGlyph : openGlyph;
      btn.setAttribute("aria-expanded", collapsed ? "false" : "true");
    }
  }

  /* 按窗口宽度与本地偏好同步左栏：窄屏一律收起（不改写已保存的偏好）。
   * 参数：无。
   * 返回：无。
   */
  function applyTreePreference() {
    narrow = window.innerWidth < NARROW_PX;
    const saved = readFlag(KEY_TREE, false);
    setPanel("treePanel", "btnToggleTree", narrow || saved, "»", "«");
  }

  /* 设置右栏折叠态（不落盘，供 inspector.js 在配音视图自动展开）。
   * 参数：collapsed 是否收起。
   * 返回：无。
   */
  function setInspectorCollapsed(collapsed) {
    setPanel("inspectorPanel", "btnToggleInspector", collapsed, "«", "»");
  }

  /* 绑定三个面板的折叠按钮。
   * 参数：无。
   * 返回：无。
   */
  function bindPanelToggles() {
    const treeBtn = el("btnToggleTree");
    if (treeBtn) {
      treeBtn.addEventListener("click", () => {
        const panel = el("treePanel");
        const next = panel.dataset.collapsed !== "true";
        setPanel("treePanel", "btnToggleTree", next, "»", "«");
        if (!narrow) writeFlag(KEY_TREE, next); // 窄屏下的强制收起不落盘
      });
    }

    const bottomBtn = el("btnToggleBottom");
    if (bottomBtn) {
      bottomBtn.addEventListener("click", () => {
        const panel = el("bottomBar");
        const next = panel.dataset.collapsed !== "true";
        setPanel("bottomBar", "btnToggleBottom", next, "▴", "▾");
        writeFlag(KEY_BOTTOM, next);
        // 时间线据此决定是否挂音频源（折叠态不加载，省流量）
        if (window.NWTimeline) NWTimeline.setExpanded(!next);
      });
    }

    const inspectorBtn = el("btnToggleInspector");
    if (inspectorBtn) {
      inspectorBtn.addEventListener("click", () => {
        const panel = el("inspectorPanel");
        const next = panel.dataset.collapsed !== "true";
        setInspectorCollapsed(next);
        writeFlag(KEY_INSPECTOR, next);
      });
    }
  }

  /* 恢复底部栏高度偏好并挂上分隔条拖拽。
   * 参数：无。
   * 返回：无。
   */
  function initBottomResize() {
    const bar = el("bottomBar");
    const splitter = el("bottomSplitter");
    if (!bar || !splitter) return;

    const savedHeight = Number(localStorage.getItem(KEY_BOTTOM_H) || 0);
    if (savedHeight >= BOTTOM_MIN && savedHeight <= BOTTOM_MAX) bar.style.height = `${savedHeight}px`;

    let startY = 0;
    let startHeight = 0;

    /* 拖拽移动：向上拖变高，向下拖变矮，高度限制在 [BOTTOM_MIN, BOTTOM_MAX]。 */
    const onMove = (event) => {
      const next = Math.min(BOTTOM_MAX, Math.max(BOTTOM_MIN, startHeight + (startY - event.clientY)));
      bar.style.height = `${next}px`;
    };

    /* 松手：落盘高度并解绑全局监听。 */
    const onUp = () => {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
      localStorage.setItem(KEY_BOTTOM_H, String(Math.round(bar.getBoundingClientRect().height)));
    };

    splitter.addEventListener("mousedown", (event) => {
      event.preventDefault();
      // 收起状态下拖拽视为展开
      if (bar.dataset.collapsed === "true") {
        setPanel("bottomBar", "btnToggleBottom", false, "‹", "›");
        writeFlag(KEY_BOTTOM, false);
        if (window.NWTimeline) NWTimeline.setExpanded(true);
      }
      startY = event.clientY;
      startHeight = bar.getBoundingClientRect().height;
      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp);
    });
  }

  /* 当前 hash 对应的阶段名（与 core.js 的 VIEWS 保持一致，空 hash 回落到 voice）。
   * 参数：无。
   * 返回：阶段名字符串。
   */
  function currentStage() {
    const raw = (location.hash || "").replace(/^#\/?/, "").trim();
    return STAGE_ORDER.includes(raw) ? raw : "voice";
  }

  /* 当前 hash 是否停在「设置」页。
   * 设置页不属于 7 个流程阶段，流程条据此跳过三态重算，避免把某个阶段误标成 current。
   * 参数：无。
   * 返回：布尔值。
   */
  function isSettingsView() {
    return (location.hash || "").replace(/^#\/?/, "").trim() === "settings";
  }

  /* 从 tree.js 的快照推导 7 个阶段是否完成。
   * 参数：无。
   * 返回：{ 阶段名: 布尔值 }；数据缺失时对应阶段按未完成处理。
   */
  function stageDone() {
    const snap = window.NWTree && window.NWTree.snapshot ? window.NWTree.snapshot() : null;
    const state = (window.NW && window.NW.state) || {};
    const chapterId = state.chapter || "";
    const chapter = snap ? (snap.chapters || []).find((item) => item.id === chapterId) : null;
    const stats = (snap && snap.stats && snap.stats[chapterId]) || {};
    const counts = stats.lines || {};
    const lineTotal = Object.values(counts).reduce((sum, value) => sum + (Number(value) || 0), 0);
    const media = stats.media || { total: 0, ready: 0 };

    return {
      project: Boolean(state.path),
      outline: Boolean(snap && (snap.acts || []).length),
      chapter: Boolean(chapter && Number(chapter.word_count) > 0),
      script: Boolean(stats.script),
      voice: lineTotal > 0 && (Number(counts.ready) || 0) === lineTotal,
      media: Number(media.total) > 0 && Number(media.ready) >= Number(media.total),
      export: Boolean(snap && snap.renderedIds && snap.renderedIds.has(chapterId)),
    };
  }

  /* 按当前 hash 与完成情况重画流程条三态，并更新「下一步」按钮。
   * 参数：无。
   * 返回：无。
   */
  function renderFlow() {
    const done = stageDone();
    const view = currentStage();
    const nodes = document.querySelectorAll("#flowbar .stage");
    // 设置页不是流程阶段：保留流程条现有三态，不改写任何阶段
    if (!isSettingsView()) {
      nodes.forEach((node) => {
        const stage = node.dataset.stage;
        if (done[stage]) node.dataset.state = "done";
        else if (stage === view) node.dataset.state = "current";
        else node.dataset.state = "todo";
      });
    }

    const nextStage = STAGE_ORDER.find((stage) => !done[stage]);
    const nextBtn = el("flowNext");
    if (nextBtn) {
      nextBtn.textContent = nextStage ? `下一步：${STAGE_LABEL[nextStage]}` : "全流程已完成";
      nextBtn.disabled = !nextStage;
      nextBtn.dataset.stage = nextStage || "";
    }

    const tip = el("flowTip");
    if (tip) {
      const state = (window.NW && window.NW.state) || {};
      const snap = window.NWTree && window.NWTree.snapshot ? window.NWTree.snapshot() : null;
      const chapter = snap ? (snap.chapters || []).find((item) => item.id === state.chapter) : null;
      if (!state.path) tip.textContent = "先打开工程";
      else if (!chapter) tip.textContent = "本章还没建：先在大纲「落地成章节」";
      else tip.textContent = `${chapter.index}. ${chapter.title}`;
    }

    renderBottom();
  }

  /* 底部栏「当前上下文」：与顶栏下拉、左树、中区同源（都读 NW.state + 左树快照），
   * 任一处切章后这里跟着更新，作为四处状态同步的第四处。
   * 参数：无。
   * 返回：无。
   */
  function renderBottom() {
    const box = el("bottomInfo");
    if (!box) return;
    const state = (window.NW && window.NW.state) || {};
    if (!state.path) {
      box.innerHTML = "<span class='muted'>当前上下文：先打开工程</span>";
      return;
    }
    const snap = window.NWTree && window.NWTree.snapshot ? window.NWTree.snapshot() : null;
    const chapter = snap ? (snap.chapters || []).find((item) => item.id === state.chapter) : null;
    const stats = (snap && snap.stats && snap.stats[state.chapter]) || {};
    const counts = stats.lines || {};
    const lineTotal = Object.values(counts).reduce((sum, value) => sum + (Number(value) || 0), 0);
    const media = stats.media || { total: 0, ready: 0 };
    const scenes = (snap && snap.scenes && snap.scenes[state.chapter]) || [];
    const scene = scenes.find((item) => item.id === (snap && snap.scene));

    box.innerHTML = [
      `<span>工程 <b>${esc(shortPath(state.path))}</b></span>`,
      chapter
        ? `<span>章节 <b>${esc(chapter.index)}. ${esc(chapter.title || "")}</b></span>`
        : "<span class='muted'>未选章节</span>",
      scene ? `<span>场景 <b>${esc(scene.title || scene.location || "")}</b></span>` : "",
      `<span>正文 <b>${esc(chapter ? chapter.word_count || 0 : 0)}</b> 字</span>`,
      `<span>台词 <b>${lineTotal}</b> 行（已配音 ${Number(counts.ready) || 0}）</span>`,
      `<span>媒体 <b>${Number(media.ready) || 0}/${Number(media.total) || 0}</b></span>`,
      `<span>标注 ${stats.script ? "已生成" : "未生成"}</span>`,
    ]
      .filter(Boolean)
      .join("");
  }

  /* 工程路径只显示末尾两段，避免底部栏被长路径挤爆。
   * 参数：path 绝对路径。
   * 返回：缩写后的字符串。
   */
  function shortPath(path) {
    const parts = String(path || "").split("/").filter(Boolean);
    return parts.length <= 2 ? String(path || "") : `…/${parts.slice(-2).join("/")}`;
  }

  /* 绑定顶栏「模型设置」按钮：只切 hash，页面渲染交给 page-settings.js。
   * 参数：无。
   * 返回：无。
   */
  function bindSettingsEntry() {
    const btn = el("btnSettings");
    if (!btn) return;
    btn.addEventListener("click", () => {
      location.hash = "#/settings";
    });
  }

  /* 绑定流程条：点阶段直接换 hash，点「下一步」跳到第一个未完成阶段。
   * 参数：无。
   * 返回：无。
   */
  function bindFlow() {
    const bar = el("flowbar");
    if (bar) {
      bar.addEventListener("click", (event) => {
        const stage = event.target.closest(".stage");
        if (!stage) return;
        const done = stageDone();
        if (window.NWInspector) {
          // 右栏顺带给出这个阶段的完成情况（只读信息，不拦导航）
          NWInspector.show("stage", { stage: stage.dataset.stage, label: STAGE_LABEL[stage.dataset.stage], done: done[stage.dataset.stage] });
        }
        location.hash = `#/${stage.dataset.stage}`;
      });
    }

    const next = el("flowNext");
    if (next) {
      next.addEventListener("click", () => {
        const stage = next.dataset.stage;
        if (!stage) return;
        location.hash = `#/${stage}`;
      });
    }

    window.addEventListener("hashchange", renderFlow);
  }

  /* ---------------- 顶栏：工程切换器 popover / 最近工程 / 章节置灰 ---------------- */

  /* 取路径最后一段作为工程显示名。
   * 参数：path 绝对路径。
   * 返回：末段字符串；空值返回空串。
   */
  function baseName(path) {
    const parts = String(path || "").split("/").filter(Boolean);
    return parts.length ? parts[parts.length - 1] : "";
  }

  /* 同步顶栏：工程切换器文案（无工程显示「选择工程」）与章节下拉的可用态。
   * 参数：无。
   * 返回：无。
   */
  function renderProjectSwitch() {
    const state = (window.NW && window.NW.state) || {};
    const path = state.path || "";
    const name = el("projectName");
    const btn = el("projectSwitch");
    if (name) name.textContent = path ? baseName(path) : "选择工程";
    if (btn) btn.title = path ? `切换工程（${path}）` : "选择工程";
    const select = el("chapterSelect");
    if (select) select.disabled = !path;
  }

  /* 开合工程 popover。
   * 参数：open 为 true 展开、false 收起、省略时按当前状态反转。
   * 返回：无。
   */
  function toggleProjectMenu(open) {
    const menu = el("projectMenu");
    const btn = el("projectSwitch");
    if (!menu) return;
    const next = open === undefined ? menu.hidden : Boolean(open);
    menu.hidden = !next;
    if (btn) btn.setAttribute("aria-expanded", next ? "true" : "false");
    // 每次展开都拉一次，保证「最近工程」与后端索引同序
    if (next) loadRecentProjects();
  }

  /* 渲染「最近工程」：数据来自 GET /api/projects（[{path, title, last_opened_at}]，按最近打开倒序）。
   * 参数：无。
   * 返回：Promise，无；取数失败时保留空态，不打扰用户。
   */
  async function loadRecentProjects() {
    const box = el("projectRecent");
    if (!box) return;
    let items = [];
    try {
      if (window.NW && typeof NW.api === "function") items = await NW.api("/api/projects");
      else return;
    } catch (error) {
      items = [];
    }
    if (!Array.isArray(items) || !items.length) {
      box.innerHTML = "<span class='recent-empty'>还没有最近工程</span>";
      return;
    }
    box.innerHTML = items
      .map((item) => {
        const path = String(item.path || "");
        const title = esc(item.title || baseName(path) || path);
        // 已点过垃圾桶的行：原地换成确认条，避免误删
        if (pendingDelete === path) {
          return `<div class="recent-row recent-row-confirm">
            <span class="recent-confirm-text">移入废纸篓？</span>
            <button class="btn small danger" type="button" data-act="delete-ok" data-path="${esc(path)}">确认</button>
            <button class="btn small" type="button" data-act="delete-cancel">取消</button>
          </div>`;
        }
        return `<div class="recent-row">
          <button class="recent-item" type="button" data-path="${esc(path)}" title="${esc(path)}">
            <span class="recent-title">${title}</span>
            <span class="recent-path">${esc(path)}</span>
          </button>
          <button class="recent-act" type="button" data-act="reveal" data-path="${esc(path)}" title="在访达中打开">📂</button>
          <button class="recent-act" type="button" data-act="delete" data-path="${esc(path)}" title="删除工程（移入废纸篓）">🗑</button>
        </div>`;
      })
      .join("");
  }

  /* 把工程目录移入废纸篓；删的是当前工程时清空当前状态并回到项目页。
   * 参数：path 工程目录。
   * 返回：无。
   */
  async function deleteProject(path) {
    if (!path) return;
    try {
      await NW.api("/api/project/delete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path, confirm: true }),
      });
      pendingDelete = null;
      NW.toast("已移入废纸篓", true);
      const current = (NW.state && NW.state.path) || "";
      if (current && current === path) {
        // 当前打开的工程已被删除，清掉残留状态后回到项目页
        NW.state.path = "";
        NW.state.chapterId = "";
        toggleProjectMenu(false);
        location.hash = "#/project";
        location.reload();
        return;
      }
      loadRecentProjects();
    } catch (error) {
      pendingDelete = null;
      loadRecentProjects();
      NW.toast(error.message || "删除失败");
    }
  }

  /* 请求后端在访达里打开工程目录（浏览器本身无法唤起访达，必须走接口）。
   * 参数：path 工程目录。
   * 返回：无。
   */
  async function revealProject(path) {
    if (!path) return;
    try {
      await NW.api("/api/project/reveal", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path }),
      });
    } catch (error) {
      NW.toast(error.message || "打开失败");
    }
  }

  /* 点击某条最近工程：回填路径后复用 #btnOpen 的既有打开逻辑（app.js 未改动）。
   * 参数：path 工程目录。
   * 返回：无。
   */
  function openRecent(path) {
    const input = el("projectPath");
    const open = el("btnOpen");
    if (!input || !open) return;
    input.value = path;
    open.click();
  }

  /* 绑定工程 popover：按钮开合、外部点击关闭、Esc 关闭、打开工程后收起、最近工程点击。
   * 参数：无。
   * 返回：无。
   */
  function bindProjectMenu() {
    const btn = el("projectSwitch");
    const menu = el("projectMenu");
    if (!btn || !menu) return;

    btn.addEventListener("click", () => toggleProjectMenu());

    // 点菜单内部不关闭；点 .project-switch 之外一律收起
    document.addEventListener("click", (event) => {
      if (menu.hidden) return;
      if (event.target.closest(".project-switch")) return;
      toggleProjectMenu(false);
    });

    document.addEventListener("keydown", (event) => {
      if (event.key !== "Escape" || menu.hidden) return;
      toggleProjectMenu(false);
      btn.focus();
    });

    // 打开工程后自动收起（openProject 本身仍在 app.js，这里只收菜单）
    const open = el("btnOpen");
    if (open) open.addEventListener("click", () => toggleProjectMenu(false));

    menu.addEventListener("click", (event) => {
      const hit = event.target.closest("[data-act]");
      if (hit) {
        event.stopPropagation();
        const act = hit.dataset.act;
        const path = hit.dataset.path || "";
        if (act === "reveal") revealProject(path);
        else if (act === "delete") {
          // 第一次点垃圾桶：原地换成确认条，不直接删
          pendingDelete = path;
          loadRecentProjects();
        } else if (act === "delete-ok") deleteProject(path);
        else if (act === "delete-cancel") {
          pendingDelete = "";
          loadRecentProjects();
        }
        return;
      }
      const recent = event.target.closest(".recent-item");
      if (!recent) return;
      toggleProjectMenu(false);
      openRecent(recent.dataset.path || "");
    });

    const create = el("btnNewProject");
    if (create) {
      create.addEventListener("click", () => {
        toggleProjectMenu(false);
        gotoNewProjectForm();
      });
    }
  }

  /* 跳到项目页的「新建工程」表单：切路由 → 滚到卡片 → 聚焦书名著录 → 高亮一次。
   * 参数：无。
   * 返回：无。
   */
  function gotoNewProjectForm() {
    if (location.hash !== "#/project") location.hash = "#/project";
    // 等一帧让 #view-project 显示出来，隐藏元素无法滚动与聚焦
    setTimeout(() => {
      const card = el("npDir") ? el("npDir").closest(".card") : null;
      const target = card || el("npDir");
      if (target && typeof target.scrollIntoView === "function") {
        target.scrollIntoView({ behavior: "smooth", block: "center" });
      }
      if (card) {
        card.classList.add("card-flash");
        setTimeout(() => card.classList.remove("card-flash"), 1200);
      }
      const title = el("npTitle");
      if (title) title.focus({ preventScroll: true });
    }, 60);
  }

  /* 让 #hint 的 title 始终是完整文本（app.js 只写 textContent，这里旁路同步）。
   * 参数：无。
   * 返回：无。
   */
  function watchHint() {
    const hint = el("hint");
    if (!hint) return;
    const sync = () => {
      hint.title = hint.textContent.trim();
    };
    sync();
    new MutationObserver(sync).observe(hint, { childList: true, characterData: true, subtree: true });
  }

  /* 初始化骨架：偏好在 append 阶段读取一次，避免首帧闪烁由 CSS 兜底。
   * 参数：无。
   * 返回：无。
   */
  function init() {
    if (inited) return;
    inited = true;

    applyTreePreference();
    setPanel("bottomBar", "btnToggleBottom", readFlag(KEY_BOTTOM, false), "▴", "▾");
    setPanel("inspectorPanel", "btnToggleInspector", readFlag(KEY_INSPECTOR, true), "«", "»");

    bindPanelToggles();
    initBottomResize();
    bindFlow();
    bindSettingsEntry();
    bindProjectMenu();
    watchHint();
    renderProjectSwitch();
    loadRecentProjects();
    renderFlow();

    window.addEventListener("resize", applyTreePreference);
    if (window.NW && NW.onChapter) NW.onChapter(() => renderFlow());
    if (window.NW && NW.onProject) NW.onProject(() => renderProjectSwitch());
    if (window.NWTree && NWTree.onUpdate) NWTree.onUpdate(() => renderFlow());
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();

  window.NWLayout = { init, renderFlow, stageDone, currentStage, setInspectorCollapsed, renderBottom };
})();
