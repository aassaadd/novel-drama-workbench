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
    timer: null,
    startedAt: 0,
    bound: false,
  };

  /* 当前工程路径；未打开工程时返回空串。 */
  function projectPath() {
    return (window.NW && NW.state && NW.state.path) || "";
  }

  /* 工程内音色参考音频的试听地址。 */
  function voiceAudioUrl(voice) {
    return `/api/ui/voices/audio?${buildQuery({ path: projectPath(), voice_id: voice.id })}`;
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
        return `<div class="voice-row">
          <span class="voice-name">${esc(voice.name)}</span>
          <span class="tag">${genderText(voice.gender)}</span>
          ${tags}
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
    if (button) button.disabled = true;
    if (status) status.textContent = "转换并保存中…";
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
          audio_base64: audioBase64,
          audio_filename: state.blobName,
        },
        "音色已保存"
      );
      if (data) {
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
        if (audio) {
          audio.hidden = true;
          audio.removeAttribute("src");
        }
        setRecordState("未录音");
        renderAll();
      }
    } finally {
      if (button) button.disabled = false;
      if (status) status.textContent = "";
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
