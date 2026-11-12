/* 音效 / 配图 / 视频位页。
 *
 * 条目来自导演标注物化；「运行」调 `media sfx|image|video run`，
 * 离线渲染器会真出文件（ffmpeg 合成音效与视频、Pillow 画图），状态写回 asset。
 *
 * 本页只做三列清单；点某一行把条目交给右栏 Inspector（`NWInspector.show('media', …)`）
 * 看提示词、时长、状态并内联播放，右栏「重跑此项」仍走同一个 `/api/ui/media/run`。
 */

(function () {
  let bound = false;
  let busy = false;
  let rowsById = new Map(); // `${kind}:${id}` → 媒体条目，供右栏取原始字段
  let selected = "";
  let lastMedia = { sfx: [], image: [], video: [] }; // 最近一次清单，生成前据此提示范围

  const el = (id) => document.getElementById(id);
  const RUN_BUTTON = { sfx: "mdRunSfx", image: "mdRunImage", video: "mdRunVideo" };

  //: 配乐档位文案：页面只暴露「叙事 / 铺底」，不出现 audio codes 等实现词
  const BGM_MODE_LABELS = { narrative: "叙事", bed: "铺底", auto: "自动推断" };
  //: 配乐后端文案（表格里用短名）
  const BGM_PROVIDER_LABELS = { local: "本地 SAO", comfyui: "ACE-Step" };
  //: 后端清单与默认值缓存，由 `/api/ui/bgm` 填充
  let bgmSpec = { providers: [], defaults: { provider: "local", normalize_narrative: true, trim_narrative_edges: true }, cues: [] };
  let bgmPlanCache = [];
  let bgmTimer = null;

  /* 去设置页的入口：沿用现有模型页，不新开导航。 */
  function settingsLinkHtml() {
    return "<a class='btn small' href='#/settings'>去设置页下载 / 检查模型</a>";
  }

  /* 把配乐生成面板注入媒体页网格（不改 index.html，避免和并行任务冲突）。
   * 放在四类媒体的最前面：配乐和音效/配图/视频属于同一类「生成」操作，不单独沉底。
   * 参数：无。返回：无。
   */
  function injectBgmPanel() {
    if (el("mdBgmCard")) return;
    const grid = document.querySelector("#view-media .media-grid");
    if (!grid) return;
    const card = document.createElement("div");
    card.className = "card";
    card.id = "mdBgmCard";
    card.innerHTML =
      "<h3>配乐（BGM）<span class='muted'>叙事 / 铺底</span></h3>" +
      "<div style='display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:8px'>" +
      "  <label class='muted'>后端 <select id='bgmProvider'>" +
      "    <option value='local'>本地 Stable Audio Open（5~47 秒）</option>" +
      "    <option value='comfyui'>ComfyUI ACE-Step（10~180 秒）</option>" +
      "  </select></label>" +
      "  <label class='muted'>关联计划 <select id='bgmPlan' style='min-width:180px'><option value=''>自由描述</option></select></label>" +
      "  <label class='muted'>用途 <select id='bgmMode'>" +
      "    <option value='auto'>自动推断</option><option value='narrative'>叙事</option><option value='bed'>铺底</option>" +
      "  </select></label>" +
      "  <label class='muted'>时长 <input id='bgmDuration' type='number' min='5' max='47' step='5' value='40' style='width:72px'> 秒</label>" +
      "  <label class='muted'>响度归一 <select id='bgmNormalize'>" +
      "    <option value='auto'>跟随默认</option><option value='true'>开</option><option value='false'>关</option>" +
      "  </select></label>" +
      "  <label class='muted'>首尾裁剪 <select id='bgmTrim'>" +
      "    <option value='auto'>跟随默认</option><option value='true'>开</option><option value='false'>关</option>" +
      "  </select></label>" +
      "  <button id='bgmGenerate' class='btn small'>生成配乐</button>" +
      "  <span id='bgmHint' class='muted'></span>" +
      "</div>" +
      "<div id='bgmProviderHint' class='muted' style='margin-bottom:4px'></div>" +
      "<div id='bgmParamSource' class='muted' style='margin-bottom:6px'></div>" +
      "<div id='bgmLocalWarn' class='muted' style='margin-bottom:6px' hidden></div>" +
      "<textarea id='bgmPrompt' rows='2' placeholder='中文描述：风格 / 情绪 / 配器，如「电影感悬疑铺底，低沉弦乐与钟表滴答，暗色小调」' " +
      "style='width:100%;box-sizing:border-box;margin-bottom:8px'></textarea>" +
      "<div id='bgmStatus' style='display:none;margin-bottom:8px'>" +
      "  <div style='height:6px;background:#e5e7eb;border-radius:3px;overflow:hidden'>" +
      "    <div id='bgmProgressBar' style='height:100%;width:0;background:#3b82f6;transition:width .4s'></div>" +
      "  </div>" +
      "  <span id='bgmStatusText' class='muted'></span>" +
      "</div>" +
      "<div id='bgmError' class='error-box' style='margin-bottom:8px' hidden></div>" +
      "<div id='mdBgmList' class='media-table'>填写描述后点「生成配乐」</div>" +
      "<p id='mdBgmRecord' class='muted'></p>";
    grid.insertBefore(card, grid.firstChild);
    el("bgmPlan").addEventListener("change", onPlanChange);
    el("bgmGenerate").addEventListener("click", generateBgm);
    el("bgmProvider").addEventListener("change", syncBgmProviderUi);
    el("bgmDuration").addEventListener("input", onDurationChange);
    el("bgmMode").addEventListener("change", updateBgmParamSource);
    el("bgmNormalize").addEventListener("change", updateBgmParamSource);
    el("bgmTrim").addEventListener("change", updateBgmParamSource);
  }

  /* 取某个后端的能力描述（时长上下限、可用性）。参数：id local/comfyui。返回：对象或 null。 */
  function bgmProviderInfo(id) {
    return (bgmSpec.providers || []).find((item) => item.id === id) || null;
  }

  /* 刷新后端相关 UI：时长边界、上限差异说明、本地不可用告警。参数：无。返回：无。 */
  function syncBgmProviderUi() {
    const select = el("bgmProvider");
    if (!select) return;
    const info = bgmProviderInfo(select.value);
    const duration = el("bgmDuration");
    if (info) {
      duration.min = info.min_seconds;
      duration.max = info.max_seconds;
    }
    let hint = info ? `${info.label}：可用 ${info.min_seconds}~${info.max_seconds} 秒` : "未读取到后端信息";
    if (select.value === "local") {
      if (info) hint += `；超过 ${info.max_seconds} 秒会自动改用 ACE-Step`;
    } else {
      hint += "；本地模型最长约 47 秒，长曲请保持 ACE-Step";
    }
    el("bgmProviderHint").textContent = hint;
    const warn = el("bgmLocalWarn");
    if (select.value === "local" && info && !info.available) {
      warn.hidden = false;
      warn.innerHTML = `<span class="err">本地 provider 不可用：${esc(info.detail || "权重或环境缺失")}</span> ${settingsLinkHtml()}`;
    } else {
      warn.hidden = true;
      warn.innerHTML = "";
    }
  }

  /* 当前关联计划已有的 cue（用于显示开关来源）。返回：cue 对象或 null。 */
  function currentPlanCue() {
    const planId = el("bgmPlan") ? el("bgmPlan").value : "";
    if (!planId) return null;
    return (
      (bgmSpec.cues || []).find(
        (cue) => (cue.plan_ref && cue.plan_ref.plan_id === planId) || cue.plan_id === planId
      ) || null
    );
  }

  /* 显示归一 / 首尾裁剪的当前生效值与来源（默认值 / 条目设置 / 页面选择）。参数：无。返回：无。 */
  function updateBgmParamSource() {
    if (!el("bgmParamSource")) return;
    const cue = currentPlanCue();
    const defaults = bgmSpec.defaults || {};
    const baseNormalize = cue && typeof cue.normalize === "boolean" ? cue.normalize : defaults.normalize_narrative !== false;
    const baseTrim = cue && typeof cue.trim_edges === "boolean" ? cue.trim_edges : defaults.trim_narrative_edges !== false;
    const normalizeChoice = el("bgmNormalize").value;
    const trimChoice = el("bgmTrim").value;
    const normalize = normalizeChoice === "auto" ? baseNormalize : normalizeChoice === "true";
    const trim = trimChoice === "auto" ? baseTrim : trimChoice === "true";
    let source = "默认值";
    if (cue) source = "条目设置";
    if (normalizeChoice !== "auto" || trimChoice !== "auto") source = "页面选择";
    const bedNote = el("bgmMode").value === "bed" ? "（铺底档生成时始终归一）" : "";
    el("bgmParamSource").textContent =
      `当前生效：响度归一 ${normalize ? "开" : "关"}${bedNote}、首尾裁剪 ${trim ? "开" : "关"} · 来源：${source}`;
  }

  /* 时长超过本地上限时自动改选 ACE-Step，并说明原因。参数：无。返回：无。 */
  function onDurationChange() {
    const select = el("bgmProvider");
    if (!select) return;
    const local = bgmProviderInfo("local");
    const duration = Number(el("bgmDuration").value) || 0;
    if (select.value === "local" && local && duration > local.max_seconds) {
      select.value = "comfyui";
      syncBgmProviderUi();
      el("bgmProviderHint").textContent =
        `已自动切换 ACE-Step：本地模型最长 ${local.max_seconds} 秒，当前 ${duration} 秒`;
    }
  }

  /* 按后端与时长粗估生成耗时（秒），只用于进度条的“预计”参考。 */
  function estimateBgmSeconds(provider, duration) {
    if (provider === "local") return Math.round(6 + duration * 2.35);
    return Math.round(20 + duration * 3);
  }

  /* 更新内联进度条。参数：text 文案；percent 进度 0~100。返回：无。 */
  function bgmStatusShow(text, percent) {
    const box = el("bgmStatus");
    if (!box) return;
    box.style.display = "block";
    el("bgmStatusText").textContent = text;
    el("bgmProgressBar").style.width = `${Math.max(0, Math.min(100, percent))}%`;
  }

  /* 隐藏内联进度条。参数：无。返回：无。 */
  function bgmStatusHide() {
    const box = el("bgmStatus");
    if (!box) return;
    box.style.display = "none";
    el("bgmProgressBar").style.width = "0";
    el("bgmStatusText").textContent = "";
  }

  /* 显示内联错误；本地 provider 不可用时附「去设置页」入口。 */
  function showBgmError(message, withSettings) {
    const box = el("bgmError");
    if (!box) return;
    box.hidden = false;
    box.innerHTML = `<span class="err">${esc(message)}</span>` + (withSettings ? ` ${settingsLinkHtml()}` : "");
  }

  /* 判断错误是否属于「本地 provider 不可用」，需要引导去设置页。 */
  function isLocalUnavailable(error) {
    const code = String((error && error.code) || "");
    return ["UNAVAILABLE", "MODEL_MISSING", "PROVIDER_DISABLED", "PROVIDER_UNREACHABLE"].includes(code);
  }

  /* 切换关联计划：带出推断档位、时长与提示，不覆盖用户手选的用途。
   * 参数：event change 事件。返回：无。
   */
  function onPlanChange(event) {
    const plan = (bgmPlanCache || []).find((item) => item.id === event.target.value);
    const cue = currentPlanCue();
    // 条目已保存过开关时，控件直接带出条目设置，避免“显示默认值、实际用条目值”的歧义
    el("bgmNormalize").value = cue && typeof cue.normalize === "boolean" ? String(cue.normalize) : "auto";
    el("bgmTrim").value = cue && typeof cue.trim_edges === "boolean" ? String(cue.trim_edges) : "auto";
    if (!plan) {
      el("bgmHint").textContent = "";
      updateBgmParamSource();
      return;
    }
    el("bgmDuration").value = Math.round(plan.duration_s || 40);
    el("bgmHint").textContent = `推断为「${plan.mode_label}」：${plan.reason}`;
    // 计划时长超过本地上限时，切到 ACE-Step 并说明原因
    onDurationChange();
    updateBgmParamSource();
  }

  /* 渲染章节配乐清单：可试听、标出用途 / 后端与生成耗时。
   * 参数：data 接口返回的 {cues, records, plans, providers, defaults}。返回：无。
   */
  function renderBgm(data) {
    bgmPlanCache = data.plans || [];
    bgmSpec = {
      providers: data.providers || [],
      defaults: data.defaults || {},
      cues: data.cues || [],
    };
    const select = el("bgmPlan");
    if (select) {
      const current = select.value;
      select.innerHTML =
        "<option value=''>自由描述</option>" +
        bgmPlanCache
          .map((plan) => `<option value="${esc(plan.id)}">${esc(plan.name)}（推断：${esc(plan.mode_label)}）</option>`)
          .join("");
      select.value = bgmPlanCache.some((plan) => plan.id === current) ? current : "";
    }
    const providerSelect = el("bgmProvider");
    if (providerSelect) {
      const current = providerSelect.value || bgmSpec.defaults.provider || "local";
      providerSelect.innerHTML = (bgmSpec.providers || [])
        .map(
          (item) =>
            `<option value="${esc(item.id)}">${esc(item.label)}（${item.min_seconds}~${item.max_seconds} 秒）` +
            `${item.id === "local" && !item.available ? " · 不可用" : ""}</option>`
        )
        .join("");
      providerSelect.value = (bgmSpec.providers || []).some((item) => item.id === current) ? current : "local";
      syncBgmProviderUi();
    }
    // 从生成记录反查每条成品用的后端，cue 本身不存 provider
    const recordByAsset = new Map((data.records || []).map((item) => [item.asset_id, item]));
    const cues = (data.cues || []).filter((cue) => cue.asset_id || cue.mode);
    const list = el("mdBgmList");
    if (!cues.length) {
      list.innerHTML = "<p class='muted'>本章还没有配乐：填写上面的描述后点「生成配乐」。</p>";
    } else {
      list.innerHTML =
        "<table class='grid'><thead><tr><th>名称</th><th>用途</th><th>后端</th><th>试听</th><th>起点</th></tr></thead><tbody>" +
        cues
          .map((cue) => {
            const url = uiFileUrl(cue.asset_id || "");
            const audio = url ? `<audio class='md-audio' preload='none' controls src='${esc(url)}'></audio>` : "<span class='muted'>无产物</span>";
            const record = recordByAsset.get(cue.asset_id);
            const providerText = record ? BGM_PROVIDER_LABELS[record.provider] || record.provider || "—" : "—";
            return (
              `<tr><td>${esc(cue.name || cue.id)}</td>` +
              `<td>${esc(BGM_MODE_LABELS[cue.mode] || "—")}</td>` +
              `<td>${esc(providerText)}</td>` +
              `<td>${audio}</td><td>${msText(cue.start_ms)}</td></tr>`
            );
          })
          .join("") +
        "</tbody></table>";
    }
    const last = (data.records || [])[(data.records || []).length - 1];
    el("mdBgmRecord").textContent = last
      ? `最近一次：${last.mode_label || BGM_MODE_LABELS[last.mode] || "—"} · ` +
        `${BGM_PROVIDER_LABELS[last.provider] || last.provider || "—"} · 生成 ${last.generation_s}s · 后处理 ${last.postprocess_s}s · ` +
        `${typeof last.lufs === "number" ? last.lufs.toFixed(2) + " LUFS" : "响度未测得"}`
      : "";
    updateBgmParamSource();
  }

  /* 拉取并渲染配乐区。返回：Promise，无。 */
  async function loadBgm() {
    if (!el("mdBgmCard")) return;
    if (!NW.state.path || !NW.state.chapter) {
      el("mdBgmList").innerHTML = "<p class='muted'>先打开工程并选择章节。</p>";
      return;
    }
    try {
      renderBgm(await NW.api(`/api/ui/bgm?${NW.qs({ path: NW.state.path, chapter: NW.state.chapter })}`));
    } catch (error) {
      el("mdBgmList").innerHTML = `<p class='error-box'>读取配乐失败：${esc(error.message)}</p>`;
    }
  }

  /* 生成一段配乐：档位按「关联计划 > 页面下拉 > 场景推断」在后端解析。
   * 不弹全屏蒙版，只在面板内显示进度与预计时间，生成期间可继续操作其它页面。
   * 返回：Promise，无。
   */
  async function generateBgm() {
    if (busy || !NW.state.path || !NW.state.chapter) return;
    const prompt = el("bgmPrompt").value.trim();
    if (!prompt) {
      NW.toast("请先填写配乐描述");
      return;
    }
    const provider = el("bgmProvider").value;
    const info = bgmProviderInfo(provider);
    if (provider === "local" && info && !info.available) {
      showBgmError(`本地 provider 不可用：${info.detail || "权重或环境缺失"}`, true);
      return;
    }
    const duration = Number(el("bgmDuration").value) || 40;
    if (info && (duration < info.min_seconds || duration > info.max_seconds)) {
      showBgmError(`${info.label} 的时长范围是 ${info.min_seconds}~${info.max_seconds} 秒，当前 ${duration} 秒`, false);
      return;
    }
    const normalizeChoice = el("bgmNormalize").value;
    const trimChoice = el("bgmTrim").value;
    busy = true;
    const button = el("bgmGenerate");
    button.disabled = true;
    el("bgmError").hidden = true;
    const estimate = estimateBgmSeconds(provider, duration);
    const startedAt = Date.now();
    bgmStatusShow("正在生成配乐…", 2);
    clearInterval(bgmTimer);
    bgmTimer = setInterval(() => {
      const elapsed = (Date.now() - startedAt) / 1000;
      const percent = Math.min(95, Math.max(2, (elapsed / estimate) * 100));
      bgmStatusShow(`正在生成配乐… 已用 ${elapsed.toFixed(0)}s / 预计约 ${estimate}s`, percent);
    }, 500);
    try {
      const body = {
        path: NW.state.path,
        chapter: NW.state.chapter,
        prompt,
        provider,
        duration,
        mode: el("bgmMode").value,
        plan_id: el("bgmPlan").value || null,
      };
      // auto 表示跟随默认/条目，不传布尔，让后端按现有优先级解析
      if (normalizeChoice !== "auto") body.normalize = normalizeChoice === "true";
      if (trimChoice !== "auto") body.trim_edges = trimChoice === "true";
      const result = await NW.api("/api/ui/bgm/generate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      clearInterval(bgmTimer);
      bgmTimer = null;
      bgmStatusShow(
        `完成：${result.mode_label} · 生成 ${result.generation_s}s + 后处理 ${result.postprocess_s}s`,
        100
      );
      const lufs = typeof result.lufs === "number" ? ` · ${result.lufs.toFixed(2)} LUFS` : "";
      const providerLabel = BGM_PROVIDER_LABELS[result.provider] || result.provider || "";
      const switched = result.auto_switched ? "（已按 47 秒上限自动改用 ACE-Step）" : "";
      NW.toast(`配乐完成：${result.mode_label} · ${providerLabel}${switched}${lufs}`, true);
      await loadBgm();
    } catch (error) {
      clearInterval(bgmTimer);
      bgmTimer = null;
      bgmStatusHide();
      showBgmError(error.message || "配乐生成失败", provider === "local" && isLocalUnavailable(error));
    } finally {
      busy = false;
      button.disabled = false;
    }
  }

  /* 单个条目的就地预览：配图给缩略图（点击放大）、视频给内联播放器、音效给播放条。
   * src 统一走只读文件接口（uiFileUrl），相对路径取接口返回的 asset_id。
   * 参数：item 媒体条目；kind 媒体类型（sfx / image / video）。
   * 返回：HTML 字符串。
   */
  function previewHtml(item, kind) {
    const rel = item.asset_id || "";
    if (!rel) return "<span class='muted'>无产物</span>";
    const url = uiFileUrl(rel);
    if (!url) return `<span class="error-box">缺少工程根目录，无法构造预览地址：${esc(rel)}</span>`;
    if (kind === "image") {
      return `<img class="md-thumb" loading="lazy" alt="${esc(item.prompt || "")}" data-preview data-rel="${esc(rel)}" src="${esc(url)}" />`;
    }
    if (kind === "video") {
      return `<video class="md-video" preload="metadata" controls data-preview data-rel="${esc(rel)}" src="${esc(url)}"></video>`;
    }
    return `<audio class="md-audio" preload="none" controls data-preview data-rel="${esc(rel)}" src="${esc(url)}"></audio>`;
  }

  /* 渲染一张媒体表；行上带 kind / id，便于点选。
   * 参数：rows 条目数组；kind 媒体类型（sfx / image / video）。
   * 返回：HTML 字符串。
   */
  function table(rows, kind) {
    if (!rows.length) {
      return `<p class='muted'>暂无${MEDIA_KIND_LABELS[kind] || ""}条目：先到「标注」页生成并应用标注。</p>`;
    }
    const head =
      kind === "sfx"
        ? "<tr><th>#</th><th>类型</th><th>预览</th><th>提示词</th><th>状态</th><th>时长</th><th>产物 / 错误</th></tr>"
        : "<tr><th>#</th><th>模式</th><th>模板</th><th>画幅</th><th>宽高</th><th>参考图</th><th>预览</th><th>提示词</th><th>状态</th><th>时长</th><th>产物 / 错误</th></tr>";
    const body = rows
      .map((item) => {
        const extra = item.error
          ? `<span class="err">${esc(typeof item.error === "string" ? item.error : item.error.message || JSON.stringify(item.error))}</span>`
          : esc(item.asset_id || "—");
        const kindText =
          kind === "sfx"
            ? SFX_KIND_LABELS[item.kind] || item.kind || ""
            : ANIM_MODE_LABELS[item.mode] || item.mode || "—";
        // 图像/视频多三列只读信息：模板、画幅、宽高（后端解析失败时给空值）
        // 尺寸与工程画幅不一致时给出提示，只提示可重新生成，不自动重跑
        const sizeText = item.width && item.height ? `${esc(item.width)}×${esc(item.height)}` : "—";
        const sizeMismatch =
          item.size_mismatch && item.actual_width && item.actual_height
            ? `<br><span class="err">实际 ${esc(item.actual_width)}×${esc(item.actual_height)}，与工程画幅不一致，可重新生成</span>`
            : "";
        const specCells =
          kind === "sfx"
            ? ""
            : `<td>${esc(item.template || "—")}</td>` +
              `<td>${esc(item.aspect_ratio || "—")}</td>` +
              `<td class="wrap">${sizeText}${sizeMismatch}</td>`;
        // 参考图列：关键图显示「N 张（名称 / 名称）」，缺图告警逐条用小字提示
        const refs = Array.isArray(item.references) ? item.references : [];
        const refWarnings = Array.isArray(item.reference_warnings) ? item.reference_warnings : [];
        const refNames = refs.map((ref) => ref.name || ref.id || "").filter(Boolean);
        const refText = refs.length ? `${refs.length} 张${refNames.length ? `（${refNames.join(" / ")}）` : ""}` : "—";
        const refCell =
          kind === "sfx"
            ? ""
            : `<td class="wrap">${esc(refText)}${refWarnings.map((text) => `<br><span class="err">${esc(text)}</span>`).join("")}</td>`;
        const key = `${kind}:${item.id}`;
        rowsById.set(key, { kind, row: item });
        const active = selected === key ? "is-selected" : "";
        return (
          `<tr class="${active}" data-media="true" data-media-key="${esc(key)}">` +
          `<td>${esc(item.index ?? "")}</td><td>${esc(kindText || "")}</td>${specCells}${refCell}` +
          `<td class="md-preview">${previewHtml(item, kind)}</td>` +
          `<td class="wrap">${esc(item.prompt || "")}</td>` +
          `<td><span class="state-badge s-${esc(item.status)}"><span class="dot"></span>${esc(NW.statusLabel(item.status))}</span></td>` +
          `<td>${msText(item.duration_ms)}</td><td class="wrap">${extra}</td></tr>`
        );
      })
      .join("");
    return `<table class="grid"><thead>${head}</thead><tbody>${body}</tbody></table>`;
  }

  /* 预览元素加载失败时，把控件换成「原始路径 + 失败原因」，不留空白。
   * 参数：box 容器（某张媒体表）。返回：无。
   */
  function bindPreviewErrors(box) {
    box.querySelectorAll("[data-preview]").forEach((node) => {
      node.addEventListener("error", () => {
        const rel = node.dataset.rel || "";
        const fallback = document.createElement("div");
        fallback.className = "error-box";
        fallback.textContent = `加载失败：${rel}（文件不存在或浏览器不支持该容器）`;
        if (node.parentElement) node.parentElement.replaceChild(fallback, node);
      });
    });
    // 配图缩略图点击放大
    box.querySelectorAll("img[data-preview]").forEach((node) => {
      node.addEventListener("click", (event) => {
        event.stopPropagation();
        openLightbox(node.getAttribute("src"), node.dataset.rel || "");
      });
    });
  }

  /* 配图放大弹层（懒创建，点遮罩或按 Esc 关闭）。
   * 参数：url 图片地址；rel 相对路径（作说明文字）。
   * 返回：无。
   */
  function openLightbox(url, rel) {
    let layer = el("mediaLightbox");
    if (!layer) {
      layer = document.createElement("div");
      layer.id = "mediaLightbox";
      layer.className = "lightbox";
      layer.innerHTML = "<figure><img alt='' /><figcaption class='muted'></figcaption></figure>";
      layer.addEventListener("click", () => { layer.hidden = true; });
      document.body.appendChild(layer);
    }
    const img = layer.querySelector("img");
    const caption = layer.querySelector("figcaption");
    if (img) img.src = url || "";
    if (caption) caption.textContent = rel;
    layer.hidden = false;
  }

  /* 刷新三张表下方的生成按钮文案：把「待生成 N 项 / 全部就绪 / 无条目」直接写在按钮上，
   * 用户不用先数表格就知道这次会跑多少。
   * 参数：data `/api/ui/media` 返回的三类清单。返回：无。
   */
  function syncRunButtons(data) {
    ["sfx", "image", "video"].forEach((kind) => {
      const rows = data[kind] || [];
      const pending = rows.filter((item) => item.status !== "ready").length;
      const button = el(RUN_BUTTON[kind]);
      if (!button) return;
      const base = `生成${MEDIA_KIND_LABELS[kind] || ""}`;
      if (!rows.length) {
        button.textContent = `${base}（无条目）`;
        button.disabled = true;
      } else if (!pending) {
        button.textContent = `${base}（${rows.length} 项已就绪）`;
        button.disabled = false;
      } else {
        button.textContent = `${base}（待生成 ${pending}）`;
        button.disabled = false;
      }
    });
  }

  /* 拉取并渲染三类媒体。 */
  async function load() {
    rowsById = new Map();
    if (!NW.state.path || !NW.state.chapter) {
      ["mdSfx", "mdImage", "mdVideo"].forEach((id) => {
        el(id).innerHTML = "<p class='muted'>先打开工程并选择章节；已打开工程时请在左上角切换章节。</p>";
      });
      lastMedia = { sfx: [], image: [], video: [] };
      ["sfx", "image", "video"].forEach((kind) => {
        const button = el(RUN_BUTTON[kind]);
        if (button) {
          button.textContent = `生成${MEDIA_KIND_LABELS[kind] || ""}`;
          button.disabled = false;
        }
      });
      await loadBgm();
      return;
    }
    try {
      const data = await NW.api(`/api/ui/media?${NW.qs({ path: NW.state.path, chapter: NW.state.chapter })}`);
      lastMedia = { sfx: data.sfx || [], image: data.image || [], video: data.video || [] };
      el("mdSfx").innerHTML = table(lastMedia.sfx, "sfx");
      el("mdImage").innerHTML = table(lastMedia.image, "image");
      el("mdVideo").innerHTML = table(lastMedia.video, "video");
      syncRunButtons(lastMedia);
      ["mdSfx", "mdImage", "mdVideo"].forEach((id) => bindPreviewErrors(el(id)));
    } catch (error) {
      NW.toast(`读取媒体失败：${error.message}`);
    }
    await loadBgm();
  }

  /* 执行一类媒体生成。参数：kind 媒体类型。返回：Promise，无。 */
  async function run(kind) {
    if (busy || !NW.state.path || !NW.state.chapter) return;
    const rows = lastMedia[kind] || [];
    const label = MEDIA_KIND_LABELS[kind] || kind;
    if (!rows.length) {
      NW.toast(`当前章节没有${label}条目：先到「标注」页生成并应用标注`);
      return;
    }
    const force = el("mdForce").checked;
    const ready = rows.filter((item) => item.status === "ready").length;
    const willRun = force ? rows.length : rows.length - ready;
    if (!willRun) {
      NW.toast(`本章 ${rows.length} 个${label}条目都已就绪；如需重做，请先勾选「重新生成已就绪项」`);
      return;
    }
    const scope = force
      ? `将重新生成本章全部 ${rows.length} 个${label}条目，其中 ${ready} 个已就绪会被覆盖。`
      : `将生成本章 ${willRun} 个${label}条目，跳过 ${ready} 个已就绪项。`;
    if (!window.confirm(`${scope}\n生成可能需要较长时间，确定继续？`)) return;
    busy = true;
    const button = RUN_BUTTON[kind];
    el(button).disabled = true;
    try {
      const result = await NW.api("/api/ui/media/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: NW.state.path, chapter: NW.state.chapter, kind, force }),
        busy: `正在生成${label}…`,
        busyDetail: "媒体生成较慢，请勿关闭页面",
      });
      const text = `${label}：就绪 ${result.ready} / 跳过 ${result.skipped} / 失败 ${result.failed}`;
      NW.toast(text, result.failed === 0);
      await load();
      if (window.NWInspector) NWInspector.refreshMedia();
    } catch (error) {
      NW.toast(`执行失败（退出码 ${error.code}）：${error.message}`);
    } finally {
      busy = false;
      // 按最新清单恢复按钮：无条目时保持禁用，避免再点一次空跑
      syncRunButtons(lastMedia);
    }
  }

  /* 点选一行：记住选中并交给右栏。参数：event 点击事件。返回：无。 */
  function onRowClick(event) {
    const row = event.target.closest("tr[data-media-key]");
    if (!row) return;
    const key = row.dataset.mediaKey;
    const found = rowsById.get(key);
    if (!found) return;
    selected = key;
    el("mdSfx").querySelectorAll("tr.is-selected").forEach((node) => node.classList.remove("is-selected"));
    el("mdImage").querySelectorAll("tr.is-selected").forEach((node) => node.classList.remove("is-selected"));
    el("mdVideo").querySelectorAll("tr.is-selected").forEach((node) => node.classList.remove("is-selected"));
    row.classList.add("is-selected");
    if (window.NWInspector) NWInspector.show("media", found);
  }

  /* 首次激活绑定事件。 */
  function bind() {
    if (bound) return;
    bound = true;
    injectBgmPanel();
    el("mdRunSfx").addEventListener("click", () => run("sfx"));
    el("mdRunImage").addEventListener("click", () => run("image"));
    el("mdRunVideo").addEventListener("click", () => run("video"));
    ["mdSfx", "mdImage", "mdVideo"].forEach((id) => el(id).addEventListener("click", onRowClick));
    // Esc 关闭配图放大层
    document.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      const layer = el("mediaLightbox");
      if (layer && !layer.hidden) layer.hidden = true;
    });
    NW.onChapter(() => load());
  }

  window.Pages.media = {
    activate() {
      bind();
      load();
    },
    /* 供右栏「重跑此项」触发同一套运行逻辑。参数：kind 媒体类型。返回：Promise，无。 */
    run,
    /* 供右栏在重跑后刷新清单。返回：Promise，无。 */
    load,
    /* 按 kind + id 取当前清单里的条目（重跑后右栏据此重画）。
     * 参数：kind 媒体类型；id 条目 ID。返回：{ kind, row } 或 null。 */
    find(kind, id) {
      return rowsById.get(`${kind}:${id}`) || null;
    },
  };
})();
