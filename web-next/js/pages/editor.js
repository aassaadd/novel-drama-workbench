/**
 * 画本编辑 / 配音主工作区。
 * 结构对齐参考：顶栏 + 56px 图标栏 + 360px 章节目录 + 台词行 + 任务右栏 + 底部播控。
 * 所有写操作都转调现有 /api/ui/*，前端不复制业务规则。
 */
import { esc, icon, toast, openDialog, closeDialog, openMenu, fmtDuration, fmtClock, fmtBytes, impactListHTML } from '../dom.js';
import { getJSON, postJSON, streamJSON, fileUrl } from '../api.js';
import { ENUMS, selectHTML } from '../enums.js';
import { openProgress, watchProgress } from '../progress.js';
import { modeSwitchHTML, bindModeSwitch } from '../gen-mode.js';
import {
  state,
  resetProject,
  toneFor,
  audioOf,
  stopPlayer,
} from '../store.js';
import {
  railbar,
  topbar,
  chapterCtx,
  goChapter,
  goProjects,
  emptyProjectHTML,
  STATE_LABEL,
  stateClass,
  resplitBannerHTML,
  onResplit,
} from '../shell.js';
import {
  VIEW_META,
  MORE_VIEWS,
  viewMainHTML,
  viewPanelHTML,
  mountView,
  resetViews,
  refreshActiveView,
  configureViews,
  auditPanelHTML,
  paintAuditPanel,
  ensureAuditData,
  onAuditAction,
  stopAuditPlayback,
} from './studio-views.js';

const EMOTION_LABEL = Object.fromEntries(ENUMS.emotion.options);

let pollTimer = null;
let bound = false;
/** 当前页面根节点：重新切分后整页重画时复用 */
let pageRoot = null;

// 目录里的重命名 / 批量切分完成后同步标题；删除当前章由目录层直接导航
document.addEventListener('ndw:chapters-changed', (event) => {
  if (event.detail?.chapterId && event.detail.chapterId !== state.chapterId) return;
  const chapter = currentChapter();
  const title = chapter?.title || '未选择章节';
  document.querySelectorAll('.cp-cur').forEach((node) => {
    node.textContent = title;
  });
  const head = document.querySelector('.ed-head .et');
  if (head) head.textContent = title;
});

/** 清掉轮询，避免切页后继续打接口 */
function stopPolling() {
  if (pollTimer) {
    window.clearInterval(pollTimer);
    pollTimer = null;
  }
}

/** 当前章节对象 */
function currentChapter() {
  return state.chapters.find((item) => item.id === state.chapterId) || null;
}

/** 统计行状态（后端没给 unknown 时补 0） */
function counts() {
  const source = state.counts || {};
  const result = {
    ready: Number(source.ready || 0),
    pending: Number(source.pending || 0),
    stale: Number(source.stale || 0),
    failed: Number(source.failed || 0),
  };
  result.total = result.ready + result.pending + result.stale + result.failed;
  return result;
}

/**
 * 定位到某条台词行：清空筛选与搜索、展开该行并滚动高亮。
 * 供全局搜索结果跳转（`?line=`）与页面内「定位」动作复用。
 * @param {string} lineId 台词行 ID
 */
function focusLine(lineId) {
  if (!lineId) return;
  const exists = state.lines.some((item) => item.line_id === lineId);
  if (!exists) {
    toast('该台词行已不存在（正文或切分已变化），可在本页重新搜索', 'warn', 4200);
    return;
  }
  state.view = 'script';
  state.filter = 'all';
  state.lineSearch = '';
  state.expandedLine = lineId;
  repaintPage();
  const node = document.querySelector(`[data-line-id="${CSS.escape(lineId)}"]`);
  if (!node) return;
  node.scrollIntoView({ block: 'center' });
  const block = node.closest('.seg-block') || node;
  block.classList.add('seg-flash');
  window.setTimeout(() => block.classList.remove('seg-flash'), 1800);
}

// 全局搜索命中「已在当前章」时不会再触发路由，这里接收定位事件
document.addEventListener('ndw:focus-line', (event) => {
  if (!event.detail?.lineId) return;
  if (!pageRoot) return;
  focusLine(event.detail.lineId);
});

/** 按当前筛选与搜索过滤台词行 */
function visibleRows() {
  const keyword = state.lineSearch.trim().toLowerCase();
  return state.lines.filter((row) => {
    if (state.filter !== 'all' && row.state !== state.filter) return false;
    if (!keyword) return true;
    return (
      String(row.text || '').toLowerCase().includes(keyword) ||
      String(row.speaker || '').toLowerCase().includes(keyword)
    );
  });
}

// ---------------- 渲染 ----------------

/** 章节行 HTML */
function rowHTML(row) {
  const tone = toneFor(row.speaker_id);
  const classes = [
    'seg-row',
    state.selected.has(row.line_id) ? 'sel' : '',
    state.runningLine === row.line_id ? 'run' : '',
    row.state === 'failed' ? 'fail' : '',
  ]
    .filter(Boolean)
    .join(' ');
  const playable = Boolean(audioOf(row));
  const duration = row.duration_ms ? fmtDuration(row.duration_ms) : '—';
  const tags = [];
  if (row.kind === 'narration') tags.push('<span class="stag">旁白</span>');
  if (row.kind === 'inner_monologue') tags.push('<span class="stag">内心</span>');
  if (row.emotion && row.emotion !== 'neutral') {
    tags.push(`<span class="stag em">${esc(EMOTION_LABEL[row.emotion] || row.emotion)}</span>`);
  }
  if (Number(row.pause_after_ms) > 400) {
    tags.push(`<span class="stag cue">停顿 ${Math.round(Number(row.pause_after_ms))}ms</span>`);
  }
  const options = speakerOptions(row.speaker_id);
  const edit = state.expandedLine === row.line_id ? lineEditHTML(row) : '';
  const failReason =
    row.state === 'failed' && row.error ? String(row.error.message || row.error.code || '生成失败') : '';
  return `
    <div class="seg-block" data-block="${esc(row.line_id)}">
      <div class="${classes}" data-line-id="${esc(row.line_id)}" data-tone="${tone}">
        <div class="seg-spk">
          <input class="seg-check" type="checkbox" data-check="${esc(row.line_id)}"
                 ${state.selected.has(row.line_id) ? 'checked' : ''} title="选择该行" />
          <button class="seg-play" data-act="play-one" ${playable ? '' : 'disabled'} title="试听该行">
            ${icon(state.playingLine === row.line_id ? 'pause' : 'play', 11)}
          </button>
          <span class="seg-avatar" data-tone="${tone}" title="${esc(row.speaker || '未知')}">${esc(
            Array.from(String(row.speaker || '未'))[0] || '未'
          )}</span>
          <div class="spk-body">
            <select class="spk-select" data-speaker-for="${esc(row.line_id)}" title="改说话人">
              ${options}
            </select>
            <div class="sv">${esc(row.speaker || '未知')} · ${esc(duration)}</div>
          </div>
        </div>
        <div class="seg-text">${tags.join('')}${esc(row.text || '')}</div>
        <div class="seg-act">
          <span class="st-row ${stateClass(row.state)}">${STATE_LABEL[row.state] || row.state}</span>
          <button class="btn icon" data-act="line-menu" title="更多">${icon('more', 14)}</button>
        </div>
      </div>
      ${
        failReason
          ? `<div style="display:flex;gap:8px;align-items:center;padding:3px 12px 6px 46px;font-size:11.5px;color:var(--bad)">
              <span style="min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">失败：${esc(failReason)}</span>
              <button class="btn sm" data-act="rerun-line" data-line="${esc(row.line_id)}">重试</button>
            </div>`
          : ''
      }
      ${edit}
    </div>`;
}

/** 说话人下拉选项 */
function speakerOptions(currentId) {
  const list = [...state.characters];
  const known = new Set(list.map((item) => item.id));
  if (currentId && !known.has(currentId)) {
    list.push({ id: currentId, name: currentId === 'chr_unknown' ? '未知角色' : currentId });
  }
  return list
    .map(
      (item) =>
        `<option value="${esc(item.id)}" ${item.id === currentId ? 'selected' : ''}>${esc(item.name || item.id)}</option>`
    )
    .join('');
}

/** 行内编辑面板 */
function lineEditHTML(row) {
  const drift = state.consistency?.ok === false;
  return `
    <div class="seg-edit" data-edit-for="${esc(row.line_id)}">
      <div class="grid">
        <div class="field full"><label>台词文本</label>
          <textarea class="textarea" data-free="1" id="le-text" ${drift ? 'disabled' : ''}>${esc(row.text || '')}</textarea>
        </div>
        <div class="field"><label>情绪</label>
          ${selectHTML('emotion', { id: 'le-emotion', current: row.emotion || 'neutral' })}</div>
        <div class="field"><label>强度 0–1</label>
          <input class="input" id="le-intensity" type="number" min="0" max="1" step="0.1" value="${Number(row.emotion_intensity ?? 0.5)}" />
        </div>
        <div class="field"><label>语速</label>
          <input class="input" id="le-speed" type="number" min="0.5" max="2" step="0.05" value="${Number(row.speed ?? 1)}" />
        </div>
        <div class="field"><label>前置停顿（ms）</label>
          <input class="input" id="le-pause-before" type="number" min="0" step="50" value="${Number(row.pause_before_ms ?? 0)}" />
        </div>
        <div class="field"><label>后置停顿（ms）</label>
          <input class="input" id="le-pause-after" type="number" min="0" step="50" value="${Number(row.pause_after_ms ?? 0)}" />
        </div>
        <div class="field wide"><label>演绎说明（可选）</label>
          <input class="input" id="le-note" value="${esc(row.delivery_note || '')}" placeholder="例如：压低声音，带一点迟疑" />
        </div>
      </div>
      <div class="acts">
        <span class="hint">${
          drift
            ? '正文与切分已不一致，改词已锁定；先点顶部「重新切分本章」，回来再改。'
            : '改词会同步回章节正文；改参数会让该行变为「已过期」，需重新生成。'
        }</span>
        <div class="right">
          <button class="btn" data-act="split-at-cursor" data-line="${esc(row.line_id)}"
            title="按当前光标位置把这一行拆成两行">在光标处拆分</button>
          <button class="btn" data-act="cancel-line">取消</button>
          <button class="btn primary" data-act="save-line">保存</button>
        </div>
      </div>
    </div>`;
}

/** 状态筛选条 */
function filterBarHTML() {
  const c = counts();
  const chips = [
    ['all', '全部', c.total],
    ['pending', '待配', c.pending],
    ['stale', '已过期', c.stale],
    ['ready', '已配', c.ready],
    ['failed', '失败', c.failed],
  ];
  return `
    <div class="ed-filters">
      ${chips
        .map(
          ([key, label, num]) => `
        <button class="chip ${state.filter === key ? 'on' : ''}" data-filter="${key}">
          ${label}<span class="n">${num}</span>
        </button>`
        )
        .join('')}
      <div class="grow"></div>
      <div class="search" style="width:220px">
        ${icon('search', 14)}
        <input type="search" id="line-search" placeholder="搜索台词 / 角色" value="${esc(state.lineSearch)}" />
      </div>
    </div>
    ${batchBarHTML()}`;
}

/** 批量操作条（有选中行时出现） */
function batchBarHTML() {
  if (!state.selected.size) return '';
  const value = document.getElementById('batch-speaker')?.value || '';
  return `
    <div class="ed-filters" id="batch-bar" style="background:var(--accent-weak);border-color:var(--accent-line)">
      <span style="font-size:12px;color:var(--accent-hi)" data-batch-count>已选 ${state.selected.size} 行</span>
      <div class="grow"></div>
      <select class="select" id="batch-speaker" style="width:150px;height:26px">
        <option value="">改说话人…</option>
        ${state.characters
          .map((item) => `<option value="${esc(item.id)}" ${item.id === value ? 'selected' : ''}>${esc(item.name)}</option>`)
          .join('')}
      </select>
      <button class="btn sm primary" data-act="batch-apply">应用</button>
      <button class="btn sm" data-act="batch-merge" ${state.selected.size < 2 ? 'disabled' : ''}
        title="把选中的相邻台词行合并成一行">合并相邻行</button>
      <button class="btn sm" data-act="batch-clear">取消选择</button>
    </div>`;
}

/**
 * 画本编辑右栏：本章任务 / 智能审听 两个页签（参考右栏页签结构）。
 * @returns {string} 右栏 HTML
 */
function scriptPanelHTML() {
  const tab = state.panelTab === 'audit' ? 'audit' : 'tasks';
  return `
    <div class="sr-tabs">
      <button class="sr-tab ${tab === 'tasks' ? 'on' : ''}" data-act="panel-tab" data-tab="tasks"
        title="本章配音任务与进度">本章任务</button>
      <button class="sr-tab ${tab === 'audit' ? 'on' : ''}" data-act="panel-tab" data-tab="audit"
        title="查看本章审听结果与逐章文本">智能审听</button>
      <div class="right">
        <button class="btn icon sm" data-act="toggle-task" title="收起">${icon('next')}</button>
      </div>
    </div>
    ${tab === 'audit' ? auditPanelHTML() : taskPanelHTML({ head: false })}`;
}

/**
 * 右栏任务面板。
 * @param {{head?: boolean}} [options] head=false 时不输出自己的标题栏（外层已有页签）
 * @returns {string} HTML
 */
