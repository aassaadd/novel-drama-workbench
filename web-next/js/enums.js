/**
 * 枚举字典：枚举字段的唯一中文标签来源。
 *
 * 规则（与后端 `src/novel_drama/models/enums.py` 一一对应）：
 * 1. 编辑态一律用 `selectHTML` 生成下拉，不再出现自由输入；
 * 2. 展示态一律用 `labelOf`，未识别的值绝不原样显示英文，而是显示中文兜底并打日志；
 * 3. 历史数据里的旧值由 `selectHTML` 渲染成「原值无法识别，请重新选择」，给出可见修复提示。
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
  role_filter: {
    label: '角色筛选',
    options: [
      ['all', '全部'],
      ['protagonist', '主角'],
      ['antagonist', '反派'],
      ['supporting', '配角'],
      ['minor', '群演'],
      ['narrator', '旁白'],
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
  gender_filter: {
    label: '性别筛选',
    options: [
      ['all', '全部'],
      ['male', '男'],
      ['female', '女'],
      ['unknown', '未指定'],
    ],
  },
  voice_gender: {
    label: '音色性别',
    options: [
      ['male', '男声'],
      ['female', '女声'],
      ['other', '其他'],
      ['unknown', '中性'],
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
  speech_tempo: {
    label: '语速',
    options: [
      ['slow', '慢'],
      ['medium', '中速'],
      ['fast', '快'],
    ],
  },
  speech_pitch: {
    label: '音高',
    options: [
      ['low', '偏低'],
      ['medium', '中等'],
      ['high', '偏高'],
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
  line_kind: {
    label: '台词类型',
    options: [
      ['dialogue', '对白'],
      ['narration', '旁白'],
      ['inner_monologue', '内心独白'],
    ],
  },
  scene_mood: {
    label: '场景情绪',
    options: [
      ['calm', '平静'],
      ['tense', '紧张'],
      ['sad', '悲情'],
      ['romantic', '浪漫'],
      ['mysterious', '神秘'],
      ['action', '动作'],
      ['comedic', '喜剧'],
      ['epic', '史诗'],
      ['horror', '恐怖'],
    ],
  },
  time_of_day: {
    label: '时间',
    options: [
      ['dawn', '黎明'],
      ['morning', '早晨'],
      ['day', '白天'],
      ['dusk', '黄昏'],
      ['night', '夜晚'],
      ['unknown', '未指定'],
    ],
  },
  camera_move: {
    label: '镜头运动',
    options: [
      ['static', '固定'],
      ['slow_push_in', '缓慢推近'],
      ['pan_left', '向左摇镜'],
      ['pan_right', '向右摇镜'],
      ['handheld', '手持'],
    ],
  },
  anim_mode: {
    label: '动画方式',
    options: [
      ['i2v', '图生视频'],
      ['t2v', '文生视频'],
    ],
  },
  line_state: {
    label: '配音状态',
    options: [
      ['pending', '待配'],
      ['queued', '排队中'],
      ['running', '合成中'],
      ['ready', '已配'],
      ['failed', '失败'],
      ['stale', '已过期'],
      ['unknown', '状态未知'],
    ],
  },
  chapter_status: {
    label: '章节状态',
    options: [
      ['draft', '草稿'],
      ['written', '已成文'],
      ['split', '已切分'],
      ['scripted', '已标注'],
      ['voiced', '已配音'],
      ['visualized', '已出画'],
      ['composed', '已混音'],
      ['stale', '已过期'],
    ],
  },
  scene_status: {
    label: '场景状态',
    options: [
      ['draft', '草稿'],
      ['ready', '可配音'],
      ['voiced', '已配音'],
      ['done', '已完成'],
    ],
  },
  bgm_mode: {
    label: '音乐用途',
    options: [
      ['narrative', '叙事型（跟着剧情走）'],
      ['bed', '铺底型（环境垫底）'],
    ],
  },
  bgm_status: {
    label: 'BGM 状态',
    options: [
      ['planned', '已规划'],
      ['asset_ready', '素材就绪'],
      ['rendered', '已渲染'],
      ['missing', '缺失'],
    ],
  },
  media_tab: {
    label: '素材分类',
    options: [
      ['sfx', '音效'],
      ['image', '配图'],
      ['video', '视频'],
      ['bgm', 'BGM'],
    ],
  },
  track_kind: {
    label: '轨道类型',
    options: [
      ['voice', '配音'],
      ['sfx', '音效'],
      ['music', '音乐'],
      ['video', '视频'],
      ['image', '画面'],
    ],
  },
  asset_status: {
    // 音效/配图/视频三类素材共用：视频段落会带 generating / candidates_ready / selected
    label: '素材状态',
    options: [
      ['pending', '待生成'],
      ['queued', '排队'],
      ['running', '生成中'],
      ['ready', '已生成'],
      ['failed', '失败'],
      ['skipped', '已跳过'],
      ['stale', '已过期'],
      ['generating', '生成中'],
      ['candidates_ready', '候选就绪'],
      ['selected', '已选用'],
    ],
  },
  key_image_status: {
    label: '配图状态',
    options: [
      ['pending', '待生成'],
      ['queued', '排队'],
      ['generating', '生成中'],
      ['candidates_ready', '候选就绪'],
      ['selected', '已选用'],
      ['failed', '失败'],
      ['stale', '已过期'],
    ],
  },
  sfx_kind: {
    label: '音效类型',
    options: [
      ['ambience', '环境音'],
      ['foley', '拟音'],
      ['impact', '冲击'],
      ['transition', '转场'],
      ['music_sting', '音乐点缀'],
    ],
  },
  shot_type: {
    label: '景别',
    options: [
      ['特写', '特写'],
      ['中景', '中景'],
      ['全景', '全景'],
      ['空镜', '空镜'],
    ],
  },
  aspect_ratio: {
    label: '画幅',
    options: [
      ['1:1', '正方形'],
      ['3:4', '竖版 3:4'],
      ['4:3', '横版 4:3'],
      ['9:16', '竖屏 9:16'],
      ['16:9', '宽屏 16:9'],
    ],
  },
  job_kind: {
    label: '任务类型',
    options: [
      ['writing', '正文生成'],
      ['tts', '配音'],
      ['sfx', '音效'],
      ['image', '配图'],
      ['video', '视频'],
      ['render', '渲染'],
      ['mix', '混音'],
      ['export', '导出'],
      ['produce', '全链路编排'],
      ['setting', '写作设定'],
      ['chapter', '章节正文'],
      ['act', '整幕正文'],
      ['chapter.continue', '正文续写'],
      ['character', '角色'],
      ['character.desc', '角色描述'],
      ['world', '世界观'],
      ['outline', '大纲'],
      ['script', '导演标注'],
      ['voice', '音色'],
      ['voice.fill', '补齐音色'],
    ],
  },
  job_status: {
    label: '任务状态',
    options: [
      ['queued', '排队'],
      ['running', '运行中'],
      ['succeeded', '成功'],
      ['partial', '部分完成'],
      ['pending', '待生成'],
      ['ready', '就绪'],
      ['failed', '失败'],
      ['canceled', '已取消'],
      ['interrupted', '已中断'],
      ['skipped', '已跳过'],
    ],
  },
  produce_stage: {
    label: '编排阶段',
    options: [
      ['setting', '设定库'],
      ['outline', '大纲'],
      ['chapter', '章节正文'],
      ['scenes', '场景切分'],
      ['lines', '台词切分'],
      ['script', '导演标注'],
      ['tts', '配音合成'],
      ['sfx', '音效'],
      ['image', '配图'],
      ['video', '视频位'],
      ['timeline', '时间轴'],
      ['concat', '配音拼接'],
      ['mix', '混音'],
      ['mux', '混流'],
      ['export', '导出'],
    ],
  },
  produce_status: {
    label: '阶段状态',
    options: [
      ['pending', '待跑'],
      ['running', '运行中'],
      ['succeeded', '成功'],
      ['failed', '失败'],
      ['skipped', '跳过'],
    ],
  },
  voice_status: {
    label: '音色状态',
    options: [
      ['ready', '可用'],
      ['unusable', '不可用'],
      ['archived', '已归档'],
    ],
  },
  voice_source: {
    label: '音色来源',
    options: [
      ['preset', '内置'],
      ['cloned', '克隆'],
      ['recorded', '录制'],
      ['designed', '设计'],
    ],
  },
  voice_design_status: {
    label: '音色设计状态',
    options: [
      ['running', '生成中'],
      ['ready', '已完成'],
      ['partial', '部分完成'],
      ['failed', '失败'],
      ['stopped', '已停止'],
    ],
  },
  voice_check: {
    label: '音色检查',
    options: [
      ['unchecked', '未检查'],
      ['pass', '通过'],
      ['warn', '有告警'],
      ['fail', '不通过'],
    ],
  },
  cast_action: {
    label: '编排动作',
    options: [
      ['create', '新建'],
      ['fill', '补齐'],
      ['keep', '保持'],
      ['manual', '待人工'],
    ],
  },
  image_kind: {
    label: '形象图类型',
    options: [
      ['fullbody', '全身像'],
      ['closeup', '特写'],
      ['turnaround', '三视图'],
    ],
  },
  issue_level: {
    label: '问题等级',
    options: [
      ['high', '高'],
      ['medium', '中'],
      ['low', '低'],
    ],
  },
  issue_kind: {
    label: '问题类型',
    options: [
      ['speech', '口吻'],
      ['knowledge', '已知信息'],
      ['relation', '关系'],
      ['unplanned', '计划外'],
      ['pov', '视角'],
      ['gender', '性别'],
      ['age', '年龄'],
      ['appearance', '外貌'],
      ['personality', '性格'],
      ['other', '其它'],
    ],
  },
  view_key: {
    label: '工作室视图',
    options: [
      ['script', '画本'],
      ['record', '录音'],
      ['post', '后期'],
      ['align', '对轨'],
    ],
  },
  nav_key: {
    label: '导航',
    options: [
      ['projects', '项目'],
      ['novel', '小说'],
      ['characters', '角色'],
      ['voices', '音色'],
      ['media', '素材'],
      ['tasks', '任务'],
      ['settings', '设置'],
      ['logs', '日志'],
    ],
  },
  log_kind: {
    label: '日志类型',
    options: [
      ['op', '操作'],
      ['llm', 'LLM 调用'],
    ],
  },
  log_status: {
    label: '日志状态',
    options: [
      ['ok', '成功'],
      ['error', '失败'],
      ['aborted', '中断'],
    ],
  },
  llm_role: {
    label: '消息角色',
    options: [
      ['system', '系统'],
      ['user', '用户'],
      ['assistant', '助手'],
      ['tool', '工具'],
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
  thinking_effort: {
    label: '思考强度',
    options: [
      ['low', '低（更快、更省）'],
      ['medium', '中（默认）'],
      ['high', '高（更稳、更慢）'],
    ],
  },
};

/** 未识别枚举值的统一中文兜底（绝不把英文原值直接渲染到界面） */
export const UNKNOWN_ENUM_TEXT = '未识别';

