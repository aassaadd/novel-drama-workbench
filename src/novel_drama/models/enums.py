"""枚举唯一来源：字段 → 枚举类型 → 中文标签 → 别名归一。

约定：
- 所有「本质是档位 / 状态」的字段都在这里定义成 `str` 枚举：值是落盘值（稳定英文键，
  兼容已有数据），中文标签只给用户看，界面与后端报错共用这一份。
- 新增枚举时必须同时登记 `ENUM_FIELD_LABELS`（报错里的字段中文名），
  否则 `check_enums` 会以「未登记中文名」报错，防止悄悄加字段却漏掉展示。
- `normalize` 把模型输出 / 旧数据里的中文与近义写法收敛到枚举值；收敛不了返回 None，
  由调用方给出可读错误，绝不把自由字符串直接落盘。

前端 `web-next/js/enums.js` 与这里一一对应（键名相同），只允许在这两处维护标签。
"""

from __future__ import annotations

import re
from enum import Enum

# 未指定 / 未知的统一中文占位（前端未识别枚举值也用这个词，避免英文裸露）
UNKNOWN_ZH = "未识别"


class ZhEnum(str, Enum):
    """带中文标签的字符串枚举基类。

    成员声明形如 `FAST = ("fast", "快")`：第一个值是落盘值，第二个是中文标签。
    """

    def __new__(cls, value: str, label: str):
        """构造成员：字符串值 + 中文标签。

        参数:
            value: 落盘值（稳定英文键）。
            label: 中文标签。

        返回:
            枚举成员。
        """
        obj = str.__new__(cls, value)
        obj._value_ = value
        obj.label = label
        return obj

    def __str__(self) -> str:
        """返回落盘值，便于直接拼进提示词与 JSON。"""
        return str(self._value_)

    @classmethod
    def values(cls) -> tuple[str, ...]:
        """返回全部落盘值（顺序即声明顺序）。"""
        return tuple(member.value for member in cls)

    @classmethod
    def label_of(cls, value) -> str | None:
        """按落盘值取中文标签。

        参数:
            value: 落盘值，可为 None。

        返回:
            中文标签；未命中返回 None。
        """
        key = str(value or "")
        for member in cls:
            if member.value == key:
                return member.label
        return None

    @classmethod
    def coerce(cls, raw) -> str | None:
        """严格归一：只接受落盘值或完全一致的中文标签，不做模糊猜测。

        用于用户直接输入（命令行 / 界面），避免把「30-40」这类旧写法悄悄改成别的档位。

        参数:
            raw: 用户输入。

        返回:
            合法落盘值；不匹配返回 None。
        """
        text = str(raw or "").strip()
        if not text:
            return None
        for member in cls:
            if text == member.value or text == member.label:
                return member.value
        return None

    @classmethod
    def normalize(cls, raw) -> str | None:
        """把任意写法收敛成落盘值。

        参数:
            raw: 模型输出或旧数据里的原始值（可能是中文、英文或带补充说明的长文本）。

        返回:
            合法落盘值；无法识别返回 None。
        """
        text = str(raw or "").strip()
        if not text:
            return None
        squeezed = _squeeze(text)
        for member in cls:
            if squeezed == _squeeze(member.value):
                return member.value
        aliases = _ALIASES.get(cls, {})
        if squeezed in aliases:
            return aliases[squeezed]
        # 长文本（如「偏慢，常停顿」）按别名包含关系兜底；别名表需把长词放前面
        for key, value in aliases.items():
            if key and key in squeezed:
                return value
        return None


def _squeeze(text: str) -> str:
    """把文本压成可比对形式：去空白、下划线、标点，英文转小写。

    参数:
        text: 原始文本。

    返回:
        压缩后的文本。
    """
    return re.sub(r"[\s_，,。;；、/·\-—（）()\"'“”]+", "", str(text)).lower()


class CharacterRole(ZhEnum):
    """角色定位。"""

    NARRATOR = ("narrator", "旁白")
    PROTAGONIST = ("protagonist", "主角")
    ANTAGONIST = ("antagonist", "反派")
    SUPPORTING = ("supporting", "配角")
    MINOR = ("minor", "群演")


