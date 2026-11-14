/**
 * 角色视图（V3）：角色列表 + 档案编辑。
 * 结构对齐参考：左侧上下文面板是角色列表（旁白置顶、类型筛选、搜索），
 * 主区是选中角色的档案表单；音色绑定在 V4 音色视图里完成。
 */
import { esc, icon, toast, openDialog, closeDialog } from '../dom.js';
import { getJSON, patchJSON, postJSON, fileUrl } from '../api.js';
import { state, resetProject } from '../store.js';
import { railbar, topbar, goChapter, goProjects } from '../shell.js';

const ROLE_LABEL = {
  narrator: '旁白',
  protagonist: '主角',
  antagonist: '反派',
  supporting: '配角',
  minor: '群演',
};

const ROLE_ORDER = ['narrator', 'protagonist', 'antagonist', 'supporting', 'minor'];

/**
 * 一致性规则矩阵：每条规则对应后端 check_cast 的一组 finding code。
 * 顺序即界面展示顺序；未归类的 code 落到「其它」一栏，避免悄悄漏掉。
 */
const CONSISTENCY_RULES = [
  {
    key: 'narrator',
    title: '旁白单一音色',
    scope: '全剧',
    hint: '旁白跨章只能有一个音色；换音色必须在已登记的旁白切换点区间内',
    codes: ['NARRATOR_DRIFT', 'NARRATOR_SWITCH_ILLEGAL'],
  },
  {
    key: 'cross',
    title: '跨章音色一致',
    scope: '全剧',
    hint: '同一角色在不同章节解析到同一音色；行级显式覆盖不计入漂移',
    codes: ['CHARACTER_VOICE_DRIFT', 'VOICE_PROFILE_STALE'],
  },
  {
    key: 'scene',
    title: '同场景音色不撞',
    scope: '单场景',
    hint: '同一场景内多个角色不能共用同一音色',
    codes: ['SCENE_VOICE_COLLISION'],
  },
  {
    key: 'binding',
    title: '音色可用',
    scope: '逐行',
    hint: '每个有台词的角色都要有音色描述并绑定可用音色/就绪变体',
    codes: ['VOICE_BINDING_UNRESOLVED', 'VOICE_VARIANT_NOT_READY', 'VOICE_PROFILE_MISSING'],
  },
  {
    key: 'stock',
    title: '龙套槽位不冲突',
    scope: '全剧',
    hint: '同一槽位只能归属一个龙套；per_scene 复用除外；未归类说话人要收口',
    codes: ['STOCK_SLOT_CONFLICT', 'STOCK_UNCLASSIFIED'],
  },
  {
    key: 'override',
    title: '改音色需理由',
    scope: '章节/行',
    hint: '章节覆盖只允许旁白或龙套，且必须写 reason；行级覆盖同样要写理由',
    codes: ['LINE_OVERRIDE_NO_REASON', 'CHAPTER_OVERRIDE_FORBIDDEN'],
  },
  {
    key: 'bgm',
    title: 'BGM 与素材',
    scope: '全剧',
    hint: 'BGM 计划、段落锚点、循环设置与音频素材必须完整可解析',
    codes: ['BGM_PLAN_MISSING', 'BGM_SEGMENT_ANCHOR_ORPHAN', 'BGM_PLAN_GONE', 'BGM_LOOP_MISMATCH', 'BGM_ASSET_MISSING'],
  },
];

let characters = [];
let voices = [];
let check = null;
/** 角色出场统计：`{角色ID: {lines, chapters:[{id,title,index,lines}]}}` */
let stats = {};
let selectedId = '';
let keyword = '';
let roleFilter = 'all';
let bound = false;
let saveTimer = null;
/** 试听中的音频对象（切换角色 / 离开页面时停止） */
let audio = null;
/** 章节音色覆盖缓存：`{character, data}`，避免每次重画都逐章请求 */
let overrideCache = { character: '', data: null };

/** 更新顶栏保存态（参考：输入后显示「待保存」，落盘后「已自动保存」） */
function setSaveState(kind, text) {
  const node = document.getElementById('chara-save');
  if (!node) return;
  node.className = `save-state ${kind}`;
  node.innerHTML = `${icon(kind === 'saving' ? 'refresh' : 'check', 12)}${esc(text)}`;
}

/** 输入后延迟保存：800ms 内的连续输入合并成一次 PATCH */
function scheduleSave() {
  setSaveState('saving', '待保存…');
  if (saveTimer) window.clearTimeout(saveTimer);
  saveTimer = window.setTimeout(() => {
    saveTimer = null;
    saveCharacter({ silent: true });
  }, 800);
}

/** 立即落盘（失焦时调用，避免用户切走丢改动） */
function flushSave() {
  if (!saveTimer) return;
  window.clearTimeout(saveTimer);
  saveTimer = null;
  saveCharacter({ silent: true });
}

/** 按筛选条件过滤角色；旁白始终排在最前 */
function visibleCharacters() {
  const word = keyword.trim().toLowerCase();
  return characters
    .filter((item) => {
      if (roleFilter !== 'all' && item.role !== roleFilter) return false;
      if (!word) return true;
      const hay = [item.name, ...(item.aliases || [])].join(' ').toLowerCase();
      return hay.includes(word);
    })
    .sort((a, b) => {
      const ai = ROLE_ORDER.indexOf(a.role);
      const bi = ROLE_ORDER.indexOf(b.role);
      return (ai < 0 ? 99 : ai) - (bi < 0 ? 99 : bi) || String(a.name).localeCompare(String(b.name), 'zh');
    });
}

/** 角色是否已绑定音色 */
function hasVoice(item) {
  return Boolean(item?.voice_binding?.voice_id || item?.voice_binding?.ref_audio);
}

/** 当前选中角色 */
function current() {
  return characters.find((item) => item.id === selectedId) || null;
}

