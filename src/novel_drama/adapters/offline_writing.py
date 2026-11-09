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
        elif task == "script":
            text = self._script(meta)
        elif task == "image_prompt":
            text = self._image_prompt(meta)
        elif task == "freeform":
            text = self._freeform(req)
        else:
            raise ValueError(f"离线写作不支持的 task={task!r}（可选 outline/chapter/script/image_prompt/freeform）")
        return TextGenResult(
            text=text,
            finish_reason="stop",
            model="offline-writing",
            usage=Usage(prompt_tokens=len(req.system) + sum(len(m.content) for m in req.messages), completion_tokens=len(text)),
            raw={"provider": "offline-writing", "task": task},
        )

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
        return [{"id": "offline-writing", "kind": "offline", "tasks": ["outline", "chapter", "script", "image_prompt"]}]

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

    # ---------- 自由文本 ----------

    def _freeform(self, req: TextGenRequest) -> str:
        """回显请求内容（调试与联调占位用）。

        参数:
            req: 请求。

        返回:
            带说明的文本。
        """
        last = req.messages[-1].content if req.messages else ""
        return f"[offline-writing] 已收到 {len(req.messages)} 条消息，末条长度 {len(last)}。系统提示：{req.system[:80]}"
