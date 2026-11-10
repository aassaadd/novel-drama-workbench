/* 共享字段工具：放大编辑 + 按区块生成。
 *
 * 只做「字段级」的通用交互，不关心页面业务、不直接落盘：
 *   - NWTools.amplify(input, opts)      复用 NWEditor 放大编辑单个输入框
 *   - NWTools.bindAmplify(root)         事件委托，绑定 [data-amplify]
 *   - NWTools.generateBlock(options)    调 /api/ui/setting/generate，预览后回填表单
 * 页面只需要在激活时调用一次 bindAmplify，并在「✨ 生成」按钮上调用 generateBlock。
 */

(function () {
  /* 区块中文名：预览标题与需求输入框用 */
  const SECTION_LABELS = {
    world: "世界设计",
    premise: "故事梗概",
    style: "文风设定",
    character: "人物档案",
  };

  /* 字段中文名：未登记的字段直接显示原始 key，便于排查 */
  const FIELD_LABELS = {
    // 世界
    name: "名称",
    era: "时代",
    summary: "摘要",
    rules: "规则",
    factions: "势力",
    locations: "地点",
    glossary: "词条",
    visual_style: "视觉基调",
    // 梗概
    logline: "一句话概要",
    theme: "主题",
    core_conflict: "核心冲突",
    ending_intent: "结局意图",
    target_audience: "目标读者",
    target_length: "目标篇幅",
    // 文风
    tone: "基调",
    pov: "视角",
    tense: "时态",
    prose_style: "文风",
    forbidden: "禁止写法",
    // 人物
    role: "定位",
    gender: "性别",
    age_range: "年龄区间",
    personality: "性格",
    background: "小传",
    situation: "处境",
    appearance_prompt: "外貌提示词",
    aliases: "别名",
    relationships: "关系",
  };

  /* 各区块的字段展示顺序：未列出的字段排在后面 */
  const FIELD_ORDER = {
    world: ["name", "era", "summary", "visual_style", "rules", "factions", "locations", "glossary"],
    premise: ["logline", "theme", "core_conflict", "ending_intent", "target_audience", "target_length"],
    style: ["tone", "pov", "tense", "prose_style", "visual_style", "forbidden"],
    character: ["name", "role", "gender", "age_range", "personality", "background", "situation", "appearance_prompt", "aliases", "relationships"],
  };

  /* 生成结果里的内部字段：预览不展示，但回填时原样保留 */
  const INTERNAL_FIELDS = new Set(["id", "created_at", "updated_at", "cover_asset"]);

  const boundRoots = new WeakSet(); // 已绑定的委托根节点，保证重复 bind 不重复监听
  const el = (id) => document.getElementById(id);
  let previewMask = null; // 当前生成预览浮层（单例）
  let fieldSeq = 0;       // textarea id 自增序号

  /* 判断一个值是否算「已有内容」：字符串看去空白，数组/对象递归判断。
   * 参数：value 任意字段值。
   * 返回：布尔值。
   */
  function hasContent(value) {
    if (value === null || value === undefined) return false;
    if (typeof value === "string") return Boolean(value.trim());
    if (Array.isArray(value)) return value.some((item) => hasContent(item));
    if (typeof value === "object") return Object.keys(value).some((key) => hasContent(value[key]));
    return true;
  }

  /* 按钮 loading：记住原文并禁用，结束后恢复；同一按钮可重复调用。
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

  /* 用通用编辑器放大编辑一个输入框：保存后写回并触发 input 事件，让页面自己的字数统计等逻辑跟上。
   * 参数：input 输入框节点或元素 id；opts {maxLength, hint, title}，均可选。
   * 返回：无。
   */
  function amplify(input, opts) {
    const node = typeof input === "string" ? el(input) : input;
    if (!node || !window.NWEditor) return;
    const options = opts || {};
    NWEditor.open({
      title: options.title || "放大编辑",
      value: node.value,
      maxLength: options.maxLength,
      hint: options.hint,
      onSave(text) {
        node.value = text;
        node.dispatchEvent(new Event("input", { bubbles: true }));
      },
    });
  }

  /* 事件委托：点击 [data-amplify] 时找到同一字段容器里的输入框并放大编辑。
   * 支持 data-target="元素id" 直接指定目标；否则按 .gen-field / .field-action / .stack 向上找容器。
   * 参数：root 委托根节点，缺省 document；重复调用同一根节点不会重复绑定。
   * 返回：无。
   */
  function bindAmplify(root) {
    const host = root || document;
    if (boundRoots.has(host)) return;
    boundRoots.add(host);
    host.addEventListener("click", (event) => {
      // 同一事件可能在 document 与页面容器上各绑一次，这里只处理一次
      if (event.__nwAmplifyHandled) return;
      const trigger = event.target.closest("[data-amplify]");
      if (!trigger) return;
      event.__nwAmplifyHandled = true;
      event.preventDefault();
      const targetId = trigger.dataset.target || "";
      let input = targetId ? el(targetId) : null;
      if (!input) {
        const holder = trigger.closest(".gen-field, .field-action, .stack") || trigger.parentElement;
        input = holder ? holder.querySelector("input, textarea") : null;
      }
      if (!input) return;
      amplify(input, {
        maxLength: Number(trigger.dataset.maxLength) || 0,
        title: trigger.dataset.title || "",
      });
    });
  }

  /* 把对象数组的单项转成「主字段|说明」一行；数组值用顿号连接，id 不展示。
   * 参数：item 对象项。
   * 返回：一行文本。
   */
  function objectLine(item) {
    return Object.keys(item)
      .filter((key) => key !== "id")
      .map((key) => {
        const cell = item[key];
        if (Array.isArray(cell)) return cell.join("、");
        if (cell && typeof cell === "object") return "";
        return cell === null || cell === undefined ? "" : String(cell);
      })
      .join("|");
  }

  /* 把字段值序列化成预览框里可直接编辑的文本。
   * 参数：value 任意字段值。
   * 返回：字符串（数组一行一项，对象数组用「主字段|说明」）。
   */
  function fieldText(value) {
    if (value === null || value === undefined) return "";
    if (Array.isArray(value)) {
      return value
        .map((item) => {
          if (item === null || item === undefined) return "";
          if (typeof item === "object") return objectLine(item);
          return String(item);
        })
        .filter((line) => line !== "")
        .join("\n");
    }
    if (typeof value === "object") return JSON.stringify(value, null, 2);
    return String(value);
  }

  /* 把预览框里的多行文本还原成字段值；结构参考生成结果的原始值。
   * 参数：text 用户编辑后的文本；template 原始字段值（用于判断字符串数组 / 对象数组）。
   * 返回：还原后的字段值。
   */
  function parseFieldText(text, template) {
    const raw = String(text || "");
    if (!Array.isArray(template)) {
      if (template && typeof template === "object") {
        try {
          return JSON.parse(raw);
        } catch (error) {
          return raw;
        }
      }
      return raw;
    }
    const sample = template.find((item) => item && typeof item === "object" && !Array.isArray(item));
    const lines = raw
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean);
    if (!sample) return lines; // 字符串数组：一行一项
    const keys = Object.keys(sample).filter((key) => key !== "id");
    return lines.map((line, index) => {
      const parts = line.split("|");
      const item = {};
      keys.forEach((key, partIndex) => {
        const cell = String(parts[partIndex] === undefined ? "" : parts[partIndex]).trim();
        item[key] = Array.isArray(sample[key])
          ? cell
            ? cell.split(/[、,，;；]+/).map((value) => value.trim()).filter(Boolean)
            : []
          : cell;
      });
      // 复用模板里同下标项的 id，保证地点等需要 id 的结构在未编辑时仍然合法
      const matched = template[index];
      if (matched && typeof matched === "object" && matched.id) item.id = matched.id;
      return item;
    });
  }

  /* 计算预览里要展示的字段顺序：先按区块顺序，再补未知字段。
   * 参数：section 区块名；fields 生成结果。
   * 返回：字段 key 数组。
   */
  function fieldKeys(section, fields) {
    const all = Object.keys(fields || {}).filter((key) => !INTERNAL_FIELDS.has(key));
    const known = (FIELD_ORDER[section] || []).filter((key) => all.includes(key));
    all.forEach((key) => {
      if (!known.includes(key)) known.push(key);
    });
    return known;
  }

  /* 关闭生成预览浮层：移除 DOM 与键盘监听。
   * 参数：无。
   * 返回：无。
   */
  function closePreview() {
    if (!previewMask) return;
    document.removeEventListener("keydown", onPreviewKeydown, true);
    previewMask.remove();
    previewMask = null;
  }

  /* 预览浮层键盘：Esc 关闭（丢弃）。
   * 参数：event 键盘事件。
   * 返回：无。
   */
  function onPreviewKeydown(event) {
    if (event.key !== "Escape") return;
    // 预览里再打开「⤢ 放大编辑」时，Esc 应该只关掉最上层的编辑器
    if (previewMask && previewMask.nextElementSibling) return;
    event.preventDefault();
    closePreview();
  }

  /* 打开多字段生成预览：每个字段一个可编辑 textarea，确认后把值交给 onApply。
   * 参数：options {section, brief, fields, onApply}。
   * 返回：无。
   */
  function openPreview(options) {
    const opts = options || {};
    const fields = opts.fields || {};
    const keys = fieldKeys(opts.section, fields);
    const label = SECTION_LABELS[opts.section] || opts.section || "内容";
    closePreview();

    previewMask = document.createElement("div");
    previewMask.className = "modal-mask";

    const dialog = document.createElement("div");
    dialog.className = "modal-editor gen-preview";
    dialog.setAttribute("role", "dialog");
    dialog.setAttribute("aria-modal", "true");

    const title = document.createElement("h3");
    title.className = "modal-title";
    title.textContent = `${label} · 生成预览`;
    dialog.appendChild(title);

    const brief = document.createElement("p");
    brief.className = "modal-hint";
    brief.textContent = `需求：${opts.brief && String(opts.brief).trim() ? opts.brief : "（未填写）"}；可直接修改后再填入表单。`;
    dialog.appendChild(brief);

    const list = document.createElement("div");
    list.className = "gen-fields";
    const entries = [];

    keys.forEach((key) => {
      const original = fields[key];
      const text = fieldText(original);
      const wrapper = document.createElement("div");
      wrapper.className = "gen-field";

      const head = document.createElement("div");
      head.className = "gen-field-head";

      const nameNode = document.createElement("span");
      nameNode.className = "gen-field-label";
      nameNode.textContent = FIELD_LABELS[key] || key;
      head.appendChild(nameNode);

      const count = document.createElement("span");
      count.className = "gen-field-count";
      count.textContent = `${text.length} 字`;
      head.appendChild(count);

      const zoom = document.createElement("button");
      zoom.type = "button";
      zoom.className = "btn small amplify-btn";
      zoom.textContent = "⤢";
      zoom.title = "放大编辑";
      zoom.setAttribute("data-amplify", "");
      const inputId = `gen-field-${++fieldSeq}`;
      zoom.dataset.target = inputId;
      zoom.dataset.title = FIELD_LABELS[key] || key;
      head.appendChild(zoom);

      const textarea = document.createElement("textarea");
      textarea.className = "gen-input";
      textarea.id = inputId;
      textarea.value = text;
      textarea.rows = Math.min(8, Math.max(2, text.split("\n").length));
      textarea.addEventListener("input", () => {
        count.textContent = `${textarea.value.length} 字`;
      });

      wrapper.appendChild(head);
      wrapper.appendChild(textarea);
      list.appendChild(wrapper);
      entries.push({ key, original, textarea });
    });

    dialog.appendChild(list);

    const foot = document.createElement("div");
    foot.className = "modal-foot";

    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "btn";
    cancel.textContent = "取消";
    cancel.addEventListener("click", closePreview);
    foot.appendChild(cancel);

    const apply = document.createElement("button");
    apply.type = "button";
    apply.className = "btn primary";
    apply.textContent = "填入表单";
    apply.addEventListener("click", () => {
      const result = Object.assign({}, fields);
      entries.forEach((entry) => {
        // 未改动的字段原样保留（包括 id / 时间戳），只解析真正编辑过的字段
        result[entry.key] =
          entry.textarea.value === fieldText(entry.original)
            ? entry.original
            : parseFieldText(entry.textarea.value, entry.original);
      });
      closePreview();
      if (typeof opts.onApply === "function") opts.onApply(result);
    });
    foot.appendChild(apply);

    dialog.appendChild(foot);
    previewMask.appendChild(dialog);

    previewMask.addEventListener("click", (event) => {
      if (event.target === previewMask) closePreview();
    });
    document.body.appendChild(previewMask);
    document.addEventListener("keydown", onPreviewKeydown, true);
  }

  /* 调后端生成一个区块并弹出预览。
   * 参数：section 区块名；path 工程目录；brief 需求；opts {button, targetId, onApply}。
   * 返回：Promise，无。
   */
  async function runGenerate(section, path, brief, opts) {
    const button = opts.button;
    setLoading(button, true, "生成中…");
    try {
      const body = { path, section, brief };
      if (opts.targetId) body.target_id = opts.targetId;
      const result = await window.NW.api("/api/ui/setting/generate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const fields = (result && result.fields) || {};
      if (!Object.keys(fields).length) {
        NW.toast("生成结果为空，请补充需求后重试");
        return;
      }
      openPreview({ section, brief, fields, onApply: opts.onApply });
    } catch (error) {
      NW.toast(error.message || "生成失败");
    } finally {
      setLoading(button, false);
    }
  }

  /* 区块生成入口：先收集需求，再生成预览，最后交给调用方回填表单。
   * 参数：options {section, targetId, button, fields, onApply}
   *   - section  world / premise / style / character
   *   - targetId 改写目标（世界 ID / 角色 ID），可选
   *   - button   触发按钮，生成中会禁用并显示「生成中…」
   *   - fields   当前表单值，用于判断是否已有内容
   *   - onApply(fields) 用户点「填入表单」后的回调
   * 返回：无。
   */
  function generateBlock(options) {
    const opts = options || {};
    const section = String(opts.section || "").trim();
    if (!section) return;
    if (!window.NWEditor) return;
    const path = (window.NW && NW.state && NW.state.path) || "";
    if (!path) {
      NW.toast("先打开或新建工程");
      return;
    }
    if (hasContent(opts.fields) && !window.confirm("该区块已有内容，生成会整体覆盖，继续？")) return;
    const label = SECTION_LABELS[section] || section;
    NWEditor.openBrief({
      title: `生成${label}需求`,
      placeholder: `想怎么写？留空则按现有设定自动生成（${label}）`,
      onSave(brief) {
        runGenerate(section, path, brief, opts);
      },
    });
  }

  window.NWTools = { amplify, bindAmplify, generateBlock };
})();
