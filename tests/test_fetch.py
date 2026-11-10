from __future__ import annotations

import unittest

from app.tools.fetch import FetchError, PageFetcher, ensure_public_url


class FetchGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_dangerous_schemes(self) -> None:
        for url in ("file:///C:/Windows/win.ini", "ftp://example.com/data", "javascript:alert(1)"):
            with self.subTest(url=url), self.assertRaises(FetchError):
                await ensure_public_url(url)

    async def test_rejects_credentials(self) -> None:
        with self.assertRaises(FetchError):
            await ensure_public_url("https://user:secret@example.com/page")

    async def test_rejects_localhost_and_private_ips(self) -> None:
        for url in (
            "http://localhost:8080/admin",
            "http://127.0.0.1/",
            "http://10.1.2.3/",
            "http://192.168.1.1/",
            "http://169.254.169.254/latest/meta-data",
            "http://[::1]/",
        ):
            with self.subTest(url=url), self.assertRaises(FetchError):
                await ensure_public_url(url)


class ExtractTests(unittest.TestCase):
    def test_extract_prefers_article_and_drops_navigation(self) -> None:
        html = """
        <html><head>
          <title>Fallback title</title>
          <meta property="og:title" content="报告标题">
          <style>.x{color:red}</style>
        </head><body>
          <nav>导航菜单</nav>
          <article>
            <h1>正文标题</h1>
            <p>第一段正文，包含足够多的字符用于提取。</p>
            <p>第二段正文。</p>
          </article>
          <footer>版权信息</footer>
          <script>alert(1)</script>
        </body></html>
        """
        title, body = PageFetcher._extract(html)
        self.assertEqual(title, "报告标题")
        self.assertIn("正文标题", body)
        self.assertIn("第一段正文", body)
        for removed in ("导航菜单", "版权信息", "alert(1)", "color:red"):
            self.assertNotIn(removed, body)

    def test_decode_falls_back_to_gb18030(self) -> None:
        raw = "中文正文".encode("gb18030")
        self.assertEqual(PageFetcher._decode(raw, "gb18030"), "中文正文")
        self.assertEqual(PageFetcher._decode(raw, "not-a-charset"), "中文正文")


if __name__ == "__main__":
    unittest.main()