/** 角色列表行 */
function listHTML() {
  const rows = visibleCharacters();
  let body = '';
  if (characters.length === 0) {
    body = `<div class="empty" style="padding:22px 12px">
      <b>还没有角色</b><span>从正文切分时会自动识别人物，也可以手动新增。</span>
      <button class="btn sm" data-act="new-character" style="margin-top:8px">新增角色</button>
    </div>`;
  } else if (rows.length) {
    body = rows
      .map(
        (item) => `
      <div class="chap-row ${item.id === selectedId ? 'on' : ''}" data-chara="${esc(item.id)}"
           title="${esc(item.name)} · ${esc(ROLE_LABEL[item.role] || item.role || '')}${hasVoice(item) ? '' : ' · 缺音色'}">
        <span class="cn">${esc(item.name)}</span>
        <span class="dot ${hasVoice(item) ? 'ok' : 'warn'}"></span>
        <span class="cw">${esc(ROLE_LABEL[item.role] || item.role || '')}</span>
      </div>`
      )
      .join('');
  } else {
    body = `<div class="empty"><b>没有匹配的角色</b><span>换个关键词或清空筛选</span></div>`;
  }
  return `
    <section class="ctx" aria-label="角色列表">
      <div class="ctx-head">
        <span class="ct">角色</span>
        <span class="badge mute">${characters.length} 人</span>
        <div class="right">
          <button class="btn icon sm" data-act="new-character" title="新增角色">${icon('plus')}</button>
        </div>
      </div>
      <div class="ctx-tools">
        <div class="toolrow">
          <div class="search">${icon('search', 14)}
            <input type="search" id="chara-search" placeholder="搜索姓名 / 别名" value="${esc(keyword)}"
                   title="按姓名或别名过滤角色" />
          </div>
        </div>
        <div class="toolrow">
          ${['all', ...ROLE_ORDER]
            .map(
              (role) => `<button class="chip ${roleFilter === role ? 'on' : ''}" data-role="${role}"
                title="${role === 'all' ? `显示全部 ${characters.length} 个角色` : `只看${ROLE_LABEL[role]}`}">
                ${role === 'all' ? '全部' : ROLE_LABEL[role]}</button>`
            )
            .join('')}
        </div>
      </div>
      <div class="chap-list">${body}</div>
    </section>`;
}

/** 说话风格（只读展示，避免与档案字段重复编辑） */
function speechLine(item) {
  const style = item.speech_style || {};
  const parts = [style.tempo && `语速 ${style.tempo}`, style.pitch && `音高 ${style.pitch}`, style.accent]
    .filter(Boolean)
    .join(' · ');
  return parts || '未填写';
}

/** 音色绑定摘要 */
function voiceLine(item) {
  const binding = item.voice_binding || {};
  if (!binding.voice_id && !binding.ref_audio) return '未绑定音色';
  const voice = voices.find((entry) => entry.id === binding.voice_id);
  return voice?.name || binding.voice_name || binding.voice_id || binding.ref_audio;
}

/** 已绑定音色的参考音试听地址；没有绑定或没有音色 ID 时返回空串 */
function boundVoiceAudioUrl(item) {
  const binding = item.voice_binding || {};
  const voiceId = binding.voice_id || binding.voice_library_id;
  if (!voiceId) return '';
  const query = new URLSearchParams({ path: state.path, voice_id: voiceId });
  if (binding.default_variant_id) query.set('variant_id', binding.default_variant_id);
  return `/api/ui/voices/audio?${query.toString()}`;
}

/** 停止上一次试听，避免多个角色声音叠在一起 */
function stopAudio() {
  if (audio) {
    audio.pause();
    audio = null;
  }
}

/** 试听当前角色的默认音色 */
function playBoundVoice(item) {
  const url = boundVoiceAudioUrl(item);
  if (!url) return toast('这个角色还没有绑定音色', 'warn');
  stopAudio();
  audio = new Audio(url);
  audio.play().catch((error) => toast(`试听失败：${error.message}`, 'bad'));
}

/** 音色绑定区（参考：角色默认音色，可去音色库更换） */
function voiceBindingHTML(item) {
  const binding = item.voice_binding || {};
  const bound = hasVoice(item);
  const audioUrl = boundVoiceAudioUrl(item);
  const usedCount = characters.filter((entry) => entry.voice_binding?.voice_id === binding.voice_id).length;
  return `
    <div class="card" style="margin:0 18px 18px">
      <div class="card-head"><span class="ct">音色绑定</span>
        <div class="right"><span class="badge ${bound ? 'ok' : 'warn'}">${bound ? '已绑定' : '缺音色'}</span></div>
      </div>
      <div class="card-body">
        <div class="summary-line">
          <span style="min-width:0">
            <span style="color:var(--text)">${esc(bound ? voiceLine(item) : '未绑定音色')}</span>
            <span style="display:block;color:var(--text-4);font-size:11px;margin-top:2px">
              角色默认 · 可被章节或单行覆盖${bound ? ` · 该音色已用于 ${usedCount} 个角色` : ''}
            </span>
          </span>
          <span class="right" style="display:flex;gap:6px">
            <button class="btn sm" data-act="play-bound-voice" ${audioUrl ? '' : 'disabled'}
              title="${audioUrl ? '试听角色默认音色' : '这个角色还没有绑定音色，无法试听'}">${icon('play', 11)} 试听</button>
            <button class="btn sm" data-act="go-voices" title="到音色视图绑定或更换音色">去音色库…</button>
          </span>
        </div>
        ${bound ? '' : '<div class="hint">没有音色的角色无法批量配音；先在音色视图给它分配一个音色。</div>'}
      </div>
    </div>`;
}

/** 章节音色覆盖卡片（内容异步填充；角色默认音色 + 每章可覆盖） */
function chapterOverrideCardHTML() {
  return `
    <div class="card" id="ch-overrides" style="margin:0 18px 18px">
      <div class="card-head"><span class="ct">章节音色覆盖</span>
        <div class="right"><span class="badge mute">角色默认 + 章节覆盖</span></div>
      </div>
      <div class="card-body"><div class="empty" style="padding:12px">正在读取章节覆盖…</div></div>
    </div>`;
}

/**
 * 填充章节音色覆盖卡片：列出该角色出场的章节与本章生效音色。
 * @param {string} characterId 角色 ID
 * @param {boolean} [force] 忽略缓存强制重拉
 */
async function paintChapterOverrides(characterId, force = false) {
  const host = document.getElementById('ch-overrides');
  if (!host || !characterId) return;
  if (!force && overrideCache.character === characterId && overrideCache.data) {
    renderOverrideRows(host, characterId, overrideCache.data);
    return;
  }
  try {
    const data = await getJSON('/api/ui/cast/character', { path: state.path, character: characterId });
    if (current()?.id !== characterId) return;
    overrideCache = { character: characterId, data };
    renderOverrideRows(host, characterId, data);
  } catch (error) {
    const body = host.querySelector('.card-body');
    if (body) body.innerHTML = `<div class="dlg-note bad">${esc(error.message)}</div>`;
  }
}

/**
 * 把章节覆盖数据渲染进卡片，并绑定下拉改动。
 * @param {HTMLElement} host 卡片根节点
 * @param {string} characterId 角色 ID
 * @param {object} data `/api/ui/cast/character` 返回值
 */
