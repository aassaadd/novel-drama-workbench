/* 导演标注页：生成有声剧标注（镜头/旁白/对白情绪/音效/画面/视频位）并可选应用。
 *
 * 走 `script suggest`：`--apply` 时会解析标注、同步台词并物化媒体条目。
 * 标注文本落 `scripts/<chapter>.markup.txt`，页面直接读这份产物展示。
 */

(function () {
  let bound = false;
  let busy = false;

  const el = (id) => document.getElementById(id);

  /* 渲染标注文本与文件状态。 */
  function render(data) {
    el("scInfo").innerHTML = [
      `<div class="kv-row"><span class="kv-key">标注文件</span><span class="kv-val">${esc(data.markup_path)}</span></div>`,
      `<div class="kv-row"><span class="kv-key">已解析脚本</span><span class="kv-val">${data.has_script ? "有" : "无"}</span></div>`,
    ].join("");
    el("scMarkup").textContent = data.markup || "（还没有标注文本：点上面的按钮生成）";
  }

  /* 读标注产物。 */
  async function load() {
    if (!NW.state.path || !NW.state.chapter) {
      el("scInfo").textContent = "先打开工程并选择章节";
      return;
    }
    try {
      render(await NW.api(`/api/ui/script?${NW.qs({ path: NW.state.path, chapter: NW.state.chapter })}`));
    } catch (error) {
      NW.toast(`读取标注失败：${error.message}`);
    }
  }

  /* 生成标注；apply=true 时一并应用。 */
  async function suggest(apply) {
    if (busy || !NW.state.path || !NW.state.chapter) return;
    busy = true;
    el("scSuggest").disabled = true;
    el("scApply").disabled = true;
    try {
      const result = await NW.api("/api/ui/script/suggest", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: NW.state.path, chapter: NW.state.chapter, apply }),
      });
      const warnings = result.warnings || [];
      const box = el("scWarnings");
      if (warnings.length) {
        box.hidden = false;
        box.innerHTML = `<strong>告警 ${warnings.length} 条</strong><ul>${warnings.map((item) => `<li>${esc(item)}</li>`).join("")}</ul>`;
      } else {
        box.hidden = true;
      }
      const summary = result.apply
        ? `，已同步：新建场景 ${result.apply.scenes_created}、新增台词 ${result.apply.lines_added}、更新台词 ${result.apply.lines_updated}、音效 ${result.apply.sfx_added}、画面 ${result.apply.images_added}、视频位 ${result.apply.videos_added}`
        : "";
      NW.toast(`标注：镜头 ${result.shots} 个 / 台词 ${result.lines} 行${summary}`, true);
      await load();
      if (apply && NW.openProject) await NW.openProject();
    } catch (error) {
      NW.toast(`生成失败（退出码 ${error.code}）：${error.message}`);
    } finally {
      busy = false;
      el("scSuggest").disabled = false;
      el("scApply").disabled = false;
    }
  }

  /* 首次激活绑定事件。 */
  function bind() {
    if (bound) return;
    bound = true;
    el("scSuggest").addEventListener("click", () => suggest(false));
    el("scApply").addEventListener("click", () => suggest(true));
    NW.onChapter(() => load());
  }

  window.Pages.script = {
    activate() {
      bind();
      load();
    },
  };
})();