class CharacterGender(ZhEnum):
    """角色性别。"""

    MALE = ("male", "男")
    FEMALE = ("female", "女")
    OTHER = ("other", "其他")
    UNKNOWN = ("unknown", "未指定")


class VoiceGender(ZhEnum):
    """音色性别（音色库口径，与角色性别文案不同）。"""

    MALE = ("male", "男声")
    FEMALE = ("female", "女声")
    OTHER = ("other", "其他")
    UNKNOWN = ("unknown", "中性")


class AgeRange(ZhEnum):
    """年龄段分档（音色推荐用）。"""

    CHILD = ("10-14", "儿童（10-14 岁）")
    YOUNG = ("18-25", "青年（18-25 岁）")
    ADULT = ("26-39", "成年（26-39 岁）")
    MIDDLE = ("40-54", "中年（40-54 岁）")
    ELDER = ("55-70", "老年（55-70 岁）")

    @classmethod
    def normalize(cls, raw) -> str | None:
        """收敛年龄段写法；数字区间按中点归入分档（仅作建议值，界面需用户确认）。

        参数:
            raw: 原始年龄段，如「30-40」「30s」「中年」。

        返回:
            合法分档值；无法识别返回 None。
        """
        hit = super().normalize(raw)
        if hit:
            return hit
        matched = re.search(r"(\d{1,3})\s*[-~至到]\s*(\d{1,3})", str(raw or ""))
        if matched:
            middle = (int(matched.group(1)) + int(matched.group(2))) / 2
        else:
            single = re.search(r"(\d{1,3})", str(raw or ""))
            if not single:
                return None
            middle = float(single.group(1))
        for member in cls:
            low, high = (int(part) for part in member.value.split("-"))
            if low - 2 <= middle <= high + 2:
                return member.value
        return None


class SpeechTempo(ZhEnum):
    """说话语速档位（角色档案 · 语速）。"""

    SLOW = ("slow", "慢")
    MEDIUM = ("medium", "中速")
    FAST = ("fast", "快")


class SpeechPitch(ZhEnum):
    """说话音高档位（角色档案 · 音高 / 音色）。"""

    LOW = ("low", "偏低")
    MEDIUM = ("medium", "中等")
    HIGH = ("high", "偏高")


class Emotion(ZhEnum):
    """统一情绪枚举（TTS 适配器负责映射到具体引擎标签）。"""

    NEUTRAL = ("neutral", "平静")
    JOY = ("joy", "喜悦")
    SAD = ("sad", "悲伤")
    ANGRY = ("angry", "愤怒")
    FEAR = ("fear", "恐惧")
    SURPRISE = ("surprise", "惊讶")
    TENDER = ("tender", "温柔")
    EXCITED = ("excited", "激动")
    WHISPER = ("whisper", "低语")
    SERIOUS = ("serious", "严肃")


class LineKind(ZhEnum):
    """台词行类型。"""

    DIALOGUE = ("dialogue", "对白")
    NARRATION = ("narration", "旁白")
    INNER_MONOLOGUE = ("inner_monologue", "内心独白")


class Paralinguistic(ZhEnum):
    """副语言标记。"""

    LAUGH = ("laugh", "笑")
    SIGH = ("sigh", "叹气")
    BREATH = ("breath", "呼吸")
    CRY = ("cry", "哭")
    WHISPER = ("whisper", "低语")


class LineTtsStatus(ZhEnum):
    """台词行配音状态（stale 为派生态，不落盘）。"""

    PENDING = ("pending", "待配")
    QUEUED = ("queued", "排队中")
    RUNNING = ("running", "合成中")
    READY = ("ready", "已配")
    FAILED = ("failed", "失败")
    STALE = ("stale", "已过期")


class SceneStatus(ZhEnum):
    """场景状态。"""

    DRAFT = ("draft", "草稿")
    READY = ("ready", "可配音")
    VOICED = ("voiced", "已配音")
    DONE = ("done", "已完成")


class ChapterStatus(ZhEnum):
    """章节状态。"""

    DRAFT = ("draft", "草稿")
    WRITTEN = ("written", "已成文")
    SPLIT = ("split", "已切分")
    SCRIPTED = ("scripted", "已标注")
    VOICED = ("voiced", "已配音")
    VISUALIZED = ("visualized", "已出画")
    COMPOSED = ("composed", "已混音")
    STALE = ("stale", "已过期")


