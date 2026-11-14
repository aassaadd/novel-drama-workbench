/**
 * 设置视图：全局设置（本机 / 与工程无关）+ 当前工程设置（作品信息与制作要求）。
 * 全局设置不依赖工程路径，没有打开工程时也能进入；工程区块只在带 path 时渲染。
 */
import { esc, icon, toast, openDialog, closeDialog, fmtBytes } from '../dom.js';
import { getJSON, postJSON } from '../api.js';
import { state, resetProject, saveUIPrefs } from '../store.js';
import { railbar, topbar, goChapter, goProjects, openShortcuts } from '../shell.js';

/** 当前展示工程区块的工程路径；空串表示只展示全局设置 */
let projectPath = '';
let work = null;
let info = null;
let globalError = '';
let timer = null;
/** 项目级制作要求（静音间隔 + 音频导出参数） */
let production = null;
/** 制作要求的自动保存定时器 */
let prodTimer = null;
let bound = false;
/** 是否有尚未落盘的改动（控制顶栏「保存」按钮的可用状态） */
let dirty = false;
/** 本机环境数据：readiness / models / 最近一次 ComfyUI 与 LLM 检查 */
let env = { readiness: null, models: null, comfy: null, llm: null, error: '' };
/** 环境卡片里下载任务的轮询定时器 */
let envTimer = null;
/** 模型分组（顺序即展示顺序） */
const MODEL_GROUPS = [
  ['audio', '配音 / 音乐权重'],
  ['image', '图像模型'],
  ['video', '视频模型'],
  ['music', '配乐模型'],
];

/** 静音间隔字段：键、参考字段名、单位（秒） */
const SILENCE_FIELDS = [
  ['start', '起始留白'],
  ['title_to_body', '标题⇀正文'],
  ['narration_to_dialogue', '旁白⇀对话'],
  ['narration_to_sfx', '旁白⇀音效'],
  ['narration_to_narration', '旁白⇀旁白'],
  ['dialogue_to_narration', '对话⇀旁白'],
  ['dialogue_to_sfx', '对话⇀音效'],
  ['dialogue_to_dialogue', '对话⇀对话'],
  ['sfx_to_dialogue', '音效⇀对白'],
  ['sfx_to_narration', '音效⇀旁白'],
  ['sfx_to_sfx', '音效⇀音效'],
  ['end', '结束留白'],
];

/** 更新顶栏保存态，并同步「保存」按钮的禁用条件 */
function setSaveState(kind, text) {
  const node = document.getElementById('settings-save');
  if (node) {
    node.className = `save-state ${kind}`;
    node.innerHTML = `${icon(kind === 'saving' ? 'refresh' : 'check', 12)}${esc(text)}`;
  }
  const button = document.getElementById('settings-save-now');
  if (button) {
    button.disabled = !dirty;
    button.title = dirty ? '立即保存作品信息（⌘S）' : '没有未保存的改动';
  }
}

/** 目标时长格式化 */
function durationText(ms) {
  if (!ms) return '未设置';
  const minutes = Math.round(Number(ms) / 60000);
  return `${minutes} 分钟`;
}

/** 制作要求区块：静音间隔（秒）+ 音频格式参数 */
function productionHTML() {
  const silences = production?.silences_ms || {};
  const audio = production?.audio || {};
  const fields = SILENCE_FIELDS.map(
    ([key, label]) => `
      <div class="field prod-field">
        <label>${esc(label)}</label>
        <div class="prod-input">
          <input class="input" type="number" min="0" step="0.1" data-prod-silence="${key}"
            value="${(Number(silences[key] ?? 0) / 1000).toFixed(1)}"
            title="${esc(label)} 静音时长，单位秒" />
          <span class="unit">s</span>
        </div>
      </div>`
  ).join('');
  const formatOptions = ['mp3', 'wav', 'm4a']
    .map((item) => `<option value="${item}" ${audio.format === item ? 'selected' : ''}>${item}</option>`)
    .join('');
  const rateOptions = [44100, 48000]
    .map(
      (item) =>
        `<option value="${item}" ${Number(audio.sample_rate) === item ? 'selected' : ''}>${item}hz</option>`
    )
    .join('');
  const bitrateOptions = [192, 256]
    .map(
      (item) =>
        `<option value="${item}" ${Number(audio.bitrate_kbps) === item ? 'selected' : ''}>${item}kbps</option>`
    )
    .join('');
  return `
    <div class="card" style="margin-top:14px">
      <div class="card-head"><span class="ct">制作要求（当前工程）</span>
        <span class="hint">自动保存到当前工程；静音间隔影响时间轴拼接，音频参数影响交付导出</span></div>
      <div class="card-body">
        <div class="prod-group">
          <h4>静音间隔设置</h4>
          <div class="prod-grid">${fields}</div>
        </div>
        <div class="prod-group" style="margin-top:16px">
          <h4>音频格式参数</h4>
          <div class="prod-grid">
            <div class="field prod-field"><label>音频格式</label>
              <select class="select" data-prod-audio="format" title="交付包音频格式">${formatOptions}</select></div>
            <div class="field prod-field"><label>采样率</label>
              <select class="select" data-prod-audio="sample_rate" title="导出与混音采样率">${rateOptions}</select></div>
            <div class="field prod-field"><label>目标码率</label>
              <select class="select" data-prod-audio="bitrate_kbps" title="有损格式的目标码率；wav 忽略">${bitrateOptions}</select></div>
          </div>
        </div>
        <div class="hint" style="margin-top:12px">改动静音间隔后，已有时间轴需要重建一次才会生效；新导出的交付包立即使用新的音频参数。</div>
      </div>
    </div>`;
}

