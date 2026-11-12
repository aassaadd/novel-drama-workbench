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
  let landedIds = new Set(); // 已落地成章节文件的章节 ID（删除时用于二次确认）
  let lastContinueRequest = null; // 上次续写预览的入参，写入时复用，避免生成另一份
  let continueMask = null;    // 续写面板浮层
  let continueChapters = 3;   // 续写章数默认值（上次面板里填的数）
  let loadedPath = "";    // 上次加载的工程，切工程时关闭面板

  /* 续写快捷方向：chip 只把整句填进输入框，不直接提交。 */
  const CONTINUE_CHIPS = [
    "推进主线：把当前冲突再往前推一步",
    "补一段感情线：让两位主角的关系有实质进展",
    "收束支线：把前面埋下的伏笔收掉一条",
    "加一次反转：让读者对已知信息产生怀疑",
  ];
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
      <div class="bind-line"><span class="muted">视角人物</span>${povId ? `<span class="bind-chip">${esc(characterName(povId))}</span>` : `<span class="bind-none">未设置</span>`}</div>
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
          `<summary><span class="act-title">第 ${esc(act.index ?? actIndex + 1)} 幕 · ${esc(act.title || "未命名幕")}</span>` +
          `<span class="muted">${actChapters.length} 章</span>` +
          `<button class="btn small act-del" type="button" data-del-act="${esc(act.id || "")}" ` +
          `style="margin-left:auto;color:#b3261e;border-color:#d9a5a0">删除整幕</button>` +
          `</summary>` +
          (act.summary ? `<p class="muted">${esc(act.summary)}</p>` : "")
      );

      actChapters.forEach((chapter, chapterIndex) => {
        const beatList = chapter.beats || [];
        const landed = landedIds.has(chapter.id);
        parts.push(
          `<details class="chapter-row" ${actIndex === 0 && chapterIndex === 0 ? "open" : ""}>` +
            `<summary><span>${esc(chapter.index)}. ${esc(chapter.title || "")}</span>` +
            `${landed ? "<span class='tag'>已落地</span>" : "<span class='muted'>未落地</span>"}` +
            `<span class="muted">${beatList.length} 节拍</span>` +
            `<button class="btn small" type="button" data-del-chapter="${esc(chapter.id || "")}">删除整章</button>` +
            `</summary>`
        );
        if (chapter.outline) parts.push(`<p class="muted">${esc(chapter.outline)}</p>`);
        parts.push(bindBlockHtml(chapter));
        const list = beatList
          .map(
            (beat, beatIndex) =>
              `<li><span class="beat-no">${beatIndex + 1}</span><span>${beat.turning_point ? "<span class='tag'>转折</span>" : ""}` +
              `${esc(beat.summary || "")}</span><span class="muted">${esc(beat.target_words ?? "?")} 字</span>` +
              `<button class="btn small" type="button" data-del-beat="${beatIndex + 1}" data-chapter="${esc(chapter.id || "")}">删除</button></li>`
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
      lastContinueRequest = null;
      closeContinuePanel();
      closeBindPanel();
    }
    try {
      const [outline, worldData, charData, chapterList] = await Promise.all([
        NW.api(`/api/ui/outline?${NW.qs({ path: NW.state.path })}`),
        NW.api(`/api/ui/worlds?${NW.qs({ path: NW.state.path })}`).catch(() => ({ worlds: [] })),
        NW.api(`/api/ui/characters?${NW.qs({ path: NW.state.path })}`).catch(() => ({ characters: [] })),
        NW.api(`/api/ui/chapters?${NW.qs({ path: NW.state.path })}`).catch(() => []),
      ]);
      worlds = worldData.worlds || [];
      cast = (charData.characters || []).filter((item) => item.role !== "narrator" && item.id !== NARRATOR_ID);
      actsCache = outline.acts || [];
      const rows = Array.isArray(chapterList) ? chapterList : chapterList.chapters || [];
      landedIds = new Set(rows.map((item) => item.id));
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
    hint.textContent = "世界可多选；视角人物决定正文的主要视角。至少绑定 1 个世界与 1 个视角人物才能生成正文。";
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
    povLabel.textContent = "视角人物";
    const select = document.createElement("select");
    select.setAttribute("data-bind-pov", "");
    const none = document.createElement("option");
    none.value = "";
    none.textContent = "（不设置）";
    select.appendChild(none);
    cast.forEach((item) => {
      const option = document.createElement("option");
      option.value = item.id || "";
      // 角色定位统一走 core.js 的 roleLabel，未知值原样显示，不再兜底成「配角」
      option.textContent = `${item.name || item.id}（${roleLabel(item.role)}）`;
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

  /* 生成大纲。已有大纲时先确认，避免一次点击替换整份大纲。 */
  async function generate() {
    if (busy || !NW.state.path) return;
    const currentChapters = actsCache.reduce((sum, act) => sum + (act.chapters || []).length, 0);
    if (actsCache.length) {
      const ok = window.confirm(
        `当前已有 ${actsCache.length} 幕 / ${currentChapters} 章大纲。\n` +
          "重新生成会替换这份大纲（已落地章节的正文不受影响）。确定继续？"
      );
      if (!ok) return;
    }
    busy = true;
    el("olGenerate").disabled = true;
    const progress = NW.progressInit();
    NW.busyShow("正在生成大纲…", "正在准备提示词…");
    try {
      /* 走流式进度接口：显示批次与模型已返回的 JSON 增量字数 */
      const result = await NW.streamPost(
        "/api/ui/outline/generate/stream",
        {
          path: NW.state.path,
          acts: Number(el("olActs").value),
          chapters_per_act: Number(el("olChapters").value),
          beats_per_chapter: Number(el("olBeats").value),
          words: Number(el("olWords").value),
        },
        (event, data) => {
          if (event === "batch" || event === "chars") NW.busyDetail(NW.progressApply(progress, data));
        }
      );
      NW.toast(`大纲已生成：${result.chapters} 章 / ${result.beats} 个节拍`, true);
      /* 重新拉取大纲与候选，保证绑定信息一并刷新 */
      await load();
      /* 生成后自动跑一次黄金三章检查（只提示，不阻断） */
      await checkOutline();
    } catch (error) {
      NW.toast(`生成失败（退出码 ${error.code}）：${error.message}`);
    } finally {
      NW.busyHide();
      busy = false;
      el("olGenerate").disabled = false;
    }
  }

  /* 落地成章节。 */
  async function apply() {
    if (busy || !NW.state.path) return;
    const chapterCount = actsCache.reduce((sum, act) => sum + (act.chapters || []).length, 0);
    if (chapterCount) {
      const ok = window.confirm(
        `将按当前大纲落地 ${chapterCount} 章：缺失的章节会新建，已存在的同序号章节会更新标题与摘要等元数据。\n` +
          "章节正文不会被覆盖。确定继续？"
      );
      if (!ok) return;
    }
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

  /* 删除一个节拍：只改大纲数据，已落地章节的正文不动。
   * 参数：chapterId 大纲章节 ID；beatIndex 节拍序号（从 1 开始）。
   * 返回：Promise<void>。
   */
  async function deleteBeat(chapterId, beatIndex) {
    if (!NW.state.path || !chapterId || !beatIndex) return;
    const chapter = findChapter(chapterId);
    const beat = chapter && (chapter.beats || [])[beatIndex - 1];
    const label = beat && beat.summary ? `「${beat.summary}」` : "";
    if (!window.confirm(`删除第 ${beatIndex} 个节拍${label}？\n只改大纲数据，不影响章节正文。`)) return;
    try {
      await NW.api("/api/ui/outline/beat/delete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: NW.state.path, chapter: chapterId, beat: beatIndex }),
      });
      NW.toast("节拍已删除，正文未改动", true);
      await load();
    } catch (error) {
      NW.toast(`删除节拍失败：${error.message}`);
    }
  }

  /* 删除整章：先取影响做二次确认，再删大纲条目（已落地时连章节文件一起删）。
   * 参数：chapterId 大纲章节 ID。
   * 返回：Promise<void>。
   */
  async function deleteChapter(chapterId) {
    if (!NW.state.path || !chapterId) return;
    let impact = null;
    try {
      impact = await NW.api("/api/ui/outline/chapter/delete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: NW.state.path, chapter: chapterId, dry_run: true }),
      });
    } catch (error) {
      NW.toast(`读取章节影响失败：${error.message}`);
      return;
    }
    let message;
    if (impact.landed) {
      message =
        `删除《第 ${impact.index} 章 ${impact.title}》后不可恢复：\n` +
        `· 已落地内容：正文 ${impact.chars} 字、台词 ${impact.lines} 条、音频 ${impact.audio} 条、图片 ${impact.image} 条、视频 ${impact.video} 条\n` +
        "· 大纲条目一并删除，后续章节序号自动前移\n" +
        "· assets 下的素材文件保留，不自动删除\n" +
        "确定删除？";
    } else {
      message =
        `删除生成区里的《第 ${impact.index} 章 ${impact.title}》？\n` +
        "只删大纲条目，后续章节序号自动前移；已落地章节不受影响。";
    }
    if (!window.confirm(message)) return;
    try {
      const result = await NW.api("/api/ui/outline/chapter/delete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: NW.state.path, chapter: chapterId }),
      });
      let note = result.deleted.landed ? "已删除章节与大纲条目（素材文件保留）" : "已删除大纲章节";
      if (result.remaining === 0) note += "；大纲已清空，可重新生成或续写";
      if ((result.warnings || []).length) note += `；${result.warnings.join("；")}`;
      NW.toast(note, true);
      /* 已落地章节删除后左侧目录也要刷新；当前章节被删时由 openProject 兜底切到可用章节 */
      if (result.deleted.landed && NW.openProject) {
        await NW.openProject();
      }
      await load();
    } catch (error) {
      NW.toast(`删除章节失败：${error.message}`);
    }
  }

  /* 删除整幕：先按幕汇总影响做二次确认，再删该幕及其下全部章节。
   * 参数：actId 幕 ID。
   * 返回：Promise<void>。
   */
  async function deleteAct(actId) {
    if (!NW.state.path || !actId) return;
    let impact = null;
    try {
      impact = await NW.api("/api/ui/outline/act/delete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: NW.state.path, act: actId, dry_run: true }),
      });
    } catch (error) {
      NW.toast(`读取幕影响失败：${error.message}`);
      return;
    }
    const rows = impact.chapters.map((item) =>
      item.landed
        ? `· 第 ${item.index} 章 ${item.title}（已落地）：正文 ${item.chars} 字、台词 ${item.lines} 条、音频 ${item.audio} 条、图片 ${item.image} 条、视频 ${item.video} 条`
        : `· 第 ${item.index} 章 ${item.title}（未落地）`
    );
    const totals = impact.totals;
    const message =
      `删除《第 ${impact.index} 幕 ${impact.title}》及其 ${impact.chapters.length} 章后不可恢复：\n` +
      rows.join("\n") +
      `\n合计：正文 ${totals.chars} 字、台词 ${totals.lines} 条、音频 ${totals.audio} 条、图片 ${totals.image} 条、视频 ${totals.video} 条\n` +
      "· 后续章节序号与幕序号自动前移\n" +
      "· assets 下的素材文件保留，不自动删除\n" +
      "确定删除整幕？";
    if (!window.confirm(message)) return;
    try {
      const result = await NW.api("/api/ui/outline/act/delete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: NW.state.path, act: actId }),
      });
      let note = `已删除「${impact.title}」及其 ${impact.chapters.length} 章（素材文件保留）`;
      if (result.remaining_chapters === 0) note += "；大纲已清空，可重新生成或续写";
      if ((result.warnings || []).length) note += `；${result.warnings.join("；")}`;
      NW.toast(note, true);
      if (impact.landed_count && NW.openProject) {
        await NW.openProject();
      }
      await load();
    } catch (error) {
      NW.toast(`删除整幕失败：${error.message}`);
    }
  }

  /* 大纲列表点击分发：删除节拍 / 删除整章 / 删除整幕 / 打开绑定面板。
   * 参数：event 点击事件。
   * 返回：无。
   */
  function onBodyClick(event) {
    const delAct = event.target.closest("[data-del-act]");
    if (delAct) {
      event.preventDefault();
      event.stopPropagation();
      deleteAct(delAct.dataset.delAct || "");
      return;
    }
    const delBeat = event.target.closest("[data-del-beat]");
    if (delBeat) {
      event.preventDefault();
      event.stopPropagation();
      deleteBeat(delBeat.dataset.chapter || "", Number(delBeat.dataset.delBeat));
      return;
    }
    const delChapter = event.target.closest("[data-del-chapter]");
    if (delChapter) {
      event.preventDefault();
      event.stopPropagation();
      deleteChapter(delChapter.dataset.delChapter || "");
      return;
    }
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

  /* ---------------- 续写大纲 ---------------- */

  /* 关闭续写面板（不提交）。 */
  function closeContinuePanel() {
    if (!continueMask) return;
    document.removeEventListener("keydown", onContinueKeydown, true);
    continueMask.remove();
    continueMask = null;
  }

  /* 续写面板键盘：Esc 关闭（不提交）。 */
  function onContinueKeydown(event) {
    if (event.key !== "Escape") return;
    event.preventDefault();
    closeContinuePanel();
  }

  /* 打开「续写大纲」面板：章数 + 需求 textarea + 快捷方向 chip + 预览。
   * 参数：无。
   * 返回：无。
   */
  function openContinuePanel() {
    if (!NW.state.path) {
      NW.toast("先打开或新建工程");
      return;
    }
    closeContinuePanel();
    continueMask = document.createElement("div");
    continueMask.className = "modal-mask";

    const dialog = document.createElement("div");
    dialog.className = "modal-editor continue-panel";
    dialog.setAttribute("role", "dialog");
    dialog.setAttribute("aria-modal", "true");

    const title = document.createElement("h3");
    title.className = "modal-title";
    title.textContent = "续写大纲";
    dialog.appendChild(title);

    const hint = document.createElement("p");
    hint.className = "modal-hint";
    hint.textContent = "按当前大纲节奏往后追加，先预览，确认后再写入设定库。";
    dialog.appendChild(hint);

    const chaptersLabel = document.createElement("label");
    chaptersLabel.className = "stack";
    chaptersLabel.textContent = "续写章数";
    const chaptersInput = document.createElement("input");
    chaptersInput.type = "number";
    chaptersInput.min = "1";
    chaptersInput.max = "12";
    chaptersInput.value = String(continueChapters);
    chaptersInput.style.width = "96px";
    chaptersLabel.appendChild(chaptersInput);
    const chaptersHint = document.createElement("span");
    chaptersHint.className = "muted";
    chaptersHint.style.fontSize = "12px";
    chaptersHint.textContent = "按当前大纲节奏";
    chaptersLabel.appendChild(chaptersHint);
    dialog.appendChild(chaptersLabel);

    const intentLabel = document.createElement("label");
    intentLabel.className = "stack";
    intentLabel.textContent = "续写需求";
    const intentInput = document.createElement("textarea");
    intentInput.rows = 4;
    intentInput.placeholder = "例如：这一段落要收束师父的旧案，让主角第一次动摇";
    intentInput.style.cssText = "width:100%;box-sizing:border-box;min-height:96px;resize:vertical";
    intentLabel.appendChild(intentInput);
    dialog.appendChild(intentLabel);

    const intentHint = document.createElement("p");
    intentHint.className = "muted";
    intentHint.style.cssText = "margin:0;font-size:12px";
    intentHint.textContent = "留空则按现有大纲自然续写（Ctrl/Cmd + Enter 可预览）";
    dialog.appendChild(intentHint);

    const chips = document.createElement("div");
    chips.className = "actions";
    chips.style.cssText = "flex-wrap:wrap;gap:6px";
    CONTINUE_CHIPS.forEach((text) => {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "btn small";
      chip.textContent = text.split("：")[0];
      chip.title = text;
      /* 只填充输入框，不提交预览 */
      chip.addEventListener("click", () => {
        const current = String(intentInput.value || "").trim();
        intentInput.value = current ? `${current}；${text}` : text;
        intentInput.focus();
      });
      chips.appendChild(chip);
    });
    dialog.appendChild(chips);

    const foot = document.createElement("div");
    foot.className = "modal-foot";
    foot.style.cssText = "display:flex;gap:8px;justify-content:flex-end;flex-wrap:wrap";
    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "btn";
    cancel.textContent = "取消";
    cancel.addEventListener("click", closeContinuePanel);
    const preview = document.createElement("button");
    preview.type = "button";
    preview.className = "btn primary";
    preview.textContent = "预览";
    preview.addEventListener("click", () => previewContinue(chaptersInput, intentInput, preview));
    foot.append(cancel, preview);
    dialog.appendChild(foot);

    /* Ctrl / Cmd + Enter 触发预览（不写入） */
    dialog.addEventListener("keydown", (event) => {
      if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
        event.preventDefault();
        previewContinue(chaptersInput, intentInput, preview);
      }
    });
    continueMask.appendChild(dialog);
    continueMask.addEventListener("click", (event) => {
      if (event.target === continueMask) closeContinuePanel();
    });
    document.body.appendChild(continueMask);
    document.addEventListener("keydown", onContinueKeydown, true);
    intentInput.focus();
  }

  /* 请求大纲续写预览：在已有章节之后追加，跨幕自动建幕，不写盘。
   * 参数：chaptersInput 章数输入框；intentInput 需求输入框；button 预览按钮。
   * 返回：Promise<void>。
   */
  async function previewContinue(chaptersInput, intentInput, button) {
    if (busy || !NW.state.path) return;
    const chapters = Math.max(1, Math.min(12, Number((chaptersInput && chaptersInput.value) || 3)));
    const intent = String((intentInput && intentInput.value) || "").trim();
    const words = Number((el("olWords") || {}).value || 1200);
    continueChapters = chapters;
    lastContinueRequest = { chapters, words, intent };
    busy = true;
    if (button) button.disabled = true;
    try {
      const result = await NW.api("/api/ui/outline/continue", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: NW.state.path, chapters, words, intent }),
        busy: "正在续写大纲…",
        busyDetail: intent ? "按填写的续写需求往后排，先预览再写入" : "接着已有章节往后排，先预览再写入",
      });
      closeContinuePanel();
      showContinueDialog(result);
    } catch (error) {
      NW.toast(`续写大纲失败（${error.code || "-"}）：${error.message}`);
    } finally {
      busy = false;
      if (button) button.disabled = false;
    }
  }

  /* 「续写大纲」入口：先打开面板收集章数与需求。 */
  function requestOutlineContinue() {
    openContinuePanel();
  }

  /* 大纲续写确认弹窗：列出新增幕/章，可取消或写入设定库。
   * 参数：result 续写预览结果。
   * 返回：无。
   */
  function showContinueDialog(result) {
    const mask = document.createElement("div");
    mask.style.cssText =
      "position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:60;display:flex;align-items:center;justify-content:center";
    const box = document.createElement("div");
    box.style.cssText =
      "background:#fff;color:#222;border-radius:10px;max-width:680px;width:92%;max-height:82vh;display:flex;flex-direction:column;padding:16px 18px;gap:10px;box-shadow:0 12px 40px rgba(0,0,0,.25)";
    const chapters = result.new_chapters || [];
    const rows = chapters
      .map(
        (item) =>
          `<div style="font-size:13px;line-height:1.8">第 ${item.index} 章《${item.title || "未命名"}》` +
          (item.summary ? `：${item.summary}` : "") +
          "</div>"
      )
      .join("");
    const head = document.createElement("div");
    const intentLine = result.intent
      ? `<div style="margin-top:6px;font-size:13px;line-height:1.7">续写需求：<b>${esc(result.intent)}</b></div>` +
        `<div style="font-size:13px;line-height:1.7">将按这个要求续写 <b>${result.chapters_added}</b> 章</div>`
      : `<div style="margin-top:6px;font-size:13px;line-height:1.7">未填写续写需求，按现有大纲续写 <b>${result.chapters_added}</b> 章</div>`;
    head.innerHTML =
      "<b>大纲续写预览（尚未写入）</b>" +
      intentLine +
      `<div style="margin-top:6px;font-size:13px;line-height:1.7">本次将新增 <b>${result.acts_added}</b> 幕 / ` +
      `<b>${result.chapters_added}</b> 章：第 ${result.start_index} 章起</div>` +
      `<div style="max-height:46vh;overflow:auto;margin-top:8px">${rows || "（模型没有返回可用章节）"}</div>` +
      '<div style="margin-top:8px;font-size:12px;color:#888">写入后仍需点「落地成章节」才会创建章节文件。</div>';
    const actions = document.createElement("div");
    actions.style.cssText = "display:flex;gap:8px;justify-content:flex-end";
    const mk = (text, primary, onClick) => {
      const button = document.createElement("button");
      button.className = primary ? "btn primary" : "btn";
      button.textContent = text;
      button.addEventListener("click", onClick);
      return button;
    };
    actions.appendChild(mk("取消", false, () => mask.remove()));
    actions.appendChild(mk("写入大纲", true, () => applyOutlineContinue(mask)));
    box.append(head, actions);
    mask.appendChild(box);
    mask.addEventListener("click", (event) => {
      if (event.target === mask) mask.remove();
    });
    document.body.appendChild(mask);
  }

  /* 把预览结果写入设定库大纲（逐幕落盘），随后刷新页面大纲。
   * 参数：mask 当前弹窗。
   * 返回：Promise<void>。
   */
  async function applyOutlineContinue(mask) {
    const button = mask ? mask.querySelector("button.primary") : null;
    if (button) button.disabled = true;
    try {
      const request = lastContinueRequest || {};
      const result = await NW.api("/api/ui/outline/continue", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          path: NW.state.path,
          chapters: request.chapters,
          words: request.words,
          intent: request.intent,
          apply: true,
        }),
        busy: "正在写入大纲…",
      });
      if (mask) mask.remove();
      NW.toast(`已追加 ${result.acts_added} 幕 / ${result.chapters_added} 章，记得点「落地成章节」`, true);
      await load();
    } catch (error) {
      NW.toast(`写入大纲失败（${error.code || "-"}）：${error.message}`);
      if (button) button.disabled = false;
    }
  }

  /* 首次激活绑定事件。 */
  function bind() {
    if (bound) return;
    bound = true;
    el("olContinue").addEventListener("click", requestOutlineContinue);
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
