/**
 * 角色视图（V3）：角色列表 + 档案编辑。
 * 结构对齐参考：左侧上下文面板是角色列表（旁白置顶、类型筛选、搜索），
 * 主区是选中角色的档案表单；音色绑定在 V4 音色视图里完成。
 */
import { esc, icon, toast, openDialog, closeDialog } from '../dom.js';
import { getJSON, patchJSON, postJSON, fileUrl, streamJSON } from '../api.js';
import { selectHTML, labelOf, isEnumValue, ENUMS } from '../enums.js';
import { openProgress } from '../progress.js';
import { confirmLlm, llmErrorText } from '../llm.js';
import { modeSwitchHTML, bindModeSwitch } from '../gen-mode.js';
import { state, resetProject } from '../store.js';
import { railbar, topbar, goChapter, goProjects } from '../shell.js';

/** 角色定位顺序取自枚举表，避免和下拉选项脱节 */
const ROLE_ORDER = ENUMS.role.options.map(([value]) => value);

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
/** 大纲生成时按章登记的新建角色：`[{index, title, items:[{id,name,created_at}]}]` */
let createdChapters = [];
/** 待定角色队列：第二次出现才建卡的候选人（含出现次数与归属线索） */
let pendingCast = [];
/** 历史遗留的非法枚举值（`/api/ui/characters` 的 enum_issues，含原值与建议值） */
let enumIssues = [];
/** 修正进行中：避免连点重复提交 */
let enumFixing = false;

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
      <b>还没有角色</b><span>从正文切分时会自动识别人物，也可以点上方「+」手动新增。</span>
    </div>`;
  } else if (rows.length) {
    body = rows
      .map(
        (item) => `
      <div class="chap-row ${item.id === selectedId ? 'on' : ''}" data-chara="${esc(item.id)}"
           title="${esc(item.name)} · ${esc(labelOf('role', item.role, '角色'))}${hasVoice(item) ? '' : ' · 缺音色'}">
        <span class="cn">${esc(item.name)}</span>
        <span class="dot ${hasVoice(item) ? 'ok' : 'warn'}"></span>
        <span class="cw">${esc(labelOf('role', item.role, '角色'))}</span>
      </div>`
      )
      .join('');
  } else {
    body = `<div class="empty"><b>没有匹配的角色</b><span>换个关键词或清空筛选</span></div>`;
  }
  const createdCount = createdChapters.reduce((total, row) => total + (row.items || []).length, 0);
  return `
    <section class="ctx" aria-label="角色列表">
      <div class="ctx-head">
        <span class="ct">角色</span>
        <span class="badge mute">${characters.length} 人</span>
        <div class="right">
          <button class="btn sm" data-act="new-this-chapter" ${createdCount ? '' : 'disabled'}
            title="${createdCount ? '查看大纲生成时每章新建的角色，点名字可直接切换' : '这次大纲生成没有按章新建角色'}">
            本章新建${createdCount ? ` ${createdCount}` : ''}
          </button>
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
                title="${role === 'all' ? `显示全部 ${characters.length} 个角色` : `只看${labelOf('role', role, '角色')}`}">
                ${role === 'all' ? '全部' : esc(labelOf('role', role, '角色'))}</button>`
            )
            .join('')}
        </div>
      </div>
      <div class="chap-list">${body}</div>
      ${pendingCastHTML()}
    </section>`;
}

/**
 * 待定角色区：第二次出现才建卡的候选人。
 * 空队列也给出明确说明，避免侧栏留一块空白。
 * @returns {string} HTML
 */
function pendingCastHTML() {
  const rows = pendingCast
    .map(
      (item) => `
      <div class="summary-line" style="gap:6px;align-items:flex-start;padding:6px 4px">
        <span style="min-width:0">
          <b style="font-size:12px">${esc(item.name)}</b>
          <span class="badge mute" style="margin-left:4px">${Number(item.occurrences || 0)} 次 · ${Number(
            item.chapter_count || 0
          )} 章</span>
          <div class="hint" style="margin-top:2px">首次：第 ${Number(item.first_index || 0)} 章《${esc(
            item.first_title || ''
          )}》</div>
          <div class="hint" style="margin-top:1px" title="${esc(item.clue || '暂无归属线索')}">线索：${esc(
            item.clue || '暂无'
          )}</div>
        </span>
        <span class="right" style="display:flex;gap:4px;flex-shrink:0">
          <button class="btn sm" data-act="pending-promote" data-key="${esc(item.key)}"
            title="立即建卡：沿用角色识别的建卡字段，不会改动已有角色">建卡</button>
          <button class="btn sm" data-act="pending-discard" data-key="${esc(item.key)}"
            title="只从待定队列移除，不影响已建角色">丢弃</button>
        </span>
      </div>`
    )
    .join('');
  return `
    <div class="ctx-group" style="margin-top:4px" title="角色识别遇到首次出现的新人物时先放到这里，等第二次出现自动建卡">
      待定角色 <span class="n">${pendingCast.length}</span>
      <span class="right hint">第二次出现才建卡</span>
    </div>
    <div style="max-height:190px;overflow-y:auto;padding:0 8px 8px">
      ${
        pendingCast.length
          ? rows
          : `<div class="hint" style="padding:6px 4px;line-height:1.6">当前没有待定角色。故事里首次出现的新人物会先放在这里，等第二次出现自动建卡；也可以直接建卡或丢弃。</div>`
      }
    </div>`;
}

