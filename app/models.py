from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def domain_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").removeprefix("www.")
    except ValueError:
        return ""


@dataclass
class Source:
    id: str
    title: str
    url: str
    domain: str
    snippet: str
    question: str = ""
    provider: str = "bing"
    fetched: bool = False
    chars: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "url": self.url,
            "domain": self.domain,
            "snippet": self.snippet,
            "question": self.question,
            "provider": self.provider,
            "fetched": self.fetched,
            "chars": self.chars,
        }


@dataclass
class SubQuestion:
    id: str
    question: str
    search_query: str
    rationale: str = ""
    kind: str = "plan"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "question": self.question,
            "search_query": self.search_query,
            "rationale": self.rationale,
            "kind": self.kind,
        }


@dataclass
class Note:
    question_id: str
    question: str
    summary: str
    key_points: list[str] = field(default_factory=list)
    source_ids: list[str] = field(default_factory=list)
    raw: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "question_id": self.question_id,
            "question": self.question,
            "summary": self.summary,
            "key_points": self.key_points,
            "source_ids": self.source_ids,
        }


@dataclass
class Critique:
    verdict: str
    gaps: list[str] = field(default_factory=list)
    followups: list[SubQuestion] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "gaps": self.gaps,
            "followups": [f.to_dict() for f in self.followups],
        }


class SourceRegistry:
    """Assigns stable S1..Sn ids to unique URLs across concurrent researchers."""

    def __init__(self) -> None:
        self._sources: dict[str, Source] = {}
        self._by_url: dict[str, str] = {}
        self._counter = 0
        self._lock = asyncio.Lock()

    async def add(
        self,
        *,
        title: str,
        url: str,
        snippet: str,
        question: str,
        provider: str,
    ) -> tuple[Source, bool]:
        key = url.split("#", 1)[0].rstrip("/")
        async with self._lock:
            existing = self._by_url.get(key)
            if existing:
                return self._sources[existing], False
            self._counter += 1
            sid = f"S{self._counter}"
            source = Source(
                id=sid,
                title=(title or domain_of(url) or url)[:220],
                url=url,
                domain=domain_of(url),
                snippet=(snippet or "")[:500],
                question=question,
                provider=provider,
            )
            self._sources[sid] = source
            self._by_url[key] = sid
            return source, True

    def get(self, sid: str) -> Source | None:
        return self._sources.get(sid)

    def all(self) -> list[Source]:
        return list(self._sources.values())

    def ids(self) -> list[str]:
        return list(self._sources.keys())

    def map_text(self, ids: list[str] | None = None) -> str:
        chosen = ids if ids is not None else self.ids()
        lines = []
        for sid in chosen:
            src = self._sources.get(sid)
            if src:
                lines.append(f"[{sid}] {src.title} — {src.url}")
        return "\n".join(lines)


class ResearchRun:
    """In-memory run state + publish/subscribe fan-out for SSE."""

    def __init__(self, run_id: str, query: str, *, deep: bool, mode: str, model: str) -> None:
        self.id = run_id
        self.query = query
        self.deep = deep
        self.mode = mode
        self.model = model
        self.status = "queued"
        self.error: str | None = None
        self.created_at = utc_now()
        self.started_at: str | None = None
        self.finished_at: str | None = None

        self.plan: list[SubQuestion] = []
        self.notes: list[Note] = []
        self.critique: Critique | None = None
        self.report: str = ""
        self.sources: list[dict[str, Any]] = []
        self.usage: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        self.stats: dict[str, Any] = {}

        self.events: list[dict[str, Any]] = []
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._seq = 0

    def emit(self, event_type: str, agent: str = "", message: str = "", **data: Any) -> dict[str, Any]:
        self._seq += 1
        event = {
            "seq": self._seq,
            "ts": utc_now(),
            "type": event_type,
            "agent": agent,
            "message": message,
            "data": data,
        }
        self.events.append(event)
        for queue in list(self._subscribers):
            queue.put_nowait(event)
        return event

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)

    def replayable_events(self) -> list[dict[str, Any]]:
        """Skip token deltas on replay; the final report is sent separately."""
        return [e for e in self.events if e["type"] != "token"]

    def snapshot(self, *, include_events: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "query": self.query,
            "deep": self.deep,
            "mode": self.mode,
            "model": self.model,
            "status": self.status,
            "error": self.error,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "plan": [q.to_dict() for q in self.plan],
            "notes": [n.to_dict() for n in self.notes],
            "report": self.report,
            "sources": self.sources,
            "usage": self.usage,
            "stats": self.stats,
        }
        if include_events:
            data["events"] = self.events
        return data

    def persisted(self) -> dict[str, Any]:
        data = self.snapshot(include_events=False)
        data["events"] = self.replayable_events()
        return data

    def set_sources(self, sources: list[Source]) -> None:
        self.sources = [s.to_dict() for s in sources]


def extract_json(text: str) -> dict[str, Any] | None:
    """Best-effort JSON object extraction from LLM output (handles code fences)."""
    if not text:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        import json

        value = json.loads(cleaned)
        return value if isinstance(value, dict) else None
    except (ValueError, TypeError):
        pass
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end > start:
        try:
            import json

            value = json.loads(cleaned[start : end + 1])
            return value if isinstance(value, dict) else None
        except (ValueError, TypeError):
            return None
    return None
