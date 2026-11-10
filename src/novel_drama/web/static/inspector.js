/* 右栏 Inspector：统一承接当前上下文（章节 / 台词行 / 媒体条目 / 阶段）。
 *
 * 两条形态：
 *   1. `#/voice` 台词校对视图：右栏就是该视图自己的编辑表单。初始化时把 `#inspector`
 *      整个节点 appendChild 搬到 `#inspectorPanel` 里（纯 DOM 搬家，所有 id、事件绑定、
 *      diff / 回滚 / 保存行为原样保留），进 voice 视图时自动展开，离开时保持用户手动选择。
 *   2. 其余视图：右栏渲染只读信息 + 动作按钮。动作全部复用既有接口（同一个
 *      `/api/ui/media/run`、同一条配音页选中逻辑），不新增后端。
 *
 * 事件来源：core.js 的 `view`、app.js 的 `chapter` / `line`、page-chapter.js 的
 * `chapterData`，以及页面里显式调用的 `NWInspector.show(kind, payload)`。
 */

(function () {
  let host = null;
  let readOnly = null;
  let voiceNode = null;
  let current = { kind: "", payload: null };
  let chapterData = null; // 章节页加载到的详情，用来补充字/场景/台词统计

  const el = (id) => document.getElementById(id);

  /* 一行只读信息。参数：key 字段名；value 已转义或可信的 HTML。返回：HTML 字符串。 */
  function row(key, value) {
    return `<div class="ins-row"><span class="ins-key">${esc(key)}</span><span class="ins-val">${value}</span></div>`;
  }

  /* 动作按钮。参数：act 动作名；label 文案；extra 额外属性；primary 是否主按钮。返回：HTML。 */
  function action(act, label, primary) {
    return `<button class="btn small${primary ? " primary" : ""}" data-act="${act}">${esc(label)}</button>`;
  }

  /* 当前章节的汇总数据：左树快照 + 章节详情缓存。
   * 参数：无。
   * 返回：{ id, chapter, counts, media, script, scene } 或 null。
   */
  function chapterSummary() {
    const state = (window.NW && NW.state) || {};
    const id = state.chapter || "";
    if (!id) return null;
    const snap = window.NWTree && window.NWTree.snapshot ? window.NWTree.snapshot() : null;
    const chapter = (snap && (snap.chapters || []).find((item) => item.id === id)) || null;
    const stats = (snap && snap.stats && snap.stats[id]) || {};
    const counts = stats.lines || {};
    return {
      id,
      chapter: chapterData && chapterData.id === id ? chapterData : chapter,
      counts,
      media: stats.media || { total: 0, ready: 0 },
      script: Boolean(stats.script),
      scene: (snap && snap.scenes && snap.scenes[id] || []).find((item) => item.id === (snap && snap.scene)) || null,
    };
  }

  /* 章节卡片。参数：payload 任意（忽略，按当前章节取）。返回：HTML 字符串。 */
  function chapterHtml() {
    const summary = chapterSummary();
    if (!summary) return "<p class='muted'>先打开工程并选择章节。</p>";
    const chapter = summary.chapter || {};
    const total = Object.values(summary.counts).reduce((sum, value) => sum + (Number(value) || 0), 0);
    const scenes = (chapter.scenes || []).length || chapter.scene_count || 0;
    return (
      `<div class="ins-title"><span><b>${esc(chapter.index ?? "")}. ${esc(chapter.title || "")}</b></span>` +
      `<span class="kicker">${esc(chapter.status || "")}</span></div>` +
      row("章节 ID", `<code>${esc(summary.id)}</code>`) +
      row("字数", esc(chapter.word_count ?? 0)) +
      row("场景", String(scenes)) +
      row("台词", `${total} 行（已配音 ${Number(summary.counts.ready) || 0}）`) +
      row("媒体", `${Number(summary.media.ready) || 0}/${Number(summary.media.total) || 0}`) +
      row("标注", summary.script ? "已生成" : "未生成") +
      (summary.scene ? row("选中场景", esc(summary.scene.title || summary.scene.location || "")) : "") +
      `<div class="ins-actions">${action("goChapter", "查看正文", true)}${action("goVoice", "去配音校对")}</div>`
    );
  }

  /* 台词行卡片：兼容配音页的 row 与章节页的 line。
   * 参数：payload 行对象。返回：HTML 字符串。
   */
  function lineHtml(payload) {
    const line = payload || {};
    const id = line.line_id || line.id || "";
    if (!id) return "<p class='muted'>没有选中台词行。</p>";
    const tts = line.tts || {};
    const state = line.state || tts.status || "—";
    const duration = line.duration_ms ?? tts.duration_ms;
    const bits = [`[${esc(line.emotion || "neutral")}] ×${esc(line.emotion_intensity ?? "?")}`, `语速 ${esc(line.speed ?? "?")}`];
    return (
      `<div class="ins-title"><span><b>${esc(NW.kindLabel(line.kind))}</b></span><span class="kicker">${esc(state)}</span></div>` +
      row("行 ID", `<code>${esc(id)}</code>`) +
      row("说话人", esc(line.speaker || line.speaker_id || "—")) +
      row("表演", esc(bits.join(" · "))) +
      row("停顿", `${esc(line.pause_before_ms ?? 0)} / ${esc(line.pause_after_ms ?? 0)} ms`) +
      row("时长", esc(msText(duration))) +
      (line.delivery_note ? row("备注", esc(line.delivery_note)) : "") +
      row("文本", esc(line.text || "")) +
      (line.para !== undefined ? row("正文段落", `第 ${Number(line.para) + 1} 段`) : "") +
      `<div class="ins-actions">${action("focusLine", "在配音页校对", true)}</div>`
    );
  }

  /* 媒体条目卡片：提示词 / 时长 / 状态 / 相对路径 + 内联播放器 + 重跑。
   * 参数：payload { kind, row }。返回：HTML 字符串。
   */
  function mediaHtml(payload) {
    const kind = (payload && payload.kind) || "";
    const item = (payload && payload.row) || {};
    if (!item.id) return "<p class='muted'>没有选中媒体条目。</p>";
    const rel = item.asset_id || "";
    const url = rel ? uiFileUrl(rel) : ""; // 与媒体页、时间线共用同一套只读文件 URL
    const isVideo = kind === "video" || /\.(mp4|webm|mov)$/i.test(rel);
    const isImage = /\.(png|jpe?g|webp)$/i.test(rel);
    let player = "<div class='ins-note'>还没有产物文件。</div>";
    if (rel && isVideo) {
      player = `<div class="ins-player"><video controls preload="metadata" data-player="${esc(rel)}" src="${esc(url)}"></video><div class="ins-note" data-player-note>${esc(rel)}</div></div>`;
    } else if (rel && !isImage) {
      player = `<div class="ins-player"><audio controls preload="metadata" data-player="${esc(rel)}" src="${esc(url)}"></audio><div class="ins-note" data-player-note>${esc(rel)}</div></div>`;
    } else if (rel) {
      player = `<div class="ins-note">配图文件：${esc(rel)}</div>`;
    }
    return (
      `<div class="ins-title"><span><b>${esc(kind)} #${esc(item.index ?? "")}</b></span>` +
      `<span class="kicker">${esc(item.status || "")}</span></div>` +
      row("类型", esc(kind === "sfx" ? item.kind || "sfx" : item.mode || kind)) +
      row("提示词", esc(item.prompt || "—")) +
      row("时长", esc(msText(item.duration_ms))) +
      row("状态", esc(item.status || "—")) +
      (item.error ? row("错误", `<span class="err">${esc(typeof item.error === "string" ? item.error : JSON.stringify(item.error))}</span>`) : "") +
      row("文件", `<code>${esc(rel || "—")}</code>`) +
      player +
      `<div class="ins-actions">${action("rerunMedia", "重跑此项", true)}${action("goMedia", "去媒体页")}</div>`
    );
  }

  /* 阶段卡片。参数：payload { stage, label, done }。返回：HTML 字符串。 */
  function stageHtml(payload) {
    const info = payload || {};
    const chapter = chapterSummary();
    return (
      `<div class="ins-title"><span><b>${esc(info.label || info.stage || "")}</b></span>` +
      `<span class="kicker">${info.done ? "已完成" : "未完成"}</span></div>` +
      row("阶段键", `<code>${esc(info.stage || "")}</code>`) +
      row("当前章节", chapter ? esc(`${chapter.chapter ? chapter.chapter.index + ". " + (chapter.chapter.title || "") : chapter.id}`) : "未选") +
      `<div class="ins-actions">${action("goStage", "进入该阶段", true)}</div>`
    );
  }

  /* 按 kind 生成只读内容 HTML。参数：kind 类型；payload 负载。返回：HTML 字符串。 */
  function htmlFor(kind, payload) {
    if (kind === "line") return lineHtml(payload);
    if (kind === "media") return mediaHtml(payload);
    if (kind === "stage") return stageHtml(payload);
    return chapterHtml();
  }

  /* 按当前视图切换「编辑表单 / 只读信息」，并在配音视图自动展开右栏。
   * 参数：无。
   * 返回：无。
   */
  function applyVisibility() {
    if (!readOnly || !voiceNode) return;
    const isVoice = currentView() === "voice";
    voiceNode.hidden = !isVoice;
    readOnly.hidden = isVoice;
    if (isVoice && window.NWLayout && NWLayout.setInspectorCollapsed) NWLayout.setInspectorCollapsed(false);
  }

  /* 给内联播放器加一次加载失败提示（相对路径原样展示，不伪造路径）。
   * 参数：无。返回：无。
   */
  function bindPlayers() {
    if (!readOnly) return;
    readOnly.querySelectorAll("[data-player]").forEach((node) => {
      node.addEventListener("error", () => {
        const note = node.parentElement && node.parentElement.querySelector("[data-player-note]");
        if (note) note.textContent = `浏览器没能加载 ${node.dataset.player}；工程里的相对路径就是这一条。`;
      });
    });
  }

  /* 重画只读区。参数：无。返回：无。 */
  function render() {
    if (!readOnly) return;
    if (!current.kind) {
      readOnly.innerHTML = `<p class="muted">左栏选章节、页面里点台词行或媒体条目，这里显示详情。</p>`;
    } else {
      readOnly.innerHTML = htmlFor(current.kind, current.payload);
    }
    applyVisibility();
    bindPlayers();
  }

  /* 统一入口：展示某类上下文。
   * 参数：kind chapter | line | media | stage；payload 该类型的负载。
   * 返回：无。
   */
  function show(kind, payload) {
    current = { kind: kind || "", payload: payload || null };
    render();
  }

  /* 执行右栏动作（全部复用既有接口 / 既有选中逻辑）。
   * 参数：act 动作名。返回：Promise，无。
   */
  async function act(name) {
    if (name === "goChapter") {
      location.hash = "#/chapter";
      return;
    }
    if (name === "goVoice") {
      location.hash = "#/voice";
      return;
    }
    if (name === "goMedia") {
      location.hash = "#/media";
      return;
    }
    if (name === "goStage") {
      const stage = (current.payload && current.payload.stage) || "voice";
      location.hash = `#/${stage}`;
      return;
    }
    if (name === "focusLine") {
      const payload = current.payload || {};
      NW.emit("focusLine", payload.line_id || payload.id || "");
      return;
    }
    if (name === "rerunMedia") {
      await rerunMedia();
    }
  }

  /* 重跑当前选中的媒体条目：走媒体页同一个 `/api/ui/media/run`。
   * 参数：无。返回：Promise，无。
   */
  async function rerunMedia() {
    const payload = current.payload || {};
    const kind = payload.kind;
    if (!kind || !NW.state.path || !NW.state.chapter) return;
    try {
      const result = await NW.api("/api/ui/media/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: NW.state.path, chapter: NW.state.chapter, kind, force: true }),
      });
      NW.toast(`${kind}：就绪 ${result.ready} / 跳过 ${result.skipped} / 失败 ${result.failed}`, result.failed === 0);
      // 直接等媒体页刷完清单，再用最新条目重画右栏（状态字段会变）
      if (window.Pages && Pages.media && Pages.media.load) await Pages.media.load();
      refreshMedia();
    } catch (error) {
      NW.toast(`重跑失败（退出码 ${error.code}）：${error.message}`);
    }
  }

  /* 媒体列表刷新后，用最新的条目重画右栏（找不到就保持原样）。
   * 参数：无。返回：无。
   */
  function refreshMedia() {
    if (current.kind !== "media") return;
    const payload = current.payload || {};
    const id = payload.row && payload.row.id;
    const found = window.Pages && Pages.media && Pages.media.find ? Pages.media.find(payload.kind, id) : null;
    if (found) show("media", found);
  }

  /* 初始化：建只读容器、把配音页的 #inspector 搬进右栏、绑事件与订阅。
   * 参数：无。返回：无。
   */
  function init() {
    host = el("inspectorHost");
    if (!host) return;

    const empty = el("insEmpty");
    if (empty) empty.remove();

    readOnly = document.createElement("div");
    readOnly.className = "ins-sec";
    readOnly.id = "insReadOnly";
    host.appendChild(readOnly);

    // DOM 搬家：保留原节点、原 id、原事件；只在非 voice 视图隐藏
    voiceNode = el("inspector");
    if (voiceNode) {
      host.appendChild(voiceNode);
      voiceNode.hidden = true;
    }

    readOnly.addEventListener("click", (event) => {
      const button = event.target.closest("[data-act]");
      if (button) act(button.dataset.act);
    });

    if (window.NW) {
      NW.on("view", applyVisibility);
      // 章节换了两条来源：显式 emit('chapter') 与 openProject 后的 notifyChapter，两者都要跟上
      const onChapterChange = () => {
        if (current.kind === "chapter" || !current.kind) show("chapter");
        else render(); // 其它类型的上下文跟着换章重画（数据来源是同一条快照）
      };
      NW.on("chapter", onChapterChange);
      if (NW.onChapter) NW.onChapter(onChapterChange);
      NW.on("chapterData", (detail) => {
        chapterData = detail || null;
        if (current.kind === "chapter" || !current.kind) show("chapter");
      });
      NW.on("line", (payload) => show("line", payload));
      NW.on("scene", () => {
        if (current.kind === "chapter") show("chapter");
      });
    }

    show("chapter");
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();

  window.NWInspector = { show, render, refreshMedia };
})();
