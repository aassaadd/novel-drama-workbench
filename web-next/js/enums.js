/**
 * 枚举字典：枚举字段的唯一中文标签来源。
 *
 * 后端仍存枚举值（如 novella / male / third_limited），前端只在两处使用本文件：
 * 1. 编辑态：`selectHTML` 生成真正的下拉，禁止自由输入；
 * 2. 展示态：`labelOf` 反显中文，不把裸值直接呈现给用户。
 */

/** 全部枚举：键 → { 中文名, 选项 [[枚举值, 中文标签], ...] } */
export const ENUMS = {
  target_length: {
    label: '目标篇幅',
    options: [
      ['short', '短篇'],
      ['novella', '中篇'],
      ['novel', '长篇'],
      ['series', '长篇连载'],
    ],
  },
  pov: {
    label: '叙事视角',
    options: [
      ['first', '第一人称'],
      ['third_limited', '第三人称 · 有限视角'],
      ['third_omniscient', '第三人称 · 全知视角'],
      ['second', '第二人称'],
    ],
  },
  tense: {
    label: '时态',
    options: [
      ['past', '过去时'],
      ['present', '现在时'],
    ],
  },
  role: {
    label: '角色定位',
    options: [
      ['narrator', '旁白'],
      ['protagonist', '主角'],
      ['antagonist', '反派'],
      ['supporting', '配角'],
      ['minor', '群演'],
    ],
  },
  gender: {
    label: '性别',
    options: [
      ['male', '男'],
      ['female', '女'],
      ['other', '其他'],
      ['unknown', '未指定'],
    ],
  },
  age_range: {
    label: '年龄段',
    options: [
      ['10-14', '儿童（10-14 岁）'],
      ['18-25', '青年（18-25 岁）'],
      ['26-39', '成年（26-39 岁）'],
      ['40-54', '中年（40-54 岁）'],
      ['55-70', '老年（55-70 岁）'],
    ],
  },
  emotion: {
    label: '情绪',
    options: [
      ['neutral', '平静'],
      ['joy', '喜悦'],
      ['sad', '悲伤'],
      ['angry', '愤怒'],
      ['fear', '恐惧'],
      ['surprise', '惊讶'],
      ['tender', '温柔'],
      ['excited', '激动'],
      ['whisper', '低语'],
      ['serious', '严肃'],
    ],
  },
  bgm_mode: {
    label: '音乐用途',
    options: [
      ['narrative', '叙事型（跟着剧情走）'],
      ['bed', '铺底型（环境垫底）'],
    ],
  },
  export_format: {
    label: '音频格式',
    options: [
      ['mp3', 'MP3（通用有损）'],
      ['wav', 'WAV（无损）'],
      ['m4a', 'M4A（苹果设备常用）'],
    ],
  },
  sample_rate: {
    label: '采样率',
    options: [
      ['44100', '44.1 kHz'],
      ['48000', '48 kHz'],
    ],
  },
  bitrate: {
    label: '目标码率',
    options: [
      ['192', '192 kbps'],
      ['256', '256 kbps'],
    ],
  },
  language: {
    label: '语言',
    options: [
      ['zh-CN', '简体中文'],
      ['zh-TW', '繁体中文'],
      ['en-US', '英语'],
    ],
  },
};

/**
 * 生成下拉的 option 列表。
 * @param {string} key 枚举键（ENUMS 的 key）
 * @param {*} current 当前枚举值
 * @param {{emptyLabel?: string, extra?: string[]}} options emptyLabel 非空时插入空值项；extra 为历史自由值（如旧年龄区间），保留为可选项
 * @returns {string} `<option>` 拼接结果
 */
export function optionsHTML(key, current, { emptyLabel = '', extra = [] } = {}) {
  const spec = ENUMS[key] || { options: [] };
  const all = spec.options.slice();
  const known = new Set(all.map(([value]) => String(value)));
  // 历史数据不在枚举里：追加为可选项，避免打开表单时把已有值悄悄丢掉
  extra.forEach((value) => {
    const text = String(value ?? '').trim();
    if (!text || known.has(text)) return;
    all.push([text, text]);
    known.add(text);
  });
  const now = String(current ?? '').trim();
  const rows = all.map(
    ([value, label]) => `<option value="${value}" ${String(value) === now ? 'selected' : ''}>${label}</option>`
  );
  if (emptyLabel) rows.unshift(`<option value="" ${now ? '' : 'selected'}>${emptyLabel}</option>`);
  return rows.join('');
}

/**
 * 生成一个真正的枚举下拉控件（带 data-enum 标记，供扫描脚本核对）。
 * @param {string} key 枚举键
 * @param {{id?:string,current?:*,title?:string,emptyLabel?:string,extra?:string[],className?:string,attrs?:string}} options 控件配置
 * @returns {string} `<select>` HTML
 */
export function selectHTML(
  key,
  { id = '', current = '', title = '', emptyLabel = '', extra = [], className = 'select', attrs = '' } = {}
) {
  const spec = ENUMS[key] || { label: key };
  return `<select class="${className}"${id ? ` id="${id}"` : ''} data-enum="${key}"
    ${attrs} title="${title || `${spec.label}，只能从列表中选择`}">${optionsHTML(key, current, { emptyLabel, extra })}</select>`;
}

/**
 * 展示态：把枚举值翻译成中文标签。
 * @param {string} key 枚举键
 * @param {*} value 枚举值
 * @param {string} fallback 未取值时的中文占位
 * @returns {string} 中文标签
 */
export function labelOf(key, value, fallback = '未设置') {
  const spec = ENUMS[key];
  const text = String(value ?? '').trim();
  if (!spec) return text || fallback;
  const hit = spec.options.find(([optionValue]) => String(optionValue) === text);
  if (hit) return hit[1];
  // 非枚举但确实存了文本（如旧版自由填写的年龄区间）：原样展示，优先于占位
  return text || fallback;
}
