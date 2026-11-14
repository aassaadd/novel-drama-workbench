/**
 * 全站统一的 AI 生成进度浮层。
 *
 * 设计要点：
 * - 独立于 dom.js 的 openDialog / activeDialog 单例，挂在 document.body 上；
 *   其它弹窗打开、页面重渲染或路由切换都不会把它顶掉。
 * - 生成中显示阶段、进度条、百分比、当前项与实时输出；结束（成功或失败）后
 *   保留结果与关闭按钮，失败给出中文原因和「重试」入口，不会静默消失。
 * - 支持同时存在多个浮层（例如批量素材和单条音效），按打开顺序从下往上排列。
 */
import { esc, icon, toast } from './dom.js';
import { getJSON } from './api.js';

/** 浮层自增序号，用于给每个卡片一个独立标识 */
let seq = 0;

/** 运行中的浮层（后打开的在最后），SSE 事件自动投递给最新一个 */
const runningPanels = [];
/** 是否已把流式事件入口挂到 window 上 */
let tapBound = false;

/**
 * 把 SSE 进度事件接到当前最新的浮层上。
 * 只处理批次 / 条目类事件；正文 delta 由各页面自行 append，避免重复。
 */
function bindStreamTap() {
  if (tapBound) return;
  tapBound = true;
  window.__ndwLiveTap = (event, data) => {
    const panel = runningPanels[runningPanels.length - 1];
    if (panel) panel.tap(event, data);
  };
}

/**
 * 取（必要时创建）浮层容器。
 * @returns {HTMLElement} `#progress-host`
 */
function host() {
  let node = document.getElementById('progress-host');
  if (!node) {
    node = document.createElement('div');
    node.id = 'progress-host';
    node.className = 'prog-host';
    document.body.appendChild(node);
  }
  return node;
}

/** 秒数 → 00:12 */
function clock(seconds) {
  const total = Math.max(0, Math.floor(seconds));
  return `${String(Math.floor(total / 60)).padStart(2, '0')}:${String(total % 60).padStart(2, '0')}`;
}

/**
 * 打开一个进度浮层。
 *
 * @param {object} options 配置
 * @param {string} options.title 标题（例如「正在生成第 3 章正文」）
 * @param {string} [options.hint] 副标题（说明本次会做什么）
 * @param {string} [options.stage] 初始阶段文案
 * @param {boolean} [options.stream] 是否展示实时输出区
 * @returns {{
 *   update: (patch: {stage?: string, percent?: number|null, current?: string, detail?: string}) => void,
 *   append: (text: string) => void,
 *   finish: (result: {message?: string, detail?: string, actions?: Array<{label: string, primary?: boolean, onClick: Function}>}) => void,
 *   fail: (result: {message: string, detail?: string, retry?: Function}) => void,
 *   close: () => void
 * }} 控制器
 */
