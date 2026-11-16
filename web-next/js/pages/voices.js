/**
 * 音色视图（V4）：音色池、内置音色、试听、绑定到角色、候选对比。
 * 只做参考产品有的音色分配，不出现自动编排 / 音色生成。
 */
import { esc, icon, toast, openDialog, closeDialog, fmtDuration } from '../dom.js';
import { getJSON, postJSON } from '../api.js';
import { labelOf } from '../enums.js';
import { runMedia } from '../media-bus.js';
import { runLlm } from '../llm.js';
import { modeSwitchHTML, bindModeSwitch } from '../gen-mode.js';
import { state, resetProject } from '../store.js';
import { railbar, topbar, goChapter, goProjects, emptyProjectHTML, checkConsistency, resplitBannerHTML, onResplit } from '../shell.js';

let characters = [];
let voices = [];
let builtin = { items: [] };
let selectedCharacter = '';
let selectedVoices = new Set();
let keyword = '';
/** 音色池性别筛选：all / male / female / unknown */
let genderFilter = 'all';
let bound = false;
let audio = null;
/** 当前章节正文与切分是否一致 */
let consistency = null;
/** 最近一次批量生成音色任务（GET /api/ui/voices/design/batch/latest） */
let latestBatchJob = null;

/** 批量音色任务状态中文名 */
const VOICE_BATCH_STATUS = {
  running: '生成中',
  ready: '已完成',
  partial: '部分完成',
  failed: '失败',
  stopped: '已停止',
};

/**
 * 「最近一次批量任务」摘要：只展示后端已有字段，不编造进度。
 * @returns {string} HTML 片段
 */
function batchJobHTML() {
  const job = latestBatchJob;
  if (!job) return '<div class="hint">还没有批量生成记录。</div>';
  const items = job.items || [];
  const countOf = (status) => items.filter((item) => item.status === status).length;
  const status = String(job.status || '');
  const badge = status === 'ready' ? 'ok' : status === 'failed' ? 'bad' : status === 'running' ? 'run' : 'mute';
  return `
    <div class="summary-line">
      <span style="min-width:0">最近一次批量任务
        <span class="badge ${badge}" style="margin-left:6px">${esc(VOICE_BATCH_STATUS[status] || '状态未知')}</span>
        <span style="display:block;color:var(--text-4);font-size:11px;margin-top:2px">
          ${esc(job.created_at || '')} · 范围 ${job.scope === 'chapter' ? '指定章节' : '未绑定角色'} ·
          成功 ${countOf('ready')} · 失败 ${countOf('failed')} · 跳过 ${countOf('skipped')} · 待生成 ${countOf('pending')}
        </span>
      </span>
      <span class="right hint" style="max-width:46%;text-align:right">${esc(job.note || '')}</span>
    </div>`;
}

/** 一键批量生成音色卡：范围 / 章节 / 候选数 + 最近一次任务摘要 */
function batchCardHTML() {
  const chapters = state.chapters
    .map(
      (item) =>
        `<option value="${esc(item.id)}" ${item.id === state.chapterId ? 'selected' : ''}>${
          item.index !== undefined ? `${Number(item.index) + 1}. ` : ''
        }${esc(item.title || item.id)}</option>`
    )
    .join('');
  return `
    <div class="card" style="margin-top:14px">
      <div class="card-head"><span class="ct">一键批量生成音色</span>
        <div class="right"><span class="hint">已有音色、缺性别或缺描述的角色会自动跳过</span></div>
      </div>
      <div class="card-body">
        <div style="display:grid;grid-template-columns:2fr 1fr 120px;gap:10px;align-items:end">
          <div class="field"><label>生成范围</label>
            <select class="select" id="vb-scope" title="按未绑定角色或指定章节出现的角色批量抽卡">
              <option value="unbound">全部未绑定音色的角色</option>
              <option value="chapter">指定章节出现的角色</option>
            </select></div>
          <div class="field"><label>章节（章节范围用）</label>
            <select class="select" id="vb-chapter" title="选择要批量生成音色的章节">
              <option value="">${chapters ? '按范围自动选择' : '工程还没有章节'}</option>${chapters}
            </select></div>
          <div class="field"><label>每个角色候选数</label>
            <input class="input" id="vb-count" type="number" min="1" max="6" value="4"
              title="每个角色抽几个候选，生成后自动采用第 1 条" /></div>
        </div>
        <div style="display:flex;gap:10px;align-items:center;margin-top:8px;flex-wrap:wrap">
          <button class="btn primary" data-act="voice-batch"
            title="逐角色生成音色候选，并自动采用第 1 条；可随时取消">批量生成音色</button>
          <span class="hint">生成后可在角色页继续调整音色；整批可撤销。</span>
        </div>
        <div style="margin-top:10px">${batchJobHTML()}</div>
      </div>
    </div>`;
}

/** 当前选中角色 */
function currentCharacter() {
  return characters.find((item) => item.id === selectedCharacter) || null;
}

/** 音色默认变体 */
function defaultVariant(voice) {
  const id = voice.default_variant_id;
  return (voice.variants || []).find((item) => item.id === id) || (voice.variants || [])[0] || null;
}

/** 音色试听地址 */
function voiceAudioUrl(voice) {
  const variant = defaultVariant(voice);
  const query = new URLSearchParams({ path: state.path, voice_id: voice.id });
  if (variant?.id) query.set('variant_id', variant.id);
  return `/api/ui/voices/audio?${query.toString()}`;
}

/** 停止上一次试听 */
function stopAudio() {
  if (audio) {
    audio.pause();
    audio = null;
  }
}

/** 试听一段音频 */
function playUrl(url, label) {
  stopAudio();
  audio = new Audio(url);
  audio.play().catch((error) => toast(`试听失败：${error.message}`, 'bad'));
  if (label) toast(`正在试听：${label}`, 'info', 1500);
}