/** 说话风格（只读展示，避免与档案字段重复编辑） */
function speechLine(item) {
  const style = item.speech_style || {};
  const parts = [
    style.tempo && `语速 ${labelOf('speech_tempo', style.tempo, '未填写')}`,
    style.pitch && `音高 ${labelOf('speech_pitch', style.pitch, '未填写')}`,
    style.accent,
  ]
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

const RELATION_SENTIMENTS = [
  ['', '未标注'],
  ['ally', '盟友'],
  ['enemy', '敌对'],
  ['family', '亲属'],
  ['romance', '情感'],
  ['neutral', '一般'],
];

/** 姓名 → 角色 ID（旧关系备注可能以姓名为键，展示时统一解析成 ID） */
function idByName() {
  const map = {};
  characters.forEach((item) => {
    if (item.name) map[item.name] = item.id;
  });
  return map;
}

/**
 * 整理当前角色的关系行：结构化 relations 优先，旧 relationship_notes 补位。
 * @param {object} item 角色
 * @returns {Array<{target_id,label,sentiment,note}>}
 */
function relationRows(item) {
  const byName = idByName();
  const rows = [];
  const seen = new Set();
  ((item.state || {}).relations || []).forEach((row) => {
    const target = String(row.target_id || '');
    if (!target) return;
    rows.push({
      target_id: target,
      label: row.label || '',
      sentiment: row.sentiment || '',
      note: row.note || '',
    });
    seen.add(target);
  });
  Object.entries((item.state || {}).relationship_notes || {}).forEach(([key, note]) => {
    const target = key.startsWith('chr_') ? key : byName[key] || '';
    if (!target || seen.has(target) || !String(note || '').trim()) return;
    rows.push({ target_id: target, label: '', sentiment: '', note: String(note) });
    seen.add(target);
  });
  return rows;
}

/**
 * 关系编辑卡：目标角色只能从角色库选择，落盘为角色 ID。
 * @param {object} item 角色
 * @returns {string} HTML
 */
function relationsCardHTML(item) {
  const cast = characters.filter((row) => row.role !== 'narrator' && row.id !== item.id);
  const rows = relationRows(item);
  const nameOf = (id) => characters.find((row) => row.id === id)?.name || id;
  const body = rows
    .map(
      (row, index) => `<div class="arc-row" data-rel="${index}"
        style="display:grid;grid-template-columns:150px 110px 110px 1fr auto;gap:6px;align-items:center">
        <select class="select" data-rel-target="${index}">
          ${cast
            .map(
              (option) =>
                `<option value="${esc(option.id)}" ${option.id === row.target_id ? 'selected' : ''}>${esc(
                  option.name
                )}</option>`
            )
            .join('')}
        </select>
        <input class="input" data-rel-label="${index}" placeholder="关系名" value="${esc(row.label || '')}" />
        <select class="select" data-rel-sentiment="${index}">${RELATION_SENTIMENTS.map(
          ([value, label]) =>
            `<option value="${value}" ${value === row.sentiment ? 'selected' : ''}>${label}</option>`
        ).join('')}</select>
        <input class="input" data-rel-note="${index}" placeholder="备注（会进提示词）" value="${esc(
          row.note || ''
        )}" />
        <button class="btn icon sm" data-act="relation-del" data-index="${index}" title="删除这段关系">${icon(
          'trash',
          12
        )}</button>
      </div>`
    )
    .join('');
  return `
    <div class="card" style="margin:0 18px 18px" id="chara-relations">
      <div class="card-head"><span class="ct">角色关系</span>
        <div class="right">
          <button class="btn sm" data-act="relation-add">添加关系</button>
          <button class="btn sm" data-act="relation-graph">关系图</button>
          <button class="btn sm" data-act="view-arcs">角色弧线</button>
        </div>
      </div>
      <div class="card-body" style="display:grid;gap:8px">
        <div id="chara-rel-rows" style="display:grid;gap:6px">
          ${
            body ||
            '<div class="hint">还没有关系：关系目标只能从角色库选择，保存的是角色 ID；姓名只用于展示。</div>'
          }
        </div>
        <div class="hint">关系会随角色卡进入生成提示词。目标角色：${esc(
          rows.map((row) => nameOf(row.target_id)).join('、') || '—'
        )}</div>
      </div>
    </div>`;
}

/** 读取关系编辑区，返回结构化 relations（用于 PATCH） */
function collectRelations() {
  return [...document.querySelectorAll('#chara-rel-rows [data-rel]')]
    .map((row) => {
      const index = row.dataset.rel;
      const target = row.querySelector(`[data-rel-target="${index}"]`)?.value || '';
      if (!target) return null;
      return {
        target_id: target,
        label: row.querySelector(`[data-rel-label="${index}"]`)?.value.trim() || '',
        sentiment: row.querySelector(`[data-rel-sentiment="${index}"]`)?.value || null,
        note: row.querySelector(`[data-rel-note="${index}"]`)?.value.trim() || '',
      };
    })
    .filter(Boolean);
}

/**
 * 历史枚举值的「改前」展示：纯中文原样显示，含英文字母的旧值统一显示「未识别」，
 * 原始值放在 title 里备查，不在页面上裸露英文。
 * @param {object} issue 枚举问题（含 value）
 * @returns {string} 展示文案
 */
function legacyValueText(issue) {
  const text = String(issue.value ?? '').trim();
  if (!text) return '（空值）';
  return /[A-Za-z]/.test(text) ? '未识别' : text;
}

/**
 * 把一条枚举问题换算成 PATCH 字段：语速 / 音高要连带整份 speech_style，
 * 其余字段直接按路径末段提交。
 * @param {object} character 角色对象
 * @param {object} issue 枚举问题
 * @returns {object|null} PATCH 补丁；没有建议值时返回 null
 */
function enumPatchOf(character, issue) {
  const suggestion = issue.suggestion?.value;
  if (!character || !suggestion) return null;
  const field = String(issue.path || '').split('/').filter(Boolean).pop() || '';
  if (field === 'tempo' || field === 'pitch') {
    return { speech_style: { ...(character.speech_style || {}), [field]: suggestion } };
  }
  return { [field]: suggestion };
}

/**
 * 把问题列表换算成可执行的改动（丢弃没有建议值或找不到角色的项）。
 * @param {object[]} issues 枚举问题列表
 * @returns {{issue:object,character:object,patch:object}[]} 改动列表
 */
function enumChangesOf(issues) {
  return issues
    .map((issue) => {
      const character = characters.find((row) => row.id === issue.character_id);
      return { issue, character, patch: enumPatchOf(character, issue) };
    })
    .filter((row) => row.patch);
}

/** 历史枚举值告警卡片：逐条给出「按建议修正」，也可一次全部修正 */
function enumIssuesCardHTML() {
  if (!enumIssues.length) return '';
  const fixable = enumIssues.filter((item) => item.suggestion?.value).length;
  const rows = enumIssues
    .map((issue) => {
      const before = legacyValueText(issue);
      const after = issue.suggestion?.label || '';
      // 「快 → 快」这种旧写法与标准值同名的，补一句说明，避免看起来没变化
      const sameText = issue.suggestion && before === after;
      return `
      <div class="summary-line" style="align-items:flex-start">
        <span style="min-width:0">
          <b>${esc(issue.character_name || '未知角色')}</b>
          <span class="badge mute" style="margin-left:6px">${esc(issue.field || '枚举字段')}</span>
          <span style="font-size:11.5px;color:var(--text-3);margin-left:8px">
            当前：<span title="原值：${esc(String(issue.value ?? ''))}">${esc(before)}</span>${
              sameText ? '（旧写法）' : ''
            }
            ${
              issue.suggestion
                ? `→ 建议改为：<b style="color:var(--text)">${esc(after)}</b>${sameText ? '（标准写法）' : ''}`
                : '；没有可用建议值，请在下方下拉里手动选择'
            }
          </span>
        </span>
        <span class="right">${
          issue.suggestion?.value
            ? `<button class="btn sm" data-act="enum-fix" data-char="${esc(issue.character_id || '')}"
                 data-path="${esc(issue.path || '')}"
                 title="把「${esc(issue.field || '')}」改成建议值并立即保存">按建议修正</button>`
            : ''
        }</span>
      </div>`;
    })
    .join('');
  return `
    <div class="card" id="chara-enum-issues" style="margin-bottom:14px">
      <div class="card-head"><span class="ct">历史枚举值待修正</span>
        <div class="right"><span class="badge warn">${enumIssues.length} 处</span></div>
      </div>
      <div class="card-body" style="padding:0">
        ${rows}
        <div class="summary-line" style="gap:10px">
          <span class="hint">这些值来自旧工程，只有点「按建议修正」才会写入；没有建议值的项不会自动改。</span>
          <span class="right">${
            fixable
              ? `<button class="btn primary sm" data-act="enum-fix-all">全部按建议修正（${fixable} 处）</button>`
              : ''
          }</span>
        </div>
      </div>
    </div>`;
}

/**
 * 修正枚举问题：多处改动先弹确认并逐条列出，单处直接提交。
 * @param {object[]} issues 待修正的枚举问题
 */
function confirmEnumFixes(issues) {
  const changes = enumChangesOf(issues);
  if (!changes.length) return toast('这些历史值没有可自动修正的建议值，请在对应下拉里手动选择', 'warn', 4200);
  if (changes.length === 1) {
    void applyEnumFixes(changes);
    return;
  }
  const rows = changes
    .map(
      ({ issue, character }) => `
      <div class="summary-line">
        <span style="min-width:0">${esc(character.name || character.id)} · ${esc(issue.field || '枚举字段')}</span>
        <span class="right">${esc(legacyValueText(issue))} → <b>${esc(issue.suggestion.label || '')}</b></span>
      </div>`
    )
    .join('');
  const mask = openDialog({
    title: `按建议修正 ${changes.length} 处历史枚举值`,
    body: `<div class="dlg-note">确认后会把下面每一处改成建议值并立即保存；未列出的字段保持不动。</div>${rows}`,
    foot: `<div class="right"><button class="btn" data-dlg-close>取消</button>
      <button class="btn primary" id="enum-fix-ok">确认修正</button></div>`,
  });
  mask.querySelector('#enum-fix-ok').addEventListener('click', (event) => {
    event.currentTarget.disabled = true;
    event.currentTarget.textContent = '修正中…';
    closeDialog();
    void applyEnumFixes(changes);
  });
}

/**
 * 通过现有保存接口逐条提交枚举修正，结束后刷新页面数据。
 * @param {{issue:object,character:object,patch:object}[]} changes 改动列表
 */
async function applyEnumFixes(changes) {
  if (enumFixing) return;
  enumFixing = true;
  const failures = [];
  for (const { issue, character, patch } of changes) {
    try {
      await patchJSON('/api/ui/characters', { path: state.path, id: character.id, ...patch });
    } catch (error) {
      failures.push(`${character.name || character.id} · ${issue.field || '枚举字段'}：${error.message}`);
    }
  }
  enumFixing = false;
  await reloadPreservingView();
  if (!failures.length) {
    toast(`已按建议修正 ${changes.length} 处历史枚举值`, 'ok');
    return;
  }
  if (failures.length === 1) {
    toast(`修正失败：${failures[0]}`, 'bad', 6000);
    return;
  }
  openDialog({
    title: '部分枚举值修正失败',
    body: `<div class="dlg-note">以下 ${failures.length} 处没有改成功，原因如上；可修正后重试。</div>
      ${failures.map((text) => `<div class="summary-line"><span>${esc(text)}</span></div>`).join('')}`,
    foot: `<div class="right"><button class="btn" data-dlg-close>知道了</button></div>`,
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
  const locked = item.role === 'narrator';
  const lockHint = locked ? '旁白是固定角色，不可改名或改定位' : '';
  const rowStats = stats[item.id] || { lines: 0, scenes: 0, chapters: [], planned_chapters: [] };
  const firstChapter = rowStats.chapters[0];
  const hasAppearance = Boolean(rowStats.lines || rowStats.scenes);
  const appearanceLine = hasAppearance
    ? `${Number(rowStats.lines || 0)} 行台词 · ${Number(rowStats.scenes || 0)} 个场景${
        firstChapter ? ` · 首次登场第 ${firstChapter.index} 章` : ''
      }`
    : (rowStats.planned_chapters || []).length
      ? `还没有正文出场 · 已排进 ${rowStats.planned_chapters.length} 章大纲`
      : '还没有出场记录';
  return `
    <div class="page-head">
      <div class="ph">
        <div class="eyebrow"><span class="ln"></span>${esc(labelOf('role', item.role, '角色'))}</div>
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
        <button class="btn" data-act="view-appearances" ${
          hasAppearance || (rowStats.planned_chapters || []).length ? '' : 'disabled'
        }
          title="查看实际出场（台词/场景）与大纲计划出场，可双向跳转">
          查看出场${hasAppearance ? `（${Number(rowStats.lines || 0)} 行）` : ''}
        </button>
        ${
          locked
            ? ''
            : `<button class="btn" data-act="merge-character"
                 title="把这张卡并到已有角色上；台词、场景、大纲节拍与关系会一起迁移">合并到已有角色…</button>`
        }
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
            <input class="input" data-free="1" id="ch-name" value="${esc(item.name || '')}" ${locked ? 'disabled' : ''}
              title="${esc(lockHint || '角色正式名，用于台词说话人识别')}" /></div>
          <div class="field"><label>别名（逗号分隔，自由填写）</label>
            <input class="input" data-free="1" id="ch-aliases" value="${esc((item.aliases || []).join('，'))}"
              title="别名也用于台词说话人识别，用逗号分隔" /></div>
          <div class="field"><label>定位</label>
            ${selectHTML('role', {
              id: 'ch-role',
              current: item.role,
              title: lockHint || '决定角色在列表里的排序与筛选，只能从列表选择',
              attrs: locked ? 'disabled' : '',
            })}</div>
          <div class="field"><label>性别</label>
            ${selectHTML('gender', {
              id: 'ch-gender',
              current: item.gender || 'unknown',
              title: '影响音色推荐与同场听感，只能从列表选择',
            })}</div>
          <div class="field"><label>年龄段</label>
            ${selectHTML('age_range', {
              id: 'ch-age',
              current: item.age_range || '',
              emptyLabel: '未指定',
              title: '用于音色推荐，只能从档位中选择；旧工程的自由文本会提示重新选择',
            })}</div>
          <div class="field"><label>性格（逗号分隔，自由填写）</label>
            <input class="input" data-free="1" id="ch-personality" value="${esc((item.personality || []).join('，'))}"
              title="性格关键词，多个用逗号分隔" /></div>
          <div class="field" style="grid-column:1/-1"><label>背景（自由填写）</label>
            <textarea class="textarea" data-free="1" id="ch-background" rows="3" title="角色的来历、动机与关系背景">${esc(item.background || '')}</textarea></div>
          <div class="field" style="grid-column:1/-1"><label>当前处境（写作时注入「当前处境」）</label>
            <textarea class="textarea" data-free="1" id="ch-situation" rows="2"
              title="角色在最新一章结束时的处境；生成正文时会作为角色卡的一部分进入提示词">${esc(
                (item.state || {}).situation || ''
              )}</textarea></div>
          <div class="field" style="grid-column:1/-1"><label>已知信息（逗号分隔，自由填写）</label>
            <input class="input" data-free="1" id="ch-knowledge" value="${esc(
              ((item.state || {}).knowledge || []).join('，')
            )}" title="角色当前知道的关键事实；生成时会限制角色不能说出未知信息" /></div>
          <div class="field"><label>语速</label>
            ${selectHTML('speech_tempo', {
              id: 'ch-speech-tempo',
              current: (item.speech_style || {}).tempo || '',
              emptyLabel: '未指定',
              title: '写作口吻约束：慢 / 中速 / 快，只能从列表选择',
            })}</div>
          <div class="field"><label>音高 / 音色</label>
            ${selectHTML('speech_pitch', {
              id: 'ch-speech-pitch',
              current: (item.speech_style || {}).pitch || '',
              emptyLabel: '未指定',
              title: '写作口吻约束：偏低 / 中等 / 偏高，只能从列表选择',
            })}</div>
          <div class="field"><label>口音 / 用词习惯</label>
            <input class="input" data-free="1" id="ch-speech-accent" value="${esc(
              (item.speech_style || {}).accent || ''
            )}" placeholder="如：带江南口音，爱用反问" title="直接写进提示词的口语习惯" /></div>
          <div class="field"><label>口头禅（逗号分隔）</label>
            <input class="input" data-free="1" id="ch-speech-catch" value="${esc(
              ((item.speech_style || {}).catchphrases || []).join('，')
            )}" placeholder="如：这事没那么简单，先别急" title="写作时会要求角色复用这些口头禅" /></div>
          <div class="field" style="grid-column:1/-1"><label>说话风格（音色侧，只读）</label>
            <div class="input" style="display:flex;align-items:center;color:var(--text-3)"
              title="音色与行内参数推导出的听感；写作侧用上面的「说话方式」字段">${esc(speechLine(item))}</div></div>
          <div class="field" style="grid-column:1/-1"><label>外貌提示词（自由填写）</label>
            <textarea class="textarea" data-free="1" id="ch-appearance" rows="3"
              title="用于角色立绘与配图的形象描述">${esc(item.appearance_prompt || '')}</textarea></div>
          <div class="field" style="grid-column:1/-1"><label>负向提示词（自由填写）</label>
            <textarea class="textarea" data-free="1" id="ch-negative" rows="2"
              title="生成角色形象时要避免的特征，多个用逗号分隔">${esc(item.negative_prompt || '')}</textarea></div>
        </div>
        <div class="hint">音色绑定在下方或「音色」视图完成；档案改动自动保存并写回 characters.json。</div>
      </div>
    </div>
    ${enumIssuesCardHTML()}
    ${relationsCardHTML(item)}
    ${imageCardHTML(item)}
    ${voiceBindingHTML(item)}
    ${chapterOverrideCardHTML()}`;
}

/** 读取全部数据 */
async function loadAll(path) {
  const [info, chapters, charaPayload, castPayload, checkPayload, statsPayload, createdPayload, pendingPayload] =
    await Promise.all([
      getJSON('/api/project/info', { path }),
      getJSON('/api/ui/chapters', { path }),
      getJSON('/api/ui/characters', { path }),
      getJSON('/api/ui/cast', { path }).catch(() => null),
      getJSON('/api/ui/cast/check', { path }).catch(() => null),
      getJSON('/api/ui/characters/stats', { path }).catch(() => null),
      getJSON('/api/ui/characters/new-this-chapter', { path }).catch(() => null),
      getJSON('/api/ui/cast/pending', { path }).catch(() => null),
    ]);
  state.info = info;
  state.chapters = chapters || [];
  if (!state.chapters.some((item) => item.id === state.chapterId)) {
    state.chapterId = state.chapters[0]?.id || '';
  }
  characters = charaPayload?.characters || [];
  enumIssues = charaPayload?.enum_issues || [];
  voices = castPayload?.voices || [];
  check = checkPayload;
  stats = statsPayload?.characters || {};
  createdChapters = (createdPayload?.chapters || []).filter((row) => (row.items || []).length);
  pendingCast = pendingPayload?.items || [];
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
  assertEnumsPicked();
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
    situation: document.getElementById('ch-situation')?.value || '',
    knowledge: split(document.getElementById('ch-knowledge')?.value).join('，'),
    speech_style: JSON.stringify({
      tempo: document.getElementById('ch-speech-tempo')?.value.trim() || '',
      pitch: document.getElementById('ch-speech-pitch')?.value.trim() || '',
      accent: document.getElementById('ch-speech-accent')?.value.trim() || '',
      catchphrases: split(document.getElementById('ch-speech-catch')?.value),
    }),
    relations: JSON.stringify(collectRelations()),
    appearance_prompt: document.getElementById('ch-appearance')?.value || '',
    negative_prompt: document.getElementById('ch-negative')?.value || '',
  };
  return fields;
}

/**
 * 保存前检查枚举下拉：历史非法值必须重新选择，避免把旧英文值再写回去。
 * @throws {Error} 存在未修正的枚举字段
 */
function assertEnumsPicked() {
  const ids = ['ch-role', 'ch-gender', 'ch-age', 'ch-speech-tempo', 'ch-speech-pitch'];
  const broken = ids
    .map((id) => document.getElementById(id))
    .filter((node) => node && node.value && !isEnumValue(node.dataset.enum, node.value))
    .map((node) => ENUMS[node.dataset.enum]?.label || node.dataset.enum);
  if (broken.length) {
    throw new Error(`请先重新选择：${[...new Set(broken)].join('、')}（原值不是有效选项）`);
  }
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
    body: `<div class="field"><label>姓名</label><input class="input" data-free="1" id="nc-name" placeholder="角色正式名" /></div>
      <div class="field"><label>定位</label>${selectHTML('role', {
        id: 'nc-role',
        current: 'protagonist',
        attrs: '',
      })}</div>`,
    foot: `<div class="right"><button class="btn" data-dlg-close>取消</button>
      <button class="btn primary" id="nc-ok">创建</button></div>`,
  });
  mask.querySelector('#nc-ok').addEventListener('click', async (event) => {
    const name = mask.querySelector('#nc-name').value.trim();
    if (!name) return toast('请填写姓名', 'warn');
    event.currentTarget.disabled = true;
    event.currentTarget.textContent = '创建中…';
    try {
      await postJSON('/api/ui/characters', {
        path: state.path,
        characters: [{ name, role: mask.querySelector('#nc-role').value }],
      });
      closeDialog();
      toast('角色已创建', 'ok');
      await reloadPreservingView();
    } catch (error) {
      toast(`创建失败：${error.message}；可检查姓名是否与已有角色重复`, 'bad', 4200);
      event.currentTarget.disabled = false;
      event.currentTarget.textContent = '创建';
    }
  });
}

