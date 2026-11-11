/* 导出页：启动全链路编排（后台进程）并跟踪阶段、查看成品文件。
 *
 * 编排状态只来自 `.ndw/runs/*.json`（`run show --json`），
 * 前端不推断「跑到哪一步」，因此刷新页面后进度不会错位。
 *
 * 展示形态：阶段用一条横向步骤条（全 15 个阶段都列出，状态来自编排记录，
 * 记录里没有的阶段算 pending），产物用卡片列出文件名 / 体积 / 类型。
 */

(function () {
  const STAGES = [
    ["setting", "设定库"],
    ["outline", "大纲"],
    ["chapter", "章节正文"],
    ["scenes", "场景切分"],
    ["lines", "台词切分"],
    ["script", "导演标注"],
    ["tts", "配音合成"],
    ["sfx", "音效"],
    ["image", "配图"],
    ["video", "视频位"],
    ["timeline", "时间轴"],
    ["concat", "配音拼接"],
    ["mix", "混音"],
    ["mux", "混流"],
    ["export", "导出"],
  ];
  const LABEL = Object.fromEntries(STAGES);
  const MARK = { succeeded: "✓", failed: "✗", skipped: "–", pending: "·", running: "…" };

  let bound = false;
  let busy = false;
  let timer = null;
  let polling = false;

  const el = (id) => document.getElementById(id);

  /* 切换「开始编排」按钮状态。
   * 参数：mode 取值 running（禁用 + 文案「编排中…」）/ checking（只禁用，等记录读回来）/ idle（按是否有章节恢复）。
   * 返回：无。
   */
  function setStartButton(mode) {
    const button = el("exStart");
    if (!button) return;
    if (mode === "running") {
      button.disabled = true;
      button.textContent = "编排中…";
      return;
    }
    button.textContent = "开始编排";
    button.disabled = mode === "checking" || !NW.state.chapter;
  }

  /* 由编排记录推算阶段进度：计划区间内的总步数、已记录步数、正在执行的下一阶段。
   * 参数：run 编排记录，可为 null。
   * 返回：{ total, done, current, failed }；current / failed 为阶段名，取不到时为空串。
   */
  function runProgressOf(run) {
    const recorded = new Set(((run && run.stages) || []).map((item) => item.stage));
    const fromIndex = Math.max(0, STAGES.findIndex(([value]) => value === ((run && run.from_stage) || "setting")));
    const toIndex = STAGES.findIndex(([value]) => value === ((run && run.to_stage) || "export"));
    const planned = STAGES.slice(fromIndex, (toIndex < 0 ? STAGES.length - 1 : toIndex) + 1).map(([value]) => value);
    const failed = ((run && run.stages) || []).find((item) => item.status === "failed");
    return {
      total: planned.length,
      done: planned.filter((stage) => recorded.has(stage)).length,
      current: planned.find((stage) => !recorded.has(stage)) || "",
      failed: failed ? failed.stage : "",
    };
  }

  /* 填两个阶段下拉框。 */
  function fillStages() {
    const options = STAGES.map(([value, label]) => `<option value="${value}">${esc(label)}</option>`).join("");
    el("exFrom").innerHTML = options;
    el("exTo").innerHTML = options;
    el("exFrom").value = "setting";
    el("exTo").value = "export";
  }

  /* 按编排记录画横向步骤条：15 个阶段全列出，缺记录的阶段按 pending，正在跑的那一步标 running。
   * 参数：run 编排记录（`run show --json` 的负载），可为 null；current 当前阶段名，可空。
   * 返回：HTML 字符串。
   */
  function stepbarHtml(run, current) {
    const recorded = new Map();
    ((run && run.stages) || []).forEach((item) => recorded.set(item.stage, item));
    return STAGES.map(([stage, label]) => {
      const item = recorded.get(stage);
      const status = item ? item.status : stage === current ? "running" : "pending";
      const note = item ? item.error || item.note || "" : "";
      return (
        `<li class="step" data-status="${esc(status)}" title="${esc(note || `${label}：${statusLabel(status)}`)}">` +
        `${esc(item && item.label ? item.label : label)}</li>`
      );
    }).join("");
  }

  /* 渲染阶段进度与状态行：运行中给「当前阶段 + 已完成 X / 共 N 步」，失败给失败阶段与原因。
   * 参数：run 编排记录，可为 null。
   * 返回：无。
   */
  function renderRun(run) {
    if (!run) {
      el("exStatus").textContent = "还没有编排记录。";
      el("exStages").innerHTML = stepbarHtml(null, "");
      setStartButton("idle");
      return;
    }
    const stages = run.stages || [];
    const progress = runProgressOf(run);
    const running = run.status === "running";
    const last = stages.length ? stages[stages.length - 1] : null;
    const parts = [`run ${run.run_id}`, `状态 ${statusLabel(run.status)}`, `章节 ${run.chapter_id}`, `开始 ${run.started_at || ""}`];
    if (running) {
      const currentLabel = progress.current ? LABEL[progress.current] : "收尾";
      parts.push(`当前阶段「${currentLabel}」`);
      parts.push(`已完成 ${progress.done} / 共 ${progress.total} 步`);
      // 阶段自带的统计（如「沿用已有台词 12 行」）直接当作逐条进度展示
      if (last && last.note) parts.push(`上一步${last.label || LABEL[last.stage] || last.stage}：${last.note}`);
    } else {
      parts.push(`共 ${progress.done} / ${progress.total} 步`);
      if (progress.failed) {
        const item = stages.find((row) => row.stage === progress.failed) || {};
        parts.push(`失败阶段「${item.label || LABEL[progress.failed] || progress.failed}」：${item.error || "未给出原因"}`);
      }
      if (run.finished_at) parts.push(`结束 ${run.finished_at}`);
    }
    el("exStatus").textContent = parts.join("　");
    el("exStages").innerHTML = stepbarHtml(run, running ? progress.current : "");
    setStartButton(running ? "running" : "idle");
  }

  /* 拉最近一次编排记录。 */
  async function loadRun() {
    if (!NW.state.path) {
      renderRun(null);
      return null;
    }
    try {
      const data = await NW.api(`/api/ui/produce/latest?${NW.qs({ path: NW.state.path })}`);
      renderRun(data.run);
      return data.run;
    } catch (error) {
      el("exStatus").textContent = `读取编排记录失败：${error.message}`;
      setStartButton("idle");
      return null;
    }
  }

  /* 产物卡片：文件名 + 类型 + 体积 + 修改时间 + 相对路径。
   * 参数：item 产物条目 { rel, size, mtime }。
   * 返回：HTML 字符串。
   */
  function artifactHtml(item) {
    const rel = item.rel || "";
    const name = rel.split("/").pop() || rel;
    const ext = name.includes(".") ? name.split(".").pop().toLowerCase() : "file";
    const src = uiFileUrl(rel);
    let preview = "";
    if (src && ext === "mp4") {
      preview = `<video class="artifact-video" preload="metadata" controls src="${esc(src)}"></video>`;
    } else if (src && (ext === "wav" || ext === "mp3")) {
      preview = `<audio class="artifact-audio" preload="metadata" controls src="${esc(src)}"></audio>`;
    } else if (src && (ext === "png" || ext === "jpg" || ext === "jpeg")) {
      preview = `<img class="artifact-image" loading="lazy" alt="${esc(name)}" src="${esc(src)}">`;
    }
    return (
      `<div class="artifact"><div class="artifact-name">${esc(name)}</div>` +
      `<div class="artifact-meta"><span class="tag">${esc(ext)}</span>` +
      `<span>${esc(humanSize(item.size))}</span><span>${esc(item.mtime || "")}</span></div>` +
      preview +
      `<div class="artifact-path">${esc(rel)}</div></div>`
    );
  }

  /* 拉产物清单。 */
  async function loadFiles() {
    if (!NW.state.path) {
      el("exFiles").innerHTML = "<p class='muted'>先打开工程。</p>";
      return;
    }
    try {
      const data = await NW.api(`/api/ui/exports?${NW.qs({ path: NW.state.path })}`);
      const files = data.files || [];
      el("exFiles").innerHTML = files.length
        ? files.map(artifactHtml).join("")
        : "<p class='muted'>还没有产物：先跑一次编排。</p>";
    } catch (error) {
      NW.toast(`读取产物失败：${error.message}`);
    }
  }

  /* 轮询编排状态；结束后停止。 */
  function schedule(delay) {
    clearTimeout(timer);
    timer = setTimeout(poll, delay);
  }

  async function poll() {
    if (!polling) return;
    const run = await loadRun();
    if (!run || run.status !== "running") {
      polling = false;
      await loadFiles();
      if (!run) return;
      if (run.status === "failed") {
        const item = (run.stages || []).find((row) => row.status === "failed") || {};
        const stageLabel = item.label || LABEL[item.stage] || "未知阶段";
        NW.toast(`编排失败于「${stageLabel}」：${item.error || "未给出原因"}`);
      } else {
        NW.toast(`编排结束：${statusLabel(run.status)}`, run.status === "succeeded" || run.status === "dry_run");
      }
      return;
    }
    schedule(2500);
  }

  /* 启动编排：按钮立刻进入运行态（禁用 + 「编排中…」），接口超过 200ms 才弹 busy 蒙版。 */
  async function start() {
    if (busy || !NW.state.path || !NW.state.chapter) return;
    busy = true;
    setStartButton("running");
    try {
      const result = await NW.api("/api/ui/produce", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        busy: "正在启动编排…",
        busyDetail: `${LABEL[el("exFrom").value] || ""} → ${LABEL[el("exTo").value] || ""}`,
        body: JSON.stringify({
          path: NW.state.path,
          chapter: NW.state.chapter,
          from: el("exFrom").value,
          to: el("exTo").value,
          force: el("exForce").checked,
          no_resume: !el("exResume").checked,
          keep_going: el("exKeepGoing").checked,
        }),
      });
      NW.toast(`已启动：PID ${result.pid}，日志 ${result.log}`, true);
      polling = true;
      schedule(1200);
    } catch (error) {
      NW.toast(`启动失败（退出码 ${error.code}）：${error.message}`);
      setStartButton("idle");
    } finally {
      busy = false;
    }
  }

  /* 生成剪映草稿 zip：成功后展示 zip 路径、轨道统计、素材对账与字幕兜底记录。
   * 返回：无。
   */
  async function startJianying() {
    if (busy || !NW.state.path || !NW.state.chapter) {
      NW.toast("先打开工程并选中章节");
      return;
    }
    const button = el("exJianying");
    const box = el("exJianyingResult");
    busy = true;
    button.disabled = true;
    box.textContent = "正在生成剪映草稿…";
    try {
      const result = await NW.api("/api/ui/export/jianying", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        busy: "正在生成剪映草稿…",
        busyDetail: `章节 ${NW.state.chapter}`,
        body: JSON.stringify({ path: NW.state.path, chapter: NW.state.chapter }),
      });
      const counts = result.counts || {};
      const reconcile = result.reconcile || {};
      const lines = [
        `草稿：${result.draft_dir}`,
        `压缩包：${result.zip}`,
        `导入位置：${result.install_dir}`,
        `字幕 ${counts.subtitles || 0}/${counts.dialogue_lines || 0} 行　视频 ${counts.video_segments || 0}　图片 ${counts.image_segments || 0}　配音 ${counts.voice_segments || 0}　音效 ${counts.sfx_segments || 0}　BGM ${counts.bgm_segments || 0}`,
        `素材对账：引用 ${reconcile.referenced || 0} / 已复制 ${reconcile.copied || 0} / 缺失 ${(reconcile.missing || []).length}`,
      ];
      (result.subtitle_gaps || []).forEach((item) => lines.push(`字幕兜底：${item.line_id} ${item.reason}`));
      (result.warnings || []).forEach((item) => lines.push(`告警：${item}`));
      box.textContent = lines.join("\n");
      NW.toast("剪映草稿已生成", true);
      await loadFiles();
    } catch (error) {
      box.textContent = `生成失败：${error.message}`;
      NW.toast(`生成失败（退出码 ${error.code}）：${error.message}`);
    } finally {
      busy = false;
      button.disabled = false;
    }
  }

  /* 首次激活绑定事件。 */
  function bind() {
    if (bound) return;
    bound = true;
    fillStages();
    el("exStart").addEventListener("click", start);
    el("exJianying").addEventListener("click", startJianying);
    el("exRefresh").addEventListener("click", loadFiles);
    NW.onProject(() => {
      // 换工程后先禁用，等新记录读回来再决定按钮状态，避免刷新/切换期间连点触发第二个编排
      setStartButton("checking");
      loadFiles();
      loadRun();
    });
    NW.onChapter(() => {
      if (!polling) loadRun();
    });
  }

  window.Pages.export = {
    activate() {
      bind();
      setStartButton("checking");
      loadFiles();
      loadRun().then((run) => {
        if (run && run.status === "running") {
          polling = true;
          schedule(2000);
        }
      });
    },
  };
})();