/** 角色列表（左侧上下文面板） */
function ctxHTML() {
  const word = keyword.trim().toLowerCase();
  const rows = characters
    .filter((item) => !word || String(item.name || '').toLowerCase().includes(word))
    .map((item) => {
      const voice = voices.find((entry) => entry.id === item.voice_id);
      return `
      <div class="chap-row ${item.id === selectedCharacter ? 'on' : ''}" data-chara="${esc(item.id)}"
           title="${esc(item.name)} · ${esc(voice?.name || '未绑定音色')}">
        <span class="cn">${esc(item.name)}</span>
        <span class="dot ${voice ? 'ok' : 'warn'}"></span>
        <span class="cw">${esc(voice?.name || '未绑定')}</span>
      </div>`;
    })
    .join('');
  const emptyList =
    characters.length === 0
      ? `<div class="empty"><b>还没有角色</b><span>先在角色视图新增角色，再回来分配音色</span></div>`
      : `<div class="empty"><b>没有匹配的角色</b><span>换个关键词</span></div>`;
  return `
    <section class="ctx" aria-label="角色与音色">
      <div class="ctx-head">
        <span class="ct">角色</span>
        <span class="badge mute">${characters.length} 人</span>
      </div>
      <div class="ctx-tools">
        <div class="toolrow">
          <div class="search">${icon('search', 14)}
            <input type="search" id="voice-search" placeholder="搜索角色" value="${esc(keyword)}"
              title="按角色名过滤" />
          </div>
        </div>
      </div>
      <div class="chap-list">${rows || emptyList}</div>
    </section>`;
}

/** 按性别筛选音色池（未知/其他合并为「中性」一组） */
function visibleVoices() {
  if (genderFilter === 'all') return voices;
  if (genderFilter === 'unknown') return voices.filter((voice) => !['male', 'female'].includes(voice.gender));
  return voices.filter((voice) => (voice.gender || 'unknown') === genderFilter);
}

/** 某个音色当前被几个角色使用 */
function usedByCount(voice) {
  return characters.filter((item) => item.voice_id === voice.id).length;
}

/** 音色卡 */
function voiceCard(voice) {
  const variant = defaultVariant(voice);
  const character = currentCharacter();
  const bound = Boolean(character?.voice_id === voice.id);
  const checked = selectedVoices.has(voice.id);
  const used = usedByCount(voice);
  const bindTitle = !character
    ? '先在左侧选择一个角色'
    : bound
      ? `${character.name} 已经使用这个音色`
      : `把「${voice.name || '这个音色'}」绑定给 ${character.name}`;
  return `
    <div class="card" data-voice="${esc(voice.id)}" style="padding:12px;display:flex;flex-direction:column;gap:8px">
      <div style="display:flex;align-items:flex-start;gap:8px">
        <div style="min-width:0">
          <div style="font-size:13px;color:var(--text);font-weight:600">${esc(voice.name || '未命名音色')}</div>
          <div style="font-size:11px;color:var(--text-4)">
            ${esc(labelOf('voice_gender', voice.gender, '未指定'))} ·
            ${variant?.reference?.duration_ms ? fmtDuration(variant.reference.duration_ms) : '无参考音'} ·
            已用于 ${used} 个角色
          </div>
        </div>
        <div class="right" style="margin-left:auto;display:flex;gap:4px">
          <input type="checkbox" data-compare="${esc(voice.id)}" ${checked ? 'checked' : ''}
                 title="加入候选对比（最多 3 个）" />
        </div>
      </div>
      <div style="display:flex;gap:4px;flex-wrap:wrap">
        ${(voice.tags || []).slice(0, 3).map((tag) => `<span class="stag">${esc(tag)}</span>`).join('')}
      </div>
      <div style="display:flex;gap:6px;margin-top:2px">
        <button class="btn sm" data-act="play-voice" ${variant ? '' : 'disabled'}
          title="${variant ? `试听「${esc(voice.name || '')}」的参考音` : '这个音色没有参考音频，无法试听'}">${icon('play', 11)} 试听</button>
        <button class="btn sm" data-act="bind-voice" ${bound || !character ? 'disabled' : ''}
          title="${esc(bindTitle)}">
          ${bound ? '已绑定' : '绑定给角色'}</button>
        <button class="btn sm danger" data-act="delete-voice"
          title="从音色池删除；正在使用它的角色会自动解绑">删除</button>
      </div>
    </div>`;
}

/** 内置音色行 */
function builtinRows() {
  const items = builtin?.items || [];
  if (!items.length) return `<div class="hint">没有可用的内置音色。</div>`;
  return items
    .map(
      (item) => `
      <div class="summary-line" data-builtin="${esc(item.key)}">
        <span>${esc(item.name)} <span style="color:var(--text-4);font-size:11px">${
          labelOf('voice_gender', item.gender, '未指定')
        } · ${item.duration_ms ? fmtDuration(item.duration_ms) : ''}</span></span>
        <span class="right" style="display:flex;gap:6px;align-items:center">
          <span class="badge ${item.present ? 'ok' : 'mute'}"
            title="${item.present ? '参考音已在本机' : '尚未下载到本机'}">${item.present ? '已下载' : '未下载'}</span>
          <button class="btn sm" data-act="play-builtin" ${item.present ? '' : 'disabled'}
            title="${item.present ? `试听「${esc(item.name)}」` : '先下载内置音色才能试听'}">试听</button>
          <button class="btn sm ${item.present ? 'primary' : ''}" data-act="import-builtin" ${item.present ? '' : 'disabled'}
            title="${item.present ? '导入后可在音色池绑定给角色' : '先下载内置音色才能导入'}">导入</button>
        </span>
      </div>`
    )
    .join('');
}