/** 删除角色（先看影响面：谁会受影响、各多少处、来自哪里；有引用必须二次确认） */
async function deleteCharacter() {
  const item = current();
  if (!item) return;
  let refs = { total: 0, chapters: [], sources: [], relations: [] };
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
  const sourceRows = (refs.sources || [])
    .map(
      (source) => `<div class="imp-row">
        <span style="min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(source.label)}</span>
        <span class="badge warn">${Number(source.count)} 处</span>
      </div>
      <div class="hint">${esc(source.where || '')}</div>`
    )
    .join('');
  const mask = openDialog({
    title: '删除角色',
    wide: blocked,
    body: blocked
      ? `<div class="dlg-note bad">「${esc(item.name)}」仍被引用，共 ${Number(refs.total)} 处，删除前请先确认影响面。</div>
         <div class="impact">${sourceRows}</div>
         ${
           samples.length
             ? `<div class="impact">
                  ${samples
                    .map(
                      (line) => `<div class="imp-row">
                        <span style="min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
                          ${esc(line.chapter)} · ${esc((line.text || '').slice(0, 34))}
                        </span>
                        <span class="badge warn">台词</span>
                      </div>`
                    )
                    .join('')}
                </div>`
             : ''
         }
         <div class="hint">台词只列出前 ${samples.length} 行；大范围引用建议先改派台词与大纲选角，再回来删除。</div>`
      : `<div class="dlg-note">「${esc(item.name)}」没有被任何台词、大纲或关系引用，可以安全删除。</div>`,
    foot: `<div class="right">
      <button class="btn" data-dlg-close>取消</button>
      ${
        blocked
          ? '<button class="btn danger" id="del-chara-force">仍要删除（清理引用）</button>'
          : '<button class="btn danger" id="del-chara-ok">确认删除</button>'
      }</div>`,
  });
  const finish = async (force) => {
    try {
      const result = await postJSON('/api/ui/characters/delete', { path: state.path, id: item.id, force });
      closeDialog();
      const cleaned = Object.values((result && result.cleaned) || {}).reduce((sum, value) => sum + Number(value || 0), 0);
      toast(cleaned ? `角色已删除，并清理 ${cleaned} 处引用` : '角色已删除', 'ok');
      selectedId = '';
      await reloadPreservingView();
    } catch (error) {
      toast(`删除失败：${error.message}`, 'bad', 4200);
    }
  };
  mask.querySelector('#del-chara-ok')?.addEventListener('click', async (event) => {
    event.currentTarget.disabled = true;
    await finish(false);
  });
  mask.querySelector('#del-chara-force')?.addEventListener('click', () => {
    const confirmMask = openDialog({
      title: '确认删除并清理引用',
      body: `<div class="dlg-note bad">删除后不可恢复。将同时清理「${esc(item.name)}」在下列位置的引用：
        </div>
        <div class="impact">${sourceRows}</div>
        <div class="hint">台词会保留，但说话人变为「未知角色」，需要重新识别；大纲视角会被清空。</div>`,
      foot: `<div class="right"><button class="btn" data-dlg-close>再想想</button>
        <button class="btn danger" id="del-chara-confirm">确认删除并清理</button></div>`,
    });
    confirmMask.querySelector('#del-chara-confirm')?.addEventListener('click', async (event) => {
      event.currentTarget.disabled = true;
      await finish(true);
    });
  });
}

