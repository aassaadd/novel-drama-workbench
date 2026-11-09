"""长文本上下文装配。

固定顺序（预算不足时从后往前裁剪）：
    故事圣经 → 角色卡 → 滚动摘要 → 当前章节大纲/场景目标 → 最近原文 → 本次任务指令。
角色设定只从 characters.json 取，不接受模型临时编造。
"""

from __future__ import annotations

from ..adapters.base import Message, TextGenRequest


class ContextAssembler:
    """按预算拼装一次文本生成请求。"""

    def __init__(self, work: dict, characters: list[dict], chapter: dict, budget_tokens: int = 32768):
        """绑定上下文素材。

        参数:
            work: 作品元数据（title/style_guide/logline 等）。
            characters: 角色表。
            chapter: 当前章节（含大纲、摘要、场景）。
            budget_tokens: 上下文预算（中文按 1 字 ≈ 1 token 粗算）。
        """
        self.work = work or {}
        self.characters = characters or []
        self.chapter = chapter or {}
        self.budget_tokens = budget_tokens

    def story_bible(self) -> str:
        """生成故事圣经（作品设定 + 文风 + 人物关系摘要）。"""
        lines = [f"作品：{self.work.get('title', '')}"]
        if self.work.get("logline"):
            lines.append(f"一句话简介：{self.work['logline']}")
        if self.work.get("genre"):
            lines.append("类型：" + "、".join(self.work["genre"]))
        if self.work.get("style_guide"):
            lines.append(f"文风要求：{self.work['style_guide']}")
        return "\n".join(lines)

    def character_cards(self, speaker_ids: list[str] | None = None) -> str:
        """生成角色卡；未指定 speaker_ids 时输出全部非旁白角色。

        参数:
            speaker_ids: 只输出这些角色（场景出场角色）。

        返回:
            角色卡文本。
        """
        cards = []
        for character in self.characters:
            if speaker_ids is not None and character.get("id") not in speaker_ids:
                continue
            if character.get("role") == "narrator":
                continue
            parts = [f"- {character.get('name')}（{character.get('role')}）"]
            if character.get("personality"):
                parts.append("性格：" + "、".join(character["personality"]))
            if character.get("background"):
                parts.append("背景：" + character["background"])
            speech = character.get("speech_style") or {}
            if speech.get("tempo") or speech.get("pitch"):
                parts.append(f"说话方式：{speech.get('tempo') or ''} {speech.get('pitch') or ''}".strip())
            cards.append("；".join(parts))
        return "\n".join(cards)

    def rolling_summary(self) -> str:
        """生成滚动摘要（章节摘要 + 前序场景摘要）。"""
        parts = []
        if self.chapter.get("synopsis"):
            parts.append(f"本章摘要：{self.chapter['synopsis']}")
        for scene in self.chapter.get("scenes") or []:
            if scene.get("summary"):
                parts.append(f"场景 {scene.get('index', 0) + 1}：{scene['summary']}")
        return "\n".join(parts)

    def recent_text(self, recent_scenes: int = 2) -> str:
        """取最近 N 个场景的已确认原文。

        参数:
            recent_scenes: 取最近几个场景。

        返回:
            原文文本（可能被预算裁剪）。
        """
        scenes = (self.chapter.get("scenes") or [])[-recent_scenes:]
        chunks = []
        for scene in scenes:
            texts = [line.get("text", "") for line in scene.get("lines") or []]
            if texts:
                chunks.append("\n".join(texts))
            elif scene.get("summary"):
                chunks.append(scene["summary"])
        return "\n".join(chunks)

    def build(self, task: str, instruction: str, speaker_ids: list[str] | None = None) -> TextGenRequest:
        """装配一次文本生成请求。

        参数:
            task: 任务类型（outline/chapter/scene/dialogue_split/emotion_tag/image_prompt/freeform）。
            instruction: 本次任务指令（永不裁剪）。
            speaker_ids: 本场出场角色 ID 列表。

        返回:
            TextGenRequest；超预算时先裁最近原文，再压摘要。
        """
        system_parts = [self.story_bible()]
        cards = self.character_cards(speaker_ids)
        if cards:
            system_parts.append("角色设定：\n" + cards)
        system = "\n\n".join(part for part in system_parts if part)

        summary = self.rolling_summary()
        recent = self.recent_text()
        used = len(system) + len(instruction)

        # 预算控制：先裁原文，再裁摘要
        if used + len(recent) > self.budget_tokens:
            keep = max(0, self.budget_tokens - used - len(summary))
            recent = recent[-keep:] if keep else ""
        if used + len(summary) + len(recent) > self.budget_tokens:
            keep = max(0, self.budget_tokens - used - len(recent))
            summary = summary[-keep:] if keep else ""

        user_parts = []
        if summary:
            user_parts.append("前情摘要：\n" + summary)
        if recent:
            user_parts.append("最近原文：\n" + recent)
        user_parts.append("任务指令：\n" + instruction)

        return TextGenRequest(task=task, system=system, messages=[Message(role="user", content="\n\n".join(user_parts))], context_budget_tokens=self.budget_tokens)
