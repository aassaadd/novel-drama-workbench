/**
 * 音色视图（V4）：音色池、内置音色、试听、绑定到角色、候选对比。
 * 只做参考产品有的音色分配，不出现自动编排 / 音色生成。
 */
import { esc, icon, toast, openDialog, fmtDuration } from '../dom.js';
import { getJSON, postJSON } from '../api.js';
import { openProgress } from '../progress.js';
import { modeSwitchHTML, bindModeSwitch } from '../gen-mode.js';
import { state, resetProject } from '../store.js';
import { railbar, topbar, goChapter, goProjects, emptyProjectHTML, checkConsistency, resplitBannerHTML, onResplit } from '../shell.js';

const GENDER_LABEL = { male: '男声', female: '女声', unknown: '中性', other: '其他' };

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
            ${esc(GENDER_LABEL[voice.gender] || '未指定')} ·
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
          GENDER_LABEL[item.gender] || ''
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
                  title="按性别筛选音色池">${key === 'all' ? '全部' : GENDER_LABEL[key]}<span class="n">${genderCounts[key]}</span></button>`
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
  const [info, chapters, payload] = await Promise.all([
    getJSON('/api/project/info', { path }),
    getJSON('/api/ui/chapters', { path }),
    getJSON('/api/ui/voices', { path }),
  ]);
  state.info = info;
  state.chapters = chapters || [];
  if (!state.chapters.some((item) => item.id === state.chapterId)) {
    state.chapterId = state.chapters[0]?.id || '';
  }
  characters = payload?.characters || [];
  voices = payload?.voices || [];
  builtin = payload?.builtin || { items: [] };
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
          GENDER_LABEL[voice.gender] || ''
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

/** 音色设计弹窗的轮询定时器（关闭弹窗后自动停止） */
let designTimer = null;
/** 音色抽卡进度浮层：独立于弹窗，弹窗被关掉也能继续显示到结束 */
let designPanel = null;

/** 候选批次状态文案 */
const DESIGN_STATUS = {
  running: '生成中',
  ready: '已完成',
  partial: '部分完成',
  failed: '失败',
  stopped: '已停止',
};

/**
 * 打开音色设计：按角色档案生成音色描述 → 抽卡候选 → 试听 → 采用并绑定。
 * 接口：preview / batches / batches/{id} / audio / select / retry / cancel / history。
 */
function openVoiceDesign() {
  const character = currentCharacter();
  if (!character) return toast('先从左侧选择一个角色', 'warn');
  if (designTimer) {
    window.clearTimeout(designTimer);
    designTimer = null;
  }
  if (designPanel) {
    designPanel.close();
    designPanel = null;
  }
  let profile = null;
  let batchId = '';
  /** 复用已有批次时给进度浮层的结束文案（空串表示本次是新抽卡） */
  let reuseMessage = '';
  const mask = openDialog({
    title: `音色设计 · ${character.name}`,
    wide: true,
    body: `
      ${modeSwitchHTML('voice.desc', { fallback: 'regen' })}
      ${modeSwitchHTML('voice.draw', { fallback: 'regen' })}
      <div class="dlg-note">按角色档案生成中文音色描述，再由本机 VoxCPM2 抽卡候选；采用后可直接绑定给该角色。</div>
      <div class="field" style="margin-top:8px"><label>音色描述（可手动改写）</label>
        <textarea class="textarea" id="vd-prompt" rows="3"
          placeholder="例如：中年男性，低沉沙哑，语速偏慢，带一点疲惫感"></textarea></div>
      <div style="display:flex;gap:10px;align-items:center;margin-top:8px;flex-wrap:wrap">
        <button class="btn" id="vd-preview">重新生成描述</button>
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

  const promptNode = mask.querySelector('#vd-prompt');
  const hintNode = mask.querySelector('#vd-hint');
  const profileHost = mask.querySelector('#vd-profile');
  const resultHost = mask.querySelector('#vd-result');
  const historyHost = mask.querySelector('#vd-history');

  /** 停止轮询（弹窗已关闭或批次结束） */
  const stopPoll = () => {
    if (designTimer) {
      window.clearTimeout(designTimer);
      designTimer = null;
    }
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

  /** 渲染批次进度与候选清单；进度浮层独立于弹窗，弹窗关闭也不影响 */
  const paintBatch = (meta) => {
    if (!meta) return;
    batchId = meta.batch_id || batchId;
    const candidates = meta.candidates || [];
    const failures = meta.failures || [];
    const total = Number(meta.total || 0);
    const done = Number(meta.done || candidates.length);
    const running = ['running', 'queued'].includes(meta.status);
    if (designPanel) {
      if (running) {
        designPanel.update({
          stage: `${DESIGN_STATUS[meta.status] || '生成中'} ${done}/${total}`,
          percent: total ? Math.round((done / total) * 100) : null,
          current: meta.current_index ? `正在生成第 ${meta.current_index} 个候选` : '候选中…',
        });
      } else if (meta.status === 'failed') {
        designPanel.fail({
          message: `音色抽卡失败${failures.length ? `：${failures[0]?.message || '未知原因'}` : ''}`,
          detail: failures.length ? `共有 ${failures.length} 个候选失败，可在弹窗里单独重试。` : '',
          retry: () => {
            designPanel = openProgress({
              title: `正在重试「${character.name}」的音色候选…`,
              stage: '已提交重试请求…',
            });
            designPanel.update({ percent: null });
            void retryDesign();
          },
        });
        designPanel = null;
      } else {
        designPanel.finish({
          message: reuseMessage || `音色候选已生成：${candidates.length} 个`,
          detail: failures.length ? `${failures.length} 个候选失败，可在弹窗里单独重试。` : '可在弹窗里试听后采用并绑定。',
        });
        designPanel = null;
        reuseMessage = '';
      }
    }
    // 弹窗被关掉后仍继续轮询到批次结束，保证浮层能读到最终结果
    if (!document.body.contains(mask)) {
      schedulePoll(running);
      return;
    }
    if (hintNode) {
      hintNode.textContent = `${DESIGN_STATUS[meta.status] || meta.status || ''} ${done}/${total}${
        meta.current_index ? ` · 正在生成第 ${meta.current_index} 个` : ''
      }`;
    }
    resultHost.innerHTML = `
      <div class="summary-line"><span>批次 ${esc(batchId)}</span>
        <span class="right badge ${meta.status === 'ready' ? 'ok' : meta.status === 'failed' ? 'bad' : 'warn'}">
          ${esc(DESIGN_STATUS[meta.status] || meta.status || '')}</span></div>
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
          ? `<div style="margin-top:8px"><button class="btn sm danger" data-vd="cancel"
               title="停止后续候选，已完成的候选保留">停止抽卡</button></div>`
          : ''
      }`;
    schedulePoll(running);
  };

  /** 安排下一次轮询；弹窗和浮层都关闭时停止，避免后台空转 */
  const schedulePoll = (running) => {
    stopPoll();
    if (!running) return;
    if (!document.body.contains(mask) && !designPanel) return;
    designTimer = window.setTimeout(() => void poll(batchId), 1500);
  };

  /** 只重试失败候选（沿用弹窗内已有的 retry 接口） */
  const retryDesign = async () => {
    if (!batchId) return;
    try {
      const meta = await postJSON(
        `/api/ui/voices/design/batches/${encodeURIComponent(batchId)}/retry`,
        { path: state.path }
      );
      paintBatch(meta);
      toast('已重试失败候选', 'ok');
    } catch (error) {
      designPanel?.fail({ message: `重试失败：${error.message}` });
    }
  };

  /** 轮询批次状态 */
  const poll = async (id) => {
    try {
      const meta = await getJSON(`/api/ui/voices/design/batches/${encodeURIComponent(id)}`, {
        path: state.path,
      });
      paintBatch(meta);
    } catch (error) {
      stopPoll();
      if (document.body.contains(mask)) resultHost.innerHTML = `<div class="dlg-note bad">${esc(error.message)}</div>`;
      designPanel?.fail({ message: `读取抽卡进度失败：${error.message}` });
      designPanel = null;
    }
  };

  /** 预览音色描述：fill 保留已有描述，regen 忽略文本框旧内容按档案重写 */
  mask.querySelector('#vd-preview').addEventListener('click', async (event) => {
    const button = event.currentTarget;
    const mode = descModeOf();
    if (mode === 'fill' && promptNode.value.trim()) {
      return toast('已有音色描述，已保留；要覆盖请选重新生成', 'info', 3200);
    }
    const idleLabel = mode === 'fill' ? '补缺失' : '重新生成描述';
    button.disabled = true;
    button.textContent = '生成中…';
    const panel = openProgress({
      title:
        mode === 'fill'
          ? `正在补全「${character.name}」的音色描述…`
          : `正在重新生成「${character.name}」的音色描述…`,
      hint:
        mode === 'fill'
          ? '只补缺失：文本框为空时才调用模型生成描述'
          : '忽略文本框旧内容，按角色档案重新生成描述',
      stage: '正在调用写作模型…',
    });
    panel.update({ percent: null });
    try {
      // regen 不传 prompt，让后端忽略旧文本、按档案重新生成
      const result = await postJSON('/api/ui/voices/design/preview', {
        path: state.path,
        character_id: character.id,
      });
      profile = result.profile || null;
      promptNode.value = result.prompt || promptNode.value;
      if (result.candidates) mask.querySelector('#vd-count').value = String(result.candidates);
      profileHost.innerHTML = `
        <div class="summary-line"><span>描述来源</span>
          <span class="right">${result.source === 'manual' ? '手动改写' : '按角色档案生成'}</span></div>
        ${
          result.note
            ? `<div class="dlg-note warn" style="margin-top:6px">${esc(result.note)}</div>`
            : ''
        }
        ${
          result.script
            ? `<div class="hint" style="margin-top:6px">试听台词：${esc(result.script)}</div>`
            : ''
        }`;
      // 非流式结果：把模型返回的描述压成一行显示在进度浮层
      panel.note(String(result.prompt || '').slice(0, 80));
      panel.finish({
        message: mode === 'fill' ? '音色描述已补全' : '音色描述已重新生成',
        detail: '已写回文本框，可直接抽卡或手动改写。',
      });
      toast('音色描述已生成', 'ok');
    } catch (error) {
      toast(error.message, 'bad', 4600);
      profileHost.innerHTML = `<div class="dlg-note bad">${esc(error.message)}</div>`;
      panel.fail({ message: `生成音色描述失败：${error.message}` });
    } finally {
      button.disabled = false;
      button.textContent = idleLabel;
    }
  });

  /** 开始抽卡：fill 复用已有就绪批次，regen 按新描述重新抽卡 */
  mask.querySelector('#vd-start').addEventListener('click', async (event) => {
    const mode = drawModeOf();
    const prompt = promptNode.value.trim();
    const button = event.currentTarget;
    button.disabled = true;
    try {
      if (mode === 'fill') {
        const wanted = Number(mask.querySelector('#vd-count').value || 4);
        const availableCount = (batch) =>
          (batch.candidates || []).filter((item) => item.available !== false).length;
        let reuse = null;
        try {
          // 先查历史批次：已有就绪且候选数量足够的批次直接复用，不重新抽卡
          const data = await getJSON('/api/ui/voices/design/history', {
            path: state.path,
            character_id: character.id,
          });
          reuse = (data.batches || []).find(
            (batch) => batch.status === 'ready' && availableCount(batch) >= wanted
          );
        } catch {
          // 历史读取失败时按「没有可复用批次」处理，继续走新建流程
        }
        if (reuse) {
          batchId = reuse.batch_id || '';
          const existing = availableCount(reuse);
          reuseMessage = `已有 ${existing} 个候选，未重新抽卡`;
          resultHost.innerHTML = `<div class="dlg-note">已找到 ${existing} 个候选的批次，正在载入…</div>`;
          designPanel = openProgress({
            title: `正在载入「${character.name}」已有的音色候选…`,
            hint: `只补缺失候选：已有 ${existing} 个，未重新抽卡`,
            stage: '正在读取批次结果…',
          });
          designPanel.update({ percent: null });
          await poll(batchId);
          toast(`已复用批次 ${String(batchId).slice(-6)}，未重新抽卡`, 'info', 2600);
          return;
        }
      }
      if (!prompt) return toast('先填音色描述，或点「按档案生成描述」', 'warn');
      resultHost.innerHTML = `<div class="dlg-note">正在启动抽卡…</div>`;
      designPanel = openProgress({
        title:
          mode === 'fill'
            ? `正在补抽「${character.name}」的音色候选…`
            : `正在重新抽卡「${character.name}」的音色候选…`,
        hint: `候选数 ${Number(mask.querySelector('#vd-count').value || 4)} · ${
          mode === 'fill' ? '已有候选不足，补抽缺失' : '逐个重新抽卡'
        }`,
        stage: '已提交抽卡请求，等待第一个候选…',
      });
      designPanel.update({ percent: 0 });
      const meta = await postJSON('/api/ui/voices/design/batches', {
        path: state.path,
        character_id: character.id,
        prompt,
        profile,
        count: Number(mask.querySelector('#vd-count').value || 4),
      });
      batchId = meta.batch_id || '';
      paintBatch(meta);
      toast('已开始抽卡，候选会逐个出现', 'ok');
    } catch (error) {
      toast(error.message, 'bad', 4600);
      resultHost.innerHTML = `<div class="dlg-note bad">${esc(error.message)}</div>`;
      designPanel?.fail({ message: `启动抽卡失败：${error.message}` });
      designPanel = null;
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
      const batches = data.batches || [];
      historyHost.innerHTML = `
        <div class="card"><div class="card-head"><span class="ct">历史批次</span>
          <span class="hint">${batches.length} 个</span></div>
          <div class="card-body" style="padding:0">
            ${
              batches
                .map(
                  (batch) => `
              <div class="summary-line">
                <span style="min-width:0">${esc(batch.created_at || '')}
                  <span class="badge ${
                    batch.status === 'ready' ? 'ok' : batch.status === 'failed' ? 'bad' : 'mute'
                  }" style="margin-left:6px">${esc(DESIGN_STATUS[batch.status] || batch.status || '')}</span>
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
        stopPoll();
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
        const meta = await postJSON(`/api/ui/voices/design/batches/${encodeURIComponent(batchId)}/retry`, {
          path: state.path,
          index,
        });
        paintBatch(meta.batch || meta);
        toast(`已重新生成候选 ${index}`, 'ok');
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
        paintBatch(result.batch || result);
        toast('已停止后续候选，已完成候选保留', 'info');
      } catch (error) {
        toast(error.message, 'bad', 4600);
        button.disabled = false;
      }
      return;
    }
    if (act === 'open') {
      const id = button.dataset.batch;
      if (!id) return;
      try {
        const meta = await getJSON(`/api/ui/voices/design/batches/${encodeURIComponent(id)}`, {
          path: state.path,
        });
        paintBatch(meta);
        toast(`已载入批次 ${id.slice(-6)}`, 'info', 1600);
      } catch (error) {
        toast(error.message, 'bad', 4600);
      }
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
        button.closest('.summary-line')?.remove();
        toast('批次已清理', 'ok');
      } catch (error) {
        toast(error.message, 'bad', 4600);
        button.disabled = false;
      }
    }
  });
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
  if (act === 'compare') return openCompare();
  if (act === 'design') return openVoiceDesign();
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
