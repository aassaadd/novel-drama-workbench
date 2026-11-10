/* 项目页：极简新建 + 创作向导（作品信息 / 梗概 / 文风）。
 *
 * 取数纪律：
 *   - 读取走 `/api/project/info`、`/api/ui/setting`、`/api/ui/project/work`。
 *   - 生成 / 润色只回填表单，不落盘；只有显式「保存」才写盘。
 *   - 世界与人物已拆到各自页面（page-worlds.js / page-characters.js），本页不再读写。
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
    ["tone", "基调", "style"],
    ["pov", "视角", "style"],
    ["tense", "时态", "style"],
    ["prose_style", "文风", "style"],
    ["visual_style", "视觉风格", "style"],
  ];

  /* 两个分区各自的表单容器，顺序与页面上的卡片一致 */
  const SECTION_INPUTS = { premise: "pjPremiseFields", style: "pjStyleFields" };

  /* 枚举字段的中文标签映射：界面显示中文，写回仍是 schema 要求的英文枚举值。 */
  const ENUMS = {
    target_length: { short: "短篇", novella: "中篇", novel: "长篇", series: "系列" },
    pov: { first: "第一人称", third_limited: "第三人称·有限", third_omniscient: "第三人称·全知", second: "第二人称" },
    tense: { past: "过去时", present: "现在时" },
  };

  let bound = false;
  let busy = false;
  let bibleCache = null;
  let stylePresets = []; // 固定画面风格预设（写实 / 动漫 / 古风…），图片与视频共用

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

  /* 放大编辑按钮：与润色按钮同尺寸，点击后由 NWTools.bindAmplify 处理。 */
  function amplifyButton(title) {
    return `<button class="btn small amplify-btn" type="button" data-amplify data-title="${esc(title)}" title="放大编辑">⤢</button>`;
  }

  /* 单个设定字段：文本框带「✨润色」与「⤢ 放大」，枚举字段是下拉框（可选值固定，润色按钮置灰）。 */
  function fieldHtml(key, label, value) {
    const labels = ENUMS[key];
    if (key === "visual_style") {
      /* 视觉风格：预设下拉 + 可编辑文本框。选中预设即把风格词写入输入框，
       * 图片（母图 / 头像 / 三视图）与视频生成都读这一份，保证统一。 */
      const extra = value && !stylePresets.some((item) => item.prompt === value)
        ? `<option value="${esc(value)}" selected>自定义（当前值）</option>`
        : "";
      const items = stylePresets
        .map((item) => `<option value="${esc(item.prompt)}"${item.prompt === value ? " selected" : ""}>${esc(item.label)}</option>`)
        .join("");
      return `<div class="stack">${esc(label)}
        <span class="field-action">
          <select data-style-preset>${extra}<option value="">自定义 / 未选</option>${items}</select>
        </span>
        <span class="field-action">
          <input data-field="${esc(key)}" value="${esc(value)}" placeholder="选上方预设，或直接填写风格词" />
          ${amplifyButton(label)}
          <button class="btn small polish-btn" type="button" data-polish="${esc(key)}">✨润色</button>
        </span>
      </div>`;
    }
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
        ${amplifyButton(label)}
        <button class="btn small polish-btn" type="button" data-polish="${esc(key)}">✨润色</button>
      </span>
    </div>`;
  }

  /* 渲染设定库两个分区（梗概 / 文风），data-field 保持原 key。 */
  function renderSettings(bible) {
    const groups = { premise: [], style: [] };
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

  /* 拉取并渲染整个页面。 */
  async function load() {
    const path = NW.state.path;
    if (!path) {
      el("pjInfo").textContent = "先在上方填工程目录并点「打开」";
      return;
    }
    try {
      const [info, bible, work, styles] = await Promise.all([
        NW.api(`/api/project/info?${NW.qs({ path })}`),
        NW.api(`/api/ui/setting?${NW.qs({ path })}`),
        NW.api(`/api/ui/project/work?${NW.qs({ path })}`),
        NW.api(`/api/ui/styles`).catch(() => ({ styles: [] })),
      ]);
      bibleCache = bible;
      stylePresets = styles.styles || [];
      renderInfo(info, bible);
      renderWork(work);
      renderSettings(bible);
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

  /* 保存两个分区的标量设定（梗概 / 文风一次提交）。 */
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

  /* 收集某个分区当前表单值（生成前用于判断「已有内容」）。
   * 参数：section premise / style。
   * 返回：{字段名: 字符串}，文风分区额外带 forbidden 数组。
   */
  function currentFields(section) {
    const box = el(SECTION_INPUTS[section]);
    const fields = {};
    FIELDS.filter((item) => item[2] === section).forEach(([key]) => {
      const input = box.querySelector(`[data-field="${key}"]`);
      fields[key] = input ? input.value.trim() : "";
    });
    if (section === "style") fields.forbidden = splitList(el("pjForbidden").value);
    return fields;
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

  /* 把生成结果回填到表单（只回填，不落盘）。 */
  function applyGenerated(section, fields) {
    FIELDS.filter((item) => item[2] === section).forEach(([key]) => {
      if (fields[key] !== undefined) setFieldValue(key, fields[key]);
    });
    /* 文风生成的 forbidden 是数组，回填到逗号分隔输入框 */
    if (section === "style" && Array.isArray(fields.forbidden)) {
      el("pjForbidden").value = fields.forbidden.join("，");
    }
    NW.toast("已回填生成结果，确认后点保存", true);
  }

  /* 调 NWTools 按需求生成整个分区，预览后一次性回填全部字段。
   * 参数：section premise / style；button 触发按钮。
   * 返回：无。
   */
  function generateSection(section, button) {
    if (!window.NWTools) return;
    NWTools.generateBlock({
      section,
      button,
      fields: currentFields(section),
      onApply(fields) {
        applyGenerated(section, fields);
      },
    });
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
    setButtonLoading(button, true, "润色中…");
    try {
      const result = await NW.api("/api/ui/setting/polish", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: NW.state.path, field, text }),
      });
      if (result.text) input.value = result.text;
      NW.toast("润色完成（未保存）", true);
    } catch (error) {
      NW.toast(`润色失败：${error.message}`);
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
      NW.toast("工程已创建，下一步：到大纲页生成大纲", true);
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

  /* 项目页统一点击分发：润色按钮在内容动态渲染后仍然可用。 */
  function onViewClick(event) {
    const polish = event.target.closest("[data-polish]");
    if (polish) polishField(polish);
  }

  /* 视觉风格预设下拉：选中后把风格词写入同一字段的输入框（保存仍走原逻辑）。 */
  function onViewChange(event) {
    const select = event.target.closest("[data-style-preset]");
    if (!select) return;
    const input = select.closest(".stack").querySelector('[data-field="visual_style"]');
    if (!input) return;
    input.value = select.value || input.value;
  }

  /* 首次激活时绑定事件，避免重复绑定。 */
  function bind() {
    if (bound) return;
    bound = true;
    el("pjSave").addEventListener("click", saveSettings);
    el("pjExport").addEventListener("click", exportBible);
    el("npCreate").addEventListener("click", createProject);
    el("pjWorkSave").addEventListener("click", saveWork);
    el("pjGenPremise").addEventListener("click", () => generateSection("premise", el("pjGenPremise")));
    el("pjGenStyle").addEventListener("click", () => generateSection("style", el("pjGenStyle")));
    el("view-project").addEventListener("click", onViewClick);
    el("view-project").addEventListener("change", onViewChange);
    if (window.NWTools) NWTools.bindAmplify(document);
    NW.onProject(() => load());
  }

  window.Pages.project = {
    activate() {
      bind();
      load();
    },
  };
})();
