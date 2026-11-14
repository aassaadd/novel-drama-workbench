/**
 * 小说工作台：世界设定 / 分层大纲 / 章节正文（分阶段接入）。
 *
 * 与画本编辑共用顶栏、56px 图标栏与三栏骨架，但内容完全来自写作域接口：
 * `setting show`、`world list`、`outline show`、`chapter generate` 等，
 * 前端只做展示与参数拼装，不复制写作规则。
 * 视觉与交互沿用 web-next 既有规范（卡片、chip、ed-head、vw-body、弹窗）。
 */
import { esc, fmtClock, icon, toast, openDialog, closeDialog, reuseListHTML } from '../dom.js';
import { getJSON, postJSON, streamJSON } from '../api.js';
import { ENUMS, selectHTML } from '../enums.js';
import { openProgress, watchProgress } from '../progress.js';
import { modeSwitchHTML, bindModeSwitch } from '../gen-mode.js';
import { state } from '../store.js';
import { railbar, topbar, goChapter, goProjects } from '../shell.js';

/** 本页局部状态：选中对象、忙碌标记与最近一次 AI 预览 */
const ui = {
  /** 选中对象：setting 写作设定 / worldOverview 世界观总述 / worldEntry 分类条目 / chapter 大纲章 */
  sel: { kind: 'setting', id: '' },
  /** 当前世界模块 ID（多套世界时由下拉切换） */
  worldId: '',
  busy: '',
  /** AI 世界生成预览（未采纳前不落盘） */
  worldPreview: null,
  /** 是否正在新建世界（表单空白但尚未落盘） */
  draftWorld: false,
  /** 是否正在新建分类条目（表单空白但尚未落盘） */
  draftEntry: false,
};

/** 页面数据缓存，切章或写操作后整体重载 */
const cache = {
  path: '',
  setting: null,
  worlds: [],
  outline: null,
  chapters: [],
  check: null,
  /** 当前章节正文（含 word_count/revision） */
  body: null,
  /** 正文对应的章节 ID，避免切章时串数据 */
  bodyChapter: '',
  /** 未应用的续写草稿 */
  draft: null,
  /** 本章配音补齐预检（缺多少行、是否有阻断） */
  plan: null,
  /** 预检对应的章节 ID */
  planChapter: '',
};

/** 弹窗里待采用的世界条目（勾选状态） */
let previewPicked = new Set();
/** 根节点点击监听只绑一次（main.js 复用同一个根节点） */
let bound = false;

/**
 * 启动数据块状态：每块独立加载、独立渲染，慢接口不阻塞其它区域。
 * state 取值 idle / loading / ok / error；errors 存中文失败原因。
 */
const boot = {
  /** 当前启动批次；切换工程或重进视图后旧响应直接丢弃 */
  seq: 0,
  /** 这些状态对应的工程目录；同一工程再次进入时先用缓存渲染 */
  path: '',
  state: { info: 'idle', chapters: 'idle', setting: 'idle', worlds: 'idle', outline: 'idle' },
  errors: {},
};

// ---------------- 数据 ----------------

/**
 * 读取写作域全部数据（设定 / 世界 / 大纲 / 章节目录）。
 * @param {string} path 工程目录
 * @param {boolean} [keepCheck] 是否保留上一次黄金三章检查结果
 */
async function loadAll(path, keepCheck = false) {
  // 空工程允许设定库 / 世界条目 / 大纲尚未初始化：单项失败按空数据处理，
  // 不让整个小说视图中断（章节列表仍以真实返回为准）
  const [setting, worlds, outline, chapters] = await Promise.all([
    getJSON('/api/ui/setting', { path }).catch(() => ({})),
    getJSON('/api/ui/worlds', { path }).catch(() => ({ worlds: [] })),
    getJSON('/api/ui/outline', { path }).catch(() => ({ acts: [] })),
    getJSON('/api/ui/chapters', { path }),
  ]);
  cache.path = path;
  cache.setting = setting || {};
  cache.worlds = worlds?.worlds || [];
  cache.outline = outline || { acts: [] };
  cache.chapters = Array.isArray(chapters) ? chapters : [];
  // 手动刷新成功后清掉启动阶段的失败态，避免旧错误块一直留在界面上
  boot.state.setting = 'ok';
  boot.state.worlds = 'ok';
  boot.state.outline = 'ok';
  boot.state.chapters = 'ok';
  boot.errors = {};
  if (!keepCheck) cache.check = null;
}

/** 按 ID 找世界条目 */
function findWorld(id) {
  return cache.worlds.find((item) => item.id === id) || null;
}

/**
 * 世界模块的分区定义：分类键 → 世界字段、中文名、空态说明。
 * 顶层是「世界观总述」，下层是这四类条目。
 */
const CATEGORY_META = {
  location: { field: 'locations', label: '地点', hint: '故事发生的主要场景，可写氛围与来源章节' },
  faction: { field: 'factions', label: '势力', hint: '阵营、组织、家族等推动冲突的群体' },
  term: { field: 'glossary', label: '术语', hint: '专有名词、设定术语及其释义' },
  other: { field: 'others', label: '其它', hint: '关键物件、习俗、生物等未归类内容' },
};

/** 当前选中的世界模块（未选中时取第一套） */
function activeWorld() {
  return findWorld(ui.worldId) || cache.worlds[0] || null;
}

/** 取某套世界里某一类条目 */
function categoryEntries(category, world = activeWorld()) {
  const meta = CATEGORY_META[category];
  return meta && world ? world[meta.field] || [] : [];
}

/** 条目显示名：术语用 term，其它分类用 name */
function entryName(category, item) {
  if (category === 'term') return String(item?.term || item?.name || '').trim();
  return String(item?.name || '').trim();
}

/** 条目一句话说明 */
function entryDesc(category, item) {
  if (category === 'term') return String(item?.meaning || item?.description || '').trim();
  if (category === 'location' && item?.atmosphere) {
    return [item.description, `氛围：${item.atmosphere}`].filter(Boolean).join(' · ');
  }
  return String(item?.description || '').trim();
}

/** 章节 ID → 可读标题（用于来源章节标签） */
function chapterTitleOf(id) {
  const hit = outlineChapters().find((row) => row.chapter.id === id);
  return hit?.chapter?.title || String(id || '');
}


/** 展开大纲里的所有章节，附带所属幕 */
function outlineChapters() {
  const rows = [];
  for (const act of cache.outline?.acts || []) {
    for (const chapter of act.chapters || []) {
      rows.push({ act, chapter });
    }
  }
  return rows;
}

/**
 * 大纲生成进度文案：严格按阶段区分分母与单位，避免把章数/节拍数配到「幕」上。
 * - 幕阶段：规划 10 幕（一次请求出全量，不虚构「第 k/10 幕」）
 * - 章阶段：第 x/10 幕 · 已完成 k/50 章
 * - 节拍阶段：第 x/10 幕 · 第 k/50 章 · 第 y/200 节拍
 * @param {object} data 后端进度事件
 * @param {object} totals 兜底总量 `{acts, chapters, beats}`
 * @returns {{stage: string, current: string}} 浮层主文案与副标题
 */
function outlineProgressText(data, totals = {}) {
  const phase = String(data.phase || '');
  const actsTotal = Number(data.act_total || (phase === 'acts' || phase === 'chapters' ? data.total : 0) || totals.acts || 0);
  const actIndex = Number(data.act_index || data.index || 0);
  const chaptersTotal = Number(data.chapter_total || data.chapters_total || totals.chapters || 0);
  const chapterIndex = Number(data.chapter_index || 0);
  const beatsTotal = Number(data.beat_total || totals.beats || 0);
  const beatIndex = Number(data.beat_index || data.index || 0);
  if (phase === 'acts') {
    return {
      stage: `正在规划 ${actsTotal || '?'} 幕（一次请求出全量）`,
      current: data.name ? String(data.name) : '',
    };
  }
  if (phase === 'chapters') {
    const done = Number(data.chapters_done || 0);
    const expected = Number(data.chapters_expected || 0);
    const span = expected ? `第 ${done + 1}–${done + expected} 章` : `已完成 ${done} 章`;
    return {
      stage: `第 ${actIndex}/${actsTotal} 幕 · 生成章节 · ${span}`,
      current: chaptersTotal ? `全书共 ${chaptersTotal} 章` : '',
    };
  }
  if (phase === 'beats') {
    return {
      stage: `第 ${actIndex}/${actsTotal} 幕 · 第 ${chapterIndex}/${chaptersTotal} 章 · 第 ${Math.max(
        1,
        beatIndex
      )}/${beatsTotal} 节拍`,
      current: data.chapter || data.name ? `《${data.chapter || data.name}》` : '',
    };
  }
  // 兼容旧事件（没有 phase）：按 unit 区分单位，绝不把章数写成「幕」
  const unitLabel = { act: '幕', chapter: '章', beat: '个节拍' }[data.unit] || '批';
  const total = Number(data.total || 0);
  return {
    stage: `正在生成第 ${Number(data.index || 1)}/${total || '?'} ${unitLabel}`,
    current: data.name ? String(data.name) : '',
  };
}

/** 按章节 ID 找大纲章 + 所属幕 */
function findOutlineChapter(id) {
  return outlineChapters().find((row) => row.chapter.id === id) || null;
}

/** 章节是否已落成正文文件 */
function landedChapter(id) {
  return cache.chapters.find((item) => item.id === id) || null;
}

/** 当前章已写正文字数（优先正文接口，其次章节列表缓存） */
function currentBodyWords() {
  if (ui.sel.kind !== 'chapter') return 0;
  if (cache.bodyChapter === ui.sel.id) return Number(cache.body?.word_count || 0);
  const landed = landedChapter(ui.sel.id);
  return Number(landed?.word_count || 0);
}

/** 大纲规模统计 */
function outlineCounts() {
  const acts = cache.outline?.acts || [];
  return {
    acts: acts.length,
    chapters: acts.reduce((sum, act) => sum + (act.chapters?.length || 0), 0),
    beats: acts.reduce(
      (sum, act) => sum + (act.chapters || []).reduce((n, ch) => n + (ch.beats?.length || 0), 0),
      0
    ),
  };
}

/**
 * 扩充 / 续写的可读来源说明：按当前大纲即时定位锚点，插入后编号变化也能对上。
 * @param {object} origin 大纲对象上的 origin 字段
 * @returns {string} 说明文本；非扩充对象返回空串
 */
function originLabel(origin) {
  if (!origin || !origin.kind) return '';
  const kindLabel = origin.kind === 'continuation' ? '结局后续写' : '扩充';
  const acts = cache.outline?.acts || [];
  if (origin.mode === 'after_chapter' && origin.anchor_chapter_id) {
    const hit = outlineChapters().find((row) => row.chapter.id === origin.anchor_chapter_id);
    const number = hit ? Number(hit.chapter.index || 0) : null;
    return `${kindLabel} · 插在第 ${number ?? '?'} 章《${origin.anchor_chapter_title || ''}》之后`;
  }
  if (origin.mode === 'after_act' && origin.anchor_act_id) {
    const pos = acts.findIndex((act) => act.id === origin.anchor_act_id);
    return `${kindLabel} · 插在第 ${pos >= 0 ? pos + 1 : '?'} 幕《${origin.anchor_act_title || ''}》之后`;
  }
  return `${kindLabel} · 接在全书结尾之后`;
}

/**
 * 扩充 / 续写来源徽标（只标注，不改变已有内容）。
 * @param {object} origin 大纲对象上的 origin 字段
 * @returns {string} HTML
 */
function originBadge(origin) {
  if (!origin || !origin.kind) return '';
  const label = origin.kind === 'continuation' ? '续写' : '扩充';
  return `<span class="badge warn" title="${esc(originLabel(origin))}">${label}</span>`;
}

/** 从已有大纲推断常用规模（每幕章数 / 每章节拍 / 单章字数） */
function outlineScaleDefaults() {
  const acts = cache.outline?.acts || [];
  const chapters = acts.flatMap((act) => act.chapters || []);
  const pick = (values, fallback) => {
    const rows = values.filter((value) => Number(value) > 0);
    if (!rows.length) return fallback;
    const counts = new Map();
    for (const value of rows) counts.set(value, (counts.get(value) || 0) + 1);
    return [...counts.entries()].sort((left, right) => right[1] - left[1])[0][0];
  };
  const words = chapters.map((chapter) =>
    (chapter.beats || []).reduce((sum, beat) => sum + Number(beat.target_words || 0), 0)
  );
  return {
    chaptersPerAct: pick(acts.map((act) => (act.chapters || []).length), 5),
    beatsPerChapter: pick(chapters.map((chapter) => (chapter.beats || []).length), 4),
    chapterWords: pick(words, 3000),
  };
}

/** 把多行文本切成数组（去掉空行与首尾空格） */
function splitLines(text) {
  return String(text || '')
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean);
}

/** 数组拼成多行文本 */
function joinLines(list) {
  return (Array.isArray(list) ? list : []).join('\n');
}

/**
 * 尝试把字符串解析成 JSON 值；普通文本返回 null。
 * 用于兼容「数组被写成 JSON 字符串」的历史数据，避免把原始 JSON 直接显示给用户。
 * @param {any} value 原始值
 * @returns {any|null} 解析结果或 null
 */
