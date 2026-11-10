from __future__ import annotations

import asyncio
import html
import re
from dataclasses import dataclass
from urllib.parse import quote_plus, urljoin

import httpx
from bs4 import BeautifulSoup

from ..config import Settings

_CJK_GAP_RE = re.compile(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])")


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str
    provider: str


class SearchError(RuntimeError):
    pass


def _clean_text(value: str) -> str:
    value = _CJK_GAP_RE.sub("", value or "")
    return re.sub(r"\s+", " ", value).strip()


def _looks_relevant(query: str, results: list[SearchResult]) -> bool:
    terms = [t for t in re.split(r"\s+", query) if len(t) >= 2]
    if not terms:
        return True
    corpus = " ".join(f"{r.title} {r.snippet}" for r in results)
    hits = sum(1 for term in terms if term in corpus)
    return hits >= min(2, max(1, len(terms) // 2))


class SearchTool:
    """Keyless-first search with a pluggable provider chain.

    Order: Tavily (if key) -> 360 Search -> Sogou -> Bing RSS -> offline demo.
    360 result cards expose canonical URLs in `data-mdurl`, so citations do not
    depend on redirect resolution (faster, and far less likely to be captcha'd).
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.provider = settings.search_provider
        self._cache: dict[str, list[SearchResult]] = {}
        self._so_lock = asyncio.Semaphore(2)
        self._sogou_lock = asyncio.Semaphore(1)
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=12.0),
            follow_redirects=True,
            trust_env=not bool(settings.http_proxy),
            proxy=settings.http_proxy or None,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
                ),
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
        )

    async def search(self, query: str, limit: int = 6) -> tuple[list[SearchResult], str, str | None]:
        """Return (results, provider_used, warning)."""
        key = f"{self.provider}:{query}:{limit}"
        if key in self._cache:
            cached = self._cache[key]
            return cached, cached[0].provider if cached else self.provider, None

        warning: str | None = None
        results: list[SearchResult] = []
        used = self.provider

        try:
            if self.provider == "offline":
                results = self._offline(query, limit)
                used = "offline"
            elif self.provider == "tavily":
                if not self.settings.tavily_api_key:
                    warning = "未配置 TAVILY_API_KEY，已回退到 360 搜索"
                    results = await self._so360(query, limit)
                    used = "so360"
                else:
                    results = await self._tavily(query, limit)
                    used = "tavily"
            elif self.provider in {"so360", "360", "so"}:
                results = await self._so360(query, limit)
                used = "so360"
            elif self.provider == "sogou":
                results = await self._sogou(query, limit)
                used = "sogou"
            elif self.provider == "bing":
                results = await self._bing(query, limit)
                used = "bing"
            else:  # auto
                if self.settings.tavily_api_key:
                    try:
                        results = await self._tavily(query, limit)
                        used = "tavily"
                    except Exception as exc:  # noqa: BLE001
                        warning = f"Tavily 检索失败，回退 360：{type(exc).__name__}"
                if not results:
                    try:
                        results = await self._so360(query, limit)
                        used = "so360"
                    except Exception as exc:  # noqa: BLE001
                        warning = f"360 检索失败，回退搜狗：{type(exc).__name__}"
                if not results:
                    try:
                        results = await self._sogou(query, limit)
                        used = "sogou"
                    except Exception as exc:  # noqa: BLE001
                        warning = f"搜狗检索失败，回退 Bing：{type(exc).__name__}"
                if not results:
                    try:
                        results = await self._bing(query, limit)
                        used = "bing"
                    except Exception as exc:  # noqa: BLE001
                        warning = f"Bing 检索失败，已切换离线演示来源：{type(exc).__name__}"
        except Exception as exc:  # noqa: BLE001 - provider failures must degrade gracefully
            warning = f"{self.provider} 检索失败（{type(exc).__name__}），已切换离线演示来源"
            results = []
            used = "offline"

        if not results:
            results = self._offline(query, limit)
            used = "offline"
            warning = warning or "未检索到公开结果，已使用离线演示来源"

        results = results[:limit]
        self._cache[key] = results
        return results, used, warning

    async def close(self) -> None:
        await self._http.aclose()

    # ── providers ────────────────────────────────────────────────
    async def _so360(self, query: str, limit: int) -> list[SearchResult]:
        async with self._so_lock:
            resp = await self._http.get("https://www.so.com/s", params={"q": query})
        if resp.status_code != 200:
            raise SearchError(f"360 HTTP {resp.status_code}")
        soup = BeautifulSoup(resp.text, "html.parser")
        nodes = soup.select("li.res-list")
        if not nodes:
            if "验证码" in resp.text or "安全验证" in resp.text:
                raise SearchError("360 触发安全验证")
            raise SearchError("360 未返回结果")

        results: list[SearchResult] = []
        seen: set[str] = set()
        for node in nodes:
            link = node.select_one("h3 a")
            if not link:
                continue
            title = _clean_text(link.get_text(" ", strip=True))
            url = (
                link.get("data-mdurl")
                or link.get("data-cache")
                or link.get("href")
                or ""
            ).strip()
            if not url.startswith(("http://", "https://")):
                continue
            if "so.com" in url or "360.cn" in url or "360kan.com" in url:
                continue
            if len(title) < 4 or url in seen:
                continue
            snippet_node = node.select_one(".res-desc, .res-rich, .content, .mh-desc, p")
            snippet = _clean_text(snippet_node.get_text(" ", strip=True)) if snippet_node else ""
            seen.add(url)
            results.append(SearchResult(title=title[:220], url=url, snippet=snippet[:400], provider="so360"))
            if len(results) >= limit + 2:
                break
        if not results:
            raise SearchError("360 结果解析为空")
        if not _looks_relevant(query, results):
            raise SearchError("360 结果与查询相关性不足")
        return results[:limit]

    async def _sogou(self, query: str, limit: int) -> list[SearchResult]:
        async with self._sogou_lock:
            resp = await self._http.get("https://www.sogou.com/web", params={"query": query})
            await asyncio.sleep(0.8)
        if resp.status_code != 200:
            raise SearchError(f"Sogou HTTP {resp.status_code}")
        soup = BeautifulSoup(resp.text, "html.parser")
        nodes = soup.select("div.vrwrap, div.rb")
        if not nodes:
            if "验证码" in resp.text or "antispider" in str(resp.url):
                raise SearchError("搜狗触发验证码")
            raise SearchError("搜狗未返回结果")

        rows: list[tuple[str, str, str]] = []
        for node in nodes:
            link = node.select_one("h3 a")
            if not link:
                continue
            title = _clean_text(link.get_text(" ", strip=True))
            href = (link.get("href") or "").strip()
            snippet_node = node.select_one(".str_info, .text-layout, .fz-mid, .space-txt, .str-text-info")
            snippet = _clean_text(snippet_node.get_text(" ", strip=True)) if snippet_node else ""
            if len(title) < 4 or not href:
                continue
            rows.append((title, href, snippet))
            if len(rows) >= max(limit + 2, 6):
                break
        if not rows:
            raise SearchError("搜狗结果解析为空")

        results: list[SearchResult] = []
        for title, href, snippet in rows[:limit]:
            try:
                url = await self._resolve_sogou_url(href)
            except Exception:  # noqa: BLE001
                continue
            if not url.startswith(("http://", "https://")) or "sogou.com" in url:
                continue
            results.append(SearchResult(title=title, url=url, snippet=snippet[:400], provider="sogou"))
        if not results:
            raise SearchError("搜狗结果均无法解析出原文链接")
        if not _looks_relevant(query, results):
            raise SearchError("搜狗结果与查询相关性不足")
        return results

    async def _resolve_sogou_url(self, href: str) -> str:
        if href.startswith("//"):
            href = "https:" + href
        url = urljoin("https://www.sogou.com", href)
        if "sogou.com/link" not in url:
            return url
        resp = await self._http.get(url)
        text = resp.text[:5000]
        match = re.search(r'window\.location\.replace\("([^"]+)"\)', text)
        if not match:
            match = re.search(r"URL='([^']+)'", text)
        return html.unescape(match.group(1)) if match else ""

    async def _bing(self, query: str, limit: int) -> list[SearchResult]:
        url = f"https://www.bing.com/search?q={quote_plus(query)}&format=rss"
        resp = await self._http.get(url)
        if resp.status_code != 200:
            raise SearchError(f"Bing HTTP {resp.status_code}")
        results = self._parse_rss(resp.content, limit)
        if not _looks_relevant(query, results):
            raise SearchError("Bing 对中文查询分词效果差，结果不相关")
        return results

    def _parse_rss(self, raw: bytes, limit: int) -> list[SearchResult]:
        import xml.etree.ElementTree as ET

        try:
            root = ET.fromstring(raw)
        except ET.ParseError as exc:
            raise SearchError(f"RSS 解析失败: {exc}") from exc
        results: list[SearchResult] = []
        for item in root.findall(".//item"):
            title = html.unescape((item.findtext("title") or "").strip())
            link = (item.findtext("link") or "").strip()
            description = item.findtext("description") or ""
            snippet = BeautifulSoup(html.unescape(description), "html.parser").get_text(" ", strip=True)
            snippet = _clean_text(snippet)
            if title and link.startswith(("http://", "https://")):
                results.append(
                    SearchResult(title=title, url=link, snippet=snippet[:400], provider="bing")
                )
            if len(results) >= limit:
                break
        if not results:
            raise SearchError("Bing 未返回结果")
        return results

    async def _tavily(self, query: str, limit: int) -> list[SearchResult]:
        resp = await self._http.post(
            "https://api.tavily.com/search",
            json={
                "api_key": self.settings.tavily_api_key,
                "query": query,
                "max_results": limit,
                "search_depth": "basic",
                "include_answer": False,
            },
        )
        if resp.status_code != 200:
            raise SearchError(f"Tavily HTTP {resp.status_code}")
        body = resp.json()
        results = [
            SearchResult(
                title=(row.get("title") or row.get("url") or "")[:220],
                url=row.get("url") or "",
                snippet=(row.get("content") or "")[:400],
                provider="tavily",
            )
            for row in body.get("results", [])
            if row.get("url")
        ]
        if not results:
            raise SearchError("Tavily 未返回结果")
        return results

    def _offline(self, query: str, limit: int) -> list[SearchResult]:
        demo = [
            ("演示来源：概念与现状综述", f"https://example.org/demo/overview?q={quote_plus(query)}"),
            ("演示来源：技术架构与实现路径", f"https://example.org/demo/architecture?q={quote_plus(query)}"),
            ("演示来源：落地案例与效果评估", f"https://example.org/demo/cases?q={quote_plus(query)}"),
            ("演示来源：风险、争议与趋势", f"https://example.org/demo/risks?q={quote_plus(query)}"),
            ("演示来源：评测方法与开源工具", f"https://example.org/demo/evaluation?q={quote_plus(query)}"),
        ]
        return [
            SearchResult(
                title=title,
                url=url,
                snippet=f"离线演示数据：关于「{query[:60]}」的{title.split('：')[-1]}。",
                provider="offline",
            )
            for title, url in demo[:limit]
        ]