/** 主区 */
function mainHTML() {
  const character = currentCharacter();
  const boundVoice = voices.find((item) => item.id === character?.voice_id);
  const boundCount = characters.filter((item) => item.voice_id).length;
  const byGender = (key) =>
    voices.filter((voice) => (key === 'unknown' ? !['male', 'female'].includes(voice.gender) : (voice.gender || 'unknown') === key))
      .length;
  const genderCounts = { all: voices.length, male: byGender('male'), female: byGender('female'), unknown: byGender('unknown') };
  const pool = visibleVoices();
  return `
    ${resplitBannerHTML(consistency)}
    <div class="page-head">
      <div class="ph">
        <div class="eyebrow"><span class="ln"></span>音色</div>
        <h1 class="page-title">${esc(character?.name || '选择角色')}</h1>
        <div class="page-sub">${
          character ? `当前音色：${esc(boundVoice?.name || '未绑定')}` : '从左侧选择角色后，为它分配音色'
        } · 已绑定 ${boundCount}/${characters.length}</div>
      </div>
      <div class="ph-actions">
        ${
          character && boundVoice
            ? `<button class="btn" data-act="unbind-voice"
                 title="解除 ${esc(character.name)} 的音色绑定">解除绑定</button>`
            : ''
        }
        <button class="btn" data-act="design" ${character ? '' : 'disabled'}
          title="${character ? '按角色档案生成音色描述，抽取 1–6 个候选并试听采用' : '先在左侧选择角色'}">
          ${icon('spark')} 音色设计
        </button>
        <button class="btn" data-act="compare" ${selectedVoices.size < 2 ? 'disabled' : ''}
          title="${selectedVoices.size < 2 ? '至少勾选 2 个音色才能并排试听' : `并排试听已勾选的 ${selectedVoices.size} 个音色`}">
          ${icon('audio')} 候选对比（${selectedVoices.size}）
        </button>
      </div>
    </div>
    <div style="padding:0 18px 18px;overflow:auto;flex:1">
      <div class="card" style="margin-bottom:14px">
        <div class="card-head"><span class="ct">音色池</span>
          <div class="right" style="display:flex;gap:4px;align-items:center">
            ${['all', 'male', 'female', 'unknown']
              .map(
                (key) => `<button class="chip sm ${genderFilter === key ? 'on' : ''}" data-gender="${key}"
                  title="按性别筛选音色池">${key === 'all' ? '全部' : esc(labelOf('voice_gender', key, '其他'))}<span class="n">${genderCounts[key]}</span></button>`
              )
              .join('')}
          </div>
        </div>
        <div class="card-body">
          ${
            voices.length
              ? pool.length
                ? `<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:10px">${pool
                    .map(voiceCard)
                    .join('')}</div>`
                : `<div class="empty"><b>这个筛选下没有音色</b><span>切回「全部」或导入内置音色</span></div>`
              : `<div class="empty"><b>音色池还是空的</b><span>从下方「内置音色」下载并导入，或在角色页绑定已有音色</span></div>`
          }
        </div>
      </div>
      <div class="card">
        <div class="card-head"><span class="ct">内置音色</span>
          <div class="right"><span class="hint">下载后可导入工程音色池</span></div>
        </div>
        <div class="card-body" style="display:flex;flex-direction:column;gap:6px">${builtinRows()}</div>
      </div>
      ${batchCardHTML()}
    </div>`;
}

/** 重画 */
function repaint({ ctxOnly = false } = {}) {
  const studio = document.getElementById('studio');
  if (!studio) return;
  const old = studio.querySelector('.ctx');
  if (old) old.outerHTML = ctxHTML();
  if (!ctxOnly) {
    const main = document.getElementById('voice-main');
    // 音色池本身不依赖章节；无章节时在顶部补一条起步引导，配置照常可用
    if (main) {
      main.innerHTML = `${state.chapters.length ? '' : emptyProjectHTML(state.path, { compact: true })}${mainHTML()}`;
    }
  }
}

/** 读取数据 */
async function loadAll(path) {
  const [info, chapters, payload, batchLatest] = await Promise.all([
    getJSON('/api/project/info', { path }),
    getJSON('/api/ui/chapters', { path }),
    getJSON('/api/ui/voices', { path }),
    getJSON('/api/ui/voices/design/batch/latest', { path }).catch(() => null),
  ]);
  state.info = info;
  state.chapters = chapters || [];
  if (!state.chapters.some((item) => item.id === state.chapterId)) {
    state.chapterId = state.chapters[0]?.id || '';
  }
  characters = payload?.characters || [];
  voices = payload?.voices || [];
  builtin = payload?.builtin || { items: [] };
  latestBatchJob = batchLatest?.job || null;
  if (!characters.some((item) => item.id === selectedCharacter)) {
    selectedCharacter = characters[0]?.id || '';
  }
  consistency = await checkConsistency(path, state.chapterId);
}

/** 绑定音色到指定角色 */
async function bindVoice(voiceId, characterId) {
  try {
    await postJSON('/api/ui/voices/bind', {
      path: state.path,
      character_id: characterId,
      voice_id: voiceId,
    });
    await loadAll(state.path);
    repaint();
    return true;
  } catch (error) {
    toast(error.message, 'bad');
    return false;
  }
}

/** 候选对比：并排试听 2-3 个音色 */
function openCompare() {
  const picks = voices.filter((voice) => selectedVoices.has(voice.id)).slice(0, 3);
  if (picks.length < 2) return toast('至少勾选 2 个音色', 'warn');
  const body = picks
    .map(
      (voice, index) => `
      <div class="summary-line" data-compare-row="${esc(voice.id)}">
        <span>候选 ${index + 1}：${esc(voice.name)} <span style="color:var(--text-4);font-size:11px">${
          labelOf('voice_gender', voice.gender, '未指定')
        }</span></span>
        <span class="right"><button class="btn sm" data-act="play-candidate" data-candidate="${esc(voice.id)}">试听</button></span>
      </div>`
    )
    .join('');
  const mask = openDialog({
    title: '候选音色对比',
    body,
    foot: `<div class="right"><button class="btn" data-dlg-close>关闭</button></div>`,
  });
  mask.querySelectorAll('[data-candidate]').forEach((button) => {
    button.addEventListener('click', () => {
      const voice = voices.find((item) => item.id === button.dataset.candidate);
      if (voice) playUrl(voiceAudioUrl(voice), voice.name);
    });
  });
}