function parseJSONish(value) {
  if (typeof value !== 'string') return null;
  const text = value.trim();
  if (!text || !/^[[{]/.test(text)) return null;
  try {
    const parsed = JSON.parse(text);
    return parsed && typeof parsed === 'object' ? parsed : null;
  } catch {
    return null;
  }
}

/**
 * 任意值转中文可读文本：对象按键值拼接、数组用顿号连接，绝不输出 JSON 括号与引号。
 * @param {any} value 原始值
 * @returns {string} 可读文本
 */
function readableText(value) {
  if (value === null || value === undefined) return '';
  if (typeof value === 'string') {
    const parsed = parseJSONish(value);
    return parsed === null ? value.trim() : readableText(parsed);
  }
  if (Array.isArray(value)) return value.map(readableText).filter(Boolean).join('；');
  if (typeof value === 'object') {
    // 只取字段值拼接，避免把英文键名暴露到只读展示里
    return Object.values(value).map(readableText).filter(Boolean).join('；');
  }
  return String(value);
}

/**
 * 归一化字符串数组（世界规则 / 阵营 / 禁止项）。
 * 兼容数组、JSON 字符串、普通多行字符串与对象项，保证界面上不出现 JSON。
 * @param {any} value 原始值
 * @returns {string[]} 文本数组
 */
function asStringList(value) {
  if (value === null || value === undefined || value === '') return [];
  if (typeof value === 'string') {
    const parsed = parseJSONish(value);
    if (parsed !== null) return asStringList(parsed);
    return splitLines(value);
  }
  if (Array.isArray(value)) {
    return value.map(readableText).map((text) => text.trim()).filter(Boolean);
  }
  return [readableText(value)].filter(Boolean);
}

/**
 * 归一化地点数组：兼容对象数组、JSON 字符串、单对象、名称映射与 `名称 | 描述 | 氛围` 文本。
 * @param {any} value 原始值
 * @returns {Array<object>} 地点条目（保留未知字段）
 */
function asLocationList(value) {
  let rows = value;
  if (typeof rows === 'string') {
    const parsed = parseJSONish(rows);
    if (parsed !== null) rows = parsed;
    else return splitLines(rows).map((line) => {
      const [name = '', description = '', atmosphere = ''] = line.split('|').map((part) => part.trim());
      return { name, description, atmosphere };
    });
  }
  if (rows && !Array.isArray(rows) && typeof rows === 'object') {
    if ('name' in rows || '名称' in rows || 'title' in rows) rows = [rows];
    else rows = Object.entries(rows).map(([name, description]) => ({ name, description: readableText(description) }));
  }
  const list = [];
  for (const item of Array.isArray(rows) ? rows : []) {
    if (item && typeof item === 'object' && !Array.isArray(item)) {
      list.push({
        ...item,
        name: readableText(item.name ?? item.名称 ?? item.title ?? ''),
        description: readableText(item.description ?? item.desc ?? item.描述 ?? ''),
        atmosphere: readableText(item.atmosphere ?? item.氛围 ?? ''),
      });
      continue;
    }
    const parsed = parseJSONish(item);
    if (parsed !== null) {
      list.push(...asLocationList(parsed));
      continue;
    }
    const [name = '', description = '', atmosphere = ''] = String(item ?? '').split('|').map((part) => part.trim());
    if (name || description || atmosphere) list.push({ name, description, atmosphere });
  }
  return list;
}

/**
 * 别名归一化：数组直接展开，字符串按顿号/逗号/分号拆分。
 * @param {any} value 原始别名
 * @returns {string[]} 别名数组
 */
function splitAliases(value) {
  if (Array.isArray(value)) return value.map(readableText).flatMap(splitAliases).filter(Boolean);
  const text = readableText(value);
  return text
    .split(/[、,，;；]/)
    .map((part) => part.trim())
    .filter(Boolean);
}

/** 当前选中对象的标题（用于顶栏/空状态） */
function selectionTitle() {
  if (ui.sel.kind === 'setting') return '写作设定';
  if (ui.sel.kind === 'worldOverview') return `${activeWorld()?.name || '世界'} · 世界观总述`;
  if (ui.sel.kind === 'worldEntry') {
    const meta = CATEGORY_META[ui.sel.category] || {};
    const item = (activeWorld()?.[meta.field] || []).find((row) => row.id === ui.sel.id);
    return `${activeWorld()?.name || '世界'} · ${meta.label || '条目'}${item ? ` · ${entryName(ui.sel.category, item)}` : ''}`;
  }
  const hit = findOutlineChapter(ui.sel.id);
  return hit ? `${hit.chapter.index ? `第 ${hit.chapter.index} 章 · ` : ''}${hit.chapter.title || '未命名'}` : '分层大纲';
}

// ---------------- 左侧目录 ----------------

/**
 * 目录区的加载 / 失败占位（世界条目、分层大纲共用）。
 * @param {'worlds'|'outline'} key 数据块
 * @param {string} emptyText 已加载但为空时的提示
 * @returns {string} HTML
 */
function ctxBlockHTML(key, emptyText) {
  const status = boot.state[key];
  if (status === 'error') {
    return `<div class="ctx-boot bad">
      <span>${esc(boot.errors[key] || '读取失败')}</span>
      <button class="link" data-act="boot-retry" data-key="${key}">重试</button>
    </div>`;
  }
  if (status !== 'ok') {
    return `<div class="ctx-boot" aria-busy="true">
      <span class="skel skel-line" style="width:76%"></span>
      <span class="skel skel-line" style="width:52%"></span>
    </div>`;
  }
  return emptyText;
}

/**
 * 左栏折叠状态：世界分区与大纲幕，按工程路径持久化到 localStorage。
 * 用户还没手动操作过时不写盘，按「当前选中项」算默认值（只展开选中的分类/幕）。
 */
const collapse = {
  /** 状态所属工程路径，切换工程时重新读取 */
  path: '',
  /** 是否已有用户操作过的持久化状态 */
  stored: false,
  /** 折叠的世界分区键：overview / location / faction / term / other */
  world: new Set(),
  /** 折叠的幕键（大纲幕 ID，缺失时用位置） */
  acts: new Set(),
};

/** 折叠状态在 localStorage 里的键（按工程隔离） */
function collapseKey(path) {
  return `ndw.novel.collapse.${path || ''}`;
}

/**
 * 读取指定工程的折叠状态；没有记录时保持未初始化，由默认值接管。
 * @param {string} path 工程目录
 */
function loadCollapse(path) {
  collapse.path = path;
  collapse.stored = false;
  collapse.world = new Set();
  collapse.acts = new Set();
  try {
    const raw = JSON.parse(localStorage.getItem(collapseKey(path)) || 'null');
    if (!raw || typeof raw !== 'object') return;
    collapse.stored = true;
    collapse.world = new Set((raw.world || []).map(String));
    collapse.acts = new Set((raw.acts || []).map(String));
  } catch {
    // 隐私模式或坏数据：忽略，回落到默认折叠
  }
}

/** 把当前折叠状态写回 localStorage（失败不影响本次会话内的折叠） */
function saveCollapse() {
  try {
    localStorage.setItem(
      collapseKey(cache.path || collapse.path),
      JSON.stringify({ world: [...collapse.world], acts: [...collapse.acts] })
    );
  } catch {
    // 写不进去时只在本次会话内生效
  }
}

/** 幕的稳定键：优先用大纲里的幕 ID，缺失时用位置序号 */
function actKey(act, index) {
  return String(act?.id || `#${index}`);
}

/** 默认只展开「当前选中的世界分区」，其余折叠 */
function defaultCollapsedWorld() {
  const active = ui.sel.kind === 'worldEntry' && CATEGORY_META[ui.sel.category] ? ui.sel.category : 'overview';
  return new Set(['overview', ...Object.keys(CATEGORY_META)].filter((key) => key !== active));
}

/** 默认只展开「当前章节所在的幕」，没有选中章节时展开第一幕 */
function defaultCollapsedActs() {
  const acts = cache.outline?.acts || [];
  const current = ui.sel.kind === 'chapter' ? ui.sel.id : '';
  let keep = acts.findIndex((act) => (act.chapters || []).some((chapter) => chapter.id === current));
  if (keep < 0) keep = 0;
  return new Set(acts.map((act, index) => actKey(act, index)).filter((key, index) => index !== keep));
}

/** 首次折叠操作前把默认值固化，避免只记住一半状态 */
function materializeCollapse() {
  if (collapse.stored) return;
  collapse.world = defaultCollapsedWorld();
  collapse.acts = defaultCollapsedActs();
  collapse.stored = true;
}

/** 世界分区是否展开 */
function worldGroupOpen(key) {
  if (!collapse.stored) return !defaultCollapsedWorld().has(key);
  return !collapse.world.has(key);
}

/** 某一幕是否展开 */
function actOpen(act, index) {
  const key = actKey(act, index);
  if (!collapse.stored) return !defaultCollapsedActs().has(key);
  return !collapse.acts.has(key);
}

/** 切换世界分区折叠：只翻转这一项，不影响其它分区 */
function toggleWorldGroup(key) {
  materializeCollapse();
  if (collapse.world.has(key)) collapse.world.delete(key);
  else collapse.world.add(key);
  saveCollapse();
  paint();
}

/** 切换某一幕的折叠：只翻转这一项 */
function toggleAct(key) {
  materializeCollapse();
  if (collapse.acts.has(key)) collapse.acts.delete(key);
  else collapse.acts.add(key);
  saveCollapse();
  paint();
}

/** 确保某个世界分区展开（新增条目时用，避免表单落在折叠区里看不到） */
function openWorldGroup(key) {
  materializeCollapse();
  if (!collapse.world.has(key)) return;
  collapse.world.delete(key);
  saveCollapse();
}

/**
 * 左侧「世界」模块：世界观总述入口 + 地点/势力/术语/其它四类条目。
 * 总述与条目都归属于当前选中的世界；切换世界用总述上方的下拉。
 * 每个分区（总述与四类条目）都可折叠，折叠状态按工程记住。
 * @returns {string} HTML
 */
function worldModuleHTML() {
  const world = activeWorld();
  if (!world) {
    return `<div class="hint" style="padding:6px 12px;display:grid;gap:4px">
      <div>还没有世界</div>
      <div style="color:var(--text-4)">点上方「+」新建一套世界，或点「✨」让 AI 生成世界观总述与分类条目；写作设定只保留故事内核与文风。</div>
    </div>`;
  }
  const picker =
    cache.worlds.length > 1
      ? `<div style="padding:6px 12px 2px">
          <select class="select" id="novel-world-pick" data-world-pick title="切换当前世界">
            ${cache.worlds
              .map(
                (item) =>
                  `<option value="${esc(item.id)}" ${item.id === world.id ? 'selected' : ''}>${esc(
                    item.name || '未命名世界'
                  )}</option>`
              )
              .join('')}
          </select>
        </div>`
      : '';
  const overviewOpen = worldGroupOpen('overview');
  const overview = `
    <div class="ctx-group toggle ${overviewOpen ? 'open' : ''}" data-act="novel-toggle-world-group" data-group="overview"
      title="世界观总述：名称、时代、世界观文本与规则；点击${overviewOpen ? '折叠' : '展开'}">
      <span class="novel-caret" aria-hidden="true">▸</span>
      世界观总述 <span class="n">1</span>
    </div>
    ${
      overviewOpen
        ? `<div class="novel-group-body">
      <button class="chap-row novel-row ${ui.sel.kind === 'worldOverview' ? 'on' : ''}" data-act="novel-sel-world-overview">
        <span class="mono novel-row-icon">${icon('book', 12)}</span>
        <span class="novel-world-body">
          <b class="novel-world-name">世界观总述</b>
          <span class="novel-world-desc">${esc(world.era || '时代与舞台待填写')} · 地点 ${categoryEntries(
            'location',
            world
          ).length} / 势力 ${categoryEntries('faction', world).length}</span>
        </span>
      </button>
    </div>`
        : ''
    }`;
  const groups = Object.entries(CATEGORY_META)
    .map(([category, meta]) => {
      const rows = categoryEntries(category, world);
      const open = worldGroupOpen(category);
      const body = rows.length
        ? `<div class="novel-group-body">${rows
            .map(
              (item) => `
        <button class="chap-row novel-row ${
          ui.sel.kind === 'worldEntry' && ui.sel.category === category && ui.sel.id === item.id ? 'on' : ''
        }" data-act="novel-sel-world-entry" data-category="${category}" data-id="${esc(item.id)}">
          <span class="mono novel-row-icon">${icon('image', 12)}</span>
          <span class="novel-world-body">
            <b class="novel-world-name">${esc(entryName(category, item) || '未命名条目')}</b>
            <span class="novel-world-desc">${esc(entryDesc(category, item))}</span>
          </span>
        </button>`
            )
            .join('')}</div>`
        : `<div class="hint" style="padding:4px 12px 6px">${esc(meta.hint)}</div>`;
      return `
      <div class="ctx-group toggle ${open ? 'open' : ''}" data-act="novel-toggle-world-group" data-group="${category}"
        title="${esc(meta.hint)}；点击${open ? '折叠' : '展开'}">
        <span class="novel-caret" aria-hidden="true">▸</span>
        ${esc(meta.label)} <span class="n">${rows.length}</span>
        <button class="btn icon sm" data-act="novel-entry-new" data-category="${category}" style="margin-left:auto"
          title="新增${esc(meta.label)}条目">${icon('plus', 12)}</button>
      </div>
      ${open ? body : ''}`;
    })
    .join('');
  return `${picker}${overview}${groups}`;
}

/** 左侧面板：写作设定入口 + 世界模块 + 分层大纲树 */
function ctxHTML() {
  const counts = outlineCounts();
  const acts = (cache.outline?.acts || [])
    .map((act, actIndex) => {
      const open = actOpen(act, actIndex);
      const chapters = act.chapters || [];
      return `
      <div class="novel-act">
        <div class="novel-act-head toggle ${open ? 'open' : ''}" data-act="novel-toggle-act"
          data-key="${esc(actKey(act, actIndex))}"
          title="${esc(act.summary || '')}；点击${open ? '折叠' : '展开'}本章列表">
          <span class="novel-caret" aria-hidden="true">▸</span>
          <span class="mono novel-act-index">第 ${Number(act.index || actIndex + 1)} 幕</span>
          <span class="novel-act-title" title="${esc(act.title || '')}">${esc(act.title || '未命名幕')}</span>
          ${originBadge(act.origin)}
          <button class="btn icon sm" data-act="novel-act-generate" data-id="${esc(act.id || '')}"
            data-index="${Number(act.index || actIndex + 1)}" data-pos="${actIndex + 1}"
            title="生成整幕正文：按章节顺序一次跑完，可选只补缺失或重新生成已有正文">${icon('spark', 12)}</button>
          <span class="n">${chapters.length} 章</span>
        </div>
        ${
          open
            ? `<div class="novel-act-body">${chapters
                .map(
                  (chapter) => `
          <button class="chap-row novel-row novel-chapter ${
            ui.sel.kind === 'chapter' && ui.sel.id === chapter.id ? 'on' : ''
          }" data-act="novel-sel-chapter" data-id="${esc(chapter.id)}">
            <span class="mono novel-chapter-num">${Number(chapter.index || 0)}</span>
            <span class="novel-chapter-title" title="${esc(chapter.title || '未命名章节')}">${esc(
              chapter.title || '未命名章节'
            )}</span>
            ${originBadge(chapter.origin)}
            ${landedChapter(chapter.id) ? '<span class="badge ok">已落章</span>' : ''}
          </button>`
                )
                .join('')}</div>`
            : ''
        }
      </div>`;
    })
    .join('');

  return `
    <section class="ctx">
      <div class="ctx-head">
        <span class="ct">小说</span>
        <span class="badge mute">${counts.acts} 幕 · ${counts.chapters} 章</span>
        <div class="right">
          <button class="btn icon sm" data-act="novel-outline-apply" title="把大纲里尚未落章的章节建成章节文件">${icon(
            'export',
            12
          )}</button>
          <button class="btn icon sm" data-act="novel-refresh" title="重新载入设定与大纲">${icon('refresh')}</button>
        </div>
      </div>
      <div class="chap-list">
        <button class="chap-row novel-row ${ui.sel.kind === 'setting' ? 'on' : ''}" data-act="novel-sel-setting">
          <span class="mono novel-row-icon">${icon('gear', 12)}</span>
          <b class="novel-row-title">写作设定</b>
          <span class="badge mute">故事内核 · 文风</span>
        </button>
        <div class="ctx-group" title="世界模块：上层是世界观总述，下层是地点/势力/术语/其它条目；写作设定不再包含世界观">
          世界 <span class="n">${cache.worlds.length}</span>
          <button class="btn icon sm" data-act="novel-world-new" style="margin-left:auto"
            title="新建一套世界：先写世界观总述，再加分类条目">${icon('plus', 12)}</button>
          <button class="btn icon sm" data-act="novel-world-ai"
            title="AI 生成世界：世界观总述 + 地点/势力/术语条目，先预览再采用">${icon('spark', 12)}</button>
        </div>
        ${boot.state.worlds !== 'ok' ? ctxBlockHTML('worlds') : worldModuleHTML()}
        <div class="ctx-group">
          分层大纲
          <button class="btn icon sm" data-act="novel-outline-expand" title="扩充 / 续写大纲（只新增，不改已有幕与章）">
            ${icon('plus', 12)}
          </button>
          <button class="btn icon sm" data-act="novel-outline-generate" title="AI 生成分层大纲" style="margin-left:auto">
            ${icon('spark', 12)}
          </button>
        </div>
        ${boot.state.outline === 'ok' ? acts || '<div class="hint" style="padding:6px 12px">还没有大纲，先让 AI 生成一版</div>' : ctxBlockHTML('outline')}
      </div>
      <div class="ctx-foot hint" title="全书大纲统计，与当前选中项无关">
        <span>${counts.acts ? `全书大纲 · ${counts.beats} 个节拍` : '全书大纲尚未生成'}</span>
      </div>
    </section>`;
}

/** 编码后的工程路径（用于站内链接） */
function safePath() {
  return encodeURIComponent(cache.path || state.path || '');
}

// ---------------- 主区 ----------------

/** 写作设定：故事内核 / 文风两张卡，均可保存与 AI 生成；世界观归左侧「世界」模块 */
function settingHTML() {
  const premise = cache.setting?.premise || {};
  const style = cache.setting?.style || {};
  const text = (id, label, value, rows = 3, hint = '') => `
    <div class="field">
      <label>${esc(label)}</label>
      <textarea class="textarea" data-free="1" id="${id}" rows="${rows}">${esc(value || '')}</textarea>
      ${hint ? `<div class="hint" style="margin-top:2px">${esc(hint)}</div>` : ''}
    </div>`;
  const line = (id, label, value, hint = '') => `
    <div class="field">
      <label>${esc(label)}</label>
      <input class="input" data-free="1" id="${id}" value="${esc(value || '')}" />
      ${hint ? `<div class="hint" style="margin-top:2px">${esc(hint)}</div>` : ''}
    </div>`;

  return `
    <div class="ed-head">
      <div style="min-width:0">
        <div class="et">写作设定</div>
        <div class="em">AI 写小说时读取的故事层设定（故事内核、文风）；世界观、地点、势力、术语统一在左侧「世界」模块编辑，改完保存即生效</div>
      </div>
      <div class="right">
        <button class="btn sm" data-act="novel-outline-check">${icon('check', 12)} 黄金三章检查</button>
        <button class="btn sm" data-act="novel-setting-ai" data-section="premise">${icon('spark', 12)} AI 补内核</button>
        <button class="btn sm" data-act="novel-setting-ai" data-section="style">${icon('spark', 12)} AI 补文风</button>
      </div>
    </div>
    <div class="vw-body" style="padding:16px;overflow:auto;display:grid;gap:14px">
      <div class="card">
        <div class="card-head"><span class="ct">故事内核</span>
          <div class="right"><button class="btn sm primary" data-act="novel-setting-save" data-section="premise">保存</button></div>
        </div>
        <div class="card-body" style="display:grid;gap:10px">
          <div class="hint">故事内核属于写作设定，用来约束大纲与正文；它不进入左侧「世界」模块或角色库。</div>
          ${text('ns-logline', '一句话梗概', premise.logline, 2, '用一句话说清主角、欲望与阻力')}
          ${text('ns-theme', '主题', premise.theme, 2)}
          ${text('ns-conflict', '核心冲突', premise.core_conflict, 3)}
          ${text('ns-ending', '结局意图', premise.ending_intent, 2)}
          <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px">
            ${line('ns-audience', '目标读者', premise.target_audience, '自由填写，例如：都市悬疑读者')}
            <div class="field"><label>目标篇幅</label>
              ${selectHTML('target_length', {
                id: 'ns-length',
                current: premise.target_length,
                emptyLabel: '未设置',
                title: '目标篇幅档位，只能从列表选择；具体字数用右侧字段单独填',
              })}</div>
            <div class="field"><label>目标总字数（可选）</label>
              <input class="input" id="ns-words" type="number" min="0" step="1000"
                value="${Number(premise.target_words) > 0 ? Number(premise.target_words) : ''}"
                placeholder="例如 120000" title="可选；只作为字数约束参考，不影响上面的篇幅档位" /></div>
          </div>
        </div>
      </div>
      <div class="card">
        <div class="card-head"><span class="ct">文风</span>
          <div class="right"><button class="btn sm primary" data-act="novel-setting-save" data-section="style">保存</button></div>
        </div>
        <div class="card-body" style="display:grid;gap:10px">
          ${text('ns-tone', '语调', style.tone, 3)}
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
            <div class="field"><label>叙事视角</label>
              ${selectHTML('pov', {
                id: 'ns-pov',
                current: style.pov,
                emptyLabel: '未设置',
                title: '叙事人称与视角，只能从列表选择',
              })}</div>
            <div class="field"><label>时态</label>
              ${selectHTML('tense', {
                id: 'ns-tense',
                current: style.tense,
                emptyLabel: '未设置',
                title: '叙述时态，只能从列表选择',
              })}</div>
          </div>
          ${text('ns-visual', '视觉风格', style.visual_style, 2, '给分镜与封面用，写具体场景、光线、色彩与质感')}
          ${text('ns-prose', '行文风格', style.prose_style, 3)}
          ${text('ns-forbidden', '禁止项（每行一条）', joinLines(asStringList(style.forbidden)), 3)}
        </div>
      </div>
    </div>`;
}

/**
 * 世界观总述页：世界名称 / 时代 / 总述 / 规则 / 画面风格。
 * 下方按地点/势力/术语/其它列出条目，点击即可下钻到条目详情。
 * @returns {string} HTML
 */
function worldOverviewHTML() {
  const world =
    findWorld(ui.worldId) ||
    (ui.draftWorld
      ? {
          name: '',
          era: '',
          summary: '',
          rules: [],
          locations: [],
          factions: [],
          glossary: [],
          others: [],
          visual_style: '',
        }
      : null);
  if (!world) {
    return `<div class="empty" style="margin:auto">${icon('image', 22)}<b>还没有世界</b>
      <span>世界模块上层是世界观总述，下层是地点/势力/术语/其它条目；写作设定不再保存世界观</span>
      <span class="hint">点左侧「世界」标题栏的「+」新建，或点「✨」让 AI 生成完整世界；生成结果先预览，采用后才写入。</span></div>`;
  }
  const categoryCards = Object.entries(CATEGORY_META)
    .map(([category, meta]) => {
      const rows = categoryEntries(category, world);
      const chips = rows.length
        ? `<div style="display:flex;flex-wrap:wrap;gap:6px">${rows
            .map(
              (item) => `<button class="btn sm" data-act="novel-sel-world-entry" data-category="${category}"
                data-id="${esc(item.id)}" title="编辑${esc(meta.label)}条目${item.source_chapters?.length ? `；来源 ${item.source_chapters.length} 章` : ''}">
                ${esc(entryName(category, item) || '未命名')}</button>`
            )
            .join('')}</div>`
        : `<div class="hint">${esc(meta.hint)}</div>`;
      return `<div class="card" style="box-shadow:none;border:1px solid var(--line-1)">
        <div class="card-head"><span class="ct">${esc(meta.label)} <span class="n">${rows.length}</span></span>
          <div class="right"><button class="btn sm" data-act="novel-entry-new" data-category="${category}">${icon(
            'plus',
            12
          )} 新增</button></div>
        </div>
        <div class="card-body">${chips}</div>
      </div>`;
    })
    .join('');
  return `
    <div class="ed-head">
      <div style="min-width:0">
        <div class="et">${esc(world.name || '新世界')}</div>
        <div class="em">世界 › 世界观总述：写世界的整体运行方式；地点、势力、术语在下方作为条目管理</div>
      </div>
      <div class="right">
        ${ui.draftWorld ? '' : `<button class="btn sm" data-act="novel-world-delete" style="color:var(--bad)">删除世界</button>`}
        <button class="btn sm primary" data-act="novel-world-overview-save">保存</button>
      </div>
    </div>
    <div class="vw-body" style="padding:16px;overflow:auto;display:grid;gap:14px">
      <div class="card">
        <div class="card-head"><span class="ct">世界观总述</span></div>
        <div class="card-body" style="display:grid;gap:10px">
          <div class="hint">这里是世界观的上层文本，会随章节生成提供给模型；地点、势力、术语请放在下面的分类条目里，避免同一信息写两遍。多套世界可在左侧「世界」标题下切换。</div>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
            <div class="field"><label>世界名称</label><input class="input" id="nw-name" value="${esc(world.name || '')}" placeholder="例如：主世界 / 镜中城" /></div>
            <div class="field"><label>时代与舞台</label><input class="input" id="nw-era" value="${esc(world.era || '')}" placeholder="例如：民国上海，租界与老城并存" /></div>
          </div>
          <div class="field"><label>世界观总述</label>
            <textarea class="textarea" id="nw-summary" rows="6" placeholder="这个世界如何运转：力量来源、社会结构、核心矛盾与氛围">${esc(
              world.summary || ''
            )}</textarea></div>
          <div class="field"><label>世界规则（每行一条）</label>
            <textarea class="textarea" id="nw-rules" rows="4" placeholder="例如：听到器物记忆需要触碰且会损耗寿数">${esc(
              joinLines(world.rules)
            )}</textarea></div>
          <div class="field"><label>画面风格（可选）</label>
            <textarea class="textarea" id="nw-visual" rows="2" placeholder="供分镜与封面使用的统一视觉描述">${esc(
              world.visual_style || ''
            )}</textarea></div>
        </div>
      </div>
      <div class="card">
        <div class="card-head"><span class="ct">分类条目</span></div>
        <div class="card-body" style="display:grid;gap:10px">
          <div class="hint">总述里提到的地点、势力、术语在这里以条目存在；点条目下钻编辑，条目页可返回总述并标记来源章节。</div>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">${categoryCards}</div>
        </div>
      </div>
    </div>`;
}

/**
 * 世界条目详情页：按分类编辑字段、标记来源章节，并可返回世界观总述。
 * @returns {string} HTML
 */
function worldEntryHTML() {
  const world = activeWorld();
  const category = ui.sel.category || 'location';
  const meta = CATEGORY_META[category];
  if (!world || !meta) return worldOverviewHTML();
  const base = ui.draftEntry ? {} : (world[meta.field] || []).find((item) => item.id === ui.sel.id);
  if (!base) {
    return `<div class="empty" style="margin:auto">${icon('image', 22)}<b>条目已不存在</b>
      <span>它可能刚被删除，回到总述查看当前条目。</span>
      <button class="btn sm" data-act="novel-sel-world-overview" style="margin-top:8px">返回世界观总述</button></div>`;
  }
  const sources = new Set(base.source_chapters || []);
  const chapters = outlineChapters();
  const chapterBox = chapters.length
    ? `<div style="max-height:190px;overflow:auto;border:1px solid var(--line-1);border-radius:var(--radius-sm);padding:8px 10px;display:grid;gap:6px">
        ${chapters
          .map(
            ({ act, chapter }) => `<label style="display:flex;align-items:center;gap:8px">
              <input type="checkbox" data-source="${esc(chapter.id)}" ${sources.has(chapter.id) ? 'checked' : ''} />
              <span>${esc(chapter.title || '未命名章节')}</span>
              <span class="hint">${esc(act.title || '')}</span>
            </label>`
          )
          .join('')}
      </div>`
    : `<div class="hint">分层大纲还没有章节；生成大纲后即可标记来源章节。</div>`;
  const nameField =
    category === 'term'
      ? `<div class="field"><label>词条</label><input class="input" id="we-name" value="${esc(base.term || '')}" /></div>`
      : `<div class="field"><label>名称</label><input class="input" id="we-name" value="${esc(base.name || '')}" /></div>`;
  const detailFields =
    category === 'term'
      ? `<div class="field"><label>释义</label><textarea class="textarea" id="we-desc" rows="3">${esc(
          base.meaning || ''
        )}</textarea></div>
         <div class="field"><label>别名（逗号 / 顿号分隔）</label><input class="input" id="we-aliases" value="${esc(
           (base.aliases || []).join('、')
         )}" /></div>`
      : `<div class="field"><label>说明</label><textarea class="textarea" id="we-desc" rows="3">${esc(
          base.description || ''
        )}</textarea></div>
         ${
           category === 'location'
             ? `<div class="field"><label>氛围（可选）</label><input class="input" id="we-atmos" value="${esc(
                 base.atmosphere || ''
               )}" placeholder="供音效与画面提示词使用" /></div>`
             : ''
         }`;
  return `
    <div class="ed-head">
      <div style="min-width:0">
        <div class="et">${esc(entryName(category, base) || `新建${meta.label}`)}</div>
        <div class="em">世界「${esc(world.name || '未命名')}」 › ${esc(meta.label)}条目</div>
      </div>
      <div class="right">
        <button class="btn sm" data-act="novel-sel-world-overview">${icon('back', 12)} 返回总述</button>
        ${ui.draftEntry ? '' : `<button class="btn sm" data-act="novel-world-entry-delete" style="color:var(--bad)">删除</button>`}
        <button class="btn sm primary" data-act="novel-world-entry-save">保存</button>
      </div>
    </div>
    <div class="vw-body" style="padding:16px;overflow:auto">
      <div class="card"><div class="card-body" style="display:grid;gap:10px">
        <div class="hint">${esc(meta.hint)}；条目属于当前世界「${esc(world.name || '未命名')}」，保存后可在世界观总述里下钻查看。</div>
        ${nameField}
        ${detailFields}
        <div class="field"><label>来源章节</label>
          <div class="hint" style="margin-bottom:4px">标记该条目在哪些章节出现，生成正文时用于检索；可多选。</div>
          ${chapterBox}
        </div>
      </div></div>
    </div>`;
}

/** 大纲章详情：标题/概要/节拍可编辑，提供黄金三章检查与删除 */
function chapterHTML() {
  const hit = findOutlineChapter(ui.sel.id);
  if (!hit) {
    return `<div class="empty" style="margin:auto">${icon('list', 22)}<b>先在左侧选一章</b>
      <span>大纲按「幕 → 章 → 节拍」组织，生成后可逐条改</span></div>`;
  }
  const { act, chapter } = hit;
  const landed = landedChapter(chapter.id);
  const beats = (chapter.beats || [])
    .map(
      (beat, index) => `
      <div class="novel-beat${beat.turning_point ? ' turning' : ''}" data-beat="${index}">
        <div class="novel-beat-head">
          <span class="mono">节拍 ${index + 1}</span>
          ${beat.target_words ? `<span class="badge mute">${Number(beat.target_words)} 字</span>` : ''}
          ${(beat.character_ids || []).length ? `<span class="badge mute">${beat.character_ids.length} 人</span>` : ''}
          <label class="beat-turn" style="margin-left:auto" title="标记为本章转折（小高潮），黄金三章检查据此判断">
            <input type="checkbox" id="nb-beat-turn-${index}" data-beat-turn="${index}"
                   ${beat.turning_point ? 'checked' : ''} /> 转折（小高潮）
          </label>
          <button class="btn icon sm" data-act="novel-beat-delete" data-index="${index}" title="删除该节拍">${icon('trash', 12)}</button>
        </div>
        <textarea class="textarea" id="nb-beat-${index}" rows="2">${esc(beat.summary || '')}</textarea>
      </div>`
    )
    .join('');
  return `
    <div class="ed-head">
      <div style="min-width:0">
        <div class="et">${esc(chapter.title || '未命名章节')}</div>
        <div class="em">第 ${Number(act.index || 0)} 幕 · ${esc(act.title || '')} ·
          ${landed ? `已落章（${Number(landed.word_count || 0)} 字）` : '尚未落成正文'}${
            chapter.origin ? ` · ${esc(originLabel(chapter.origin))}` : ''
          }</div>
      </div>
      <div class="right">
        <button class="btn sm primary" data-act="novel-outline-save">保存</button>
        <a class="btn sm" href="#/p/${esc(safePath())}/c/${esc(chapter.id)}" ${landed ? '' : 'hidden'}>去画本</a>
        <button class="btn sm" data-act="novel-chapter-delete" style="color:var(--bad)">删除本章</button>
      </div>
    </div>
    <div class="vw-body" style="padding:16px;overflow:auto;display:grid;gap:12px">
      <div class="card"><div class="card-body" style="display:grid;gap:10px">
        <div class="field"><label>章节标题</label><input class="input" id="no-title" value="${esc(
          chapter.title || ''
        )}" /></div>
        <div class="field"><label>章节概要</label><textarea class="textarea" id="no-summary" rows="3">${esc(
          chapter.summary || ''
        )}</textarea></div>
        <div class="field"><label>幕概要（只读，改大纲时统一调整）</label>
          <div class="hint" style="line-height:1.6">${esc(act.summary || '—')}</div></div>
      </div></div>
      <div class="card" id="novel-beat-list">
        <div class="card-head"><span class="ct">节拍（${(chapter.beats || []).length}）</span>
          <div class="right hint">节拍是正文生成的最小单位 · 勾选「转折（小高潮）」标记本章反转</div>
        </div>
        <div class="card-body" style="display:grid;gap:10px">
          ${beats || '<div class="hint">这一章还没有节拍</div>'}
        </div>
      </div>
      ${linkCardHTML(chapter, landed)}
      ${bodyCardHTML(chapter, landed)}
    </div>`;
}

/**
 * 阻断项里「下一步」的站内地址：能直达就直接打开对应界面或位置。
 * @param {object} action 后端给出的 `{kind, label, chapter_id?, line_id?, character_id?, act?}`
 * @returns {string} 站内链接；无法定位时返回空串
 */
function blockerHref(action) {
  if (!action) return '';
  const base = `#/p/${safePath()}`;
  if (action.kind === 'character' && action.character_id) {
    return `${base}/characters?character=${encodeURIComponent(action.character_id)}`;
  }
  if (action.kind === 'characters') return `${base}/characters`;
  if (action.kind === 'models') return '#/settings?section=env';
  if (action.kind === 'novel') return `${base}/novel`;
  if (action.kind === 'chapter' && action.chapter_id) {
    const query = action.line_id
      ? `?line=${encodeURIComponent(action.line_id)}`
      : action.act === 'split'
        ? '?act=split'
        : '';
    return `${base}/c/${encodeURIComponent(action.chapter_id)}${query}`;
  }
  return '';
}

