/* 章节页：正文生成、场景/台词切分与正文预览。
 *
 * 生成走 `chapter generate`（默认离线写作实现），切分走 `scene split` / `line split`。
 * 章节由上方「章节」下拉框统一选择，切换章节时页面跟着重载。
 *
 * 布局：正文占主区，生成/切分/预览收进一条工具条，右侧是场景/台词紧凑列表。
 *
 * 「台词行 ↔ 正文段落」定位说明（降级实现）：
 *   台词行实体只有 scene_id / index / text，没有字符偏移；真锚点
 *   （`anchor.block_id` / `char_start` / `state`）只写在 `scripts/<chapter>.json` 里，
 *   而 `/api/ui/script` 只回 markup 文本与 has_script，前端读不到锚点。
 *   因此这里按「正文顺序 + 单调游标」做文本匹配：把每行文本依次在正文里往后找，
 *   命中就映射到所在段落；找不到（正文被改过、行文本被编辑）就不映射、不高亮，
 *   不伪造位置。段落切分规则与 `director/markup.py` 的 `_block_index` 一致（按空行）。
 */

(function () {
  let bound = false;
  let busy = false;
  let blocks = [];          // 当前正文段落 [{ index, start, end, text }]
  let lineMap = new Map();  // line_id → { block, start, end }（相对段落起点）
  let lineIndex = new Map(); // line_id → 章节详情里的行对象（右栏展示用）

  const el = (id) => document.getElementById(id);

  /* 把正文按空行切段，并记录每段在原文里的字符起止。
   * 参数：text 章节正文。
   * 返回：数组，元素为 { index, start, end, text }。
   */
  function paragraphsOf(text) {
    const source = String(text || "");
    const result = [];
    const pattern = /[^\n]+(?:\n(?!\s*\n)[^\n]+)*/g;
    let match = pattern.exec(source);
    while (match) {
      if (match[0].trim()) {
        result.push({ index: result.length, start: match.index, end: match.index + match[0].length, text: match[0] });
      }
      match = pattern.exec(source);
    }
    return result;
  }

  /* 建「台词行 → 正文段落」映射（文本匹配降级，见文件头说明）。
   * 参数：chapter 章节详情；list 段落数组。
   * 返回：Map(line_id → { block, start, end })，未命中的行不进 Map。
   */
  function buildLineMap(chapter, list) {
    const map = new Map();
    const content = chapter.content || "";
    let cursor = 0;
    (chapter.scenes || []).forEach((scene) => {
      (scene.lines || []).forEach((line) => {
        const needle = (line.text || "").trim();
        if (!needle) return;
        const position = content.indexOf(needle, cursor);
        if (position < 0) return; // 找不到就不映射，保持「无位置」而不是猜一个
        const block = list.find((item) => item.start <= position && position < item.end);
        if (!block) return;
        map.set(line.id, {
          block: block.index,
          start: position - block.start,
          end: position - block.start + needle.length,
        });
        cursor = position + needle.length;
      });
    });
    return map;
  }

  /* 渲染正文：每段一个 `<p>`；marks 里给出的行会被就地 `mark` 高亮。
   * 参数：list 段落数组；marks Map(line_id → { block, start, end })，可为空。
   * 返回：无。
   */
  function renderDoc(list, marks) {
    const spans = new Map();
    marks.forEach((span) => {
      if (!spans.has(span.block)) spans.set(span.block, []);
      spans.get(span.block).push(span);
    });

    const html = list.map((block) => {
      const ranges = (spans.get(block.index) || []).slice().sort((a, b) => a.start - b.start);
      let out = "";
      let cursor = 0;
      ranges.forEach((range) => {
        out += esc(block.text.slice(cursor, range.start));
        out += `<mark>${esc(block.text.slice(range.start, range.end))}</mark>`;
        cursor = range.end;
      });
      out += esc(block.text.slice(cursor));
      return `<p class="doc-p" data-para="${block.index}">${out}</p>`;
    });
    el("chContent").innerHTML = html.length ? html.join("") : "<p class='muted'>（正文为空）</p>";
  }

  /* 渲染章节元信息（横排 kv）。参数：chapter 章节详情；lineTotal 台词行数。返回：无。 */
  function renderInfo(chapter, lineTotal) {
    const scenes = chapter.scenes || [];
    el("chInfo").innerHTML = [
      `<div class="kv-row"><span class="kv-key">章节</span><span class="kv-val">${esc(chapter.index)}. ${esc(chapter.title || "")}</span></div>`,
      `<div class="kv-row"><span class="kv-key">状态</span><span class="kv-val">${esc(chapter.status || "")}</span></div>`,
      `<div class="kv-row"><span class="kv-key">字数</span><span class="kv-val">${esc(chapter.word_count || 0)}</span></div>`,
      `<div class="kv-row"><span class="kv-key">场景</span><span class="kv-val">${scenes.length}</span></div>`,
      `<div class="kv-row"><span class="kv-key">台词</span><span class="kv-val">${lineTotal} 行</span></div>`,
      `<div class="kv-row"><span class="kv-key">可定位</span><span class="kv-val">${lineMap.size} 行</span></div>`,
    ].join("");
  }

  /* 渲染右侧场景 / 台词紧凑列表：场景一块，台词一行一个按钮。
   * 参数：chapter 章节详情。
   * 返回：无。
   */
  function renderScenes(chapter) {
    const scenes = chapter.scenes || [];
    lineIndex = new Map();
    if (!scenes.length) {
      el("chScenesBody").innerHTML = "<p class='muted'>还没有场景：正文生成后可点「切分场景」。</p>";
      return;
    }
    el("chScenesBody").innerHTML = scenes
      .map((scene, sceneIndex) => {
        const lines = scene.lines || [];
        const rows = lines
          .map((line, lineIndexer) => {
            lineIndex.set(line.id, { line, scene });
            const entry = lineMap.get(line.id);
            const para = entry ? entry.block : "";
            return (
              `<button class="line-chip" data-line="${esc(line.id)}" data-para="${para}" ` +
              `title="${para === "" ? "正文里没找到这一行，无法定位" : `正文第 ${Number(para) + 1} 段`}">` +
              `<span class="lc-no">${lineIndexer + 1}</span>` +
              `<span class="lc-kind">${esc(NW.kindLabel(line.kind))}</span>` +
              `<span class="lc-text">${esc(line.text || "")}</span></button>`
            );
          })
          .join("");
        const extra = `${(scene.sfx_events || []).length}/${(scene.key_images || []).length}/${(scene.video_segments || []).length}`;
        return (
          `<div class="scene-block" data-scene="${esc(scene.id)}"><div class="scene-head">` +
          `<span>${sceneIndex + 1}. ${esc(scene.title || scene.location || "场景")}</span>` +
          `<span class="muted">${lines.length} 行 · 音效/图/视频 ${extra}</span></div>` +
          `${rows || "<p class='muted'>还没有台词行</p>"}</div>`
        );
      })
      .join("");
  }

  /* 渲染整页。参数：chapter 章节详情。返回：无。 */
  function render(chapter) {
    blocks = paragraphsOf(chapter.content);
    lineMap = buildLineMap(chapter, blocks);
    const lineTotal = (chapter.scenes || []).reduce((sum, scene) => sum + (scene.lines || []).length, 0);
    renderInfo(chapter, lineTotal);
    renderDoc(blocks, new Map());
    renderScenes(chapter);
    // 场景数据已就绪：让右栏能显示本章信息
    NW.emit("chapterData", chapter);
  }

  /* 读当前章节。 */
  async function load() {
    if (!NW.state.path || !NW.state.chapter) {
      el("chInfo").textContent = "先打开工程并选择章节";
      el("chContent").innerHTML = "—";
      el("chScenesBody").innerHTML = "—";
      return;
    }
    try {
      const data = await fetchChapter();
      if (!data) {
        el("chContent").innerHTML = "<p class='muted'>读到的章节数据不完整，请点顶栏「刷新」重试。</p>";
        return;
      }
      render(data);
    } catch (error) {
      NW.toast(`读取章节失败：${error.message}`);
    }
  }

  /* 读本章详情。本地开发服务在并发请求下偶发串响应（拿到别的接口的 body），
   * 形状对不上就退避重试一次；仍失败返回 null，由调用方给出提示而不是渲染空页。
   * 参数：无。
   * 返回：Promise<章节详情 | null>。
   */
  async function fetchChapter() {
    for (let attempt = 0; attempt < 2; attempt += 1) {
      const data = await NW.api(`/api/ui/chapter?${NW.qs({ path: NW.state.path, chapter: NW.state.chapter })}`);
      if (data && data.id === NW.state.chapter) return data;
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
    return null;
  }

  /* 点台词行：在正文里定位并高亮所在段落；点段落则反过来点亮该段的台词行。
   * 参数：event 台词列表的点击事件。
   * 返回：无。
   */
  function onLineClick(event) {
    const chip = event.target.closest(".line-chip");
    if (!chip) return;
    const lineId = chip.dataset.line;
    const entry = lineMap.get(lineId);
    const found = lineIndex.get(lineId) || {};
    const line = found.line || {};
    const scene = found.scene || {};
    const payload = {
      line_id: lineId,
      index: line.index,
      kind: line.kind,
      speaker: line.speaker || line.speaker_id,
      text: line.text || chip.textContent.trim(),
      emotion: line.emotion,
      emotion_intensity: line.emotion_intensity,
      speed: line.speed,
      pause_before_ms: line.pause_before_ms,
      pause_after_ms: line.pause_after_ms,
      delivery_note: line.delivery_note,
      tts: line.tts,
      state: (line.tts || {}).status,
      duration_ms: (line.tts || {}).duration_ms,
      scene: scene.title || scene.location,
    };
    document.querySelectorAll("#chScenesBody .line-chip.is-target").forEach((node) => node.classList.remove("is-target"));
    chip.classList.add("is-target");

    if (!entry) {
      // 文本匹配降级：这一行在正文里找不到，不清空正文、也不伪造位置
      renderDoc(blocks, new Map());
      NW.toast("这一行在正文里没找到对应段落（正文可能已修改），不做高亮");
      NW.emit("line", payload);
      return;
    }
    renderDoc(blocks, new Map([[lineId, entry]]));
    const target = el("chContent").querySelector(`.doc-p[data-para="${entry.block}"]`);
    if (target) {
      target.classList.add("is-target");
      target.scrollIntoView({ block: "center" });
    }
    payload.para = entry.block;
    NW.emit("line", payload);
  }

  /* 点正文段落：高亮该段映射到的台词行，便于从正文找回台词。
   * 参数：event 正文点击事件。
   * 返回：无。
   */
  function onParaClick(event) {
    const paraNode = event.target.closest(".doc-p");
    if (!paraNode) return;
    const para = Number(paraNode.dataset.para);
    el("chContent").querySelectorAll(".doc-p.is-target").forEach((node) => node.classList.remove("is-target"));
    paraNode.classList.add("is-target");
    document.querySelectorAll("#chScenesBody .line-chip").forEach((chip) => {
      chip.classList.toggle("is-target", chip.dataset.para !== "" && Number(chip.dataset.para) === para);
    });
    const first = [...lineMap.entries()].find(([, span]) => span.block === para);
    if (first) {
      const found = lineIndex.get(first[0]) || {};
      NW.emit("line", { ...(found.line || {}), line_id: first[0], para });
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

  /* 只预览不写盘：预览文本按同样的段落规则渲染，但清掉定位映射（不是落盘正文）。 */
  async function preview() {
    if (busy || !NW.state.path || !NW.state.chapter) return;
    busy = true;
    try {
      const result = await NW.api("/api/ui/chapter/generate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: NW.state.path, chapter: NW.state.chapter, regen: true, dry_run: true }),
      });
      blocks = paragraphsOf(result.preview || "");
      lineMap = new Map();
      renderDoc(blocks, new Map());
      el("chInfo").innerHTML = `<div class="kv-row"><span class="kv-key">预览</span><span class="kv-val">未写盘（${blocks.length} 段）</span></div>`;
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
    el("chScenesBody").addEventListener("click", onLineClick);
    el("chContent").addEventListener("click", onParaClick);
    NW.onChapter(() => load());
  }

  window.Pages.chapter = {
    activate() {
      bind();
      load();
    },
  };
})();
