"""统一 LLM 总线。

所有写作模型调用都必须经过这里：构造适配器、校验 action、按分片检查取消、
把适配器错误补上动作名后抛给上层。这样页面进度、取消语义和错误文案不会
因为入口不同而各写一套。

超时策略：LLM 调用不设额外总超时，由用户取消；只保留后端连接超时等
业务必需的超时（由适配器配置控制），本模块不新增任何总时长限制。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from ...adapters.errors import AdapterError, ErrorCode, HealthStatus
from ...adapters.llm.contracts import TextGenRequest, TextGenResult
from ...adapters.llm.factory import build_writing_adapter
from ...infrastructure.config import AppConfig
from . import llm_cancel
from .llm_plan import action_label, action_spec

__all__ = ["LlmBus", "BusWritingAdapter", "LlmCancelled", "current_call_id", "new_call_id"]


#: 取消异常从总线模块透出，调用方不必同时导入两个 support 模块
LlmCancelled = llm_cancel.LlmCancelled
current_call_id = llm_cancel.current_call_id
new_call_id = llm_cancel.new_call_id


@dataclass
class LlmBus:
    """一次请求内的写作模型调用入口。

    参数:
        config: 本机配置。
        settings: 工程设置（可选）；`writing` 子项覆盖本机 omlx 参数。
        call_id: 本次页面调用的标识；为空时自动读取当前请求上下文。
    """

    config: AppConfig | None = None
    settings: dict | None = None
    call_id: str = ""
    _adapter: Any = field(default=None, init=False, repr=False)
    _parent_job: str = field(default="", init=False, repr=False)

    def __post_init__(self) -> None:
        """补全 call_id、登记取消令牌，并挂到当前媒体任务（若有）。"""
        self.call_id = str(self.call_id or llm_cancel.current_call_id() or "")
        if self.call_id:
            llm_cancel.register(self.call_id)
            self._attach_child()

    def _attach_child(self) -> None:
        """把本次调用登记到当前父媒体任务下（不在媒体任务内时不做任何事）。"""
        parent = llm_cancel.current_parent_job()
        if not parent or not self.call_id:
            return
        self._parent_job = parent
        llm_cancel.register_child(parent, self.call_id)

    def _release_child(self) -> None:
        """取消子调用登记；保留父任务信息，下一次调用会重新登记。"""
        if self.call_id and self._parent_job:
            llm_cancel.release_child(self._parent_job, self.call_id)

    @classmethod
    def from_adapter(cls, adapter, action: str, call_id: str = "") -> "BusWritingAdapter":
        """用已构造好的适配器创建总线外观（供无 AppConfig 的辅助模块使用）。

        参数:
            adapter: 真实写作适配器。
            action: 动作键。
            call_id: 页面调用标识（可选）。

        返回:
            绑定动作的 `BusWritingAdapter`。
        """
        bus = cls(config=None, call_id=call_id)
        bus._adapter = adapter
        return bus.bind(action)

    def _raw_adapter(self):
        """取（必要时构造）真实写作适配器；仅供总线内部使用。

        返回:
            实现 `WritingAdapter` 协议的实例。
        """
        if self._adapter is None:
            if self.config is None:
                raise AdapterError(ErrorCode.INVALID_INPUT, "LlmBus 缺少 config，无法构造写作适配器")
            self._adapter = build_writing_adapter(self.config, self.settings)
        return self._adapter

    def bind(self, action: str) -> "BusWritingAdapter":
        """把总线绑定到一个动作，返回可直接调用的适配器外观。

        兼容仍按 `adapter.generate(...)` 写法的旧调用方：所有调用仍会经过
        总线校验 action、检查取消并统一错误文案。

        参数:
            action: 动作键。

        返回:
            `BusWritingAdapter`。
        """
        self.spec(action)
        return BusWritingAdapter(self, action)

    def adapter(self):
        """兼容旧代码：返回未绑定动作的原始适配器。

        新代码请使用 `text` / `tool` / `stream` 或 `bind(action)`。
        返回:
            实现 `WritingAdapter` 协议的实例。
        """
        return self._raw_adapter()

    def spec(self, action: str) -> dict:
        """校验并返回动作登记项。

        参数:
            action: 动作键。

        返回:
            动作登记项。

        异常:
            AdapterError(INVALID_INPUT): 动作未在 `llm_plan.ACTIONS` 登记。
        """
        try:
            return action_spec(action)
        except ValueError as exc:
            raise AdapterError(ErrorCode.INVALID_INPUT, str(exc)) from exc

    def label(self, action: str) -> str:
        """取动作中文名，供错误文案使用。

        参数:
            action: 动作键。

        返回:
            中文标题。
        """
        return action_label(action)

    def _check(self) -> None:
        """检查当前调用是否已被取消。

        异常:
            LlmCancelled: 用户已取消本次调用。
        """
        llm_cancel.ensure_active(self.call_id)

    def _wrap(self, action: str, exc: AdapterError) -> AdapterError:
        """给适配器错误补上动作名，避免页面只看到底层报错。

        参数:
            action: 动作键。
            exc: 适配器错误。

        返回:
            带动作名的 AdapterError。
        """
        title = self.label(action)
        message = str(exc.message or "")
        if message.startswith(title):
            return exc
        detail = dict(exc.detail or {})
        detail.setdefault("action", action)
        return AdapterError(exc.code, f"{title}失败：{message}", retryable=exc.retryable, detail=detail)

    def text(self, action: str, req: TextGenRequest, on_delta=None, ctx: dict | None = None) -> TextGenResult:
        """生成文本；内部强制流式分片，以便随时取消。

        参数:
            action: 动作键（必须已登记）。
            req: 文本请求。
            on_delta: 增量回调；为空时只用于取消检查。
            ctx: 预留的调用上下文（当前未使用）。

        返回:
            TextGenResult。

        异常:
            LlmCancelled: 用户取消。
            AdapterError: 模型配置/上游错误（文案已带动作名）。
        """
        self.spec(action)
        self._attach_child()
        self._check()

        def tap(text: str) -> None:
            """每个分片先检查取消，再把增量交给页面。"""
            self._check()
            if on_delta is not None:
                on_delta(text)

        try:
            adapter = self._raw_adapter()
            stream_fn = getattr(adapter, "generate_stream", None)
            if stream_fn is not None:
                # 即使调用方不需要增量，也强制流式：分片之间才有取消点
                result = stream_fn(replace(req, stream=True), on_delta=tap)
            else:
                self._check()
                result = adapter.generate(req)
                if on_delta is not None and getattr(result, "text", ""):
                    on_delta(result.text)
            self._check()
            return result
        except LlmCancelled:
            raise
        except AdapterError as exc:
            raise self._wrap(action, exc) from exc
        finally:
            self._release_child()

    def tool(
        self,
        action: str,
        req: TextGenRequest,
        tool: dict,
        *,
        step: str | None = None,
        on_delta=None,
        timeout_s: float | None = None,
    ) -> dict:
        """以工具调用方式生成结构化结果；内部强制流式，以便随时取消。

        参数:
            action: 动作键（必须已登记）。
            req: 文本请求。
            tool: 工具定义。
            step: 兼容旧调用方的步骤名（思考阶段由 req.metadata 决定）。
            on_delta: 工具参数增量回调。
            timeout_s: 业务显式指定的单次超时；为空时用适配器配置。

        返回:
            工具参数解析后的 dict。

        异常:
            LlmCancelled: 用户取消。
            AdapterError: 模型配置/上游错误（文案已带动作名）。
        """
        self.spec(action)
        self._attach_child()
        self._check()

        def tap(text: str) -> None:
            """每个分片先检查取消，再把增量交给页面。"""
            self._check()
            if on_delta is not None:
                on_delta(text)

        try:
            raw = self._raw_adapter()
            generate_tool = getattr(raw, "generate_tool", None)
            if generate_tool is None:
                raise AdapterError(
                    ErrorCode.INVALID_INPUT,
                    f"写作适配器不支持工具调用: {type(raw).__name__}",
                )
            result = generate_tool(replace(req, stream=True), tool, timeout_s=timeout_s, on_delta=tap)
            self._check()
            return result
        except LlmCancelled:
            raise
        except AdapterError as exc:
            raise self._wrap(action, exc) from exc
        finally:
            self._release_child()

    def stream(self, action: str, req: TextGenRequest, on_delta, ctx: dict | None = None) -> TextGenResult:
        """生成文本并把每个分片交给页面（`text` 的显式流式别名）。

        参数:
            action: 动作键。
            req: 文本请求。
            on_delta: 增量回调。
            ctx: 预留上下文。

        返回:
            TextGenResult。
        """
        return self.text(action, req, on_delta=on_delta, ctx=ctx)

    def health(self, timeout_s: float | None = None) -> HealthStatus:
        """检查写作服务连通性（业务显式超时，允许调用方指定）。

        参数:
            timeout_s: 连通性探测超时（秒）；为空时用适配器默认值。

        返回:
            HealthStatus。
        """
        return self._raw_adapter().health(timeout_s=timeout_s)


class BusWritingAdapter:
    """绑定到某个动作的适配器外观。

    让旧调用方继续写 `adapter.generate(req)`，但内部仍走总线：
    - 校验动作已登记；
    - 每次分片检查取消；
    - 错误统一补上动作名。

    参数:
        bus: 所属总线。
        action: 默认动作键；请求 metadata 里的 `llm_action` 可覆盖。
    """

    def __init__(self, bus: LlmBus, action: str):
        """绑定总线与动作。"""
        self.bus = bus
        self.action = action

    def _action(self, req: TextGenRequest) -> str:
        """取本次请求实际使用的动作（允许 metadata 覆盖默认动作）。

        参数:
            req: 文本请求。

        返回:
            动作键。
        """
        override = str((getattr(req, "metadata", None) or {}).get("llm_action") or "")
        return override or self.action

    def generate(self, req: TextGenRequest) -> TextGenResult:
        """生成文本（内部强制流式，可在分片间取消）。"""
        return self.bus.text(self._action(req), req)

    def generate_stream(self, req: TextGenRequest, on_delta=None) -> TextGenResult:
        """流式生成文本。"""
        return self.bus.text(self._action(req), req, on_delta=on_delta)

    def generate_tool(self, req: TextGenRequest, tool: dict, timeout_s: float | None = None, on_delta=None) -> dict:
        """以工具调用生成结构化结果。"""
        return self.bus.tool(self._action(req), req, tool, timeout_s=timeout_s, on_delta=on_delta)

    def health(self, timeout_s: float | None = None):
        """连通性探测。"""
        return self.bus.health(timeout_s=timeout_s)

    def __getattr__(self, name: str):
        """其余只读属性透传给真实适配器。"""
        return getattr(self.bus._raw_adapter(), name)
