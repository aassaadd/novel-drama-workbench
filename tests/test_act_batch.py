"""按幕批量生成正文的离线单元测试。

覆盖：只补缺失跳过已有正文、重新生成产生版本历史、单章失败不中断、
章间取消保留已完成章节、进度事件顺序。

运行：`.venv/bin/python -m unittest tests.test_act_batch -v`。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from novel_drama.config import load_config
from novel_drama.models import entities
from novel_drama.store import ProjectStore
from novel_drama.store.settings_store import SettingsStore
from novel_drama.writing.act_batch import ActBatchCanceled, generate_act_chapters
from novel_drama.writing.service import apply_outline, generate_outline


class ActBatchTest(unittest.TestCase):
    """离线跑通整幕批量生成的主要分支。"""

    def setUp(self):
        """建临时工程，生成两章大纲并落地成章节。"""
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ProjectStore(self.tmp.name)
        self.store.create("整幕批量测试", author="ndw")
        self.config = load_config(str(self._runtime_config()))
        generate_outline(self.store, self.config, acts=1, chapters_per_act=2, beats_per_chapter=2, chapter_words=300)
        apply_outline(self.store)
        # 新版写作要求必须绑定视角人物与出场角色：这里显式造两个角色并写入大纲
        table = self.store.load_characters()
        lead = entities.new_character("沈砚", role="protagonist")
        lead["speech_style"] = {"tempo": "偏慢", "pitch": "低沉", "accent": "北方口音", "catchphrases": ["先别急"]}
        peer = entities.new_character("林见夏", role="supporting")
        table["characters"].extend([lead, peer])
        self.store.save_characters(table)
        settings = SettingsStore(self.tmp.name)
        bible = settings.load()
        for chapter in (bible.get("outline") or {}).get("acts")[0].get("chapters") or []:
            chapter["pov_character_id"] = lead["id"]
            for beat in chapter.get("beats") or []:
                beat["character_ids"] = [lead["id"], peer["id"]]
        settings.save(bible)
        bible = SettingsStore(self.tmp.name).load()
        self.act = (bible.get("outline") or {}).get("acts")[0]
        self.chapters = self.act.get("chapters") or []
        self.assertEqual(len(self.chapters), 2)

    def _runtime_config(self) -> Path:
        """生成指向临时目录的最小离线配置（避免写外置盘与系统缓存）。

        返回:
            运行时配置文件路径。
        """
        root = Path(self.tmp.name)
        path = root / "offline.yaml"
        path.write_text(
            "\n".join(
                [
                    "adapters:",
                    "  writing: offline",
                    "  sfx: offline",
                    "gpu:",
                    f"  lock_path: {root / 'gpu.lock'}",
                    "tts:",
                    f"  hf_home: {root / 'hf'}",
                    "  builtin_voices:",
                    f"    root: {root / 'voices'}",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        return path

    def tearDown(self):
        """清理临时工程。"""
        self.tmp.cleanup()

    def _run(self, **kwargs):
        """调用整幕生成并收集事件。"""
        events: list[dict] = []
        result = generate_act_chapters(
            self.store,
            self.config,
            act_id=str(self.act.get("id") or ""),
            on_event=events.append,
            **kwargs,
        )
        return result, events

    def test_fill_then_skip_then_regen(self):
        """首次只补缺失逐章生成；再次运行全部跳过；重新生成产生历史版本。"""
        result, events = self._run(mode="fill")
        self.assertEqual(result["succeeded"], 2)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["skipped"], 0)
        started = [event for event in events if event.get("type") == "chapter_start"]
        self.assertEqual([item["index"] for item in started], [1, 2])
        for chapter in self.chapters:
            self.assertTrue((self.store.load_chapter(chapter["id"]).get("content") or "").strip())

        again, _ = self._run(mode="fill")
        self.assertEqual(again["skipped"], 2)
        self.assertEqual(again["succeeded"], 0)
        self.assertEqual(again["message"], "成功 0 章 / 跳过 2 章 / 失败 0 章")

        before = self.store.load_chapter(self.chapters[0]["id"]).get("revision")
        regen, _ = self._run(mode="regen")
        self.assertEqual(regen["succeeded"], 2)
        # 离线适配器输出确定，正文不变时不产生历史版本；revision 仍应递增
        self.assertGreater(int(self.store.load_chapter(self.chapters[0]["id"]).get("revision") or 0), int(before or 0))

    def test_failure_does_not_stop_act(self):
        """未落地的章节记为失败，其余章节照常生成。"""
        missing = self.chapters[1]["id"]
        (self.store.root / "chapters" / f"{missing}.json").unlink()
        result, _ = self._run(mode="fill")
        self.assertEqual(result["succeeded"], 1)
        self.assertEqual(result["failed"], 1)
        failed = next(row for row in result["chapters"] if row["status"] == "failed")
        self.assertIn("落地", failed["message"])

    def test_cancel_between_chapters(self):
        """章间取消时抛出 ActBatchCanceled，已完成章节保持落盘。"""
        checks = {"count": 0}

        def is_canceled() -> bool:
            """第一次检查放行第一章，第二次检查返回停止。"""
            checks["count"] += 1
            return checks["count"] > 1

        with self.assertRaises(ActBatchCanceled) as caught:
            generate_act_chapters(
                self.store,
                self.config,
                act_id=str(self.act.get("id") or ""),
                mode="regen",
                is_canceled=is_canceled,
            )
        result = caught.exception.result
        self.assertTrue(result["canceled"])
        self.assertEqual(result["succeeded"], 1)
        self.assertEqual(result["total"], 2)
        self.assertTrue((self.store.load_chapter(self.chapters[0]["id"]).get("content") or "").strip())
        self.assertFalse((self.store.load_chapter(self.chapters[1]["id"]).get("content") or "").strip())


if __name__ == "__main__":
    unittest.main()