function renderOverrideRows(host, characterId, data) {
  const rows = data?.chapters || [];
  const voices = data?.voices || [];
  const body = host.querySelector('.card-body');
  if (!body) return;
  if (!rows.length) {
    body.innerHTML = `<div class="empty" style="padding:12px"><span>这个角色还没有在任何章节出场</span></div>`;
    return;
  }
  const anyOverridable = rows.some((row) => row.overridable);
  body.innerHTML = `
    ${rows
      .map((row) => {
        const effective = row.effective || {};
        const inherited = row.inherited || {};
        const options = voices
          .map(
            (voice) =>
              `<option value="${esc(voice.id)}" ${
                row.mode === 'override' && row.override?.voice_id === voice.id ? 'selected' : ''
              }>${esc(voice.name || voice.id)}</option>`
          )
          .join('');
        return `
        <div class="summary-line">
          <span style="min-width:0">
            第 ${row.index ?? '—'} 章 · ${esc(row.title || '未命名章节')}
            <span style="display:block;color:var(--text-4);font-size:11px;margin-top:2px">
              ${
                row.mode === 'override'
                  ? `本章覆盖：${esc(row.override?.voice_name || '音色不可用')}`
                  : `跟随全局：${esc(inherited.voice_name || '未绑定')}`
              }
            </span>
          </span>
          <span class="right" style="display:flex;gap:6px;align-items:center">
            ${
              row.overridable
                ? `<select class="select" data-ov-chapter="${esc(row.id)}" style="width:170px;height:26px"
                    title="选择后只改这一章；选「跟随全局」清除覆盖">
                    <option value="">跟随全局（${esc(inherited.voice_name || '未绑定')}）</option>${options}
                  </select>`
                : `<span class="hint" title="章节覆盖只允许旁白或龙套角色；普通角色请改角色默认音色">
                    使用默认音色（${esc(effective.voice_name || '未绑定')}）</span>`
            }
          </span>
        </div>`;
      })
      .join('')}
    ${
      anyOverridable
        ? '<div class="hint">章节覆盖只影响所选章节；单行音色仍可在画本行内覆盖。</div>'
        : '<div class="hint">章节覆盖只允许旁白或龙套角色；普通角色请到音色视图改角色默认音色。</div>'
    }`;
  body.querySelectorAll('[data-ov-chapter]').forEach((select) => {
    select.addEventListener('change', async () => {
      select.disabled = true;
      try {
        await postJSON('/api/ui/cast/chapter', {
          path: state.path,
          chapter: select.dataset.ovChapter,
          character_id: characterId,
          voice_id: select.value || null,
        });
        overrideCache = { character: '', data: null };
        await paintChapterOverrides(characterId, true);
        toast(select.value ? '已设置本章音色覆盖' : '已恢复跟随全局', 'ok');
      } catch (error) {
        toast(error.message, 'bad', 4000);
        select.disabled = false;
      }
    });
  });
}

/** 主区档案表单 */
function profileHTML() {
  const item = current();
  if (!item) {
    return `<div class="empty" style="height:100%">${icon('folder', 22)}<span>从左侧选择一个角色</span>
      <div style="display:flex;gap:8px;margin-top:10px">
        <button class="btn sm" data-act="ai-generate">${icon('spark', 12)} AI 生成角色</button>
        <button class="btn sm" data-act="autofill">${icon('mic', 12)} 自动推荐音色</button>
      </div>
    </div>`;
  }
  const roleOptions = ROLE_ORDER.map(
    (role) => `<option value="${role}" ${item.role === role ? 'selected' : ''}>${ROLE_LABEL[role]}</option>`
  ).join('');
  const locked = item.role === 'narrator';
  const lockHint = locked ? '旁白是固定角色，不可改名或改定位' : '';
  const rowStats = stats[item.id] || { lines: 0, chapters: [] };
  const firstChapter = rowStats.chapters[0];
  const appearanceLine = rowStats.lines
    ? `出场 ${rowStats.lines} 行${firstChapter ? ` · 首次登场第 ${firstChapter.index} 章` : ''}`
    : '还没有出场记录';
  return `
    <div class="page-head">
      <div class="ph">
        <div class="eyebrow"><span class="ln"></span>${esc(ROLE_LABEL[item.role] || item.role || '角色')}</div>
        <h1 class="page-title">${esc(item.name)}</h1>
        <div class="page-sub">${esc(voiceLine(item))} · ${esc(speechLine(item))} · ${esc(appearanceLine)}</div>
      </div>
      <div class="ph-actions">
        <button class="btn" data-act="ai-generate" title="按作品设定 AI 生成主要角色名单，可逐个确认后追加">
          ${icon('spark', 13)} AI 生成角色
        </button>
        <button class="btn" data-act="autofill" title="按性别/年龄/同场不撞音为全剧角色一键编排音色">
          ${icon('mic', 13)} 自动推荐音色
        </button>
        <button class="btn" data-act="view-appearances" ${rowStats.lines ? '' : 'disabled'}
          title="${rowStats.lines ? '查看这个角色在每章的出场行数' : '这个角色还没有出现在任何台词里'}">
          查看出场${rowStats.lines ? `（${rowStats.lines}）` : ''}
        </button>
        ${
          locked
            ? `<span class="hint">${esc(lockHint)}</span>`
            : `<button class="btn danger" data-act="delete-character"
                 title="删除后不可恢复；被台词引用时会先提示">${icon('x')} 删除角色</button>`
        }
      </div>
    </div>
    <div class="card" style="margin:14px 18px 0">
      <div class="card-head"><span class="ct">角色档案</span>
        <div class="right">
          <button class="btn sm" data-act="ai-fill" title="调用写作模型补全缺失的音色 / 外貌中英文描述，不覆盖已有内容">
            ${icon('spark', 12)} AI 补全
          </button>
          <button class="btn sm" data-act="char-image" title="用 ComfyUI 生成角色特写 / 全身像 / 三视图">
            ${icon('image', 12)} 形象图
          </button>
          <span class="hint">9 个字段 · 自动保存</span>
          ${hasVoice(item) ? '' : `<span class="badge warn" title="这个角色还没有绑定音色">缺音色</span>`}
        </div>
      </div>
      <div class="card-body">
        <div style="display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px">
          <div class="field"><label>姓名</label>
            <input class="input" id="ch-name" value="${esc(item.name || '')}" ${locked ? 'disabled' : ''}
              title="${esc(lockHint || '角色正式名，用于台词说话人识别')}" /></div>
          <div class="field"><label>别名（逗号分隔）</label>
            <input class="input" id="ch-aliases" value="${esc((item.aliases || []).join('，'))}"
              title="别名也用于台词说话人识别，用逗号分隔" /></div>
          <div class="field"><label>定位</label>
            <select class="select" id="ch-role" ${locked ? 'disabled' : ''} title="${esc(lockHint || '决定角色在列表里的排序与筛选')}">${roleOptions}</select></div>
          <div class="field"><label>性别</label>
            <select class="select" id="ch-gender" title="影响音色推荐与同场听感">
              ${['unknown', 'male', 'female', 'other']
                .map((key) => `<option value="${key}" ${(item.gender || 'unknown') === key ? 'selected' : ''}>${
                  { unknown: '未指定', male: '男', female: '女', other: '其他' }[key]
                }</option>`)
                .join('')}
            </select></div>
          <div class="field"><label>年龄段</label>
            <input class="input" id="ch-age" value="${esc(item.age_range || '')}" placeholder="例如 25-35"
              title="用于音色选择，可填区间或「中年」这类描述" /></div>
          <div class="field"><label>性格（逗号分隔）</label>
            <input class="input" id="ch-personality" value="${esc((item.personality || []).join('，'))}"
              title="性格关键词，多个用逗号分隔" /></div>
          <div class="field" style="grid-column:1/-1"><label>背景</label>
            <textarea class="textarea" id="ch-background" rows="3" title="角色的来历、动机与关系背景">${esc(item.background || '')}</textarea></div>
          <div class="field" style="grid-column:1/-1"><label>说话风格（只读）</label>
            <div class="input" style="display:flex;align-items:center;color:var(--text-3)"
              title="说话风格由音色与行内参数决定，这里只读展示">${esc(speechLine(item))}</div></div>
          <div class="field" style="grid-column:1/-1"><label>外貌提示词</label>
            <textarea class="textarea" id="ch-appearance" rows="3"
              title="用于角色立绘与配图的形象描述">${esc(item.appearance_prompt || '')}</textarea></div>
          <div class="field" style="grid-column:1/-1"><label>负向提示词</label>
            <textarea class="textarea" id="ch-negative" rows="2"
              title="生成角色形象时要避免的特征，多个用逗号分隔">${esc(item.negative_prompt || '')}</textarea></div>
        </div>
        <div class="hint">音色绑定在下方或「音色」视图完成；档案改动自动保存并写回 characters.json。</div>
      </div>
    </div>
    ${imageCardHTML(item)}
    ${voiceBindingHTML(item)}
    ${chapterOverrideCardHTML()}`;
}