/**
 * 打开音色设计：按角色档案生成音色描述 → 抽卡候选 → 试听 → 采用并绑定。
 * 抽卡本身交给媒体总线（`voice.design`），页面不再轮询批次状态。
 */
function openVoiceDesign() {
  const character = currentCharacter();
  if (!character) return toast('先从左侧选择一个角色', 'warn');
  let profile = null;
  let batchId = '';
  /** 上次翻译时的中文原文；用于判断中文是否被改过 */
  let lastTranslatedZh = '';
  /** 用户是否手动改过英文描述；改过就不再自动覆盖 */
  let enEdited = false;
  /** 历史批次缓存：查看 / 复用都从这里取，不再单独读单个批次 */
  let historyRows = [];
  const mask = openDialog({
    title: `音色设计 · ${character.name}`,
    wide: true,
    body: `
      ${modeSwitchHTML('voice.desc', { fallback: 'regen' })}
      ${modeSwitchHTML('voice.draw', { fallback: 'regen' })}
      <div class="dlg-note">按角色档案生成中英双语音色描述；中文可手动改写，抽卡前会用模型自动翻译成英文（VoxCPM2 只使用英文描述）。</div>
      <div class="field" style="margin-top:8px"><label>中文音色描述（可手动改写）</label>
        <textarea class="textarea" id="vd-prompt-zh" rows="3"
          placeholder="例如：中年男性，低沉沙哑，语速偏慢，带一点疲惫感"></textarea></div>
      <div class="field" style="margin-top:8px"><label>英文音色描述（VoxCPM2 抽卡用，可手动改写）</label>
        <textarea class="textarea" id="vd-prompt-en" rows="3"
          placeholder="例如：A middle-aged male voice, low and slightly hoarse, slow pace, weary but restrained"></textarea></div>
      <div style="display:flex;gap:10px;align-items:center;margin-top:8px;flex-wrap:wrap">
        <button class="btn" id="vd-preview">重新生成描述</button>
        <button class="btn" id="vd-translate">中文翻译成英文</button>
        <label style="font-size:11.5px;color:var(--text-3)">候选数
          <input class="input" id="vd-count" type="number" min="1" max="6" value="4"
            style="width:64px;height:26px;display:inline-block" /></label>
        <button class="btn primary" id="vd-start">重新抽卡</button>
        <span class="hint" id="vd-hint"></span>
      </div>
      <div id="vd-profile" style="margin-top:10px"></div>
      <div id="vd-result" style="margin-top:10px"></div>
      <div id="vd-history" style="margin-top:10px"></div>`,
    foot: `<div class="right">
      <button class="btn" data-vd="close">关闭</button>
      <button class="btn" data-vd="history" title="列出该角色历史抽卡批次，可恢复查看或清理">历史批次</button>
    </div>`,
  });

  /** 读取描述生成方式：fill=已有描述时保留，regen=按档案重写 */
  const descModeOf = bindModeSwitch(mask, 'voice.desc', {
    fallback: 'regen',
    action: mask.querySelector('#vd-preview'),
    labels: { fill: '补缺失', regen: '重新生成描述' },
  });
  /** 读取抽卡方式：fill=复用已有就绪批次，regen=重新抽卡 */
  const drawModeOf = bindModeSwitch(mask, 'voice.draw', {
    fallback: 'regen',
    action: mask.querySelector('#vd-start'),
    labels: { fill: '只补缺失候选', regen: '重新抽卡' },
  });

  const promptZhNode = mask.querySelector('#vd-prompt-zh');
  const promptEnNode = mask.querySelector('#vd-prompt-en');
  const hintNode = mask.querySelector('#vd-hint');
  const profileHost = mask.querySelector('#vd-profile');
  const resultHost = mask.querySelector('#vd-result');
  const historyHost = mask.querySelector('#vd-history');
  promptEnNode.addEventListener('input', () => {
    enEdited = true;
  });
  /** 调模型把中文描述翻成英文；失败或取消时返回空串 */
  const translateToEnglish = async (zh) => {
    let data = null;
    try {
      data = await runLlm({
        action: 'voice.design.translate',
        confirm: false,
        params: { path: state.path, text: zh },
        title: `正在翻译「${character.name}」的音色描述…`,
        hint: '中文 → 英文，供 VoxCPM2 抽卡使用 · 可随时取消',
        stage: '正在调用写作模型翻译…',
        request: ({ callId, signal }) =>
          postJSON(
            '/api/ui/voices/design/translate',
            { path: state.path, text: zh },
            { timeout: 0, callId, signal }
          ),
        finish: () => ({ message: '音色描述已翻译成英文', detail: '已写回英文文本框，可直接抽卡或手动改写。' }),
      });
    } catch (error) {
      toast(`翻译失败：${error.message}`, 'bad', 5200);
      return '';
    }
    if (!data) return ''; // 用户取消或确认弹窗被关闭
    const english = String(data.english || '').trim();
    if (!english) {
      toast('翻译结果为空，请手动填写英文描述', 'warn', 4200);
      return '';
    }
    return english;
  };

  /** 候选音频地址 */
  const audioUrl = (index) => {
    const query = new URLSearchParams({
      path: state.path,
      batch_id: batchId,
      index: String(index),
    });
    return `/api/ui/voices/design/audio?${query.toString()}`;
  };

  /**
   * 渲染批次候选清单：进度卡由媒体总线负责，这里只画结果。
   * @param {object} meta 批次元数据（来自历史批次或批次控制接口）
   */
  const renderBatch = (meta) => {
    if (!meta) return;
    batchId = meta.batch_id || batchId;
    const candidates = meta.candidates || [];
    const failures = meta.failures || [];
    const total = Number(meta.total || 0);
    const done = Number(meta.done || candidates.length);
    const running = ['running', 'queued'].includes(meta.status);
    if (!document.body.contains(mask)) return;
    if (hintNode) {
      hintNode.textContent = `${labelOf('voice_design_status', meta.status, '生成中')} ${done}/${total}${
        meta.current_index ? ` · 正在生成第 ${meta.current_index} 个` : ''
      }`;
    }
    resultHost.innerHTML = `
      <div class="summary-line"><span>批次 ${esc(batchId)}</span>
        <span class="right badge ${meta.status === 'ready' ? 'ok' : meta.status === 'failed' ? 'bad' : 'warn'}">
          ${esc(labelOf('voice_design_status', meta.status, '生成中'))}</span></div>
      <div class="bar" style="margin:6px 0 10px"><span style="width:${
        total ? Math.round((done / total) * 100) : 0
      }%"></span></div>
      ${
        candidates
          .map(
            (item) => `
        <div class="summary-line">
          <span>候选 ${Number(item.index)}${item.duration_ms ? ` · ${fmtDuration(item.duration_ms)}` : ''}</span>
          <span class="right" style="display:flex;gap:6px">
            <button class="btn sm" data-vd="play" data-index="${Number(item.index)}"
              title="试听这个候选">${icon('play', 11)} 试听</button>
            <button class="btn sm primary" data-vd="select" data-index="${Number(item.index)}"
              title="采用为正式音色并绑定给 ${esc(character.name)}">采用并绑定</button>
          </span>
        </div>`
          )
          .join('') || '<div class="hint">还没有生成出候选</div>'
      }
      ${
        failures.length
          ? `<div class="dlg-note warn" style="margin-top:8px">失败 ${failures.length} 个：${failures
              .map(
                (item) => `${Number(item.index)}（${esc(item.message || '失败')}）
                  <button class="btn sm" data-vd="retry" data-index="${Number(item.index)}">重试</button>`
              )
              .join('；')}</div>`
          : ''
      }
      ${
        running
          ? `<div style="margin-top:8px;display:flex;gap:6px">
               <button class="btn sm" data-vd="refresh" title="重新读取这个批次的最新候选">刷新候选</button>
               <button class="btn sm danger" data-vd="cancel"
                 title="停止后续候选，已完成的候选保留">停止抽卡</button>
             </div>`
          : ''
      }`;
  };

  /** 渲染历史批次列表：数据来自 historyRows 缓存 */
  const paintHistory = () => {
    if (!document.body.contains(mask)) return;
    historyHost.innerHTML = `
      <div class="card"><div class="card-head"><span class="ct">历史批次</span>
        <span class="hint">${historyRows.length} 个</span></div>
        <div class="card-body" style="padding:0">
          ${
            historyRows
              .map(
                (batch) => `
            <div class="summary-line">
              <span style="min-width:0">${esc(batch.created_at || '')}
                <span class="badge ${
                  batch.status === 'ready' ? 'ok' : batch.status === 'failed' ? 'bad' : 'mute'
                }" style="margin-left:6px">${esc(labelOf('voice_design_status', batch.status, '生成中'))}</span>
                <span style="font-size:11px;color:var(--text-4);display:block;margin-top:2px">
                  ${Number(batch.done || 0)}/${Number(batch.total || 0)} 候选${
                    batch.adopted ? ' · 已采用' : ''
                  }</span>
              </span>
              <span class="right" style="display:flex;gap:6px">
                <button class="btn sm" data-vd="open" data-batch="${esc(batch.batch_id)}">查看</button>
                <button class="btn sm danger" data-vd="delete" data-batch="${esc(batch.batch_id)}"
                  title="清理这个批次的候选文件">清理</button>
              </span>
            </div>`
              )
              .join('') || '<div class="hint" style="padding:10px">还没有历史批次</div>'
          }
        </div></div>`;
  };

  /**
   * 读取该角色的历史批次并选中最新批次。
   * 抽卡由媒体总线执行，完成后由这里刷新，不再自己轮询批次进度。
   */
  const refreshHistory = async () => {
    try {
      const data = await getJSON('/api/ui/voices/design/history', {
        path: state.path,
        character_id: character.id,
      });
      historyRows = data.batches || [];
    } catch (error) {
      toast(`读取抽卡历史失败：${error.message}`, 'bad', 4600);
      return;
    }
    paintHistory();
    if (historyRows[0]) renderBatch(historyRows[0]);
  };

  /**
   * 只补缺失：从历史批次里找候选数量足够的就绪批次，避免重复抽卡。
   * @param {number} wanted 需要的候选数
   * @returns {Promise<object|null>} 可复用的批次；没有时返回 null
   */
  const findReuseBatch = async (wanted) => {
    try {
      const data = await getJSON('/api/ui/voices/design/history', {
        path: state.path,
        character_id: character.id,
      });
      historyRows = data.batches || [];
    } catch {
      // 历史读取失败时按「没有可复用批次」处理，继续走新抽卡
      return null;
    }
    const availableCount = (batch) =>
      (batch.candidates || []).filter((item) => item.available !== false).length;
    return historyRows.find((batch) => batch.status === 'ready' && availableCount(batch) >= wanted) || null;
  };

  /** 预览音色描述：fill 保留已有描述，regen 忽略文本框旧内容按档案重写 */
  mask.querySelector('#vd-preview').addEventListener('click', async (event) => {
    const button = event.currentTarget;
    const mode = descModeOf();
    if (mode === 'fill' && promptZhNode.value.trim() && promptEnNode.value.trim()) {
      return toast('已有中英文音色描述，已保留；要覆盖请选重新生成', 'info', 3200);
    }
    const idleLabel = mode === 'fill' ? '补缺失' : '重新生成描述';
    button.disabled = true;
    button.textContent = '生成中…';
    try {
      const result = await runLlm({
        action: 'voice.design.preview',
        params: { path: state.path, character: character.id || '' },
        title:
          mode === 'fill'
            ? `正在补全「${character.name}」的音色描述…`
            : `正在重新生成「${character.name}」的音色描述…`,
        hint:
          mode === 'fill'
            ? '只补缺失：文本框为空时才调用模型生成描述 · 可随时取消'
            : '忽略文本框旧内容，按角色档案重新生成描述 · 可随时取消',
        stage: '正在调用写作模型…',
        request: ({ callId, signal }) =>
          postJSON(
            '/api/ui/voices/design/preview',
            {
              path: state.path,
              character_id: character.id,
            },
            { timeout: 0, callId, signal }
          ),
        finish: (data) => {
          profile = data.profile || null;
          const zh = String(data.prompt_zh || data.profile?.design_prompt_zh || '').trim();
          const en = String(data.prompt || '').trim();
          if (zh) promptZhNode.value = zh;
          if (en) promptEnNode.value = en;
          lastTranslatedZh = promptZhNode.value.trim();
          enEdited = false;
          if (data.candidates) mask.querySelector('#vd-count').value = String(data.candidates);
          profileHost.innerHTML = `
            <div class="summary-line"><span>描述来源</span>
              <span class="right">${data.source === 'manual' ? '手动改写' : '按角色档案生成'}</span></div>
            ${
              data.note
                ? `<div class="dlg-note warn" style="margin-top:6px">${esc(data.note)}</div>`
                : ''
            }
            <div class="hint" style="margin-top:6px">中文用于阅读确认，英文用于 VoxCPM2 抽卡；改中文后点「中文翻译成英文」或直接抽卡会自动翻译。</div>
            ${
              data.script
                ? `<div class="hint" style="margin-top:6px">试听台词：${esc(data.script)}</div>`
                : ''
            }`;
          toast('中英双语音色描述已生成', 'ok');
          return {
            message: mode === 'fill' ? '音色描述已补全' : '音色描述已重新生成',
            detail: '中英文已分别写回两个文本框，可直接抽卡或手动改写。',
          };
        },
      });
      if (!result) return;
    } catch (error) {
      profileHost.innerHTML = `<div class="dlg-note bad">${esc(error.message)}</div>`;
    } finally {
      button.disabled = false;
      button.textContent = idleLabel;
    }
  });

  /** 手动把中文描述翻译成英文，写回英文文本框 */
  mask.querySelector('#vd-translate').addEventListener('click', async (event) => {
    const button = event.currentTarget;
    const zh = promptZhNode.value.trim();
    if (!zh) return toast('先填写中文音色描述', 'warn');
    button.disabled = true;
    button.textContent = '翻译中…';
    try {
      const english = await translateToEnglish(zh);
      if (!english) return;
      promptEnNode.value = english;
      lastTranslatedZh = zh;
      enEdited = false;
      toast('已翻译成英文', 'ok');
    } finally {
      button.disabled = false;
      button.textContent = '中文翻译成英文';
    }
  });

  /** 开始抽卡：fill 复用已有就绪批次，regen 按新描述重新抽卡 */
  mask.querySelector('#vd-start').addEventListener('click', async (event) => {
    const mode = drawModeOf();
    const zh = promptZhNode.value.trim();
    let prompt = promptEnNode.value.trim();
    const button = event.currentTarget;
    const count = Number(mask.querySelector('#vd-count').value || 4);
    button.disabled = true;
    try {
      if (mode === 'fill') {
        const reuse = await findReuseBatch(count);
        if (reuse) {
          batchId = reuse.batch_id || '';
          const existing = (reuse.candidates || []).filter((item) => item.available !== false).length;
          resultHost.innerHTML = `<div class="dlg-note">已找到 ${existing} 个候选的批次，未重新抽卡。</div>`;
          renderBatch(reuse);
          toast(`已复用批次 ${String(batchId).slice(-6)}，未重新抽卡`, 'info', 2600);
          return;
        }
      }
      // 中文改过或英文为空时，自动用模型翻成英文；用户手动改过英文则不覆盖
      if (zh && (!prompt || (zh !== lastTranslatedZh && !enEdited))) {
        const english = await translateToEnglish(zh);
        if (!english) return;
        prompt = english;
        promptEnNode.value = english;
        lastTranslatedZh = zh;
        enEdited = false;
        toast('已自动把中文描述翻译成英文', 'ok');
      }
      if (!prompt) return toast('先填中文或英文音色描述，或点「重新生成描述」', 'warn');
      resultHost.innerHTML = `<div class="dlg-note">正在启动抽卡，进度见右下角任务卡…</div>`;
      const outcome = await runMedia({
        action: 'voice.design',
        params: {
          path: state.path,
          character_id: character.id,
          prompt,
          count,
          profile,
        },
        title: `音色抽卡 · ${character.name}`,
        hint: `候选数 ${count} · ${mode === 'fill' ? '已有候选不足，补抽缺失' : '逐个重新抽卡'} · 可随时取消`,
        confirm: false,
        refresh: refreshHistory,
      });
      // 失败或取消时也刷新一次：可能已经有部分候选落盘
      if (outcome?.job && !['succeeded', 'partial'].includes(String(outcome.job.status))) {
        await refreshHistory();
      }
    } catch (error) {
      toast(error.message, 'bad', 4600);
      resultHost.innerHTML = `<div class="dlg-note bad">${esc(error.message)}</div>`;
    } finally {
      button.disabled = false;
    }
  });

  /** 历史批次 */
  mask.querySelector('[data-vd="history"]').addEventListener('click', async (event) => {
    event.currentTarget.disabled = true;
    try {
      const data = await getJSON('/api/ui/voices/design/history', {
        path: state.path,
        character_id: character.id,
      });
      historyRows = data.batches || [];
      paintHistory();
    } catch (error) {
      toast(error.message, 'bad', 4600);
    } finally {
      event.currentTarget.disabled = false;
    }
  });

  /** 弹窗内所有动作 */
  mask.addEventListener('click', async (event) => {
    const button = event.target.closest('[data-vd]');
    if (!button) return;
    const act = button.dataset.vd;
    if (act === 'close') return closeDialog();
    const index = Number(button.dataset.index || 0);
    if (act === 'play') return playUrl(audioUrl(index), `候选 ${index}`);
    if (act === 'select') {
      button.disabled = true;
      try {
        await postJSON('/api/ui/voices/design/select', {
          path: state.path,
          batch_id: batchId,
          index,
          name: `${character.name}·设计音色`,
          gender: character.gender || 'unknown',
          gender_source: 'manual',
          bind: true,
        });
        closeDialog();
        toast(`已采用候选 ${index} 并绑定给 ${character.name}`, 'ok');
        await loadAll(state.path);
        repaint();
      } catch (error) {
        toast(error.message, 'bad', 4600);
        button.disabled = false;
      }
      return;
    }
    if (act === 'retry') {
      button.disabled = true;
      try {
        const result = await postJSON(`/api/ui/voices/design/batches/${encodeURIComponent(batchId)}/retry`, {
          path: state.path,
          index,
        });
        renderBatch(result.batch || result);
        toast(`已开始重试候选 ${index}，完成后点「刷新候选」`, 'ok', 4200);
      } catch (error) {
        toast(error.message, 'bad', 4600);
        button.disabled = false;
      }
      return;
    }
    if (act === 'cancel') {
      button.disabled = true;
      try {
        const result = await postJSON(
          `/api/ui/voices/design/batches/${encodeURIComponent(batchId)}/cancel`,
          { path: state.path }
        );
        renderBatch(result.batch || result);
        toast('已停止后续候选，已完成候选保留', 'info');
      } catch (error) {
        toast(error.message, 'bad', 4600);
        button.disabled = false;
      }
      return;
    }
    if (act === 'refresh') {
      button.disabled = true;
      try {
        const data = await getJSON('/api/ui/voices/design/history', {
          path: state.path,
          character_id: character.id,
        });
        historyRows = data.batches || [];
        const current = historyRows.find((row) => row.batch_id === batchId) || historyRows[0];
        if (current) renderBatch(current);
        toast('候选已刷新', 'ok', 2000);
      } catch (error) {
        toast(`刷新候选失败：${error.message}`, 'bad', 4600);
      } finally {
        button.disabled = false;
      }
      return;
    }
    if (act === 'open') {
      const id = button.dataset.batch;
      if (!id) return;
      if (!historyRows.length) {
        try {
          const data = await getJSON('/api/ui/voices/design/history', {
            path: state.path,
            character_id: character.id,
          });
          historyRows = data.batches || [];
        } catch (error) {
          toast(error.message, 'bad', 4600);
          return;
        }
      }
      const batch = historyRows.find((item) => item.batch_id === id);
      if (!batch) return toast('批次已不在历史里，请重新读取历史批次', 'warn', 4200);
      renderBatch(batch);
      toast(`已载入批次 ${id.slice(-6)}`, 'info', 1600);
      return;
    }
    if (act === 'delete') {
      const id = button.dataset.batch;
      if (!id) return;
      button.disabled = true;
      try {
        await fetch(`/api/ui/voices/design/batches/${encodeURIComponent(id)}?path=${encodeURIComponent(state.path)}`, {
          method: 'DELETE',
        });
        historyRows = historyRows.filter((item) => item.batch_id !== id);
        button.closest('.summary-line')?.remove();
        toast('批次已清理', 'ok');
      } catch (error) {
        toast(error.message, 'bad', 4600);
        button.disabled = false;
      }
    }
  });
}