/** 读取制作要求表单 */
function collectProduction() {
  const silences = {};
  document.querySelectorAll('[data-prod-silence]').forEach((node) => {
    const seconds = Number(node.value || 0);
    silences[node.dataset.prodSilence] = Math.max(0, Math.min(10000, Math.round(seconds * 1000)));
  });
  return {
    path: projectPath,
    production: {
      silences_ms: silences,
      audio: {
        format: document.querySelector('[data-prod-audio="format"]')?.value || 'mp3',
        sample_rate: Number(document.querySelector('[data-prod-audio="sample_rate"]')?.value || 48000),
        bitrate_kbps: Number(document.querySelector('[data-prod-audio="bitrate_kbps"]')?.value || 192),
      },
    },
  };
}

/** 保存制作要求（自动保存入口） */
async function saveProduction() {
  if (!production) return;
  const payload = collectProduction();
  setSaveState('saving', '保存中…');
  try {
    const result = await postJSON('/api/ui/project/production', payload);
    production = result.production || payload.production;
    setSaveState('ok', '已自动保存');
  } catch (error) {
    setSaveState('bad', '保存失败');
    toast(error.message, 'bad');
  }
}

/** 制作要求延迟保存 */
function scheduleProductionSave() {
  if (prodTimer) window.clearTimeout(prodTimer);
  setSaveState('saving', '待保存…');
  prodTimer = window.setTimeout(() => {
    prodTimer = null;
    saveProduction();
  }, 500);
}

/** 本机环境卡片（内容异步填充） */
function environmentHTML() {
  return `
    <div class="card" style="margin-top:14px">
      <div class="card-head"><span class="ct">本机环境与模型（全局）</span>
        <div class="right">
          <button class="btn sm" data-act="comfy-check" title="检查 ComfyUI 服务与工作流模板可用性">检查 ComfyUI</button>
          <button class="btn sm" data-act="llm-check" title="检查写作模型服务与模型列表">检查写作模型</button>
          <button class="btn sm" data-act="env-refresh" title="重新读取配音就绪情况与模型清单">重新自检</button>
        </div>
      </div>
      <div class="card-body" id="env-body">
        <div class="empty" style="padding:12px"><span>正在读取本机环境…</span></div>
      </div>
    </div>`;
}

/** 模型行：就绪状态、体积与下载 / 补录入口 */
function modelRowHTML(group, item) {
  const ready = item.present && item.complete;
  const gate = item.license_gate && !['none', ''].includes(item.license_gate);
  const size = item.bytes
    ? fmtBytes(item.bytes)
    : item.expected_bytes
      ? `预计 ${fmtBytes(item.expected_bytes)}`
      : '';
  return `
    <div class="summary-line" style="align-items:flex-start">
      <span style="min-width:0">
        <span style="color:var(--text)">${esc(item.display_name || item.key)}</span>
        <span class="badge ${ready ? 'ok' : item.present ? 'warn' : 'mute'}" style="margin-left:6px">${
          ready ? '就绪' : item.present ? '不完整' : '未下载'
        }</span>
        <span style="display:block;color:var(--text-4);font-size:11px;margin-top:2px">
          ${esc(item.key)}${size ? ` · ${esc(size)}` : ''}${
            item.license ? ` · ${esc(item.license)}${item.commercial_use === false ? '（不可商用）' : ''}` : ''
          }
        </span>
        ${item.note ? `<span style="display:block;color:var(--text-4);font-size:11px">${esc(item.note)}</span>` : ''}
      </span>
      <span class="right" style="display:flex;gap:6px;align-items:center">
        ${
          ready
            ? ''
            : item.downloadable
              ? `<button class="btn sm" data-act="model-install" data-group="${esc(group)}"
                   data-key="${esc(item.key)}" data-gate="${esc(item.license_gate || '')}"
                   title="${gate ? '需要先确认许可限制' : '开始下载并落地到模型目录'}">下载</button>`
              : group !== 'audio'
                ? `<button class="btn sm" data-act="model-manual" data-group="${esc(group)}"
                     data-key="${esc(item.key)}" title="手工补录 HuggingFace 仓库或下载地址">补录来源</button>
                   <button class="btn sm" data-act="model-resolve" data-group="${esc(group)}"
                     data-key="${esc(item.key)}" title="重新联网解析下载地址">解析</button>`
                : ''
        }
      </span>
    </div>`;
}