/**
 * 历史写法 → 落盘值（与后端 `enums.py` 的别名表同口径）。
 * 只做「已知同义词」的等价映射，不做猜测式归一：数字区间（如 30-39）仍按未识别处理，
 * 由界面给出建议档位让用户确认。
 */
const ALIASES = {
  speech_tempo: {
    很慢: 'slow', 偏慢: 'slow', 缓慢: 'slow', 慢速: 'slow', 慢: 'slow',
    中速: 'medium', 中等: 'medium', 正常: 'medium', 适中: 'medium',
    measured: 'medium', normal: 'medium', moderate: 'medium',
    偏快: 'fast', 快速: 'fast', 轻快: 'fast', 急促: 'fast', 飞快: 'fast', 快: 'fast',
  },
  speech_pitch: {
    偏低: 'low', 低沉: 'low', 沙哑: 'low', 低音: 'low', 厚: 'low', 低: 'low',
    中等: 'medium', 中性: 'medium', 适中: 'medium', mid: 'medium', 中音: 'medium',
    偏高: 'high', 尖: 'high', 清亮: 'high', 高音: 'high', 高: 'high',
  },
  emotion: {
    平静: 'neutral', 中性: 'neutral', 无: 'neutral',
    喜悦: 'joy', 高兴: 'joy', 开心: 'joy',
    悲伤: 'sad', 难过: 'sad',
    愤怒: 'angry', 生气: 'angry',
    恐惧: 'fear', 害怕: 'fear',
    惊讶: 'surprise',
    温柔: 'tender', 柔情: 'tender',
    激动: 'excited', 兴奋: 'excited',
    低语: 'whisper', 耳语: 'whisper',
    严肃: 'serious', 郑重: 'serious',
  },
  line_kind: { 对白: 'dialogue', 对话: 'dialogue', 旁白: 'narration', 叙述: 'narration', 内心独白: 'inner_monologue', 独白: 'inner_monologue' },
  scene_mood: {
    平静: 'calm', 宁静: 'calm', 安静: 'calm', 舒缓: 'calm', 温和: 'calm', 沉思: 'calm', 克制: 'calm',
    紧张: 'tense', 压抑: 'tense', 压迫: 'tense', 紧绷: 'tense', 不安: 'tense', 悬疑: 'tense',
    悲情: 'sad', 悲伤: 'sad', 悲凉: 'sad', 凄凉: 'sad', 忧郁: 'sad', 沉重: 'sad', 哀伤: 'sad',
    浪漫: 'romantic', 甜蜜: 'romantic', 温馨: 'romantic', 温柔: 'romantic',
    神秘: 'mysterious', 诡异: 'mysterious', 诡秘: 'mysterious', 怪诞: 'mysterious',
    动作: 'action', 激烈: 'action', 打斗: 'action', 追击: 'action', 冲突: 'action',
    喜剧: 'comedic', 搞笑: 'comedic', 幽默: 'comedic', 轻松: 'comedic',
    史诗: 'epic', 宏大: 'epic', 壮阔: 'epic',
    恐怖: 'horror', 惊悚: 'horror', 阴森: 'horror', 骇人: 'horror',
  },
  bgm_mode: { 叙事: 'narrative', 叙事型: 'narrative', 铺底: 'bed', 铺底型: 'bed', 垫底: 'bed' },
  line_state: { 待配: 'pending', 待生成: 'pending', 合成中: 'running', 已配: 'ready', 就绪: 'ready', 失败: 'failed', 已过期: 'stale' },
  asset_status: { 待生成: 'pending', 排队: 'queued', 生成中: 'running', 就绪: 'ready', 失败: 'failed', 跳过: 'skipped' },
  chapter_status: { 草稿: 'draft', 已成文: 'written', 已切分: 'split', 已标注: 'scripted', 已配音: 'voiced', 已出画: 'visualized', 已混音: 'composed', 已过期: 'stale' },
  pov: { 第一人称: 'first', 第二人称: 'second', 第三人称有限: 'third_limited', 第三人称全知: 'third_omniscient' },
  time_of_day: { 黎明: 'dawn', 清晨: 'morning', 早晨: 'morning', 白天: 'day', 黄昏: 'dusk', 傍晚: 'dusk', 夜晚: 'night', 夜里: 'night', 未指定: 'unknown' },
};