function taskPanelHTML({ head = true } = {}) {
  const c = counts();
  const task = state.task || {};
  const ratio = c.total ? Math.round((c.ready / c.total) * 100) : 0;
  const failedRows = (task.rows || []).slice(0, 4);
  return `
    ${
      head
        ? `<div class="sr-head">
      <span class="ct">本章任务</span>
      <span class="badge ${task.running ? 'run' : 'mute'}">${
              task.running ? `生成中 ${task.done || 0}/${task.total || 0}` : ratiosText(c)
            }</span>
      <div class="right">
        <button class="btn icon sm" data-act="toggle-task" title="收起">${icon('next')}</button>
      </div>
    </div>`
        : ''
    }
    <div class="sr-body">
      <div class="sr-sec">
        <h4>配音进度</h4>
        <div class="bar"><span style="width:${ratio}%"></span></div>
        <div style="display:flex;justify-content:space-between;font-size:11px;color:var(--text-3)">
          <span>已配 ${c.ready} / ${c.total} 行</span><span class="mono">${ratio}%</span>
        </div>
        <div class="stat-grid" style="margin-top:12px">
          <div class="stat warn"><div class="k">待配</div><div class="v">${c.pending}</div></div>
          <div class="stat warn"><div class="k">已过期</div><div class="v">${c.stale}</div></div>
          <div class="stat ${c.failed ? 'bad' : ''}"><div class="k">失败</div><div class="v">${c.failed}</div></div>
          <div class="stat ok"><div class="k">已配</div><div class="v">${c.ready}</div></div>
        </div>
      </div>
      <div class="sr-sec">
        <h4>批量配音</h4>
        <div class="sr-actions">
          <button class="btn primary" data-act="batch-plan">${icon('mic')} 补齐缺失配音…</button>
          <button class="btn" data-act="regenerate-chapter">重新生成本章</button>
          ${task.running ? `<button class="btn danger" data-act="stop">${icon('stop')} 停止生成</button>` : ''}
          ${
            task.running
              ? `<button class="btn" data-act="pause" title="暂停后当前行会跑完，点「继续生成」接着配">${
                  state.paused ? `${icon('play')} 继续生成` : `${icon('pause')} 暂停`
                }</button>`
              : ''
          }
        </div>
        <div class="hint" style="margin-top:9px">补齐只处理未生成 / 过期 / 失败的行，可随时中断后续跑。</div>
      </div>
      ${
        task.running
          ? `<div class="sr-sec"><h4>正在生成</h4>
              <div class="hint">${task.done || 0} / ${task.total || 0} 行${
                task.eta_ms ? ` · 预计剩余 ${fmtClock(task.eta_ms)}` : ''
              }${state.paused ? ' · 已暂停，当前行跑完后等待继续' : ''}</div>
              <div class="bar" style="margin-top:8px"><span style="width:${
                task.total ? Math.min(100, Math.round(((task.done || 0) / task.total) * 100)) : 0
              }%"></span></div>
            </div>`
          : ''
      }
      ${
        failedRows.length
          ? `<div class="sr-sec"><h4>失败行（最近 ${failedRows.length}）</h4>
              <div class="fail-list">${failedRows
                .map(
                  (row) => `<div class="fail-row">
                    <span class="t" title="${esc(row.message || row.code || '')}">${esc(row.line_id || '')} ${esc(
                    row.message || row.code || ''
                  )}</span>
                    <button class="btn sm" data-retry-line="${esc(row.line_id || '')}">重录</button>
                  </div>`
                )
                .join('')}</div>
            </div>`
          : ''
      }
      <div class="sr-sec">
        <h4>本章信息</h4>
        <div class="hint">
          场景 ${esc(currentChapter()?.scene_count ?? '—')} 个 · 字数 ${esc(currentChapter()?.word_count ?? '—')}<br />
          可试听 ${state.lines.filter(audioOf).length} 行，总时长 ${fmtClock(
            state.lines.reduce((sum, row) => sum + (audioOf(row) ? Number(row.duration_ms || 0) : 0), 0)
          )}
        </div>
      </div>
    </div>`;
}

/** 右栏折叠时的小进度条文字 */
function ratiosText(c) {
  if (!c.total) return '暂无台词';
  return `${c.ready}/${c.total}`;
}

/** 底部播控条 */
function playerHTML() {
  const ready = state.lines.filter(audioOf);
  const totalMs = ready.reduce((sum, row) => sum + Number(row.duration_ms || 0), 0);
  const current = state.player.playing
    ? `正在播放第 ${state.player.index + 1} / ${ready.length} 段`
    : '整章试听会按顺序播放已配好的行';
  return `
    <div class="player">
      <div class="pl-main">
        <button class="btn icon" data-act="play-all" title="${state.player.playing ? '暂停' : '整章试听'}">
          ${icon(state.player.playing ? 'pause' : 'play')}
        </button>
        <span class="pl-time">${fmtClock(totalMs)}</span>
      </div>
      <div class="wave">${Array.from({ length: 28 }, (_, i) => `<i style="height:${4 + ((i * 7) % 16)}px"></i>`).join('')}</div>
      <div class="pl-cur" id="player-cur">${esc(current)}</div>
      <button class="btn sm" data-act="toggle-timeline">${icon('chapter', 13)} 时间轴</button>
      <span class="pl-time">${ready.length} 段</span>
    </div>`;
}

/** 时间轴面板状态 */
const tl = { open: false, data: null, loading: false, peaks: new Map(), audio: null, raf: 0 };

/** 时间轴面板 HTML（形态按参考：刻度 + 分轨 + 音频块） */
function timelineHTML() {
  if (!tl.open) return '';
  if (tl.loading || !tl.data) {
    return `<div class="tl-panel" id="tl-panel"><div class="empty"><span>正在构建时间轴…</span></div></div>`;
  }
  const total = Math.max(1, Number(tl.data.duration_ms || 0));
  const marks = Array.from({ length: 11 }, (_, index) => {
    const at = Math.round((total / 10) * index);
    return `<span style="left:${index * 10}%">${fmtClock(at)}</span>`;
  }).join('');
  const tracks = (tl.data.tracks || [])
    .map((track) => {
      const items = (track.items || [])
        .map((item) => {
          const start = Number(item.start_ms || 0);
          const end = Number(item.end_ms || start);
          const left = ((start / total) * 100).toFixed(3);
          const width = Math.max(0.35, ((end - start) / total) * 100).toFixed(3);
          return `<button class="tl-item k-${esc(track.kind || 'voice')}" style="left:${left}%;width:${width}%"
            data-tl-asset="${esc(item.asset_id || '')}" data-tl-start="${start}" title="${esc(item.label || '')}">
            <span class="tl-wave" data-wave="${esc(item.asset_id || '')}"></span>
          </button>`;
        })
        .join('');
      return `<div class="tl-track"><span class="tl-name">${esc(track.name || track.kind || '')}</span>
        <div class="tl-lane" data-lane="${esc(track.kind || '')}">${items}</div></div>`;
    })
    .join('');
  return `
    <div class="tl-panel" id="tl-panel">
      <div class="tl-head">
        <span class="ct">时间轴</span>
        <span class="hint">${fmtClock(total)} · 点击音频块试听；拖动 / 切割等编辑操作待参考复核后开放</span>
        <div class="right">
          <button class="btn sm" data-act="tl-build">重建</button>
          <button class="btn icon sm" data-act="toggle-timeline" title="收起">${icon('next')}</button>
        </div>
      </div>
      <div class="tl-ruler">${marks}</div>
      <div class="tl-tracks">${tracks}</div>
      <div class="tl-playhead" id="tl-playhead" style="left:0"></div>
    </div>`;
}

/** 展开 / 收起时间轴；首次展开自动构建 */
async function toggleTimeline() {
  tl.open = !tl.open;
  const anchor = document.querySelector('.player');
  const wrapper = document.createElement('div');
  wrapper.innerHTML = timelineHTML();
  const panel = wrapper.firstElementChild;
  if (panel) anchor.before(panel);
  else document.getElementById('tl-panel')?.remove();
  if (!tl.open) {
    tl.audio?.pause();
    cancelAnimationFrame(tl.raf);
    return;
  }
  if (!tl.data) await buildTimeline();
}

/** 构建时间轴并重画面板 */
async function buildTimeline() {
  tl.loading = true;
  repaintTimeline();
  try {
    tl.data = await postJSON('/api/ui/timeline/build', { path: state.path, chapter: state.chapterId });
    tl.peaks.clear();
  } catch (error) {
    toast(error.message, 'bad');
    tl.data = null;
  } finally {
    tl.loading = false;
    repaintTimeline();
  }
}

/** 只重画时间轴面板 */
function repaintTimeline() {
  const old = document.getElementById('tl-panel');
  if (!old) return;
  const wrapper = document.createElement('div');
  wrapper.innerHTML = timelineHTML();
  const panel = wrapper.firstElementChild;
  if (panel) old.replaceWith(panel);
}

/** 计算真实波形峰值（首播时解码一次，结果缓存） */
async function loadPeaks(asset, node) {
  if (!asset) return;
  if (tl.peaks.has(asset)) {
    paintPeaks(node, tl.peaks.get(asset));
    return;
  }
  try {
    const response = await fetch(fileUrl(asset, state.path));
    const buffer = await response.arrayBuffer();
    const context = new (window.AudioContext || window.webkitAudioContext)();
    const decoded = await context.decodeAudioData(buffer);
    const samples = decoded.getChannelData(0);
    const bars = 64;
    const step = Math.floor(samples.length / bars) || 1;
    const peaks = Array.from({ length: bars }, (_, index) => {
      let max = 0;
      for (let cursor = index * step; cursor < (index + 1) * step && cursor < samples.length; cursor += 16) {
        const value = Math.abs(samples[cursor]);
        if (value > max) max = value;
      }
      return max;
    });
    context.close?.();
    tl.peaks.set(asset, peaks);
    paintPeaks(node, peaks);
  } catch {
    // 解码失败（格式不支持）时保留纯色块，不伪造波形
  }
}

/** 把峰值画进音频块 */
function paintPeaks(node, peaks) {
  const height = node.clientHeight || 30;
  const width = Math.max(1, node.clientWidth);
  const step = width / peaks.length;
  node.innerHTML = `<svg width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none">
    ${peaks
      .map((peak, index) => {
        const bar = Math.max(1, peak * height * 0.9);
        return `<rect x="${(index * step).toFixed(1)}" y="${((height - bar) / 2).toFixed(1)}"
          width="${Math.max(1, step - 0.6).toFixed(1)}" height="${bar.toFixed(1)}" fill="currentColor" opacity="0.55" />`;
      })
      .join('')}
  </svg>`;
}

/** 播放时间轴上的音频块并推进播放头 */
async function playTimelineItem(asset, startMs) {
  if (!asset) return;
  tl.audio?.pause();
  tl.audio = new Audio(fileUrl(asset, state.path));
  const total = Math.max(1, Number(tl.data?.duration_ms || 0));
  const tick = () => {
    const node = document.getElementById('tl-playhead');
    if (!node || !tl.audio) return;
    const at = Number(startMs) + tl.audio.currentTime * 1000;
    node.style.left = `${Math.min(100, (at / total) * 100)}%`;
    tl.raf = requestAnimationFrame(tick);
  };
  tl.audio.addEventListener('ended', () => cancelAnimationFrame(tl.raf));
  try {
    await tl.audio.play();
    cancelAnimationFrame(tl.raf);
    tl.raf = requestAnimationFrame(tick);
  } catch (error) {
    toast(`播放失败：${error.message}`, 'bad');
  }
}

/** 整页 HTML */
function pageHTML() {
  // 旧版本可能把「审听与对轨」当作主视图；现在它已拆成独立对轨页 + 右栏页签
  if (state.view === 'review') state.view = 'script';
  const chapter = currentChapter();
  const c = counts();
  const index = state.chapters.findIndex((item) => item.id === state.chapterId);
  const prev = state.chapters[index - 1];
  const next = state.chapters[index + 1];
  const rows = visibleRows();
  return `
    ${topbar({
      left: `<button class="btn icon" data-act="back" title="返回工作区">${icon('back')}</button>
             <span class="save-state ${state.saveState === 'saving' ? '' : 'ok'}" id="tb-save" title="已自动保存">
               ${icon('cloud', 15)}<em>${state.saveState === 'saving' ? '保存中…' : '已自动保存'}</em>
             </span>
             <span class="tb-sep"></span>
             ${VIEW_META.map(
               (view) => `<button class="btn icon view-tab ${state.view === view.key ? 'on' : ''}"
                 data-act="set-view" data-view="${view.key}" title="${view.title}">${icon(view.icon)}</button>`
             ).join('')}
             <button class="btn icon view-tab ${state.view === 'align' || (state.view === 'script' && state.panelTab === 'audit') ? 'on' : ''}"
               data-act="more-views" title="审听与对轨视图">${icon('more')}</button>`,
      crumbs: `<button class="btn icon sm" data-act="prev-chapter" ${prev ? '' : 'disabled'} title="上一章">${icon('prev')}</button>
               <button class="chapter-picker" data-act="chapter-picker" title="切换章节">
                 <span class="cp-title">${esc(state.info?.work?.title || '工程')}</span>
                 <span class="sep">/</span>
                 <span class="cp-cur">${esc(chapter?.title || '未选择章节')}</span>
                 <span class="cp-caret">⌄</span>
               </button>
               <button class="btn icon sm" data-act="next-chapter" ${next ? '' : 'disabled'} title="下一章">${icon('next')}</button>`,
      right: `<button class="btn icon" id="tb-undo" data-act="undo" ${history.undo.length ? '' : 'disabled'} title="撤销（⌘Z）">${icon('undo')}</button>
        <button class="btn icon" id="tb-redo" data-act="redo" ${history.redo.length ? '' : 'disabled'} title="重做（⇧⌘Z）">${icon('redo')}</button>
        <button class="btn icon" data-act="open-settings" title="工程设置：作品信息与制作要求">${icon('gear')}</button>`,
    })}
    <div class="studio" id="studio" data-font="${state.fontSize}">
      ${railbar('chapter', { chapters: state.chapters.length, ready: c.ready, total: c.total })}
      ${chapterCtx({ search: state.chapterSearch })}
      <button class="ctx-restore" data-act="toggle-ctx" title="展开目录">${icon('next', 13)}</button>
      ${
        state.view === 'script'
          ? `<section class="editor ${state.dense ? 'dense' : ''} ${state.hideTags ? 'hide-tags' : ''}">
        <div id="drift-host">${resplitBannerHTML(state.consistency)}</div>
        <div class="ed-head">
          <div style="min-width:0">
            <div class="et">${esc(chapter?.title || '未选择章节')}</div>
            <div class="em">${chapter ? `第 ${chapter.index} 章 · ${chapter.word_count || 0} 字 · ${chapter.scene_count || 0} 场景` : ''}</div>
          </div>
          <div class="right">
            <span class="badge ${c.failed ? 'bad' : 'mute'}">失败 ${c.failed}</span>
            <span class="badge warn">待配 ${c.pending + c.stale}</span>
            <button class="chip ${state.hideTags ? 'on' : ''}" data-act="toggle-tags"
              title="隐藏标签（旁白 / 情绪 / 停顿）">隐藏标签</button>
            <button class="chip ${state.dense ? 'on' : ''}" data-act="toggle-dense"
              title="切换段落显示模式">${state.dense ? '完整显示' : '紧凑显示'}</button>
            <button class="btn icon sm" data-act="font-size" title="字号">A</button>
            <button class="btn sm" data-act="view-original" title="查看原文">查看原文</button>
            <button class="btn sm" data-act="scene-split-preview" title="按地点与时间拆场景，先预览再覆盖">拆场景</button>
            <button class="btn sm" data-act="merge-narration" title="把相邻旁白合并成更长的段落">批量合并旁白</button>
            <button class="btn sm" data-act="script-annotate"
              title="按规则解析正文生成导演标注，并查看告警的三级定位">规则标注</button>
            <button class="btn icon sm" data-act="focus-search" title="查找（⌘F）">${icon('search')}</button>
            <button class="btn sm primary" data-act="batch-plan">${icon('mic', 13)} 生成配音</button>
          </div>
        </div>
        <div id="filter-bar">${filterBarHTML()}</div>
        <div class="ed-body" id="ed-body">
          ${
            rows.length
              ? rows.map(rowHTML).join('')
              : `<div class="empty">${icon('chapter', 22)}<b>${state.lines.length ? '当前筛选下没有行' : '本章还没有台词行'}</b>
                  <span>${
                    state.lines.length
                      ? '切回「全部」或清空搜索'
                      : state.chapters.find((item) => item.id === state.chapterId)?.word_count
                        ? '重新切分本章后，台词行会出现在这里'
                        : '这一章是从大纲落地的，正文还没写：回小说视图生成或粘贴正文并保存，台词会自动切分。'
                  }</span>
                  ${
                    state.lines.length || state.chapters.find((item) => item.id === state.chapterId)?.word_count
                      ? ''
                      : `<a class="btn primary" href="#/p/${encodeURIComponent(state.path || '')}/novel"
                           style="margin-top:10px">${icon('book', 13)} 去小说视图写正文</a>`
                  }</div>`
          }
        </div>
        ${timelineHTML()}
        ${playerHTML()}
      </section>`
          : `<section class="editor view-${state.view}" id="vw-main">${viewMainHTML(state.view)}</section>`
      }
      <aside class="task-rail" data-act="toggle-task" title="展开本章任务">
        <span class="tr-label">本章任务</span>
        <div class="tr-prog"><span style="height:${
          c.total ? Math.round((c.ready / c.total) * 100) : 0
        }%"></span></div>
        <span class="tr-badge">${c.total ? Math.round((c.ready / c.total) * 100) : 0}%</span>
      </aside>
      <aside class="studio-right" id="task-panel">${
        state.view === 'script' ? scriptPanelHTML() : viewPanelHTML(state.view)
      }</aside>
    </div>`;
}

