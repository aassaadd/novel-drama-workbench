/* 左栏章节树：书 → 幕 → 章 → 场景。
 *
 * 数据全部来自已有只读接口，不新增后端：
 *   GET /api/project/info   书名 + 章节列表（章节状态的主来源）
 *   GET /api/ui/outline     幕 / 章，用于分幕
 *   GET /api/ui/chapters    章节列表（含字数、场景数）
 *   GET /api/ui/exports     renders/ 产物 → rendered 状态
 *   GET /api/ui/lines       每章台词计数 → marked / voiced 状态
 *   GET /api/ui/chapter     场景层（点开某章时才拉）
 *   GET /api/ui/script + /api/ui/media  当前章的标注 / 媒体进度（给流程条用）
 *
 * 状态灯优先级：rendered > voiced > marked > writing > idle，数据拿不到时降级 unknown。
 * 切章复用 app.js 里既有逻辑：改 #chapterSelect 的值并派发 change 事件。
 */

(function () {
  const ENRICH_LIMIT = 24; // 台词状态回填的章节上限，超出部分只按字数判断，避免一次打太多请求
  const CONCURRENCY = 3;

  const tree = {
    inited: false,
    loading: false,
    token: 0,          // 每次整体刷新自增，用来丢弃过期请求的回填
    path: "",
    title: "",
    acts: [],
    chapters: [],
    renderedIds: new Set(),
    stats: {},         // chapterId -> { lines, script, media, pendingExtras }
    scenes: {},        // chapterId -> [{ id, title, location }]
    pendingScenes: new Set(),
    expanded: new Set(["book"]),
    scene: "",
  };

  const listeners = [];
  const el = (id) => document.getElementById(id);

  /* 取某章的统计数据容器，没有就建一个。
   * 参数：chapterId 章节 ID。
   * 返回：统计对象。
   */
  function ensureStats(chapterId) {
    if (!tree.stats[chapterId]) tree.stats[chapterId] = { lines: null, script: false, media: { total: 0, ready: 0 } };
    return tree.stats[chapterId];
  }

  /* 通知订阅者（流程条等）数据已变化。
   * 参数：无。
   * 返回：无。
   */
  function notify() {
    const snap = snapshot();
    listeners.forEach((fn) => {
      try {
        fn(snap);
      } catch (error) {
        // 单个订阅者出错不影响树本身
      }
    });
  }

  /* 对外快照：给流程条 / 右栏 / 底部栏推导当前上下文。
   * 参数：无。
   * 返回：{ path, title, acts, chapters, stats, scenes, scene, renderedIds }。
   */
  function snapshot() {
    return {
      path: tree.path,
      title: tree.title,
      acts: tree.acts,
      chapters: tree.chapters,
      stats: tree.stats,
      scenes: tree.scenes,
      scene: tree.scene,
      renderedIds: tree.renderedIds,
    };
  }

  /* 转发事件到 app.js 的事件总线（NW.emit），供后续阶段订阅。
   * 参数：name 事件名；payload 负载。
   * 返回：无。
   */
  function emit(name, payload) {
    if (window.NW && window.NW.emit) window.NW.emit(name, payload);
  }

  /* 当前章节 ID。 */
  function activeChapterId() {
    return (window.NW && window.NW.state && window.NW.state.chapter) || "";
  }

  /* 推导章节状态灯。
   * 参数：chapter 章节列表项（含 id / word_count）。
   * 返回：idle | writing | marked | voiced | rendered | unknown。
   */
  function chapterState(chapter) {
    if (tree.renderedIds.has(chapter.id)) return "rendered";
    const stats = tree.stats[chapter.id];
    if (stats && stats.lines) {
      const total = Object.values(stats.lines).reduce((sum, value) => sum + (Number(value) || 0), 0);
      if (total > 0) return (Number(stats.lines.ready) || 0) === total ? "voiced" : "marked";
    }
    const words = Number(chapter.word_count);
    if (Number.isFinite(words)) return words > 0 ? "writing" : "idle";
    return "unknown";
  }

  /* 章节行右侧的紧凑元信息：有台词显示行数，否则显示字数。
   * 参数：chapter 章节列表项。
   * 返回：字符串（可能为空）。
   */
  function statsMeta(chapter) {
    const stats = tree.stats[chapter.id];
    if (stats && stats.lines) {
      const total = Object.values(stats.lines).reduce((sum, value) => sum + (Number(value) || 0), 0);
      if (total > 0) return `${total} 行`;
    }
    const words = Number(chapter.word_count) || 0;
    return words > 0 ? `${words} 字` : "";
  }

  /* 按大纲把章节分到各幕下；大纲里没有的章节归入「未分幕」，没有大纲时只分一组。
   * 参数：无。
   * 返回：数组，元素为 { key, title, chapters }。
   */
  function buildGroups() {
    const byId = new Map(tree.chapters.map((item) => [item.id, item]));
    const used = new Set();
    const groups = [];

    (tree.acts || []).forEach((act, index) => {
      const chapters = (act.chapters || []).map((item) => byId.get(item.id)).filter(Boolean);
      chapters.forEach((item) => used.add(item.id));
      if (chapters.length) groups.push({ key: `act:${act.id || index}`, title: act.title || `第 ${index + 1} 幕`, chapters });
    });

    const rest = tree.chapters.filter((item) => !used.has(item.id));
    if (rest.length) groups.push({ key: "act:rest", title: groups.length ? "未分幕" : "全书", chapters: rest });
    return groups;
  }

  /* 生成一行树节点 HTML。
   * 参数：node 节点描述 { kind, key, level, label, caret, state, meta, chapter, scene, active }。
   * 返回：HTML 字符串。
   */
  function rowHtml(node) {
    const attrs = [
      `class="tree-row"`,
      `data-kind="${node.kind}"`,
      node.key ? `data-key="${esc(node.key)}"` : "",
      node.chapter ? `data-chapter="${esc(node.chapter)}"` : "",
      node.scene ? `data-scene="${esc(node.scene)}"` : "",
      node.active ? `data-active="true"` : "",
      `style="--level:${node.level || 0}"`,
    ].filter(Boolean).join(" ");
    const caret = `<span class="tree-caret">${node.caret || ""}</span>`;
    const dot = node.state ? `<span class="tree-dot" data-state="${node.state}"></span>` : "";
    const meta = node.meta ? `<span class="tree-meta">${esc(node.meta)}</span>` : "";
    return `<div ${attrs}>${caret}${dot}<span class="tree-label">${esc(node.label)}</span>${meta}</div>`;
  }

  /* 重画整棵树（章节数不多，整体重画即可；保留滚动位置）。
   * 参数：无。
   * 返回：无。
   */
  function renderTree() {
    const box = el("tree");
    if (!box) return;
    const scrollTop = box.scrollTop;

    if (!tree.path) {
      box.innerHTML = `<div class="tree-empty">先打开工程</div>`;
      return;
    }
    if (!tree.chapters.length) {
      box.innerHTML = `<div class="tree-empty">${tree.loading ? "读取中…" : "工程里还没有章节"}</div>`;
      return;
    }

    const activeId = activeChapterId();
    const parts = [
      rowHtml({
        kind: "book",
        key: "book",
        level: 0,
        label: tree.title || "未命名作品",
        caret: tree.expanded.has("book") ? "▾" : "▸",
        meta: `${tree.chapters.length} 章`,
      }),
    ];

    if (tree.expanded.has("book")) {
      buildGroups().forEach((group) => {
        parts.push(
          rowHtml({
            kind: "act",
            key: group.key,
            level: 1,
            label: group.title,
            caret: tree.expanded.has(group.key) ? "▾" : "▸",
            meta: `${group.chapters.length} 章`,
          })
        );
        if (!tree.expanded.has(group.key)) return;
        group.chapters.forEach((chapter) => {
          const key = `ch:${chapter.id}`;
          const open = tree.expanded.has(key);
          parts.push(
            rowHtml({
              kind: "chapter",
              key,
              level: 2,
              label: `${chapter.index}. ${chapter.title || ""}`,
              caret: open ? "▾" : "▸",
              state: chapterState(chapter),
              meta: statsMeta(chapter),
              chapter: chapter.id,
              active: chapter.id === activeId,
            })
          );
          if (!open) return;
          const scenes = tree.scenes[chapter.id];
          if (!scenes) {
            parts.push(`<div class="tree-empty" style="--level:3">读取场景…</div>`);
          } else if (!scenes.length) {
            parts.push(`<div class="tree-empty" style="--level:3">还没有场景</div>`);
          } else {
            scenes.forEach((scene, index) => {
              parts.push(
                rowHtml({
                  kind: "scene",
                  level: 3,
                  label: `${index + 1}. ${scene.title || scene.location || "场景"}`,
                  scene: scene.id,
                  active: scene.id === tree.scene,
                })
              );
            });
          }
        });
      });
    }

    box.innerHTML = parts.join("");
    box.scrollTop = scrollTop;
  }

  /* 拉某章的场景层（只拉一次）。
   * 参数：chapterId 章节 ID。
   * 返回：Promise，场景数组。
   */
  async function loadScenes(chapterId) {
    if (!chapterId || tree.scenes[chapterId] || tree.pendingScenes.has(chapterId)) return tree.scenes[chapterId] || [];
    tree.pendingScenes.add(chapterId);
    const token = tree.token;
    let scenes = [];
    try {
      const detail = await NW.api(`/api/ui/chapter?${NW.qs({ path: tree.path, chapter: chapterId })}`);
      scenes = (detail.scenes || []).map((scene) => ({ id: scene.id, title: scene.title, location: scene.location }));
      if (token === tree.token) tree.scenes[chapterId] = scenes;
    } catch (error) {
      if (token === tree.token) tree.scenes[chapterId] = [];
    } finally {
      tree.pendingScenes.delete(chapterId);
    }
    if (token === tree.token) renderTree();
    return scenes;
  }

  /* 拉某章的台词状态计数（只用于状态灯，不展示台词内容）。
   * 参数：chapterId 章节 ID。
   * 返回：Promise，counts 对象或 null。
   */
  async function loadLineStats(chapterId) {
    const token = tree.token;
    const stats = ensureStats(chapterId);
    try {
      const data = await NW.api(`/api/ui/lines?${NW.qs({ path: tree.path, chapter: chapterId })}`);
      if (token !== tree.token) return null;
      stats.lines = data.counts || {};
      return stats.lines;
    } catch (error) {
      if (token === tree.token) stats.lines = null; // 拿不到就按字数降级，不弹提示打扰用户
      return null;
    }
  }

  /* 拉某章的标注 / 媒体进度（给流程条判断「标注」「媒体」是否完成）。
   * 参数：chapterId 章节 ID。
   * 返回：Promise，无。
   */
  async function loadChapterExtras(chapterId) {
    if (!chapterId || !tree.path) return;
    const stats = ensureStats(chapterId);
    if (stats.pendingExtras) return;
    stats.pendingExtras = true;
    const token = tree.token;
    const [script, media] = await Promise.all([
      NW.api(`/api/ui/script?${NW.qs({ path: tree.path, chapter: chapterId })}`).catch(() => null),
      NW.api(`/api/ui/media?${NW.qs({ path: tree.path, chapter: chapterId })}`).catch(() => null),
    ]);
    if (token !== tree.token) return;
    stats.script = Boolean(script && ((script.markup || "").trim() || script.has_script));
    const rows = media ? [...(media.sfx || []), ...(media.image || []), ...(media.video || [])] : [];
    stats.media = { total: rows.length, ready: rows.filter((row) => row.status === "ready").length };
    stats.pendingExtras = false;
    notify();
  }

  /* 后台回填所有章节的台词状态：限流并发，超出上限的章节不拉。
   * 参数：token 刷新令牌，过期即停。
   * 返回：Promise，无。
   */
  async function enrichChapters(token) {
    const list = tree.chapters.slice(0, ENRICH_LIMIT);
    let cursor = 0;
    const worker = async () => {
      while (cursor < list.length && token === tree.token) {
        const chapter = list[cursor];
        cursor += 1;
        await loadLineStats(chapter.id);
        if (token === tree.token) renderTree();
      }
    };
    await Promise.all([worker(), worker(), worker()]);
    if (token === tree.token) notify();
  }

  /* 整体刷新：工程信息 / 大纲 / 章节 / 产物并行取回，再补台账。
   * 参数：无。
   * 返回：Promise，无。
   */
  async function refresh() {
    const path = (window.NW && window.NW.state.path) || "";
    if (!path) {
      tree.token += 1;
      tree.path = "";
      tree.title = "";
      tree.acts = [];
      tree.chapters = [];
      tree.renderedIds = new Set();
      tree.stats = {};
      tree.scenes = {};
      renderTree();
      notify();
      return;
    }
    if (tree.loading) return;

    const token = ++tree.token;
    tree.loading = true;
    tree.path = path;
    // 先清空再取数：避免取数期间切章回填的章节数据被这里覆盖
    tree.stats = {};
    tree.scenes = {};

    const [info, outline, chapters, exports] = await Promise.all([
      NW.api(`/api/project/info?${NW.qs({ path })}`).catch(() => null),
      NW.api(`/api/ui/outline?${NW.qs({ path })}`).catch(() => null),
      NW.api(`/api/ui/chapters?${NW.qs({ path })}`).catch(() => null),
      NW.api(`/api/ui/exports?${NW.qs({ path })}`).catch(() => null),
    ]);
    if (token !== tree.token) return;

    tree.title = (info && info.work && info.work.title) || path.split("/").pop() || "工程";
    tree.acts = (outline && outline.acts) || [];
    const list = (chapters && chapters.length ? chapters : (info && info.chapters) || []).slice();
    list.sort((a, b) => (Number(a.index) || 0) - (Number(b.index) || 0));
    tree.chapters = list;
    tree.renderedIds = new Set();
    ((exports && exports.files) || []).forEach((file) => {
      const rel = file.rel || "";
      if (!rel.startsWith("renders/")) return;
      const chapterId = rel.slice("renders/".length).split("/")[0].split(".")[0];
      if (chapterId) tree.renderedIds.add(chapterId);
    });
    tree.loading = false;
    // 默认展开书与各幕，并展开当前章节
    tree.expanded.add("book");
    buildGroups().forEach((group) => tree.expanded.add(group.key));
    const active = activeChapterId();
    if (active && list.some((item) => item.id === active)) tree.expanded.add(`ch:${active}`);

    renderTree();
    notify();

    if (active) {
      loadScenes(active);
      loadChapterExtras(active);
    }
    enrichChapters(token);
  }

  /* 当前章节变化：展开该章、补拉场景与标注/媒体进度。
   * 参数：无。
   * 返回：无。
   */
  function onChapterChanged() {
    const chapterId = activeChapterId();
    if (!chapterId || tree.path !== ((window.NW && window.NW.state.path) || "")) return;
    tree.scene = "";
    tree.expanded.add(`ch:${chapterId}`);
    renderTree();
    loadScenes(chapterId);
    loadChapterExtras(chapterId);
  }

  /* 切章：只发 `chapter` 事件，由 app.js 统一改 #chapterSelect 并重载。
   * 参数：chapterId 目标章节 ID。
   * 返回：无。
   */
  function selectChapter(chapterId) {
    if (!chapterId) return;
    const select = el("chapterSelect");
    if (!select) return;
    if (!Array.from(select.options).some((option) => option.value === chapterId)) return;
    if (select.value === chapterId && window.NW && NW.state.chapter === chapterId) return;
    emit("chapter", chapterId);
  }

  /* 绑定树与按钮事件。参数：无。返回：无。 */
  function bindEvents() {
    const box = el("tree");
    if (box) {
      box.addEventListener("click", (event) => {
        const row = event.target.closest(".tree-row");
        if (!row) return;
        const kind = row.dataset.kind;

        if (kind === "scene") {
          tree.scene = row.dataset.scene;
          renderTree();
          emit("scene", tree.scene);
          return;
        }

        if (kind === "chapter") {
          const chapterId = row.dataset.chapter;
          const key = row.dataset.key;
          if (tree.expanded.has(key)) tree.expanded.delete(key);
          else tree.expanded.add(key);
          renderTree();
          if (tree.expanded.has(key)) loadScenes(chapterId);
          selectChapter(chapterId);
          return;
        }

        const key = row.dataset.key;
        if (key) {
          if (tree.expanded.has(key)) tree.expanded.delete(key);
          else tree.expanded.add(key);
          renderTree();
        }
      });
    }

    const reload = el("btnReload");
    if (reload) reload.addEventListener("click", () => refresh());
  }

  /* 初始化：注册订阅与生命周期钩子。参数：无。返回：无。 */
  function init() {
    if (tree.inited) return;
    tree.inited = true;
    bindEvents();
    if (window.NW) {
      if (NW.onProject) NW.onProject(() => refresh());
      if (NW.onChapter) NW.onChapter(() => onChapterChanged());
    }
    if (window.NW && NW.state.path) refresh();
    else renderTree();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();

  window.NWTree = {
    refresh,
    snapshot,
    init,
    onUpdate(fn) {
      listeners.push(fn);
    },
  };
})();
