from __future__ import annotations

import asyncio
import re
from typing import Any

from ..config import Settings
from ..llm import LLMClient
from ..models import Note, ResearchRun, SourceRegistry, SubQuestion, extract_json
from ..tools.fetch import PageFetcher
from ..tools.search import SearchTool

SYSTEM_PROMPT = """你是 DeepScope 的检索研究 Agent（Researcher）。
你会看到某个子问题、若干条带编号的网页材料。请只依据材料提炼结论。

要求：
1. 不要编造材料中没有的数据、年份、公司或结论。
2. 如果材料之间矛盾，明确指出分歧，而不是强行统一。
3. summary 用 120-220 字回答问题；key_points 给出 2-4 条可核查要点。
4. source_ids 只列出真正支持这些结论的来源编号，例如 ["S1","S3"]。
5. 只输出 JSON：
{"summary":"...","key_points":["..."],"source_ids":["S1"]}"""


def _note_from_materials(
    subq: SubQuestion, sources: list[dict[str, Any]], reason: str = ""
) -> Note:
    usable = [s for s in sources if s.get("snippet") or s.get("fetched")]
    if not usable:
        return Note(
            question_id=subq.id,
            question=subq.question,
            summary="未检索到可用来源，该子问题暂无法形成有依据的结论。",
            key_points=[],
            source_ids=[],
        )
    lines = [f"- {s['title']}（{s['domain'] or s['url']}）：{(s.get('snippet') or '')[:140]}" for s in usable[:3]]
    return Note(
        question_id=subq.id,
        question=subq.question,
        summary=("模型提炼失败，以下为来源摘要，供写作 Agent 参考：" + reason)[:260],
        key_points=[line.lstrip("- ")[:220] for line in lines],
        source_ids=[s["id"] for s in usable[:3]],
    )


def _build_material_block(sources: list[dict[str, Any]], max_chars: int) -> str:
    blocks: list[str] = []
    for src in sources:
        body = src.get("text") or src.get("snippet") or ""
        body = re.sub(r"\n{2,}", "\n", body)[:max_chars]
        blocks.append(
            f"[{src['id']}] {src['title']}\nURL: {src['url']}\n{body}"
        )
    return "\n\n---\n\n".join(blocks)


async def research_subquestion(
    *,
    llm: LLMClient,
    run: ResearchRun,
    search: SearchTool,
    fetcher: PageFetcher,
    registry: SourceRegistry,
    subq: SubQuestion,
    settings: Settings,
    fetch_sem: asyncio.Semaphore,
) -> Note:
    provider = settings.search_provider
    run.emit(
        "search",
        agent="researcher",
        message=f"检索 {subq.id}：{subq.search_query}",
        question_id=subq.id,
        query=subq.search_query,
    )
    results, provider_used, warning = await search.search(subq.search_query, limit=max(6, settings.fetch_top_k + 3))
    if warning:
        run.emit("warning", agent="researcher", message=warning, question_id=subq.id)

    sources: list[dict[str, Any]] = []
    domain_counts: dict[str, int] = {}
    target_sources = settings.fetch_top_k + 2
    for result in results:
        domain = result.url.split("/")[2].lower() if "://" in result.url else ""
        if domain_counts.get(domain, 0) >= 2:
            continue
        domain_counts[domain] = domain_counts.get(domain, 0) + 1
        source, is_new = await registry.add(
            title=result.title,
            url=result.url,
            snippet=result.snippet,
            question=subq.id,
            provider=provider_used,
        )
        if is_new:
            run.emit("source", agent="researcher", message=f"发现来源 {source.id}：{source.title}", source=source.to_dict())
        sources.append(
            {
                "id": source.id,
                "title": source.title,
                "url": source.url,
                "domain": source.domain,
                "snippet": source.snippet,
                "fetched": source.fetched,
                "text": "",
            }
        )
        if len(sources) >= target_sources:
            break

    # Fetch the top results (dedupe by URL already handled by registry).
    to_fetch = sources[: settings.fetch_top_k]
    pages = []
    for src in to_fetch:
        async with fetch_sem:
            page = await fetcher.fetch(src["url"])
        pages.append((src, page))

    for src, page in pages:
        if page.ok:
            src["fetched"] = True
            src["text"] = page.text
            src["chars"] = len(page.text)
            source = registry.get(src["id"])
            if source:
                source.fetched = True
                source.chars = len(page.text)
            run.emit(
                "reading",
                agent="researcher",
                message=f"阅读 {src['id']} · {len(page.text):,} 字符",
                source_id=src["id"],
                url=src["url"],
                ok=True,
                chars=len(page.text),
            )
        else:
            run.emit(
                "reading",
                agent="researcher",
                message=f"跳过 {src['id']}：{page.error}",
                source_id=src["id"],
                url=src["url"],
                ok=False,
                chars=0,
                error=page.error,
            )

    material = _build_material_block(sources, settings.max_chars_per_page)
    available_ids = [src["id"] for src in sources]
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"研究子问题：{subq.question}\n"
                f"检索意图：{subq.search_query}\n"
                f"可用来源编号：{', '.join(available_ids) or '无'}\n\n"
                f"材料如下：\n\n{material[:24000]}"
            ),
        },
    ]

    try:
        raw = await llm.chat(messages, purpose="researcher", temperature=0.2, max_tokens=1000, json_mode=True)
        data = extract_json(raw) or {}
        summary = str(data.get("summary") or "").strip()
        key_points = [str(p).strip()[:240] for p in (data.get("key_points") or []) if str(p).strip()]
        used_ids = [sid for sid in (data.get("source_ids") or []) if sid in available_ids]
        if len(summary) < 20:
            raise ValueError("summary 过短")
        note = Note(
            question_id=subq.id,
            question=subq.question,
            summary=summary[:800],
            key_points=key_points[:5],
            source_ids=list(dict.fromkeys(used_ids))[:6],
            raw=raw,
        )
    except Exception as exc:  # noqa: BLE001 - degrade to excerpt notes
        run.emit("warning", agent="researcher", message=f"{subq.id} 模型提炼失败，使用来源摘要兜底：{exc}")
        note = _note_from_materials(subq, sources, str(exc))

    run.emit(
        "note",
        agent="researcher",
        message=f"完成 {subq.id} 的笔记 · 引用 {len(note.source_ids)} 个来源",
        note=note.to_dict(),
    )
    return note