/**
 * 阻断项清单：每条给名称、涉及章节或数量、原因和下一步。
 * @param {Array<object>} blockers 后端返回的阻断项
 * @returns {string} HTML
 */
function blockerIssuesHTML(blockers) {
  const hard = blockers.filter((item) => !item.skippable).length;
  const head = hard
    ? `补齐配音前先处理这 ${hard} 项${blockers.length > hard ? `（另有 ${blockers.length - hard} 项可选择跳过）` : ''}`
    : `补齐配音前建议先处理这 ${blockers.length} 项（也可以选择跳过）`;
  const rows = blockers
    .map((item) => {
      const meta = [
        (item.chapters || []).join('、'),
        Number(item.count) > 1 ? `${Number(item.count)} ${item.unit || '处'}` : '',
      ]
        .filter(Boolean)
        .join(' · ');
      const href = blockerHref(item.action);
      return `<div class="banner-issue">
        <span class="bi-head"><b>${esc(item.title || '需要处理')}</b>${
          meta ? `<span class="bi-problem"> · ${esc(meta)}</span>` : ''
        }</span>
        ${item.detail ? `<span class="bi-why">${esc(item.detail)}</span>` : ''}
        ${href && item.action?.label ? `<a class="bi-jump" href="${href}">${esc(item.action.label)} ›</a>` : ''}
      </div>`;
    })
    .join('');
  return `<div class="banner warn" style="margin-top:8px">
    <div style="min-width:0;flex:1">
      <div>${icon('warn', 13)} ${esc(head)}</div>
      <div class="banner-issues">${rows}</div>
    </div>
  </div>`;
}

/** 流程衔接卡：正文 → 台词切分 → 配音，三步状态与动作 */
function linkCardHTML(chapter, landed) {
  if (!landed) {
    return `
      <div class="card" id="novel-link-card">
        <div class="card-head"><span class="ct">流程衔接</span><div class="right hint">落地后可切分台词并配音</div></div>
        <div class="card-body"><div class="hint">生成正文并保存后，本章会自动重建台词切分；之后再补齐配音。</div></div>
      </div>`;
  }
  const plan = cache.planChapter === chapter.id ? cache.plan : null;
  const status = landed.status || 'draft';
  const statusLabel = { draft: '草稿', written: '已写正文', split: '已切分', stale: '台词过期' }[status] || status;
  const statusClass = status === 'split' ? 'ok' : status === 'stale' ? 'warn' : 'mute';
  const blockers = plan?.blockers || [];
  const reuse = plan?.reuse || {};
  const missing = Number(plan?.total || 0);
  const lines = Number(plan?.lines_total || 0);
  const reused = Number(reuse.keep || 0) + Number(reuse.reuse || 0);
  const regenerate = Number(reuse.regenerate || 0);
  return `
    <div class="card" id="novel-link-card">
      <div class="card-head">
        <span class="ct">流程衔接</span>
        <span class="badge ${statusClass}">${esc(statusLabel)}</span>
        <div class="right">
          <a class="btn sm" href="#/p/${esc(safePath())}/c/${esc(chapter.id)}">${icon('chapter', 12)} 去画本</a>
          <button class="btn sm" data-act="novel-tts-batch" ${plan ? '' : 'disabled'}>
            ${icon('mic', 12)} ${missing ? `补齐配音（${missing}）` : '检查配音'}
          </button>
        </div>
      </div>
      <div class="card-body">
        <div style="display:flex;gap:18px;flex-wrap:wrap">
          <span>正文 <b class="mono">${Number(landed.word_count || 0)}</b> 字</span>
          <span>场景 <b class="mono">${Number(landed.scene_count || 0)}</b></span>
          <span>台词 <b class="mono">${plan ? lines : '—'}</b> 行</span>
          <span>待补配音 <b class="mono" style="color:var(--warn)">${plan ? missing : '—'}</b> 行</span>
          ${plan && reuse.regenerate !== undefined ? `<span>可复用 <b class="mono" style="color:var(--ok)">${reused}</b> 行</span>` : ''}
        </div>
        ${
          plan
            ? blockers.length
              ? blockerIssuesHTML(blockers)
              : `<div class="hint" style="margin-top:6px">预检通过：本次重新合成 ${regenerate} 行，其余 ${reused} 行沿用已有音频或命中缓存。</div>`
            : '<div class="hint" style="margin-top:6px">正在读取配音预检…</div>'
        }
      </div>
    </div>`;
}

/**
 * 读取本章配音补齐预检（只读，不启动任务）。
 * @param {string} chapterId 章节 ID
 */
async function loadPlan(chapterId) {
  if (!landedChapter(chapterId)) return;
  try {
    const plan = await getJSON('/api/ui/tts/batch-all/plan', {
      path: cache.path,
      scope: 'chapter',
      chapter: chapterId,
    });
    if (ui.sel.kind !== 'chapter' || ui.sel.id !== chapterId) return;
    cache.plan = plan;
    cache.planChapter = chapterId;
    const node = document.getElementById('novel-link-card');
    const hit = findOutlineChapter(chapterId);
    if (node && hit) node.outerHTML = linkCardHTML(hit.chapter, landedChapter(chapterId));
  } catch (error) {
    toast(`读取配音预检失败：${error.message}`, 'bad', 3600);
  }
}

/** 补齐本章配音：先展示预检，确认后启动后台批处理 */
function openBatchDialog() {
  const hit = findOutlineChapter(ui.sel.id);
  if (!hit) return;
  const plan = cache.planChapter === hit.chapter.id ? cache.plan : null;
  if (!plan) return toast('预检还没完成，稍等再点', 'warn');
  const blockers = plan.blockers || [];
  const hard = blockers.filter((item) => !item.skippable);
  const skippable = blockers.filter((item) => item.skippable);
  const skipCount = skippable.reduce((sum, item) => sum + Number(item.count || 0), 0);
  const reuse = plan.reuse || {};
  const reused = Number(reuse.keep || 0) + Number(reuse.reuse || 0);
  openDialog({
    title: '补齐本章配音',
    body: `
      <div class="summary-line"><span>本章台词</span><span class="right mono">${Number(plan.lines_total || 0)} 行</span></div>
      <div class="summary-line"><span>需要补</span><span class="right mono">${Number(plan.total || 0)} 行</span></div>
      ${
        reuse.regenerate !== undefined
          ? `<div class="summary-line"><span>复用 / 重新合成</span><span class="right mono">
               <span style="color:var(--ok)">复用 ${reused} 行</span> · 重新合成 ${Number(reuse.regenerate || 0)} 行</span></div>`
          : ''
      }
      <div class="summary-line"><span>已有音频跳过</span><span class="right mono">${Number(plan.skipped || 0)} 行</span></div>
      ${reuse.saved_ms_estimate ? `<div class="summary-line"><span>预计节省</span><span class="right mono" style="color:var(--ok)">约 ${fmtClock(reuse.saved_ms_estimate)}</span></div>` : ''}
      ${
        blockers.length
          ? blockerIssuesHTML(blockers)
          : '<div class="hint" style="margin-top:8px">只重配合词文本、说话人、音色或合成参数变过的行；任务在后台执行，进度与暂停/继续在章节右栏「本章任务」和录音 / 后期视图里。</div>'
      }
      ${
        skipCount
          ? `<label class="checkline"><input type="checkbox" id="novel-skip-unknown" />
               <span>跳过这 ${skipCount} 行没指定说话人的台词，先配其余行</span></label>`
          : ''
      }
      ${reuseListHTML(plan)}`,
    foot: `<div class="right"><button class="btn" data-dlg-close>取消</button>
      <button class="btn primary" data-batch-run ${
        hard.length || !Number(plan.total) ? 'disabled' : ''
      }>
        ${icon('mic', 12)} 开始补齐</button></div>`,
    onMount: (mask) => {
      const button = mask.querySelector('[data-batch-run]');
      const skipBox = mask.querySelector('#novel-skip-unknown');
      // 只有「没指定说话人」这类可跳过项时，勾选后才能开始
      const refreshButton = () => {
        button.disabled = Boolean(hard.length || !Number(plan.total) || (skipCount > 0 && !skipBox?.checked));
      };
      skipBox?.addEventListener('change', refreshButton);
      refreshButton();
      button.addEventListener('click', async () => {
        button.disabled = true;
        button.textContent = '启动中…';
        try {
          const result = await postJSON('/api/ui/tts/batch-all', {
            path: cache.path,
            scope: 'chapter',
            chapter: hit.chapter.id,
            skip_unknown: Boolean(skipBox?.checked),
          });
          closeDialog();
          toast(`已开始补齐 ${Number(result?.total || 0)} 行，进度见浮层或章节右栏「本章任务」`, 'ok', 3200);
          watchProgress({
            path: cache.path,
            title: '正在补齐本章配音…',
            hint: `本次 ${Number(result?.total || 0)} 行 · 已有音频自动跳过`,
            chapter: hit.chapter.id,
          });
          cache.plan = null;
          cache.planChapter = '';
          paint();
          setTimeout(() => loadPlan(hit.chapter.id), 1200);
        } catch (error) {
          toast(`启动失败：${error.message}`, 'bad', 3600);
          button.disabled = false;
          button.textContent = '开始补齐';
        }
      });
    },
  });
}

/** 章节正文卡：未落章先引导落地；已落章提供保存 / 重新生成 / 续写 */
function bodyCardHTML(chapter, landed) {
  if (!landed) {
    return `
      <div class="card" id="novel-body-card">
        <div class="card-head"><span class="ct">章节正文</span><div class="right hint">尚未落章</div></div>
        <div class="card-body">
          <div class="hint">先点左上角「落地成章节」把大纲建成章节文件，之后才能生成正文与续写。</div>
        </div>
      </div>`;
  }
  const loading = cache.bodyChapter !== chapter.id;
  const content = loading ? '' : cache.body?.content || '';
  const words = loading ? Number(landed.word_count || 0) : Number(cache.body?.word_count || 0);
  const hasBody = words > 0;
  const draft = cache.bodyChapter === chapter.id ? cache.draft : null;
  return `
    <div class="card" id="novel-body-card">
      <div class="card-head">
        <span class="ct">章节正文</span>
        <span class="badge mute" id="novel-body-words">${words} 字</span>
        <div class="right">
          <button class="btn sm" data-act="novel-body-versions">版本记录</button>
          <button class="btn sm" data-act="novel-body-save">保存正文</button>
          <button class="btn sm" data-act="novel-body-generate">${icon('spark', 12)} ${hasBody ? '重新生成' : '生成正文'}</button>
          <button class="btn sm primary" data-act="novel-body-continue" ${hasBody ? '' : 'disabled'}>续写</button>
        </div>
      </div>
      <div class="card-body" style="display:grid;gap:8px">
        ${
          draft?.exists
            ? `<div class="banner warn" id="novel-draft">
                 ${icon('warn', 13)} 有一份未应用的续写草稿（+${Number(draft.draft?.added_words || 0)} 字）
                 <span style="margin-left:auto;display:flex;gap:6px">
                   <button class="btn sm" data-act="novel-draft-apply">应用</button>
                   <button class="btn sm" data-act="novel-draft-regen">重抽</button>
                 </span>
               </div>`
            : ''
        }
        <textarea class="textarea" id="no-body" rows="16"
          placeholder="${loading ? '正在读取正文…' : hasBody ? '正文…' : '还没有正文，点右上「生成正文」让 AI 起草'}">${esc(
            content
          )}</textarea>
        <div class="hint">保存正文会重建台词切分并标记过期；生成按节拍流式输出、完成后直接写入正文；续写先存草稿，确认后写入。覆盖前会自动存档旧版本。</div>
      </div>
    </div>`;
}

/** 主区 HTML：按选中对象切换；对应数据块未就绪时先显示骨架或失败原因 */
function mainHTML() {
  if (ui.sel.kind === 'worldOverview' || ui.sel.kind === 'worldEntry') {
    if (boot.state.worlds !== 'ok') return bootBlockHTML('worlds', '世界');
    return ui.sel.kind === 'worldEntry' ? worldEntryHTML() : worldOverviewHTML();
  }
  if (ui.sel.kind === 'chapter') {
    if (boot.state.outline !== 'ok') return bootBlockHTML('outline', '分层大纲');
    return chapterHTML();
  }
  if (boot.state.setting !== 'ok') return bootBlockHTML('setting', '写作设定');
  return settingHTML();
}

/**
 * 主区数据块的骨架 / 失败提示（失败时给出中文原因与重试入口）。
 * @param {'setting'|'worlds'|'outline'} key 数据块
 * @param {string} label 用户可读名称
 * @returns {string} HTML
 */
