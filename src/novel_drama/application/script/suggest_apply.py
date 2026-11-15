"""生成标注建议并按需落盘/同步到章节（页面与 CLI 共用）。"""

from __future__ import annotations

from pathlib import Path

from ...adapters.persistence import ProjectStore
from ...composition import validate
from .annotate import suggest_script
from .apply_script import apply_script_to_chapter


def suggest_and_apply(
    store: ProjectStore,
    config,
    chapter_id: str,
    apply: bool = False,
    dry_run: bool = False,
    resume: bool = False,
    scenes_per_round: int | None = None,
    out_path: str | None = None,
) -> dict:
    """生成导演标注建议；`apply` 时校验并写回脚本与章节。

    参数:
        store: 工程。
        config: 本机配置。
        chapter_id: 章节 ID。
        apply: 是否把成功批次的脚本同步到章节。
        dry_run: 只生成不落盘。
        resume: 只重试失败批次。
        scenes_per_round: 每轮处理镜头数。
        out_path: 标注文本输出路径；为空时落到 `scripts/<chapter>.markup.txt`。

    返回:
        `{"payload": 接口返回体, "result": suggest_script 原始结果, "markup_path": 标注文本路径}`。
    """
    result = suggest_script(
        store,
        config,
        chapter_id,
        dry_run=dry_run,
        scenes_per_round=scenes_per_round,
        resume=resume,
    )
    markup_path = Path(out_path) if out_path else store.root / "scripts" / f"{chapter_id}.markup.txt"
    if not dry_run:
        markup_path.parent.mkdir(parents=True, exist_ok=True)
        markup_path.write_text(result["markup"] + "\n", encoding="utf-8")

    base = {key: value for key, value in result.items() if key != "markup"}
    failures = result.get("failures") or []
    if apply and not dry_run and not failures:
        script = result["script"]
        validate(script, "director-script.schema.json")
        store.save_script(script)
        chapter = store.load_chapter(chapter_id)
        summary = apply_script_to_chapter(store, chapter, script)
        payload = {**base, "apply": summary, "markup_path": str(markup_path)}
    else:
        payload = {**base, "markup_path": str(markup_path)}
    return {"payload": payload, "result": result, "markup_path": str(markup_path)}
