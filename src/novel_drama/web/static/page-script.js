/* 导演标注页：生成有声剧标注（镜头/旁白/对白情绪/音效/画面/视频位）并可选应用。
 *
 * 走 `script suggest`：`--apply` 时会解析标注、同步台词并物化媒体条目。
 * 标注文本落 `scripts/<chapter>.markup.txt`，页面直接读这份产物展示。
 *
 * 展示形态：按 `@shot` 分段的只读脚本，行内把已有字段做成彩色标记
 * （情绪 `[angry]`、类型 `(inner_monologue)`、音效/画面/动画/静音 `{sfx}` 等）。
 * 语法完全按 `director/markup.py` 的实现：不加任何原文里没有的标记。
 */

(function () {
  let bound = false;
  let busy = false;

  const el = (id) => document.getElementById(id);
  const KIND_LABEL = { dialogue: "对白", narration: "旁白", inner_monologue: "独白" };
  const CUE_LABEL = { sfx: "音效", visual: "画面", anim: "动画", silence: "静音" };

  /* 把标注文本解析成「镜头 → 台词行 / 指令行」两段结构。
   * 参数：markup 标注原文（`scripts/<chapter>.markup.txt`）。
   * 返回：数组，元素为 { index, name, meta, lines, cues }；
   *       lines 元素 { head, text }，cues 元素 { head, parts }。
   */
  function parse(markup) {
    const shots = [];
    let current = null;

    String(markup || "")
      .split(/\r?\n/)
      .forEach((raw) => {
        const line = raw.trim();
        if (!line || line.startsWith("#")) return;

        if (line.startsWith("@shot")) {
          const tokens = line.split(/\s+/);
          current = {
            index: tokens[1] || "",
            name: tokens.slice(2).filter((token) => !token.includes("=")).join(" "),
            meta: tokens.slice(2).filter((token) => token.includes("=")),
            lines: [],
            cues: [],
          };
          shots.push(current);
          return;
        }

        if (!current) {
          current = { index: "", name: "（未声明镜头）", meta: [], lines: [], cues: [] };
          shots.push(current);
        }

        if (line.startsWith("!")) {
          const parts = line.slice(1).trim().split(/\s+/);
          current.cues.push({ head: (parts[0] || "").toLowerCase(), parts: parts.slice(1) });
          return;
        }

        const sep = line.search(/[：:]/);
        if (sep >= 0) current.lines.push({ head: line.slice(0, sep).trim(), text: line.slice(sep + 1).trim() });
        else current.lines.push({ head: "", text: line });
      });

    return shots;
  }

  /* 指令行 → HTML：`{音效}` 之类的尖括号标记 + 关键参数。
   * 参数：cue { head, parts }。
   * 返回：HTML 字符串。
   */
  function cueHtml(cue) {
    const parts = cue.parts || [];
    const tag = CUE_LABEL[cue.head] || cue.head || "指令";
    let key = "";
    let detail = "";
    if (cue.head === "sfx") {
      key = parts[0] || "";
      detail = `${parts[1] || "?"}ms · ${parts[2] || "?"}`;
      parts.splice(0, 3);
    } else if (cue.head === "silence") {
      detail = `${parts[0] || "?"}ms · ${parts[1] || "?"}`;
      parts.splice(0, 2);
    } else if (cue.head === "visual") {
      key = parts[0] || "";
      parts.splice(0, 1);
    } else if (cue.head === "anim") {
      key = [parts[0], parts[1], parts[2]].filter(Boolean).join(" ");
      detail = parts[2] || "";
      parts.splice(0, 3);
    }
    const color = { sfx: "mk-sfx", visual: "mk-visual", anim: "mk-anim", silence: "mk-silence" }[cue.head] || "mk-kind";
    return (
      `<div class="mk-cue"><span class="mk-tag ${color}">{${esc(tag)}}</span>` +
      (key ? `<span class="mk-tag ${color}">${esc(key)}</span>` : "") +
      `<span class="mk-cue-text">${esc(parts.join(" "))}</span>` +
      (detail ? `<span class="muted">${esc(detail)}</span>` : "") +
      `</div>`
    );
  }

  /* 台词行 → HTML：说话人 + `[情绪]` + `(类型)` + 正文。
   * 参数：row { head, text }，head 形如 `林晚|angry|inner_monologue`。
   * 返回：HTML 字符串。
   */
  function lineHtml(row) {
    const tokens = (row.head || "").split("|").map((token) => token.trim());
    const speaker = tokens[0] || "旁白";
    const emotion = tokens[1] || "";
    const kind = tokens[2] || "";
    const kindText = KIND_LABEL[kind] || kind;
    return (
      `<div class="mk-line"><span class="mk-speaker">${esc(speaker)}</span>` +
      (emotion ? `<span class="mk-tag mk-emotion">[${esc(emotion)}]</span>` : "") +
      (kindText ? `<span class="mk-tag mk-kind">(${esc(kindText)})</span>` : "") +
      `<span class="mk-text">${esc(row.text || "")}</span></div>`
    );
  }

  /* 整份标注 → HTML。参数：markup 标注原文。返回：HTML 字符串。 */
  function markupHtml(markup) {
    if (!String(markup || "").trim()) return "<p class='muted'>（还没有标注文本：点上面的按钮生成）</p>";
    return parse(markup)
      .map((shot) => {
        const head =
          `<div class="shot-head">` +
          (shot.index ? `<span class="shot-no">#${esc(shot.index)}</span>` : "") +
          `<span class="shot-name">${esc(shot.name || "未命名镜头")}</span>` +
          (shot.meta.length ? `<span class="shot-meta">${shot.meta.map((item) => `<span>${esc(item)}</span>`).join("")}</span>` : "") +
          `</div>`;
        const body = shot.lines.map(lineHtml).join("") + shot.cues.map(cueHtml).join("");
        return `<div class="shot">${head}${body || "<p class='muted'>这个镜头没有行</p>"}</div>`;
      })
      .join("");
  }

  /* 渲染标注文本与文件状态。 */
  function render(data) {
    el("scInfo").innerHTML = [
      `<div class="kv-row"><span class="kv-key">标注文件</span><span class="kv-val">${esc(data.markup_path)}</span></div>`,
      `<div class="kv-row"><span class="kv-key">已解析脚本</span><span class="kv-val">${data.has_script ? "有" : "无"}</span></div>`,
    ].join("");
    el("scMarkup").innerHTML = markupHtml(data.markup);
  }

  /* 读标注产物。 */
  async function load() {
    if (!NW.state.path || !NW.state.chapter) {
      el("scInfo").textContent = "先打开工程并选择章节";
      el("scMarkup").innerHTML = "";
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
    if (busy) {
      NW.toast("正在生成标注，请稍候…");
      return;
    }
    if (!NW.state.path || !NW.state.chapter) {
      NW.toast("先打开工程并选择章节");
      return;
    }
    busy = true;
    const buttons = [el("scSuggest"), el("scApply")];
    const labels = buttons.map((btn) => btn.textContent);
    buttons.forEach((btn) => {
      btn.disabled = true;
    });
    const hint = apply
      ? "正在生成并应用标注（长章节会分批调用模型，可能需要 1–2 分钟）…"
      : "正在生成标注（长章节会分批调用模型，可能需要 1–2 分钟）…";
    el("scInfo").textContent = hint;
    el("scMarkup").innerHTML = "<p class='muted'>生成中，请稍候…</p>";
    NW.toast(hint);
    try {
      const result = await NW.api("/api/ui/script/suggest", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: NW.state.path, chapter: NW.state.chapter, apply }),
        busy: apply ? "正在生成并应用标注…" : "正在生成导演标注…",
        busyDetail: "分批调用模型，长章节可能需要 1–2 分钟",
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
      await load();
    } finally {
      busy = false;
      buttons.forEach((btn, index) => {
        btn.disabled = false;
        btn.textContent = labels[index];
      });
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
