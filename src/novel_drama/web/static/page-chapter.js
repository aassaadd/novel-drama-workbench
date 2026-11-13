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
  let editing = false;      // 正文是否处于编辑态（textarea 会顶替只读段落）
  let chapterCache = null;  // 最近一次渲染的章节详情（编辑 / 取消时用）
  let lastSelection = null; // 最近一次在正文里选中的字符范围（打开 AI 编辑时用）
  let aiMask = null;        // AI 编辑弹窗遮罩
  let summarizeMask = null; // 按正文重总结弹窗遮罩
  let blocks = [];          // 当前正文段落 [{ index, start, end, text }]
  let lineMap = new Map();  // line_id → { block, start, end }（相对段落起点）
  let lineIndex = new Map(); // line_id → 章节详情里的行对象（右栏展示用）
  let lineEdit = null;      // 正在行内编辑的台词：{ lineId, wrap, input }
  let castRows = null;      // 人物表缓存（行内编辑「说话人」下拉用）
  let castLoading = null;   // 正在进行的人物表请求

  const el = (id) => document.getElementById(id);

  /* ---------------- 页面实时流（SSE） ---------------- */

  let streamAbort = null;   // 当前流式请求的 AbortController（取消时中断连接）
  let streamBox = null;     // 正文区里的实时输出容器
  let streamText = "";      // 已收到的增量全文
  let streamStartedAt = 0;  // 开始时间（算「已用 X 秒」）
  let streamTimer = null;   // 计时器（没有新内容时秒数也要走）

  /* 解析一段 SSE 文本（到空行为止），按事件名分发。
   * 参数：block 原始块；onEvent(event, data) 回调。
   * 返回：无。
   */
  function dispatchSse(block, onEvent) {
    let event = "message";
    const dataLines = [];
    block.split("\n").forEach((line) => {
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
    });
    if (!dataLines.length) return;
    let data = null;
    try {
      data = JSON.parse(dataLines.join("\n"));
    } catch (error) {
      return;
    }
    onEvent(event, data || {});
  }

  /* 用 fetch + ReadableStream 读取 SSE（EventSource 不能带 POST body）。
   * 参数：url 接口地址；body 请求体；onEvent 事件回调；signal 取消信号。
   * 返回：Promise，done 事件的 result；error 事件或连接失败时 reject。
   */
  async function streamPost(url, body, onEvent, signal) {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal,
    });
    if (!response.ok || !response.body) {
      let message = `HTTP ${response.status}`;
      try {
        const detail = await response.json();
        message = (detail && (detail.detail || detail.message)) || message;
      } catch (error) {
        // 非 JSON 错误体：保留 HTTP 状态码
      }
      throw new Error(message);
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buffer = "";
    let result = null;
    let failure = "";
    const handle = (event, data) => {
      if (event === "delta" && data.text) onEvent("delta", data);
      else if (event === "done") {
        result = data.result || null;
        onEvent("done", data);
      } else if (event === "error") {
        failure = data.message || "生成失败";
      } else if (event === "start") {
        onEvent("start", data);
      }
    };
    while (true) {
      const chunk = await reader.read();
      if (chunk.done) break;
      buffer += decoder.decode(chunk.value, { stream: true });
      let index = buffer.indexOf("\n\n");
      while (index >= 0) {
        dispatchSse(buffer.slice(0, index), handle);
        buffer = buffer.slice(index + 2);
        index = buffer.indexOf("\n\n");
      }
    }
    if (buffer.trim()) dispatchSse(buffer, handle);
    if (failure) throw new Error(failure);
    if (!result && signal && signal.aborted) throw new Error("已停止生成");
    return result;
  }

  /* 统计正文有效字数（与后端一致：去掉空白后的字符数）。
   * 参数：text 文本。
   * 返回：字数。
   */
  function streamWordCount(text) {
    return String(text || "").replace(/\s/g, "").length;
  }

  /* 刷新「已生成 N 字 / 已用 X 秒」。
   * 参数：无。
   * 返回：无。
   */
  function refreshStreamStatus() {
    if (!streamStartedAt) return;
    const seconds = ((Date.now() - streamStartedAt) / 1000).toFixed(1);
    el("chInfo").innerHTML =
      `<div class="kv-row"><span class="kv-key">生成中</span>` +
      `<span class="kv-val">已生成 ${streamWordCount(streamText)} 字 / 已用 ${seconds} 秒</span></div>`;
  }

  /* 开始流式输出：清空正文区并挂一个实时文本容器。
   * 参数：无。
   * 返回：无。
   */
  function beginStream() {
    streamText = "";
    streamStartedAt = Date.now();
    streamBox = document.createElement("div");
    streamBox.style.cssText = "white-space:pre-wrap;line-height:1.85";
    el("chContent").innerHTML = "";
    el("chContent").appendChild(streamBox);
    refreshStreamStatus();
    streamTimer = window.setInterval(refreshStreamStatus, 250);
  }

  /* 追加一段增量文本。
   * 参数：text 增量文本。
   * 返回：无。
   */
  function appendStream(text) {
    streamText += String(text || "");
    if (streamBox) streamBox.textContent = streamText;
    refreshStreamStatus();
  }

  /* 结束流式输出（清计时器并恢复按钮）。
   * 参数：无。
   * 返回：无。
   */
  function endStream() {
    if (streamTimer) window.clearInterval(streamTimer);
    streamTimer = null;
    streamStartedAt = 0;
    streamBox = null;
    streamAbort = null;
    const stop = el("chStreamStop");
    if (stop) stop.hidden = true;
    const generateBtn = el("chGenerate");
    const previewBtn = el("chPreview");
    const continueBtn = el("chContinuePreview");
    if (generateBtn) generateBtn.disabled = false;
    if (previewBtn) previewBtn.disabled = false;
    if (continueBtn) continueBtn.disabled = false;
    busy = false;
  }

  /* 取消当前流式请求：中断 fetch，后台会在下一个增量处停止。
   * 参数：无。
   * 返回：无。
   */
  function stopStream() {
    if (streamAbort) streamAbort.abort();
  }

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
      `<div class="kv-row"><span class="kv-key">章节</span><span class="kv-val">${esc(chapter.index)}. ${esc(stripTitleIndex(chapter.title))}</span></div>`,
      `<div class="kv-row"><span class="kv-key">状态</span><span class="kv-val">${esc(NW.statusLabel(chapter.status))}</span></div>`,
      `<div class="kv-row"><span class="kv-key">字数</span><span class="kv-val">${esc(chapter.word_count || 0)}</span></div>`,
      `<div class="kv-row"><span class="kv-key">场景</span><span class="kv-val">${scenes.length}</span></div>`,
      `<div class="kv-row"><span class="kv-key">台词</span><span class="kv-val">${lineTotal} 行</span></div>`,
      `<div class="kv-row"><span class="kv-key">切分</span><span class="kv-val">${scenes.length ? `已切分 ${scenes.length} 个场景 / ${lineTotal} 行（重切会覆盖）` : "未切分"}</span></div>`,
      `<div class="kv-row"><span class="kv-key">可定位</span><span class="kv-val">${lineMap.size} 行</span></div>`,
    ].join("");
    // 未切分时按钮文案保持「切分」，已切分时明确提示这是覆盖操作
    el("chScenes").textContent = scenes.length ? "重新切分场景" : "切分场景";
    el("chLines").textContent = lineTotal ? "重新切分台词" : "切分台词";
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
              `title="${para === "" ? "正文里没找到这一行，无法定位" : `正文第 ${Number(para) + 1} 段`} · 双击编辑台词">` +
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

  /* 切换正文编辑态：编辑时显示「保存 / 取消」，隐藏「编辑正文」。
   * 参数：next 是否进入编辑态。
   * 返回：无。
   */
  function setEditing(next) {
    editing = next;
    el("chEdit").hidden = next;
    el("chSave").hidden = !next;
    el("chCancel").hidden = !next;
  }

  /* 编辑态下拦截会重渲染正文的动作，避免未保存内容被覆盖。
   * 参数：无。
   * 返回：true 表示已拦截（调用方应直接 return）。
   */
  function blockWhileEditing() {
    if (!editing) return false;
    NW.toast("正在编辑正文，请先保存或取消");
    return true;
  }

  /* 渲染整页。参数：chapter 章节详情。返回：无。 */
  function render(chapter) {
    chapterCache = chapter;
    lastSelection = null;
    lineEdit = null;
    setEditing(false);
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
      el("chContent").innerHTML = "<p class='muted'>还没有打开工程：点左上角工程名，打开或新建一个工程。</p>";
      el("chScenesBody").innerHTML = "<p class='muted'>选择章节后，这里会列出场景与台词。</p>";
      return;
    }
    try {
      const data = await fetchChapter();
      if (!data) {
        el("chContent").innerHTML = "<p class='muted'>读到的章节数据不完整，请点顶栏「刷新」重试。</p>";
        return;
      }
      render(data);
      // 预取人物表：行内编辑台词时直接用于「说话人」下拉，不额外等待
      if (!castRows) ensureCast();
      await refreshContinueDraft();
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

  /* 读取人物表（行内编辑「说话人」下拉用），成功后缓存，失败返回空数组。
   * 参数：force 是否强制重新读取。
   * 返回：Promise<Array<{id, name, role}>>。
   */
  async function ensureCast(force) {
    if (castRows && !force) return castRows;
    if (castLoading) return castLoading;
    castLoading = NW.api(`/api/ui/characters?${NW.qs({ path: NW.state.path })}`)
      .then((data) => {
        castRows = (data && data.characters) || [];
        return castRows;
      })
      .catch(() => {
        castRows = [];
        return castRows;
      })
      .finally(() => {
        castLoading = null;
      });
    return castLoading;
  }

  /* 点台词行：在正文里定位并高亮所在段落；点段落则反过来点亮该段的台词行。
   * 参数：event 台词列表的点击事件。
   * 返回：无。
   */
  function onLineClick(event) {
    const chip = event.target.closest(".line-chip");
    if (!chip) return;
    if (editing) return; // 编辑态下正文已被 textarea 顶替，不做定位也不重渲染
    if (lineEdit || chip.classList.contains("is-editing")) return; // 行内编辑时不响应单击定位
    const lineId = chip.dataset.line;
    const entry = lineMap.get(lineId);
    const found = lineIndex.get(lineId) || {};
    const line = found.line || {};
    const scene = found.scene || {};
    const payload = {
      line_id: lineId,
      index: line.index,
      kind: line.kind,
      speaker: speakerLabel(line.speaker, line.speaker_id),
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

  /* 双击台词行：把这一行换成输入框就地编辑。
   * 交互约定：Enter 保存、Esc 取消、失焦保存；空内容拒绝保存并提示。
   * 参数：event 台词列表的双击事件。
   * 返回：无。
   */
  function beginLineEdit(event) {
    if (editing || busy || !NW.state.path || !NW.state.chapter) return;
    const chip = event.target.closest(".line-chip");
    if (!chip || chip.classList.contains("is-editing")) return;
    if (lineEdit) return; // 一次只编辑一行
    const lineId = chip.dataset.line;
    const found = lineIndex.get(lineId);
    if (!found) {
      NW.toast("没有这一行的数据，请刷新后重试");
      return;
    }
    const line = found.line || {};
    const original = line.text || "";
    const para = chip.dataset.para || "";
    const speakerOriginal = String(line.speaker_id || "");

    // 复用行样式：序号/类型保持原位，文本区换成输入框，前面补一个说话人下拉
    const wrap = document.createElement("div");
    wrap.className = "line-chip is-editing";
    wrap.dataset.line = lineId;
    wrap.dataset.para = para;
    wrap.innerHTML = `<span class="lc-kind">${esc(NW.kindLabel(line.kind))}</span>`;

    const speakerSelect = document.createElement("select");
    speakerSelect.className = "line-edit-speaker";
    speakerSelect.setAttribute("aria-label", "说话人");
    speakerSelect.style.cssText = "max-width:104px;font-size:12px;margin-right:6px;";
    const seen = new Set();
    const addSpeaker = (value, text) => {
      if (!value || seen.has(value)) return;
      seen.add(value);
      const option = document.createElement("option");
      option.value = value;
      option.textContent = text || value;
      speakerSelect.appendChild(option);
    };
    // 当前说话人放第一项：列表里没有这个角色时也能保住原值
    if (speakerOriginal) {
      addSpeaker(speakerOriginal, speakerLabel(line.speaker, speakerOriginal) || speakerOriginal);
    }
    addSpeaker("chr_narrator", "旁白");
    (castRows || []).forEach((row) => {
      if (!row || !row.id || row.role === "narrator") return;
      addSpeaker(row.id, row.name || row.id);
    });
    if (speakerOriginal === "chr_unknown") addSpeaker("chr_unknown", "未知角色");
    speakerSelect.value = speakerOriginal || "chr_narrator";
    wrap.appendChild(speakerSelect);

    const input = document.createElement("input");
    input.type = "text";
    input.className = "line-edit-input";
    input.setAttribute("aria-label", "台词");
    input.maxLength = 5000;
    input.value = original;
    wrap.appendChild(input);
    chip.replaceWith(wrap);
    input.focus();
    input.select();

    let finished = false;   // 已保存或已取消
    let submitting = false; // 正在请求保存

    /* 取消编辑：丢弃输入，还原原来的台词行。 */
    function cancel() {
      if (finished || submitting) return;
      finished = true;
      wrap.replaceWith(chip);
      lineEdit = null;
    }

    /* 保存：台词走 `line edit`（同步正文），说话人走 `line set`，成功后重载整页。 */
    async function save() {
      if (finished || submitting) return;
      const value = input.value.trim();
      if (!value) {
        NW.toast("台词内容不能为空");
        input.focus();
        return;
      }
      const nextSpeaker = speakerSelect.value;
      const textChanged = value !== original.trim();
      const speakerChanged = Boolean(nextSpeaker) && nextSpeaker !== speakerOriginal;
      if (!textChanged && !speakerChanged) {
        cancel();
        return;
      }
      submitting = true;
      input.disabled = true;
      speakerSelect.disabled = true;
      try {
        let revision = line.revision;
        if (textChanged) {
          const result = await NW.api("/api/ui/line/edit", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ path: NW.state.path, chapter: NW.state.chapter, line: lineId, text: value }),
          });
          revision = result.revision;
        }
        if (speakerChanged) {
          // 说话人从旁白改成角色（或反过来）时同步台词类型，避免类型与说话人矛盾
          const patch = { speaker: nextSpeaker };
          if (line.kind === "narration" || line.kind === "dialogue") {
            patch.kind = nextSpeaker === "chr_narrator" ? "narration" : "dialogue";
          }
          await NW.api("/api/ui/line/set", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ path: NW.state.path, chapter: NW.state.chapter, line: lineId, ...patch }),
          });
        }
        finished = true;
        lineEdit = null;
        NW.toast(speakerChanged ? `已保存台词与说话人（revision=${revision}）` : `已保存台词（revision=${revision}），正文已同步`, true);
        await load();
      } catch (error) {
        // 失败不静默：保留输入框与用户输入，允许改完重试或按 Esc 放弃
        submitting = false;
        input.disabled = false;
        speakerSelect.disabled = false;
        const reason = String(error.message || error).replace(/^\[[A-Z]+\]\s*/, "");
        NW.toast(`保存失败：${reason}`);
        input.focus();
      }
    }

    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        save();
      } else if (event.key === "Escape") {
        event.preventDefault();
        cancel();
      }
    });
    // 失焦保存，但焦点移到同一行的说话人下拉时不触发（否则点下拉就提前关掉编辑）
    input.addEventListener("blur", () => {
      setTimeout(() => {
        if (!wrap.isConnected) return;
        if (wrap.contains(document.activeElement)) return;
        save();
      }, 0);
    });

    lineEdit = { lineId, wrap, input };
  }

  /* 点正文段落：高亮该段映射到的台词行，便于从正文找回台词。
   * 参数：event 正文点击事件。
   * 返回：无。
   */
  function onParaClick(event) {
    if (editing) return; // 编辑态不触发段落联动
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

  /* 生成正文（regen 由勾选框决定）。
   * 流式请求：正文区实时追加，状态显示「已生成 N 字 / 已用 X 秒」。
   * 勾选「覆盖已有正文」且本章已有正文时，先二次确认，避免一次误点覆盖全文。 */
  async function generate() {
    if (busy || !NW.state.path || !NW.state.chapter) return;
    if (blockWhileEditing()) return;
    const regen = el("chRegen").checked;
    const currentWords = Number((chapterCache && chapterCache.word_count) || 0);
    if (regen && currentWords > 0) {
      const ok = window.confirm(
        `将覆盖本章现有正文（${currentWords} 字）重新生成，原正文不可恢复。\n` +
          "生成后场景与台词需要重新切分。确定继续？"
      );
      if (!ok) return;
    }
    busy = true;
    el("chGenerate").disabled = true;
    el("chPreview").disabled = true;
    el("chContinuePreview").disabled = true;
    el("chStreamStop").hidden = false;
    streamAbort = new AbortController();
    beginStream();
    try {
      const result = await streamPost(
        "/api/ui/chapter/generate/stream",
        { path: NW.state.path, chapter: NW.state.chapter, regen },
        (event, payload) => {
          if (event === "delta") appendStream(payload.text);
        },
        streamAbort.signal
      );
      if (!result) throw new Error("生成中断：未收到完成事件");
      refreshStreamStatus();
      // 多轮生成会带回 rounds：优先展示轮次与字数，旧版无 rounds 时回落到原提示
      if (result && result.rounds) {
        NW.toast(`生成完成（${result.rounds} 轮，${result.words} 字）${regen ? "，请重新切分场景与台词" : ""}`, true);
      } else {
        NW.toast(`正文已生成：${(result && result.words) || streamWordCount(streamText)} 字${regen ? "，请重新切分场景与台词" : ""}`, true);
      }
      const dialogueWarnings = (result && result.dialogue_warnings) || [];
      if (dialogueWarnings.length) {
        NW.toast(`有 ${dialogueWarnings.length} 行对白可能未加引号，请人工确认：${dialogueWarnings[0]}`);
      }
      await load();
      if (NW.openProject) await NW.openProject();
    } catch (error) {
      if (streamAbort && streamAbort.signal.aborted) {
        NW.toast("已停止生成");
      } else {
        NW.toast(`生成失败：${error.message}`);
      }
      await load();
    } finally {
      endStream();
    }
  }

  /* 只预览不写盘：流式输出，结束后按同样的段落规则渲染预览（不落盘正文）。 */
  async function preview() {
    if (busy || !NW.state.path || !NW.state.chapter) return;
    if (blockWhileEditing()) return;
    busy = true;
    el("chGenerate").disabled = true;
    el("chPreview").disabled = true;
    el("chContinuePreview").disabled = true;
    el("chStreamStop").hidden = false;
    streamAbort = new AbortController();
    beginStream();
    try {
      const result = await streamPost(
        "/api/ui/chapter/generate/stream",
        { path: NW.state.path, chapter: NW.state.chapter, regen: true, dry_run: true },
        (event, payload) => {
          if (event === "delta") appendStream(payload.text);
        },
        streamAbort.signal
      );
      if (!result) throw new Error("生成中断：未收到完成事件");
      const text = String((result && result.content) || streamText || "");
      blocks = paragraphsOf(text);
      lineMap = new Map();
      renderDoc(blocks, new Map());
      el("chInfo").innerHTML = `<div class="kv-row"><span class="kv-key">预览</span><span class="kv-val">未写盘（${blocks.length} 段）</span></div>`;
      NW.toast("预览已生成，未写盘", true);
    } catch (error) {
      if (streamAbort && streamAbort.signal.aborted) {
        NW.toast("已停止生成");
      } else {
        NW.toast(`预览失败：${error.message}`);
      }
      await load();
    } finally {
      endStream();
    }
  }

  /* 进入正文编辑态：用 textarea 顶替只读段落，内容取当前章节正文。
   * 参数：无。
   * 返回：无。
   */
  function startEdit() {
    if (busy || editing || !chapterCache) return;
    el("chContent").innerHTML = '<textarea id="chEditArea" class="doc-edit" spellcheck="false"></textarea>';
    const area = el("chEditArea");
    // 落盘正文末尾统一带一个换行，编辑时去掉它，避免 textarea 里多出一个空行
    area.value = String(chapterCache.content || "").replace(/\n$/, "");
    setEditing(true);
    area.focus();
  }

  /* 取消编辑：丢弃未保存内容，恢复只读段落渲染。
   * 参数：无。
   * 返回：无。
   */
  function cancelEdit() {
    if (!editing) return;
    setEditing(false);
    renderDoc(blocks, new Map());
  }

  /* 保存正文：调用 /api/ui/chapter/save，成功后刷新章节详情与目录。
   * 参数：无。
   * 返回：Promise<void>。
   */
  async function saveEdit() {
    if (busy || !editing || !NW.state.path || !NW.state.chapter) return;
    const area = el("chEditArea");
    if (!area) return;
    const content = area.value;
    if (!content.trim()) {
      NW.toast("正文不能为空");
      return;
    }
    busy = true;
    el("chSave").disabled = true;
    try {
      const result = await NW.api("/api/ui/chapter/save", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: NW.state.path, chapter: NW.state.chapter, content }),
      });
      const warnings = result.warnings || [];
      const note = warnings.length ? `；${warnings.join("；")}` : "";
      setEditing(false);
      NW.toast(`已保存：${result.word_count} 字（revision=${result.revision}）${note}`, true);
      await load();
      if (NW.openProject) await NW.openProject();
    } catch (error) {
      NW.toast(`保存失败：${error.message}`);
    } finally {
      busy = false;
      el("chSave").disabled = false;
    }
  }

  /* 切分场景或台词：先出影响清单，确认后再执行（默认保留人工修改）。 */
  async function split(which) {
    if (busy || !NW.state.path || !NW.state.chapter) return;
    if (blockWhileEditing()) return;
    busy = true;
    try {
      await NWResplit.run(which, {
        keep: el("chKeepEdits").checked,
        onDone: async () => {
          await load();
          if (NW.openProject) await NW.openProject();
        },
      });
    } catch (error) {
      NW.toast(`切分失败（退出码 ${error.code}）：${error.message}`);
    } finally {
      busy = false;
    }
  }

  /* 取某个 DOM 节点在正文全文里的字符偏移。
   * 参数：node 选区端点节点；offset 节点内偏移。
   * 返回：相对正文的绝对偏移；节点不在正文段落内时返回 null。
   */
  function offsetInContent(node, offset) {
    if (!node) return null;
    const elem = node.nodeType === 3 ? node.parentElement : node;
    const para = elem && elem.closest ? elem.closest(".doc-p") : null;
    if (!para) return null;
    const block = blocks[Number(para.dataset.para)];
    if (!block) return null;
    if (node.nodeType !== 3) {
      // 元素节点（整段选择）按子节点文本长度折算偏移
      let inner = 0;
      Array.from(node.childNodes).slice(0, offset).forEach((child) => {
        inner += child.textContent.length;
      });
      return block.start + inner;
    }
    let inner = 0;
    const walker = document.createTreeWalker(para, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) {
      if (walker.currentNode === node) return block.start + inner + offset;
      inner += walker.currentNode.textContent.length;
    }
    return block.start + block.text.length;
  }

  /* 读取当前正文中用户选中的字符范围。
   * 参数：无。
   * 返回：{start, end} 或 null（未选中 / 选区不在正文里）。
   */
  function currentSelection() {
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed || !sel.rangeCount) return null;
    const range = sel.getRangeAt(0);
    const box = el("chContent");
    if (!box || !box.contains(range.startContainer) || !box.contains(range.endContainer)) return null;
    const start = offsetInContent(range.startContainer, range.startOffset);
    const end = offsetInContent(range.endContainer, range.endOffset);
    if (start === null || end === null || start >= end) return null;
    return { start, end };
  }

  /* 记住最近一次选区：点工具条按钮会清掉浏览器选区，用这里缓存的副本。
   * 参数：无。
   * 返回：无。
   */
  function captureSelection() {
    if (editing) return;
    lastSelection = currentSelection();
  }

  /* 把字符范围翻译成人能读的段号。
   * 参数：range 字符范围 {start, end}。
   * 返回：如「第 3 段」「第 3–5 段」。
   */
  function describeSelection(range) {
    const first = blocks.findIndex((block) => block.start <= range.start && range.start < block.end);
    const last = blocks.findIndex((block) => block.start < range.end && range.end <= block.end);
    if (first < 0 || last < 0) return `字符 ${range.start}–${range.end}`;
    return first === last ? `第 ${first + 1} 段` : `第 ${first + 1}–${last + 1} 段`;
  }

  /* 关闭 AI 编辑弹窗。参数：无。返回：无。 */
  function closeAiEdit() {
    if (!aiMask) return;
    document.removeEventListener("keydown", onAiKeydown, true);
    aiMask.remove();
    aiMask = null;
  }

  /* Esc 关闭 AI 编辑弹窗。参数：event 键盘事件。返回：无。 */
  function onAiKeydown(event) {
    if (event.key === "Escape") closeAiEdit();
  }

  /* 构造 AI 编辑请求体。参数：state 弹窗状态；dryRun 是否只预览。返回：body 对象。 */
  function aiBody(state, dryRun) {
    const body = {
      path: NW.state.path,
      chapter: NW.state.chapter,
      instruction: state.input.value.trim(),
      dry_run: dryRun,
    };
    if (state.range) body.range = { char_start: state.range.start, char_end: state.range.end };
    return body;
  }

  /* 在弹窗内渲染预览：整章显示全文，选区显示替换片段及前后各一段。
   * 参数：state 弹窗状态；result `/api/ui/chapter/edit` 的返回。
   * 返回：无。
   */
  function renderAiPreview(state, result) {
    const box = state.preview;
    box.hidden = false;
    if (!state.range) {
      box.innerHTML =
        `<p class="ai-seg-title">整章改写结果（${Number(result.word_count) || 0} 字）</p>` +
        `<pre class="ai-pre">${esc(result.after)}</pre>`;
      return;
    }
    const tail = result.before.length - state.range.end;
    const generated = result.after.slice(state.range.start, result.after.length - tail);
    const list = paragraphsOf(result.before);
    const prev = [...list].reverse().find((item) => item.end <= state.range.start);
    const next = list.find((item) => item.start >= state.range.end);
    const parts = [];
    if (prev) parts.push(`<p class="ai-seg-title">前一段（未改动）</p><p class="ai-seg-text">${esc(prev.text)}</p>`);
    parts.push(`<p class="ai-seg-title">改写后（将替换选中片段）</p><pre class="ai-pre">${esc(generated)}</pre>`);
    if (next) parts.push(`<p class="ai-seg-title">后一段（未改动）</p><p class="ai-seg-text">${esc(next.text)}</p>`);
    box.innerHTML = parts.join("");
  }

  /* 先请求 dry_run 预览，不写盘。参数：state 弹窗状态。返回：Promise<void>。 */
  async function requestAiPreview(state) {
    if (!state.input.value.trim()) {
      NW.toast("提示词不能为空");
      return;
    }
    state.previewBtn.disabled = true;
    state.apply.hidden = true;
    state.status.textContent = "正在请求模型…";
    try {
      const result = await NW.api("/api/ui/chapter/edit", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(aiBody(state, true)),
        busy: "正在按提示改写正文…",
        busyDetail: "长章节会分块改写，请稍候",
      });
      state.result = result;
      renderAiPreview(state, result);
      state.apply.hidden = !result.changed;
      state.status.textContent = result.changed
        ? `预览完成：${result.word_count} 字，尚未写盘`
        : "改写结果与原文一致，无需应用";
    } catch (error) {
      state.result = null;
      state.status.textContent = `预览失败：${error.message}`;
    } finally {
      state.previewBtn.disabled = false;
    }
  }

  /* 应用改写：再次请求并落盘，成功后刷新章节与目录。
   * 参数：state 弹窗状态。
   * 返回：Promise<void>。
   */
  async function applyAiEdit(state) {
    if (!state.result) return;
    if (!state.input.value.trim()) {
      NW.toast("提示词不能为空");
      return;
    }
    state.apply.disabled = true;
    state.status.textContent = "正在应用…";
    try {
      const result = await NW.api("/api/ui/chapter/edit", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(aiBody(state, false)),
        busy: "正在应用改写…",
        busyDetail: "会重新调用模型改写，长章节需要较长时间",
      });
      const warnings = result.warnings || [];
      const note = warnings.length ? `；${warnings.join("；")}` : "";
      closeAiEdit();
      NW.toast(`已应用 AI 改写：${result.word_count} 字（revision=${result.revision}）${note}`, true);
      await load();
      if (NW.openProject) await NW.openProject();
    } catch (error) {
      state.status.textContent = `应用失败：${error.message}`;
      state.apply.disabled = false;
    }
  }

  /* 打开「AI 编辑」弹窗：提示词 + 可选选区 + 预览 + 应用。
   * 参数：无。
   * 返回：无。
   */
  function openAiEdit() {
    if (busy || !NW.state.path || !NW.state.chapter) return;
    if (blockWhileEditing()) return;
    if (!String((chapterCache && chapterCache.content) || "").trim()) {
      NW.toast("正文为空，无法编辑");
      return;
    }
    const range = currentSelection() || lastSelection;
    closeAiEdit();

    const mask = document.createElement("div");
    mask.className = "modal-mask";
    const dialog = document.createElement("div");
    dialog.className = "modal-editor";
    dialog.setAttribute("role", "dialog");
    dialog.setAttribute("aria-modal", "true");

    const title = document.createElement("h3");
    title.className = "modal-title";
    title.textContent = range ? `AI 编辑 · 将改写${describeSelection(range)}` : "AI 编辑 · 整章";
    dialog.appendChild(title);

    const hint = document.createElement("p");
    hint.className = "modal-hint";
    hint.textContent = range
      ? `已带上选中范围（字符 ${range.start}–${range.end}），选区外正文保持不变。`
      : "未选中正文，将改写整章；先预览，确认后再应用。";
    dialog.appendChild(hint);

    const input = document.createElement("textarea");
    input.className = "ai-edit-input";
    input.placeholder = "输入改写要求，例如：把对峙写得更克制，删掉解释性旁白";
    dialog.appendChild(input);

    const preview = document.createElement("div");
    preview.className = "ai-edit-preview";
    preview.hidden = true;
    dialog.appendChild(preview);

    const status = document.createElement("p");
    status.className = "modal-hint";
    dialog.appendChild(status);

    const foot = document.createElement("div");
    foot.className = "modal-foot";
    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "btn";
    cancel.textContent = "取消";
    cancel.addEventListener("click", closeAiEdit);
    foot.appendChild(cancel);

    const previewBtn = document.createElement("button");
    previewBtn.type = "button";
    previewBtn.className = "btn";
    previewBtn.textContent = "预览";
    foot.appendChild(previewBtn);

    const apply = document.createElement("button");
    apply.type = "button";
    apply.className = "btn primary";
    apply.textContent = "应用";
    apply.hidden = true;
    foot.appendChild(apply);
    dialog.appendChild(foot);

    const state = { input, preview, status, previewBtn, apply, range, result: null };
    previewBtn.addEventListener("click", () => requestAiPreview(state));
    apply.addEventListener("click", () => applyAiEdit(state));
    input.addEventListener("input", () => {
      state.result = null;
      apply.hidden = true;
      status.textContent = "提示词已修改，请重新预览";
    });

    mask.appendChild(dialog);
    mask.addEventListener("click", (event) => {
      if (event.target === mask) closeAiEdit();
    });
    document.body.appendChild(mask);
    document.addEventListener("keydown", onAiKeydown, true);
    aiMask = mask;
    input.focus();
  }

  /* 关闭「按正文重总结」弹窗。参数：无。返回：无。 */
  function closeSummarize() {
    if (!summarizeMask) return;
    document.removeEventListener("keydown", onSummarizeKeydown, true);
    summarizeMask.remove();
    summarizeMask = null;
  }

  /* Esc 关闭重总结弹窗。参数：event 键盘事件。返回：无。 */
  function onSummarizeKeydown(event) {
    if (event.key === "Escape") closeSummarize();
  }

  /* 构造重总结请求体。参数：apply 是否写盘。返回：body 对象。 */
  function summarizeBody(apply) {
    return { path: NW.state.path, chapter: NW.state.chapter, apply: Boolean(apply) };
  }

  /* 在弹窗内渲染总结预览：标题新旧对照、摘要、大纲与节拍。
   * 参数：state 弹窗状态；result `/api/ui/chapter/summarize` 的返回。
   * 返回：无。
   */
  function renderSummarizePreview(state, result) {
    const preview = result.preview || {};
    const beats = preview.beats || [];
    const box = state.box;
    box.hidden = false;
    const beatText = beats
      .map((beat, index) => `${index + 1}. ${beat.turning_point ? "★ " : ""}${beat.summary || ""}`)
      .join("\n");
    box.innerHTML =
      `<p class="ai-seg-title">标题（当前 → 新）</p>` +
      `<p class="ai-seg-text">${esc(state.oldTitle || "（空）")} → ${esc(preview.title || "（空）")}</p>` +
      `<p class="ai-seg-title">摘要</p>` +
      `<p class="ai-seg-text">${esc(preview.synopsis || "（空）")}</p>` +
      `<p class="ai-seg-title">大纲</p>` +
      `<pre class="ai-pre">${esc(preview.outline || "（空）")}</pre>` +
      `<p class="ai-seg-title">节拍（${beats.length} 条，替换旧节拍 ${Number(result.replaced_beats) || 0} 条）</p>` +
      `<pre class="ai-pre">${esc(beatText || "（空）")}</pre>`;
  }

  /* 请求一次重总结预览，不写盘。参数：state 弹窗状态。返回：Promise<void>。 */
  async function requestSummarizePreview(state) {
    state.previewBtn.disabled = true;
    state.apply.hidden = true;
    state.status.textContent = "正在总结…";
    try {
      const result = await NW.api("/api/ui/chapter/summarize", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(summarizeBody(false)),
        busy: "正在根据正文重总结…",
        busyDetail: "长章节会分块提炼事件要点",
      });
      state.result = result;
      renderSummarizePreview(state, result);
      const changes = result.changes || [];
      state.apply.hidden = !changes.length;
      state.status.textContent = changes.length
        ? `预览完成：将变更 ${changes.join("、")}；尚未写盘`
        : "总结结果与当前内容一致，无需应用";
    } catch (error) {
      state.result = null;
      state.status.textContent = `总结失败：${error.message}`;
    } finally {
      state.previewBtn.disabled = false;
    }
  }

  /* 确认应用重总结：后端复用刚生成的预览直接写盘（不再跑模型），成功后刷新章节、目录与大纲页。
   * 参数：state 弹窗状态。
   * 返回：Promise<void>。
   */
  async function applySummarize(state) {
    if (!state.result) return;
    state.apply.disabled = true;
    state.status.textContent = "正在应用…";
    try {
      const result = await NW.api("/api/ui/chapter/summarize", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(summarizeBody(true)),
      });
      const warnings = result.warnings || [];
      const note = warnings.length ? `；${warnings.join("；")}` : "";
      closeSummarize();
      const text = result.applied
        ? `已按正文重总结：${result.old_title} → ${result.new_title}${note}`
        : `总结结果与当前内容一致，未写盘${note}`;
      NW.toast(text, true);
      await load();
      if (NW.openProject) await NW.openProject();
    } catch (error) {
      state.status.textContent = `应用失败：${error.message}`;
      state.apply.disabled = false;
    }
  }

  /* 打开「按正文重总结」弹窗：先请求预览，确认后再应用。
   * 参数：无。
   * 返回：无。
   */
  function openSummarize() {
    if (busy || !NW.state.path || !NW.state.chapter) return;
    if (blockWhileEditing()) return;
    if (!String((chapterCache && chapterCache.content) || "").trim()) {
      NW.toast("正文为空，无法总结");
      return;
    }
    closeSummarize();

    const mask = document.createElement("div");
    mask.className = "modal-mask";
    const dialog = document.createElement("div");
    dialog.className = "modal-editor";
    dialog.setAttribute("role", "dialog");
    dialog.setAttribute("aria-modal", "true");

    const title = document.createElement("h3");
    title.className = "modal-title";
    title.textContent = "按正文重总结 · 预览";
    dialog.appendChild(title);

    const hint = document.createElement("p");
    hint.className = "modal-hint";
    hint.textContent = "根据本章正文重写标题、摘要、大纲与节拍；确认后才会写回章节与设定库。";
    dialog.appendChild(hint);

    const box = document.createElement("div");
    box.className = "ai-edit-preview";
    box.hidden = true;
    dialog.appendChild(box);

    const status = document.createElement("p");
    status.className = "modal-hint";
    dialog.appendChild(status);

    const foot = document.createElement("div");
    foot.className = "modal-foot";
    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "btn";
    cancel.textContent = "取消";
    cancel.addEventListener("click", closeSummarize);
    foot.appendChild(cancel);

    const previewBtn = document.createElement("button");
    previewBtn.type = "button";
    previewBtn.className = "btn";
    previewBtn.textContent = "重新预览";
    foot.appendChild(previewBtn);

    const apply = document.createElement("button");
    apply.type = "button";
    apply.className = "btn primary";
    apply.textContent = "确认应用";
    apply.hidden = true;
    foot.appendChild(apply);
    dialog.appendChild(foot);

    const state = { box, status, previewBtn, apply, result: null, oldTitle: (chapterCache && chapterCache.title) || "" };
    previewBtn.addEventListener("click", () => requestSummarizePreview(state));
    apply.addEventListener("click", () => applySummarize(state));

    mask.appendChild(dialog);
    mask.addEventListener("click", (event) => {
      if (event.target === mask) closeSummarize();
    });
    document.body.appendChild(mask);
    document.addEventListener("keydown", onSummarizeKeydown, true);
    summarizeMask = mask;
    requestSummarizePreview(state);
  }

  /* ---------------- 续写正文 ---------------- */

  let continueDraft = null; // 最近一次草稿（预览结果或页面加载时查到的未应用草稿）
  let continuePanel = null; // 续写参数面板浮层
  let continueWords = 800;  // 上次选择的篇幅（面板默认值）
  let lastContinueOptions = null; // 上次续写入参，重抽时复用

  /* 查询本章是否留有未应用的续写草稿（刷新页面后仍可一键应用）。
   * 参数：无。
   * 返回：Promise<void>。
   */
  async function refreshContinueDraft() {
    const applyBtn = el("chContinueApply");
    if (!applyBtn) return;
    continueDraft = null;
    applyBtn.hidden = true;
    if (!NW.state.path || !NW.state.chapter) return;
    try {
      const data = await NW.api(
        `/api/ui/chapter/continue/draft?${NW.qs({ path: NW.state.path, chapter: NW.state.chapter })}`
      );
      if (data && data.exists && data.draft) {
        continueDraft = data.draft;
        const chars = String(data.draft.text || "").replace(/\s/g, "").length;
        applyBtn.hidden = false;
        applyBtn.textContent = `应用续写草稿（+${chars} 字）`;
      }
    } catch (error) {
      // 草稿查询失败不影响正文展示
    }
  }

  /* 关闭续写参数面板（不发起请求）。 */
  function closeContinuePanel() {
    if (!continuePanel) return;
    document.removeEventListener("keydown", onContinuePanelKeydown, true);
    continuePanel.remove();
    continuePanel = null;
  }

  /* 续写参数面板键盘：Esc 关闭（不发起预览）。 */
  function onContinuePanelKeydown(event) {
    if (event.key !== "Escape") return;
    event.preventDefault();
    closeContinuePanel();
  }

  /* 打开「续写正文」面板：篇幅 + 可选续写起点 + 多行需求，确认后才预览。
   * 与「续写大纲」面板共用 modal-mask / modal-editor 结构与按钮语义。
   * 参数：无。
   * 返回：无。
   */
  function openContinuePanel() {
    if (busy || !NW.state.path || !NW.state.chapter) {
      NW.toast("先选择要续写的章节");
      return;
    }
    if (blockWhileEditing()) return;
    closeContinuePanel();
    continuePanel = document.createElement("div");
    continuePanel.className = "modal-mask";

    const dialog = document.createElement("div");
    dialog.className = "modal-editor continue-panel";
    dialog.setAttribute("role", "dialog");
    dialog.setAttribute("aria-modal", "true");

    const title = document.createElement("h3");
    title.className = "modal-title";
    title.textContent = "续写正文";
    dialog.appendChild(title);

    const hint = document.createElement("p");
    hint.className = "modal-hint";
    hint.textContent = "先预览，确认后再写入正文；新内容只会追加在正文末尾。";
    dialog.appendChild(hint);

    const wordsLabel = document.createElement("label");
    wordsLabel.className = "stack";
    wordsLabel.textContent = "篇幅";
    const wordsSelect = document.createElement("select");
    [
      ["500", "短 500 字"],
      ["800", "中 800 字"],
      ["1200", "长 1200 字"],
    ].forEach(([value, text]) => {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = text;
      if (String(continueWords) === value) option.selected = true;
      wordsSelect.appendChild(option);
    });
    wordsLabel.appendChild(wordsSelect);
    const wordsHint = document.createElement("span");
    wordsHint.className = "muted";
    wordsHint.style.fontSize = "12px";
    wordsHint.textContent = "本次目标总字数；达到即停，内容不足时分轮补足，可能略多";
    wordsLabel.appendChild(wordsHint);
    dialog.appendChild(wordsLabel);

    const beatLabel = document.createElement("label");
    beatLabel.className = "stack";
    beatLabel.textContent = "续写起点";
    const beatSelect = document.createElement("select");
    const autoOption = document.createElement("option");
    autoOption.value = "";
    autoOption.textContent = "自动（从正文末尾继续）";
    beatSelect.appendChild(autoOption);
    beatLabel.appendChild(beatSelect);
    const beatHint = document.createElement("span");
    beatHint.className = "muted";
    beatHint.style.fontSize = "12px";
    beatHint.textContent = "选具体节拍时从该拍开始写；自动按已写字数推断";
    beatLabel.appendChild(beatHint);
    dialog.appendChild(beatLabel);

    const intentLabel = document.createElement("label");
    intentLabel.className = "stack";
    intentLabel.textContent = "续写需求";
    const intentInput = document.createElement("textarea");
    intentInput.rows = 4;
    intentInput.placeholder = "例如：让主角在雨里追上师父，第一次听到旧案的真相";
    intentInput.style.cssText = "width:100%;box-sizing:border-box;min-height:96px;resize:vertical";
    intentLabel.appendChild(intentInput);
    dialog.appendChild(intentLabel);

    const intentHint = document.createElement("p");
    intentHint.className = "muted";
    intentHint.style.cssText = "margin:0;font-size:12px";
    intentHint.textContent = "留空则按当前正文自然续写（Ctrl/Cmd + Enter 可预览）";
    dialog.appendChild(intentHint);

    const foot = document.createElement("div");
    foot.className = "modal-foot";
    foot.style.cssText = "display:flex;gap:8px;justify-content:flex-end;flex-wrap:wrap";
    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "btn";
    cancel.textContent = "取消";
    cancel.addEventListener("click", closeContinuePanel);
    foot.appendChild(cancel);
    if (continueDraft) {
      const chars = String(continueDraft.text || "").replace(/\s/g, "").length;
      const applyDraft = document.createElement("button");
      applyDraft.type = "button";
      applyDraft.className = "btn";
      applyDraft.textContent = `应用续写草稿（+${chars} 字）`;
      applyDraft.addEventListener("click", async () => {
        closeContinuePanel();
        await applyContinueDraft();
      });
      foot.appendChild(applyDraft);
    }
    const preview = document.createElement("button");
    preview.type = "button";
    preview.className = "btn primary";
    preview.textContent = "生成预览";
    preview.addEventListener("click", () => submitContinuePanel(wordsSelect, beatSelect, intentInput, preview));
    foot.appendChild(preview);
    dialog.appendChild(foot);

    /* Ctrl / Cmd + Enter 直接生成预览 */
    dialog.addEventListener("keydown", (event) => {
      if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
        event.preventDefault();
        submitContinuePanel(wordsSelect, beatSelect, intentInput, preview);
      }
    });

    continuePanel.appendChild(dialog);
    continuePanel.addEventListener("click", (event) => {
      if (event.target === continuePanel) closeContinuePanel();
    });
    document.body.appendChild(continuePanel);
    document.addEventListener("keydown", onContinuePanelKeydown, true);
    intentInput.focus();

    /* 异步补「续写起点」：从大纲里取本章节拍，失败不影响手动续写 */
    NW.api(`/api/ui/outline?${NW.qs({ path: NW.state.path })}`)
      .then((data) => {
        if (!continuePanel) return;
        let beats = [];
        ((data && data.acts) || []).forEach((act) => {
          (act.chapters || []).forEach((chapter) => {
            if (chapter.id === NW.state.chapter) beats = chapter.beats || [];
          });
        });
        beats.forEach((beat, index) => {
          const option = document.createElement("option");
          option.value = String(index + 1);
          option.textContent = `第 ${index + 1} 拍：${String(beat.summary || "").slice(0, 24)}`;
          beatSelect.appendChild(option);
        });
      })
      .catch(() => {});
  }

  /* 读取面板参数并关闭面板，随后发起续写预览。
   * 参数：wordsSelect 篇幅；beatSelect 续写起点；intentInput 需求；button 预览按钮。
   * 返回：无。
   */
  function submitContinuePanel(wordsSelect, beatSelect, intentInput, button) {
    const words = Number((wordsSelect && wordsSelect.value) || 800);
    const intent = String((intentInput && intentInput.value) || "").trim();
    const fromBeat = Number((beatSelect && beatSelect.value) || 0) || null;
    continueWords = words;
    closeContinuePanel();
    if (button) button.disabled = false;
    requestContinue(false, { words, intent, fromBeat });
  }

  /* 请求续写预览（结果先落成未应用草稿，不写正文）。
   * 流式请求：正文区实时追加新增段落，状态显示「已生成 N 字 / 已用 X 秒」。
   * 参数：retry 是否重抽（忽略已有草稿重新生成）；options `{words, intent, fromBeat}`。
   * 返回：Promise<void>。
   */
  async function requestContinue(retry, options) {
    if (busy || !NW.state.path || !NW.state.chapter) return;
    if (blockWhileEditing()) return;
    const opts = options || lastContinueOptions || {};
    const words = Number(opts.words || continueWords || 800);
    const intent = String(opts.intent || "").trim();
    const fromBeat = Number(opts.fromBeat || 0) || null;
    lastContinueOptions = { words, intent, fromBeat };
    busy = true;
    el("chContinuePreview").disabled = true;
    el("chGenerate").disabled = true;
    el("chPreview").disabled = true;
    el("chStreamStop").hidden = false;
    streamAbort = new AbortController();
    beginStream();
    try {
      const result = await streamPost(
        "/api/ui/chapter/continue/stream",
        {
          path: NW.state.path,
          chapter: NW.state.chapter,
          words,
          intent,
          from_beat: fromBeat,
          retry: Boolean(retry),
        },
        (event, payload) => {
          if (event === "delta") appendStream(payload.text);
        },
        streamAbort.signal
      );
      if (!result) throw new Error("生成中断：未收到完成事件");
      continueDraft = result;
      await load();
      showContinueDialog(result, words);
      await refreshContinueDraft();
    } catch (error) {
      if (streamAbort && streamAbort.signal.aborted) {
        NW.toast("已停止续写");
      } else {
        NW.toast(`续写失败：${error.message}`);
      }
      await load();
    } finally {
      endStream();
    }
  }

  /* 续写确认弹窗：显示新增字数/位置与预览文本，可取消、重抽、应用。
   * 参数：result 续写结果；words 本次字数档位。
   * 返回：无。
   */
  function showContinueDialog(result, words) {
    const mask = document.createElement("div");
    mask.style.cssText =
      "position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:60;display:flex;align-items:center;justify-content:center";
    const box = document.createElement("div");
    box.style.cssText =
      "background:#fff;color:#222;border-radius:10px;max-width:760px;width:92%;max-height:82vh;display:flex;flex-direction:column;padding:16px 18px;gap:10px;box-shadow:0 12px 40px rgba(0,0,0,.25)";
    const covered = result.covered_beats || [];
    const beat = covered.length
      ? `，从第 ${result.from_beat} 个节拍继续（覆盖第 ${covered[0]}~${covered[covered.length - 1]} 拍）`
      : "";
    const head = document.createElement("div");
    const intentLine = result.intent
      ? `<div style="margin-top:6px;font-size:13px;line-height:1.7">续写需求：<b>${esc(result.intent)}</b></div>`
      : `<div style="margin-top:6px;font-size:13px;line-height:1.7">未填写续写需求，按当前正文自然续写</div>`;
    head.innerHTML =
      "<b>续写预览（尚未写入正文）</b>" +
      intentLine +
      `<div style="margin-top:6px;font-size:13px;line-height:1.7">本次将追加 <b>${result.added_words}</b> 字：` +
      `${result.before_words} 字 → ${result.after_words} 字${beat}<br/>` +
      `追加位置：正文末尾（第 ${result.append_start} 字之后）· 字数档位 ${words} 字</div>`;
    const pre = document.createElement("pre");
    pre.textContent = result.preview || "（模型没有返回可用新内容，可重抽或补充意图）";
    pre.style.cssText =
      "white-space:pre-wrap;font-size:13px;line-height:1.75;max-height:46vh;overflow:auto;background:#f6f7f9;border-radius:8px;padding:10px;margin:0";
    const actions = document.createElement("div");
    actions.style.cssText = "display:flex;gap:8px;justify-content:flex-end";
    const mk = (text, primary, onClick) => {
      const button = document.createElement("button");
      button.className = primary ? "btn primary" : "btn";
      button.textContent = text;
      button.addEventListener("click", onClick);
      return button;
    };
    actions.appendChild(mk("取消", false, () => mask.remove()));
    actions.appendChild(
      mk("重抽", false, () => {
        mask.remove();
        requestContinue(true, lastContinueOptions);
      })
    );
    actions.appendChild(mk("应用并写入正文", true, () => applyContinue(mask)));
    box.append(head, pre, actions);
    mask.appendChild(box);
    mask.addEventListener("click", (event) => {
      if (event.target === mask) mask.remove();
    });
    document.body.appendChild(mask);
  }

  /* 应用续写草稿：后端把草稿追加进正文并清理草稿文件。
   * 参数：mask 当前弹窗（可为空，从工具条直接应用时传 null）。
   * 返回：Promise<void>。
   */
  async function applyContinue(mask) {
    if (!NW.state.path || !NW.state.chapter) return;
    const button = mask ? mask.querySelector("button.primary") : el("chContinueApply");
    if (button) button.disabled = true;
    try {
      const result = await NW.api("/api/ui/chapter/continue", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: NW.state.path, chapter: NW.state.chapter, apply: true }),
        busy: "正在写入正文…",
      });
      if (mask) mask.remove();
      NW.toast(`已续写 ${result.added_words} 字（${result.before_words} → ${result.after_words} 字）`, true);
      await load();
      if (NW.openProject) await NW.openProject();
    } catch (error) {
      NW.toast(`应用续写失败（${error.code || "-"}）：${error.message}`);
      if (button) button.disabled = false;
    }
  }

  /* 工具条上的「应用续写草稿」：直接消费页面加载时查到的草稿。
   * 参数：无。
   * 返回：Promise<void>。
   */
  async function applyContinueDraft() {
    if (busy || !continueDraft) return;
    if (blockWhileEditing()) return;
    busy = true;
    try {
      await applyContinue(null);
    } finally {
      busy = false;
    }
  }

  /* 首次激活绑定事件。 */
  function bind() {
    if (bound) return;
    bound = true;
    el("chContinuePreview").addEventListener("click", openContinuePanel);
    el("chStreamStop").addEventListener("click", stopStream);
    el("chContinueApply").addEventListener("click", applyContinueDraft);
    el("chGenerate").addEventListener("click", generate);
    el("chEdit").addEventListener("click", startEdit);
    el("chAiEdit").addEventListener("click", openAiEdit);
    el("chSummarize").addEventListener("click", openSummarize);
    el("chSave").addEventListener("click", saveEdit);
    el("chCancel").addEventListener("click", cancelEdit);
    el("chPreview").addEventListener("click", preview);
    el("chScenes").addEventListener("click", () => split("scene"));
    el("chLines").addEventListener("click", () => split("line"));
    el("chScenesBody").addEventListener("click", onLineClick);
    el("chScenesBody").addEventListener("dblclick", beginLineEdit);
    el("chContent").addEventListener("click", onParaClick);
    el("chContent").addEventListener("mouseup", captureSelection);
    el("chContent").addEventListener("keyup", captureSelection);
    NW.onChapter(() => load());
  }

  window.Pages.chapter = {
    activate() {
      bind();
      load();
    },
  };
})();