/** 读取全部数据 */
async function loadAll(path) {
  const [info, chapters, charaPayload, castPayload, checkPayload, statsPayload] = await Promise.all([
    getJSON('/api/project/info', { path }),
    getJSON('/api/ui/chapters', { path }),
    getJSON('/api/ui/characters', { path }),
    getJSON('/api/ui/cast', { path }).catch(() => null),
    getJSON('/api/ui/cast/check', { path }).catch(() => null),
    getJSON('/api/ui/characters/stats', { path }).catch(() => null),
  ]);
  state.info = info;
  state.chapters = chapters || [];
  if (!state.chapters.some((item) => item.id === state.chapterId)) {
    state.chapterId = state.chapters[0]?.id || '';
  }
  characters = charaPayload?.characters || [];
  voices = castPayload?.voices || [];
  check = checkPayload;
  stats = statsPayload?.characters || {};
  overrideCache = { character: '', data: null };
  if (!characters.some((item) => item.id === selectedId)) selectedId = characters[0]?.id || '';
}

/** 主区重画 */
function repaint(ctxOnly = false) {
  const studio = document.getElementById('studio');
  if (!studio) return;
  const ctxOld = studio.querySelector('.ctx');
  if (ctxOld) ctxOld.outerHTML = listHTML();
  if (!ctxOnly) {
    const main = document.getElementById('chara-main');
    if (main) main.innerHTML = profileHTML();
    // 章节覆盖按角色异步填充；切角色后从缓存命中或重新拉取
    const item = current();
    if (item) void paintChapterOverrides(item.id);
  }
  const badge = document.getElementById('chara-bind');
  if (badge) {
    const total = characters.length;
    const boundCount = characters.filter(hasVoice).length;
    badge.textContent = `已绑定 ${boundCount}/${total}`;
  }
}

/** 把表单写回角色对象（aliases/personality 按 CLI 的逗号分隔格式传） */
function collectForm(item) {
  const split = (value) =>
    (value || '')
      .split(/[,，、;；]/)
      .map((text) => text.trim())
      .filter(Boolean);
  const fields = {
    name: document.getElementById('ch-name')?.value.trim() || item.name,
    aliases: split(document.getElementById('ch-aliases')?.value).join('，'),
    role: document.getElementById('ch-role')?.value || item.role,
    gender: document.getElementById('ch-gender')?.value || item.gender,
    age_range: document.getElementById('ch-age')?.value.trim() || null,
    personality: split(document.getElementById('ch-personality')?.value).join('，'),
    background: document.getElementById('ch-background')?.value || '',
    appearance_prompt: document.getElementById('ch-appearance')?.value || '',
    negative_prompt: document.getElementById('ch-negative')?.value || '',
  };
  return fields;
}

/** 保存当前角色（silent=true 时不弹 toast，用于自动保存） */
async function saveCharacter({ silent = false } = {}) {
  const item = current();
  if (!item) return;
  setSaveState('saving', '保存中…');
  try {
    await patchJSON('/api/ui/characters', { path: state.path, id: item.id, ...collectForm(item) });
    if (!silent) toast('角色已保存', 'ok');
    setSaveState('ok', '已自动保存');
    await loadAll(state.path);
    // 自动保存时只刷新左侧列表与计数，不重画表单，避免打断继续输入
    repaint(silent);
  } catch (error) {
    setSaveState('', '保存失败');
    toast(error.message, 'bad');
  }
}

/** 新增角色（参考：弹窗填名字与定位） */
function newCharacter() {
  const mask = openDialog({
    title: '新增角色',
    body: `<div class="field"><label>姓名</label><input class="input" id="nc-name" placeholder="角色正式名" /></div>
      <div class="field"><label>定位</label><select class="select" id="nc-role">
        ${['protagonist', 'supporting', 'antagonist', 'minor']
          .map((role) => `<option value="${role}">${ROLE_LABEL[role]}</option>`)
          .join('')}
      </select></div>`,
    foot: `<div class="right"><button class="btn" data-dlg-close>取消</button>
      <button class="btn primary" id="nc-ok">创建</button></div>`,
  });
  mask.querySelector('#nc-ok').addEventListener('click', async (event) => {
    const name = mask.querySelector('#nc-name').value.trim();
    if (!name) return toast('请填写姓名', 'warn');
    event.currentTarget.disabled = true;
    try {
      await postJSON('/api/ui/characters', {
        path: state.path,
        characters: [{ name, role: mask.querySelector('#nc-role').value }],
      });
      closeDialog();
      toast('角色已创建', 'ok');
      await loadAll(state.path);
      repaint();
    } catch (error) {
      toast(error.message, 'bad');
      event.currentTarget.disabled = false;
    }
  });
}