/**
 * 切换工作室视图：'script' / 'record' / 'post' / 'align'。
 * 'audit' 不是独立视图，等价于回到画本编辑并打开右栏「智能审听」页签。
 * @param {string} key 视图键
 */
function switchView(key) {
  const view = key === 'audit' ? 'script' : key;
  stopAuditPlayback();
  state.view = view;
  if (key === 'audit') state.panelTab = 'audit';
  repaintPage();
  const studio = document.getElementById('studio');
  // 参考的录音视图与智能审听默认展开右栏
  if (view === 'record' || key === 'audit') {
    if (studio) {
      studio.classList.add('right-open');
      if (window.innerWidth < 1680) studio.classList.add('ctx-hidden');
    }
  }
  if (key === 'audit' || (view === 'script' && state.panelTab === 'audit')) {
    ensureAuditData().then(() => paintAuditPanel());
  }
}

/**
 * 切换视图 / 重新切分后整页重画。
 * 顶栏、图标栏、章节目录、右栏骨架保持不变，只换主区与右栏内容。
 */
function repaintPage() {
  if (!pageRoot) return;
  stopPlayer();
  stopPolling();
  pageRoot.innerHTML = pageHTML();
  mountView(pageRoot);
  startPolling();
}

/** 只重画主区（保留滚动位置） */
function repaintRows() {
  const body = document.getElementById('ed-body');
  if (!body) return;
  const top = body.scrollTop;
  const rows = visibleRows();
  body.innerHTML = rows.length
    ? rows.map(rowHTML).join('')
    : `<div class="empty">${icon('chapter', 22)}<b>当前筛选下没有行</b><span>切回「全部」或清空搜索</span></div>`;
  body.scrollTop = top;
  const filter = document.getElementById('filter-bar');
  if (filter) filter.innerHTML = filterBarHTML();
}

/**
 * 同步批量操作条：按当前勾选增删或就地更新，不重建列表与筛选条。
 * 勾选 / 取消勾选都不会打断滚动位置、搜索框焦点和批量下拉已选的值。
 */
function syncBatchBar() {
  const host = document.getElementById('filter-bar');
  if (!host) return;
  let bar = host.querySelector('#batch-bar');
  if (!state.selected.size) {
    bar?.remove();
    return;
  }
  if (!bar) {
    host.insertAdjacentHTML('beforeend', batchBarHTML());
    bar = host.querySelector('#batch-bar');
  }
  if (!bar) return;
  const label = bar.querySelector('[data-batch-count]');
  if (label) label.textContent = `已选 ${state.selected.size} 行`;
  const merge = bar.querySelector('[data-act="batch-merge"]');
  if (merge) merge.disabled = state.selected.size < 2;
}

/**
 * 就地更新一条台词行的展示（说话人、头像、状态、行内标签），不替换节点。
 * @param {object} row 台词行数据
 */
function patchLineRow(row) {
  const node = document.querySelector(`[data-line-id="${CSS.escape(row.line_id)}"]`);
  if (!node) return;
  const tone = toneFor(row.speaker_id);
  node.dataset.tone = tone;
  const avatar = node.querySelector('.seg-avatar');
  if (avatar) {
    avatar.dataset.tone = tone;
    avatar.textContent = Array.from(String(row.speaker || '未'))[0] || '未';
    avatar.title = row.speaker || '未知';
  }
  const select = node.querySelector('.spk-select');
  if (select) select.value = row.speaker_id;
  const sv = node.querySelector('.sv');
  if (sv) {
    sv.textContent = `${row.speaker || '未知'} · ${
      row.duration_ms ? fmtDuration(row.duration_ms) : '—'
    }`;
  }
  const badge = node.querySelector('.st-row');
  if (badge) {
    badge.className = `st-row ${stateClass(row.state)}`;
    badge.textContent = STATE_LABEL[row.state] || row.state;
  }
  const text = node.querySelector('.seg-text');
  if (text) {
    const tags = [];
    if (row.kind === 'narration') tags.push('<span class="stag">旁白</span>');
    if (row.kind === 'inner_monologue') tags.push('<span class="stag">内心</span>');
    if (row.emotion && row.emotion !== 'neutral') {
      tags.push(`<span class="stag em">${esc(EMOTION_LABEL[row.emotion] || row.emotion)}</span>`);
    }
    if (Number(row.pause_after_ms) > 400) {
      tags.push(`<span class="stag cue">停顿 ${Math.round(Number(row.pause_after_ms))}ms</span>`);
    }
    text.innerHTML = `${tags.join('')}${esc(row.text || '')}`;
  }
}

/** 只更新筛选条上的计数徽标，保留批量条与搜索框焦点 */
function paintFilterCounts() {
  const c = counts();
  const map = { all: c.total, pending: c.pending, stale: c.stale, ready: c.ready, failed: c.failed };
  document.querySelectorAll('#filter-bar .chip[data-filter]').forEach((chip) => {
    const num = chip.querySelector('.n');
    if (num) num.textContent = String(map[chip.dataset.filter] ?? 0);
  });
}

/**
 * 重新读取台词数据并就地刷新：只改受影响行的文本与状态，不重建列表。
 * 勾选集合、滚动位置、批量面板和当前焦点都保持不变。
 */
async function refreshLinesInPlace(keepTop) {
  const body = document.getElementById('ed-body');
  const top = keepTop ?? body?.scrollTop ?? 0;
  const payload = await getJSON('/api/ui/lines', { path: state.path, chapter: state.chapterId });
  state.lines = payload?.lines || [];
  state.counts = payload?.counts || {};
  state.consistency = payload?.consistency || null;
  state.lines.forEach(patchLineRow);
  paintFilterCounts();
  repaintBanner();
  repaintTask();
  if (state.view !== 'script') refreshActiveView();
  // 行高可能因标签/状态变化而微调，这里锁死滚动位置，避免浏览器锚定导致跳动
  if (body) body.scrollTop = top;
}

/**
 * 用行模板生成一个台词块节点（不插入文档）。
 * @param {object} row 台词行数据
 * @returns {HTMLElement|null}
 */
function blockNode(row) {
  const wrap = document.createElement('div');
  wrap.innerHTML = rowHTML(row).trim();
  return wrap.firstElementChild;
}

/**
 * 按当前筛选顺序就地校准列表节点：已有行只更新行内展示，缺的行按位置插入，多余的行移除。
 * 不重建已有节点，因此输入框焦点、光标位置与滚动都不会被重置。
 */
function reconcileRowNodes() {
  const body = document.getElementById('ed-body');
  if (!body) return;
  const rows = visibleRows();
  if (!rows.length) {
    body.innerHTML = `<div class="empty">${icon('chapter', 22)}<b>当前筛选下没有行</b><span>切回「全部」或清空搜索</span></div>`;
    return;
  }
  body.querySelector('.empty')?.remove();
  const nodes = new Map();
  body.querySelectorAll('[data-block]').forEach((node) => nodes.set(node.dataset.block, node));
  const wanted = new Set(rows.map((row) => row.line_id));
  for (const [id, node] of nodes) {
    if (wanted.has(id)) continue;
    node.remove();
    nodes.delete(id);
  }
  let anchor = null;
  for (const row of rows) {
    let node = nodes.get(row.line_id);
    if (!node) {
      node = blockNode(row);
      if (!node) continue;
      nodes.set(row.line_id, node);
    }
    patchLineRow(row);
    if (anchor) {
      if (anchor.nextElementSibling !== node) anchor.after(node);
    } else if (body.firstElementChild !== node) {
      body.prepend(node);
    }
    anchor = node;
  }
}

/** 只重画任务面板 */
function repaintTask() {
  const panel = document.getElementById('task-panel');
  if (panel) panel.innerHTML = state.view === 'script' ? scriptPanelHTML() : viewPanelHTML(state.view);
  const rail = document.querySelector('.task-rail .tr-badge');
  if (rail) {
    const c = counts();
    rail.textContent = c.total ? `${Math.round((c.ready / c.total) * 100)}%` : '0%';
    const bar = document.querySelector('.task-rail .tr-prog > span');
    if (bar) bar.style.height = `${c.total ? Math.round((c.ready / c.total) * 100) : 0}%`;
  }
}

// ---------------- 数据 ----------------

/** 读取整页所需数据 */
async function loadData(path, chapterId) {
  const [info, chapters, characters, cast] = await Promise.all([
    getJSON('/api/project/info', { path }),
    getJSON('/api/ui/chapters', { path }),
    getJSON('/api/ui/characters', { path }),
    getJSON('/api/ui/cast', { path }).catch(() => null),
  ]);
  state.info = info;
  state.chapters = chapters || [];
  state.characters = (characters?.characters || []).map((item) => ({ id: item.id, name: item.name }));
  if (!state.characters.some((item) => item.id === 'chr_narrator')) {
    state.characters.unshift({ id: 'chr_narrator', name: '旁白' });
  }
  state.cast = cast;
  state.chapterId = state.chapters.some((item) => item.id === chapterId)
    ? chapterId
    : state.chapters[0]?.id || '';
  resetHistory();
}

/** 读取章节台词与批量任务状态 */
async function loadChapterState() {
  if (!state.path || !state.chapterId) return;
  const [payload, task] = await Promise.all([
    getJSON('/api/ui/lines', { path: state.path, chapter: state.chapterId }),
    getJSON('/api/ui/tts/batch-all/progress', { path: state.path }).catch(() => null),
  ]);
  state.lines = payload?.lines || [];
  state.counts = payload?.counts || {};
  state.consistency = payload?.consistency || null;
  state.task = task;
  state.selected = new Set();
  state.expandedLine = '';
  state.saveState = 'saved';
  updateSaveBadge();
}

/** 顶栏云保存态：与参考一致，图标 + 文案常驻 */
function updateSaveBadge() {
  const node = document.getElementById('tb-save');
  if (!node) return;
  const saving = state.saveState === 'saving';
  node.className = `save-state ${saving ? '' : 'ok'}`;
  node.innerHTML = `${icon('cloud', 15)}<em>${saving ? '保存中…' : '已自动保存'}</em>`;
}

/** 软刷新（保留滚动与筛选） */
async function softRefresh() {
  try {
    const payload = await getJSON('/api/ui/lines', { path: state.path, chapter: state.chapterId });
    state.lines = payload?.lines || [];
    state.counts = payload?.counts || {};
    state.consistency = payload?.consistency || null;
    repaintBanner();
    repaintRows();
    repaintTask();
    // 三个制作视图共用同一份台词数据，刷新后同步重画当前视图
    if (state.view !== 'script') refreshActiveView();
  } catch (error) {
    toast(error.message, 'bad');
  }
}

/** 只重画「正文 / 切分不一致」提示条 */
function repaintBanner() {
  const host = document.getElementById('drift-host');
  if (host) host.innerHTML = resplitBannerHTML(state.consistency);
}

// ---------------- 操作 ----------------

// ---------------- 撤销 / 重做 ----------------

/** 行编辑历史：每条记录包含若干 step，撤销/重做都按 before/after 反向写回 */
const history = { undo: [], redo: [] };
const HISTORY_LIMIT = 50;

/** 记录一次操作并清空重做栈 */
function pushHistory(entry) {
  history.undo.push(entry);
  if (history.undo.length > HISTORY_LIMIT) history.undo.shift();
  history.redo.length = 0;
  updateHistoryButtons();
}

/** 顶栏撤销/重做按钮的可用态 */
function updateHistoryButtons() {
  const undoBtn = document.getElementById('tb-undo');
  const redoBtn = document.getElementById('tb-redo');
  if (undoBtn) undoBtn.disabled = history.undo.length === 0;
  if (redoBtn) redoBtn.disabled = history.redo.length === 0;
}

/** 清空历史（切章或离开时） */
function resetHistory() {
  history.undo.length = 0;
  history.redo.length = 0;
  updateHistoryButtons();
}

/**
 * 按方向写回一条历史记录。
 * @param {object} entry 历史记录
 * @param {'undo'|'redo'} direction 方向
 */
async function applyHistoryEntry(entry, direction) {
  for (const step of entry.steps) {
    const payloadMap = direction === 'undo' ? step.before : step.after;
    for (const [line, payload] of Object.entries(payloadMap)) {
      if (step.kind === 'text') {
        await postJSON('/api/ui/line/edit', {
          path: state.path,
          chapter: state.chapterId,
          line,
          text: payload,
        });
      } else {
        await postJSON('/api/ui/line/set', {
          path: state.path,
          chapter: state.chapterId,
          line,
          ...payload,
        });
      }
    }
  }
}

/** 撤销 */
async function undo() {
  const entry = history.undo.pop();
  if (!entry) return;
  try {
    await applyHistoryEntry(entry, 'undo');
    history.redo.push(entry);
    await softRefresh();
    toast(`已撤销：${entry.label}`, 'ok');
  } catch (error) {
    history.undo.push(entry);
    toast(error.message, 'bad');
  }
  updateHistoryButtons();
}

/** 重做 */
async function redo() {
  const entry = history.redo.pop();
  if (!entry) return;
  try {
    await applyHistoryEntry(entry, 'redo');
    history.undo.push(entry);
    await softRefresh();
    toast(`已重做：${entry.label}`, 'ok');
  } catch (error) {
    history.redo.push(entry);
    toast(error.message, 'bad');
  }
  updateHistoryButtons();
}

