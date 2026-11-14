/**
 * 工作室的视图：画本编辑 / 录音 / 后期制作 / 智能对轨。
 * 与画本编辑共用顶栏、56px 图标栏、章节目录与右栏骨架；
 * 内容审听不再是独立主视图，而是画本编辑右栏的「智能审听」页签。
 * 数据全部来自现有接口（台词表、时间轴、重录、版本），不复制后端业务规则。
 */
import { esc, icon, toast, openDialog, closeDialog, openMenu, fmtClock, fmtDuration, charCount } from '../dom.js';
import { getJSON, postJSON, fileUrl } from '../api.js';
import { state, audioOf, toneFor } from '../store.js';
import { STATE_LABEL, stateClass } from '../shell.js';

/** 顶栏三个主视图：顺序与参考一致 */
export const VIEW_META = [
  { key: 'script', title: '画本编辑视图', icon: 'chapter' },
  { key: 'record', title: '录音视图', icon: 'mic' },
  { key: 'post', title: '后期制作视图', icon: 'audio' },
];

/** 「更多视图」下拉：智能对轨是独立上传页，内容审听进画本编辑右栏页签 */
export const MORE_VIEWS = [
  { key: 'align', title: '智能对轨', icon: 'arrow' },
  { key: 'audit', title: '内容审听', icon: 'list' },
];

/** 页面注入的刷新回调（切章、生成完成后由 editor.js 提供） */
const hooks = { refresh: async () => {}, openInScript: () => {} };

/**
 * 注册编辑器侧回调，避免视图模块反向依赖页面实现。
 * @param {{refresh?: Function, openInScript?: Function}} next 回调集合
 */
export function configureViews(next) {
  Object.assign(hooks, next);
}

/** 各视图的本地状态（不放进全局 store，避免和画本编辑互相干扰） */
const ui = {
  record: {
    filter: 'all',
    panelTab: 'tasks',
    playing: '',
    audio: null,
    peaks: new Map(),
    busy: '',
    current: '',
    deviceId: '',
    autoNext: true,
    startedAt: 0,
  },
  post: {
    zoom: 1,
    muted: new Set(),
    soloed: new Set(),
    volumes: new Map(),
    snap: true,
    linked: true,
    selected: null,
    /** 当前选中的片段 `{lane, index}`，用于淡入淡出与拖动后的定位 */
    selectedItem: null,
    /** 拖动片段时抑制随后的 click 试听 */
    dragSuppress: false,
    playing: false,
    cursor: 0,
    timers: [],
    audios: [],
    raf: 0,
    base: 0,
    startedAt: 0,
  },
  review: {
    onlyAbnormal: false,
    playing: false,
    index: -1,
    queue: [],
    audio: null,
    currentLine: '',
  },
  align: {
    file: null,
    tasks: [],
    loading: false,
    busy: false,
    error: '',
    start: '',
    end: '',
  },
};

/** 时间轴缓存：按章节 + 一次构建结果，避免每次重画都跑 CLI */
let timelineCache = { chapter: '', data: null };
/** 时间轴加载中的章节，防止并发重复构建 */
let timelineLoading = '';

/** 当前章节标题 */
function chapterLabel() {
  return state.chapters.find((item) => item.id === state.chapterId)?.title || '未选择章节';
}

/** 当前章节的时间轴（未加载时为 null） */
function timeline() {
  return timelineCache.chapter === state.chapterId ? timelineCache.data : null;
}

/**
 * 读取本章时间轴：先用已有产物，缺失或强制重建时再调用 build。
 * @param {boolean} force 是否强制重建
 * @returns {Promise<object|null>} 时间轴数据
 */
async function loadTimeline(force = false) {
  if (!state.path || !state.chapterId) return null;
  if (!force && timelineCache.chapter === state.chapterId && timelineCache.data) {
    return timelineCache.data;
  }
  if (!force) {
    try {
      const data = await getJSON(`/api/project/timeline/${encodeURIComponent(state.chapterId)}`, {
        path: state.path,
      });
      timelineCache = { chapter: state.chapterId, data };
      return data;
    } catch (error) {
      if (error.status !== 404) throw error;
    }
  }
  const data = await postJSON('/api/ui/timeline/build', {
    path: state.path,
    chapter: state.chapterId,
  });
  timelineCache = { chapter: state.chapterId, data };
  return data;
}

/** 轨道中文名 */
const TRACK_LABEL = { voice: '配音', sfx: '音效', music: '音乐', video: '视频', image: '画面' };

// ---------------- 录音视图 ----------------

/** 录音视图的统计（待录 = 待配 + 已过期） */
function recordCounts() {
  const source = state.counts || {};
  const ready = Number(source.ready || 0);
  const pending = Number(source.pending || 0);
  const stale = Number(source.stale || 0);
  const failed = Number(source.failed || 0);
  return { ready, pending, stale, failed, pendingAll: pending + stale, total: ready + pending + stale + failed };
}

/** 按录音筛选过滤台词行 */
function recordRows() {
  return state.lines.filter((row) => {
    if (ui.record.filter === 'pending') return row.state === 'pending' || row.state === 'stale';
    if (ui.record.filter === 'ready') return row.state === 'ready';
    if (ui.record.filter === 'failed') return row.state === 'failed';
    return true;
  });
}

/** 单条录音行 HTML */
function recordRowHTML(row) {
  const audio = audioOf(row);
  const duration = row.duration_ms ? fmtDuration(row.duration_ms) : '';
  const playing = ui.record.playing === row.line_id;
  const busy = ui.record.busy === row.line_id;
  return `
    <div class="rec-row ${playing ? 'playing' : ''}" data-vact="rec-row" data-rec-line="${esc(row.line_id)}"
         data-tone="${toneFor(row.speaker_id)}">
      <span class="rec-no mono">${esc(row.ordinal ?? row.index ?? '')}</span>
      <span class="rec-spk">
        <b>${esc(row.speaker || '未知')}</b>
        <span>${esc(row.kind === 'narration' ? '旁白' : row.kind === 'inner_monologue' ? '内心独白' : '对白')}${
          row.scene_ordinal ? ` · 第 ${row.scene_ordinal} 场` : ''
        }</span>
      </span>
      <span class="rec-text" title="${esc(row.text || '')}">${esc(row.text || '')}</span>
      <span class="rec-wave">
        ${
          audio
            ? `<span class="rec-wave-canvas" data-audio="${esc(audio)}"></span>`
            : '<span class="rec-wave-empty">未录音</span>'
        }
        <span class="rec-dur mono">${esc(duration || '—')}</span>
      </span>
      <span class="st-row ${stateClass(row.state)}">${STATE_LABEL[row.state] || row.state}</span>
      <span class="rec-act">
        <button class="btn icon sm" data-vact="rec-play" ${audio ? '' : 'disabled'}
                title="${audio ? (playing ? '暂停试听' : '试听这一行') : '还没有音频'}">
          ${icon(playing ? 'pause' : 'play', 12)}
        </button>
        <button class="btn icon sm" data-vact="rec-record" title="用麦克风录这一行">${icon('mic', 12)}</button>
        <button class="btn sm" data-vact="rec-rerun" ${busy ? 'disabled' : ''}>
          ${busy ? '重录中…' : '重录'}
        </button>
        <button class="btn icon sm" data-vact="rec-menu" title="更多">${icon('more', 14)}</button>
      </span>
    </div>`;
}

/** 录音视图主体 HTML */
function recordInnerHTML() {
  const c = recordCounts();
  const rows = recordRows();
  const chips = [
    ['all', '全部', c.total],
    ['pending', '待录', c.pendingAll],
    ['ready', '已录', c.ready],
    ['failed', '失败', c.failed],
  ];
  // 按场景分组：切换场景时插入一条场景分隔
  let lastScene = null;
  const body = rows
    .map((row) => {
      let head = '';
      if (row.scene_ordinal && row.scene_ordinal !== lastScene) {
        lastScene = row.scene_ordinal;
        head = `<div class="rec-scene">第 ${row.scene_ordinal} 场</div>`;
      }
      return head + recordRowHTML(row);
    })
    .join('');
  return `
    <div class="ed-head">
      <div style="min-width:0">
        <div class="et">配音画本</div>
        <div class="em">${esc(chapterLabel())} · ${c.pendingAll} 待录 · ${c.ready} 已录${
          c.failed ? ` · ${c.failed} 失败` : ''
        }</div>
      </div>
      <div class="right">
        <button class="btn sm" data-act="batch-plan">${icon('mic', 13)} 补齐缺失配音</button>
        <button class="btn icon sm" data-vact="rec-refresh" title="刷新录音状态">${icon('refresh')}</button>
      </div>
    </div>
    <div class="ed-filters">
      ${chips
        .map(
          ([key, label, num]) => `
        <button class="chip ${ui.record.filter === key ? 'on' : ''}" data-vact="rec-filter" data-filter="${key}">
          ${label}<span class="n">${num}</span>
        </button>`
        )
        .join('')}
      <div class="grow"></div>
      <span class="hint">点击行试听；重录会保留旧版本，可回版本记录找回</span>
    </div>
    <div class="vw-body">
      ${
        rows.length
          ? body
          : `<div class="empty">${icon('mic', 22)}<b>${
              state.lines.length ? '当前筛选下没有行' : '本章还没有台词行'
            }</b><span>${state.lines.length ? '切回「全部」或先补齐配音' : '先在画本编辑里导入正文并切分台词'}</span></div>`
      }
    </div>
    ${recordTransportHTML()}
  `;
}

/** 当前录音段：优先用户选中的行，否则第一条未录行 */
function currentRecordLine() {
  if (ui.record.current) {
    const hit = state.lines.find((row) => row.line_id === ui.record.current);
    if (hit) return hit;
  }
  return state.lines.find((row) => row.state !== 'ready') || state.lines[0] || null;
}

/** 底部录音 transport（与参考同结构：计时 / 上一段 / 开始录音 / 下一段 / 设备 / 录音配置） */
function recordTransportHTML() {
  const row = currentRecordLine();
  const duration = row?.duration_ms ? fmtDuration(row.duration_ms) : '00:00.000';
  return `
    <div class="rec-transport">
      <span class="rec-clock mono" id="rec-clock">${duration}</span>
      <button class="btn icon" data-vact="rec-prev" title="播放 / 录制上一段" ${state.lines.length ? '' : 'disabled'}>
        ${icon('prev')}
      </button>
      <button class="btn primary" data-vact="rec-start" ${row ? '' : 'disabled'} title="录制当前段">
        ${icon('mic', 13)} 开始录音
      </button>
      <button class="btn icon" data-vact="rec-next" title="播放 / 录制下一段" ${state.lines.length ? '' : 'disabled'}>
        ${icon('next')}
      </button>
      <div class="grow"></div>
      <span class="rec-device-label">设备</span>
      <select class="select" id="rec-device" title="选择录音设备">
        <option value="">Default - 外置麦克风</option>
      </select>
      <button class="btn sm" data-vact="rec-config" title="录音配置">录音配置</button>
    </div>
    <div class="rec-hint">
      小象提示：录音时按 ↓ 自动提交当前段并连续录制下一段
      <button class="link" data-vact="rec-shortcuts">查看更多快捷键</button>
    </div>`;
}

/** 填充录音设备下拉（授权前只能拿到设备数量，标签用默认文案） */
async function loadRecordDevices() {
  const select = document.getElementById('rec-device');
  if (!select || !navigator.mediaDevices?.enumerateDevices) return;
  try {
    const devices = await navigator.mediaDevices.enumerateDevices();
    const inputs = devices.filter((device) => device.kind === 'audioinput');
    const current = ui.record.deviceId;
    select.innerHTML = [
      '<option value="">Default - 外置麦克风</option>',
      ...inputs.map(
        (device, index) =>
          `<option value="${esc(device.deviceId)}" ${device.deviceId === current ? 'selected' : ''}>${esc(
            device.label || `麦克风 ${index + 1}`
          )}</option>`
      ),
    ].join('');
    if (current) select.value = current;
  } catch {
    // 没有权限时保留下拉默认项
  }
}

