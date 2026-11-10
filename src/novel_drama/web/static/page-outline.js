/* 大纲页：生成大纲（写入设定库）、落地成章节。
 *
 * 这两步分别对应 `outline generate` 与 `outline apply`；
 * 生成是模型调用（默认离线实现），落地是纯数据操作（建章节文件）。
 */

(function () {
  let bound = false;
  let busy = false;

  const el = (id) => document.getElementById(id);

  /* 渲染幕 / 章 / 节拍：幕与章都是可折叠的层级卡片，节拍是幕内的一行一条。
   * 参数：data `outline generate` / `/api/ui/outline` 的原始负载。
   * 返回：无（直接写 #olBody）。
   */
  function render(data) {
    const acts = data.acts || [];
    if (!acts.length) {
      el("olBody").innerHTML = "<p class='muted'>还没有大纲：先填参数点「生成大纲」。</p>";
      return;
    }
    const chapters = acts.reduce((sum, act) => sum + (act.chapters || []).length, 0);
    const beats = acts.reduce((sum, act) => sum + (act.chapters || []).reduce((n, ch) => n + (ch.beats || []).length, 0), 0);
    const parts = [`<p class="muted">共 ${acts.length} 幕 / ${chapters} 章 / ${beats} 个节拍（settings_revision=${esc(data.settings_revision ?? "?")}）</p>`];

    acts.forEach((act, actIndex) => {
      const actChapters = act.chapters || [];
      parts.push(
        `<details class="act" ${actIndex === 0 ? "open" : ""}>` +
          `<summary><span class="act-title">${esc(act.title || "未命名幕")}</span>` +
          `<span class="muted">${actChapters.length} 章</span></summary>` +
          (act.summary ? `<p class="muted">${esc(act.summary)}</p>` : "")
      );

      actChapters.forEach((chapter, chapterIndex) => {
        const beatList = chapter.beats || [];
        parts.push(
          `<details class="chapter-row" ${actIndex === 0 && chapterIndex === 0 ? "open" : ""}>` +
            `<summary><span>${esc(chapter.index)}. ${esc(chapter.title || "")}</span>` +
            `<span class="muted">${beatList.length} 节拍</span></summary>`
        );
        if (chapter.outline) parts.push(`<p class="muted">${esc(chapter.outline)}</p>`);
        const list = beatList
          .map(
            (beat, beatIndex) =>
              `<li><span class="beat-no">${beatIndex + 1}</span><span>${beat.turning_point ? "<span class='tag'>转折</span>" : ""}` +
              `${esc(beat.summary || "")}</span><span class="muted">${esc(beat.target_words ?? "?")} 字</span></li>`
          )
          .join("");
        parts.push(`<ul class="beats">${list || "<li class='muted'>没有节拍</li>"}</ul></details>`);
      });

      parts.push("</details>");
    });
    el("olBody").innerHTML = parts.join("");
  }

  /* 读大纲。 */
  async function load() {
    if (!NW.state.path) {
      el("olBody").innerHTML = "<p class='muted'>先在上方选择并打开工程。</p>";
      return;
    }
    try {
      render(await NW.api(`/api/ui/outline?${NW.qs({ path: NW.state.path })}`));
    } catch (error) {
      el("olBody").innerHTML = `<p class="muted">读取大纲失败：${esc(error.message)}</p>`;
    }
  }

  /* 渲染黄金三章检查结果：通过给绿框，未通过逐条列出「章节 + 问题 + 建议」。
   * 参数：data `/api/ui/outline/check` 的原始负载 {passed, issues}。
   * 返回：无（直接写 #olCheck）。
   */
  function renderCheck(data) {
    const box = el("olCheck");
    const passed = Boolean(data && data.passed);
    const issues = (data && data.issues) || [];
    box.className = `check-result ${passed ? "ok" : "fail"}`;
    if (passed) {
      box.innerHTML = `<div class="check-head">✓ 黄金三章检查通过</div>`;
      box.hidden = false;
      return;
    }
    const items = issues
      .map(
        (issue) =>
          `<li><span class="check-chapter">第 ${esc(issue.chapter ?? "?")} 章</span>` +
          `<span class="check-problem">${esc(issue.problem || "")}</span>` +
          `<span class="check-hint">${esc(issue.hint || "")}</span></li>`
      )
      .join("");
    box.innerHTML =
      `<div class="check-head">✗ 黄金三章未通过（${issues.length} 条建议）</div>` +
      (items ? `<ul class="check-list">${items}</ul>` : "");
    box.hidden = false;
  }

  /* 检查黄金三章：调 `/api/ui/outline/check`，只读不改大纲。 */
  async function checkOutline() {
    const box = el("olCheck");
    if (!NW.state.path) {
      box.className = "check-result fail";
      box.textContent = "先在上方选择并打开工程。";
      box.hidden = false;
      return;
    }
    const button = el("olCheckBtn");
    button.disabled = true;
    try {
      renderCheck(await NW.api(`/api/ui/outline/check?${NW.qs({ path: NW.state.path })}`));
    } catch (error) {
      box.className = "check-result fail";
      box.innerHTML = `<div class="check-head">检查失败：${esc(error.message)}</div>`;
      box.hidden = false;
      NW.toast(`检查失败：${error.message}`);
    } finally {
      button.disabled = false;
    }
  }

  /* 生成大纲。 */
  async function generate() {
    if (busy || !NW.state.path) return;
    busy = true;
    el("olGenerate").disabled = true;
    try {
      const result = await NW.api("/api/ui/outline/generate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          path: NW.state.path,
          acts: Number(el("olActs").value),
          chapters_per_act: Number(el("olChapters").value),
          beats_per_chapter: Number(el("olBeats").value),
          words: Number(el("olWords").value),
        }),
      });
      NW.toast(`大纲已生成：${result.chapters} 章 / ${result.beats} 个节拍（provider=${result.provider}）`, true);
      render(result);
      /* 生成后自动跑一次黄金三章检查（只提示，不阻断） */
      await checkOutline();
    } catch (error) {
      NW.toast(`生成失败（退出码 ${error.code}）：${error.message}`);
    } finally {
      busy = false;
      el("olGenerate").disabled = false;
    }
  }

  /* 落地成章节。 */
  async function apply() {
    if (busy || !NW.state.path) return;
    busy = true;
    el("olApply").disabled = true;
    try {
      const result = await NW.api("/api/ui/outline/apply", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: NW.state.path }),
      });
      NW.toast(`已落地：新建 ${result.created.length} 章，更新 ${result.updated.length} 章`, true);
      await load();
      if (NW.openProject) await NW.openProject();
    } catch (error) {
      NW.toast(`落地失败（退出码 ${error.code}）：${error.message}`);
    } finally {
      busy = false;
      el("olApply").disabled = false;
    }
  }

  /* 首次激活绑定事件。 */
  function bind() {
    if (bound) return;
    bound = true;
    el("olGenerate").addEventListener("click", generate);
    el("olCheckBtn").addEventListener("click", checkOutline);
    el("olApply").addEventListener("click", apply);
    NW.onProject(() => load());
  }

  window.Pages.outline = {
    activate() {
      bind();
      load();
    },
  };
})();