class PovMode(ZhEnum):
    """叙事视角。"""

    FIRST = ("first", "第一人称")
    SECOND = ("second", "第二人称")
    THIRD_LIMITED = ("third_limited", "第三人称 · 有限视角")
    THIRD_OMNISCIENT = ("third_omniscient", "第三人称 · 全知视角")


class TimeOfDay(ZhEnum):
    """场景时间。"""

    DAWN = ("dawn", "黎明")
    MORNING = ("morning", "早晨")
    DAY = ("day", "白天")
    DUSK = ("dusk", "黄昏")
    NIGHT = ("night", "夜晚")
    UNKNOWN = ("unknown", "未指定")


class SceneMood(ZhEnum):
    """场景情绪底色。"""

    CALM = ("calm", "平静")
    TENSE = ("tense", "紧张")
    SAD = ("sad", "悲情")
    ROMANTIC = ("romantic", "浪漫")
    MYSTERIOUS = ("mysterious", "神秘")
    ACTION = ("action", "动作")
    COMEDIC = ("comedic", "喜剧")
    EPIC = ("epic", "史诗")
    HORROR = ("horror", "恐怖")


class ShotType(ZhEnum):
    """景别（落盘值本身就是中文）。"""

    CLOSE_UP = ("特写", "特写")
    MEDIUM_SHOT = ("中景", "中景")
    WIDE_SHOT = ("全景", "全景")
    EMPTY_SHOT = ("空镜", "空镜")


class BgmMode(ZhEnum):
    """BGM 档位。"""

    NARRATIVE = ("narrative", "叙事型（跟着剧情走）")
    BED = ("bed", "铺底型（环境垫底）")


class BgmStatus(ZhEnum):
    """BGM 计划状态。"""

    PLANNED = ("planned", "已规划")
    ASSET_READY = ("asset_ready", "素材就绪")
    RENDERED = ("rendered", "已渲染")
    MISSING = ("missing", "缺失")


class JobKind(ZhEnum):
    """渲染任务类型。"""

    WRITING = ("writing", "正文生成")
    TTS = ("tts", "配音合成")
    SFX = ("sfx", "音效")
    IMAGE = ("image", "配图")
    VIDEO = ("video", "视频")
    MIX = ("mix", "混音")
    EXPORT = ("export", "导出")


class JobStatus(ZhEnum):
    """渲染任务状态。"""

    QUEUED = ("queued", "排队")
    RUNNING = ("running", "运行中")
    SUCCEEDED = ("succeeded", "成功")
    FAILED = ("failed", "失败")
    CANCELED = ("canceled", "已取消")
    INTERRUPTED = ("interrupted", "已中断")
    SKIPPED = ("skipped", "已跳过")


class SfxKind(ZhEnum):
    """音效类型。"""

    AMBIENCE = ("ambience", "环境音")
    FOLEY = ("foley", "拟音")
    IMPACT = ("impact", "冲击")
    TRANSITION = ("transition", "转场")
    MUSIC_STING = ("music_sting", "音乐点缀")


class SfxPositionMode(ZhEnum):
    """音效锚点方式。"""

    BEFORE_LINE = ("before_line", "台词前")
    AFTER_LINE = ("after_line", "台词后")
    DURING_LINE = ("during_line", "台词中")
    SHOT_START = ("shot_start", "镜头开始")
    SHOT_END = ("shot_end", "镜头结束")
    ABSOLUTE = ("absolute", "绝对位置")


class AssetStatus(ZhEnum):
    """素材产物状态（音效 / 配图 / 视频共用）。"""

    PENDING = ("pending", "待生成")
    QUEUED = ("queued", "排队")
    RUNNING = ("running", "生成中")
    READY = ("ready", "就绪")
    FAILED = ("failed", "失败")
    SKIPPED = ("skipped", "已跳过")