/** 录音配置：设备与「录完自动续录」 */
function openRecordConfig() {
  const devices = [...(document.getElementById('rec-device')?.options || [])].map(
    (option) => `<option value="${esc(option.value)}" ${option.value === ui.record.deviceId ? 'selected' : ''}>${esc(option.textContent)}</option>`
  );
  const mask = openDialog({
    title: '录音配置',
    body: `
      <div class="field"><label>录音设备</label>
        <select class="select" id="rc-device">${devices.join('')}</select></div>
      <label class="check-line" style="margin-top:12px">
        <input type="checkbox" id="rc-auto" ${ui.record.autoNext ? 'checked' : ''} />
        录完自动跳到下一段并继续录制
      </label>
      <div class="hint" style="margin-top:10px">设备切换在下次开始录音时生效；浏览器首次授权后才会显示设备名称。</div>`,
    foot: `<div class="right"><button class="btn" data-dlg-close>取消</button>
      <button class="btn primary" id="rc-save">保存</button></div>`,
  });
  mask.querySelector('#rc-save')?.addEventListener('click', () => {
    ui.record.deviceId = mask.querySelector('#rc-device')?.value || '';
    ui.record.autoNext = Boolean(mask.querySelector('#rc-auto')?.checked);
    const select = document.getElementById('rec-device');
    if (select) select.value = ui.record.deviceId;
    closeDialog();
    toast('录音配置已保存', 'ok', 1500);
  });
}

/** 快捷键说明 */
function openRecordShortcuts() {
  openDialog({
    title: '录音快捷键',
    body: `
      <div class="summary-line"><span>开始 / 停止录音</span><span class="right mono">空格（弹窗内）</span></div>
      <div class="summary-line"><span>提交当前段并录下一段</span><span class="right mono">↓</span></div>
      <div class="summary-line"><span>重录当前段</span><span class="right mono">R</span></div>
      <div class="hint" style="margin-top:10px">参考站的连续录制依赖 ↓ 提交；本地同样在录音弹窗内支持。</div>`,
    foot: `<div class="right"><button class="btn" data-dlg-close>关闭</button></div>`,
  });
}

/**
 * 选中一段并播放（没有音频时就打开录音弹窗）。
 * @param {number} step -1 上一段 / 1 下一段
 */
function moveRecordLine(step) {
  const rows = state.lines;
  if (!rows.length) return;
  const current = currentRecordLine();
  const index = Math.max(0, rows.findIndex((row) => row.line_id === current?.line_id));
  const next = rows[Math.max(0, Math.min(rows.length - 1, index + step))];
  ui.record.current = next.line_id;
  paintView();
  const node = document.querySelector(`[data-rec-line="${CSS.escape(next.line_id)}"]`);
  node?.scrollIntoView({ block: 'center' });
  if (audioOf(next)) playRecordLine(next.line_id);
  else openRecordDialog(next.line_id);
}

/** 开始录音当前段 */
function startRecordLine() {
  const row = currentRecordLine();
  if (!row) return toast('本章还没有台词行', 'warn');
  ui.record.current = row.line_id;
  openRecordDialog(row.line_id);
}

/** 波形峰值缓存：asset → 归一化峰值数组 */
const peakCache = new Map();
/** 录音行波形观察器：滚动到可见再解码，避免整章一次性解码 */
let waveObserver = null;
/** 录音 transport 计时器 */
let recordClock = 0;

/**
 * 读取音频峰值（首次解码后缓存）。
 * @param {string} asset 相对工程根目录的音频路径
 * @returns {Promise<number[]>} 0–1 的峰值数组
 */
async function peaksOf(asset) {
  if (peakCache.has(asset)) return peakCache.get(asset);
  const response = await fetch(fileUrl(asset, state.path));
  const buffer = await response.arrayBuffer();
  const context = new (window.AudioContext || window.webkitAudioContext)();
  const decoded = await context.decodeAudioData(buffer);
  const samples = decoded.getChannelData(0);
  const bars = 56;
  const step = Math.max(1, Math.floor(samples.length / bars));
  const peaks = Array.from({ length: bars }, (_, index) => {
    let max = 0;
    for (let cursor = index * step; cursor < (index + 1) * step && cursor < samples.length; cursor += 24) {
      const value = Math.abs(samples[cursor]);
      if (value > max) max = value;
    }
    return max;
  });
  context.close?.();
  peakCache.set(asset, peaks);
  return peaks;
}

/**
 * 把峰值画进录音行（真实波形，不伪造）。
 * @param {HTMLElement} node 承载波形的容器
 */
async function drawWave(node) {
  const asset = node.dataset.audio;
  if (!asset || node.dataset.drawn === '1') return;
  try {
    const peaks = await peaksOf(asset);
    const width = 100;
    const height = 28;
    node.innerHTML = `<svg width="100%" height="${height}" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none">
      ${peaks
        .map((peak, index) => {
          const bar = Math.max(1, peak * height * 0.9);
          return `<rect x="${(index * (width / peaks.length)).toFixed(2)}" y="${((height - bar) / 2).toFixed(2)}"
            width="${Math.max(0.6, width / peaks.length - 0.7).toFixed(2)}" height="${bar.toFixed(2)}"
            fill="currentColor" opacity="0.6" />`;
        })
        .join('')}
    </svg>`;
    node.dataset.drawn = '1';
  } catch {
    // 解码失败时保留空白，不伪造波形
  }
}

/**
 * 给录音行波形挂延迟绘制。
 * @param {HTMLElement} root 当前页面根节点
 */
function observeWaves(root) {
  waveObserver?.disconnect();
  waveObserver = null;
  const nodes = root.querySelectorAll('.rec-wave-canvas[data-audio]');
  if (!nodes.length) return;
  const scroller = root.querySelector('.vw-body');
  waveObserver = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        waveObserver?.unobserve(entry.target);
        drawWave(entry.target);
      }
    },
    { root: scroller || null, rootMargin: '120px' }
  );
  nodes.forEach((node) => waveObserver.observe(node));
}

/** 停止录音视图的单行试听 */
function stopRecordPlayback() {
  ui.record.audio?.pause();
  ui.record.audio = null;
  ui.record.playing = '';
}

/**
 * 试听 / 暂停一行录音。
 * @param {string} lineId 台词行 ID
 */
function playRecordLine(lineId) {
  const row = state.lines.find((item) => item.line_id === lineId);
  if (!row) return;
  const rel = audioOf(row);
  if (!rel) return toast('这一行还没有录音', 'warn');
  if (ui.record.playing === lineId) {
    stopRecordPlayback();
    paintView();
    return;
  }
  stopRecordPlayback();
  const audio = new Audio(fileUrl(rel, state.path));
  ui.record.audio = audio;
  ui.record.playing = lineId;
  audio.onended = () => {
    ui.record.playing = '';
    ui.record.audio = null;
    paintView();
  };
  audio.onerror = () => {
    toast('音频加载失败', 'bad');
    ui.record.playing = '';
    ui.record.audio = null;
    paintView();
  };
  audio.play().catch((error) => {
    toast(`播放失败：${error.message}`, 'bad');
    ui.record.playing = '';
    paintView();
  });
  paintView();
}

/**
 * 单行重录：revision 递增、旧版本保留，完成后刷新本章数据。
 * @param {string} lineId 台词行 ID
 */
async function rerunRecordLine(lineId) {
  if (ui.record.busy) return toast('已有重录任务在进行，请等它结束', 'warn');
  ui.record.busy = lineId;
  paintView();
  toast('正在重录这一行，可以继续浏览', 'info');
  try {
    const result = await postJSON('/api/ui/line/rerun', {
      path: state.path,
      chapter: state.chapterId,
      line: lineId,
    });
    timelineCache = { chapter: '', data: null };
    toast(`已重录 r${result?.revision ?? '?'}，旧版本仍可试听`, 'ok');
  } catch (error) {
    toast(error.message, 'bad');
  } finally {
    ui.record.busy = '';
    await hooks.refresh();
    paintView();
    paintPanel();
  }
}

/** 行尾更多菜单：试听 / 真人录音 / 重录 / 版本记录 / 到画本编辑中定位 */
function openRecordMenu(lineId, anchor) {
  const row = state.lines.find((item) => item.line_id === lineId);
  if (!row) return;
  const playable = Boolean(audioOf(row));
  openMenu(
    anchor,
    [
      { key: 'play', label: '试听这一行', disabled: !playable },
      { key: 'record', label: '真人录音…' },
      { key: 'rerun', label: '重录这一行' },
      { key: 'versions', label: '录音版本记录' },
      { sep: true },
      { key: 'script', label: '到画本编辑中定位' },
    ],
    (key) => {
      if (key === 'play') playRecordLine(lineId);
      else if (key === 'record') openRecordDialog(lineId);
      else if (key === 'rerun') rerunRecordLine(lineId);
      else if (key === 'versions') openVersions(row);
      else if (key === 'script') hooks.openInScript(lineId);
    }
  );
}

/** 把秒数格式化成 mm:ss */
function clockText(seconds) {
  const value = Math.max(0, Math.floor(seconds));
  return `${String(Math.floor(value / 60)).padStart(2, '0')}:${String(value % 60).padStart(2, '0')}`;
}

/**
 * 从 MediaRecorder 的 mimeType 推断上传文件后缀。
 * @param {string} mimeType 浏览器实际使用的容器
 * @returns {string} webm / mp4 / ogg / wav
 */
function recordExt(mimeType) {
  if (mimeType.includes('mp4')) return 'mp4';
  if (mimeType.includes('ogg')) return 'ogg';
  if (mimeType.includes('wav')) return 'wav';
  return 'webm';
}

/**
 * 真人录音弹窗：麦克风录制 → 试听 → 采用为该行新 take。
 * 上传后由后端转成 48kHz 单声道 wav，与 TTS 产物同一结构，可直接被时间轴/导出消费。
 * @param {string} lineId 台词行 ID
 */
