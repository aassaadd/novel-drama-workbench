/* 导出页：启动全链路编排（后台进程）并跟踪阶段、查看成品文件。
 *
 * 编排状态只来自 `.ndw/runs/*.json`（`run show --json`），
 * 前端不推断「跑到哪一步」，因此刷新页面后进度不会错位。
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

  /* 渲染阶段表。 */
  function renderRun(run) {
    if (!run) {
      el("exStatus").textContent = "还没有编排记录。";
      el("exStages").innerHTML = "—";
      return;
    }
    const stages = run.stages || [];
    const running = run.status === "running";
    el("exStatus").textContent =
      `run ${run.run_id}　状态 ${run.status}　章节 ${run.chapter_id}　开始 ${run.started_at || ""}` +
      (run.finished_at ? `　结束 ${run.finished_at}` : "") +
      (running ? `　已记录 ${stages.length} 个阶段` : "");
    const rows = stages
      .map((item) => {
        const note = item.error ? `<span class="err">${esc(item.error)}</span>` : esc(item.note || "");
        return `<tr class="stage-${esc(item.status)}"><td>${MARK[item.status] || "?"}</td>` +
          `<td>${esc(item.label || LABEL[item.stage] || item.stage)}</td>` +
          `<td>${esc(item.status)}</td><td class="wrap">${note}</td></tr>`;
      })
      .join("");
    el("exStages").innerHTML = rows
      ? `<table class="grid"><thead><tr><th></th><th>阶段</th><th>状态</th><th>说明</th></tr></thead><tbody>${rows}</tbody></table>`
      : "<p class='muted'>进程刚启动，还没有阶段记录。</p>";
    el("exStart").disabled = running || !NW.state.chapter;
  }

  /* 拉最近一次编排记录。 */
  async function loadRun() {
    if (!NW.state.path) return null;
    try {
      const data = await NW.api(`/api/ui/produce/latest?${NW.qs({ path: NW.state.path })}`);
      renderRun(data.run);
      return data.run;
    } catch (error) {
      el("exStatus").textContent = `读取编排记录失败：${error.message}`;
      return null;
    }
  }

  /* 拉产物清单。 */
  async function loadFiles() {
    if (!NW.state.path) return;
    try {
      const data = await NW.api(`/api/ui/exports?${NW.qs({ path: NW.state.path })}`);
      const files = data.files || [];
      if (!files.length) {
        el("exFiles").innerHTML = "<p class='muted'>还没有产物：先跑一次编排。</p>";
        return;
      }
      const rows = files
        .map(
          (item) =>
            `<tr><td class="wrap">${esc(item.rel)}</td><td>${humanSize(item.size)}</td><td>${esc(item.mtime)}</td></tr>`
        )
        .join("");
      el("exFiles").innerHTML =
        `<table class="grid"><thead><tr><th>文件</th><th>体积</th><th>修改时间</th></tr></thead><tbody>${rows}</tbody></table>`;
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
