from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from ..config import Settings

MAX_BYTES = 2_500_000
BLOCKED_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1", "metadata.google.internal"}


class FetchError(RuntimeError):
    pass


@dataclass
class Page:
    url: str
    title: str
    text: str
    ok: bool = True
    error: str = ""


async def ensure_public_url(url: str) -> None:
    """Reject non-HTTP schemes, credentials and private-network destinations (SSRF guard)."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise FetchError("仅允许 http/https 链接")
    if parsed.username or parsed.password:
        raise FetchError("链接中不允许包含认证信息")
    host = (parsed.hostname or "").strip().lower()
    if not host or host in BLOCKED_HOSTS or host.endswith((".local", ".internal", ".localhost")):
        raise FetchError("非公开链接")
    try:
        infos = await asyncio.wait_for(
            asyncio.to_thread(socket.getaddrinfo, host, parsed.port or (443 if parsed.scheme == "https" else 80)),
            timeout=5,
        )
    except (socket.gaierror, OSError, asyncio.TimeoutError) as exc:
        raise FetchError(f"域名解析失败: {host}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved:
            raise FetchError("非公开网络地址")


class PageFetcher:
    def __init__(self, settings: Settings) -> None:
        self.max_chars = settings.max_chars_per_page
        self._cache: dict[str, Page] = {}
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(25.0, connect=12.0),
            follow_redirects=True,
            trust_env=not bool(settings.http_proxy),
            proxy=settings.http_proxy or None,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
        )

    async def fetch(self, url: str) -> Page:
        cached = self._cache.get(url)
        if cached is not None:
            return cached
        try:
            await ensure_public_url(url)
            raw, charset, content_type = await self._download(url)
        except FetchError as exc:
            page = Page(url=url, title=url, text="", ok=False, error=str(exc))
            self._cache[url] = page
            return page
        except Exception as exc:  # noqa: BLE001 - one bad page must not fail the run
            page = Page(url=url, title=url, text="", ok=False, error=f"{type(exc).__name__}: {exc}")
            self._cache[url] = page
            return page

        if "html" not in content_type and "text" not in content_type:
            page = Page(url=url, title=url, text="", ok=False, error=f"不支持的内容类型: {content_type}")
            self._cache[url] = page
            return page
        text = self._decode(raw, charset)
        title, body = self._extract(text)
        if len(body) < 80:
            page = Page(url=url, title=title or url, text="", ok=False, error="正文过短")
        else:
            page = Page(url=url, title=title or url, text=body[: self.max_chars], ok=True)
        self._cache[url] = page
        return page

    async def close(self) -> None:
        await self._http.aclose()

    async def _download(self, url: str) -> tuple[bytes, str, str]:
        async with self._http.stream("GET", url) as resp:
            if resp.status_code >= 400:
                raise FetchError(f"HTTP {resp.status_code}")
            final_host = (urlparse(str(resp.url)).hostname or "").lower()
            if final_host in BLOCKED_HOSTS:
                raise FetchError("重定向到非公开地址")
            content_type = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
            if content_type and "html" not in content_type and "text" not in content_type:
                raise FetchError(f"不支持的内容类型: {content_type}")
            charset = resp.charset_encoding or ""
            chunks: list[bytes] = []
            total = 0
            async for chunk in resp.aiter_bytes():
                total += len(chunk)
                if total > MAX_BYTES:
                    break
                chunks.append(chunk)
            return b"".join(chunks), charset, content_type or "text/html"

    @staticmethod
    def _decode(raw: bytes, charset: str) -> str:
        if not charset:
            meta = re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", raw[:4096], re.I)
            charset = meta.group(1).decode("ascii", errors="ignore") if meta else "utf-8"
        for enc in (charset, "utf-8", "gb18030"):
            try:
                return raw.decode(enc, errors="strict")
            except (LookupError, UnicodeDecodeError):
                continue
        return raw.decode("utf-8", errors="replace")

    @staticmethod
    def _extract(html_text: str) -> tuple[str, str]:
        soup = BeautifulSoup(html_text, "html.parser")
        for tag in soup(["script", "style", "noscript", "svg", "iframe", "header", "footer", "nav", "aside", "form", "button"]):
            tag.decompose()
        title = ""
        for selector in ("meta[property='og:title']", "meta[name='title']"):
            node = soup.select_one(selector)
            if node and node.get("content"):
                title = node["content"].strip()
                break
        if not title and soup.title:
            title = soup.title.get_text(strip=True)
        main = soup.select_one("article, main, [role='main'], .article-content, .post-content, #content")
        body = (main or soup.body or soup).get_text("\n", strip=True)
        body = re.sub(r"[ \t]+", " ", body)
        body = re.sub(r"\n{2,}", "\n", body).strip()
        return title[:220], body