/**
 * 重新读取并局部重画，保留列表/主区滚动位置与当前输入焦点。
 * 用于合并、删除等必须刷新数据的操作，避免整页重绘把用户位置打回顶部。
 */
async function reloadPreservingView() {
  const scrolled = ['#chara-main', '.chap-list']
    .map((selector) => document.querySelector(selector))
    .filter(Boolean)
    .map((node) => ({ node, top: node.scrollTop }));
  const active = document.activeElement;
  const focusId = active?.id || '';
  const start = typeof active?.selectionStart === 'number' ? active.selectionStart : null;
  const end = typeof active?.selectionEnd === 'number' ? active.selectionEnd : null;
  await loadAll(state.path);
  repaint();
  scrolled.forEach(({ node, top }) => {
    if (node.isConnected) node.scrollTop = top;
  });
  if (focusId) {
    const node = document.getElementById(focusId);
    if (node) {
      node.focus({ preventScroll: true });
      if (start != null && typeof node.setSelectionRange === 'function') node.setSelectionRange(start, end);
    }
  }
  return { focusId };
}

/**
 * 合并到已有角色：先统计影响面，二次确认后迁移引用并删除源角色。
 * 影响面包含大纲节拍选角、章节视角、场景出场、台词行、关系与音色覆盖。
 */
function mergeCharacter() {
  const item = current();
  if (!item) return;
  const targets = characters.filter((row) => row.id !== item.id && row.role !== 'narrator');
  if (!targets.length) return toast('没有可合并的其它角色，请先新增一个目标角色', 'warn');
  const options = targets
    .map((row) => `<option value="${esc(row.id)}">${esc(row.name)} · ${esc(labelOf('role', row.role, '角色'))}</option>`)
    .join('');
  const mask = openDialog({
    title: `合并「${item.name}」到已有角色`,
    wide: true,
    body: `
      <div class="dlg-note warn">合并不可撤销：台词、场景出场、大纲节拍选角、章节视角、关系与音色覆盖都会迁移到目标角色，随后源角色被删除（姓名会变成目标角色的别名）。</div>
      <div class="field"><label>合并到</label>
        <select class="select" id="mg-target" title="选择要保留的角色卡">
          <option value="">请选择目标角色</option>
          ${options}
        </select>
      </div>
      <div id="mg-impact" class="hint">选择目标角色后显示影响面。</div>
      <div id="mg-error"></div>`,
    foot: `<div class="right"><button class="btn" data-dlg-close>取消</button>
      <button class="btn danger" id="mg-ok" disabled title="先选择目标角色并查看影响面">确认合并</button></div>`,
  });
  const targetSelect = mask.querySelector('#mg-target');
  const okButton = mask.querySelector('#mg-ok');
  const impactBox = mask.querySelector('#mg-impact');
  const errorBox = mask.querySelector('#mg-error');
  let impact = null;

  /** 把影响面渲染成可读明细 */
  const renderImpact = (data) => {
    const outline = data.outline || {};
    const chapters = data.chapters || {};
    const rows = [
      ['大纲节拍选角', Number(outline.beats || 0)],
      ['章节视角', Number(outline.pov || 0)],
      ['角色弧线', Number(outline.arcs || 0)],
      ['场景出场', Number(chapters.scenes || 0)],
      ['台词行', Number(chapters.lines || 0)],
      ['章节音色覆盖', Number(chapters.overrides || 0)],
      ['其它角色的关系', Number(data.relations || 0)],
    ];
    impactBox.innerHTML = `
      <div class="summary-line" style="margin-bottom:6px">
        <span>共 <b>${Number(data.total || 0)}</b> 处引用会改到「${esc(data.target?.name || '')}」名下</span>
        <span class="right badge ${Number(data.total || 0) ? 'warn' : 'ok'}">${Number(data.total || 0) ? '需迁移' : '无引用'}</span>
      </div>
      ${rows
        .map(
          ([label, count]) => `<div class="summary-line">
            <span style="min-width:0;font-size:12px;color:var(--text-3)">${esc(label)}</span>
            <span class="right mono" style="color:${count ? 'var(--text)' : 'var(--text-4)'}">${count}</span>
          </div>`
        )
        .join('')}`;
  };

  targetSelect.addEventListener('change', async () => {
    const target = targetSelect.value;
    impact = null;
    okButton.disabled = true;
    okButton.textContent = '确认合并';
    errorBox.innerHTML = '';
    if (!target) {
      impactBox.textContent = '选择目标角色后显示影响面。';
      return;
    }
    impactBox.textContent = '正在统计引用…';
    try {
      const data = await getJSON('/api/ui/characters/merge/impact', {
        path: state.path,
        source: item.id,
        target,
      });
      if (!targetSelect.isConnected || targetSelect.value !== target) return;
      impact = data;
      renderImpact(data);
      okButton.disabled = false;
      okButton.title = '确认后立即迁移引用并删除源角色';
    } catch (error) {
      impactBox.textContent = '';
      errorBox.innerHTML = `<div class="dlg-note bad">读取影响面失败：${esc(error.message)}
        <br>可关闭弹窗后重试；若持续失败，先到画本与大纲核对这个角色的引用是否正常。</div>`;
    }
  });

  okButton.addEventListener('click', async () => {
    const target = targetSelect.value;
    if (!target || !impact) return;
    okButton.disabled = true;
    targetSelect.disabled = true;
    okButton.textContent = '合并中…';
    errorBox.innerHTML = '';
    try {
      const data = await postJSON('/api/ui/characters/merge', {
        path: state.path,
        source: item.id,
        target,
      });
      closeDialog(false);
      selectedId = data.target?.id || target;
      const total = Number(data.impact?.total || 0);
      toast(`已把「${data.source?.name || item.name}」合并到「${data.target?.name || ''}」，迁移 ${total} 处引用`, 'ok', 4200);
      await reloadPreservingView();
      document.getElementById('ch-name')?.focus({ preventScroll: true });
    } catch (error) {
      errorBox.innerHTML = `<div class="dlg-note bad">合并失败：${esc(error.message)}
        <br>本次没有完成迁移。可关闭弹窗，打开该角色核对台词与大纲后再试；
        若引用被锁定，请先到「一致性」检查是否有非法绑定。</div>`;
      okButton.textContent = '确认合并';
      okButton.disabled = false;
      targetSelect.disabled = false;
    }
  });
}