export function openProgress({ title, hint = '', stage = '准备中…', stream = false } = {}) {
  const id = `prog-${++seq}`;
  const card = document.createElement('section');
  card.className = 'prog-card';
  card.dataset.prog = id;
  card.setAttribute('role', 'status');
  card.setAttribute('aria-live', 'polite');
  card.innerHTML = `
    <div class="prog-head">
      <span class="prog-spinner" aria-hidden="true"></span>
      <span class="prog-title">${esc(title)}</span>
      <span class="prog-time mono">00:00</span>
      <button class="prog-close" type="button" data-prog-close
        title="收起进度面板（不会中断后台生成）">${icon('x', 12)}</button>
    </div>
    <div class="prog-hint" ${hint ? '' : 'hidden'}>${esc(hint)}</div>
    <div class="prog-stage">${esc(stage)}</div>
    <div class="prog-bar"><span></span></div>
    <div class="prog-meta">
      <span class="prog-percent mono">—</span>
      <span class="prog-current"></span>
    </div>
    <div class="prog-detail hint" hidden></div>
    <div class="prog-live" hidden></div>
    <div class="prog-result" hidden></div>
    <div class="prog-actions"></div>`;

  host().appendChild(card);

  const el = {
    spinner: card.querySelector('.prog-spinner'),
    title: card.querySelector('.prog-title'),
    time: card.querySelector('.prog-time'),
    close: card.querySelector('.prog-close'),
    hint: card.querySelector('.prog-hint'),
    stage: card.querySelector('.prog-stage'),
    bar: card.querySelector('.prog-bar > span'),
    percent: card.querySelector('.prog-percent'),
    current: card.querySelector('.prog-current'),
    detail: card.querySelector('.prog-detail'),
    live: card.querySelector('.prog-live'),
    result: card.querySelector('.prog-result'),
    actions: card.querySelector('.prog-actions'),
  };

  /** 已运行秒数（结束或关闭后停止） */
  const startedAt = Date.now();
  let timer = null;
  /** 实时输出累计字数 */
  let received = 0;
  /** 最近一次收到模型原文片段的时间（毫秒），用于避免批次/条目名立刻把它顶掉 */
  let lastCharsAt = 0;
  /** 实时输出只保留末尾一段，界面始终显示一行动态文字 */
  let liveTail = '';
  /** 是否仍在生成（结束后不再接受 update） */
  let running = true;

  /** 把实时内容压成一行：去掉换行、只留末尾 160 字 */
  const oneLine = (text) => String(text || '').replace(/\s+/g, ' ').trim().slice(-160);

  /** 结束运行态：停表、停掉转圈图标 */
  const settle = (kind) => {
    if (!running) return;
    running = false;
    const index = runningPanels.indexOf(controller);
    if (index >= 0) runningPanels.splice(index, 1);
    if (timer) {
      window.clearInterval(timer);
      timer = null;
    }
    card.classList.remove('indeterminate');
    card.classList.add(kind);
    el.spinner.classList.add(kind === 'done' ? 'ok' : 'bad');
    el.spinner.innerHTML = icon(kind === 'done' ? 'check' : 'warn', 13);
    el.time.textContent = clock((Date.now() - startedAt) / 1000);
  };

  if (running) {
    timer = window.setInterval(() => {
      el.time.textContent = clock((Date.now() - startedAt) / 1000);
    }, 1000);
  }

  /**
   * 移除浮层；运行中关闭只收起界面，不中断后台任务。
   */
  const close = () => {
    if (running) toast('已收起进度面板，生成仍在后台继续', 'info', 2600);
    if (timer) {
      window.clearInterval(timer);
      timer = null;
    }
    running = false;
    const index = runningPanels.indexOf(controller);
    if (index >= 0) runningPanels.splice(index, 1);
    card.remove();
    if (!host().children.length) host().remove();
  };

  el.close.addEventListener('click', close);

  const controller = {
    /**
     * 更新阶段 / 进度 / 当前项。
     * @param {{stage?: string, percent?: number|null, current?: string, detail?: string}} patch
     */
    update(patch = {}) {
      if (!running) return;
      if (patch.stage) el.stage.textContent = patch.stage;
      if (patch.current !== undefined) el.current.textContent = patch.current;
      if (patch.detail !== undefined) {
        el.detail.textContent = patch.detail;
        el.detail.hidden = !patch.detail;
      }
      if (patch.percent === undefined) return;
      const percent = Number(patch.percent);
      if (patch.percent === null || Number.isNaN(percent)) {
        card.classList.add('indeterminate');
        el.percent.textContent = '进行中';
        return;
      }
      card.classList.remove('indeterminate');
      const safe = Math.max(0, Math.min(100, Math.round(percent)));
      el.bar.style.width = `${safe}%`;
      el.percent.textContent = `${safe}%`;
    },

    /**
     * 追加实时输出（流式正文）：只保留末尾内容并始终显示为一行。
     * @param {string} text 增量文本
     */
    append(text) {
      if (!running) return;
      const chunk = String(text || '');
      if (!chunk) return;
      received += Array.from(chunk).length;
      liveTail = oneLine(liveTail + chunk);
      if (!liveTail) return;
      el.live.hidden = false;
      el.live.textContent = liveTail;
      el.current.textContent = `已接收 ${received} 字`;
    },

    /**
     * 显示最近一条模型 / 批次返回内容（不累加，替换成一行）。
     * @param {string} text 内容摘要
     */
    note(text) {
      if (!running) return;
      const line = oneLine(text);
      if (!line) return;
      el.live.hidden = false;
      el.live.textContent = line;
    },

    /**
     * 接收全局 SSE 事件：批次 / 条目类事件自动变成一行最新内容。
     * 正文 delta 由页面自行 append，这里跳过避免重复。
     * @param {string} event 事件名
     * @param {any} data 事件负载
     */
    tap(event, data) {
      if (!running) return;
      if (event === 'start' || event === 'delta' || event === 'done') return;
      // 工具型生成（大纲 / 世界 / 角色 / 标注）的 chars 事件带最新原文片段
      if (event === 'chars') {
        if (data?.sample) {
          lastCharsAt = Date.now();
          this.note(String(data.sample));
        }
        return;
      }
      // 模型原文刚更新过时，不让紧随其后的批次 / 条目名把实时内容顶掉
      if (['batch', 'item'].includes(event) && Date.now() - lastCharsAt < 4000) return;
      const text =
        data?.name ||
        data?.title ||
        data?.stage ||
        data?.message ||
        data?.current ||
        data?.candidate?.name ||
        data?.item?.name ||
        '';
      this.note(String(text));
    },

    /**
     * 成功结束：保留面板与结果，提供关闭 / 后续操作按钮。
     * @param {{message?: string, detail?: string, actions?: Array<{label: string, primary?: boolean, onClick: Function}>}} result
     */
    finish(result = {}) {
      settle('done');
      el.stage.textContent = '已完成';
      card.classList.remove('indeterminate');
      el.bar.style.width = '100%';
      el.percent.textContent = '100%';
      if (!el.current.textContent) el.current.textContent = '';
      el.result.hidden = false;
      el.result.className = 'prog-result ok';
      el.result.innerHTML = `${icon('check', 12)} ${esc(result.message || '生成完成')}${
        result.detail ? `<div class="prog-detail hint">${esc(result.detail)}</div>` : ''
      }`;
      el.actions.innerHTML = '';
      for (const action of result.actions || []) {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = `btn sm ${action.primary ? 'primary' : ''}`;
        button.textContent = action.label;
        button.addEventListener('click', () => action.onClick?.(close));
        el.actions.appendChild(button);
      }
      const done = document.createElement('button');
      done.type = 'button';
      done.className = 'btn sm';
      done.textContent = '关闭';
      done.addEventListener('click', close);
      el.actions.appendChild(done);
    },

    /**
     * 失败结束：给出中文原因，可选「重试」。
     * @param {{message: string, detail?: string, retry?: Function}} result
     */
    fail(result = {}) {
      settle('failed');
      el.stage.textContent = '生成失败';
      card.classList.remove('indeterminate');
      el.result.hidden = false;
      el.result.className = 'prog-result bad';
      el.result.innerHTML = `${icon('warn', 12)} ${esc(result.message || '生成失败')}${
        result.detail ? `<div class="prog-detail hint">${esc(result.detail)}</div>` : ''
      }`;
      el.actions.innerHTML = '';
      if (result.retry) {
        const retry = document.createElement('button');
        retry.type = 'button';
        retry.className = 'btn sm primary';
        retry.textContent = '重试';
        retry.addEventListener('click', () => {
          close();
          result.retry();
        });
        el.actions.appendChild(retry);
      }
      const done = document.createElement('button');
      done.type = 'button';
      done.className = 'btn sm';
      done.textContent = '关闭';
      done.addEventListener('click', close);
      el.actions.appendChild(done);
    },

    close,
  };

  runningPanels.push(controller);
  bindStreamTap();
  return controller;
}