/** 渲染环境卡片内容 */
function paintEnvironment() {
  const host = document.getElementById('env-body');
  if (!host) return;
  if (env.error) {
    host.innerHTML = `<div class="dlg-note bad">${esc(env.error)}</div>`;
    return;
  }
  const readiness = env.readiness;
  const models = env.models || {};
  const providers = readiness?.providers || [];
  host.innerHTML = `
    <div class="summary-line"><span>配音引擎</span>
      <span class="right"><span class="badge ${readiness?.ready ? 'ok' : 'bad'}">${
        readiness?.ready ? '就绪' : '缺可用引擎'
      }</span></span></div>
    ${
      providers
        .map(
          (item) => `
      <div class="summary-line" style="align-items:flex-start">
        <span style="min-width:0">${esc(item.model || item.provider_id)}
          <span class="badge ${item.available ? 'ok' : item.enabled ? 'warn' : 'mute'}" style="margin-left:6px">${
            item.available ? '可用' : item.enabled ? '不可用' : '未启用'
          }</span>
          <span style="display:block;color:var(--text-4);font-size:11px;margin-top:2px">
            ${esc(item.provider_id)} · ${esc(item.backend || '')}${
              item.vram_estimate_gb ? ` · 约 ${item.vram_estimate_gb}GB 显存` : ''
            }
          </span>
        </span>
        <span class="right hint" style="max-width:52%;text-align:right">${esc(item.detail || '')}</span>
      </div>`
        )
        .join('') || '<div class="hint">没有读取到配音 provider</div>'
    }
    <div id="env-job" style="margin-top:8px"></div>
    <div style="margin-top:14px">
      ${MODEL_GROUPS.map(([group, label]) => {
        const rows = models[group]?.models || [];
        return `
          <div style="margin-top:8px">
            <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">
              <b style="font-size:12px;color:var(--text)">${label}</b>
              <span class="badge mute">${rows.length} 个</span>
              <span class="hint" style="font-size:11px">${esc(models[group]?.root || '')}</span>
            </div>
            ${rows.map((item) => modelRowHTML(group, item)).join('') || '<div class="hint">没有登记模型</div>'}
          </div>`;
      }).join('')}
    </div>
    <div id="env-checks" style="margin-top:12px">${checksHTML()}</div>`;
}

/** ComfyUI / 写作模型的最近一次检查结果 */
function checksHTML() {
  if (!env.comfy && !env.llm) return '';
  const comfy = env.comfy;
  const llm = env.llm;
  return `
    ${
      comfy
        ? `<div class="dlg-note ${comfy.ok ? '' : 'warn'}">
             ComfyUI：${comfy.ok ? '连接正常' : '未连接'}${comfy.version ? ` · ${esc(String(comfy.version))}` : ''}
             ${comfy.queue ? ` · 队列 ${Number(comfy.queue.running || 0)} 运行 / ${Number(comfy.queue.pending || 0)} 等待` : ''}
             ${(comfy.templates || []).length ? ` · 模板 ${(comfy.templates || []).filter((item) => item.ok).length}/${(comfy.templates || []).length} 可用` : ''}
           </div>`
        : ''
    }
    ${
      llm
        ? `<div class="dlg-note ${llm.connected ? '' : 'warn'}">
             写作模型：${llm.connected ? '已连接' : '未连接'} · ${esc(llm.model || '未指定')}
             ${llm.endpoint ? ` · ${esc(llm.endpoint)}` : ''}
             ${llm.model_found === false ? ' · 目标模型不在列表里' : ''}
           </div>`
        : ''
    }`;
}

/** 读取环境与模型清单 */
async function loadEnvironment() {
  const host = document.getElementById('env-body');
  if (host) {
    host.innerHTML = `<div class="empty" style="padding:12px"><span>正在读取本机环境…</span></div>`;
  }
  try {
    // 全局设置：本机环境与模型清单不依赖任何工程
    const [readiness, models] = await Promise.all([
      getJSON('/api/ui/readiness'),
      getJSON('/api/ui/models'),
    ]);
    env = { ...env, readiness, models, error: '' };
  } catch (error) {
    env = { ...env, error: `环境自检失败：${error.message}` };
  }
  paintEnvironment();
}