/** 删除角色（二次确认；旁白由后端拒绝） */
async function deleteCharacter() {
  const item = current();
  if (!item) return;
  let refs = { total: 0, chapters: [] };
  try {
    refs = await getJSON('/api/ui/characters/refs', { path: state.path, character: item.id });
  } catch {
    // 引用查询失败时按无引用处理，删除接口仍会做最终校验
  }
  const blocked = Number(refs.total || 0) > 0;
  const samples = (refs.chapters || []).flatMap((chapter) =>
    (chapter.lines || [])
      .slice(0, 3)
      .map((line) => ({ ...line, chapter: chapter.title || chapter.id }))
  );
  const mask = openDialog({
    title: '删除角色',
    wide: blocked,
    body: blocked
      ? `<div class="dlg-note bad">「${esc(item.name)}」仍被 ${refs.total} 行台词引用，暂不能删除。
          先把这些行的说话人改派给其它角色（画本里可直接在说话人下拉里切换）。</div>
         <div class="impact">
           ${samples
             .map(
               (line) => `<div class="imp-row">
                 <span style="min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
                   ${esc(line.chapter)} · ${esc((line.text || '').slice(0, 34))}
                 </span>
                 <span class="badge warn">引用</span>
               </div>`
             )
             .join('')}
         </div>
         <div class="hint">共 ${(refs.chapters || []).length} 章有引用；这里只列出前 ${samples.length} 行。</div>`
      : `<div class="dlg-note">「${esc(item.name)}」没有被任何台词引用，可以安全删除。</div>`,
    foot: `<div class="right"><button class="btn" data-dlg-close>${blocked ? '知道了' : '取消'}</button>
      ${blocked ? '' : '<button class="btn danger" id="del-chara-ok">确认删除</button>'}</div>`,
  });
  mask.querySelector('#del-chara-ok')?.addEventListener('click', async (event) => {
    event.currentTarget.disabled = true;
    try {
      await postJSON('/api/ui/characters/delete', { path: state.path, id: item.id });
      closeDialog();
      toast('角色已删除', 'ok');
      selectedId = '';
      await loadAll(state.path);
      repaint();
    } catch (error) {
      toast(error.message, 'bad');
      event.currentTarget.disabled = false;
    }
  });
}

/** 查看出场：按章列出该角色的台词行数，点击跳到对应章节 */
function viewAppearances() {
  const item = current();
  if (!item) return;
  const row = stats[item.id];
  if (!row?.lines) return toast('这个角色还没有出场记录', 'info');
  const rows = row.chapters
    .map(
      (chapter) => `<button class="btn" style="height:auto;padding:9px 11px;justify-content:space-between"
        data-jump="${esc(chapter.id)}" title="跳到第 ${chapter.index} 章">
        <span style="min-width:0">第 ${chapter.index} 章 · ${esc(chapter.title || '未命名章节')}</span>
        <span class="badge mute">${chapter.lines} 行</span>
      </button>`
    )
    .join('');
  const mask = openDialog({
    title: `${item.name} 的出场`,
    body: `<div class="dlg-note">共 ${row.lines} 行，分布在 ${row.chapters.length} 章。点击章节跳到画本。</div>
      <div style="display:flex;flex-direction:column;gap:6px;max-height:340px;overflow:auto">${rows}</div>`,
    foot: `<div class="right"><button class="btn" data-dlg-close>关闭</button></div>`,
  });
  mask.querySelectorAll('[data-jump]').forEach((button) => {
    button.addEventListener('click', () => {
      closeDialog();
      goChapter(state.path, button.dataset.jump);
    });
  });
}

/** 形象图类型（顺序即展示顺序） */
const IMAGE_KINDS = [
  ['fullbody', '全身像'],
  ['closeup', '特写'],
  ['turnaround', '三视图'],
];
/** 自动编排动作的展示文案 */
const AUTOFILL_ACTION = {
  create: '新建',
  fill: '补齐',
  keep: '保持',
  manual: '待人工',
};

/**
 * 角色形象卡：三类形象图缩略与生成入口。
 * 图片地址走 /api/ui/file，路径来自 characters.json 的 image_assets。
 * @param {object} item 角色对象
 */
function imageCardHTML(item) {
  const assets = item.image_assets || {};
  const cells = IMAGE_KINDS.map(([kind, label]) => {
    const rel = assets[kind];
    return `
      <div style="display:flex;flex-direction:column;gap:6px">
        <div style="height:132px;border-radius:6px;overflow:hidden;background:var(--bg-2);
          display:flex;align-items:center;justify-content:center;color:var(--text-4)">
          ${
            rel
              ? `<a href="${fileUrl(rel, state.path)}" target="_blank" rel="noopener" title="打开原图"
                   style="display:block;width:100%;height:100%">
                   <img src="${fileUrl(rel, state.path)}" alt="${esc(label)}" loading="lazy"
                     style="width:100%;height:100%;object-fit:cover;display:block" /></a>`
              : `${icon('image', 22)}`
          }
        </div>
        <div style="display:flex;align-items:center;gap:6px">
          <span style="font-size:11.5px;color:var(--text-3);flex:1">${label}</span>
          <button class="btn sm" data-act="char-image" data-kind="${kind}"
            title="${rel ? `重新生成${label}` : `生成${label}`}">${rel ? '重生成' : '生成'}</button>
        </div>
      </div>`;
  }).join('');
  return `
    <div class="card" style="margin:0 18px 18px">
      <div class="card-head"><span class="ct">角色形象图</span>
        <div class="right"><span class="hint">ComfyUI 本地生成 · 可作为配图 / 视频参考</span></div>
      </div>
      <div class="card-body">
        <div style="display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px">${cells}</div>
      </div>
    </div>`;
}

/** 形象图弹窗里的缩略网格 */
function characterImageBodyHTML(item) {
  const assets = item.image_assets || {};
  const cells = IMAGE_KINDS.map(([kind, label]) => {
    const rel = assets[kind];
    return `
      <div style="display:flex;flex-direction:column;gap:6px">
        <div style="height:240px;border-radius:6px;overflow:hidden;background:var(--bg-2);
          display:flex;align-items:center;justify-content:center;color:var(--text-4)">
          ${
            rel
              ? `<a href="${fileUrl(rel, state.path)}" target="_blank" rel="noopener" style="display:block;width:100%;height:100%">
                   <img src="${fileUrl(rel, state.path)}" alt="${esc(label)}"
                     style="width:100%;height:100%;object-fit:contain;display:block" /></a>`
              : `${icon('image', 26)}<span style="font-size:11px">还没有${label}</span>`
          }
        </div>
        <div style="display:flex;align-items:center;gap:6px">
          <span style="font-size:11.5px;color:var(--text-3);flex:1">${label}</span>
          <button class="btn sm" data-ci="gen" data-kind="${kind}">${rel ? '重新生成' : '生成'}</button>
        </div>
      </div>`;
  }).join('');
  return `
    <div class="dlg-note">形象图由本机 ComfyUI 生成并写回角色档案；重新生成会覆盖同类型旧图。</div>
    <div style="display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin-top:10px">${cells}</div>
    <div id="ci-progress" style="margin-top:10px"></div>`;
}

/**
 * 打开形象图弹窗；autoKind 非空时打开即开始生成该类形象图。
 * @param {string} [autoKind] fullbody / closeup / turnaround
 */
