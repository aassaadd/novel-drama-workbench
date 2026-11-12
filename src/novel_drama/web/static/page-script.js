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
  let streamAbort = null; // 标注流式请求的中断句柄

  const el = (id) => document.getElementById(id);
  const KIND_LABEL = { dialogue: "对白", narration: "旁白", inner_monologue: "独白" };
  const CUE_LABEL = { sfx: "音效", visual: "画面", anim: "动画", silence: "静音", bgm: "配乐", "bgm-seg": "配乐段落" };
  const BGM_MOOD_LABELS = {
    calm: "平静",
    tense: "紧张",
    sad: "悲伤",
    romantic: "浪漫",
    mysterious: "悬疑",
    action: "动作",
    comedic: "喜剧",
    epic: "史诗",
    horror: "恐怖",
  };

  /* 标注里的英文枚举 → 中文：只影响展示，markup 原文与写回仍是英文原值。
   * 情绪 / 时间 / 音效类别 / 动画模式 / 机位的中文名来自 core.js，避免多处维护。 */
  const META_LABELS = { location: "地点", time: "时间", mood: "情绪" };

  /* 查表翻译英文枚举；未登记的值原样返回，避免静默丢信息。
   * 参数：labels 枚举映射；value 原值。
   * 返回：中文字符串；空值返回空串。
   */
  function labelOf(labels, value) {
    return labels[value] || value || "";
  }

  /* 指令位置参数翻译：after-line=N → 第 N 行后，其他值原样返回。
   * 参数：token 位置参数。
   * 返回：展示文本。
   */
  function positionText(token) {
    const match = /^after-line=(\d+)$/.exec(token || "");
    return match ? `第 ${match[1]} 行后` : token || "";
  }

  /* 镜头参数（location=… / time=… / mood=…）拆成中文名与中文值。
   * 参数：token 形如 `time=night`。
   * 返回：{ name, value }；无法解析时 name 为空、value 为原文。
   */
  function metaPair(token) {
    const index = String(token || "").indexOf("=");
    if (index <= 0) return { name: "", value: token || "" };
    const key = token.slice(0, index);
    const value = token.slice(index + 1);
    let text = value;
    if (key === "time") text = labelOf(TIME_LABELS, value);
    else if (key === "mood") text = emotionLabel(value);
    return { name: META_LABELS[key] || key, value: text };
  }

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
      key = labelOf(SFX_KIND_LABELS, parts[0] || "");
      detail = [parts[1] ? `${parts[1]}ms` : "", positionText(parts[2])].filter(Boolean).join(" · ");
      parts.splice(0, 3);
    } else if (cue.head === "silence") {
      detail = [parts[0] ? `${parts[0]}ms` : "", positionText(parts[1])].filter(Boolean).join(" · ");
      parts.splice(0, 2);
    } else if (cue.head === "visual") {
      key = parts[0] || "";
      parts.splice(0, 1);
    } else if (cue.head === "anim") {
      key = labelOf(ANIM_MODE_LABELS, parts[0] || "");
      detail = [parts[1] ? `${parts[1]}ms` : "", labelOf(CAMERA_LABELS, parts[2] || "")].filter(Boolean).join(" · ");
      parts.splice(0, 3);
    } else if (cue.head === "bgm" || cue.head === "bgm-seg") {
      const options = {};
      parts.forEach((item) => {
        const at = item.indexOf("=");
        if (at > 0) options[item.slice(0, at)] = item.slice(at + 1);
      });
      key = labelOf(BGM_MOOD_LABELS, options.mood || "");
      detail = [
        options.name ? options.name.replace(/\+/g, " ") : "",
        options.energy ? `energy ${options.energy}` : "",
        options.anchor ? `锚点 ${options.anchor}` : "",
        options.dur ? `${options.dur}ms` : "",
      ]
        .filter(Boolean)
        .join(" · ");
      parts.length = 0;
    }
    const color = { sfx: "mk-sfx", visual: "mk-visual", anim: "mk-anim", silence: "mk-silence", bgm: "mk-sfx", "bgm-seg": "mk-sfx" }[cue.head] || "mk-kind";
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
      `<div class="mk-line"><span class="mk-speaker">${esc(speakerLabel(speaker))}</span>` +
      (emotion ? `<span class="mk-tag mk-emotion">[${esc(emotionLabel(emotion))}]</span>` : "") +
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
          (shot.meta.length
            ? `<span class="shot-meta">${shot.meta
                .map((item) => {
                  const pair = metaPair(item);
                  return `<span>${esc([pair.name, pair.value].filter(Boolean).join(" "))}</span>`;
                })
                .join("")}</span>`
            : "") +
          `</div>`;
        const body = shot.lines.map(lineHtml).join("") + shot.cues.map(cueHtml).join("");
        return `<div class="shot">${head}${body || "<p class='muted'>这个镜头没有行</p>"}</div>`;
      })
      .join("");
  }

  /* 渲染标注文本与文件状态。 */
  function render(data) {
    const hasMarkup = Boolean(String(data.markup || "").trim());
    const fileName = String(data.markup_path || "").split("/").pop() || "";
    el("scInfo").innerHTML = [
      `<div class="kv-row"><span class="kv-key">标注状态</span><span class="kv-val">${esc(
        hasMarkup ? (data.has_script ? "已生成并应用" : "已生成预览，尚未应用") : "还没有标注"
      )}</span></div>`,
      fileName ? `<div class="kv-row"><span class="kv-key">文件</span><span class="kv-val">${esc(fileName)}</span></div>` : "",
      hasMarkup
        ? `<div class="kv-row"><span class="kv-key">重切</span><span class="kv-val">已有标注，重切场景/台词会使其失效，需要重新生成</span></div>`
        : "",
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

  /* 生成标注（流式）；apply=true 时生成完整后一并同步到章节。
   * 单批失败会保留已成功批次，并可只重试失败批次。
   * 参数：apply 是否同步进章节。
   * 返回：Promise<void>。
   */
  async function suggest(apply) {
    if (busy) {
      NW.toast("正在生成标注，请稍候…");
      return;
    }
    if (!NW.state.path || !NW.state.chapter) {
      NW.toast("先打开工程并选择章节");
      return;
    }
    if (apply) {
      const ok = window.confirm(
        "将根据本章正文重新生成标注，并同步更新本章的台词、音效、画面与视频位。\n" +
          "已有同名条目会被更新，手工改过的台词可能被覆盖。确定继续？"
      );
      if (!ok) return;
    }
    busy = true;
    const buttons = [el("scSuggest"), el("scApply")];
    const labels = buttons.map((btn) => btn.textContent);
    buttons.forEach((btn) => {
      btn.disabled = true;
    });
    if (el("scStop")) el("scStop").hidden = false;
    const progress = NW.progressInit();
    NW.busyShow(apply ? "正在生成并应用标注…" : "正在生成导演标注…", "正在规划批次…");
    el("scInfo").textContent = "生成中，可在弹层里查看批次进度…";
    el("scMarkup").innerHTML = "<p class='muted'>生成中，请稍候…</p>";

    /* 一批流式生成；返回最终结果（含 failures） */
    const run = async (body) => {
      streamAbort = new AbortController();
      return NW.streamPost(
        "/api/ui/script/suggest/stream",
        body,
        (event, data) => {
          if (event === "batch" || event === "chars") NW.busyDetail(NW.progressApply(progress, data));
        },
        streamAbort.signal
      );
    };

    try {
      let result = await run({ path: NW.state.path, chapter: NW.state.chapter, apply });
      const failures = result.failures || [];
      if (failures.length) {
        const detail = failures
          .slice(0, 3)
          .map((item) => `第 ${item.start_scene}–${item.end_scene} 个镜头（第 ${item.batch} 批）：${item.message}`)
          .join("\n");
        const retry = window.confirm(
          `有 ${failures.length} 批没有生成成功：\n${detail}\n\n已成功批次的标注已保留，只重试失败批次？`
        );
        if (retry) {
          NW.busyDetail("正在重试失败批次…");
          result = await run({ path: NW.state.path, chapter: NW.state.chapter, apply, resume: true });
        }
      }
      const warnings = result.warnings || [];
      const box = el("scWarnings");
      if (warnings.length) {
        box.hidden = false;
        box.innerHTML = `<strong>告警 ${warnings.length} 条</strong><ul>${warnings.map((item) => `<li>${esc(item)}</li>`).join("")}</ul>`;
      } else {
        box.hidden = true;
      }
      const left = (result.failures || []).length;
      const summary = result.apply
        ? `，已同步：新建场景 ${result.apply.scenes_created}、新增台词 ${result.apply.lines_added}、更新台词 ${result.apply.lines_updated}、音效 ${result.apply.sfx_added}、画面 ${result.apply.images_added}、视频位 ${result.apply.videos_added}`
        : apply && left
          ? "（仍有失败批次，未写入章节）"
          : "";
      NW.toast(
        `标注：镜头 ${result.shots} 个 / 台词 ${result.lines} 行${left ? `（仍有 ${left} 批失败）` : ""}${summary}`,
        !left
      );
      await load();
      if (apply && NW.openProject) await NW.openProject();
    } catch (error) {
      if (error && error.name === "AbortError") {
        NW.toast("已停止标注生成");
      } else {
        NW.toast(`生成失败（退出码 ${error.code}）：${error.message}`);
      }
      await load();
    } finally {
      busy = false;
      streamAbort = null;
      buttons.forEach((btn, index) => {
        btn.disabled = false;
        btn.textContent = labels[index];
      });
      if (el("scStop")) el("scStop").hidden = true;
      NW.busyHide();
      el("scInfo").textContent = "标注预览已更新";
    }
  }

  /* 重新切分场景 / 台词：先出影响清单，确认后执行（默认保留人工修改）。 */
  async function resplit(kind) {
    try {
      await NWResplit.run(kind, {
        keep: el("scKeepEdits").checked,
        onDone: () => load(),
      });
    } catch (error) {
      NW.toast(`切分失败（退出码 ${error.code}）：${error.message}`);
    }
  }

  /* 首次激活绑定事件。 */
  function bind() {
    if (bound) return;
    bound = true;
    el("scSuggest").addEventListener("click", () => suggest(false));
    el("scApply").addEventListener("click", () => suggest(true));
    el("scResplitScene").addEventListener("click", () => resplit("scene"));
    el("scResplitLine").addEventListener("click", () => resplit("line"));
    if (el("scStop")) {
      el("scStop").addEventListener("click", () => {
        if (streamAbort) streamAbort.abort();
      });
    }
    NW.onChapter(() => load());
  }

  window.Pages.script = {
    activate() {
      bind();
      load();
    },
  };
})();
