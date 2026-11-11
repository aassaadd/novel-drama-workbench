"""离线写作实现：不联网、不加载模型，用确定性规则产出可检查文本。

定位：让「大纲 → 章节 → 标注 → 画面提示词」在**没有任何 LLM** 的情况下也能跑通全链路，
产出结构与真实模型完全一致（schema、字段、落盘位置都相同）。
接口与 `WritingAdapter` 协议保持一致，配置 `providers.writing=omlx` 时可无缝替换。

产出原则：
- 只使用 `metadata.characters` 里已有的角色，不新造人物；
- 同 seed + 同输入 → 完全相同的输出（可回归、可 diff）；
- 正文对白用中文引号并让说话人名字出现在引号前 30 字内，保证 `split_lines` 能识别。
"""

from __future__ import annotations

import hashlib
import json
import random
import re
from typing import Iterator

from .base import HealthStatus, TextGenChunk, TextGenRequest, TextGenResult, Usage

# 幕标题模板（按序号循环使用，避免生成重复标题）
ACT_TITLES = ("起", "承", "转", "合", "余波", "回响")
# 章节标题池
CHAPTER_TITLES = (
    "夜雨敲门",
    "旧信",
    "空屋",
    "第二把钥匙",
    "无人接听",
    "灯下",
    "断线",
    "归途",
    "证词",
    "余烬",
    "暗涌",
    "对岸",
)
# 叙述句池（用于补足目标字数）
NARRATION_POOL = (
    "空气里有一点潮味，像很久没有开过的窗。",
    "他没有立刻回答，只是把手里的东西放回原处。",
    "窗外的光落在桌面上，把影子拉得很长。",
    "远处传来一声闷响，随后一切又安静下来。",
    "她数着自己的呼吸，等对方先开口。",
    "墙上的钟走得很慢，秒针每一下都像在提醒什么。",
    "雨停了一会儿，又开始下。",
    "这个房间比记忆里小，也比记忆里冷。",
    "他忽然意识到，有些话一旦说出口就收不回去。",
    "两个人都没有再提那个名字。",
)
# 情绪候选（与项目 10 情绪对齐，供标注建议使用）
EMOTION_CYCLE = ("neutral", "tender", "sad", "angry", "surprise", "fear", "joy")
# 镜头时间候选
TIME_CYCLE = ("night", "day", "dusk", "morning")

# ---------- 设定生成素材（确定性抽取，保证离线可复现） ----------
WORLD_ERAS = (
    "近未来的沿海都会，常年被雾与潮声包裹",
    "民国末年的南方港城，报刊与帮会同时兴盛",
    "架空的北境边城，冬季长达八个月",
    "当代内陆小城，旧工业区正在被拆到一半",
)
WORLD_RULES = (
    "所有超自然现象都必须在午夜到凌晨三点之间发生",
    "谎言一旦被说出口，就会在说话人掌心留下痕迹",
    "进入旧城区必须交出一段真实记忆作为通行凭证",
    "身份文书由行会统一签发，伪造者终身不得入城",
    "任何契约都不能在雨中签署，否则条款会失效",
    "梦境中的死亡会真实反映为身体上的旧伤",
    "只有持灯人能在停电夜看清街巷的真实走向",
)
WORLD_FACTIONS = ("旧城行会", "守夜人小队", "衡平法庭", "北境运输联会", "灰市中间人")
WORLD_LOCATIONS = (
    ("雾港码头", "终年不散的雾里堆满无人认领的货箱，铁轨一直铺到水边。", "潮湿、空旷，远处有汽笛与铁链声"),
    ("旧档案楼", "六层砖楼，电梯常年停运，顶层被封条封住。", "安静、粉尘味重，翻纸声格外清楚"),
    ("长明街市", "夜里才开张的集市，摊主大多不肯报真名。", "人声细碎，灯光偏暖，偶有短促的争执"),
    ("北堤灯塔", "废弃多年，灯室里的镜片仍被人擦拭得很干净。", "海风灌入，金属结构轻微共振"),
    ("城南旧宅", "院墙很高，门牌被磨得看不清数字。", "压抑、回音明显，雨后更冷"),
)
WORLD_GLOSSARY = (
    ("持灯人", "在停电夜维持街巷秩序的人，行会不承认其存在。"),
    ("静默期", "每晚零点到三点，所有公开通信被迫中断的时段。"),
    ("旧案", "十年前未结的一批卷宗，涉及当年的整条运输线。"),
    ("凭证", "进入旧城区所需的记忆碎片，一旦交出无法取回。"),
    ("回声", "超自然现象残留的声音痕迹，只在原地可听。"),
    ("封条", "由衡平法庭签发的封存标记，撕开即视为违约。"),
)
TONE_POOL = ("冷峻克制", "绵密阴郁", "平静表面下暗流涌动", "疏离而克制，偶尔锋利")
PROSE_POOL = (
    "短句为主，少用形容词，靠动作与留白推进情绪",
    "长短句交替，对话密集，避免大段心理独白",
    "叙述冷静，细节具体，关键处刻意省略",
)
VISUAL_POOL = ("低饱和冷调，潮湿质感，浅景深", "灰蓝主色，硬光与长阴影", "暖黄灯光与冷色夜雨的对比")
FORBIDDEN_POOL = ("不要使用网络流行语", "不要出现作者旁白式议论", "不要用感叹号堆砌情绪", "不要写露骨暴力细节")
THEME_POOL = ("人在被规则驯服之后，还能不能为自己做一次选择", "真相的代价由谁来承担", "记忆与身份哪一个更接近真实")
ENDING_POOL = ("主角赢下真相，却失去唯一想守住的人", "主角放弃翻案，把证据留给后来者", "主角与对手达成交易，旧秩序被撬开一道缝")

