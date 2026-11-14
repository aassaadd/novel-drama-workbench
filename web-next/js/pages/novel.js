/**
 * 小说工作台：世界设定 / 分层大纲 / 章节正文（分阶段接入）。
 *
 * 与画本编辑共用顶栏、56px 图标栏与三栏骨架，但内容完全来自写作域接口：
 * `setting show`、`world list`、`outline show`、`chapter generate` 等，
 * 前端只做展示与参数拼装，不复制写作规则。
 * 视觉与交互沿用 web-next 既有规范（卡片、chip、ed-head、vw-body、弹窗）。
 */
import { esc, icon, toast, openDialog, closeDialog } from '../dom.js';
import { getJSON, postJSON, streamJSON } from '../api.js';
import { state } from '../store.js';
import { railbar, topbar, goChapter, goProjects } from '../shell.js';

/** 本页局部状态：选中对象、忙碌标记与最近一次 AI 预览 */
const ui = {
  /** 选中对象：`setting` 写作设定 / `world` 世界条目 / `chapter` 大纲章 */
  sel: { kind: 'setting', id: '' },
  busy: '',
  /** AI 世界生成预览（未采纳前不落盘） */
  worldPreview: null,
  /** 是否正在新建世界（表单空白但尚未落盘） */
  draftWorld: false,
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

// ---------------- 数据 ----------------

/**
 * 读取写作域全部数据（设定 / 世界 / 大纲 / 章节目录）。
 * @param {string} path 工程目录
 * @param {boolean} [keepCheck] 是否保留上一次黄金三章检查结果
 */
async function loadAll(path, keepCheck = false) {
  const [setting, worlds, outline, chapters] = await Promise.all([
    getJSON('/api/ui/setting', { path }),
    getJSON('/api/ui/worlds', { path }),
    getJSON('/api/ui/outline', { path }),
    getJSON('/api/ui/chapters', { path }),
  ]);
  cache.path = path;
  cache.setting = setting || {};
  cache.worlds = worlds?.worlds || [];
  cache.outline = outline || { acts: [] };
  cache.chapters = Array.isArray(chapters) ? chapters : [];
  if (!keepCheck) cache.check = null;
}

/** 按 ID 找世界条目 */
function findWorld(id) {
  return cache.worlds.find((item) => item.id === id) || null;
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
 * 地点转一行文本（地点在 schema 里是对象，用 `|` 分隔名称/描述/氛围）。
 * @param {object|string} item 地点对象或历史字符串
 */
function locationLine(item) {
  if (typeof item === 'string') return item;
  return [item.name, item.description, item.atmosphere].filter(Boolean).join(' | ');
}

/** 名词表转一行文本：`词条 | 释义 | 别名1、别名2` */
function glossaryLine(item) {
  if (typeof item === 'string') return item;
  const aliases = Array.isArray(item.aliases) && item.aliases.length ? ` | ${item.aliases.join('、')}` : '';
  return `${item.term || ''} | ${item.meaning || ''}${aliases}`;
}

/** 生成地点 ID（schema 要求 `loc_` + 6~32 位字母数字） */
function newLocationId() {
  const bytes = crypto.getRandomValues(new Uint8Array(13));
  return `loc_${Array.from(bytes, (byte) => (byte % 36).toString(36)).join('').toUpperCase()}`;
}

/**
 * 文本行 → 地点对象数组；按行号沿用旧对象的 id，新行生成 id。
 * @param {string} text 多行文本
 * @param {Array<object>} originals 原地点数组
 */
function parseLocations(text, originals = []) {
  return splitLines(text).map((line, index) => {
    const [name = '', description = '', atmosphere = ''] = line.split('|').map((part) => part.trim());
    const base = originals[index] && typeof originals[index] === 'object' ? originals[index] : {};
    return { ...base, id: base.id || newLocationId(), name, description, atmosphere };
  });
}

/**
 * 文本行 → 名词条目数组；按行号沿用旧条目的别名。
 * @param {string} text 多行文本
 * @param {Array<object>} originals 原名词表
 */
function parseGlossary(text, originals = []) {
  return splitLines(text).map((line, index) => {
    const [term = '', meaning = '', aliasText = ''] = line.split('|').map((part) => part.trim());
    const base = originals[index] && typeof originals[index] === 'object' ? originals[index] : {};
    const aliases = aliasText
      ? aliasText
          .split(/[、,，]/)
          .map((part) => part.trim())
          .filter(Boolean)
      : base.aliases || [];
    return { ...base, term, meaning, ...(aliases.length ? { aliases } : {}) };
  });
}

/** 当前选中对象的标题（用于顶栏/空状态） */
function selectionTitle() {
  if (ui.sel.kind === 'setting') return '写作设定';
  if (ui.sel.kind === 'world') return findWorld(ui.sel.id)?.name || '世界设定';
  const hit = findOutlineChapter(ui.sel.id);
  return hit ? `${hit.chapter.index ? `第 ${hit.chapter.index} 章 · ` : ''}${hit.chapter.title || '未命名'}` : '分层大纲';
}

// ---------------- 左侧目录 ----------------

/** 左侧面板：写作设定入口 + 世界列表 + 分层大纲树 */
function ctxHTML() {
  const counts = outlineCounts();
  const worlds = cache.worlds
    .map(
      (world) => `
      <button class="chap-row novel-row novel-world-row ${
        ui.sel.kind === 'world' && ui.sel.id === world.id ? 'on' : ''
      }" data-act="novel-sel-world" data-id="${esc(world.id)}">
        <span class="mono novel-world-icon">${icon('image', 12)}</span>
        <span class="novel-world-body">
          <b class="novel-world-name">${esc(world.name || '未命名世界')}</b>
          <span class="novel-world-desc">${esc(String(world.summary || world.era || ''))}</span>
        </span>
      </button>`
    )
    .join('');
  const acts = (cache.outline?.acts || [])
    .map(
      (act, actIndex) => `
      <div class="novel-act">
        <div class="novel-act-head" title="${esc(act.summary || '')}">
          <span class="mono novel-act-index">第 ${Number(act.index || actIndex + 1)} 幕</span>
          <span class="novel-act-title" title="${esc(act.title || '')}">${esc(act.title || '未命名幕')}</span>
        </div>
        ${(act.chapters || [])
          .map(
            (chapter) => `
          <button class="chap-row novel-row novel-chapter ${
            ui.sel.kind === 'chapter' && ui.sel.id === chapter.id ? 'on' : ''
          }" data-act="novel-sel-chapter" data-id="${esc(chapter.id)}">
            <span class="mono novel-chapter-num">${Number(chapter.index || 0)}</span>
            <span class="novel-chapter-title" title="${esc(chapter.title || '未命名章节')}">${esc(
              chapter.title || '未命名章节'
            )}</span>
            ${landedChapter(chapter.id) ? '<span class="badge ok">已落章</span>' : ''}
          </button>`
          )
          .join('')}
      </div>`
    )
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
          <span class="badge mute">内核 · 世界观 · 文风</span>
        </button>
        <div class="ctx-group">
          世界设定 <span class="n">${cache.worlds.length}</span>
          <button class="btn icon sm" data-act="novel-world-ai" title="AI 生成世界条目" style="margin-left:auto">
            ${icon('spark', 12)}
          </button>
        </div>
        ${worlds || '<div class="hint" style="padding:6px 12px">还没有世界条目，点上方 ✨ 让 AI 生成</div>'}
        <button class="chap-row novel-row novel-world-new" data-act="novel-world-new">
          <span class="mono novel-row-icon">${icon('plus', 12)}</span>
          <b class="novel-row-title">新建世界</b>
        </button>
        <div class="ctx-group">
          分层大纲
          <button class="btn icon sm" data-act="novel-outline-generate" title="AI 生成分层大纲" style="margin-left:auto">
            ${icon('spark', 12)}
          </button>
        </div>
        ${acts || '<div class="hint" style="padding:6px 12px">还没有大纲，先让 AI 生成一版</div>'}
      </div>
      <div class="ctx-foot hint">
        <span>${counts.beats} 个节拍</span>
        <a class="link" href="#/p/${esc(safePath())}/characters">角色库</a>
      </div>
    </section>`;
}

/** 编码后的工程路径（用于站内链接） */
function safePath() {
  return encodeURIComponent(cache.path || state.path || '');
}

// ---------------- 主区 ----------------

/** 写作设定：故事内核 / 世界观 / 文风三张卡，均可保存与 AI 生成 */
function settingHTML() {
  const premise = cache.setting?.premise || {};
  const world = cache.setting?.world || {};
  const style = cache.setting?.style || {};
  const text = (id, label, value, rows = 3, hint = '') => `
    <div class="field">
      <label>${esc(label)}</label>
      <textarea class="textarea" id="${id}" rows="${rows}">${esc(value || '')}</textarea>
      ${hint ? `<div class="hint" style="margin-top:2px">${esc(hint)}</div>` : ''}
    </div>`;
  const line = (id, label, value, hint = '') => `
    <div class="field">
      <label>${esc(label)}</label>
      <input class="input" id="${id}" value="${esc(value || '')}" />
      ${hint ? `<div class="hint" style="margin-top:2px">${esc(hint)}</div>` : ''}
    </div>`;

  return `
    <div class="ed-head">
      <div style="min-width:0">
        <div class="et">写作设定</div>
        <div class="em">AI 写小说时读取的内核、世界观与文风；改完保存即生效</div>
      </div>
      <div class="right">
        <button class="btn sm" data-act="novel-outline-check">${icon('check', 12)} 黄金三章检查</button>
        <button class="btn sm" data-act="novel-setting-ai" data-section="premise">${icon('spark', 12)} AI 补内核</button>
        <button class="btn sm" data-act="novel-setting-ai" data-section="world">${icon('spark', 12)} AI 补世界观</button>
        <button class="btn sm" data-act="novel-setting-ai" data-section="style">${icon('spark', 12)} AI 补文风</button>
      </div>
    </div>
    <div class="vw-body" style="padding:16px;overflow:auto;display:grid;gap:14px">
      <div class="card">
        <div class="card-head"><span class="ct">故事内核</span>
          <div class="right"><button class="btn sm primary" data-act="novel-setting-save" data-section="premise">保存</button></div>
        </div>
        <div class="card-body" style="display:grid;gap:10px">
          ${text('ns-logline', '一句话梗概', premise.logline, 2, '用一句话说清主角、欲望与阻力')}
          ${text('ns-theme', '主题', premise.theme, 2)}
          ${text('ns-conflict', '核心冲突', premise.core_conflict, 3)}
          ${text('ns-ending', '结局意图', premise.ending_intent, 2)}
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
            ${line('ns-audience', '目标读者', premise.target_audience)}
            ${line('ns-length', '目标篇幅（字）', premise.target_length)}
          </div>
        </div>
      </div>
      <div class="card">
        <div class="card-head"><span class="ct">世界观</span>
          <div class="right"><button class="btn sm primary" data-act="novel-setting-save" data-section="world">保存</button></div>
        </div>
        <div class="card-body" style="display:grid;gap:10px">
          ${text('ns-era', '时代与舞台', world.era, 2)}
          ${text('ns-rules', '世界规则（每行一条）', joinLines(world.rules), 4)}
          ${text('ns-locations', '关键地点（每行：名称 | 描述 | 氛围）', joinLines((world.locations || []).map(locationLine)), 3)}
          ${text('ns-factions', '阵营势力（每行一条）', joinLines(world.factions), 3)}
          ${text('ns-glossary', '名词表（每行：词条 | 释义 | 别名）', joinLines((world.glossary || []).map(glossaryLine)), 3)}
        </div>
      </div>
      <div class="card">
        <div class="card-head"><span class="ct">文风</span>
          <div class="right"><button class="btn sm primary" data-act="novel-setting-save" data-section="style">保存</button></div>
        </div>
        <div class="card-body" style="display:grid;gap:10px">
          ${text('ns-tone', '语调', style.tone, 3)}
          <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px">
            ${line('ns-pov', '视角', style.pov, 'first / third_limited / third_omniscient')}
            ${line('ns-tense', '时态', style.tense, 'past / present')}
            ${line('ns-visual', '视觉风格', style.visual_style, '给分镜与封面用')}
          </div>
          ${text('ns-prose', '行文风格', style.prose_style, 3)}
          ${text('ns-forbidden', '禁止项（每行一条）', joinLines(style.forbidden), 3)}
        </div>
      </div>
    </div>`;
}

/** 世界条目详情：可编辑并保存；AI 只负责生成预览，采纳后才落盘 */
function worldHTML() {
  const world =
    findWorld(ui.sel.id) ||
    (ui.draftWorld
      ? { name: '', era: '', summary: '', rules: [], factions: [], locations: [], glossary: [], visual_style: '' }
      : null);
  if (!world) {
    return `<div class="empty" style="margin:auto">${icon('image', 22)}<b>还没有世界条目</b>
      <span>点左侧 ✨ 让 AI 生成一版，或手动新建</span>
      <button class="btn primary" data-act="novel-world-ai" style="margin-top:10px">${icon('spark', 12)} AI 生成世界</button></div>`;
  }
  return `
    <div class="ed-head">
      <div style="min-width:0">
        <div class="et">${esc(world.name || '未命名世界')}</div>
        <div class="em">世界条目会作为场景参考图与正文提示词的一部分</div>
      </div>
      <div class="right">
        <button class="btn sm primary" data-act="novel-world-save">保存</button>
        <button class="btn sm" data-act="novel-world-delete" style="color:var(--bad)">删除</button>
      </div>
    </div>
    <div class="vw-body" style="padding:16px;overflow:auto">
      <div class="card"><div class="card-body" style="display:grid;gap:10px">
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
          <div class="field"><label>名称</label><input class="input" id="nw-name" value="${esc(world.name || '')}" /></div>
          <div class="field"><label>时代</label><input class="input" id="nw-era" value="${esc(world.era || '')}" /></div>
        </div>
        <div class="field"><label>概要</label><textarea class="textarea" id="nw-summary" rows="3">${esc(
          world.summary || ''
        )}</textarea></div>
        <div class="field"><label>规则（每行一条）</label><textarea class="textarea" id="nw-rules" rows="4">${esc(
          joinLines(world.rules)
        )}</textarea></div>
        <div class="field"><label>阵营（每行一条）</label><textarea class="textarea" id="nw-factions" rows="3">${esc(
          joinLines(world.factions)
        )}</textarea></div>
        <div class="field"><label>地点（每行：名称 | 描述 | 氛围）</label><textarea class="textarea" id="nw-locations" rows="3">${esc(
          joinLines((world.locations || []).map(locationLine))
        )}</textarea></div>
        <div class="field"><label>词条（每行：词条 | 释义 | 别名）</label><textarea class="textarea" id="nw-glossary" rows="3">${esc(
          joinLines((world.glossary || []).map(glossaryLine))
        )}</textarea></div>
        <div class="field"><label>视觉风格</label><textarea class="textarea" id="nw-visual" rows="2">${esc(
          world.visual_style || ''
        )}</textarea></div>
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
      <div class="novel-beat">
        <div class="novel-beat-head">
          <span class="mono">节拍 ${index + 1}</span>
          ${beat.turning_point ? `<span class="badge warn">转折</span>` : ''}
          ${beat.target_words ? `<span class="badge mute">${Number(beat.target_words)} 字</span>` : ''}
          ${(beat.character_ids || []).length ? `<span class="badge mute">${beat.character_ids.length} 人</span>` : ''}
          <button class="btn icon sm" data-act="novel-beat-delete" data-index="${index}" title="删除该节拍"
                  style="margin-left:auto">${icon('trash', 12)}</button>
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
          ${landed ? `已落章（${Number(landed.word_count || 0)} 字）` : '尚未落成正文'}</div>
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
      <div class="card">
        <div class="card-head"><span class="ct">节拍（${(chapter.beats || []).length}）</span>
          <div class="right hint">节拍是正文生成的最小单位</div>
        </div>
        <div class="card-body" style="display:grid;gap:10px">
          ${beats || '<div class="hint">这一章还没有节拍</div>'}
        </div>
      </div>
      ${linkCardHTML(chapter, landed)}
      ${bodyCardHTML(chapter, landed)}
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
  const missing = Number(plan?.total || 0);
  const lines = Number(plan?.lines_total || 0);
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
        </div>
        ${
          plan
            ? blockers.length
              ? `<div class="banner warn" style="margin-top:8px">${icon('warn', 13)} ${esc(
                  blockers[0]?.message || '有阻断项，先修好再补齐配音'
                )}</div>`
              : `<div class="hint" style="margin-top:6px">预检通过，补齐只跑缺失/失败/过期行，已有音频自动跳过。</div>`
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
  openDialog({
    title: '补齐本章配音',
    body: `
      <div class="summary-line"><span>本章台词</span><span class="right mono">${Number(plan.lines_total || 0)} 行</span></div>
      <div class="summary-line"><span>需要补</span><span class="right mono">${Number(plan.total || 0)} 行</span></div>
      <div class="summary-line"><span>已有音频跳过</span><span class="right mono">${Number(plan.skipped || 0)} 行</span></div>
      ${
        blockers.length
          ? `<div class="banner warn" style="margin-top:8px">${icon('warn', 13)} ${esc(
              blockers.map((item) => item.message || item.code).join('；')
            )}</div>`
          : '<div class="hint" style="margin-top:8px">任务在后台执行，可去「任务」页看进度，随时暂停/继续。</div>'
      }`,
    foot: `<div class="right"><button class="btn" data-dlg-close>取消</button>
      <button class="btn primary" data-batch-run ${blockers.length || !Number(plan.total) ? 'disabled' : ''}>
        ${icon('mic', 12)} 开始补齐</button></div>`,
    onMount: (mask) => {
      mask.querySelector('[data-batch-run]').addEventListener('click', async () => {
        const button = mask.querySelector('[data-batch-run]');
        button.disabled = true;
        button.textContent = '启动中…';
        try {
          const result = await postJSON('/api/ui/tts/batch-all', {
            path: cache.path,
            scope: 'chapter',
            chapter: hit.chapter.id,
          });
          closeDialog();
          toast(`已开始补齐 ${Number(result?.total || 0)} 行，去「任务」页看进度`, 'ok', 3200);
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
        <div class="hint">保存正文会重建台词切分并标记过期；生成/续写会先出预览，确认后再写入。</div>
      </div>
    </div>`;
}

/** 主区 HTML：按选中对象切换 */
function mainHTML() {
  if (ui.sel.kind === 'world') return worldHTML();
  if (ui.sel.kind === 'chapter') return chapterHTML();
  return settingHTML();
}

/** 顶栏右侧：进入画本 / 回工作区 */
function topRightHTML() {
  const chapter = ui.sel.kind === 'chapter' ? landedChapter(ui.sel.id) : null;
  return `
    ${chapter ? `<a class="btn sm" href="#/p/${esc(safePath())}/c/${esc(chapter.id)}">${icon('chapter', 12)} 去画本</a>` : ''}
    <a class="btn sm" href="#/p/${esc(safePath())}/tasks">${icon('task', 12)} 任务</a>`;
}

// ---------------- 写操作 ----------------

/** 保存当前世界条目（无 id 时创建） */
async function saveWorld() {
  const base = findWorld(ui.sel.id) || {};
  const locationText = document.getElementById('nw-locations')?.value;
  const payload = {
    ...base,
    name: document.getElementById('nw-name')?.value.trim() || '',
    era: document.getElementById('nw-era')?.value.trim() || '',
    summary: document.getElementById('nw-summary')?.value.trim() || '',
    rules: splitLines(document.getElementById('nw-rules')?.value),
    factions: splitLines(document.getElementById('nw-factions')?.value),
    locations: parseLocations(locationText, base.locations || []),
    glossary: parseGlossary(document.getElementById('nw-glossary')?.value, base.glossary || []),
    visual_style: document.getElementById('nw-visual')?.value.trim() || '',
    cover_asset: base.cover_asset ?? null,
  };
  if (!payload.name) return toast('世界名称不能为空', 'warn');
  if (!payload.era) return toast('时代不能为空', 'warn');
  await run('保存世界', () => postJSON('/api/ui/worlds', { path: cache.path, world: payload }));
  const saved = (await getJSON('/api/ui/worlds', { path: cache.path }))?.worlds || [];
  cache.worlds = saved;
  const matched = payload.id
    ? saved.find((item) => item.id === payload.id)
    : saved.find((item) => item.name === payload.name);
  if (matched) ui.sel = { kind: 'world', id: matched.id };
  ui.draftWorld = false;
  toast('世界条目已保存', 'ok');
  paint();
}

/** 删除当前世界条目 */
async function deleteWorld() {
  const world = findWorld(ui.sel.id);
  if (!world) return;
  if (!window.confirm(`删除世界「${world.name || '未命名'}」？已绑定的章节会失去参考。`)) return;
  await run('删除世界', () => postJSON('/api/ui/worlds/delete', { path: cache.path, world: world.id }));
  cache.worlds = (await getJSON('/api/ui/worlds', { path: cache.path }))?.worlds || [];
  ui.sel = { kind: 'setting', id: '' };
  toast('世界条目已删除', 'ok');
  paint();
}

/** 新建空白世界：直接切到空白表单，保存时才落盘 */
function newWorld() {
  ui.sel = { kind: 'world', id: '' };
  ui.draftWorld = true;
  paint();
  document.getElementById('nw-name')?.focus();
}

/** 读取设定分区表单并保存（数组字段按行拆分） */
async function saveSetting(section) {
  const fields = {};
  const read = (id) => document.getElementById(id)?.value.trim() || '';
  if (section === 'premise') {
    Object.assign(fields, {
      logline: read('ns-logline'),
      theme: read('ns-theme'),
      core_conflict: read('ns-conflict'),
      ending_intent: read('ns-ending'),
      target_audience: read('ns-audience'),
      target_length: read('ns-length'),
    });
  } else if (section === 'world') {
    const current = cache.setting?.world || {};
    Object.assign(fields, {
      era: read('ns-era'),
      rules: splitLines(document.getElementById('ns-rules')?.value),
      locations: parseLocations(document.getElementById('ns-locations')?.value, current.locations || []),
      factions: splitLines(document.getElementById('ns-factions')?.value),
      glossary: parseGlossary(document.getElementById('ns-glossary')?.value, current.glossary || []),
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
  const clean = Object.fromEntries(Object.entries(fields).filter(([, value]) => (Array.isArray(value) ? value.length : value)));
  if (!Object.keys(clean).length) return toast('没有可保存的内容', 'warn');
  await run('保存设定', () => postJSON('/api/ui/setting/apply', { path: cache.path, section, fields: clean }));
  cache.setting = await getJSON('/api/ui/setting', { path: cache.path });
  toast('设定已保存', 'ok');
  paint();
}

/** 保存大纲章：标题 / 概要 / 各节拍摘要 */
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
      const value = document.getElementById(`nb-beat-${index}`)?.value.trim() || '';
      if (value !== (chapter.beats[index].summary || '')) {
        await postJSON('/api/ui/outline/text', {
          path: cache.path,
          chapter: chapter.id,
          beat: index,
          summary: value,
        });
      }
    }
  });
  cache.outline = await getJSON('/api/ui/outline', { path: cache.path });
  toast('大纲已保存', 'ok');
  paint();
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

/** AI 生成世界：先出预览，勾选后逐条创建 */
function openWorldGenerate() {
  openDialog({
    title: 'AI 生成世界设定',
    body: `
      <div class="field"><label>题材 / 一句话方向</label>
        <textarea class="textarea" id="wg-brief" rows="3" placeholder="例：民国上海，一个能听见器物记忆的当铺伙计"></textarea></div>
      <div class="field"><label>生成数量</label>
        <select class="select" id="wg-count">
          <option value="1">1 个</option><option value="2">2 个</option><option value="3">3 个</option>
        </select></div>
      <div class="hint">生成结果先预览，勾选「采用」后才会写入世界库，不改动已有条目。</div>`,
    foot: `<div class="right"><button class="btn" data-dlg-close>取消</button>
      <button class="btn primary" data-wg-run>${icon('spark', 12)} 生成预览</button></div>`,
    onMount: (mask) => {
      mask.querySelector('[data-wg-run]').addEventListener('click', async () => {
        const brief = mask.querySelector('#wg-brief').value.trim();
        const count = Number(mask.querySelector('#wg-count').value) || 1;
        if (!brief) return toast('先写一句方向', 'warn');
        const button = mask.querySelector('[data-wg-run]');
        button.disabled = true;
        button.textContent = '生成中…';
        try {
          const data = await postJSON('/api/ui/worlds/generate', { path: cache.path, brief, count });
          ui.worldPreview = data?.worlds || [];
          previewPicked = new Set(ui.worldPreview.map((_, index) => index));
          closeDialog();
          renderWorldPreview();
        } catch (error) {
          toast(`生成失败：${error.message}`, 'bad', 3600);
          button.disabled = false;
          button.textContent = '生成预览';
        }
      });
    },
  });
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
            ${(world.rules || []).length ? `<div class="hint">规则：${esc(world.rules.join('；'))}</div>` : ''}
            ${
              (world.locations || []).length
                ? `<div class="hint">地点：${esc(
                    world.locations.map((item) => locationLine(item).split(' | ')[0]).join('、')
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
          if (first) ui.sel = { kind: 'world', id: first.id };
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

/** AI 生成大纲：参数确认后直接生成并落盘（生成结果整份替换大纲，不动已落章正文） */
function openOutlineGenerate() {
  const counts = outlineCounts();
  openDialog({
    title: 'AI 生成分层大纲',
    body: `
      <div class="hint" style="margin-bottom:8px">
        当前大纲：${counts.acts} 幕 / ${counts.chapters} 章 / ${counts.beats} 个节拍。
        重新生成会替换大纲，但不会删除已落成的正文文件。
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
        <div class="field"><label>幕数</label><input class="input" id="og-acts" type="number" min="1" max="12" value="${counts.acts || 3}" /></div>
        <div class="field"><label>每幕章数</label><input class="input" id="og-chapters" type="number" min="1" max="20" value="3" /></div>
        <div class="field"><label>每章节拍</label><input class="input" id="og-beats" type="number" min="1" max="12" value="4" /></div>
        <div class="field"><label>每章目标字数</label><input class="input" id="og-words" type="number" min="500" step="500" value="3000" /></div>
      </div>
      <div class="field"><label>补充要求（可选）</label>
        <textarea class="textarea" id="og-brief" rows="2" placeholder="例：前三章必须完成身份揭示与第一次反转"></textarea></div>
      <label class="hint" style="display:flex;gap:6px;align-items:center;margin-top:4px">
        <input type="checkbox" id="og-multi" checked /> 按幕多轮生成（长大纲更稳）
      </label>`,
    foot: `<div class="right"><button class="btn" data-dlg-close>取消</button>
      <button class="btn primary" data-og-run>${icon('spark', 12)} 开始生成</button></div>`,
    onMount: (mask) => {
      mask.querySelector('[data-og-run]').addEventListener('click', async () => {
        const button = mask.querySelector('[data-og-run]');
        button.disabled = true;
        button.textContent = '生成中…';
        const body = {
          path: cache.path,
          acts: Number(mask.querySelector('#og-acts').value) || 3,
          chapters_per_act: Number(mask.querySelector('#og-chapters').value) || 3,
          beats_per_chapter: Number(mask.querySelector('#og-beats').value) || 4,
          words: Number(mask.querySelector('#og-words').value) || 3000,
          multi_turn: mask.querySelector('#og-multi').checked,
        };
        const brief = mask.querySelector('#og-brief').value.trim();
        if (brief) body.brief = brief;
        try {
          await run('生成大纲', () => postJSON('/api/ui/outline/generate', body));
          cache.outline = await getJSON('/api/ui/outline', { path: cache.path });
          closeDialog();
          const first = outlineChapters()[0];
          ui.sel = first ? { kind: 'chapter', id: first.chapter.id } : { kind: 'setting', id: '' };
          toast('大纲已生成', 'ok');
          paint();
        } catch {
          button.disabled = false;
          button.textContent = '开始生成';
        }
      });
    },
  });
}

/** AI 补设定分区：预览字段后合并写入 */
function openSettingGenerate(section) {
  const label = { premise: '故事内核', world: '世界观', style: '文风' }[section] || section;
  openDialog({
    title: `AI 补${label}`,
    body: `
      <div class="field"><label>补充要求（可选）</label>
        <textarea class="textarea" id="sg-brief" rows="2" placeholder="例：更冷峻一些，突出器物记忆的规则"></textarea></div>
      <div class="hint">生成结果先预览；点「写入设定」才覆盖对应字段，其余字段保持不动。</div>`,
    foot: `<div class="right"><button class="btn" data-dlg-close>取消</button>
      <button class="btn primary" data-sg-run>${icon('spark', 12)} 生成预览</button></div>`,
    onMount: (mask) => {
      mask.querySelector('[data-sg-run]').addEventListener('click', async () => {
        const brief = mask.querySelector('#sg-brief').value.trim();
        const button = mask.querySelector('[data-sg-run]');
        button.disabled = true;
        button.textContent = '生成中…';
        try {
          const data = await postJSON('/api/ui/setting/generate', { path: cache.path, section, brief });
          const fields = data?.fields || data || {};
          closeDialog();
          openSettingPreview(section, label, fields);
        } catch (error) {
          toast(`生成失败：${error.message}`, 'bad', 3600);
          button.disabled = false;
          button.textContent = '生成预览';
        }
      });
    },
  });
}

/** 设定预览：列出字段可编辑，确认后 apply */
function openSettingPreview(section, label, fields) {
  const labelOf = {
    logline: '一句话梗概',
    theme: '主题',
    core_conflict: '核心冲突',
    ending_intent: '结局意图',
    target_audience: '目标读者',
    target_length: '目标篇幅',
    era: '时代与舞台',
    locations: '关键地点',
    rules: '世界规则',
    factions: '阵营势力',
    glossary: '名词表',
    tone: '语调',
    pov: '视角',
    tense: '时态',
    prose_style: '行文风格',
    forbidden: '禁止项',
    visual_style: '视觉风格',
  };
  const rows = Object.entries(fields)
    .map(([key, value]) => {
      // 地点/名词表是对象数组，先转成可编辑文本行
      const raw = Array.isArray(value) ? value : [];
      const textValue =
        key === 'locations' ? joinLines(raw.map(locationLine)) : key === 'glossary' ? joinLines(raw.map(glossaryLine)) : joinLines(raw);
      return `
      <div class="field"><label>${esc(labelOf[key] || key)}</label>
        <textarea class="textarea" data-sf-key="${esc(key)}" rows="${Array.isArray(value) ? 3 : 2}">${esc(
          Array.isArray(value) ? textValue : value || ''
        )}</textarea></div>`;
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
        const currentWorld = cache.setting?.world || {};
        mask.querySelectorAll('[data-sf-key]').forEach((node) => {
          const key = node.dataset.sfKey;
          const original = fields[key];
          const rawText = node.value.trim();
          if (key === 'locations') next[key] = parseLocations(rawText, currentWorld.locations || []);
          else if (key === 'glossary') next[key] = parseGlossary(rawText, currentWorld.glossary || []);
          else next[key] = Array.isArray(original) ? splitLines(rawText) : rawText;
        });
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

/** 删除节拍（无确认，节拍可重新生成） */
async function deleteBeat(index) {
  const hit = findOutlineChapter(ui.sel.id);
  if (!hit) return;
  await run('删除节拍', () =>
    postJSON('/api/ui/outline/beat/delete', { path: cache.path, chapter: hit.chapter.id, beat: index })
  );
  cache.outline = await getJSON('/api/ui/outline', { path: cache.path });
  toast('节拍已删除', 'ok');
  paint();
}

/** 删除大纲章：先 dry-run 取影响，再确认删除并同步删掉已落章文件 */
async function deleteChapter() {
  const hit = findOutlineChapter(ui.sel.id);
  if (!hit) return;
  const impact = await run('评估影响', () =>
    postJSON('/api/ui/outline/chapter/delete', { path: cache.path, chapter: hit.chapter.id, dry_run: true })
  );
  const parts = [];
  if (impact?.landed) parts.push('已落成正文');
  if (impact?.chars) parts.push(`${Number(impact.chars)} 字`);
  if (impact?.lines) parts.push(`${Number(impact.lines)} 行台词`);
  if (impact?.audio) parts.push(`${Number(impact.audio)} 条音频`);
  if (impact?.image) parts.push(`${Number(impact.image)} 张图`);
  if (impact?.video) parts.push(`${Number(impact.video)} 个视频`);
  if (!window.confirm(`删除「${hit.chapter.title || '未命名章节'}」？\n影响：${parts.join(' · ') || '仅大纲'}`)) return;
  await run('删除章节', () =>
    postJSON('/api/ui/outline/chapter/delete', { path: cache.path, chapter: hit.chapter.id })
  );
  cache.outline = await getJSON('/api/ui/outline', { path: cache.path });
  cache.chapters = await getJSON('/api/ui/chapters', { path: cache.path });
  ui.sel = { kind: 'setting', id: '' };
  toast('章节已删除', 'ok');
  paint();
}

/** 大纲落地成章节（幂等：已落章的不会重复建） */
async function applyOutline() {
  const result = await run('落地章节', () => postJSON('/api/ui/outline/apply', { path: cache.path }));
  cache.chapters = await getJSON('/api/ui/chapters', { path: cache.path });
  cache.outline = await getJSON('/api/ui/outline', { path: cache.path });
  const created = result?.created?.length || result?.chapters?.length || 0;
  toast(created ? `已落地 ${created} 章` : '没有需要新建的章节', created ? 'ok' : 'info');
  paint();
}

/** 黄金三章检查：结果放主区顶部，不阻断流程 */
async function checkOutline() {
  cache.check = await run('黄金三章检查', () => getJSON('/api/ui/outline/check', { path: cache.path }));
  const issues = cache.check?.issues || [];
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

/** 打开流式面板：正文生成/续写共用；完成后出现一个二次操作按钮 */
function openStreamPanel(title, doneLabel, onDoneClick) {
  const mask = openDialog({
    title,
    wide: true,
    body: `<pre class="novel-stream" id="stream-live"></pre>
           <div class="hint" id="stream-note">正在准备…</div>`,
    foot: `<div class="right"><button class="btn" data-dlg-close>关闭</button>
      <button class="btn primary" data-stream-extra hidden>${doneLabel}</button></div>`,
  });
  const live = mask.querySelector('#stream-live');
  const note = mask.querySelector('#stream-note');
  const extra = mask.querySelector('[data-stream-extra]');
  return {
    /** 追加增量并滚到底部 */
    append(text) {
      live.textContent += text;
      live.scrollTop = live.scrollHeight;
    },
    /** 完成：显示结果摘要与二次操作 */
    finish(message, label) {
      note.textContent = message;
      extra.textContent = label || doneLabel;
      extra.hidden = false;
      extra.onclick = () => {
        closeDialog();
        onDoneClick?.();
      };
    },
    /** 失败：红色提示，不再出现二次操作 */
    fail(message) {
      note.textContent = message;
      note.style.color = 'var(--bad)';
    },
  };
}

/** 正文生成：确认参数后走 SSE 流，完成后刷新 */
function openGenerateDialog(hasBody) {
  const hit = findOutlineChapter(ui.sel.id);
  if (!hit) return;
  openDialog({
    title: hasBody ? '重新生成本章正文' : 'AI 生成本章正文',
    body: `
      ${hasBody ? '<div class="banner warn" style="margin-bottom:8px">重新生成会覆盖当前正文，旧版本仍可从章节版本记录找回。</div>' : ''}
      <div class="hint" style="margin-bottom:8px">按大纲节拍写作，读取写作设定与本章节拍作为提示词。</div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
        <div class="field"><label>每段字数</label><input class="input" id="gen-seg" type="number" min="200" step="100" value="1200" /></div>
        <div class="field"><label>最大段数</label><input class="input" id="gen-rounds" type="number" min="1" max="8" value="3" /></div>
      </div>
      <label class="hint" style="display:flex;gap:6px;align-items:center;margin-top:4px">
        <input type="checkbox" id="gen-multi" checked /> 分段多轮续写（更稳，耗时更长）
      </label>`,
    foot: `<div class="right"><button class="btn" data-dlg-close>取消</button>
      <button class="btn primary" data-gen-run>${icon('spark', 12)} 开始生成</button></div>`,
    onMount: (mask) => {
      mask.querySelector('[data-gen-run]').addEventListener('click', () => {
        const body = {
          path: cache.path,
          chapter: hit.chapter.id,
          regen: hasBody,
          multi_turn: mask.querySelector('#gen-multi').checked,
          segment_words: Number(mask.querySelector('#gen-seg').value) || 1200,
          max_rounds: Number(mask.querySelector('#gen-rounds').value) || 3,
        };
        closeDialog();
        runGenerate(body);
      });
    },
  });
}

/**
 * 执行正文生成（SSE）；完成后可刷新正文。
 * @param {object} body 生成参数
 */
async function runGenerate(body) {
  const panel = openStreamPanel('正在生成正文…', '刷新正文', async () => {
    await reloadAfterBodyChange(body.chapter);
  });
  try {
    await streamJSON('/api/ui/chapter/generate/stream', body, (event, data) => {
      if (event === 'delta') panel.append(data?.text || '');
      else if (event === 'done') {
        const result = data?.result || {};
        const words = Number(result.word_count || cache.body?.word_count || 0);
        panel.finish(`生成完成：${words} 字${result.status ? ` · ${result.status}` : ''}`, '刷新正文');
      } else if (event === 'error') {
        panel.fail(data?.message || '生成失败');
      }
    });
  } catch (error) {
    panel.fail(`生成失败：${error.message}`);
  }
}

/** 续写：先流式生成预览并存草稿，应用后写入正文 */
function openContinueDialog(retry = false) {
  const hit = findOutlineChapter(ui.sel.id);
  if (!hit) return;
  openDialog({
    title: retry ? '重新生成续写草稿' : 'AI 续写本章',
    body: `
      <div class="hint" style="margin-bottom:8px">续写基于当前正文与后续节拍；生成结果先存草稿，确认后写入。</div>
      <div class="field"><label>本轮字数</label>
        <input class="input" id="ct-words" type="number" min="200" step="100" value="800" /></div>
      <div class="field"><label>接下来发生什么（可选）</label>
        <textarea class="textarea" id="ct-intent" rows="3" placeholder="例：夫妻发现影厨的记忆迷雾对彼此无效，借机反杀"></textarea></div>`,
    foot: `<div class="right"><button class="btn" data-dlg-close>取消</button>
      <button class="btn primary" data-ct-run>${icon('spark', 12)} 生成续写预览</button></div>`,
    onMount: (mask) => {
      mask.querySelector('[data-ct-run]').addEventListener('click', () => {
        const body = {
          path: cache.path,
          chapter: hit.chapter.id,
          words: Number(mask.querySelector('#ct-words').value) || 800,
          intent: mask.querySelector('#ct-intent').value.trim(),
          retry,
        };
        closeDialog();
        runContinue(body);
      });
    },
  });
}

/**
 * 执行续写预览（SSE），完成后可应用草稿。
 * @param {object} body 续写参数
 */
async function runContinue(body) {
  const panel = openStreamPanel('正在续写…', '应用草稿', async () => {
    await applyDraft(body.chapter);
  });
  try {
    await streamJSON('/api/ui/chapter/continue/stream', body, (event, data) => {
      if (event === 'delta') panel.append(data?.text || '');
      else if (event === 'done') {
        const result = data?.result || {};
        panel.finish(`续写完成：+${Number(result.added_words || 0)} 字，已存为草稿`, '应用草稿');
      } else if (event === 'error') {
        panel.fail(data?.message || '续写失败');
      }
    });
  } catch (error) {
    panel.fail(`续写失败：${error.message}`);
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

/** 检查结果条：只在大纲视图且有结果时出现 */
function checkBannerHTML() {
  const check = cache.check;
  if (!check) return '';
  const issues = check.issues || [];
  if (!issues.length) {
    return `<div class="banner ok" style="margin:10px 16px 0">${icon('check', 13)} 黄金三章检查通过</div>`;
  }
  return `
    <div class="banner warn" style="margin:10px 16px 0">
      ${icon('warn', 13)} 黄金三章检查发现 ${issues.length} 个问题
      <details style="margin-top:6px"><summary style="cursor:pointer">展开明细</summary>
        ${issues
          .slice(0, 20)
          .map((item) => `<div class="hint" style="margin-top:4px">${esc(item.message || item.code || '')}</div>`)
          .join('')}
      </details>
    </div>`;
}

/** 整页重画（保留滚动位置） */
function paint() {
  const main = document.getElementById('novel-main');
  const ctx = document.querySelector('#novel-root .ctx');
  const scroll = main?.scrollTop || 0;
  const listScroll = document.querySelector('#novel-root .chap-list')?.scrollTop || 0;
  if (ctx) ctx.outerHTML = ctxHTML();
  if (main) {
    main.innerHTML = `${checkBannerHTML()}${mainHTML()}`;
    main.scrollTop = scroll;
  }
  const list = document.querySelector('#novel-root .chap-list');
  if (list) list.scrollTop = listScroll;
  const title = document.querySelector('#novel-root .novel-title');
  if (title) title.textContent = selectionTitle();
}

/**
 * 渲染小说工作台。
 * @param {HTMLElement} root 应用根节点
 * @param {{path: string}} target 路由参数
 */
export async function renderNovel(root, target) {
  const path = target.path;
  state.path = path;
  if (cache.path !== path) {
    cache.bodyChapter = '';
    cache.body = null;
    cache.draft = null;
    cache.planChapter = '';
    cache.plan = null;
  }
  root.innerHTML = `${topbar({ left: '', crumbs: '<span class="cur">正在加载小说工作台…</span>' })}`;
  try {
    const [info, chapters] = await Promise.all([
      getJSON('/api/project/info', { path }),
      getJSON('/api/ui/chapters', { path }),
    ]);
    state.info = info;
    state.chapters = Array.isArray(chapters) ? chapters : [];
    await loadAll(path);
  } catch (error) {
    root.innerHTML = `<div class="page"><div class="empty">${icon('warn', 22)}<b>小说工作台加载失败</b>
      <span>${esc(error.message)}</span><a class="btn" href="#/projects" style="margin-top:10px">回工作区</a></div></div>`;
    return;
  }
  // 默认选中：优先上次的对象，其次写作设定
  if (ui.sel.kind === 'world' && !ui.draftWorld && !findWorld(ui.sel.id)) ui.sel = { kind: 'setting', id: '' };
  if (ui.sel.kind === 'chapter' && !findOutlineChapter(ui.sel.id)) ui.sel = { kind: 'setting', id: '' };

  root.innerHTML = `
    <div id="novel-root" style="display:flex;flex-direction:column;height:100vh">
      ${topbar({
        left: `<button class="btn icon" data-act="novel-back" title="回工作区">${icon('back')}</button>
               <div class="brand"><span class="brand-mark"></span>
                 <span style="font-size:12.5px">小说<small>AI 写作</small></span></div>`,
        crumbs: `${esc(state.info?.title || state.info?.name || '未命名工程')}<span class="sep">/</span>
                 <span class="cur novel-title">${esc(selectionTitle())}</span>`,
        stateText: ui.busy || '',
        right: topRightHTML(),
      })}
      <div class="studio">
        ${railbar('novel', { chapters: state.chapters.length })}
        ${ctxHTML()}
        <section class="editor">
          <div id="novel-main" style="display:flex;flex-direction:column;min-height:0;flex:1;overflow:hidden">
            ${checkBannerHTML()}${mainHTML()}
          </div>
        </section>
        <aside class="studio-right"></aside>
      </div>
    </div>`;

  if (!bound) root.addEventListener('click', onClick);
  bound = true;
  // 初始若停在大纲章，异步补正文、草稿与配音预检
  if (ui.sel.kind === 'chapter') {
    loadBody(ui.sel.id);
    loadPlan(ui.sel.id);
  }
}

/** 点击统一分发（页面不在时直接返回，避免污染其它视图） */
async function onClick(event) {
  if (!document.getElementById('novel-main')) return;
  const el = event.target.closest('[data-act]');
  if (!el) return;
  const act = el.dataset.act;
  if (act === 'novel-back') return goProjects();
  if (act === 'novel-refresh') {
    try {
      await loadAll(cache.path);
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
    return paint();
  }
  if (act === 'novel-sel-world') {
    ui.sel = { kind: 'world', id: el.dataset.id };
    ui.draftWorld = false;
    return paint();
  }
  if (act === 'novel-sel-chapter') {
    ui.sel = { kind: 'chapter', id: el.dataset.id };
    ui.draftWorld = false;
    paint();
    loadBody(el.dataset.id);
    loadPlan(el.dataset.id);
    return;
  }
  if (act === 'novel-world-new') return newWorld();
  if (act === 'novel-world-ai') return openWorldGenerate();
  if (act === 'novel-world-save') return saveWorld();
  if (act === 'novel-world-delete') return deleteWorld();
  if (act === 'novel-setting-save') return saveSetting(el.dataset.section);
  if (act === 'novel-setting-ai') return openSettingGenerate(el.dataset.section);
  if (act === 'novel-outline-save') return saveOutlineChapter();
  if (act === 'novel-outline-generate') return openOutlineGenerate();
  if (act === 'novel-outline-apply') return applyOutline();
  if (act === 'novel-outline-check') return checkOutline();
  if (act === 'novel-beat-delete') return deleteBeat(Number(el.dataset.index));
  if (act === 'novel-chapter-delete') return deleteChapter();
  if (act === 'novel-body-save') return saveBody();
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
