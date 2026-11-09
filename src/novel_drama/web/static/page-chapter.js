/* 章节页：正文生成、场景/台词切分与正文预览。
 *
 * 生成走 `chapter generate`（默认离线写作实现），切分走 `scene split` / `line split`。
 * 章节由上方「章节」下拉框统一选择，切换章节时页面跟着重载。
 */

(function () {
  let bound = false;
  let busy = false;

  const el = (id) => document.getElementById(id);

  /* 渲染章节元信息与场景列表。 */
  function render(chapter) {
    const scenes = chapter.scenes || [];
    const lines = scenes.reduce((sum, scene) => sum + (scene.lines || []).length, 0);
    el("chInfo").innerHTML = [
      `<div class="kv-row"><span class="kv-key">章节</span><span class="kv-val">${esc(chapter.id)}</span></div>`,
      `<div class="kv-row"><span class="kv-key">标题</span><span class="kv-val">${esc(chapter.index)}. ${esc(chapter.title || "")}</span></div>`,
      `<div class="kv-row"><span class="kv-key">状态</span><span class="kv-val">${esc(chapter.status || "")}</span></div>`,
      `<div class="kv-row"><span class="kv-key">字数</span><span class="kv-val">${esc(chapter.word_count || 0)}</span></div>`,
      `<div class="kv-row"><span class="kv-key">场景</span><span class="kv-val">${scenes.length} 个</span></div>`,
      `<div class="kv-row"><span class="kv-key">台词</span><span class="kv-val">${lines} 行</span></div>`,
    ].join("");
    const rows = scenes
      .map(
        (scene, index) =>
          `<tr><td>${index + 1}</td><td>${esc(scene.title || "场景")}</td><td>${esc(scene.location || "—")}</td>` +
          `<td>${esc(scene.mood || "—")}</td><td>${(scene.lines || []).length}</td>` +
          `<td>${(scene.sfx_events || []).length}/${(scene.key_images || []).length}/${(scene.video_segments || []).length}</td></tr>`
      )
      .join("");
    el("chScenesBody").innerHTML = scenes.length
      ? `<table class="grid"><thead><tr><th>#</th><th>标题</th><th>地点</th><th>氛围</th><th>台词</th><th>音效/图/视频</th></tr></thead><tbody>${rows}</tbody></table>`
      : "<p class='muted'>还没有场景：正文生成后可点「切分场景」。</p>";
    el("chContent").textContent = (chapter.content || "").trim() || "（正文为空）";
  }

  /* 读当前章节。 */
  async function load() {
    if (!NW.state.path || !NW.state.chapter) {
      el("chInfo").textContent = "先打开工程并选择章节";
      return;
    }
    try {
      render(await NW.api(`/api/ui/chapter?${NW.qs({ path: NW.state.path, chapter: NW.state.chapter })}`));
    } catch (error) {
      NW.toast(`读取章节失败：${error.message}`);
    }
  }

  /* 生成正文（regen 由勾选框决定）。 */
  async function generate() {
    if (busy || !NW.state.path || !NW.state.chapter) return;
    busy = true;
    el("chGenerate").disabled = true;
    try {
      const result = await NW.api("/api/ui/chapter/generate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: NW.state.path, chapter: NW.state.chapter, regen: el("chRegen").checked }),
      });
      NW.toast(`正文已生成：${result.words} 字（provider=${result.provider}）`, true);
      await load();
      if (NW.openProject) await NW.openProject();
    } catch (error) {
      NW.toast(`生成失败（退出码 ${error.code}）：${error.message}`);
    } finally {
      busy = false;
      el("chGenerate").disabled = false;
    }
  }

  /* 只预览不写盘。 */
  async function preview() {
    if (busy || !NW.state.path || !NW.state.chapter) return;
    busy = true;
    try {
      const result = await NW.api("/api/ui/chapter/generate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: NW.state.path, chapter: NW.state.chapter, regen: true, dry_run: true }),
      });
      el("chContent").textContent = result.preview || "（没有预览内容）";
      NW.toast("预览已生成，未写盘", true);
    } catch (error) {
      NW.toast(`预览失败（退出码 ${error.code}）：${error.message}`);
    } finally {
      busy = false;
    }
  }

  /* 切分场景或台词。 */
  async function split(which) {
    if (busy || !NW.state.path || !NW.state.chapter) return;
    busy = true;
    try {
      const result = await NW.api(which === "scene" ? "/api/ui/scene/split" : "/api/ui/line/split", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: NW.state.path, chapter: NW.state.chapter }),
      });
      const text =
        which === "scene"
          ? `已切分场景 ${(result.scenes || []).length} 个`
          : `已切分台词 ${result.lines} 行（未识别 ${result.unknown}）`;
      NW.toast(text, true);
      await load();
      if (NW.openProject) await NW.openProject();
    } catch (error) {
      NW.toast(`切分失败（退出码 ${error.code}）：${error.message}`);
    } finally {
      busy = false;
    }
  }

  /* 首次激活绑定事件。 */
  function bind() {
    if (bound) return;
    bound = true;
    el("chGenerate").addEventListener("click", generate);
    el("chPreview").addEventListener("click", preview);
    el("chScenes").addEventListener("click", () => split("scene"));
    el("chLines").addEventListener("click", () => split("line"));
    NW.onChapter(() => load());
  }

  window.Pages.chapter = {
    activate() {
      bind();
      load();
    },
  };
})();