/** 检查 ComfyUI */
async function checkComfy(button) {
  button.disabled = true;
  try {
    env.comfy = await postJSON('/api/ui/comfy/health', {});
    paintEnvironment();
    toast(env.comfy.ok ? 'ComfyUI 连接正常' : 'ComfyUI 未连接，请先启动服务', env.comfy.ok ? 'ok' : 'warn', 4200);
  } catch (error) {
    toast(error.message, 'bad', 4600);
  } finally {
    button.disabled = false;
  }
}

/** 检查写作模型 */
async function checkLlm(button) {
  button.disabled = true;
  try {
    env.llm = await postJSON('/api/ui/llm/status', {});
    paintEnvironment();
    toast(
      env.llm.connected ? `写作模型已连接：${env.llm.model || ''}` : '写作模型未连接',
      env.llm.connected ? 'ok' : 'warn',
      4200
    );
  } catch (error) {
    toast(error.message, 'bad', 4600);
  } finally {
    button.disabled = false;
  }
}

/** 下载任务的进度区 */
function paintInstallJob(job, label) {
  const host = document.getElementById('env-job');
  if (!host) return;
  const percent = job.progress?.percent;
  const logs = (job.logs || []).slice(-6).join('\n');
  host.innerHTML = `
    <div class="dlg-note">
      ${esc(label)}：${job.status === 'running' ? '下载中' : job.status === 'done' ? '已完成' : '失败'}
      ${percent != null ? ` · ${Math.round(percent)}%` : ''}
      <div style="height:6px;border-radius:3px;background:var(--bg-2);margin-top:6px;overflow:hidden">
        <span style="display:block;height:6px;width:${percent ?? 0}%;background:var(--ok)"></span>
      </div>
      ${logs ? `<pre style="max-height:110px;overflow:auto;margin-top:6px;font-size:10.5px;white-space:pre-wrap">${esc(logs)}</pre>` : ''}
    </div>`;
}

/** 轮询下载任务直到结束 */
async function pollInstall(jobId, label) {
  if (envTimer) {
    window.clearInterval(envTimer);
    envTimer = null;
  }
  await new Promise((resolve) => {
    envTimer = window.setInterval(async () => {
      try {
        const job = await getJSON(`/api/ui/models/install/${encodeURIComponent(jobId)}`);
        paintInstallJob(job, label);
        if (job.status === 'running') return;
        window.clearInterval(envTimer);
        envTimer = null;
        if (job.status === 'done') {
          toast(`${label} 下载完成`, 'ok');
          await loadEnvironment();
        } else {
          toast(`${label} 下载失败：${job.result?.error || job.status}`, 'bad', 5200);
        }
        resolve();
      } catch (error) {
        window.clearInterval(envTimer);
        envTimer = null;
        toast(error.message, 'bad', 4600);
        resolve();
      }
    }, 1500);
  });
}

/** 开始下载一个模型：需要许可确认时先弹确认框 */
async function startInstall(group, key, gate) {
  const accept = false;
  if (gate && !['none', ''].includes(gate)) {
    const mask = openDialog({
      title: '确认模型许可',
      body: `<div class="dlg-note warn">该模型需要确认许可限制后才能下载（${esc(gate)}）。请确认已理解其商用与分发限制。</div>
        <label style="display:flex;gap:8px;align-items:center;margin-top:10px;font-size:12px">
          <input type="checkbox" id="ml-accept" /> 我已理解并接受该模型许可</label>`,
      foot: `<div class="right"><button class="btn" data-dlg-close>取消</button>
        <button class="btn primary" id="ml-go" disabled>开始下载</button></div>`,
    });
    const box = mask.querySelector('#ml-accept');
    const go = mask.querySelector('#ml-go');
    box.addEventListener('change', () => (go.disabled = !box.checked));
    go.addEventListener('click', async () => {
      go.disabled = true;
      try {
        const { job_id: jobId } = await postJSON('/api/ui/models/install', {
          group,
          key,
          accept_license: true,
          yes: true,
        });
        closeDialog();
        await pollInstall(jobId, key);
      } catch (error) {
        toast(error.message, 'bad', 4600);
        go.disabled = false;
      }
    });
    return;
  }
  try {
    const { job_id: jobId } = await postJSON('/api/ui/models/install', { group, key, accept_license: accept, yes: true });
    await pollInstall(jobId, key);
  } catch (error) {
    toast(error.message, 'bad', 4600);
  }
}