function bootBlockHTML(key, label) {
  if (boot.state[key] === 'error') {
    return `<div class="boot-block bad" role="alert">
      <div class="bb-head">${icon('warn', 16)}<b>${esc(label)}读取失败</b></div>
      <div class="hint">${esc(boot.errors[key] || '未知原因，请稍后重试')}</div>
      <button class="btn sm" data-act="boot-retry" data-key="${key}">重新载入${esc(label)}</button>
    </div>`;
  }
  return `<div class="boot-block" aria-busy="true">
    <div class="bb-head">${icon('refresh', 15)}<b>正在读取${esc(label)}…</b></div>
    <div class="skel skel-line" style="width:38%"></div>
    <div class="skel skel-line" style="width:86%"></div>
    <div class="skel skel-line" style="width:68%"></div>
    <div class="skel skel-card"></div>
  </div>`;
}

/**
 * 空工程引导条：还没有章节时固定在主区顶部。
 * 给出「从写小说开始 / 导入已有正文」两条入口；已有大纲时再给「落地成章节」。
 * @returns {string} HTML；有章节时为空串
 */
function emptyGuideHTML() {
  // 章节列表还没返回时先不判断空工程，避免一闪而过的错误引导
  if (boot.state.chapters !== 'ok' || state.chapters.length) return '';
  const counts = outlineCounts();
  return `
    <div class="banner project-empty-banner">
      ${icon('book', 13)}
      <div class="pe-main">
        <b>这个工程还没有章节</b>
        <span>从写小说开始：先写设定与世界观，再生成分层大纲并落地成章节；也可以直接导入已有正文。</span>
      </div>
      <div class="right">
        <button class="btn sm" data-act="novel-start">${icon('book', 12)} 从写小说开始</button>
        ${
          counts.chapters
            ? `<button class="btn sm" data-act="novel-outline-apply">${icon('export', 12)} 把大纲落地成章节</button>`
            : ''
        }
        <button class="btn sm" data-act="empty-import">${icon('arrow', 12)} 导入已有正文</button>
      </div>
    </div>`;
}

/**
 * 落章后同步图标栏「目录」入口。
 * 小说视图首次进入时还没有章节，图标栏按空章节渲染成回项目列表；
 * 这里把入口改到第一章，保证落地后能直接进画本。
 */
function syncChapterEntry() {
  if (!state.chapterId) return;
  const link = [...document.querySelectorAll('#novel-root .railbar a.rail-item')].find(
    (node) => node.getAttribute('title') === '目录'
  );
  if (link) {
    link.setAttribute(
      'href',
      `#/p/${encodeURIComponent(cache.path)}/c/${encodeURIComponent(state.chapterId)}`
    );
  }
}

/** 顶栏右侧：进入画本 / 回工作区 */
function topRightHTML() {
  const chapter = ui.sel.kind === 'chapter' ? landedChapter(ui.sel.id) : null;
  return `
    ${chapter ? `<a class="btn sm" href="#/p/${esc(safePath())}/c/${esc(chapter.id)}">${icon('chapter', 12)} 去画本</a>` : ''}
    <a class="btn sm" href="#/p/${esc(safePath())}/tasks">${icon('task', 12)} 任务</a>`;
}

// ---------------- 写操作 ----------------

/** 保存世界观总述（无 id 时创建新世界） */
async function saveWorldOverview() {
  const base = findWorld(ui.worldId) || {};
  const isNew = !base.id;
  const payload = {
    ...base,
    name: document.getElementById('nw-name')?.value.trim() || '',
    era: document.getElementById('nw-era')?.value.trim() || '',
    summary: document.getElementById('nw-summary')?.value.trim() || '',
    rules: splitLines(document.getElementById('nw-rules')?.value),
    visual_style: document.getElementById('nw-visual')?.value.trim() || '',
    // 分类条目与封面不属于总述表单，保存时按原值带回
    factions: base.factions || [],
    locations: base.locations || [],
    glossary: base.glossary || [],
    others: base.others || [],
    cover_asset: base.cover_asset ?? null,
    created_at: base.created_at,
    updated_at: base.updated_at,
  };
  if (!payload.name) return toast('世界名称不能为空', 'warn');
  if (!payload.era) return toast('时代与舞台不能为空', 'warn');
  await run('保存世界', () => postJSON('/api/ui/worlds', { path: cache.path, world: payload }));
  cache.worlds = (await getJSON('/api/ui/worlds', { path: cache.path }))?.worlds || [];
  const matched = payload.id
    ? cache.worlds.find((item) => item.id === payload.id)
    : cache.worlds.find((item) => item.name === payload.name);
  if (matched) {
    ui.worldId = matched.id;
    ui.sel = { kind: 'worldOverview', id: matched.id };
  }
  ui.draftWorld = false;
  toast('世界观总述已保存', 'ok');
  // 新建世界需要重绘才能出现分类入口；已有世界只就地更新标题
  if (isNew) return paint();
  const head = document.querySelector('#novel-main .ed-head .et');
  if (head) head.textContent = payload.name;
}

/** 收集条目页勾选的来源章节 */
function collectSources() {
  return [...document.querySelectorAll('[data-source]')]
    .filter((node) => node.checked)
    .map((node) => node.dataset.source);
}

/** 保存当前分类条目（无 id 时追加到当前世界） */
async function saveWorldEntry() {
  const world = activeWorld();
  if (!world) return toast('先创建或选择一个世界', 'warn');
  const category = ui.sel.category;
  const meta = CATEGORY_META[category];
  if (!meta) return;
  const isNew = ui.draftEntry;
  const list = [...(world[meta.field] || [])];
  const index = list.findIndex((item) => item.id === ui.sel.id);
  const base = index >= 0 ? list[index] : {};
  const name = document.getElementById('we-name')?.value.trim() || '';
  if (!name) return toast(category === 'term' ? '词条不能为空' : '名称不能为空', 'warn');
  const sources = collectSources();
  const detail = document.getElementById('we-desc')?.value.trim() || '';
  const entry =
    category === 'term'
      ? {
          ...base,
          term: name,
          meaning: detail,
          aliases: splitAliases(document.getElementById('we-aliases')?.value),
          source_chapters: sources,
        }
      : {
          ...base,
          name,
          description: detail,
          ...(category === 'location'
            ? { atmosphere: document.getElementById('we-atmos')?.value.trim() || '' }
            : {}),
          source_chapters: sources,
        };
  if (isNew) list.push(entry);
  else list[index] = entry;
  const payload = {
    ...world,
    [meta.field]: list,
    cover_asset: world.cover_asset ?? null,
    created_at: world.created_at,
    updated_at: world.updated_at,
  };
  await run('保存条目', () => postJSON('/api/ui/worlds', { path: cache.path, world: payload }));
  const saved = (await getJSON('/api/ui/worlds', { path: cache.path }))?.worlds || [];
  cache.worlds = saved;
  const fresh = saved.find((item) => item.id === world.id) || world;
  const rows = fresh[meta.field] || [];
  const matched = isNew
    ? rows.find((row) => entryName(category, row) === name)
    : rows.find((row) => row.id === ui.sel.id);
  if (matched) ui.sel = { kind: 'worldEntry', category, id: matched.id };
  ui.draftEntry = false;
  toast('条目已保存', 'ok');
  // 新建条目需要重绘才能出现在左侧分类里；编辑已有条目只更新标题
  if (isNew) return paint();
  const head = document.querySelector('#novel-main .ed-head .et');
  if (head) head.textContent = name;
}

/** 删除当前分类条目（从所属世界的分类数组里移除） */
async function deleteWorldEntry() {
  const world = activeWorld();
  const category = ui.sel.category;
  const meta = CATEGORY_META[category];
  if (!world || !meta) return;
  const item = (world[meta.field] || []).find((row) => row.id === ui.sel.id);
  if (!item) return;
  if (!window.confirm(`删除${meta.label}条目「${entryName(category, item) || '未命名'}」？`)) return;
  const payload = {
    ...world,
    [meta.field]: (world[meta.field] || []).filter((row) => row.id !== ui.sel.id),
    cover_asset: world.cover_asset ?? null,
    created_at: world.created_at,
    updated_at: world.updated_at,
  };
  await run('删除条目', () => postJSON('/api/ui/worlds', { path: cache.path, world: payload }));
  cache.worlds = (await getJSON('/api/ui/worlds', { path: cache.path }))?.worlds || [];
  ui.sel = { kind: 'worldOverview', id: world.id };
  ui.draftEntry = false;
  toast('条目已删除', 'ok');
  paint();
}

/** 删除整套世界：确认影响范围 → 进行中 → 成功刷新列表 / 失败保留弹窗并给出原因 */
async function deleteWorld() {
  const world = findWorld(ui.worldId);
  if (!world) return toast('没有可删除的世界，请先在左侧选择一套世界', 'warn');
  const isLast = cache.worlds.length <= 1;
  const counts = Object.entries(CATEGORY_META)
    .map(([category, meta]) => `${meta.label} ${(world[meta.field] || []).length}`)
    .join(' · ');
  const bound = outlineChapters().filter((row) => (row.chapter.world_ids || []).includes(world.id)).length;
  const mask = openDialog({
    title: '删除整套世界',
    body: `
      <div class="dlg-note bad">删除的是整套世界「${esc(world.name || '未命名')}」，不只是世界观总述。</div>
      <div class="summary-line"><span>${esc(world.name || '未命名')}</span><span class="right">${esc(
        world.era || '未填写时代'
      )}</span></div>
      <div class="hint">会同时删除：世界观总述、${esc(counts)}。${
        bound ? `另有 ${bound} 个章节绑定该世界，删除后会解除绑定。` : ''
      }删除后不可恢复。</div>
      ${
        isLast
          ? '<div class="hint" style="color:var(--warn)">这是最后一套世界：删除后「世界」模块会变空，可以重新新建或让 AI 生成。</div>'
          : ''
      }`,
    foot: `<div class="right"><button class="btn" id="world-del-cancel" data-dlg-close>取消</button>
      <button class="btn danger" id="world-del-ok">删除世界</button></div>`,
  });
  const button = mask.querySelector('#world-del-ok');
  const cancel = mask.querySelector('#world-del-cancel');
  button.addEventListener('click', async () => {
    button.disabled = true;
    cancel.disabled = true;
    button.textContent = '删除中…';
    let result;
    try {
      result = await postJSON('/api/ui/worlds/delete', { path: cache.path, id: world.id });
    } catch (error) {
      // 删除失败不关弹窗：保留影响说明，按钮可重试，原因直接显示
      button.disabled = false;
      cancel.disabled = false;
      button.textContent = '重试删除';
      toast(`删除失败：${error.message}`, 'bad', 4600);
      return;
    }
    closeDialog();
    // 先从本地缓存移除，保证列表立刻刷新；再拉取一次完整数据校准
    cache.worlds = cache.worlds.filter((item) => item.id !== world.id);
    try {
      const fresh = await getJSON('/api/ui/worlds', { path: cache.path });
      if (Array.isArray(fresh?.worlds)) cache.worlds = fresh.worlds;
    } catch (error) {
      toast(`世界已删除，但列表刷新失败：${error.message}`, 'warn', 4600);
    }
    const next = cache.worlds[0];
    ui.worldId = next?.id || '';
    ui.draftWorld = false;
    ui.draftEntry = false;
    // 删掉最后一套世界时保留「世界总述」空态，避免用户以为模块消失了
    ui.sel = next ? { kind: 'worldOverview', id: next.id } : { kind: 'worldOverview', id: '' };
    const unbound = result?.unbound?.outline_chapters || 0;
    toast(
      `已删除世界「${world.name || '未命名'}」及其全部条目${unbound ? `，并解除 ${unbound} 个章节绑定` : ''}`,
      'ok',
      3400
    );
    paint();
  });
}

/** 新建空白世界：直接切到空白总述表单，保存时才落盘 */
function newWorld() {
  ui.worldId = '';
  ui.sel = { kind: 'worldOverview', id: '' };
  ui.draftWorld = true;
  ui.draftEntry = false;
  paint();
  document.getElementById('nw-name')?.focus();
}

/** 在指定分类下新建条目：先显示空白表单，保存时落盘 */
function newEntry(category) {
  if (!CATEGORY_META[category]) return;
  if (!activeWorld()) return toast('先创建或选择一个世界', 'warn');
  openWorldGroup(category);
  ui.sel = { kind: 'worldEntry', category, id: '' };
  ui.draftWorld = false;
  ui.draftEntry = true;
  paint();
  document.getElementById('we-name')?.focus();
}

/** 读取设定分区表单并保存（数组字段按行拆分） */
async function saveSetting(section) {
  const fields = {};
  const read = (id) => document.getElementById(id)?.value.trim() || '';
  if (section === 'premise') {
    const words = Number(document.getElementById('ns-words')?.value || 0);
    Object.assign(fields, {
      logline: read('ns-logline'),
      theme: read('ns-theme'),
      core_conflict: read('ns-conflict'),
      ending_intent: read('ns-ending'),
      target_audience: read('ns-audience'),
      target_length: read('ns-length'),
      // 目标总字数是可选数字字段：0 表示清空，但仍要写入以覆盖旧值
      target_words: Number.isFinite(words) && words > 0 ? Math.round(words) : 0,
    });
  } else {
    Object.assign(fields, {
      tone: read('ns-tone'),
      pov: read('ns-pov'),
      tense: read('ns-tense'),
      prose_style: read('ns-prose'),
      visual_style: read('ns-visual'),
      forbidden: splitLines(document.getElementById('ns-forbidden')?.value),
    });
  }
  const keepEmpty = new Set(['target_words', 'forbidden']);
  const clean = Object.fromEntries(
    Object.entries(fields).filter(([key, value]) =>
      keepEmpty.has(key) ? true : Array.isArray(value) ? value.length : value
    )
  );
  if (!Object.keys(clean).length) return toast('没有可保存的内容', 'warn');
  await run('保存设定', () => postJSON('/api/ui/setting/apply', { path: cache.path, section, fields: clean }));
  cache.setting = await getJSON('/api/ui/setting', { path: cache.path });
  toast('设定已保存', 'ok');
  // 保存成功后不重绘页面：保住输入焦点与滚动位置（失败时也不会走到这里）
}

/** 保存大纲章：标题 / 概要 / 各节拍摘要与转折标记（节拍序号从 1 开始，与后端一致） */
async function saveOutlineChapter() {
  const hit = findOutlineChapter(ui.sel.id);
  if (!hit) return;
  const chapter = hit.chapter;
  const title = document.getElementById('no-title')?.value.trim() || '';
  const summary = document.getElementById('no-summary')?.value.trim() || '';
  await run('保存章节', async () => {
    if (title !== (chapter.title || '') || summary !== (chapter.summary || '')) {
      await postJSON('/api/ui/outline/text', { path: cache.path, chapter: chapter.id, title, summary });
    }
    for (let index = 0; index < (chapter.beats || []).length; index += 1) {
      const beat = chapter.beats[index];
      const value = document.getElementById(`nb-beat-${index}`)?.value.trim() || '';
      if (value !== (beat.summary || '')) {
        await postJSON('/api/ui/outline/text', {
          path: cache.path,
          chapter: chapter.id,
          beat: index + 1,
          summary: value,
        });
      }
      const turning = Boolean(document.getElementById(`nb-beat-turn-${index}`)?.checked);
      if (turning !== Boolean(beat.turning_point)) {
        await postJSON('/api/ui/outline/text', {
          path: cache.path,
          chapter: chapter.id,
          beat: index + 1,
          turning_point: turning,
        });
      }
    }
  });
  cache.outline = await getJSON('/api/ui/outline', { path: cache.path });
  toast('大纲已保存', 'ok');
  // 就地更新标题，不整页重绘，避免输入焦点与滚动位置丢失
  const head = document.querySelector('#novel-main .ed-head .et');
  if (head) head.textContent = title || '未命名章节';
  const row = document.querySelector(`[data-act="novel-sel-chapter"][data-id="${esc(chapter.id)}"] .novel-chapter-title`);
  if (row) {
    row.textContent = title || '未命名章节';
    row.setAttribute('title', title || '未命名章节');
  }
  // 转折标记会影响黄金三章结论：结果条已展示时立刻重查并刷新
  await refreshCheckBanner();
}

/** 重跑黄金三章检查并就地替换主区顶部的结果条（不重绘主区，保住输入焦点） */
async function refreshCheckBanner() {
  if (!cache.check) return;
  const slot = document.getElementById('novel-check-slot');
  if (!slot) return;
  try {
    cache.check = await getJSON('/api/ui/outline/check', { path: cache.path });
  } catch {
    // 重查失败保留上一次结果，不影响刚保存的大纲
    return;
  }
  slot.innerHTML = checkBannerHTML();
}

/**
 * 通用写操作包装：统一忙碌态、错误提示与按钮禁用。
 * @param {string} label 操作名（用于提示）
 * @param {() => Promise<any>} action 实际操作
 */
async function run(label, action) {
  ui.busy = label;
  try {
    return await action();
  } catch (error) {
    toast(`${label}失败：${error.message}`, 'bad', 3600);
    throw error;
  } finally {
    ui.busy = '';
  }
}

// ---------------- AI 弹窗 ----------------

/** AI 生成世界库条目：先出预览，勾选后逐条创建 */
function openWorldGenerate() {
  const modeKey = 'world.generate';
  openDialog({
    title: 'AI 生成世界',
    body: `
      ${modeSwitchHTML(modeKey, { fallback: 'fill' })}
      <div class="field"><label>题材 / 一句话方向</label>
        <textarea class="textarea" id="wg-brief" rows="3" placeholder="例：民国上海，一个能听见器物记忆的当铺伙计"></textarea></div>
      <div class="field"><label>生成数量</label>
        <select class="select" id="wg-count">
          <option value="1">1 个</option><option value="2">2 个</option><option value="3">3 个</option>
        </select></div>
      <div class="hint">生成结果先预览，勾选「采用」后才会写入世界；每套世界包含世界观总述与地点/势力/术语/其它条目。「只补缺失」会跳过与现有世界重名的条目。</div>`,
    foot: `<div class="right"><button class="btn" data-dlg-close>取消</button>
      <button class="btn primary" data-wg-run>${icon('spark', 12)} 生成预览</button></div>`,
    onMount: (mask) => {
      const modeOf = bindModeSwitch(mask, modeKey, {
        fallback: 'fill',
        action: mask.querySelector('[data-wg-run]'),
        labels: { fill: '补缺失预览', regen: '重新生成预览' },
      });
      mask.querySelector('[data-wg-run]').addEventListener('click', () => {
        const brief = mask.querySelector('#wg-brief').value.trim();
        const count = Number(mask.querySelector('#wg-count').value) || 1;
        if (!brief) return toast('先写一句方向', 'warn');
        const mode = modeOf();
        closeDialog();
        runWorldGenerate(brief, count, mode);
      });
    },
  });
}

/**
 * 执行世界生成（SSE 进度），完成后打开勾选预览。
 * @param {string} brief 方向描述
 * @param {number} count 生成数量
 * @param {'fill'|'regen'} [mode] 只补缺失 / 重新生成
 */
async function runWorldGenerate(brief, count, mode = 'fill') {
  const panel = openProgress({
    title: `正在${mode === 'fill' ? '补缺失' : '重新生成'}世界设定…`,
    hint: `${brief} · ${mode === 'fill' ? '跳过与现有世界重名的条目' : '忽略现有条目重新生成预览'}`,
    stage: '已提交生成请求，等待模型返回…',
  });
  panel.update({ percent: null });
  let settled = false;
  try {
    await streamJSON(
      '/api/ui/worlds/generate/stream',
      { path: cache.path, brief, count, overwrite: mode === 'regen' },
      (event, data) => {
        if (event === 'batch') {
          const index = Number(data.index || 1);
          const total = Number(data.total || 1) || 1;
          panel.update({
            stage: `正在生成第 ${index}/${total} 批世界条目…`,
            percent: index > 1 ? Math.round(((index - 1) / total) * 100) : null,
            current: `第 ${index} / ${total} 批`,
          });
        } else if (event === 'chars') {
          const index = Number(data.batch || data.index || 1);
          const total = Number(data.total || 1) || 1;
          panel.update({
            percent: null,
            current: `第 ${index}/${total} 批 · 已接收 ${Number(data.chars || 0)} 字`,
          });
        } else if (event === 'done') {
          settled = true;
          const all = data?.result?.worlds || [];
          panel.update({ percent: 100 });
          // 只补缺失：过滤掉与现有世界重名的条目，避免重复写入
          const existingNames = new Set(cache.worlds.map((item) => String(item.name || '').trim()));
          const worlds = mode === 'fill' ? all.filter((item) => !existingNames.has(String(item.name || '').trim())) : all;
          const skipped = all.length - worlds.length;
          panel.note(worlds.map((item) => item.name).filter(Boolean).join('、') || '模型已返回世界条目');
          if (!worlds.length) {
            panel.finish({
              message: skipped ? `生成结果与现有 ${skipped} 个世界重名，已全部跳过` : '模型没有返回可用的世界条目',
              detail: '要覆盖现有条目请选「重新生成」后重试。',
            });
            return;
          }
          ui.worldPreview = worlds;
          previewPicked = new Set(worlds.map((_, index) => index));
          panel.finish({
            message: `世界生成完成：${worlds.length} 个条目待采用${skipped ? `，跳过重名 ${skipped} 个` : ''}`,
            detail: '预览里勾选后点「采用选中」才会写入世界。',
            actions: [
              {
                label: '查看预览',
                primary: true,
                onClick: () => renderWorldPreview(),
              },
            ],
          });
          renderWorldPreview();
        } else if (event === 'canceled') {
          settled = true;
          panel.fail({ message: '已停止生成', detail: '任务已在任务队列中取消，已接收的内容不会写入世界库。' });
        } else if (event === 'error') {
          settled = true;
          panel.fail({ message: data?.message || '世界生成失败', retry: () => runWorldGenerate(brief, count, mode) });
        }
      }
    );
    if (!settled) {
      panel.fail({ message: '连接已中断，没有收到世界生成结果', retry: () => runWorldGenerate(brief, count, mode) });
    }
  } catch (error) {
    if (!settled) {
      panel.fail({ message: `世界生成失败：${error.message}`, retry: () => runWorldGenerate(brief, count, mode) });
    }
  }
}