/** 已告警过的「枚举键:值」，避免同一处反复刷控制台 */
const warned = new Set();

/**
 * 记录一次未识别枚举值告警（同键同值只打一次）。
 * @param {string} key 枚举键
 * @param {*} value 原始值
 * @param {string} where 场景说明（展示 / 下拉）
 */
function warnUnknown(key, value, where) {
  const token = `${key}:${String(value)}`;
  if (warned.has(token)) return;
  warned.add(token);
  console.warn(`[枚举] ${where}遇到未识别的取值：${key} = ${JSON.stringify(value)}；已按中文兜底展示`);
}

/** 已报过错的枚举键，避免每帧重复刷控制台 */
const unknownKeys = new Set();

/**
 * 取枚举定义；漏登记的键属于开发期问题，直接报 error（每个键只报一次），
 * 让它在开发时就暴露，而不是等某个页面渲染出「未识别」。
 * @param {string} key 枚举键
 * @param {string} where 调用位置（labelOf / selectHTML / optionsHTML）
 * @returns {object|null} 枚举定义；未登记返回 null
 */
function specOf(key, where) {
  const spec = ENUMS[key];
  if (spec) return spec;
  const token = `${key}@${where}`;
  if (!unknownKeys.has(token)) {
    unknownKeys.add(token);
    console.error(`[枚举] 未登记的枚举键：${key}（${where}）；请在 web-next/js/enums.js 的 ENUMS 中补齐`);
  }
  return null;
}