function openCharacterImage(autoKind = '') {
  const item = current();
  if (!item) return toast('先从左侧选择一个角色', 'warn');
  const mask = openDialog({
    title: `${item.name} 的形象图`,
    wide: true,
    body: `<div id="ci-body">${characterImageBodyHTML(item)}</div>`,
    foot: `<div class="right">
      <button class="btn" data-ci="close">关闭</button>
      <button class="btn" data-ci="refresh" title="重新读取角色档案里的形象图">刷新</button>
      <button class="btn primary" data-ci="gen" data-kind="fullbody">生成全身像</button>
    </div>`,
  });
  mask.addEventListener('click', (event) => {
    const button = event.target.closest('[data-ci]');
    if (!button) return;
    if (button.dataset.ci === 'close') return closeDialog();
    if (button.dataset.ci === 'refresh') {
      const fresh = current();
      const body = mask.querySelector('#ci-body');
      if (body && fresh) body.innerHTML = characterImageBodyHTML(fresh);
      return;
    }
    if (button.dataset.ci === 'gen') void runCharacterImage(mask, button.dataset.kind);
  });
  if (autoKind) void runCharacterImage(mask, autoKind);
}

/**
 * 生成一类角色形象图并刷新档案。
 * @param {HTMLElement} mask 弹窗根节点
 * @param {string} kind fullbody / closeup / turnaround
 */
async function runCharacterImage(mask, kind) {
  const item = current();
  if (!item || !kind) return;
  const buttons = mask.querySelectorAll('[data-ci="gen"]');
  buttons.forEach((button) => (button.disabled = true));
  const progress = mask.querySelector('#ci-progress');
  const label = (IMAGE_KINDS.find(([key]) => key === kind) || [kind, kind])[1];
  if (progress) {
    progress.innerHTML = `<div class="dlg-note">正在生成${esc(label)}…（ComfyUI 出图可能需要几十秒）</div>`;
  }
  try {
    const result = await postJSON('/api/ui/characters/image', { path: state.path, id: item.id, kind });
    await loadAll(state.path);
    repaint();
    const fresh = current();
    const body = mask.querySelector('#ci-body');
    if (body && fresh) body.innerHTML = characterImageBodyHTML(fresh);
    toast(`${label}${result?.created ? '已生成' : '已复用旧图'}`, 'ok');
  } catch (error) {
    toast(error.message, 'bad', 4600);
    if (progress) progress.innerHTML = `<div class="dlg-note bad">${esc(error.message)}</div>`;
  } finally {
    buttons.forEach((button) => (button.disabled = false));
  }
}

/** AI 补全当前角色的音色 / 外貌描述（只补缺失，不覆盖已有内容） */
function openAiFill() {
  const item = current();
  if (!item) return toast('先从左侧选择一个角色', 'warn');
  const mask = openDialog({
    title: 'AI 补全角色档案',
    body: `<div class="dlg-note">只补缺失的音色描述、外貌描述与负向提示词（中英文），已有内容不会被覆盖。</div>
      <div class="summary-line"><span>目标角色</span><span class="right">${esc(item.name)}</span></div>
      <div id="af-result" style="margin-top:8px"></div>`,
    foot: `<div class="right"><button class="btn" data-dlg-close>取消</button>
      <button class="btn primary" id="af-run">开始补全</button></div>`,
  });
  mask.querySelector('#af-run').addEventListener('click', async (event) => {
    const button = event.currentTarget;
    const resultHost = mask.querySelector('#af-result');
    button.disabled = true;
    button.textContent = '补全中…';
    if (resultHost) resultHost.innerHTML = `<div class="dlg-note">正在调用写作模型补全，请稍候…</div>`;
    try {
      const result = await postJSON('/api/ui/characters/descriptions/fill', {
        path: state.path,
        names: [item.name],
      });
      if (resultHost) {
        resultHost.innerHTML = `
          <div class="summary-line"><span>补全成功</span><span class="right">${(result.updated || []).length} 个</span></div>
          <div class="summary-line"><span>字段已齐全</span><span class="right">${(result.skipped || []).length} 个</span></div>
          ${
            (result.failures || []).length
              ? `<div class="dlg-note bad">失败：${result.failures
                  .map((row) => `${esc(row.name)}：${esc(row.message)}`)
                  .join('；')}</div>`
              : ''
          }
          <div class="hint" style="margin-top:6px">${esc(result.summary || '')}</div>`;
      }
      await loadAll(state.path);
      repaint();
      toast((result.updated || []).length ? '角色档案已补全' : '没有需要补全的字段', 'ok');
    } catch (error) {
      toast(error.message, 'bad', 4600);
      if (resultHost) resultHost.innerHTML = `<div class="dlg-note bad">${esc(error.message)}</div>`;
    } finally {
      button.disabled = false;
      button.textContent = '重新补全';
    }
  });
}

/** AI 生成角色名单：预览 → 勾选 → 追加 */
function openAiGenerate() {
  const mask = openDialog({
    title: 'AI 生成角色',
    wide: true,
    body: `<div class="dlg-note">按作品设定与已有角色生成名单；先预览，勾选后才会写入 characters.json。</div>
      <div style="display:grid;grid-template-columns:140px 1fr;gap:10px;margin-top:10px">
        <div class="field"><label>人数</label>
          <input class="input" id="ag-count" type="number" min="1" max="30" value="4" /></div>
        <div class="field"><label>补充要求（可选）</label>
          <input class="input" id="ag-brief" placeholder="例如：再补两个反派，年龄 40 岁以上" /></div>
      </div>
      <div id="ag-result" style="margin-top:10px"></div>`,
    foot: `<div class="right"><button class="btn" data-dlg-close>取消</button>
      <button class="btn" id="ag-preview">生成预览</button>
      <button class="btn primary" id="ag-apply" disabled>追加选中角色</button></div>`,
  });
  let rows = [];
  const applyButton = mask.querySelector('#ag-apply');
  const resultHost = mask.querySelector('#ag-result');
  const renderRows = () => {
    resultHost.innerHTML = rows.length
      ? rows
          .map(
            (row, index) => `
        <div class="summary-line" style="align-items:flex-start">
          <span style="display:flex;gap:8px;min-width:0">
            <input type="checkbox" data-ag-pick="${index}" ${row.__pick === false ? '' : 'checked'} />
            <span style="min-width:0">
              <b>${esc(row.name || '未命名')}</b>
              <span class="badge mute" style="margin-left:6px">${esc(
                ROLE_LABEL[row.role] || row.role || '角色'
              )}</span>
              <div style="font-size:11.5px;color:var(--text-3);margin-top:2px">${esc(
                row.one_line || row.background || row.personality || ''
              )}</div>
            </span>
          </span>
          <span class="right hint">${esc([row.gender, row.age_range].filter(Boolean).join(' · '))}</span>
        </div>`
          )
          .join('')
      : `<div class="empty"><span>还没有预览结果</span></div>`;
    applyButton.disabled = !rows.length;
  };
  renderRows();
  resultHost.addEventListener('change', (event) => {
    const box = event.target.closest('[data-ag-pick]');
    if (!box) return;
    rows[Number(box.dataset.agPick)].__pick = box.checked;
  });
  mask.querySelector('#ag-preview').addEventListener('click', async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    button.textContent = '生成中…';
    resultHost.innerHTML = `<div class="dlg-note">正在生成角色名单与档案，请稍候…</div>`;
    try {
      const result = await postJSON('/api/ui/characters/generate', {
        path: state.path,
        count: Number(mask.querySelector('#ag-count').value || 4),
        brief: mask.querySelector('#ag-brief').value.trim() || undefined,
      });
      rows = (result.characters || []).map((row) => ({ ...row }));
      renderRows();
      toast(`已生成 ${rows.length} 个角色预览`, 'ok');
    } catch (error) {
      toast(error.message, 'bad', 4600);
      resultHost.innerHTML = `<div class="dlg-note bad">${esc(error.message)}</div>`;
    } finally {
      button.disabled = false;
      button.textContent = '重新预览';
    }
  });
  applyButton.addEventListener('click', async (event) => {
    const picks = rows.filter((row) => row.__pick !== false);
    if (!picks.length) return toast('至少勾选一个角色', 'warn');
    event.currentTarget.disabled = true;
    try {
      const payload = picks.map(({ __pick, ...row }) => row);
      const result = await postJSON('/api/ui/characters', { path: state.path, characters: payload });
      closeDialog();
      toast(`已追加 ${(result.characters || []).length} 个角色`, 'ok');
      await loadAll(state.path);
      repaint();
    } catch (error) {
      toast(error.message, 'bad', 4600);
      event.currentTarget.disabled = false;
    }
  });
}