/** 保存说话人 */
async function saveSpeaker(lineId, speakerId) {
  const row = state.lines.find((item) => item.line_id === lineId);
  const before = row?.speaker_id;
  state.saveState = 'saving';
  updateSaveBadge();
  try {
    await postJSON('/api/ui/line/set', {
      path: state.path,
      chapter: state.chapterId,
      line: lineId,
      speaker: speakerId,
    });
    if (before !== speakerId) {
      pushHistory({
        label: '改说话人',
        steps: [
          {
            kind: 'set',
            before: { [lineId]: { speaker: before } },
            after: { [lineId]: { speaker: speakerId } },
          },
        ],
      });
    }
    toast('已改说话人，该行需要重新生成', 'ok');
    await refreshLinesInPlace();
    state.saveState = 'saved';
    updateSaveBadge();
  } catch (error) {
    toast(error.message, 'bad');
    state.saveState = 'idle';
    updateSaveBadge();
  }
}

/**
 * 把行内编辑面板里的当前输入写回后端：文本变化走 `/api/ui/line/edit`，
 * 参数变化合并成一次 `/api/ui/line/set`，并记录一步可撤销编辑。
 * 只负责落盘与本地数据同步，不重画列表、不弹提示；
 * 「保存」按钮与「在光标处拆分」前的自动落盘共用这一份逻辑。
 * @param {string} lineId 台词行 ID
 * @returns {Promise<boolean>} 是否有改动被写入
 */
async function persistLineEdit(lineId) {
  const row = state.lines.find((item) => item.line_id === lineId);
  const panel = document.querySelector(`[data-edit-for="${CSS.escape(lineId)}"]`);
  if (!row || !panel) return false;
  const text = panel.querySelector('#le-text').value;
  const emotion = panel.querySelector('#le-emotion').value;
  const intensity = Number(panel.querySelector('#le-intensity').value);
  const speed = Number(panel.querySelector('#le-speed').value);
  const pauseBefore = Math.round(Number(panel.querySelector('#le-pause-before').value));
  const pauseAfter = Math.round(Number(panel.querySelector('#le-pause-after').value));
  const note = panel.querySelector('#le-note').value.trim();
  // 记录一次可撤销编辑（文本与参数合并成一条，撤销一次全部回退）
  const steps = [];
  if (text !== (row.text || '')) {
    const result = await postJSON('/api/ui/line/edit', {
      path: state.path,
      chapter: state.chapterId,
      line: lineId,
      text,
    });
    steps.push({ kind: 'text', before: { [lineId]: row.text || '' }, after: { [lineId]: text } });
    // 后端会去掉首尾空白：按返回文本同步本地，后续拆分偏移才和界面一致
    row.text = String(result?.text ?? text);
  }
  const beforeParams = {};
  const afterParams = {};
  if (emotion !== row.emotion) {
    beforeParams.emotion = row.emotion || 'neutral';
    afterParams.emotion = emotion;
  }
  if (intensity !== Number(row.emotion_intensity)) {
    beforeParams.emotion_intensity = Number(row.emotion_intensity);
    afterParams.emotion_intensity = intensity;
  }
  if (speed !== Number(row.speed)) {
    beforeParams.speed = Number(row.speed);
    afterParams.speed = speed;
  }
  if (pauseBefore !== Number(row.pause_before_ms)) {
    beforeParams.pause_before_ms = Number(row.pause_before_ms);
    afterParams.pause_before_ms = pauseBefore;
  }
  if (pauseAfter !== Number(row.pause_after_ms)) {
    beforeParams.pause_after_ms = Number(row.pause_after_ms);
    afterParams.pause_after_ms = pauseAfter;
  }
  if (note !== (row.delivery_note || '')) {
    beforeParams.delivery_note = row.delivery_note || '';
    afterParams.delivery_note = note;
  }
  if (Object.keys(afterParams).length) {
    await postJSON('/api/ui/line/set', {
      path: state.path,
      chapter: state.chapterId,
      line: lineId,
      ...afterParams,
    });
    Object.assign(row, afterParams);
    steps.push({ kind: 'set', before: { [lineId]: beforeParams }, after: { [lineId]: afterParams } });
  }
  if (steps.length) pushHistory({ label: '编辑台词', steps });
  return steps.length > 0;
}

/**
 * 保存行内编辑：就地重读并更新受影响行，不重建列表、不收起面板，
 * 保存完焦点和光标仍留在文本框里，可以接着改或直接在光标处拆分。
 * @param {string} lineId 台词行 ID
 */
async function saveLine(lineId) {
  const panel = document.querySelector(`[data-edit-for="${CSS.escape(lineId)}"]`);
  if (!panel) return;
  const area = panel.querySelector('#le-text');
  // 记下光标位置，保存完把焦点放回文本框，接着改或直接拆分都不用手动再点
  const caret = area ? [area.selectionStart || 0, area.selectionEnd || 0] : null;
  const button = panel.querySelector('[data-act="save-line"]');
  if (button) {
    button.disabled = true;
    button.textContent = '保存中…';
  }
  state.saveState = 'saving';
  updateSaveBadge();
  try {
    await persistLineEdit(lineId);
    await refreshLinesInPlace();
    toast('已保存', 'ok');
    state.saveState = 'saved';
    updateSaveBadge();
  } catch (error) {
    toast(error.message, 'bad');
    state.saveState = 'idle';
    updateSaveBadge();
  } finally {
    if (button?.isConnected) {
      button.disabled = false;
      button.textContent = '保存';
    }
    if (area?.isConnected) {
      area.focus();
      if (caret) area.setSelectionRange(caret[0], caret[1]);
    }
  }
}

/** 单行重新生成 */
async function runOne(lineId) {
  if (state.runningLine) return toast('已有单行在生成，请稍候', 'warn');
  state.runningLine = lineId;
  repaintRows();
  try {
    await postJSON(
      '/api/ui/line/rerun',
      { path: state.path, chapter: state.chapterId, line: lineId },
      { timeout: 0 }
    );
    toast('这一行已重新生成', 'ok');
  } catch (error) {
    toast(error.message, 'bad');
  } finally {
    state.runningLine = '';
    await softRefresh();
  }
}

/**
 * 拆场景预览：先展示「原文段落 → 归属场景」，确认后再覆盖。
 * 数据来自 `scene split --impact`，不写盘；确认后依次重切场景与台词（保留人工改动）。
 */
async function openSceneSplitPreview() {
  if (!state.chapterId) return toast('先选择一个章节', 'warn');
  let impact;
  try {
    impact = await postJSON('/api/ui/scene/split/impact', {
      path: state.path,
      chapter: state.chapterId,
      keep_lines: true,
    });
  } catch (error) {
    toast(error.message, 'bad', 4000);
    return;
  }
  const scenes = impact?.preview?.scenes || [];
  const rebuilt = impact?.preview?.rebuilt || [];
  const mask = openDialog({
    title: '拆场景预览',
    wide: true,
    body: `
      ${modeSwitchHTML('scene.split', { fallback: 'regen' })}
      <div class="dlg-note">按地点与时间切分为 ${scenes.length} 场${
        rebuilt.length ? ` · 将覆盖 ${rebuilt.length} 个已有场景` : ''
      }</div>
      <div class="impact">
        ${scenes
          .map(
            (item) => `<div class="imp-row">
              <span style="min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
                <span class="mono" style="font-size:10px;color:var(--text-4)">${String(item.index + 1).padStart(3, '0')}</span>
                ${esc(item.excerpt || '（空场景）')}
              </span>
              <span class="badge accent">场景 ${item.index + 1} · ${esc(item.title)}</span>
            </div>`
          )
          .join('')}
        ${rebuilt
          .map(
            (item) => `<div class="imp-row" style="color:var(--warn)">
              <span>${esc(item.title || `场景 ${item.index + 1}`)} 的 ${item.lines} 行台词将按规则重建</span>
              <span class="badge warn">配音需重生成</span>
            </div>`
          )
          .join('')}
      </div>
      <div class="hint">确认后先重切场景（正文未变化的场景沿用原台词与配音），再重切台词（保留人工改写与新增行）。</div>`,
    foot: `<div class="right"><button class="btn" data-dlg-close>取消</button>
      <button class="btn primary" id="ss-ok">确认拆场景</button></div>`,
  });
  const okButton = mask.querySelector('#ss-ok');
  // 生成方式：只补缺失（只重建变化的场景 / 新增行）或全部重切
  const readMode = bindModeSwitch(mask, 'scene.split', {
    fallback: 'regen',
    action: okButton,
    labels: { fill: '只补缺失场景', regen: '重新切分全部' },
  });
  okButton.addEventListener('click', () => {
    const mode = readMode();
    closeDialog();
    void runSceneResplit(mode);
  });
}

/**
 * 执行重切场景 + 重切台词（两步），进度走统一浮层。
 * 正文未变化的场景沿用原台词与配音，保留人工改写。
 * @param {'fill'|'regen'} [mode] fill 只重建变化的场景与新增行；regen 强制全部重切
 */
async function runSceneResplit(mode = 'regen') {
  const regen = mode === 'regen';
  const panel = openProgress({
    title: regen ? '正在重新切分全部场景与台词…' : '正在补齐缺失的场景与台词…',
    hint: regen
      ? '先重切场景，再重切台词；会强制覆盖已有切分结果'
      : '先重切场景，再重切台词；正文未变化的场景与人工改写都会保留',
    stage: '第 1/2 步：重切场景…',
  });
  panel.update({ percent: 0 });
  try {
    await postJSON('/api/ui/scene/split', {
      path: state.path,
      chapter: state.chapterId,
      force: regen,
      keep_lines: true,
    });
    panel.update({ stage: '第 2/2 步：重切台词…', percent: 55 });
    const result = await postJSON('/api/ui/line/split', {
      path: state.path,
      chapter: state.chapterId,
      force: regen,
      keep_manual: true,
    });
    await loadChapterState();
    repaintPage();
    const message = `已拆场景：${Number(result?.scenes || 0)} 个场景 / ${Number(result?.lines || 0)} 行`;
    panel.note(message);
    panel.finish({
      message,
      detail: regen
        ? '全部场景与台词已重切，人工改写按规则保留。'
        : '只重建了变化的场景与新增行，其余台词与配音保持不变。',
      actions: [{ label: '查看台词', primary: true, onClick: () => repaintPage() }],
    });
    toast(message, 'ok');
  } catch (error) {
    panel.fail({ message: `拆场景失败：${error.message}`, retry: () => void runSceneResplit(mode) });
  }
}

/** 当前章节的场景下拉选项（按台词行出现顺序编号） */
function sceneChoices() {
  const seen = [];
  for (const row of state.lines) {
    if (row.scene_id && !seen.includes(row.scene_id)) seen.push(row.scene_id);
  }
  return seen.map((id, index) => ({ id, label: `第 ${index + 1} 场` }));
}

/** 拉取批量合并旁白的预览并渲染到弹窗 */
async function loadNarrationPreview(host, maxChars, sceneId) {
  host.innerHTML = `<div class="empty">正在计算合并预览…</div>`;
  try {
    const data = await postJSON('/api/ui/line/merge-narration', {
      path: state.path,
      chapter: state.chapterId,
      max_chars: maxChars,
      scene: sceneId || undefined,
      dry_run: true,
    });
    const counts = data?.counts || {};
    if (!counts.groups) {
      host.innerHTML = `<div class="empty">没有可合并的相邻旁白（需要同场景内连续两段以上旁白）</div>`;
      return;
    }
    host.innerHTML = `
      <div class="impact">
        <div class="imp-row"><span>合并预览</span><b>${counts.lines_before} 段 → ${counts.lines_after} 段</b></div>
        ${(data.groups || [])
          .slice(0, 6)
          .map(
            (item) => `<div class="imp-row">
              <span style="min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
                ${esc(item.scene_title || '')} · ${item.line_ids.length} 段 / ${item.chars} 字
              </span>
              <span style="max-width:46%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--text-3)">
                ${esc((item.text || '').slice(0, 40))}
              </span>
            </div>`
          )
          .join('')}
      </div>`;
  } catch (error) {
    host.innerHTML = `<div class="dlg-note bad">${esc(error.message)}</div>`;
  }
}

/** 批量合并旁白：参数弹窗 + 实时预览，确认后一次提交 */
function openMergeNarration() {
  if (!state.chapterId) return toast('先选择一个章节', 'warn');
  const scenes = sceneChoices();
  const mask = openDialog({
    title: '批量合并旁白',
    body: `<div class="dlg-note">把相邻旁白合并成更长的段落，减少碎片感。角色台词与内心独白不会被合并。</div>
      <div class="grid">
        <div class="field"><label>期望字数 / 段</label>
          <input class="input" id="mn-chars" type="number" min="20" step="10" value="120" /></div>
        <div class="field"><label>合并范围</label>
          <select class="select" id="mn-scene">
            <option value="">本章全部场景</option>
            ${scenes.map((item) => `<option value="${esc(item.id)}">${esc(item.label)}</option>`).join('')}
          </select></div>
      </div>
      <div id="mn-preview" style="margin-top:10px"></div>
      <div class="hint">覆盖所选范围内的相邻旁白；被合并行已有配音会失去引用（文件仍保留在磁盘）。</div>`,
    foot: `<div class="right"><button class="btn" data-dlg-close>取消</button>
      <button class="btn primary" id="mn-ok">合并</button></div>`,
  });
  const chars = mask.querySelector('#mn-chars');
  const scene = mask.querySelector('#mn-scene');
  const host = mask.querySelector('#mn-preview');
  const target = () => Math.max(20, Number(chars.value) || 120);
  const refresh = () => loadNarrationPreview(host, target(), scene.value);
  refresh();
  chars.addEventListener('change', refresh);
  scene.addEventListener('change', refresh);
  mask.querySelector('#mn-ok').addEventListener('click', () => {
    const maxChars = target();
    const sceneId = scene.value || undefined;
    closeDialog();
    void runMergeNarration(maxChars, sceneId);
  });
}

/**
 * 执行批量合并旁白，进度走统一浮层。
 * @param {number} maxChars 期望每段字数
 * @param {string|undefined} sceneId 限定场景
 */
async function runMergeNarration(maxChars, sceneId) {
  const panel = openProgress({
    title: '正在合并相邻旁白…',
    hint: `目标每段约 ${maxChars} 字${sceneId ? ' · 仅当前场景' : ' · 本章全部场景'}`,
    stage: '正在重排旁白段落…',
  });
  panel.update({ percent: null });
  try {
    const result = await postJSON('/api/ui/line/merge-narration', {
      path: state.path,
      chapter: state.chapterId,
      max_chars: maxChars,
      scene: sceneId,
      dry_run: false,
    });
    state.selected = new Set();
    await softRefresh();
    const message = `已合并 ${Number(result?.counts?.lines_before || 0)} 段旁白为 ${Number(
      result?.counts?.lines_after || 0
    )} 段`;
    panel.finish({
      message,
      detail: '被合并行已有配音会失去引用（文件仍保留在磁盘）。',
      actions: [{ label: '查看台词', primary: true, onClick: () => repaintPage() }],
    });
    toast(message, 'ok');
  } catch (error) {
    panel.fail({ message: `合并旁白失败：${error.message}`, retry: () => void runMergeNarration(maxChars, sceneId) });
  }
}

