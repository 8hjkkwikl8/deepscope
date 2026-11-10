from __future__ import annotations

import unittest

from app.tools.citations import (
    append_sources,
    compute_stats,
    extract_citations,
    sanitize_citations,
    strip_generated_sources_section,
)


class CitationTests(unittest.TestCase):
    def test_extract_is_unique_and_ordered(self) -> None:
        self.assertEqual(extract_citations("结论 [S2]，补充 [S1][S2]，再引 [S10]。"), ["S2", "S1", "S10"])

    def test_sanitize_removes_unknown_ids(self) -> None:
        cleaned, invalid = sanitize_citations("结论甲 [S1] 与结论乙 [S9]，以及 [S2]。", {"S1", "S2"})
        self.assertEqual(invalid, ["S9"])
        self.assertNotIn("[S9]", cleaned)
        self.assertIn("[S1]", cleaned)
        self.assertNotIn(" ，", cleaned)

    def test_stats_ignore_sources_section(self) -> None:
        report = (
            "## 摘要\n\n"
            "这是一句有来源支撑的结论。[S1]\n\n"
            "这是一句没有来源的结论。\n\n"
            "## 参考来源\n\n"
            "- [S1] 示例 — https://example.com\n"
        )
        stats = compute_stats(report, {"S1", "S2"})
        self.assertEqual(stats["cited_sources"], 1)
        self.assertEqual(stats["valid_sources"], 2)
        self.assertEqual(stats["content_lines"], 2)
        self.assertEqual(stats["grounded_lines"], 1)
        self.assertEqual(stats["grounded_ratio"], 0.5)
        self.assertEqual(stats["source_coverage"], 0.5)

    def test_append_sources_escapes_brackets_in_titles(self) -> None:
        text = append_sources(
            "结论 [S1]",
            [{"id": "S1", "title": "报告 [草稿]", "url": "https://example.com/a", "domain": "example.com"}],
        )
        self.assertIn("## 参考来源", text)
        self.assertIn("（草稿）", text)
        self.assertIn("[S1]", text)

    def test_strip_generated_sources_section(self) -> None:
        report = "## 摘要\n\n正文。\n\n## 参考来源\n\n- 模型自己编的来源"
        self.assertEqual(strip_generated_sources_section(report), "## 摘要\n\n正文。")


if __name__ == "__main__":
    unittest.main()