class KeyImageStatus(ZhEnum):
    """关键图状态。"""

    PENDING = ("pending", "待生成")
    QUEUED = ("queued", "排队")
    GENERATING = ("generating", "生成中")
    CANDIDATES_READY = ("candidates_ready", "候选就绪")
    SELECTED = ("selected", "已选用")
    FAILED = ("failed", "失败")
    STALE = ("stale", "已过期")


class VideoStatus(ZhEnum):
    """视频段落状态（比关键图多一个「已跳过」）。"""

    PENDING = ("pending", "待生成")
    QUEUED = ("queued", "排队")
    GENERATING = ("generating", "生成中")
    CANDIDATES_READY = ("candidates_ready", "候选就绪")
    SELECTED = ("selected", "已选用")
    FAILED = ("failed", "失败")
    SKIPPED = ("skipped", "已跳过")
    STALE = ("stale", "已过期")


class PromptSource(ZhEnum):
    """提示词来源。"""

    AUTO = ("auto", "自动生成")
    MANUAL = ("manual", "人工填写")


class AspectRatio(ZhEnum):
    """画幅。"""

    SQUARE = ("1:1", "正方形")
    PORTRAIT_3_4 = ("3:4", "竖版 3:4")
    LANDSCAPE_4_3 = ("4:3", "横版 4:3")
    PORTRAIT_9_16 = ("9:16", "竖屏 9:16")
    LANDSCAPE_16_9 = ("16:9", "宽屏 16:9")


class VideoMode(ZhEnum):
    """视频生成模式。"""

    T2V = ("t2v", "文生视频")
    I2V = ("i2v", "图生视频")


class CameraMove(ZhEnum):
    """运镜。"""

    STATIC = ("static", "固定机位")
    SLOW_PUSH_IN = ("slow_push_in", "缓慢推近")
    PAN_LEFT = ("pan_left", "左摇")
    PAN_RIGHT = ("pan_right", "右摇")
    HANDHELD = ("handheld", "手持")


class AudioPolicy(ZhEnum):
    """视频音轨策略。"""

    KEEP = ("keep", "保留原声")
    MUTE = ("mute", "静音")
    DUCK = ("duck", "压低原声")


class TrackKind(ZhEnum):
    """时间轴轨道类型。"""

    VOICE = ("voice", "配音")
    SFX = ("sfx", "音效")
    MUSIC = ("music", "音乐")
    VIDEO = ("video", "视频")
    IMAGE = ("image", "画面")


class VoiceStatus(ZhEnum):
    """音色状态。"""

    READY = ("ready", "可用")
    UNUSABLE = ("unusable", "不可用")
    ARCHIVED = ("archived", "已归档")


class VoiceSource(ZhEnum):
    """音色来源。"""

    PRESET = ("preset", "内置")
    CLONED = ("cloned", "克隆")
    RECORDED = ("recorded", "录制")
    DESIGNED = ("designed", "设计")


class VoiceProfileStatus(ZhEnum):
    """音色描述状态。"""

    DRAFT = ("draft", "草稿")
    READY = ("ready", "就绪")
    STALE = ("stale", "已过期")
    MISSING = ("missing", "缺失")


class AgeFeel(ZhEnum):
    """音色年龄感。"""

    CHILD = ("child", "儿童")
    TEEN = ("teen", "少年")
    YOUNG_ADULT = ("young_adult", "青年")
    ADULT = ("adult", "成年")
    MIDDLE_AGED = ("middle_aged", "中年")
    ELDERLY = ("elderly", "老年")


class GenderSource(ZhEnum):
    """角色音色描述里的性别来源。"""

    CHARACTER = ("character", "角色设定")
    MANUAL = ("manual", "手动指定")
    INFERRED = ("inferred", "自动推断")


class VoiceGenderSource(ZhEnum):
    """音色变体上的性别来源（只允许确认过的两种）。"""

    CHARACTER = ("character", "角色设定")
    MANUAL = ("manual", "手动指定")


class RelationSentiment(ZhEnum):
    """角色关系倾向。"""

    ALLY = ("ally", "盟友")
    ENEMY = ("enemy", "敌手")
    FAMILY = ("family", "家人")
    ROMANCE = ("romance", "恋人")
    NEUTRAL = ("neutral", "中立")


class StockReusePolicy(ZhEnum):
    """龙套音色复用策略。"""

    REUSE = ("reuse", "跨场景复用")
    PER_SCENE = ("per_scene", "每场独立")