/** 全剧自动推荐音色：预览计划 → 应用 / 撤回 */
async function openAutofill() {
  let data = null;
  try {
    data = await getJSON('/api/ui/cast/autofill', { path: state.path });
  } catch (error) {
    return toast(error.message, 'bad', 4600);
  }
  const plan = data?.plan || {};
  const items = plan.items || [];
  const manual = plan.manual || [];
  const stats = plan.stats || {};
  const badge = { create: 'ok', fill: 'warn', keep: 'mute', manual: 'bad' };
  const rows = items
    .map(
      (item) => `
      <div class="summary-line" style="align-items:flex-start">
        <span style="min-width:0">
          <b>${esc(item.label || item.key || '')}</b>
          <span class="badge ${badge[item.action] || 'mute'}" style="margin-left:6px">${
            AUTOFILL_ACTION[item.action] || item.action || ''
          }</span>
          <div style="font-size:11.5px;color:var(--text-3);margin-top:2px">
            ${esc(item.proposed_name || (item.action === 'manual' ? '需要人工指定' : '保持现状'))}
            · ${esc(item.reason || '')}
          </div>
        </span>
      </div>`
    )
    .join('');
  const mask = openDialog({
    title: '自动推荐音色',
    wide: true,
    body: `
      <div class="dlg-note">按角色性别 / 年龄 / 同场不撞音生成编排计划；「保持」项不会改动，应用前会保留一份可撤回快照。</div>
      <div class="summary-line"><span>计划改动</span>
        <span class="right">新建 <b>${Number(stats.created || 0)}</b> · 补齐 <b>${Number(
          stats.filled || stats.changed || 0
        )}</b> · 保持 ${Number(stats.kept || 0)}</span></div>
      ${manual.length ? `<div class="dlg-note warn">需人工指定 ${manual.length} 项：${manual
        .slice(0, 4)
        .map((item) => esc(item.message || ''))
        .join('；')}${manual.length > 4 ? '…' : ''}</div>` : ''}
      <div style="margin-top:10px;max-height:340px;overflow:auto">${
        rows || '<div class="empty"><span>没有需要处理的角色</span></div>'
      }</div>
      <div id="af-note" style="margin-top:8px"></div>`,
    foot: `<div class="right">
      <button class="btn" data-dlg-close>关闭</button>
      <button class="btn" id="af-undo" ${data?.undo?.available ? '' : 'disabled'}
        title="${data?.undo?.available ? '撤销上一次自动编排，恢复原 cast' : '没有可撤回的自动编排记录'}">撤回上次</button>
      <button class="btn primary" id="af-apply" ${items.some((item) => item.action !== 'keep') ? '' : 'disabled'}>
        应用编排</button>
    </div>`,
  });
  mask.querySelector('#af-apply').addEventListener('click', async (event) => {
    event.currentTarget.disabled = true;
    try {
      const result = await postJSON('/api/ui/cast/autofill', { path: state.path });
      toast(
        result.applied ? `已应用，改动 ${Number(result.changed || 0)} 项` : '当前已是最优编排，没有需要改动的地方',
        result.applied ? 'ok' : 'info'
      );
      closeDialog();
      await loadAll(state.path);
      repaint();
      check = await getJSON('/api/ui/cast/check', { path: state.path }).catch(() => check);
      repaint();
    } catch (error) {
      toast(error.message, 'bad', 4600);
      event.currentTarget.disabled = false;
    }
  });
  mask.querySelector('#af-undo').addEventListener('click', async (event) => {
    event.currentTarget.disabled = true;
    try {
      await postJSON('/api/ui/cast/autofill/undo', { path: state.path });
      toast('已撤回上一次自动编排', 'ok');
      closeDialog();
      await loadAll(state.path);
      repaint();
    } catch (error) {
      toast(error.message, 'bad', 4600);
      event.currentTarget.disabled = false;
    }
  });
}

/** 主区点击/输入绑定 */
async function onRootClick(event) {
  // 监听绑在共享根节点上：离开角色页后不再处理，避免抢走其它页面的同名操作
  if (!document.getElementById('chara-main')) return;
  const row = event.target.closest('[data-chara]');
  if (row) {
    // 切角色前先落盘，避免 800ms 防抖窗口里的改动丢在旧角色上
    flushSave();
    stopAudio();
    selectedId = row.dataset.chara;
    repaint();
    return;
  }
  const roleChip = event.target.closest('[data-role]');
  if (roleChip) {
    roleFilter = roleChip.dataset.role;
    repaint(true);
    return;
  }
  const button = event.target.closest('[data-act]');
  if (!button) return;
  const act = button.dataset.act;
  if (act === 'back') return goProjects();
  if (act === 'to-editor') return goChapter(state.path, state.chapterId);
  if (act === 'consistency') return openConsistencyMatrix();
  if (act === 'new-character') return newCharacter();
  if (act === 'ai-generate') return openAiGenerate();
  if (act === 'ai-fill') return openAiFill();
  if (act === 'autofill') return void openAutofill();
  if (act === 'char-image') return openCharacterImage(button.dataset.kind || '');
  if (act === 'delete-character') return deleteCharacter();
  if (act === 'view-appearances') return viewAppearances();
  if (act === 'go-voices') {
    if (!state.path) return goProjects();
    location.hash = `#/p/${encodeURIComponent(state.path)}/voices`;
    return;
  }
  if (act === 'play-bound-voice') {
    const item = current();
    if (item) playBoundVoice(item);
  }
}