/** 世界预览弹窗：勾选 + 采用 */
function renderWorldPreview() {
  const list = ui.worldPreview || [];
  if (!list.length) return toast('没有生成结果，换个方向再试', 'warn');
  openDialog({
    title: `世界预览（${list.length}）`,
    wide: true,
    body: `
      <div class="hint" style="margin-bottom:8px">勾选要采用的世界；采用后写入世界库，稍后可在详情里继续改。</div>
      ${list
        .map(
          (world, index) => `
        <div class="card" style="margin-bottom:10px">
          <div class="card-head">
            <label style="display:flex;align-items:center;gap:8px">
              <input type="checkbox" data-wp-pick="${index}" checked />
              <span class="ct">${esc(world.name || `世界 ${index + 1}`)}</span>
            </label>
            <div class="right hint">${esc(world.era || '')}</div>
          </div>
          <div class="card-body" style="display:grid;gap:6px">
            <div>${esc(world.summary || '')}</div>
            ${asStringList(world.rules).length ? `<div class="hint">规则：${esc(asStringList(world.rules).join('；'))}</div>` : ''}
            <div class="hint">地点 ${(world.locations || []).length} · 势力 ${(world.factions || []).length} · 术语 ${
              (world.glossary || []).length
            } · 其它 ${(world.others || []).length}</div>
            ${
              (world.locations || []).length
                ? `<div class="hint">地点：${esc(
                    asLocationList(world.locations)
                      .map((item) => item.name)
                      .filter(Boolean)
                      .join('、')
                  )}</div>`
                : ''
            }
          </div>
        </div>`
        )
        .join('')}`,
    foot: `<div class="right"><button class="btn" data-dlg-close>放弃</button>
      <button class="btn primary" data-wp-adopt>采用选中</button></div>`,
    onMount: (mask) => {
      mask.querySelector('[data-wp-adopt]').addEventListener('click', async () => {
        const picked = list.filter((_, index) => mask.querySelector(`[data-wp-pick="${index}"]`)?.checked);
        if (!picked.length) return toast('至少选一个世界', 'warn');
        const button = mask.querySelector('[data-wp-adopt]');
        button.disabled = true;
        button.textContent = '采用中…';
        try {
          await postJSON('/api/ui/worlds', { path: cache.path, worlds: picked });
          cache.worlds = (await getJSON('/api/ui/worlds', { path: cache.path }))?.worlds || [];
          const first = cache.worlds.find((item) => picked.some((row) => row.name === item.name));
          if (first) {
            ui.worldId = first.id;
            ui.sel = { kind: 'worldOverview', id: first.id };
          }
          closeDialog();
          toast(`已采用 ${picked.length} 个世界`, 'ok');
          paint();
        } catch (error) {
          toast(`采用失败：${error.message}`, 'bad', 3600);
          button.disabled = false;
          button.textContent = '采用选中';
        }
      });
    },
  });
}

/**
 * 生成选项：角色绑定（binding）。默认自动，关掉后只做引用清洗。
 * @returns {string} 表单 HTML 片段
 */
function bindingFieldHTML() {
  return `<div class="field"><label>角色绑定</label>
    <select class="select" data-bind-mode>
      <option value="auto" selected>自动绑定人物（过滤非法引用后回落到 POV）</option>
      <option value="none">只保留合法角色引用，不自动选角</option>
    </select>
    <div class="hint">自动绑定会按幕安排出场角色和视角，关掉后只做引用清洗。</div>
  </div>`;
}

/**
 * 把返回体里的绑定统计拼成完成摘要的一行。
 * @param {object} result SSE done 事件里的 result
 * @returns {string} 中文摘要；缺少 binding 字段时返回空串
 */
function bindingSummary(result) {
  if (!result || !result.binding) return '';
  const mode =
    result.binding === 'auto'
      ? '自动绑定人物（按幕安排出场角色和视角）'
      : '只保留合法角色引用（不自动选角）';
  const bound = Number(result.bound_characters || 0);
  const refs = Number(result.bound_character_refs || 0);
  const ignored = Number(result.ignored_character_refs || 0);
  return `角色绑定：${mode} · 绑定 ${bound} 个角色（${refs} 处节拍引用）· 忽略 ${ignored} 个非法引用`;
}

/** AI 生成大纲：参数确认后直接生成并落盘（生成结果整份替换大纲，不动已落章正文） */
function openOutlineGenerate() {
  const counts = outlineCounts();
  const modeKey = 'outline.generate';
  openDialog({
    title: 'AI 生成分层大纲',
    body: `
      ${modeSwitchHTML(modeKey, { fallback: 'regen' })}
      <div class="hint" style="margin-bottom:8px">
        当前大纲：${counts.acts} 幕 / ${counts.chapters} 章 / ${counts.beats} 个节拍。
        「只补缺失」在没有大纲时生成，已有大纲时不会覆盖；「重新生成」会替换整份大纲，但不会删除已落成的正文文件。
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
        <div class="field"><label>幕数</label><input class="input" id="og-acts" type="number" min="1" max="12" value="${counts.acts || 3}" /></div>
        <div class="field"><label>每幕章数</label><input class="input" id="og-chapters" type="number" min="1" max="20" value="3" /></div>
        <div class="field"><label>每章节拍</label><input class="input" id="og-beats" type="number" min="1" max="12" value="4" /></div>
        <div class="field"><label>每章目标字数</label><input class="input" id="og-words" type="number" min="500" step="500" value="3000" /></div>
      </div>
      <div class="field"><label>补充要求（可选）</label>
        <textarea class="textarea" id="og-brief" rows="2" placeholder="例：前三章必须完成身份揭示与第一次反转"></textarea></div>
      ${bindingFieldHTML()}
      <div class="hint" style="margin-top:4px">生成顺序：一次规划全部幕 → 逐幕生成章 → 逐章生成节拍，每层校验通过才继续。</div>`,
    foot: `<div class="right"><button class="btn" data-dlg-close>取消</button>
      <button class="btn primary" data-og-run>${icon('spark', 12)} 开始生成</button></div>`,
    onMount: (mask) => {
      const modeOf = bindModeSwitch(mask, modeKey, {
        fallback: 'regen',
        action: mask.querySelector('[data-og-run]'),
        labels: { fill: '补缺失', regen: '重新生成大纲' },
      });
      mask.querySelector('[data-og-run]').addEventListener('click', () => {
        const body = {
          path: cache.path,
          acts: Number(mask.querySelector('#og-acts').value) || 3,
          chapters_per_act: Number(mask.querySelector('#og-chapters').value) || 3,
          beats_per_chapter: Number(mask.querySelector('#og-beats').value) || 4,
          words: Number(mask.querySelector('#og-words').value) || 3000,
          binding: mask.querySelector('[data-bind-mode]').value || 'auto',
        };
        const brief = mask.querySelector('#og-brief').value.trim();
        if (brief) body.brief = brief;
        const mode = modeOf();
        closeDialog();
        runOutlineGenerate(body, mode);
      });
    },
  });
}

/**
 * 执行大纲生成（SSE 进度）：按幕逐批推进，完成后刷新目录。
 * @param {object} body 生成参数
 * @param {'fill'|'regen'} [mode] 只补缺失 / 重新生成
 */
async function runOutlineGenerate(body, mode = 'regen') {
  const counts = outlineCounts();
  // 只补缺失且已有大纲：不调用模型，避免把现有大纲整体覆盖掉
  if (mode === 'fill' && counts.acts > 0) {
    const panel = openProgress({
      title: '检查大纲缺失…',
      hint: '只补缺失不会覆盖现有大纲',
      stage: '已读取现有大纲',
    });
    panel.note(`现有 ${counts.acts} 幕 / ${counts.chapters} 章 / ${counts.beats} 个节拍`);
    panel.finish({
      message: `已有 ${counts.acts} 幕大纲，没有缺失需要补`,
      detail: '「只补缺失」不会覆盖现有大纲；要重写整份大纲请选「重新生成」。',
      actions: [
        {
          label: '改为重新生成',
          primary: true,
          onClick: (close) => {
            close();
            runOutlineGenerate(body, 'regen');
          },
        },
      ],
    });
    return;
  }
  const totalActs = Number(body.acts || 3);
  const panel = openProgress({
    title: `正在${mode === 'fill' ? '补缺失' : '重新生成'}分层大纲…`,
    hint: `${totalActs} 幕 × ${Number(body.chapters_per_act || 3)} 章 × ${Number(
      body.beats_per_chapter || 4
    )} 个节拍 · 每章约 ${Number(body.words || 3000)} 字${mode === 'fill' ? ' · 保留现有大纲' : ' · 覆盖现有大纲'}`,
    stage: '已提交生成请求，正在拆解幕结构…',
  });
  panel.update({ percent: null });
  let settled = false;
  const finishWithOutline = async (result) => {
    cache.outline = await getJSON('/api/ui/outline', { path: cache.path });
    const counts = outlineCounts();
    const first = outlineChapters()[0];
    ui.sel = first ? { kind: 'chapter', id: first.chapter.id } : { kind: 'setting', id: '' };
    paint();
    const firstAct = (cache.outline?.acts || [])[0];
    panel.note(firstAct ? `第 1 幕：${firstAct.title || '未命名幕'}` : `共 ${counts.acts} 幕`);
    const validation = result?.validation || null;
    const out = Array.isArray(validation?.out_of_range) ? validation.out_of_range : [];
    const summary =
      validation?.summary || `实际 ${counts.acts} 幕 / ${counts.chapters} 章 / ${counts.beats} 个节拍`;
    if (out.length) {
      // 越界只提示不改内容：标黄 + 原因，并提供按层重写/接受/稍后再调
      toast('大纲有幕超出允许区间，已按层重写仍未达标', 'warn', 5200);
      const actions = out.slice(0, 2).map((index) => ({
        label: `重新生成第 ${index} 幕`,
        onClick: (close) => {
          close();
          runOutlineGenerate({ ...body, resume: true, only_acts: [index] }, 'regen');
        },
      }));
      actions.push({
        label: '接受实际值',
        onClick: (close) => {
          close();
          toast(`已保留实际章数；下次生成可在对话框把每幕章数改为 ${out.length ? '实际值' : '设定值'}`, 'ok');
        },
      });
      actions.push({ label: '稍后手动调整', onClick: (close) => close() });
      panel.finish({
        message: `大纲已生成：${counts.acts} 幕 / ${counts.chapters} 章 / ${counts.beats} 个节拍（有越界）`,
        detail: [summary, bindingSummary(result)].filter(Boolean).join('；'),
        actions,
      });
      return;
    }
    panel.finish({
      message: `大纲已生成：${counts.acts} 幕 / ${counts.chapters} 章 / ${counts.beats} 个节拍`,
      detail: [
        result?.failures?.length ? `有 ${result.failures.length} 个批次失败，可在目录里补生成。` : summary,
        bindingSummary(result),
      ]
        .filter(Boolean)
        .join('；'),
      actions: [{ label: '查看大纲', primary: true, onClick: () => paint() }],
    });
    toast('大纲已生成', 'ok');
  };
  try {
    const totals = {
      acts: totalActs,
      chapters: totalActs * Number(body.chapters_per_act || 3),
      beats: totalActs * Number(body.chapters_per_act || 3) * Number(body.beats_per_chapter || 4),
    };
    await streamJSON('/api/ui/outline/hierarchical/stream', body, (event, data) => {
      if (!data) return;
      if (event === 'batch') {
        const info = outlineProgressText(data, totals);
        panel.update({
          stage: info.stage,
          current: info.current,
          // 后端给了整体百分比就按它走；否则在首项显示「进行中」
          percent: typeof data.percent === 'number' ? data.percent : null,
        });
      } else if (event === 'chars') {
        const info = outlineProgressText(data, totals);
        const chars = Number(data.chars || 0);
        panel.update({
          percent: null,
          stage: info.stage,
          current: `${info.current ? `${info.current} · ` : ''}已接收 ${chars} 字`,
        });
      } else if (event === 'stage') {
        // 阶段切换：幕 → 章 → 节拍，先把主文案切到新阶段，避免沿用上一阶段的单位
        const stage = String(data.stage || '');
        const names = { acts: '规划幕结构', chapters: '逐幕生成章节', beats: '逐章生成节拍' };
        if (names[stage]) panel.update({ stage: names[stage], percent: null, current: '' });
      } else if (event === 'rewrite') {
        const level = data.level === 'chapters' ? '章' : data.level === 'beats' ? '节拍' : '幕';
        // 章/节拍层重写带的是幕内章序号：补上幕序号，避免与全局章号混淆
        const who = data.chapter
          ? `第 ${data.index || '?'} 幕 · 第 ${data.chapter_index || ''} 章《${data.chapter}》`
          : `第 ${data.index || '?'} 幕`;
        panel.update({
          percent: null,
          stage: `第 ${data.attempt} 次重写 ${who}（${level}层）：按设定纠正中…`,
          current: data.reason || '',
        });
        panel.note(`第 ${data.attempt} 次重写 · ${level}层 · ${who} · ${data.reason || ''}`);
      } else if (event === 'done') {
        settled = true;
        panel.update({ percent: 100 });
        void finishWithOutline(data.result || {}).catch((error) =>
          panel.fail({
            message: `大纲已生成，但刷新目录失败：${error.message}`,
            retry: () => runOutlineGenerate(body, mode),
          })
        );
      } else if (event === 'canceled') {
        settled = true;
        panel.fail({ message: '已停止生成', detail: '任务已在任务队列中取消，已接收的内容不会写入大纲。' });
      } else if (event === 'error') {
        settled = true;
        // 可续跑时只补失败幕：复用后端保留的已完成幕
        const canResume = Boolean(data.resume || data.retryable);
        const gateStuck = /「(章|节拍)」层|「章」|「节拍」/.test(String(data.message || ''));
        panel.fail({
          message: data.message || '大纲生成失败',
          detail: data.detail || '',
          current: canResume
            ? gateStuck
              ? '已合格的幕与章已保留；点「重试」从当前层继续'
              : '已成功的幕已保留；点「重试」只补失败幕'
            : '',
          retry: () => runOutlineGenerate(canResume ? { ...body, resume: true } : body, mode),
        });
      }
    });
    if (!settled) {
      panel.fail({ message: '连接已中断，没有收到大纲结果', retry: () => runOutlineGenerate(body, mode) });
    }
  } catch (error) {
    if (!settled) {
      panel.fail({ message: `大纲生成失败：${error.message}`, retry: () => runOutlineGenerate(body, mode) });
    }
  }
}

/**
 * AI 扩充 / 续写大纲：在末尾续写、某幕后或某章后插入新内容。
 * 只新增，不重写已有幕与章；新增部分走同样的逐层校验。
 */
function openOutlineExpand() {
  const counts = outlineCounts();
  if (!counts.acts) {
    toast('还没有大纲，先整本生成一版再扩充', 'warn');
    return;
  }
  const scale = outlineScaleDefaults();
  const acts = cache.outline?.acts || [];
  const chapters = outlineChapters();
  const actOptions = acts
    .map((act, index) => `<option value="${index + 1}">第 ${index + 1} 幕《${esc(act.title || '')}》</option>`)
    .join('');
  const chapterOptions = chapters
    .map(({ chapter }) => `<option value="${Number(chapter.index || 0)}">第 ${Number(chapter.index || 0)} 章《${esc(chapter.title || '')}》</option>`)
    .join('');
  openDialog({
    title: 'AI 扩充 / 续写大纲',
    body: `
      <div class="hint" style="margin-bottom:8px">
        当前大纲：${counts.acts} 幕 / ${counts.chapters} 章 / ${counts.beats} 个节拍。
        扩充只新增内容：已有幕、章、节拍与已写正文、标注、配音都不改动；插入位置之后的编号自动顺延。
      </div>
      <div class="field"><label>插入位置</label>
        <select class="select" id="oe-mode">
          <option value="append">结局后续写（接在全书末尾）</option>
          <option value="after_act">在某幕之后扩充</option>
          <option value="after_chapter">在某章之后扩充</option>
        </select>
      </div>
      <div class="field" id="oe-act-wrap" hidden><label>插在第几幕之后</label>
        <select class="select" id="oe-anchor-act">${actOptions}</select></div>
      <div class="field" id="oe-chapter-wrap" hidden><label>插在第几章之后</label>
        <select class="select" id="oe-anchor-chapter">${chapterOptions}</select></div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
        <div class="field" id="oe-count-wrap"><label id="oe-count-label">新增幕数</label>
          <input class="input" id="oe-count" type="number" min="1" max="20" value="1" /></div>
        <div class="field"><label>每幕章数</label>
          <input class="input" id="oe-cpa" type="number" min="1" max="20" value="${scale.chaptersPerAct}" /></div>
        <div class="field"><label>每章节拍</label>
          <input class="input" id="oe-bpc" type="number" min="1" max="12" value="${scale.beatsPerChapter}" /></div>
        <div class="field"><label>每章目标字数</label>
          <input class="input" id="oe-words" type="number" min="500" step="500" value="${scale.chapterWords}" /></div>
      </div>
      <div class="field"><label>补充要求（可选）</label>
        <textarea class="textarea" id="oe-brief" rows="2" placeholder="例：插入一段反派视角的支线，交代他为何倒戈"></textarea></div>
      ${bindingFieldHTML()}`,
    foot: `<div class="right"><button class="btn" data-dlg-close>取消</button>
      <button class="btn primary" data-oe-run>${icon('spark', 12)} 开始生成</button></div>`,
    onMount: (mask) => {
      const modeEl = mask.querySelector('#oe-mode');
      const actWrap = mask.querySelector('#oe-act-wrap');
      const chapterWrap = mask.querySelector('#oe-chapter-wrap');
      const countWrap = mask.querySelector('#oe-count-wrap');
      const countLabel = mask.querySelector('#oe-count-label');
      const countInput = mask.querySelector('#oe-count');
      const cpaInput = mask.querySelector('#oe-cpa');
      /** 按插入方式切换可见字段与默认数量 */
      const sync = () => {
        const mode = modeEl.value;
        actWrap.hidden = mode !== 'after_act';
        chapterWrap.hidden = mode !== 'after_chapter';
        // 「每幕章数」只在新增幕时生效；某章后扩充用「新增章数」
        cpaInput.closest('.field').hidden = mode === 'after_chapter';
        countLabel.textContent = mode === 'after_chapter' ? '新增章数' : '新增幕数';
        if (mode === 'after_chapter') countInput.value = String(scale.chaptersPerAct);
        else if (!countInput.value || Number(countInput.value) > 12) countInput.value = '1';
        countWrap.hidden = false;
      };
      modeEl.addEventListener('change', sync);
      sync();
      mask.querySelector('[data-oe-run]').addEventListener('click', () => {
        const mode = modeEl.value;
        const body = {
          path: cache.path,
          mode,
          new_acts: mode === 'after_chapter' ? 1 : Number(countInput.value) || 1,
          chapters_per_act: Number(cpaInput.value) || scale.chaptersPerAct,
          beats_per_chapter: Number(mask.querySelector('#oe-bpc').value) || scale.beatsPerChapter,
          words: Number(mask.querySelector('#oe-words').value) || scale.chapterWords,
          binding: mask.querySelector('[data-bind-mode]').value || 'auto',
        };
        if (mode === 'after_act') body.anchor_act = Number(mask.querySelector('#oe-anchor-act').value) || counts.acts;
        if (mode === 'after_chapter') {
          body.anchor_chapter = Number(mask.querySelector('#oe-anchor-chapter').value) || counts.chapters;
          body.new_chapters = Number(countInput.value) || scale.chaptersPerAct;
        }
        const brief = mask.querySelector('#oe-brief').value.trim();
        if (brief) body.brief = brief;
        closeDialog();
        runOutlineExpand(body);
      });
    },
  });
}

