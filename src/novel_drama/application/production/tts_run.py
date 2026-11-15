"""按范围入队合成台词：装配串行队列、排序、执行，并给后台进程提供应用入口。

页面与 CLI 共用本模块：`tts run` 走同一套规划与队列装配，
后台消费者由 `interface/http/executor.spawn_application` 以独立进程启动，
入参是「模块:函数」，不再拼 CLI 命令串。
"""

from __future__ import annotations

from ...composition import ErrorCode, ProjectStore, build_adapters
from ...infrastructure.config import AppConfig, load_config
from ...orchestration import GpuQueue
from .tts_plan import plan_chapter


def build_adapters_for(config: AppConfig, store: ProjectStore | None):
    """装配适配器（工程存在时合并工程级设置）。

    参数:
        config: 本机配置。
        store: 工程（可为空）。

    返回:
        Adapters。
    """
    settings = store.settings() if store is not None else None
    return build_adapters(config, settings)


def make_queue(store: ProjectStore, config: AppConfig) -> GpuQueue:
    """创建串行队列。

    参数:
        store: 工程。
        config: 本机配置。

    返回:
        GpuQueue。
    """
    adapters = build_adapters_for(config, store)
    lock_path = config.get("gpu.lock_path")
    return GpuQueue(store, adapters, lock_path, poll_interval_s=float(config.get("gpu.poll_interval_s", 2)))


def queue_service(queue: GpuQueue):
    """取队列内部使用的配音服务实例（与执行阶段完全同一个对象）。

    参数:
        queue: 串行队列。

    返回:
        TtsService；占位适配器时为 None。
    """
    tts = getattr(getattr(queue, "adapters", None), "tts", None)
    return getattr(tts, "service", None)


def sort_jobs_by_group(
    jobs: list[dict],
    chapter: dict,
    characters: list[dict],
    service,
    store: ProjectStore,
) -> list[dict]:
    """按 worker 复用键对配音任务做稳定排序（只改执行顺序，不改产物与章节顺序）。

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
        from ...tts.batch import line_group_key

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


def drain(queue: GpuQueue) -> int:
    """执行队列中所有可执行任务，并按执行结果给出退出码。

    参数:
        queue: 串行队列。

    返回:
        退出码：全部成功返回 0；存在 NOT_IMPLEMENTED 失败返回 3；其他失败返回 5。
    """
    exit_code = 0
    while True:
        job = queue.run_once()
        if job is None:
            return exit_code
        print(
            f"[{job['status']}] {job['kind']} {job['id']}"
            + (f"  {job['error']['message']}" if job.get("error") else "")
        )
        if job["status"] == "failed":
            code = (job.get("error") or {}).get("code")
            if code == ErrorCode.NOT_IMPLEMENTED.value:
                exit_code = 3
            elif exit_code == 0:
                exit_code = 5


def run_chapter_tts(
    store: ProjectStore,
    config: AppConfig,
    chapter_id: str,
    scope: str | None = None,
    line_ids: list[str] | None = None,
    scene_ids: list[str] | None = None,
    statuses_raw: str | None = None,
    force: bool = False,
    wait: bool = True,
) -> dict:
    """规划指定范围并入队，按需等待队列执行完。

    参数:
        store: 工程。
        config: 本机配置。
        chapter_id: 章节 ID。
        scope: 范围（selected / pending / scene / chapter）；为空时用默认范围。
        line_ids: `scope=selected` 时选中的行。
        scene_ids: `scope=scene` 时选中的场景。
        statuses_raw: 逗号分隔的派生状态筛选。
        force: 忽略状态筛选与产物缓存，强制重新生成。
        wait: 是否在本次调用里等队列跑完（后台消费者传 True）。

    返回:
        `{"jobs": 规划行数, "created": 实际入队数, "exit_code": 队列执行退出码}`。
    """
    plan = plan_chapter(
        store,
        config,
        chapter_id,
        scope=scope,
        line_ids=line_ids,
        scene_ids=scene_ids,
        statuses_raw=statuses_raw,
        force=force,
    )
    jobs = plan["jobs"]
    print(
        f"待合成台词行: {len(jobs)} 条（范围 {plan['scope']}，适配器 {plan['adapter']}）"
        f"｜未生成 {plan['counts'].get('pending', 0)} / 过期 {plan['counts'].get('stale', 0)}"
        f" / 失败 {plan['counts'].get('failed', 0)} / 已完成 {plan['counts'].get('ready', 0)}"
    )
    if not jobs:
        print("没有需要合成的台词行")
        return {"jobs": 0, "created": 0, "exit_code": 0}
    queue = make_queue(store, config)
    service = queue_service(queue)
    if service is not None:
        jobs = sort_jobs_by_group(jobs, plan["chapter"], plan["characters"], service, store)
    try:
        created = sum(1 for job in jobs if queue.enqueue(job)[1])
        print(f"已入队 {created} 条配音任务（去重后）")
        code = drain(queue) if wait else 0
        return {"jobs": len(jobs), "created": created, "exit_code": code}
    finally:
        # 整批结束（含异常/停止）都要回收常驻 worker，避免留下孤儿进程
        if service is not None:
            service.close()


def run_chapter_entry(
    project_root: str,
    config_path: str | None = None,
    chapter_id: str = "",
    scope: str | None = None,
    line_ids: list[str] | None = None,
    scene_ids: list[str] | None = None,
    statuses: str | None = None,
    force: bool = False,
    wait: bool = True,
) -> dict:
    """后台进程入口：按工程目录读取配置并执行一次 `tts run`。

    参数:
        project_root: 工程目录。
        config_path: 本机配置文件路径（可空）。
        chapter_id: 章节 ID。
        scope: 范围；为空时用默认范围。
        line_ids: `scope=selected` 时选中的行。
        scene_ids: `scope=scene` 时选中的场景。
        statuses: 逗号分隔的派生状态筛选。
        force: 强制重新生成。
        wait: 是否等队列跑完。

    返回:
        `run_chapter_tts` 的结果，额外带 `exit_code` 供子进程退出码使用。
    """
    config = load_config(config_path) if config_path else load_config()
    store = ProjectStore(project_root)
    return run_chapter_tts(
        store,
        config,
        chapter_id,
        scope=scope,
        line_ids=line_ids,
        scene_ids=scene_ids,
        statuses_raw=statuses,
        force=force,
        wait=wait,
    )