function onRootInput(event) {
  if (event.target.id === 'chara-search') {
    keyword = event.target.value;
    repaint(true);
    const input = document.getElementById('chara-search');
    if (input) {
      input.focus();
      input.setSelectionRange(input.value.length, input.value.length);
    }
  } else if (event.target.closest('#chara-main')) {
    scheduleSave();
  }
}

/** 失焦立即落盘，避免切走时还在 800ms 防抖窗口里丢改动 */
function onRootFocusOut(event) {
  if (event.target.closest('#chara-main')) flushSave();
}

/**
 * 打开一致性规则矩阵：逐条规则给出通过/警告/错误与命中明细。
 * 数据来自 `/api/ui/cast/check` 的 findings，前端只做归类，不重算规则。
 */
function openConsistencyMatrix() {
  const findings = check?.findings || [];
  const buckets = CONSISTENCY_RULES.map((rule) => ({ rule, items: [] }));
  const other = [];
  for (const item of findings) {
    const bucket = buckets.find((entry) => entry.rule.codes.includes(item.code));
    if (bucket) bucket.items.push(item);
    else other.push(item);
  }
  if (other.length) buckets.push({ rule: { title: '其它检查', scope: '全剧', hint: '未归类的校验项' }, items: other });

  /** 单条规则的结果徽标 */
  const stateOf = (items) => {
    const errors = items.filter((item) => item.level === 'error').length;
    const warns = items.filter((item) => item.level === 'warn').length;
    if (errors) return `<span class="badge bad">错误 ${errors}</span>`;
    if (warns) return `<span class="badge warn">警告 ${warns}</span>`;
    return '<span class="badge ok">通过</span>';
  };

  const rows = buckets
    .map(
      ({ rule, items }) => `
      <div class="summary-line" style="align-items:flex-start;gap:10px">
        <span style="min-width:0">
          <b>${esc(rule.title)}</b>
          <span class="badge mute" style="margin-left:6px">${esc(rule.scope)}</span>
          <div style="font-size:11px;color:var(--text-4);margin-top:2px">${esc(rule.hint)}</div>
        </span>
        <span class="right">${stateOf(items)}</span>
      </div>
      ${
        items.length
          ? `<details style="margin:0 0 8px 0">
               <summary style="cursor:pointer;font-size:11px;color:var(--text-3)">命中 ${items.length} 项</summary>
               <div style="padding:6px 0 0 8px">
                 ${items
                   .slice(0, 30)
                   .map(
                     (item) => `<div class="summary-line" style="gap:8px">
                       <span style="min-width:0">${esc(item.message || item.code || '')}</span>
                       <span class="right mono" style="color:var(--text-4)">${esc(item.code || '')}</span>
                     </div>`
                   )
                   .join('')}
                 ${items.length > 30 ? `<div class="hint">仅显示前 30 项</div>` : ''}
               </div>
             </details>`
          : ''
      }`
    )
    .join('');

  const level = check?.level || (findings.length ? 'warn' : 'ok');
  const levelText = { error: '存在错误', warn: '有警告', ok: '全部通过' }[level] || level;
  openDialog({
    title: `一致性规则矩阵 · ${levelText}`,
    wide: true,
    body: `
      <div class="hint" style="margin-bottom:8px">
        规则都在后端执行（检查全剧音色解析结果）；这里只按规则归类展示，改完角色/音色后重新进入本页即可刷新。
      </div>
      ${rows || '<div class="empty"><span>没有需要展示的规则</span></div>'}
      <div class="hint" style="margin-top:8px">
        台词统计：${Number(check?.stats?.lines || 0)} 行 / ${Number(check?.stats?.speakers || 0)} 个说话人 /
        已解析音色 ${Number(check?.stats?.resolved || 0)} 行
      </div>`,
    foot: `<div class="right"><button class="btn" data-dlg-close>关闭</button></div>`,
  });
}

/** 进入角色视图 */
export async function renderCharacters(root, { path }) {
  stopAudio();
  if (state.path !== path) resetProject(path);
  if (!bound) {
    root.addEventListener('click', onRootClick);
    root.addEventListener('input', onRootInput);
    root.addEventListener('focusout', onRootFocusOut);
    bound = true;
  }
  root.innerHTML = `${topbar({ left: '', crumbs: '<span class="cur">正在加载角色…</span>', right: '' })}`;
  try {
    await loadAll(path);
  } catch (error) {
    root.innerHTML = `<div class="page"><div class="empty">${icon('warn', 22)}<b>角色加载失败</b>
      <span>${esc(error.message)}</span><a class="btn" href="#/projects" style="margin-top:10px">回工作区</a></div></div>`;
    return;
  }
  const boundCount = characters.filter(hasVoice).length;
  root.innerHTML = `
    ${topbar({
      left: `<button class="btn icon" data-act="back" title="回工作区">${icon('back')}</button>
             <div class="brand"><span class="brand-mark"></span><span style="font-size:12.5px">角色<small>本地版</small></span></div>`,
      crumbs: `<a href="#/projects">${esc(state.info?.work?.title || '工程')}</a>
               <span class="sep">/</span><span class="cur">角色</span>`,
      right: `<span class="save-state ok" id="chara-save">${icon('check', 12)}已自动保存</span>
        <span class="badge ${boundCount === characters.length ? 'ok' : 'warn'}" id="chara-bind">
          已绑定 ${boundCount}/${characters.length}</span>
        <button class="btn" data-act="to-editor">${icon('chapter')} 回画本</button>`,
    })}
    <div class="studio" id="studio">
      ${railbar('characters', { chapters: state.chapters.length })}
      ${listHTML()}
      <section class="editor">
        <div class="ed-filters" style="height:44px">
          <span style="font-size:12px;color:var(--text-3)">角色档案</span>
          <div class="grow"></div>
          <button class="badge ${check?.level === 'error' ? 'bad' : check?.findings?.length ? 'warn' : 'ok'}"
            data-act="consistency" title="查看一致性规则矩阵：旁白单一音色、跨章一致、同场景不撞音等">
            ${icon('check', 11)} 一致性${
              check?.findings?.length ? `提示 ${check.findings.length}` : '通过'
            }</button>
        </div>
        <div id="chara-main" style="overflow:auto;flex:1">${profileHTML()}</div>
      </section>
      <aside class="studio-right"></aside>
    </div>`;
  repaint();
}