/** 补录图像 / 视频 / 音乐模型的下载来源 */
function openManualModel(group, key) {
  const mask = openDialog({
    title: `补录来源 · ${key}`,
    body: `<div class="dlg-note">填 HuggingFace 仓库（可带文件名）或直接填下载地址，保存后回到列表点「下载」。</div>
      <div class="field" style="margin-top:8px"><label>HF 仓库</label>
        <input class="input" id="mm-repo" placeholder="例如：Comfy-Org/flux1-dev" /></div>
      <div class="field" style="margin-top:8px"><label>仓库内文件名（可选）</label>
        <input class="input" id="mm-file" placeholder="例如：flux1-dev.safetensors" /></div>
      <div class="field" style="margin-top:8px"><label>直接下载地址（可选）</label>
        <input class="input" id="mm-url" placeholder="https://…" /></div>`,
    foot: `<div class="right"><button class="btn" data-dlg-close>取消</button>
      <button class="btn primary" id="mm-save">保存来源</button></div>`,
  });
  mask.querySelector('#mm-save').addEventListener('click', async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      await postJSON('/api/ui/models/manual', {
        group,
        key,
        repo: mask.querySelector('#mm-repo').value.trim() || undefined,
        hf_filename: mask.querySelector('#mm-file').value.trim() || undefined,
        url: mask.querySelector('#mm-url').value.trim() || undefined,
      });
      closeDialog();
      toast('来源已补录，可以重新下载', 'ok');
      await loadEnvironment();
    } catch (error) {
      toast(error.message, 'bad', 4600);
      button.disabled = false;
    }
  });
}

/** 左侧目录（复用章节列表，仅用于导航） */
function ctxHTML() {
  return `
    <section class="ctx" aria-label="章节目录">
      <div class="ctx-head"><span class="ct">章节目录</span>
        <span class="badge mute">${state.chapters.length} 章</span>
      </div>
      <div class="chap-list" id="ctx-list">${ctxRowsHTML()}</div>
    </section>`;
}

/** 章节行（仅在有工程上下文时展示，点击回到该章画本） */
function ctxRowsHTML() {
  if (!projectPath || projectPath !== state.path || !state.chapters.length) {
    return `<div class="empty" style="padding:18px 12px">
      <span>${projectPath ? '正在读取章节…' : '设置不需要选择工程'}</span></div>`;
  }
  return state.chapters
    .map(
      (chapter) => `
      <div class="chap-row" data-chapter="${esc(chapter.id)}"
           title="${esc(chapter.title || `第 ${chapter.index} 章`)} · 回到画本">
        <span class="cn">${esc(chapter.title || `第 ${chapter.index} 章`)}</span>
        <span class="dot ${chapter.status === 'split' ? 'warn' : ''}"></span>
        <span class="cw">${chapter.word_count ? `${chapter.word_count}字` : '未写'}</span>
      </div>`
    )
    .join('');
}

/** 分区标题：明确这是全局还是当前工程 */
function sectionHead(title, desc) {
  return `<div class="set-section-head">
    <span class="ct">${esc(title)}</span>
    <span class="hint">${esc(desc)}</span>
  </div>`;
}

/** 快捷键卡片（只读；详细面板复用全站快捷键弹窗） */
function shortcutsHTML() {
  const rows = [
    ['⌘/Ctrl + K', '全局搜索'],
    ['⌘/Ctrl + F', '当前章查找替换'],
    ['⌘/Ctrl + Z', '撤销'],
    ['⇧ + ⌘/Ctrl + Z', '重做'],
    ['⌘/Ctrl + S', '在设置页立即保存工程信息'],
    ['?', '打开快捷键面板'],
    ['Alt + T', '打开通知'],
  ];
  return `
    <div class="card" style="margin-top:14px">
      <div class="card-head"><span class="ct">快捷键</span>
        <div class="right"><button class="btn sm" data-act="shortcuts-help"
          title="查看全局、录音、后期制作的全部快捷键">查看全部</button></div>
      </div>
      <div class="card-body" style="gap:0">
        ${rows
          .map(
            ([key, desc]) =>
              `<div class="set-kb-row"><kbd>${esc(key)}</kbd><span>${esc(desc)}</span></div>`
          )
          .join('')}
        <div class="hint">快捷键是全局行为，不随工程变化。</div>
      </div>
    </div>`;
}

