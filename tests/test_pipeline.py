from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.config import Settings
from app.models import ResearchRun
from app.orchestrator import Orchestrator
from app.store import RunStore
from app.tools.fetch import Page, PageFetcher


async def _fake_fetch(self: PageFetcher, url: str) -> Page:
    text = (
        "这是一段用于测试的公开资料，讨论多智能体系统的规划、检索、审阅与写作流程。"
        "资料内容包括架构设计、成本权衡、评测方法与落地风险，长度满足正文提取的下限要求。"
    ) * 6
    return Page(url=url, title="离线测试资料", text=text, ok=True)


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_mock_pipeline_end_to_end_offline(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings()
            settings.force_mode = "mock"
            settings.search_provider = "offline"
            settings.http_proxy = ""
            settings.max_subquestions = 3
            settings.fetch_top_k = 2

            store = RunStore(Path(tmp) / "runs")
            run = ResearchRun("rs_testpipeline", "多智能体系统如何落地", deep=False, mode="mock", model="demo")

            with patch.object(PageFetcher, "fetch", _fake_fetch):
                await Orchestrator(settings, store).execute(run)

            self.assertEqual(run.status, "done")
            self.assertTrue(run.report.startswith("## 摘要"))
            self.assertIn("## 参考来源", run.report)
            self.assertGreaterEqual(len(run.plan), 3)
            self.assertEqual(len(run.notes), len(run.plan) + len(run.critique.followups))
            self.assertGreater(len(run.sources), 0)
            self.assertGreater(run.stats["char_count"], 500)
            self.assertGreater(run.stats["cited_sources"], 0)
            self.assertEqual(run.stats["invalid_citation_ids"], [])
            self.assertGreater(run.stats["grounded_ratio"], 0)
            self.assertIn("duration_seconds", run.stats)

            event_types = {event["type"] for event in run.events}
            for expected in ("run_started", "stage", "question", "source", "note", "critique", "token", "report", "done"):
                self.assertIn(expected, event_types)
            tokens = [event for event in run.events if event["type"] == "token"]
            self.assertGreater(len(tokens), 0)

            saved = store.load("rs_testpipeline")
            self.assertIsNotNone(saved)
            assert saved is not None
            self.assertEqual(saved["status"], "done")
            self.assertNotIn("token", {event["type"] for event in saved["events"]})

    async def test_failed_subquestion_does_not_kill_run(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings()
            settings.force_mode = "mock"
            settings.search_provider = "offline"
            settings.http_proxy = ""
            settings.max_subquestions = 2
            settings.fetch_top_k = 1

            store = RunStore(Path(tmp) / "runs")
            run = ResearchRun("rs_testpartial", "部分失败测试", deep=False, mode="mock", model="demo")

            calls = {"n": 0}

            async def flaky_fetch(self: PageFetcher, url: str) -> Page:
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("模拟抓取失败")
                return await _fake_fetch(self, url)

            with patch.object(PageFetcher, "fetch", flaky_fetch):
                await Orchestrator(settings, store).execute(run)

            self.assertEqual(run.status, "done")
            self.assertIn("## 参考来源", run.report)
            warnings = [event for event in run.events if event["type"] == "warning"]
            self.assertTrue(any("抓取" in event["message"] or "跳过" in event["message"] for event in warnings))


if __name__ == "__main__":
    unittest.main()