function openRecordDialog(lineId) {
  const row = state.lines.find((item) => item.line_id === lineId);
  if (!row) return;
  if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === 'undefined') {
    return toast('当前浏览器不支持麦克风录制', 'warn');
  }
  const mask = openDialog({
    title: '真人录音',
    body: `
      <div class="rec-dlg">
        <div class="rec-dlg-line">
          <b>${esc(row.speaker || '未知')}</b>
          <span>${esc(row.text || '')}</span>
        </div>
        <div class="rec-dlg-stage">
          <span class="rec-dlg-dot" id="rec-dot"></span>
          <span id="rec-status">准备就绪，点「开始录音」</span>
          <span class="mono" id="rec-time">00:00</span>
        </div>
        <div class="rec-dlg-acts">
          <button class="btn primary" id="rec-start">${icon('mic', 13)} 开始录音</button>
          <button class="btn" id="rec-stop" disabled>${icon('stop', 12)} 停止</button>
          <button class="btn" id="rec-redo" disabled>重新录</button>
          <button class="btn primary" id="rec-use" disabled>采用并上传</button>
        </div>
        <audio id="rec-preview" controls style="display:none;width:100%;margin-top:12px"></audio>
        <div class="hint" style="margin-top:10px">
          录音会作为这一行的新版本上传，旧 take（含 TTS）保留在「录音版本记录」里，可随时切回。
        </div>
      </div>`,
    foot: `<div class="right"><button class="btn" data-dlg-close>取消</button></div>`,
  });

  const dot = mask.querySelector('#rec-dot');
  const status = mask.querySelector('#rec-status');
  const timeLabel = mask.querySelector('#rec-time');
  const startBtn = mask.querySelector('#rec-start');
  const stopBtn = mask.querySelector('#rec-stop');
  const redoBtn = mask.querySelector('#rec-redo');
  const useBtn = mask.querySelector('#rec-use');
  const preview = mask.querySelector('#rec-preview');

  let stream = null;
  let recorder = null;
  let chunks = [];
  let blob = null;
  let mimeType = '';
  let startedAt = 0;
  let timer = 0;
  let uploading = false;
  /** ↓ 提交：停止后自动采用并续录下一段 */
  let pendingUse = false;

  /** 释放麦克风与定时器，弹窗被关掉时也要调用 */
  const cleanup = () => {
    window.clearInterval(timer);
    timer = 0;
    ui.record.startedAt = 0;
    document.removeEventListener('keydown', onKey);
    if (recorder && recorder.state !== 'inactive') {
      try {
        recorder.stop();
      } catch {
        // 已停止
      }
    }
    stream?.getTracks().forEach((track) => track.stop());
    stream = null;
    recorder = null;
  };

  /**
   * 弹窗内快捷键：↓ 提交并续录，空格开始/停止，R 重录。
   * @param {KeyboardEvent} event 键盘事件
   */
  const onKey = (event) => {
    if (!document.body.contains(mask)) return;
    if (event.target.closest?.('input, textarea, select, [contenteditable="true"]')) return;
    if (event.key === 'ArrowDown') {
      event.preventDefault();
      if (recorder && recorder.state !== 'inactive') {
        pendingUse = true;
        stopBtn.click();
      } else if (blob) {
        useBtn.click();
      }
      return;
    }
    if (event.key === ' ') {
      event.preventDefault();
      if (recorder && recorder.state !== 'inactive') stopBtn.click();
      else if (!blob) startBtn.click();
      return;
    }
    if (event.key.toLowerCase() === 'r' && !recorder && blob) redoBtn.click();
  };
  document.addEventListener('keydown', onKey);

  /** 刷新按钮可用状态 */
  const syncButtons = () => {
    startBtn.disabled = Boolean(recorder) || uploading;
    stopBtn.disabled = !recorder;
    redoBtn.disabled = !blob || uploading;
    useBtn.disabled = !blob || uploading;
  };

  startBtn.addEventListener('click', async () => {
    const constraints = ui.record.deviceId
      ? { audio: { deviceId: { exact: ui.record.deviceId } } }
      : { audio: true };
    try {
      stream = await navigator.mediaDevices.getUserMedia(constraints);
    } catch (error) {
      status.textContent = `无法打开麦克风：${error.message}`;
      return;
    }
    const candidates = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4', 'audio/ogg;codecs=opus'];
    mimeType = candidates.find((item) => MediaRecorder.isTypeSupported(item)) || '';
    recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
    chunks = [];
    blob = null;
    preview.style.display = 'none';
    preview.removeAttribute('src');
    recorder.addEventListener('dataavailable', (event) => {
      if (event.data && event.data.size) chunks.push(event.data);
    });
    recorder.addEventListener('stop', () => {
      window.clearInterval(timer);
      timer = 0;
      blob = new Blob(chunks, { type: mimeType || 'audio/webm' });
      preview.src = URL.createObjectURL(blob);
      preview.style.display = 'block';
      status.textContent = '已录完，可以试听；满意就「采用并上传」';
      dot.classList.remove('on');
      startBtn.disabled = false;
      stopBtn.disabled = true;
      redoBtn.disabled = false;
      useBtn.disabled = false;
      redoBtn.dataset.ready = '1';
      if (pendingUse) {
        pendingUse = false;
        useBtn.click();
      }
    });
    recorder.start();
    startedAt = Date.now();
    ui.record.startedAt = startedAt;
    status.textContent = '正在录音…';
    dot.classList.add('on');
    timeLabel.textContent = '00:00';
    syncButtons();
    timer = window.setInterval(() => {
      if (!document.body.contains(mask)) {
        cleanup();
        return;
      }
      timeLabel.textContent = clockText((Date.now() - startedAt) / 1000);
    }, 200);
  });

  stopBtn.addEventListener('click', () => {
    if (recorder && recorder.state !== 'inactive') recorder.stop();
    stream?.getTracks().forEach((track) => track.stop());
    stream = null;
    status.textContent = '正在整理录音…';
    dot.classList.remove('on');
  });

  redoBtn.addEventListener('click', () => {
    blob = null;
    preview.style.display = 'none';
    preview.removeAttribute('src');
    timeLabel.textContent = '00:00';
    status.textContent = '准备就绪，点「开始录音」';
    syncButtons();
  });

  useBtn.addEventListener('click', async () => {
    if (!blob) return;
    uploading = true;
    status.textContent = '正在上传并转码…';
    syncButtons();
    const query = new URLSearchParams({
      path: state.path,
      chapter: state.chapterId,
      line: lineId,
      ext: recordExt(blob.type || mimeType),
    });
    try {
      const response = await fetch(`/api/ui/line/audio/upload?${query.toString()}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/octet-stream' },
        body: blob,
      });
      const text = await response.text();
      let payload = null;
      try {
        payload = text ? JSON.parse(text) : null;
      } catch {
        payload = null;
      }
      if (!response.ok) {
        const detail = payload?.detail;
        throw new Error(typeof detail === 'string' ? detail : detail?.message || `上传失败（HTTP ${response.status}）`);
      }
      cleanup();
      closeDialog();
      timelineCache = { chapter: '', data: null };
      toast(`已采用真人录音 r${payload?.revision ?? '?'}`, 'ok');
      await hooks.refresh();
      paintView();
      paintPanel();
      // 连续录制：采用后自动打开下一段待录行
      if (ui.record.autoNext) {
        const index = state.lines.findIndex((row) => row.line_id === lineId);
        const next = state.lines.slice(index + 1).find((row) => row.state !== 'ready');
        if (next) {
          ui.record.current = next.line_id;
          window.setTimeout(() => openRecordDialog(next.line_id), 400);
        }
      }
    } catch (error) {
      uploading = false;
      status.textContent = `上传失败：${error.message}`;
      syncButtons();
    }
  });

  mask.addEventListener('click', (event) => {
    if (event.target.closest('[data-dlg-close]')) cleanup();
  });
}

/**
 * 录音版本记录：列出该行历史 take，可试听 / 设为生效 / 删除。
 * @param {object} row 台词行
 */
async function openVersions(row) {
  const mask = openDialog({
    title: '录音版本记录',
    body: `<div id="ver-body"><div class="empty">${icon('refresh', 18)}<span>正在读取版本…</span></div></div>`,
    foot: `<div class="right"><button class="btn" data-dlg-close>关闭</button></div>`,
  });
  const body = mask.querySelector('#ver-body');

  /**
   * 读取并渲染版本列表。
   * @param {object} [payload] 已有接口结果；不传则重新请求
   */
  const render = (payload) => {
    const versions = payload?.versions || [];
    if (!versions.length) {
      body.innerHTML = `<div class="empty"><span>这一行还没有历史录音</span></div>`;
      return;
    }
    body.innerHTML = `
      ${versions
        .map(
          (item) => `
        <div class="summary-line">
          <span>r${item.revision}${item.active ? '（当前生效）' : ''}</span>
          <span class="right" style="display:flex;gap:6px;align-items:center">
            <span class="tag" style="color:${item.source === 'human' ? 'var(--ok)' : 'var(--text-3)'}">${
              item.source === 'human' ? '真人' : 'TTS'
            }</span>
            <span class="mono" style="color:var(--text-4)">${fmtDuration(item.duration_ms || 0)}</span>
            <button class="btn sm" data-ver="${esc(item.path)}">试听</button>
            ${
              item.active
                ? ''
                : `<button class="btn sm" data-ver-activate="${item.revision}">设为生效</button>`
            }
            <button class="btn sm danger" data-ver-delete="${item.revision}">删除</button>
          </span>
        </div>`
        )
        .join('')}
      <div class="hint" style="margin-top:10px">「设为生效」只切换这一行使用哪一版音频，不重新合成；若该版录于正文修改前，切换后可能显示「已过期」。</div>`;

    body.querySelectorAll('[data-ver]').forEach((button) => {
      button.addEventListener('click', () => {
        const audio = new Audio(fileUrl(button.dataset.ver, state.path));
        audio.play().catch((error) => toast(`播放失败：${error.message}`, 'bad'));
      });
    });
    body.querySelectorAll('[data-ver-activate]').forEach((button) => {
      button.addEventListener('click', async () => {
        button.disabled = true;
        try {
          const result = await postJSON('/api/ui/line/audio/activate', {
            path: state.path,
            chapter: state.chapterId,
            line: row.line_id,
            revision: Number(button.dataset.verActivate),
          });
          timelineCache = { chapter: '', data: null };
          toast(`已切换为 r${button.dataset.verActivate} 生效`, 'ok');
          render(result);
          await hooks.refresh();
          paintView();
          paintPanel();
        } catch (error) {
          button.disabled = false;
          toast(error.message, 'bad');
        }
      });
    });
    body.querySelectorAll('[data-ver-delete]').forEach((button) => {
      button.addEventListener('click', async () => {
        const revision = Number(button.dataset.verDelete);
        const hint = button.closest('.summary-line')?.textContent?.includes('当前生效')
          ? `r${revision} 正在生效，删除后这一行会退回「待配」。确定删除？`
          : `确定删除 r${revision} 这一版录音？文件删除后不可恢复。`;
        if (!window.confirm(hint)) return;
        button.disabled = true;
        try {
          const result = await postJSON('/api/ui/line/audio/version/delete', {
            path: state.path,
            chapter: state.chapterId,
            line: row.line_id,
            revision,
          });
          timelineCache = { chapter: '', data: null };
          toast(`已删除 r${revision}`, 'ok');
          render(result);
          await hooks.refresh();
          paintView();
          paintPanel();
        } catch (error) {
          button.disabled = false;
          toast(error.message, 'bad');
        }
      });
    });
  };

  try {
    const payload = await getJSON('/api/ui/line/versions', {
      path: state.path,
      chapter: state.chapterId,
      line: row.line_id,
    });
    render(payload);
  } catch (error) {
    body.innerHTML = `<div class="dlg-note bad">${esc(error.message)}</div>`;
  }
}

/** 录音右栏：配音任务 / 角色档案（参考右栏两个页签） */
function recordPanelHTML() {
  const c = recordCounts();
  const task = state.task || {};
  const ratio = c.total ? Math.round((c.ready / c.total) * 100) : 0;
  const failedRows = (task.rows || []).slice(0, 5);
  const tab = ui.record.panelTab || 'tasks';
  const characterRows = state.characters
    .map((item) => {
      const bound = item.voice_binding || {};
      const voiceName = bound.voice_name || bound.voice_id || '未绑定音色';
      const lines = state.lines.filter((row) => row.speaker_id === item.id).length;
      return `<div class="fail-row">
        <span class="t" title="${esc(item.name)} · ${esc(voiceName)}">
          <b style="color:var(--text)">${esc(item.name)}</b>
          <span style="display:block;color:var(--text-4);font-size:11px">${esc(voiceName)} · ${lines} 行</span>
        </span>
      </div>`;
    })
    .join('');
  return `
    <div class="sr-head" style="align-items:center">
      <div style="display:flex;gap:2px">
        <button class="chip sm ${tab === 'tasks' ? 'on' : ''}" data-vact="rec-panel" data-panel="tasks"
          title="查看本章配音任务与进度">配音任务</button>
        <button class="chip sm ${tab === 'characters' ? 'on' : ''}" data-vact="rec-panel" data-panel="characters"
          title="查看本章出场角色与绑定音色">角色档案</button>
      </div>
      <span class="badge ${task.running ? 'run' : 'mute'}">${
        task.running ? `生成中 ${task.done || 0}/${task.total || 0}` : c.total ? `${c.ready}/${c.total}` : '0/0'
      }</span>
      <div class="right">
        <button class="btn icon sm" data-act="toggle-task" title="收起">${icon('next')}</button>
      </div>
    </div>
    <div class="sr-body">
      ${
        tab === 'characters'
          ? `<div class="sr-sec">
               <h4>全部配音角色（${state.characters.length}）</h4>
               ${characterRows || '<div class="hint">本章还没有识别到角色。</div>'}
               <a class="btn" style="margin-top:9px" href="#/p/${encodeURIComponent(state.path)}/voices">${icon(
                 'audio',
                 13
               )} 音色管理</a>
             </div>`
          : `<div class="sr-sec">
        <h4>录音进度</h4>
        <div class="bar"><span style="width:${ratio}%"></span></div>
        <div style="display:flex;justify-content:space-between;font-size:11px;color:var(--text-3)">
          <span>已录 ${c.ready} / ${c.total} 行</span><span class="mono">${ratio}%</span>
        </div>
        <div class="stat-grid" style="margin-top:12px">
          <div class="stat warn"><div class="k">待录</div><div class="v">${c.pendingAll}</div></div>
          <div class="stat ok"><div class="k">已录</div><div class="v">${c.ready}</div></div>
          <div class="stat ${c.failed ? 'bad' : ''}"><div class="k">失败</div><div class="v">${c.failed}</div></div>
          <div class="stat"><div class="k">总行数</div><div class="v">${c.total}</div></div>
        </div>
      </div>
      <div class="sr-sec">
        <h4>录音操作</h4>
        <div class="sr-actions">
          <button class="btn primary" data-act="batch-plan">${icon('mic')} 补齐缺失配音…</button>
          <button class="btn" data-act="regenerate-chapter">重新生成本章</button>
          ${task.running ? `<button class="btn danger" data-act="stop">${icon('stop')} 停止录音</button>` : ''}
        </div>
        <div class="hint" style="margin-top:9px">重录只影响该行；批量补齐会跳过已有音频的行。</div>
      </div>
      ${
        failedRows.length
          ? `<div class="sr-sec"><h4>失败行（最近 ${failedRows.length}）</h4>
              <div class="fail-list">${failedRows
                .map(
                  (row) => `<div class="fail-row">
                    <span class="t" title="${esc(row.message || row.code || '')}">${esc(
                    row.message || row.code || row.line_id || ''
                  )}</span>
                    <button class="btn sm" data-retry-line="${esc(row.line_id || '')}">重录</button>
                  </div>`
                )
                .join('')}</div>
            </div>`
          : ''
      }
      <div class="sr-sec">
        <h4>音色</h4>
        <div class="hint">录音使用角色当前绑定的音色；缺音色会导致该行生成失败。</div>
        <a class="btn" style="margin-top:9px" href="#/p/${encodeURIComponent(state.path)}/voices">${icon(
          'audio',
          13
        )} 音色管理</a>
      </div>`
      }
    </div>`;
}

// ---------------- 后期制作视图 ----------------

/** 根据总时长与缩放选刻度步长（可见刻度控制在 12 个以内） */
function tickStep(total, zoom) {
  const visible = Math.max(1, total / zoom);
  const steps = [1000, 2000, 5000, 10000, 15000, 30000, 60000, 120000, 300000];
  return steps.find((step) => visible / step <= 12) || steps[steps.length - 1];
}

/** 轨道音量（dB），未设置时为 0 */
function trackVolume(kind) {
  return Number(ui.post.volumes.get(kind) || 0);
}

/**
 * 时间轴轨道展开：配音按角色拆成独立轨（与参考「配音：角色名」一致），
 * 音效 / 音乐 / 视频 / 画面保持原轨。轨道键用于静音、独奏与音量。
 * @param {object} data 时间轴
 * @returns {Array<object>} 轨道列表（含 key）
 */
function postTracks(data) {
  const groups = new Map();
  const others = [];
  for (const track of data?.tracks || []) {
    if (track.kind !== 'voice') {
      others.push({ ...track, key: track.kind });
      continue;
    }
    for (const item of track.items || []) {
      const row = state.lines.find((entry) => entry.line_id === item.source_id);
      const speakerId = row?.speaker_id || 'chr_unknown';
      const speaker = row?.speaker || '未知';
      if (!groups.has(speakerId)) {
        groups.set(speakerId, {
          key: `voice:${speakerId}`,
          kind: 'voice',
          name: `配音：${speaker}`,
          speaker,
          items: [],
        });
      }
      groups.get(speakerId).items.push(item);
    }
  }
  // 旁白排第一，其余保持出现顺序
  const voices = [...groups.values()].sort((a, b) => {
    const an = a.key === 'voice:chr_narrator' ? 0 : 1;
    const bn = b.key === 'voice:chr_narrator' ? 0 : 1;
    return an - bn;
  });
  return [...voices, ...others];
}

/**
 * 轨道是否可听：有独奏时只听独奏轨，否则受静音开关控制。
 * @param {string} key 轨道键
 * @returns {boolean} 可听
 */
function trackAudible(key) {
  return ui.post.soloed.size ? ui.post.soloed.has(key) : !ui.post.muted.has(key);
}

/**
 * 设置轨道音量并即时应用到正在播放的音频。
 * @param {string} kind 轨道类型
 * @param {number} value 音量（dB，-24–6）
 */
function setTrackVolume(kind, value) {
  const db = Math.max(-24, Math.min(6, Number(value) || 0));
  ui.post.volumes.set(kind, db);
  document.querySelectorAll(`[data-vact="post-volume"][data-track="${CSS.escape(kind)}"]`).forEach((node) => {
    if (Number(node.value) !== db) node.value = String(db);
    const label = node.parentElement?.querySelector('.vol-val');
    if (label) label.textContent = `${db > 0 ? '+' : ''}${db}dB`;
  });
  ui.post.audios.forEach((audio) => {
    if (audio.dataset.track !== kind) return;
    const gain = Number(audio.dataset.gain || 0) + db;
    audio.volume = Math.max(0, Math.min(1, 10 ** (gain / 20)));
  });
}

/** 一条轨道 HTML */
function postTrackHTML(track, total) {
  const kind = track.kind || 'voice';
  const key = track.key || kind;
  const items = track.items || [];
  const muted = ui.post.muted.has(key);
  const soloed = ui.post.soloed.has(key);
  const volume = trackVolume(key);
  const selected = ui.post.selected;
  const blocks = items
    .map((item, index) => {
      const start = Number(item.start_ms || 0);
      const end = Number(item.end_ms || start);
      const left = ((start / total) * 100).toFixed(3);
      const widthPct = Math.max(0.25, ((end - start) / total) * 100);
      const width = widthPct.toFixed(3);
      const hasAsset = Boolean(item.asset_id);
      const gain = Number(item.gain_db ?? 0);
      const fadeIn = Number(item.fade_in_ms || 0);
      const fadeOut = Number(item.fade_out_ms || 0);
      const title = `${item.label || kind} · ${fmtDuration(start)}–${fmtDuration(end)} · ${gain}dB${
        fadeIn || fadeOut ? ` · 淡入 ${fadeIn}ms / 淡出 ${fadeOut}ms` : ''
      }${hasAsset ? ' · 拖动可平移' : ' · 估算时长（未合成）'}`;
      // 过窄的块不画文字与边框，避免整轨变成噪点带
      const tiny = widthPct < 0.9;
      const hit = selected && start < selected.end && end > selected.start;
      const picked = ui.post.selectedItem && ui.post.selectedItem.lane === key && ui.post.selectedItem.index === index;
      return `<button class="post-item k-${esc(kind)} ${hasAsset ? '' : 'est'} ${tiny ? 'tiny' : ''} ${
        hit ? 'sel' : ''
      } ${picked ? 'pick' : ''}"
        style="left:${left}%;width:${width}%" data-vact="post-item" data-asset="${esc(item.asset_id || '')}"
        data-start="${start}" data-end="${end}" data-idx="${index}" title="${esc(title)}">${
          tiny ? '' : `<span>${esc(item.label || '')}</span>`
        }</button>`;
    })
    .join('');
  return `
    <div class="post-track">
      <div class="post-tname">
        <div class="tname-row"><b>${esc(track.name || TRACK_LABEL[kind] || kind)}</b>
          <button class="post-ctl solo ${soloed ? 'on' : ''}" data-vact="post-solo" data-track="${esc(key)}"
            title="${soloed ? '取消独奏' : '独奏这条轨道'}">S</button>
          <button class="post-ctl mute ${muted ? 'on' : ''}" data-vact="post-mute" data-track="${esc(key)}"
            title="${muted ? '取消静音' : '静音这条轨道'}">M</button>
        </div>
        <div class="post-vol-row" title="轨道音量（-24dB ~ +6dB）">
          <input class="post-vol" type="range" min="-24" max="6" step="1" value="${volume}"
            data-vact="post-volume" data-track="${esc(key)}" />
          <span class="mono vol-val">${volume > 0 ? '+' : ''}${volume}dB</span>
        </div>
        <span>${items.length} 段${muted ? ' · 已静音' : ''}${soloed ? ' · 独奏中' : ''}</span>
      </div>
      <div class="post-lane" data-lane="${esc(key)}">${blocks}</div>
    </div>`;
}

/** 时间轴片段（按界面轨道键 + 索引取），用于拖动与淡入淡出编辑 */
function timelineItem(lane, index) {
  const data = timeline();
  if (!data) return null;
  // 配音轨按角色拆成多条，界面索引对应拆分后的 items，必须走 postTracks 还原
  const track = postTracks(data).find((item) => item.key === lane || item.kind === lane);
  return track?.items?.[index] || null;
}

/** 选中片段的编辑条：淡入 / 淡出（毫秒），并提示可拖动平移 */
function postClipInspectorHTML() {
  const pick = ui.post.selectedItem;
  const item = pick ? timelineItem(pick.lane, pick.index) : null;
  if (!pick || !item) return '';
  return `
    <span class="mono" style="color:var(--text-3);max-width:150px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
      片段 ${esc(item.label || pick.lane)}
    </span>
    <label class="hint" style="display:flex;align-items:center;gap:4px">淡入
      <input class="input" type="number" min="0" step="10" style="width:72px;height:24px"
        data-post-fade="in" value="${Number(item.fade_in_ms || 0)}" />ms</label>
    <label class="hint" style="display:flex;align-items:center;gap:4px">淡出
      <input class="input" type="number" min="0" step="10" style="width:72px;height:24px"
        data-post-fade="out" value="${Number(item.fade_out_ms || 0)}" />ms</label>
    <button class="btn sm" data-vact="post-clear-pick" title="取消选中片段">完成</button>`;
}

/** 保存时间轴（片段平移 / 淡入淡出后立即落盘，混音与导出直接复用） */
async function saveTimeline() {
  const data = timeline();
  if (!data) return;
  try {
    await postJSON('/api/ui/timeline/save', {
      path: state.path,
      chapter: state.chapterId,
      timeline: data,
    });
  } catch (error) {
    toast(error.message, 'bad', 4000);
  }
}

/**
 * 修改选中片段的淡入/淡出；两者之和超过片段时长时压缩另一端。
 * @param {'in'|'out'} side 修改哪一端
 * @param {number} value 新的毫秒数
 */
function applyClipFade(side, value) {
  const pick = ui.post.selectedItem;
  const item = pick ? timelineItem(pick.lane, pick.index) : null;
  if (!item) return;
  const length = Math.max(1, Number(item.end_ms || 0) - Number(item.start_ms || 0));
  let next = Math.max(0, Math.round(Number(value) || 0));
  const other = side === 'in' ? Number(item.fade_out_ms || 0) : Number(item.fade_in_ms || 0);
  if (next + other > length) next = Math.max(0, length - other);
  if (side === 'in') item.fade_in_ms = next;
  else item.fade_out_ms = next;
  saveTimeline();
  paintView();
}

/** 拖动片段：按住音频块左右平移，松开后落盘 */
let postDrag = null;

/** 指针按下：记录片段与轨道宽度，后续 move 里换算毫秒 */
function onDocPointerDown(event) {
  if (state.view !== 'post') return;
  // 新的按下视为新一轮交互，清掉上一次拖动留下的点击抑制
  ui.post.dragSuppress = false;
  const el = event.target.closest?.('.post-item');
  if (!el) return;
  const lane = el.closest('.post-lane');
  const rect = lane?.getBoundingClientRect();
  const total = Number(timeline()?.duration_ms || 0);
  if (!lane || !rect?.width || !total) return;
  postDrag = {
    el,
    lane: lane.dataset.lane || 'voice',
    total,
    width: rect.width,
    startX: event.clientX,
    startMs: Number(el.dataset.start) || 0,
    endMs: Number(el.dataset.end) || 0,
    moved: false,
    newStart: null,
  };
}

/** 指针移动：按像素差换算毫秒，吸附到 50ms 网格 */
function onDocPointerMove(event) {
  if (!postDrag) return;
  const dx = event.clientX - postDrag.startX;
  if (!postDrag.moved && Math.abs(dx) < 4) return;
  postDrag.moved = true;
  const length = postDrag.endMs - postDrag.startMs;
  const delta = (dx / postDrag.width) * postDrag.total;
  let start = Math.max(0, Math.min(postDrag.total - length, postDrag.startMs + delta));
  if (ui.post.snap) start = Math.round(start / 50) * 50;
  postDrag.newStart = start;
  postDrag.el.style.left = `${((start / postDrag.total) * 100).toFixed(3)}%`;
}

/** 指针松开：把新起点写回时间轴并保存（拖动过则抑制随后的试听点击） */
function onDocPointerUp() {
  const drag = postDrag;
  postDrag = null;
  if (!drag || !drag.moved) return;
  const index = Number(drag.el.dataset.idx);
  const item = timelineItem(drag.lane, index);
  if (!item) return;
  const start = Number(drag.newStart ?? drag.startMs);
  const length = drag.endMs - drag.startMs;
  item.start_ms = Math.round(start);
  item.end_ms = Math.round(start + length);
  ui.post.dragSuppress = true;
  ui.post.selectedItem = { lane: drag.lane, index };
  ui.post.cursor = item.start_ms;
  saveTimeline();
  paintView();
}

/** 后期制作视图主体 HTML */
function postInnerHTML() {
  const data = timeline();
  if (!data) {
    return `<div class="ed-head">
        <div><div class="et">后期制作</div><div class="em">正在读取本章时间轴…</div></div>
      </div>
      <div class="vw-body"><div class="empty">${icon('refresh', 20)}<span>正在读取时间轴…</span></div></div>`;
  }
  const total = Math.max(1, Number(data.duration_ms || 0));
  const tracks = postTracks(data);
  const warnings = (data.warnings || []).length;
  const counts = tracks.map(
    (track) => `${track.speaker || TRACK_LABEL[track.kind] || track.kind} ${(track.items || []).length}`
  );
  const step = tickStep(total, ui.post.zoom);
  const ticks = [];
  for (let at = 0; at <= total; at += step) {
    ticks.push(`<span style="left:${((at / total) * 100).toFixed(3)}%">${fmtClock(at)}</span>`);
  }
  return `
    <div class="ed-head">
      <div style="min-width:0">
        <div class="et">后期制作</div>
        <div class="em">${fmtClock(total)} · ${esc(counts.join(' · '))}${warnings ? ` · 警告 ${warnings}` : ''}</div>
      </div>
      <div class="right">
        <button class="btn sm" data-vact="post-play" title="空格键播放 / 暂停">${icon(
          ui.post.playing ? 'pause' : 'play',
          13
        )} ${ui.post.playing ? '暂停' : '播放'}</button>
        <button class="btn sm" data-vact="post-markers" title="查看时间轴标记">${icon('list', 13)} 查看标记${
          (data.markers || []).length ? `<span class="badge mute">${(data.markers || []).length}</span>` : ''
        }</button>
        <button class="chip ${ui.post.snap ? 'on' : ''}" data-vact="post-snap"
          title="吸附到时间刻度与音频块边界">吸附</button>
        <button class="chip ${ui.post.linked ? 'on' : ''}" data-vact="post-link"
          title="点选音频块时联动同时间范围的其它轨道">联动</button>
        <button class="btn icon sm" data-vact="post-zoom-out" title="水平缩小">−</button>
        <span class="mono" style="font-size:11px;color:var(--text-3)">${Math.round(ui.post.zoom * 100)}%</span>
        <button class="btn icon sm" data-vact="post-zoom-in" title="水平放大">＋</button>
        <button class="btn sm" data-vact="post-build">${icon('refresh', 13)} 重建</button>
      </div>
    </div>
    <div class="post-scroll">
      <div class="post-inner" style="width:${ui.post.zoom * 100}%">
        <div class="post-ruler" data-vact="post-seek">${ticks.join('')}</div>
        <div class="post-tracks">${tracks.map((track) => postTrackHTML(track, total)).join('')}</div>
        <div class="post-playhead" id="post-playhead"></div>
      </div>
    </div>
    <div class="post-foot">
      <span class="mono" id="post-cur">${fmtDuration(ui.post.cursor)}</span>
      <span class="mono" style="color:var(--text-4)">/ ${fmtDuration(total)}</span>
      ${postClipInspectorHTML()}
      <div style="flex:1"></div>
      <span class="hint">${
        ui.post.soloed.size
          ? `独奏：${[...ui.post.soloed].map((kind) => TRACK_LABEL[kind] || kind).join('、')}；`
          : ui.post.muted.size
            ? `已静音：${[...ui.post.muted].map((kind) => TRACK_LABEL[kind] || kind).join('、')}；`
            : ''
      }S 独奏 · M 静音 · 滑杆调轨道音量；吸附 ${ui.post.snap ? '开' : '关'} · 联动 ${
        ui.post.linked ? '开' : '关'
      }</span>
      ${
        warnings
          ? `<button class="btn sm" data-vact="post-warnings">${icon('warn', 13)} 警告 ${warnings}</button>`
          : ''
      }
    </div>`;
}

/** 更新后期播放头与时间文案（不重画整个时间轴） */
function updatePlayhead() {
  const data = timeline();
  if (!data) return;
  const total = Math.max(1, Number(data.duration_ms || 0));
  const node = document.getElementById('post-playhead');
  if (node) {
    // 播放头按第一条轨道的实际起点与宽度定位，左侧轨道名列不参与时间刻度
    const lane = document.querySelector('.post-lane');
    const inner = document.querySelector('.post-inner');
    if (lane && inner) {
      const laneRect = lane.getBoundingClientRect();
      const innerRect = inner.getBoundingClientRect();
      const left = laneRect.left - innerRect.left + (ui.post.cursor / total) * laneRect.width;
      node.style.left = `${left}px`;
    }
  }
  const cur = document.getElementById('post-cur');
  if (cur) cur.textContent = fmtDuration(ui.post.cursor);
}

/** 停止后期试听；keepCursor 为真时保留播放头位置 */
function stopPostPlayback(keepCursor = true) {
  ui.post.playing = false;
  cancelAnimationFrame(ui.post.raf);
  ui.post.raf = 0;
  ui.post.timers.forEach((timer) => window.clearTimeout(timer));
  ui.post.timers = [];
  ui.post.audios.forEach((audio) => {
    try {
      audio.pause();
    } catch {
      // 忽略已释放的音频
    }
  });
  ui.post.audios = [];
  if (!keepCursor) ui.post.cursor = 0;
  updatePlayhead();
}

/** 后期播放 / 暂停 */
function togglePostPlayback() {
  const data = timeline();
  if (!data) return toast('时间轴还在读取，请稍候', 'warn');
  if (ui.post.playing) {
    stopPostPlayback(true);
    paintView();
    return;
  }
  const total = Math.max(1, Number(data.duration_ms || 0));
  let base = ui.post.cursor;
  if (base >= total - 100) base = 0;
  const items = [];
  for (const track of postTracks(data)) {
    if (!trackAudible(track.key)) continue;
    for (const item of track.items || []) {
      if (item.asset_id) items.push({ item, kind: track.key });
    }
  }
  if (!items.length) {
    return toast('当前没有可播放的音频：配音尚未生成，或所有带音频的轨道都被静音 / 未独奏', 'warn');
  }
  ui.post.base = base;
  ui.post.startedAt = performance.now();
  ui.post.playing = true;
  for (const { item, kind } of items) {
    const start = Number(item.start_ms || 0);
    const end = Number(item.end_ms || start);
    if (end <= base) continue;
    const delay = Math.max(0, start - base);
    const offset = Math.max(0, base - start) / 1000;
    const audio = new Audio(fileUrl(item.asset_id, state.path));
    const gain = Number(item.gain_db ?? 0);
    // 轨道音量叠加到条目自身增益上（预览与导出各自独立）
    const totalGain = gain + trackVolume(kind);
    audio.dataset.gain = String(gain);
    audio.dataset.track = kind;
    audio.volume = Math.max(0, Math.min(1, 10 ** (totalGain / 20)));
    if (offset > 0) {
      audio.addEventListener('loadedmetadata', () => {
        try {
          audio.currentTime = offset;
        } catch {
          // 部分格式不支持定位，从头播放即可
        }
      });
    }
    ui.post.timers.push(
      window.setTimeout(() => {
        audio.play().catch(() => {});
      }, delay)
    );
    ui.post.audios.push(audio);
  }
  const tick = () => {
    if (!ui.post.playing) return;
    const at = ui.post.base + (performance.now() - ui.post.startedAt);
    ui.post.cursor = Math.min(total, at);
    updatePlayhead();
    if (at >= total) {
      stopPostPlayback(false);
      paintView();
      return;
    }
    ui.post.raf = requestAnimationFrame(tick);
  };
  ui.post.raf = requestAnimationFrame(tick);
  paintView();
}

/**
 * 吸附定位：在阈值内取最近的刻度或音频块边界。
 * @param {number} at 目标毫秒
 * @param {number} width 轨道像素宽度
 * @param {number} total 总时长毫秒
 * @returns {number} 吸附后的毫秒
 */
function snapMs(at, width, total) {
  const threshold = (10 / Math.max(1, width)) * total; // 10px 对应的毫秒
  const step = tickStep(total, ui.post.zoom);
  const candidates = new Set();
  for (let tick = 0; tick <= total; tick += step) candidates.add(tick);
  for (const track of timeline()?.tracks || []) {
    for (const item of track.items || []) {
      candidates.add(Number(item.start_ms || 0));
      candidates.add(Number(item.end_ms || 0));
    }
  }
  let best = at;
  let distance = threshold;
  for (const value of candidates) {
    const diff = Math.abs(value - at);
    if (diff < distance) {
      distance = diff;
      best = value;
    }
  }
  return best;
}

/** 点击刻度定位播放头；播放中先停 */
function seekPost(event) {
  const data = timeline();
  const ruler = event.target.closest('.post-ruler');
  if (!data || !ruler) return;
  const rect = ruler.getBoundingClientRect();
  const ratio = Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width));
  const total = Number(data.duration_ms || 0);
  stopPostPlayback(true);
  ui.post.cursor = ui.post.snap ? Math.round(snapMs(ratio * total, rect.width, total)) : Math.round(ratio * total);
  updatePlayhead();
  paintView();
}

/** 单击音频块试听（估算块给出提示）；联动时选中同时间范围的块 */
function previewPostItem(el) {
  const asset = el?.dataset.asset || '';
  const start = Number(el?.dataset.start) || 0;
  // 先记录选中片段：即使没有音频，也能在底栏改淡入淡出
  const lane = el?.closest('.post-lane')?.dataset.lane || 'voice';
  ui.post.selectedItem = { lane, index: Number(el?.dataset.idx) };
  if (!asset) {
    paintView();
    return toast('该段还没有音频（估算时长），先在录音视图生成配音', 'warn');
  }
  stopPostPlayback(true);
  ui.post.cursor = start;
  // 联动：按块自身的起止选中所有时间上重叠的块
  if (ui.post.linked) {
    const lane = el.closest('.post-lane');
    const block = el;
    const laneRect = lane?.getBoundingClientRect();
    const blockRect = block.getBoundingClientRect();
    const total = Number(timeline()?.duration_ms || 0);
    if (laneRect && laneRect.width > 0) {
      const startMs = Math.round(((blockRect.left - laneRect.left) / laneRect.width) * total);
      const endMs = Math.round(((blockRect.right - laneRect.left) / laneRect.width) * total);
      ui.post.selected = { start: startMs, end: endMs };
    }
  } else {
    ui.post.selected = null;
  }
  updatePlayhead();
  const audio = new Audio(fileUrl(asset, state.path));
  const kind = el.closest('.post-lane')?.dataset.lane || 'voice';
  audio.dataset.track = kind;
  audio.dataset.gain = '0';
  audio.volume = Math.max(0, Math.min(1, 10 ** (trackVolume(kind) / 20)));
  ui.post.audios.push(audio);
  audio.onended = () => {
    ui.post.audios = ui.post.audios.filter((item) => item !== audio);
  };
  audio.play().catch((error) => toast(`播放失败：${error.message}`, 'bad'));
  paintView();
}

/** 查看时间轴标记：列出场景与标记点，点击定位 */
function openMarkers() {
  const data = timeline();
  if (!data) return toast('时间轴还在读取，请稍候', 'warn');
  const markers = [...(data.markers || [])].sort((a, b) => Number(a.at_ms) - Number(b.at_ms));
  const mask = openDialog({
    title: `查看标记（${markers.length}）`,
    wide: true,
    body: `
      <div class="summary-line"><span>总时长</span><span class="right mono">${fmtDuration(
        data.duration_ms || 0
      )}</span></div>
      ${
        markers.length
          ? markers
              .map(
                (marker, index) => `<button class="mk-row" data-mk-at="${Number(marker.at_ms) || 0}">
                  <span class="no mono">${String(index + 1).padStart(2, '0')}</span>
                  <span class="at mono">${fmtDuration(marker.at_ms || 0)}</span>
                  <span class="label">${esc(marker.label || '标记')}</span>
                </button>`
              )
              .join('')
          : '<div class="empty"><span>本章还没有标记；重建时间轴后按场景生成</span></div>'
      }
      <div class="hint" style="margin-top:10px">标记按场景起点生成，点击可把播放头定位到该时刻。</div>`,
    foot: `<div class="right"><button class="btn" data-mk-close>关闭</button></div>`,
  });
  mask.addEventListener('click', (event) => {
    if (event.target.closest('[data-mk-close]')) return closeDialog();
    const row = event.target.closest('[data-mk-at]');
    if (!row) return;
    stopPostPlayback(true);
    ui.post.cursor = Number(row.dataset.mkAt) || 0;
    updatePlayhead();
    closeDialog();
    paintView();
  });
}

/** 缩放时间轴：重建刻度与块宽度，保留播放头 */
function zoomPost(direction) {
  stopPostPlayback(true);
  const levels = [1, 1.5, 2, 3, 4, 6, 8];
  const index = levels.findIndex((level) => level >= ui.post.zoom - 0.01);
  const next = Math.max(0, Math.min(levels.length - 1, (index < 0 ? 0 : index) + direction));
  ui.post.zoom = levels[next];
  paintView();
}

/** 重建时间轴（重画后同步右栏信息） */
async function rebuildTimeline() {
  if (timelineLoading === state.chapterId) return;
  timelineLoading = state.chapterId;
  paintView();
  try {
    const data = await loadTimeline(true);
    timelineCache = { chapter: state.chapterId, data };
    const voice = (data.tracks || []).find((track) => track.kind === 'voice');
    toast(`时间轴已重建：${fmtClock(data.duration_ms || 0)} · 配音 ${(voice?.items || []).length} 段`, 'ok');
  } catch (error) {
    toast(error.message, 'bad');
  } finally {
    timelineLoading = '';
    paintView();
    paintPanel();
  }
}

/** 警告按原因归组（后端 warning 形如「行 ID 原因」） */
function warningGroups(data) {
  const map = new Map();
  for (const raw of data?.warnings || []) {
    const text = String(raw).replace(/^\S+\s+/, '');
    map.set(text, (map.get(text) || 0) + 1);
  }
  return [...map.entries()].sort((a, b) => b[1] - a[1]);
}

/** 时间轴警告清单弹窗 */
function openWarnings() {
  const data = timeline();
  if (!data) return;
  const groups = warningGroups(data);
  const mask = openDialog({
    title: '时间轴警告',
    wide: true,
    body: `
      <div class="summary-line"><span>本章警告</span><span class="right">${(data.warnings || []).length} 条</span></div>
      ${groups
        .map(
          ([text, count]) => `<div class="summary-line"><span>${esc(text)}</span>
            <span class="right mono" style="color:var(--warn)">${count} 条</span></div>`
        )
        .join('') || '<div class="empty"><span>没有警告</span></div>'}
      <div class="hint" style="margin-top:10px">「使用估算时长」表示该行还没有合成音频，时间轴按文本字数估算占位。</div>`,
    foot: `<div class="right"><button class="btn" data-dlg-close>关闭</button></div>`,
  });
  mask.querySelector('.dlg-body')?.scrollTo?.(0, 0);
}

/** 后期右栏：混音信息、轨道统计、警告 */
function postPanelHTML() {
  const data = timeline();
  if (!data) {
    return `
      <div class="sr-head"><span class="ct">混音信息</span>
        <div class="right"><button class="btn icon sm" data-act="toggle-task" title="收起">${icon('next')}</button></div>
      </div>
      <div class="sr-body"><div class="empty">${icon('refresh', 18)}<span>正在读取时间轴…</span></div></div>`;
  }
  const tracks = postTracks(data);
  const warnings = warningGroups(data);
  return `
    <div class="sr-head">
      <span class="ct">混音信息</span>
      <span class="badge mute">r${esc(data.revision ?? '—')}</span>
      <div class="right">
        <button class="btn icon sm" data-act="toggle-task" title="收起">${icon('next')}</button>
      </div>
    </div>
    <div class="sr-body">
      <div class="sr-sec">
        <h4>本章时间轴</h4>
        <div class="summary-line"><span>总时长</span><span class="right mono">${fmtDuration(data.duration_ms || 0)}</span></div>
        <div class="summary-line"><span>采样率 / 声道</span><span class="right mono">${esc(
          data.sample_rate || '—'
        )} Hz · ${Number(data.channels) === 1 ? '单声道' : '立体声'}</span></div>
        <div class="summary-line"><span>构建时间</span><span class="right mono">${esc(
          String(data.built_at || '—').replace('T', ' ').replace('Z', '')
        )}</span></div>
      </div>
      <div class="sr-sec">
        <h4>分轨</h4>
        ${tracks
          .map(
            (track) => `<div class="summary-line"><span>${esc(
              track.name || TRACK_LABEL[track.kind] || track.kind
            )}</span><span class="right mono">${(track.items || []).length} 段</span></div>`
          )
          .join('')}
        <div class="hint" style="margin-top:8px">配音按角色分轨；S 独奏、M 静音，滑杆调整该轨音量（仅影响预览）。</div>
      </div>
      <div class="sr-sec">
        <h4>警告</h4>
        ${
          warnings.length
            ? warnings
                .slice(0, 5)
                .map(
                  ([text, count]) => `<div class="summary-line"><span class="t" style="max-width:70%">${esc(
                    text
                  )}</span><span class="right mono" style="color:var(--warn)">${count}</span></div>`
                )
                .join('')
            : '<div class="hint">没有警告</div>'
        }
        ${
          warnings.length > 5
            ? `<button class="btn sm" style="margin-top:8px" data-vact="post-warnings">查看全部 ${
                (data.warnings || []).length
              } 条</button>`
            : ''
        }
      </div>
      <div class="sr-sec">
        <h4>操作</h4>
        <div class="sr-actions">
          <button class="btn" data-vact="post-build">${icon('refresh')} 重建时间轴</button>
          <button class="btn" data-act="batch-plan">${icon('mic')} 补齐缺失配音…</button>
          <a class="btn" href="#/p/${encodeURIComponent(state.path)}/media">${icon('image')} 素材库</a>
        </div>
      </div>
    </div>`;
}

// ---------------- 审听与对轨视图 ----------------

/**
 * 计算本章对齐数据：优先用时间轴配音轨的起止时间，缺失时按台词顺序累计。
 * @returns {Array<{row:object,start:number,end:number,dur:number,estimated:boolean,flag:{text:string,cls:string,abnormal:boolean}}>}
 */
function reviewRows() {
  const data = timeline();
  const voice = (data?.tracks || []).find((track) => track.kind === 'voice');
  const itemMap = new Map();
  for (const item of voice?.items || []) itemMap.set(item.source_id, item);
  let cursor = 0;
  return state.lines.map((row) => {
    const item = itemMap.get(row.line_id);
    const start = item ? Number(item.start_ms || 0) : cursor;
    const dur = item ? Math.max(0, Number(item.end_ms || 0) - start) : Number(row.duration_ms || 0);
    const end = start + dur;
    cursor = end;
    const chars = charCount(row.text);
    const cps = dur > 0 ? chars / (dur / 1000) : 0;
    let flag = { text: '—', cls: 'mute', abnormal: false };
    if (!audioOf(row)) {
      flag = { text: '未录音', cls: 'mute', abnormal: false };
    } else if (cps > 8) {
      flag = { text: '语速偏快', cls: 'warn', abnormal: true };
    } else if (cps > 0 && cps < 1.6) {
      flag = { text: '语速偏慢', cls: 'warn', abnormal: true };
    }
    return { row, start, end, dur, estimated: Boolean(item?.estimated), flag };
  });
}

/** 本章是否全为 AI 配音（决定「无须智能审听」提示） */
function allAiChapter() {
  return !state.lines.some((row) => row.tts?.provider_id === 'human' || row.tts?.adapter === 'human');
}

/** 审听行（受「只看异常」过滤） */
function auditRows() {
  const all = reviewRows();
  return ui.review.onlyAbnormal ? all.filter((item) => item.flag.abnormal) : all;
}

/** 智能审听页签主体 HTML（画本编辑右栏） */
function auditBodyHTML() {
  const all = reviewRows();
  const rows = auditRows();
  const audioCount = all.filter((item) => audioOf(item.row)).length;
  const fast = all.filter((item) => item.flag.text === '语速偏快').length;
  const slow = all.filter((item) => item.flag.text === '语速偏慢').length;
  const list = rows
    .map((item) => {
      const playing = ui.review.currentLine === item.row.line_id;
      return `<div class="audit-row ${playing ? 'on' : ''}" data-rv-line="${esc(item.row.line_id)}">
        <span class="no mono">${esc(item.row.ordinal ?? '')}</span>
        <button class="btn icon sm" data-act="audit-play-one" data-line="${esc(item.row.line_id)}"
          ${audioOf(item.row) ? '' : 'disabled'} title="${audioOf(item.row) ? '试听这一行' : '这一行还没有录音'}">
          ${icon(playing ? 'pause' : 'play', 11)}
        </button>
        <span class="spk">${esc(item.row.speaker || '未知')}</span>
        <span class="text" title="${esc(item.row.text || '')}">${esc(item.row.text || '')}</span>
        <span class="flag ${item.flag.cls}">${item.flag.text}</span>
      </div>`;
    })
    .join('');
  return `
    <div class="sr-body" id="audit-body">
      <div class="sr-sec">
        <h4>本章审听</h4>
        <div class="audit-note">${
          allAiChapter() ? '本章内容均为 AI 配音，无须智能审听' : '本章包含真人录音，建议逐行试听确认'
        }</div>
        <div class="stat-grid">
          <div class="stat ok"><div class="k">可试听</div><div class="v">${audioCount}</div></div>
          <div class="stat"><div class="k">待录音</div><div class="v">${all.length - audioCount}</div></div>
          <div class="stat ${fast ? 'warn' : ''}"><div class="k">语速偏快</div><div class="v">${fast}</div></div>
          <div class="stat ${slow ? 'warn' : ''}"><div class="k">语速偏慢</div><div class="v">${slow}</div></div>
        </div>
      </div>
      <div class="sr-sec">
        <h4>操作</h4>
        <div class="sr-actions">
          <button class="btn primary" data-act="audit-play">${icon(
            ui.review.playing ? 'pause' : 'play'
          )} ${ui.review.playing ? '暂停审听' : '从头审听'}</button>
          <button class="btn" data-act="audit-only">${ui.review.onlyAbnormal ? '显示全部行' : '只看异常'}</button>
          <button class="btn" data-act="audit-build">${icon('refresh')} 重建时间轴</button>
        </div>
        <div class="hint" style="margin-top:9px">语速按「字数 ÷ 音频时长」判定：超过 8 字/秒为偏快，低于 1.6 字/秒为偏慢。</div>
      </div>
      <div class="sr-sec">
        <h4>逐章文本（${rows.length} 行）</h4>
        <div class="audit-list">${
          rows.length ? list : '<div class="hint">没有符合条件的行。</div>'
        }</div>
      </div>
    </div>`;
}

/** 画本编辑右栏「智能审听」页签（由 editor.js 拼进右栏） */
export function auditPanelHTML() {
  return auditBodyHTML();
}

/** 只重画智能审听主体，保留滚动位置 */
export function paintAuditPanel() {
  const node = document.getElementById('audit-body');
  if (!node) return;
  const top = node.scrollTop;
  node.outerHTML = auditBodyHTML();
  const after = document.getElementById('audit-body');
  if (after) after.scrollTop = top;
}

/** 审听播放时重画：画本编辑内只刷右栏，独立视图刷主区 */
function repaintAudit() {
  if (state.view === 'script') paintAuditPanel();
  else paintView();
}

/**
 * 智能审听页签的点击分发（由 editor.js 转进来）。
 * @param {string} act data-act 值
 * @param {HTMLElement} el 触发元素
 */
export function onAuditAction(act, el) {
  if (act === 'audit-only') {
    ui.review.onlyAbnormal = !ui.review.onlyAbnormal;
    paintAuditPanel();
    return;
  }
  if (act === 'audit-play') {
    playReview();
    return;
  }
  if (act === 'audit-play-one') {
    const lineId = el.dataset.line || '';
    const row = state.lines.find((item) => item.line_id === lineId);
    if (!row) return;
    if (ui.review.playing && ui.review.currentLine === lineId) {
      stopReviewPlayback();
      repaintAudit();
      return;
    }
    if (!audioOf(row)) return toast('这一行还没有录音，先到录音视图补齐', 'warn');
    if (ui.review.playing) stopReviewPlayback();
    playReview(lineId);
    return;
  }
  if (act === 'audit-build') {
    rebuildAuditTimeline();
  }
}

/** 智能审听里重建本章时间轴（不切换视图） */
async function rebuildAuditTimeline() {
  if (timelineLoading === state.chapterId) return;
  timelineLoading = state.chapterId;
  try {
    const data = await loadTimeline(true);
    timelineCache = { chapter: state.chapterId, data };
    toast(`时间轴已重建：${fmtClock(data.duration_ms || 0)}`, 'ok');
  } catch (error) {
    toast(error.message, 'bad');
  } finally {
    timelineLoading = '';
    paintAuditPanel();
  }
}

/**
 * 打开「智能审听」页签前确保本章时间轴已读取。
 * 读取失败时退回按台词顺序估算，不打断编辑器。
 */
export async function ensureAuditData() {
  if (timeline() || timelineLoading === state.chapterId) return;
  timelineLoading = state.chapterId;
  try {
    await loadTimeline(false);
  } catch {
    // 审听可退化到按台词顺序估算
  } finally {
    timelineLoading = '';
    paintAuditPanel();
  }
}

/** 停止审听队列 */
function stopReviewPlayback() {
  ui.review.audio?.pause();
  ui.review.audio = null;
  ui.review.playing = false;
  ui.review.queue = [];
  ui.review.index = -1;
  updateReviewButton();
}

/** 「从头审听」按钮图标与文案同步 */
function updateReviewButton() {
  const buttons = document.querySelectorAll('[data-act="audit-play"]');
  buttons.forEach((button) => {
    button.innerHTML = `${icon(ui.review.playing ? 'pause' : 'play', 13)} ${
      ui.review.playing ? '暂停审听' : '从头审听'
    }`;
  });
}

/** 高亮正在播放的行并滚动到可见区域 */
function markReviewActive(lineId) {
  document.querySelectorAll('.audit-row').forEach((node) => {
    const on = node.dataset.rvLine === lineId;
    node.classList.toggle('on', on);
    if (on) node.scrollIntoView({ block: 'nearest' });
  });
}

/**
 * 从指定行开始顺序审听（只播放已录好的行）。
 * @param {string} [fromLine] 起始台词行 ID；省略时从头开始
 */
function playReview(fromLine = '') {
  if (ui.review.playing) {
    stopReviewPlayback();
    repaintAudit();
    return;
  }
  const queue = reviewRows().filter((item) => audioOf(item.row));
  if (!queue.length) return toast('本章还没有已录好的台词，先到录音视图补齐配音', 'warn');
  let index = fromLine ? queue.findIndex((item) => item.row.line_id === fromLine) : 0;
  if (index < 0) index = 0;
  ui.review.queue = queue;
  ui.review.playing = true;
  playReviewAt(index);
}

/** 播放队列中的第 index 行，结束后自动接下一行 */
function playReviewAt(index) {
  const queue = ui.review.queue;
  const item = queue[index];
  if (!item) {
    ui.review.playing = false;
    ui.review.currentLine = '';
    ui.review.audio = null;
    updateReviewButton();
    repaintAudit();
    return;
  }
  ui.review.index = index;
  ui.review.currentLine = item.row.line_id;
  const audio = new Audio(fileUrl(audioOf(item.row), state.path));
  ui.review.audio = audio;
  audio.onended = () => playReviewAt(index + 1);
  audio.onerror = () => playReviewAt(index + 1);
  audio.play().catch(() => playReviewAt(index + 1));
  markReviewActive(item.row.line_id);
  updateReviewButton();
  repaintAudit();
}

// ---------------- 智能对轨视图 ----------------

/** 章节下拉选项（含「未选择」占位） */
function alignChapterOptions(selected, placeholder) {
  const rows = state.chapters
    .map(
      (chapter) =>
        `<option value="${esc(chapter.id)}" ${chapter.id === selected ? 'selected' : ''}>${esc(
          chapter.title || `第 ${chapter.index} 章`
        )}</option>`
    )
    .join('');
  return `<option value="">${esc(placeholder)}</option>${rows}`;
}

/** 任务状态文案 */
const ALIGN_STATUS = { ready: '已完成', failed: '失败', pending: '排队中' };

/** 单条对轨任务 HTML */
function alignTaskHTML(task) {
  const status = ALIGN_STATUS[task.status] || task.status || '未知';
  return `<div class="align-task">
    <span class="name" title="${esc(task.name || '')}">${esc(task.name || '未命名音频')}</span>
    <span class="range">${esc(task.range || '—')}</span>
    <span class="badge ${task.status === 'ready' ? 'ok' : task.status === 'failed' ? 'bad' : 'mute'}">${esc(status)}</span>
    <span class="msg" title="${esc(task.message || '')}">${esc(task.message || '')}</span>
    <button class="btn icon sm" data-vact="align-del" data-id="${esc(task.id)}" title="删除任务记录">${
      icon('x', 12)
    }</button>
  </div>`;
}

/** 智能对轨主区 HTML（上传页形态，与参考一致） */
function alignInnerHTML() {
  const tasks = ui.align.tasks;
  return `
    <div class="ed-head">
      <div style="min-width:0">
        <div class="et">智能对轨</div>
        <div class="em">上传整段播音，按静音切分后依次写入所选章节的台词行</div>
      </div>
      <div class="right">
        <button class="btn icon sm" data-vact="align-refresh" title="刷新任务列表">${icon('refresh')}</button>
      </div>
    </div>
    <div class="vw-body">
      <div class="align-form">
        <div class="field">
          <label>选择要对轨的播音</label>
          <div class="input static" title="本地版只有一个工程，播音固定为当前作品">${esc(
            state.info?.work?.title || '当前作品'
          )}</div>
        </div>
        <div class="align-row">
          <div class="field">
            <label>选择起始章节</label>
            <select class="select" id="align-start">${alignChapterOptions(ui.align.start, '请选择起始章节')}</select>
          </div>
          <div class="field">
            <label>选择结束章节</label>
            <select class="select" id="align-end">${alignChapterOptions(ui.align.end, '请选择结束章节')}</select>
          </div>
        </div>
        <div class="field">
          <label>上传音频（MP3 / WAV / M4A，不超过 100MB）</label>
          <div class="align-drop" data-vact="align-pick" title="点击选择音频文件">
            ${icon('export', 18)}
            <b>${esc(ui.align.file ? ui.align.file.name : '点击选择音频文件')}</b>
            <span>${
              ui.align.file
                ? `${(ui.align.file.size / 1024 / 1024).toFixed(1)} MB`
                : '支持 mp3 / wav / m4a，单个文件不超过 100MB'
            }</span>
          </div>
          <input type="file" id="align-file" accept=".mp3,.wav,.m4a,audio/mpeg,audio/wav,audio/mp4" hidden />
        </div>
        ${ui.align.error ? `<div class="dlg-note bad">${esc(ui.align.error)}</div>` : ''}
        <div class="align-actions">
          <button class="btn primary" data-vact="align-submit" ${ui.align.busy ? 'disabled' : ''}>
            ${icon(ui.align.busy ? 'refresh' : 'arrow', 13)} ${ui.align.busy ? '对轨中…' : '开始对轨'}
          </button>
          <span class="hint">按静音切分后与所选章节的台词行顺序对应；行数与切分数不一致时不会写入，可调整章节范围后重试。</span>
        </div>
      </div>
      <div class="align-list-head">
        <span class="ct">任务列表</span>
        <span class="badge mute">${tasks.length}</span>
      </div>
      <div class="align-list">
        ${
          tasks.length
            ? tasks.map(alignTaskHTML).join('')
            : `<div class="empty">${icon('arrow', 20)}<b>未发现对轨任务</b><span>上传一段播音后，任务会显示在这里</span></div>`
        }
      </div>
      <div class="align-help">
        <h4>使用说明</h4>
        <ol>
          <li>选择起始与结束章节，覆盖本次要写入的台词范围。</li>
          <li>上传整段播音（MP3 / WAV / M4A，≤100MB），建议一人一段、按章节顺序录制。</li>
          <li>系统按静音切分音频，并依次写入每一行作为真人录音版本；已有版本会保留。</li>
          <li>切分数量与台词行数不一致时不会写入，可缩小章节范围后重试。</li>
        </ol>
      </div>
    </div>`;
}

/** 智能对轨右栏 HTML */
function alignPanelHTML() {
  const tasks = ui.align.tasks;
  const ready = tasks.filter((item) => item.status === 'ready').length;
  const failed = tasks.filter((item) => item.status === 'failed').length;
  return `
    <div class="sr-head">
      <span class="ct">对轨任务</span>
      <span class="badge ${failed ? 'bad' : 'mute'}">${tasks.length}</span>
      <div class="right">
        <button class="btn icon sm" data-act="toggle-task" title="收起">${icon('next')}</button>
      </div>
    </div>
    <div class="sr-body">
      <div class="sr-sec">
        <h4>任务概况</h4>
        <div class="stat-grid">
          <div class="stat ok"><div class="k">已完成</div><div class="v">${ready}</div></div>
          <div class="stat ${failed ? 'bad' : ''}"><div class="k">失败</div><div class="v">${failed}</div></div>
        </div>
        <div class="hint" style="margin-top:10px">对轨会把切分出的音频写成台词行的真人录音版本，可在录音视图试听与切换版本。</div>
      </div>
      <div class="sr-sec">
        <h4>操作</h4>
        <div class="sr-actions">
          <button class="btn" data-vact="align-refresh">${icon('refresh')} 刷新任务列表</button>
          <button class="btn" data-act="set-view" data-view="record">${icon('mic')} 去录音视图</button>
        </div>
      </div>
    </div>`;
}

/** 读取对轨任务列表并重画 */
async function loadAlignTasks() {
  if (!state.path) return;
  ui.align.loading = true;
  try {
    const payload = await getJSON('/api/ui/align/tasks', { path: state.path });
    ui.align.tasks = payload?.tasks || [];
    ui.align.error = '';
  } catch (error) {
    ui.align.error = error.message;
  } finally {
    ui.align.loading = false;
    paintView();
  }
}

/** 校验并记录用户选择的音频文件 */
function pickAlignFile(file) {
  if (!file) return;
  const suffix = (file.name.split('.').pop() || '').toLowerCase();
  if (!['mp3', 'wav', 'm4a'].includes(suffix)) {
    ui.align.error = '只支持 MP3 / WAV / M4A 格式';
    paintView();
    return;
  }
  if (file.size > 100 * 1024 * 1024) {
    ui.align.error = '音频超过 100MB，请压缩或分段上传';
    paintView();
    return;
  }
  ui.align.file = file;
  ui.align.error = '';
  paintView();
}

/** 提交对轨任务（multipart 上传） */
async function submitAlign() {
  if (ui.align.busy) return;
  const start = document.getElementById('align-start')?.value || '';
  const end = document.getElementById('align-end')?.value || '';
  ui.align.start = start;
  ui.align.end = end;
  if (!start || !end) {
    ui.align.error = '请先选择起始与结束章节';
    paintView();
    return;
  }
  if (!ui.align.file) {
    ui.align.error = '请先选择要上传的音频文件';
    paintView();
    return;
  }
  ui.align.busy = true;
  ui.align.error = '';
  paintView();
  const query = new URLSearchParams({
    path: state.path,
    start_chapter: start,
    end_chapter: end,
    filename: ui.align.file.name,
  });
  try {
    const response = await fetch(`/api/ui/align/upload?${query.toString()}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/octet-stream' },
      body: ui.align.file,
    });
    const payload = await response.json().catch(() => null);
    if (!response.ok) {
      const detail = payload?.detail;
      throw new Error(typeof detail === 'string' ? detail : detail?.message || '对轨失败');
    }
    ui.align.file = null;
    const task = payload?.task || {};
    toast(
      task.status === 'ready'
        ? `对轨完成：${task.lines || 0} 行已写入真人录音`
        : `对轨未写入：${task.message || '切分数量与台词行数不一致'}`,
      task.status === 'ready' ? 'ok' : 'warn'
    );
    await loadAlignTasks();
  } catch (error) {
    ui.align.error = error.message;
    toast(error.message, 'bad');
    paintView();
  } finally {
    ui.align.busy = false;
    paintView();
    paintPanel();
    hooks.refresh();
  }
}

