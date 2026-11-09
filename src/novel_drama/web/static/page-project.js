/* 项目页：工程概览、设定库编辑、世界设定增补、新建工程。
 *
 * 读取一律出 CLI 的 `--json`：`project info` 与 `setting show`。
 * 写入走 `setting set` / `setting world` / `init`，不在前端拼设定库 JSON。
 */

(function () {
  /* 可编辑的标量字段：key 与 `setting set` 的参数一一对应 */
  const FIELDS = [
    ["logline", "一句话概要", "premise"],
    ["theme", "主题", "premise"],
    ["core_conflict", "核心冲突", "premise"],
    ["ending_intent", "结局意图", "premise"],
    ["target_audience", "目标读者", "premise"],
    ["target_length", "目标篇幅", "premise"],
    ["era", "时代", "world"],
    ["tone", "基调", "style"],
    ["pov", "视角", "style"],
    ["tense", "时态", "style"],
    ["prose_style", "文风", "style"],
    ["visual_style", "视觉风格", "style"],
  ];

  let bound = false;
  let busy = false;

  const el = (id) => document.getElementById(id);

  /* 一行「标签 → 值」的展示结构。 */
  function row(label, value) {
    return `<div class="kv-row"><span class="kv-key">${esc(label)}</span><span class="kv-val">${esc(value)}</span></div>`;
  }

  /* 渲染工程概览。 */
  function renderInfo(info, bible) {
    const work = info.work || {};
    el("pjInfo").innerHTML = [
      row("工程", info.root),
      row("书名", work.title || "—"),
      row("作者", work.author || "—"),
      row("题材", (work.genre || []).join("、") || "—"),
      row("章节", `${(info.chapters || []).length} 章`),
      row("角色", `${info.characters || 0} 位`),
      row("设定库", `r${bible.settings_revision ?? "?"}（${bible.updated_at || ""}）`),
    ].join("");
  }

  /* 渲染设定库表单（值来自 bible 的 premise/world/style 三段）。 */
  function renderSettings(bible) {
    const html = FIELDS.map(([key, label, section]) => {
      const value = (bible[section] || {})[key] ?? "";
      return `<label class="stack">${esc(label)}
        <input data-field="${key}" value="${esc(value)}" />
      </label>`;
    });
    el("pjSettings").innerHTML = html.join("");
  }

  /* 渲染世界设定：规则/阵营/地点/词条。 */
  function renderWorld(world) {
    const rules = (world.rules || []).map((item) => `<li>${esc(item)}</li>`).join("") || "<li class='muted'>—</li>";
    const factions = (world.factions || []).map((item) => `<li>${esc(item)}</li>`).join("") || "<li class='muted'>—</li>";
    const locations =
      (world.locations || [])
        .map((item) => `<li>${esc(item.name)}　${esc(item.description || "")}${item.atmosphere ? `（${esc(item.atmosphere)}）` : ""}</li>`)
        .join("") || "<li class='muted'>—</li>";
    const glossary =
      (world.glossary || []).map((item) => `<li>${esc(item.term)}：${esc(item.meaning)}</li>`).join("") || "<li class='muted'>—</li>";
    el("pjWorld").innerHTML = `
      <div><h4>规则</h4><ul>${rules}</ul></div>
      <div><h4>阵营</h4><ul>${factions}</ul></div>
      <div><h4>地点</h4><ul>${locations}</ul></div>
      <div><h4>词条</h4><ul>${glossary}</ul></div>`;
  }

  /* 拉取并渲染整个页面。 */
  async function load() {
    const path = NW.state.path;
    if (!path) {
      el("pjInfo").textContent = "先在上方填工程目录并点「打开」";
      return;
    }
    try {
      const [info, bible] = await Promise.all([
        NW.api(`/api/project/info?${NW.qs({ path })}`),
        NW.api(`/api/ui/setting?${NW.qs({ path })}`),
      ]);
      renderInfo(info, bible);
      renderSettings(bible);
      renderWorld(bible.world || {});
    } catch (error) {
      NW.toast(`读取工程失败：${error.message}`);
    }
  }

  /* 保存标量设定。 */
  async function saveSettings() {
    if (busy || !NW.state.path) return;
    const fields = {};
    el("pjSettings")
      .querySelectorAll("input[data-field]")
      .forEach((input) => {
        fields[input.dataset.field] = input.value.trim();
      });
    busy = true;
    try {
      const result = await NW.api("/api/ui/setting/set", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: NW.state.path, fields }),
      });
      NW.toast(`已保存：设定库 r${result.settings_revision}`, true);
      await load();
    } catch (error) {
      NW.toast(`保存失败（退出码 ${error.code}）：${error.message}`);
    } finally {
      busy = false;
    }
  }

  /* 增补世界设定。 */
  async function addWorld() {
    if (busy || !NW.state.path) return;
    const body = {
      path: NW.state.path,
      rules: [el("wRule").value.trim()].filter(Boolean),
      factions: [el("wFaction").value.trim()].filter(Boolean),
      location: el("wLocation").value.trim(),
      glossary: el("wGlossary").value.trim(),
    };
    if (!body.rules.length && !body.factions.length && !body.location && !body.glossary) {
      NW.toast("先填一条要加入的内容");
      return;
    }
    busy = true;
    try {
      const result = await NW.api("/api/ui/setting/world", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      ["wRule", "wFaction", "wLocation", "wGlossary"].forEach((id) => {
        el(id).value = "";
      });
      NW.toast(`已更新世界设定 r${result.settings_revision}`, true);
      await load();
    } catch (error) {
      NW.toast(`写入失败（退出码 ${error.code}）：${error.message}`);
    } finally {
      busy = false;
    }
  }

  /* 把设定库导出成本地 JSON 文件（纯前端下载，不额外加接口）。 */
  async function exportBible() {
    if (!NW.state.path) return;
    try {
      const bible = await NW.api(`/api/ui/setting?${NW.qs({ path: NW.state.path })}`);
      const blob = new Blob([JSON.stringify(bible, null, 2)], { type: "application/json" });
      const link = document.createElement("a");
      link.href = URL.createObjectURL(blob);
      link.download = "setting-bible.json";
      link.click();
      URL.revokeObjectURL(link.href);
    } catch (error) {
      NW.toast(`导出失败：${error.message}`);
    }
  }

  /* 新建工程并切过去。 */
  async function createProject() {
    if (busy) return;
    const dir = el("npDir").value.trim();
    const title = el("npTitle").value.trim();
    if (!dir || !title) {
      NW.toast("目录与书名都要填");
      return;
    }
    busy = true;
    try {
      const result = await NW.api("/api/ui/project/init", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ dir, title, author: el("npAuthor").value.trim(), genres: [el("npGenre").value.trim()].filter(Boolean) }),
      });
      NW.state.path = result.root || dir;
      localStorage.setItem("ndw.path", NW.state.path);
      el("projectPath").value = NW.state.path;
      NW.toast("工程已创建", true);
      await NW.openProject();
    } catch (error) {
      NW.toast(`创建失败（退出码 ${error.code}）：${error.message}`);
    } finally {
      busy = false;
    }
  }

  /* 首次激活时绑定事件，避免重复绑定。 */
  function bind() {
    if (bound) return;
    bound = true;
    el("pjSave").addEventListener("click", saveSettings);
    el("pjWorldAdd").addEventListener("click", addWorld);
    el("pjExport").addEventListener("click", exportBible);
    el("npCreate").addEventListener("click", createProject);
    NW.onProject(() => load());
  }

  window.Pages.project = {
    activate() {
      bind();
      load();
    },
  };
})();