/**
 * 把取值解析成枚举项：先按落盘值匹配，再按中文标签匹配。
 * 旧数据里存过中文标签（如「快」「偏高」）时按标签认回枚举值，展示正常且下拉自动选中该项，
 * 保存时会写成规范落盘值，不需要用户手工重选。
 * @param {string} key 枚举键
 * @param {*} value 原始取值
 * @returns {{value:string,label:string,legacy:boolean}|null} 枚举项；无法解析返回 null
 */
export function resolveEnum(key, value) {
  const spec = ENUMS[key];
  const text = String(value ?? '').trim();
  if (!spec || !text) return null;
  const byValue = spec.options.find(([optionValue]) => String(optionValue) === text);
  if (byValue) return { value: String(byValue[0]), label: byValue[1], legacy: false };
  const byLabel = spec.options.find(([, label]) => String(label) === text);
  if (byLabel) return { value: String(byLabel[0]), label: byLabel[1], legacy: true };
  const alias = (ALIASES[key] || {})[text] || (ALIASES[key] || {})[text.toLowerCase()];
  const byAlias = alias && spec.options.find(([optionValue]) => String(optionValue) === alias);
  if (byAlias) return { value: String(byAlias[0]), label: byAlias[1], legacy: true };
  return null;
}

/**
 * 判断取值是否属于该枚举（只认规范落盘值）。
 * @param {string} key 枚举键
 * @param {*} value 枚举值
 * @returns {boolean} 是否合法
 */
