/* 大纲页：生成大纲（写入设定库）、落地成章节。
 *
 * 这两步分别对应 `outline generate` 与 `outline apply`；
 * 生成是模型调用（默认离线实现），落地是纯数据操作（建章节文件）。
 */

(function () {
  let bound = false;
  let busy = false;

  const el = (id) => document.getElementById(id);

  /* 渲染幕 / 章 / 节拍。 */
  function render(data) {
    const acts = data.acts || [];
    if (!acts.length) {
      el("olBody").innerHTML = "<p class='muted'>还没有大纲：先填参数点「生成大纲」。</p>";
      return;
    }
    const chapters = acts.reduce((sum, act) => sum + (act.chapters || []).length, 0);
    const beats = acts.reduce((sum, act) => sum + (act.chapters || []).reduce((n, ch) => n + (ch.beats || []).length, 0), 0);
    const parts = [`<p class="muted">共 ${acts.length} 幕 / ${chapters} 章 / ${beats} 个节拍（settings_revision=${data.settings_revision ?? "?"}）</p>`];
    acts.forEach((act) => {
      parts.push(`<div class="act"><h3>${esc(act.title || "未命名幕")}</h3><p class="muted">${esc(act.summary || "")}</p>`);
      (act.chapters || []).forEach((chapter) => {
        parts.push(`<div class="chapter-row"><h4>${esc(chapter.index)}. ${esc(chapter.title || "")}</h4>`);
        const outline = chapter.outline || "";
        if (outline) parts.push(`<p class="muted">${esc(outline)}</p>`);
        const list = (chapter.beats || [])
          .map(
            (beat, index) =>
              `<li><span class="beat-no">${index + 1}</span>${beat.turning_point ? "<span class='tag'>转折</span>" : ""}` +
              `${esc(beat.summary || "")}<span class="muted">（${esc(beat.target_words ?? "?")} 字）</span></li>`
          )
          .join("");
        parts.push(`<ul class="beats">${list}</ul></div>`);
      });
      parts.push("</div>");
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