/**
 * 执行大纲扩充 / 续写（SSE 进度）：逐层校验，完成后刷新目录并定位到新增内容。
 * @param {object} body 扩充参数
 */
async function runOutlineExpand(body) {
  const label = { after_act: '某幕后扩充', after_chapter: '某章后扩充', append: '结局后续写' }[body.mode] || '大纲扩充';
  const panel = openProgress({
    title: `正在${label}…`,
    hint: '只新增，不改动已有幕、章、节拍与已写正文',
    stage: '已提交请求，正在生成新增内容…',
  });
  panel.update({ percent: null });
  let settled = false;
  const chapterCount = Number(body.new_chapters || body.chapters_per_act || 0);
  const totals = {
    acts: Number(body.new_acts || 1),
    chapters: body.mode === 'after_chapter' ? chapterCount : Number(body.new_acts || 1) * Number(body.chapters_per_act || 0),
    beats:
      (body.mode === 'after_chapter' ? chapterCount : Number(body.new_acts || 1) * Number(body.chapters_per_act || 0)) *
      Number(body.beats_per_chapter || 0),
  };
  try {
    await streamJSON('/api/ui/outline/expand/stream', body, (event, data) => {
      if (!data) return;
      if (event === 'batch') {
        // unit=expand 是插入位置说明；其余是逐幕/逐章生成
        if (data.unit === 'expand') {
          panel.note(String(data.name || ''));
          return;
        }
        const info = outlineProgressText(data, totals);
        panel.update({
          stage: info.stage,
          current: info.current,
          percent: typeof data.percent === 'number' ? data.percent : null,
        });
      } else if (event === 'chars') {
        const info = outlineProgressText(data, totals);
        panel.update({
          percent: null,
          stage: info.stage,
          current: `${info.current ? `${info.current} · ` : ''}已接收 ${Number(data.chars || 0)} 字`,
        });
      } else if (event === 'rewrite') {
        const level = data.level === 'chapters' ? '章' : data.level === 'beats' ? '节拍' : '幕';
        // 章/节拍层重写带的是幕内章序号：补上幕序号，避免与全局章号混淆
        const who = data.chapter
          ? `第 ${data.index || '?'} 幕 · 第 ${data.chapter_index || ''} 章《${data.chapter}》`
          : `第 ${data.index || '?'} 幕`;
        panel.update({
          percent: null,
          stage: `第 ${data.attempt} 次重写 ${who}（${level}层）：按设定纠正中…`,
          current: data.reason || '',
        });
        panel.note(`第 ${data.attempt} 次重写 · ${level}层 · ${who} · ${data.reason || ''}`);
      } else if (event === 'done') {
        settled = true;
        panel.update({ percent: 100 });
        void finishWithExpand(data.result || {}, panel).catch((error) =>
          panel.fail({ message: `扩充已生成，但刷新目录失败：${error.message}`, retry: () => runOutlineExpand(body) })
        );
      } else if (event === 'canceled') {
        settled = true;
        panel.fail({ message: '已停止生成', detail: '任务已取消，未写入任何新增内容。' });
      } else if (event === 'error') {
        settled = true;
        panel.fail({
          message: data.message || '扩充失败',
          detail: data.detail || '',
          current: data.resume ? '新增内容未写入；点「重试」重新生成这一批' : '',
          retry: () => runOutlineExpand(body),
        });
      }
    });
    if (!settled) {
      panel.fail({ message: '连接已中断，没有收到扩充结果', retry: () => runOutlineExpand(body) });
    }
  } catch (error) {
    if (!settled) panel.fail({ message: `扩充失败：${error.message}`, retry: () => runOutlineExpand(body) });
  }
}

/**
 * 扩充完成后：刷新大纲、定位到第一个新增章节，并展示逐层校验摘要。
 * @param {object} result 后端返回
 * @param {object} panel 进度面板
 */
async function finishWithExpand(result, panel) {
  cache.outline = await getJSON('/api/ui/outline', { path: cache.path });
  const inserted = Array.isArray(result.inserted_chapters) ? result.inserted_chapters : [];
  const first = inserted[0];
  if (first?.chapter_id) ui.sel = { kind: 'chapter', id: first.chapter_id };
  paint();
  if (first?.chapter_id) {
    loadBody(first.chapter_id);
    loadPlan(first.chapter_id);
  }
  const counts = outlineCounts();
  const total = result.total || {};
  const validation = result.validation || null;
  const out = Array.isArray(validation?.out_of_range) ? validation.out_of_range : [];
  const summary = result.summary || `已新增 ${inserted.length} 章；全书 ${counts.acts} 幕 / ${counts.chapters} 章 / ${counts.beats} 个节拍`;
  if (out.length) {
    toast('新增部分有幕超出允许区间，已标注在摘要里', 'warn', 5200);
  }
  panel.finish({
    message: `${result.label || '扩充'}完成：新增 ${inserted.length} 章，全书 ${counts.acts} 幕 / ${counts.chapters} 章 / ${counts.beats} 个节拍`,
    detail: [
      summary + (total.acts ? `（全书现为 ${total.acts} 幕 / ${total.chapters} 章 / ${total.beats} 个节拍）` : ''),
      bindingSummary(result),
    ]
      .filter(Boolean)
      .join('；'),
    actions: [
      { label: '查看大纲', primary: true, onClick: () => paint() },
      ...(first?.chapter_id
        ? [{ label: '打开新增章节', onClick: () => { ui.sel = { kind: 'chapter', id: first.chapter_id }; paint(); loadBody(first.chapter_id); } }]
        : []),
    ],
  });
  toast(`${result.label || '扩充'}完成`, 'ok');
}

/** AI 补设定分区：预览字段后合并写入（世界观归世界模块，不走这里） */
function openSettingGenerate(section) {
  const label = { premise: '故事内核', style: '文风' }[section] || section;
  const modeKey = `setting.${section}`;
  openDialog({
    title: `AI 生成${label}`,
    body: `
      ${modeSwitchHTML(modeKey, { fallback: 'regen' })}
      <div class="field"><label>补充要求（可选）</label>
        <textarea class="textarea" id="sg-brief" rows="2" placeholder="例：更冷峻一些，突出器物记忆的规则"></textarea></div>
      <div class="field"><label>固定种子（可选）</label>
        <input class="input" id="sg-seed" type="number" placeholder="留空 = 每次随机" /></div>
      <div class="hint">随机性：温度 0.7，默认不固定种子，每次生成结果都会不同。填了种子会把 seed 传给模型，本机服务支持 seed 时可复现同一结果。</div>
      <div class="hint">生成结果先预览；点「写入设定」才覆盖对应字段，其余字段保持不动。</div>`,
    foot: `<div class="right"><button class="btn" data-dlg-close>取消</button>
      <button class="btn primary" data-sg-run>${icon('spark', 12)} 生成预览</button></div>`,
    onMount: (mask) => {
      const modeOf = bindModeSwitch(mask, modeKey, {
        action: mask.querySelector('[data-sg-run]'),
        labels: { fill: '补缺失', regen: '重新生成' },
      });
      mask.querySelector('[data-sg-run]').addEventListener('click', () => {
        const brief = mask.querySelector('#sg-brief').value.trim();
        const seedText = mask.querySelector('#sg-seed').value.trim();
        const seed = seedText ? Number(seedText) : null;
        if (seedText && !Number.isInteger(seed)) return toast('种子必须是整数', 'warn');
        const mode = modeOf();
        closeDialog();
        runSettingGenerate(section, label, brief, seed, mode);
      });
    },
  });
}

/**
 * 补全设定分区（无流式接口，用不确定进度浮层），完成后打开字段预览。
 * @param {string} section premise / style
 * @param {string} label 分区中文名
 * @param {string} brief 补充要求
 * @param {number|null} seed 固定种子；null 表示每次随机
 * @param {'fill'|'regen'} mode 只补缺失 / 重新生成
 */
async function runSettingGenerate(section, label, brief, seed = null, mode = 'regen') {
  const panel = openProgress({
    title: `正在${mode === 'fill' ? '补缺失' : '重新生成'}${label}…`,
    hint: `${mode === 'fill' ? '保留已有字段，只生成空缺' : '已有字段会被新结果覆盖'} · 温度取设置页「写作」分组${
      seed === null ? ' · 未固定种子' : ` · 种子 ${seed}`
    }`,
    stage: '已提交生成请求，等待模型返回…',
  });
  panel.update({ percent: null });
  try {
    // 生成可能持续数十秒：不设前端超时，避免把仍在跑的生成误判为失败
    const data = await postJSON(
      '/api/ui/setting/generate',
      { path: cache.path, section, brief, seed: seed === null ? undefined : seed, overwrite: mode !== 'fill' },
      { timeout: 0 }
    );
    const fields = data?.fields || data || {};
    const count = Object.keys(fields).length;
    const skipped = Array.isArray(data?.skipped) ? data.skipped.length : 0;
    // 非流式入口也要展示最近一条模型返回内容（一行灰色小字）
    panel.note(fields.logline || fields.summary || fields.tone || fields.premise || '模型已返回字段');
    panel.finish({
      message:
        mode === 'fill'
          ? `${label}补缺失完成：${count} 个字段，跳过已有 ${skipped} 个`
          : `${label}重新生成完成：${count} 个字段待确认`,
      detail: '确认后点「写入设定」才会覆盖对应字段。',
      actions: [
        { label: '查看字段', primary: true, onClick: () => openSettingPreview(section, label, fields) },
        // 明确的重新生成入口：不关闭浮层时也能直接再抽一版
        { label: '重新生成', onClick: (close) => {
          close();
          runSettingGenerate(section, label, brief, seed, 'regen');
        } },
      ],
    });
    openSettingPreview(section, label, fields);
  } catch (error) {
    panel.fail({
      message: `生成失败：${error.message}`,
      retry: () => runSettingGenerate(section, label, brief, seed, mode),
    });
  }
}

/** 设定预览：只展示业务字段，可编辑后写入 */
function openSettingPreview(section, label, fields) {
  // 每个分区允许出现的字段：系统字段（id/created_at/updated_at/cover_asset 等）
  // 与模型编造的多余键都不进表单，避免裸露英文键名与误改元数据。
  const allowed = {
    premise: ['logline', 'theme', 'core_conflict', 'ending_intent', 'target_audience', 'target_length', 'target_words'],
    style: ['tone', 'pov', 'tense', 'prose_style', 'forbidden', 'visual_style'],
  }[section] || [];
  const labelOf = {
    logline: '一句话梗概',
    theme: '主题',
    core_conflict: '核心冲突',
    ending_intent: '结局意图',
    target_audience: '目标读者',
    target_length: '目标篇幅',
    target_words: '目标总字数',
    tone: '语调',
    pov: '叙事视角',
    tense: '时态',
    prose_style: '行文风格',
    forbidden: '禁止项',
    visual_style: '视觉风格',
  };
  /** 长文本字段用多行，其余用单行/短行 */
  const longFields = new Set(['logline', 'theme', 'core_conflict', 'ending_intent', 'target_audience', 'tone', 'prose_style', 'visual_style']);
  const keys = allowed.filter((key) => key in fields);
  const rows = keys
    .map((key) => {
      const value = fields[key];
      const textLabel = esc(ENUMS[key]?.label || labelOf[key] || key);
      if (ENUMS[key]) {
        return `
      <div class="field"><label>${textLabel}</label>
        ${selectHTML(key, { current: value, emptyLabel: '未设置', attrs: `data-sf-key="${esc(key)}"` })}</div>`;
      }
      if (key === 'target_words') {
        return `
      <div class="field"><label>${textLabel}</label>
        <input class="input" type="number" min="0" step="1000" data-sf-key="${esc(key)}"
          value="${Number(value) > 0 ? Number(value) : ''}" placeholder="可留空" /></div>`;
      }
      if (Array.isArray(value) || key === 'forbidden') {
        const list = asStringList(value);
        return `
      <div class="field"><label>${textLabel}（每行一条）</label>
        <textarea class="textarea" data-free="1" data-sf-key="${esc(key)}" rows="3">${esc(joinLines(list))}</textarea></div>`;
      }
      const text = readableText(value);
      return longFields.has(key)
        ? `<div class="field"><label>${textLabel}</label>
            <textarea class="textarea" data-free="1" data-sf-key="${esc(key)}" rows="${key === 'visual_style' ? 3 : 2}">${esc(text)}</textarea></div>`
        : `<div class="field"><label>${textLabel}</label>
            <input class="input" data-free="1" data-sf-key="${esc(key)}" value="${esc(text)}" /></div>`;
    })
    .join('');
  openDialog({
    title: `预览：${label}`,
    wide: true,
    body: `<div style="display:grid;gap:10px">${rows || '<div class="hint">没有返回字段</div>'}</div>`,
    foot: `<div class="right"><button class="btn" data-dlg-close>放弃</button>
      <button class="btn primary" data-sf-apply>写入设定</button></div>`,
    onMount: (mask) => {
      mask.querySelector('[data-sf-apply]').addEventListener('click', async () => {
        const next = {};
        mask.querySelectorAll('[data-sf-key]').forEach((node) => {
          const key = node.dataset.sfKey;
          const original = fields[key];
          const rawText = node.value.trim();
          if (key === 'target_words') {
            next[key] = Number.isFinite(Number(rawText)) ? Math.max(0, Math.round(Number(rawText) || 0)) : 0;
            return;
          }
          // 空值不提交，避免把已有字段清空（显式清空请直接编辑表单后保存）
          if (!rawText) return;
          next[key] = Array.isArray(original) || key === 'forbidden' ? splitLines(rawText) : rawText;
        });
        if (!Object.keys(next).length) return toast('没有可写入的内容', 'warn');
        try {
          await postJSON('/api/ui/setting/apply', { path: cache.path, section, fields: next });
          cache.setting = await getJSON('/api/ui/setting', { path: cache.path });
          closeDialog();
          toast(`${label}已写入`, 'ok');
          paint();
        } catch (error) {
          toast(`写入失败：${error.message}`, 'bad', 3600);
        }
      });
    },
  });
}

/** 删除节拍（无确认，节拍可重新生成）；index 为页面上的 0 基序号，后端按 1 基接收 */
async function deleteBeat(index) {
  const hit = findOutlineChapter(ui.sel.id);
  if (!hit) return;
  await run('删除节拍', () =>
    postJSON('/api/ui/outline/beat/delete', { path: cache.path, chapter: hit.chapter.id, beat: index + 1 })
  );
  cache.outline = await getJSON('/api/ui/outline', { path: cache.path });
  toast('节拍已删除', 'ok');
  paint();
}

/** 删除大纲章：先 dry-run 取影响，再确认删除并同步删掉已落章文件 */
async function deleteChapter() {
  const hit = findOutlineChapter(ui.sel.id);
  if (!hit) return;
  const chapterId = hit.chapter.id;
  let impact = null;
  try {
    impact = await run('评估影响', () =>
      postJSON('/api/ui/outline/chapter/delete', { path: cache.path, chapter: chapterId, dry_run: true })
    );
  } catch {
    return; // run 已经弹出可读错误
  }
  const parts = [];
  if (impact?.landed) parts.push('已落成正文');
  if (impact?.chars) parts.push(`${Number(impact.chars)} 字`);
  if (impact?.lines) parts.push(`${Number(impact.lines)} 行台词`);
  if (impact?.audio) parts.push(`${Number(impact.audio)} 条音频`);
  if (impact?.image) parts.push(`${Number(impact.image)} 张图`);
  if (impact?.video) parts.push(`${Number(impact.video)} 个视频`);
  if (!window.confirm(`删除「${hit.chapter.title || '未命名章节'}」？\n影响：${parts.join(' · ') || '仅大纲'}`)) return;
  const button = document.querySelector('[data-act="novel-chapter-delete"]');
  if (button) {
    button.disabled = true;
    button.textContent = '删除中…';
  }
  try {
    // 删除会同步清理章节文件、脚本与时间轴：给足超时并落明确结果
    await run('删除章节', () =>
      postJSON('/api/ui/outline/chapter/delete', { path: cache.path, chapter: chapterId }, { timeout: 30000 })
    );
    cache.outline = await getJSON('/api/ui/outline', { path: cache.path });
    cache.chapters = await getJSON('/api/ui/chapters', { path: cache.path });
    // 图标栏与其它视图都读 state.chapters：同步更新，避免留下悬空引用
    state.chapters = cache.chapters;
    if (state.chapterId === chapterId) state.chapterId = cache.chapters[0]?.id || '';
    if (ui.sel.kind === 'chapter' && ui.sel.id === chapterId) ui.sel = { kind: 'setting', id: '' };
    toast('章节已删除', 'ok');
    paint();
    syncChapterEntry();
  } catch {
    if (button) {
      button.disabled = false;
      button.textContent = '删除本章';
    }
  }
}

/** 大纲落地成章节（幂等：已落章的不会重复建） */
async function applyOutline() {
  const result = await run('落地章节', () => postJSON('/api/ui/outline/apply', { path: cache.path }));
  cache.chapters = await getJSON('/api/ui/chapters', { path: cache.path });
  cache.outline = await getJSON('/api/ui/outline', { path: cache.path });
  // 其它视图与图标栏都读 state.chapters：落章后立刻同步，避免继续显示「没有章节」
  state.chapters = cache.chapters;
  if (!cache.chapters.some((chapter) => chapter.id === state.chapterId)) {
    state.chapterId = cache.chapters[0]?.id || '';
  }
  const created = result?.created?.length || result?.chapters?.length || 0;
  toast(created ? `已落地 ${created} 章` : '没有需要新建的章节', created ? 'ok' : 'info');
  paint();
  syncChapterEntry();
}

/** 黄金三章检查：结果放主区顶部，不阻断流程 */
async function checkOutline() {
  cache.check = await run('黄金三章检查', () => getJSON('/api/ui/outline/check', { path: cache.path }));
  const issues = checkIssues();
  toast(issues.length ? `发现 ${issues.length} 个问题（见主区）` : '黄金三章检查通过', issues.length ? 'warn' : 'ok');
  paint();
}

// ---------------- 章节正文 ----------------

/**
 * 读取当前章正文与未应用草稿；非落章章节直接清空缓存。
 * @param {string} chapterId 章节 ID
 */
async function loadBody(chapterId) {
  const landed = landedChapter(chapterId);
  if (!landed) {
    cache.body = null;
    cache.bodyChapter = chapterId;
    cache.draft = null;
    return;
  }
  try {
    const [body, draft] = await Promise.all([
      getJSON('/api/ui/chapter', { path: cache.path, chapter: chapterId }),
      getJSON('/api/ui/chapter/continue/draft', { path: cache.path, chapter: chapterId }).catch(() => null),
    ]);
    // 用户可能已切到别的章，丢弃过期结果
    if (ui.sel.kind !== 'chapter' || ui.sel.id !== chapterId) return;
    cache.body = body;
    cache.bodyChapter = chapterId;
    cache.draft = draft;
    refreshBodyCard();
  } catch (error) {
    toast(`读取正文失败：${error.message}`, 'bad', 3600);
  }
}

/** 只重画正文卡，保留标题/节拍等未保存编辑 */
function refreshBodyCard() {
  const hit = findOutlineChapter(ui.sel.id);
  const node = document.getElementById('novel-body-card');
  if (!hit || !node) return;
  node.outerHTML = bodyCardHTML(hit.chapter, landedChapter(hit.chapter.id));
}

/** 保存手工编辑后的正文 */
async function saveBody() {
  const hit = findOutlineChapter(ui.sel.id);
  if (!hit) return;
  const content = document.getElementById('no-body')?.value || '';
  if (!content.trim()) return toast('正文不能为空', 'warn');
  const result = await run('保存正文', () =>
    postJSON('/api/ui/chapter/save', { path: cache.path, chapter: hit.chapter.id, content })
  );
  cache.body = { ...(cache.body || {}), content, word_count: result?.word_count };
  cache.bodyChapter = hit.chapter.id;
  cache.chapters = await getJSON('/api/ui/chapters', { path: cache.path });
  refreshBodyCard();
  loadPlan(hit.chapter.id);
  toast(`正文已保存（${Number(result?.word_count || 0)} 字）`, 'ok');
}