/**
 * 在行内编辑面板的光标处拆分当前行。
 * 文本框里有未保存改词时先静默落盘一次（不弹提示、不打断操作），再按已保存文本的
 * 偏移直接拆分；拆分后只原位更新原行、插入新行，并把焦点交给新行开头。
 * @param {string} lineId 台词行 ID
 */
async function splitAtCursor(lineId) {
  const area = document.getElementById('le-text');
  const row = state.lines.find((item) => item.line_id === lineId);
  if (!area || !row) return;
  const offset = Number(area.selectionStart || 0);
  if (offset <= 0 || offset >= area.value.length) {
    return toast('把光标放到台词中间再拆分', 'warn');
  }
  const button = area.closest('.seg-edit')?.querySelector('[data-act="split-at-cursor"]');
  if (button) button.disabled = true;
  try {
    let savedOffset = offset;
    if (area.value !== String(row.text || '')) {
      // 未保存改词先落盘：只写一次、不弹提示，保证拆分偏移与落盘文本一致
      state.saveState = 'saving';
      updateSaveBadge();
      await persistLineEdit(lineId);
      // `line edit` 会去掉首尾空白，按差值把光标位置换算到已保存文本上
      const lead = area.value.length - area.value.replace(/^\s+/, '').length;
      savedOffset = offset - lead;
      if (savedOffset <= 0 || savedOffset >= String(row.text || '').length) {
        return toast('把光标放到台词中间再拆分', 'warn');
      }
    }
    const result = await postJSON('/api/ui/line/split-at', {
      path: state.path,
      chapter: state.chapterId,
      line: lineId,
      offset: savedOffset,
    });
    await applySplitRows(lineId, result?.new_line_id);
    state.saveState = 'saved';
    updateSaveBadge();
    toast(
      `已拆分为两行（${String(result.first_text || '').length} 字 + ${String(result.second_text || '').length} 字）`,
      'ok'
    );
  } catch (error) {
    toast(error.message, 'bad', 4000);
  } finally {
    if (button?.isConnected) button.disabled = false;
  }
}

/**
 * 拆分后的局部刷新：重读本章台词，只原位更新已有行、插入新行并移除消失的行，
 * 不重建列表；随后收起原行面板、展开新行并把光标放到开头，可直接接着输入。
 * @param {string} lineId 拆分前的原行 ID
 * @param {string} newLineId 拆分出的新行 ID
 */
async function applySplitRows(lineId, newLineId) {
  const body = document.getElementById('ed-body');
  const top = body?.scrollTop ?? 0;
  const payload = await getJSON('/api/ui/lines', { path: state.path, chapter: state.chapterId });
  state.lines = payload?.lines || [];
  state.counts = payload?.counts || {};
  state.consistency = payload?.consistency || null;
  // 先标记展开新行，新节点插入时直接带上编辑面板
  state.expandedLine = newLineId || '';
  reconcileRowNodes();
  // 原行的面板收起，避免两个 #le-text 同时存在
  document.querySelector(`[data-block="${CSS.escape(lineId)}"] .seg-edit`)?.remove();
  paintFilterCounts();
  repaintBanner();
  repaintTask();
  if (state.view !== 'script') refreshActiveView();
  if (body) body.scrollTop = top;
  const field = newLineId
    ? document.querySelector(`[data-block="${CSS.escape(newLineId)}"] #le-text`)
    : null;
  if (field) {
    field.focus();
    field.setSelectionRange(0, 0);
  }
}

/** 选中行合并：先校验同场景且相邻，再二次确认后提交 */
function confirmMergeSelected() {
  const rows = [...state.selected]
    .map((id) => state.lines.find((row) => row.line_id === id))
    .filter(Boolean)
    .sort((a, b) => Number(a.index || 0) - Number(b.index || 0));
  if (rows.length < 2) return toast('至少选择相邻的两行', 'warn');
  if (new Set(rows.map((row) => row.scene_id)).size > 1) return toast('只能合并同一场景内的行', 'warn');
  const indexes = rows.map((row) => Number(row.index || 0));
  if (!indexes.every((value, index) => index === 0 || value === indexes[index - 1] + 1)) {
    return toast('只能合并相邻的行（中间不能跳过行）', 'warn');
  }
  const lost = rows.slice(1).filter((row) => row.audio).length;
  const mask = openDialog({
    title: '合并相邻行',
    body: `<div class="dlg-note">将把选中的 ${rows.length} 行合并为一行，文本取正文里首行开头到末行结尾的原文片段。
      ${lost ? `被合并行里有 ${lost} 条已生成配音，会失去引用（文件仍保留在磁盘，可在版本记录里找回）。` : ''}</div>
      <div class="summary-line"><span>合并后说话人 / 情绪</span>
        <span class="right">${esc(rows[0].speaker || '')} · ${esc(EMOTION_LABEL[rows[0].emotion] || rows[0].emotion || '平静')}</span></div>
      <div class="summary-line"><span>合并后文本</span>
        <span class="right" style="max-width:62%;text-align:right">${esc(rows.map((row) => row.text || '').join(' ').slice(0, 120))}</span></div>`,
    foot: `<div class="right"><button class="btn" data-dlg-close>取消</button>
      <button class="btn primary" id="lm-ok">合并 ${rows.length} 行</button></div>`,
  });
  mask.querySelector('#lm-ok').addEventListener('click', async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      const result = await postJSON('/api/ui/line/merge', {
        path: state.path,
        chapter: state.chapterId,
        lines: rows.map((row) => row.line_id),
      });
      closeDialog();
      state.selected = new Set();
      await softRefresh();
      const lostAudio = Number(result?.merged?.lost_audio || 0);
      toast(`已合并为 ${result?.merged?.line_id || '一行'}${lostAudio ? `，${lostAudio} 条配音失去引用` : ''}`, 'ok');
    } catch (error) {
      toast(error.message, 'bad', 4000);
      button.disabled = false;
    }
  });
}

/**
 * 行尾「⋯」菜单：与参考一致，所有行级操作都从这一个入口进入。
 * 顺序对齐参考：试听 → 向上合并 → 向下合并 → 说话情绪 → 段末停顿 → 在线配音 → 配音记录；
 * 本地另有两项：复制文本、删除本条音频。
 * @param {string} lineId 台词行 ID
 * @param {HTMLElement} anchor 触发按钮
 */
function openLineMenu(lineId, anchor) {
  const row = state.lines.find((item) => item.line_id === lineId);
  if (!row) return;
  const index = state.lines.findIndex((item) => item.line_id === lineId);
  const playable = Boolean(audioOf(row));
  openMenu(
    anchor,
    [
      { key: 'play', label: '试听这一行', disabled: !playable },
      { key: 'merge-up', label: '向上合并', disabled: index <= 0 },
      { key: 'merge-down', label: '向下合并', disabled: index < 0 || index >= state.lines.length - 1 },
      { key: 'emotion', label: '说话情绪' },
      { key: 'pause', label: '段末停顿' },
      { key: 'rerun', label: '生成配音' },
      { key: 'versions', label: '配音记录' },
      { key: 'copy', label: '复制文本' },
      { sep: true },
      { key: 'delete', label: '删除本条音频', danger: true, disabled: !row.audio },
    ],
    (key) => {
      if (key === 'play') playRow(row);
      else if (key === 'merge-up') mergeWithNeighbor(lineId, 'up');
      else if (key === 'merge-down') mergeWithNeighbor(lineId, 'down');
      else if (key === 'emotion') focusLineField(lineId, '#le-emotion');
      else if (key === 'pause') focusLineField(lineId, '#le-pause-after');
      else if (key === 'rerun') runOne(lineId);
      else if (key === 'versions') openVersions(row);
      else if (key === 'copy') copyLineText(row);
      else if (key === 'delete') confirmDeleteAudio(row);
    }
  );
}

/**
 * 菜单里的「向上合并 / 向下合并」：把当前行与相邻行组成选择集，
 * 复用同一套「只合并同场景相邻行」的校验与二次确认，不另写一套合并逻辑。
 * @param {string} lineId 当前行
 * @param {'up'|'down'} direction 合并方向
 */
function mergeWithNeighbor(lineId, direction) {
  const index = state.lines.findIndex((item) => item.line_id === lineId);
  if (index < 0) return;
  const other = state.lines[direction === 'up' ? index - 1 : index + 1];
  if (!other) return toast(direction === 'up' ? '上面没有可合并的行' : '下面没有可合并的行', 'warn');
  state.selected = new Set([lineId, other.line_id]);
  confirmMergeSelected();
}

/**
 * 打开行内编辑面板并把焦点放到指定字段（说话情绪 / 段末停顿共用）。
 * @param {string} lineId 台词行 ID
 * @param {string} fieldSelector 面板内字段选择器
 */
function focusLineField(lineId, fieldSelector) {
  state.expandedLine = lineId;
  repaintRows();
  const box = document.querySelector(`[data-edit-for="${CSS.escape(lineId)}"]`);
  if (!box) return;
  box.scrollIntoView({ block: 'nearest' });
  const field = box.querySelector(fieldSelector);
  if (!field) return;
  field.focus();
  if (typeof field.select === 'function') field.select();
}

/** 复制该行台词文本；剪贴板不可用时退回 execCommand */
async function copyLineText(row) {
  const text = String(row.text || '');
  try {
    await navigator.clipboard.writeText(text);
    toast('台词已复制', 'ok');
  } catch {
    const area = document.createElement('textarea');
    area.value = text;
    area.style.position = 'fixed';
    area.style.opacity = '0';
    document.body.appendChild(area);
    area.select();
    document.execCommand('copy');
    area.remove();
    toast('台词已复制', 'ok');
  }
}

/** 版本记录：列出该行的历史音频，可逐条试听 */
async function openVersions(row) {
  const mask = openDialog({
    title: '版本记录',
    body: `<div id="ver-body"><div class="empty">${icon('refresh', 18)}<span>正在读取版本…</span></div></div>`,
    foot: `<div class="right"><button class="btn" data-dlg-close>关闭</button></div>`,
  });
  const body = mask.querySelector('#ver-body');
  try {
    const payload = await getJSON('/api/ui/line/versions', {
      path: state.path,
      chapter: state.chapterId,
      line: row.line_id,
    });
    const versions = payload?.versions || [];
    if (!versions.length) {
      body.innerHTML = `<div class="empty"><span>这一行还没有历史音频</span></div>`;
      return;
    }
    body.innerHTML = versions
      .map(
        (item) => `
        <div class="summary-line">
          <span>r${item.revision}${item.active ? '（当前生效）' : ''}</span>
          <span class="right" style="display:flex;gap:8px;align-items:center">
            <span class="mono" style="color:var(--text-4)">${fmtBytes(item.size)}</span>
            <button class="btn sm" data-ver="${esc(item.path)}">试听</button>
          </span>
        </div>`
      )
      .join('');
    body.querySelectorAll('[data-ver]').forEach((button) => {
      button.addEventListener('click', () => {
        const audio = new Audio(fileUrl(button.dataset.ver, state.path));
        audio.play().catch((error) => toast(`播放失败：${error.message}`, 'bad'));
      });
    });
  } catch (error) {
    body.innerHTML = `<div class="dlg-note bad">${esc(error.message)}</div>`;
  }
}

/** 删除该行生效音频：确认后清空 tts 记录，行回到待配 */
function confirmDeleteAudio(row) {
  const mask = openDialog({
    title: '删除本条音频',
    body: `<div class="dlg-note warn">将删除这一行当前生效的音频，行状态回到「待配」。历史版本文件仍保留，可在版本记录里试听。</div>
      <div class="summary-line"><span>台词</span><span class="right" style="max-width:60%;text-align:right">${esc(row.text || '')}</span></div>`,
    foot: `<div class="right"><button class="btn" data-dlg-close>取消</button>
      <button class="btn danger" id="del-audio-ok">删除音频</button></div>`,
  });
  mask.querySelector('#del-audio-ok').addEventListener('click', async (event) => {
    event.currentTarget.disabled = true;
    try {
      await postJSON('/api/ui/line/audio/delete', {
        path: state.path,
        chapter: state.chapterId,
        line: row.line_id,
      });
      closeDialog();
      toast('已删除该行音频', 'ok');
      await softRefresh();
    } catch (error) {
      toast(error.message, 'bad');
      event.currentTarget.disabled = false;
    }
  });
}

/** 查看 / 编辑原文：只读展示本章正文，可切换为编辑并保存 */
async function openOriginal() {
  if (!state.chapterId) return toast('先选择一个章节', 'warn');
  const mask = openDialog({
    title: `查看原文 · ${currentChapter()?.title || ''}`,
    wide: true,
    body: `<div id="orig-body" class="orig-body"><div class="empty">${icon(
      'refresh',
      18
    )}<span>正在读取正文…</span></div></div>`,
    foot: `<div class="right"><button class="btn" data-dlg-close>关闭</button>
      <button class="btn primary" id="orig-edit" disabled>编辑正文</button></div>`,
  });
  const editButton = mask.querySelector('#orig-edit');
  let content = '';
  try {
    const chapter = await getJSON(`/api/project/chapter/${encodeURIComponent(state.chapterId)}`, {
      path: state.path,
    });
    content = chapter?.content || '';
    const body = mask.querySelector('#orig-body');
    if (body) {
      body.innerHTML = content ? esc(content) : '<div class="empty"><span>本章还没有正文</span></div>';
    }
    if (editButton) editButton.disabled = false;
  } catch (error) {
    const body = mask.querySelector('#orig-body');
    if (body) body.innerHTML = `<div class="empty"><span>${esc(error.message)}</span></div>`;
    return;
  }
  editButton?.addEventListener('click', () => {
    const body = mask.querySelector('#orig-body');
    if (!body) return;
    body.classList.remove('orig-body');
    body.innerHTML = `<textarea class="textarea" id="orig-text" style="min-height:40vh">${esc(content)}</textarea>
      <div class="hint" id="orig-status" style="margin-top:8px">修改后自动保存；台词行需要重新切分才会同步</div>`;
    editButton.disabled = true;
    // 自动保存：停止输入 1.2s 落盘，正文较长时也不会每次按键都写
    let autoTimer = 0;
    const status = mask.querySelector('#orig-status');
    const saveContent = async (text) => {
      await postJSON('/api/ui/chapter/save', {
        path: state.path,
        chapter: state.chapterId,
        content: text,
      });
      content = text;
    };
    mask.querySelector('#orig-text')?.addEventListener('input', (event) => {
      const next = event.target.value;
      if (status) status.textContent = '待保存…';
      if (autoTimer) window.clearTimeout(autoTimer);
      autoTimer = window.setTimeout(async () => {
        try {
          await saveContent(next);
          if (status) {
            status.textContent = `已自动保存 ${new Date().toLocaleTimeString('zh-CN', { hour12: false })}`;
          }
        } catch (error) {
          if (status) status.textContent = `保存失败：${error.message}`;
        }
      }, 1200);
    });
    mask.querySelector('[data-dlg-close]')?.addEventListener('click', () => {
      if (autoTimer) window.clearTimeout(autoTimer);
      window.setTimeout(() => softRefresh(), 60);
    });
    const foot = mask.querySelector('.right');
    if (foot) {
      foot.insertAdjacentHTML(
        'afterbegin',
        `<button class="btn primary" id="orig-save">保存正文</button>
         <button class="btn" id="orig-reset">放弃修改</button>`
      );
      foot.querySelector('#orig-reset')?.addEventListener('click', () => closeDialog());
      foot.querySelector('#orig-save')?.addEventListener('click', async (event) => {
        const next = mask.querySelector('#orig-text')?.value ?? '';
        const button = event.currentTarget;
        if (autoTimer) window.clearTimeout(autoTimer);
        button.disabled = true;
        button.textContent = '保存中…';
        try {
          await saveContent(next);
          closeDialog();
          toast('正文已保存；重新切分本章后改动才会进入台词行', 'ok');
          await softRefresh();
        } catch (error) {
          toast(error.message, 'bad');
          button.disabled = false;
          button.textContent = '保存正文';
        }
      });
    }
  });
}

