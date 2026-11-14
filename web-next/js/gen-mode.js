/**
 * AI 生成方式：只补缺失 / 重新生成。
 *
 * 每个入口用独立的 key 记住上次选择（localStorage），
 * 生成前的分段按钮与提交按钮文案都跟着选择变化，
 * 避免用户点下去才发现是「补」还是「重写」。
 */

const STORE_KEY = 'ndw.genMode';

/**
 * 读取全部入口的生成方式。
 * @returns {Record<string, 'fill'|'regen'>}
 */
function readAll() {
  try {
    const parsed = JSON.parse(localStorage.getItem(STORE_KEY) || '{}');
    return parsed && typeof parsed === 'object' ? parsed : {};
  } catch {
    return {};
  }
}

/**
 * 取某个入口上次选择的生成方式。
 * @param {string} key 入口标识（如 `setting.premise`）
 * @param {'fill'|'regen'} [fallback] 没记录时的默认值
 * @returns {'fill'|'regen'}
 */
export function getGenMode(key, fallback = 'regen') {
  const value = readAll()[key];
  return value === 'fill' || value === 'regen' ? value : fallback;
}

/**
 * 记住某个入口的生成方式。
 * @param {string} key 入口标识
 * @param {'fill'|'regen'} mode 生成方式
 */
export function setGenMode(key, mode) {
  if (mode !== 'fill' && mode !== 'regen') return;
  const all = readAll();
  all[key] = mode;
  try {
    localStorage.setItem(STORE_KEY, JSON.stringify(all));
  } catch {
    // 隐私模式下写入失败不影响本次生成
  }
}

/**
 * 生成方式的说明文案。
 * @param {'fill'|'regen'} mode 生成方式
 * @returns {string}
 */
export function genModeHint(mode) {
  return mode === 'fill' ? '已有内容会保留并跳过，只生成缺失部分' : '忽略已有内容，重新生成并覆盖';
}

/**
 * 生成方式的分段选择器（可直接插进弹窗 / 面板）。
 * @param {string} key 入口标识
 * @param {{fallback?: 'fill'|'regen'}} [options] 默认方式
 * @returns {string} HTML
 */
export function modeSwitchHTML(key, { fallback = 'regen' } = {}) {
  const mode = getGenMode(key, fallback);
  return `<div class="gen-mode" data-gen-mode="${key}">
    <div class="seg-chips" role="group" aria-label="生成方式">
      <button type="button" class="chip ${mode === 'fill' ? 'on' : ''}" data-mode="fill"
        title="跳过已有内容，只生成缺失部分">只补缺失</button>
      <button type="button" class="chip ${mode === 'regen' ? 'on' : ''}" data-mode="regen"
        title="忽略已有内容，重新生成并覆盖">重新生成</button>
    </div>
    <div class="hint" data-mode-hint>${genModeHint(mode)}</div>
  </div>`;
}

/**
 * 给生成方式分段按钮绑定交互，并同步提交按钮文案。
 * @param {HTMLElement} scope 包含 `modeSwitchHTML` 的容器
 * @param {string} key 入口标识
 * @param {{fallback?: 'fill'|'regen', action?: HTMLElement|null, labels?: {fill?: string, regen?: string}}} [options]
 *        action 为提交按钮；labels 为两种方式下提交按钮的文案
 * @returns {() => 'fill'|'regen'} 读取当前选择
 */
export function bindModeSwitch(scope, key, { fallback = 'regen', action = null, labels = {} } = {}) {
  const box = scope.querySelector(`[data-gen-mode="${key}"]`);
  const hint = box?.querySelector('[data-mode-hint]');
  const fillLabel = labels.fill || '补缺失';
  const regenLabel = labels.regen || '重新生成';

  /** 同步分段按钮、说明与提交按钮文案 */
  const sync = (mode) => {
    if (!box) return;
    box.querySelectorAll('[data-mode]').forEach((item) => {
      item.classList.toggle('on', item.dataset.mode === mode);
    });
    if (hint) hint.textContent = genModeHint(mode);
    if (action) action.textContent = mode === 'fill' ? fillLabel : regenLabel;
  };

  if (box) {
    box.querySelectorAll('[data-mode]').forEach((button) => {
      button.addEventListener('click', () => {
        const mode = button.dataset.mode;
        setGenMode(key, mode);
        sync(mode);
      });
    });
    sync(getGenMode(key, fallback));
  }
  return () => getGenMode(key, fallback);
}