# ---------- 人物生成素材 ----------
CHARACTER_NAME_POOL = (
    "沈砚",
    "林见夏",
    "顾长风",
    "苏晚",
    "程砚秋",
    "陆知微",
    "白翎",
    "韩照",
    "温叙",
    "叶阑",
    "许听澜",
    "周慎",
)
PERSONALITY_POOL = ("克制", "多疑", "护短", "执拗", "沉默", "敏锐", "体面", "记仇", "心软", "谨慎", "寡言", "刚硬")
AGE_RANGE_POOL = ("24-30", "28-35", "32-40", "38-45", "45-55")
FUNCTION_NOTES = ("对手：目标与主角相冲，但共享同一条底线", "盟友：愿意替主角承担风险，却隐瞒了一件事", "导师：知道旧案全貌，只在关键处给半句提示", "情感线：与主角互相试探，谁都不肯先低头")
POLISH_TAILS = (
    "这句话背后压着一段没人愿意提起的旧事。",
    "他把它说得很轻，像是在试探对方的分寸。",
    "语气停了一拍，留下一点不容追问的空白。",
)


class OfflineWritingAdapter:
    """离线写作适配器：实现 `WritingAdapter` 协议。"""

    def __init__(self, settings: dict | None = None):
        """构造适配器。

        参数:
            settings: 可选配置（目前支持 seed / chapter_words 覆盖）。
        """
        self.settings = settings or {}

    # ---------- 协议实现 ----------

    def generate(self, req: TextGenRequest) -> TextGenResult:
        """按任务类型生成文本。

        参数:
            req: 统一文本生成请求；`task` 决定走哪条规则。

        返回:
            TextGenResult（finish_reason=stop）。

        异常:
            ValueError: 未知 task。
        """
        task = (req.task or "freeform").strip()
        meta = req.metadata or {}
        if task == "outline":
            text = self._outline(meta)
        elif task == "chapter":
            text = self._chapter(meta, req)
        elif task == "summarize":
            text = self._summarize(meta)
        elif task == "script":
            text = self._script(meta)
        elif task == "image_prompt":
            text = self._image_prompt(meta)
        elif task == "setting":
            text = self._setting(meta)
        elif task == "character":
            text = self._character(meta)
        elif task == "polish":
            text = self._polish(meta)
        elif task == "freeform":
            text = self._freeform(req)
        else:
            raise ValueError(
                f"离线写作不支持的 task={task!r}（可选 outline/chapter/summarize/script/image_prompt/setting/character/polish/freeform）"
            )
        return TextGenResult(
            text=text,
            finish_reason="stop",
            model="offline-writing",
            usage=Usage(prompt_tokens=len(req.system) + sum(len(m.content) for m in req.messages), completion_tokens=len(text)),
            raw={"provider": "offline-writing", "task": task},
        )

    def generate_tool(self, req: TextGenRequest, tool: dict, timeout_s: float | None = None) -> dict:
        """离线实现工具调用：直接解析自身规则生成的结构化 JSON。

        参数:
            req: 统一文本请求（task/metadata 决定规则分支）。
            tool: 工具定义；只读取 function.name 以兼容包装结构。
            timeout_s: 忽略（本地规则无网络）。

        返回:
            与工具参数一致的结构化 dict。

        异常:
            ValueError: 规则输出不是合法 JSON。
        """
        payload = json.loads(self.generate(req).text)
        name = str(((tool or {}).get("function") or {}).get("name") or "")
        # 单人物规则输出多包一层 {"characters": [...]}，而工具参数要求平铺
        if name == "emit_character" and isinstance(payload.get("characters"), list) and payload["characters"]:
            return payload["characters"][0]
        # 单幕工具：从确定性整本大纲里取出本轮对应的那一幕（metadata.act_offset 由编排层指定）
        if name == "emit_act":
            acts = payload.get("acts")
            if isinstance(acts, list) and acts:
                try:
                    offset = int((req.metadata or {}).get("act_offset") or 0)
                except (TypeError, ValueError):
                    offset = 0
                index = offset if 0 <= offset < len(acts) else 0
                return {"act": acts[index]}
        return payload

    def stream(self, req: TextGenRequest) -> Iterator[TextGenChunk]:
        """流式输出：离线实现一次性给出整段，再用 done 块收尾。

        参数:
            req: 统一文本生成请求。

        产出:
            TextGenChunk 序列。
        """
        result = self.generate(req)
        yield TextGenChunk(delta=result.text)
        yield TextGenChunk(delta="", done=True, usage=result.usage)

    def health(self, timeout_s: float | None = None) -> HealthStatus:
        """离线能力固定可用。

        参数:
            timeout_s: 忽略。

        返回:
            HealthStatus(ok=True)。
        """
        return HealthStatus(ok=True, detail="离线写作（本地规则，无需模型）")

    def list_models(self) -> list[dict]:
        """列出能力标识。

        返回:
            [{"id": "offline-writing", ...}]。
        """
        return [{"id": "offline-writing", "kind": "offline", "tasks": ["outline", "chapter", "summarize", "script", "image_prompt", "setting", "character", "polish"]}]

    def unload(self, model: str | None = None) -> dict:
        """无模型可卸载。

        参数:
            model: 忽略。

        返回:
            {"status": "skipped"}。
        """
        return {"status": "skipped", "reason": "离线实现不占用显存"}

    # ---------- 大纲 ----------

    def _rng(self, meta: dict, extra: str = "") -> random.Random:
        """构造确定性随机源。

        参数:
            meta: 任务元信息（读取 seed）。
            extra: 额外区分因子。

        返回:
            random.Random 实例。
        """
        seed = str(meta.get("seed") if meta.get("seed") is not None else self.settings.get("seed", 20261110))
        raw = f"{seed}:{json.dumps(meta, ensure_ascii=False, sort_keys=True)}:{extra}"
        return random.Random(hashlib.sha256(raw.encode("utf-8")).hexdigest())

    def _outline(self, meta: dict) -> str:
        """生成幕/章/节拍三级大纲的 JSON 文本。

        参数:
            meta: {acts, chapters_per_act, beats_per_chapter, title, logline, characters}。

        返回:
            JSON 字符串（`{"acts": [...]}`）。
        """
        acts_n = max(1, int(meta.get("acts") or 3))
        per_act = max(1, int(meta.get("chapters_per_act") or 3))
        beats_n = max(1, int(meta.get("beats_per_chapter") or 3))
        characters = meta.get("characters") or []
        names = [item.get("name") for item in characters if item.get("name")] or ["主人公"]
        char_ids = [item.get("id") for item in characters if item.get("id")]
        logline = meta.get("logline") or meta.get("title") or "一个尚未命名的故事"
        rng = self._rng(meta, "outline")

        acts = []
        chapter_no = 0
        for act_index in range(acts_n):
            chapters = []
            for _ in range(per_act):
                chapter_no += 1
                title = CHAPTER_TITLES[(chapter_no - 1) % len(CHAPTER_TITLES)]
                beats = []
                for beat_index in range(beats_n):
                    focus = names[(chapter_no + beat_index) % len(names)]
                    beats.append(
                        {
                            "summary": f"{focus}在第 {chapter_no} 章的第 {beat_index + 1} 个转折里推进主线：{logline[:24]}",
                            "character_ids": [char_ids[(chapter_no + beat_index) % len(char_ids)]] if char_ids else [],
                            "turning_point": beat_index == beats_n - 1,
                            "target_words": int(meta.get("chapter_words") or 1200) // beats_n,
                        }
                    )
                chapters.append(
                    {
                        "index": chapter_no,
                        "title": title,
                        "summary": f"{names[chapter_no % len(names)]}面对新的线索，冲突升级到不可回避的地步。",
                        "beats": beats,
                    }
                )
            acts.append(
                {
                    "title": f"第{act_index + 1}幕·{ACT_TITLES[act_index % len(ACT_TITLES)]}",
                    "summary": f"围绕「{logline[:30]}」推进第 {act_index + 1} 阶段，人物关系发生变化。",
                    "chapters": chapters,
                }
            )
        return json.dumps({"acts": acts}, ensure_ascii=False, indent=2)

    # ---------- 章节正文 ----------

    def _chapter(self, meta: dict, req: TextGenRequest) -> str:
        """按节拍生成章节正文（Markdown）。

        参数:
            meta: {title, summary, beats, characters, pov, tone, target_words}。
            req: 原始请求（用于 system 提示回显）。

        返回:
            正文文本，段落之间空行分隔。
        """
        # 多轮续写：消息里有多条 user（首轮指令 + 续写指令）时，只按最后一条 user 指令产出增量
        if sum(1 for message in req.messages or [] if message.role == "user") > 1:
            return self._chapter_continuation(meta, req)
        beats = meta.get("beats") or [{"summary": meta.get("summary") or "推进主线", "target_words": 1000}]
        characters = [item for item in (meta.get("characters") or []) if item.get("name")]
        names = [item["name"] for item in characters] or ["主人公"]
        target_total = max(200, int(meta.get("target_words") or 1200))
        per_beat = max(120, target_total // max(1, len(beats)))
        rng = self._rng(meta, "chapter")
        paragraphs: list[str] = []

        for beat_index, beat in enumerate(beats):
            summary = (beat.get("summary") or "").strip() or "事情继续向前推进"
            lead = names[beat_index % len(names)]
            other = names[(beat_index + 1) % len(names)] if len(names) > 1 else lead
            # 叙述段：把节拍摘要展开成两句可读叙述
            paragraphs.append(f"{lead}停在门口，先看了一眼屋里的人。{summary}。")
            paragraphs.append(rng.choice(NARRATION_POOL))
            # 对白段：说话人名字必须出现在引号前 30 字内，供规则切分识别
            line_text = self._dialogue_line(summary, beat_index, rng)
            paragraphs.append(f"{other}把手里的东西放下，看着他：“{line_text}”")
            paragraphs.append(f"{lead}没有马上接话。{rng.choice(NARRATION_POOL)}")
            # 补足该节拍目标字数
            while self._count_words("\n\n".join(paragraphs)) < per_beat * (beat_index + 1):
                paragraphs.append(rng.choice(NARRATION_POOL))
                if len(paragraphs) > 400:
                    break
            if beat.get("turning_point"):
                paragraphs.append(f"就在这一刻，{lead}终于明白，事情已经没有回头路了。")

        content = "\n\n".join(paragraphs).strip() + "\n"
        # 超出目标 20% 时截断到最后一个完整句子
        if self._count_words(content) > target_total * 1.2:
            content = self._truncate(content, int(target_total * 1.15))
        return content

    @staticmethod
    def _last_user_content(req: TextGenRequest) -> str:
        """取最后一条 user 消息的正文（多轮续写的输入）。

        参数:
            req: 统一文本请求。

        返回:
            最后一条 user 消息的 content；没有 user 消息时返回空串。
        """
        for message in reversed(req.messages or []):
            if message.role == "user":
                return message.content or ""
        return ""

    @staticmethod
    def _segment_target(instruction: str, default: int = 400) -> int:
        """从续写指令里解析本段目标字数。

        参数:
            instruction: 续写指令文本（形如「本段目标约 800 字」）。
            default: 解析失败时的默认字数。

        返回:
            目标字数，限制在 100–4000 之间。
        """
        match = re.search(r"(\d+)\s*字", instruction or "")
        if not match:
            return default
        try:
            value = int(match.group(1))
        except ValueError:
            return default
        return max(100, min(4000, value))

    def _chapter_continuation(self, meta: dict, req: TextGenRequest) -> str:
        """多轮续写轮：按最后一条 user 指令产出确定性的新增正文。

        参数:
            meta: 章节元信息（提供确定性种子）。
            req: 当前轮请求（最后一条 user 消息即续写指令）。

        返回:
            新增正文文本，段落之间空行分隔。
        """
        instruction = self._last_user_content(req)
        target = self._segment_target(instruction)
        # 轮次不同 → 消息条数不同 → 种子不同，保证每轮新增内容不重复且可复现
        rng = self._rng(meta, f"chapter-continue:{len(req.messages)}:{instruction}")
        paragraphs: list[str] = []
        while self._count_words("\n\n".join(paragraphs)) < target:
            paragraphs.append(rng.choice(NARRATION_POOL))
            if len(paragraphs) > 60:
                break
        return "\n\n".join(paragraphs).strip() + "\n"

    # ---------- 正文反向总结 ----------

    def _summarize(self, meta: dict) -> str:
        """从正文确定性反推标题、摘要、大纲与节拍。

        参数:
            meta: {title, content, beats}；content 为正文（可能已被服务端截取开头与结尾）。

        返回:
            JSON 字符串（`{"title", "synopsis", "outline", "beats": [...]}`）。
        """
        content = str(meta.get("content") or "").strip()
        paragraphs = [item.strip() for item in content.split("\n\n") if item.strip()]
        if not paragraphs:
            paragraphs = [content or "（正文为空）"]
        current_title = str(meta.get("title") or "").strip()

        title = self._summarize_title(paragraphs[0], current_title)
        synopsis = self._summarize_synopsis(paragraphs)
        old_beats = [item for item in (meta.get("beats") or []) if isinstance(item, dict)]
        count = max(1, min(8, len(old_beats) or min(3, len(paragraphs))))
        beats = self._summarize_beats(paragraphs, count)
        outline = "\n".join(f"{index}. {beat['summary']}" for index, beat in enumerate(beats, start=1))
        return json.dumps(
            {"title": title, "synopsis": synopsis, "outline": outline, "beats": beats},
            ensure_ascii=False,
            indent=2,
        )

    @staticmethod
    def _summarize_title(first_paragraph: str, current_title: str) -> str:
        """从首段摘出标题候选，取不到时保留现有标题。

        参数:
            first_paragraph: 正文首段。
            current_title: 当前章节标题。

        返回:
            不超过 20 字的标题。
        """
        candidate = OfflineWritingAdapter._clip_sentence(first_paragraph, 20)
        return candidate or current_title or "正文重述"

    @staticmethod
    def _summarize_synopsis(paragraphs: list[str]) -> str:
        """用首段与末段拼出章节摘要。

        参数:
            paragraphs: 正文段落列表。

        返回:
            不超过 1000 字的摘要。
        """
        head = OfflineWritingAdapter._clip_sentence(paragraphs[0], 90)
        tail = OfflineWritingAdapter._clip_sentence(paragraphs[-1], 60) if len(paragraphs) > 1 else ""
        text = f"{head}……{tail}" if tail else head
        return text[:1000] or "本章推进主线。"

    @staticmethod
    def _summarize_beats(paragraphs: list[str], count: int) -> list[dict]:
        """把正文段落按顺序均分成若干节拍。

        参数:
            paragraphs: 正文段落列表。
            count: 节拍数量（1–8）。

        返回:
            [{summary, turning_point}]，最后一条标记为转折点。
        """
        total = len(paragraphs)
        base, extra = divmod(total, count)
        beats: list[dict] = []
        cursor = 0
        for index in range(count):
            take = base + (1 if index < extra else 0)
            group = paragraphs[cursor : cursor + take] or [paragraphs[min(cursor, total - 1)]]
            cursor += take
            summary = OfflineWritingAdapter._clip_sentence(group[0], 80)
            beats.append({"summary": summary or f"推进第 {index + 1} 个情节", "turning_point": index == count - 1})
        return beats

    @staticmethod
    def _clip_sentence(text: str, limit: int) -> str:
        """截取到最近的句读，避免标题或摘要停在半个词上。

        参数:
            text: 原文。
            limit: 最大字数。

        返回:
            截断后的文本（不含句末标点）。
        """
        source = str(text or "").strip().replace("\n", " ")
        positions = [source.find(mark) for mark in ("。", "！", "？", "；", "，", "…")]
        positions = [position for position in positions if 2 <= position <= limit]
        if positions:
            return source[: min(positions)].strip()
        return source[:limit].strip()

    @staticmethod
    def _dialogue_line(summary: str, index: int, rng: random.Random) -> str:
        """根据节拍摘要造一句可朗读的对白。

        参数:
            summary: 节拍摘要。
            index: 节拍序号（用于轮换句式）。
            rng: 确定性随机源。

        返回:
            一句对白文本（不含引号）。
        """
        core = summary.replace("\n", " ").strip("。")[:18] or "这件事"
        patterns = (
            f"你确定要现在说{core}的事吗",
            f"{core}，我们躲了这么久，还能躲多久",
            "我不信你没看出来，从一开始就不对",
            f"再给我一点时间，{core}我会处理",
            "别问了，问了你也一样睡不着",
        )
        return patterns[(index + rng.randint(0, len(patterns) - 1)) % len(patterns)]

    @staticmethod
    def _count_words(text: str) -> int:
        """统计中文字符数（去掉空白与标点后的粗略字数）。

        参数:
            text: 文本。

        返回:
            字数。
        """
        return sum(1 for char in text if not char.isspace())

    @staticmethod
    def _truncate(text: str, limit: int) -> str:
        """按字数上限截断，并尽量停在句末标点后。

        参数:
            text: 原文。
            limit: 字数上限。

        返回:
            截断后的文本。
        """
        count = 0
        cut = len(text)
        for index, char in enumerate(text):
            if not char.isspace():
                count += 1
            if count >= limit:
                cut = index + 1
                break
        head = text[:cut]
        for mark in ("。", "！", "？", "\n"):
            position = head.rfind(mark)
            if position > len(head) * 0.6:
                return head[: position + 1].strip() + "\n"
        return head.strip() + "\n"

    # ---------- 导演脚本标注 ----------

    def _script(self, meta: dict) -> str:
        """生成可直接被 `parse_markup` 解析的标注文本。

        参数:
            meta: {scenes:[{index,title,location,time_of_day,mood,lines:[{speaker_name,text,emotion,kind}],visual_prompt}], style}。

        返回:
            标注文本。
        """
        scenes = meta.get("scenes") or []
        style = (meta.get("style") or {}).get("visual_style") or ""
        lines_out: list[str] = []
        for scene_index, scene in enumerate(scenes):
            index = int(scene.get("index") or scene_index) + 1
            title = scene.get("title") or f"场景 {index}"
            lines = [item for item in (scene.get("lines") or []) if (item.get("text") or "").strip()]
            location = scene.get("location") or "未标注"
            time_of_day = scene.get("time_of_day") or TIME_CYCLE[scene_index % len(TIME_CYCLE)]
            mood = scene.get("mood") or "克制"
            lines_out.append(f"@shot {index} {title} location={location} time={time_of_day} mood={mood} goal=推进剧情")
            for line_index, line in enumerate(lines, start=1):
                speaker = (line.get("speaker_name") or "").strip()
                text = (line.get("text") or "").strip().replace("\n", " ")
                emotion = line.get("emotion") or "neutral"
                kind = line.get("kind") or ""
                if line.get("speaker_id") in (None, "chr_narrator") and not speaker:
                    lines_out.append(f"旁白：{text}")
                    continue
                head = speaker or "旁白"
                if emotion != "neutral":
                    head += f"|{emotion}"
                if kind == "inner_monologue":
                    head += "|inner_monologue"
                lines_out.append(f"{head}：{text}")
            # 每个镜头一条环境音 + 一张关键图 + 一段动画位
            lines_out.append(f"!sfx ambience 6000 after-line=1 {title}环境底噪")
            lines_out.append(f"!visual 3:4 {self._visual_prompt(scene, style)}")
            lines_out.append("!anim i2v 3000 slow_push_in 镜头缓慢推近，强化情绪")
        return "\n".join(lines_out) + "\n"

    @staticmethod
    def _visual_prompt(scene: dict, style: str) -> str:
        """拼一条画面提示词。

        参数:
            scene: 场景信息。
            style: 全局视觉风格。

        返回:
            提示词文本（单行）。
        """
        parts = [scene.get("title") or "场景"]
        if scene.get("lines"):
            parts.append((scene["lines"][0].get("text") or "")[:16])
        if style:
            parts.append(style)
        parts.append("电影感构图，柔和光线，细节清晰")
        return "，".join(part for part in parts if part).replace("\n", " ")

    # ---------- 画面提示词 ----------

    def _image_prompt(self, meta: dict) -> str:
        """生成单条画面提示词。

        参数:
            meta: {scene_title, summary, style, time_of_day}。

        返回:
            提示词文本。
        """
        title = meta.get("scene_title") or "场景"
        summary = (meta.get("summary") or "").strip()
        style = (meta.get("style") or {}).get("visual_style") or ""
        time_of_day = meta.get("time_of_day") or ""
        parts = [title]
        if summary:
            parts.append(summary[:40])
        if time_of_day:
            parts.append(f"{time_of_day}光线")
        if style:
            parts.append(style)
        parts.append("电影感，浅景深")
        return "，".join(parts)

    # ---------- 设定 / 人物 / 润色 ----------

    def _setting(self, meta: dict) -> str:
        """按 step 生成世界 / 梗概 / 文风的结构化 JSON。

        参数:
            meta: {step, work:{title,author,genre}, existing, target_length, seed}。

        返回:
            JSON 字符串（世界步骤为平铺的 world 字段，梗概/文风同理）。
        """
        step = str(meta.get("step") or "").strip()
        work = meta.get("work") or {}
        title = str(work.get("title") or "未命名").strip() or "未命名"
        genres = [str(item).strip() for item in (work.get("genre") or []) if str(item).strip()]
        rng = self._rng(meta, f"setting:{step}")
        genre_text = "、".join(genres) or "悬疑"
        brief = str(meta.get("brief") or "").strip()

        if step == "premise":
            target_length = meta.get("target_length")
            if target_length not in ("short", "novella", "novel", "series"):
                target_length = "novella"
            fields = {
                "logline": f"《{title}》里，一个习惯替别人收拾残局的人，被迫重查一桩被所有人默认已经结案的旧事，最后要在真相与仅剩的牵挂之间做一次不可逆的选择。",
                "theme": rng.choice(THEME_POOL),
                "core_conflict": f"主角想查清旧案，体制与旧关系网却需要这桩案子永远封存；越接近真相，他越要付出身边的人作代价。",
                "ending_intent": rng.choice(ENDING_POOL),
                "target_audience": f"偏好{genre_text}与人物弧光的成年读者",
                "target_length": target_length,
            }
            return json.dumps(fields, ensure_ascii=False, indent=2)

        if step == "style":
            fields = {
                "tone": rng.choice(TONE_POOL),
                "pov": "third_limited",
                "tense": "past",
                "prose_style": rng.choice(PROSE_POOL),
                "forbidden": list(rng.sample(FORBIDDEN_POOL, 2)),
                "visual_style": rng.choice(VISUAL_POOL),
            }
            return json.dumps(fields, ensure_ascii=False, indent=2)

        if step == "worlds":
            try:
                total = int(meta.get("count") or 2)
            except (TypeError, ValueError):
                total = 2
            total = max(1, min(6, total))
            worlds = []
            for index in range(total):
                rules = list(rng.sample(WORLD_RULES, 4))
                factions = list(rng.sample(WORLD_FACTIONS, 3))
                locations = [
                    {"name": name, "description": desc, "atmosphere": mood}
                    for name, desc, mood in rng.sample(WORLD_LOCATIONS, 3)
                ]
                glossary = [{"term": term, "meaning": meaning} for term, meaning in rng.sample(WORLD_GLOSSARY, 4)]
                tag = (brief or title)[:30]
                worlds.append(
                    {
                        "name": f"{tag}·世界{index + 1}",
                        "era": (f"{brief}：{rng.choice(WORLD_ERAS)}（{genre_text}基调）" if brief else f"{rng.choice(WORLD_ERAS)}（{genre_text}基调）")[:200],
                        "summary": (
                            f"{brief}。这是第 {index + 1} 个世界的舞台设定，核心矛盾围绕「{title}」的主线展开。"
                            if brief
                            else f"《{title}》的第 {index + 1} 个世界：旧秩序与主线真相相互牵制，人物在规则缝隙里推进。"
                        )[:2000],
                        "rules": rules,
                        "factions": factions,
                        "locations": locations,
                        "glossary": glossary,
                        "visual_style": rng.choice(VISUAL_POOL),
                    }
                )
            return json.dumps({"worlds": worlds}, ensure_ascii=False, indent=2)

        if step == "world":
            rules = list(rng.sample(WORLD_RULES, 4))
            factions = list(rng.sample(WORLD_FACTIONS, 3))
            locations = [
                {"name": name, "description": desc, "atmosphere": mood}
                for name, desc, mood in rng.sample(WORLD_LOCATIONS, 3)
            ]
            glossary = [{"term": term, "meaning": meaning} for term, meaning in rng.sample(WORLD_GLOSSARY, 4)]
            fields = {
                # name / summary / visual_style 供 generate_section 使用，设定库路径会按字段白名单过滤
                "name": (brief or f"{title}·主世界")[:120],
                "era": (f"{brief}：{rng.choice(WORLD_ERAS)}（{genre_text}基调）" if brief else f"{rng.choice(WORLD_ERAS)}（{genre_text}基调）")[:200],
                "summary": (
                    f"{brief}。核心舞台与主线冲突都在这个世界里展开。"
                    if brief
                    else f"《{title}》的主世界：旧秩序与主线真相相互牵制。"
                )[:2000],
                "rules": rules,
                "factions": factions,
                "locations": locations,
                "glossary": glossary,
                "visual_style": rng.choice(VISUAL_POOL),
            }
            return json.dumps(fields, ensure_ascii=False, indent=2)

        raise ValueError(f"离线写作不支持的设定步骤: {step!r}（可选 world/worlds/premise/style）")

    def _character(self, meta: dict) -> str:
        """生成 1 名主角 + 2–6 名配角的人物 JSON。

        参数:
            meta: {work, premise, world, count, brief, mode, seed}；mode="single" 时只产出 1 个完整人物。

        返回:
            JSON 字符串（`{"characters": [...]}`）；single 模式的人物额外含 appearance_prompt / negative_prompt / state。
        """
        rng = self._rng(meta, "character")
        brief = str(meta.get("brief") or "").strip()
        if meta.get("mode") == "single":
            name = rng.choice(CHARACTER_NAME_POOL)
            personality = list(rng.sample(PERSONALITY_POOL, 3))
            if brief:
                personality = [brief[:40]] + personality[:2]
            age_range = rng.choice(AGE_RANGE_POOL)
            background = (
                (f"{brief}；" if brief else "")
                + "旧案发生时是唯一的目击者，成年后进入体制内做善后工作；"
                "目标是把当年被掩埋的事实拼回原样，缺陷是不肯求助、习惯独自承担，"
                "弧光从「只信证据」走向「愿意为活人让步」。"
            )
            appearance = ((brief + "，") if brief else "") + f"{age_range}，五官清晰，服装质感明确，光线柔和"
            return json.dumps(
                {
                    "characters": [
                        {
                            "name": name,
                            "role": "protagonist",
                            "gender": ("male", "female", "other")[rng.randint(0, 2)],
                            "age_range": age_range,
                            "personality": personality,
                            "background": background,
                            "appearance_prompt": appearance,
                            "negative_prompt": "低质, 多余手指, 水印, 文字",
                            "state": {"relationship_notes": {}},
                        }
                    ]
                },
                ensure_ascii=False,
                indent=2,
            )
        try:
            total = int(meta.get("count") or 4)
        except (TypeError, ValueError):
            total = 4
        total = max(3, min(7, total))
        names = list(rng.sample(CHARACTER_NAME_POOL, total))
        roles = ["protagonist", "antagonist"] + ["supporting"] * (total - 2)

        characters = []
        for index, (name, role) in enumerate(zip(names, roles)):
            personality = list(rng.sample(PERSONALITY_POOL, 3))
            gender = ("male", "female", "other")[index % 3]
            age_range = AGE_RANGE_POOL[index % len(AGE_RANGE_POOL)]
            if role == "protagonist":
                background = (
                    (f"{brief}；" if brief else "")
                    + "旧案发生时是唯一的目击者，成年后进入体制内做善后工作；"
                    "目标是把当年被掩埋的事实拼回原样，动机是替一个人兑现迟到的承诺，"
                    "缺陷是不肯求助、习惯独自承担，秘密是他也动过销毁一份证据的念头；"
                    "弧光从「只信证据」走向「愿意为活人让步」。"
                )
                if brief:
                    personality = [brief[:40]] + personality[:2]
            else:
                background = (
                    f"{FUNCTION_NOTES[(index - 1) % len(FUNCTION_NOTES)]}；"
                    f"与主角的旧案有直接牵连，立场随剧情推进发生偏移。"
                )
            relationships = {}
            for other in names:
                if other == name:
                    continue
                relationships[other] = f"{name}与{other}之间有一笔没结清的旧账"
            if role == "protagonist":
                appearance = ((brief + "，") if brief else "") + f"{age_range}，五官清晰，服装质感明确，光线柔和"
            else:
                appearance = (
                    ((brief + "，") if brief else "")
                    + f"{age_range}，{gender}，{role} 定位，五官清晰，服装质感明确，光线柔和"
                )
            characters.append(
                {
                    "name": name,
                    "role": role,
                    "gender": gender,
                    "age_range": age_range,
                    "personality": personality,
                    "background": background,
                    "relationships": relationships,
                    "appearance_prompt": appearance,
                    "negative_prompt": "低质, 多余手指, 水印, 文字",
                }
            )
        return json.dumps({"characters": characters}, ensure_ascii=False, indent=2)

    def _polish(self, meta: dict) -> str:
        """确定性润色单字段文本。

        参数:
            meta: {field, text, context}。

        返回:
            JSON 字符串（`{"text": "..."}`）。
        """
        field = str(meta.get("field") or "").strip()
        source = " ".join(str(meta.get("text") or "").split())
        rng = self._rng(meta, f"polish:{field}")
        if not source:
            return json.dumps({"text": ""}, ensure_ascii=False)
        if len(source) < 24:
            source = source.rstrip("，,。；; ") + "，" + rng.choice(POLISH_TAILS)
        if source[-1] not in "。！？…":
            source += "。"
        return json.dumps({"text": source}, ensure_ascii=False)

    # ---------- 自由文本 ----------

    def _freeform(self, req: TextGenRequest) -> str:
        """回显请求内容（调试与联调占位用）。

        参数:
            req: 请求。

        返回:
            带说明的文本。
        """
        last = self._last_user_content(req)
        return f"[offline-writing] 已收到 {len(req.messages)} 条消息，末条长度 {len(last)}。系统提示：{req.system[:80]}"