/** 删除一条对轨任务记录 */
async function deleteAlignTask(id) {
  try {
    await postJSON('/api/ui/align/tasks/delete', { path: state.path, id });
    await loadAlignTasks();
    paintPanel();
  } catch (error) {
    toast(error.message, 'bad');
  }
}

/**
 * 智能对轨视图的点击分发。
 * @param {string} act data-vact 值
 * @param {HTMLElement} el 触发元素
 */
function onAlignAction(act, el) {
  if (act === 'align-pick') {
    document.getElementById('align-file')?.click();
    return;
  }
  if (act === 'align-refresh') {
    loadAlignTasks();
    return;
  }
  if (act === 'align-submit') {
    submitAlign();
    return;
  }
  if (act === 'align-del') {
    deleteAlignTask(el.dataset.id || '');
  }
}

// ---------------- 对外渲染与生命周期 ----------------

/**
 * 主区内容（不含 .editor 外壳，外壳由 editor.js 生成）。
 * @param {'script'|'record'|'post'|'align'} view 视图键
 * @returns {string} HTML
 */
export function viewMainHTML(view) {
  if (view === 'record') return recordInnerHTML();
  if (view === 'post') return postInnerHTML();
  if (view === 'align') return alignInnerHTML();
  return '';
}

/**
 * 右栏内容。
 * @param {'record'|'post'|'align'} view 视图键
 * @returns {string} HTML
 */