class ExpertMode(ZhEnum):
    """音色路由锁定层级。"""

    AUTO = ("auto", "自动")
    LOCKED_LINE = ("locked_line", "锁定单行")
    LOCKED_CHARACTER = ("locked_character", "锁定角色")
    LOCKED_PROJECT = ("locked_project", "锁定工程")
    MANUAL = ("manual", "手动")


class BlockKind(ZhEnum):
    """章节正文块类型。"""

    PARAGRAPH = ("paragraph", "正文段落")
    DIALOGUE = ("dialogue", "对白")
    HEADING = ("heading", "小标题")


class GenerationMode(ZhEnum):
    """正文生成方式。"""

    CONTINUE = ("continue", "顺写续写")
    OUTLINE_DRIVEN = ("outline_driven", "按大纲生成")
    REWRITE_RANGE = ("rewrite_range", "局部重写")


class GenerationStatus(ZhEnum):
    """正文生成状态。"""

    PENDING = ("pending", "待生成")
    RUNNING = ("running", "生成中")
    DONE = ("done", "已完成")
    FAILED = ("failed", "失败")
    CANCELED = ("canceled", "已取消")


class ScriptStatus(ZhEnum):
    """导演脚本状态。"""

    DRAFT = ("draft", "草稿")
    READY = ("ready", "就绪")
    STALE = ("stale", "已过期")


class VoiceVariantEmotion(ZhEnum):
    """音色变体情绪（default 为默认变体）。"""

    DEFAULT = ("default", "默认")
    NEUTRAL = ("neutral", "平静")
    JOY = ("joy", "喜悦")
    SAD = ("sad", "悲伤")
    ANGRY = ("angry", "愤怒")
    FEAR = ("fear", "恐惧")
    SURPRISE = ("surprise", "惊讶")
    TENDER = ("tender", "温柔")
    EXCITED = ("excited", "激动")
    WHISPER = ("whisper", "低语")
    SERIOUS = ("serious", "严肃")


class VoiceCheckStatus(ZhEnum):
    """音色一致性检查状态。"""

    UNCHECKED = ("unchecked", "未检查")
    PASS = ("pass", "通过")
    WARN = ("warn", "有告警")
    FAIL = ("fail", "不通过")


class ProviderStatus(ZhEnum):
    """TTS provider 可用状态。"""

    READY = ("ready", "可用")
    PENDING = ("pending", "待配置")
    UNUSABLE = ("unusable", "不可用")


class WorkUsage(ZhEnum):
    """作品用途。"""

    COMMERCIAL = ("commercial", "商用")
    PERSONAL = ("personal", "个人使用")


class UnloadPolicy(ZhEnum):
    """写作模型卸载策略。"""

    AFTER_TEXT_TASK = ("after_text_task", "文本任务后卸载")
    NEVER = ("never", "从不卸载")
    ALWAYS = ("always", "每次任务后卸载")


class RoutingMode(ZhEnum):
    """TTS 自动路由模式。"""

    AUTO = ("auto", "自动路由")
    MANUAL = ("manual", "手动指定")


class NarratorSwitchScope(ZhEnum):
    """旁白切换作用范围。"""

    FROM_HERE = ("from_here", "从此章起")
    THIS_CHAPTER_ONLY = ("this_chapter_only", "仅本章")


class AudioFormat(ZhEnum):
    """导出音频格式。"""

    MP3 = ("mp3", "MP3（通用有损）")
    WAV = ("wav", "WAV（无损）")
    M4A = ("m4a", "M4A（苹果设备常用）")


class GpuUnloadMode(ZhEnum):
    """切换 GPU 任务前的模型卸载策略。"""

    AUTO = ("auto", "按需判断")
    NEVER = ("never", "不卸载")
    ALWAYS = ("always", "总是卸载")