/**
 * 一键批量生成音色：交给媒体总线（voice.batch），完成后刷新角色与最近任务。
 * @param {HTMLElement} button 触发按钮
 */
async function runBatchVoices(button) {
  const scope = document.getElementById('vb-scope')?.value || 'unbound';
  const chapterId = document.getElementById('vb-chapter')?.value || state.chapterId || '';
  const count = Number(document.getElementById('vb-count')?.value || 4);
  if (scope === 'chapter' && !chapterId) {
    return toast('章节范围需要先选择一个章节', 'warn', 4200);
  }
  button.disabled = true;
  const oldText = button.textContent;
  button.textContent = '生成中…';
  try {
    await runMedia({
      action: 'voice.batch',
      params: {
        path: state.path,
        scope,
        chapter_id: scope === 'chapter' ? chapterId : undefined,
        count,
      },
      title: '批量生成音色',
      hint: `${scope === 'chapter' ? '只处理本章出现的角色' : '只处理未绑定音色的角色'} · 每个角色 ${count} 个候选 · 可随时取消`,
      refresh: async () => {
        await loadAll(state.path);
        repaint();
      },
    });
  } catch (error) {
    toast(error.message, 'bad', 5200);
  } finally {
    if (document.body.contains(button)) {
      button.disabled = false;
      button.textContent = oldText;
    }
  }
}