export function viewPanelHTML(view) {
  if (view === 'record') return recordPanelHTML();
  if (view === 'post') return postPanelHTML();
  if (view === 'align') return alignPanelHTML();
  return '';
}

/** 重画主区（录音 / 审听保留纵向滚动，后期保留横向滚动） */
function paintView() {
  const node = document.getElementById('vw-main');
  if (!node) return;
  if (state.view === 'post') {
    const scroll = node.querySelector('.post-scroll');
    const left = scroll ? scroll.scrollLeft : 0;
    node.innerHTML = postInnerHTML();
    const after = node.querySelector('.post-scroll');
    if (after) after.scrollLeft = left;
    requestAnimationFrame(updatePlayhead);
    return;
  }
  const body = node.querySelector('.vw-body');
  const top = body ? body.scrollTop : 0;
  node.innerHTML = state.view === 'record' ? recordInnerHTML() : alignInnerHTML();
  const after = node.querySelector('.vw-body');
  if (after) after.scrollTop = top;
  if (state.view === 'record') observeWaves(node);
}

/** 重画右栏 */
function paintPanel() {
  const panel = document.getElementById('task-panel');
  if (!panel) return;
  panel.innerHTML = viewPanelHTML(state.view);
}

/**
 * 数据刷新后供 editor.js 调用（轮询 / 撤销 / 批次完成后同步视图）。
 * 画本编辑由 editor 自己重画，这里只处理制作视图与智能对轨。
 */
