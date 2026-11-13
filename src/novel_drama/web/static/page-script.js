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
  let warningItems = []; // 硬告警的结构化定位信息（与 warnings 文案一一对应）
  let sceneTimer = null; // 镜头高亮的清理定时器
  let loadPromise = Promise.resolve(); // 最近一次标注加载的 Promise（跨页定位等它渲染完）

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
   * 参数：row { head, text }，head 形如 `林晚|angry|inner_monologue`；
   *       lineIndex 镜头内行序号（从 1 开始，告警定位高亮用）。
   * 返回：HTML 字符串。
   */
  function lineHtml(row, lineIndex) {
    const tokens = (row.head || "").split("|").map((token) => token.trim());
    const speaker = tokens[0] || "旁白";
    const emotion = tokens[1] || "";
    const kind = tokens[2] || "";
    const kindText = KIND_LABEL[kind] || kind;
    return (
      `<div class="mk-line" data-line-index="${lineIndex || ""}">` +
      `<span class="mk-speaker">${esc(speakerLabel(speaker))}</span>` +
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
        const body = shot.lines.map((line, index) => lineHtml(line, index + 1)).join("") + shot.cues.map(cueHtml).join("");
        // data-shot-index 供「告警定位」按 @shot 序号滚动高亮
        return `<div class="shot" data-shot-index="${esc(shot.index)}">${head}${body || "<p class='muted'>这个镜头没有行</p>"}</div>`;
      })
      .join("");
  }

  /* 告警的定位提示文案：告诉用户会跳到哪里，避免点了不知道为什么。
   * 参数：item 结构化告警（可能为 null）。
   * 返回：简短提示文本。
   */
  function warningHint(item) {
    if (!item) return "本章";
    if (item.kind === "speaker_unknown") return `台词行 · 镜头 ${item.shot_index}`;
    if (item.kind === "orphan") {
      return `镜头 ${item.shot_index}${item.line_index ? ` · 第 ${item.line_index} 行` : ""}`;
    }
    if (item.kind === "sfx_position") return `镜头 ${item.shot_index} · 音效`;
    return item.shot_index !== null && item.shot_index !== undefined ? `镜头 ${item.shot_index}` : "本章";
  }

  /* 渲染告警区：硬告警做成可点定位按钮，信息级保持折叠纯文本。
   * 参数：warnings 告警字符串列表；items 结构化定位列表（可缺省）。
   * 返回：无。
   */
  function renderWarnings(warnings, items) {
    const box = el("scWarnings");
    const infoPrefix = "【已自动修正】";
    const list = Array.isArray(warnings) ? warnings : [];
    warningItems = Array.isArray(items) ? items : [];
    if (!list.length) {
      box.hidden = true;
      box.innerHTML = "";
      return;
    }
    box.hidden = false;
    const info = list.filter((item) => String(item).startsWith(infoPrefix));
    const hard = list.filter((item) => !String(item).startsWith(infoPrefix));
    const hardHtml = hard.length
      ? `<strong>告警 ${hard.length} 条</strong><span class="warn-tip">点一条直接定位</span><ul class="warn-list">` +
        hard
          .map((text) => {
            const index = warningItems.findIndex((item) => item && item.message === text);
            const item = index >= 0 ? warningItems[index] : null;
            return (
              `<li><button type="button" class="warn-link" data-warn="${index}">` +
              `<span class="warn-text">${esc(text)}</span>` +
              `<span class="warn-go">定位 · ${esc(warningHint(item))}</span></button></li>`
            );
          })
          .join("") +
        "</ul>"
      : "<strong>没有需要人工处理的告警</strong>";
    const infoHtml = info.length
      ? `<details><summary>已自动修正 ${info.length} 项</summary><ul>${info
          .map((item) => `<li>${esc(String(item).replace(infoPrefix, ""))}</li>`)
          .join("")}</ul></details>`
      : "";
    box.innerHTML = hardHtml + infoHtml;
  }

  /* 等待某个元素出现（跨视图切换或页面异步重渲染时使用）。
   * 参数：selector CSS 选择器；timeout 超时毫秒数。
   * 返回：Promise<Element|null>。
   */
  function waitForElement(selector, timeout) {
    return new Promise((resolve) => {
      const found = document.querySelector(selector);
      if (found) {
        resolve(found);
        return;
      }
      const finish = (node) => {
        observer.disconnect();
        clearTimeout(timer);
        resolve(node);
      };
      const observer = new MutationObserver(() => {
        const node = document.querySelector(selector);
        if (node) finish(node);
      });
      observer.observe(document.body, { childList: true, subtree: true });
      const timer = setTimeout(() => finish(null), timeout);
    });
  }

  /* 切到指定视图并等切换完成；已在目标视图时直接复用（不重复触发加载，避免冲掉高亮）。
   * 参数：name 视图名（voice / script）。
   * 返回：Promise<void>。
   */
  async function ensureView(name) {
    if (location.hash !== `#/${name}`) {
      await new Promise((resolve) => {
        let settled = false;
        const done = () => {
          if (settled) return;
          settled = true;
          window.removeEventListener("hashchange", done);
          resolve();
        };
        window.addEventListener("hashchange", done);
        location.hash = `#/${name}`;
        setTimeout(done, 300); // hash 未变化或事件丢失时兜底继续
      });
    }
    // 标注页激活后会异步重渲染，等它完成，否则滚动与高亮会被随后的 innerHTML 覆盖
    if (name === "script") await loadPromise;
  }

  /* 清掉上一次镜头级定位的高亮与说明条。参数：无。返回：无。 */
  function clearSceneHighlight() {
    if (sceneTimer) {
      clearTimeout(sceneTimer);
      sceneTimer = null;
    }
    document.querySelectorAll("#scMarkup .shot.is-warn-target").forEach((node) => node.classList.remove("is-warn-target"));
    document.querySelectorAll("#scMarkup .mk-line.is-warn-line").forEach((node) => node.classList.remove("is-warn-line"));
    document.querySelectorAll("#scMarkup .warn-snippet").forEach((node) => node.remove());
  }

  /* 配音页选中行短暂闪烁，避免只靠右栏变化让人找不到行。
   * 参数：lineId 台词行 ID。
   * 返回：无。
   */
  function flashRow(lineId) {
    setTimeout(() => {
      const node = document.querySelector(`#rows .row[data-line="${CSS.escape(lineId)}"]`);
      if (!node) return;
      node.classList.add("row-flash");
      setTimeout(() => node.classList.remove("row-flash"), 2200);
    }, 80);
  }

  /* 定位到具体台词行：切到配音页、选中该行并展开说话人下拉。
   * 参数：item 结构化告警。
   * 返回：Promise<boolean>，行不存在时返回 false 供上层降级。
   */
  async function locateLine(item) {
    await ensureView("voice");
    await NW.openLine(item.chapter_id || NW.state.chapter, item.line_id);
    const resolved = (NW.state.rows || []).some((row) => row.line_id === item.line_id);
    if (!resolved) return false;
    flashRow(item.line_id);
    const speaker = document.getElementById("fSpeaker");
    if (speaker) {
      speaker.focus();
      // 原生 select 支持 showPicker 时直接展开，方便马上改说话人
      if (typeof speaker.showPicker === "function") {
        try {
          speaker.showPicker();
        } catch (error) {
          // 浏览器拒绝（非用户手势）时保持聚焦即可
        }
      }
    }
    NW.toast(`已定位到台词行：镜头 ${item.shot_index} · 第 ${item.line_index} 行`);
    return true;
  }

  /* 定位到镜头（正文里锚不到时的降级层级）：滚动到标注镜头并高亮该行、展示片段。
   * 参数：item 结构化告警。
   * 返回：Promise<boolean>，镜头不存在时返回 false。
   */
  async function locateShot(item) {
    await ensureView("script");
    const selector = `.shot[data-shot-index="${CSS.escape(String(item.shot_index))}"]`;
    const shot = await waitForElement(selector, 1600);
    if (!shot) return false;
    clearSceneHighlight();
    shot.classList.add("is-warn-target");
    const line = item.line_index ? shot.querySelector(`.mk-line[data-line-index="${item.line_index}"]`) : null;
    if (line) line.classList.add("is-warn-line");
    const head = shot.querySelector(".shot-head");
    if (head) {
      const hint = document.createElement("div");
      hint.className = "warn-snippet";
      hint.innerHTML =
        `<span>告警定位 · 镜头 ${esc(String(item.shot_index))}</span>` +
        (item.snippet ? `<span class="warn-snippet-text">「${esc(item.snippet)}」</span>` : "");
      head.insertAdjacentElement("afterend", hint);
    }
    // 镜头块可能很长：优先滚到目标台词行本身，避免整块居中后目标行落在视口外
    (line || shot).scrollIntoView({ block: "center", behavior: "smooth" });
    sceneTimer = setTimeout(() => {
      shot.classList.remove("is-warn-target");
      if (line) line.classList.remove("is-warn-line");
      const hint = shot.querySelector(".warn-snippet");
      if (hint) hint.remove();
      sceneTimer = null;
    }, 10000);
    NW.toast(`已定位到标注镜头 ${item.shot_index}${item.line_index ? ` 第 ${item.line_index} 行` : ""}`);
    return true;
  }

  /* 章节级兜底：滚到告警区并说明没有更精确的位置，不给死链。
   * 参数：item 结构化告警（可为 null）。
   * 返回：Promise<void>。
   */
  async function locateChapterLevel(item) {
    await ensureView("script");
    const box = el("scWarnings");
    if (!box) return;
    document.querySelectorAll("#scWarnings .warn-level-note").forEach((node) => node.remove());
    if (item) {
      // 持久说明降级原因：只靠 toast（4 秒）容易错过
      const note = document.createElement("div");
      note.className = "warn-level-note";
      note.textContent = "这类告警没有更精确的台词或镜头位置，只能定位到本章告警区。";
      box.insertBefore(note, box.firstChild);
    }
    box.scrollIntoView({ block: "center", behavior: "smooth" });
    box.classList.add("is-warn-target");
    setTimeout(() => box.classList.remove("is-warn-target"), 10000);
    NW.toast(item ? "这类告警没有精确位置，已定位到本章告警区" : "已定位到本章告警区");
  }

  /* 按类型分派告警定位：台词行 → 配音页；orphan / 音效 → 标注镜头；其余 → 告警区。
   * 参数：item 结构化告警（null 表示没有结构化信息）。
   * 返回：Promise<void>。
   */
  async function locateWarning(item) {
    if (!item) {
      await locateChapterLevel(null);
      return;
    }
    const anchorUsable = item.anchor_state && item.anchor_state !== "orphan";
    if (item.kind === "speaker_unknown" && item.line_id && anchorUsable) {
      if (await locateLine(item)) return;
      NW.toast("这行还没写入章节（当前为预览），已改为定位到标注中的镜头");
    }
    if (item.shot_index !== null && item.shot_index !== undefined && item.shot_index !== "") {
      if (await locateShot(item)) return;
      NW.toast("标注里没找到该镜头，已定位到本章告警区");
    }
    await locateChapterLevel(item);
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
    ].join("");
    el("scMarkup").innerHTML = markupHtml(data.markup);
    renderWarnings(data.warnings, data.warning_items);
  }

  /* 读标注产物；同时暴露本次加载的 Promise，供跨页定位等待渲染完成。
   * 参数：无。
   * 返回：Promise<void>。
   */
  function load() {
    loadPromise = loadInternal();
    return loadPromise;
  }

  /* 实际读取并渲染标注（load 的内部实现）。
   * 参数：无。
   * 返回：Promise<void>。
   */
  async function loadInternal() {
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

  /* 在全屏进度蒙版里挂一个「停止」按钮：蒙版会盖住工具条上的停止标注，
   * 不补这个入口生成中途就没法停。生成结束（finally）时移除。
   * 参数：无。
   * 返回：创建的按钮元素；蒙版不存在时返回 null。
   */
  function attachMaskStop() {
    const card = document.querySelector("#busyMask .busy-card");
    if (!card) return null;
    const button = document.createElement("button");
    button.type = "button";
    button.className = "btn small";
    button.textContent = "停止生成";
    button.style.marginTop = "10px";
    button.addEventListener("click", () => {
      if (streamAbort) streamAbort.abort();
    });
    card.appendChild(button);
    return button;
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
    const maskStop = attachMaskStop();
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

    let result = null;
    let interrupted = false;
    try {
      result = await run({ path: NW.state.path, chapter: NW.state.chapter, apply });
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
      renderWarnings(result.warnings || [], result.warning_items || []);
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
      const stopped = error && (error.name === "AbortError" || String(error.message || "").includes("已停止"));
      if (stopped) {
        interrupted = true;
        NW.toast("已停止标注生成");
      } else {
        NW.toast(`生成失败（退出码 ${error.code}）：${error.message}`);
      }
      await load();
    } finally {
      busy = false;
      streamAbort = null;
      if (maskStop && maskStop.parentElement) maskStop.remove();
      buttons.forEach((btn, index) => {
        btn.disabled = false;
        btn.textContent = labels[index];
      });
      if (el("scStop")) el("scStop").hidden = true;
      NW.busyHide();
      el("scInfo").textContent = interrupted ? "已停止生成（已完成批次已保留）" : "标注预览已更新";
    }
  }

  /* 首次激活绑定事件。 */
  function bind() {
    if (bound) return;
    bound = true;
    el("scSuggest").addEventListener("click", () => suggest(false));
    el("scApply").addEventListener("click", () => suggest(true));
    if (el("scStop")) {
      el("scStop").addEventListener("click", () => {
        if (streamAbort) streamAbort.abort();
      });
    }
    // 告警点一条 → 按类型定位（台词行 / 镜头 / 告警区），不让人自己找
    el("scWarnings").addEventListener("click", (event) => {
      const link = event.target.closest(".warn-link");
      if (!link) return;
      const index = Number(link.dataset.warn);
      void locateWarning(index >= 0 ? warningItems[index] : null);
    });
    NW.onChapter(() => load());
  }

  window.Pages.script = {
    activate() {
      bind();
      load();
    },
  };
})();
