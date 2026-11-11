/* 底部时间线：按当前章节的台词行铺开时间轴，并挂本章成品音频试听。
 *
 * 数据来源（全部是既有只读接口，不新增后端）：
 *   GET /api/ui/lines    每行 duration_ms / pause_before_ms / pause_after_ms / speaker_id / state
 *   GET /api/ui/exports  renders/ 下的章节成品音频（文件真存在才用作播放源）
 *
 * 布局规则：
 *   - 宽度 ∝ 时长：每行按 `duration_ms` 给像素，前后停顿用更窄的间隙块表示；
 *   - 行状态 ready 视为已配音（绿），其余按未配音（灰 + 斜纹）；
 *   - 刻度尺按 1/2/5/10/30 秒自适应，保证相邻刻度间隔不小于 ~44px；
 *   - 点击块 → `NW.emit('focusLine', lineId)`；有成品音频时播放头跟随 `timeupdate` + rAF。
 *
 * 底部栏折叠时不设置 audio 的 src（省流量）；展开时才挂上播放源。
 */

(function () {
  const SPEAKER_PALETTE = ["#4c8dff", "#2fb8c6", "#e0b45c", "#c8a2ff", "#e08a9a", "#6fd0b8", "#9ab6d8", "#d9a021"];
  const AUDIO_EXT = /\.(wav|mp3|m4a|aac|ogg|flac)$/i;
  const TICK_STEPS = [1, 2, 5, 10, 15, 30, 60, 120]; // 秒
  const MIN_TICK_PX = 44;
  const PX_MIN = 18; // 每秒最少像素（再密就看不清块了，允许横向滚动）
  const PX_MAX = 80;

  const model = {
    chapterId: "",
    lines: [],
    audioRel: "",
    audioDurationMs: 0,
    pxPerSec: 24,
    expanded: false,
    loading: false,
    retry: 0,
    raf: 0,
  };

  let bound = false;
  let resizeTimer = null;

  const el = (id) => document.getElementById(id);

  /* 相对路径转成只读文件接口 URL（与媒体页、右栏共用同一套构造方式）。
   * 参数：rel 工程内相对路径。
   * 返回：URL 字符串；缺工程根目录时返回空串。
   */
  function fileUrl(rel) {
    return uiFileUrl(rel);
  }

  /* 从产物清单里挑本章的成品音频。
   * 参数：files `/api/ui/exports` 的 files；chapterId 章节 ID。
   * 返回：{ rel } 或 null（找不到就返回 null，不伪造路径）。
   */
  function pickAudio(files, chapterId) {
    if (!chapterId) return null;
    const candidates = (files || []).filter((file) => {
      const rel = String(file.rel || "");
      if (!rel.startsWith("renders/")) return false;
      if (!AUDIO_EXT.test(rel)) return false;
      const base = rel.split("/").pop() || "";
      return base.startsWith(`${chapterId}.`);
    });
    if (!candidates.length) return null;
    // 优先级：混音归一化 > 混音 > 干声 > 其他（stem.*/分轨排在最后）
    const score = (rel) => {
      if (/\.mix\.norm\.(wav|mp3|m4a)$/i.test(rel)) return 0;
      if (/\.mix\.(wav|mp3|m4a)$/i.test(rel)) return 1;
      if (/\.(mp3|m4a|aac)$/i.test(rel) && !/stem\./.test(rel)) return 2;
      if (/\.voice\.(wav|mp3)$/i.test(rel)) return 3;
      if (/stem\./.test(rel)) return 5;
      return 4;
    };
    candidates.sort((a, b) => score(a.rel) - score(b.rel) || a.rel.localeCompare(b.rel));
    return candidates[0];
  }

  /* 按 speaker_id 分配一个稳定的调色板颜色。
   * 参数：list 台词行数组。
   * 返回：Map(speaker_id → 颜色)。
   */
  function speakerColors(list) {
    const map = new Map();
    list.forEach((line) => {
      const key = line.speaker_id || "unknown";
      if (!map.has(key)) map.set(key, SPEAKER_PALETTE[map.size % SPEAKER_PALETTE.length]);
    });
    return map;
  }

  /* 求整条时间线的总时长（含首尾停顿），并给每行算出起止毫秒。
   * 参数：list 台词行数组。
   * 返回：{ spans, totalMs }，spans 元素为 { line, before, start, end, after }。
   */
  function layoutSpans(list) {
    const spans = [];
    let cursor = 0;
    list.forEach((line) => {
      const before = Math.max(0, Number(line.pause_before_ms) || 0);
      const duration = Math.max(0, Number(line.duration_ms) || 0);
      const after = Math.max(0, Number(line.pause_after_ms) || 0);
      const start = cursor + before;
      const end = start + duration;
      spans.push({ line, before, start, end, after });
      cursor = end + after;
    });
    return { spans, totalMs: cursor };
  }

  /* 按总时长与容器宽度算「每秒多少像素」。
   * 参数：totalMs 总时长；width 可视宽度。
   * 返回：数字（PX_MIN ~ PX_MAX）。
   */
  function pxPerSecond(totalMs, width) {
    const seconds = Math.max(totalMs, 1000) / 1000;
    const fit = (width || 900) / seconds;
    return Math.min(PX_MAX, Math.max(PX_MIN, fit));
  }

  /* 刻度尺 HTML：按自适应步长画刻度与秒数标签。
   * 参数：totalMs 总时长；px 每秒像素。
   * 返回：HTML 字符串。
   */
  function rulerHtml(totalMs, px) {
    const step = TICK_STEPS.find((value) => value * px >= MIN_TICK_PX) || TICK_STEPS[TICK_STEPS.length - 1];
    const secondsTotal = totalMs / 1000;
    const parts = [];
    for (let second = 0; second <= secondsTotal; second += step) {
      parts.push(`<span class="tl-tick" style="left:${(second * px).toFixed(1)}px">${second}s</span>`);
    }
    return parts.join("");
  }

  /* 间隙块（停顿）HTML。
   * 参数：left 左偏移毫秒；ms 停顿毫秒；px 每秒像素；kind before | after。
   * 返回：HTML 字符串（停顿为 0 时返回空串）。
   */
  function gapHtml(left, ms, px, kind) {
    if (!ms) return "";
    const width = Math.max(1, (ms / 1000) * px);
    return `<span class="tl-gap" data-gap="${kind}" style="left:${((left / 1000) * px).toFixed(1)}px;width:${width.toFixed(1)}px" title="${kind === "before" ? "前停" : "后停"} ${ms} ms"></span>`;
  }

  /* 台词块 HTML。宽度 ∝ duration_ms，颜色按说话人，未配音走灰态斜纹。
   * 参数：span layoutSpans 的单项；colors 说话人色表；px 每秒像素。
   * 返回：HTML 字符串。
   */
  function blockHtml(span, colors, px) {
    const line = span.line;
    const voiced = line.state === "ready" || line.tts_status === "ready" || line.state === "stale";
    const width = Math.max(3, ((span.end - span.start) / 1000) * px);
    const left = (span.start / 1000) * px;
    const label = line.text || "";
    const speaker = speakerLabel(line.speaker, line.speaker_id);
    const tip = `#${Number(line.index) + 1} ${speaker} · ${(Number(line.duration_ms) || 0)} ms · ${line.state || ""}\n${label}`;
    const speakerColor = voiced ? colors.get(line.speaker_id || "unknown") : "#6b7280";
    return (
      `<button class="tl-block" data-line="${esc(line.line_id)}" data-speaker="${esc(line.speaker_id || "unknown")}" ` +
      `data-state="${esc(voiced ? "voiced" : "idle")}" style="left:${left.toFixed(1)}px;width:${width.toFixed(1)}px;--spk:${speakerColor}" ` +
      `title="${esc(tip)}">` +
      `<span class="tl-no">${Number(line.index) + 1}</span>` +
      `<span class="tl-label">${esc(label)}</span></button>`
    );
  }

  /* 说话人图例：每个角色一个小圆点 + 名字。
   * 参数：list 台词行数组；colors 说话人色表。
   * 返回：HTML 字符串。
   */
  function legendHtml(list, colors) {
    const names = new Map();
    list.forEach((line) => {
      const key = line.speaker_id || "unknown";
      if (!names.has(key)) names.set(key, speakerLabel(line.speaker, line.speaker_id));
    });
    return [...names.entries()]
      .map(([key, name]) => `<span class="tl-legend-item"><i style="background:${colors.get(key)}"></i>${esc(name)}</span>`)
      .join("");
  }

  /* 画整条时间线（含播放器挂载）。只在数据变化 / 宽度变化时调用。
   * 参数：无。
   * 返回：无。
   */
  function render() {
    const box = el("timeline");
    if (!box) return;
    const state = (window.NW && NW.state) || {};

    if (!state.path) {
      box.innerHTML = "<p class='muted'>时间线：先打开工程。</p>";
      return;
    }
    if (!model.chapterId) {
      box.innerHTML = "<p class='muted'>时间线：先选择章节。</p>";
      return;
    }
    if (model.loading) {
      box.innerHTML = "<p class='muted'>时间线：读取台词…</p>";
      return;
    }
    if (!model.lines.length) {
      box.innerHTML = "<p class='muted'>本章还没有台词行：先在终端执行 <code>line split</code>。</p>";
      return;
    }

    const colors = speakerColors(model.lines);
    const { spans, totalMs } = layoutSpans(model.lines);
    const scrollWidth = (el("tlScroll") && el("tlScroll").clientWidth) || 0;
    const durationMs = Math.max(totalMs, model.audioDurationMs || 0, 1000);
    model.pxPerSec = pxPerSecond(durationMs, scrollWidth || box.clientWidth);
    const px = model.pxPerSec;
    const canvasWidth = Math.max(320, (durationMs / 1000) * px + px * 2);

    const blocks = spans
      .map((span) => gapHtml(span.start - span.before, span.before, px, "before") + blockHtml(span, colors, px) + gapHtml(span.end, span.after, px, "after"))
      .join("");

    const player = model.audioRel
      ? `<audio id="tlAudio" controls preload="none" src="${esc(fileUrl(model.audioRel))}"></audio>`
      : "<span class='muted'>本章还没有成品音频</span>";

    box.innerHTML =
      `<div class="tl-bar">` +
      `<span class="tl-total" id="tlTotal">总时长 <b>${esc(msText(durationMs))}</b>` +
      (model.audioDurationMs ? `　成品 ${esc(msText(model.audioDurationMs))}` : "") +
      `<span class="tl-audio-path muted">${model.audioRel ? esc(model.audioRel) : ""}</span></span>` +
      `<div class="tl-legend">${legendHtml(model.lines, colors)}</div>` +
      `<div class="tl-player" id="tlPlayer">${player}</div>` +
      `</div>` +
      `<div class="tl-scroll" id="tlScroll">` +
      `<div class="tl-canvas" id="tlCanvas" style="width:${canvasWidth.toFixed(0)}px">` +
      `<div class="tl-ruler">${rulerHtml(durationMs, px)}</div>` +
      `<div class="tl-lane">${blocks}</div>` +
      `<div class="tl-playhead" id="tlPlayhead" hidden></div>` +
      `</div></div>`;

    attachAudio();
    updatePlayhead();
  }

  /* 挂上播放源（折叠与非折叠的差别都在这里）并绑定播放头跟随。
   * 参数：无。
   * 返回：无。
   */
  function attachAudio() {
    const audio = el("tlAudio");
    if (!audio) return;
    if (!model.expanded) {
      // 折叠状态不加载音频：清掉 src，避免浏览器预取
      audio.removeAttribute("src");
      audio.load();
      return;
    }
    if (model.audioRel && !audio.getAttribute("src")) audio.setAttribute("src", fileUrl(model.audioRel));

    audio.addEventListener("loadedmetadata", () => {
      if (!Number.isFinite(audio.duration) || audio.duration <= 0) return;
      const next = audio.duration * 1000;
      if (Math.abs(next - model.audioDurationMs) < 500) return;
      model.audioDurationMs = next;
      syncScale(); // 成品比台词合计更长（尾巴音乐等）时只拉长画布，不重画块、不打断播放
    });
    audio.addEventListener("timeupdate", updatePlayhead);
    audio.addEventListener("play", loop);
    audio.addEventListener("pause", () => {
      cancelAnimationFrame(model.raf);
      updatePlayhead();
    });
    audio.addEventListener("ended", () => {
      cancelAnimationFrame(model.raf);
      updatePlayhead();
    });
    audio.addEventListener("error", () => {
      const player = el("tlPlayer");
      if (player && model.audioRel) {
        player.innerHTML = `<span class="tl-audio-path muted">浏览器没能加载 ${esc(model.audioRel)}（接口仍返回 200 时多为容器/编码不被支持）</span>`;
      }
    });
  }

  /* 播放中按帧推进播放头。参数：无。返回：无。 */
  function loop() {
    cancelAnimationFrame(model.raf);
    const audio = el("tlAudio");
    if (!audio || audio.paused) return;
    updatePlayhead();
    model.raf = requestAnimationFrame(loop);
  }

  /* 依据音频当前时间摆放播放头（没有音频时隐藏）。
   * 参数：无。
   * 返回：无。
   */
  function updatePlayhead() {
    const head = el("tlPlayhead");
    const audio = el("tlAudio");
    if (!head) return;
    if (!audio || !Number.isFinite(audio.currentTime)) {
      head.hidden = true;
      return;
    }
    head.hidden = false;
    head.style.left = `${(audio.currentTime * model.pxPerSec).toFixed(1)}px`;
  }

  /* 音频元数据到手后只拉长画布并补上「成品」时长，块的位置与比例保持不变。
   * 参数：无。
   * 返回：无。
   */
  function syncScale() {
    const canvas = el("tlCanvas");
    const total = el("tlTotal");
    if (!canvas) return;
    const { totalMs } = layoutSpans(model.lines);
    const durationMs = Math.max(totalMs, model.audioDurationMs || 0, 1000);
    canvas.style.width = `${Math.max(320, (durationMs / 1000) * model.pxPerSec + model.pxPerSec * 2).toFixed(0)}px`;
    if (total) {
      total.innerHTML =
        `总时长 <b>${esc(msText(durationMs))}</b>` +
        (model.audioDurationMs ? `　成品 ${esc(msText(model.audioDurationMs))}` : "") +
        `<span class="tl-audio-path muted">${model.audioRel ? esc(model.audioRel) : ""}</span>`;
    }
  }

  /* 点刻度尺：让音频跳到对应时间（没有成品音频时不做任何事）。
   * 参数：event 点击事件。
   * 返回：无。
   */
  function onRulerClick(event) {
    const ruler = event.target.closest(".tl-ruler");
    const audio = el("tlAudio");
    if (!ruler || !audio) return;
    const box = ruler.getBoundingClientRect();
    const seconds = (event.clientX - box.left) / model.pxPerSec;
    if (!Number.isFinite(seconds) || seconds < 0) return;
    audio.currentTime = seconds;
    updatePlayhead();
  }

  /* 点台词块：交给 app.js 的 focusLine（切到配音页并选中该行）。
   * 参数：event 点击事件。
   * 返回：无。
   */
  function onBlockClick(event) {
    const block = event.target.closest(".tl-block");
    if (!block) return;
    if (window.NW && NW.emit) NW.emit("focusLine", block.dataset.line);
  }

  /* 是否正在试听：重画会重建 audio 节点，因此播放期间跳过比例重算，避免打断。 */
  function isPlaying() {
    const audio = el("tlAudio");
    return Boolean(audio && !audio.paused);
  }

  /* 绑一次容器事件。参数：无。返回：无。 */
  function bind() {
    const box = el("timeline");
    if (!box || bound) return;
    bound = true;
    box.addEventListener("click", (event) => {
      onBlockClick(event);
      onRulerClick(event);
    });
    window.addEventListener("resize", () => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => {
        if (model.lines.length && !isPlaying()) render();
      }, 150);
    });
  }

  /* 读当前章节的台词行与成品音频并重画。
   * 参数：无。
   * 返回：Promise，无。
   */
  async function refresh() {
    bind();
    const state = (window.NW && NW.state) || {};
    const chapterId = state.chapter || "";
    model.chapterId = chapterId;
    if (!state.path || !chapterId) {
      model.lines = [];
      model.audioRel = "";
      model.audioDurationMs = 0;
      render();
      return;
    }
    model.loading = true;
    model.audioDurationMs = 0;
    render();
    try {
      const [lineData, exportData] = await Promise.all([
        NW.api(`/api/ui/lines?${NW.qs({ path: state.path, chapter: chapterId })}`),
        NW.api(`/api/ui/exports?${NW.qs({ path: state.path })}`).catch(() => ({ files: [] })),
      ]);
      if (model.chapterId !== chapterId) return; // 期间切走了，丢弃这次回包
      model.lines = lineData.lines || [];
      const picked = pickAudio(exportData.files || [], chapterId);
      model.audioRel = picked ? picked.rel : "";
      model.loading = false;
      // 后端偶发返回空表（同一 URL 紧接再请求就有数据），空表时补一次重试
      if (!model.lines.length && model.retry < 2) {
        model.retry += 1;
        setTimeout(refresh, 400);
      }
      if (model.lines.length) model.retry = 0;
      render();
    } catch (error) {
      model.loading = false;
      model.lines = [];
      model.audioRel = "";
      const box = el("timeline");
      if (box) box.innerHTML = `<div class="error-box">读取时间线失败：${esc(error.message)}</div>`;
    }
  }

  /* 展开 / 收起底部栏：收起时暂停并卸掉播放源（省流量）。
   * 参数：expanded 是否展开。
   * 返回：无。
   */
  function setExpanded(expanded) {
    const next = Boolean(expanded);
    if (next === model.expanded) return;
    model.expanded = next;
    const audio = el("tlAudio");
    if (!next && audio) {
      audio.pause();
      audio.removeAttribute("src");
      audio.load();
    }
    if (next && model.lines.length) render();
  }

  /* 初始化：订阅切章 / 打开工程，并按底部栏当前折叠态决定是否挂音频。
   * 参数：无。
   * 返回：无。
   */
  function init() {
    const bar = el("bottomBar");
    model.expanded = Boolean(bar && bar.dataset.collapsed !== "true");
    bind();
    if (window.NW) {
      if (NW.onChapter) NW.onChapter(() => refresh());
      if (NW.onProject) NW.onProject(() => refresh());
      NW.on("view", () => {
        // 视图切换后容器宽度可能变了，重算比例（不重新取数；播放中不打断）
        if (model.lines.length && !isPlaying()) render();
      });
    }
    refresh();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();

  window.NWTimeline = { refresh, render, setExpanded };
})();
