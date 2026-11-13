/* 音色库：内置音色下载/导入、录音/上传、试听、角色绑定。
 *
 * 挂在「人物设计」页的「音色库」子标签下；数据全部走 /api/ui/voices*。
 * 录音用浏览器 MediaRecorder，上传前统一转 base64，由后端 ffmpeg 转成 24k 单声道 wav。
 */
(function () {
  const el = (id) => document.getElementById(id);

  const state = {
    voices: [],
    characters: [],
    builtin: { root: "", items: [] },
    recording: false,
    recorder: null,
    chunks: [],
    stream: null,
    blob: null,
    blobName: "",
    recorded: false,
    denoiseInit: false,
    timer: null,
    startedAt: 0,
    bound: false,
  };

  /* 当前工程路径；未打开工程时返回空串。 */
  function projectPath() {
    return (window.NW && NW.state && NW.state.path) || "";
  }

  /* 工程内音色参考音频的试听地址；variantId 为空时用默认变体。 */
  function voiceAudioUrl(voice, variantId) {
    const query = { path: projectPath(), voice_id: voice.id };
    if (variantId) query.variant_id = variantId;
    return `/api/ui/voices/audio?${buildQuery(query)}`;
  }

  /* 内置音色试听地址。 */
  function builtinAudioUrl(key) {
    return `/api/ui/voices/builtin/audio?${buildQuery({ key })}`;
  }

  /* 性别中文名。 */
  function genderText(value) {
    return { male: "男声", female: "女声", other: "其他", unknown: "不确定" }[value] || "不确定";
  }

  /* 播放一段音频（复用页面里的 audio 节点）。 */
  function play(url) {
    const audio = el("voicePreviewAudio");
    if (!audio) return;
    audio.hidden = false;
    audio.src = url;
    audio.play().catch(() => toast("浏览器拒绝播放，请再点一次"));
  }

  /* 拉取音色库数据并重绘。 */
  async function loadBoard() {
    const path = projectPath();
    if (!path) {
      renderAll();
      return;
    }
    try {
      const data = await api(`/api/ui/voices?${buildQuery({ path })}`);
      state.voices = data.voices || [];
      state.characters = data.characters || [];
      state.builtin = data.builtin || { root: "", items: [] };
      if (!state.denoiseInit && data.denoise_config) {
        const checkbox = el("voiceDenoise");
        if (checkbox) checkbox.checked = !!data.denoise_config.default_enabled;
        state.denoiseInit = true;
      }
      renderAll();
    } catch (error) {
      toast(error.message);
    }
  }

  /* 重绘三块区域。 */
  function renderAll() {
    syncBatchProject();
    renderBuiltin();
    renderMine();
    renderBinds();
    fillDesignCharacters();
    loadCasting();
  }

  /* 渲染内置音色卡片。 */
  function renderBuiltin() {
    const box = el("voiceBuiltinList");
    if (!box) return;
    const root = el("voiceBuiltinRoot");
    if (root) root.textContent = state.builtin.root ? `目录：${state.builtin.root}` : "";
    const items = state.builtin.items || [];
    if (!items.length) {
      box.innerHTML = '<p class="muted">没有可用的内置音色清单。</p>';
      return;
    }
    box.innerHTML = items
      .map((item) => {
        const stateText = item.present ? `已下载 ${humanSize(item.bytes)}` : "未下载";
        const listen = item.present
          ? `<button class="btn small" type="button" data-voice-act="audition-builtin" data-key="${esc(item.key)}">试听</button>`
          : "";
        const action = item.present
          ? `<button class="btn small" type="button" data-voice-act="import-one" data-key="${esc(item.key)}">导入</button>`
          : `<button class="btn small" type="button" data-voice-act="install-one" data-key="${esc(item.key)}">下载</button>`;
        return `<div class="voice-card">
          <div class="voice-title"><span class="voice-name">${esc(item.name)}</span>
            <span class="tag">${genderText(item.gender)}</span></div>
          <div class="voice-meta">源文件 ${esc(item.key)}.wav · ${msText(item.duration_ms)} · ${stateText}</div>
          <div class="voice-actions">${listen}${action}</div>
        </div>`;
      })
      .join("");
  }

  /* 渲染「我的音色」列表。 */
  function renderMine() {
    const box = el("voiceMineList");
    const empty = el("voiceMineEmpty");
    const count = el("voiceMineCount");
    if (!box) return;
    if (count) count.textContent = state.voices.length ? `共 ${state.voices.length} 个` : "";
    if (empty) empty.hidden = state.voices.length > 0;
    box.innerHTML = state.voices
      .map((voice) => {
        const variants = voice.variants || [];
        const tags = (voice.tags || []).map((tag) => `<span class="tag">${esc(tag)}</span>`).join(" ");
        const denoised = variants.some((item) => ((item.reference || {}).denoise || {}).enabled);
        return `<div class="voice-row">
          <span class="voice-name">${esc(voice.name)}</span>
          <span class="tag">${genderText(voice.gender)}</span>
          ${tags}
          ${denoised ? '<span class="tag">已降噪</span>' : ""}
          <span class="muted">变体 ${variants.length}</span>
          <audio controls preload="none" src="${voiceAudioUrl(voice)}"></audio>
          <div class="voice-actions">
            <button class="btn small" type="button" data-voice-act="audition-voice" data-id="${esc(voice.id)}">试听</button>
            <button class="btn small" type="button" data-voice-act="delete-voice" data-id="${esc(voice.id)}">删除</button>
          </div>
        </div>`;
      })
      .join("");
  }

  /* 渲染角色 → 音色绑定表。
   * 绑定值统一按字符串比较；后端已把 v1 voice_id、变体 id、旧版内置名称
   * 解析成音色 id，这里再做一次容错，避免类型或残留旧值导致下拉不回显。
   */
  function renderBinds() {
    const box = el("voiceBindList");
    if (!box) return;
    if (!state.characters.length) {
      box.innerHTML = '<p class="muted">还没有人物，先到「人物档案」新增或生成。</p>';
      return;
    }
    box.innerHTML = state.characters
      .map((character) => {
        const bound = character.voice_id === null || character.voice_id === undefined ? "" : String(character.voice_id);
        const options = state.voices
          .map((voice) => {
            const voiceId = String(voice.id);
            return `<option value="${esc(voiceId)}" ${voiceId === bound ? "selected" : ""}>${esc(voice.name)}</option>`;
          })
          .join("");
        const matched = !bound || state.voices.some((voice) => String(voice.id) === bound);
        const missing = matched
          ? ""
          : `<option value="${esc(bound)}" selected disabled>已绑定（音色缺失）</option>`;
        return `<div class="voice-bind-row">
          <span class="voice-bind-name">${esc(character.name)}</span>
          <span class="tag">${esc(roleLabel(character.role))}</span>
          <select data-voice-act="bind" data-character="${esc(character.id)}">
            <option value="">未绑定</option>
            ${missing}
            ${options}
          </select>
        </div>`;
      })
      .join("");

    // innerHTML 写入后再显式赋值一次，确保各浏览器都把已绑定项回显出来
    box.querySelectorAll('select[data-voice-act="bind"]').forEach((select) => {
      const character = state.characters.find((item) => item.id === select.dataset.character);
      const bound = character && character.voice_id !== null && character.voice_id !== undefined
        ? String(character.voice_id)
        : "";
      if (bound && Array.from(select.options).some((option) => option.value === bound)) {
        select.value = bound;
      } else {
        select.value = "";
      }
    });
  }

  /* 统一调用 POST 接口，失败时 toast。 */
  async function post(url, payload, okText) {
    try {
      const data = await api(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (okText) toast(okText, true);
      return data;
    } catch (error) {
      toast(error.message);
      return null;
    }
  }

  /* 下载内置音色（全部或单个）。 */
  async function installBuiltin(keys) {
    const button = el("voiceBuiltinInstall");
    if (button) button.disabled = true;
    const data = await post("/api/ui/voices/builtin/install", keys ? { keys } : {}, "内置音色下载完成");
    if (button) button.disabled = false;
    if (data && data.result && !data.result.ok) {
      const failed = (data.result.failed || []).map((item) => item.key).join("、");
      toast(`部分音色下载失败：${failed || "未知原因"}`);
    }
    await loadBoard();
  }

  /* 导入内置音色到工程（全部或单个）。 */
  async function importBuiltin(keys) {
    const path = projectPath();
    if (!path) {
      toast("请先打开工程");
      return;
    }
    const button = el("voiceBuiltinImport");
    if (button) button.disabled = true;
    const data = await post("/api/ui/voices/builtin/import", keys ? { path, keys } : { path });
    if (button) button.disabled = false;
    if (data) {
      const added = (data.added || []).length;
      const updated = (data.updated || []).length;
      toast(`内置音色：新增 ${added} 个，更新 ${updated} 个`, true);
      state.voices = data.voices || [];
      state.builtin.items = data.items || state.builtin.items;
      renderAll();
    }
  }

  /* 删除音色（同时解绑角色）。 */
  async function deleteVoice(voiceId) {
    const voice = state.voices.find((item) => item.id === voiceId);
    if (!voice) return;
    if (!window.confirm(`删除音色「${voice.name}」？引用它的角色会自动解绑。`)) return;
    const data = await post("/api/ui/voices/delete", { path: projectPath(), voice_id: voiceId }, "已删除");
    if (data) {
      state.voices = data.voices || [];
      state.characters = data.characters || [];
      renderAll();
    }
  }

  /* 绑定角色到音色。 */
  async function bindVoice(characterId, voiceId) {
    const data = await post(
      "/api/ui/voices/bind",
      { path: projectPath(), character_id: characterId, voice_id: voiceId || null },
      "绑定已更新"
    );
    if (data) {
      state.characters = data.characters || state.characters;
      renderBinds();
    }
  }

  /* 事件委托：音色库所有按钮 / 下拉框。 */
  function onAction(event) {
    const trigger = event.target.closest("[data-voice-act]");
    if (!trigger) return;
    const act = trigger.dataset.voiceAct;
    if (act === "audition-builtin") play(builtinAudioUrl(trigger.dataset.key || ""));
    if (act === "audition-voice") {
      const voice = state.voices.find((item) => item.id === trigger.dataset.id);
      if (voice) play(voiceAudioUrl(voice));
    }
    if (act === "install-one") installBuiltin([trigger.dataset.key || ""]);
    if (act === "import-one") importBuiltin([trigger.dataset.key || ""]);
    if (act === "delete-voice") deleteVoice(trigger.dataset.id || "");
    if (act === "bind") bindVoice(trigger.dataset.character || "", trigger.value);
  }

  /* Blob 转 base64（去掉 data URL 前缀）。 */
  function blobToBase64(blob) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result || "").split(",", 2)[1] || "");
      reader.onerror = () => reject(reader.error || new Error("读取录音失败"));
      reader.readAsDataURL(blob);
    });
  }

  /* 根据 MIME 猜文件后缀，供后端 ffmpeg 识别。 */
  function suffixForMime(mime) {
    if (mime.includes("mp4")) return ".m4a";
    if (mime.includes("ogg")) return ".ogg";
    if (mime.includes("wav")) return ".wav";
    return ".webm";
  }

  /* 更新录音状态文字与按钮。 */
  function setRecordState(text) {
    const node = el("voiceRecordState");
    if (node) node.textContent = text;
    const button = el("voiceRecordBtn");
    if (button) button.textContent = state.recording ? "■ 停止录音" : "● 开始录音";
  }

  /* 停止录音并生成可试听的 Blob。 */
  function stopRecording() {
    if (!state.recorder) return;
    state.recording = false;
    clearInterval(state.timer);
    state.recorder.stop();
    setRecordState("处理录音中…");
  }

  /* 开始/停止录音按钮。 */
  async function toggleRecording() {
    if (state.recording) {
      stopRecording();
      return;
    }
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || !window.MediaRecorder) {
      toast("当前浏览器不支持录音，请改用「选择音频文件」");
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const mime = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg;codecs=opus"].find(
        (item) => MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported(item)
      );
      const recorder = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
      state.stream = stream;
      state.recorder = recorder;
      state.chunks = [];
      state.recorded = true;
      state.recording = true;
      state.startedAt = Date.now();
      recorder.ondataavailable = (event) => {
        if (event.data && event.data.size) state.chunks.push(event.data);
      };
      recorder.onstop = () => {
        const type = recorder.mimeType || mime || "audio/webm";
        state.blob = new Blob(state.chunks, { type });
        state.blobName = `recording${suffixForMime(type)}`;
        const audio = el("voicePreviewAudio");
        if (audio) {
          audio.hidden = false;
          audio.src = URL.createObjectURL(state.blob);
        }
        setRecordState(`已录制 ${msText(Date.now() - state.startedAt)}，可试听后保存`);
        stream.getTracks().forEach((track) => track.stop());
        state.recorder = null;
        state.stream = null;
      };
      recorder.start();
      setRecordState("录音中 0.0s");
      state.timer = setInterval(() => {
        const seconds = (Date.now() - state.startedAt) / 1000;
        setRecordState(`录音中 ${seconds.toFixed(1)}s`);
        if (seconds >= 20) stopRecording();
      }, 200);
    } catch (error) {
      toast(`无法访问麦克风：${error.message}`);
    }
  }

  /* 选择本地音频文件。 */
  function onFile(event) {
    const file = (event.target.files || [])[0];
    if (!file) return;
    state.blob = file;
    state.blobName = file.name || "upload.wav";
    state.recorded = false;
    const audio = el("voicePreviewAudio");
    if (audio) {
      audio.hidden = false;
      audio.src = URL.createObjectURL(file);
    }
    setRecordState(`已选择 ${file.name}`);
  }

  /* 保存新音色。 */
  async function saveVoice() {
    const name = (el("voiceName") || {}).value || "";
    if (!name.trim()) {
      toast("请先填写音色名称");
      return;
    }
    if (!state.blob) {
      toast("请先录音或选择音频文件");
      return;
    }
    if (!(el("voiceConsent") || {}).checked) {
      toast("请先确认你拥有这段录音的使用授权");
      return;
    }
    const button = el("voiceSave");
    const status = el("voiceSaveState");
    const denoise = !!(el("voiceDenoise") || {}).checked;
    if (button) button.disabled = true;
    if (status) status.textContent = denoise ? "转码并降噪中…" : "转换并保存中…";
    try {
      const audioBase64 = await blobToBase64(state.blob);
      const data = await post(
        "/api/ui/voices",
        {
          path: projectPath(),
          name: name.trim(),
          gender: (el("voiceGender") || {}).value || "unknown",
          ref_text: (el("voiceRefText") || {}).value || "",
          consent: true,
          recorded: state.recorded,
          denoise,
          audio_base64: audioBase64,
          audio_filename: state.blobName,
        },
        "音色已保存"
      );
      if (data) {
        const info = data.denoise || {};
        if (denoise && !info.enabled) toast(info.note || "降噪未生效，已保留原音");
        state.voices = data.voices || state.voices;
        state.characters = data.characters || state.characters;
        state.blob = null;
        state.blobName = "";
        state.recorded = false;
        if (el("voiceName")) el("voiceName").value = "";
        if (el("voiceRefText")) el("voiceRefText").value = "";
        if (el("voiceFile")) el("voiceFile").value = "";
        if (el("voiceConsent")) el("voiceConsent").checked = false;
        const audio = el("voicePreviewAudio");
        if (audio && data.voice) {
          // 保存后试听切到服务端产物：勾选降噪时即为降噪后的版本
          audio.hidden = false;
          audio.src = voiceAudioUrl(data.voice, data.variant_id);
          audio.load();
        }
        setRecordState("未录音");
        renderAll();
      }
    } finally {
      if (button) button.disabled = false;
      if (status) status.textContent = "";
    }
  }

  /* ---------------- 角色设定生成音色（voice design） ---------------- */

  const design = {
    batchId: "",
    total: 0,
    running: false,
    overlay: null,
    overlayTimer: null,
    abort: null,
    token: 0,
    stopped: false,
    // 最近一次带入面板的角色 id；用于判断下拉变化后是否需要重新带入
    characterId: "",
  };

  // 一键批量生成音色：任务状态、流连接与续跑标记
  const batch = {
    projectPath: "",
    jobId: "",
    job: null,
    running: false,
    abort: null,
    token: 0,
    stopped: false,
    overlay: null,
  };

  // 音色编排：总览数据、撤销状态、展开详情的角色与搜索防抖计时器
  const cast = {
    data: null,
    preview: null,
    undo: null,
    expanded: "",
    searchTimer: null,
  };

  /* 候选音频试听地址。 */
  function designAudioUrl(batchId, index) {
    return `/api/ui/voices/design/audio?${buildQuery({ path: projectPath(), batch_id: batchId, index })}`;
  }

  /* 批次时间显示：ISO 转本地可读格式。 */
  function batchTime(value) {
    if (!value) return "时间未知";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
  }

  /* 从磁盘恢复当前角色的历史候选批次；没有正在查看的批次时自动打开最近一条。 */
  async function loadDesignHistory(autoRestore = true) {
    const path = projectPath();
    const select = el("voiceDesignCharacter");
    const characterId = select ? select.value : "";
    const box = el("voiceDesignHistory");
    if (!box) return;
    if (!path || !characterId) {
      box.innerHTML = "";
      return;
    }
    try {
      const data = await api(
        `/api/ui/voices/design/history?${buildQuery({ path, character_id: characterId })}`
      );
      const batches = data.batches || [];
      renderDesignHistory(batches);
      if (autoRestore && !design.batchId) {
        const latest = batches.find((item) => item.status === "ready" && (item.candidates || []).some((cand) => cand.available))
          || batches.find((item) => item.status === "running");
        if (latest) restoreDesignBatch(latest);
      }
    } catch (error) {
      // 历史恢复失败不影响正常抽卡流程，静默留空
      box.innerHTML = "";
    }
  }

  /* 渲染历史批次按钮（含批次 ID、生成时间、进度与是否已采用）。 */
  function renderDesignHistory(batches) {
    const box = el("voiceDesignHistory");
    if (!box) return;
    if (!batches.length) {
      box.innerHTML = '<p class="muted">该角色还没有历史候选批次。</p>';
      return;
    }
    box.innerHTML = batches
      .map((item) => {
        const active = item.batch_id === design.batchId;
        const statusText = item.status === "ready"
          ? "已完成"
          : item.status === "running"
            ? `生成中 ${item.done || 0}/${item.total || 0}`
            : item.status === "stopped"
              ? `已停止 ${item.done || 0}/${item.total || 0}`
              : item.status === "partial"
                ? `部分完成 ${item.done || 0}/${item.total || 0}（${(item.failures || []).length} 条失败）`
                : "失败";
        const adopted = item.adopted ? '<span class="tag">已采用</span>' : "";
        return `<div class="voice-row">
          <button class="btn small${active ? " primary" : ""}" type="button" data-design-batch="${esc(item.batch_id)}">${esc(item.batch_id)}</button>
          <span class="muted">${esc(batchTime(item.created_at))} · ${esc(statusText)}</span>
          ${adopted}
        </div>`;
      })
      .join("");
  }

  /* 打开一个历史批次：把候选重新渲染到卡片区，可继续试听或采用。 */
  async function loadDesignBatch(batchId) {
    const path = projectPath();
    const characterId = el("voiceDesignCharacter") ? el("voiceDesignCharacter").value : "";
    if (!path || !batchId) return;
    try {
      // 摘要里带「文件是否仍在 / 已采用序号」，优先用摘要；缺失时退回原始批次元数据
      const [meta, history] = await Promise.all([
        api(`/api/ui/voices/design/batches/${encodeURIComponent(batchId)}?${buildQuery({ path })}`),
        api(`/api/ui/voices/design/history?${buildQuery({ path, character_id: characterId })}`),
      ]);
      const summary = (history.batches || []).find((item) => item.batch_id === batchId);
      restoreDesignBatch(summary || meta);
      await loadDesignHistory();
    } catch (error) {
      toast(error.message);
    }
  }

  /* 确保角色行在当前筛选/搜索结果里；被隐藏时自动切「全部」并清空搜索。 */
  async function ensureCastRowVisible(characterId) {
    const inRows = () =>
      cast.data && (cast.data.rows || []).some((row) => String(row.character_id) === String(characterId));
    await loadCasting();
    if (inRows()) return false;
    let switched = false;
    if (castQuery()) {
      const search = el("voiceCastSearch");
      if (search) search.value = "";
      switched = true;
    }
    if (castFilter() !== "all") {
      const filter = el("voiceCastFilter");
      if (filter) filter.value = "all";
      switched = true;
    }
    if (!switched) return false;
    await loadCasting();
    toast("该角色在当前筛选外，已切换为全部");
    return switched;
  }

  /* 高亮刚跳转到的角色行，避免用户找不到位置。 */
  function highlightCastRow(characterId) {
    const list = el("voiceCastList");
    if (!list || !list.querySelector) return;
    const row = list.querySelector(`[data-cast-row="${characterId}"]`);
    if (!row || !row.classList) return;
    row.classList.add("cast-highlight");
    setTimeout(() => row.classList.remove("cast-highlight"), 2500);
  }

  /* 从批量条目跳到主表：展开该角色的就地详情并加载它的候选批次。 */
  async function openBatchCandidates(characterId, batchId) {
    if (!projectPath()) {
      toast("请先打开工程");
      return;
    }
    cast.expanded = characterId;
    const select = el("voiceDesignCharacter");
    if (select) {
      select.value = characterId;
      design.batchId = "";
      design.meta = null;
      design.running = false;
      if (el("voiceDesignCandidates")) el("voiceDesignCandidates").innerHTML = "";
      setDesignStatus("");
      // 带入该角色的性别与描述，保持与「按角色设定生成」一致
      applyDesignCharacter(false);
    }
    // 行可能被「只看需要处理」或搜索过滤掉：先切到能看见它的视图再展开
    await ensureCastRowVisible(characterId);
    const visible =
      cast.data && (cast.data.rows || []).some((row) => String(row.character_id) === String(characterId));
    if (!visible) {
      toast("未找到该角色，可能已被删除");
      return;
    }
    renderCasting();
    highlightCastRow(characterId);
    if (!batchId) {
      toast("该角色没有可查看的候选批次");
      return;
    }
    await loadDesignBatch(batchId);
    const target = el("voiceCastDetail");
    if (target && target.scrollIntoView) target.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  /* 把批次数据恢复到候选区；运行中的批次继续用流式接收后续候选。 */
  function restoreDesignBatch(meta) {
    design.batchId = meta.batch_id;
    design.total = Number(meta.total) || 0;
    design.meta = meta;
    if (meta.status === "running") {
      // 从历史恢复时重新开启流式，避免沿用上一次「用户已关闭」的停止标记
      design.stopped = false;
      design.token += 1;
      design.running = true;
      setDesignStatus(`已恢复批次 ${meta.batch_id}，正在继续生成…`);
      streamDesignBatch(design.token);
      return;
    }
    design.running = false;
    renderDesignCandidates(meta);
    const adopted = (meta.adopted_indices || []).length ? "，含已采用候选" : "";
    setDesignStatus(`已从磁盘恢复批次 ${meta.batch_id}（${(meta.candidates || []).length} 条候选${adopted}）`);
  }


  /* 按角色档案拼出音色面板的角色描述（只作为输入，不写回档案）。 */
  function designBrief(character) {
    const style = character.speech_style || {};
    const details = [];
    if ((character.personality || []).length) details.push(`性格：${character.personality.join("、")}`);
    if (character.background) details.push(`背景：${character.background}`);
    if (style.tempo) details.push(`语速：${style.tempo}`);
    if (style.pitch) details.push(`音区：${style.pitch}`);
    if (style.accent) details.push(`口音：${style.accent}`);
    if (!details.length) return "";
    const head = [];
    if (character.role) head.push(`定位：${roleLabel(character.role)}`);
    if (character.age_range) head.push(`年龄：${character.age_range}`);
    return head.concat(details).join("\n");
  }

  /* 确保性别下拉有不带默认值的空选项，缺档案时不会自动落到第一个枚举。 */
  function ensureDesignGenderPlaceholder(select) {
    if (!select || select.querySelector('option[value=""]')) return;
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "请选择（未带入）";
    select.insertBefore(option, select.firstChild);
  }

  /* 把当前选中角色的性别与描述整体带入面板；切换角色时也用它覆盖旧值。 */
  function applyDesignCharacter(notify) {
    const select = el("voiceDesignCharacter");
    const genderSelect = el("voiceDesignGender");
    const prompt = el("voiceDesignPrompt");
    const hint = el("voiceDesignHint");
    if (!select) return;
    ensureDesignGenderPlaceholder(genderSelect);
    const character = state.characters.find((item) => item.id === select.value);
    if (!character) {
      if (genderSelect) genderSelect.value = "";
      if (prompt) prompt.value = "";
      if (hint) hint.textContent = "";
      design.characterId = "";
      return;
    }
    const missing = [];
    const gender = ["male", "female", "other"].includes(character.gender) ? character.gender : "";
    if (genderSelect) genderSelect.value = gender;
    if (!gender) missing.push("性别");
    const brief = designBrief(character);
    if (prompt) prompt.value = brief;
    if (!brief) missing.push("角色描述");
    const text = missing.length
      ? `该角色缺少${missing.join("、")}，请先在人物页补充`
      : `已带入「${character.name}」的档案，可修改后再生成`;
    if (hint) hint.textContent = text;
    if (notify && missing.length) toast(text);
    design.characterId = character.id;
  }

  /* 填充「选择角色」下拉；保留当前选中项。 */
  function fillDesignCharacters() {
    const select = el("voiceDesignCharacter");
    if (!select) return;
    const current = select.value;
    select.innerHTML = state.characters.map((item) => `<option value="${esc(item.id)}">${esc(item.name)}</option>`).join("");
    if (current && state.characters.some((item) => item.id === current)) select.value = current;
    // 首次打开、工程切换或没有可选角色时带入一次；同一角色重复渲染不覆盖用户手改内容
    if (!select.value || design.characterId !== select.value) applyDesignCharacter(false);
    loadDesignHistory();
  }

  /* 更新设计区状态文案。 */
  function setDesignStatus(text) {
    if (el("voiceDesignState")) el("voiceDesignState").textContent = text;
  }

  /* 显示生成进度浮层（只在请求超过 200ms 后调用）。 */
  function showDesignOverlay(text) {
    if (design.overlay) {
      design.overlay.update(text);
      return;
    }
    design.overlay = NWEditor.openProgress({
      title: "正在生成音色候选",
      text,
      hint: "首次运行需加载约 4.6GB 模型；关闭窗口会停止后续候选，已完成的候选会保留。",
      onClose(reason) {
        design.overlay = null;
        // 只有用户主动关闭才复位；完成/失败由轮询自己收尾
        if (reason === "button" || reason === "escape" || reason === "mask" || reason === "manual") {
          stopDesignWaiting();
        }
      },
    });
  }

  /* 关闭生成浮层；reason 为 done/failed 时不提示「已停止等待」。 */
  function closeDesignOverlay(reason) {
    const overlay = design.overlay;
    design.overlay = null;
    if (overlay) overlay.close(reason);
  }

  /* 用户关闭浮层后的统一复位：停流、停请求、通知服务端停止后续候选。 */
  function stopDesignWaiting() {
    design.stopped = true;
    design.token += 1;
    design.running = false;
    if (design.overlayTimer) {
      clearTimeout(design.overlayTimer);
      design.overlayTimer = null;
    }
    if (design.abort) {
      design.abort.abort();
      design.abort = null;
    }
    if (el("voiceDesignStart")) el("voiceDesignStart").disabled = false;
    const text = "已停止生成，后续候选未生成；已完成的候选可继续试听或采用。";
    setDesignStatus(text);
    toast(text);
    const batchId = design.batchId;
    const path = projectPath();
    if (!batchId || !path) return;
    // 让服务端也停下 worker，避免继续占 GPU；已落盘候选保留
    api(`/api/ui/voices/design/batches/${encodeURIComponent(batchId)}/cancel`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    })
      .then((data) => {
        if (data && data.batch) renderDesignCandidates(data.batch);
        loadDesignHistory(false);
      })
      .catch(() => {});
  }

  /* 根据人物设定生成（或手写）音色描述。 */
  async function generateDesignProfile() {
    const path = projectPath();
    const characterId = el("voiceDesignCharacter") ? el("voiceDesignCharacter").value : "";
    if (!path || !characterId) {
      toast("请先打开工程并选择角色");
      return;
    }
    const button = el("voiceDesignProfile");
    if (button) button.disabled = true;
    setDesignStatus("正在生成描述…");
    try {
      const data = await post("/api/ui/voices/design/preview", { path, character_id: characterId });
      if (!data) return;
      if (el("voiceDesignPrompt")) el("voiceDesignPrompt").value = data.prompt || "";
      const gender = (data.profile && data.profile.gender) || "unknown";
      if (el("voiceDesignGender")) el("voiceDesignGender").value = data.profile ? gender : el("voiceDesignGender").value;
      const summary = (data.profile && data.profile.voice_summary) || "已使用手写描述";
      if (el("voiceDesignHint")) {
        el("voiceDesignHint").textContent = `${summary}${data.note ? `（${data.note}）` : ""}`;
      }
      if (data.note) toast(data.note);
      setDesignStatus("描述已生成，可编辑后开始抽卡");
    } finally {
      if (button) button.disabled = false;
    }
  }

  /* 提交抽卡批次并轮询进度。 */
  async function startDesignBatch() {
    const path = projectPath();
    const characterId = el("voiceDesignCharacter") ? el("voiceDesignCharacter").value : "";
    const prompt = el("voiceDesignPrompt") ? el("voiceDesignPrompt").value.trim() : "";
    if (!path || !characterId) {
      toast("请先打开工程并选择角色");
      return;
    }
    if (!prompt) {
      toast("请先生成或填写音色描述");
      return;
    }
    const count = Math.max(1, Math.min(6, Number(el("voiceDesignCount") && el("voiceDesignCount").value) || 4));
    const button = el("voiceDesignStart");
    if (button) button.disabled = true;
    // 新批次从干净状态开始：清掉上一次的浮层、计时器与中断标记
    closeDesignOverlay("done");
    design.stopped = false;
    design.token += 1;
    const token = design.token;
    design.abort = new AbortController();
    const started = Date.now();
    try {
      const data = await api("/api/ui/voices/design/batches", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path, character_id: characterId, prompt, count }),
        signal: design.abort.signal,
      });
      if (design.stopped || token !== design.token) return;
      design.batchId = data.batch_id;
      design.total = Number(data.total) || count;
      design.running = true;
      setDesignStatus("已提交，排队中…");
      // 超过 200ms 仍未完成才弹浮层，避免快请求闪一下
      design.overlayTimer = setTimeout(() => {
        design.overlayTimer = null;
        if (design.running && !design.stopped && token === design.token) showDesignOverlay("排队中…");
      }, Math.max(0, 200 - (Date.now() - started)));
      streamDesignBatch(token);
    } catch (error) {
      if (error.name === "AbortError" || design.stopped || token !== design.token) return;
      design.running = false;
      closeDesignOverlay("failed");
      setDesignStatus(error.message);
      toast(error.message);
    } finally {
      if (button) button.disabled = false;
    }
  }

  /* 批次收尾文案：区分完成 / 部分失败 / 已停止 / 整体失败。 */
  function designSummary(meta) {
    const done = (meta.candidates || []).length;
    const total = Number(meta.total) || done;
    const failed = (meta.failures || []).length;
    if (meta.status === "stopped") {
      return `已停止生成，后续候选未生成（已完成 ${done}/${total}，可试听或采用）`;
    }
    if (meta.status === "partial") {
      return `已生成 ${done}/${total} 条候选，${failed} 条失败，可单条重试`;
    }
    if (meta.status === "ready") {
      const adopted = (meta.adopted_indices || []).length;
      return adopted
        ? `已生成 ${done} 条候选，已自动采用候选 ${(meta.adopted_indices || [])[0]}，可在候选里改`
        : `已生成 ${done} 条候选，试听后点「采用」`;
    }
    if (meta.status === "failed") return meta.error || "生成失败";
    return `生成中…（已完成 ${done}/${total}）`;
  }

  /* 流式接收候选：每完成一条立即渲染，失败项单独标出。 */
  async function streamDesignBatch(token = design.token) {
    const batchId = design.batchId;
    const path = projectPath();
    if (!batchId || !path || design.stopped || token !== design.token) return;
    design.running = true;
    design.abort = new AbortController();
    const live = {
      batch_id: batchId,
      total: Number(design.total) || 0,
      candidates: [],
      failures: [],
      status: "running",
    };
    const progress = NW.progressInit();
    /* 按序号去重合并，保证重试时同一条不会出现两行。 */
    const upsert = (list, item) => {
      const index = Number(item.index) || 0;
      const next = list.filter((row) => Number(row.index) !== index);
      next.push(item);
      next.sort((a, b) => Number(a.index) - Number(b.index));
      return next;
    };
    try {
      const meta = await NW.streamPost(
        `/api/ui/voices/design/batches/${encodeURIComponent(batchId)}/stream`,
        { path },
        (event, data) => {
          if (design.stopped || token !== design.token) return;
          if (event === "stage") {
            const text = data.stage === "loading" ? "加载模型…" : "排队中…";
            setDesignStatus(text);
            if (design.overlay) design.overlay.update(text);
          } else if (event === "batch" || event === "chars") {
            const text = NW.progressApply(progress, data);
            if (text) {
              setDesignStatus(text);
              if (design.overlay) design.overlay.update(text);
            }
          } else if (event === "candidate") {
            live.candidates = upsert(live.candidates, data);
            live.failures = live.failures.filter((row) => Number(row.index) !== Number(data.index));
            if (data.done) live.done = Number(data.done);
            renderDesignCandidates(live);
          } else if (event === "candidate_error") {
            live.failures = upsert(live.failures, data);
            renderDesignCandidates(live);
          }
        },
        design.abort.signal
      );
      if (design.stopped || token !== design.token) return;
      const final = meta || live;
      design.running = false;
      design.meta = final;
      closeDesignOverlay("done");
      renderDesignCandidates(final);
      setDesignStatus(designSummary(final));
      if (final.status === "partial") {
        toast(`有 ${(final.failures || []).length} 条候选失败，可单条重试`);
      } else if (final.status === "failed") {
        toast(final.error || "生成失败");
      }
      // 生成完成后自动采用第 1 条；用户仍可在候选里点其它候选改绑
      const candidates = final.candidates || [];
      const adopted = final.adopted_indices || [];
      if (["ready", "partial"].includes(final.status) && candidates.length && !adopted.length) {
        await selectDesignCandidate(Number(candidates[0].index), { auto: true });
      }
      loadDesignHistory(false);
    } catch (error) {
      if (error.name === "AbortError" || design.stopped || token !== design.token) return;
      design.running = false;
      closeDesignOverlay("failed");
      // 流中断不等于批次失败：回读磁盘状态，已完成候选照常展示
      try {
        const latest = await api(`/api/ui/voices/design/batches/${encodeURIComponent(batchId)}?${buildQuery({ path })}`);
        renderDesignCandidates(latest);
        setDesignStatus(designSummary(latest));
      } catch (inner) {
        setDesignStatus(error.message);
      }
      toast(error.message);
    }
  }

  /* 单条重试：提交后重新订阅同一批次的流，完成后继续合并到候选区。 */
  async function retryDesignCandidate(index) {
    const path = projectPath();
    const batchId = design.batchId;
    if (!path || !batchId) {
      toast("没有可重试的候选批次");
      return;
    }
    design.stopped = false;
    design.token += 1;
    const token = design.token;
    setDesignStatus(`正在重试候选 ${index}…`);
    const meta = await post(
      `/api/ui/voices/design/batches/${encodeURIComponent(batchId)}/retry`,
      { path, index },
      `已提交候选 ${index} 的重试`
    );
    if (!meta) return;
    design.total = Number(meta.total) || design.total;
    renderDesignCandidates(meta);
    streamDesignBatch(token);
  }

  /* ---------------- 一键批量生成音色 ---------------- */

  /* 批量范围/章节下拉的当前值。 */
  function batchScope() {
    return el("voiceBatchScope") ? el("voiceBatchScope").value : "unbound";
  }

  function batchChapterId() {
    return el("voiceBatchChapter") ? el("voiceBatchChapter").value : "";
  }

  /* 写批量状态文案。 */
  function setBatchState(text) {
    if (el("voiceBatchState")) el("voiceBatchState").textContent = text || "";
  }

  /* 渲染批量任务的逐角色条目：状态、自动采用结果、失败原因与重试。 */
  function renderBatchItems(job) {
    const box = el("voiceBatchItems");
    const retryAll = el("voiceBatchRetryFailed");
    if (retryAll) retryAll.hidden = !job || !(job.items || []).some((item) => item.status === "failed");
    if (!box) return;
    if (!job) {
      box.innerHTML = "";
      return;
    }
    const labels = { pending: "待生成", running: "生成中", ready: "已完成", failed: "失败", skipped: "已跳过" };
    box.innerHTML = (job.items || [])
      .map((item) => {
        const detail = item.status === "skipped"
          ? esc(item.reason || "已跳过")
          : item.status === "failed"
            ? esc(item.error || "生成失败")
            : item.status === "ready"
              ? item.adopted
                ? `已自动采用候选 ${item.candidate_index}，可在候选里改（${item.candidates_done}/${item.candidates_total}）`
                : `候选 ${item.candidates_done}/${item.candidates_total} 已生成${item.error ? `；${esc(item.error)}` : ""}`
              : item.status === "running"
                ? `候选 ${item.candidates_done || 0}/${item.candidates_total || 0}`
                : item.error
                  ? esc(item.error)
                  : "等待生成";
        const view = item.status === "ready" && item.batch_id
          ? `<button class="btn small" type="button" data-batch-candidates="${esc(item.character_id)}" data-batch-id="${esc(item.batch_id)}">查看并改选候选</button>`
          : "";
        const retry = item.status === "failed"
          ? `<button class="btn small" type="button" data-batch-retry="${esc(item.character_id)}">重试这个角色</button>`
          : "";
        return `<div class="voice-row">
          <span class="voice-name">${esc(item.name || item.character_id)}</span>
          <span class="tag">${labels[item.status] || esc(item.status)}</span>
          <span class="muted">${detail}</span>
          ${view}${retry}
        </div>`;
      })
      .join("");
  }

  /* 批量任务收尾文案：完成 / 部分失败 / 已停止 / 失败。 */
  function batchSummary(job) {
    if (!job) return "";
    const total = (job.items || []).filter((item) => item.status !== "skipped").length;
    const done = job.done || 0;
    const failed = job.failed || 0;
    if (job.status === "stopped") return `已停止生成，未完成的角色可续跑（已完成 ${done}/${total}）`;
    if (job.status === "partial") return `已完成 ${done}/${total} 个角色，${failed} 个失败，可单独重试或重试全部失败`;
    if (job.status === "ready") return `已为 ${done} 个角色生成并自动采用第 1 条候选，可在候选里改`;
    if (job.status === "failed") return job.note || "批量生成失败";
    return `生成中…（已完成 ${done}/${total}）`;
  }

  /* 预览批量范围：填充章节下拉并显示「待生成 N 个，跳过 M 个」。 */
  async function refreshBatchPlan() {
    const path = projectPath();
    if (!path) return null;
    const scope = batchScope();
    const chapterId = scope === "chapter" ? batchChapterId() : "";
    const data = await post("/api/ui/voices/design/batch/plan", {
      path,
      scope,
      chapter_id: chapterId || null,
    });
    if (!data) return null;
    const chapterSelect = el("voiceBatchChapter");
    if (chapterSelect && data.chapters) {
      const current = chapterSelect.value;
      chapterSelect.innerHTML = (data.chapters || [])
        .map((item) => `<option value="${esc(item.id)}">第${item.index || "?"}章 ${esc(item.title || "")}</option>`)
        .join("");
      if (current && (data.chapters || []).some((item) => item.id === current)) chapterSelect.value = current;
    }
    const skipped = (data.skipped || []).map((item) => `${item.name}（${item.reason}）`).join("、");
    if (el("voiceBatchHint")) {
      el("voiceBatchHint").textContent = `待生成 ${(data.pending || []).length} 个角色，跳过 ${(data.skipped || []).length} 个${skipped ? `：${skipped}` : ""}`;
    }
    return data;
  }

  /* 批量进度浮层：关闭即停止后续角色。 */
  function showBatchOverlay(text) {
    if (batch.overlay) {
      batch.overlay.update(text);
      return;
    }
    batch.overlay = NWEditor.openProgress({
      title: "正在批量生成音色",
      text: text || "准备中…",
      hint: "逐角色串行生成并自动采用第 1 条候选；关闭窗口会停止后续角色，已完成的结果保留。",
      onClose(reason) {
        batch.overlay = null;
        if (reason === "button" || reason === "escape" || reason === "mask" || reason === "manual") {
          stopBatchWaiting();
        }
      },
    });
  }

  /* 关闭批量进度浮层；done/failed 不触发停止。 */
  function closeBatchOverlay(reason) {
    const overlay = batch.overlay;
    batch.overlay = null;
    if (overlay) overlay.close(reason);
  }

  /* 用户关闭浮层后的批量复位：停流并通知服务端停止当前角色。 */
  function stopBatchWaiting() {
    batch.stopped = true;
    batch.token += 1;
    batch.running = false;
    if (batch.abort) {
      batch.abort.abort();
      batch.abort = null;
    }
    if (el("voiceBatchStart")) el("voiceBatchStart").disabled = false;
    const text = "已停止生成，未完成的角色可续跑；已完成的结果保留。";
    setBatchState(text);
    toast(text);
    const jobId = batch.jobId;
    const path = projectPath();
    if (!jobId || !path) return;
    api(`/api/ui/voices/design/batch/${encodeURIComponent(jobId)}/cancel`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    })
      .then((data) => {
        if (data && data.job) {
          batch.job = data.job;
          renderBatchItems(data.job);
          setBatchState(batchSummary(data.job));
        }
        if (el("voiceBatchResume")) el("voiceBatchResume").hidden = false;
      })
      .catch(() => {});
  }

  /* 流式接收批量任务：逐角色条目与逐候选进度，完成后刷新音色板。 */
  async function streamBatchJob(token = batch.token) {
    const jobId = batch.jobId;
    const path = projectPath();
    if (!jobId || !path || batch.stopped || token !== batch.token) return;
    batch.running = true;
    batch.abort = new AbortController();
    const progress = NW.progressInit();
    try {
      const job = await NW.streamPost(
        `/api/ui/voices/design/batch/${encodeURIComponent(jobId)}/stream`,
        { path },
        (event, data) => {
          if (batch.stopped || token !== batch.token) return;
          if (event === "item") {
            if (!batch.job) batch.job = { job_id: jobId, items: [], status: "running" };
            const items = batch.job.items || [];
            const index = items.findIndex((item) => item.character_id === data.character_id);
            if (index >= 0) items[index] = data;
            else items.push(data);
            batch.job.items = items;
            batch.job.done = items.filter((item) => item.status === "ready").length;
            batch.job.failed = items.filter((item) => item.status === "failed").length;
            renderBatchItems(batch.job);
          } else if (event === "batch" || event === "chars") {
            const text = NW.progressApply(progress, data);
            if (text) {
              setBatchState(text);
              if (batch.overlay) batch.overlay.update(text);
            }
          }
        },
        batch.abort.signal
      );
      if (batch.stopped || token !== batch.token) return;
      batch.running = false;
      batch.job = job || batch.job;
      closeBatchOverlay("done");
      renderBatchItems(batch.job);
      setBatchState(batchSummary(batch.job));
      if (batch.job && (batch.job.status === "partial" || batch.job.status === "failed")) {
        toast(batch.job.note || "批量生成存在失败项");
      }
      if (el("voiceBatchResume")) {
        el("voiceBatchResume").hidden = !(batch.job && ["stopped", "partial", "failed"].includes(batch.job.status));
      }
      await loadBoard();
    } catch (error) {
      if (error.name === "AbortError" || batch.stopped || token !== batch.token) return;
      batch.running = false;
      closeBatchOverlay("failed");
      setBatchState(error.message);
      toast(error.message);
    }
  }

  /* 开始批量生成：先预览统计，确认后逐角色串行生成。 */
  async function startBatchJob() {
    const path = projectPath();
    if (!path) return;
    const plan = await refreshBatchPlan();
    if (!plan) return;
    if (!(plan.pending || []).length) {
      toast("没有需要生成音色的角色");
      return;
    }
    const count = Number(el("voiceBatchCount") ? el("voiceBatchCount").value : 4) || 4;
    const confirmed = window.confirm(
      `待生成 ${plan.pending.length} 个角色，跳过 ${plan.skipped.length} 个。\n每个角色生成 ${count} 条候选并自动采用第 1 条，开始？`
    );
    if (!confirmed) return;
    batch.stopped = false;
    batch.token += 1;
    const token = batch.token;
    const job = await post(
      "/api/ui/voices/design/batch",
      { path, scope: plan.scope, chapter_id: plan.chapter_id || null, count },
      "已开始批量生成音色"
    );
    if (!job || !job.job_id) return;
    batch.jobId = job.job_id;
    batch.job = job;
    batch.running = true;
    renderBatchItems(job);
    setBatchState(batchSummary(job));
    showBatchOverlay("准备中…");
    streamBatchJob(token);
  }

  /* 续跑/重试：characterIds 为空时处理全部未完成与失败项。 */
  async function retryBatchCharacters(characterIds) {
    const path = projectPath();
    const jobId = batch.jobId;
    if (!path || !jobId) {
      toast("没有可续跑的批量任务");
      return;
    }
    batch.stopped = false;
    batch.token += 1;
    const token = batch.token;
    const data = await post(
      `/api/ui/voices/design/batch/${encodeURIComponent(jobId)}/resume`,
      { path, character_ids: characterIds || null },
      "已提交批量续跑"
    );
    if (!data || !data.job) return;
    batch.job = data.job;
    renderBatchItems(data.job);
    showBatchOverlay("准备续跑…");
    streamBatchJob(token);
  }

  /* 恢复最近一次批量任务：运行中则继续看进度，中断/失败则展示并允许续跑。 */
  async function loadLatestBatch() {
    const path = projectPath();
    if (!path) return;
    let data = null;
    try {
      data = await api(`/api/ui/voices/design/batch/latest?${buildQuery({ path })}`);
    } catch (error) {
      return;
    }
    const job = data && data.job;
    if (!job) {
      renderBatchItems(null);
      refreshBatchPlan();
      return;
    }
    batch.jobId = job.job_id;
    batch.job = job;
    renderBatchItems(job);
    setBatchState(batchSummary(job));
    if (el("voiceBatchResume")) {
      el("voiceBatchResume").hidden = !["stopped", "partial", "failed"].includes(job.status);
    }
    if (job.status === "running") {
      batch.stopped = false;
      batch.token += 1;
      batch.running = true;
      showBatchOverlay("恢复批量任务…");
      streamBatchJob(batch.token);
    }
  }

  /* 切换工程时清掉批量任务视图，避免串到上一个工程。 */
  function syncBatchProject() {
    const path = projectPath();
    if (batch.projectPath === path) return;
    batch.projectPath = path;
    batch.jobId = "";
    batch.job = null;
    batch.running = false;
    batch.stopped = false;
    batch.token += 1;
    renderBatchItems(null);
    setBatchState("");
    loadLatestBatch();
  }

  /* ---------------- 音色编排（自动 + 手动） ---------------- */

  /* 编排筛选与搜索词的当前值。 */
  function castFilter() {
    return el("voiceCastFilter") ? el("voiceCastFilter").value : "need";
  }

  function castQuery() {
    return el("voiceCastSearch") ? el("voiceCastSearch").value.trim() : "";
  }

  /* 写编排状态文案。 */
  function setCastState(text) {
    if (el("voiceCastState")) el("voiceCastState").textContent = text || "";
  }

  /* 同步撤销状态：按钮可用性只看最近一条撤销记录。 */
  function syncCastUndo(undo) {
    cast.undo = undo || null;
    const button = el("voiceCastUndo");
    if (button) button.hidden = !(undo && undo.available);
  }

  /* 拉取编排总览（默认只显示需要处理的角色）并重绘。 */
  async function loadCasting() {
    const path = projectPath();
    const box = el("voiceCastList");
    if (!box) return;
    if (!path) {
      box.innerHTML = '<p class="muted">请先打开工程。</p>';
      return;
    }
    try {
      const data = await api(
        `/api/ui/voices/casting/overview?${buildQuery({ path, q: castQuery(), filter: castFilter() })}`
      );
      cast.data = data;
      syncCastUndo(data.undo);
      renderCasting();
    } catch (error) {
      toast(error.message);
    }
  }

  /* 渲染编排表：问题行高亮，正常行只在「全部角色」筛选下低调展示。 */
  function renderCasting() {
    const box = el("voiceCastList");
    if (!box || !cast.data) return;
    const data = cast.data;
    const stats = data.stats || {};
    if (el("voiceCastStats")) {
      el("voiceCastStats").textContent = `共 ${stats.characters || 0} 个角色，需要处理 ${stats.need || 0} 个`;
    }
    const rows = data.rows || [];
    const normal = data.normal || [];
    const normalHint = normal.length
      ? `<p class="muted">另有 ${normal.length} 个角色已正常，切换筛选「全部角色」可查看。</p>`
      : "";
    // innerHTML 会移除详情面板，先取到节点引用，渲染后原位挂回
    const detail = el("voiceCastDetail");
    box.innerHTML = (rows.length
      ? rows.map((row) => castRow(row)).join("")
      : '<p class="muted">没有匹配的角色。</p>') + normalHint;
    mountCastDetail(detail);
  }

  /* 把常驻详情面板挂到展开行下方；未展开或无匹配行时放回隐藏容器。 */
  function mountCastDetail(detail) {
    detail = detail || el("voiceCastDetail");
    const home = el("voiceCastDetailHome");
    const list = el("voiceCastList");
    if (!detail || !home) return;
    const slot = cast.expanded && list ? list.querySelector(`[data-cast-detail="${cast.expanded}"]`) : null;
    if (slot) {
      slot.hidden = false;
      slot.appendChild(detail);
      detail.hidden = false;
    } else {
      detail.hidden = true;
      home.appendChild(detail);
    }
  }

  /* 展开/收起某个角色的就地详情（描述、候选、采用）。 */
  function toggleCastDetail(characterId) {
    if (!characterId) return;
    cast.expanded = cast.expanded === characterId ? "" : characterId;
    if (cast.expanded) {
      const select = el("voiceDesignCharacter");
      if (select) {
        select.value = characterId;
        design.batchId = "";
        design.meta = null;
        design.running = false;
        if (el("voiceDesignCandidates")) el("voiceDesignCandidates").innerHTML = "";
        setDesignStatus("");
        // 带入该角色的性别与描述，切换角色时不残留上一行内容
        applyDesignCharacter(false);
        loadDesignHistory();
      }
    }
    renderCasting();
  }

  /* 单行：角色 → 音色下拉、问题标签、自动建议理由与「调整」入口。 */
  function castRow(row) {
    const problems = (row.problems || [])
      .map((item) => `<span class="tag">${esc(item.message)}</span>`)
      .join(" ");
    const suggestion = row.suggestion;
    const suggestionText = suggestion
      ? `建议：${esc(suggestion.voice_name || suggestion.voice_id || "-")}${suggestion.reason ? `（${esc(suggestion.reason)}）` : ""}`
      : "无可用建议";
    const changed = suggestion && String(suggestion.voice_id || "") !== String(row.current_voice_id || "");
    const bound = row.current_voice_id === null || row.current_voice_id === undefined
      ? ""
      : String(row.current_voice_id);
    const voices = cast.data.voices || [];
    const options = voices
      .map((voice) => {
        const voiceId = String(voice.id);
        return `<option value="${esc(voiceId)}" ${voiceId === bound ? "selected" : ""}>${esc(voice.name)}（${esc(genderText(voice.gender))}）</option>`;
      })
      .join("");
    const missing = bound && !voices.some((voice) => String(voice.id) === bound)
      ? `<option value="${esc(bound)}" selected disabled>已绑定（音色缺失）</option>`
      : "";
    const expanded = cast.expanded === String(row.character_id);
    return `<div class="voice-bind-row${problems ? "" : " muted"}" data-cast-row="${esc(row.character_id)}">
      <span class="voice-bind-name">${esc(row.name || row.character_id)}</span>
      <span class="tag">${esc(roleLabel(row.role))}</span>
      ${problems}
      <select data-cast-bind="${esc(row.character_id)}">
        <option value="" ${bound ? "" : "selected"}>未绑定</option>
        ${missing}
        ${options}
      </select>
      <button class="btn small" type="button" data-cast-edit="${esc(row.character_id)}">${expanded ? "收起" : "调整"}</button>
      <span class="muted">${suggestionText}${changed ? ' <span class="tag">待对齐</span>' : ""}</span>
    </div>
    <div class="cast-detail-slot" data-cast-detail="${esc(row.character_id)}" hidden></div>`;
  }

  /* 预览自动编排：给出每个角色的建议与理由，不落盘。 */
  async function previewCasting() {
    const path = projectPath();
    if (!path) return;
    const data = await post("/api/ui/voices/casting/preview", { path });
    if (!data) return;
    cast.preview = data;
    await loadCasting();
    const stats = data.stats || {};
    const skipped = (data.skipped || []).map((item) => `${item.name}（${item.reason}）`).join("、");
    setCastState(
      `预览：${stats.changed || 0} 个角色将调整，${stats.skipped || 0} 个跳过${skipped ? `：${skipped}` : ""}；确认后点上方「一键应用全部建议（可撤销）」`
    );
  }

  /* 按建议填充全部：确认后落盘（可撤销）。 */
  async function applyCasting() {
    const path = projectPath();
    if (!path) return;
    const changed = cast.preview ? (cast.preview.stats || {}).changed || 0 : null;
    const message = changed === null
      ? "按角色设定自动匹配并绑定音色？旁白固定、龙套同池复用，应用后可撤销。"
      : `将调整 ${changed} 个角色的音色（含需对齐的章节覆盖），应用后可撤销，继续？`;
    if (!window.confirm(message)) return;
    const result = await post("/api/ui/voices/casting/apply", { path }, "自动编排已应用");
    if (!result) return;
    cast.preview = null;
    cast.data = result.overview || cast.data;
    syncCastUndo(result.undo);
    renderCasting();
    setCastState(`已应用：${(result.applied || []).length} 个角色；可撤销`);
    await loadBoard();
  }

  /* 单行替换音色（写撤销日志，可撤销）。 */
  async function bindCasting(characterId, voiceId) {
    const data = await post(
      "/api/ui/voices/casting/bind",
      { path: projectPath(), character_id: characterId, voice_id: voiceId || null },
      "音色已替换"
    );
    if (!data) return;
    cast.data = data.overview || cast.data;
    syncCastUndo(data.undo);
    renderCasting();
    await loadBoard();
  }

  /* 撤销最近一次自动编排。 */
  async function undoLastCasting() {
    const path = projectPath();
    if (!path) return;
    if (!window.confirm("撤销最近一次自动编排？只恢复绑定关系，已生成的音色保留在音色库。")) return;
    const data = await post("/api/ui/voices/undo/last", { path, kind: "casting" }, "已撤销最近一次自动编排");
    if (!data) return;
    cast.data = data.overview || cast.data;
    syncCastUndo(data.undo);
    renderCasting();
    setCastState("");  // 撤销后清掉上一轮的「已应用…可撤销」提示，避免与按钮状态矛盾
    await loadBoard();
  }

  /* 撤销最近一次批量自动生成（只恢复绑定，不删除已生成音色）。 */
  async function undoLastGeneration() {
    const path = projectPath();
    if (!path) return;
    if (!window.confirm("撤销最近一次自动生成？只恢复绑定关系，已生成的音色保留在音色库。")) return;
    const data = await post("/api/ui/voices/undo/last", { path, kind: "generation" }, "已撤销最近一次自动生成");
    if (!data) return;
    cast.data = data.overview || cast.data;
    syncCastUndo(data.undo);
    renderCasting();
    await loadBoard();
  }

  /* 渲染候选卡片（含基频提示、文件是否仍在、是否已被采用）与失败项。 */
  function renderDesignCandidates(meta) {
    const box = el("voiceDesignCandidates");
    if (!box) return;
    const adoptedIndices = meta.adopted_indices || [];
    const cards = (meta.candidates || [])
      .map((item) => {
        const hint = item.hint || {};
        const adopted = adoptedIndices.indexOf(item.index) >= 0;
        const missing = item.available === false;
        const warn = adopted
          ? '<span class="tag">已采用</span>'
          : hint.mismatch
            ? '<span class="tag">听感可能与设定不符</span>'
            : "";
        const f0 = hint.f0_median ? `基频 ${hint.f0_median}Hz` : "基频未测";
        const audio = missing
          ? '<p class="muted">候选音频已被清理，无法试听</p>'
          : `<audio controls preload="none" src="${designAudioUrl(meta.batch_id, item.index)}"></audio>`;
        const action = adopted
          ? '<button class="btn small" type="button" disabled>已采用</button>'
          : `<button class="btn small primary" type="button" data-design-select="${item.index}" ${missing ? "disabled" : ""}>采用这个音色</button>`;
        return `<div class="voice-card">
          <div class="voice-title"><span class="voice-name">候选 ${item.index}</span>${warn}</div>
          <div class="voice-meta">${(item.duration_ms / 1000).toFixed(1)}s · ${f0} · seed ${item.seed ?? "-"}</div>
          ${audio}
          <div class="voice-actions">${action}</div>
        </div>`;
      })
      .join("");
    // 失败项排在已完成候选之后，可单独重试，不影响其它候选
    const failures = (meta.failures || [])
      .map(
        (item) => `<div class="voice-card">
          <div class="voice-title"><span class="voice-name">候选 ${item.index}</span><span class="tag">生成失败</span></div>
          <div class="voice-meta">${esc(item.message || "生成失败")}</div>
          <div class="voice-actions"><button class="btn small" type="button" data-design-retry="${item.index}">重试这一条</button></div>
        </div>`
      )
      .join("");
    box.innerHTML = cards + failures;
  }

  /* 采用后角色可能不再算问题行：把筛选切到「全部」，避免正在看的详情面板被筛掉。 */
  function keepExpandedRowVisible(characterId) {
    if (!cast.expanded || String(cast.expanded) !== String(characterId)) return;
    if (castFilter() !== "all") {
      const filter = el("voiceCastFilter");
      if (filter) filter.value = "all";
    }
  }

  /* 采用候选：auto=true 时由生成完成自动触发，不弹确认、保留候选列表便于改选。 */
  async function selectDesignCandidate(index, options = {}) {
    const auto = !!options.auto;
    const path = projectPath();
    const batchId = design.batchId;
    const characterId = el("voiceDesignCharacter") ? el("voiceDesignCharacter").value : "";
    const character = state.characters.find((item) => item.id === characterId);
    let gender = el("voiceDesignGender") ? el("voiceDesignGender").value : "unknown";
    let genderSource = "manual";
    // 面板未带入也没有手动选择性别时不猜值（尤其不落成「其他」），先提示补充
    if (!gender) {
      toast("该角色缺少性别，请先在人物页补充，或在上方手动选择后再采用");
      if (el("voiceDesignGender")) el("voiceDesignGender").focus();
      return;
    }
    // 兜底不落成「其他」：档案有明确性别就沿用档案，否则记为未知
    if (gender === "other" && (character || {}).gender !== "other") {
      const archive = (character || {}).gender;
      gender = archive === "male" || archive === "female" ? archive : "unknown";
      genderSource = archive === "male" || archive === "female" ? "character" : "manual";
      toast(`已按角色档案把音色性别记为「${genderText(gender)}」，不会记录「其他」`);
    }
    const name = (el("voiceDesignName") && el("voiceDesignName").value.trim()) || (character && character.name) || "生成音色";
    if (!auto) {
      const confirmed = window.confirm(`采用候选 ${index}？\n性别标签将记为「${genderText(gender)}」，请确认与实际听感一致。`);
      if (!confirmed) return;
    }
    const data = await post("/api/ui/voices/design/select", {
      path,
      batch_id: batchId,
      index,
      name,
      gender,
      gender_source: genderSource,
      bind: true,
    }, auto ? "" : "已采用并绑定角色");
    if (!data) return;
    if (data.gender_note) toast(data.gender_note);
    state.voices = data.voices || state.voices;
    state.characters = data.characters || state.characters;
    if (auto) {
      // 自动采用后保留候选列表：显示「已采用」标签，用户可点其它候选改绑
      if (design.meta) {
        design.meta.adopted_indices = [Number(index)];
        renderDesignCandidates(design.meta);
      }
      setDesignStatus(`已自动采用候选 ${index}，可在候选里改`);
      toast(`已自动采用候选 ${index}，可在候选里改`);
      keepExpandedRowVisible(characterId);
      renderAll();
      return;
    }
    // 手动改选也保留候选列表：更新「已采用」标记，可继续换其它候选
    design.batchId = design.batchId || batchId;
    if (design.meta) {
      design.meta.adopted_indices = [Number(index)];
      renderDesignCandidates(design.meta);
    }
    setDesignStatus(`已采用候选 ${index}，可在候选里改`);
    toast(`已采用候选 ${index}，可在候选里改`, true);
    keepExpandedRowVisible(characterId);
    renderAll();
  }

  /* 清理当前批次候选（不影响已入库音色）。 */
  async function cleanupDesignBatch() {
    if (!design.batchId) {
      toast("当前没有待清理的候选批次");
      return;
    }
    try {
      await api(`/api/ui/voices/design/batches/${encodeURIComponent(design.batchId)}?${buildQuery({ path: projectPath() })}`, { method: "DELETE" });
      if (el("voiceDesignCandidates")) el("voiceDesignCandidates").innerHTML = "";
      design.batchId = "";
      setDesignStatus("候选已清理");
      await loadDesignHistory(false);
    } catch (error) {
      toast(error.message);
    }
  }

  /* 切换「人物档案 / 音色库」子标签。 */
  function onTabClick(event) {
    const trigger = event.target.closest("[data-char-tab]");
    if (!trigger) return;
    const tabs = el("charTabs");
    if (tabs) tabs.querySelectorAll(".subtab").forEach((item) => item.classList.toggle("active", item === trigger));
    const voices = trigger.dataset.charTab === "voices";
    if (!voices && state.recording) stopRecording();
    if (el("charProfilesPane")) el("charProfilesPane").hidden = voices;
    if (el("charVoicesPane")) el("charVoicesPane").hidden = !voices;
    if (voices) loadBoard();
  }

  /* 首次绑定事件，避免重复绑定。 */
  function bind() {
    if (state.bound) return;
    state.bound = true;
    if (el("charTabs")) el("charTabs").addEventListener("click", onTabClick);
    if (el("voiceBuiltinInstall")) el("voiceBuiltinInstall").addEventListener("click", () => installBuiltin(null));
    if (el("voiceBuiltinImport")) el("voiceBuiltinImport").addEventListener("click", () => importBuiltin(null));
    if (el("voiceRecordBtn")) el("voiceRecordBtn").addEventListener("click", toggleRecording);
    if (el("voiceFile")) el("voiceFile").addEventListener("change", onFile);
    if (el("voiceSave")) el("voiceSave").addEventListener("click", saveVoice);
    if (el("voiceMineList")) el("voiceMineList").addEventListener("click", onAction);
    if (el("voiceBuiltinList")) el("voiceBuiltinList").addEventListener("click", onAction);
    if (el("voiceBindList")) el("voiceBindList").addEventListener("change", onAction);
    if (el("voiceDesignProfile")) el("voiceDesignProfile").addEventListener("click", generateDesignProfile);
    if (el("voiceDesignStart")) el("voiceDesignStart").addEventListener("click", startDesignBatch);
    if (el("voiceDesignCleanup")) el("voiceDesignCleanup").addEventListener("click", cleanupDesignBatch);
    if (el("voiceDesignCandidates")) {
      el("voiceDesignCandidates").addEventListener("click", (event) => {
        const retry = event.target.closest("[data-design-retry]");
        if (retry) {
          retryDesignCandidate(Number(retry.dataset.designRetry));
          return;
        }
        const trigger = event.target.closest("[data-design-select]");
        if (trigger) selectDesignCandidate(Number(trigger.dataset.designSelect));
      });
    }
    if (el("voiceDesignHistory")) {
      el("voiceDesignHistory").addEventListener("click", (event) => {
        const trigger = event.target.closest("[data-design-batch]");
        if (trigger) loadDesignBatch(trigger.dataset.designBatch);
      });
    }
    if (el("voiceDesignCharacter")) {
      el("voiceDesignCharacter").addEventListener("change", () => {
        // 换角色时清掉上一角色的批次状态，再从磁盘恢复该角色自己的候选
        design.batchId = "";
        design.running = false;
        if (el("voiceDesignCandidates")) el("voiceDesignCandidates").innerHTML = "";
        setDesignStatus("");
        // 整体替换性别与角色描述（不保留上一角色内容），缺失字段给出提示
        applyDesignCharacter(true);
        loadDesignHistory();
      });
    }
    if (el("voiceBatchScope")) el("voiceBatchScope").addEventListener("change", () => refreshBatchPlan());
    if (el("voiceBatchChapter")) el("voiceBatchChapter").addEventListener("change", () => refreshBatchPlan());
    if (el("voiceBatchPreview")) el("voiceBatchPreview").addEventListener("click", () => refreshBatchPlan());
    if (el("voiceBatchStart")) el("voiceBatchStart").addEventListener("click", startBatchJob);
    if (el("voiceBatchResume")) el("voiceBatchResume").addEventListener("click", () => retryBatchCharacters(null));
    if (el("voiceBatchRetryFailed")) {
      el("voiceBatchRetryFailed").addEventListener("click", () => retryBatchCharacters(null));
    }
    if (el("voiceBatchUndo")) el("voiceBatchUndo").addEventListener("click", undoLastGeneration);
    if (el("voiceBatchItems")) {
      el("voiceBatchItems").addEventListener("click", (event) => {
        const retry = event.target.closest("[data-batch-retry]");
        if (retry) {
          retryBatchCharacters([retry.dataset.batchRetry]);
          return;
        }
        const view = event.target.closest("[data-batch-candidates]");
        if (view) openBatchCandidates(view.dataset.batchCandidates, view.dataset.batchId);
      });
    }
    if (el("voiceCastPreview")) el("voiceCastPreview").addEventListener("click", previewCasting);
    if (el("voiceCastApply")) el("voiceCastApply").addEventListener("click", applyCasting);
    if (el("voiceCastUndo")) el("voiceCastUndo").addEventListener("click", undoLastCasting);
    // 批量生成收进工具条：点开时自动预览范围
    if (el("voiceCastBatchToggle")) {
      el("voiceCastBatchToggle").addEventListener("click", async () => {
        const panel = el("voiceBatchPanel");
        if (!panel) return;
        panel.hidden = !panel.hidden;
        if (!panel.hidden) await refreshBatchPlan();
      });
    }
    if (el("voiceCastDetailClose")) {
      el("voiceCastDetailClose").addEventListener("click", () => {
        cast.expanded = "";
        renderCasting();
      });
    }
    if (el("voiceCastFilter")) el("voiceCastFilter").addEventListener("change", () => loadCasting());
    if (el("voiceCastSearch")) {
      el("voiceCastSearch").addEventListener("input", () => {
        if (cast.searchTimer) clearTimeout(cast.searchTimer);
        cast.searchTimer = setTimeout(() => {
          cast.searchTimer = null;
          loadCasting();
        }, 250);
      });
    }
    if (el("voiceCastList")) {
      el("voiceCastList").addEventListener("change", (event) => {
        const trigger = event.target.closest("[data-cast-bind]");
        if (trigger) bindCasting(trigger.dataset.castBind, trigger.value);
      });
      el("voiceCastList").addEventListener("click", (event) => {
        const trigger = event.target.closest("[data-cast-edit]");
        if (trigger) toggleCastDetail(trigger.dataset.castEdit);
      });
    }
    if (window.NW && NW.onProject) {
      NW.onProject(() => {
        if (el("charVoicesPane") && !el("charVoicesPane").hidden && projectPath()) loadBoard();
      });
    }
  }

  window.VoiceLibrary = {
    init() {
      bind();
    },
    /* 人物页激活时调用；只在音色库标签打开时取数。 */
    activate() {
      bind();
      if (el("charVoicesPane") && !el("charVoicesPane").hidden) loadBoard();
    },
  };
})();
