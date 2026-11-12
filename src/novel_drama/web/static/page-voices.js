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
    renderBuiltin();
    renderMine();
    renderBinds();
    fillDesignCharacters();
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
    timer: null,
    running: false,
    overlay: null,
    overlayTimer: null,
    abort: null,
    token: 0,
    stopped: false,
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

  /* 把批次数据恢复到候选区；运行中的批次继续轮询。 */
  function restoreDesignBatch(meta) {
    design.batchId = meta.batch_id;
    if (meta.status === "running") {
      // 从历史恢复时重新开启轮询，避免沿用上一次「用户已关闭」的停止标记
      design.stopped = false;
      design.token += 1;
      design.running = true;
      setDesignStatus(`已恢复批次 ${meta.batch_id}，正在继续生成…`);
      pollDesignBatch(design.token);
      return;
    }
    design.running = false;
    renderDesignCandidates(meta);
    const adopted = (meta.adopted_indices || []).length ? "，含已采用候选" : "";
    setDesignStatus(`已从磁盘恢复批次 ${meta.batch_id}（${(meta.candidates || []).length} 条候选${adopted}）`);
  }


  /* 填充「选择角色」下拉；保留当前选中项。 */
  function fillDesignCharacters() {
    const select = el("voiceDesignCharacter");
    if (!select) return;
    const current = select.value;
    select.innerHTML = state.characters.map((item) => `<option value="${esc(item.id)}">${esc(item.name)}</option>`).join("");
    if (current && state.characters.some((item) => item.id === current)) select.value = current;
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
      hint: "首次运行需加载约 4.6GB 模型；关闭窗口只会停止等待，服务端任务可能仍会完成。",
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

  /* 用户关闭浮层后的统一复位：停轮询、停请求、恢复按钮与状态。 */
  function stopDesignWaiting() {
    design.stopped = true;
    design.token += 1;
    design.running = false;
    if (design.timer) {
      clearTimeout(design.timer);
      design.timer = null;
    }
    if (design.overlayTimer) {
      clearTimeout(design.overlayTimer);
      design.overlayTimer = null;
    }
    if (design.abort) {
      design.abort.abort();
      design.abort = null;
    }
    if (el("voiceDesignStart")) el("voiceDesignStart").disabled = false;
    const text = "已停止等待；服务端任务可能仍会完成，稍后可重新打开音色库查看历史批次。";
    setDesignStatus(text);
    toast(text);
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
      design.running = true;
      setDesignStatus("已提交，排队中…");
      // 超过 200ms 仍未完成才弹浮层，避免快请求闪一下
      design.overlayTimer = setTimeout(() => {
        design.overlayTimer = null;
        if (design.running && !design.stopped && token === design.token) showDesignOverlay("排队中…");
      }, Math.max(0, 200 - (Date.now() - started)));
      pollDesignBatch(token);
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

  /* 轮询批次状态，完成后渲染候选；token 变化或用户关闭后不再更新页面。 */
  async function pollDesignBatch(token = design.token) {
    const batchId = design.batchId;
    const path = projectPath();
    if (!batchId || !path || design.stopped || token !== design.token) return;
    try {
      const meta = await api(`/api/ui/voices/design/batches/${encodeURIComponent(batchId)}?${buildQuery({ path })}`);
      if (design.stopped || token !== design.token) return;
      if (meta.status === "running") {
        const text = meta.stage === "loading"
          ? "加载模型…"
          : meta.stage === "generating"
            ? `生成中 ${meta.done}/${meta.total}`
            : "排队中…";
        setDesignStatus(text);
        if (design.overlay) design.overlay.update(text);
        design.timer = setTimeout(() => pollDesignBatch(token), 2000);
        return;
      }
      design.running = false;
      if (design.timer) {
        clearTimeout(design.timer);
        design.timer = null;
      }
      closeDesignOverlay("done");
      if (meta.status === "ready") {
        renderDesignCandidates(meta);
        setDesignStatus(`已生成 ${meta.candidates.length} 条候选，试听后点「采用」`);
      } else {
        setDesignStatus(meta.error || "生成失败");
        toast(meta.error || "生成失败");
      }
    } catch (error) {
      if (error.name === "AbortError" || design.stopped || token !== design.token) return;
      design.running = false;
      closeDesignOverlay("failed");
      toast(error.message);
    }
  }

  /* 渲染候选卡片（含基频提示、文件是否仍在、是否已被采用）。 */
  function renderDesignCandidates(meta) {
    const box = el("voiceDesignCandidates");
    if (!box) return;
    const adoptedIndices = meta.adopted_indices || [];
    box.innerHTML = (meta.candidates || [])
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
  }

  /* 采用候选：人工确认性别后入库，可选绑定角色。 */
  async function selectDesignCandidate(index) {
    const path = projectPath();
    const batchId = design.batchId;
    const characterId = el("voiceDesignCharacter") ? el("voiceDesignCharacter").value : "";
    const character = state.characters.find((item) => item.id === characterId);
    const gender = el("voiceDesignGender") ? el("voiceDesignGender").value : "unknown";
    const name = (el("voiceDesignName") && el("voiceDesignName").value.trim()) || (character && character.name) || "生成音色";
    const confirmed = window.confirm(`采用候选 ${index}？\n性别标签将记为「${genderText(gender)}」，请确认与实际听感一致。`);
    if (!confirmed) return;
    const data = await post("/api/ui/voices/design/select", {
      path,
      batch_id: batchId,
      index,
      name,
      gender,
      gender_source: "manual",
      bind: true,
    }, "已采用并绑定角色");
    if (!data) return;
    state.voices = data.voices || state.voices;
    state.characters = data.characters || state.characters;
    design.batchId = "";
    if (el("voiceDesignCandidates")) el("voiceDesignCandidates").innerHTML = "";
    setDesignStatus("已入库；可继续「重新抽卡」生成下一批");
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
        clearTimeout(design.timer);
        if (el("voiceDesignCandidates")) el("voiceDesignCandidates").innerHTML = "";
        setDesignStatus("");
        loadDesignHistory();
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