/**
 * 本章新建：按章列出大纲生成时新建的角色（一次性龙套不在其中）。
 * 点角色名直接切到该角色，不重画整页。
 */
function openCreatedThisChapter() {
  const rows = createdChapters || [];
  if (!rows.length) return toast('这次大纲生成没有按章新建角色', 'info');
  const mask = openDialog({
    title: '本章新建角色',
    wide: true,
    body: `
      <div class="hint">大纲生成时按「第二次出现才建卡」登记的新角色；一次性的龙套不在这里。点名字可直接切换。</div>
      ${rows
        .map(
          (row) => `<div style="border-top:1px solid var(--line);padding:8px 0">
            <div class="summary-line" style="margin-bottom:6px">
              <span style="min-width:0;font-size:12px;color:var(--text-2)">第 ${Number(row.index || 0)} 章 · ${esc(
                row.title || '未命名章节'
              )}</span>
              <span class="right badge mute">${(row.items || []).length} 个</span>
            </div>
            <div style="display:flex;flex-wrap:wrap;gap:6px">
              ${(row.items || [])
                .map(
                  (entry) => `<button class="btn sm" data-pick-created="${esc(entry.id)}"
                    title="切换到 ${esc(entry.name)} 的角色档案">${esc(entry.name)}</button>`
                )
                .join('')}
            </div>
          </div>`
        )
        .join('')}`,
    foot: `<div class="right"><button class="btn" data-dlg-close>关闭</button></div>`,
  });
  mask.querySelectorAll('[data-pick-created]').forEach((button) => {
    button.addEventListener('click', () => {
      flushSave();
      stopAudio();
      selectedId = button.dataset.pickCreated;
      closeDialog(false);
      repaint();
    });
  });
}

/**
 * 待定角色立即建卡：沿用角色识别的建卡字段，并在成功后切到新角色档案。
 * @param {string} key 待定队列键
 * @param {HTMLButtonElement} button 触发按钮（用于禁用与「正在…」文案）
 */
async function promotePending(key, button) {
  const label = button.textContent;
  button.disabled = true;
  button.textContent = '建卡中…';
  try {
    const result = await postJSON('/api/ui/cast/pending/promote', { path: state.path, key });
    if (result.character?.id) selectedId = result.character.id;
    await reloadPreservingView();
    toast(
      result.created
        ? `已建卡：${result.character?.name || ''}`
        : `${result.character?.name || '该角色'} 已有角色卡，已从待定队列移除`,
      'ok'
    );
  } catch (error) {
    button.disabled = false;
    button.textContent = label;
    toast(error.message, 'bad', 4200);
  }
}

/**
 * 丢弃待定角色：二次确认后只从队列移除，不影响已建角色与正文。
 * @param {string} key 待定队列键
 */
function discardPending(key) {
  const item = pendingCast.find((row) => row.key === key);
  const name = item?.name || '这个角色';
  const mask = openDialog({
    title: '丢弃待定角色',
    body: `<div class="banner">${icon('warn', 13)} 丢弃后「${esc(
      name
    )}」不会建卡，也不会影响已有角色与已写正文。</div>
      <div class="hint" style="margin-top:8px">如果之后再次识别到同名角色，会重新进入待定队列。</div>`,
    foot: `<div class="right"><button class="btn" data-dlg-close>取消</button>
      <button class="btn danger" data-discard-ok>丢弃</button></div>`,
  });
  const ok = mask.querySelector('[data-discard-ok]');
  ok.addEventListener('click', async () => {
    if (ok.disabled) return;
    ok.disabled = true;
    ok.textContent = '丢弃中…';
    try {
      await postJSON('/api/ui/cast/pending/discard', { path: state.path, key });
      closeDialog();
      await reloadPreservingView();
      toast(`已丢弃：${name}`, 'ok');
    } catch (error) {
      ok.disabled = false;
      ok.textContent = '丢弃';
      toast(error.message, 'bad', 4200);
    }
  });
}

/**
 * 出场弹窗：实际出场（台词/场景）与计划出场（大纲节拍）并列，双向可跳。
 * 实际出场跳画本，计划出场跳小说页对应章节大纲。
 */
function viewAppearances() {
  const item = current();
  if (!item) return;
  const row = stats[item.id] || { lines: 0, scenes: 0, chapters: [], planned_chapters: [] };
  const planned = row.planned_chapters || [];
  if (!row.lines && !row.scenes && !planned.length) {
    return toast('这个角色既没有出场记录，也没有被排进任何章节', 'info');
  }
  const novelHref = (chapterId) =>
    `#/p/${encodeURIComponent(state.path)}/novel?chapter=${encodeURIComponent(chapterId)}`;
  const actualRows = (row.chapters || [])
    .map(
      (chapter) => `<button class="btn" style="height:auto;padding:9px 11px;justify-content:space-between"
        data-jump="${esc(chapter.id)}" title="跳到第 ${chapter.index} 章画本">
        <span style="min-width:0">第 ${chapter.index} 章 · ${esc(chapter.title || '未命名章节')}</span>
        <span class="badge mute">${Number(chapter.lines || 0)} 行 · ${Number(chapter.scenes || 0)} 场</span>
      </button>`
    )
    .join('');
  const plannedRows = planned
    .map(
      (chapter) => `<a class="btn" style="height:auto;padding:9px 11px;justify-content:space-between;text-decoration:none"
        href="${novelHref(chapter.id)}" title="跳到第 ${chapter.index} 章大纲">
        <span style="min-width:0">第 ${chapter.index} 章 · ${esc(chapter.title || '未命名章节')}</span>
        <span class="badge">计划 ${Number(chapter.beats || 0)} 个节拍</span>
      </a>`
    )
    .join('');
  const mask = openDialog({
    title: `${item.name} 的出场与计划`,
    body: `<div class="dlg-note">实际：${Number(row.lines || 0)} 行台词、${Number(
      row.scenes || 0
    )} 个场景；计划：${planned.length} 章。点实际出场跳画本，点计划跳大纲。</div>
      <div style="display:grid;gap:4px;margin-bottom:10px">
        <div class="hint">实际出场</div>
        ${
          actualRows ||
          '<div class="hint">还没有切分台词或场景标注；保存正文后这里会出现。</div>'
        }
      </div>
      <div style="display:grid;gap:4px">
        <div class="hint">计划出场（大纲节拍）</div>
        ${plannedRows || '<div class="hint">大纲里还没有给这个角色安排节拍。</div>'}
      </div>`,
    foot: `<div class="right"><button class="btn" data-dlg-close>关闭</button></div>`,
  });
  mask.querySelectorAll('[data-jump]').forEach((button) => {
    button.addEventListener('click', () => {
      closeDialog();
      goChapter(state.path, button.dataset.jump);
    });
  });
}

/** 关系编辑区追加一行（默认选第一个目标角色，保存由自动保存负责） */
function addRelationRow() {
  const item = current();
  const wrap = document.getElementById('chara-rel-rows');
  if (!item || !wrap) return;
  const cast = characters.filter((row) => row.role !== 'narrator' && row.id !== item.id);
  if (!cast.length) return toast('还没有其它角色可建立关系', 'warn');
  const index = wrap.querySelectorAll('[data-rel]').length;
  const hint = wrap.querySelector('.hint');
  if (hint) hint.remove();
  wrap.insertAdjacentHTML(
    'beforeend',
    `<div class="arc-row" data-rel="${index}"
      style="display:grid;grid-template-columns:150px 110px 110px 1fr auto;gap:6px;align-items:center">
      <select class="select" data-rel-target="${index}">${cast
        .map((row) => `<option value="${esc(row.id)}">${esc(row.name)}</option>`)
        .join('')}</select>
      <input class="input" data-rel-label="${index}" placeholder="关系名" />
      <select class="select" data-rel-sentiment="${index}">${RELATION_SENTIMENTS.map(
        ([value, label]) => `<option value="${value}">${label}</option>`
      ).join('')}</select>
      <input class="input" data-rel-note="${index}" placeholder="备注（会进提示词）" />
      <button class="btn icon sm" data-act="relation-del" data-index="${index}" title="删除这段关系">${icon(
        'trash',
        12
      )}</button>
    </div>`
  );
  scheduleSave();
}