export function refreshActiveView() {
  if (state.view === 'record') {
    paintView();
    paintPanel();
  } else if (state.view === 'post') {
    paintPanel();
  } else if (state.view === 'align') {
    loadAlignTasks().then(() => paintPanel());
  }
}

/** 视图事件绑定标记与监听器引用，离开页面时统一解绑 */
let mounted = false;

/**
 * 点击分发：所有视图动作都走 data-vact，避免和画本编辑的 data-act 冲突。
 * @param {MouseEvent} event 点击事件
 */
function onDocClick(event) {
  const el = event.target.closest('[data-vact]');
  if (!el) return;
  const act = el.dataset.vact;
  if (state.view === 'record') {
    if (act === 'rec-filter') {
      ui.record.filter = el.dataset.filter || 'all';
      paintView();
      return;
    }
    if (act === 'rec-panel') {
      ui.record.panelTab = el.dataset.panel === 'characters' ? 'characters' : 'tasks';
      paintView();
      paintPanel();
      return;
    }
    if (act === 'rec-row' && !event.target.closest('button')) {
      ui.record.current = el.dataset.recLine || '';
      playRecordLine(el.dataset.recLine);
      return;
    }
    if (act === 'rec-start') {
      startRecordLine();
      return;
    }
    if (act === 'rec-prev') {
      moveRecordLine(-1);
      return;
    }
    if (act === 'rec-next') {
      moveRecordLine(1);
      return;
    }
    if (act === 'rec-config') {
      openRecordConfig();
      return;
    }
    if (act === 'rec-shortcuts') {
      openRecordShortcuts();
      return;
    }
    if (act === 'rec-play') {
      playRecordLine(el.dataset.recLine || el.closest('[data-rec-line]')?.dataset.recLine || '');
      return;
    }
    if (act === 'rec-record') {
      openRecordDialog(el.closest('[data-rec-line]')?.dataset.recLine || '');
      return;
    }
    if (act === 'rec-rerun') {
      rerunRecordLine(el.closest('[data-rec-line]')?.dataset.recLine || '');
      return;
    }
    if (act === 'rec-menu') {
      openRecordMenu(el.closest('[data-rec-line]')?.dataset.recLine || '', el);
      return;
    }
    if (act === 'rec-refresh') {
      hooks.refresh().then(() => {
        paintView();
        paintPanel();
      });
      return;
    }
    if (act === 'rec-versions') {
      const lineId = el.closest('[data-rec-line]')?.dataset.recLine;
      const row = state.lines.find((item) => item.line_id === lineId);
      if (row) openVersions(row);
      return;
    }
    return;
  }
  if (state.view === 'post') {
    if (act === 'post-play') return togglePostPlayback();
    if (act === 'post-zoom-in') return zoomPost(1);
    if (act === 'post-zoom-out') return zoomPost(-1);
    if (act === 'post-build') return rebuildTimeline();
    if (act === 'post-seek') return seekPost(event);
    if (act === 'post-markers') return openMarkers();
    if (act === 'post-snap') {
      ui.post.snap = !ui.post.snap;
      paintView();
      toast(`吸附已${ui.post.snap ? '开启' : '关闭'}`, 'info', 1200);
      return;
    }
    if (act === 'post-link') {
      ui.post.linked = !ui.post.linked;
      if (!ui.post.linked) ui.post.selected = null;
      paintView();
      toast(`联动已${ui.post.linked ? '开启' : '关闭'}`, 'info', 1200);
      return;
    }
    if (act === 'post-mute') {
      const kind = el.dataset.track;
      stopPostPlayback(true);
      if (ui.post.muted.has(kind)) ui.post.muted.delete(kind);
      else ui.post.muted.add(kind);
      paintView();
      return;
    }
    if (act === 'post-solo') {
      const kind = el.dataset.track;
      stopPostPlayback(true);
      if (ui.post.soloed.has(kind)) ui.post.soloed.delete(kind);
      else ui.post.soloed.add(kind);
      paintView();
      return;
    }
    if (act === 'post-item') {
      // 刚拖动过的块不再触发试听，避免松手时误播
      if (ui.post.dragSuppress) return;
      previewPostItem(el);
      return;
    }
    if (act === 'post-clear-pick') {
      ui.post.selectedItem = null;
      paintView();
      return;
    }
    if (act === 'post-warnings') {
      openWarnings();
      return;
    }
    return;
  }
  if (state.view === 'align') {
    onAlignAction(act, el);
    return;
  }
}

