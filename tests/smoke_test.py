"""冒烟自检：验证 M1 数据链路 + 队列占位行为。

直接运行：`python tests/smoke_test.py`。不依赖 pytest，不调用任何模型。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novel_drama.adapters import build_adapters  # noqa: E402
from novel_drama.config import load_config  # noqa: E402
from novel_drama.media import build_timeline  # noqa: E402
from novel_drama.models import entities  # noqa: E402
from novel_drama.models.ids import new_id  # noqa: E402
from novel_drama.orchestration import GpuQueue  # noqa: E402
from novel_drama.orchestration.pipeline import build_tts_jobs, split_chapter_scenes, split_scene_lines  # noqa: E402
from novel_drama.store import ProjectStore, validate  # noqa: E402

CHAPTER_TEXT = """沈砚推开门，屋里一片漆黑。

“说重点。”沈砚低声说。

「我不知道。」她后退一步。

第二天清晨，走廊的灯忽明忽暗。

“卷宗缺了三页。”沈砚把文件摊在桌上。
"""


def check(condition: bool, message: str) -> None:
    """断言并打印结果。

    参数:
        condition: 断言条件。
        message: 通过时的描述。

    异常:
        AssertionError: 条件不成立。
    """
    if not condition:
        raise AssertionError(message)
    print(f"[OK] {message}")


def main() -> int:
    """执行冒烟自检。

    返回:
        0 表示全部通过。
    """
    check(bool(new_id("chp").startswith("chp_")), "ID 生成带前缀")

    with tempfile.TemporaryDirectory() as tmp:
        store = ProjectStore(Path(tmp) / "demo")
        project = store.create("长夜将明", author="本地作者", genre=["悬疑"])
        check(store.exists(), "工程创建")
        check(project["settings"]["gpu"]["max_concurrent"] == 1, "GPU 串行固定为 1")

        # 角色
        table = store.load_characters()
        shen = entities.new_character("沈砚", role="protagonist", aliases=["沈队"])
        shen["appearance_prompt"] = "35 岁刑警，短寸头，深色风衣"
        table["characters"].append(shen)
        store.save_characters(table)
        check(len(store.load_characters()["characters"]) == 2, "角色表含旁白与新增角色")

        # 章节与场景
        chapter = store.create_chapter("第一章", content=CHAPTER_TEXT, outline="卷宗缺页")
        check(chapter["word_count"] > 0, "章节字数统计")

        scenes = split_chapter_scenes(store, chapter)
        check(len(scenes) >= 2, f"场景切分（{len(scenes)} 个场景）")

        chapter = store.load_chapter(chapter["id"])
        result = split_scene_lines(store, chapter, store.load_characters()["characters"])
        check(result["lines"] >= 5, f"台词切分（{result['lines']} 行）")
        check(result["unknown"] >= 1, "未识别说话人被标记为 chr_unknown")

        chapter = store.load_chapter(chapter["id"])
        validate(chapter, "chapter.schema.json")
        check(True, "章节通过 schema 校验")

        # 时间轴（全部未合成，应全部为估算）
        timeline = build_timeline(chapter, store.settings())
        check(timeline["duration_ms"] > 0, "时间轴时长计算")
        check(any("估算" in warning for warning in timeline["warnings"]), "未合成行写入估算告警")
        validate(timeline, "timeline.schema.json")
        check(True, "时间轴通过 schema 校验")

        # 队列：占位适配器应给出 NOT_IMPLEMENTED
        jobs = build_tts_jobs(chapter, store.load_characters()["characters"], adapter="placeholder")
        check(len(jobs) == result["lines"], "配音任务按行规划")

        config = load_config()
        # 本机配置里 adapters.tts 是 auto（走真实 provider），这里要验的是「占位适配器必须失败」，
        # 所以显式把 tts 覆盖成 placeholder，否则会用真模型出音、这条断言失去意义
        config.raw.setdefault("adapters", {})["tts"] = "placeholder"
        adapters = build_adapters(config, store.settings())
        queue = GpuQueue(store, adapters, lock_path=Path(tmp) / "gpu.lock", poll_interval_s=0.2)
        created = sum(1 for job in jobs if queue.enqueue(job)[1])
        check(created == len(jobs), "任务入队并按幂等键去重")
        duplicate = queue.enqueue(jobs[0])
        check(duplicate[1] is False, "同幂等键不重复入队")

        done = queue.run_once()
        check(done is not None and done["status"] == "failed", "占位适配器执行失败而非假成功")
        check(done["error"]["code"] == "NOT_IMPLEMENTED", "失败错误码为 NOT_IMPLEMENTED")

        # BGM 短素材按时间槽循环填充：loop=true 时 end_ms 取槽时长，而不是补静音
        from novel_drama.media.mixer import plan_mix

        dummy_asset = Path(tmp) / "loop_asset.wav"
        dummy_asset.write_bytes(b"placeholder")
        bgm_plan = plan_mix(
            {"chapter_id": "loop", "duration_ms": 30000, "sample_rate": 48000, "channels": 2, "tracks": []},
            tmp,
            [{"id": "bgm_test", "asset_id": "loop_asset.wav", "loop": True, "gain_db": -18}],
        )
        music_item = bgm_plan["tracks"]["music"][0]
        check(music_item["loop"] is True and music_item["end_ms"] == 30000, "BGM 短素材按时间槽循环填充")

        # 时间轴文件落盘
        timeline_path = store.timeline_path(chapter["id"])
        store._write_json(timeline_path, timeline)
        check(json.loads(timeline_path.read_text(encoding="utf-8"))["chapter_id"] == chapter["id"], "时间轴落盘")

    print("\nSMOKE OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
