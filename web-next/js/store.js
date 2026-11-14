/**
 * 前端运行时状态。
 * 只有一个工程在编辑，因此这里用扁平对象，不做复杂状态管理。
 */

const LAST_KEY = 'ndw.lastProject';
/** 全局界面偏好（与工程无关）：隐藏标签 / 紧凑段落 / 正文字号 */
const PREFS_KEY = 'ndw.uiPrefs';

export const state = {
  projects: [],
  projectInfos: {},
  path: '',
  info: null,
  chapters: [],
  chapterId: '',
  lines: [],
  counts: {},
  /** 本章正文与切分台词是否一致（null 表示还没检查） */
  consistency: null,
  characters: [],
  cast: null,
  filter: 'all',
  chapterSearch: '',
  lineSearch: '',
  selected: new Set(),
  expandedLine: '',
  task: null,
  runningLine: '',
  playingLine: '',
  saveState: 'idle',
  /** 工作室视图：画本编辑 / 录音 / 后期制作 / 智能对轨（align） */
  view: 'script',
  /** 画本编辑右栏页签：本章任务 / 智能审听 */
  panelTab: 'tasks',
  /** 画本编辑显示偏好：隐藏标签、紧凑段落、字号 */
  hideTags: false,
  dense: false,
  fontSize: 'md',
  player: { playing: false, index: -1, audio: null },
};

/** 读取上次打开的工程路径 */
export function lastProject() {
  try {
    return localStorage.getItem(LAST_KEY) || '';
  } catch {
    return '';
  }
}

/** 记住上次打开的工程路径 */
export function rememberProject(path) {
  try {
    if (path) localStorage.setItem(LAST_KEY, path);
  } catch {
    // 隐私模式下 localStorage 不可用，忽略即可
  }
}

/**
 * 读取全局界面偏好并写回 state。
 * 偏好与工程无关，启动时调用一次即可；读取失败时保持默认值。
 */
export function loadUIPrefs() {
  try {
    const saved = JSON.parse(localStorage.getItem(PREFS_KEY) || '{}');
    if (typeof saved.hideTags === 'boolean') state.hideTags = saved.hideTags;
    if (typeof saved.dense === 'boolean') state.dense = saved.dense;
    if (['sm', 'md', 'lg'].includes(saved.fontSize)) state.fontSize = saved.fontSize;
  } catch {
    // 隐私模式或数据损坏时使用默认偏好
  }
}

/**
 * 保存全局界面偏好（先合并当前 state，再写入 localStorage）。
 * @param {{hideTags?:boolean, dense?:boolean, fontSize?:string}} [patch] 本次改动的字段
 */
export function saveUIPrefs(patch = {}) {
  try {
    const merged = {
      hideTags: state.hideTags,
      dense: state.dense,
      fontSize: state.fontSize,
      ...patch,
    };
    localStorage.setItem(PREFS_KEY, JSON.stringify(merged));
  } catch {
    // 写不进去不影响本次会话的界面效果
  }
}

/** 切到某工程前清空与旧工程相关的派生状态 */
export function resetProject(path) {
  state.path = path;
  state.info = null;
  state.chapters = [];
  state.chapterId = '';
  state.lines = [];
  state.counts = {};
  state.consistency = null;
  state.characters = [];
  state.cast = null;
  state.selected = new Set();
  state.expandedLine = '';
  state.runningLine = '';
  state.filter = 'all';
  state.lineSearch = '';
  state.chapterSearch = '';
  state.task = null;
  stopPlayer();
  rememberProject(path);
}

// ---------------- 播放器（整章试听） ----------------

/** 停止整章试听并释放 audio 元素 */
export function stopPlayer() {
  if (state.player.audio) {
    state.player.audio.pause();
    state.player.audio = null;
  }
  state.player.playing = false;
  state.player.index = -1;
  state.playingLine = '';
}

/** 当前行的配音相对路径（没有则为空） */
export function audioOf(row) {
  return row && row.state === 'ready' && row.audio ? row.audio : '';
}

// ---------------- 角色配色 ----------------

const TONES = ['a', 'b', 'c', 'd', 'n', 'a', 'b', 'c'];

/**
 * 角色 → 行左侧色条色号。
 * 旁白固定 n（中性灰），其余按角色在列表中的位置轮转，保证同章稳定。
 * @param {string} speakerId 角色 ID
 */
export function toneFor(speakerId) {
  if (!speakerId || speakerId === 'chr_narrator' || speakerId === 'chr_unknown') return 'n';
  const index = state.characters.findIndex((item) => item.id === speakerId);
  if (index < 0) return 'n';
  return TONES[index % TONES.length];
}
