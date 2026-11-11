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

  /* 填两个阶段下拉框。 */
  function fillStages() {
    const options = STAGES.map(([value, label]) => `<option value="${value}">${esc(label)}</option>`).join("");
    el("exFrom").innerHTML = options;
    el("exTo").innerHTML = options;
    el("exFrom").value = "setting";
    el("exTo").value = "export";
  }

  /* 按编排记录画横向步骤条：15 个阶段全列出，缺记录的阶段按 pending。
   * 参数：run 编排记录（`run show --json` 的负载），可为 null。
   * 返回：HTML 字符串。
   */
  function stepbarHtml(run) {
    const recorded = new Map();
    ((run && run.stages) || []).forEach((item) => recorded.set(item.stage, item));
    return STAGES.map(([stage, label]) => {
      const item = recorded.get(stage);
      const status = item ? item.status : "pending";
      const note = item ? item.error || item.note || "" : "";
      return (
        `<li class="step" data-status="${esc(status)}" title="${esc(note || `${label}：${statusLabel(status)}`)}">` +
        `${esc(item && item.label ? item.label : label)}</li>`
      );
    }).join("");
  }

  /* 渲染阶段进度与状态行。参数：run 编排记录，可为 null。返回：无。 */
  function renderRun(run) {
    if (!run) {
      el("exStatus").textContent = "还没有编排记录。";
      el("exStages").innerHTML = stepbarHtml(null);
      return;
    }
    const stages = run.stages || [];
    const running = run.status === "running";
    el("exStatus").textContent =
      `run ${run.run_id}　状态 ${statusLabel(run.status)}　章节 ${run.chapter_id}　开始 ${run.started_at || ""}` +
      (run.finished_at ? `　结束 ${run.finished_at}` : "") +
      (running ? `　已记录 ${stages.length} 个阶段` : "");
    el("exStages").innerHTML = stepbarHtml(run);
    el("exStart").disabled = running || !NW.state.chapter;
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
      if (run) NW.toast(`编排结束：${run.status}`, run.status === "succeeded" || run.status === "dry_run");
      return;
    }
    schedule(2500);
  }

  /* 启动编排。 */
  async function start() {
    if (busy || !NW.state.path || !NW.state.chapter) return;
    busy = true;
    el("exStart").disabled = true;
    try {
      const result = await NW.api("/api/ui/produce", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
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
      el("exStart").disabled = false;
    } finally {
      busy = false;
    }
  }

  /* 首次激活绑定事件。 */
  function bind() {
    if (bound) return;
    bound = true;
    fillStages();
    el("exStart").addEventListener("click", start);
    el("exRefresh").addEventListener("click", loadFiles);
    NW.onProject(() => {
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
