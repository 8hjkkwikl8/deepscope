from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.models import ResearchRun, SourceRegistry, extract_json
from app.store import RunStore


class ModelTests(unittest.IsolatedAsyncioTestCase):
    async def test_registry_deduplicates_urls_and_assigns_ids(self) -> None:
        registry = SourceRegistry()
        first, created = await registry.add(
            title="A", url="https://example.com/x#part", snippet="s", question="q1", provider="offline"
        )
        duplicate, created_again = await registry.add(
            title="B", url="https://example.com/x/", snippet="s", question="q2", provider="offline"
        )
        second, _ = await registry.add(
            title="C", url="https://example.org/y", snippet="s", question="q3", provider="offline"
        )
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first.id, "S1")
        self.assertEqual(duplicate.id, "S1")
        self.assertEqual(second.id, "S2")
        self.assertEqual(registry.ids(), ["S1", "S2"])
        self.assertIn("[S1] A — https://example.com/x#part", registry.map_text())

    async def test_registry_is_safe_under_concurrency(self) -> None:
        import asyncio

        registry = SourceRegistry()

        async def add(i: int):
            return await registry.add(
                title=f"T{i}",
                url=f"https://example.com/{i}",
                snippet="",
                question="q",
                provider="offline",
            )

        await asyncio.gather(*(add(i) for i in range(20)))
        self.assertEqual(len(registry.ids()), 20)
        self.assertEqual(len(set(registry.ids())), 20)

    def test_extract_json_handles_fences_and_noise(self) -> None:
        self.assertEqual(extract_json('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(extract_json('前言 {"a": 2, "b": [1]} 后记'), {"a": 2, "b": [1]})
        self.assertIsNone(extract_json("no json here"))

    def test_replayable_events_skip_tokens(self) -> None:
        run = ResearchRun("rs_test1", "q", deep=False, mode="mock", model="demo")
        run.emit("stage", stage="planning", progress=6)
        run.emit("token", text="a")
        run.emit("token", text="b")
        run.emit("report", report="done")
        self.assertEqual([e["type"] for e in run.replayable_events()], ["stage", "report"])
        self.assertEqual(len(run.events), 4)

    def test_store_roundtrip_and_list(self) -> None:
        with TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp))
            run = ResearchRun("rs_store1", "持久化测试", deep=True, mode="mock", model="demo")
            run.status = "done"
            run.report = "## 摘要\n\n内容 [S1]"
            run.stats = {"char_count": 10}
            run.emit("done")
            store.save(run)

            loaded = store.load("rs_store1")
            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(loaded["status"], "done")
            self.assertIn("摘要", loaded["report"])
            self.assertEqual(loaded["events"][0]["type"], "done")

            self.assertIsNone(store.load("../escape"))
            listing = store.list()
            self.assertEqual(listing[0]["id"], "rs_store1")
            self.assertIn("摘要", listing[0]["report_excerpt"])


if __name__ == "__main__":
    unittest.main()
