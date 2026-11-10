"""跨章上下文装配。

固定顺序（system 与 user 合计）：
    故事圣经 → 世界设定 → 角色卡 → 前情摘要 → 本章大纲与节拍 → 前文摘录 → 任务指令。
预算不足时按以下顺序裁剪：
    前文摘录 → 前情摘要（先删最旧）→ 世界细节（先地点/术语，再硬规则/势力）
    → 角色卡细节（先删背景，再删性格/说话方式）。
故事圣经与本次任务指令永不被裁剪；角色设定只从 characters.json 取。
"""

from __future__ import annotations

from ..adapters.base import Message, TextGenRequest
from ..models.entities import NARRATOR_ID

#: 前序章节缺少摘要时，回退取正文末尾的字数
SYNOPSIS_FALLBACK_CHARS = 800
#: 前文摘录（上一章结尾 / 本章已有正文结尾）每段最多取的字数
EXCERPT_CHARS = 800
#: 视角枚举的中文说明，未知取值原样展示
POV_LABELS = {
    "first": "第一人称",
    "second": "第二人称",
    "third_limited": "第三人称有限视角",
    "third_omniscient": "第三人称全知",
}
#: 时态枚举的中文说明，未知取值原样展示
TENSE_LABELS = {"past": "过去时", "present": "现在时"}
#: 角色定位枚举的中文说明，避免角色卡里出现英文取值
ROLE_LABELS = {
    "protagonist": "主角",
    "antagonist": "反派",
    "supporting": "配角",
    "minor": "龙套",
    "narrator": "旁白",
}
#: 性别枚举的中文说明
GENDER_LABELS = {"male": "男", "female": "女", "other": "其他", "unknown": "未知"}


def _text(value) -> str:
    """把任意值转成去空白字符串。

    参数:
        value: 原始值。

    返回:
        去空白后的字符串；None 返回空串。
    """
    return str(value or "").strip()