export function isEnumValue(key, value) {
  const spec = ENUMS[key];
  const text = String(value ?? '').trim();
  if (!spec || !text) return false;
  return spec.options.some(([optionValue]) => String(optionValue) === text);
}

/**
 * 展示态：把枚举值翻译成中文标签，未识别的值显示中文兜底并记日志。
 * @param {string} key 枚举键
 * @param {*} value 枚举值
 * @param {string} fallback 未取值（空）时的中文占位
 * @returns {string} 中文标签
 */
export function labelOf(key, value, fallback = '未设置') {
  const spec = specOf(key, 'labelOf');
  const text = String(value ?? '').trim();
  // 枚举键写错属于开发期问题，specOf 会报 error，这里按占位兜底
  if (!spec) return fallback;
  if (!text) return fallback;
  const hit = resolveEnum(key, text);
  if (hit) return hit.label;
  warnUnknown(key, value, '展示');
  return UNKNOWN_ENUM_TEXT;
}

/**
 * 给历史非法值一个中文建议值（目前只覆盖年龄段：按数字区间中点归入分档）。
 * @param {string} key 枚举键
 * @param {*} value 原始值
 * @returns {{value:string,label:string}|null} 建议值；无建议返回 null
 */
export function suggestEnum(key, value) {
  const text = String(value ?? '');
  if (key !== 'age_range') return null;
  const matched = text.match(/(\d{1,3})\s*[-~至到]\s*(\d{1,3})/) || text.match(/(\d{1,3})/);
  if (!matched) return null;
  const middle = matched[2] ? (Number(matched[1]) + Number(matched[2])) / 2 : Number(matched[1]);
  const spec = ENUMS.age_range;
  const band = spec.options.find(([optionValue]) => {
    const [low, high] = optionValue.split('-').map(Number);
    return middle >= low - 2 && middle <= high + 2;
  });
  return band ? { value: band[0], label: band[1] } : null;
}

