"""标识类型与 ID 生成。

与 `novel_drama/models/ids.py` 的关系：迁移期两者并存，本模块是新骨架的唯一来源；
阶段 6 删除旧实现后，全仓只保留这一份。生成算法（Crockford Base32 ULID）与旧实现逐位一致，
因此同一个工程在新旧代码之间来回读写不会产生 ID 形态差异。

约定：
- 每个实体类型有固定前缀，前缀与类型不符视为数据损坏；
- 前缀只能取 `ID_PREFIXES` 里的值，避免出现 `xx_` 这类无法追溯归属的 ID。
"""

from __future__ import annotations

import secrets
import time
from typing import NewType

#: Crockford Base32（去掉易混淆的 I/L/O/U）
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

#: 实体类型 → ID 前缀。新增实体必须先在这里登记。
ID_PREFIXES: dict[str, str] = {
    "work": "wrk",
    "chapter": "chp",
    "act": "act",
    "beat": "bea",
    "world": "wld",
    "character": "chr",
    "voice": "voc",
    "variant": "vav",
    "narrator_persona": "npr",
    "script": "scr",
    "shot": "sht",
    "line": "lin",
    "sfx": "sfx",
    "image": "img",
    "video": "vid",
    "bgm_plan": "bgp",
    "silence": "sil",
    "candidate": "cnd",
    "block": "blk",
    "continuation": "cnt",
    "thread": "thr",
    "job": "job",
    "timeline": "tml",
    "export": "exp",
    "log": "lg",
}

# 品牌类型：运行时仍是 str，只在类型检查与阅读时区分含义
WorkId = NewType("WorkId", str)
ChapterId = NewType("ChapterId", str)
ActId = NewType("ActId", str)
BeatId = NewType("BeatId", str)
WorldId = NewType("WorldId", str)
CharacterId = NewType("CharacterId", str)
VoiceId = NewType("VoiceId", str)
VariantId = NewType("VariantId", str)
NarratorPersonaId = NewType("NarratorPersonaId", str)
ScriptId = NewType("ScriptId", str)
ShotId = NewType("ShotId", str)
LineId = NewType("LineId", str)
SfxId = NewType("SfxId", str)
ImageId = NewType("ImageId", str)
VideoId = NewType("VideoId", str)
BgmPlanId = NewType("BgmPlanId", str)
SilenceId = NewType("SilenceId", str)
CandidateId = NewType("CandidateId", str)
BlockId = NewType("BlockId", str)
ContinuationId = NewType("ContinuationId", str)
ThreadId = NewType("ThreadId", str)
JobId = NewType("JobId", str)


def _encode(value: int, length: int) -> str:
    """把整数编码为指定长度的 Base32 字符串（高位在前）。

    参数:
        value: 待编码的非负整数。
        length: 输出字符数。

    返回:
        Base32 编码字符串。
    """
    chars = []
    for _ in range(length):
        chars.append(_ALPHABET[value & 31])
        value >>= 5
    return "".join(reversed(chars))


def new_id(prefix: str) -> str:
    """生成 `<前缀>_<ULID>` 形式的实体 ID。

    参数:
        prefix: 实体前缀，取自 `ID_PREFIXES` 的值，如 wrk/chp/chr/lin。

    返回:
        26 位 ULID（毫秒时间戳 10 位 + 80 位随机数 16 位）拼成的 ID，可按时间大致排序。

    异常:
        ValueError: 前缀为空。
    """
    if not prefix:
        raise ValueError("ID 前缀不能为空")
    timestamp_ms = int(time.time() * 1000)
    randomness = secrets.randbits(80)
    return f"{prefix}_{_encode(timestamp_ms, 10)}{_encode(randomness, 16)}"


def new_entity_id(entity: str) -> str:
    """按实体类型生成 ID（前缀从 `ID_PREFIXES` 查表）。

    参数:
        entity: 实体名，如 "chapter"、"line"。

    返回:
        实体 ID。

    异常:
        KeyError: 实体名未登记前缀。
    """
    return new_id(ID_PREFIXES[entity])


def has_prefix(value: str | None, prefix: str) -> bool:
    """判断 ID 是否是「指定前缀 + 下划线」的形态。

    参数:
        value: 待检查的 ID。
        prefix: 期望前缀，如 "chp"。

    返回:
        形态匹配时为 True；空值返回 False。
    """
    text = str(value or "")
    return text.startswith(f"{prefix}_") and len(text) > len(prefix) + 1