/** 删除关系行：只从表单移除，自动保存后写回 */
function deleteRelationRow(index) {
  document.querySelector(`#chara-rel-rows [data-rel="${index}"]`)?.remove();
  scheduleSave();
}

/** 角色弧线弹窗：按章节列出该角色的 from → to，点击跳对应章节大纲 */
async function openCharacterArcs() {
  const item = current();
  if (!item) return;
  try {
    const data = await getJSON('/api/ui/characters/arcs', { path: state.path, character: item.id });
    const rows = data?.arcs || [];
    const html = rows
      .map(
        (row) => `<a class="btn" style="height:auto;padding:9px 11px;text-decoration:none;display:block"
          href="#/p/${encodeURIComponent(state.path)}/novel?chapter=${encodeURIComponent(row.chapter_id)}"
          title="跳到第 ${row.index} 章大纲">
          <div><b>第 ${row.index} 章</b> · ${esc(row.title || '未命名章节')}</div>
          <div class="hint">${esc(row.from_state || '（未填写起点）')} → ${esc(
            row.to_state || '（未填写终点）'
          )}${row.note ? `；${esc(row.note)}` : ''}</div>
        </a>`
      )
      .join('');
    openDialog({
      title: `${item.name} 的角色弧线`,
      body: `<div class="dlg-note">只在有角色弧线的大纲章节里出现；点击跳到大纲对应章节编辑。</div>
        <div style="display:grid;gap:6px;max-height:420px;overflow:auto">${
          html || '<div class="hint">还没有为该角色写过弧线：到小说页章节详情「角色绑定 → 角色弧线」添加。</div>'
        }</div>`,
      foot: `<div class="right"><button class="btn" data-dlg-close>关闭</button></div>`,
    });
  } catch (error) {
    toast(`读取角色弧线失败：${error.message}`, 'bad', 3600);
  }
}

/** 关系图弹窗：圆形布局画节点与边，点击节点切换角色 */
async function openRelationGraph() {
  try {
    const data = await getJSON('/api/ui/characters/graph', { path: state.path });
    const nodes = data?.nodes || [];
    const edges = data?.edges || [];
    if (!nodes.length) return toast('角色库还是空的', 'info');
    const width = 760;
    const height = 520;
    const cx = width / 2;
    const cy = height / 2;
    const radius = Math.min(width, height) / 2 - 70;
    const pos = {};
    nodes.forEach((node, index) => {
      const angle = (Math.PI * 2 * index) / nodes.length - Math.PI / 2;
      pos[node.id] = {
        x: cx + radius * Math.cos(angle),
        y: cy + radius * Math.sin(angle),
      };
    });
    const edgeHTML = edges
      .map((edge) => {
        const from = pos[edge.source];
        const to = pos[edge.target];
        if (!from || !to) return '';
        const mx = (from.x + to.x) / 2;
        const my = (from.y + to.y) / 2;
        const label = edge.label || (edge.kind === 'note' ? '备注' : '');
        return `<line x1="${from.x}" y1="${from.y}" x2="${to.x}" y2="${to.y}"
            stroke="${edge.sentiment === 'enemy' ? 'var(--bad)' : 'var(--line)'}"
            stroke-width="${edge.sentiment ? 1.6 : 1}" stroke-dasharray="${edge.kind === 'note' ? '4 3' : ''}" />
          ${label ? `<text x="${mx}" y="${my - 4}" font-size="10" fill="var(--text-4)" text-anchor="middle">${esc(label)}</text>` : ''}`;
      })
      .join('');
    const nodeHTML = nodes
      .map((node) => {
        const point = pos[node.id];
        return `<g data-graph-node="${esc(node.id)}" style="cursor:pointer">
          <circle cx="${point.x}" cy="${point.y}" r="24"
            fill="${node.id === selectedId ? 'var(--accent)' : 'var(--bg-2)'}"
            stroke="var(--line)" />
          <text x="${point.x}" y="${point.y + 4}" font-size="11" text-anchor="middle"
            fill="${node.id === selectedId ? '#fff' : 'var(--text-2)'}">${esc((node.name || '').slice(0, 4))}</text>
          <text x="${point.x}" y="${point.y + 42}" font-size="10" text-anchor="middle"
            fill="var(--text-4)">${esc(node.name || '')}</text>
        </g>`;
      })
      .join('');
    const mask = openDialog({
      title: '角色关系图',
      wide: true,
      body: `<div class="dlg-note">实线为结构化关系，虚线为旧关系备注；点击节点切换角色。关系目标统一为角色 ID。</div>
        <svg viewBox="0 0 ${width} ${height}" style="width:100%;height:auto;background:var(--bg-1);border-radius:8px">
          ${edgeHTML}${nodeHTML}
        </svg>`,
      foot: `<div class="right"><button class="btn" data-dlg-close>关闭</button></div>`,
    });
    mask.querySelectorAll('[data-graph-node]').forEach((node) => {
      node.addEventListener('click', () => {
        selectedId = node.dataset.graphNode;
        closeDialog();
        repaint();
      });
    });
  } catch (error) {
    toast(`读取关系图失败：${error.message}`, 'bad', 3600);
  }
}

/** 形象图类型（顺序与中文名统一取自 enums.js） */
const IMAGE_KINDS = ENUMS.image_kind.options.map(([value]) => value);
/**
 * 角色形象卡：三类形象图缩略与生成入口。
 * 图片地址走 /api/ui/file，路径来自 characters.json 的 image_assets。
 * @param {object} item 角色对象
 */
function imageCardHTML(item) {
  const assets = item.image_assets || {};
  const cells = IMAGE_KINDS.map((kind) => {
    const rel = assets[kind];
    const label = labelOf('image_kind', kind, '形象图');
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
  const cells = IMAGE_KINDS.map((kind) => {
    const rel = assets[kind];
    const label = labelOf('image_kind', kind, '形象图');
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
    // 生成方式放在 #ci-body 之外：刷新缩略图时只重绘 #ci-body，分段按钮的绑定不会丢
    body: `${modeSwitchHTML('character.image', { fallback: 'regen' })}
      <div id="ci-body">${characterImageBodyHTML(item)}</div>`,
    foot: `<div class="right">
      <button class="btn" data-ci="close">关闭</button>
      <button class="btn" data-ci="refresh" title="重新读取角色档案里的形象图">刷新</button>
      <button class="btn primary" data-ci="gen" data-kind="fullbody">生成全身像</button>
    </div>`,
  });
  /** 读取当前选择的生成方式：fill=已有类型跳过，regen=覆盖重生成 */
  const modeOf = bindModeSwitch(mask, 'character.image', { fallback: 'regen' });
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
    if (button.dataset.ci === 'gen') void runCharacterImage(mask, button.dataset.kind, modeOf);
  });
  if (autoKind) void runCharacterImage(mask, autoKind, modeOf);
}

/**
 * 生成一类角色形象图并刷新档案。
 * @param {HTMLElement} mask 弹窗根节点
 * @param {string} kind fullbody / closeup / turnaround
 * @param {() => 'fill'|'regen'} [modeOf] 读取生成方式；fill 且该类图已存在时跳过
 */
async function runCharacterImage(mask, kind, modeOf = () => 'regen') {
  const item = current();
  if (!item || !kind) return;
  const mode = modeOf();
  const label = labelOf('image_kind', kind, '形象图');
  // 与 characterImageBodyHTML 一致：characters.json 的 image_assets[kind] 即该类形象图
  const existed = Boolean((item.image_assets || {})[kind]);
  if (mode === 'fill' && existed) {
    const skippedText = `已有${label}，已跳过；要覆盖请选「重新生成」`;
    const progress = mask.querySelector('#ci-progress');
    if (progress) progress.innerHTML = `<div class="dlg-note">${esc(skippedText)}</div>`;
    const panel = openProgress({
      title: `「${item.name}」的${label}已存在`,
      hint: '只补缺失模式不会覆盖已有形象图',
      stage: '已跳过本次生成',
    });
    panel.finish({ message: skippedText });
    toast(skippedText, 'info');
    return;
  }
  const buttons = mask.querySelectorAll('[data-ci="gen"]');
  buttons.forEach((button) => (button.disabled = true));
  const progress = mask.querySelector('#ci-progress');
  if (progress) {
    progress.innerHTML = `<div class="dlg-note">正在生成${esc(label)}…（ComfyUI 出图可能需要几十秒）</div>`;
  }
  const panel = openProgress({
    title: mode === 'regen' ? `正在重新生成「${item.name}」的${label}…` : `正在生成「${item.name}」的${label}…`,
    hint: mode === 'regen' ? '重新生成并覆盖同类型旧图；本机 ComfyUI 出图通常需要几十秒' : '只生成缺失的形象图，不覆盖已有图片',
    stage: '已提交出图任务，等待模型返回…',
  });
  panel.update({ percent: null });
  try {
    const result = await postJSON('/api/ui/characters/image', {
      path: state.path,
      id: item.id,
      kind,
      force: mode === 'regen',
    });
    await loadAll(state.path);
    repaint();
    const fresh = current();
    const body = mask.querySelector('#ci-body');
    if (body && fresh) body.innerHTML = characterImageBodyHTML(fresh);
    panel.finish({
      message: `${label}${result?.created ? '已生成' : '已复用旧图'}`,
      detail: '形象图已写入角色档案，可在弹窗里查看。',
      actions: [{ label: '重新读取', primary: true, onClick: () => {
        const latest = current();
        const host = mask.querySelector('#ci-body');
        if (host && latest) host.innerHTML = characterImageBodyHTML(latest);
      } }],
    });
    toast(`${label}${result?.created ? '已生成' : '已复用旧图'}`, 'ok');
  } catch (error) {
    toast(error.message, 'bad', 4600);
    if (progress) progress.innerHTML = `<div class="dlg-note bad">${esc(error.message)}</div>`;
    panel.fail({ message: `${label}生成失败：${error.message}`, retry: () => void runCharacterImage(mask, kind, modeOf) });
  } finally {
    buttons.forEach((button) => (button.disabled = false));
  }
}