/** 查找替换浮层：本章范围内替换台词文本（走 line edit 同步正文） */
function openFindReplace() {
  const mask = openDialog({
    title: '查找替换',
    body: `<div class="field"><label>查找</label>
        <input class="input" id="fr-find" placeholder="要查找的文字" /></div>
      <div class="field"><label>替换为</label>
        <input class="input" id="fr-replace" placeholder="留空表示删除" /></div>
      <div class="hint" id="fr-count">范围：本章全部台词行</div>`,
    foot: `<div class="right"><button class="btn" data-dlg-close>取消</button>
      <button class="btn primary" id="fr-ok">全部替换</button></div>`,
  });
  const findInput = mask.querySelector('#fr-find');
  const countNode = mask.querySelector('#fr-count');
  /** 统计命中行数（前端预演，最终以接口返回为准） */
  const recount = () => {
    const word = findInput.value;
    if (!word) {
      countNode.textContent = '范围：本章全部台词行';
      return;
    }
    const hits = state.lines.filter((row) => String(row.text || '').includes(word)).length;
    countNode.textContent = `本章命中 ${hits} 行`;
  };
  findInput.addEventListener('input', recount);
  findInput.focus();
  mask.querySelector('#fr-ok').addEventListener('click', async (event) => {
    const find = findInput.value;
    if (!find) return toast('先填写查找内容', 'warn');
    event.currentTarget.disabled = true;
    event.currentTarget.textContent = '替换中…';
    const replace = mask.querySelector('#fr-replace').value;
    // 先按当前文本算出替换前后，供整批撤销
    const before = {};
    const after = {};
    for (const row of state.lines) {
      const old = String(row.text || '');
      if (!old.includes(find)) continue;
      before[row.line_id] = old;
      after[row.line_id] = old.split(find).join(replace);
    }
    try {
      const result = await postJSON('/api/ui/lines/replace', {
        path: state.path,
        chapter: state.chapterId,
        find,
        replace,
      });
      if (result.replaced > 0 && Object.keys(after).length) {
        pushHistory({ label: '查找替换', steps: [{ kind: 'text', before, after }] });
      }
      closeDialog();
      toast(`已替换 ${result.replaced} 行${result.failed ? `，失败 ${result.failed} 行` : ''}`, result.failed ? 'warn' : 'ok');
      await softRefresh();
    } catch (error) {
      toast(error.message, 'bad');
      event.currentTarget.disabled = false;
      event.currentTarget.textContent = '全部替换';
    }
  });
}

/** 批量配音：先取计划，再确认启动 */
async function openBatchDialog(scope = 'chapter') {
  const body = `
    <div id="batch-plan-body"><div class="empty">${icon('refresh', 18)}<span>正在计算本次要生成的行…</span></div></div>
    <div class="checkline"><input type="checkbox" id="batch-scope-all" ${scope === 'all' ? 'checked' : ''} />
      <span>范围：勾选后配整本，默认只配本章</span></div>`;
  const mask = openDialog({
    title: '生成配音',
    wide: true,
    body,
    foot: `<div class="right">
      <button class="btn" data-dlg-close>取消</button>
      <button class="btn primary" id="batch-start" disabled>开始生成</button>
    </div>`,
  });
  const planBody = mask.querySelector('#batch-plan-body');
  const startBtn = mask.querySelector('#batch-start');
  let plan = null;

  const fetchPlan = async () => {
    const all = mask.querySelector('#batch-scope-all').checked;
    planBody.innerHTML = `<div class="empty">${icon('refresh', 18)}<span>正在计算…</span></div>`;
    startBtn.disabled = true;
    try {
      plan = await getJSON('/api/ui/tts/batch-all/plan', {
        path: state.path,
        scope: all ? 'all' : 'chapter',
        chapter: all ? undefined : state.chapterId,
      });
      const blockers = plan.blockers || [];
      planBody.innerHTML = `
        ${plan.ready ? '' : `<div class="dlg-note bad">${esc(plan.ready_reason || '配音模型未就绪')}</div>`}
        ${blockers.length ? `<div class="dlg-note bad">阻断项：<br />${blockers.map((item) => `· ${esc(item)}`).join('<br />')}</div>` : ''}
        <div class="summary-line"><span>本次预计生成</span><span class="right">${plan.total} 行</span></div>
        <div class="summary-line"><span>可跳过（已有音频）</span><span class="right">${plan.skipped} 行</span></div>
        ${plan.unknown_total ? `<div class="summary-line"><span>说话人未识别</span><span class="right" style="color:var(--warn)">${plan.unknown_total} 行</span></div>` : ''}
        ${plan.eta_ms ? `<div class="summary-line"><span>预计耗时</span><span class="right">${fmtClock(plan.eta_ms)}</span></div>` : ''}
        ${impactListHTML(plan, state.characters)}
        ${
          plan.unknown_total
            ? `<label class="checkline"><input type="checkbox" id="batch-skip-unknown" />
                 <span>跳过这 ${plan.unknown_total} 行未识别说话人的台词，先配其余行</span></label>`
            : ''
        }
        <div class="dlg-note">只补齐未生成 / 过期 / 失败的行；停止后可从这里继续。</div>`;
      startBtn.disabled = !plan.ready || Boolean(blockers.length) || Number(plan.total) <= 0;
    } catch (error) {
      planBody.innerHTML = `<div class="dlg-note bad">${esc(error.message)}</div>`;
    }
  };

  mask.querySelector('#batch-scope-all').addEventListener('change', fetchPlan);
  startBtn.addEventListener('click', async () => {
    if (!plan) return;
    const all = mask.querySelector('#batch-scope-all').checked;
    const skip = Boolean(mask.querySelector('#batch-skip-unknown')?.checked);
    startBtn.disabled = true;
    startBtn.textContent = '启动中…';
    try {
      await postJSON('/api/ui/tts/batch-all', {
        path: state.path,
        scope: all ? 'all' : 'chapter',
        chapter: all ? undefined : state.chapterId,
        skip_unknown: skip,
      });
      closeDialog();
      toast('已开始批量配音，进度见右侧任务面板', 'ok');
      watchProgress({
        path: state.path,
        title: '正在补齐缺失配音…',
        hint: `本次 ${Number(plan.total || 0)} 行 · 跳过 ${Number(plan.skipped || 0)} 行`,
      });
      await refreshTask();
      startPolling();
    } catch (error) {
      toast(error.message, 'bad');
      startBtn.disabled = false;
      startBtn.textContent = '开始生成';
    }
  });
  await fetchPlan();
}

/** 重新生成本章（含已就绪行，需二次确认） */
function confirmRegenerate() {
  const c = counts();
  const sceneOrder = [];
  for (const row of state.lines) {
    if (row.scene_id && !sceneOrder.includes(row.scene_id)) sceneOrder.push(row.scene_id);
  }
  const items = state.lines.map((row) => ({
    chapter_id: state.chapterId,
    chapter_title: currentChapter()?.title,
    scene_index: sceneOrder.indexOf(row.scene_id) + 1,
    speaker_id: row.speaker_id,
    text: row.text,
    has_audio: Boolean(row.audio),
  }));
  const plan = { total: c.total, items, assets: items.filter((item) => item.has_audio).length };
  const mask = openDialog({
    title: '重新生成本章',
    body: `<div class="dlg-note warn">本章 ${c.total} 行将全部重新合成，已配好的音频会被新版本替换（旧版本仍可在版本记录里找回）。</div>
      <div class="summary-line"><span>当前章节</span><span class="right">${esc(currentChapter()?.title || '')}</span></div>
      ${impactListHTML(plan, state.characters)}`,
    foot: `<div class="right"><button class="btn" data-dlg-close>取消</button>
      <button class="btn danger" id="regen-ok">确认全部重配</button></div>`,
  });
  mask.querySelector('#regen-ok').addEventListener('click', async (event) => {
    event.currentTarget.disabled = true;
    try {
      await postJSON('/api/ui/generate', { path: state.path, chapter: state.chapterId, mode: 'regenerate' });
      closeDialog();
      toast('已开始重新生成', 'ok');
      await refreshTask();
      startPolling();
    } catch (error) {
      toast(error.message, 'bad');
      event.currentTarget.disabled = false;
    }
  });
}

/** 停止批量生成 */
async function stopGeneration() {
  try {
    await postJSON('/api/ui/stop', { path: state.path });
    toast('已停止生成', 'ok');
    await refreshTask();
  } catch (error) {
    toast(error.message, 'bad');
  }
}

/** 暂停 / 继续批量生成（任务进程保留，继续后从下一行接着跑） */
async function togglePause() {
  try {
    const paused = !state.paused;
    await postJSON('/api/ui/tts/pause', { path: state.path, paused });
    state.paused = paused;
    toast(paused ? '已暂停：当前行完成后续跑' : '已继续生成', 'ok');
    await refreshTask();
  } catch (error) {
    toast(error.message, 'bad');
  }
}

/** 新建章节 */
function openNewChapter() {
  const mask = openDialog({
    title: '新建章节',
    body: `<div class="field"><label>章节标题</label><input class="input" id="nc-title" placeholder="例如：第一章 雨夜归人" /></div>
      <div class="dlg-note">新章节为空白稿，需要在旧版界面写入正文并切分台词后才能配音。</div>`,
    foot: `<div class="right"><button class="btn" data-dlg-close>取消</button>
      <button class="btn primary" id="nc-ok">创建</button></div>`,
  });
  mask.querySelector('#nc-ok').addEventListener('click', async (event) => {
    const title = mask.querySelector('#nc-title').value.trim();
    if (!title) return toast('请填写章节标题', 'warn');
    event.currentTarget.disabled = true;
    try {
      const chapter = await postJSON('/api/ui/chapter/add', { path: state.path, title });
      closeDialog();
      toast('章节已创建', 'ok');
      goChapter(state.path, chapter.id);
    } catch (error) {
      toast(error.message, 'bad');
      event.currentTarget.disabled = false;
    }
  });
}

// ---------------- 播放 ----------------

const PLAYER_IDLE = '整章试听会按顺序播放已配好的行';

/** 更新底栏播放文案 */
function setPlayerLabel(text) {
  const el = document.getElementById('player-cur');
  if (el) el.textContent = text;
}

function playRow(row) {
  const rel = audioOf(row);
  if (!rel) return;
  if (state.playingLine === row.line_id && state.player.audio) {
    stopPlayer();
    setPlayerLabel(PLAYER_IDLE);
    repaintRows();
    return;
  }
  stopPlayer();
  const audio = new Audio(fileUrl(rel, state.path));
  state.player.audio = audio;
  state.playingLine = row.line_id;
  state.player.playing = true;
  state.player.index = -1;
  setPlayerLabel(`正在试听：${row.speaker || '当前行'}`);
  audio.onended = () => {
    state.playingLine = '';
    state.player.playing = false;
    setPlayerLabel(PLAYER_IDLE);
    repaintRows();
  };
  audio.onerror = () => {
    toast('音频加载失败', 'bad');
    state.playingLine = '';
    state.player.playing = false;
    setPlayerLabel(PLAYER_IDLE);
    repaintRows();
  };
  audio.play().catch((error) => {
    toast(`播放失败：${error.message}`, 'bad');
    state.playingLine = '';
    state.player.playing = false;
    setPlayerLabel(PLAYER_IDLE);
    repaintRows();
  });
  repaintRows();
}

function playAll() {
  const ready = state.lines.filter(audioOf);
  if (!ready.length) return toast('本章还没有已配好的行', 'warn');
  // 正在整章播放时再次点击 = 暂停；正在单行试听则直接切到整章
  if (state.player.playing && state.player.index >= 0) {
    stopPlayer();
    setPlayerLabel(PLAYER_IDLE);
    repaintRows();
    return;
  }
  stopPlayer();
  playQueue(ready, 0);
}

function playQueue(list, index) {
  if (index >= list.length) {
    stopPlayer();
    setPlayerLabel(PLAYER_IDLE);
    repaintRows();
    return;
  }
  const row = list[index];
  const audio = new Audio(fileUrl(audioOf(row), state.path));
  state.player.audio = audio;
  state.player.playing = true;
  state.player.index = index;
  state.playingLine = row.line_id;
  setPlayerLabel(`正在播放第 ${index + 1} / ${list.length} 段`);
  audio.onended = () => playQueue(list, index + 1);
  audio.onerror = () => playQueue(list, index + 1);
  audio.play().catch(() => playQueue(list, index + 1));
}

// ---------------- 轮询 ----------------

async function refreshTask() {
  try {
    const [progress, pause] = await Promise.all([
      getJSON('/api/ui/tts/batch-all/progress', { path: state.path }),
      getJSON('/api/ui/tts/pause', { path: state.path }),
    ]);
    state.task = progress;
    state.paused = Boolean(pause?.paused);
  } catch {
    state.task = null;
    state.paused = false;
  }
  repaintTask();
  return state.task;
}

let lastRunning = false;

function startPolling() {
  stopPolling();
  lastRunning = Boolean(state.task?.running);
  pollTimer = window.setInterval(async () => {
    if (!document.getElementById('task-panel')) {
      stopPolling();
      return;
    }
    const task = await refreshTask();
    const running = Boolean(task?.running);
    if (running || lastRunning) await softRefresh();
    if (lastRunning && !running) {
      const failed = Number(task?.failed || 0);
      toast(failed ? `配音结束，${failed} 行失败` : '配音完成', failed ? 'warn' : 'ok');
    }
    lastRunning = running;
  }, 1800);
}

// ---------------- 章节下拉 ----------------

/** 当前打开的章节下拉浮层 */
let chapterPicker = null;

/** 关闭章节下拉 */
function closeChapterPicker() {
  if (!chapterPicker) return;
  chapterPicker.remove();
  chapterPicker = null;
  document.removeEventListener('mousedown', onPickerDown, true);
  document.removeEventListener('keydown', onPickerKey, true);
}