/** 点击事件 */
async function onRootClick(event) {
  const row = event.target.closest('[data-chara]');
  if (row) {
    stopAudio();
    selectedCharacter = row.dataset.chara;
    repaint();
    return;
  }
  const genderChip = event.target.closest('[data-gender]');
  if (genderChip) {
    genderFilter = genderChip.dataset.gender;
    repaint();
    return;
  }
  const card = event.target.closest('[data-voice]');
  const button = event.target.closest('[data-act]');
  if (!button) return;
  const act = button.dataset.act;
  const voice = card ? voices.find((item) => item.id === card.dataset.voice) : null;

  if (act === 'back') return goProjects();
  if (act === 'to-editor') return goChapter(state.path, state.chapterId);
  if (act === 'play-voice' && voice) return playUrl(voiceAudioUrl(voice), voice.name);
  if (act === 'bind-voice' && voice) {
    const character = currentCharacter();
    if (!character) return toast('先从左侧选择角色', 'warn');
    if (await bindVoice(voice.id, character.id)) toast(`已绑定给 ${character.name}`, 'ok');
    return;
  }
  if (act === 'unbind-voice') {
    const character = currentCharacter();
    if (!character) return;
    if (await bindVoice(null, character.id)) toast('已解除绑定', 'ok');
    return;
  }
  if (act === 'delete-voice' && voice) {
    const used = usedByCount(voice);
    const name = voice.name || '未命名音色';
    const hint = [
      `删除音色「${name}」？`,
      '参考音频文件会一并删除，不可恢复。',
      used ? `它正被 ${used} 个角色使用，删除后这些角色会自动解绑，需要重新选择音色。` : '',
    ]
      .filter(Boolean)
      .join('\n');
    if (!window.confirm(hint)) return;
    button.disabled = true;
    try {
      const result = await postJSON('/api/ui/voices/delete', {
        path: state.path,
        voice_id: voice.id,
      });
      const files = (result.deleted_files || []).length;
      selectedVoices.delete(voice.id);
      await loadAll(state.path);
      repaint();
      toast(
        [
          used ? `已删除音色，${used} 个角色已解绑` : '已删除音色',
          files ? `参考音频 ${files} 个` : '无参考音频文件',
        ].join('，'),
        'ok'
      );
    } catch (error) {
      toast(`删除音色失败：${error.message}`, 'bad');
      button.disabled = false;
    }
    return;
  }
  if (act === 'compare') return openCompare();
  if (act === 'design') return openVoiceDesign();
  if (act === 'voice-batch') return void runBatchVoices(button);
  if (act === 'play-builtin') {
    const rowNode = button.closest('[data-builtin]');
    const item = (builtin?.items || []).find((entry) => entry.key === rowNode?.dataset.builtin);
    if (item) playUrl(`/api/ui/voices/builtin/audio?key=${encodeURIComponent(item.key)}`, item.name);
    return;
  }
  if (act === 'import-builtin') {
    const rowNode = button.closest('[data-builtin]');
    const key = rowNode?.dataset.builtin;
    if (!key) return;
    button.disabled = true;
    try {
      const result = await postJSON('/api/ui/voices/builtin/import', { path: state.path, keys: [key] });
      toast(`已导入 ${result?.added || 0} 个音色`, 'ok');
      await loadAll(state.path);
      repaint();
    } catch (error) {
      toast(error.message, 'bad');
      button.disabled = false;
    }
  }
}

