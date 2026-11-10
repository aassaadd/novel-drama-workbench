/* 人物页：角色档案列表、就地编辑、生成人物、脸部特写 / 三视图与删除。
 *
 * 取数：
 *   - GET  /api/ui/characters          人物列表（旁白也在内，界面不展示）
 *   - POST /api/ui/characters          批量追加（单条新增；生成候选确认后保存）
 *   - PATCH /api/ui/characters         按 ID 更新
 *   - POST /api/ui/characters/generate 生成候选（dry-run，不落盘）
 *   - POST /api/ui/characters/image    生成特写 / 三视图（kind=closeup|turnaround）
 *   - POST /api/ui/characters/delete   删除（保留角色 chr_narrator 会被后端拒绝）
 * 所有写操作成功后重新拉列表，并刷新流程条上的人物数量（排除旁白）。
 */

(function () {
  /* 人物定位 / 性别的中文名（写回仍是英文枚举） */
  const ROLE_LABELS = { protagonist: "主角", antagonist: "反派", supporting: "配角", minor: "龙套" };
  const GENDER_LABELS = { male: "男", female: "女", other: "其他", unknown: "性别未知" };

  const NARRATOR_ID = "chr_narrator";
  const EMPTY_TEXT = "还没有人物：先打开工程，再点「✨ 生成人物」或「＋ 新增人物」。";
  const NO_PROJECT_TEXT = "先打开工程：在上方「工程目录」里选择或新建一个工程。";

  /* 图像槽位：key 与 character.image_assets 一致 */
  const IMAGE_SLOTS = [
    { kind: "closeup", label: "脸部特写", hint: "按外貌提示词生成半身特写" },
    { kind: "turnaround", label: "三视图", hint: "正面 / 侧面 / 背面全身设定" },
  ];

  let bound = false;
  let characters = [];      // 已落盘人物（含旁白）
  let editingId = null;     // 正在就地编辑的人物 ID；"__new__" 表示新增
  let loadedPath = "";      // 上次加载的工程，切工程时清空编辑态
  let wizardMask = null;    // 生成候选预览浮层
  let wizardRows = [];      // 当前预览的候选人物

  const el = (id) => document.getElementById(id);

  /* 按钮 loading：记住原文并禁用，结束后恢复。
   * 参数：button 按钮节点；loading 是否进入 loading；text loading 文案。
   * 返回：无。
   */
  function setLoading(button, loading, text) {
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

  /* 按逗号 / 顿号 / 分号把输入拆成列表。
   * 参数：text 输入文本。
   * 返回：去空后的字符串数组。
   */
  function splitList(text) {
    return String(text || "")
      .split(/[、,，;；]+/)
      .map((item) => item.trim())
      .filter(Boolean);
  }

  /* 背景摘要：把换行压成空格并截断，卡片里只展示一句。
   * 参数：text 原文；limit 最大字数。
   * 返回：摘要字符串；空值返回「—」。
   */
  function summary(text, limit = 120) {
    const value = String(text || "").replace(/\s+/g, " ").trim();
    if (!value) return "—";
    return value.length > limit ? `${value.slice(0, limit)}…` : value;
  }

  /* 把「目标ID=备注」多行文本解析成 relationships 对象。
   * 参数：text 多行文本。
   * 返回：{目标ID: 备注}。
   */
  function parseRelationships(text) {
    const result = {};
    String(text || "")
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean)
      .forEach((line) => {
        const index = line.indexOf("=");
        if (index <= 0) return;
        const key = line.slice(0, index).trim();
        const note = line.slice(index + 1).trim();
        if (key) result[key] = note;
      });
    return result;
  }

  /* 把关系对象转成多行文本（一行一项「目标ID=备注」）。
   * 参数：notes 关系对象。
   * 返回：多行文本；空对象返回空串。
   */
  function relationshipsText(notes) {
    return Object.entries(notes || {})
      .map(([key, note]) => `${key}=${note || ""}`)
      .join("\n");
  }

  /* 人物定位徽标：主角 / 反派 / 配角 / 龙套用不同颜色区分。
   * 参数：role 英文枚举。
   * 返回：HTML 字符串。
   */
  function roleBadge(role) {
    const key = ROLE_LABELS[role] ? role : "supporting";
    return `<span class="char-badge role-${esc(key)}">${esc(ROLE_LABELS[key])}</span>`;
  }

  /* 关系备注展示：把目标 ID 换成人物姓名，未命中时直接显示 ID。
   * 参数：item 人物条目。
   * 返回：可读文本；没有备注返回「—」。
   */
  function relationshipText(item) {
    const notes = (item.state && item.state.relationship_notes) || item.relationships || {};
    const entries = Object.entries(notes);
    if (!entries.length) return "—";
    const nameById = Object.fromEntries(characters.map((row) => [row.id, row.name]));
    return entries.map(([key, note]) => `${nameById[key] || key}：${note || ""}`).join("；");
  }

  /* 放大编辑按钮：点击后由 NWTools.bindAmplify 找到同容器输入框。
   * 参数：title 放大面板标题。
   * 返回：HTML 字符串。
   */
  function amplifyButton(title) {
    return `<button class="btn small amplify-btn" type="button" data-amplify data-title="${esc(title)}" title="放大编辑">⤢</button>`;
  }

  /* 单个图像槽：有图显示缩略图，没图显示占位与提示；已保存人物才给生成按钮。
   * 参数：item 人物条目；slot {kind, label, hint}。
   * 返回：HTML 字符串。
   */
  function imageSlotHtml(item, slot) {
    const rel = ((item.image_assets || {})[slot.kind]) || "";
    const url = rel ? uiFileUrl(rel) : "";
    const id = item.id || "";
    const action = id
      ? `<button class="btn small" type="button" data-char-act="image" data-id="${esc(id)}" data-kind="${esc(slot.kind)}" data-has="${rel ? "1" : "0"}">${rel ? "重新生成" : "生成"}</button>`
      : "";
    const body = url
      ? `<img class="char-asset-img" loading="lazy" src="${esc(url)}" alt="${esc(`${item.name || "人物"} · ${slot.label}`)}" />`
      : `<div class="char-asset-empty"><span>未生成</span><span class="muted">${esc(slot.hint)}</span></div>`;
    return `<div class="char-asset${url ? "" : " is-empty"}">
      <div class="char-asset-head"><span>${esc(slot.label)}</span>${action}</div>
      ${body}
    </div>`;
  }

  /* 人物编辑表单（就地展开，字段名与 PATCH 接口一致）。
   * 参数：item 人物条目（新增时传空结构）。
   * 返回：HTML 字符串。
   */
  function characterEditHtml(item) {
    const roleOptions = Object.entries(ROLE_LABELS)
      .map(([value, label]) => `<option value="${esc(value)}"${value === (item.role || "supporting") ? " selected" : ""}>${esc(label)}</option>`)
      .join("");
    const genderOptions = Object.entries(GENDER_LABELS)
      .map(([value, label]) => `<option value="${esc(value)}"${value === (item.gender || "unknown") ? " selected" : ""}>${esc(label)}</option>`)
      .join("");
    const notes = (item.state && item.state.relationship_notes) || item.relationships || {};
    return `<div class="char-edit">
      <div class="actions">
        <button class="btn" type="button" data-char-act="generate">✨ 按需求生成</button>
        <span class="muted">按需求一次生成全部字段，确认后回填到下方表单</span>
      </div>
      <div class="form-grid">
        <label class="stack">姓名<input data-edit="name" value="${esc(item.name || "")}" /></label>
        <label class="stack">定位<select data-edit="role">${roleOptions}</select></label>
        <label class="stack">性别<select data-edit="gender">${genderOptions}</select></label>
        <label class="stack">年龄段<input data-edit="age_range" value="${esc(item.age_range || "")}" placeholder="如：25–35" /></label>
      </div>
      <div class="stack">性格（逗号分隔）
        <span class="field-action">
          <textarea data-edit="personality" rows="2" placeholder="如：克制、敏锐、嘴硬心软">${esc((item.personality || []).join("、"))}</textarea>
          ${amplifyButton("性格")}
        </span>
      </div>
      <div class="stack">小传
        <span class="field-action">
          <textarea data-edit="background" rows="3" placeholder="出身、经历与当下动机">${esc(item.background || "")}</textarea>
          ${amplifyButton("小传")}
        </span>
      </div>
      <div class="stack">外貌提示词
        <span class="field-action">
          <textarea data-edit="appearance_prompt" rows="3" placeholder="供图像模型拼装的稳定外貌描述">${esc(item.appearance_prompt || "")}</textarea>
          ${amplifyButton("外貌提示词")}
        </span>
      </div>
      <div class="stack">负面提示词
        <span class="field-action">
          <textarea data-edit="negative_prompt" rows="2" placeholder="如：低质, 多余手指, 水印">${esc(item.negative_prompt || "")}</textarea>
          ${amplifyButton("负面提示词")}
        </span>
      </div>
      <div class="stack">关系备注（目标ID=备注，一行一项）
        <textarea data-edit="relationships" rows="2" placeholder="chr_xxx=互相提防">${esc(relationshipsText(notes))}</textarea>
      </div>
      <div class="actions">
        <button class="btn primary" type="button" data-char-act="save">保存</button>
        <button class="btn" type="button" data-char-act="cancel">取消</button>
      </div>
    </div>`;
  }

  /* 一张人物卡片：姓名 / 定位 / 性别年龄 / 性格 / 小传 / 外貌提示词 / 关系备注 + 两个图槽。
   * 参数：item 人物条目；editing 是否展开编辑表单；pending 是否为生成候选项。
   * 返回：HTML 字符串。
   */
  function characterCardHtml(item, editing, pending = false) {
    const id = item.id || "";
    const slots = IMAGE_SLOTS.map((slot) => imageSlotHtml(item, slot)).join("");
    return `<article class="char-card${pending ? " pending" : ""}" data-id="${esc(id)}">
      <div class="char-head">
        <span class="char-name">${esc(item.name || "（未命名）")}</span>
        ${roleBadge(item.role)}
        <span class="char-sub muted">${esc(GENDER_LABELS[item.gender] || "性别未知")} · ${esc(item.age_range || "年龄未知")}</span>
        ${id && item.id !== NARRATOR_ID
          ? `<span class="char-actions">
              <button class="btn small" type="button" data-char-act="edit" data-char-id="${esc(id)}">编辑</button>
              <button class="btn small" type="button" data-char-act="delete" data-char-id="${esc(id)}">删除</button>
            </span>`
          : ""}
      </div>
      <div class="char-meta"><span class="muted">性格</span>${esc((item.personality || []).join("、") || "—")}</div>
      <div class="char-bg"><span class="muted">小传</span>${esc(summary(item.background))}</div>
      <div class="char-bg"><span class="muted">外貌提示词</span>${esc(summary(item.appearance_prompt))}</div>
      <div class="char-bg"><span class="muted">关系备注</span>${esc(summary(relationshipText(item)))}</div>
      <div class="char-assets">${slots}</div>
      ${editing ? characterEditHtml(item) : ""}
    </article>`;
  }

  /* 当前可展示的人物：旁白是系统角色，不在列表里编辑。
   * 返回：人物数组。
   */
  function visibleCharacters() {
    return characters.filter((item) => item.role !== "narrator" && item.id !== NARRATOR_ID);
  }

  /* 渲染人物列表与空态。
   * 参数：emptyText 空态提示，省略时用默认文案。
   * 返回：无。
   */
  function render(emptyText) {
    const box = el("charactersList");
    const empty = el("charactersEmpty");
    const rows = visibleCharacters();
    const cards = rows.map((item) => characterCardHtml(item, editingId === item.id));
    if (editingId === "__new__") {
      cards.unshift(characterCardHtml({ name: "", role: "supporting", gender: "unknown", personality: [], background: "" }, true, true));
    }
    if (box) box.innerHTML = cards.join("");
    if (empty) {
      empty.textContent = emptyText || EMPTY_TEXT;
      empty.hidden = cards.length > 0;
    }
  }

  /* 刷新流程条上的人物数量（排除旁白）。
   * 参数：count 人物数。
   * 返回：无。
   */
  function setFlowCount(count) {
    if (!window.NW || !NW.state) return;
    if (!NW.state.flowData) NW.state.flowData = { worlds: 0, characters: 0 };
    NW.state.flowData.characters = Number(count) || 0;
    const layout = window.NWLayout || {};
    const refresh = layout.refreshFlow || layout.renderFlow;
    if (typeof refresh === "function") refresh();
  }

  /* 拉取人物列表并渲染。
   * 参数：无。
   * 返回：Promise，无。
   */
  async function load() {
    const path = (NW.state && NW.state.path) || "";
    if (!path) {
      characters = [];
      editingId = null;
      setFlowCount(0);
      render(NO_PROJECT_TEXT);
      return;
    }
    if (loadedPath !== path) {
      loadedPath = path;
      editingId = null;
      closeWizard();
    }
    try {
      const data = await NW.api(`/api/ui/characters?${NW.qs({ path })}`);
      // 接口约定返回 {characters: [...]}；结构异常时兜底为空数组，避免整页渲染中断
      if (!data || !Array.isArray(data.characters)) console.warn("[characters] 接口返回结构异常", String(JSON.stringify(data)).slice(0, 300));
      characters = data && Array.isArray(data.characters) ? data.characters : [];
      setFlowCount(visibleCharacters().length);
      render("");
    } catch (error) {
      characters = [];
      setFlowCount(0);
      render("读取人物失败，请重试。");
      NW.toast(`读取人物失败：${error.message}`);
    }
  }

  /* 打开「新增人物」的就地表单。
   * 参数：无。
   * 返回：无。
   */
  function startNew() {
    if (!(NW.state && NW.state.path)) {
      NW.toast("先打开或新建工程");
      return;
    }
    editingId = "__new__";
    render("");
  }

  /* 读取某张人物卡片里的编辑表单值。
   * 参数：card 人物卡片节点。
   * 返回：可直接提交的人物对象。
   */
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
      appearance_prompt: read("appearance_prompt"),
      negative_prompt: read("negative_prompt"),
      relationships: parseRelationships(read("relationships")),
    };
  }

  /* 保存人物：新增走 POST 批量追加（单条），编辑走 PATCH。
   * 参数：button 保存按钮。
   * 返回：Promise，无。
   */
  async function saveCharacter(button) {
    if (!NW.state.path) return;
    const card = button.closest(".char-card");
    if (!card) return;
    const payload = readCharacterForm(card);
    if (!payload.name) {
      NW.toast("人物姓名不能为空");
      return;
    }
    setLoading(button, true, "保存中…");
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
      await load();
    } catch (error) {
      NW.toast(`保存人物失败：${error.message}`);
    } finally {
      setLoading(button, false);
    }
  }

  /* 删除人物：确认后调删除接口（后端会拒绝删除旁白）。
   * 参数：id 人物 ID。
   * 返回：Promise，无。
   */
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
      await load();
    } catch (error) {
      NW.toast(`删除失败：${error.message}`);
    }
  }

  /* 生成 / 重新生成角色设定图：已有图时带 force，成功后重拉卡片。
   * 参数：button 触发按钮；id 人物 ID；kind closeup / turnaround。
   * 返回：Promise，无。
   */
  async function generateImage(button, id, kind) {
    if (!NW.state.path || !id) return;
    setLoading(button, true, "生成中…");
    try {
      await NW.api("/api/ui/characters/image", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: NW.state.path, id, kind, force: button.dataset.has === "1" }),
      });
      NW.toast(`${kind === "closeup" ? "脸部特写" : "三视图"}已生成`, true);
      await load();
    } catch (error) {
      NW.toast(`生成角色设定图失败：${error.message}`);
    } finally {
      setLoading(button, false);
    }
  }

  /* 把生成结果回填到编辑表单（只回填，不落盘）。
   * 参数：card 人物卡片；fields 生成的人物字段。
   * 返回：无。
   */
  function applyGenerated(card, fields) {
    const set = (name, value) => {
      const node = card.querySelector(`[data-edit="${name}"]`);
      if (node) node.value = value === null || value === undefined ? "" : String(value);
    };
    set("name", fields.name || "");
    set("role", ROLE_LABELS[fields.role] ? fields.role : "supporting");
    set("gender", GENDER_LABELS[fields.gender] ? fields.gender : "unknown");
    set("age_range", fields.age_range || "");
    set("personality", (fields.personality || []).join("、"));
    set("background", fields.background || "");
    set("appearance_prompt", fields.appearance_prompt || "");
    set("negative_prompt", fields.negative_prompt || "");
    const notes = (fields.state && fields.state.relationship_notes) || fields.relationships || {};
    set("relationships", relationshipsText(notes));
    NW.toast("已回填生成结果，确认后点保存", true);
  }

  /* 表单上方的「按需求生成」：一次生成完整人物并回填当前表单。
   * 参数：button 触发按钮；card 人物卡片。
   * 返回：无。
   */
  function generateForForm(button, card) {
    if (!card || !window.NWTools) return;
    const fields = readCharacterForm(card);
    NWTools.generateBlock({
      section: "character",
      targetId: editingId && editingId !== "__new__" ? editingId : "",
      button,
      fields,
      onApply(generated) {
        applyGenerated(card, generated);
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
    wizardRows = [];
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

  /* 把生成候选裁剪成可提交的人物对象（去掉服务端可能回带的内部字段）。
   * 参数：row 生成的人物。
   * 返回：人物对象。
   */
  function pickPayload(row) {
    return {
      name: row.name || "",
      role: row.role || "supporting",
      gender: row.gender || "unknown",
      age_range: row.age_range || "",
      personality: row.personality || [],
      background: row.background || "",
      relationships: row.relationships || (row.state && row.state.relationship_notes) || {},
    };
  }

  /* 保存勾选的候选人物。
   * 参数：mask 预览浮层；button 保存按钮。
   * 返回：Promise，无。
   */
  async function savePicked(mask, button) {
    const picks = Array.from(mask.querySelectorAll("input[data-character-pick]:checked"));
    if (!picks.length) {
      NW.toast("先勾选至少一个人物");
      return;
    }
    const rows = picks
      .map((node) => pickPayload(wizardRows[Number(node.value)] || {}))
      .filter((row) => row.name);
    if (!rows.length) {
      NW.toast("勾选的人物缺少姓名，无法保存");
      return;
    }
    setLoading(button, true, "保存中…");
    try {
      const result = await NW.api("/api/ui/characters", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: NW.state.path, characters: rows }),
      });
      const created = (result.characters || []).length;
      const skipped = result.skipped || [];
      NW.toast(`已写入 ${created} 个人物${skipped.length ? `（重名跳过：${skipped.join("、")}）` : ""}`, true);
      closeWizard();
      editingId = null;
      await load();
    } catch (error) {
      NW.toast(`保存人物失败：${error.message}`);
    } finally {
      setLoading(button, false);
    }
  }

  /* 打开生成候选预览：姓名 / 定位 / 性别年龄 / 小传，可勾选后保存。
   * 参数：candidates 生成的人物列表。
   * 返回：无。
   */
  function openWizard(candidates) {
    closeWizard();
    wizardRows = candidates || [];
    if (!wizardRows.length) {
      NW.toast("没有生成任何人物，请补充需求后重试");
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
    title.textContent = `生成人物 · 候选 ${wizardRows.length} 人`;
    dialog.appendChild(title);

    const hint = document.createElement("p");
    hint.className = "modal-hint";
    hint.textContent = "勾选要写入人物表的角色；确认后会追加保存，不影响已有角色与音色绑定。";
    dialog.appendChild(hint);

    const list = document.createElement("div");
    list.className = "gen-check-list";

    const countNode = document.createElement("span");
    countNode.className = "gen-field-count";

    wizardRows.forEach((row, index) => {
      const line = document.createElement("label");
      line.className = "gen-check";

      const check = document.createElement("input");
      check.type = "checkbox";
      check.checked = true;
      check.value = String(index);
      check.setAttribute("data-character-pick", "");
      line.appendChild(check);

      const body = document.createElement("div");
      body.className = "gen-check-body";

      const head = document.createElement("div");
      head.className = "gen-check-head";
      const nameNode = document.createElement("span");
      nameNode.className = "gen-check-name";
      nameNode.textContent = row.name || "（未命名）";
      head.appendChild(nameNode);
      const meta = document.createElement("span");
      meta.className = "muted";
      meta.textContent = `${ROLE_LABELS[row.role] || "配角"} · ${GENDER_LABELS[row.gender] || "性别未知"} · ${row.age_range || "年龄未知"}`;
      head.appendChild(meta);
      body.appendChild(head);

      const background = document.createElement("p");
      background.textContent = summary(row.background, 160);
      body.appendChild(background);

      const personality = document.createElement("span");
      personality.className = "muted";
      personality.textContent = `性格：${(row.personality || []).join("、") || "—"}`;
      body.appendChild(personality);

      line.appendChild(body);
      list.appendChild(line);
    });

    dialog.appendChild(list);

    const foot = document.createElement("div");
    foot.className = "modal-foot";

    /* 同步「已选 N 人」。
     * 参数：无。
     * 返回：无。
     */
    const syncCount = () => {
      countNode.textContent = `已选 ${list.querySelectorAll("input[data-character-pick]:checked").length} 人`;
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

  /* 生成人物：先收需求，再预览候选（不落盘）。
   * 参数：button 触发按钮。
   * 返回：Promise，无。
   */
  async function generateCharacters(button) {
    if (!(NW.state && NW.state.path)) {
      NW.toast("先打开或新建工程");
      return;
    }
    if (!window.NWEditor) return;
    NWEditor.openBrief({
      title: "生成人物需求",
      placeholder: "如：主角是失忆的法医，配角包含一名旧警察与一名记者",
      onSave: async (brief) => {
        setLoading(button, true, "生成中…");
        try {
          const result = await NW.api("/api/ui/characters/generate", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ path: NW.state.path, brief }),
          });
          openWizard(result.characters || []);
        } catch (error) {
          NW.toast(`生成人物失败：${error.message}`);
        } finally {
          setLoading(button, false);
        }
      },
    });
  }

  /* 卡片点击分发：编辑 / 取消 / 保存 / 生成 / 删除 / 生成图像。
   * 参数：event 点击事件。
   * 返回：无。
   */
  function onListClick(event) {
    const trigger = event.target.closest("[data-char-act]");
    if (!trigger) return;
    const act = trigger.dataset.charAct;
    const card = trigger.closest(".char-card");
    if (act === "edit") {
      editingId = trigger.dataset.charId || "";
      render("");
      return;
    }
    if (act === "cancel") {
      editingId = null;
      render("");
      return;
    }
    if (act === "save") {
      saveCharacter(trigger);
      return;
    }
    if (act === "generate") {
      generateForForm(trigger, card);
      return;
    }
    if (act === "delete") {
      deleteCharacter(trigger.dataset.charId || "");
      return;
    }
    if (act === "image") {
      generateImage(trigger, trigger.dataset.id || "", trigger.dataset.kind || "");
    }
  }

  /* 首次激活时绑定事件，避免重复绑定。
   * 参数：无。
   * 返回：无。
   */
  function bind() {
    if (bound) return;
    bound = true;
    const add = el("charactersAdd");
    if (add) add.addEventListener("click", startNew);
    const generate = el("charactersGenerate");
    if (generate) generate.addEventListener("click", () => generateCharacters(generate));
    const list = el("charactersList");
    if (list) list.addEventListener("click", onListClick);
    if (window.NWTools) NWTools.bindAmplify(document);
    NW.onProject(() => load());
  }

  window.Pages.characters = {
    init() {
      bind();
    },
    activate() {
      bind();
      load();
    },
  };
})();
