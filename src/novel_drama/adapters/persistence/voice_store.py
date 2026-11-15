"""音色库（voices.json）的查询与解析：角色 → 音色 → 变体 → 参考音频。

只做纯数据操作，不加载模型；试听由 TtsService 完成。
"""

from __future__ import annotations

from pathlib import Path


def find_voice(table: dict, key: str) -> dict | None:
    """按 ID 或名称查找音色。

    参数:
        table: 音色库文档。
        key: 音色 ID（voc_xxx）或名称。

    返回:
        音色 dict；未命中返回 None。
    """
    for voice in table.get("voices") or []:
        if voice.get("id") == key or voice.get("name") == key:
            return voice
    return None


def find_variant(voice: dict, key: str) -> dict | None:
    """按 ID 或名称查找变体。

    参数:
        voice: 音色 dict。
        key: 变体 ID（vav_xxx）或名称。

    返回:
        变体 dict；未命中返回 None。
    """
    for variant in voice.get("variants") or []:
        if variant.get("id") == key or variant.get("name") == key:
            return variant
    return None


def pick_variant(voice: dict, emotion: str | None, require_reference: bool = False) -> dict | None:
    """按情绪挑选变体：精确命中 → default 标签 → 音色默认变体。

    参数:
        voice: 音色 dict。
        emotion: 台词情绪（neutral/joy/...）。
        require_reference: 只接受带参考音频的变体（克隆类 provider 必用）。

    返回:
        变体 dict；音色无可用变体时返回 None。
    """
    def usable(variant: dict) -> bool:
        """变体是否满足状态与参考音频要求。"""
        if variant.get("status") != "ready":
            return False
        if require_reference and not ((variant.get("reference") or {}).get("audio_path")):
            return False
        return True

    variants = [item for item in voice.get("variants") or [] if usable(item)]
    if emotion:
        for variant in variants:
            if emotion in (variant.get("emotion_tags") or []):
                return variant
    for variant in variants:
        if "default" in (variant.get("emotion_tags") or []):
            return variant
    default_id = voice.get("default_variant_id")
    for variant in variants:
        if variant.get("id") == default_id:
            return variant
    return None


def variant_ref(store, variant: dict) -> tuple[str | None, str | None]:
    """把变体里的相对参考音频转成绝对路径与参考文本。

    参数:
        store: ProjectStore（提供工程根目录）。
        variant: 变体 dict。

    返回:
        (绝对音频路径或 None, 参考文本或 None)。
    """
    reference = variant.get("reference") or {}
    audio = reference.get("audio_path")
    if not audio:
        return None, None
    path = Path(audio)
    if not path.is_absolute():
        path = store.root / path
    return str(path), reference.get("text")


def resolve_for_character(store, characters_table: dict, character_id: str, emotion: str | None) -> dict:
    """解析某个角色在某情绪下应使用的音色变体。

    参数:
        store: ProjectStore。
        characters_table: 角色表。
        character_id: 角色 ID。
        emotion: 台词情绪。

    返回:
        {"voice_id", "variant_id", "ref_audio", "ref_text", "source"}；
        无法解析时各字段为 None，由调用方回退到配置默认音色。
    """
    empty = {"voice_id": None, "variant_id": None, "ref_audio": None, "ref_text": None, "source": None}
    character = next((item for item in characters_table.get("characters") or [] if item.get("id") == character_id), None)
    binding = (character or {}).get("voice_binding") or {}
    voice_id = binding.get("voice_id")
    if voice_id:
        voice = find_voice(store.load_voices(), voice_id)
        if voice is not None:
            variant = pick_variant(voice, emotion, require_reference=True)
            if variant is not None:
                ref_audio, ref_text = variant_ref(store, variant)
                if ref_audio:
                    return {
                        "voice_id": voice.get("id"),
                        "variant_id": variant.get("id"),
                        "ref_audio": ref_audio,
                        "ref_text": ref_text,
                        "source": variant.get("source"),
                    }
            # 绑定了音色但没有可用参考音频：交给调用方报 VOICE_MISSING，不静默回退
            return {
                "voice_id": voice.get("id"),
                "variant_id": None,
                "ref_audio": None,
                "ref_text": None,
                "source": "unavailable",
            }
    if binding.get("reference_audio"):
        ref_audio, ref_text = None, None
        path = Path(binding["reference_audio"])
        if not path.is_absolute():
            path = store.root / path
        ref_audio = str(path)
        return {"voice_id": voice_id, "variant_id": None, "ref_audio": ref_audio, "ref_text": ref_text, "source": "binding"}
    return empty


def voice_binding_summary(store, characters_table: dict) -> list[dict]:
    """列出角色与音色绑定/变体数量，供 CLI 展示。

    参数:
        store: ProjectStore。
        characters_table: 角色表。

    返回:
        [{character_id, name, voice_id, voice_name, variants, default_variant}]
    """
    table = store.load_voices()
    rows = []
    for character in characters_table.get("characters") or []:
        binding = character.get("voice_binding") or {}
        voice_id = binding.get("voice_id")
        voice = find_voice(table, voice_id) if voice_id else None
        rows.append(
            {
                "character_id": character.get("id"),
                "name": character.get("name"),
                "voice_id": voice_id,
                "voice_name": (voice or {}).get("name"),
                "variants": len((voice or {}).get("variants") or []),
                "default_variant": (voice or {}).get("default_variant_id"),
            }
        )
    return rows