class UiJobKind(ZhEnum):
    """界面生成任务类型（`.ndw/ui_jobs.json`）。"""

    PRODUCE = ("produce", "全链路编排")
    SETTING = ("setting", "写作设定")
    CHAPTER = ("chapter", "章节正文")
    ACT = ("act", "整幕正文")
    CHAPTER_CONTINUE = ("chapter.continue", "正文续写")
    CHARACTER = ("character", "角色")
    CHARACTER_DESC = ("character.desc", "角色描述")
    WORLD = ("world", "世界观")
    OUTLINE = ("outline", "大纲")
    SCRIPT = ("script", "导演标注")
    VOICE = ("voice", "音色")
    VOICE_FILL = ("voice.fill", "补齐音色")


class UiJobStatus(ZhEnum):
    """界面生成任务状态。"""

    QUEUED = ("queued", "排队")
    RUNNING = ("running", "运行中")
    SUCCEEDED = ("succeeded", "成功")
    PARTIAL = ("partial", "部分完成")
    FAILED = ("failed", "失败")
    CANCELED = ("canceled", "已取消")
    INTERRUPTED = ("interrupted", "已中断")


#: 报错与界面用的字段中文名；新增枚举必须登记，否则 `field_label` 直接报错
ENUM_FIELD_LABELS: dict[type[ZhEnum], str] = {
    CharacterRole: "角色定位",
    CharacterGender: "性别",
    VoiceGender: "音色性别",
    AgeRange: "年龄段",
    SpeechTempo: "语速",
    SpeechPitch: "音高 / 音色",
    Emotion: "情绪",
    LineKind: "台词行类型",
    Paralinguistic: "副语言",
    LineTtsStatus: "配音状态",
    SceneStatus: "场景状态",
    ChapterStatus: "章节状态",
    PovMode: "叙事视角",
    TimeOfDay: "场景时间",
    SceneMood: "场景情绪",
    ShotType: "景别",
    BgmMode: "BGM 档位",
    BgmStatus: "BGM 状态",
    JobKind: "任务类型",
    JobStatus: "任务状态",
    SfxKind: "音效类型",
    SfxPositionMode: "音效锚点",
    AssetStatus: "素材状态",
    KeyImageStatus: "配图状态",
    VideoStatus: "视频状态",
    PromptSource: "提示词来源",
    AspectRatio: "画幅",
    VideoMode: "视频模式",
    CameraMove: "运镜",
    AudioPolicy: "视频音轨策略",
    TrackKind: "轨道类型",
    VoiceStatus: "音色状态",
    VoiceSource: "音色来源",
    VoiceProfileStatus: "音色描述状态",
    AgeFeel: "音色年龄感",
    GenderSource: "性别来源",
    VoiceGenderSource: "音色性别来源",
    RelationSentiment: "关系倾向",
    StockReusePolicy: "龙套复用策略",
    ExpertMode: "路由锁定层级",
    BlockKind: "正文块类型",
    GenerationMode: "生成方式",
    GenerationStatus: "生成状态",
    ScriptStatus: "导演脚本状态",
    VoiceVariantEmotion: "音色变体情绪",
    VoiceCheckStatus: "音色检查状态",
    ProviderStatus: "引擎状态",
    WorkUsage: "作品用途",
    UnloadPolicy: "模型卸载策略",
    RoutingMode: "路由模式",
    NarratorSwitchScope: "旁白切换范围",
    AudioFormat: "音频格式",
    GpuUnloadMode: "显存卸载策略",
    UiJobKind: "任务类型",
    UiJobStatus: "任务状态",
}


def field_label(enum_cls: type[ZhEnum]) -> str:
    """取枚举对应的字段中文名。

    参数:
        enum_cls: 枚举类。

    返回:
        字段中文名；未登记时抛 KeyError 提示补齐标签。
    """
    try:
        return ENUM_FIELD_LABELS[enum_cls]
    except KeyError as exc:  # pragma: no cover - 属开发期错误
        raise KeyError(f"枚举 {enum_cls.__name__} 未登记中文名，请在 ENUM_FIELD_LABELS 补齐") from exc


def choices_text(enum_cls: type[ZhEnum]) -> str:
    """把枚举拼成「中文（值）、…」形式，用于报错与提示。

    参数:
        enum_cls: 枚举类。

    返回:
        可读的可选值列表。
    """
    return "、".join(
        member.label if member.value in member.label else f"{member.label}（{member.value}）" for member in enum_cls
    )