/**
 * 生成下拉的 option 列表。
 * @param {string} key 枚举键
 * @param {*} current 当前枚举值
 * @param {{emptyLabel?: string}} options emptyLabel 非空时插入空值项
 * @returns {string} `<option>` 拼接结果
 */
export function optionsHTML(key, current, { emptyLabel = '' } = {}) {
  const spec = specOf(key, 'optionsHTML');
  if (!spec) return '';
  const now = String(current ?? '').trim();
  const hit = resolveEnum(key, now);
  // 旧数据存的是中文标签时，直接选中对应的规范选项，保存即写成落盘值
  const selectedValue = hit ? hit.value : now;
  const known = Boolean(hit);
  const rows = [];
  // 历史值不在枚举里：只给「无法识别」的占位，不把旧英文值渲染出来；保留原值避免静默丢数据
  if (now && !known) {
    warnUnknown(key, current, '下拉');
    rows.push(`<option value="${now}" selected disabled>原值无法识别，请重新选择</option>`);
  }
  spec.options.forEach(([value, label]) => {
    rows.push(`<option value="${value}" ${String(value) === selectedValue ? 'selected' : ''}>${label}</option>`);
  });
  if (emptyLabel && !(now && !known)) {
    rows.unshift(`<option value="" ${now ? '' : 'selected'}>${emptyLabel}</option>`);
  }
  return rows.join('');
}

/**
 * 绑定枚举下拉的修复提示清理：用户在历史非法值上重新选了合法值后，提示立即消失。
 * 在应用启动时调用一次即可。
 */
export function bindEnumWarnings() {
  document.addEventListener('change', (event) => {
    const el = event.target;
    if (!(el instanceof HTMLSelectElement) || !el.dataset.enum) return;
    const hint = el.nextElementSibling;
    if (hint && hint.classList.contains('enum-warn') && isEnumValue(el.dataset.enum, el.value)) hint.remove();
  });
}

/**
 * 生成一个真正的枚举下拉控件（带 data-enum 标记，供扫描脚本核对）。
 * 历史非法值会在控件下方附带修复提示，保存前必须重新选择。
 * @param {string} key 枚举键
 * @param {{id?:string,current?:*,title?:string,emptyLabel?:string,className?:string,attrs?:string}} options 控件配置
 * @returns {string} `<select>` HTML（含修复提示）
 */
export function selectHTML(key, { id = '', current = '', title = '', emptyLabel = '', className = 'select', attrs = '' } = {}) {
  const spec = specOf(key, 'selectHTML');
  if (!spec) return '';
  const now = String(current ?? '').trim();
  const invalid = Boolean(now) && !resolveEnum(key, now);
  const select = `<select class="${className}"${id ? ` id="${id}"` : ''} data-enum="${key}"
    ${attrs} title="${title || `${spec.label}，只能从列表中选择`}">${optionsHTML(key, now, { emptyLabel })}</select>`;
  if (!invalid) return select;
  const suggestion = suggestEnum(key, now);
  const hint = suggestion ? `，建议改为「${suggestion.label}」` : '';
  return `${select}<span class="enum-warn" data-enum-warn="${key}">当前保存的是旧版取值，无法识别${hint}；请重新选择后保存</span>`;
}