/** 界面偏好卡片：写入 localStorage，画本编辑下次渲染时生效 */
function prefsHTML() {
  const sizeOptions = [
    ['sm', '小号字'],
    ['md', '中号字'],
    ['lg', '大号字'],
  ]
    .map(
      ([value, label]) =>
        `<option value="${value}" ${state.fontSize === value ? 'selected' : ''}>${label}</option>`
    )
    .join('');
  return `
    <div class="card" style="margin-top:14px">
      <div class="card-head"><span class="ct">界面偏好</span>
        <span class="hint">保存在本机，对所有工程生效</span>
      </div>
      <div class="card-body">
        <label class="set-pref"><input type="checkbox" data-pref="hideTags" ${state.hideTags ? 'checked' : ''} />
          <span><b>隐藏标签</b><em>画本编辑里收起旁白 / 情绪 / 停顿等标签，只显示正文</em></span></label>
        <label class="set-pref"><input type="checkbox" data-pref="dense" ${state.dense ? 'checked' : ''} />
          <span><b>紧凑段落</b><em>压缩行间距，同一屏能看到更多台词行</em></span></label>
        <div class="field" style="max-width:260px">
          <label>正文字号</label>
          <select class="select" data-pref="fontSize" title="画本编辑的正文字号，只影响本机显示">${sizeOptions}</select>
        </div>
        <div class="hint">改动即时保存；回到画本编辑后按这里的设置显示。</div>
      </div>
    </div>`;
}

/** 工程信息卡片（当前工程专属） */
function projectInfoHTML(item) {
  return `
    <div class="card">
      <div class="card-head"><span class="ct">作品信息</span>
        <span class="hint">只写入当前工程</span></div>
      <div class="card-body">
        <div style="display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px">
          <div class="field"><label>书名</label>
            <input class="input" id="set-title" value="${esc(item.title || '')}" title="作品名，显示在顶栏与工程列表" /></div>
          <div class="field"><label>作者</label>
            <input class="input" id="set-author" value="${esc(item.author || '')}" placeholder="未填写"
              title="作者署名，可留空" /></div>
          <div class="field" style="grid-column:1/-1"><label>题材（逗号分隔）</label>
            <input class="input" id="set-genres" value="${esc((item.genres || []).join('，'))}" placeholder="例如：悬疑，都市"
              title="题材关键词，多个用逗号分隔" /></div>
          <div class="field"><label>语言</label>
            <div class="input" style="display:flex;align-items:center;color:var(--text-3)"
              title="语言在导入阶段确定，本地版暂不支持在这里修改">${esc(item.language || 'zh-CN')}</div></div>
          <div class="field"><label>目标时长</label>
            <div class="input" style="display:flex;align-items:center;color:var(--text-3)"
              title="目标时长在导入阶段确定，本地版暂不支持在这里修改">${durationText(
              item.target_duration_ms
            )}</div></div>
        </div>
        <div class="hint">作品信息写回 project.json；语言与目标时长本阶段只读展示。</div>
      </div>
    </div>`;
}

/** 当前工程区块：作品信息 + 制作要求 + 制作概况 */
function projectBlockHTML() {
  if (globalError) {
    return `
      <section class="set-section" style="margin-top:22px" data-section="project">
        ${sectionHead('当前工程', '工程级设置读取失败')}
        <div class="card"><div class="card-body">
          <div class="empty" style="padding:18px">${icon('warn', 20)}
            <b>当前工程设置加载失败</b><span>${esc(globalError)}</span></div>
        </div></div>
      </section>`;
  }
  const item = work || {};
  const totalWords = (info?.chapters || []).reduce((sum, chapter) => sum + Number(chapter.word_count || 0), 0);
  return `
    <section class="set-section" style="margin-top:22px" data-section="project">
      ${sectionHead(
        `当前工程：${item.title || '未命名作品'}`,
        '只对当前打开的工程生效：作品信息、制作要求、制作概况；与全局设置互不影响'
      )}
      ${projectInfoHTML(item)}
      ${productionHTML()}
      <div class="card" style="margin-top:14px">
        <div class="card-head"><span class="ct">制作概况</span></div>
        <div class="card-body">
          <div class="summary-line"><span>章节数</span><span class="right">${(info?.chapters || []).length} 章</span></div>
          <div class="summary-line"><span>正文字数</span><span class="right">${totalWords} 字</span></div>
          <div class="summary-line"><span>创建时间</span><span class="right mono">${esc(item.created_at || '—')}</span></div>
          <div class="summary-line"><span>最近更新</span><span class="right mono">${esc(item.updated_at || '—')}</span></div>
        </div>
      </div>
    </section>`;
}

/** 没有工程上下文时的工程区块提示 */
function noProjectHTML() {
  return `
    <section class="set-section" style="margin-top:22px" data-section="project">
      ${sectionHead('当前工程', '没有打开工程时这里为空；全局设置不受影响')}
      <div class="card"><div class="card-body">
        <div class="empty" style="padding:18px">${icon('folder', 20)}
          <b>还没有打开工程</b>
          <span>打开一个工程后，这里会出现该工程的作品信息与制作要求</span>
          <a class="btn" href="#/projects" style="margin-top:10px">去打开工程</a>
        </div>
      </div></div>
    </section>`;
}