#: 别名表：压缩后的写法 → 落盘值。长的词放前面，避免「中等偏慢」先命中「中等」
_ALIASES: dict[type[ZhEnum], dict[str, str]] = {
    CharacterRole: {
        "旁白": "narrator",
        "叙述者": "narrator",
        "主角": "protagonist",
        "男主": "protagonist",
        "女主": "protagonist",
        "反派": "antagonist",
        "对手": "antagonist",
        "配角": "supporting",
        "群演": "minor",
        "龙套": "minor",
        "路人": "minor",
    },
    CharacterGender: {"男": "male", "男性": "male", "女": "female", "女性": "female", "其他": "other", "未知": "unknown", "未指定": "unknown"},
    VoiceGender: {"男": "male", "男声": "male", "女": "female", "女声": "female", "中性": "unknown", "其他": "other"},
    AgeRange: {
        "儿童": "10-14",
        "少年": "10-14",
        "青少年": "18-25",
        "青年": "18-25",
        "年轻": "18-25",
        "成年": "26-39",
        "壮年": "26-39",
        "中年": "40-54",
        "中老年": "40-54",
        "老年": "55-70",
        "老人": "55-70",
    },
    SpeechTempo: {
        "很慢": "slow",
        "偏慢": "slow",
        "缓慢": "slow",
        "慢速": "slow",
        "慢": "slow",
        "中速": "medium",
        "中等": "medium",
        "正常": "medium",
        "适中": "medium",
        "measured": "medium",
        "normal": "medium",
        "moderate": "medium",
        "偏快": "fast",
        "快速": "fast",
        "轻快": "fast",
        "急促": "fast",
        "飞快": "fast",
        "快": "fast",
    },
    SpeechPitch: {
        "偏低": "low",
        "低沉": "low",
        "沙哑": "low",
        "低音": "low",
        "厚": "low",
        "低": "low",
        "中等": "medium",
        "中性": "medium",
        "适中": "medium",
        "mid": "medium",
        "中音": "medium",
        "偏高": "high",
        "尖": "high",
        "清亮": "high",
        "高音": "high",
        "高": "high",
    },
    Emotion: {
        "平静": "neutral",
        "中性": "neutral",
        "无": "neutral",
        "喜悦": "joy",
        "高兴": "joy",
        "开心": "joy",
        "悲伤": "sad",
        "难过": "sad",
        "愤怒": "angry",
        "生气": "angry",
        "恐惧": "fear",
        "害怕": "fear",
        "惊讶": "surprise",
        "温柔": "tender",
        "柔情": "tender",
        "激动": "excited",
        "兴奋": "excited",
        "低语": "whisper",
        "耳语": "whisper",
        "严肃": "serious",
        "郑重": "serious",
    },
    LineKind: {"对白": "dialogue", "对话": "dialogue", "旁白": "narration", "叙述": "narration", "内心独白": "inner_monologue", "独白": "inner_monologue"},
    BgmMode: {"叙事": "narrative", "叙事型": "narrative", "铺底": "bed", "铺底型": "bed", "垫底": "bed"},
    JobStatus: {"排队": "queued", "运行中": "running", "成功": "succeeded", "完成": "succeeded", "失败": "failed", "已取消": "canceled", "中断": "interrupted", "跳过": "skipped"},
    LineTtsStatus: {"待配": "pending", "待生成": "pending", "合成中": "running", "已配": "ready", "就绪": "ready", "失败": "failed", "已过期": "stale"},
    AssetStatus: {"待生成": "pending", "排队": "queued", "生成中": "running", "就绪": "ready", "失败": "failed", "跳过": "skipped"},
    ChapterStatus: {"草稿": "draft", "已成文": "written", "已切分": "split", "已标注": "scripted", "已配音": "voiced", "已出画": "visualized", "已混音": "composed", "已过期": "stale"},
    PovMode: {"第一人称": "first", "第二人称": "second", "第三人称有限": "third_limited", "第三人称全知": "third_omniscient"},
    TimeOfDay: {"黎明": "dawn", "清晨": "morning", "早晨": "morning", "白天": "day", "黄昏": "dusk", "傍晚": "dusk", "夜晚": "night", "夜里": "night"},
}
