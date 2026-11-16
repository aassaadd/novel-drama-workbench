"""配音错误文案归一化：把 provider / 批次错误翻成中文可操作提示。

页面旧实现把这段逻辑放在路由层；媒体总线接管单行重录后，路由与执行器都需要
同一套说法，因此集中到应用层，避免两处文案漂移。
"""

from __future__ import annotations


def error_text(error: dict | None, fallback: str = "") -> str:
    """把 provider / 批次错误翻译成中文可操作提示。

    参数:
        error: `line.tts.error` 或批次结果里的错误 dict（含 `code/message`）。
        fallback: 没有错误 dict 时的兜底文案。

    返回:
        中文原因；不吞错。
    """
    if not error:
        return fallback or "合成失败，但未拿到具体原因（查看 .ndw/logs 下日志）"
    code = str(error.get("code") or "")
    message = str(error.get("message") or "").strip() or "未知错误"
    lower = message.lower()
    if code == "VOICE_MISSING" or "参考音频不存在" in message or "no such file" in lower or "not found" in lower:
        if "不存在" in message or "no such file" in lower or "not found" in lower:
            return f"参考音失效：{message}（请重新上传或重新选择该角色的音色）"
        return f"缺音色或参考音：{message}（检查角色绑定的音色是否有可用参考音频）"
    if code == "MODEL_MISSING":
        return f"模型未就绪：{message}"
    if code == "NOT_IMPLEMENTED":
        return f"当前音色 provider 未实现：{message}"
    return f"合成失败（{code or '未知错误'}）：{message}"
