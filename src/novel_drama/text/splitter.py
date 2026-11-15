"""规则优先的切分：章节 → 场景 → 台词行。

设计原则：
- 切分必须可复现、可解释，因此默认走规则；
- 规则无法覆盖的歧义段落（无引号对白、多人抢话）留给写作模型，且只替换该段；
- 说话人归属按「段内引语先合并 → 三位置 cue → 轮次先验」判定，无法确定时标记 `chr_unknown`；
- 角色卡外的龙套（司仪、大学生等）不强行归属，回退未知，进入待校对列表。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..models.entities import NARRATOR_ID, UNKNOWN_SPEAKER_ID

# 显式场景分隔符
_SCENE_SEPARATOR = re.compile(r"^\s*(?:-{3,}|\*{3,}|#{3,}|【[^】]{1,20}】)\s*$")

# 场景转场提示词（出现在段首时倾向切场）
_TRANSITION_WORDS = (
    "第二天",
    "次日",
    "与此同时",
    "半小时后",
    "一小时后",
    "数日后",
    "几天后",
    "当晚",
    "深夜",
    "清晨",
    "傍晚",
    "凌晨",
    "回到",
    "门外",
    "屋内",
    "另一个",
)

# 对话引号（中英文、直角引号）
# 不设长度上限：超长独白同样算对白，块级切分交给 TTS 切分逻辑处理；
# 同时要求必须有闭合引号才匹配，避免未闭合的引号把后半章吞成一句对白
_QUOTE_PATTERN = re.compile(r"“([^”]+)”|「([^」]+)」|『([^』]+)』|【([^】]+)】|\"([^\"]+)\"")

# 句末标点与换行，用于切分过长旁白
# 换行也作为硬边界：台词行里带 \n 会导致后续「按原文定位」失败，正文无法同步
_SENTENCE_SPLIT = re.compile(r"(?<=[。！？])|\n+")

# 引号对：外层引号决定对白边界；同类引号允许嵌套，内层不再单独成行
_QUOTE_PAIRS = {"“": "”", "「": "」", "『": "』", "【": "】", '"': '"'}

# 说话动词（长的放前面，避免「说道」只匹配到「说」）
_SPEECH_VERBS = (
    "低声说道",
    "轻声说道",
    "沉声说道",
    "低声说",
    "轻声说",
    "小声说",
    "大声说",
    "冷笑道",
    "笑着说",
    "低声道",
    "沉声道",
    "说道",
    "问道",
    "答道",
    "喊道",
    "叫道",
    "笑道",
    "念道",
    "骂道",
    "应道",
    "追问道",
    "反问道",
    "接着说",
    "继续说",
    "开口",
    "出声",
    "回答",
    "追问",
    "反问",
    "嘟囔",
    "嘀咕",
    "喃喃",
    "补充",
    "提醒",
    "招呼",
    "接话",
    "插话",
    "心道",
    "暗道",
    "说",
    "问",
    "答",
    "喊",
    "叫",
    "嚷",
    "吼",
    "骂",
    "念",
)

# 动词后必须是标点、句末或常见补语，避免把「说明」「问题」「叫她的名字」误判成说话
_VERB_TAIL = r"(?=[。！？，、；：\s“”「」『』]|$|完|着|了|道|出声|一句)"
_VERB_ALT = "|".join(re.escape(verb) for verb in sorted(_SPEECH_VERBS, key=len, reverse=True))
_VERB_RE = re.compile(rf"(?:{_VERB_ALT}){_VERB_TAIL}")
_PLAIN_VERB_RE = re.compile(_VERB_ALT)

# 「A 对 B 说」里的介词：A 才是说话人，B 是听话人
_ADDRESSEE_PREFIX = "对跟向朝冲"

# 动作提示词：称呼紧贴引号时，用动作反推说话人（「周也摸了摸脸。“……”」）
_ACTION_WORDS = (
    "环顾",
    "环视",
    "转身",
    "站起",
    "坐下",
    "点头",
    "摇头",
    "伸手",
    "抬头",
    "低头",
    "皱眉",
    "挑眉",
    "重复",
    "沉默",
    "收住",
    "停住",
    "喘着气",
    "笑了笑",
    "摸了摸",
    "看了一眼",
)
_ACTION_CHARS = "看摸碰走站坐停转伸抬低点摇皱笑收拿推拉捏听盯望叹顿想骂跪趴靠"

# 代词：只处理第三人称，第一人称在第三人称小说里多为叙述，不猜
_PRONOUN_RE = re.compile(r"(?<!其)(他们|她们|他|她)")

# 不算「角色卡外称呼」的词：这些是代词或泛指，交给代词解析
_NON_SPEAKER_TOKENS = {
    "我",
    "你",
    "您",
    "他",
    "她",
    "它",
    "我们",
    "你们",
    "他们",
    "她们",
    "它们",
    "大家",
    "众人",
    "有人",
    "没人",
}

# 常见副词 / 描写词：与动词连写时不是「称呼」，避免「然后说」被当成龙套
_NON_NAME_WORDS = {
    "然后",
    "接着",
    "继续",
    "于是",
    "最后",
    "忽然",
    "突然",
    "急忙",
    "连忙",
    "立刻",
    "马上",
    "随即",
    "随后",
    "终于",
    "这时",
    "此时",
    "同时",
    "之后",
    "另外",
    "声音",
    "心里",
    "心中",
    "嘴上",
    "嘴里",
    "低声",
    "沉声",
    "轻声",
    "小声",
    "大声",
    "冷笑",
    "笑着",
    "点头",
    "摇头",
    "抬头",
    "低头",
    "一边",
    "一面",
    "慢慢",
    "缓缓",
    "轻轻",
    "冷冷",
    "淡淡",
    "静静",
    "默默",
    "低声地",
    "沉声地",
}

# 单场景候选上限：候选过多会显著增加误命中
_MAX_CANDIDATES = 10

# 弱代称：语义宽泛的称呼或非人代词（「女人」「纸人」「它」）。
# 这类词天然歧义大：只在场景内唯一指向某个候选角色、且该角色在场景里有强称呼时采信，
# 置信度压到低档；同时不参与候选集计分，避免「它」把角色带进每个场景。
_WEAK_ALIASES = {
    "女人",
    "男人",
    "老人",
    "姑娘",
    "少女",
    "少年",
    "孩子",
    "青年",
    "纸人",
    "那人",
    "这人",
    "来人",
    "对方",
    "家伙",
    "它",
}

# 弱代称里只能靠说话动词采信的代词（「它拿起来」不一定是傩王）
_WEAK_PRONOUN_ALIASES = {"它"}

# cue 置信度
_CONF_NAME = 0.9
_CONF_PRONOUN = 0.6
_CONF_ACTION = 0.55
_CONF_PARAGRAPH = 0.7
_CONF_ROTATION = 0.45
_CONF_AMBIGUOUS = 0.3
_CONF_WEAK_NAME = 0.4
_CONF_WEAK_ACTION = 0.35
_CONF_WEAK_PROPAGATION = 0.45


def split_scenes(content: str, max_chars: int = 1200) -> list[dict]:
    """把章节正文切成场景草案。

    参数:
        content: 章节正文（纯文本/Markdown）。
        max_chars: 单场景软上限，超过则在此处切分。

    返回:
        [{"title": 场景标题, "content": 场景正文}]，至少返回一项（正文为空时返回空列表）。
    """
    if not content or not content.strip():
        return []

    scenes: list[dict] = []
    buffer: list[str] = []
    buffer_chars = 0

    def flush() -> None:
        """把当前缓冲区落成一个场景。"""
        nonlocal buffer, buffer_chars
        text = "\n".join(buffer).strip()
        if text:
            scenes.append({"title": f"场景 {len(scenes) + 1}", "content": text})
        buffer = []
        buffer_chars = 0

    for raw_paragraph in re.split(r"\n\s*\n", content):
        paragraph = raw_paragraph.strip()
        if not paragraph:
            continue
        if _SCENE_SEPARATOR.match(paragraph):
            flush()
            continue
        starts_transition = paragraph.startswith(_TRANSITION_WORDS)
        if buffer and (starts_transition or buffer_chars + len(paragraph) > max_chars):
            flush()
        buffer.append(paragraph)
        buffer_chars += len(paragraph)

    flush()
    return scenes


@dataclass
class _Span:
    """一个顶层引语片段在场景正文里的位置。"""

    start: int
    end: int
    text: str
    paragraph: int


@dataclass
class _Cue:
    """一条说话人线索。"""

    speaker_id: str
    confidence: float
    kind: str
    distance: int


def _scan_quotes(text: str) -> list[_Span]:
    """扫描顶层引语片段，嵌套引号并入外层，不单独成行。

    参数:
        text: 场景正文。

    返回:
        [_Span]，按出现顺序；未闭合的引号直接跳过。
    """
    spans: list[_Span] = []
    index = 0
    length = len(text)
    while index < length:
        opener = text[index]
        closer = _QUOTE_PAIRS.get(opener)
        if not closer:
            index += 1
            continue
        if opener == closer:
            # 英文双引号：不嵌套，只取最近的一对
            end = text.find(closer, index + 1)
            if end > index + 1 and "\n" not in text[index + 1 : end]:
                spans.append(_Span(index, end + 1, text[index + 1 : end], -1))
                index = end + 1
                continue
            index += 1
            continue
        depth = 1
        cursor = index + 1
        while cursor < length and depth:
            if text[cursor] == opener:
                depth += 1
            elif text[cursor] == closer:
                depth -= 1
                if depth == 0:
                    break
            cursor += 1
        if depth == 0 and cursor > index + 1:
            spans.append(_Span(index, cursor + 1, text[index + 1 : cursor], -1))
            index = cursor + 1
            continue
        index += 1
    return spans


def _paragraph_bounds(text: str) -> list[tuple[int, int]]:
    """按单个换行切出段落区间。

    `split_scenes` 用 `\\n` 合并原文段落，因此场景内的一个自然段就是一行。

    参数:
        text: 场景正文。

    返回:
        [(段落起始偏移, 段落结束偏移)]，与 `text.split("\\n")` 一一对应。
    """
    bounds: list[tuple[int, int]] = []
    start = 0
    for line in text.split("\n"):
        bounds.append((start, start + len(line)))
        start += len(line) + 1
    return bounds


def _paragraph_of(bounds: list[tuple[int, int]], position: int) -> int:
    """根据字符偏移定位所在段落序号。

    参数:
        bounds: `_paragraph_bounds` 的结果。
        position: 引号起始偏移。

    返回:
        段落下标（越界时返回最后一段）。
    """
    for index, (start, end) in enumerate(bounds):
        if position <= end:
            return index
        if position < start:
            return max(0, index - 1)
    return max(0, len(bounds) - 1)


def _alias_words(character: dict, include_weak: bool = True) -> list[str]:
    """取角色的名字与别名，可选是否包含弱代称。

    参数:
        character: 角色 dict。
        include_weak: 为 False 时剔除「女人」「纸人」「它」这类宽泛代称。

    返回:
        非空称呼列表。
    """
    names = [character.get("name") or ""] + list(character.get("aliases") or [])
    return [name for name in names if name and (include_weak or name not in _WEAK_ALIASES)]


def _alias_index(characters: list[dict]) -> list[tuple[str, str]]:
    """构建「称呼 → 角色 ID」索引，按名称长度降序以优先匹配长名。

    弱代称不进这个索引：它们只在 `_weak_alias_map` 里按场景唯一性单独放行。

    参数:
        characters: 角色表。

    返回:
        [(称呼, 角色 ID)]，排除旁白与弱代称。
    """
    pairs: list[tuple[str, str]] = []
    for character in characters:
        if character.get("role") == "narrator" or character.get("id") == NARRATOR_ID:
            continue
        for name in _alias_words(character, include_weak=False):
            pairs.append((name, character["id"]))
    return sorted(pairs, key=lambda item: len(item[0]), reverse=True)


def _weak_alias_map(candidates: list[dict]) -> dict[str, str]:
    """找出本场景可采信的弱代称（唯一指向一个候选角色）。

    候选集本身只按强称呼统计，因此能进入这里，说明该角色在场景里另有明确出场；
    若同一个代称同时属于多个候选角色，说明指代不唯一，直接放弃。

    参数:
        candidates: 本场景候选角色。

    返回:
        {代称: 角色 ID}，只含唯一映射。
    """
    owners: dict[str, set[str]] = {}
    for character in candidates:
        for name in _alias_words(character):
            if name in _WEAK_ALIASES:
                owners.setdefault(name, set()).add(character["id"])
    return {name: next(iter(ids)) for name, ids in owners.items() if len(ids) == 1}


def _scene_candidates(characters: list[dict], scene_text: str) -> list[dict]:
    """挑出本场景实际出场的角色（候选集）。

    角色卡里没在本场景出现过的角色不参与归属，避免全角色表带来的误命中；
    按别名出现频次排序，最多保留 10 个。弱代称不计分，避免「它」把角色带进每个场景。

    参数:
        characters: 完整角色表。
        scene_text: 场景正文。

    返回:
        角色 dict 列表，按出场强度降序。
    """
    scored: list[tuple[int, dict]] = []
    for character in characters:
        if character.get("role") == "narrator" or character.get("id") == NARRATOR_ID:
            continue
        score = 0
        for name in _alias_words(character, include_weak=False):
            score += scene_text.count(name) * max(1, len(name))
        if score:
            scored.append((score, character))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [character for _, character in scored[:_MAX_CANDIDATES]]


def _gender_key(character: dict) -> str:
    """把角色性别归一成 male / female / 空串。

    参数:
        character: 角色 dict。

    返回:
        "male" / "female" / ""。
    """
    value = str(character.get("gender") or "").strip().lower()
    if value in ("male", "m", "男", "男性"):
        return "male"
    if value in ("female", "f", "女", "女性"):
        return "female"
    return ""


def _contains_alias(text: str, aliases: list[tuple[str, str]]) -> bool:
    """判断片段里是否还出现了别的称呼。

    参数:
        text: 待检查片段。
        aliases: 称呼索引。

    返回:
        出现任一称呼返回 True。
    """
    return any(name and name in text for name, _ in aliases)


def _search_verb(text: str, aliases: list[tuple[str, str]]) -> re.Match | None:
    """在片段里找说话动词。

    常规写法要求动词后是标点或常见补语；「她问周也」「A 对 B 说」这类带角色宾语的
    写法单独放行，避免把说话人误判成被问的人。

    参数:
        text: 动词所在片段。
        aliases: 本场景称呼索引。

    返回:
        动词 match；未找到返回 None。
    """
    for candidate in _PLAIN_VERB_RE.finditer(text):
        if _VERB_RE.match(text, candidate.start()):
            return candidate
        tail = text[candidate.end() : candidate.end() + 4]
        if any(tail.startswith(name) for name, _ in aliases):
            return candidate
    return None


def _name_cues(
    segment: str, aliases: list[tuple[str, str]], weak_aliases: list[tuple[str, str]] = ()
) -> list[tuple[int, int, str, bool]]:
    """找出片段里所有「称呼 + 说话动词」的候选。

    参数:
        segment: 引号前或引号后的叙述片段。
        aliases: 强称呼索引。
        weak_aliases: 本场景允许采信的弱代称索引。

    返回:
        [(动词结束位置, 称呼起始位置, 角色 ID, 是否弱代称)]，位置相对片段。
    """
    found: list[tuple[int, int, str, bool]] = []
    for group, is_weak in ((aliases, False), (weak_aliases, True)):
        for name, character_id in group:
            start = segment.find(name)
            while start >= 0:
                end = start + len(name)
                # 「对 / 跟 / 向 + 名字」是听话人，不是说话人
                if start and segment[start - 1] in _ADDRESSEE_PREFIX:
                    start = segment.find(name, end)
                    continue
                tail = segment[end : end + 18]
                if tail.startswith("的声音"):
                    found.append((end + 3, start, character_id, is_weak))
                else:
                    match = _search_verb(tail, aliases)
                    if match:
                        gap = tail[: match.start()]
                        # 中间不能有句末标点；夹带别的称呼时，只有「A 对 B 说」这种介词结构才算 A
                        if not re.search(r"[。！？\n]", gap):
                            if not _contains_alias(gap, aliases) or (gap and gap[0] in _ADDRESSEE_PREFIX):
                                found.append((end + match.end(), start, character_id, is_weak))
                start = segment.find(name, end)
    return found


def _pronoun_cues(segment: str, aliases: list[tuple[str, str]]) -> list[tuple[int, int, str]]:
    """找出片段里所有「代词 + 说话动词」的候选。

    参数:
        segment: 叙述片段。
        aliases: 称呼索引（用于识别「她问周也」里的宾语）。

    返回:
        [(动词结束位置, 代词起始位置, 代词)]。
    """
    found: list[tuple[int, int, str]] = []
    for match in _PRONOUN_RE.finditer(segment):
        tail = segment[match.end() : match.end() + 14]
        verb = _search_verb(tail, aliases)
        if verb and not re.search(r"[。！？\n]", tail[: verb.start()]):
            # 「她站在……司仪说」：真正的说话人贴在动词前，代词只是叙述主语
            if _offscreen_suffix(tail[: verb.end()]):
                continue
            found.append((match.end() + verb.end(), match.start(), match.group(1)))
    return found


def _offscreen_prefix(segment: str) -> bool:
    """判断片段开头是否是「角色卡外的称呼 + 说话动词」。

    参数:
        segment: 引号后的叙述片段。

    返回:
        是角色卡外提示返回 True。
    """
    text = segment.lstrip(" \t，。；：、")
    for length in range(2, 7):
        token = text[:length]
        if len(token) < length or not re.fullmatch(r"[\u4e00-\u9fff]+", token):
            return False
        if token in _NON_SPEAKER_TOKENS or token in _NON_NAME_WORDS:
            return False
        if _VERB_RE.match(text, length):
            return True
    return False


def _offscreen_suffix(segment: str) -> bool:
    """判断片段结尾是否是「角色卡外的称呼 + 说话动词」。

    参数:
        segment: 引号前的叙述片段。

    返回:
        是角色卡外提示返回 True。
    """
    text = segment.rstrip(" \t，。；：、")
    for match in _VERB_RE.finditer(text):
        if match.end() != len(text):
            continue
        end = match.start()
        # 动词前允许 2–6 个汉字作为称呼（司仪 / 中年女人 / 无脸面具）；单字多为「然后说」这类副词
        for length in range(2, min(6, end) + 1):
            token = text[end - length : end]
            if token in _NON_SPEAKER_TOKENS or token in _NON_NAME_WORDS:
                break
            if re.fullmatch(r"[\u4e00-\u9fff]+", token):
                return True
    return False


def _has_action(text: str) -> bool:
    """判断片段里是否出现动作提示词。

    参数:
        text: 片段。

    返回:
        出现动作词返回 True。
    """
    if any(word in text for word in _ACTION_WORDS):
        return True
    return any(char in _ACTION_CHARS for char in text)


def _last_sentence(segment: str) -> str:
    """取片段里最后一个句子，用于引号前的「称呼 + 动作」提示。

    参数:
        segment: 引号前的叙述片段。

    返回:
        去掉标点后的最后一句。
    """
    parts = [part for part in re.split(r"[。！？\n]", segment) if part.strip()]
    return parts[-1].lstrip(" \t，、；：、") if parts else ""


def _last_clause(segment: str) -> str:
    """取片段里最后一个分句，用于引号前的紧邻动作提示。

    参数:
        segment: 引号前的叙述片段。

    返回:
        去掉标点后的最后一个分句。
    """
    parts = [part for part in re.split(r"[。！？\n，；：]", segment) if part.strip()]
    return parts[-1].lstrip(" \t、") if parts else ""


def _action_alias(
    segment: str,
    aliases: list[tuple[str, str]],
    weak_aliases: list[tuple[str, str]] = (),
    side: str = "post",
) -> tuple[str, int, bool] | None:
    """识别紧贴引号「称呼 + 动作」的提示。

    强称呼沿用宽松窗口；弱代称必须紧贴引号（引号后开头或引号前最后分句），
    且「它」这类代词只允许通过说话动词采信，避免「它拿起来」被当成傩王。

    参数:
        segment: 引号前或引号后紧邻的片段。
        aliases: 强称呼索引。
        weak_aliases: 本场景允许采信的弱代称索引。
        side: "pre" 表示引号前，其他值按引号后处理。

    返回:
        (角色 ID, 距离, 是否弱代称)；未命中返回 None。
    """
    text = segment.lstrip(" \t，。；：、")
    for name, character_id in aliases:
        position = text.find(name)
        while position >= 0:
            if _has_action(text[position + len(name) : position + len(name) + 12]):
                return character_id, 0, False
            position = text.find(name, position + len(name))
    for name, character_id in weak_aliases:
        if name in _WEAK_PRONOUN_ALIASES:
            continue
        if side == "pre":
            scope = _last_clause(segment)
        else:
            scope = text
        if not scope.startswith(name):
            continue
        if _has_action(scope[len(name) : len(name) + 8]):
            return character_id, 0, True
    return None


def _action_pronoun(segment: str) -> str | None:
    """识别紧贴引号「代词 + 动作」的提示。

    参数:
        segment: 引号前或引号后紧邻的片段。

    返回:
        代词（他/她/他们/她们）；未命中返回 None。
    """
    text = segment.lstrip(" \t，。；：、")
    match = _PRONOUN_RE.match(text)
    if match and _has_action(text[match.end() : match.end() + 12]):
        return match.group(1)
    return None


def _resolve_pronoun(pronoun: str, candidates: list[dict], paragraph: str, last_speaker: str | None) -> tuple[str, float] | None:
    """把第三人称代词解析到角色。

    参数:
        pronoun: 他 / 她 / 他们 / 她们。
        candidates: 本场景候选角色。
        paragraph: 当前段落正文（用于按提及次数兜底）。
        last_speaker: 上一位说话人。

    返回:
        (角色 ID, 置信度)；无法解析返回 None。
    """
    if pronoun in ("他们", "她们"):
        gender = "male" if pronoun == "他们" else "female"
    else:
        gender = "male" if pronoun == "他" else "female"
    gendered = [character for character in candidates if _gender_key(character) == gender]
    if not gendered:
        # 角色卡完全没有性别信息时才退化为全候选；有性别信息但对象不符，说明指代不明确
        if any(_gender_key(character) for character in candidates):
            return None
        gendered = candidates
    if not gendered:
        return None
    if len(gendered) == 1:
        return gendered[0]["id"], _CONF_PRONOUN
    if last_speaker and any(character["id"] == last_speaker for character in gendered):
        return last_speaker, _CONF_PRONOUN - 0.05
    # 多候选：选本段被提及次数最多的角色，置信度降到低档
    def mention_count(character: dict) -> int:
        """统计角色本段被提及次数（弱代称不计，避免「它」抬高非人角色）。"""
        return sum(paragraph.count(name) for name in _alias_words(character, include_weak=False))

    ranked = sorted(gendered, key=mention_count, reverse=True)
    return ranked[0]["id"], _CONF_AMBIGUOUS


def _weak_nickname_cue_seen(paragraph: str, weak_aliases: list[tuple[str, str]]) -> bool:
    """判断本段是否有「代称 + 说话/动作」的明确线索。

    只认紧贴动词或动作的写法（「女人站起身」「纸人说」），普通叙述里的
    「纸人肚子里」「跟纸人一样」不算，避免误抑制代词解析。

    参数:
        paragraph: 当前段落正文。
        weak_aliases: 本场景允许采信的弱代称索引。

    返回:
        存在代称线索返回 True。
    """
    for name, _character_id in weak_aliases:
        if name in _WEAK_PRONOUN_ALIASES:
            continue
        position = paragraph.find(name)
        while position >= 0:
            tail = paragraph[position + len(name) : position + len(name) + 12]
            if _search_verb(tail, []) or _has_action(tail):
                return True
            position = paragraph.find(name, position + len(name))
    return False


def _prefer_cue(current: _Cue | None, candidate: _Cue) -> _Cue:
    """在两条线索中择优：距离近优先，同距离按类型优先级。"""
    if current is None:
        return candidate
    rank = {"name": 3, "nickname": 2, "pronoun": 2, "action": 1}
    if candidate.distance < current.distance:
        return candidate
    if candidate.distance == current.distance and rank.get(candidate.kind, 0) > rank.get(current.kind, 0):
        return candidate
    return current


def _cue_for_span(
    pre: str,
    post: str,
    aliases: list[tuple[str, str]],
    weak_aliases: list[tuple[str, str]],
    candidates: list[dict],
    paragraph: str,
    last_speaker: str | None,
) -> tuple[_Cue | None, bool]:
    """为单个引语片段找说话人 cue。

    三位置：引号前（X 说：「……」）、引号后（「……」X 说）、引号中（「……」X 说，「……」）。
    引号中的 cue 会同时落在前一片段的后面与后一片段的后面，因此前后两侧都查即可覆盖。

    参数:
        pre: 引号前的叙述片段（本段内、距引号 30 字内）。
        post: 引号后的叙述片段（本段内、距引号 30 字内）。
        aliases: 本场景强称呼索引。
        weak_aliases: 本场景允许采信的弱代称索引（置信度压到低档）。
        candidates: 本场景候选角色。
        paragraph: 当前段落正文。
        last_speaker: 上一位说话人。

    返回:
        (线索, 是否命中角色卡外龙套)；无线索返回 (None, False)。
    """
    best: _Cue | None = None
    name_hits: list[tuple[int, str]] = []

    for end, start, character_id, is_weak in _name_cues(pre, aliases, weak_aliases):
        distance = len(pre) - end
        if not is_weak:
            name_hits.append((distance, character_id))
        best = _prefer_cue(
            best,
            _Cue(
                character_id,
                _CONF_WEAK_NAME if is_weak else _CONF_NAME,
                "nickname" if is_weak else "name",
                distance,
            ),
        )
    for end, start, character_id, is_weak in _name_cues(post, aliases, weak_aliases):
        distance = start
        if not is_weak:
            name_hits.append((distance, character_id))
        best = _prefer_cue(
            best,
            _Cue(
                character_id,
                _CONF_WEAK_NAME if is_weak else _CONF_NAME,
                "nickname" if is_weak else "name",
                distance,
            ),
        )

    pre_pronouns = _pronoun_cues(pre, aliases)
    post_pronouns = _pronoun_cues(post, aliases)
    for end, _start, pronoun in pre_pronouns:
        resolved = _resolve_pronoun(pronoun, candidates, paragraph, last_speaker)
        if resolved:
            best = _prefer_cue(best, _Cue(resolved[0], resolved[1], "pronoun", max(0, len(pre) - end)))
            break
    for end, start, pronoun in post_pronouns:
        resolved = _resolve_pronoun(pronoun, candidates, paragraph, last_speaker)
        if resolved:
            best = _prefer_cue(best, _Cue(resolved[0], resolved[1], "pronoun", max(0, start)))
            break
    # 出现代词但无法解析时，不再用动作线索硬猜（「他说」可能指场景外的人）
    pronoun_seen = bool(pre_pronouns or post_pronouns)

    # 角色卡外的龙套贴在引号两侧时，不再用动作/代词猜卡内角色
    offscreen = _offscreen_prefix(post) or _offscreen_suffix(pre)
    # 本段出现「女人站起身」「纸人说」这类明确代称线索时，不再用泛化的「她 + 动作」抢走归属；
    # 「它」本身是代词，不参与这个抑制，避免叙述里大量「它」拖累她/他的解析
    weak_seen = _weak_nickname_cue_seen(paragraph, weak_aliases)
    if best is None and not offscreen and not pronoun_seen:
        action = _action_alias(pre, aliases, weak_aliases, "pre") or _action_alias(
            post[:14], aliases, weak_aliases, "post"
        )
        if action:
            best = _Cue(
                action[0],
                _CONF_WEAK_ACTION if action[2] else _CONF_ACTION,
                "nickname" if action[2] else "action",
                action[1],
            )
        elif not weak_seen:
            pronoun = _action_pronoun(post) or _action_pronoun(_last_sentence(pre))
            if pronoun:
                resolved = _resolve_pronoun(pronoun, candidates, paragraph, last_speaker)
                if resolved:
                    best = _Cue(resolved[0], _CONF_ACTION, "action", 0)

    # 角色卡外的龙套：「司仪说」「大学生喊道」不在候选集里，回退未知而不是硬塞给卡内角色
    if best is None and offscreen:
        return _Cue(UNKNOWN_SPEAKER_ID, 0.0, "offscreen", 0), True
    if best is None:
        return None, False

    # 同距离出现多个不同角色 → 多候选，置信度降到低档
    if best.kind == "name" and name_hits:
        near = {character_id for distance, character_id in name_hits if distance <= best.distance + 3}
        if len(near) > 1:
            best.confidence = _CONF_AMBIGUOUS
    return best, False


def _emphasis_offsets(spans: list[_Span], segments: list[tuple[str, str]]) -> set[int]:
    """找出叙述里被引号截断的强调短语。

    典型形态：`带“好”，`、`刻着“柳庄”，`、`司仪说“一拜天地”，九斤没拜`。
    这些引号不是独立台词，不应被段内统一说话人规则带走。

    参数:
        spans: 本段引语片段。
        segments: 与片段一一对应的 (引号前, 引号后) 片段。

    返回:
        需要排除的片段下标集合。
    """
    result: set[int] = set()
    for offset, span in enumerate(spans):
        pre, post = segments[offset]
        if not pre.strip() or not post.strip() or len(span.text.strip()) > 12:
            continue
        head = post.lstrip()
        if head[:1] in "，、；。":
            # 引号后紧跟逗号继续叙述，且附近没有说话动词 → 强调短语
            if not _VERB_RE.search(head[:16]):
                result.add(offset)
        elif re.search(r"[带写刻着的是叫称]$", pre.rstrip()):
            result.add(offset)
    return result


def _is_bare_dialogue(paragraph: str, spans: list[_Span], para_start: int = 0) -> bool:
    """判断段落是否「几乎只有对白」，用于决定是否启用轮次先验。

    参数:
        paragraph: 段落正文。
        spans: 本段内的引语片段（偏移是场景绝对位置）。
        para_start: 段落起始偏移，用于把片段位置换算到段内。

    返回:
        去掉引语后只剩标点或极短提示语时返回 True。
    """
    parts: list[str] = []
    cursor = 0
    for span in spans:
        parts.append(paragraph[cursor : max(0, span.start - para_start)])
        cursor = max(0, span.end - para_start)
    parts.append(paragraph[cursor:])
    residual = "".join(parts).strip(" \t，。！？…—、；：“”「」『』\n")
    return len(residual) <= 12


def _rotation_choice(state: dict, candidate_ids: list[str]) -> str | None:
    """按轮次先验选下一位说话人。

    连续无标记台词在最近两个说话人之间交替；场景候选恰好两人时，
    即使只记录到一个说话人也允许轮到另一人。

    参数:
        state: {"last": 上一位说话人 ID 或 None, "last_two": 最近两位说话人}。
        candidate_ids: 本场景候选角色 ID。

    返回:
        角色 ID；无法判断返回 None。
    """
    last = state.get("last")
    if not last:
        return None
    for speaker_id in state.get("last_two") or []:
        if speaker_id != last:
            return speaker_id
    if len(candidate_ids) == 2 and last in candidate_ids:
        return next(speaker_id for speaker_id in candidate_ids if speaker_id != last)
    return None


def _update_state(state: dict, cue: _Cue | None) -> None:
    """用一条归属结果更新轮次状态。

    角色卡外龙套或无法归属时打断对话链；第三人被点名时重置轮次基准。

    参数:
        state: 轮次状态（原地修改）。
        cue: 本条归属线索。
    """
    if cue is None or cue.kind == "offscreen" or cue.speaker_id == UNKNOWN_SPEAKER_ID:
        state["last"] = None
        state["last_two"] = []
        return
    speaker_id = cue.speaker_id
    last_two = [item for item in state.get("last_two") or [] if item != speaker_id]
    if speaker_id not in (state.get("last_two") or []):
        if len(last_two) >= 2:
            # 第三人被点名 → 重置为新的对话对
            last_two = []
    state["last_two"] = [speaker_id] + last_two
    state["last_two"] = state["last_two"][:2]
    state["last"] = speaker_id


def _attribute_scene(scene_text: str, characters: list[dict]) -> tuple[list[_Span], list[_Cue | None], set[int]]:
    """给场景内所有引语片段分配说话人。

    参数:
        scene_text: 场景正文。
        characters: 完整角色表。

    返回:
        (引语片段列表, 与片段一一对应的线索列表, 需要折回旁白的强调引语下标集合)。
    """
    spans = _scan_quotes(scene_text)
    if not spans:
        return spans, [], set()

    bounds = _paragraph_bounds(scene_text)
    candidates = _scene_candidates(characters, scene_text)
    aliases = _alias_index(candidates)
    weak_aliases = list(_weak_alias_map(candidates).items())
    candidate_ids = [character["id"] for character in candidates]

    for span in spans:
        span.paragraph = _paragraph_of(bounds, span.start)

    # 按段落分组，保持阅读顺序
    grouped: dict[int, list[int]] = {}
    for index, span in enumerate(spans):
        grouped.setdefault(span.paragraph, []).append(index)

    cues: list[_Cue | None] = [None] * len(spans)
    emphasis_all: set[int] = set()
    state: dict = {"last": None, "last_two": []}
    previous_para = ""

    for paragraph_index in range(len(bounds)):
        para_start, para_end = bounds[paragraph_index]
        paragraph = scene_text[para_start:para_end]
        index_list = grouped.get(paragraph_index)
        if not index_list:
            # 纯叙述段落打断对话链，轮次先验重新开始；但保留正文供下一段首句回看
            state["last"] = None
            state["last_two"] = []
            previous_para = paragraph
            continue
        para_spans = [spans[index] for index in index_list]

        blocked = False
        local: list[_Cue | None] = []
        segments: list[tuple[str, str]] = []
        for offset, span in enumerate(para_spans):
            prev_end = para_spans[offset - 1].end if offset else para_start
            next_start = para_spans[offset + 1].start if offset + 1 < len(para_spans) else para_end
            pre = scene_text[max(prev_end, span.start - 30) : span.start]
            post = scene_text[span.end : min(next_start, span.end + 30)]
            segments.append((pre, post))
            cue, offscreen = _cue_for_span(
                pre, post, aliases, weak_aliases, candidates, paragraph, state["last"]
            )
            blocked = blocked or offscreen
            local.append(cue)

        # 叙述里被引号截断的短语（「带“好”」「刻着“柳庄”，」）不是台词，不参与归属
        emphasis = _emphasis_offsets(para_spans, segments)
        for offset in emphasis:
            local[offset] = None
            emphasis_all.add(index_list[offset])

        # 段内首句无 cue 时，回看上一段结尾（「……周也开口。」单独成段，对白在下一段）。
        # 上一段本身是对白段时不回看，交给轮次先验，避免把上一句说话人重复给下一句。
        # 强调引语已定为非台词，回看要跳过它们，取本段第一条真正的对白。
        first_dialogue = next((offset for offset in range(len(local)) if offset not in emphasis), None)
        if (
            first_dialogue is not None
            and local[first_dialogue] is None
            and _is_bare_dialogue(paragraph, para_spans, para_start)
            and not _scan_quotes(previous_para)
        ):
            tail_cue = _previous_tail_cue(previous_para, aliases, weak_aliases, candidates, state["last"])
            if tail_cue is not None:
                local[first_dialogue] = tail_cue
                if tail_cue.kind == "offscreen":
                    blocked = True

        # 段内只有一个确定说话人时，把同段其余无标记引语归给同一人；
        # 如果这个说话人仅由弱代称推出，传播置信度也压到低档，方便人工复核
        definite_cues = [cue for cue in local if cue and cue.speaker_id != UNKNOWN_SPEAKER_ID]
        definite = {cue.speaker_id for cue in definite_cues}
        if not blocked and len(definite) == 1:
            speaker_id = next(iter(definite))
            weak_only = all(cue.kind == "nickname" for cue in definite_cues)
            confidence = _CONF_WEAK_PROPAGATION if weak_only else _CONF_PARAGRAPH
            for offset, cue in enumerate(local):
                if cue is None and offset not in emphasis:
                    local[offset] = _Cue(speaker_id, confidence, "paragraph", 0)

        # 轮次先验只用在「几乎只有对白」的段落，避免把叙述里的强调引语算成台词
        if not blocked and _is_bare_dialogue(paragraph, para_spans, para_start):
            for offset, cue in enumerate(local):
                if cue is not None or offset in emphasis:
                    continue
                speaker_id = _rotation_choice(state, candidate_ids)
                if speaker_id:
                    local[offset] = _Cue(speaker_id, _CONF_ROTATION, "rotation", 0)
                    _update_state(state, local[offset])

        for offset, cue in enumerate(local):
            cues[index_list[offset]] = cue
            if cue is not None and cue.kind not in ("paragraph", "rotation"):
                _update_state(state, cue)

        previous_para = paragraph

    return spans, cues, emphasis_all


def _previous_tail_cue(
    previous: str,
    aliases: list[tuple[str, str]],
    weak_aliases: list[tuple[str, str]],
    candidates: list[dict],
    last_speaker: str | None,
) -> _Cue | None:
    """从上一段结尾的叙述里找「引出下一句对白」的线索。

    真实写法常见「……周也开口。」「……他抬起头。」单独成段，对白在下一段，
    因此段内 cue 找不到时回看上一段末尾：先找句末说话动词，再退化为「最后一句只点名了一个角色」。

    参数:
        previous: 上一段正文。
        aliases: 本场景强称呼索引。
        weak_aliases: 本场景允许采信的弱代称索引。
        candidates: 本场景候选角色。
        last_speaker: 上一位说话人。

    返回:
        线索；无把握返回 None。
    """
    text = previous.strip()
    if not text:
        return None
    tail = text[-24:]
    best: _Cue | None = None
    for end, _start, character_id, is_weak in _name_cues(tail, aliases, weak_aliases):
        if len(tail) - end <= 3:
            best = _Cue(
                character_id,
                _CONF_WEAK_NAME if is_weak else 0.6,
                "nickname" if is_weak else "name",
                0,
            )
    if best is None:
        for end, _start, pronoun in _pronoun_cues(tail, aliases):
            if len(tail) - end <= 3:
                resolved = _resolve_pronoun(pronoun, candidates, previous, last_speaker)
                if resolved:
                    best = _Cue(resolved[0], 0.5, "pronoun", 0)
    if best is not None:
        return best
    # 上一段结尾在说角色卡外的龙套 → 下一句不要硬塞给卡内角色
    if _offscreen_suffix(tail):
        return _Cue(UNKNOWN_SPEAKER_ID, 0.0, "offscreen", 0)

    # 退化为「最后一句只点名了一个角色」：置信度低，仅用于明显的一对一叙述。
    # 取最后一个句子里最先出现的角色（中文叙述的主语通常在前）。
    sentence = re.split(r"[。！？\n]", text)[-1]
    first: tuple[int, str] | None = None
    for character in candidates:
        names = _alias_words(character, include_weak=False)
        positions = [sentence.find(name) for name in names if name and name in sentence]
        if not positions:
            continue
        position = min(positions)
        if first is None or position < first[0]:
            first = (position, character["id"])
    for pronoun in _PRONOUN_RE.findall(sentence):
        position = sentence.find(pronoun)
        resolved = _resolve_pronoun(pronoun, candidates, sentence, last_speaker)
        if resolved and (first is None or position < first[0]):
            first = (position, resolved[0])
    if first is not None:
        return _Cue(first[1], 0.35, "name", 0)
    return None


def _push_narration(lines: list[dict], text: str) -> None:
    """把一段叙述文本按句末标点切分后追加为旁白行。

    参数:
        lines: 输出列表（原地追加）。
        text: 叙述文本。
    """
    cleaned = text.strip()
    if len(cleaned) < 2:
        return
    chunks = [chunk.strip() for chunk in _SENTENCE_SPLIT.split(cleaned) if chunk.strip()]
    for chunk in chunks or [cleaned]:
        lines.append({"kind": "narration", "text": chunk, "speaker_id": NARRATOR_ID, "confidence": 1.0})


def split_lines(scene_text: str, characters: list[dict], folded_sink: set[str] | None = None) -> list[dict]:
    """把场景正文切成台词行（对白/旁白/内心独白）。

    参数:
        scene_text: 场景正文。
        characters: 角色表（用于说话人识别）。
        folded_sink: 可选集合；传入时把「折回旁白的强调引语」原文收集进去，供重切保护使用。

    返回:
        [{"kind", "text", "speaker_id", "confidence"}]，保持原文顺序。

    说明:
        引号内为对白；引号外叙述为旁白；引号前出现「心想/暗道/心道」或紧邻「（」时视为内心独白。
        说话人优先取本场景候选角色：先看引号前/后/中的明确 cue，再看段内统一说话人，
        最后才用轮次先验；角色卡外的龙套保持 `chr_unknown` 交由人工校对。
        叙述里的强调引语（「带“好”，」）不单独成行，原引号片段折回旁白，保证整段仍是自然叙述。
    """
    if not scene_text or not scene_text.strip():
        return []

    spans, cues, emphasis = _attribute_scene(scene_text, characters or [])
    lines: list[dict] = []
    cursor = 0
    pending = ""

    for index, span in enumerate(spans):
        prefix = scene_text[cursor:span.start]
        if index in emphasis:
            # 强调引语不是台词：保留原引号片段，和前后叙述一起走旁白切分
            pending += prefix + scene_text[span.start : span.end]
            if folded_sink is not None:
                folded_sink.add(span.text.strip())
            cursor = span.end
            continue

        _push_narration(lines, pending + prefix)
        pending = ""

        cue = cues[index] if index < len(cues) else None
        speaker_id = cue.speaker_id if cue else UNKNOWN_SPEAKER_ID
        confidence = cue.confidence if cue else 0.0
        before = prefix.rstrip().rstrip("：:")
        is_inner = before.endswith("心想") or before.endswith("暗道") or before.endswith("心道") or before.endswith("（") or before.endswith("(")
        if is_inner and speaker_id == UNKNOWN_SPEAKER_ID:
            # 内心独白通常紧贴最近出现的角色，仍需人工确认，置信度调低
            confidence = 0.3
        lines.append(
            {
                "kind": "inner_monologue" if is_inner else "dialogue",
                "text": span.text.strip(),
                "speaker_id": speaker_id,
                "confidence": confidence,
            }
        )
        cursor = span.end

    _push_narration(lines, pending + scene_text[cursor:])
    return lines