/** AI 补全当前角色的音色 / 外貌描述（可选只补缺失或重新生成并覆盖） */
function openAiFill() {
  const item = current();
  if (!item) return toast('先从左侧选择一个角色', 'warn');
  const mask = openDialog({
    title: 'AI 补全角色档案',
    body: `${modeSwitchHTML('character.desc', { fallback: 'fill' })}
      <div class="dlg-note">「只补缺失」保留已有内容，只生成空缺字段；「重新生成」会重写音色描述、外貌描述与负向提示词并覆盖已有内容。</div>
      <div class="summary-line"><span>目标角色</span><span class="right">${esc(item.name)}</span></div>
      <div id="af-result" style="margin-top:8px"></div>`,
    foot: `<div class="right"><button class="btn" data-dlg-close>取消</button>
      <button class="btn primary" id="af-run">补全缺失字段</button></div>`,
  });
  /** 读取当前选择的生成方式：fill=只补缺失，regen=重新生成并覆盖 */
  const modeOf = bindModeSwitch(mask, 'character.desc', {
    fallback: 'fill',
    action: mask.querySelector('#af-run'),
    labels: { fill: '补全缺失字段', regen: '重新生成并覆盖' },
  });
  mask.querySelector('#af-run').addEventListener('click', (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    button.textContent = '补全中…';
    const resultHost = mask.querySelector('#af-result');
    if (resultHost) resultHost.innerHTML = `<div class="dlg-note">正在调用写作模型补全，请稍候…</div>`;
    void runDescriptionFill(item, mask, resultHost, modeOf()).finally(() => {
      if (!document.body.contains(button)) return;
      button.disabled = false;
      button.textContent = modeOf() === 'regen' ? '重新生成并覆盖' : '补全缺失字段';
    });
  });
}

/**
 * 执行角色描述补全 / 重新生成（流式进度），进度走统一浮层，结果同时回填弹窗。
 * @param {object} item 目标角色
 * @param {HTMLElement} mask 弹窗根节点
 * @param {HTMLElement|null} resultHost 弹窗内结果容器
 * @param {'fill'|'regen'} [mode] 生成方式：fill 只补缺失，regen 重新生成并覆盖
 */
async function runDescriptionFill(item, mask, resultHost, mode = 'fill') {
  const check = await confirmLlm({
    action: 'characters.describe',
    params: { path: state.path, character: item.id || '', regen: mode === 'regen' },
  });
  if (!check.ok) {
    if (check.error) toast(llmErrorText(check), 'bad', 3600);
    return;
  }
  const regen = mode === 'regen';
  const aborter = new AbortController();
  const panel = openProgress({
    title: regen ? `正在重新生成「${item.name}」的档案…` : `正在补全「${item.name}」的档案…`,
    hint: regen
      ? '重新生成音色描述、外貌描述与负向提示词，并覆盖已有内容'
      : '只补缺失的音色描述、外貌描述与负向提示词，已有内容不会被覆盖',
    stage: '正在分析缺失字段…',
    onStop: async () => {
      aborter.abort();
      panel.note('正在停止：当前字段生成结束后中断，已写入的字段会保留');
    },
  });
  panel.update({ percent: null });
  let settled = false;
  try {
    await streamJSON(
      '/api/ui/characters/descriptions/fill/stream',
      { path: state.path, names: [item.name], overwrite: regen },
      (event, data) => {
        if (!data) return;
        if (event === 'total') {
          panel.update({
            stage: `共 ${Number(data.total || 0)} 个角色待处理`,
            current: `待补全 ${Number(data.pending || 0)} · 跳过 ${Number(data.skipped || 0)}`,
          });
        } else if (event === 'batch') {
          const index = Number(data.index || 1);
          const total = Number(data.total || 1) || 1;
          panel.update({
            stage: `${regen ? '正在重新生成' : '正在补全'} ${data.name || item.name}…`,
            percent: Math.round(((index - 1) / total) * 100),
            current: `第 ${index} / ${total} 个`,
          });
        } else if (event === 'chars') {
          panel.update({ current: `${data.name || item.name} · 已接收 ${Number(data.chars || 0)} 字` });
        } else if (event === 'item') {
          const status = String(data.status || '');
          const text =
            status === 'done'
              ? `${data.name}：已补 ${Number(data.fields || 0)} 个字段`
              : status === 'skipped'
                ? `${data.name}：跳过（${data.reason || '无需补全'}）`
                : `${data.name}：失败（${data.message || '未知原因'}）`;
          panel.update({ current: text });
        } else if (event === 'done') {
          settled = true;
          const result = data.result || {};
          const updated = result.updated || [];
          const failures = result.failures || [];
          panel.update({ percent: 100 });
          // 流式返回的模型原文保留在灰色一行里，结束时不覆盖（摘要放到 message/detail）
          panel.finish({
            message: result.overwrite
              ? `已重新生成并覆盖 ${updated.length} 个角色${failures.length ? `，${failures.length} 个失败` : ''}`
              : `补全完成：更新 ${updated.length} 个角色${failures.length ? `，${failures.length} 个失败` : ''}`,
            detail: result.summary || '',
            actions: [
              { label: '重新读取档案', primary: true, onClick: () => void loadAll(state.path).then(repaint) },
            ],
          });
          renderFillResult(resultHost, result);
          void loadAll(state.path).then(repaint);
          toast(updated.length ? '角色档案已补全' : '没有需要补全的字段', 'ok');
        } else if (event === 'error') {
          settled = true;
          panel.fail({ message: data.message || '补全失败', retry: () => void runDescriptionFill(item, mask, resultHost, mode) });
        }
      }, { signal: aborter.signal });
    if (!settled) panel.fail({ message: '连接已中断，没有收到补全结果', retry: () => void runDescriptionFill(item, mask, resultHost, mode) });
  } catch (error) {
    if (aborter.signal.aborted) {
      if (!settled) panel.finish({ message: '已停止补全：已写入的字段会保留' });
      return;
    }
    if (!settled) panel.fail({ message: `补全失败：${error.message}`, retry: () => void runDescriptionFill(item, mask, resultHost, mode) });
    if (resultHost) resultHost.innerHTML = `<div class="dlg-note bad">${esc(error.message)}</div>`;
  }
}

/**
 * 把补全结果写回弹窗内的结果区。
 * @param {HTMLElement|null} host 结果容器
 * @param {object} result 补全结果
 */
