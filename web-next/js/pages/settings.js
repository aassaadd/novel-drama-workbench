/**
 * 设置视图：只放全局设置（本机环境 / 模型 / 连接配置 / 快捷键 / 界面偏好）。
 * 工程相关字段（作品信息、制作要求、制作概况）已迁到工程设置页 `#/p/<路径>/settings`。
 */
import { esc, icon, toast, openDialog, closeDialog, fmtBytes } from '../dom.js';
import { getJSON, postJSON } from '../api.js';
import { state, saveUIPrefs } from '../store.js';
import { railbar, topbar, goProjects, openShortcuts } from '../shell.js';

let bound = false;
/** 本机环境数据：readiness / models / 最近一次 ComfyUI 与 LLM 检查 */
let env = { readiness: null, models: null, comfy: null, llm: null, error: '' };
/** 环境卡片里下载任务的轮询定时器 */
let envTimer = null;
/** 连接配置（ComfyUI / 写作模型）与配置文件位置 */
let connection = null;
/** 模型分组（顺序即展示顺序） */
const MODEL_GROUPS = [
  ['audio', '配音 / 音乐权重'],
  ['image', '图像模型'],
  ['video', '视频模型'],
  ['music', '配乐模型'],
];


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

/**
 * 拼出「未下全」的具体原因：已下多少 / 期望多少 / 还差多少 / 缺哪个文件 / 校验不符。
 * 行内与悬停提示共用这一份文案，保证两处说法一致。
 * @param {object} item 模型行
 * @returns {string} 中文原因
 */
function incompleteReason(item) {
  const reasons = [];
  const expected = Number(item.expected_bytes) || 0;
  const actual = Number(item.bytes) || 0;
  // 服务端已经给了更精确的原因（ComfyUI 文件为空 / 大小不符）时不再重复拼体积
  const serverReasons = (Array.isArray(item.reasons) ? item.reasons : []).map(String);
  if (!serverReasons.length) {
    if (expected) {
      const gap = expected - actual;
      reasons.push(
        `已下 ${fmtBytes(actual)} / 期望 ${fmtBytes(expected)}` + (gap > 0 ? `，还差 ${fmtBytes(gap)}` : '')
      );
    } else if (actual) {
      reasons.push(`已下 ${fmtBytes(actual)}`);
    }
  }
  serverReasons.forEach((text) => reasons.push(text));
  // 音频模型缺的具体文件名（可能带「大小不符：实际/期望」）
  const missing = Array.isArray(item.missing_files) ? item.missing_files : [];
  if (missing.length) reasons.push(`缺少或校验不符：${missing.join('、')}`);
  return reasons.length ? reasons.join('；') : '文件没下全：可能下载中断或校验未通过，建议重新下载';
}