/** 正文生成：确认参数后走 SSE 流，完成后刷新 */
function openGenerateDialog(hasBody) {
  const hit = findOutlineChapter(ui.sel.id);
  if (!hit) return;
  const modeKey = 'chapter.generate';
  openDialog({
    title: hasBody ? '重新生成本章正文' : 'AI 生成本章正文',
    body: `
      ${modeSwitchHTML(modeKey, { fallback: hasBody ? 'regen' : 'fill' })}
      ${hasBody ? '<div class="banner warn" style="margin-bottom:8px">选「重新生成」会覆盖当前正文；覆盖前会自动存档旧版本，可在「版本记录」里查看并一键恢复。选「只补缺失」则保留现有正文不调用模型。</div>' : ''}
      <div class="hint" style="margin-bottom:8px">按本章节拍逐轮写作：写完一组节拍即推进，全部节拍写完就收尾；篇幅只作参考。</div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
        <div class="field"><label>每轮节拍数</label><input class="input" id="gen-beats" type="number" min="0" step="1" value="0" />
          <div class="hint">0 = 按节拍总数自动分配</div></div>
        <div class="field"><label>最大轮数（保险）</label><input class="input" id="gen-rounds" type="number" min="1" max="20" value="8" /></div>
      </div>
      <label class="hint" style="display:flex;gap:6px;align-items:center;margin-top:4px">
        <input type="checkbox" id="gen-multi" checked /> 按节拍分轮（更稳，耗时更长）
      </label>`,
    foot: `<div class="right"><button class="btn" data-dlg-close>取消</button>
      <button class="btn primary" data-gen-run>${icon('spark', 12)} 开始生成</button></div>`,
    onMount: (mask) => {
      const modeOf = bindModeSwitch(mask, modeKey, {
        fallback: hasBody ? 'regen' : 'fill',
        action: mask.querySelector('[data-gen-run]'),
        labels: { fill: '生成缺失正文', regen: '重新生成并覆盖' },
      });
      mask.querySelector('[data-gen-run]').addEventListener('click', () => {
        const mode = modeOf();
        const body = {
          path: cache.path,
          chapter: hit.chapter.id,
          regen: mode === 'regen',
          multi_turn: mask.querySelector('#gen-multi').checked,
          beats_per_round: Number(mask.querySelector('#gen-beats').value) || null,
          max_rounds: Number(mask.querySelector('#gen-rounds').value) || null,
        };
        closeDialog();
        runGenerate(body, hasBody);
      });
    },
  });
}

/**
 * 执行正文生成（SSE）；进度走统一浮层，完成后可刷新正文。
 * @param {object} body 生成参数
 * @param {boolean} [hadBody] 打开弹窗时本章是否已有正文
 */
async function runGenerate(body, hadBody = false) {
  // 只补缺失且已有正文：不调用模型，避免报「已有正文」或误覆盖
  if (!body.regen && hadBody) {
    const landed = landedChapter(body.chapter);
    const panel = openProgress({
      title: '检查本章正文…',
      hint: '只补缺失不会覆盖已有正文',
      stage: '已读取本章正文',
    });
    panel.note(`现有正文 ${Number(landed?.word_count || cache.body?.word_count || 0)} 字`);
    panel.finish({
      message: '本章已有正文，已跳过生成',
      detail: '要重写正文请选「重新生成」；只想续写请用「AI 续写本章」。',
      actions: [
        {
          label: '改为重新生成',
          primary: true,
          onClick: (close) => {
            close();
            runGenerate({ ...body, regen: true }, true);
          },
        },
      ],
    });
    return;
  }
  const panel = openProgress({
    title: body.regen ? '正在重新生成本章正文…' : '正在生成本章正文…',
    hint: `${body.beats_per_round ? `每轮 ${Number(body.beats_per_round)} 个节拍` : '每轮节拍数自动分配'} · 最多 ${Number(
      body.max_rounds || 8
    )} 轮${body.regen ? ' · 覆盖现有正文' : ''}`,
    stage: '已提交生成请求，等待模型返回…',
    stream: true,
  });
  panel.update({ percent: null });
  let settled = false;
  try {
    await streamJSON('/api/ui/chapter/generate/stream', body, (event, data) => {
      if (event === 'start') {
        panel.update({ stage: '模型正在写作…', percent: null });
      } else if (event === 'delta') {
        panel.append(data?.text || '');
      } else if (event === 'done') {
        settled = true;
        const result = data?.result || {};
        const words = Number(result.word_count || result.words || 0);
        const beats = `${Number(result.beats_done || 0)}/${Number(result.beats_total || 0)}`;
        const deviation = result.word_deviation?.message || '';
        const summaryError = result.summary_error ? `摘要未更新：${result.summary_error}` : '';
        panel.finish({
          message: `正文生成完成：${words} 字 · 节拍 ${beats}${result.status ? ` · ${result.status}` : ''}`,
          detail: [deviation, summaryError, '结果已写入本章正文，可点「刷新正文」查看；如需回退可在「版本记录」恢复旧版本。']
            .filter(Boolean)
            .join('；'),
          actions: [
            {
              label: '刷新正文',
              primary: true,
              onClick: async () => {
                await reloadAfterBodyChange(body.chapter);
                toast('正文已刷新', 'ok');
              },
            },
          ],
        });
      } else if (event === 'canceled') {
        settled = true;
        panel.fail({ message: '已停止生成', detail: '任务已在任务队列中取消，已接收的内容不会写入正文。' });
      } else if (event === 'error') {
        settled = true;
        panel.fail({ message: data?.message || '生成失败', retry: () => runGenerate(body) });
      }
    });
    if (!settled) panel.fail({ message: '连接已中断，没有收到生成结果', retry: () => runGenerate(body) });
  } catch (error) {
    if (!settled) panel.fail({ message: `生成失败：${error.message}`, retry: () => runGenerate(body) });
  }
}

/**
 * 整幕正文生成：确认模式后按章节顺序一次跑完本幕所有章。
 * @param {object} act 幕对象
 * @param {number} actIndex 幕序号（从 1 开始）
 */
function openActGenerateDialog(act, actIndex) {
  const chapters = act.chapters || [];
  if (!chapters.length) return toast('这一幕还没有章节，先补齐大纲', 'warn');
  const landed = chapters.filter((chapter) => landedChapter(chapter.id));
  const hasBody = chapters.filter((chapter) => Number(landedChapter(chapter.id)?.word_count || 0) > 0);
  const modeKey = 'act.generate';
  const fallback = hasBody.length ? 'fill' : 'regen';
  openDialog({
    title: `生成整幕正文 · 第 ${actIndex} 幕《${act.title || '未命名幕'}》`,
    body: `
      ${modeSwitchHTML(modeKey, { fallback })}
      <div class="summary-line"><span>本幕章节</span><span class="right mono">${chapters.length} 章</span></div>
      <div class="summary-line"><span>已落成章节</span><span class="right mono">${landed.length} / ${chapters.length} 章</span></div>
      <div class="summary-line"><span>已有正文</span><span class="right mono">${hasBody.length} / ${chapters.length} 章</span></div>
      ${
        landed.length < chapters.length
          ? `<div class="banner warn" style="margin-top:8px">${icon('warn', 13)} 有 ${
              chapters.length - landed.length
            } 章还没落成章节文件，生成时会记为失败；可先回大纲点左上角「落地成章节」。</div>`
          : ''
      }
      <div class="hint" style="margin-top:8px">按章节顺序逐章生成，复用单章链路（按节拍推进、滚动摘要、版本历史）；单章失败不中断整幕；「只补缺失」会跳过已有正文并计入结果。</div>`,
    foot: `<div class="right"><button class="btn" data-dlg-close>取消</button>
      <button class="btn primary" data-ag-run>${icon('spark', 12)} 开始生成整幕</button></div>`,
    onMount: (mask) => {
      const modeOf = bindModeSwitch(mask, modeKey, {
        fallback,
        action: mask.querySelector('[data-ag-run]'),
        labels: { fill: '只补缺失并开始', regen: '重新生成并开始' },
      });
      mask.querySelector('[data-ag-run]').addEventListener('click', () => {
        const mode = modeOf();
        closeDialog();
        runActGenerate(act, actIndex, mode);
      });
    },
  });
}

/**
 * 执行整幕正文生成（SSE）：逐章展示第 k/N 章与整体进度，支持停止。
 * @param {object} act 幕对象
 * @param {number} actIndex 幕序号（从 1 开始）
 * @param {'fill'|'regen'} mode 只补缺失 / 重新生成已有正文
 */
async function runActGenerate(act, actIndex, mode) {
  const total = (act.chapters || []).length;
  const counters = { succeeded: 0, skipped: 0, failed: 0 };
  const failures = [];
  let settled = false;
  let jobId = '';
  const aborter = new AbortController();
  const panel = openProgress({
    title: `正在生成第 ${actIndex} 幕正文…`,
    hint: `共 ${total} 章 · 按章节顺序 · ${mode === 'regen' ? '重新生成已有正文' : '只补缺失'}`,
    stage: '已提交生成请求，准备第 1 章…',
    onStop: async () => {
      // 停止只终止后续章节：已生成完成的章已由单章链路落盘
      if (!jobId) {
        aborter.abort();
        finishStopped();
        return;
      }
      await postJSON('/api/ui/jobs/cancel', { path: cache.path, id: jobId });
      window.setTimeout(() => {
        if (!settled) {
          aborter.abort();
          finishStopped();
        }
      }, 3000);
    },
  });
  panel.update({ percent: 0 });

  /** 本地兜底：停止后即使没收到 canceled 事件，也给出明确结果 */
  function finishStopped() {
    if (settled) return;
    settled = true;
    panel.fail({
      message: '已停止生成',
      detail: `已完成章节已保存。本次已处理：成功 ${counters.succeeded} 章 / 跳过 ${counters.skipped} 章 / 失败 ${counters.failed} 章`,
    });
  }

  /** 生成结束后刷新章节目录与当前正文 */
  async function refreshAfterAct() {
    try {
      cache.chapters = await getJSON('/api/ui/chapters', { path: cache.path });
      if (ui.sel.kind === 'chapter') await loadBody(ui.sel.id);
      paint();
      toast('章节目录与正文已刷新', 'ok', 1600);
    } catch (error) {
      toast(`刷新失败：${error.message}`, 'bad', 3600);
    }
  }

  try {
    await streamJSON(
      '/api/ui/act/generate/stream',
      {
        path: cache.path,
        act: act.id || '',
        act_index: actIndex,
        mode,
      },
      (event, data) => {
        if (event === 'start') {
          jobId = data?.job_id || '';
          panel.update({ stage: '已提交生成请求，准备第 1 章…', percent: 0 });
        } else if (event === 'act_start') {
          panel.update({ stage: `本幕共 ${Number(data?.total || total)} 章 · 按章节顺序生成`, percent: 0 });
        } else if (event === 'chapter_start') {
          const index = Number(data?.index || 1);
          const count = Number(data?.total || total);
          panel.update({
            stage: `第 ${index}/${count} 章 · 正在生成《${data?.title || ''}》`,
            percent: Math.round(((index - 1) / Math.max(1, count)) * 100),
            current: '',
          });
        } else if (event === 'delta') {
          panel.append(data?.text || '');
        } else if (event === 'chapter_skip') {
          counters.skipped += 1;
          panel.note(`第 ${Number(data?.index || 0)}/${Number(data?.total || total)} 章《${data?.title || ''}》已有正文，已跳过`);
        } else if (event === 'chapter_done') {
          counters.succeeded += 1;
          const count = Number(data?.total || total);
          const index = Number(data?.index || 0);
          panel.update({
            stage: `第 ${index}/${count} 章《${data?.title || ''}》完成 · ${Number(data?.words || 0)} 字`,
            percent: Math.round((index / Math.max(1, count)) * 100),
            current: '',
          });
        } else if (event === 'chapter_error') {
          counters.failed += 1;
          failures.push(`第 ${Number(data?.index || 0)} 章《${data?.title || ''}》：${data?.message || '未知原因'}`);
        } else if (event === 'done') {
          settled = true;
          const result = data?.result || {};
          const failedRows = (result.chapters || []).filter((row) => row.status === 'failed');
          panel.finish({
            message: `整幕生成完成：成功 ${Number(result.succeeded || 0)} 章 / 跳过 ${Number(
              result.skipped || 0
            )} 章 / 失败 ${Number(result.failed || 0)} 章`,
            detail: failedRows.length
              ? `失败章节：${failedRows
                  .map((row) => `第 ${row.index} 章《${row.title || ''}》${row.message || '未知原因'}`)
                  .join('；')}`
              : '已完成的章节已写入正文，旧正文自动存档，可在「版本记录」里回退。',
            actions: [{ label: '刷新目录与正文', primary: true, onClick: refreshAfterAct }],
          });
        } else if (event === 'canceled') {
          settled = true;
          const result = data?.result || {};
          const stats = result.total
            ? `成功 ${Number(result.succeeded || 0)} 章 / 跳过 ${Number(result.skipped || 0)} 章 / 失败 ${Number(
                result.failed || 0
              )} 章`
            : `成功 ${counters.succeeded} 章 / 跳过 ${counters.skipped} 章 / 失败 ${counters.failed} 章`;
          panel.fail({
            message: '已停止生成',
            detail: `已完成章节已保存。本次已处理：${stats}`,
          });
        } else if (event === 'error') {
          settled = true;
          const detail = failures.length ? `已处理：${failures.join('；')}` : '';
          panel.fail({
            message: data?.message || '整幕生成失败',
            detail,
            retry: () => runActGenerate(act, actIndex, mode),
          });
        }
      },
      { signal: aborter.signal }
    );
    if (!settled) {
      settled = true;
      panel.fail({
        message: '连接已中断，没有收到整幕生成结果',
        detail: `本次已处理：成功 ${counters.succeeded} 章 / 跳过 ${counters.skipped} 章 / 失败 ${counters.failed} 章`,
        retry: () => runActGenerate(act, actIndex, mode),
      });
    }
  } catch (error) {
    // 主动停止后断开连接属于预期路径，不当作失败重复提示
    if (settled) return;
    settled = true;
    panel.fail({
      message: `整幕生成失败：${error.message}`,
      detail: `本次已处理：成功 ${counters.succeeded} 章 / 跳过 ${counters.skipped} 章 / 失败 ${counters.failed} 章`,
      retry: () => runActGenerate(act, actIndex, mode),
    });
  }
}

/** 续写：先流式生成预览并存草稿，应用后写入正文 */
function openContinueDialog(retry = false) {
  const hit = findOutlineChapter(ui.sel.id);
  if (!hit) return;
  const modeKey = 'chapter.continue';
  const hasDraft = Boolean(cache.draft?.draft?.text);
  openDialog({
    title: retry ? '重新生成续写草稿' : 'AI 续写本章',
    body: `
      ${modeSwitchHTML(modeKey, { fallback: retry ? 'regen' : hasDraft ? 'fill' : 'regen' })}
      <div class="hint" style="margin-bottom:8px">续写基于当前正文与后续节拍；生成结果先存草稿，确认后写入。${
        hasDraft ? '选「只补缺失」会沿用现有草稿，不再调用模型。' : ''
      }</div>
      <div class="field"><label>本轮字数</label>
        <input class="input" id="ct-words" type="number" min="200" step="100" value="800" /></div>
      <div class="field"><label>接下来发生什么（可选）</label>
        <textarea class="textarea" id="ct-intent" rows="3" placeholder="例：夫妻发现影厨的记忆迷雾对彼此无效，借机反杀"></textarea></div>`,
    foot: `<div class="right"><button class="btn" data-dlg-close>取消</button>
      <button class="btn primary" data-ct-run>${icon('spark', 12)} 生成续写预览</button></div>`,
    onMount: (mask) => {
      const modeOf = bindModeSwitch(mask, modeKey, {
        fallback: retry ? 'regen' : hasDraft ? 'fill' : 'regen',
        action: mask.querySelector('[data-ct-run]'),
        labels: { fill: '沿用现有草稿', regen: '重新生成续写' },
      });
      mask.querySelector('[data-ct-run]').addEventListener('click', () => {
        const mode = modeOf();
        const body = {
          path: cache.path,
          chapter: hit.chapter.id,
          words: Number(mask.querySelector('#ct-words').value) || 800,
          intent: mask.querySelector('#ct-intent').value.trim(),
          // 重新生成时才作废旧草稿并重抽
          retry: mode === 'regen',
        };
        closeDialog();
        runContinue(body, mode);
      });
    },
  });
}

/**
 * 执行续写预览（SSE）；进度走统一浮层，完成后可应用草稿。
 * @param {object} body 续写参数
 * @param {'fill'|'regen'} [mode] 只补缺失（沿用草稿）/ 重新生成
 */
async function runContinue(body, mode = 'regen') {
  // 只补缺失且已有草稿：直接复用，不调用模型
  if (mode === 'fill' && cache.draft?.draft?.text) {
    const draftText = String(cache.draft.draft.text || '');
    const panel = openProgress({
      title: '检查续写草稿…',
      hint: '只补缺失会沿用现有草稿',
      stage: '已读取未应用草稿',
    });
    panel.note(draftText.replace(/\s+/g, ' ').slice(-160));
    panel.finish({
      message: `已有续写草稿 ${Array.from(draftText).length} 字，未重新生成`,
      detail: '可直接应用草稿；要重抽请选「重新生成」。',
      actions: [
        {
          label: '应用草稿',
          primary: true,
          onClick: async () => {
            try {
              await applyDraft(body.chapter);
            } catch {
              // applyDraft 内部已给出失败提示
            }
          },
        },
      ],
    });
    return;
  }
  const panel = openProgress({
    title: body.retry ? '正在重新生成续写草稿…' : '正在生成续写草稿…',
    hint: `本轮约 ${Number(body.words || 0)} 字${body.retry ? ' · 覆盖旧草稿' : ''}`,
    stage: '已提交续写请求，等待模型返回…',
    stream: true,
  });
  panel.update({ percent: null });
  let settled = false;
  try {
    await streamJSON('/api/ui/chapter/continue/stream', body, (event, data) => {
      if (event === 'start') {
        panel.update({ stage: '模型正在续写…', percent: null });
      } else if (event === 'delta') {
        panel.append(data?.text || '');
      } else if (event === 'done') {
        settled = true;
        const result = data?.result || {};
        panel.finish({
          message: `续写完成：+${Number(result.added_words || 0)} 字，已存为草稿`,
          detail: '草稿不会自动写入正文，确认后可应用。',
          actions: [
            {
              label: '应用草稿',
              primary: true,
              onClick: async () => {
                try {
                  await applyDraft(body.chapter);
                } catch {
                  // applyDraft 内部已给出失败提示
                }
              },
            },
          ],
        });
      } else if (event === 'error') {
        settled = true;
        panel.fail({ message: data?.message || '续写失败', retry: () => runContinue(body) });
      }
    });
    if (!settled) panel.fail({ message: '连接已中断，没有收到续写结果', retry: () => runContinue(body) });
  } catch (error) {
    if (!settled) panel.fail({ message: `续写失败：${error.message}`, retry: () => runContinue(body) });
  }
}

/**
 * 应用续写草稿（不重复调用模型）。
 * @param {string} chapterId 章节 ID
 */
async function applyDraft(chapterId) {
  const result = await run('应用续写', () =>
    postJSON('/api/ui/chapter/continue', { path: cache.path, chapter: chapterId, apply: true })
  );
  await reloadAfterBodyChange(chapterId);
  toast(`续写已写入（+${Number(result?.added_words || 0)} 字）`, 'ok');
}

/**
 * 格式化版本时间（本地时区，24 小时制）。
 * @param {string} value ISO 时间字符串
 * @returns {string} 可读时间；解析失败时原样返回
 */
function formatVersionTime(value) {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString('zh-CN', { hour12: false });
}

/**
 * 打开章节版本记录：列出每次覆盖正文前自动存档的旧版本，可一键恢复。
 * @param {string} chapterId 章节 ID
 */