function renderFillResult(host, result) {
  if (!host) return;
  host.innerHTML = `
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

/** AI 生成角色名单：预览 → 勾选 → 追加 */
function openAiGenerate() {
  const mask = openDialog({
    title: 'AI 生成角色',
    wide: true,
    body: `${modeSwitchHTML('character.generate', { fallback: 'fill' })}
      <div class="dlg-note">按作品设定与已有角色生成名单；先预览，勾选后才会写入 characters.json。「只补缺失」会跳过与已有角色同名的行。</div>
      <div style="display:grid;grid-template-columns:140px 1fr;gap:10px;margin-top:10px">
        <div class="field"><label>人数</label>
          <input class="input" id="ag-count" type="number" min="1" max="30" value="4" /></div>
        <div class="field"><label>补充要求（可选）</label>
          <input class="input" id="ag-brief" placeholder="例如：再补两个反派，年龄 40 岁以上" /></div>
      </div>
      <div id="ag-result" style="margin-top:10px"></div>`,
    foot: `<div class="right"><button class="btn" data-dlg-close>取消</button>
      <button class="btn" id="ag-preview">生成缺失角色</button>
      <button class="btn primary" id="ag-apply" disabled>追加选中角色</button></div>`,
  });
  /** 读取当前选择的生成方式：fill=过滤同名角色，regen=重新生成完整名单 */
  const modeOf = bindModeSwitch(mask, 'character.generate', {
    fallback: 'fill',
    action: mask.querySelector('#ag-preview'),
    labels: { fill: '生成缺失角色', regen: '重新生成名单' },
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
                labelOf('role', row.role, '角色')
              )}</span>
              <div style="font-size:11.5px;color:var(--text-3);margin-top:2px">${esc(
                row.one_line || row.background || row.personality || ''
              )}</div>
            </span>
          </span>
          <span class="right hint">${esc(
            [labelOf('gender', row.gender, ''), labelOf('age_range', row.age_range, '')].filter(Boolean).join(' · ')
          )}</span>
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
  mask.querySelector('#ag-preview').addEventListener('click', (event) => {
    const button = event.currentTarget;
    const mode = modeOf();
    button.disabled = true;
    button.textContent = '生成中…';
    resultHost.innerHTML = `<div class="dlg-note">正在生成角色名单与档案，请稍候…</div>`;
    const count = Number(mask.querySelector('#ag-count').value || 4);
    const brief = mask.querySelector('#ag-brief').value.trim() || undefined;
    void runCharacterGenerate(count, brief, resultHost, mode)
      .then((list) => {
        rows = list;
        renderRows();
      })
      .finally(() => {
        if (!document.body.contains(button)) return;
        button.disabled = false;
        button.textContent = mode === 'regen' ? '重新生成名单' : '生成缺失角色';
      });
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

/**
 * 执行角色名单生成（SSE 进度），返回可勾选的预览行。
 * @param {number} count 生成人数
 * @param {string|undefined} brief 补充要求
 * @param {HTMLElement|null} resultHost 弹窗内结果容器（失败时显示原因）
 * @param {'fill'|'regen'} [mode] 生成方式：fill 过滤与已有角色同名（含结果内部重名）的行
 * @returns {Promise<Array<object>>} 角色预览行
 */
async function runCharacterGenerate(count, brief, resultHost, mode = 'fill') {
  const check = await confirmLlm({
    action: 'characters.generate',
    params: { path: state.path, count, mode },
  });
  if (!check.ok) {
    if (check.error) toast(llmErrorText(check), 'bad', 3600);
    return [];
  }
  const regen = mode === 'regen';
  const aborter = new AbortController();
  const panel = openProgress({
    title: regen ? `正在重新生成 ${count} 个角色名单…` : `正在生成缺失角色（目标 ${count} 个）…`,
    hint: regen ? brief || '忽略已有角色，重新生成完整名单' : brief || '跳过与已有角色同名的行，只生成缺失角色',
    stage: '已提交生成请求，等待模型返回…',
    onStop: async () => {
      aborter.abort();
      panel.note('正在停止：当前批次结束后中断，已生成的行会保留');
    },
  });
  panel.update({ percent: null });
  let settled = false;
  let rows = [];
  // 已有角色名来自 loadAll 写入的模块级 characters，与左侧列表同源
  const existingNames = new Set(
    (characters || []).map((item) => String(item.name || '').trim()).filter(Boolean)
  );
  try {
    await streamJSON('/api/ui/characters/generate/stream', { path: state.path, count, brief }, (event, data) => {
      if (!data) return;
      if (event === 'batch') {
        const index = Number(data.index || 1);
        const total = Number(data.total || 1) || 1;
        panel.update({
          stage: `正在生成第 ${index}/${total} 批角色…`,
          percent: index > 1 ? Math.round(((index - 1) / total) * 100) : null,
          current: `第 ${index} / ${total} 批`,
        });
      } else if (event === 'chars') {
        const batch = Number(data.batch || data.index || 1);
        const total = Number(data.total || 1) || 1;
        panel.update({
          percent: null,
          current: `第 ${batch}/${total} 批 · 已接收 ${Number(data.chars || 0)} 字`,
        });
      } else if (event === 'done') {
        settled = true;
        const raw = (data.result?.characters || []).map((row) => ({ ...row }));
        let skipped = 0;
        if (regen) {
          rows = raw;
        } else {
          // fill：先按已有角色名去重，再按结果内部重名去重
          const seen = new Set();
          rows = raw.filter((row) => {
            const name = String(row.name || '').trim();
            if (!name) return true;
            if (existingNames.has(name) || seen.has(name)) {
              skipped += 1;
              return false;
            }
            seen.add(name);
            return true;
          });
        }
        panel.update({ percent: 100, current: skipped ? `已跳过 ${skipped} 个同名角色` : '' });
        // 非流式结果摘要：让进度浮层显示最近一条模型返回内容
        panel.note(rows[0] ? `${rows[0].name || ''} / ${rows[0].one_line || ''}` : `已返回 ${raw.length} 个角色`);
        panel.finish({
          message: regen
            ? `角色名单已重新生成：${rows.length} 个待勾选`
            : `缺失角色生成完成：${rows.length} 个待勾选${skipped ? ` · 已跳过 ${skipped} 个同名角色` : ''}`,
          detail: '在弹窗里勾选后点「追加选中角色」才会写入 characters.json。',
        });
        toast(`已生成 ${rows.length} 个角色预览`, 'ok');
      } else if (event === 'canceled') {
        settled = true;
        panel.fail({ message: '已停止生成', detail: '任务已在任务队列中取消，已接收的内容不会写入角色表。' });
      } else if (event === 'error') {
        settled = true;
        panel.fail({
          message: data.message || '角色生成失败',
          retry: () => void runCharacterGenerate(count, brief, resultHost, mode),
        });
      }
    }, { signal: aborter.signal });
    if (!settled) {
      panel.fail({
        message: '连接已中断，没有收到角色生成结果',
        retry: () => void runCharacterGenerate(count, brief, resultHost, mode),
      });
    }
  } catch (error) {
    if (aborter.signal.aborted) {
      if (!settled) panel.finish({ message: '已停止生成：已生成的角色行会保留，可重新勾选' });
      return rows;
    }
    if (!settled) {
      panel.fail({
        message: `角色生成失败：${error.message}`,
        retry: () => void runCharacterGenerate(count, brief, resultHost, mode),
      });
    }
    if (resultHost) resultHost.innerHTML = `<div class="dlg-note bad">${esc(error.message)}</div>`;
    throw error;
  }
  return rows;
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
            labelOf('cast_action', item.action, '待处理')
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
    const panel = openProgress({
      title: '正在应用音色编排…',
      hint: `新建 ${Number(stats.created || 0)} · 补齐 ${Number(stats.filled || stats.changed || 0)}`,
      stage: '正在写入 cast 并刷新角色绑定…',
    });
    panel.update({ percent: null });
    try {
      const result = await postJSON('/api/ui/cast/autofill', { path: state.path });
      const message = result.applied
        ? `已应用音色编排，改动 ${Number(result.changed || 0)} 项`
        : '当前已是最优编排，没有需要改动的地方';
      panel.finish({ message, detail: '可在「音色」视图逐个角色查看绑定结果。' });
      toast(message, result.applied ? 'ok' : 'info');
      closeDialog();
      await loadAll(state.path);
      repaint();
      check = await getJSON('/api/ui/cast/check', { path: state.path }).catch(() => check);
      repaint();
    } catch (error) {
      toast(error.message, 'bad', 4600);
      event.currentTarget.disabled = false;
      panel.fail({ message: `应用音色编排失败：${error.message}` });
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
  if (act === 'enum-fix') {
    const issue = enumIssues.find(
      (row) => row.character_id === button.dataset.char && row.path === button.dataset.path
    );
    if (!issue) return toast('这条枚举告警已不存在，请刷新后再试', 'warn');
    return confirmEnumFixes([issue]);
  }
  if (act === 'enum-fix-all') return confirmEnumFixes(enumIssues.filter((row) => row.suggestion?.value));
  if (act === 'new-character') return newCharacter();
  if (act === 'ai-generate') return openAiGenerate();
  if (act === 'ai-fill') return openAiFill();
  if (act === 'autofill') return void openAutofill();
  if (act === 'char-image') return openCharacterImage(button.dataset.kind || '');
  if (act === 'delete-character') return deleteCharacter();
  if (act === 'merge-character') return mergeCharacter();
  if (act === 'new-this-chapter') return openCreatedThisChapter();
  if (act === 'pending-promote') return void promotePending(button.dataset.key, button);
  if (act === 'pending-discard') return discardPending(button.dataset.key);
  if (act === 'view-appearances') return viewAppearances();
  if (act === 'relation-add') return addRelationRow();
  if (act === 'relation-del') return deleteRelationRow(Number(el.dataset.index));
  if (act === 'relation-graph') return openRelationGraph();
  if (act === 'view-arcs') return openCharacterArcs();
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
export async function renderCharacters(root, { path, character = '' }) {
  stopAudio();
  if (state.path !== path) resetProject(path);
  // 从阻断项直达：地址里带角色 ID 时优先选中该角色，不存在时由 loadAll 回落
  if (character) selectedId = character;
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
