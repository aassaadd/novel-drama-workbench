"""统一媒体生成总线。

声音、图片、视频、音效、配乐的所有生成入口都必须经过这里：
校验 action、创建统一任务、起后台线程执行处理器、转发进度与取消、
归一化错误文案，并在结束时写一条操作日志。页面不再各自维护 job 接口。

处理器由具体执行器在导入时注册，签名为
`handler(handle, store, config, params) -> dict`，返回的 dict 会写入任务
`meta.result` 并作为成功摘要展示。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from ... import cancel as cancel_mod
from ...adapters.observability import oplog
from ...adapters.persistence.project_store import ProjectStore
from ...cancel import GenerationCanceled
from ...infrastructure.config import AppConfig
from ...models.enums import UiJobStatus
from ..production.job_tracker import (
    TERMINAL_STATUSES,
    UiJob,
    clear_finished,
    job_snapshot,
    load_ui_jobs,
    update_job,
)
from . import llm_cancel
from .llm_cancel import LlmCancelled
from .media_plan import CHANNEL_LABELS, MediaActionSpec, action_spec

logger = logging.getLogger(__name__)

#: 媒体处理器：action → 执行函数
_HANDLERS: dict[str, Callable[[MediaJobHandle, ProjectStore, AppConfig, dict], dict]] = {}
_HANDLERS_LOCK = threading.Lock()

#: 从 params 里挑出能定位任务的字段，写入任务 target
_TARGET_KEYS = (
    "chapter",
    "chapter_id",
    "line",
    "line_id",
    "scene",
    "scene_id",
    "character",
    "character_id",
    "act",
    "act_id",
    "sfx_id",
    "key_image_id",
    "video_id",
    "item_id",
    "asset_id",
)

#: 取消等待上限（秒）与轮询步长（秒）
_CANCEL_WAIT_S = 5.0
_CANCEL_STEP_S = 0.1


def register_handler(
    action: str,
    handler: Callable[[MediaJobHandle, ProjectStore, AppConfig, dict], dict],
) -> None:
    """登记一个媒体动作的执行器。

    参数:
        action: 动作键（必须在 `media_plan.ACTIONS` 中登记）。
        handler: `handler(handle, store, config, params) -> dict`。

    返回:
        无。

    异常:
        ValueError: 动作未登记或该动作已注册过执行器。
    """
    spec = action_spec(action)
    with _HANDLERS_LOCK:
        if spec.action in _HANDLERS:
            raise ValueError(f"媒体动作「{spec.action}」已经注册过执行器，不能重复注册")
        _HANDLERS[spec.action] = handler


def registered_handlers() -> dict[str, Callable]:
    """取当前已注册的执行器（副本）。

    返回:
        `{action: handler}` 的新 dict。
    """
    with _HANDLERS_LOCK:
        return dict(_HANDLERS)


class MediaJobHandle:
    """传给执行器的任务句柄：转发进度、产物、元数据与取消检查。

    参数:
        job: 已 `start()` 的任务写入器。
    """

    def __init__(self, job: UiJob):
        """绑定任务写入器。"""
        self._job = job
        self._result: dict = {}

    @property
    def job_id(self) -> str:
        """任务 ID。"""
        return str((self._job.job or {}).get("id") or "")

    @property
    def kind(self) -> str:
        """任务类型（`UiJobKind` 落盘值）。"""
        return str(self._job.kind or "")

    @property
    def action(self) -> str:
        """媒体动作键。"""
        return str(self._job.action or "")

    @property
    def channel(self) -> str:
        """通道（audio/image/video）。"""
        return str(self._job.channel or "")

    @property
    def result_summary(self) -> dict:
        """最后一次写入的结果摘要（处理器返回值的副本）。"""
        return dict(self._result)

    def progress(
        self,
        current: str | None = None,
        percent: float | None = None,
        stage: str | None = None,
        force: bool = False,
    ) -> None:
        """更新任务进度。

        参数:
            current: 当前项文案。
            percent: 百分比；None 表示不修改。
            stage: 阶段文案；None 表示不修改。
            force: True 时忽略写盘节流。
        """
        self._job.progress(current, percent, stage=stage, force=force)

    def stage(self, name: str) -> None:
        """只更新阶段文案。

        参数:
            name: 阶段中文名。
        """
        self._job.stage(name)

    def artifacts(self, items: list[str]) -> None:
        """写入已生成产物列表。

        参数:
            items: 产物路径（相对工程根或绝对路径）。
        """
        self._job.set_artifacts(list(items))

    def set_meta(self, **fields) -> None:
        """写入任务元数据。

        参数:
            **fields: 合并进任务 `meta` 的字段。
        """
        self._job.set_meta(**fields)

    def cancel_requested(self) -> bool:
        """任务是否已被请求取消。

        返回:
            True 表示用户已点停止，执行器应尽快退出且不再写产物。
        """
        return self._job.cancel_requested()

    def _remember(self, result: dict) -> None:
        """记录处理器返回值（仅供总线内部使用）。

        参数:
            result: 处理器返回的 dict。
        """
        self._result = dict(result or {})


def _target_of(params: dict) -> dict:
    """从请求参数里挑出能定位任务的字段。

    参数:
        params: 前端传来的参数。

    返回:
        仅含非空定位字段的 dict。
    """
    target: dict = {}
    for key in _TARGET_KEYS:
        value = params.get(key)
        if value is None or str(value).strip() == "":
            continue
        target[key] = value
    return target


def plan(store: ProjectStore | None, config: AppConfig | None, action: str, params: dict | None) -> dict:
    """返回一次媒体生成的事实清单（供统一确认弹窗）。

    参数:
        store: 工程（计划阶段允许为空，当前实现不读取工程）。
        config: 本机配置（当前实现不读取）。
        action: 动作键。
        params: 前端参数（原样回显在 `params_echo`）。

    返回:
        含 `action/channel/channel_label/kind/title/stages/cancelable/gpu/usage/
        params_echo/count/eta_ms/queue/cancel_text` 的 dict；`count/eta_ms` 暂为 None，
        待具体动作注册计划函数后再补，不编造数据。

    异常:
        ValueError: 动作未登记。
    """
    spec: MediaActionSpec = action_spec(action)
    echo = dict(params or {})
    return {
        "action": spec.action,
        "channel": spec.channel,
        "channel_label": CHANNEL_LABELS.get(spec.channel, spec.channel),
        "kind": spec.kind,
        "title": spec.title,
        "stages": list(spec.stages),
        "cancelable": spec.cancelable,
        "gpu": spec.gpu,
        "usage": spec.usage,
        "params_echo": echo,
        "count": None,
        "eta_ms": None,
        "queue": spec.gpu,
        "cancel_text": "可随时取消" if spec.cancelable else "不可取消",
    }


def _result_text(result: dict) -> str:
    """把处理器返回值压成一句可展示的结果摘要。

    参数:
        result: 处理器返回的 dict。

    返回:
        用户可读的中文摘要。
    """
    if not result:
        return "已完成"
    for key in ("message", "summary", "note", "title"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return f"返回 {len(result)} 个字段"


def _oplog_status(status: str) -> str:
    """把任务终态映射成操作日志状态。

    参数:
        status: 任务终态值。

    返回:
        `ok` / `partial` / `error`。
    """
    if status == "succeeded":
        return "ok"
    if status in ("partial", "canceled"):
        return "partial"
    return "error"


def _ensure_handlers() -> None:
    """兜底加载执行器：调用方未提前注册时，按动作登记表补齐一次。

    返回:
        无。

    说明:
        Web 启动时由 `create_app` 显式注册；测试或脚本直接调用总线时，
        这里懒加载一次，避免「动作已登记但没有执行器」。导入失败只记日志，
        由 `run` 继续给出「还没有可用的执行器」的 400。
    """
    if _HANDLERS:
        return
    try:
        from .media_handlers import register_all
    except ImportError:  # pragma: no cover - 模块缺失时保持原有 400 行为
        logger.warning("媒体执行器模块导入失败，回退为「未注册执行器」", exc_info=True)
        return
    register_all()


def run(store: ProjectStore, config: AppConfig | None, action: str, params: dict | None) -> dict:
    """创建并异步执行一次媒体生成任务。

    参数:
        store: 工程。
        config: 本机配置（透传给处理器）。
        action: 动作键（必须已登记且有执行器）。
        params: 前端参数（需含 path 等定位字段）。

    返回:
        新建任务的统一快照（立即返回，不等生成完成）。

    异常:
        ValueError: 动作未登记，或该动作还没有注册执行器。
    """
    spec = action_spec(action)
    _ensure_handlers()
    with _HANDLERS_LOCK:
        handler = _HANDLERS.get(spec.action)
    if handler is None:
        raise ValueError(f"媒体动作「{spec.action}」还没有可用的执行器")

    payload = dict(params or {})
    target = _target_of(payload)
    job = UiJob(
        store,
        spec.kind,
        target,
        spec.title,
        action=spec.action,
        channel=spec.channel,
        cancelable=spec.cancelable,
    )
    job.start()
    handle = MediaJobHandle(job)
    threading.Thread(
        target=_execute,
        args=(handle, spec, handler, store, config, payload, target),
        name=f"ndw-media-{spec.action}",
        daemon=True,
    ).start()
    return job.snapshot()


def _execute(
    handle: MediaJobHandle,
    spec: MediaActionSpec,
    handler: Callable[[MediaJobHandle, ProjectStore, AppConfig, dict], dict],
    store: ProjectStore,
    config: AppConfig | None,
    params: dict,
    target: dict,
) -> None:
    """后台线程：执行处理器并把结果落到统一任务。

    参数:
        handle: 任务句柄。
        spec: 动作规格。
        handler: 执行器。
        store: 工程。
        config: 本机配置。
        params: 前端参数。
        target: 任务定位字段（写操作日志用）。

    返回:
        无。
    """
    job = handle._job
    started = time.monotonic()
    error_text = ""
    # 绑定父任务：处理器内的 LLM 调用会登记到本任务下，取消时级联停止
    with llm_cancel.begin_media_job(handle.job_id) as counter:
        try:
            result = handler(handle, store, config, params)
            payload = result if isinstance(result, dict) else {"result": result}
            handle._remember(payload)
            if handle.cancel_requested():
                job.set_meta(llm_calls=counter.count)
                job.cancel("用户已停止生成")
            else:
                job.set_meta(result=payload, llm_calls=counter.count)
                if int(payload.get("failed") or 0) and int(payload.get("ready") or 0):
                    job.partial(_result_text(payload))
                else:
                    job.finish(_result_text(payload))
        except GenerationCanceled:
            job.set_meta(llm_calls=counter.count)
            job.cancel("用户已停止生成")
        except LlmCancelled as exc:
            job.set_meta(llm_calls=counter.count)
            job.cancel(str(getattr(exc, "message", "") or "已取消本次模型调用"))
        except Exception as exc:  # noqa: BLE001 - 任何执行器异常都要落成中文失败原因
            logger.exception("媒体动作 %s 执行失败", spec.action)
            error_text = str(exc)
            job.set_meta(llm_calls=counter.count)
            job.fail(f"{spec.title}失败：{exc}")
        finally:
            final_status = str((job.job or {}).get("status") or "")
            summary = ""
            if final_status in ("succeeded", "partial"):
                summary = str((job.job or {}).get("current") or "")
            elif final_status == "canceled":
                error_text = error_text or str((job.job or {}).get("error") or "用户已停止生成")
            try:
                oplog.record_op(
                    action=spec.action,
                    label=spec.title,
                    target=str(target),
                    summary=summary,
                    status=_oplog_status(final_status),
                    duration_ms=int((time.monotonic() - started) * 1000),
                    project=str(store.root),
                    error=error_text,
                )
            except Exception:  # noqa: BLE001 - 写操作日志失败绝不影响任务终态
                logger.warning("写媒体任务操作日志失败（action=%s）", spec.action, exc_info=True)
    # 终态已落盘：补写本次嵌套的 LLM 调用次数（任务详情展示）
    job.set_meta(llm_calls=counter.count)


def snapshot(store: ProjectStore, job_id: str) -> dict | None:
    """按任务 ID 读取统一快照。

    参数:
        store: 工程。
        job_id: 任务 ID（`id` 或 `job_id` 均可）。

    返回:
        统一任务快照；任务不存在时返回 None。
    """
    key = str(job_id or "")
    for item in load_ui_jobs(store):
        if key and key in (str(item.get("id") or ""), str(item.get("job_id") or "")):
            return job_snapshot(item)
    return None


def _find_job(store: ProjectStore, job_id: str) -> dict | None:
    """按 ID 找原始任务记录。

    参数:
        store: 工程。
        job_id: 任务 ID。

    返回:
        任务记录；不存在时返回 None。
    """
    key = str(job_id or "")
    for item in load_ui_jobs(store):
        if key and key in (str(item.get("id") or ""), str(item.get("job_id") or "")):
            return item
    return None


def cancel(store: ProjectStore, job_id: str) -> dict:
    """协作式取消一条媒体任务（幂等）。

    参数:
        store: 工程。
        job_id: 任务 ID。

    返回:
        `{canceled, status, forced, message}`；任务已结束或本进程无对应线程时
        `canceled` 可能为 False，但不会抛错。

    异常:
        KeyError: 任务不存在（路由层转 404）。
    """
    current = _find_job(store, job_id)
    if current is None:
        raise KeyError(f"任务不存在：{job_id}")
    job_key = str(current.get("id") or job_id)
    status = str(current.get("status") or "")
    if status in TERMINAL_STATUSES:
        label = UiJobStatus.label_of(status) or status
        return {
            "canceled": False,
            "status": status,
            "forced": False,
            "message": f"任务已结束（{label}），无需停止",
        }

    # 先级联取消正在进行的 LLM 子调用，让执行器在下一个分片检查时退出
    llm_cancel.cancel_children(job_key)
    notified = cancel_mod.request(job_key)
    if not notified:
        # 本进程没有对应线程：直接落取消，避免任务永远挂在运行中
        update_job(store, job_key, status="canceled", error="后端已无对应生成进程，已标记为取消")
        return {
            "canceled": True,
            "status": "canceled",
            "forced": True,
            "message": "后端已无对应生成进程，任务已标记为已取消",
        }

    deadline = time.monotonic() + _CANCEL_WAIT_S
    while time.monotonic() < deadline:
        latest = _find_job(store, job_key)
        latest_status = str((latest or {}).get("status") or "")
        if latest_status == "canceled":
            return {
                "canceled": True,
                "status": "canceled",
                "forced": False,
                "message": "已停止生成，任务标记为已取消；已完成产物保留",
            }
        if latest_status in TERMINAL_STATUSES:
            label = UiJobStatus.label_of(latest_status) or latest_status
            return {
                "canceled": False,
                "status": latest_status,
                "forced": False,
                "message": f"任务已结束（{label}），无需停止",
            }
        time.sleep(_CANCEL_STEP_S)

    update_job(
        store,
        job_key,
        status="canceled",
        error="已发出停止请求，但生成线程未在 5 秒内退出；任务已标记为已取消",
    )
    return {
        "canceled": True,
        "status": "canceled",
        "forced": False,
        "message": "已发出停止请求，任务标记为已取消；后台生成会在下次检查时退出",
    }


def clear_finished_jobs(store: ProjectStore) -> int:
    """清理已结束的媒体任务行。

    参数:
        store: 工程。

    返回:
        删除的行数。
    """
    return clear_finished(store)
