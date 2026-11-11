/* 大纲页：生成大纲（写入设定库）、落地成章节、章节的世界 / POV 绑定。
 *
 * 这两步分别对应 `outline generate` 与 `outline apply`；
 * 生成是模型调用（默认离线实现），落地是纯数据操作（建章节文件）。
 * 绑定面板只改 outline 章节的 world_ids / pov_character_id，不碰正文。
 */

(function () {
  const NARRATOR_ID = "chr_narrator";

  let bound = false;
  let busy = false;
  let worlds = [];        // 世界条目，绑定的候选项与名称展示
  let cast = [];          // 人物表（已排除旁白），POV 下拉候选项
  let actsCache = [];     // 当前大纲的幕列表，绑定面板据此查找章节
  let loadedPath = "";    // 上次加载的工程，切工程时关闭面板
  let bindMask = null;    // 绑定面板浮层

  const el = (id) => document.getElementById(id);

  /* 世界 ID → 名称；查不到时退回 ID，避免绑定信息被吞掉。
   * 参数：id 世界 ID。
   * 返回：可展示名称。
   */
  function worldName(id) {
    const hit = worlds.find((item) => item.id === id);
    return hit ? hit.name || id : id;
  }

  /* 人物 ID → 姓名；查不到时退回 ID。
   * 参数：id 人物 ID。
   * 返回：可展示姓名。
   */
  function characterName(id) {
    const hit = cast.find((item) => item.id === id);
    return hit ? hit.name || id : id;
  }

  /* 某个章节的「舞台与人物」区域：世界 chips、POV、缺绑定提示。
   * 参数：chapter 大纲章节。
   * 返回：HTML 字符串。
   */
  function bindBlockHtml(chapter) {
    const ids = chapter.world_ids || [];
    const povId = chapter.pov_character_id && chapter.pov_character_id !== NARRATOR_ID ? chapter.pov_character_id : "";
    const chips = ids
      .map((id) => `<span class="bind-chip">${esc(worldName(id))}</span>`)
      .join("");
    const missing = [];
    if (!ids.length) missing.push("世界");
    if (!povId) missing.push("主要人物");
    return `<div class="outline-bind">
      <div class="outline-bind-head">
        <span class="outline-bind-title">舞台与人物</span>
        <button class="btn small" type="button" data-bind-chapter="${esc(chapter.id || "")}">编辑绑定</button>
      </div>
      <div class="bind-line"><span class="muted">世界</span>${chips ? `<span class="bind-chips">${chips}</span>` : `<span class="bind-none">未绑定</span>`}</div>
      <div class="bind-line"><span class="muted">POV</span>${povId ? `<span class="bind-chip">${esc(characterName(povId))}</span>` : `<span class="bind-none">未设置</span>`}</div>
      ${missing.length ? `<div class="bind-warn">本章缺${missing.join("/")}，无法生成正文</div>` : ""}
    </div>`;
  }

  /* 渲染幕 / 章 / 节拍：幕与章都是可折叠的层级卡片，节拍是幕内的一行一条。
   * 参数：data `outline generate` / `/api/ui/outline` 的原始负载。
   * 返回：无（直接写 #olBody）。
   */
  function render(data) {
    const acts = data.acts || [];
    if (!acts.length) {
      el("olBody").innerHTML = "<p class='muted'>还没有大纲：先填参数点「生成大纲」。</p>";
      return;
    }
    const chapters = acts.reduce((sum, act) => sum + (act.chapters || []).length, 0);
    const beats = acts.reduce((sum, act) => sum + (act.chapters || []).reduce((n, ch) => n + (ch.beats || []).length, 0), 0);
    const parts = [`<p class="muted">共 ${acts.length} 幕 / ${chapters} 章 / ${beats} 个节拍（settings_revision=${esc(data.settings_revision ?? "?")}）</p>`];

    acts.forEach((act, actIndex) => {
      const actChapters = act.chapters || [];
      parts.push(
        `<details class="act" ${actIndex === 0 ? "open" : ""}>` +
          `<summary><span class="act-title">${esc(act.title || "未命名幕")}</span>` +
          `<span class="muted">${actChapters.length} 章</span></summary>` +
          (act.summary ? `<p class="muted">${esc(act.summary)}</p>` : "")
      );

      actChapters.forEach((chapter, chapterIndex) => {
        const beatList = chapter.beats || [];
        parts.push(
          `<details class="chapter-row" ${actIndex === 0 && chapterIndex === 0 ? "open" : ""}>` +
            `<summary><span>${esc(chapter.index)}. ${esc(chapter.title || "")}</span>` +
            `<span class="muted">${beatList.length} 节拍</span></summary>`
        );
        if (chapter.outline) parts.push(`<p class="muted">${esc(chapter.outline)}</p>`);
        parts.push(bindBlockHtml(chapter));
        const list = beatList
          .map(
            (beat, beatIndex) =>
              `<li><span class="beat-no">${beatIndex + 1}</span><span>${beat.turning_point ? "<span class='tag'>转折</span>" : ""}` +
              `${esc(beat.summary || "")}</span><span class="muted">${esc(beat.target_words ?? "?")} 字</span></li>`
          )
          .join("");
        parts.push(`<ul class="beats">${list || "<li class='muted'>没有节拍</li>"}</ul></details>`);
      });

      parts.push("</details>");
    });
    el("olBody").innerHTML = parts.join("");
  }

  /* 读大纲，并顺带拉世界 / 人物作为绑定候选项；候选失败不阻断大纲展示。
   * 参数：无。
   * 返回：Promise，无。
   */
  async function load() {
    if (!NW.state.path) {
      el("olBody").innerHTML = "<p class='muted'>先在上方选择并打开工程。</p>";
      return;
    }
    if (loadedPath !== NW.state.path) {
      loadedPath = NW.state.path;
      closeBindPanel();
    }
    try {
      const [outline, worldData, charData] = await Promise.all([
        NW.api(`/api/ui/outline?${NW.qs({ path: NW.state.path })}`),
        NW.api(`/api/ui/worlds?${NW.qs({ path: NW.state.path })}`).catch(() => ({ worlds: [] })),
        NW.api(`/api/ui/characters?${NW.qs({ path: NW.state.path })}`).catch(() => ({ characters: [] })),
      ]);
      worlds = worldData.worlds || [];
      cast = (charData.characters || []).filter((item) => item.role !== "narrator" && item.id !== NARRATOR_ID);
      actsCache = outline.acts || [];
      render(outline);
    } catch (error) {
      el("olBody").innerHTML = `<p class="muted">读取大纲失败：${esc(error.message)}</p>`;
    }
  }

  /* 按章节 ID 在当前大纲缓存里找章节。
   * 参数：chapterId 章节 ID。
   * 返回：章节对象；找不到返回 null。
   */
  function findChapter(chapterId) {
    for (const act of actsCache) {
      const hit = (act.chapters || []).find((chapter) => chapter.id === chapterId);
      if (hit) return hit;
    }
    return null;
  }

  /* 关闭绑定面板。
   * 参数：无。
   * 返回：无。
   */
  function closeBindPanel() {
    if (!bindMask) return;
    document.removeEventListener("keydown", onBindKeydown, true);
    bindMask.remove();
    bindMask = null;
  }

  /* 绑定面板键盘：Esc 关闭（不保存）。
   * 参数：event 键盘事件。
   * 返回：无。
   */
  function onBindKeydown(event) {
    if (event.key !== "Escape") return;
    event.preventDefault();
    closeBindPanel();
  }

  /* 保存章节绑定并刷新大纲。
   * 参数：mask 面板节点；button 保存按钮；chapterId 章节 ID。
   * 返回：Promise，无。
   */
  async function saveBind(mask, button, chapterId) {
    if (!NW.state.path) return;
    const ids = Array.from(mask.querySelectorAll("input[data-bind-world]:checked")).map((node) => node.value);
    const select = mask.querySelector("[data-bind-pov]");
    const pov = select ? select.value : "";
    if (button) button.disabled = true;
    try {
      await NW.api("/api/ui/outline/bind", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: NW.state.path, chapter: chapterId, world_ids: ids, pov_character_id: pov }),
      });
      NW.toast("章节绑定已更新", true);
      closeBindPanel();
      await load();
    } catch (error) {
      NW.toast(`保存绑定失败：${error.message}`);
    } finally {
      if (button) button.disabled = false;
    }
  }

  /* 打开某章的「编辑绑定」轻量面板：世界多选 + POV 下拉。
   * 参数：chapter 大纲章节。
   * 返回：无。
   */
  function openBindPanel(chapter) {
    closeBindPanel();
    const selectedWorlds = new Set(chapter.world_ids || []);
    const currentPov = chapter.pov_character_id || "";

    bindMask = document.createElement("div");
    bindMask.className = "modal-mask";

    const dialog = document.createElement("div");
    dialog.className = "modal-editor bind-panel";
    dialog.setAttribute("role", "dialog");
    dialog.setAttribute("aria-modal", "true");

    const title = document.createElement("h3");
    title.className = "modal-title";
    title.textContent = `编辑绑定 · ${chapter.index}. ${chapter.title || ""}`;
    dialog.appendChild(title);

    const hint = document.createElement("p");
    hint.className = "modal-hint";
    hint.textContent = "世界可多选；POV 决定正文的主要视角。至少绑定 1 个世界与 1 个 POV 才能生成正文。";
    dialog.appendChild(hint);

    const worldTitle = document.createElement("div");
    worldTitle.className = "bind-section-title";
    worldTitle.textContent = "涉及世界";
    dialog.appendChild(worldTitle);

    const worldList = document.createElement("div");
    worldList.className = "gen-check-list bind-world-list";
    if (!worlds.length) {
      const empty = document.createElement("p");
      empty.className = "muted";
      empty.textContent = "还没有世界条目：先到世界页生成或新增。";
      worldList.appendChild(empty);
    }
    worlds.forEach((world) => {
      const row = document.createElement("label");
      row.className = "gen-check";

      const check = document.createElement("input");
      check.type = "checkbox";
      check.value = world.id || "";
      check.checked = selectedWorlds.has(world.id);
      check.setAttribute("data-bind-world", "");
      row.appendChild(check);

      const body = document.createElement("div");
      body.className = "gen-check-body";
      const head = document.createElement("div");
      head.className = "gen-check-head";
      const nameNode = document.createElement("span");
      nameNode.className = "gen-check-name";
      nameNode.textContent = world.name || "未命名世界";
      head.appendChild(nameNode);
      const era = document.createElement("span");
      era.className = "muted";
      era.textContent = world.era || "时代未注明";
      head.appendChild(era);
      body.appendChild(head);
      if (world.summary) {
        const line = document.createElement("p");
        line.textContent = world.summary;
        body.appendChild(line);
      }
      row.appendChild(body);
      worldList.appendChild(row);
    });
    dialog.appendChild(worldList);

    const povLabel = document.createElement("label");
    povLabel.className = "stack bind-pov";
    povLabel.textContent = "POV 人物";
    const select = document.createElement("select");
    select.setAttribute("data-bind-pov", "");
    const none = document.createElement("option");
    none.value = "";
    none.textContent = "（不设置）";
    select.appendChild(none);
    cast.forEach((item) => {
      const option = document.createElement("option");
      option.value = item.id || "";
      option.textContent = `${item.name || item.id}（${item.role === "protagonist" ? "主角" : "配角"}）`;
      option.selected = item.id === currentPov;
      select.appendChild(option);
    });
    povLabel.appendChild(select);
    dialog.appendChild(povLabel);

    const foot = document.createElement("div");
    foot.className = "modal-foot";

    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "btn";
    cancel.textContent = "取消";
    cancel.addEventListener("click", closeBindPanel);
    foot.appendChild(cancel);

    const save = document.createElement("button");
    save.type = "button";
    save.className = "btn primary";
    save.textContent = "保存绑定";
    save.addEventListener("click", () => saveBind(bindMask, save, chapter.id));
    foot.appendChild(save);

    dialog.appendChild(foot);
    bindMask.appendChild(dialog);
    bindMask.addEventListener("click", (event) => {
      if (event.target === bindMask) closeBindPanel();
    });
    document.body.appendChild(bindMask);
    document.addEventListener("keydown", onBindKeydown, true);
  }

  /* 渲染黄金三章检查结果：通过给绿框，未通过逐条列出「章节 + 问题 + 建议」。
   * 参数：data `/api/ui/outline/check` 的原始负载 {passed, issues}。
   * 返回：无（直接写 #olCheck）。
   */
  function renderCheck(data) {
    const box = el("olCheck");
    const passed = Boolean(data && data.passed);
    const issues = (data && data.issues) || [];
    box.className = `check-result ${passed ? "ok" : "fail"}`;
    if (passed) {
      box.innerHTML = `<div class="check-head">✓ 黄金三章检查通过</div>`;
      box.hidden = false;
      return;
    }
    const items = issues
      .map(
        (issue) =>
          `<li><span class="check-chapter">第 ${esc(issue.chapter ?? "?")} 章</span>` +
          `<span class="check-problem">${esc(issue.problem || "")}</span>` +
          `<span class="check-hint">${esc(issue.hint || "")}</span></li>`
      )
      .join("");
    box.innerHTML =
      `<div class="check-head">✗ 黄金三章未通过（${issues.length} 条建议）</div>` +
      (items ? `<ul class="check-list">${items}</ul>` : "");
    box.hidden = false;
  }

  /* 检查黄金三章：调 `/api/ui/outline/check`，只读不改大纲。 */
  async function checkOutline() {
    const box = el("olCheck");
    if (!NW.state.path) {
      box.className = "check-result fail";
      box.textContent = "先在上方选择并打开工程。";
      box.hidden = false;
      return;
    }
    const button = el("olCheckBtn");
    button.disabled = true;
    try {
      renderCheck(await NW.api(`/api/ui/outline/check?${NW.qs({ path: NW.state.path })}`));
    } catch (error) {
      box.className = "check-result fail";
      box.innerHTML = `<div class="check-head">检查失败：${esc(error.message)}</div>`;
      box.hidden = false;
      NW.toast(`检查失败：${error.message}`);
    } finally {
      button.disabled = false;
    }
  }

  /* 生成大纲。 */
  async function generate() {
    if (busy || !NW.state.path) return;
    busy = true;
    el("olGenerate").disabled = true;
    try {
      const result = await NW.api("/api/ui/outline/generate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          path: NW.state.path,
          acts: Number(el("olActs").value),
          chapters_per_act: Number(el("olChapters").value),
          beats_per_chapter: Number(el("olBeats").value),
          words: Number(el("olWords").value),
        }),
        busy: "正在生成大纲…",
        busyDetail: "按幕分批生成，幕数越多耗时越长",
      });
      NW.toast(`大纲已生成：${result.chapters} 章 / ${result.beats} 个节拍（provider=${result.provider}）`, true);
      /* 重新拉取大纲与候选，保证绑定信息一并刷新 */
      await load();
      /* 生成后自动跑一次黄金三章检查（只提示，不阻断） */
      await checkOutline();
    } catch (error) {
      NW.toast(`生成失败（退出码 ${error.code}）：${error.message}`);
    } finally {
      busy = false;
      el("olGenerate").disabled = false;
    }
  }

  /* 落地成章节。 */
  async function apply() {
    if (busy || !NW.state.path) return;
    busy = true;
    el("olApply").disabled = true;
    try {
      const result = await NW.api("/api/ui/outline/apply", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: NW.state.path }),
      });
      NW.toast(`已落地：新建 ${result.created.length} 章，更新 ${result.updated.length} 章`, true);
      await load();
      if (NW.openProject) await NW.openProject();
    } catch (error) {
      NW.toast(`落地失败（退出码 ${error.code}）：${error.message}`);
    } finally {
      busy = false;
      el("olApply").disabled = false;
    }
  }

  /* 大纲列表点击分发：打开某章的绑定面板。
   * 参数：event 点击事件。
   * 返回：无。
   */
  function onBodyClick(event) {
    const trigger = event.target.closest("[data-bind-chapter]");
    if (!trigger) return;
    const chapterId = trigger.dataset.bindChapter || "";
    const chapter = findChapter(chapterId);
    if (!chapter) {
      NW.toast("章节已变化，请刷新后再编辑绑定");
      return;
    }
    openBindPanel(chapter);
  }

  /* 首次激活绑定事件。 */
  function bind() {
    if (bound) return;
    bound = true;
    el("olGenerate").addEventListener("click", generate);
    el("olCheckBtn").addEventListener("click", checkOutline);
    el("olApply").addEventListener("click", apply);
    const body = el("olBody");
    if (body) body.addEventListener("click", onBodyClick);
    NW.onProject(() => load());
  }

  window.Pages.outline = {
    activate() {
      bind();
      load();
    },
  };
})();
