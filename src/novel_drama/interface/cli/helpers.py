"""CLI 公共装配：适配器/队列构造、任务排序与消费、范围参数。

这些函数只服务命令行层；业务规则仍在 application，适配器实例统一经组合根取。
"""

from __future__ import annotations

from novel_drama.composition import ErrorCode, ProjectStore, build_adapters
from novel_drama.infrastructure.config import AppConfig
from novel_drama.interface.cli.common import EXIT_FAILED, EXIT_NOT_IMPLEMENTED, EXIT_OK
from novel_drama.orchestration import GpuQueue
from novel_drama.tts import line_state


def _build_adapters(config: AppConfig, store: ProjectStore | None):
    """装配适配器（工程存在时合并工程级设置）。

    参数:
        config: 本机配置。
        store: 工程（可为空）。

    返回:
        Adapters。
    """
    settings = store.settings() if store is not None else None
    return build_adapters(config, settings)


def _make_queue(store: ProjectStore, config: AppConfig) -> GpuQueue:
    """创建串行队列。

    参数:
        store: 工程。
        config: 本机配置。

    返回:
        GpuQueue。
    """
    adapters = _build_adapters(config, store)
    lock_path = config.get("gpu.lock_path")
    return GpuQueue(store, adapters, lock_path, poll_interval_s=float(config.get("gpu.poll_interval_s", 2)))


def _queue_service(queue: GpuQueue):
    """取队列内部使用的配音服务实例（与执行阶段完全同一个对象）。

    参数:
        queue: 串行队列。

    返回:
        TtsService；占位适配器时为 None。
    """
    tts = getattr(getattr(queue, "adapters", None), "tts", None)
    return getattr(tts, "service", None)


def _sort_jobs_by_group(jobs: list[dict], chapter: dict, characters: list[dict], service, store: ProjectStore) -> list[dict]:
    """按 worker 复用键对配音任务做稳定排序（只改执行顺序，不改产物与章节顺序）。

    一级键 `(provider_id, model_dir, use_qwen_emo)` 决定是否重启 worker；
    二级键 `(ref_audio, emo_audio_prompt)` 决定组内聚簇，减少参考音频重复编码。

    参数:
        jobs: `build_tts_jobs` 生成的任务列表。
        chapter: 章节 dict。
        characters: 角色表。
        service: TtsService（用于预解析路由与音色）。
        store: 工程读写入口。

    返回:
        排序后的任务列表；预解析失败时原样返回，保证不会漏排或错排。
    """
    if len(jobs) <= 1:
        return jobs
    try:
        from novel_drama.tts.batch import line_group_key

        line_index = {}
        for scene in chapter.get("scenes") or []:
            for line in scene.get("lines") or []:
                line_index[line.get("id")] = line
        characters_table = {"characters": characters}

        def group_key(job: dict) -> tuple:
            """把任务映射到行后复用 batch 的分组键。"""
            line = line_index.get((job.get("target") or {}).get("line_id"))
            if line is None:
                return ("~", "", 1, "", "")
            return line_group_key(line, chapter, characters_table, service, store)

        return sorted(jobs, key=group_key)
    except Exception as exc:  # noqa: BLE001 - 分组只是优化，失败必须退回原顺序
        print(f"[warn] 配音分组预解析失败，按原顺序执行：{exc}")
        return jobs


def _drain(queue: GpuQueue) -> int:
    """执行队列中所有可执行任务，并按执行结果给出退出码。

    参数:
        queue: 串行队列。

    返回:
        退出码：全部成功返回 EXIT_OK；存在 NOT_IMPLEMENTED 失败返回 EXIT_NOT_IMPLEMENTED；
        存在其他失败返回 EXIT_FAILED。
    """
    exit_code = EXIT_OK
    while True:
        job = queue.run_once()
        if job is None:
            return exit_code
        print(f"[{job['status']}] {job['kind']} {job['id']}" + (f"  {job['error']['message']}" if job.get("error") else ""))
        if job["status"] == "failed":
            code = (job.get("error") or {}).get("code")
            if code == ErrorCode.NOT_IMPLEMENTED.value:
                exit_code = EXIT_NOT_IMPLEMENTED
            elif exit_code == EXIT_OK:
                exit_code = EXIT_FAILED


def _add_scope_args(q) -> None:
    """给 `tts plan` / `tts run` 挂上生成范围选择参数。

    参数:
        q: 子命令 parser。
    """
    q.add_argument(
        "--scope",
        choices=list(line_state.SCOPES),
        help="selected=指定行 / pending=待处理（默认）/ scene=指定场景 / chapter=整章",
    )
    q.add_argument("--line", action="append", help="选中的台词行 ID，可重复（--scope selected 必填）")
    q.add_argument("--scene", action="append", help="选中的场景 ID，可重复（--scope scene 必填）")
    q.add_argument("--status", help="逗号分隔的派生状态：pending,stale,failed,ready；与 --scope pending 互斥")
