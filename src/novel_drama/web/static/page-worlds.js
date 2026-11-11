/* 世界页：世界条目列表、就地编辑、生成世界、封面图与删除。
 *
 * 取数：
 *   - GET  /api/ui/worlds            列表（含 count / characters）
 *   - POST /api/ui/worlds            新增或更新（带 id 视为更新）
 *   - POST /api/ui/worlds/generate   生成候选（不落盘，预览后勾选保存）
 *   - POST /api/ui/worlds/image      生成封面
 *   - POST /api/ui/worlds/delete     删除（被章节引用时返回 409 与章节列表）
 * 所有写操作成功后重新拉列表并刷新流程条上的世界数量。
 */

(function () {
  const EMPTY_TEXT = "还没有世界设定：先打开工程，再点「生成世界」，或点「＋ 新增」手工录入。";
  const WORLD_FORM_KEYS = ["name", "era", "summary", "visual_style", "rules", "factions", "locations", "glossary"];

  let bound = false;
  let worlds = [];
  let editingId = null;      // 正在就地编辑的世界 ID；"__new__" 表示新增
  let loadedPath = "";       // 上次加载的工程，用于切工程时清空编辑态
  let conflicts = {};        // 世界 ID → 引用该世界的章节列表（删除被拒绝时展示）
  let wizardMask = null;     // 生成候选预览浮层
  let wizardWorlds = [];     // 当前预览的候选世界

  const el = (id) => document.getElementById(id);

  /* 按钮 loading：记住原文并禁用，结束后恢复。
   * 参数：button 按钮节点；loading 是否进入 loading；text loading 文案。
   * 返回：无。
   */
  function setLoading(button, loading, text) {
    if (!button) return;
    if (loading) {
      if (!button.dataset.loadingLabel) button.dataset.loadingLabel = button.textContent;
      button.textContent = text || "处理中…";
      button.disabled = true;
      button.classList.add("is-loading");
      return;
    }
    if (button.dataset.loadingLabel) {
      button.textContent = button.dataset.loadingLabel;
      delete button.dataset.loadingLabel;
    }
    button.disabled = false;
    button.classList.remove("is-loading");
  }

  /* 多行文本 → 去空行数组（「一行一项」）。
   * 参数：text 多行文本。
   * 返回：字符串数组。
   */
  function lines(text) {
    return String(text || "")
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean);
  }

  /* 生成一个客户端地点 ID（schema 要求 loc_ 前缀 + 6..32 位字母数字）。
   * 参数：无。
   * 返回：地点 ID 字符串。
   */
  function newLocationId() {
    const random = Math.random().toString(36).slice(2, 8);
    const stamp = Date.now().toString(36).slice(-6);
    return `loc_${random}${stamp}`;
  }

  /* 多行文本 → 地点数组：一行一项，`名称|描述|氛围`。
   * 参数：text 多行文本。
   * 返回：[{id, name, description, atmosphere}]。
   */
  function parseLocations(text) {
    return lines(text)
      .map((line) => {
        const parts = line.split("|").map((item) => item.trim());
        const name = parts[0] || "";
        if (!name) return null;
        return {
          id: newLocationId(),
          name,
          description: parts[1] || "",
          atmosphere: parts[2] || "",
        };
      })
      .filter(Boolean);
  }

  /* 多行文本 → 词条数组：一行一项，`术语|释义`。
   * 参数：text 多行文本。
   * 返回：[{term, meaning}]。
   */
  function parseGlossary(text) {
    return lines(text)
      .map((line) => {
        const parts = line.split("|").map((item) => item.trim());
        const term = parts[0] || "";
        if (!term) return null;
        return { term, meaning: parts[1] || "" };
      })
      .filter(Boolean);
  }

  /* 世界条目 → 表单文本值（数组/对象按「一行一项」序列化）。
   * 参数：world 世界条目。
   * 返回：各表单字段的字符串值。
   */
  function worldToForm(world) {
    const data = world || {};
    return {
      name: data.name || "",
      era: data.era || "",
      summary: data.summary || "",
      visual_style: data.visual_style || "",
      rules: (data.rules || []).join("\n"),
      factions: (data.factions || []).join("\n"),
      locations: (data.locations || [])
        .map((item) => [item.name || "", item.description || "", item.atmosphere || ""].join("|"))
        .join("\n"),
      glossary: (data.glossary || []).map((item) => [item.term || "", item.meaning || ""].join("|")).join("\n"),
    };
  }

  /* 一张世界卡片的 HTML。
   * 参数：world 世界条目。
   * 返回：HTML 字符串。
   */
  function cardHtml(world) {
    const id = world.id || "";
    // 新增时临时卡片没有 id，也要展开表单
    const editing = editingId === id || (editingId === "__new__" && !id);
    const coverRel = world.cover_asset || "";
    const coverUrl = coverRel ? uiFileUrl(coverRel) : "";
    const cover = coverUrl
      ? `<img class="world-cover" loading="lazy" src="${esc(coverUrl)}" alt="${esc(world.name || "世界封面")}" />`
      : `<div class="world-cover is-empty"><span>暂无配图</span></div>`;
    const count = (key) => (world[key] || []).length;

    return `<article class="world-card${editing ? " is-editing" : ""}" data-world-id="${esc(id)}">
      ${cover}
      <div class="world-body">
        <div class="world-head">
          <span class="world-name">${esc(world.name || "未命名世界")}</span>
          <span class="world-meta muted">${esc(world.era || "时代未注明")}</span>
          ${id
            ? `<span class="world-actions">
            <button class="btn small" type="button" data-world-act="edit" data-id="${esc(id)}">编辑</button>
            <button class="btn small" type="button" data-world-act="image" data-id="${esc(id)}" data-cover="${coverRel ? "1" : "0"}">${coverRel ? "重新生成配图" : "生成配图"}</button>
            <button class="btn small" type="button" data-world-act="delete" data-id="${esc(id)}">删除</button>
          </span>`
            : ""}
        </div>
        <p class="world-summary">${esc(world.summary || "—")}</p>
        <div class="world-stats muted">
          <span>规则 ${count("rules")}</span>
          <span>势力 ${count("factions")}</span>
          <span>地点 ${count("locations")}</span>
          <span>词条 ${count("glossary")}</span>
          ${world.visual_style ? `<span>视觉 ${esc(world.visual_style)}</span>` : ""}
        </div>
        ${conflictHtml(id)}
        ${editing ? formHtml(world) : ""}
      </div>
    </article>`;
  }

  /* 删除被拒绝时的引用章节提示块。
   * 参数：id 世界 ID。
   * 返回：HTML 字符串；没有冲突时返回空串。
   */
  function conflictHtml(id) {
    const chapters = conflicts[id] || [];
    if (!chapters.length) return "";
    const items = chapters
      .map((chapter) => `<li>${esc(chapter.title || chapter.id || "未命名章节")}${chapter.id ? `（${esc(chapter.id)}）` : ""}</li>`)
      .join("");
    return `<div class="world-conflict">该世界仍被 ${chapters.length} 个章节引用，请先到大纲页解绑：<ul>${items}</ul></div>`;
  }

  /* 就地编辑表单：多行文本「一行一项」，对象类型用「主字段|说明」。
   * 参数：world 世界条目（新增时传空结构）。
   * 返回：HTML 字符串。
   */
  function formHtml(world) {
    const data = worldToForm(world);
    const amplify = (title) => `<button class="btn small amplify-btn" type="button" data-amplify data-title="${esc(title)}" title="放大编辑">⤢</button>`;
    return `<div class="world-edit">
      <div class="actions">
        <button class="btn" type="button" data-world-act="generate">✨ 按需求生成</button>
        <span class="muted">按需求一次生成全部字段，确认后回填到下方表单</span>
      </div>
      <div class="form-grid">
        <label class="stack">名称
          <input data-world-field="name" value="${esc(data.name)}" placeholder="如：镜中城" />
        </label>
        <label class="stack">时代
          <input data-world-field="era" value="${esc(data.era)}" placeholder="如：民国 · 架空上海" />
        </label>
      </div>
      <div class="stack">摘要
        <span class="field-action">
          <textarea data-world-field="summary" rows="3" placeholder="世界的整体面貌与矛盾">${esc(data.summary)}</textarea>
          ${amplify("world-summary")}
        </span>
      </div>
      <div class="stack">视觉基调
        <span class="field-action">
          <input data-world-field="visual_style" value="${esc(data.visual_style)}" placeholder="供图像提示词拼装的统一画面风格" />
          ${amplify("world-visual")}
        </span>
      </div>
      <div class="form-grid">
        <div class="stack">规则（一行一项）
          <span class="field-action">
            <textarea data-world-field="rules" rows="4" placeholder="夜里不能说出真名">${esc(data.rules)}</textarea>
            ${amplify("world-rules")}
          </span>
        </div>
        <div class="stack">势力（一行一项）
          <span class="field-action">
            <textarea data-world-field="factions" rows="4" placeholder="守夜人">${esc(data.factions)}</textarea>
            ${amplify("world-factions")}
          </span>
        </div>
      </div>
      <div class="form-grid">
        <div class="stack">地点（名称|描述|氛围）
          <span class="field-action">
            <textarea data-world-field="locations" rows="4" placeholder="钟楼|城北最高的塔|压抑">${esc(data.locations)}</textarea>
            ${amplify("world-locations")}
          </span>
        </div>
        <div class="stack">词条（术语|释义）
          <span class="field-action">
            <textarea data-world-field="glossary" rows="4" placeholder="回响|听见过去声音的能力">${esc(data.glossary)}</textarea>
            ${amplify("world-glossary")}
          </span>
        </div>
      </div>
      <div class="actions">
        <button class="btn primary" type="button" data-world-act="save">保存</button>
        <button class="btn" type="button" data-world-act="cancel">取消</button>
      </div>
    </div>`;
  }

  /* 读取表单当前值（全是字符串，用于「已有内容」判断）。
   * 参数：card 世界卡片节点。
   * 返回：{字段名: 字符串}。
   */
  function readFormText(card) {
    const result = {};
    WORLD_FORM_KEYS.forEach((key) => {
      const node = card ? card.querySelector(`[data-world-field="${key}"]`) : null;
      result[key] = node ? node.value.trim() : "";
    });
    return result;
  }

  /* 把表单值组装成可直接提交的世界条目（地点/词条按 schema 结构解析）。
   * 参数：card 世界卡片节点。
   * 返回：世界条目 dict。
   */
  function readWorldForm(card) {
    const raw = readFormText(card);
    return {
      name: raw.name,
      era: raw.era,
      summary: raw.summary,
      visual_style: raw.visual_style,
      rules: lines(raw.rules),
      factions: lines(raw.factions),
      locations: parseLocations(raw.locations),
      glossary: parseGlossary(raw.glossary),
    };
  }

  /* 把生成结果回填到表单（只回填，不落盘）。
   * 参数：card 世界卡片节点；fields 生成的世界条目。
   * 返回：无。
   */
  function applyGenerated(card, fields) {
    const data = worldToForm(fields);
    Object.entries(data).forEach(([key, value]) => {
      const node = card.querySelector(`[data-world-field="${key}"]`);
      if (node) node.value = value;
    });
    NW.toast("已回填生成结果，确认后点保存", true);
  }

  /* 渲染列表与空态。
   * 参数：emptyText 空态提示，省略时用默认文案。
   * 返回：无。
   */
  function render(emptyText) {
    const box = el("worldsList");
    const empty = el("worldsEmpty");
    if (box) box.innerHTML = worlds.map((world) => cardHtml(world)).join("");
    if (empty) {
      empty.textContent = emptyText || EMPTY_TEXT;
      empty.hidden = worlds.length > 0;
    }
  }

  /* 刷新流程条上的世界数量。
   * 参数：count 世界条目数。
   * 返回：无。
   */
  function setFlowCount(count) {
    if (!window.NW || !NW.state) return;
    if (!NW.state.flowData) NW.state.flowData = { worlds: 0, characters: 0 };
    NW.state.flowData.worlds = Number(count) || 0;
    const layout = window.NWLayout || {};
    const refresh = layout.refreshFlow || layout.renderFlow;
    if (typeof refresh === "function") refresh();
  }

  /* 拉取世界列表并渲染。
   * 参数：无。
   * 返回：Promise，无。
   */
  async function load() {
    const path = (NW.state && NW.state.path) || "";
    if (!path) {
      worlds = [];
      editingId = null;
      setFlowCount(0);
      render("先打开工程：在上方「工程目录」里选择或新建一个工程。");
      return;
    }
    if (loadedPath !== path) {
      loadedPath = path;
      editingId = null;
      conflicts = {};
      closeWizard();
    }
    try {
      const data = await NW.api(`/api/ui/worlds?${NW.qs({ path })}`);
      worlds = data.worlds || [];
      setFlowCount(Number(data.count) || worlds.length);
      render("");
    } catch (error) {
      worlds = [];
      setFlowCount(0);
      render("读取世界条目失败，请重试。");
      NW.toast(`读取世界条目失败：${error.message}`);
    }
  }

  /* 打开「新增世界」的就地表单。
   * 参数：无。
   * 返回：无。
   */
  function startNew() {
    if (!(NW.state && NW.state.path)) {
      NW.toast("先打开或新建工程");
      return;
    }
    if (editingId === "__new__") return;
    editingId = "__new__";
    const card = { name: "", era: "", summary: "", visual_style: "", rules: [], factions: [], locations: [], glossary: [] };
    // 把新卡片插到列表顶部，避免空列表时无处展开
    if (!worlds.length) {
      worlds = [card];
      render("");
    } else {
      render("");
      const list = el("worldsList");
      if (list) list.insertAdjacentHTML("afterbegin", cardHtml(card));
    }
  }

  /* 保存表单：新增走 world，编辑带 id 走更新。
   * 参数：button 保存按钮；card 世界卡片。
   * 返回：Promise，无。
   */
  async function saveForm(button, card) {
    if (!card) return;
    const payload = readWorldForm(card);
    if (!payload.name) {
      NW.toast("世界名称不能为空");
      return;
    }
    if (!payload.era) {
      NW.toast("时代背景不能为空");
      return;
    }
    const body = { path: NW.state.path, world: payload };
    if (editingId && editingId !== "__new__") body.world.id = editingId;
    setLoading(button, true, "保存中…");
    try {
      await NW.api("/api/ui/worlds", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      NW.toast(editingId === "__new__" ? `已新增世界：${payload.name}` : `已保存世界：${payload.name}`, true);
      editingId = null;
      await load();
    } catch (error) {
      NW.toast(`保存失败：${error.message}`);
    } finally {
      setLoading(button, false);
    }
  }

  /* 删除世界：被章节引用时展示引用列表并提示先解绑。
   * 参数：id 世界 ID；button 删除按钮。
   * 返回：Promise，无。
   */
  async function deleteWorld(id, button) {
    const world = worlds.find((item) => item.id === id);
    if (!world) return;
    if (!window.confirm(`确定删除世界「${world.name || id}」？删除后不可恢复。`)) return;
    setLoading(button, true, "删除中…");
    try {
      // 删除冲突需要读取 detail.chapters，这里直接用 fetch 保留错误负载
      const response = await fetch("/api/ui/worlds/delete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: NW.state.path, id }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        const detail = data.detail && typeof data.detail === "object" ? data.detail : {};
        const chapters = detail.chapters || data.chapters || [];
        if (response.status === 409 || chapters.length) {
          conflicts[id] = chapters;
          render("");
          NW.toast("该世界仍被章节引用，请先到大纲页解绑", false);
          return;
        }
        throw new Error(detail.message || data.message || `HTTP ${response.status}`);
      }
      delete conflicts[id];
      NW.toast(`已删除世界：${world.name || id}`, true);
      if (editingId === id) editingId = null;
      await load();
    } catch (error) {
      NW.toast(error.message || "删除失败");
    } finally {
      setLoading(button, false);
    }
  }

  /* 生成世界封面；已有封面时强制重绘。
   * 参数：button 触发按钮；id 世界 ID。
   * 返回：Promise，无。
   */
  async function generateImage(button, id) {
    setLoading(button, true, "生成中…");
    try {
      await NW.api("/api/ui/worlds/image", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: NW.state.path, id, force: button.dataset.cover === "1" }),
        busy: "正在生成世界封面…",
        busyDetail: "图像生成较慢，请勿关闭页面",
      });
      NW.toast("世界封面已生成", true);
      await load();
    } catch (error) {
      NW.toast(`生成配图失败：${error.message}`);
    } finally {
      setLoading(button, false);
    }
  }

  /* 表单上方的「按需求生成」：一次生成完整世界并回填当前表单。
   * 参数：button 触发按钮。
   * 返回：无。
   */
  function generateForForm(button) {
    const card = button.closest(".world-card");
    if (!card || !window.NWTools) return;
    NWTools.generateBlock({
      section: "world",
      targetId: editingId && editingId !== "__new__" ? editingId : "",
      button,
      fields: readFormText(card),
      onApply(fields) {
        applyGenerated(card, fields);
      },
    });
  }

  /* 关闭生成候选预览浮层。
   * 参数：无。
   * 返回：无。
   */
  function closeWizard() {
    if (!wizardMask) return;
    document.removeEventListener("keydown", onWizardKeydown, true);
    wizardMask.remove();
    wizardMask = null;
    wizardWorlds = [];
  }

  /* 候选预览键盘：Esc 关闭。
   * 参数：event 键盘事件。
   * 返回：无。
   */
  function onWizardKeydown(event) {
    if (event.key !== "Escape") return;
    event.preventDefault();
    closeWizard();
  }

  /* 去掉生成结果里的服务端字段，让 world apply 重新补 id 与时间戳。
   * 参数：world 生成的世界条目。
   * 返回：只保留业务字段的副本。
   */
  function stripWorld(world) {
    const copy = Object.assign({}, world);
    delete copy.id;
    delete copy.created_at;
    delete copy.updated_at;
    delete copy.cover_asset;
    copy.locations = (copy.locations || []).map((item) => ({
      name: item.name || "",
      description: item.description || "",
      atmosphere: item.atmosphere || "",
    }));
    copy.glossary = (copy.glossary || []).map((item) => {
      const entry = { term: item.term || "", meaning: item.meaning || "" };
      if (item.aliases && item.aliases.length) entry.aliases = item.aliases;
      return entry;
    });
    return copy;
  }

  /* 保存勾选的候选世界。
   * 参数：mask 预览浮层；button 保存按钮。
   * 返回：Promise，无。
   */
  async function savePicked(mask, button) {
    const picks = Array.from(mask.querySelectorAll("input[data-world-pick]:checked"));
    if (!picks.length) {
      NW.toast("先勾选至少一条世界");
      return;
    }
    const rows = picks.map((node) => stripWorld(wizardWorlds[Number(node.value)] || {})).filter((item) => item.name);
    if (!rows.length) {
      NW.toast("勾选的世界缺少名称，无法保存");
      return;
    }
    setLoading(button, true, "保存中…");
    try {
      await NW.api("/api/ui/worlds", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: NW.state.path, worlds: rows }),
      });
      NW.toast(`已保存 ${rows.length} 条世界`, true);
      closeWizard();
      editingId = null;
      await load();
    } catch (error) {
      NW.toast(`保存失败：${error.message}`);
    } finally {
      setLoading(button, false);
    }
  }

  /* 打开生成候选预览：名称 / 时代 / 摘要 / 视觉基调，可勾选后保存。
   * 参数：candidates 生成的世界条目列表。
   * 返回：无。
   */
  function openWizard(candidates) {
    closeWizard();
    wizardWorlds = candidates || [];
    if (!wizardWorlds.length) {
      NW.toast("没有生成任何世界，请补充需求后重试");
      return;
    }

    wizardMask = document.createElement("div");
    wizardMask.className = "modal-mask";

    const dialog = document.createElement("div");
    dialog.className = "modal-editor gen-preview";
    dialog.setAttribute("role", "dialog");
    dialog.setAttribute("aria-modal", "true");

    const title = document.createElement("h3");
    title.className = "modal-title";
    title.textContent = `生成世界 · 候选 ${wizardWorlds.length} 条`;
    dialog.appendChild(title);

    const hint = document.createElement("p");
    hint.className = "modal-hint";
    hint.textContent = "勾选要写入世界库的条目；确认后会追加保存，不影响已有世界。";
    dialog.appendChild(hint);

    const list = document.createElement("div");
    list.className = "gen-check-list";

    const countNode = document.createElement("span");
    countNode.className = "gen-field-count";

    wizardWorlds.forEach((world, index) => {
      const row = document.createElement("label");
      row.className = "gen-check";

      const check = document.createElement("input");
      check.type = "checkbox";
      check.checked = true;
      check.value = String(index);
      check.setAttribute("data-world-pick", "");
      row.appendChild(check);

      const body = document.createElement("div");
      body.className = "gen-check-body";

      const head = document.createElement("div");
      head.className = "gen-check-head";
      const nameNode = document.createElement("span");
      nameNode.className = "gen-check-name";
      nameNode.textContent = world.name || "未命名世界";
      head.appendChild(nameNode);
      const eraNode = document.createElement("span");
      eraNode.className = "muted";
      eraNode.textContent = world.era || "时代未注明";
      head.appendChild(eraNode);
      body.appendChild(head);

      const summary = document.createElement("p");
      summary.textContent = world.summary || "—";
      body.appendChild(summary);

      if (world.visual_style) {
        const visual = document.createElement("span");
        visual.className = "muted";
        visual.textContent = `视觉：${world.visual_style}`;
        body.appendChild(visual);
      }

      row.appendChild(body);
      list.appendChild(row);
    });

    dialog.appendChild(list);

    const foot = document.createElement("div");
    foot.className = "modal-foot";

    /* 同步「已选 N 条」。
     * 参数：无。
     * 返回：无。
     */
    const syncCount = () => {
      countNode.textContent = `已选 ${list.querySelectorAll("input[data-world-pick]:checked").length} 条`;
    };
    list.addEventListener("change", syncCount);
    syncCount();
    foot.appendChild(countNode);

    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "btn";
    cancel.textContent = "取消";
    cancel.addEventListener("click", closeWizard);
    foot.appendChild(cancel);

    const save = document.createElement("button");
    save.type = "button";
    save.className = "btn primary";
    save.textContent = "保存勾选";
    save.addEventListener("click", () => savePicked(wizardMask, save));
    foot.appendChild(save);

    dialog.appendChild(foot);
    wizardMask.appendChild(dialog);
    wizardMask.addEventListener("click", (event) => {
      if (event.target === wizardMask) closeWizard();
    });
    document.body.appendChild(wizardMask);
    document.addEventListener("keydown", onWizardKeydown, true);
  }

  /* 生成世界：先收集需求，再预览候选。
   * 参数：button 触发按钮。
   * 返回：Promise，无。
   */
  async function generateWorlds(button) {
    if (!(NW.state && NW.state.path)) {
      NW.toast("先打开或新建工程");
      return;
    }
    if (!window.NWEditor) return;
    NWEditor.openBrief({
      title: "生成世界需求",
      placeholder: "如：末日后的浮空城，蒸汽机械与旧神并存",
      onSave: async (brief) => {
        setLoading(button, true, "生成中…");
        try {
          const result = await NW.api("/api/ui/worlds/generate", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ path: NW.state.path, brief }),
            busy: "正在生成世界设定…",
            busyDetail: "数量较多时分批生成",
          });
          openWizard(result.worlds || []);
        } catch (error) {
          NW.toast(`生成世界失败：${error.message}`);
        } finally {
          setLoading(button, false);
        }
      },
    });
  }

  /* 列表点击分发：编辑 / 取消 / 保存 / 生成 / 删除 / 配图。
   * 参数：event 点击事件。
   * 返回：无。
   */
  function onListClick(event) {
    const trigger = event.target.closest("[data-world-act]");
    if (!trigger) return;
    const act = trigger.dataset.worldAct;
    const id = trigger.dataset.id || "";
    const card = trigger.closest(".world-card");
    if (act === "edit") {
      editingId = id;
      render("");
      return;
    }
    if (act === "cancel") {
      editingId = null;
      // 新增时取消要移除临时卡片
      if (worlds.length && !worlds[0].id) {
        worlds = worlds.filter((item) => item.id);
        setFlowCount(worlds.length);
      }
      render("");
      return;
    }
    if (act === "save") saveForm(trigger, card);
    else if (act === "generate") generateForForm(trigger);
    else if (act === "delete") deleteWorld(id, trigger);
    else if (act === "image") generateImage(trigger, id);
  }

  /* 首次激活时绑定事件，避免重复绑定。
   * 参数：无。
   * 返回：无。
   */
  function bind() {
    if (bound) return;
    bound = true;
    const add = el("worldsAdd");
    if (add) add.addEventListener("click", startNew);
    const generate = el("worldsGenerate");
    if (generate) generate.addEventListener("click", () => generateWorlds(generate));
    const list = el("worldsList");
    if (list) list.addEventListener("click", onListClick);
    if (window.NWTools) NWTools.bindAmplify(document);
    NW.onProject(() => load());
  }

  window.Pages.worlds = {
    init() {
      bind();
    },
    activate() {
      bind();
      load();
    },
  };
})();