/** 只更新「候选对比」按钮的计数与可用态，不重建列表（避免勾选时列表跳动、焦点丢失） */
function syncCompareButton() {
  const button = document.querySelector('[data-act="compare"]');
  if (!button) return;
  const size = selectedVoices.size;
  button.disabled = size < 2;
  button.title = size < 2 ? '至少勾选 2 个音色才能并排试听' : `并排试听已勾选的 ${size} 个音色`;
  button.innerHTML = `${icon('audio')} 候选对比（${size}）`;
}

/** 输入事件 */
function onRootInput(event) {
  if (event.target.id === 'voice-search') {
    keyword = event.target.value;
    repaint({ ctxOnly: true });
    const input = document.getElementById('voice-search');
    if (input) {
      input.focus();
      input.setSelectionRange(input.value.length, input.value.length);
    }
    return;
  }
  const compare = event.target.closest('[data-compare]');
  if (compare) {
    if (compare.checked) {
      if (selectedVoices.size >= 3) {
        compare.checked = false;
        return toast('最多同时对比 3 个音色', 'warn');
      }
      selectedVoices.add(compare.dataset.compare);
    } else {
      selectedVoices.delete(compare.dataset.compare);
    }
    syncCompareButton();
  }
}

/** 进入音色视图 */
export async function renderVoices(root, { path }) {
  if (state.path !== path) resetProject(path);
  if (!bound) {
    root.addEventListener('click', onRootClick);
    root.addEventListener('input', onRootInput);
    bound = true;
  }
  root.innerHTML = `${topbar({ left: '', crumbs: '<span class="cur">正在加载音色…</span>', right: '' })}`;
  try {
    await loadAll(path);
  } catch (error) {
    root.innerHTML = `<div class="page"><div class="empty">${icon('warn', 22)}<b>音色加载失败</b>
      <span>${esc(error.message)}</span><a class="btn" href="#/projects" style="margin-top:10px">回工作区</a></div></div>`;
    return;
  }
  root.innerHTML = `
    ${topbar({
      left: `<button class="btn icon" data-act="back" title="回工作区">${icon('back')}</button>
             <div class="brand"><span class="brand-mark"></span><span style="font-size:12.5px">音色<small>本地版</small></span></div>`,
      crumbs: `<a href="#/projects">${esc(state.info?.work?.title || '工程')}</a>
               <span class="sep">/</span><span class="cur">音色</span>`,
      right: `<button class="btn" data-act="to-editor">${icon('chapter')} 回画本</button>`,
    })}
    <div class="studio" id="studio">
      ${railbar('voices', { chapters: state.chapters.length })}
      ${ctxHTML()}
      <section class="editor">
        <div id="voice-main" style="display:flex;flex-direction:column;min-height:0;flex:1">${
          state.chapters.length ? '' : emptyProjectHTML(state.path, { compact: true })
        }${mainHTML()}</div>
      </section>
      <aside class="studio-right"></aside>
    </div>`;
  repaint();
  // 顶部「重新切分本章」：完成后重新读工程数据，提示条自动消失
  onResplit(async () => {
    await loadAll(state.path);
    repaint();
  });
}

/** 离开音色页时清掉重新切分回调 */
export function leaveVoices() {
  onResplit(null);
  stopAudio();
}