/**
 * 轮询后台批量任务进度并用浮层展示（配音补齐等由后端队列执行的任务）。
 * 任务结束后保留结果与「去任务页看明细」入口。
 *
 * @param {object} options 配置
 * @param {string} options.path 工程目录
 * @param {string} options.title 标题
 * @param {string} [options.hint] 说明
 * @param {string} [options.endpoint] 进度接口，默认配音批量进度
 * @param {number} [options.interval] 轮询间隔毫秒
 * @param {(data: object) => void} [options.onRefresh] 每次拿到进度后的回调
 * @returns {{stop: () => void}} 控制器
 */
export function watchProgress({
  path,
  title,
  hint = '',
  endpoint = '/api/ui/tts/batch-all/progress',
  interval = 2000,
  onRefresh,
}) {
  const options = { path, title, hint, endpoint, interval, onRefresh };
  const panel = openProgress({ title, hint, stage: '后台任务已启动，正在读取进度…' });
  panel.update({ percent: 0 });
  let stopped = false;
  let timer = null;
  let errors = 0;

  /** 读一次进度；任务结束或连续失败时收尾 */
  const tick = async () => {
    if (stopped) return;
    try {
      const data = await getJSON(endpoint, { path });
      errors = 0;
      onRefresh?.(data);
      const total = Number(data?.total || 0);
      const done = Number(data?.done || 0);
      const failed = Number(data?.failed || 0);
      if (data?.running) {
        panel.update({
          stage: `正在生成 ${done}/${total}${failed ? ` · 失败 ${failed}` : ''}`,
          percent: total ? Math.round((done / total) * 100) : null,
          current: data?.eta_ms ? `预计剩余 ${Math.max(1, Math.round(Number(data.eta_ms) / 1000))} 秒` : '',
        });
      } else {
        stopped = true;
        if (timer) window.clearTimeout(timer);
        panel.finish({
          message: total
            ? `本轮完成：${done}/${total}${failed ? ` · 失败 ${failed}` : ''}`
            : '任务已结束（没有可生成的条目）',
          detail: failed ? '失败行可在任务页逐条重试。' : '',
          actions: [
            {
              label: '去任务页看明细',
              primary: true,
              onClick: () => {
                window.location.hash = `#/p/${encodeURIComponent(path)}/tasks`;
              },
            },
          ],
        });
        return;
      }
    } catch (error) {
      errors += 1;
      if (errors >= 3) {
        stopped = true;
        panel.fail({
          message: `读取进度失败：${error.message}`,
          retry: () => void watchProgress(options),
        });
        return;
      }
      panel.update({ stage: `暂时读不到进度（${error.message}），正在重试…` });
    }
    if (!stopped) timer = window.setTimeout(tick, interval);
  };
  void tick();

  return {
    stop() {
      stopped = true;
      if (timer) window.clearTimeout(timer);
    },
  };
}