/** 选择音频文件后立即校验并回显（智能对轨上传区） */
function onDocChange(event) {
  if (state.view === 'record' && event.target?.id === 'rec-device') {
    ui.record.deviceId = event.target.value || '';
    toast('录音设备已切换，下次开始录音生效', 'info', 1400);
    return;
  }
  if (state.view === 'post' && event.target?.dataset?.postFade) {
    applyClipFade(event.target.dataset.postFade, Number(event.target.value) || 0);
    return;
  }
  if (state.view !== 'align') return;
  if (event.target?.id !== 'align-file') return;
  pickAlignFile(event.target.files?.[0] || null);
}

/** 拖动轨道音量滑杆：即时应用，不重画时间轴 */
function onDocInput(event) {
  if (state.view !== 'post') return;
  const el = event.target.closest?.('[data-vact="post-volume"]');
  if (!el) return;
  setTrackVolume(el.dataset.track, Number(el.value));
}

/**
 * 键盘：后期视图空格播放 / 暂停。
 * 输入框内不拦截，避免影响打字。
 * @param {KeyboardEvent} event 键盘事件
 */
function onDocKey(event) {
  if (event.target.closest?.('input, textarea, select, [contenteditable="true"]')) return;
  if (state.view !== 'post') return;
  if (event.key === ' ' && !event.target.closest('button')) {
    event.preventDefault();
    togglePostPlayback();
  }
}