/** 主区：全局设置恒在，工程区块按是否有工程上下文追加 */
function mainHTML() {
  return `
    <div class="page-head" style="padding:18px 18px 0">
      <div class="ph">
        <div class="eyebrow"><span class="ln"></span>设置</div>
        <h1 class="page-title">设置</h1>
        <div class="page-sub">全局设置作用于本机所有工程；当前工程设置只影响这一个工程。</div>
      </div>
    </div>
    <div style="padding:0 18px 18px;overflow:auto;flex:1">
      <section class="set-section" data-section="global">
        ${sectionHead(
          '全局设置（本机）',
          '与具体工程无关：模型与算力、配音 provider、环境自检与模型下载、写作模型连通、快捷键、界面偏好'
        )}
        ${environmentHTML()}
        ${shortcutsHTML()}
        ${prefsHTML()}
      </section>
      <div id="project-set">${projectPath ? `<div class="card" style="margin-top:22px"><div class="card-body">
        <div class="empty" style="padding:12px"><span>正在读取当前工程设置…</span></div></div></div>` : noProjectHTML()}</div>
    </div>`;
}

/** 收集表单 */
function collect() {
  return {
    title: document.getElementById('set-title')?.value.trim() || '',
    author: document.getElementById('set-author')?.value.trim() || '',
    genres: (document.getElementById('set-genres')?.value || '')
      .split(/[,，、;；]/)
      .map((text) => text.trim())
      .filter(Boolean),
  };
}

/** 保存作品信息 */
async function saveWork({ silent = false } = {}) {
  if (!work) return;
  const body = collect();
  setSaveState('saving', '保存中…');
  try {
    await postJSON('/api/ui/project/work', { path: projectPath, ...body });
    work = { ...work, ...body, updated_at: new Date().toISOString() };
    dirty = false;
    setSaveState('ok', '已自动保存');
    if (!silent) toast('项目设定已保存', 'ok');
  } catch (error) {
    setSaveState('', '保存失败');
    toast(error.message, 'bad');
  }
}

/** 延迟保存 */
function scheduleSave() {
  dirty = true;
  setSaveState('saving', '待保存…');
  if (timer) window.clearTimeout(timer);
  timer = window.setTimeout(() => {
    timer = null;
    saveWork({ silent: true });
  }, 800);
}

/** 失焦立即保存 */
function flushSave() {
  if (!timer) return;
  window.clearTimeout(timer);
  timer = null;
  saveWork({ silent: true });
}

/** 点击 */
function onRootClick(event) {
  const chapterRow = event.target.closest('[data-chapter]');
  if (chapterRow) return goChapter(projectPath, chapterRow.dataset.chapter);
  const button = event.target.closest('[data-act]');
  if (!button) return;
  if (button.dataset.act === 'back') return goProjects();
  if (button.dataset.act === 'to-editor') return goChapter(projectPath, state.chapterId);
  if (button.dataset.act === 'shortcuts-help') return openShortcuts();
  if (button.dataset.act === 'save-now') {
    if (timer) {
      window.clearTimeout(timer);
      timer = null;
    }
    saveWork();
    return;
  }
  if (button.dataset.act === 'env-refresh') return void loadEnvironment();
  if (button.dataset.act === 'comfy-check') return void checkComfy(button);
  if (button.dataset.act === 'llm-check') return void checkLlm(button);
  if (button.dataset.act === 'model-install') {
    return void startInstall(button.dataset.group, button.dataset.key, button.dataset.gate);
  }
  if (button.dataset.act === 'model-manual') {
    return openManualModel(button.dataset.group, button.dataset.key);
  }
  if (button.dataset.act === 'model-resolve') {
    button.disabled = true;
    void postJSON('/api/ui/models/resolve', { group: button.dataset.group, key: button.dataset.key })
      .then((result) => {
        toast(result.repo ? `已解析：${result.repo}` : result.message || '已解析', 'ok', 4200);
        return loadEnvironment();
      })
      .catch((error) => toast(error.message, 'bad', 4800))
      .finally(() => {
        button.disabled = false;
      });
  }
}

/** ⌘S 立即保存（只在设置页且有当前工程时生效，避免抢走浏览器默认行为） */
function onKeydown(event) {
  if (!(event.metaKey || event.ctrlKey) || event.key.toLowerCase() !== 's') return;
  if (!location.hash.startsWith('#/settings')) return;
  if (!projectPath) return;
  event.preventDefault();
  if (!dirty) return toast('没有未保存的改动', 'info', 1500);
  if (timer) {
    window.clearTimeout(timer);
    timer = null;
  }
  saveWork();
}

/** 输入 */
function onRootInput(event) {
  if (event.target.closest('#set-title, #set-author, #set-genres')) scheduleSave();
  if (event.target.closest('[data-prod-silence]')) scheduleProductionSave();
}