async function openVersionsDialog(chapterId) {
  let versions = [];
  try {
    const data = await getJSON('/api/ui/chapter/versions', { path: cache.path, chapter: chapterId });
    versions = data?.versions || [];
  } catch (error) {
    toast(`读取版本记录失败：${error.message}`, 'bad', 3600);
    return;
  }
  const rows = versions.length
    ? versions
        .map(
          (item) => `<tr style="border-top:1px solid var(--line,#2a2f3a)">
            <td style="padding:8px 6px">r${esc(String(item.revision ?? ''))}</td>
            <td style="padding:8px 6px">${esc(formatVersionTime(item.updated_at))}</td>
            <td style="padding:8px 6px">${Number(item.word_count || 0)} 字</td>
            <td style="padding:8px 6px;text-align:right">
              <button class="btn sm" data-ver-restore="${esc(item.version_id)}">恢复</button>
            </td>
          </tr>`
        )
        .join('')
    : '<tr><td colspan="4" class="hint" style="padding:12px 6px">还没有历史版本：覆盖正文或重新生成后会自动存档。</td></tr>';
  openDialog({
    title: '章节版本记录',
    wide: true,
    body: `
      <div class="hint" style="margin-bottom:8px">每次覆盖正文前会把旧版本完整存档；恢复后版本号继续递增，已有切分或导演脚本会标记为过期。</div>
      <table style="width:100%;border-collapse:collapse;font-size:13px">
        <thead><tr class="hint" style="text-align:left">
          <th style="padding:6px">版本</th>
          <th style="padding:6px">时间</th>
          <th style="padding:6px">字数</th>
          <th style="padding:6px"></th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>`,
    foot: `<div class="right"><button class="btn" data-dlg-close>关闭</button></div>`,
    onMount: (mask) => {
      mask.querySelectorAll('[data-ver-restore]').forEach((button) => {
        button.addEventListener('click', async () => {
          const versionId = button.getAttribute('data-ver-restore');
          const revision = versions.find((item) => String(item.version_id) === String(versionId))?.revision;
          button.disabled = true;
          try {
            const result = await postJSON('/api/ui/chapter/restore', {
              path: cache.path,
              chapter: chapterId,
              version_id: versionId,
            });
            closeDialog();
            await reloadAfterBodyChange(chapterId);
            toast(
              `已恢复到版本 r${result?.restored_from ?? revision ?? versionId}（${Number(result?.word_count || 0)} 字）`,
              'ok'
            );
          } catch (error) {
            button.disabled = false;
            toast(`恢复失败：${error.message}`, 'bad', 4200);
          }
        });
      });
    },
  });
}

/** 正文变化后同步章节字数与正文缓存，再重画 */
async function reloadAfterBodyChange(chapterId) {
  cache.chapters = await getJSON('/api/ui/chapters', { path: cache.path });
  cache.bodyChapter = '';
  cache.body = null;
  cache.draft = null;
  cache.plan = null;
  cache.planChapter = '';
  paint();
  await loadBody(chapterId);
  loadPlan(chapterId);
}

// ---------------- 渲染与事件 ----------------

/**
 * 归一化黄金三章检查明细：只保留有可展示文案的条目。
 * 数量与明细同源，避免出现「有数无内容」。
 * @returns {Array<{index: number, id: string, title: string, problem: string, hint: string, focus: string}>}
 */
function checkIssues() {
  return (cache.check?.issues || [])
    .map((item) => ({
      index: Number(item?.chapter_index || 0),
      id: String(item?.chapter_id || ''),
      title: String(item?.chapter_title || ''),
      problem: String(item?.problem || ''),
      hint: String(item?.hint || ''),
      focus: String(item?.focus || ''),
    }))
    .filter((row) => row.problem || row.hint);
}

/**
 * 从检查明细定位到大纲章节：展开所属幕并选中该章。
 * @param {string} id 章节 ID
 * @param {string} [focus] 落点标记；beat_turning_point 表示需要给该章某个节拍标转折
 */
function gotoCheckChapter(id, focus = '') {
  const hit = findOutlineChapter(id);
  if (!hit) return;
  const index = (cache.outline?.acts || []).indexOf(hit.act);
  materializeCollapse();
  collapse.acts.delete(actKey(hit.act, index));
  saveCollapse();
  ui.sel = { kind: 'chapter', id };
  ui.draftWorld = false;
  ui.draftEntry = false;
  paint();
  loadBody(id);
  loadPlan(id);
  if (focus === 'beat_turning_point') highlightTurningBeats();
}

/** 跳到该章后滚动到节拍区，并高亮尚未标转折的节拍，指明该改哪一处 */
function highlightTurningBeats() {
  const card = document.getElementById('novel-beat-list');
  if (!card) return;
  const pending = [...card.querySelectorAll('.novel-beat')].filter(
    (node) => !node.querySelector('[data-beat-turn]')?.checked
  );
  pending.forEach((node) => node.classList.add('need-turn'));
  card.scrollIntoView({ block: 'center', behavior: 'smooth' });
  pending[0]?.querySelector('[data-beat-turn]')?.focus({ preventScroll: true });
  setTimeout(() => pending.forEach((node) => node.classList.remove('need-turn')), 3200);
}

/** 检查结果条：有大纲检查结果时出现在主区顶部 */
function checkBannerHTML() {
  const check = cache.check;
  if (!check) return '';
  const issues = checkIssues();
  if (!issues.length) {
    // 未通过但没有可展示明细时只保留通过态，不显示带数量的空提示
    if (check.passed === false) return '';
    return `<div class="banner ok" style="margin:10px 16px 0">${icon('check', 13)} 黄金三章检查通过</div>`;
  }
  const rows = issues
    .map((row) => {
      const label = `${row.index > 0 ? `第 ${row.index} 章` : '大纲章节'}${row.title ? `《${row.title}》` : ''}`;
      const body = `
        <span class="bi-head"><b>${esc(label)}</b>${row.problem ? `<span class="bi-problem">· ${esc(row.problem)}</span>` : ''}</span>
        ${row.hint ? `<span class="bi-why">${esc(row.hint)}</span>` : ''}`;
      if (!row.id || !findOutlineChapter(row.id)) return `<div class="banner-issue">${body}</div>`;
      const jump = row.focus === 'beat_turning_point' ? '定位并标记转折 ›' : '定位章节 ›';
      return `<button class="banner-issue" data-act="novel-goto-check-chapter" data-id="${esc(row.id)}"
        data-focus="${esc(row.focus)}" title="定位到第 ${row.index} 章">${body}<span class="bi-jump">${jump}</span></button>`;
    })
    .join('');
  return `
    <div class="banner warn" style="margin:10px 16px 0">
      <div style="min-width:0;flex:1">
        <div>${icon('warn', 13)} 黄金三章检查发现 ${issues.length} 个问题</div>
        <details style="margin-top:6px"><summary style="cursor:pointer">展开明细</summary>
          <div class="banner-issues">${rows}</div>
        </details>
      </div>
    </div>`;
}

/** 工程标题：info 未返回时用目录名兜底，失败时给可重试的中文提示 */
function projectLabelHTML() {
  if (boot.state.info === 'error') {
    return `<button class="link" data-act="boot-retry" data-key="info" title="${esc(
      boot.errors.info || '读取失败'
    )}">工程名读取失败，点此重试</button>`;
  }
  const title = state.info?.title || state.info?.name || '';
  if (title) return esc(title);
  const base = String(cache.path || '').split('/').filter(Boolean).pop() || '未命名工程';
  return boot.state.info === 'ok' ? esc(base) : `${esc(base)}（正在读取工程信息…）`;
}

/** 顶栏面包屑：工程名 / 当前选中对象 */
function crumbHTML() {
  return `${projectLabelHTML()}<span class="sep">/</span>
    <span class="cur novel-title">${esc(selectionTitle())}</span>`;
}

/** 整页重画（保留滚动位置） */
function paint() {
  // 选中对象可能在数据到达后失效（例如工程被外部改动），回落到写作设定 / 世界总述
  if (boot.state.outline === 'ok' && ui.sel.kind === 'chapter' && !findOutlineChapter(ui.sel.id)) {
    ui.sel = { kind: 'setting', id: '' };
  }
  if (boot.state.worlds === 'ok') {
    if (!findWorld(ui.worldId)) ui.worldId = cache.worlds[0]?.id || '';
    if (ui.sel.kind === 'worldOverview' && !ui.draftWorld && !findWorld(ui.sel.id)) {
      // 删掉最后一套世界后保留「世界总述」空态；还有其它世界时落到当前世界
      if (cache.worlds.length) ui.sel = { kind: 'worldOverview', id: ui.worldId };
      else ui.sel = { kind: 'worldOverview', id: '' };
    }
    if (ui.sel.kind === 'worldEntry' && !ui.draftEntry) {
      const meta = CATEGORY_META[ui.sel.category];
      const exists = meta && (activeWorld()?.[meta.field] || []).some((item) => item.id === ui.sel.id);
      if (!exists) ui.sel = ui.worldId ? { kind: 'worldOverview', id: ui.worldId } : { kind: 'setting', id: '' };
    }
  }
  const main = document.getElementById('novel-main');
  const ctx = document.querySelector('#novel-root .ctx');
  const scroll = main?.scrollTop || 0;
  const listScroll = document.querySelector('#novel-root .chap-list')?.scrollTop || 0;
  if (ctx) ctx.outerHTML = ctxHTML();
  if (main) {
    main.innerHTML = `${emptyGuideHTML()}<div id="novel-check-slot">${checkBannerHTML()}</div>${mainHTML()}`;
    main.scrollTop = scroll;
  }
  const list = document.querySelector('#novel-root .chap-list');
  if (list) list.scrollTop = listScroll;
  const crumbs = document.getElementById('novel-crumb');
  if (crumbs) crumbs.innerHTML = crumbHTML();
}

/**
 * 渲染小说工作台。
 * 外壳与骨架先出现，设定 / 世界 / 大纲 / 章节各自到达后局部填充，
 * 任一慢接口都不会把整页挡在 loading 里。
 * @param {HTMLElement} root 应用根节点
 * @param {{path: string}} target 路由参数
 */
export async function renderNovel(root, target) {
  const path = target.path;
  state.path = path;
  if (cache.path !== path) {
    cache.path = path;
    cache.setting = null;
    cache.worlds = [];
    cache.outline = null;
    cache.chapters = [];
    cache.check = null;
    cache.bodyChapter = '';
    cache.body = null;
    cache.draft = null;
    cache.planChapter = '';
    cache.plan = null;
    state.info = null;
    state.chapters = [];
    ui.sel = { kind: 'setting', id: '' };
    ui.worldId = '';
    ui.draftWorld = false;
    ui.draftEntry = false;
    loadCollapse(path);
  }
  root.innerHTML = `
    <div id="novel-root" style="display:flex;flex-direction:column;height:100vh">
      ${topbar({
        left: `<button class="btn icon" data-act="novel-back" title="回工作区">${icon('back')}</button>
               <div class="brand"><span class="brand-mark"></span>
                 <span style="font-size:12.5px">小说<small>AI 写作</small></span></div>`,
        crumbs: `<span id="novel-crumb">${crumbHTML()}</span>`,
        stateText: ui.busy || '',
        right: topRightHTML(),
      })}
      <div class="studio">
        ${railbar('novel', { chapters: state.chapters.length })}
        ${ctxHTML()}
        <section class="editor">
          <div id="novel-main" style="display:flex;flex-direction:column;min-height:0;flex:1;overflow:hidden">
            ${emptyGuideHTML()}<div id="novel-check-slot">${checkBannerHTML()}</div>${mainHTML()}
          </div>
        </section>
        <aside class="studio-right"></aside>
      </div>
    </div>`;

  if (!bound) {
    root.addEventListener('click', onClick);
    root.addEventListener('change', onChange);
  }
  bound = true;
  // 各数据块并发拉取，谁先返回谁先渲染
  startBoot(path);
  syncChapterEntry();
  // 初始若停在大纲章，异步补正文、草稿与配音预检
  if (ui.sel.kind === 'chapter') {
    loadBody(ui.sel.id);
    loadPlan(ui.sel.id);
  }
}

/** 启动阶段各数据块对应的接口（章节只请求一次，后续复用缓存） */
const BOOT_REQUESTS = {
  info: (path) => getJSON('/api/project/info', { path }),
  chapters: (path) => getJSON('/api/ui/chapters', { path }),
  setting: (path) => getJSON('/api/ui/setting', { path }),
  worlds: (path) => getJSON('/api/ui/worlds', { path }),
  outline: (path) => getJSON('/api/ui/outline', { path }),
};

/**
 * 把某个数据块的结果写入缓存。
 * @param {'info'|'chapters'|'setting'|'worlds'|'outline'} key 数据块
 * @param {any} data 接口返回
 */
function applyBootData(key, data) {
  if (key === 'info') {
    state.info = data || null;
  } else if (key === 'chapters') {
    cache.chapters = Array.isArray(data) ? data : [];
    state.chapters = cache.chapters;
  } else if (key === 'setting') {
    cache.setting = data || {};
  } else if (key === 'worlds') {
    cache.worlds = data?.worlds || [];
    // 首次载入或当前世界被外部删除时，落到第一套世界；正在新建世界时不抢占
    if (!ui.draftWorld && !cache.worlds.some((item) => item.id === ui.worldId)) {
      ui.worldId = cache.worlds[0]?.id || '';
    }
  } else if (key === 'outline') {
    cache.outline = data || { acts: [] };
  }
}

/**
 * 单个数据块：独立请求、独立加载态，返回后立刻局部重画。
 * @param {'info'|'chapters'|'setting'|'worlds'|'outline'} key 数据块
 * @param {string} path 工程目录
 * @param {number} seq 启动批次，用于丢弃过期响应
 * @param {{ silent?: boolean }} [options] silent 表示已有缓存的后台刷新，不闪骨架
 */
async function bootBlock(key, path, seq, options = {}) {
  const silent = Boolean(options.silent);
  if (!silent) {
    boot.state[key] = 'loading';
    delete boot.errors[key];
  }
  if (seq === boot.seq) paint();
  try {
    const data = await BOOT_REQUESTS[key](path);
    if (seq !== boot.seq) return;
    applyBootData(key, data);
    boot.state[key] = 'ok';
    delete boot.errors[key];
  } catch (error) {
    if (seq !== boot.seq) return;
    // 空工程允许设定 / 世界 / 大纲文件不存在：按空数据处理，不报阻断
    if (error?.status === 404 && key !== 'chapters' && key !== 'info') {
      applyBootData(key, null);
      boot.state[key] = 'ok';
    } else if (silent) {
      // 后台刷新失败时保留旧数据，不让界面突然变成错误页
      boot.state[key] = 'ok';
    } else {
      boot.state[key] = 'error';
      boot.errors[key] = error?.message || '读取失败';
    }
  }
  if (seq === boot.seq) paint();
}

/**
 * 并行启动所有数据块；同一工程再次进入时保留旧数据并后台静默刷新。
 * @param {string} path 工程目录
 */
function startBoot(path) {
  const warm = boot.path === path;
  if (!warm) {
    boot.path = path;
    for (const key of Object.keys(BOOT_REQUESTS)) boot.state[key] = 'idle';
    boot.errors = {};
  }
  const seq = ++boot.seq;
  for (const key of Object.keys(BOOT_REQUESTS)) {
    bootBlock(key, path, seq, { silent: warm && boot.state[key] === 'ok' });
  }
}

/** 变更事件：世界选择器切换 / 节拍转折开关 */
function onChange(event) {
  if (!document.getElementById('novel-main')) return;
  // 节拍转折开关：只切卡片高亮态，落盘交给「保存」
  const turn = event.target.closest('[data-beat-turn]');
  if (turn) {
    turn.closest('.novel-beat')?.classList.toggle('turning', turn.checked);
    return;
  }
  const pick = event.target.closest('[data-world-pick]');
  if (!pick) return;
  ui.worldId = pick.value;
  ui.draftWorld = false;
  ui.draftEntry = false;
  ui.sel = { kind: 'worldOverview', id: pick.value };
  paint();
}

/** 点击统一分发（页面不在时直接返回，避免污染其它视图） */
async function onClick(event) {
  if (!document.getElementById('novel-main')) return;
  const el = event.target.closest('[data-act]');
  if (!el) return;
  const act = el.dataset.act;
  if (act === 'boot-retry') {
    // 只重试这一块，不影响其它已在途 / 已完成的区块
    const key = el.dataset.key;
    if (key && BOOT_REQUESTS[key]) {
      el.disabled = true;
      bootBlock(key, cache.path, boot.seq);
    }
    return;
  }
  if (act === 'novel-back') return goProjects();
  if (act === 'novel-refresh') {
    try {
      await loadAll(cache.path);
      bootBlock('info', cache.path, boot.seq);
      paint();
      toast('已刷新', 'ok', 1200);
    } catch (error) {
      toast(`刷新失败：${error.message}`, 'bad');
    }
    return;
  }
  if (act === 'novel-sel-setting') {
    ui.sel = { kind: 'setting', id: '' };
    ui.draftWorld = false;
    ui.draftEntry = false;
    return paint();
  }
  if (act === 'novel-start') {
    // 「从写小说开始」：回到写作设定并把光标放进第一个字段
    ui.sel = { kind: 'setting', id: '' };
    ui.draftWorld = false;
    ui.draftEntry = false;
    paint();
    document.getElementById('ns-logline')?.focus();
    return;
  }
  if (act === 'novel-sel-world-overview') {
    ui.worldId = activeWorld()?.id || '';
    ui.sel = { kind: 'worldOverview', id: ui.worldId };
    ui.draftWorld = false;
    ui.draftEntry = false;
    return paint();
  }
  if (act === 'novel-sel-world-entry') {
    ui.worldId = activeWorld()?.id || ui.worldId;
    ui.sel = { kind: 'worldEntry', category: el.dataset.category, id: el.dataset.id };
    ui.draftWorld = false;
    ui.draftEntry = false;
    return paint();
  }
  if (act === 'novel-toggle-world-group') return toggleWorldGroup(el.dataset.group);
  if (act === 'novel-toggle-act') return toggleAct(el.dataset.key);
  if (act === 'novel-act-generate') {
    const actId = el.dataset.id || '';
    const index = Number(el.dataset.index) || 0;
    const position = Number(el.dataset.pos) || index;
    const hit = (cache.outline?.acts || []).find((row, offset) =>
      actId ? String(row.id || '') === actId : offset === position - 1
    );
    if (!hit) return toast('找不到该幕，先刷新大纲', 'warn');
    return openActGenerateDialog(hit, index || position || 1);
  }
  if (act === 'novel-entry-new') return newEntry(el.dataset.category);
  if (act === 'novel-sel-chapter') {
    ui.sel = { kind: 'chapter', id: el.dataset.id };
    ui.draftWorld = false;
    ui.draftEntry = false;
    paint();
    loadBody(el.dataset.id);
    loadPlan(el.dataset.id);
    return;
  }
  if (act === 'novel-world-new') return newWorld();
  if (act === 'novel-world-ai') return openWorldGenerate();
  if (act === 'novel-world-overview-save') return saveWorldOverview();
  if (act === 'novel-world-entry-save') return saveWorldEntry();
  if (act === 'novel-world-entry-delete') return deleteWorldEntry();
  if (act === 'novel-world-delete') return deleteWorld();
  if (act === 'novel-setting-save') return saveSetting(el.dataset.section);
  if (act === 'novel-setting-ai') return openSettingGenerate(el.dataset.section);
  if (act === 'novel-outline-save') return saveOutlineChapter();
  if (act === 'novel-outline-generate') return openOutlineGenerate();
  if (act === 'novel-outline-expand') return openOutlineExpand();
  if (act === 'novel-outline-apply') return applyOutline();
  if (act === 'novel-outline-check') return checkOutline();
  if (act === 'novel-goto-check-chapter') return gotoCheckChapter(el.dataset.id, el.dataset.focus || '');
  if (act === 'novel-beat-delete') return deleteBeat(Number(el.dataset.index));
  if (act === 'novel-chapter-delete') return deleteChapter();
  if (act === 'novel-body-save') return saveBody();
  if (act === 'novel-body-versions') return openVersionsDialog(ui.sel.id);
  if (act === 'novel-body-generate') return openGenerateDialog(currentBodyWords() > 0);
  if (act === 'novel-body-continue') return openContinueDialog(false);
  if (act === 'novel-draft-apply') return applyDraft(ui.sel.id);
  if (act === 'novel-draft-regen') return openContinueDialog(true);
  if (act === 'novel-tts-batch') return openBatchDialog();
  if (act === 'novel-jump-chapter') {
    const chapter = landedChapter(el.dataset.id);
    if (chapter) return goChapter(cache.path, chapter.id);
  }
}
