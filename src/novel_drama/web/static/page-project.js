/* 项目页：极简新建 + 创作向导（作品信息 / 世界 / 梗概 / 文风 / 人物）。
 *
 * 取数纪律：
 *   - 读取走 `/api/project/info`、`/api/ui/setting`、`/api/ui/project/work`、`/api/ui/characters`。
 *   - 生成 / 润色只回填表单，不落盘；只有显式「保存 / 加入 / 全部保存」才写盘。
 *   - 人物增删改只重拉人物表，不整页重载，避免冲掉用户还没保存的设定草稿。
 */

(function () {
  /* 可编辑的标量字段：key 与 `setting set` 的参数一一对应 */
  const FIELDS = [
    ["logline", "一句话概要", "premise"],
    ["theme", "主题", "premise"],
    ["core_conflict", "核心冲突", "premise"],
    ["ending_intent", "结局意图", "premise"],
    ["target_audience", "目标读者", "premise"],
    ["target_length", "目标篇幅", "premise"],
    ["era", "时代", "world"],
    ["tone", "基调", "style"],
    ["pov", "视角", "style"],
    ["tense", "时态", "style"],
    ["prose_style", "文风", "style"],
    ["visual_style", "视觉风格", "style"],
  ];

  /* 字段 key → 中文名：生成结果里被保留（skipped）的字段用它提示用户 */
  const FIELD_LABELS = Object.fromEntries(FIELDS.map(([key, label]) => [key, label]));
  ["rules", "factions", "locations", "glossary"].forEach((key) => {
    FIELD_LABELS[key] = { rules: "规则", factions: "阵营", locations: "地点", glossary: "词条" }[key];
  });

  /* 三个分区各自的表单容器，顺序与页面上的卡片一致 */
  const SECTION_INPUTS = { world: "pjWorldFields", premise: "pjPremiseFields", style: "pjStyleFields" };
  const SECTION_LABELS = { world: "世界设计", premise: "故事梗概", style: "文风设定" };

  /* 枚举字段的中文标签映射：界面显示中文，写回仍是 schema 要求的英文枚举值。 */
  const ENUMS = {
    target_length: { short: "短篇", novella: "中篇", novel: "长篇", series: "系列" },
    pov: { first: "第一人称", third_limited: "第三人称·有限", third_omniscient: "第三人称·全知", second: "第二人称" },
    tense: { past: "过去时", present: "现在时" },
  };

  /* 人物定位 / 性别的中文名（写回仍是英文枚举） */
  const ROLE_LABELS = { protagonist: "主角", antagonist: "反派", supporting: "配角", minor: "龙套" };
  const GENDER_LABELS = { male: "男", female: "女", other: "其他", unknown: "性别未知" };

  let bound = false;
  let busy = false;
  let bibleCache = null;
  let lastPath = "";
  let worldDraft = null;        // 生成的世界设定（规则/阵营/地点/词条），保存前只存在内存
  let characters = [];          // 已落盘人物
  let pendingCharacters = [];   // 「✨ 生成人物」的待保存结果
  let editingId = null;         // 正在就地编辑的人物 ID；"__new__" 表示新增

  const el = (id) => document.getElementById(id);

  /* 一行「标签 → 值」的展示结构。 */
  function row(label, value) {
    return `<div class="kv-row"><span class="kv-key">${esc(label)}</span><span class="kv-val">${esc(value)}</span></div>`;
  }

  /* 按逗号 / 顿号 / 分号把输入框拆成列表。 */
  function splitList(text) {
    return String(text || "")
      .split(/[、,，;；]+/)
      .map((item) => item.trim())
      .filter(Boolean);
  }

  /* 背景摘要：把换行压成空格并截断，卡片里只展示一句。 */
  function summary(text, limit = 120) {
    const value = String(text || "").replace(/\s+/g, " ").trim();
    if (!value) return "—";
    return value.length > limit ? `${value.slice(0, limit)}…` : value;
  }

  /* 按钮 loading：进入时记住原文并禁用，结束后恢复。 */
  function setButtonLoading(button, loading, text) {
    if (!button) return;
    if (loading) {
      if (!button.dataset.loadingLabel) button.dataset.loadingLabel = button.textContent;
      button.textContent = text || "生成中…";
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

  /* 渲染工程概览。 */
  function renderInfo(info, bible) {
    const work = info.work || {};
    el("pjInfo").innerHTML = [
      row("工程", info.root),
      row("书名", work.title || "—"),
      row("作者", work.author || "—"),
      row("题材", (work.genre || []).join("、") || "—"),
      row("章节", `${(info.chapters || []).length} 章`),
      row("角色", `${info.characters || 0} 位`),
      row("设定库", `r${bible.settings_revision ?? "?"}（${bible.updated_at || ""}）`),
    ].join("");
  }

  /* 作品信息回填：书名 / 作者 / 题材（题材用逗号分隔展示）。 */
  function renderWork(work) {
    const data = work || {};
    el("pjWorkTitle").value = data.title || "";
    el("pjWorkAuthor").value = data.author || "";
    el("pjWorkGenres").value = (data.genres || []).join("，");
  }

  /* 单个设定字段：文本框带「✨润色」，枚举字段是下拉框（可选值固定，润色按钮置灰）。 */
  function fieldHtml(key, label, value) {
    const labels = ENUMS[key];
    if (labels) {
      /* 值不在枚举里时保留为一条「未识别」选项，避免静默丢数据 */
      const extra = value && !(value in labels)
        ? `<option value="${esc(value)}" selected>${esc(value)}（未识别）</option>`
        : "";
      const items = Object.entries(labels)
        .map(([val, text]) => `<option value="${esc(val)}"${val === value ? " selected" : ""}>${esc(text)}</option>`)
        .join("");
      return `<div class="stack">${esc(label)}
        <span class="field-action">
          <select data-field="${esc(key)}">${extra}${items}</select>
          <button class="btn small polish-btn" type="button" disabled title="枚举字段可选值固定，无需润色">✨润色</button>
        </span>
      </div>`;
    }
    return `<div class="stack">${esc(label)}
      <span class="field-action">
        <input data-field="${esc(key)}" value="${esc(value)}" />
        <button class="btn small polish-btn" type="button" data-polish="${esc(key)}">✨润色</button>
      </span>
    </div>`;
  }

  /* 渲染设定库三个分区（世界 / 梗概 / 文风），data-field 保持原 key。 */
  function renderSettings(bible) {
    const groups = { world: [], premise: [], style: [] };
    FIELDS.forEach(([key, label, section]) => {
      const value = (bible[section] || {})[key] ?? "";
      groups[section].push(fieldHtml(key, label, value));
    });
    Object.entries(SECTION_INPUTS).forEach(([section, id]) => {
      el(id).innerHTML = groups[section].join("");
    });
    /* 禁止写法是 style 里的数组字段，单独渲染成逗号分隔输入框 */
    const forbidden = ((bible.style || {}).forbidden || []).join("，");
    el("pjForbidden").value = forbidden;
  }

  /* 渲染世界设定：规则/阵营/地点/词条。 */
  function renderWorld(world) {
    const rules = (world.rules || []).map((item) => `<li>${esc(item)}</li>`).join("") || "<li class='muted'>—</li>";
    const factions = (world.factions || []).map((item) => `<li>${esc(item)}</li>`).join("") || "<li class='muted'>—</li>";
    const locations =
      (world.locations || [])
        .map((item) => `<li>${esc(item.name)}　${esc(item.description || "")}${item.atmosphere ? `（${esc(item.atmosphere)}）` : ""}</li>`)
        .join("") || "<li class='muted'>—</li>";
    const glossary =
      (world.glossary || []).map((item) => `<li>${esc(item.term)}：${esc(item.meaning)}</li>`).join("") || "<li class='muted'>—</li>";
    el("pjWorld").innerHTML = `
      <div><h4>规则</h4><ul>${rules}</ul></div>
      <div><h4>阵营</h4><ul>${factions}</ul></div>
      <div><h4>地点</h4><ul>${locations}</ul></div>
      <div><h4>词条</h4><ul>${glossary}</ul></div>`;
  }

  /* 一条生成草稿分组（标题 + 列表）。 */
  function draftGroup(title, items) {
    const list = (items || []).map((item) => `<li>${esc(item)}</li>`).join("") || "<li class='muted'>—</li>";
    return `<div><h4>${esc(title)}</h4><ul>${list}</ul></div>`;
  }

  /* 渲染世界生成草稿：只回填展示 + 提供显式「保存世界设定」。 */
  function renderWorldDraft() {
    const box = el("pjWorldDraft");
    const saveButton = el("pjSaveWorld");
    if (!worldDraft) {
      box.hidden = true;
      box.innerHTML = "";
      saveButton.hidden = true;
      return;
    }
    const locations = (worldDraft.locations || []).map(
      (item) => `${item.name || ""}${item.description ? `：${item.description}` : ""}${item.atmosphere ? `（${item.atmosphere}）` : ""}`
    );
    const glossary = (worldDraft.glossary || []).map((item) => `${item.term || ""}：${item.meaning || ""}`);
    box.innerHTML = [
      draftGroup("规则", worldDraft.rules),
      draftGroup("阵营", worldDraft.factions),
      draftGroup("地点", locations),
      draftGroup("词条", glossary),
    ].join("");
    box.hidden = false;
    saveButton.hidden = false;
  }

  /* 拉取并渲染整个页面。 */
  async function load() {
    const path = NW.state.path;
    if (!path) {
      el("pjInfo").textContent = "先在上方填工程目录并点「打开」";
      el("pjCharList").innerHTML = "<p class='muted'>先打开工程。</p>";
      return;
    }
    /* 切工程时清掉上一本的生成草稿与编辑态，避免串到新工程 */
    if (lastPath !== path) {
      lastPath = path;
      worldDraft = null;
      pendingCharacters = [];
      editingId = null;
      renderWorldDraft();
      renderPending();
    }
    try {
      const [info, bible, work] = await Promise.all([
        NW.api(`/api/project/info?${NW.qs({ path })}`),
        NW.api(`/api/ui/setting?${NW.qs({ path })}`),
        NW.api(`/api/ui/project/work?${NW.qs({ path })}`),
      ]);
      bibleCache = bible;
      renderInfo(info, bible);
      renderWork(work);
      renderSettings(bible);
      renderWorld(bible.world || {});
      await loadCharacters();
    } catch (error) {
      NW.toast(`读取工程失败：${error.message}`);
    }
  }

  /* 保存作品信息（书名 / 作者 / 题材）。 */
  async function saveWork() {
    if (!NW.state.path) {
      NW.toast("先打开或新建工程");
      return;
    }
    const title = el("pjWorkTitle").value.trim();
    if (!title) {
      NW.toast("书名不能为空");
      return;
    }
    const button = el("pjWorkSave");
    setButtonLoading(button, true, "保存中…");
    try {
      await NW.api("/api/ui/project/work", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          path: NW.state.path,
          title,
          author: el("pjWorkAuthor").value.trim(),
          genres: splitList(el("pjWorkGenres").value),
        }),
      });
      NW.toast("作品信息已保存", true);
      /* 只刷新概览，不整页重载，避免冲掉设定库里还没保存的草稿 */
      const info = await NW.api(`/api/project/info?${NW.qs({ path: NW.state.path })}`);
      renderInfo(info, bibleCache || {});
      if (NW.refreshProjects) await NW.refreshProjects();
    } catch (error) {
      NW.toast(`保存失败（退出码 ${error.code}）：${error.message}`);
    } finally {
      setButtonLoading(button, false);
    }
  }

  /* 保存三个分区的标量设定（12 个字段一次提交）。 */
  async function saveSettings() {
    if (busy) return;
    if (!NW.state.path) {
      NW.toast("先打开或新建工程");
      return;
    }
    // 表单还没渲染出来（刚切页/刚建工程）时没有可提交字段，直接拦下，
    // 否则后端只会回「没有要修改的字段（退出码 2）」，让人误以为是数据问题
    const inputs = el("pjSettings").querySelectorAll("[data-field]");
    if (!inputs.length) {
      NW.toast("设定库还没加载完，稍后再试");
      return;
    }
    const fields = {};
    inputs.forEach((input) => {
      fields[input.dataset.field] = input.value.trim();
    });
    busy = true;
    try {
      const result = await NW.api("/api/ui/setting/set", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: NW.state.path, fields }),
      });
      /* forbidden 是数组字段，setting set 不接收，单独走 apply 落盘 */
      const applied = await NW.api("/api/ui/setting/apply", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          path: NW.state.path,
          section: "style",
          fields: { forbidden: splitList(el("pjForbidden").value) },
        }),
      });
      const revision = applied.settings_revision || result.settings_revision;
      NW.toast(`已保存：设定库 r${revision}`, true);
      await load();
    } catch (error) {
      NW.toast(`保存失败（退出码 ${error.code}）：${error.message}`);
    } finally {
      busy = false;
    }
  }

  /* 保存世界生成草稿（规则/阵营/地点/词条 + 当前时代），显式写盘。 */
  async function saveWorldDraft() {
    if (busy || !NW.state.path || !worldDraft) return;
    const eraInput = el("pjSettings").querySelector('[data-field="era"]');
    const fields = {
      era: eraInput ? eraInput.value.trim() : "",
      rules: worldDraft.rules || [],
      factions: worldDraft.factions || [],
      locations: worldDraft.locations || [],
      glossary: worldDraft.glossary || [],
    };
    busy = true;
    setButtonLoading(el("pjSaveWorld"), true, "保存中…");
    try {
      const result = await NW.api("/api/ui/setting/apply", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: NW.state.path, section: "world", fields }),
      });
      worldDraft = null;
      renderWorldDraft();
      NW.toast(`世界设定已保存 r${result.settings_revision}`, true);
      /* 只重拉设定库并重画世界现状，不动用户其它输入 */
      const bible = await NW.api(`/api/ui/setting?${NW.qs({ path: NW.state.path })}`);
      bibleCache = bible;
      renderWorld(bible.world || {});
    } catch (error) {
      NW.toast(`保存失败（退出码 ${error.code}）：${error.message}`);
    } finally {
      busy = false;
      setButtonLoading(el("pjSaveWorld"), false);
    }
  }

  /* 增补世界设定。 */
  async function addWorld() {
    if (busy || !NW.state.path) return;
    const body = {
      path: NW.state.path,
      rules: [el("wRule").value.trim()].filter(Boolean),
      factions: [el("wFaction").value.trim()].filter(Boolean),
      location: el("wLocation").value.trim(),
      glossary: el("wGlossary").value.trim(),
    };
    if (!body.rules.length && !body.factions.length && !body.location && !body.glossary) {
      NW.toast("先填一条要加入的内容");
      return;
    }
    busy = true;
    try {
      const result = await NW.api("/api/ui/setting/world", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      ["wRule", "wFaction", "wLocation", "wGlossary"].forEach((id) => {
        el(id).value = "";
      });
      NW.toast(`已更新世界设定 r${result.settings_revision}`, true);
      await load();
    } catch (error) {
      NW.toast(`写入失败（退出码 ${error.code}）：${error.message}`);
    } finally {
      busy = false;
    }
  }

  /* 把设定库导出成本地 JSON 文件（纯前端下载，不额外加接口）。 */
  async function exportBible() {
    if (!NW.state.path) return;
    try {
      const bible = await NW.api(`/api/ui/setting?${NW.qs({ path: NW.state.path })}`);
      const blob = new Blob([JSON.stringify(bible, null, 2)], { type: "application/json" });
      const link = document.createElement("a");
      link.href = URL.createObjectURL(blob);
      link.download = "setting-bible.json";
      link.click();
      URL.revokeObjectURL(link.href);
    } catch (error) {
      NW.toast(`导出失败：${error.message}`);
    }
  }

  /* 当前分区是否已有非空内容：世界区还要算上规则/阵营/地点/词条。 */
  function sectionHasContent(section) {
    const box = el(SECTION_INPUTS[section]);
    const filled = FIELDS.filter((item) => item[2] === section).some(([key]) => {
      const input = box.querySelector(`[data-field="${key}"]`);
      return Boolean(input && input.value.trim());
    });
    if (filled) return true;
    if (section !== "world") return false;
    const world = (bibleCache || {}).world || {};
    return Boolean(
      (world.rules || []).length ||
      (world.factions || []).length ||
      (world.locations || []).length ||
      (world.glossary || []).length
    );
  }

  /* 把生成值写进对应输入框 / 世界草稿（只回填，不落盘）。 */
  function applyGenerated(step, fields) {
    if (step === "world") {
      if (fields.era !== undefined) setFieldValue("era", fields.era);
      worldDraft = {
        rules: fields.rules || [],
        factions: fields.factions || [],
        locations: fields.locations || [],
        glossary: fields.glossary || [],
      };
      renderWorldDraft();
      return;
    }
    FIELDS.filter((item) => item[2] === step).forEach(([key]) => {
      if (fields[key] !== undefined) setFieldValue(key, fields[key]);
    });
    /* 文风生成的 forbidden 是数组，回填到逗号分隔输入框 */
    if (step === "style" && Array.isArray(fields.forbidden)) {
      el("pjForbidden").value = fields.forbidden.join("，");
    }
  }

  /* 按 data-field 回填单个字段；枚举值不在选项里时保持原值不动。 */
  function setFieldValue(key, value) {
    const input = el("pjSettings").querySelector(`[data-field="${key}"]`);
    if (!input) return;
    const text = value === null || value === undefined ? "" : String(value);
    if (input.tagName === "SELECT") {
      if (Array.from(input.options).some((option) => option.value === text)) input.value = text;
      return;
    }
    input.value = text;
  }

  /* 调 `/api/ui/setting/generate`：已有内容时先问「保留 / 覆盖」，结果只回填。 */
  async function generateSection(step, button) {
    if (!NW.state.path) {
      NW.toast("先打开或新建工程");
      return;
    }
    const label = SECTION_LABELS[step];
    let overwrite = false;
    if (sectionHasContent(step)) {
      overwrite = window.confirm(
        `「${label}」已有内容。\n\n点「确定」= 覆盖重写（本次生成会替换已有内容）\n点「取消」= 保留已有内容（只补空字段）`
      );
    }
    setButtonLoading(button, true, "生成中…");
    try {
      const result = await NW.api("/api/ui/setting/generate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: NW.state.path, step, overwrite }),
      });
      applyGenerated(step, result.fields || {});
      const skipped = result.skipped || [];
      if (skipped.length) {
        const names = skipped.map((key) => FIELD_LABELS[key] || key).join("、");
        NW.toast(`已保留已有字段：${names}；其余已回填，确认后点保存`);
      } else {
        NW.toast(`${label}已回填，确认后点保存`, true);
      }
    } catch (error) {
      NW.toast(`生成失败：${error.message}`);
    } finally {
      setButtonLoading(button, false);
    }
  }

  /* 单字段润色：只回填输入框，不写盘。 */
  async function polishField(button) {
    if (!NW.state.path) {
      NW.toast("先打开或新建工程");
      return;
    }
    const field = button.dataset.polish;
    let input = button.dataset.polishInput ? el(button.dataset.polishInput) : null;
    if (!input) {
      const holder = button.closest(".field-action");
      input = holder ? holder.querySelector("input, textarea") : null;
    }
    if (!input) return;
    const text = input.value.trim();
    if (!text) {
      NW.toast("先填写内容再润色");
      return;
    }
    const body = { path: NW.state.path, field, text };
    if (field.startsWith("character.")) body.character = button.dataset.character;
    setButtonLoading(button, true, "润色中…");
    try {
      const result = await NW.api("/api/ui/setting/polish", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (result.text) input.value = result.text;
      NW.toast("润色完成（未保存）", true);
    } catch (error) {
      NW.toast(`润色失败：${error.message}`);
    } finally {
      setButtonLoading(button, false);
    }
  }

  /* 人物定位徽标：主角 / 反派 / 配角 / 龙套用不同颜色区分。 */
  function roleBadge(role) {
    const key = ROLE_LABELS[role] ? role : "supporting";
    return `<span class="char-badge role-${esc(key)}">${esc(ROLE_LABELS[key])}</span>`;
  }

  /* 人物编辑表单（就地展开，字段名与 PATCH 接口一致）。 */
  function characterEditHtml(item) {
    const roleOptions = Object.entries(ROLE_LABELS)
      .map(([value, label]) => `<option value="${esc(value)}"${value === (item.role || "supporting") ? " selected" : ""}>${esc(label)}</option>`)
      .join("");
    const genderOptions = Object.entries(GENDER_LABELS)
      .map(([value, label]) => `<option value="${esc(value)}"${value === (item.gender || "unknown") ? " selected" : ""}>${esc(label)}</option>`)
      .join("");
    return `<div class="char-edit">
      <div class="form-grid">
        <label class="stack">姓名<input data-edit="name" value="${esc(item.name || "")}" /></label>
        <label class="stack">定位<select data-edit="role">${roleOptions}</select></label>
        <label class="stack">性别<select data-edit="gender">${genderOptions}</select></label>
        <label class="stack">年龄区间<input data-edit="age_range" value="${esc(item.age_range || "")}" /></label>
        <label class="stack">性格（逗号分隔）<input data-edit="personality" value="${esc((item.personality || []).join("、"))}" /></label>
        <div class="stack span-all">小传
          <span class="field-action">
            <textarea data-edit="background" rows="3">${esc(item.background || "")}</textarea>
            <button class="btn small polish-btn" type="button" data-polish="character.background" data-character="${esc(item.id || "")}">✨润色</button>
          </span>
        </div>
      </div>
      <div class="actions">
        <button class="btn primary" type="button" data-char-act="save">保存</button>
        <button class="btn" type="button" data-char-act="cancel">取消</button>
      </div>
    </div>`;
  }

  /* 一张人物卡片：姓名 / 定位 / 性别年龄 / 性格 / 背景摘要，编辑时附表单。 */
  function characterCardHtml(item, editing, pending = false) {
    const id = item.id || "";
    return `<article class="char-card${pending ? " pending" : ""}" data-id="${esc(id)}">
      <div class="char-head">
        <span class="char-name">${esc(item.name || "（未命名）")}</span>
        ${roleBadge(item.role)}
        <span class="char-sub muted">${esc(GENDER_LABELS[item.gender] || "性别未知")} · ${esc(item.age_range || "年龄未知")}</span>
        ${id
          ? `<span class="char-actions">
              <button class="btn small" type="button" data-char-act="edit" data-char-id="${esc(id)}">编辑</button>
              <button class="btn small" type="button" data-char-act="delete" data-char-id="${esc(id)}">删除</button>
            </span>`
          : ""}
      </div>
      <div class="char-meta"><span class="muted">性格</span>${esc((item.personality || []).join("、") || "—")}</div>
      <div class="char-bg"><span class="muted">背景</span>${esc(summary(item.background))}</div>
      ${editing ? characterEditHtml(item) : ""}
    </article>`;
  }

  /* 渲染已保存人物（旁白是系统角色，不在这里编辑）。 */
  function renderCharacters() {
    const box = el("pjCharList");
    const cards = characters
      .filter((item) => item.role !== "narrator")
      .map((item) => characterCardHtml(item, editingId === item.id));
    if (editingId === "__new__") {
      cards.unshift(characterCardHtml({ name: "", role: "supporting", gender: "unknown", personality: [], background: "" }, true, true));
    }
    box.innerHTML = cards.length ? cards.join("") : "<p class='muted'>还没有人物：点「✨ 生成人物」或「＋ 新增人物」。</p>";
  }

  /* 读取人物表。 */
  async function loadCharacters() {
    if (!NW.state.path) {
      characters = [];
      renderCharacters();
      return;
    }
    try {
      const data = await NW.api(`/api/ui/characters?${NW.qs({ path: NW.state.path })}`);
      characters = data.characters || [];
    } catch (error) {
      characters = [];
      NW.toast(`读取人物失败：${error.message}`);
    }
    renderCharacters();
  }

  /* 打开「新增人物」的就地表单。 */
  function startAddCharacter() {
    if (!NW.state.path) {
      NW.toast("先打开或新建工程");
      return;
    }
    editingId = "__new__";
    renderCharacters();
  }

  /* 读取某张人物卡片里的编辑表单值。 */
  function readCharacterForm(card) {
    const read = (name) => {
      const node = card.querySelector(`[data-edit="${name}"]`);
      return node ? node.value.trim() : "";
    };
    return {
      name: read("name"),
      role: read("role") || "supporting",
      gender: read("gender") || "unknown",
      age_range: read("age_range"),
      personality: splitList(read("personality")),
      background: read("background"),
    };
  }

  /* 保存人物：新增走 POST 批量追加（单条），编辑走 PATCH。 */
  async function saveCharacter(button) {
    if (!NW.state.path) return;
    const card = button.closest(".char-card");
    if (!card) return;
    const payload = readCharacterForm(card);
    if (!payload.name) {
      NW.toast("人物姓名不能为空");
      return;
    }
    try {
      if (editingId === "__new__") {
        await NW.api("/api/ui/characters", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: NW.state.path, characters: [payload] }),
        });
        NW.toast(`已新增人物：${payload.name}`, true);
      } else {
        await NW.api("/api/ui/characters", {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: NW.state.path, id: editingId, ...payload }),
        });
        NW.toast(`已更新人物：${payload.name}`, true);
      }
      editingId = null;
      await loadCharacters();
    } catch (error) {
      NW.toast(`保存人物失败：${error.message}`);
    }
  }

  /* 删除人物：确认后调 DELETE（后端会拒绝删除旁白）。 */
  async function deleteCharacter(id) {
    const target = characters.find((item) => item.id === id);
    if (!target) return;
    if (!window.confirm(`确定删除人物「${target.name || id}」？删除后不可恢复。`)) return;
    try {
      await NW.api("/api/ui/characters/delete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: NW.state.path, id }),
      });
      NW.toast(`已删除人物：${target.name || id}`, true);
      if (editingId === id) editingId = null;
      await loadCharacters();
    } catch (error) {
      NW.toast(`删除失败：${error.message}`);
    }
  }

  /* 人物卡片上的动作分发（编辑 / 取消 / 保存 / 删除）。 */
  async function handleCharacterAction(button) {
    const action = button.dataset.charAct;
    if (action === "edit") {
      editingId = button.dataset.charId;
      renderCharacters();
      return;
    }
    if (action === "cancel") {
      editingId = null;
      renderCharacters();
      return;
    }
    if (action === "delete") {
      await deleteCharacter(button.dataset.charId);
      return;
    }
    if (action === "save") await saveCharacter(button);
  }

  /* 渲染「待保存」人物列表：生成结果先放这里，点「全部保存」才写盘。 */
  function renderPending() {
    const box = el("pjCharPending");
    const saveButton = el("pjCharSaveAll");
    if (!pendingCharacters.length) {
      box.hidden = true;
      box.innerHTML = "";
      saveButton.hidden = true;
      return;
    }
    box.innerHTML =
      `<p class="muted pending-title">待保存（${pendingCharacters.length} 人，确认后再写盘）</p>` +
      pendingCharacters.map((item) => characterCardHtml(item, false, true)).join("");
    box.hidden = false;
    saveButton.hidden = false;
  }

  /* 生成人物（预览）：结果只进「待保存」列表。 */
  async function generateCharacters() {
    if (!NW.state.path) {
      NW.toast("先打开或新建工程");
      return;
    }
    const button = el("pjCharGenerate");
    setButtonLoading(button, true, "生成中…");
    try {
      const result = await NW.api("/api/ui/characters/generate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: NW.state.path }),
      });
      pendingCharacters = result.characters || [];
      renderPending();
      NW.toast(`已生成 ${pendingCharacters.length} 个人物（待保存）`, true);
    } catch (error) {
      NW.toast(`生成人物失败：${error.message}`);
    } finally {
      setButtonLoading(button, false);
    }
  }

  /* 全部保存：把待保存列表批量追加到 characters.json。 */
  async function saveAllPending() {
    if (!NW.state.path || !pendingCharacters.length) return;
    const button = el("pjCharSaveAll");
    setButtonLoading(button, true, "保存中…");
    try {
      const result = await NW.api("/api/ui/characters", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: NW.state.path, characters: pendingCharacters }),
      });
      const created = (result.characters || []).length;
      const skipped = result.skipped || [];
      NW.toast(`已写入 ${created} 个人物${skipped.length ? `（重名跳过：${skipped.join("、")}）` : ""}`, true);
      pendingCharacters = [];
      renderPending();
      await loadCharacters();
    } catch (error) {
      NW.toast(`保存人物失败：${error.message}`);
    } finally {
      setButtonLoading(button, false);
    }
  }

  /* 新建工程并切过去：只提交目录与书名。 */
  async function createProject() {
    if (busy) return;
    const dir = el("npDir").value.trim();
    const title = el("npTitle").value.trim();
    if (!dir || !title) {
      NW.toast("目录与书名都要填");
      return;
    }
    busy = true;
    try {
      const result = await NW.api("/api/ui/project/init", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ dir, title }),
      });
      NW.state.path = result.root || dir;
      localStorage.setItem("ndw.path", NW.state.path);
      el("projectPath").value = NW.state.path;
      el("npDir").value = "";
      el("npTitle").value = "";
      NW.toast("工程已创建，下一步：生成世界设定", true);
      // 新工程没有章节，openProject 只广播 project 事件；再显式 load 一次，
      // 保证设定库表单一定按新工程重渲染（否则可能还是空表，点保存就报错）
      await NW.openProject();
      await load();
      if (NW.refreshProjects) await NW.refreshProjects();
    } catch (error) {
      NW.toast(`创建失败（退出码 ${error.code}）：${error.message}`);
    } finally {
      busy = false;
    }
  }

  /* 项目页统一点击分发：润色按钮与人物卡片动作都在这里收口（内容动态渲染也能用）。 */
  function onViewClick(event) {
    const polish = event.target.closest("[data-polish]");
    if (polish) {
      polishField(polish);
      return;
    }
    const action = event.target.closest("[data-char-act]");
    if (action) handleCharacterAction(action);
  }

  /* 首次激活时绑定事件，避免重复绑定。 */
  function bind() {
    if (bound) return;
    bound = true;
    el("pjSave").addEventListener("click", saveSettings);
    el("pjWorldAdd").addEventListener("click", addWorld);
    el("pjExport").addEventListener("click", exportBible);
    el("npCreate").addEventListener("click", createProject);
    el("pjWorkSave").addEventListener("click", saveWork);
    el("pjSaveWorld").addEventListener("click", saveWorldDraft);
    el("pjGenWorld").addEventListener("click", () => generateSection("world", el("pjGenWorld")));
    el("pjGenPremise").addEventListener("click", () => generateSection("premise", el("pjGenPremise")));
    el("pjGenStyle").addEventListener("click", () => generateSection("style", el("pjGenStyle")));
    el("pjCharAdd").addEventListener("click", startAddCharacter);
    el("pjCharGenerate").addEventListener("click", generateCharacters);
    el("pjCharSaveAll").addEventListener("click", saveAllPending);
    el("view-project").addEventListener("click", onViewClick);
    NW.onProject(() => load());
  }

  window.Pages.project = {
    activate() {
      bind();
      load();
    },
  };
})();