/** 失焦 */
function onRootFocusOut(event) {
  if (event.target.closest('#set-title, #set-author, #set-genres')) flushSave();
  if (event.target.closest('[data-prod-silence]')) {
    if (prodTimer) {
      window.clearTimeout(prodTimer);
      prodTimer = null;
    }
    saveProduction();
  }
}

/** 改动界面偏好：写回 state 并落到 localStorage（全局，与工程无关） */
function applyPref(name, value) {
  if (name === 'fontSize') {
    state.fontSize = value;
    saveUIPrefs({ fontSize: value });
  } else {
    state[name] = Boolean(value);
    saveUIPrefs({ [name]: state[name] });
  }
  toast('界面偏好已保存，回到画本后生效', 'ok', 1600);
}

/** 下拉选择 / 勾选：工程制作参数立即保存，界面偏好写本地 */
function onRootChange(event) {
  const pref = event.target.closest('[data-pref]');
  if (pref) {
    const name = pref.dataset.pref;
    applyPref(name, name === 'fontSize' ? pref.value : pref.checked);
    return;
  }
  if (event.target.closest('[data-prod-audio]')) saveProduction();
}

/** 读取当前工程的三份数据，并只重画工程区块与章节行 */
async function loadProject() {
  const host = document.getElementById('project-set');
  if (!host || !projectPath) return;
  try {
    const [infoPayload, workPayload, productionPayload] = await Promise.all([
      getJSON('/api/project/info', { path: projectPath }),
      getJSON('/api/ui/project/work', { path: projectPath }),
      getJSON('/api/ui/project/production', { path: projectPath }),
    ]);
    info = infoPayload;
    work = workPayload || {};
    production = productionPayload?.production || null;
    state.info = info;
    state.chapters = info?.chapters || [];
    if (!state.chapters.some((item) => item.id === state.chapterId)) {
      state.chapterId = state.chapters[0]?.id || '';
    }
    globalError = '';
  } catch (error) {
    globalError = error.message;
    // 加载失败时清掉旧工程数据，避免表单显示上一个工程的信息
    work = null;
    production = null;
  }
  host.innerHTML = projectBlockHTML();
  const list = document.getElementById('ctx-list');
  if (list) list.innerHTML = ctxRowsHTML();
  const crumb = document.getElementById('set-crumb-project');
  if (crumb) crumb.textContent = work?.title || '当前工程';
}

/** 进入设置：全局区块立即渲染，工程区块异步补上 */
export async function renderSettings(root, { path = '' } = {}) {
  if (timer) {
    window.clearTimeout(timer);
    timer = null;
  }
  dirty = false;
  if (prodTimer) {
    window.clearTimeout(prodTimer);
    prodTimer = null;
  }
  if (envTimer) {
    window.clearInterval(envTimer);
    envTimer = null;
  }
  projectPath = path || '';
  if (projectPath && state.path !== projectPath) {
    resetProject(projectPath);
    production = null;
  }
  if (!projectPath) {
    // 全局设置没有工程上下文：不要把上一个工程的表单残留当成当前工程
    work = null;
    info = null;
    production = null;
    globalError = '';
  }
  if (!bound) {
    root.addEventListener('click', onRootClick);
    root.addEventListener('input', onRootInput);
    root.addEventListener('change', onRootChange);
    root.addEventListener('focusout', onRootFocusOut);
    document.addEventListener('keydown', onKeydown);
    bound = true;
  }
  root.innerHTML = `
    ${topbar({
      left: `<button class="btn icon" data-act="back" title="回工程列表">${icon('back')}</button>
             <div class="brand"><span class="brand-mark"></span><span style="font-size:12.5px">设置<small>全局</small></span></div>`,
      crumbs: `<a href="#/projects">工程列表</a>
               <span class="sep">/</span><span class="cur">全局设置</span>
               ${projectPath ? '<span class="sep">/</span><span class="cur" id="set-crumb-project">当前工程</span>' : ''}`,
      right: projectPath
        ? `<span class="save-state ok" id="settings-save">${icon('check', 12)}已自动保存</span>
           <button class="btn sm" id="settings-save-now" data-act="save-now" disabled
             title="没有未保存的改动">保存</button>
           <button class="btn" data-act="to-editor">${icon('chapter')} 回画本</button>`
        : `<span class="save-state ok">${icon('check', 12)}全局设置即时生效</span>
           <button class="btn" data-act="back">${icon('folder')} 项目列表</button>`,
    })}
    <div class="studio" id="studio">
      ${railbar('settings')}
      ${ctxHTML()}
      <section class="editor">
        <div id="settings-main" style="display:flex;flex-direction:column;min-height:0;flex:1">${mainHTML()}</div>
      </section>
      <aside class="studio-right"></aside>
    </div>`;
  void loadEnvironment();
  if (projectPath) void loadProject();
}