class ContextAssembler:
    """按预算拼装一次文本生成请求。"""

    def __init__(
        self,
        work: dict | None = None,
        bible: dict | None = None,
        world_digest: str | dict | None = None,
        characters: list[dict] | None = None,
        chapter: dict | None = None,
        outline_chapter: dict | None = None,
        previous_chapters: list[dict] | None = None,
        current_content: str = "",
        budget_tokens: int = 32768,
    ):
        """绑定上下文素材。

        参数:
            work: 作品元数据（title/logline/genre/style_guide）。
            bible: 设定库（读取 premise/style；用于故事圣经与世界回退）。
            world_digest: 本章世界摘要文本；也接受设定库摘要 dict（未绑定世界时）。
            characters: 全量角色表（含旁白，内部会过滤）。
            chapter: 当前章节（含 title/index/outline/synopsis/content）。
            outline_chapter: 设定库中的当前大纲章节（含 summary/beats/pov_character_id）。
            previous_chapters: 前序章节列表，每项含 index/title/synopsis/content。
            current_content: 本章已有正文，用于重写场景的前文摘录。
            budget_tokens: 上下文预算（中文按 1 字 ≈ 1 token 粗算）。
        """
        self.work = work or {}
        self.bible = bible or {}
        self.world_digest = world_digest
        self.characters = characters or []
        self.chapter = chapter or {}
        self.outline_chapter = outline_chapter or {}
        self.previous_chapters = previous_chapters or []
        self.current_content = current_content or ""
        self.budget_tokens = int(budget_tokens or 32768)
        #: 本次装配产生的告警（已被重置为最新一次 build 的结果）
        self.warnings: list[str] = []

    # ---------- 各段渲染 ----------

    def story_bible(self) -> str:
        """渲染故事圣经：作品名、简介、类型、主题、核心冲突、结局意图与文风。

        返回:
            故事圣经文本（永不裁剪）。
        """
        premise = self.bible.get("premise") or {}
        style = self.bible.get("style") or {}
        lines = ["【故事圣经】", f"作品：{self.work.get('title') or '未命名'}"]
        logline = _text(premise.get("logline")) or _text(self.work.get("logline"))
        if logline:
            lines.append(f"一句话简介：{logline}")
        genre = [str(item) for item in (self.work.get("genre") or []) if _text(item)]
        if genre:
            lines.append("类型：" + "、".join(genre))
        for key, label in (("theme", "主题"), ("core_conflict", "核心冲突"), ("ending_intent", "结局意图")):
            value = _text(premise.get(key))
            if value:
                lines.append(f"{label}：{value}")
        if _text(self.work.get("style_guide")):
            lines.append(f"文风要求：{_text(self.work.get('style_guide'))}")
        tone = _text(style.get("tone"))
        if tone:
            lines.append(f"基调：{tone}")
        if _text(style.get("pov")):
            pov = _text(style.get("pov"))
            lines.append("视角：" + POV_LABELS.get(pov, pov))
        if _text(style.get("tense")):
            tense = _text(style.get("tense"))
            lines.append("时态：" + TENSE_LABELS.get(tense, tense))
        if _text(style.get("prose_style")):
            lines.append(f"句式偏好：{_text(style.get('prose_style'))}")
        forbidden = [str(item) for item in (style.get("forbidden") or []) if _text(item)]
        if forbidden:
            lines.append("禁止项：" + "；".join(forbidden))
        return "\n".join(lines)

    def _bible_world_sections(self, digest: dict) -> list[dict]:
        """把设定库摘要 dict 拆成可裁剪的世界段落。

        参数:
            digest: `_bible_digest` 风格的设定摘要 dict。

        返回:
            [{"kind": 段落类型, "text": 文本行}]。
        """
        sections: list[dict] = [{"kind": "head", "text": "【设定库摘要】"}]
        for key, label in (("logline", "一句话简介"), ("theme", "主题"), ("core_conflict", "核心冲突"), ("tone", "基调")):
            value = _text(digest.get(key))
            if value:
                sections.append({"kind": "summary", "text": f"{label}：{value}"})
        era = _text(digest.get("era"))
        if era:
            sections.append({"kind": "summary", "text": f"时代：{era}"})
        for key, label in (("rules", "硬规则"), ("factions", "势力")):
            values = [str(item) for item in (digest.get(key) or []) if _text(item)]
            if values:
                sections.append({"kind": key, "text": f"{label}：{'；'.join(values)}"})
        for key, label in (("locations", "地点"), ("glossary", "术语")):
            values = [str(item) for item in (digest.get(key) or []) if _text(item)]
            if values:
                sections.append({"kind": key, "text": f"{label}：{'；'.join(values)}"})
        return sections

    def _world_sections(self) -> list[dict]:
        """把本章世界摘要拆成可裁剪的段落（按行识别地点/术语/规则/势力）。

        返回:
            [{"kind": head|summary|rules|factions|locations|glossary, "text": 文本行}]。
        """
        if isinstance(self.world_digest, dict):
            return self._bible_world_sections(self.world_digest)
        text = _text(self.world_digest)
        if not text:
            return []
        sections: list[dict] = []
        for raw_line in text.splitlines():
            line = raw_line.rstrip()
            if not line.strip():
                continue
            if line.startswith("地点："):
                kind = "locations"
            elif line.startswith("术语："):
                kind = "glossary"
            elif line.startswith("硬规则："):
                kind = "rules"
            elif line.startswith("势力："):
                kind = "factions"
            elif line.startswith("【"):
                kind = "head"
            else:
                kind = "summary"
            sections.append({"kind": kind, "text": line})
        return sections

    def _character_models(self, speaker_ids: list[str] | None = None) -> list[dict]:
        """筛选并整理角色卡模型。

        参数:
            speaker_ids: 只取这些角色；为 None 时取全部非旁白角色。

        返回:
            角色卡 dict 列表，附带 show_background/show_persona 两个裁剪开关。
        """
        allowed = {str(item) for item in speaker_ids} if speaker_ids is not None else None
        models: list[dict] = []
        for character in self.characters:
            character_id = _text(character.get("id"))
            if character_id == NARRATOR_ID or character.get("role") == "narrator":
                continue
            if allowed is not None and character_id not in allowed:
                continue
            speech = character.get("speech_style") or {}
            speech_text = "，".join(
                part for part in (_text(speech.get("tempo")), _text(speech.get("pitch"))) if part
            )
            notes = (character.get("state") or {}).get("relationship_notes") or {}
            models.append(
                {
                    "name": _text(character.get("name")),
                    "role": _text(character.get("role")),
                    "gender": _text(character.get("gender")),
                    "age_range": _text(character.get("age_range")),
                    "aliases": [str(item) for item in (character.get("aliases") or []) if _text(item)],
                    "personality": [str(item) for item in (character.get("personality") or []) if _text(item)],
                    "background": _text(character.get("background")),
                    "speech": speech_text,
                    "relationships": {str(key): _text(value) for key, value in notes.items() if _text(value)},
                    "show_background": True,
                    "show_persona": True,
                }
            )
        return models

    def _render_cards(self, models: list[dict]) -> str:
        """把角色卡模型渲染为文本。

        参数:
            models: `_character_models` 的返回值。

        返回:
            角色卡文本；没有角色时返回空串。
        """
        cards: list[str] = []
        for card in models:
            role_label = ROLE_LABELS.get(card["role"], card["role"])
            traits = [role_label]
            gender_label = GENDER_LABELS.get(card.get("gender") or "", card.get("gender") or "")
            if gender_label:
                traits.append(gender_label)
            if card.get("age_range"):
                traits.append(card["age_range"])
            head = f"- {card['name']}（{'，'.join(trait for trait in traits if trait)}）"
            if card["aliases"]:
                head += "，别名：" + "、".join(card["aliases"])
            lines = [head]
            if card["show_persona"] and card["personality"]:
                lines.append("  性格：" + "、".join(card["personality"]))
            if card["show_background"] and card["background"]:
                lines.append("  背景：" + card["background"])
            if card["show_persona"] and card["speech"]:
                lines.append("  说话方式：" + card["speech"])
            if card["relationships"]:
                lines.append("  关系备注：" + "；".join(f"{key}：{value}" for key, value in card["relationships"].items()))
            cards.append("\n".join(lines))
        return "\n".join(cards)

    def character_cards(self, speaker_ids: list[str] | None = None) -> str:
        """生成角色卡文本（含性格、背景、说话方式、关系备注）。

        参数:
            speaker_ids: 只输出这些角色；为 None 时输出全部非旁白角色。

        返回:
            角色卡文本。
        """
        return self._render_cards(self._character_models(speaker_ids))

    def _previous_summaries(self) -> list[str]:
        """生成前情摘要列表：按 index 从旧到新，缺失摘要时回退正文末尾。

        返回:
            每条前情文本；既无摘要也无正文的章节被跳过并写入 warnings。
        """
        summaries: list[str] = []
        ordered = sorted(self.previous_chapters, key=lambda item: int(item.get("index") or 0))
        for item in ordered:
            index = item.get("index")
            title = _text(item.get("title"))
            synopsis = _text(item.get("synopsis"))
            content = _text(item.get("content"))
            if synopsis:
                text = synopsis
            elif content:
                text = content[-SYNOPSIS_FALLBACK_CHARS:]
            else:
                self.warnings.append(f"第 {index} 章既无摘要也无正文，已跳过前情")
                continue
            label = f"第 {index} 章" + (f"《{title}》" if title else "")
            summaries.append(f"{label}：{text}")
        return summaries

    def _excerpt_blocks(self) -> list[dict]:
        """生成前文摘录块：上一章正文末尾与本章已有正文末尾。

        返回:
            [{"kind": previous|current, "title": 标签, "text": 末尾文本}]。
        """
        blocks: list[dict] = []
        ordered = sorted(self.previous_chapters, key=lambda item: int(item.get("index") or 0))
        if ordered:
            last = ordered[-1]
            content = _text(last.get("content"))
            if content:
                title = _text(last.get("title"))
                label = f"第 {last.get('index')} 章" + (f"《{title}》" if title else "")
                blocks.append({"kind": "previous", "title": f"【上一章{label}结尾】", "text": content[-EXCERPT_CHARS:]})
        current = _text(self.current_content)
        if current:
            blocks.append({"kind": "current", "title": "【本章已有正文结尾】", "text": current[-EXCERPT_CHARS:]})
        return blocks

    def chapter_outline(self) -> str:
        """渲染本章大纲与节拍（不参与裁剪）。

        返回:
            大纲文本，包含章节标题、摘要与逐条节拍（目标字数/转折点）。
        """
        index = self.outline_chapter.get("index") or self.chapter.get("index")
        title = _text(self.outline_chapter.get("title")) or _text(self.chapter.get("title")) or "未命名"
        label = f"第 {index} 章《{title}》" if index else f"《{title}》"
        lines = ["【本章大纲与节拍】", f"章节：{label}"]
        summary = (
            _text(self.outline_chapter.get("summary"))
            or _text(self.chapter.get("synopsis"))
            or _text(self.chapter.get("outline"))
        )
        if summary:
            lines.append(f"章节摘要：{summary}")
        beats = self.outline_chapter.get("beats") or []
        if beats:
            lines.append("节拍：")
            for order, beat in enumerate(beats, start=1):
                tags = []
                try:
                    target = int(beat.get("target_words") or 0)
                except (TypeError, ValueError):
                    target = 0
                if target:
                    tags.append(f"目标 {target} 字")
                if beat.get("turning_point"):
                    tags.append("转折点")
                suffix = f"（{'，'.join(tags)}）" if tags else ""
                lines.append(f"{order}. {_text(beat.get('summary'))}{suffix}")
        elif _text(self.chapter.get("outline")):
            lines.append("大纲：" + _text(self.chapter.get("outline")))
        return "\n".join(lines)

    # ---------- 装配 ----------

    @staticmethod
    def _render_world(sections: list[dict]) -> str:
        """把世界段落拼成文本。

        参数:
            sections: `_world_sections` 的返回值（部分可能已被裁剪）。

        返回:
            世界设定文本；没有内容时返回空串。
        """
        lines = [section["text"] for section in sections]
        if not lines:
            return ""
        return "世界设定：\n" + "\n".join(lines)

    @staticmethod
    def _render_previous(summaries: list[str]) -> str:
        """把前情摘要列表拼成文本。

        参数:
            summaries: 每条前情文本（从旧到新）。

        返回:
            前情摘要文本；为空时返回空串。
        """
        return "\n".join(summaries)

    @staticmethod
    def _render_excerpt(blocks: list[dict]) -> str:
        """把前文摘录块拼成文本。

        参数:
            blocks: `_excerpt_blocks` 的返回值（部分可能已被裁剪）。

        返回:
            前文摘录文本；为空时返回空串。
        """
        return "\n".join(f"{block['title']}\n{block['text']}" for block in blocks)

    def build(self, task: str, instruction: str, speaker_ids: list[str] | None = None) -> TextGenRequest:
        """装配一次文本生成请求。

        参数:
            task: 任务类型（outline/chapter/scene/dialogue_split/emotion_tag/image_prompt/freeform）。
            instruction: 本次任务指令（永不裁剪）。
            speaker_ids: 本场出场角色 ID 列表；为 None 时使用全部非旁白角色。

        返回:
            TextGenRequest；超预算时按「前文摘录 → 前情摘要 → 世界细节 → 角色卡细节」裁剪。
        """
        self.warnings = []
        instruction = instruction or ""
        story = self.story_bible()
        world_sections = self._world_sections()
        cards = self._character_models(speaker_ids)
        summaries = self._previous_summaries()
        excerpts = self._excerpt_blocks()
        outline = self.chapter_outline()

        def compose() -> tuple[str, str]:
            """按固定顺序拼出 system 与 user 文本。"""
            system_parts = [story]
            world_text = self._render_world(world_sections)
            if world_text:
                system_parts.append(world_text)
            cards_text = self._render_cards(cards)
            if cards_text:
                system_parts.append("角色卡：\n" + cards_text)
            # 人物演绎准则：角色要像真人一样按人设与世界观行动，固定放在设定之后
            system_parts.append(
                "写作准则：\n"
                "- 人物言行必须符合其性格、背景、说话方式与关系备注，不得为推进剧情改变人设；\n"
                "- 人物只知道自己在故事中经历或被告知的信息，不能凭空说出未知事实；\n"
                "- 让人物按自身目标与处境做出选择来推动剧情，而不是由作者安排；\n"
                "- 世界规则与势力格局不可违背，人物行动须发生在设定允许的范围内。"
            )
            system = "\n\n".join(part for part in system_parts if part)

            user_parts: list[str] = []
            previous_text = self._render_previous(summaries)
            if previous_text:
                user_parts.append("前情摘要：\n" + previous_text)
            if outline:
                user_parts.append(outline)
            excerpt_text = self._render_excerpt(excerpts)
            if excerpt_text:
                user_parts.append("前文摘录：\n" + excerpt_text)
            user_parts.append("任务指令：\n" + instruction)
            return system, "\n\n".join(part for part in user_parts if part)

        system, user = compose()
        # 中文按 1 字 ≈ 1 token 粗算，system 与 user 合计计入预算
        total = len(system) + len(user)
        while total > self.budget_tokens:
            changed = False

            # 1. 前文摘录：先删本章已有正文结尾，再删上一章结尾
            if excerpts:
                for kind in ("current", "previous"):
                    position = next((i for i, block in enumerate(excerpts) if block["kind"] == kind), None)
                    if position is not None:
                        excerpts.pop(position)
                        changed = True
                        break

            # 2. 前情摘要：删最旧的一条
            if not changed and summaries:
                summaries.pop(0)
                changed = True

            # 3. 世界细节：先删地点/术语，再删硬规则/势力
            if not changed and world_sections:
                for kinds in (("locations", "glossary"), ("rules", "factions")):
                    position = next((i for i, section in enumerate(world_sections) if section["kind"] in kinds), None)
                    if position is not None:
                        world_sections.pop(position)
                        changed = True
                        break

            # 4. 角色卡细节：先删背景，再删性格/说话方式
            if not changed and cards:
                target = next((card for card in cards if card["show_background"]), None)
                if target is not None:
                    target["show_background"] = False
                    changed = True
                else:
                    target = next((card for card in cards if card["show_persona"]), None)
                    if target is not None:
                        target["show_persona"] = False
                        changed = True

            if not changed:
                break
            system, user = compose()
            total = len(system) + len(user)

        if total > self.budget_tokens:
            self.warnings.append(
                f"上下文仍超出预算（{total} > {self.budget_tokens}）：故事圣经与任务指令不可裁剪"
            )

        return TextGenRequest(
            task=task,
            system=system,
            messages=[Message(role="user", content=user)],
            context_budget_tokens=self.budget_tokens,
        )
