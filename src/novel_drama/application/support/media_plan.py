"""媒体生成动作登记表。

声音、图片、视频、音效、配乐的所有生成动作都先在这里登记一份规格：
通道、任务类型、标题、阶段、是否可取消、是否占用 GPU、需要哪些参数。
前端据此渲染统一确认弹窗，后端据此创建统一任务记录，避免页面各写一套
动作名与展示文案。
"""

from __future__ import annotations

from dataclasses import dataclass

from ...models.enums import UiJobKind

#: 通道中文名（前端按通道筛选与分组展示）
CHANNEL_LABELS: dict[str, str] = {
    "audio": "声音",
    "image": "图片",
    "video": "视频",
}


@dataclass(frozen=True)
class MediaActionSpec:
    """一种媒体生成动作的静态规格。

    参数:
        action: 动作键（前端传入，稳定不变）。
        channel: 通道，取值 audio/image/video。
        kind: 落盘到 `UiJobKind` 的任务类型值。
        title: 中文标题（进度卡与任务页展示）。
        stages: 阶段列表（按执行顺序的中文阶段名）。
        cancelable: 是否允许取消。
        gpu: 是否占用 GPU（需要排队）。
        usage: 需要哪些参数（中文说明，供确认弹窗与报错使用）。
    """

    action: str
    channel: str
    kind: str
    title: str
    stages: tuple[str, ...]
    cancelable: bool
    gpu: bool
    usage: str


def _spec(
    action: str,
    *,
    channel: str,
    kind: str,
    title: str,
    stages: tuple[str, ...],
    cancelable: bool,
    gpu: bool,
    usage: str,
) -> MediaActionSpec:
    """构造一条动作规格（内部登记用）。

    参数:
        action: 动作键。
        channel: 通道。
        kind: `UiJobKind` 落盘值。
        title: 中文标题。
        stages: 阶段列表。
        cancelable: 是否允许取消。
        gpu: 是否占用 GPU。
        usage: 需要的参数说明。

    返回:
        `MediaActionSpec`。
    """
    return MediaActionSpec(
        action=action,
        channel=channel,
        kind=kind,
        title=title,
        stages=stages,
        cancelable=cancelable,
        gpu=gpu,
        usage=usage,
    )


#: 全部媒体动作：键为 action，值为规格
ACTIONS: dict[str, MediaActionSpec] = {
    spec.action: spec
    for spec in (
        _spec(
            "tts.line",
            channel="audio",
            kind=UiJobKind.TTS_LINE.value,
            title="单行重录",
            stages=("载入", "合成语音", "写入产物"),
            cancelable=True,
            gpu=False,
            usage="path、chapter、line",
        ),
        _spec(
            "tts.chapter",
            channel="audio",
            kind=UiJobKind.TTS_CHAPTER.value,
            title="本章批量配音",
            stages=("载入", "筛选台词", "逐行合成", "汇总结果"),
            cancelable=True,
            gpu=False,
            usage="path、chapter",
        ),
        _spec(
            "tts.book",
            channel="audio",
            kind=UiJobKind.TTS_BOOK.value,
            title="全书批量配音",
            stages=("载入", "逐章筛选", "逐行合成", "汇总结果"),
            cancelable=True,
            gpu=False,
            usage="path",
        ),
        _spec(
            "voice.design",
            channel="audio",
            kind=UiJobKind.VOICE_DESIGN.value,
            title="音色抽卡",
            stages=("准备音色描述", "抽卡生成", "保存候选"),
            cancelable=True,
            gpu=False,
            usage="path、character_id",
        ),
        _spec(
            "voice.batch",
            channel="audio",
            kind=UiJobKind.VOICE_DESIGN.value,
            title="批量生成音色",
            stages=("载入角色", "逐角色抽卡", "汇总结果"),
            cancelable=True,
            gpu=False,
            usage="path、character_ids",
        ),
        _spec(
            "voice.preview",
            channel="audio",
            kind=UiJobKind.VOICE_PREVIEW.value,
            title="角色试听",
            stages=("准备音色", "合成试听", "写入试听"),
            cancelable=True,
            gpu=False,
            usage="path、character_id",
        ),
        _spec(
            "sfx.item",
            channel="audio",
            kind=UiJobKind.SFX.value,
            title="单条音效",
            stages=("准备提示词", "合成音效", "写入产物"),
            cancelable=True,
            gpu=False,
            usage="path、chapter、sfx_id",
        ),
        _spec(
            "sfx.chapter",
            channel="audio",
            kind=UiJobKind.SFX.value,
            title="本章音效",
            stages=("载入", "逐条生成", "汇总结果"),
            cancelable=True,
            gpu=False,
            usage="path、chapter",
        ),
        _spec(
            "bgm",
            channel="audio",
            kind=UiJobKind.BGM.value,
            title="配乐",
            stages=("规划配乐", "生成配乐", "写入产物"),
            cancelable=True,
            gpu=False,
            usage="path、chapter",
        ),
        _spec(
            "image.generate",
            channel="image",
            kind=UiJobKind.IMAGE.value,
            title="配图",
            stages=("排队", "生成图片", "写入产物"),
            cancelable=True,
            gpu=True,
            usage="path、chapter、key_image_id",
        ),
        _spec(
            "video.generate",
            channel="video",
            kind=UiJobKind.VIDEO.value,
            title="视频",
            stages=("排队", "生成视频", "写入产物"),
            cancelable=True,
            gpu=True,
            usage="path、chapter、video_id",
        ),
        _spec(
            "anim",
            channel="video",
            kind=UiJobKind.ANIM.value,
            title="图生视频",
            stages=("排队", "读取关键帧", "生成视频", "写入产物"),
            cancelable=True,
            gpu=True,
            usage="path、chapter、video_id",
        ),
    )
}


def action_spec(action: str) -> MediaActionSpec:
    """读取一个已登记媒体动作的规格。

    参数:
        action: 动作键（见 `ACTIONS`）。

    返回:
        `MediaActionSpec`。

    异常:
        ValueError: 动作未登记，消息中列出全部可选动作与标题。
    """
    spec = ACTIONS.get(str(action or ""))
    if spec is None:
        choices = "、".join(f"{key}（{item.title}）" for key, item in ACTIONS.items())
        raise ValueError(f"未登记的媒体动作「{action}」；可选：{choices}")
    return spec


def action_rows() -> list[dict]:
    """把动作登记表展开成前端可直接消费的行。

    返回:
        每项含 `action/channel/channel_label/kind/title/stages/cancelable/gpu/usage`；
        `stages` 为列表，`channel_label` 为「声音/图片/视频」。
    """
    return [
        {
            "action": spec.action,
            "channel": spec.channel,
            "channel_label": CHANNEL_LABELS.get(spec.channel, spec.channel),
            "kind": spec.kind,
            "title": spec.title,
            "stages": list(spec.stages),
            "cancelable": spec.cancelable,
            "gpu": spec.gpu,
            "usage": spec.usage,
        }
        for spec in ACTIONS.values()
    ]
