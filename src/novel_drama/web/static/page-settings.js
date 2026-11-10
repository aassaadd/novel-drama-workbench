/* 模型设置页：在页面上配置写作 LLM、音频 TTS、图像/视频（ComfyUI）与模型下载。
 *
 * 读写都走 /api/config：
 *   - GET 拿生效配置（config.yaml + 覆盖层合并后的结果）与 overrides（页面覆盖过的键）；
 *   - POST 提交 `{values: {点号路径: 值}}`，值为 null 表示删除覆盖项、回落 config.yaml。
 * 页面只提交白名单内的键（响应里的 editable），api_key 留空即不改；
 * provider 的 enabled 只读展示，不在这里改。
 *
 * 「模型与下载」卡片读 /api/ui/models，下载走 /api/ui/models/install 后台任务 +
 * /api/ui/models/install/{job_id} 轮询；这些控件不是配置项（不带 data-key），
 * 不会混进 /api/config 的提交。
 */

(function () {
  let bound = false; // 事件是否已绑定（只绑一次）
  let busy = false; // 保存中，防重复提交
  let data = null; // 最近一次 GET /api/config 的结果
  let pending = {}; // 点了「恢复」的键：保存时一律按 null 提交
  let health = null; // POST /api/ui/comfy/health 的结果
  let models = null; // GET /api/ui/models 的结果
  let modelsError = ""; // 模型清单读取失败原因
  let jobs = {}; // "group:key" → 下载任务 {job_id, status, logs, result}
  let pollingKey = ""; // 正在轮询的任务键（同时只允许一个下载任务）
  let pollTimer = null; // 轮询定时器

  /* 取元素。 */
  const el = (id) => document.getElementById(id);

  /* 该键是否被页面覆盖层覆盖（GET 返回的 overrides）。
   * 参数：key 点号路径。
   * 返回：布尔值。
   */
  function isOverridden(key) {
    return Boolean(data && data.overrides && Object.prototype.hasOwnProperty.call(data.overrides, key));
  }

  /* 该键是否允许在页面改（以接口返回的 editable 为准）。
   * 参数：key 点号路径。
   * 返回：布尔值。
   */
  function isEditable(key) {
    return Boolean(data && Array.isArray(data.editable) && data.editable.includes(key));
  }

  /* 一行只读「标签 → 值」。
   * 参数：label 中文标签；value 值。
   * 返回：HTML 字符串。
   */
  function readRow(label, value) {
    return `<div class="kv-row"><span class="kv-key">${esc(label)}</span><span class="kv-val">${esc(value)}</span></div>`;
  }

  /* 构建一个可编辑字段块（含「页面覆盖」徽标与「恢复」按钮）。
   * 参数：key 点号路径；label 中文标签；input 输入控件 HTML；hint 可选小字说明。
   * 返回：HTML 字符串。
   */
  function fieldBlock(key, label, input, hint) {
    const over = isOverridden(key) && !pending[key];
    const badge = over ? '<span class="override-badge">页面覆盖</span>' : "";
    const revert = over ? `<button class="btn small" type="button" data-revert="${esc(key)}">恢复</button>` : "";
    return `<div class="settings-field">
      <div class="settings-label"><span>${esc(label)}</span>${badge}${revert}</div>
      ${input}
      ${hint ? `<span class="settings-hint">${esc(hint)}</span>` : ""}
    </div>`;
  }

  /* 体积转人读单位（模型动辄十几 GB，不能用 core.js 里只到 MB 的 humanSize）。
   * 参数：size 字节数。
   * 返回：形如 `11.3 GB` 的字符串；0 返回 `—`。
   */
  function modelBytes(size) {
    let value = Number(size || 0);
    if (!value) return "—";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let index = 0;
    while (value >= 1024 && index < units.length - 1) {
      value /= 1024;
      index += 1;
    }
    return index ? `${value.toFixed(1)} ${units[index]}` : `${value} B`;
  }

  /* 许可门禁的中文说明。
   * 参数：gate 门禁取值（none / approval_required / block / retired）。
   * 返回：中文字符串。
   */
  function gateText(gate) {
    return { none: "可直接用", approval_required: "需确认授权", block: "禁止商用", retired: "已退役" }[gate] || gate || "—";
  }

  /* ComfyUI 连接状态徽标；未检查显示「检查中」，连接失败显示红色「未连接」。
   * 参数：无。
   * 返回：HTML 字符串。
   */
  function healthBadge() {
    if (!health) return '<span class="state-badge s-pending"><span class="dot"></span>检查中…</span>';
    if (health.ok) {
      return `<span class="state-badge s-ready"><span class="dot"></span>已连接 · 版本 ${esc(health.version || "未知")}</span>`;
    }
    return '<span class="state-badge s-failed"><span class="dot"></span>未连接</span>';
  }

  /* 连接失败时的红色说明（成功或未检查时为空）。
   * 参数：无。
   * 返回：HTML 字符串。
   */
  function healthWarnHtml() {
    if (!health || !health.message) return "";
    const badTemplate = (health.templates || []).some((item) => item.ok === false);
    if (health.ok && !badTemplate) return "";
    return `<div class="settings-warn">${esc(health.message)}</div>`;
  }

  /* 连接成功时的灰色说明（版本 / 显存 / 队列 / 耗时），失败时为空。
   * 参数：无。
   * 返回：HTML 字符串。
   */
  function healthDetailHtml() {
    if (!health || !health.ok) return "";
    const badTemplate = (health.templates || []).some((item) => item.ok === false);
    if (badTemplate) return "";
    const latency = health.latency_ms ? ` · 耗时 ${health.latency_ms}ms` : "";
    return `<div class="settings-hint">${esc(health.message || "ComfyUI 连通正常")}${esc(latency)}</div>`;
  }

  /* 渲染写作模型卡片（接口地址 / 模型 / API Key / 采样参数）。
   * 参数：writing GET 返回的 writing 段。
   * 返回：HTML 字符串。
   */
  function renderWriting(writing) {
    const models = (writing.available_models || [])
      .map((item) => `<option value="${esc(item)}"></option>`)
      .join("");
    return `<section class="settings-card">
      <h3>写作模型（LLM）</h3>
      <div class="settings-grid">
        ${fieldBlock(
          "omlx.endpoint",
          "接口地址",
          `<input data-key="omlx.endpoint" value="${esc(writing.endpoint || "")}" placeholder="http://127.0.0.1:9000" spellcheck="false" />`
        )}
        ${fieldBlock(
          "omlx.model",
          "模型",
          `<input data-key="omlx.model" list="setModelList" value="${esc(writing.model || "")}" placeholder="如 Qwen3.8-27B-oQ4e-mtp" spellcheck="false" />
          <datalist id="setModelList">${models}</datalist>`
        )}
        ${fieldBlock(
          "omlx.api_key",
          "API Key",
          `<input data-key="omlx.api_key" type="password" value="" autocomplete="new-password" placeholder="${esc(writing.api_key_hint || "未配置")}" />`,
          "留空表示不改；只在需要换 key 时才填。"
        )}
        ${fieldBlock(
          "omlx.temperature",
          "temperature",
          `<input data-key="omlx.temperature" type="number" step="0.05" min="0" max="2" value="${esc(writing.temperature ?? "")}" />`
        )}
        ${fieldBlock(
          "omlx.max_tokens",
          "max_tokens",
          `<input data-key="omlx.max_tokens" type="number" min="1" value="${esc(writing.max_tokens ?? "")}" />`
        )}
      </div>
      <div class="kv settings-readonly">${readRow("上下文窗口", writing.context_window ?? "—")}</div>
    </section>`;
  }

  /* 渲染只读的 provider 表（enabled 只能在配置文件里改）。
   * 参数：providers GET 返回的 tts.providers。
   * 返回：HTML 字符串。
   */
  function renderProviders(providers) {
    const rows = (providers || [])
      .map(
        (item) => `<tr>
          <td>${esc(item.provider_id)}</td>
          <td>${esc(item.display_name || "")}</td>
          <td>${esc(item.backend || "")}</td>
          <td class="wrap">${esc(item.weights_path || "")}</td>
          <td>${esc(item.quality_tier || "")}</td>
          <td>${item.commercial_use ? "是" : "否"}</td>
          <td>${item.enabled ? "已启用" : "未启用"}</td>
        </tr>`
      )
      .join("");
    return `<div class="settings-table">
      <table class="grid">
        <thead><tr>
          <th>provider_id</th><th>显示名</th><th>后端</th><th>权重路径</th><th>质量档</th><th>商用</th><th>是否启用</th>
        </tr></thead>
        <tbody>${rows || '<tr><td colspan="7" class="muted">配置里没有 provider</td></tr>'}</tbody>
      </table>
    </div>`;
  }

  /* 渲染音频 TTS 卡片（默认 provider / 优先级顺序 / 只读 provider 表）。
   * 参数：tts GET 返回的 tts 段。
   * 返回：HTML 字符串。
   */
  function renderTts(tts) {
    const providers = tts.providers || [];
    const current = tts.default_provider_id || "";
    const options = providers
      .map((item) => {
        const text = `${item.display_name || item.provider_id} · ${item.enabled ? "已启用" : "未启用"}`;
        return `<option value="${esc(item.provider_id)}"${item.provider_id === current ? " selected" : ""}>${esc(text)}</option>`;
      })
      .join("");
    // 当前默认值不在列表里时补一条，避免下拉静默丢掉配置
    const extra =
      current && !providers.some((item) => item.provider_id === current)
        ? `<option value="${esc(current)}" selected>${esc(current)}（未识别）</option>`
        : "";
    const order = (tts.provider_order || []).join(", ");
    return `<section class="settings-card">
      <h3>音频 TTS</h3>
      <div class="settings-grid">
        ${fieldBlock("tts.default_provider_id", "默认 provider", `<select data-key="tts.default_provider_id">${extra}${options}</select>`)}
        ${fieldBlock(
          "tts.provider_order",
          "优先级顺序",
          `<input data-key="tts.provider_order" value="${esc(order)}" placeholder="indextts_25, cosyvoice3_mlx" spellcheck="false" />`,
          "逗号分隔，从左到右依次回落。"
        )}
      </div>
      <h4 class="settings-subtitle">可用 provider（只读，启用状态请在配置文件里改）</h4>
      ${renderProviders(providers)}
    </section>`;
  }

  /* 取某个模板配置项当前选中的模板 ID：控件已渲染时以控件为准，否则回落到配置值。
   * 参数：key 点号路径（如 comfyui.templates.image）。
   * 返回：模板 ID 字符串（可能为空）。
   */
  function selectedTemplate(key) {
    const node = document.querySelector(`#setBody [data-key="${key}"]`);
    if (node) return String(node.value || "");
    const templates = (data && data.image && data.image.templates) || {};
    return String(templates[key.split(".").pop()] || "");
  }

  /* 渲染一个模板下拉：选项来自清单里对应 kind 的模板。
   * 参数：key 点号路径；label 中文标签；kind image / video；current 当前值；manifest 清单。
   * 返回：HTML 字符串。
   */
  function templateSelect(key, label, kind, current, manifest) {
    const items = (manifest || []).filter((item) => item.kind === kind);
    const options = items
      .map(
        (item) =>
          `<option value="${esc(item.id)}"${item.id === current ? " selected" : ""}>${esc(item.display_name || item.id)}</option>`
      )
      .join("");
    // 当前值不在清单里时补一条「未识别」，避免下拉静默丢掉配置
    const extra =
      current && !items.some((item) => item.id === current)
        ? `<option value="${esc(current)}" selected>${esc(current)}（未识别）</option>`
        : "";
    return fieldBlock(key, label, `<select data-key="${esc(key)}">${extra}${options}</select>`);
  }

  /* 一行只读固定参数。
   * 参数：label 中文名；value 参数值（null / 空显示「—」）。
   * 返回：HTML 字符串。
   */
  function fixedParam(label, value) {
    const text = value === null || value === undefined || value === "" ? "—" : String(value);
    return `<div class="fixed-param"><span>${esc(label)}</span><b>${esc(text)}</b></div>`;
  }

  /* 渲染单个模板的只读区：固定参数 + 尺寸策略 + 依赖模型（不生成任何输入控件）。
   * 参数：label 中文标题；kind image / video；current 当前模板 ID；manifest 模板清单。
   * 返回：HTML 字符串。
   */
  function templateInfoBlock(label, kind, current, manifest) {
    const head = `<h5 class="settings-subtitle">${esc(label)}${current ? ` · ${esc(current)}` : ""}</h5>`;
    if (!current) {
      return `<div class="model-group">${head}<div class="settings-hint">未选择模板</div></div>`;
    }
    const entry = (manifest || []).find((item) => item.id === current && item.kind === kind) || null;
    if (!entry) {
      return `<div class="model-group">${head}<div class="settings-warn">当前值 ${esc(current)} 未识别：检查模板目录与清单</div></div>`;
    }
    const fixed = entry.fixed || {};
    const params = [
      ["模型", fixed.model],
      ["LoRA", fixed.lora],
      ["步数", fixed.steps],
      ["CFG", fixed.cfg],
      ["采样器", fixed.sampler],
      ["调度器", fixed.scheduler],
      ["帧数", fixed.length],
      ["帧率", fixed.fps],
    ]
      .map(([name, value]) => fixedParam(name, value))
      .join("");
    const sizes = Object.entries(entry.size_map || {})
      .map(([ratio, wh]) => `${ratio} → ${Array.isArray(wh) ? `${wh[0]}×${wh[1]}` : wh}`)
      .join("；");
    const deps = (entry.models || [])
      .map((item) => `<div class="fixed-param"><span>依赖模型</span><b>${esc(item)}</b></div>`)
      .join("");
    return `<div class="model-group">
      ${head}
      <div class="fixed-params">${params}</div>
      <div class="fixed-params">${fixedParam("尺寸策略", sizes)}</div>
      <div class="fixed-params">${deps || fixedParam("依赖模型", "")}</div>
    </div>`;
  }

  /* 渲染三条模板的只读区（初始渲染与切换模板后共用）。
   * 参数：fromData 为 true 时取配置里的值（整页重渲染尚未入 DOM 时用），否则取当前控件值。
   * 返回：HTML 字符串。
   */
  function templateInfoHtml(fromData) {
    const manifest = (data && data.image && data.image.templates_manifest) || [];
    const templates = (data && data.image && data.image.templates) || {};
    const pick = (key) => {
      const kind = key.includes("video") ? "video" : "image";
      const current = fromData ? String(templates[key.split(".").pop()] || "") : selectedTemplate(key);
      if (current) return current;
      // 配置为空时下拉会自动选中第一条，这里同步显示第一条，避免只读区与下拉不一致
      const first = manifest.find((item) => item.kind === kind);
      return first ? first.id : "";
    };
    return [
      ["图片模板", "image", "comfyui.templates.image"],
      ["文生视频模板", "video", "comfyui.templates.video_t2v"],
      ["图生视频模板", "video", "comfyui.templates.video_i2v"],
    ]
      .map(([label, kind, key]) => templateInfoBlock(label, kind, pick(key), manifest))
      .join("");
  }

  /* 只刷新只读区，不重渲染整页——避免丢掉用户还没保存的其它输入。
   * 参数：无。
   * 返回：无。
   */
  function refreshTemplateInfo() {
    const box = el("setTplInfo");
    if (box) box.innerHTML = templateInfoHtml(false);
  }

  /* 渲染图像 / 视频卡片（连接状态、ComfyUI 地址、三个模板下拉 + 只读参数）。
   * 参数：image GET 返回的 image 段。
   * 返回：HTML 字符串。
   */
  function renderImage(image) {
    const manifest = image.templates_manifest || [];
    const templates = image.templates || {};
    const missing = image.templates_dir_exists === false;
    const warn = missing ? '<div class="settings-warn">模板目录不存在，图像/视频阶段会没有可用模板</div>' : "";
    return `<section class="settings-card">
      <div class="card-head"><h3>图像 / 视频（ComfyUI）</h3>
        <span class="health-slot">
          <button class="btn small" type="button" id="setHealthCheck">检测连接</button>
          <span id="setHealthBadge">${healthBadge()}</span>
        </span>
      </div>
      <div id="setHealthWarn">${healthWarnHtml()}</div>
      <div id="setHealthDetail">${healthDetailHtml()}</div>
      <div class="settings-grid">
        ${fieldBlock(
          "comfyui.base_url",
          "地址",
          `<input data-key="comfyui.base_url" value="${esc(image.base_url || "")}" placeholder="http://127.0.0.1:8188" spellcheck="false" />`
        )}
        ${templateSelect("comfyui.templates.image", "图片模板", "image", templates.image || "", manifest)}
        ${templateSelect("comfyui.templates.video_t2v", "文生视频模板", "video", templates.video_t2v || "", manifest)}
        ${templateSelect("comfyui.templates.video_i2v", "图生视频模板", "video", templates.video_i2v || "", manifest)}
      </div>
      <div class="kv settings-readonly">${readRow("模板目录", image.templates_dir || "—")}${readRow("ComfyUI 模型目录", image.models_dir || (models && models.dirs && models.dirs.comfy_models_dir) || "—")}</div>
      <div id="setTplInfo">${templateInfoHtml(true)}</div>
      ${warn}
    </section>`;
  }

  /* 单个模型行的下载状态（运行中 / 完成 / 失败 + 日志尾部）。
   * 参数：id 任务键（group:key）。
   * 返回：HTML 字符串。
   */
  function jobHtml(id) {
    const rec = jobs[id];
    if (!rec) return "";
    const state = rec.status === "running" ? "is-running" : rec.status === "done" ? "is-done" : "is-failed";
    const label = { running: "运行中", done: "完成", failed: "失败" }[rec.status] || rec.status;
    const logs = (rec.logs || []).slice(-8).join("\n");
    return `<div class="dl-status ${state}">${esc(label)}${logs ? "\n" + esc(logs) : ""}</div>`;
  }

  /* 渲染一组模型表（音频 / 图像 / 视频）。
   * 参数：title 分组标题；group 组名（audio/image/video）；groupData 接口返回的分组数据。
   * 返回：HTML 字符串。
   */
  function renderModelGroup(title, group, groupData) {
    const rows = (groupData && groupData.models) || [];
    const message = (groupData && groupData.message) || "";
    const body = rows.length
      ? rows.map((item) => modelRow(group, item)).join("")
      : '<tr><td colspan="6" class="muted">没有可展示的模型</td></tr>';
    return `<div class="model-group">
      <h4>${esc(title)}</h4>
      ${message ? `<div class="settings-warn">${esc(message)}</div>` : ""}
      <div class="model-table">
        <table class="grid">
          <thead><tr><th>名称</th><th>本地状态</th><th>体积</th><th>许可</th><th>线上地址</th><th>操作</th></tr></thead>
          <tbody>${body}</tbody>
        </table>
      </div>
    </div>`;
  }

  /* 渲染单个模型行：状态 / 体积 / 许可 / 线上地址 / 下载与重新解析。
   * 参数：group 组名；item 模型行。
   * 返回：HTML 字符串（含状态行时是两行）。
   */
  function modelRow(group, item) {
    const key = String(item.key || "");
    const id = `${group}:${key}`;
    const present = Boolean(item.present) && item.complete !== false;
    const gate = item.license_gate || "none";
    const status = present ? "就位" : item.present ? "不完整" : "缺失";
    const license = `${item.license || "—"} · ${gateText(gate)}`;
    const address = item.repo
      ? item.hf_filename
        ? `${item.repo} / ${item.hf_filename}`
        : `${item.repo}（整目录）`
      : "需手工指定";
    const error = !item.repo && item.error ? `<div class="settings-hint">解析失败：${esc(String(item.error).slice(0, 160))}</div>` : "";
    const manual =
      !item.repo && item.downloadable !== false
        ? `<details class="manual-box">
            <summary>手工指定</summary>
            <input data-manual-field="repo" placeholder="仓库，如 city96/Qwen-Image-gguf" spellcheck="false" />
            <input data-manual-field="hf_filename" placeholder="仓库内文件名" spellcheck="false" />
            <input data-manual-field="url" placeholder="或直接填下载地址 https://…" spellcheck="false" />
            <button class="btn small" type="button" data-manual-save="${esc(group)}" data-mkey="${esc(key)}">保存来源</button>
          </details>`
        : "";
    const check =
      gate !== "none"
        ? `<label class="license-check"><input type="checkbox" data-license="${esc(id)}" />已理解许可限制</label>`
        : "";
    const download = present
      ? '<span class="settings-hint">已就位</span>'
      : item.downloadable === false
        ? '<span class="settings-hint">不可下载</span>'
        : `<button class="btn small" type="button" data-download="${esc(group)}" data-mkey="${esc(key)}">下载</button>`;
    const resolve = `<button class="btn small" type="button" data-resolve="${esc(group)}" data-mkey="${esc(key)}">重新解析</button>`;
    const stateRow = jobs[id]
      ? `<tr class="job-row"><td colspan="6" data-job="${esc(id)}">${jobHtml(id)}</td></tr>`
      : "";
    return `<tr>
        <td class="wrap">${esc(key)}</td>
        <td>${esc(status)}</td>
        <td>${esc(modelBytes(item.bytes))}</td>
        <td class="wrap">${esc(license)}</td>
        <td class="wrap">${esc(address)}${error}${manual}</td>
        <td><div class="settings-actions">${download}${check}${resolve}</div></td>
      </tr>${stateRow}`;
  }

  /* 渲染「模型与下载」卡片（目录可编辑、镜像链只读、三组模型表）。
   * 参数：无（数据取自 data / models / modelsError）。
   * 返回：HTML 字符串。
   */
  function renderModels() {
    const paths = (data && data.paths) || {};
    const image = (data && data.image) || {};
    const inputs = `<div class="settings-grid">
      ${fieldBlock(
        "paths.models_root",
        "音频模型根目录",
        `<input data-key="paths.models_root" value="${esc(paths.models_root || "")}" placeholder="/Volumes/other/models/ndw" spellcheck="false" />`,
        paths.models_root_exists === false ? "目录不存在：下载前请在访达里创建或改到已有目录。" : ""
      )}
      ${fieldBlock(
        "comfyui.models_dir",
        "ComfyUI 模型目录",
        `<input data-key="comfyui.models_dir" value="${esc(image.models_dir || "")}" placeholder="/Volumes/other/ComfyUI-Shared/models" spellcheck="false" />`,
        image.models_dir_exists === false && image.models_dir ? "目录不存在：请确认路径。" : "留空时按 ComfyUI 启动日志自动探测。"
      )}
    </div>`;
    if (!models) {
      return `<section class="settings-card" id="setModelsCard">
        <h3>模型与下载</h3>
        ${inputs}
        <div class="error-box">模型清单读取失败：${esc(modelsError || "未知原因")}</div>
      </section>`;
    }
    const mirrors = (models.mirrors || []).join(" → ") || "—";
    return `<section class="settings-card" id="setModelsCard">
      <h3>模型与下载</h3>
      ${inputs}
      <div class="kv settings-readonly">${readRow("镜像链", mirrors)}</div>
      ${renderModelGroup("音频模型", "audio", models.audio)}
      ${renderModelGroup("图像模型", "image", models.image)}
      ${renderModelGroup("视频模型", "video", models.video)}
      <div class="settings-hint">下载在后台进行，页面按 1.2s 轮询进度；同一时间只允许一个下载任务。已就位的项不会重复下载。</div>
    </section>`;
  }

  /* 渲染整页：四张卡片 + 底部保存栏。数据来自最近一次 GET / POST 的响应。 */
  function render() {
    const writing = data.writing || {};
    const tts = data.tts || {};
    const image = data.image || {};
    el("setBody").innerHTML = `${renderWriting(writing)}${renderTts(tts)}${renderImage(image)}${renderModels()}
      <div class="settings-foot">
        <div class="settings-actions">
          <button id="setSave" class="btn primary">保存</button>
          <button id="setReset" class="btn">重置</button>
          <span class="settings-hint">保存写入 config/config.ui.yaml，config.yaml 不会被改写；新发起的任务即时生效。</span>
        </div>
        <div class="settings-hint">配置文件：${esc(data.ui_path || "—")}</div>
      </div>`;
    markInitial();
    setModelButtonsDisabled(Boolean(pollingKey));
  }

  /* 读取控件当前值并归一化为可比较的字符串。
   * 参数：node 带 data-key 的控件。
   * 返回：归一化后的字符串（多值字段用逗号连接）。
   */
  function normalized(node) {
    const raw = String(node.value || "").trim();
    if (node.dataset.key === "tts.provider_order") {
      return raw
        .split(/[,，]/)
        .map((item) => item.trim())
        .filter(Boolean)
        .join(",");
    }
    return raw;
  }

  /* 记住每个控件渲染时的初始值，保存时只提交真正改过的键，
   * 避免没动过的字段也被写进 config.ui.yaml 变成「页面覆盖」而锁死本机配置。
   * 参数：无。
   * 返回：无。
   */
  function markInitial() {
    document.querySelectorAll("#setBody [data-key]").forEach((node) => {
      node.dataset.init = normalized(node);
    });
  }

  /* 拉取 ComfyUI 连接状态（失败也存成 ok=false，页面显示红色徽标）。
   * 参数：无。
   * 返回：Promise，无。
   */
  async function loadHealth() {
    try {
      health = await NW.api("/api/ui/comfy/health", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      });
    } catch (error) {
      health = { ok: false, message: `检查失败：${error.message}` };
    }
  }

  /* 把最近一次检查结果刷到卡片上（徽标 / 失败说明 / 成功详情），不重渲染整页。
   * 参数：无。
   * 返回：无。
   */
  function renderHealth() {
    const badge = el("setHealthBadge");
    if (badge) badge.innerHTML = healthBadge();
    const warn = el("setHealthWarn");
    if (warn) warn.innerHTML = healthWarnHtml();
    const detail = el("setHealthDetail");
    if (detail) detail.innerHTML = healthDetailHtml();
  }

  /* 重新检查连接并只刷新状态区，供保存后（地址可能刚改）使用。
   * 参数：无。
   * 返回：Promise，无。
   */
  async function refreshHealth() {
    await loadHealth();
    renderHealth();
  }

  /* 手动联通检测：按输入框里当前填的地址探测，未保存的新地址也能测。
   * 检测期间禁用按钮并显示「检测中…」，结束后刷新徽标与说明。
   * 参数：无。
   * 返回：Promise，无。
   */
  async function testHealth() {
    const input = document.querySelector('#setBody [data-key="comfyui.base_url"]');
    const baseUrl = input ? String(input.value || "").trim() : "";
    const button = el("setHealthCheck");
    const badge = el("setHealthBadge");
    if (button) {
      button.disabled = true;
      button.textContent = "检测中…";
    }
    if (badge) badge.innerHTML = '<span class="state-badge s-pending"><span class="dot"></span>检测中…</span>';
    try {
      health = await NW.api("/api/ui/comfy/health", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ base_url: baseUrl }),
      });
    } catch (error) {
      health = { ok: false, message: `检测失败：${error.message}` };
    } finally {
      if (button) {
        button.disabled = false;
        button.textContent = "检测连接";
      }
      renderHealth();
    }
  }

  /* 拉取模型清单（只读接口，失败时记录原因供卡片显示）。
   * 参数：无。
   * 返回：Promise，无。
   */
  async function loadModels() {
    try {
      models = await NW.api("/api/ui/models");
      modelsError = "";
    } catch (error) {
      models = null;
      modelsError = error.message;
    }
  }

  /* 重新拉取模型清单并只替换模型卡片，保留其它未保存的输入。
   * 参数：无。
   * 返回：Promise，无。
   */
  async function refreshModels() {
    await loadModels();
    const card = el("setModelsCard");
    if (card) card.outerHTML = renderModels();
    setModelButtonsDisabled(Boolean(pollingKey));
  }

  /* 拉取配置、连接状态与模型清单并渲染；失败时在容器里留可见的错误态。
   * 参数：无。
   * 返回：Promise，无。
   */
  async function load() {
    el("setBody").innerHTML = '<p class="muted">读取配置中…</p>';
    pending = {};
    health = null;
    models = null;
    modelsError = "";
    try {
      data = await NW.api("/api/config");
      await Promise.all([loadHealth(), loadModels()]);
      render();
    } catch (error) {
      el("setBody").innerHTML = `<div class="error-box">读取配置失败：${esc(error.message)}</div>`;
      NW.toast(`读取配置失败：${error.message}`);
    }
  }

  /* 收集表单当前值，按 POST /api/config 的要求整理。
   * 空字符串 → null（删除覆盖项、回落 config.yaml）；api_key 留空时不提交；
   * 点了「恢复」的键一律置 null。只提交 editable 白名单内的键。
   * 参数：无。
   * 返回：{点号路径: 值} 对象。
   */
  function collectValues() {
    const values = {};
    document.querySelectorAll("#setBody [data-key]").forEach((node) => {
      const key = node.dataset.key;
      if (!isEditable(key)) return;
      // 没改过的字段不提交，避免把 config.yaml 的取值复制进覆盖层
      if (normalized(node) === (node.dataset.init ?? "")) return;
      const raw = String(node.value || "").trim();
      if (key === "omlx.api_key") {
        // 留空 = 不改
        if (raw) values[key] = raw;
        return;
      }
      if (key === "tts.provider_order") {
        values[key] = raw ? raw.split(/[,，]/).map((item) => item.trim()).filter(Boolean) : null;
        return;
      }
      if (key === "omlx.temperature" || key === "omlx.max_tokens") {
        values[key] = raw === "" ? null : Number(raw);
        return;
      }
      values[key] = raw === "" ? null : raw;
    });
    Object.keys(pending).forEach((key) => {
      values[key] = null;
    });
    return values;
  }

  /* 保存：提交改过的可编辑字段（含「恢复」标记），成功后重新渲染。
   * 参数：无。
   * 返回：Promise，无。
   */
  async function save() {
    if (busy || !data) return;
    busy = true;
    try {
      data = await NW.api("/api/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ values: collectValues() }),
      });
      pending = {};
      render();
      NW.toast("已保存", true);
      // 目录 / 地址可能刚改过：顺带刷新连接状态与模型清单
      Promise.all([refreshHealth(), refreshModels()]);
    } catch (error) {
      NW.toast(`保存失败：${error.message}`);
    } finally {
      busy = false;
    }
  }

  /* 标记某个键回落 config.yaml：清空控件、去掉徽标，保存时按 null 提交。
   * 参数：key 点号路径。
   * 返回：无。
   */
  function markRevert(key) {
    if (!key) return;
    pending[key] = true;
    const input = document.querySelector(`#setBody [data-key="${key}"]`);
    if (input && input.tagName !== "SELECT") input.value = "";
    const holder = input ? input.closest(".settings-field") : null;
    if (holder) {
      const badge = holder.querySelector(".override-badge");
      if (badge) badge.remove();
      const btn = holder.querySelector("[data-revert]");
      if (btn) btn.remove();
    }
    NW.toast(`已标记恢复 ${key}：点保存后回落 config.yaml`, true);
  }

  /* 在模型清单里按组查找一行。
   * 参数：group 组名；key 模型键。
   * 返回：模型行或 null。
   */
  function findModel(group, key) {
    const groupData = models && models[group];
    const rows = (groupData && groupData.models) || [];
    return rows.find((item) => item.key === key) || null;
  }

  /* 下载 / 重新解析进行中时禁用其它模型按钮（同时只允许一个下载任务）。
   * 参数：disabled 是否禁用。
   * 返回：无。
   */
  function setModelButtonsDisabled(disabled) {
    document.querySelectorAll("#setBody [data-download], #setBody [data-resolve]").forEach((node) => {
      node.disabled = disabled;
    });
  }

  /* 停止轮询定时器。
   * 参数：无。
   * 返回：无。
   */
  function stopPolling() {
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = null;
  }

  /* 把任务状态行挂到对应模型行的下方（首次渲染 / 刷新清单后调用）。
   * 参数：id 任务键（group:key）。
   * 返回：无。
   */
  function attachJobRow(id) {
    const rec = jobs[id];
    if (!rec) return;
    const group = id.split(":")[0];
    const key = id.slice(group.length + 1);
    const button = document.querySelector(`#setBody [data-mkey="${CSS.escape(key)}"][data-download="${group}"], #setBody [data-mkey="${CSS.escape(key)}"][data-resolve="${group}"]`);
    const row = button ? button.closest("tr") : null;
    if (!row) return;
    let stateRow = row.nextElementSibling;
    if (!stateRow || !stateRow.classList.contains("job-row")) {
      stateRow = document.createElement("tr");
      stateRow.className = "job-row";
      stateRow.innerHTML = `<td colspan="6"></td>`;
      row.after(stateRow);
    }
    const cell = stateRow.firstElementChild;
    cell.dataset.job = id;
    cell.innerHTML = jobHtml(id);
  }

  /* 下载：勾选校验 → 起后台任务 → 每 1.2s 轮询进度，完成后退订并刷新清单。
   * 参数：group 组名；key 模型键。
   * 返回：Promise，无。
   */
  async function startDownload(group, key) {
    if (pollingKey) {
      NW.toast("已有下载任务在运行，请等待完成");
      return;
    }
    const item = findModel(group, key);
    const id = `${group}:${key}`;
    const check = document.querySelector(`#setBody [data-license="${id}"]`);
    const gate = (item && item.license_gate) || "none";
    if (gate !== "none" && !(check && check.checked)) {
      NW.toast("该模型许可需确认，请先勾选已理解许可限制");
      return;
    }
    // 下载前做体积确认：体积未知时也提示，避免误下超大文件
    const size = Number(item && item.bytes) || 0;
    const sizeText = size ? modelBytes(size) : "体积未知";
    if (!window.confirm(`将下载 ${key}（${sizeText}），确认继续？`)) return;
    try {
      const result = await NW.api("/api/ui/models/install", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ group, key, accept_license: Boolean(check && check.checked), yes: true }),
      });
      jobs[id] = { job_id: result.job_id, status: "running", logs: [], result: null };
      pollingKey = id;
      attachJobRow(id);
      setModelButtonsDisabled(true);
      pollTimer = setInterval(() => pollJob(id), 1200);
    } catch (error) {
      NW.toast(`启动下载失败：${error.message}`);
    }
  }

  /* 轮询一次下载任务；结束（done / failed）时停止并刷新清单。
   * 参数：id 任务键（group:key）。
   * 返回：Promise，无。
   */
  async function pollJob(id) {
    const rec = jobs[id];
    if (!rec) return;
    try {
      const job = await NW.api(`/api/ui/models/install/${encodeURIComponent(rec.job_id)}`);
      rec.status = job.status;
      rec.logs = job.logs || [];
      rec.result = job.result;
      attachJobRow(id);
      if (job.status !== "running") {
        stopPolling();
        pollingKey = "";
        setModelButtonsDisabled(false);
        if (job.status === "done") {
          const group = id.split(":")[0];
          NW.toast(
            group === "audio" ? "下载完成" : "下载完成，请在 ComfyUI 界面刷新模型列表后生效",
            true
          );
          await refreshModels();
        } else {
          NW.toast("下载失败，请查看日志");
        }
      }
    } catch (error) {
      stopPolling();
      pollingKey = "";
      setModelButtonsDisabled(false);
      rec.status = "failed";
      rec.logs = (rec.logs || []).concat([`查询进度失败：${error.message}`]);
      attachJobRow(id);
      NW.toast(`查询下载进度失败：${error.message}`);
    }
  }

  /* 重新解析线上地址（图像/视频联网反查；音频直接返回仓库名）。
   * 参数：group 组名；key 模型键。
   * 返回：Promise，无。
   */
  async function resolveModel(group, key) {
    if (pollingKey) {
      NW.toast("有下载任务在运行，请等待完成后再解析");
      return;
    }
    try {
      await NW.api("/api/ui/models/resolve", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ group, key }),
      });
      NW.toast("解析完成", true);
      await refreshModels();
    } catch (error) {
      NW.toast(`解析失败：${error.message}`);
    }
  }

  /* 手工补录某个模型的线上来源（仓库 + 文件名，或直接下载地址）。
   * 参数：group 组名；key 模型键；box 所在的 .manual-box 容器（取输入值）。
   * 返回：Promise，无。
   */
  async function saveManual(group, key, box) {
    const read = (field) => {
      const node = box ? box.querySelector(`[data-manual-field="${field}"]`) : null;
      return node ? String(node.value || "").trim() : "";
    };
    const repo = read("repo");
    const hf_filename = read("hf_filename");
    const url = read("url");
    try {
      await NW.api("/api/ui/models/manual", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ group, key, repo, hf_filename, url }),
      });
      NW.toast("已保存线上来源", true);
      await refreshModels();
    } catch (error) {
      NW.toast(`保存来源失败：${error.message}`);
    }
  }

  /* 容器上的统一委托：保存 / 重置 / 恢复 / 下载 / 重新解析 / 手工指定 / 联通检测。
   * 参数：event 点击事件。
   * 返回：无。
   */
  function onClick(event) {
    const revert = event.target.closest("[data-revert]");
    if (revert) {
      markRevert(revert.dataset.revert || "");
      return;
    }
    const manual = event.target.closest("[data-manual-save]");
    if (manual) {
      saveManual(manual.dataset.manualSave || "", manual.dataset.mkey || "", manual.closest(".manual-box"));
      return;
    }
    const download = event.target.closest("[data-download]");
    if (download) {
      startDownload(download.dataset.download || "", download.dataset.mkey || "");
      return;
    }
    const resolve = event.target.closest("[data-resolve]");
    if (resolve) {
      resolveModel(resolve.dataset.resolve || "", resolve.dataset.mkey || "");
      return;
    }
    if (event.target.closest("#setHealthCheck")) testHealth();
    else if (event.target.closest("#setSave")) save();
    else if (event.target.closest("#setReset")) load();
  }

  /* 容器上的 change 委托：模板下拉切换后即时刷新只读区（不需要先保存）。
   * 参数：event change 事件。
   * 返回：无。
   */
  function onChange(event) {
    const key = event.target && event.target.dataset ? event.target.dataset.key || "" : "";
    if (key.startsWith("comfyui.templates.")) refreshTemplateInfo();
  }

  /* 首次激活时绑定事件，避免重复绑定。 */
  function bind() {
    if (bound) return;
    bound = true;
    el("setBody").addEventListener("click", onClick);
    el("setBody").addEventListener("change", onChange);
  }

  window.Pages.settings = {
    activate() {
      bind();
      load();
    },
  };
})();