/** 点击浮层外部关闭（捕获阶段，先于业务点击） */
function onPickerDown(event) {
  if (!chapterPicker) return;
  if (event.target.closest('.chap-pop')) return;
  if (event.target.closest('[data-act="chapter-picker"]')) return;
  closeChapterPicker();
}

/** Esc 关闭 */
function onPickerKey(event) {
  if (event.key === 'Escape') closeChapterPicker();
}

/**
 * 打开章节下拉（参考形态：搜索框 + 取消 + 章节列表）。
 * 点击某个章节直接切换，当前章节高亮。
 * @param {HTMLElement} anchor 触发按钮（顶栏「作品名 / 章节名」）
 */
function openChapterPicker(anchor) {
  if (chapterPicker) {
    closeChapterPicker();
    return;
  }
  const pop = document.createElement('div');
  pop.className = 'chap-pop';
  pop.innerHTML = `
    <div class="cp-pop-head">
      <div class="search">${icon('search', 14)}<input id="cp-pop-search" type="search" placeholder="搜索章节" /></div>
      <button class="btn sm" data-cp-close>取消</button>
    </div>
    <div class="cp-pop-list" id="cp-pop-list"></div>`;
  document.body.appendChild(pop);
  chapterPicker = pop;
  const rect = anchor.getBoundingClientRect();
  const width = 360;
  pop.style.left = `${Math.max(8, Math.min(rect.left, window.innerWidth - width - 8))}px`;
  pop.style.top = `${rect.bottom + 6}px`;

  const list = pop.querySelector('#cp-pop-list');
  const paint = (keyword = '') => {
    const key = keyword.trim().toLowerCase();
    const rows = state.chapters.filter(
      (chapter) => !key || String(chapter.title || '').toLowerCase().includes(key)
    );
    list.innerHTML = rows.length
      ? rows
          .map(
            (chapter) => `
        <button class="cp-pop-row ${chapter.id === state.chapterId ? 'on' : ''}" data-cp-id="${esc(chapter.id)}">
          <span class="cn">${esc(chapter.title || `第 ${chapter.index} 章`)}</span>
          <span class="cw">${chapter.word_count ? `${chapter.word_count} 字` : '未写'}</span>
        </button>`
          )
          .join('')
      : '<div class="cp-pop-empty">没有匹配的章节</div>';
  };
  paint();
  const input = pop.querySelector('#cp-pop-search');
  input.addEventListener('input', () => paint(input.value));
  input.focus();
  pop.addEventListener('click', (event) => {
    if (event.target.closest('[data-cp-close]')) {
      closeChapterPicker();
      return;
    }
    const row = event.target.closest('[data-cp-id]');
    if (!row) return;
    const id = row.dataset.cpId;
    closeChapterPicker();
    if (id !== state.chapterId) goChapter(state.path, id);
  });
  document.addEventListener('mousedown', onPickerDown, true);
  document.addEventListener('keydown', onPickerKey, true);
}

// ---------------- 事件 ----------------

async function onRootClick(event) {
  // 监听绑在共享根节点上：离开画本页后不再处理，避免抢走其它页面的同名操作。
  // 录音 / 后期 / 对轨视图没有 #ed-body，用 #vw-main 一起判定，否则这些视图里
  // 顶栏的返回、视图切换、更多视图、撤销重做、章节切换都会点了没反应。
  if (!document.getElementById('ed-body') && !document.getElementById('vw-main')) return;
  const chapterRow = event.target.closest('[data-chapter]');
  if (chapterRow) {
    goChapter(state.path, chapterRow.dataset.chapter);
    return;
  }
  const retry = event.target.closest('[data-retry-line]');
  if (retry) {
    await runOne(retry.dataset.retryLine);
    return;
  }
  const filter = event.target.closest('[data-filter]');
  if (filter) {
    state.filter = filter.dataset.filter;
    repaintRows();
    return;
  }
  const speakerSelect = event.target.closest('.spk-select');
  if (speakerSelect) return; // 交给 change 事件
  const rowEl = event.target.closest('[data-line-id]');
  // 行内编辑面板在 .seg-row 之外，需要从外层 .seg-block 取行 ID
  const blockEl = event.target.closest('[data-block]');

  const button = event.target.closest('[data-act]');
  if (!button) {
    // 时间轴音频块没有 data-act，单独处理
    const item = event.target.closest('[data-tl-asset]');
    if (item) {
      const wave = item.querySelector('.tl-wave');
      if (wave) loadPeaks(item.dataset.tlAsset, wave);
      playTimelineItem(item.dataset.tlAsset, item.dataset.tlStart);
      return;
    }
    // 复选框由 change 事件处理，不触发展开
    if (event.target.closest('.seg-check')) return;
    // 点击行正文：展开/收起行内编辑
    if (rowEl) {
      const id = rowEl.dataset.lineId;
      state.expandedLine = state.expandedLine === id ? '' : id;
      repaintRows();
      return;
    }
    return;
  }
  const act = button.dataset.act;
  const lineId = rowEl?.dataset.lineId || blockEl?.dataset.block || '';

  if (act === 'back') return goProjects();
  if (act === 'more-views') {
    openMenu(
      button,
      MORE_VIEWS.map((item) => ({ key: item.key, label: item.title })),
      (key) => switchView(key)
    );
    return;
  }
  if (act === 'panel-tab') {
    const tab = button.dataset.tab === 'audit' ? 'audit' : 'tasks';
    if (tab === state.panelTab) return;
    state.panelTab = tab;
    if (tab === 'audit') ensureAuditData().then(() => paintAuditPanel());
    else stopAuditPlayback();
    repaintTask();
    const studio = document.getElementById('studio');
    if (studio && tab === 'audit') studio.classList.add('right-open');
    return;
  }
  if (act.startsWith('audit-')) {
    onAuditAction(act, button);
    return;
  }
  if (act === 'set-view') {
    const view = button.dataset.view;
    if (view && view !== state.view) switchView(view);
    return;
  }
  if (act === 'prev-chapter' || act === 'next-chapter') {
    const index = state.chapters.findIndex((item) => item.id === state.chapterId);
    const target = state.chapters[act === 'prev-chapter' ? index - 1 : index + 1];
    if (target) goChapter(state.path, target.id);
    return;
  }
  if (act === 'focus-search') {
    openFindReplace();
    return;
  }
  if (act === 'toggle-tags') {
    state.hideTags = !state.hideTags;
    document.querySelector('.editor')?.classList.toggle('hide-tags', state.hideTags);
    button.classList.toggle('on', state.hideTags);
    toast(state.hideTags ? '已隐藏标签' : '已显示标签', 'info', 1200);
    return;
  }
  if (act === 'toggle-dense') {
    state.dense = !state.dense;
    document.querySelector('.editor')?.classList.toggle('dense', state.dense);
    button.classList.toggle('on', state.dense);
    button.textContent = state.dense ? '完整显示' : '紧凑显示';
    return;
  }
  if (act === 'font-size') {
    openMenu(
      button,
      [
        { key: 'sm', label: '小号字' },
        { key: 'md', label: '中号字' },
        { key: 'lg', label: '大号字' },
      ],
      (key) => {
        state.fontSize = key;
        const studio = document.getElementById('studio');
        if (studio) studio.dataset.font = key;
      }
    );
    return;
  }
  if (act === 'view-original') {
    openOriginal();
    return;
  }
  if (act === 'scene-split-preview') return openSceneSplitPreview();
  if (act === 'script-annotate') return openScriptAnnotate();
  if (act === 'merge-narration') return openMergeNarration();
  if (act === 'batch-merge') return confirmMergeSelected();
  if (act === 'split-at-cursor') return splitAtCursor(button.dataset.line);
  if (act === 'rerun-line') return runOne(button.dataset.line);
  if (act === 'new-chapter') return openNewChapter();
  if (act === 'chapter-picker') return openChapterPicker(button);
  if (act === 'undo') return undo();
  if (act === 'redo') return redo();
  if (act === 'open-settings') {
    // 工程侧设置：作品信息与制作要求只作用于当前工程
    window.location.hash = `#/p/${encodeURIComponent(state.path)}/settings`;
    return;
  }
  if (act === 'toggle-task') {
    const studio = document.getElementById('studio');
    if (!studio) return;
    const opening = !studio.classList.contains('right-open');
    studio.classList.toggle('right-open', opening);
    // 右栏展开会挤占正文宽度：窄屏自动收起目录，保留正文可读性；关掉时恢复
    if (opening && window.innerWidth < 1680 && !studio.classList.contains('ctx-hidden')) {
      studio.classList.add('ctx-hidden');
      studio.dataset.autoCtx = '1';
    } else if (!opening && studio.dataset.autoCtx) {
      studio.classList.remove('ctx-hidden');
      delete studio.dataset.autoCtx;
    }
    return;
  }
  if (act === 'toggle-timeline') return toggleTimeline();
  if (act === 'tl-build') return buildTimeline();
  if (act === 'batch-plan') return openBatchDialog('chapter');
  if (act === 'regenerate-chapter') return confirmRegenerate();
  if (act === 'stop') return stopGeneration();
  if (act === 'pause') return togglePause();
  if (act === 'play-all') return playAll();
  if (act === 'line-menu') {
    openLineMenu(lineId, button);
    return;
  }
  if (act === 'play-one') {
    const row = state.lines.find((item) => item.line_id === lineId);
    if (row) playRow(row);
    return;
  }
  if (act === 'cancel-line') {
    state.expandedLine = '';
    repaintRows();
    return;
  }
  if (act === 'save-line') return saveLine(lineId);
  if (act === 'batch-clear') {
    state.selected = new Set();
    document.querySelectorAll('#ed-body [data-check]').forEach((box) => {
      box.checked = false;
    });
    document.querySelectorAll('#ed-body .seg-row.sel').forEach((row) => row.classList.remove('sel'));
    syncBatchBar();
    return;
  }
  if (act === 'batch-apply') {
    const select = document.getElementById('batch-speaker');
    const speakerId = select?.value;
    if (!speakerId) return toast('先选择要设置的说话人', 'warn');
    const lines = [...state.selected];
    const before = {};
    const after = {};
    for (const lineId of lines) {
      const row = state.lines.find((item) => item.line_id === lineId);
      if (!row || row.speaker_id === speakerId) continue;
      before[lineId] = { speaker: row.speaker_id };
      after[lineId] = { speaker: speakerId };
    }
    // 先记住滚动位置：就地改行文本也可能让浏览器做滚动锚定
    const body = document.getElementById('ed-body');
    const keepTop = body?.scrollTop ?? 0;
    // 先就地反馈：接口返回前就更新行内说话人与状态，不重建列表
    const character = state.characters.find((item) => item.id === speakerId);
    const speakerName = character?.name || speakerId;
    const narratorId = state.characters.find((item) => item.role === 'narrator')?.id;
    for (const lineId of lines) {
      const row = state.lines.find((item) => item.line_id === lineId);
      if (!row) continue;
      row.speaker_id = speakerId;
      row.speaker = speakerName;
      if (row.kind !== 'inner_monologue') {
        row.kind = speakerId === narratorId ? 'narration' : 'dialogue';
      }
      patchLineRow(row);
    }
    select.focus();
    button.disabled = true;
    try {
      await postJSON('/api/ui/line/set-batch', {
        path: state.path,
        chapter: state.chapterId,
        lines,
        speaker: speakerId,
      });
      if (Object.keys(after).length) {
        pushHistory({ label: '批量改说话人', steps: [{ kind: 'set', before, after }] });
      }
      toast(`已更新 ${lines.length} 行`, 'ok');
      await refreshLinesInPlace(keepTop);
    } catch (error) {
      toast(error.message, 'bad');
      await refreshLinesInPlace(keepTop).catch(() => {});
    } finally {
      button.disabled = false;
      if (body) body.scrollTop = keepTop;
      // 焦点留在批量下拉，支持连续调整
      const next = document.getElementById('batch-speaker');
      if (next) {
        next.focus();
        next.value = speakerId;
      }
    }
  }
}

function onRootChange(event) {
  const speakerSelect = event.target.closest('.spk-select');
  if (speakerSelect) {
    saveSpeaker(speakerSelect.dataset.speakerFor, speakerSelect.value);
    return;
  }
  const check = event.target.closest('[data-check]');
  if (check) {
    const id = check.dataset.check;
    if (check.checked) state.selected.add(id);
    else state.selected.delete(id);
    // 只切换当前行的高亮与批量条，不重建列表
    const rowNode = check.closest('.seg-row');
    if (rowNode) rowNode.classList.toggle('sel', check.checked);
    syncBatchBar();
    return;
  }
  if (event.target.id === 'chapter-search') {
    state.chapterSearch = event.target.value;
    const list = document.getElementById('chapter-list');
    if (list) {
      // 只重画列表，避免整页闪烁
      const ctx = list.closest('.ctx');
      if (ctx) ctx.outerHTML = chapterCtx({ search: state.chapterSearch });
      const input = document.getElementById('chapter-search');
      if (input) {
        input.focus();
        input.setSelectionRange(input.value.length, input.value.length);
      }
    }
  }
}

function onRootInput(event) {
  if (event.target.id === 'line-search') {
    state.lineSearch = event.target.value;
    repaintRows();
    const input = document.getElementById('line-search');
    if (input) {
      input.focus();
      input.setSelectionRange(input.value.length, input.value.length);
    }
  }
}

function onRootKeydown(event) {
  const typing = event.target.closest?.('input, textarea, [contenteditable="true"]');
  if ((event.metaKey || event.ctrlKey) && !typing) {
    const key = event.key.toLowerCase();
    if (key === 'z') {
      event.preventDefault();
      if (event.shiftKey) redo();
      else undo();
      return;
    }
    if (key === 'y') {
      event.preventDefault();
      redo();
      return;
    }
    if (key === 'f') {
      event.preventDefault();
      openFindReplace();
      return;
    }
  }
  if (event.key === 'Escape' && state.expandedLine) {
    state.expandedLine = '';
    repaintRows();
  }
}

/**
 * 打开导演脚本规则标注：展示落盘标注、按「场次 · 台词序号 · 说话人」三级定位告警，
 * 并可现场生成标注建议（预览）或生成后同步回本章。
 * 数据与动作全部走 /api/ui/script 与 /api/ui/script/suggest/stream，不在前端复算规则。
 */
