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
  /* 人物定位的中文名统一在 core.js 定义（ROLE_LABELS / roleLabel），这里只保留性别映射。 */
  const GENDER_LABELS = { male: "男", female: "女", other: "其他", unknown: "性别未知" };

  const NARRATOR_ID = "chr_narrator";
  const EMPTY_TEXT = "还没有人物：先打开工程，再点「AI 生成人物」或「＋ 新增人物」。";
  const NO_PROJECT_TEXT = "还没有打开工程：点左上角工程名，打开或新建一个工程。";

  /* 图像槽位：key 与 character.image_assets 一致。
   * fullbody 是母图：头像与三视图都以它为参考图生成，保证同一张脸、同一套风格。 */
  const IMAGE_SLOTS = [
    { kind: "fullbody", label: "全身像（母图）", hint: "先出全身像，头像与三视图都参考它" },
    { kind: "closeup", label: "头像", hint: "以全身像为参考生成，保证同一张脸" },
    { kind: "turnaround", label: "三视图", hint: "以全身像为参考，正面 / 侧面 / 背面" },
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
   * 返回：摘要字符串；空值返回「未填写」。
   */
  function summary(text, limit = 120) {
    const value = String(text || "").replace(/\s+/g, " ").trim();
    if (!value) return "未填写";
    return value.length > limit ? `${value.slice(0, limit)}…` : value;
  }

  /* 可绑定的人物：排除旁白与当前人物自己。
   * 参数：ownId 当前人物 ID。
   * 返回：人物数组。
   */
  function pickableCharacters(ownId) {
    return characters.filter((row) => row.id !== ownId && row.id !== NARRATOR_ID && row.role !== "narrator");
  }

  /* 关系列表为空且没有可绑定人物时的中文空状态提示。
   * 参数：无。
   * 返回：HTML 字符串。
   */
  function relEmptyHtml() {
    return `<p class="rel-empty">暂无可绑定的人物：先新增其他人物，再填写关系备注。</p>`;
  }

  /* 关系目标下拉：选项为本工程人物姓名，回显按人物 ID 选中。
   * 参数：key 当前关系目标 ID；ownId 当前人物 ID。
   * 返回：<option> HTML 字符串。
   */
  function relationshipOptions(key, ownId) {
    const pickable = pickableCharacters(ownId);
    const nameById = Object.fromEntries(characters.map((row) => [row.id, row.name]));
    // 生成的人物关系以姓名为键：能对上人物时自动改绑到 ID，保存时顺带迁移旧键
    const matched = pickable.find((row) => row.id === key || row.name === key);
    const selectedId = matched ? matched.id : key;
    // 历史关系指向已删除人物时保留原键并标记，保存时不会静默丢弃
    const unknown = key && !matched
      ? `<option value="${esc(key)}" selected>${esc(nameById[key] || key)}${nameById[key] ? "（不可选）" : "（未找到该人物）"}</option>`
      : "";
    const placeholder = key ? "" : `<option value="">请选择人物</option>`;
    const options = pickable
      .map((row) => `<option value="${esc(row.id)}"${row.id === selectedId ? " selected" : ""}>${esc(row.name || row.id)}</option>`)
      .join("");
    return placeholder + unknown + options;
  }

  /* 一行关系备注：左侧人物下拉，右侧备注输入框与删除按钮。
   * 参数：key 关系目标 ID（新增行为空）；note 备注；ownId 当前人物 ID。
   * 返回：HTML 字符串。
   */
  function relationshipRowHtml(key, note, ownId) {
    return `<div class="rel-row" data-rel-row data-key="${esc(key || "")}">
      <select data-rel-target>${relationshipOptions(key, ownId)}</select>
      <input data-rel-note value="${esc(note || "")}" placeholder="关系备注，如：互相提防" maxlength="500" />
      <button class="rel-del" type="button" data-rel-act="remove" title="删除这一行">×</button>
    </div>`;
  }

  /* 整个关系备注编辑区：若干关系行 + 新增按钮。
   * 参数：notes 关系对象 `{目标ID: 备注}`；ownId 当前人物 ID。
   * 返回：HTML 字符串。
   */
  function relationshipListHtml(notes, ownId) {
    const pickable = pickableCharacters(ownId);
    const entries = Object.entries(notes || {});
    const rows = entries
      .map(([key, note]) => relationshipRowHtml(key, note, ownId))
      .join("");
    const addDisabled = pickable.length ? "" : " disabled";
    // 记住渲染时的目标集合，保存时据此判断哪些关系被删除了
    const original = esc(JSON.stringify(entries.map(([key]) => key)));
    return `<div class="rel-list" data-rel-list data-rel-orig="${original}">${pickable.length ? "" : relEmptyHtml()}${rows}</div>
      <div class="rel-actions">
        <button class="btn small" type="button" data-rel-act="add"${addDisabled}>＋ 新增关系</button>
      </div>`;
  }

  /* 读取关系列表：当前行组成的关系对象，以及被删除行的原目标 ID。
   * 参数：card 人物卡片节点。
   * 返回：{notes: {目标ID: 备注}, removed: [被删除的目标ID]}。
   */
  function readRelationshipRows(card) {
    const list = card.querySelector("[data-rel-list]");
    const notes = {};
    const removed = [];
    if (!list) return { notes, removed };
    list.querySelectorAll("[data-rel-row]").forEach((row) => {
      const select = row.querySelector("[data-rel-target]");
      const note = row.querySelector("[data-rel-note]");
      const key = select ? select.value : "";
      if (key) notes[key] = note ? note.value.trim() : "";
    });
    // 渲染时的目标在保存时已不存在（整行删除或改了绑定对象）→ 交给后端删除该关系
    let original = [];
    try {
      original = JSON.parse(list.dataset.relOrig || "[]");
    } catch (error) {
      original = [];
    }
    original.forEach((key) => {
      if (key && !(key in notes)) removed.push(key);
    });
    return { notes, removed };
  }

  /* 人物定位徽标：主角 / 反派 / 配角 / 龙套用不同颜色区分。
   * 参数：role 英文枚举。
   * 返回：HTML 字符串。
   */
  function roleBadge(role) {
    // 未登记的定位仍按配角配色，但文字保留原值，避免把未知枚举显示成「配角」
    const key = ROLE_LABELS[role] ? role : "supporting";
    return `<span class="char-badge role-${esc(key)}">${esc(roleLabel(role))}</span>`;
  }

  /* 关系备注展示：把目标 ID 换成人物姓名，未命中时直接显示 ID。
   * 参数：item 人物条目。
   * 返回：可读文本；没有备注返回「—」。
   */
  function relationshipText(item) {
    const notes = (item.state && item.state.relationship_notes) || item.relationships || {};
    const entries = Object.entries(notes);
    if (!entries.length) return "还没有关系备注";
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
        <button class="btn" type="button" data-char-act="generate">✨ AI 补全此条</button>
        <span class="muted">按补充要求生成整条人物档案，确认后回填到下方表单，不会自动保存</span>
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
      <div class="stack">描述性字段（后续配音 / 出图直接取用）
        <span class="muted">六项都会随人物落盘；英文外貌 / 负向留空时，保存会自动同步到上面的出图提示词</span>
        <div class="form-grid">
          <label class="stack">中文音色描述<textarea data-edit="voice_description_zh" rows="3" placeholder="音色质感、语速、语气、年龄感、口音">${esc(item.voice_description_zh || "")}</textarea></label>
          <label class="stack">英文音色描述<textarea data-edit="voice_description_en" rows="3" placeholder="English voice prompt: timbre, pace, tone, age feel, accent">${esc(item.voice_description_en || "")}</textarea></label>
          <label class="stack">中文外貌描述<textarea data-edit="appearance_desc_zh" rows="3" placeholder="脸型、五官、发型、身形、惯常着装与气质">${esc(item.appearance_desc_zh || "")}</textarea></label>
          <label class="stack">英文外貌描述<textarea data-edit="appearance_desc_en" rows="3" placeholder="English image prompt with concrete visible features">${esc(item.appearance_desc_en || "")}</textarea></label>
          <label class="stack">中文负向说明<textarea data-edit="appearance_negative_zh" rows="2" placeholder="出图应排除的元素：眼镜、胡须、伤疤">${esc(item.appearance_negative_zh || "")}</textarea></label>
          <label class="stack">英文负向提示词<textarea data-edit="appearance_negative_en" rows="2" placeholder="glasses, beard, scar, watermark">${esc(item.appearance_negative_en || "")}</textarea></label>
        </div>
      </div>
      <div class="stack">关系备注
        <span class="muted">每行选一个人物并填写备注；删除行后保存会一并清除该关系</span>
        <div data-rel-block>${relationshipListHtml(notes, item.id || "")}</div>
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
        ${item.stock ? `<span class="char-sub muted">${esc(item.stock.label || item.stock.slot)}</span>` : ""}
        <span class="char-sub muted">${esc(GENDER_LABELS[item.gender] || "性别未知")} · ${esc(item.age_range || "年龄未知")}</span>
        ${id && item.id !== NARRATOR_ID
          ? `<span class="char-actions">
              <button class="btn small" type="button" data-char-act="edit" data-char-id="${esc(id)}">编辑</button>
              <button class="btn small" type="button" data-char-act="delete" data-char-id="${esc(id)}">删除</button>
            </span>`
          : ""}
      </div>
      <div class="char-meta"><span class="muted">性格</span>${esc((item.personality || []).join("、") || "未填写")}</div>
      <div class="char-bg"><span class="muted">音色</span>${esc(voiceText(item))}</div>
      <div class="char-bg"><span class="muted">小传</span>${esc(summary(item.background))}</div>
      <div class="char-bg"><span class="muted">外貌提示词</span>${esc(summary(item.appearance_prompt))}</div>
      <div class="char-bg"><span class="muted">关系备注</span>${esc(summary(relationshipText(item)))}</div>
      <div class="char-assets">${slots}</div>
      ${editing ? characterEditHtml(item) : ""}
    </article>`;
  }

  /* 当前可展示的人物：旁白作为正式实体展示在最上方（不可删除，跨章复用音色）。
   * 返回：人物数组。
   */
  function visibleCharacters() {
    return characters.slice().sort((left, right) => {
      const leftNarrator = left.role === "narrator" || left.id === NARRATOR_ID ? 0 : 1;
      const rightNarrator = right.role === "narrator" || right.id === NARRATOR_ID ? 0 : 1;
      return leftNarrator - rightNarrator;
    });
  }

  /* 音色描述的一句话展示。
   * 参数：item 人物对象。
   * 返回：展示文本。
   */
  function voiceText(item) {
    const profile = item.voice_profile || {};
    return profile.summary || profile.voice_summary || "未生成音色描述";
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
    const rel = readRelationshipRows(card);
    return {
      name: read("name"),
      role: read("role") || "supporting",
      gender: read("gender") || "unknown",
      age_range: read("age_range"),
      personality: splitList(read("personality")),
      background: read("background"),
      appearance_prompt: read("appearance_prompt"),
      negative_prompt: read("negative_prompt"),
      voice_description_zh: read("voice_description_zh"),
      voice_description_en: read("voice_description_en"),
      appearance_desc_zh: read("appearance_desc_zh"),
      appearance_desc_en: read("appearance_desc_en"),
      appearance_negative_zh: read("appearance_negative_zh"),
      appearance_negative_en: read("appearance_negative_en"),
      relationships: rel.notes,
      removed_relationships: Array.from(new Set(rel.removed)),
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
      // 保存成功后提示音色/BGM 编排问题，不阻断保存
      if (NW.refreshCastHint) NW.refreshCastHint(NW.state.chapter);
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
    // 编辑态点生成时先把表单存盘，避免「填了外貌却提示缺少外貌」
    if (editingId === id) {
      const card = button.closest(".char-card");
      const saveBtn = card ? card.querySelector('[data-char-act="save"]') : null;
      if (saveBtn) await saveCharacter(saveBtn);
      if (editingId === id) return;
    }
    const hasImage = button.dataset.has === "1";
    const label = { fullbody: "全身像（母图）", closeup: "头像", turnaround: "三视图" }[kind] || "设定图";
    setLoading(button, true, kind === "fullbody" ? "生成中…" : "先出母图再生成…");
    try {
      await NW.api("/api/ui/characters/image", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: NW.state.path, id, kind, force: hasImage }),
        busy: `正在生成${label}…`,
        busyDetail: "图像生成较慢，请勿关闭页面",
      });
      NW.toast(`${label}已生成`, true);
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
    for (const key of [
      "voice_description_zh",
      "voice_description_en",
      "appearance_desc_zh",
      "appearance_desc_en",
      "appearance_negative_zh",
      "appearance_negative_en",
    ]) {
      set(key, fields[key] || "");
    }
    const notes = (fields.state && fields.state.relationship_notes) || fields.relationships || {};
    const block = card.querySelector("[data-rel-block]");
    if (block) block.innerHTML = relationshipListHtml(notes, card.dataset.id || "");
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
      // 生成阶段已产出的描述必须一起落盘，否则「逐人补全」等于白做
      appearance_prompt: row.appearance_prompt || "",
      negative_prompt: row.negative_prompt || "",
      voice_description_zh: row.voice_description_zh || "",
      voice_description_en: row.voice_description_en || "",
      appearance_desc_zh: row.appearance_desc_zh || "",
      appearance_desc_en: row.appearance_desc_en || "",
      appearance_negative_zh: row.appearance_negative_zh || "",
      appearance_negative_en: row.appearance_negative_en || "",
      relationships: row.relationships || (row.state && row.state.relationship_notes) || {},
      voice_profile: row.voice_profile || undefined,
      stock: row.stock || undefined,
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
      personality.textContent = `性格：${(row.personality || []).join("、") || "未填写"}`;
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

  /* 跑一次两阶段生成（先规划名单，再逐个生成详情），期间只更新蒙版进度。
   * 参数：body 请求体；busyTitle 蒙版主文案；initialDetail 初始明细。
   * 返回：Promise<done 的 result>。
   */
  async function runPlanStream(body, busyTitle, initialDetail) {
    const progress = NW.progressInit();
    NW.busyShow(busyTitle, initialDetail);
    try {
      return await NW.streamPost("/api/ui/characters/plan/stream", body, (event, data) => {
        if (event === "plan") {
          NW.busyDetail(`已规划 ${data.total} 个角色（${data.basis || "逐个生成中"}）`);
        } else if (event === "batch" || event === "chars") {
          NW.busyDetail(NW.progressApply(progress, data));
        }
      });
    } finally {
      NW.busyHide();
    }
  }

  /* 生成人物：先规划名单（可续跑），再逐个生成详情；失败可按名单只重试失败的。
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
        const collected = [];
        let result = null;
        try {
          result = await runPlanStream(
            { path: NW.state.path, brief },
            "正在批量生成人物…",
            "正在规划人物名单…"
          );
          collected.push(...(result.characters || []));
          if ((result.failures || []).length) {
            const detail = result.failures.map((item) => `${item.name}：${item.message}`).join("；");
            if (window.confirm(`有 ${result.failures.length} 个角色生成失败：${detail}\n是否只重试这些角色？`)) {
              result = await runPlanStream(
                { path: NW.state.path, retry_failed: true },
                "正在重试失败人物…",
                "按上次名单逐个重试"
              );
              collected.push(...(result.characters || []));
            }
          }
          const failures = result.failures || [];
          const skipped = result.skipped || [];
          let message = `共生成 ${collected.length} 个角色`;
          if (failures.length) {
            message += `，失败 ${failures.length}（${failures.map((item) => `${item.name}：${item.message}`).join("；")}）`;
          }
          if (skipped.length) message += `，跳过 ${skipped.length}（已存在）`;
          NW.toast(message || "没有可生成的角色", collected.length > 0 && failures.length === 0);
          if (collected.length) openWizard(collected);
        } catch (error) {
          NW.toast(`生成人物失败：${error.message}`);
        } finally {
          setLoading(button, false);
        }
      },
    });
  }

  /* 关系行操作：新增一行 / 删除一行。
   * 参数：button 触发按钮。
   * 返回：无。
   */
  function handleRelationshipAction(button) {
    const card = button.closest(".char-card");
    const list = card ? card.querySelector("[data-rel-list]") : null;
    if (!list) return;
    if (button.dataset.relAct === "remove") {
      const row = button.closest("[data-rel-row]");
      if (row) row.remove();
      // 删空且已无可绑定人物时补回空状态提示
      if (!list.querySelector("[data-rel-row]") && !pickableCharacters(card.dataset.id || "").length) {
        list.innerHTML = relEmptyHtml();
      }
      return;
    }
    list.insertAdjacentHTML("beforeend", relationshipRowHtml("", "", card.dataset.id || ""));
  }

  /* 卡片点击分发：编辑 / 取消 / 保存 / 生成 / 删除 / 生成图像。
   * 参数：event 点击事件。
   * 返回：无。
   */
  function onListClick(event) {
    const relAct = event.target.closest("[data-rel-act]");
    if (relAct) {
      handleRelationshipAction(relAct);
      return;
    }
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

  /* ===== 音色编排编辑（R18）：音色池 + 龙套映射 + 旁白人格 + 切换点 ===== */

  let castDraft = null;
  let castMeta = { chapters: [], voices: [], characters: [], stockSlots: [] };
  /* Casting 试听：只存页面状态，音频落 .ndw/cast_previews，不进正式资产 */
  let castPreview = { character: "", voice: "", log: [] };

  const CAST_DEFAULT_POOL = [
    ["male_young", "青年男声", "male", "young_adult"],
    ["male_mid", "中年男声", "male", "adult"],
    ["male_elder", "老年男声", "male", "elderly"],
    ["female_young", "青年女声", "female", "young_adult"],
    ["female_mid", "中年女声", "female", "adult"],
    ["female_elder", "老年女声", "female", "elderly"],
  ];

  /* 下拉选项：音色库。 */
  function castVoiceOptions(selected) {
    const rows = castMeta.voices.map(
      (item) => `<option value="${esc(item.id)}"${item.id === selected ? " selected" : ""}>${esc(item.name || item.id)}</option>`
    );
    return `<option value=""${selected ? "" : " selected"}>（未绑定）</option>${rows.join("")}`;
  }

  /* 下拉选项：试听候选音色（空值表示跟随角色当前绑定）。 */
  function castCandidateOptions(selected) {
    const rows = castMeta.voices.map(
      (item) => `<option value="${esc(item.id)}"${item.id === selected ? " selected" : ""}>${esc(item.name || item.id)}</option>`
    );
    return `<option value=""${selected ? "" : " selected"}>（跟随角色当前音色）</option>${rows.join("")}`;
  }

  /* 下拉选项：可试听的角色（旁白以外的登场角色）。 */
  function castPreviewCharacterOptions(selected) {
    const rows = (castMeta.characters || [])
      .filter((item) => item.role !== "narrator")
      .map(
        (item) =>
          `<option value="${esc(item.id)}"${item.id === selected ? " selected" : ""}>${esc(item.name || item.id)}${
            item.voice_id ? "" : "（未绑音色）"
          }</option>`
      );
    return rows.join("");
  }

  /* 下拉选项：章节（allowEmpty 用于「到下一个切换点」）。 */
  function castChapterOptions(selected, allowEmpty) {
    const rows = castMeta.chapters
      .slice()
      .sort((left, right) => (left.index || 0) - (right.index || 0))
      .map(
        (item) =>
          `<option value="${esc(item.id)}"${item.id === selected ? " selected" : ""}>第${esc(item.index)}章 ${esc(
            summary(item.title, 18)
          )}</option>`
      );
    const empty = allowEmpty ? `<option value=""${selected ? "" : " selected"}>（延续到下一个切换点）</option>` : "";
    return empty + rows.join("");
  }

  /* 下拉选项：旁白人格。 */
  function castPersonaOptions(selected) {
    const rows = (castDraft.narrators || []).map(
      (item) => `<option value="${esc(item.id)}"${item.id === selected ? " selected" : ""}>${esc(item.label || item.id)}</option>`
    );
    return `<option value=""${selected ? "" : " selected"}>（未指定）</option>${rows.join("")}`;
  }

  /* 确保默认 6 个音色池槽位（仅在完全空配置时填充）。 */
  function ensureDefaultPool() {
    if ((castDraft.voice_pool || []).length) return;
    castDraft.voice_pool = CAST_DEFAULT_POOL.map(([slot, label, gender, ageFeel]) => ({
      slot,
      label,
      gender,
      age_feel: ageFeel,
      voice_id: "",
    }));
  }

  /* 龙套映射可用的键：音色池槽位 + 角色实际职业槽位 + 已保存键。 */
  function stockKeys() {
    const keys = new Set((castDraft.voice_pool || []).map((item) => item.slot).filter(Boolean));
    castMeta.stockSlots.forEach((item) => {
      if (item.slot) keys.add(item.slot);
      if (item.voice_slot) keys.add(item.voice_slot);
    });
    Object.keys(castDraft.stock_defaults || {}).forEach((key) => keys.add(key));
    return Array.from(keys).sort();
  }

  /* 渲染整块编辑器。 */
  function renderCastEditor() {
    const box = el("castEditor");
    if (!box) return;
    const pool = castDraft.voice_pool || [];
    const personas = castDraft.narrators || [];
    const switches = castDraft.narrator_switches || [];
    const defaults = castDraft.stock_defaults || {};
    const poolRows = pool
      .map(
        (item, index) => `<tr>
          <td><input class="input" data-cast-section="pool" data-index="${index}" data-key="slot" value="${esc(item.slot)}" /></td>
          <td><input class="input" data-cast-section="pool" data-index="${index}" data-key="label" value="${esc(item.label)}" /></td>
          <td><select class="input" data-cast-section="pool" data-index="${index}" data-key="voice_id">${castVoiceOptions(item.voice_id)}</select></td>
          <td><select class="input" data-cast-section="pool" data-index="${index}" data-key="gender">
            <option value="">—</option>
            <option value="male"${item.gender === "male" ? " selected" : ""}>男</option>
            <option value="female"${item.gender === "female" ? " selected" : ""}>女</option>
            <option value="other"${item.gender === "other" ? " selected" : ""}>其他</option>
          </select></td>
          <td><input class="input" data-cast-section="pool" data-index="${index}" data-key="age_feel" value="${esc(item.age_feel || "")}" placeholder="young_adult" /></td>
          <td><button class="btn small" type="button" data-cast-del="pool" data-index="${index}">删除</button></td>
        </tr>`
      )
      .join("");
    const stockRows = stockKeys()
      .map((key) => {
        const meta = castMeta.stockSlots.find((item) => item.slot === key);
        return `<tr>
          <td><code>${esc(key)}</code></td>
          <td>${esc((meta && meta.label) || "")}${meta ? `（${meta.count} 个角色）` : ""}</td>
          <td><select class="input" data-cast-section="stock" data-stock-key="${esc(key)}">${castVoiceOptions(defaults[key] || "")}</select></td>
        </tr>`;
      })
      .join("");
    const personaRows = personas
      .map(
        (item, index) => `<tr>
          <td><code>${esc(item.id)}</code></td>
          <td><input class="input" data-cast-section="persona" data-index="${index}" data-key="label" value="${esc(item.label)}" /></td>
          <td><select class="input" data-cast-section="persona" data-index="${index}" data-key="voice_id">${castVoiceOptions(item.voice_id)}</select></td>
          <td><button class="btn small" type="button" data-cast-del="persona" data-index="${index}">删除</button></td>
        </tr>`
      )
      .join("");
    const switchRows = switches
      .map(
        (item, index) => `<tr>
          <td><select class="input" data-cast-section="switch" data-index="${index}" data-key="at_chapter_id">${castChapterOptions(
            item.at_chapter_id,
            false
          )}</select></td>
          <td><select class="input" data-cast-section="switch" data-index="${index}" data-key="end_chapter_id">${castChapterOptions(
            item.end_chapter_id || "",
            true
          )}</select></td>
          <td><select class="input" data-cast-section="switch" data-index="${index}" data-key="persona_id">${castPersonaOptions(
            item.persona_id
          )}</select></td>
          <td><input class="input" data-cast-section="switch" data-index="${index}" data-key="reason" value="${esc(
            item.reason || ""
          )}" placeholder="必填：为什么从这里切换" /></td>
          <td><button class="btn small" type="button" data-cast-del="switch" data-index="${index}">删除</button></td>
        </tr>`
      )
      .join("");
    box.innerHTML = `
      <div class="card" style="padding:12px;margin-top:8px">
        <h3 style="margin:0 0 8px">音色池（6 个槽位起步）</h3>
        <p class="muted" style="margin:0 0 8px">槽位键是稳定键，保存后不要随意改名；同一槽位可被多个职业龙套复用。</p>
        <table class="table"><thead><tr><th>槽位</th><th>名称</th><th>音色</th><th>性别</th><th>年龄感</th><th></th></tr></thead>
        <tbody>${poolRows || '<tr><td colspan="6" class="muted">还没有音色池槽位</td></tr>'}</tbody></table>
        <button class="btn small" type="button" id="castPoolAdd">新增音色槽位</button>

        <h3 style="margin:16px 0 8px">龙套职业映射</h3>
        <p class="muted" style="margin:0 0 8px">键可以是音色池槽位（如 male_mid），也可以是具体职业槽位（如 waiter）；职业槽位优先。</p>
        <table class="table"><thead><tr><th>键</th><th>来源</th><th>音色</th></tr></thead><tbody>${
          stockRows || '<tr><td colspan="3" class="muted">还没有可映射的槽位</td></tr>'
        }</tbody></table>

        <h3 style="margin:16px 0 8px">旁白人格</h3>
        <table class="table"><thead><tr><th>ID</th><th>名称</th><th>音色</th><th></th></tr></thead>
        <tbody>${personaRows || '<tr><td colspan="4" class="muted">还没有旁白人格</td></tr>'}</tbody></table>
        <button class="btn small" type="button" id="castPersonaAdd">新增旁白人格</button>
        <label style="margin-left:12px">默认旁白：
          <select class="input" id="castDefaultPersona">${castPersonaOptions(castDraft.default_narrator_id)}</select>
        </label>

        <h3 style="margin:16px 0 8px">旁白切换点</h3>
        <p class="muted" style="margin:0 0 8px">区间含首尾；结束章留空表示延续到下一个切换点之前。区间不允许重叠或中间断开。</p>
        <table class="table"><thead><tr><th>起始章</th><th>结束章</th><th>人格</th><th>理由</th><th></th></tr></thead>
        <tbody>${switchRows || '<tr><td colspan="5" class="muted">没有切换点，全书使用默认旁白</td></tr>'}</tbody></table>
        <button class="btn small" type="button" id="castSwitchAdd">新增切换点</button>

        <h3 style="margin:16px 0 8px">每章旁白预览（当前已保存配置）</h3>
        <div id="castPlanPreview" class="muted">—</div>

        <h3 style="margin:16px 0 8px">角色试听（用本书真实台词）</h3>
        <p class="muted" style="margin:0 0 8px">优先取当前章节的一句台词，按所选音色合成到 <code>.ndw/cast_previews/</code>（只保留最近 12 条，不写章节、不进时间轴）。换几个候选音色连续点「试听这句」即可横向对比。</p>
        <div class="cast-preview">
          <div class="row">
            <select class="input" id="castPreviewCharacter">${castPreviewCharacterOptions(castPreview.character)}</select>
            <select class="input" id="castPreviewVoice">${castCandidateOptions(castPreview.voice)}</select>
            <button class="btn small" type="button" id="castPreviewRun">试听这句</button>
            <button class="btn small" type="button" id="castPreviewClear">清空试听缓存</button>
          </div>
          <p class="muted" id="castPreviewHint" style="margin:8px 0 0">—</p>
          <div class="cast-preview-log" id="castPreviewLog"></div>
        </div>

        <div id="castErrors" style="margin-top:10px;color:#c0392b"></div>
        <div style="margin-top:10px"><button class="btn" type="button" id="castSave">保存并检查</button></div>
      </div>`;
    renderCastPreviewLog();
  }

  /* 渲染试听历史（最多 6 条，方便 A/B 对比；页面刷新即清空）。 */
  function renderCastPreviewLog() {
    const box = document.getElementById("castPreviewLog");
    if (!box) return;
    box.innerHTML = (castPreview.log || [])
      .map(
        (item) =>
          `<div class="item">` +
          `<button class="btn small" type="button" data-preview-play="${esc(item.audio)}">▶</button>` +
          `<b>${esc(item.voice_name || item.voice_id)}</b>` +
          `<span>${esc(summary(item.text || "", 24))}</span>` +
          `<span class="muted">${esc(item.chapter_title || item.chapter_id || "")} · ${esc(item.line_id || "")}</span>` +
          `</div>`
      )
      .join("");
  }

  /* 试听一句：把当前角色 + 候选音色发给后端合成。 */
  async function runCastPreview() {
    const hint = document.getElementById("castPreviewHint");
    if (!NW.state.path) {
      NW.toast("先打开工程");
      return;
    }
    if (!castPreview.character) {
      NW.toast("先选一个角色");
      return;
    }
    if (hint) hint.textContent = "合成中…";
    try {
      const result = await NW.api("/api/ui/cast/preview", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          path: NW.state.path,
          character_id: castPreview.character,
          voice_id: castPreview.voice || undefined,
          chapter: NW.state.chapter || undefined,
        }),
        busy: "正在用本书台词试听…",
      });
      castPreview.log.unshift(result);
      castPreview.log = castPreview.log.slice(0, 6);
      if (hint) {
        hint.textContent = `已用「${result.chapter_title || result.chapter_id}」的台词试听：${summary(
          result.text || "",
          30
        )}（保留最近 ${result.kept} 条）`;
      }
      renderCastPreviewLog();
      if (result.audio) {
        const audio = new Audio(uiFileUrl(result.audio, NW.state.path));
        audio.play().catch(() => {});
      }
    } catch (error) {
      if (hint) hint.textContent = `试听失败：${error.message}`;
    }
  }

  /* 清空服务器上的试听临时文件。 */
  async function clearCastPreviews() {
    if (!NW.state.path) return;
    try {
      const result = await NW.api("/api/ui/cast/preview/clear", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: NW.state.path }),
      });
      castPreview.log = [];
      renderCastPreviewLog();
      const hint = document.getElementById("castPreviewHint");
      if (hint) hint.textContent = `已清空 ${result.removed || 0} 个试听文件`;
    } catch (error) {
      NW.toast(`清空失败：${error.message}`);
    }
  }

  /* 渲染每章旁白预览。 */
  function renderCastPlan(plan) {
    const box = el("castPlanPreview");
    if (!box) return;
    const rows = plan || [];
    if (!rows.length) {
      box.textContent = "还没有章节";
      return;
    }
    box.innerHTML = rows
      .map(
        (item) =>
          `<div>第${esc(item.index)}章 ${esc(summary(item.title, 20))} → <b>${esc(item.label)}</b>（${
            item.source === "switch" ? "切换" : "默认"
          }）</div>`
      )
      .join("");
  }

  /* 渲染校验错误。 */
  function renderCastErrors(errors) {
    const box = el("castErrors");
    if (!box) return;
    const list = errors || [];
    box.innerHTML = list.length ? `<b>未保存，请先修正：</b><ul>${list.map((item) => `<li>${esc(item)}</li>`).join("")}</ul>` : "";
  }

  /* 打开编辑器：拉取当前 cast 与章节/音色。 */
  async function openCastEditor() {
    if (!NW.state.path) {
      NW.toast("先打开工程");
      return;
    }
    const box = el("castEditor");
    if (box) {
      box.hidden = false;
      box.innerHTML = '<p class="muted">读取音色编排…</p>';
    }
    try {
      const data = await NW.api(`/api/ui/cast?path=${encodeURIComponent(NW.state.path)}`);
      const cast = data.cast || {};
      castDraft = {
        narrators: (cast.narrators || []).map((item) => ({ ...item })),
        narrator_switches: (cast.narrator_switches || []).map((item) => ({ ...item })),
        voice_pool: (cast.voice_pool || []).map((item) => ({ ...item })),
        stock_defaults: { ...(cast.stock_defaults || {}) },
        default_narrator_id: cast.default_narrator_id || "",
        unknown_voice_id: cast.unknown_voice_id || "",
        minor_fallback_voice_id: cast.minor_fallback_voice_id || "",
        narrator_fallback_voice_id: cast.narrator_fallback_voice_id || "",
      };
      castMeta = {
        chapters: data.chapters || [],
        voices: data.voices || [],
        characters: data.characters || [],
        stockSlots: data.stock_slots || [],
      };
      ensureDefaultPool();
      // 试听默认选第一个非旁白角色（保留用户上次选择）
      const previewable = (castMeta.characters || []).filter((item) => item.role !== "narrator");
      if (!castPreview.character || !previewable.some((item) => item.id === castPreview.character)) {
        castPreview.character = previewable.length ? previewable[0].id : "";
        castPreview.voice = "";
      }
      renderCastEditor();
      renderCastPlan(data.plan);
      renderCastErrors(data.errors);
    } catch (error) {
      if (box) box.textContent = `读取失败：${error.message}`;
    }
  }

  /* 处理编辑器内的输入、选择与删除。 */
  function handleCastEdit(event) {
    const target = event.target;
    if (!target) return;
    if (target.id === "castPreviewCharacter") {
      castPreview.character = target.value;
      castPreview.voice = "";
      renderCastEditor();
      return;
    }
    if (target.id === "castPreviewVoice") {
      castPreview.voice = target.value;
      return;
    }
    if (target.id === "castPreviewRun") {
      runCastPreview();
      return;
    }
    if (target.id === "castPreviewClear") {
      clearCastPreviews();
      return;
    }
    const previewPlay = target.closest("[data-preview-play]");
    if (previewPlay) {
      const audio = new Audio(uiFileUrl(previewPlay.getAttribute("data-preview-play"), NW.state.path));
      audio.play().catch(() => {});
      return;
    }
    if (target.id === "castPoolAdd") {
      castDraft.voice_pool.push({ slot: `slot_${castDraft.voice_pool.length + 1}`, label: "新音色槽位", voice_id: "" });
      renderCastEditor();
      return;
    }
    if (target.id === "castPersonaAdd") {
      const id = `npr_${Date.now().toString(36)}`;
      castDraft.narrators.push({ id, label: "新旁白人格", voice_id: "" });
      renderCastEditor();
      return;
    }
    if (target.id === "castSwitchAdd") {
      const first = (castMeta.chapters || [])[0];
      if (!first) {
        NW.toast("还没有章节，无法设置切换点");
        return;
      }
      const firstPersona = (castDraft.narrators || [])[0];
      if (!firstPersona) {
        NW.toast("请先新增一个旁白人格，再设置切换点");
        return;
      }
      castDraft.narrator_switches.push({
        at_chapter_id: first.id,
        end_chapter_id: "",
        persona_id: firstPersona ? firstPersona.id : "",
        reason: "",
        scope: "from_here",
      });
      renderCastEditor();
      return;
    }
    if (target.id === "castDefaultPersona") {
      castDraft.default_narrator_id = target.value;
      return;
    }
    if (target.id === "castSave") {
      saveCast();
      return;
    }
    const del = target.getAttribute("data-cast-del");
    if (del) {
      const index = Number(target.getAttribute("data-index"));
      if (del === "pool") castDraft.voice_pool.splice(index, 1);
      if (del === "persona") castDraft.narrators.splice(index, 1);
      if (del === "switch") castDraft.narrator_switches.splice(index, 1);
      renderCastEditor();
      return;
    }
    const section = target.getAttribute("data-cast-section");
    if (!section) return;
    const key = target.getAttribute("data-key");
    const value = target.value;
    if (section === "stock") {
      const stockKey = target.getAttribute("data-stock-key");
      if (value) castDraft.stock_defaults[stockKey] = value;
      else delete castDraft.stock_defaults[stockKey];
      return;
    }
    const index = Number(target.getAttribute("data-index"));
    const row = (castDraft[section === "pool" ? "voice_pool" : section === "persona" ? "narrators" : "narrator_switches"] || [])[index];
    if (!row) return;
    row[key] = value;
    if (event.type === "change" && (section === "pool" || section === "persona")) renderCastEditor();
  }

  /* 保存并重新跑检查。 */
  async function saveCast() {
    const box = el("castErrors");
    if (box) box.textContent = "保存中…";
    try {
      const result = await NW.api("/api/ui/cast", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: NW.state.path, cast: castDraft }),
        busy: "正在保存音色编排…",
      });
      if (!result.ok) {
        renderCastErrors(result.errors);
        return;
      }
      renderCastErrors([]);
      renderCastPlan(result.plan);
      if (result.check) renderCastCheck(result.check);
      NW.toast("音色编排已保存，并重新跑了检查");
    } catch (error) {
      if (box) box.textContent = `保存失败：${error.message}`;
    }
  }

  /* 把检查结果写回工具条。 */
  function renderCastCheck(result) {
    const out = el("castResult");
    const findings = (result && result.findings) || [];
    const errors = findings.filter((item) => item.level === "error").length;
    const warns = findings.filter((item) => item.level === "warn").length;
    if (out) out.textContent = `结论 ${(result && result.level) || "ok"}：error ${errors}，warn ${warns}`;
  }

  /* 注入音色编排工具条（不改 index.html，避免与并行改动冲突）。
   * 返回：无。
   */
  function injectCastPanel() {
    const list = el("charactersList");
    if (!list || document.getElementById("castPanel")) return;
    const panel = document.createElement("div");
    panel.id = "castPanel";
    panel.className = "row";
    panel.style.cssText = "gap:8px;align-items:center;margin-bottom:12px;flex-wrap:wrap";
    panel.innerHTML = `
      <button class="btn small" type="button" id="castEditBtn">编辑音色编排</button>
      <button class="btn small" type="button" id="castCheckBtn">音色编排检查</button>
      <button class="btn small" type="button" id="castBackfillBtn">批量生成音色描述</button>
      <button class="btn small" type="button" id="castDescFillBtn">补全缺失描述</button>
      <span class="muted" id="castResult"></span>`;
    list.parentNode.insertBefore(panel, list);
    const editor = document.createElement("div");
    editor.id = "castEditor";
    editor.hidden = true;
    list.parentNode.insertBefore(editor, list);
    editor.addEventListener("input", handleCastEdit);
    editor.addEventListener("change", handleCastEdit);
    editor.addEventListener("click", handleCastEdit);
    panel.querySelector("#castDescFillBtn").addEventListener("click", async () => {
      const out = document.getElementById("castResult");
      out.textContent = "补全中…";
      try {
        const result = await NW.api("/api/ui/characters/descriptions/fill", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: NW.state.path }),
          busy: "正在补全人物描述…",
        });
        const updated = (result.updated || []).length;
        const failures = result.failures || [];
        out.textContent = failures.length
          ? `已补 ${updated} 个，失败 ${failures.length} 个（可重跑只补失败项）`
          : `已补 ${updated} 个角色`;
        if (failures.length) {
          NW.toast(`补全失败：${failures.slice(0, 2).map((item) => `${item.name}（${item.message}）`).join("；")}`);
        } else if (!updated) {
          NW.toast("所有角色的描述都已齐全，无需补全", true);
        }
        load();
      } catch (error) {
        out.textContent = `补全失败：${error.message}`;
      }
    });
    panel.querySelector("#castEditBtn").addEventListener("click", async () => {
      const box = document.getElementById("castEditor");
      if (box && !box.hidden) {
        box.hidden = true;
        return;
      }
      await openCastEditor();
    });
    panel.querySelector("#castCheckBtn").addEventListener("click", async () => {
      const out = document.getElementById("castResult");
      out.textContent = "检查中…";
      try {
        const result = await NW.api(`/api/ui/cast/check?path=${encodeURIComponent(NW.state.path)}`);
        renderCastCheck(result);
        const findings = result.findings || [];
        if (findings.length) NW.toast(findings.slice(0, 3).map((item) => item.message).join("；"));
      } catch (error) {
        out.textContent = `检查失败：${error.message}`;
      }
    });
    panel.querySelector("#castBackfillBtn").addEventListener("click", async () => {
      const out = document.getElementById("castResult");
      out.textContent = "生成中…";
      try {
        const result = await NW.api("/api/ui/cast/backfill", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: NW.state.path }),
          busy: "正在批量生成音色描述…",
        });
        out.textContent = `已补 ${result.changed || 0} 个角色`;
        load();
      } catch (error) {
        out.textContent = `生成失败：${error.message}`;
      }
    });
  }

  window.Pages.characters = {
    init() {
      bind();
    },
    activate() {
      bind();
      injectCastPanel();
      load();
      if (window.VoiceLibrary) window.VoiceLibrary.activate();
    },
  };
})();