/** 模型行：就绪状态、体积与下载 / 补录入口 */
function modelRowHTML(group, item) {
  const ready = item.present && item.complete;
  const incomplete = item.present && !item.complete;
  const gate = item.license_gate && !['none', ''].includes(item.license_gate);
  const size = item.bytes
    ? fmtBytes(item.bytes)
    : item.expected_bytes
      ? `预计 ${fmtBytes(item.expected_bytes)}`
      : '';
  const reason = incomplete ? incompleteReason(item) : '';
  const badgeTitle = ready ? '文件已下全，可以直接使用' : incomplete ? reason : '还没有下载这个模型';
  return `
    <div class="summary-line" style="align-items:flex-start">
      <span style="min-width:0">
        <span style="color:var(--text)">${esc(item.display_name || item.key)}</span>
        <span class="badge ${ready ? 'ok' : incomplete ? 'warn' : 'mute'}" style="margin-left:6px"
          title="${esc(badgeTitle)}">${ready ? '就绪' : incomplete ? '未下全' : '未下载'}</span>
        <span style="display:block;color:var(--text-4);font-size:11px;margin-top:2px">
          ${esc(item.key)}${size ? ` · ${esc(size)}` : ''}${
            item.license ? ` · ${esc(item.license)}${item.commercial_use === false ? '（不可商用）' : ''}` : ''
          }
        </span>
        ${incomplete ? `<span class="model-reason" title="${esc(reason)}">${esc(reason)}</span>` : ''}
        ${item.note ? `<span style="display:block;color:var(--text-4);font-size:11px">${esc(item.note)}</span>` : ''}
      </span>
      <span class="right" style="display:flex;gap:6px;align-items:center">
        ${
          ready
            ? ''
            : item.downloadable
              ? `<button class="btn sm" data-act="model-install" data-group="${esc(group)}"
                   data-key="${esc(item.key)}" data-gate="${esc(item.license_gate || '')}"
                   title="${gate ? '需要先确认许可限制' : '开始下载并落地到模型目录'}">${
                     incomplete ? '补下' : '下载'
                   }</button>`
              : group !== 'audio'
                ? `<button class="btn sm" data-act="model-manual" data-group="${esc(group)}"
                     data-key="${esc(item.key)}"
                     title="不知道这个模型从哪下载时，在这里填 HuggingFace 仓库或直链地址">补录来源</button>
                   <button class="btn sm" data-act="model-resolve" data-group="${esc(group)}"
                     data-key="${esc(item.key)}"
                     title="从工作流反推出来的条目不知道出处；点它按补录的仓库 / 文件名 / 直链到线上反查来源">查来源</button>`
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
        // 未下全且可下载的条目：给出分组级批量补下入口，避免逐条点
        const pending = rows.filter((item) => item.present && !item.complete && item.downloadable);
        return `
          <div style="margin-top:8px">
            <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">
              <b style="font-size:12px;color:var(--text)">${label}</b>
              <span class="badge mute">${rows.length} 个</span>
              <span class="hint" style="font-size:11px">${esc(models[group]?.root || '')}</span>
              ${
                pending.length
                  ? `<button class="btn sm" data-act="model-install-incomplete" data-group="${esc(group)}"
                       title="把这一组里未下全的 ${pending.length} 个模型排队补下；需要确认许可的会先弹一次确认">全部补下</button>`
                  : ''
              }
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

/**
 * 轮询下载任务直到结束。
 * @param {string} jobId 任务 ID
 * @param {string} label 展示名称
 * @param {{reload?: boolean}} options reload=false 时由调用方统一提示与刷新（批量补下用）
 * @returns {Promise<boolean>} 是否下载成功
 */
async function pollInstall(jobId, label, { reload = true } = {}) {
  if (envTimer) {
    window.clearInterval(envTimer);
    envTimer = null;
  }
  return await new Promise((resolve) => {
    envTimer = window.setInterval(async () => {
      try {
        const job = await getJSON(`/api/ui/models/install/${encodeURIComponent(jobId)}`);
        paintInstallJob(job, label);
        if (job.status === 'running') return;
        window.clearInterval(envTimer);
        envTimer = null;
        if (job.status === 'done') {
          if (reload) {
            toast(`${label} 下载完成`, 'ok');
            await loadEnvironment();
          }
          resolve(true);
        } else {
          toast(`${label} 下载失败：${job.result?.error || job.status}`, 'bad', 5200);
          resolve(false);
        }
      } catch (error) {
        window.clearInterval(envTimer);
        envTimer = null;
        toast(error.message, 'bad', 4600);
        resolve(false);
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

/**
 * 批量补下：把某个分组里「未下全且可下载」的模型依次排队下载。
 * 有需要确认许可的条目时先弹一次确认框；全部结束后统一提示并刷新清单。
 * @param {string} group 分组（audio / image / video / music）
 * @param {HTMLButtonElement} button 触发按钮
 */
async function installIncomplete(group, button) {
  const rows = (env.models?.[group]?.models || []).filter(
    (item) => item.present && !item.complete && item.downloadable
  );
  if (!rows.length) {
    toast('这一组没有需要补下的模型', 'warn', 2600);
    return;
  }
  const gated = rows.filter((item) => item.license_gate && !['none', ''].includes(item.license_gate));
  const run = async () => {
    button.disabled = true;
    let ok = 0;
    let bad = 0;
    for (let index = 0; index < rows.length; index += 1) {
      const item = rows[index];
      button.textContent = `补下中 ${index + 1}/${rows.length}`;
      toast(`正在补下 ${index + 1}/${rows.length}：${item.display_name || item.key}`, 'ok', 2600);
      try {
        const { job_id: jobId } = await postJSON('/api/ui/models/install', {
          group,
          key: item.key,
          accept_license: true,
          yes: true,
        });
        const done = await pollInstall(jobId, item.display_name || item.key, { reload: false });
        if (done) ok += 1;
        else bad += 1;
      } catch (error) {
        bad += 1;
        toast(`${item.key} 补下失败：${error.message}`, 'bad', 4200);
      }
    }
    button.textContent = '全部补下';
    button.disabled = false;
    toast(`补下完成：成功 ${ok} 个${bad ? `，失败 ${bad} 个` : ''}`, bad ? 'warn' : 'ok', 4600);
    await loadEnvironment();
  };
  if (!gated.length) {
    await run();
    return;
  }
  // 许可只需确认一次；确认后这一批统一带上 accept_license
  const names = gated.map((item) => item.display_name || item.key).join('、');
  const mask = openDialog({
    title: '确认模型许可',
    body: `<div class="dlg-note warn">这一批里有 ${gated.length} 个模型需要确认许可后才能补下：${esc(names)}。
      请确认已理解其商用与分发限制。</div>
      <label style="display:flex;gap:8px;align-items:center;margin-top:10px;font-size:12px">
        <input type="checkbox" id="mi-accept" /> 我已理解并接受这些模型的许可</label>`,
    foot: `<div class="right"><button class="btn" data-dlg-close>取消</button>
      <button class="btn primary" id="mi-go" disabled>开始补下 ${rows.length} 个</button></div>`,
  });
  const box = mask.querySelector('#mi-accept');
  const go = mask.querySelector('#mi-go');
  box.addEventListener('change', () => (go.disabled = !box.checked));
  go.addEventListener('click', async () => {
    closeDialog();
    await run();
  });
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

/** 全局设置的分区：键、名称、说明（左侧导航与地址参数共用） */
const SECTIONS = [
  ['env', '本机环境与模型', '本机环境、模型列表与下载'],
  ['conn', '连接配置', 'ComfyUI 与写作模型连接'],
  ['prefs', '快捷键与界面偏好', '快捷键、字号与显示偏好'],
];

/** 默认分区：地址里的 section 不认识时回落到它，避免空屏 */
const DEFAULT_SECTION = 'env';

/** 左侧目录：全局设置分区导航，点击切换分区（地址同步 section 参数） */
function ctxHTML(active = DEFAULT_SECTION) {
  const items = SECTIONS.map(
    ([key, label, desc]) => `
      <button class="rail-link ${active === key ? 'active' : ''}" data-section="${key}"
        title="${esc(desc)}" aria-current="${active === key ? 'true' : 'false'}">${esc(label)}</button>`
  ).join('');
  return `
    <section class="ctx" aria-label="设置导航">
      <div class="ctx-head"><span class="ct">全局设置</span>
        <span class="badge mute">与工程无关</span>
      </div>
      <div class="set-nav">
        ${items}
        <div class="hint" style="margin-top:6px">这里的设置写入本机配置文件，对所有工程生效；项目级设置请在工程内打开「工程设置」。</div>
      </div>
    </section>`;
}

/**
 * 滚动到指定分区并同步选中态。
 * @param {string} key 分区键
 * @param {ScrollBehavior} behavior 滚动方式
 */
function gotoSection(key, behavior = 'smooth') {
  const node = document.getElementById(key);
  if (node) node.scrollIntoView({ behavior, block: 'start' });
  document.querySelectorAll('.rail-link[data-section]').forEach((item) => {
    const on = item.dataset.section === key;
    item.classList.toggle('active', on);
    item.setAttribute('aria-current', on ? 'true' : 'false');
  });
}

/** 分区标题：全局设置与工程设置分属两个页面 */
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

/**
 * ComfyUI 与写作模型的连接配置卡片。
 * 只渲染全局连接项，不包含任何工程字段；底部给出配置文件实际路径。
 */
function connectionHTML() {
  const conn = connection || {};
  const comfy = conn.comfyui || {};
  const llm = conn.llm || {};
  const templates = comfy.templates || {};
  return `
    <div class="card" style="margin-top:14px">
      <div class="card-head"><span class="ct">连接配置（全局）</span>
        <span class="hint">保存后立即生效，不需要重启</span></div>
      <div class="card-body">
        <div class="conn-block">
          <h4>ComfyUI</h4>
          <div class="field"><label>服务地址</label>
            <input class="input" id="conn-comfy-url" value="${esc(comfy.base_url || '')}"
              placeholder="http://127.0.0.1:8188" title="ComfyUI 服务根地址，例如 http://127.0.0.1:8188" /></div>
          <div class="conn-grid">
            <div class="field"><label>工作流目录</label>
              <select class="select" id="conn-comfy-templates" data-combo
                title="从实际目录读取；需要手填时选末尾「自定义…」"></select>
              <input class="input conn-custom" id="conn-comfy-templates-custom" hidden
                placeholder="输入目录路径，例如 config/comfy" /></div>
            <div class="field"><label>模型目录</label>
              <select class="select" id="conn-comfy-models" data-combo
                title="从实际目录读取；需要手填时选末尾「自定义…」"></select>
              <input class="input conn-custom" id="conn-comfy-models-custom" hidden
                placeholder="输入模型目录路径" /></div>
          </div>
          <div class="conn-grid">
            <div class="field"><label>图像工作流</label>
              <select class="select" id="conn-comfy-tpl-image" data-combo
                title="从工作流目录读取；需要手填时选末尾「自定义…」"></select>
              <input class="input conn-custom" id="conn-comfy-tpl-image-custom" hidden
                placeholder="输入工作流标识" /></div>
            <div class="field"><label>文生视频工作流</label>
              <select class="select" id="conn-comfy-tpl-t2v" data-combo
                title="从工作流目录读取；需要手填时选末尾「自定义…」"></select>
              <input class="input conn-custom" id="conn-comfy-tpl-t2v-custom" hidden
                placeholder="输入工作流标识" /></div>
            <div class="field"><label>图生视频工作流</label>
              <select class="select" id="conn-comfy-tpl-i2v" data-combo
                title="从工作流目录读取；需要手填时选末尾「自定义…」"></select>
              <input class="input conn-custom" id="conn-comfy-tpl-i2v-custom" hidden
                placeholder="输入工作流标识" /></div>
          </div>
          <div class="conn-actions">
            <button class="btn sm" data-act="conn-test-comfy" title="用当前填写的地址测试 ComfyUI 是否可达">测试连通</button>
            <button class="btn sm" data-act="conn-refresh-workflows" title="重新读取工作流目录并刷新三个工作流下拉">刷新工作流</button>
            <button class="btn sm" data-act="conn-refresh-dirs" title="重新读取工作流目录与模型目录的子目录">刷新目录</button>
            <button class="btn sm primary" data-act="conn-save-comfy" title="写入本机配置文件，立即生效">保存</button>
          </div>
          <div class="conn-result" id="conn-comfy-result"></div>
          <div class="hint conn-list-msg" id="conn-comfy-list-msg"></div>
        </div>

        <div class="conn-block" style="margin-top:16px">
          <h4>写作模型（LLM）</h4>
          <div class="conn-grid">
            <div class="field"><label>接口地址（base_url）</label>
              <input class="input" id="conn-llm-endpoint" value="${esc(llm.endpoint || '')}"
                placeholder="http://127.0.0.1:8080/v1" title="OpenAI 兼容接口的 base_url，需包含 /v1" /></div>
            <div class="field"><label>模型名称</label>
              <select class="select" id="conn-llm-model" data-combo
                title="从接口读取可用模型；需要手填时选末尾「自定义…」"></select>
              <input class="input conn-custom" id="conn-llm-model-custom" hidden
                placeholder="输入模型名称" /></div>
          </div>
          <div class="conn-grid">
            <div class="field"><label>API Key</label>
              <input class="input" id="conn-llm-key" type="password" value=""
                placeholder="${llm.api_key_set ? '已保存，留空表示不修改' : '本地服务可留空'}"
                title="留空表示沿用已保存的密钥；本地服务可不填" /></div>
            <div class="field"><label>请求超时（秒）</label>
              <input class="input" id="conn-llm-timeout" type="number" min="1" step="1"
                value="${esc(llm.request_timeout_s ?? '')}" placeholder="120" title="单次请求最长等待时间" /></div>
            <div class="field"><label>连接超时（秒）</label>
              <input class="input" id="conn-llm-connect-timeout" type="number" min="1" step="1"
                value="${esc(llm.connect_timeout_s ?? '')}" placeholder="10" title="建立连接的最长等待时间" /></div>
            <div class="field"><label>最大并发</label>
              <input class="input" id="conn-llm-concurrency" type="number" min="1" step="1"
                value="${esc(llm.max_concurrency ?? '')}" placeholder="2" title="同时进行的写作请求数上限" /></div>
            <div class="field"><label>首字超时（秒）</label>
              <input class="input" id="conn-llm-first-token" type="number" min="1" step="1"
                value="${esc(llm.first_token_timeout_s ?? '')}" placeholder="60" title="流式输出首个字符的最长等待时间" /></div>
          </div>
          <div class="conn-grid">
            <div class="field"><label>上下文窗口（tokens）</label>
              <input class="input" id="conn-llm-context" type="number" min="1" step="1"
                value="${esc(llm.context_window ?? '')}" placeholder="例如 262144"
                title="模型可用的上下文上限；测试连通后按服务端返回值回填，保存后用于请求预算校验" /></div>
            <div class="field"><label>最大输出（tokens）</label>
              <input class="input" id="conn-llm-max-output" type="number" min="1" step="1"
                value="${esc(llm.max_output_tokens ?? '')}" placeholder="例如 32768"
                title="单次生成的最大输出预算；保存后作为写作 / 标注请求的 max_tokens" /></div>
          </div>
          <div class="conn-grid">
            <div class="field"><label>思考</label>
              <label class="set-pref" style="padding-top:4px">
                <input type="checkbox" id="conn-llm-thinking" ${llm.thinking_enabled ? 'checked' : ''}
                  title="开启后写作 / 标注请求会带上思考参数" />
                <span><b>开启思考</b><em>开启后请求带思考参数，速度变慢、结果更稳；关闭时强度不可选</em></span>
              </label>
            </div>
            <div class="field"><label>思考强度</label>
              <select class="select" id="conn-llm-effort" data-enum="thinking_effort" ${llm.thinking_enabled ? '' : 'disabled'}
                title="关闭思考时不可用；开启后随请求的 reasoning_effort 下发">
                <option value="low" ${llm.thinking_effort === 'low' ? 'selected' : ''}>低（更快、更省）</option>
                <option value="medium" ${llm.thinking_effort !== 'low' && llm.thinking_effort !== 'high' ? 'selected' : ''}>中（默认）</option>
                <option value="high" ${llm.thinking_effort === 'high' ? 'selected' : ''}>高（更稳、更慢）</option>
              </select></div>
          </div>
          <div class="conn-actions">
            <button class="btn sm" data-act="conn-test-llm" title="用当前填写的地址、密钥与模型测试连通">测试连通</button>
            <button class="btn sm" data-act="conn-refresh-llm" title="从接口重新读取可用模型列表并刷新下拉">刷新模型</button>
            <button class="btn sm primary" data-act="conn-save-llm" title="写入本机配置文件，立即生效">保存</button>
          </div>
          <div class="conn-result" id="conn-llm-result"></div>
          <div class="hint conn-list-msg" id="conn-llm-limits-msg"></div>
          <div class="conn-models" id="conn-llm-models"></div>
          <div class="hint conn-list-msg" id="conn-llm-list-msg"></div>
        </div>

        <div class="hint" style="margin-top:12px">配置文件：
          <span class="mono">${esc(conn.ui_config_path || 'config/config.ui.yaml')}</span>
          （页面写入的覆盖层，保存即生效；主配置
          <span class="mono">${esc(conn.config_path || 'config/config.yaml')}</span> 不会被改写）</div>
      </div>
    </div>`;
}

/** 主区：只有全局设置，按分区各给一个锚点，左侧导航按 id 滚动 */
function mainHTML() {
  return `
    <div class="page-head" style="padding:18px 18px 0">
      <div class="ph">
        <div class="eyebrow"><span class="ln"></span>设置</div>
        <h1 class="page-title">全局设置</h1>
        <div class="page-sub">这里的设置作用于本机所有工程，与具体工程无关；项目级设置请在工程内打开「工程设置」。</div>
      </div>
    </div>
    <div class="set-body" style="padding:0 18px 18px;overflow:auto;flex:1">
      <section class="set-section" data-section="global">
        ${sectionHead(
          '全局设置（本机）',
          '模型与算力、连接配置、环境自检与模型下载、快捷键、界面偏好'
        )}
        <div id="env" class="set-block">${environmentHTML()}</div>
        <div id="conn" class="set-block">${connectionHTML()}</div>
        <div id="prefs" class="set-block">${shortcutsHTML()}${prefsHTML()}</div>
      </section>
    </div>`;
}


/**
 * 读取全局连接配置（ComfyUI / 写作模型）。
 * 失败时保留空表单，但仍展示配置文件说明，避免整页不可用。
 */
async function loadConnection() {
  try {
    connection = await getJSON('/api/ui/config/connection');
  } catch (error) {
    connection = null;
    toast(`连接配置读取失败：${error.message}`, 'bad', 4000);
  }
}

/**
 * 从模型接口返回里取出模型名（兼容字符串与 {id} 两种形态）。
 * @param {any} item 单个模型项
 * @returns {string} 模型名
 */
function modelName(item) {
  return typeof item === 'string' ? item : item?.id || item?.name || '';
}

/**
 * 生成一组 <option>。
 * @param {Array<{value:string,label:string}>} items 选项
 * @param {string} current 当前值
 * @returns {string} option HTML
 */
function optionsHTML(items, current) {
  return items
    .map(
      (item) =>
        `<option value="${esc(item.value)}" ${item.value === current ? 'selected' : ''}>${esc(item.label)}</option>`
    )
    .join('');
}

/**
 * 填充「真实下拉 + 自定义…」控件。
 * 已保存但不在列表里的值保留为「已保存」项；列表为空时在下拉里直接显示中文原因；
 * 末尾固定「自定义…」，选中后才展开手填输入框（次要入口）。
 * @param {string} id select 的 ID（对应 <id>-custom 手填框）
 * @param {Array<{value:string,label:string}>} items 真实读到的选项
 * @param {string} saved 已保存的值
 * @param {string} emptyText 列表为空时的中文原因
 */
function fillCombo(id, items, saved, emptyText) {
  const select = document.getElementById(id);
  if (!select) return;
  const list = Array.isArray(items) ? items.slice() : [];
  const values = list.map((item) => String(item.value));
  const current = saved || '';
  const opts = [];
  if (!current) opts.push('<option value="">（未选择）</option>');
  if (current && !values.includes(current)) {
    opts.push(`<option value="${esc(current)}" selected>${esc(current)}（已保存）</option>`);
  }
  opts.push(optionsHTML(list, current));
  if (!list.length) {
    opts.push(`<option value="" disabled ${current ? '' : 'selected'}>${esc(emptyText || '没有读到可选值')}</option>`);
  }
  opts.push('<option value="__custom__">自定义…</option>');
  select.innerHTML = opts.join('');
  if (current) select.value = current;
  syncCustom(id);
}

/**
 * 选中「自定义…」时展开手填输入框并聚焦；选回列表项时收起。
 * @param {string} id select 的 ID
 */
function syncCustom(id) {
  const select = document.getElementById(id);
  const input = document.getElementById(`${id}-custom`);
  if (!select || !input) return;
  const custom = select.value === '__custom__';
  input.hidden = !custom;
  if (custom) input.focus();
}

/**
 * 读取下拉当前值（选中「自定义…」时取手填框的值）。
 * @param {string} id select 的 ID
 * @returns {string} 实际值
 */
function comboValue(id) {
  const select = document.getElementById(id);
  if (!select) return '';
  if (select.value === '__custom__') {
    return (document.getElementById(`${id}-custom`)?.value || '').trim();
  }
  return select.value || '';
}

/**
 * 用工作流清单填充三个工作流下拉（真实 select）。
 * @param {Array} workflows 工作流清单
 * @param {object} saved 已保存的三个工作流值
 * @param {string} emptyText 列表为空时的中文原因
 */
function fillWorkflowSelects(workflows, saved, emptyText) {
  const list = Array.isArray(workflows) ? workflows : [];
  const toItems = (rows) => rows.map((item) => ({ value: item.id, label: item.display_name || item.id }));
  fillCombo(
    'conn-comfy-tpl-image',
    toItems(list.filter((item) => String(item.kind || '').startsWith('image'))),
    saved.image,
    emptyText
  );
  fillCombo(
    'conn-comfy-tpl-t2v',
    toItems(list.filter((item) => item.kind === 'video' && String(item.id).includes('t2v'))),
    saved.video_t2v,
    emptyText
  );
  fillCombo(
    'conn-comfy-tpl-i2v',
    toItems(list.filter((item) => item.kind === 'video' && !String(item.id).includes('t2v'))),
    saved.video_i2v,
    emptyText
  );
}

/**
 * 读取目录子项并填充路径下拉。
 * @param {string} id select 的 ID
 * @param {string} value 目录路径
 * @returns {Promise<object|null>} 后端返回；异常返回 null
 */
async function fillDirCombo(id, value) {
  try {
    const result = await getJSON('/api/ui/fs/dirs', { path: value || '' });
    const base = String(result.resolved_dir || '').replace(/\/$/, '');
    const items = (result.dirs || []).map((name) => ({
      value: base ? `${base}/${name}` : name,
      label: name,
    }));
    fillCombo(id, items, value || '', result.ok ? '没有读到子目录' : result.message);
    return result;
  } catch (error) {
    fillCombo(id, [], value || '', `读取目录失败：${error.message}`);
    return null;
  }
}

/**
 * 刷新 ComfyUI 的工作流下拉与两个目录下拉。
 * @param {boolean} withToast 是否弹提示（手动刷新时为 true）
 */
async function refreshComfyOptions(withToast = false) {
  const saved = connection?.comfyui || {};
  const dir = comboValue('conn-comfy-templates') || saved.templates_dir || '';
  const msg = document.getElementById('conn-comfy-list-msg');
  try {
    const result = await getJSON('/api/ui/comfy/workflows', { templates_dir: dir });
    fillWorkflowSelects(
      result.workflows || [],
      {
        image: comboValue('conn-comfy-tpl-image') || saved.templates?.image || '',
        video_t2v: comboValue('conn-comfy-tpl-t2v') || saved.templates?.video_t2v || '',
        video_i2v: comboValue('conn-comfy-tpl-i2v') || saved.templates?.video_i2v || '',
      },
      result.message || '没有读到工作流'
    );
    await Promise.all([
      fillDirCombo('conn-comfy-templates', comboValue('conn-comfy-templates') || saved.templates_dir || ''),
      fillDirCombo('conn-comfy-models', comboValue('conn-comfy-models') || saved.models_dir || ''),
    ]);
    if (msg) {
      msg.className = `hint conn-list-msg ${result.ok ? '' : 'bad'}`;
      msg.textContent = result.ok
        ? `${result.message}；目录：${result.resolved_dir}`
        : `${result.message}（可在下拉里选「自定义…」手填）`;
    }
    if (withToast) toast(result.message, result.ok ? 'ok' : 'warn', 3400);
    return result;
  } catch (error) {
    fillWorkflowSelects([], {}, `读取工作流失败：${error.message}`);
    if (msg) {
      msg.className = 'hint conn-list-msg bad';
      msg.textContent = `读取工作流失败：${error.message}（可在下拉里选「自定义…」手填）`;
    }
    if (withToast) toast(`读取工作流失败：${error.message}`, 'bad', 3800);
    return null;
  }
}

/** 只刷新两个目录下拉；读不到时在下拉里与提示里给中文原因 */
async function refreshDirOptions(withToast = false) {
  const saved = connection?.comfyui || {};
  const msg = document.getElementById('conn-comfy-list-msg');
  const [tpl, models] = await Promise.all([
    fillDirCombo('conn-comfy-templates', comboValue('conn-comfy-templates') || saved.templates_dir || ''),
    fillDirCombo('conn-comfy-models', comboValue('conn-comfy-models') || saved.models_dir || ''),
  ]);
  const bad = [tpl, models].filter((item) => item && !item.ok);
  if (msg) {
    msg.className = `hint conn-list-msg ${bad.length ? 'bad' : ''}`;
    msg.textContent = bad.length
      ? bad.map((item) => item.message).join('；')
      : `已读取目录：${tpl?.resolved_dir || ''}`;
  }
  if (withToast) toast(bad.length ? bad[0].message : '目录已刷新', bad.length ? 'warn' : 'ok', 3200);
}

/**
 * 组装连通性探测请求：空值不发送，避免把已保存的密钥 / 模型覆盖成空。
 * @param {object} form 表单值
 * @returns {object} 探测请求体
 */
function llmProbeBody(form) {
  const body = { endpoint: form.endpoint };
  if (form.api_key) body.api_key = form.api_key;
  if (form.model) body.model = form.model;
  return body;
}

/**
 * 从接口读取可用写作模型并填充下拉；读不到时在下拉里给中文原因。
 * @param {boolean} withToast 是否弹提示
 * @returns {Promise<string[]>} 模型名列表
 */
async function refreshLlmOptions(withToast = false) {
  const form = llmForm();
  const msg = document.getElementById('conn-llm-list-msg');
  try {
    const result = await postJSON('/api/ui/llm/status', llmProbeBody(form));
    const names = (Array.isArray(result.models) ? result.models : []).map(modelName).filter(Boolean);
    fillCombo(
      'conn-llm-model',
      names.map((name) => ({ value: name, label: name })),
      form.model || connection?.llm?.model || '',
      result.message || '接口没有返回可用模型'
    );
    if (msg) {
      msg.className = `hint conn-list-msg ${names.length ? '' : 'bad'}`;
      msg.textContent = names.length
        ? `已读取 ${names.length} 个可用模型`
        : `${result.message || '没有读到可用模型'}（可在下拉里选「自定义…」手填）`;
    }
    if (withToast) {
      toast(names.length ? `已读取 ${names.length} 个模型` : '没有读到可用模型，可选「自定义…」手填', names.length ? 'ok' : 'warn', 3200);
    }
    return names;
  } catch (error) {
    fillCombo('conn-llm-model', [], form.model || '', `读取模型列表失败：${error.message}`);
    if (msg) {
      msg.className = 'hint conn-list-msg bad';
      msg.textContent = `读取模型列表失败：${error.message}（可在下拉里选「自定义…」手填）`;
    }
    if (withToast) toast(`读取模型列表失败：${error.message}`, 'bad', 3800);
    return [];
  }
}

/** 收集 ComfyUI 表单值 */
function comfyForm() {
  return {
    base_url: document.getElementById('conn-comfy-url')?.value.trim() || '',
    templates_dir: comboValue('conn-comfy-templates'),
    models_dir: comboValue('conn-comfy-models'),
    templates: {
      image: comboValue('conn-comfy-tpl-image'),
      video_t2v: comboValue('conn-comfy-tpl-t2v'),
      video_i2v: comboValue('conn-comfy-tpl-i2v'),
    },
  };
}

/** 收集写作模型表单值（key 留空表示不修改已保存密钥） */
function llmForm() {
  return {
    endpoint: document.getElementById('conn-llm-endpoint')?.value.trim() || '',
    model: comboValue('conn-llm-model'),
    api_key: document.getElementById('conn-llm-key')?.value.trim() || '',
    request_timeout_s: document.getElementById('conn-llm-timeout')?.value || '',
    connect_timeout_s: document.getElementById('conn-llm-connect-timeout')?.value || '',
    max_concurrency: document.getElementById('conn-llm-concurrency')?.value || '',
    first_token_timeout_s: document.getElementById('conn-llm-first-token')?.value || '',
    context_window: document.getElementById('conn-llm-context')?.value || '',
    max_tokens: document.getElementById('conn-llm-max-output')?.value || '',
    enable_thinking: Boolean(document.getElementById('conn-llm-thinking')?.checked),
    reasoning_effort: document.getElementById('conn-llm-effort')?.value || 'medium',
  };
}

/**
 * 在连接卡片里显示测试/保存结果。
 * @param {string} id 结果容器 ID
 * @param {boolean} ok 是否成功
 * @param {string} text 中文提示
 */
function setConnResult(id, ok, text) {
  const node = document.getElementById(id);
  if (!node) return;
  node.className = `conn-result ${ok ? 'ok' : 'bad'}`;
  node.innerHTML = `${icon(ok ? 'check' : 'warn', 13)}<span>${esc(text)}</span>`;
}

/** 测试 ComfyUI 连通（用表单里的地址，不要求先保存） */
async function testComfy(button) {
  const baseUrl = document.getElementById('conn-comfy-url')?.value.trim() || '';
  button.disabled = true;
  setConnResult('conn-comfy-result', true, '正在测试…');
  try {
    const result = await postJSON('/api/ui/comfy/health', { base_url: baseUrl });
    setConnResult('conn-comfy-result', Boolean(result.ok), result.message || (result.ok ? 'ComfyUI 连接正常' : 'ComfyUI 连接失败'));
    if (result.ok) {
      toast('ComfyUI 连接正常', 'ok', 2200);
      // 连上之后顺手把工作流下拉刷成目录里的真实文件
      void refreshComfyOptions();
    }
  } catch (error) {
    setConnResult('conn-comfy-result', false, error.message || 'ComfyUI 连接失败');
  } finally {
    button.disabled = false;
  }
}

/** 测试写作模型连通，并把返回的模型清单展示出来 */
async function testLlm(button) {
  const form = llmForm();
  button.disabled = true;
  setConnResult('conn-llm-result', true, '正在测试…');
  const modelsBox = document.getElementById('conn-llm-models');
  try {
    const result = await postJSON('/api/ui/llm/status', llmProbeBody(form));
    const ok = Boolean(result.connected);
    const text = result.message || (ok ? '写作模型连接正常' : '写作模型连接失败');
    setConnResult('conn-llm-result', ok && result.model_found !== false, text);
    const names = (Array.isArray(result.models) ? result.models : []).map(modelName).filter(Boolean);
    if (modelsBox) {
      modelsBox.innerHTML = names.length
        ? `<div class="hint">可用模型（${names.length}）：${names.slice(0, 30).map((name) => esc(name)).join('、')}</div>`
        : '';
    }
    // 连通后把模型下拉补成真实列表，方便直接选
    fillCombo(
      'conn-llm-model',
      names.map((name) => ({ value: name, label: name })),
      form.model || '',
      result.message || '接口没有返回可用模型'
    );
    const listMsg = document.getElementById('conn-llm-list-msg');
    if (listMsg && names.length) {
      listMsg.className = 'hint conn-list-msg';
      listMsg.textContent = `已读取 ${names.length} 个可用模型`;
    }
    // 把服务端返回的上下文 / 最大输出反显成可编辑字段，用户可直接改完保存
    const contextValue = Number(result.context_window);
    const outputValue = Number(result.max_output_tokens);
    const contextInput = document.getElementById('conn-llm-context');
    const outputInput = document.getElementById('conn-llm-max-output');
    if (contextInput && Number.isFinite(contextValue) && contextValue > 0) {
      contextInput.value = String(contextValue);
    }
    if (outputInput && Number.isFinite(outputValue) && outputValue > 0) {
      outputInput.value = String(outputValue);
    }
    const limitsMsg = document.getElementById('conn-llm-limits-msg');
    if (limitsMsg) {
      const hasLimits = (Number.isFinite(contextValue) && contextValue > 0) || (Number.isFinite(outputValue) && outputValue > 0);
      limitsMsg.className = 'hint conn-list-msg';
      limitsMsg.textContent = hasLimits
        ? `服务端字段：上下文 ${contextValue > 0 ? contextValue : '未知'} / 最大输出 ${
            outputValue > 0 ? outputValue : '未知'
          } tokens，已回填到上方字段，可直接修改后保存。`
        : '服务端未返回上下文 / 最大输出字段，可在上方直接手填后保存。';
    }
  } catch (error) {
    setConnResult('conn-llm-result', false, error.message || '写作模型连接失败');
  } finally {
    button.disabled = false;
  }
}

/** 保存连接配置（section: comfyui / llm），保存后刷新本机环境卡片 */
async function saveConnection(section, button) {
  const payload = section === 'comfyui' ? { comfyui: comfyForm() } : { llm: llmForm() };
  const resultId = section === 'comfyui' ? 'conn-comfy-result' : 'conn-llm-result';
  button.disabled = true;
  setConnResult(resultId, true, '正在保存…');
  try {
    connection = await postJSON('/api/ui/config/connection', payload);
    setConnResult(resultId, true, `已保存到 ${connection.saved_to || connection.ui_config_path}，立即生效`);
    toast('连接配置已保存，立即生效', 'ok', 2400);
    // 让「写作模型检查」「ComfyUI 状态」按新配置重新取一次
    env.llm = null;
    env.comfy = null;
    await loadEnvironment();
  } catch (error) {
    setConnResult(resultId, false, error.message || '保存失败');
  } finally {
    button.disabled = false;
  }
}

/** 点击 */
function onRootClick(event) {
  // 左侧分区导航：切区块 + 同步地址，刷新后仍停在当前分区
  const nav = event.target.closest('.rail-link[data-section]');
  if (nav) {
    const key = nav.dataset.section;
    gotoSection(key);
    history.replaceState(null, '', `#/settings?section=${encodeURIComponent(key)}`);
    return;
  }
  const button = event.target.closest('[data-act]');
  if (!button) return;
  if (button.dataset.act === 'back') return goProjects();
  if (button.dataset.act === 'shortcuts-help') return openShortcuts();
  if (button.dataset.act === 'env-refresh') return void loadEnvironment();
  if (button.dataset.act === 'comfy-check') return void checkComfy(button);
  if (button.dataset.act === 'llm-check') return void checkLlm(button);
  if (button.dataset.act === 'conn-test-comfy') return void testComfy(button);
  if (button.dataset.act === 'conn-refresh-workflows') return void refreshComfyOptions(true);
  if (button.dataset.act === 'conn-refresh-dirs') return void refreshDirOptions(true);
  if (button.dataset.act === 'conn-save-comfy') return void saveConnection('comfyui', button);
  if (button.dataset.act === 'conn-test-llm') return void testLlm(button);
  if (button.dataset.act === 'conn-refresh-llm') return void refreshLlmOptions(true);
  if (button.dataset.act === 'conn-save-llm') return void saveConnection('llm', button);
  if (button.dataset.act === 'model-install') {
    return void startInstall(button.dataset.group, button.dataset.key, button.dataset.gate);
  }
  if (button.dataset.act === 'model-install-incomplete') {
    return void installIncomplete(button.dataset.group, button);
  }
  if (button.dataset.act === 'model-manual') {
    return openManualModel(button.dataset.group, button.dataset.key);
  }
  if (button.dataset.act === 'model-resolve') {
    button.disabled = true;
    void postJSON('/api/ui/models/resolve', { group: button.dataset.group, key: button.dataset.key })
      .then((result) => {
        // 直白表达：反查到就写「来源：owner/repo（或直链）」，没反查到明确写「来源未知」
        const source = result.repo || result.url || '';
        toast(
          source
            ? `来源：${source}`
            : '来源未知：没有反查到仓库或直链，可在「补录来源」里手动填写',
          source ? 'ok' : 'warn',
          4400
        );
        return loadEnvironment();
      })
      .catch((error) => toast(`查来源失败：${error.message.replace(/^解析失败：/, '')}`, 'bad', 4800))
      .finally(() => {
        button.disabled = false;
      });
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

/** 下拉选择 / 勾选：界面偏好写本地 */
function onRootChange(event) {
  // 思考开关与强度联动：关掉思考时强度置灰不可选
  if (event.target.id === 'conn-llm-thinking') {
    const effort = document.getElementById('conn-llm-effort');
    if (effort) effort.disabled = !event.target.checked;
    return;
  }
  // 下拉切到「自定义…」时展开手填框
  if (event.target.matches('[data-combo]')) {
    syncCustom(event.target.id);
    return;
  }
  const pref = event.target.closest('[data-pref]');
  if (!pref) return;
  const name = pref.dataset.pref;
  applyPref(name, name === 'fontSize' ? pref.value : pref.checked);
}

/** 进入全局设置：不接收任何工程参数；section 决定默认停靠的分区 */
export async function renderSettings(root, target = {}) {
  const wanted = String(target.section || '');
  // 地址里的 section 不认识时回落到默认分区，绝不留空屏
  const section = SECTIONS.some(([key]) => key === wanted) ? wanted : DEFAULT_SECTION;
  if (envTimer) {
    window.clearInterval(envTimer);
    envTimer = null;
  }
  if (!bound) {
    root.addEventListener('click', onRootClick);
    root.addEventListener('change', onRootChange);
    bound = true;
  }
  await loadConnection();
  root.innerHTML = `
    ${topbar({
      left: `<button class="btn icon" data-act="back" title="回工程列表">${icon('back')}</button>
             <div class="brand"><span class="brand-mark"></span><span style="font-size:12.5px">设置<small>全局</small></span></div>`,
      crumbs: `<a href="#/projects">工程列表</a>
               <span class="sep">/</span><span class="cur">全局设置</span>`,
      right: `<span class="save-state ok">${icon('check', 12)}全局设置即时生效</span>`,
    })}
    <div class="studio" id="studio">
      ${railbar('settings')}
      ${ctxHTML(section)}
      <section class="editor">
        <div id="settings-main" style="display:flex;flex-direction:column;min-height:0;flex:1">${mainHTML()}</div>
      </section>
      <aside class="studio-right"></aside>
    </div>`;
  // 渲染完再滚动，保证目标分区已存在；刷新时也停在同一分区
  requestAnimationFrame(() => gotoSection(section, 'auto'));
  void loadEnvironment();
  // 进页面就按当前配置拉一次工作流与模型列表，读不到时保留手填
  void refreshComfyOptions();
  void refreshLlmOptions();
}