/** 后期首次进入时加载时间轴并重画 */
async function ensureTimelineForView() {
  if (state.view !== 'post') return;
  if (timeline() || timelineLoading === state.chapterId) return;
  timelineLoading = state.chapterId;
  try {
    await loadTimeline(false);
  } catch {
    // 后期会显示空状态，不在这里打断
  } finally {
    timelineLoading = '';
    if (state.view === 'post') {
      paintView();
      paintPanel();
    }
  }
}

/**
 * 进入视图时绑定事件，并按视图加载数据。
 * @param {HTMLElement} root 页面根节点
 */
export function mountView(root) {
  unmountViews();
  if (state.view === 'script') return;
  document.addEventListener('click', onDocClick);
  document.addEventListener('keydown', onDocKey);
  document.addEventListener('change', onDocChange);
  document.addEventListener('input', onDocInput);
  document.addEventListener('pointerdown', onDocPointerDown);
  document.addEventListener('pointermove', onDocPointerMove);
  document.addEventListener('pointerup', onDocPointerUp);
  mounted = true;
  if (state.view === 'record') {
    observeWaves(root);
    loadRecordDevices();
    window.clearInterval(recordClock);
    recordClock = window.setInterval(() => {
      const node = document.getElementById('rec-clock');
      if (!node || !ui.record.startedAt) return;
      const elapsed = Date.now() - ui.record.startedAt;
      const minutes = Math.floor(elapsed / 60000);
      const seconds = Math.floor(elapsed / 1000) % 60;
      const ms = elapsed % 1000;
      node.textContent = `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}.${String(ms).padStart(3, '0')}`;
    }, 90);
  }
  if (state.view === 'align') loadAlignTasks();
  ensureTimelineForView();
}

/**
 * 离开视图：停掉所有播放、解绑监听。
 * 切章时 timelineCache 以章节 ID 判断，无需手动清空。
 */
export function unmountViews() {
  waveObserver?.disconnect();
  waveObserver = null;
  window.clearInterval(recordClock);
  recordClock = 0;
  if (mounted) {
    document.removeEventListener('click', onDocClick);
    document.removeEventListener('keydown', onDocKey);
    document.removeEventListener('change', onDocChange);
    document.removeEventListener('input', onDocInput);
    document.removeEventListener('pointerdown', onDocPointerDown);
    document.removeEventListener('pointermove', onDocPointerMove);
    document.removeEventListener('pointerup', onDocPointerUp);
    mounted = false;
  }
  postDrag = null;
  ui.post.selectedItem = null;
  ui.post.dragSuppress = false;
  stopRecordPlayback();
  stopPostPlayback(true);
  stopReviewPlayback();
}

/** 画本编辑里离开「智能审听」页签时停掉审听播放 */
export function stopAuditPlayback() {
  stopReviewPlayback();
}

/** 彻底离开工作室（换工程 / 离开页面）时清理缓存与音频 */
export function resetViews() {
  unmountViews();
  timelineCache = { chapter: '', data: null };
  timelineLoading = '';
  peakCache.clear();
  ui.record.filter = 'all';
  ui.post.zoom = 1;
  ui.post.muted = new Set();
  ui.post.soloed = new Set();
  ui.post.volumes = new Map();
  ui.post.selected = null;
  ui.post.snap = true;
  ui.post.linked = true;
  ui.post.cursor = 0;
  ui.review.onlyAbnormal = false;
  ui.review.currentLine = '';
  ui.align.file = null;
  ui.align.tasks = [];
  ui.align.error = '';
  ui.align.start = '';
  ui.align.end = '';
}