function openScriptAnnotate() {
  const mask = openDialog({
    title: '导演脚本规则标注',
    wide: true,
    body: `<div id="sa-mode">${modeSwitchHTML('script.annotate', { fallback: 'fill' })}</div>
      <div id="sa-body"><div class="empty" style="padding:18px"><span>正在读取标注…</span></div></div>`,
    foot: `<div class="right">
      <button class="btn" data-sa="close">关闭</button>
      <button class="btn" data-sa="refresh" title="重新读取落盘标注与告警">重新读取</button>
      <button class="btn" data-sa="preview" title="调用写作模型生成标注建议，只预览不写盘">生成建议（预览）</button>
      <button class="btn primary" data-sa="apply" title="生成标注并同步到本章场景与台词">生成并写入本章</button>
    </div>`,
  });
  // 生成方式：只补缺失批次（resume）或重新生成全部批次
  const readMode = bindModeSwitch(mask, 'script.annotate', {
    fallback: 'fill',
    action: mask.querySelector('[data-sa="preview"]'),
    labels: { fill: '只补缺失批次', regen: '重新生成全部' },
  });
  mask.addEventListener('click', (event) => {
    const button = event.target.closest('[data-sa]');
    if (!button) return;
    const act = button.dataset.sa;
    if (act === 'close') return closeDialog();
    if (act === 'refresh') return void loadScriptAnnotate(mask);
    if (act === 'preview') return runScriptSuggest(mask, false, readMode());
    if (act === 'apply') return runScriptSuggest(mask, true, readMode());
    if (act === 'jump') {
      const lineId = button.dataset.line;
      const scene = Number(button.dataset.scene || 0);
      closeDialog();
      if (lineId) focusLine(lineId);
      else if (scene) jumpToScene(scene);
      return;
    }
  });
  void loadScriptAnnotate(mask);
}

/** 按场次序号把视口滚到该场第一行（告警没有台词行 ID 时的兜底定位） */
function jumpToScene(sceneIndex) {
  const order = [];
  for (const row of state.lines) {
    if (row.scene_id && !order.includes(row.scene_id)) order.push(row.scene_id);
  }
  const sceneId = order[sceneIndex - 1];
  const first = sceneId ? state.lines.find((row) => row.scene_id === sceneId) : null;
  if (!first) return toast('这一场在当前章节里找不到，可能正文或切分已变化', 'warn', 4200);
  focusLine(first.line_id);
}

/** 单条告警的位置文案：场次 → 台词序号 → 说话人 */
function warningWhere(item) {
  const parts = [];
  if (item.scene_index != null) parts.push(`第 ${Number(item.scene_index) + 1} 场`);
  else if (item.shot_index != null) parts.push(`镜头 ${item.shot_index}`);
  if (item.line_index != null) parts.push(`台词 ${item.line_index}`);
  if (item.speaker) parts.push(item.speaker);
  return parts.join(' · ') || '全章';
}

/** 读取并渲染标注与告警 */
async function loadScriptAnnotate(mask) {
  const body = mask.querySelector('#sa-body');
  if (!body) return;
  try {
    const data = await getJSON('/api/ui/script', { path: state.path, chapter: state.chapterId });
    const items = data.warning_items || [];
    const plain = (data.warnings || []).filter(
      (text) => !items.some((item) => String(item.message || '') === String(text))
    );
    const rows = items
      .map(
        (item) => `
        <div class="summary-line" style="align-items:flex-start;gap:10px">
          <span style="min-width:0">
            <b style="color:var(--warn)">${esc(warningWhere(item))}</b>
            <span class="badge mute" style="margin-left:6px">${esc(item.kind || '告警')}</span>
            <div style="font-size:12px;color:var(--text);margin-top:2px">${esc(item.message || '')}</div>
            ${
              item.text || item.snippet
                ? `<div style="font-size:11px;color:var(--text-4);margin-top:2px">${esc(
                    item.text || item.snippet || ''
                  )}</div>`
                : ''
            }
          </span>
          <span class="right">
            <button class="btn sm" data-sa="jump" data-line="${esc(item.line_id || '')}"
              data-scene="${item.scene_index != null ? Number(item.scene_index) + 1 : ''}">定位</button>
          </span>
        </div>`
      )
      .join('');
    body.innerHTML = `
      <div class="summary-line">
        <span>标注文件</span>
        <span class="right badge ${data.has_script ? 'ok' : 'warn'}">${
          data.has_script ? '已生成导演脚本' : '仅有标注文本'
        }</span>
      </div>
      <div class="summary-line">
        <span>标注文本</span>
        <span class="right mono" style="max-width:70%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
          title="${esc(data.markup_path || '')}">${esc(data.markup_path || '未生成')}</span>
      </div>
      <div class="summary-line">
        <span>硬告警</span>
        <span class="right"><span class="badge ${items.length ? 'warn' : 'ok'}">${items.length} 条</span></span>
      </div>
      ${
        rows
          ? `<div style="margin-top:10px">${rows}</div>`
          : `<div class="dlg-note" style="margin-top:10px">没有需要人工处理的硬告警。${
              data.markup ? '' : '还没有标注文本，可点「生成建议（预览）」先看规则解析结果。'
            }</div>`
      }
      ${
        plain.length
          ? `<details style="margin-top:10px"><summary style="cursor:pointer;font-size:11px;color:var(--text-3)">
              自动修正 / 信息提示 ${plain.length} 条</summary>
             <div style="padding:6px 0 0 8px;font-size:11.5px;color:var(--text-3)">
               ${plain.map((text) => `<div>· ${esc(text)}</div>`).join('')}
             </div></details>`
          : ''
      }
      <div id="sa-progress" style="margin-top:10px"></div>
      <details style="margin-top:12px">
        <summary style="cursor:pointer;font-size:11.5px;color:var(--text-3)">查看标注文本（${(
          data.markup || ''
        ).length} 字）</summary>
        <pre style="max-height:240px;overflow:auto;margin-top:8px;padding:10px;border-radius:6px;
          background:var(--bg-2);font-size:11.5px;line-height:1.6;white-space:pre-wrap">${esc(
            data.markup || '（还没有标注文本）'
          )}</pre>
      </details>`;
  } catch (error) {
    body.innerHTML = `<div class="dlg-note bad">${esc(error.message)}</div>`;
  }
}

/**
 * 生成标注建议（流式）。
 * @param {HTMLElement} mask 弹窗根节点
 * @param {boolean} apply true 时同步到本章（有失败批次则只落标注文本）
 * @param {'fill'|'regen'} [mode] fill 只补缺失 / 失败批次（resume=true），regen 从头生成
 */
async function runScriptSuggest(mask, apply, mode = 'fill') {
  const resume = mode === 'fill';
  const progress = mask.querySelector('#sa-progress');
  const buttons = mask.querySelectorAll('[data-sa="preview"], [data-sa="apply"], [data-sa="refresh"]');
  buttons.forEach((button) => (button.disabled = true));
  if (progress) {
    progress.innerHTML = `<div class="dlg-note">正在生成标注建议…<span id="sa-progress-text"></span></div>`;
  }
  const label = mask.querySelector('#sa-progress-text');
  const panel = openProgress({
    title: apply
      ? resume
        ? '正在补齐标注批次并写入本章…'
        : '正在重新生成标注并写入本章…'
      : resume
        ? '正在补齐缺失批次的标注建议…'
        : '正在重新生成全部标注建议…',
    hint: resume
      ? '只补做缺失 / 失败的镜头批次，已完成批次会跳过'
      : '忽略已有批次，按镜头重新生成全部标注',
    stage: '已提交生成请求，等待模型返回…',
  });
  panel.update({ percent: null });
  let settled = false;
  try {
    await streamJSON(
      '/api/ui/script/suggest/stream',
      { path: state.path, chapter: state.chapterId, apply, dry_run: !apply, resume },
      (event, data) => {
        if (!data) return;
        if (event === 'batch') {
          const index = Number(data.index || 0);
          const total = Number(data.total || 1) || 1;
          const text = ` 第 ${index + 1}/${total} 批（镜头 ${Number(data.start) + 1}–${Number(data.end)}）`;
          if (label) label.textContent = text;
          panel.update({
            stage: `正在标注第 ${index + 1}/${total} 批镜头…`,
            percent: Math.round((index / total) * 100),
            current: `镜头 ${Number(data.start) + 1}–${Number(data.end)}`,
          });
        } else if (event === 'chars') {
          const text = ` 已接收 ${Number(data.chars || 0)} 字`;
          if (label) label.textContent = text;
          panel.update({ current: `已接收 ${Number(data.chars || 0)} 字` });
        } else if (event === 'done') {
          settled = true;
          const result = data.result || {};
          const failures = result.failures || [];
          panel.update({ percent: 100 });
          // 结果摘要：优先取模型返回的标注文本开头，便于确认最近一批内容
          const markupLine = String(result.markup || '')
            .replace(/\s+/g, ' ')
            .trim()
            .slice(0, 80);
          panel.note(
            markupLine || (failures.length ? `仍有 ${failures.length} 个批次失败` : '全部批次已完成')
          );
          panel.finish({
            message: apply
              ? `标注已生成并同步到本章${failures.length ? `，${failures.length} 个批次失败` : ''}`
              : `标注建议已生成（未写盘）${failures.length ? `，${failures.length} 个批次失败` : ''}`,
            detail: failures.length
              ? '失败批次可用「重试失败批次」只补做失败部分。'
              : '标注文件已更新，可在弹窗里查看告警明细。',
            actions: [
              {
                label: '刷新标注',
                primary: true,
                onClick: () => void loadScriptAnnotate(mask),
              },
              ...(failures.length
                ? [
                    {
                      label: '重试失败批次',
                      onClick: () => void runScriptSuggestResume(mask, apply),
                    },
                  ]
                : []),
            ],
          });
        } else if (event === 'error') {
          settled = true;
          panel.fail({
            message: data.message || '标注生成失败',
            retry: () => void runScriptSuggest(mask, apply, mode),
          });
        }
      }
    );
    if (!settled) {
      panel.fail({ message: '连接已中断，没有收到标注结果', retry: () => void runScriptSuggest(mask, apply, mode) });
    }
    if (apply) {
      toast('标注已生成并同步到本章，正在刷新台词', 'ok', 3600);
      await loadChapterState();
      repaintPage();
    } else {
      toast('已生成标注建议（未写盘）', 'ok');
    }
    await loadScriptAnnotate(mask);
  } catch (error) {
    toast(error.message, 'bad', 4600);
    if (progress) progress.innerHTML = `<div class="dlg-note bad">${esc(error.message)}</div>`;
    if (!settled) panel.fail({ message: `标注生成失败：${error.message}`, retry: () => void runScriptSuggest(mask, apply, mode) });
  } finally {
    buttons.forEach((button) => (button.disabled = false));
  }
}

/**
 * 只重试失败批次的标注（resume=true）。
 * @param {HTMLElement} mask 弹窗根节点
 * @param {boolean} apply 是否同步到本章
 */
async function runScriptSuggestResume(mask, apply) {
  const panel = openProgress({
    title: '正在重试失败批次…',
    hint: '只补做上次失败的镜头批次，已完成批次会跳过',
    stage: '已提交重试请求…',
  });
  panel.update({ percent: null });
  try {
    await streamJSON(
      '/api/ui/script/suggest/stream',
      { path: state.path, chapter: state.chapterId, apply, dry_run: !apply, resume: true },
      (event, data) => {
        if (event === 'done') {
          const failures = data?.result?.failures || [];
          panel.finish({
            message: failures.length ? `仍有 ${failures.length} 个批次失败` : '失败批次已补齐',
            actions: [{ label: '刷新标注', primary: true, onClick: () => void loadScriptAnnotate(mask) }],
          });
        } else if (event === 'error') {
          panel.fail({ message: data?.message || '重试失败', retry: () => void runScriptSuggestResume(mask, apply) });
        }
      }
    );
    await loadScriptAnnotate(mask);
  } catch (error) {
    panel.fail({ message: `重试失败：${error.message}`, retry: () => void runScriptSuggestResume(mask, apply) });
  }
}

/** 绑定根节点事件（每次进入页面先解绑旧的） */
function bind(root) {
  if (bound) {
    root.removeEventListener('click', onRootClick);
    root.removeEventListener('change', onRootChange);
    root.removeEventListener('input', onRootInput);
    document.removeEventListener('keydown', onRootKeydown);
  }
  root.addEventListener('click', onRootClick);
  root.addEventListener('change', onRootChange);
  root.addEventListener('input', onRootInput);
  // 快捷键挂 document：焦点在 body 时也能触发 ⌘F
  document.addEventListener('keydown', onRootKeydown);
  bound = true;
}

/** 进入编辑页入口 */
export async function renderEditor(root, { path, chapterId, lineId = '' }) {
  stopPolling();
  pageRoot = root;
  if (state.path !== path) resetProject(path);
  bind(root);
  root.innerHTML = `
    ${topbar({
      left: `<button class="btn icon" data-act="back" title="返回工作区">${icon('back')}</button>`,
      crumbs: '<span class="cur">正在加载工程…</span>',
      right: '',
    })}
    <div class="page"><div class="empty">${icon('refresh', 20)}<span>正在读取工程与章节…</span></div></div>`;
  try {
    await loadData(path, chapterId);
    if (!state.chapterId) {
      // 空工程不报错、不退回列表：保留完整骨架，主区给两条起步入口
      root.innerHTML = `
        ${topbar({ left: `<button class="btn icon" data-act="back" title="返回工作区">${icon('back')}</button>`, crumbs: '<span class="cur">还没有章节</span>', right: '' })}
        <div class="studio" id="studio">
          ${railbar('chapter', { chapters: 0 })}
          ${chapterCtx({ search: state.chapterSearch })}
          <section class="editor"><div class="page" style="overflow:auto">${emptyProjectHTML(path)}</div></section>
          <aside class="studio-right"></aside>
        </div>`;
      return;
    }
    await loadChapterState();
    configureViews({
      /** 制作视图里的写操作完成后刷新本章数据（会同步重画当前视图） */
      refresh: () => softRefresh(),
      /** 录音视图「到画本编辑中定位」：切回画本编辑并展开该行 */
      openInScript: (lineId) => {
        state.view = 'script';
        state.expandedLine = lineId;
        repaintPage();
      },
    });
    root.innerHTML = pageHTML();
    mountView(root);
    startPolling();
    // 顶部「重新切分本章」：完成后重新读本章数据并整页重画，提示条自动消失
    onResplit(async () => {
      await loadChapterState();
      repaintPage();
    });
    // 全局搜索直达台词行：数据就绪后再定位，避免命中空列表
    if (lineId) focusLine(lineId);
  } catch (error) {
    root.innerHTML = `
      ${topbar({ left: `<button class="btn icon" data-act="back" title="返回工作区">${icon('back')}</button>`, crumbs: '<span class="cur">加载失败</span>', right: '' })}
      <div class="page"><div class="empty">${icon('warn', 22)}<b>无法打开这个工程</b>
        <span>${esc(error.message)}</span>
        <button class="btn" data-act="back" style="margin-top:10px">返回工程列表</button>
      </div></div>`;
  }
}

/** 离开编辑页时停掉轮询与播放 */
export function leaveEditor() {
  stopPolling();
  stopPlayer();
  closeChapterPicker();
  resetHistory();
  onResplit(null);
  resetViews();
  pageRoot = null;
  document.removeEventListener('keydown', onRootKeydown);
  tl.audio?.pause();
  cancelAnimationFrame(tl.raf);
  tl.open = false;
}
