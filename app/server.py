from __future__ import annotations

import asyncio
import copy
import json
import secrets
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__
from .config import settings
from .llm import LLMClient
from .models import ResearchRun
from .orchestrator import Orchestrator
from .store import RunStore

store = RunStore(settings.data_dir)
app = FastAPI(title="DeepScope", version=__version__, docs_url="/api/docs", redoc_url=None)


class RunManager:
    def __init__(self) -> None:
        self.runs: dict[str, ResearchRun] = {}
        self.tasks: dict[str, asyncio.Task[None]] = {}

    def create(self, query: str, deep: bool) -> ResearchRun:
        run_id = "rs_" + secrets.token_hex(6)
        run = ResearchRun(
            run_id,
            query.strip(),
            deep=deep,
            mode=settings.mode,
            model=settings.llm_model if settings.mode == "live" else "demo-planner-v1",
        )
        self.runs[run_id] = run
        task = asyncio.create_task(Orchestrator(settings, store).execute(run))
        self.tasks[run_id] = task
        task.add_done_callback(lambda _t, rid=run_id: self.tasks.pop(rid, None))
        return run

    def cancel(self, run_id: str) -> bool:
        task = self.tasks.get(run_id)
        if not task or task.done():
            return False
        task.cancel()
        return True

    def snapshot(self, run_id: str) -> dict[str, Any] | None:
        run = self.runs.get(run_id)
        if run:
            return run.snapshot(include_events=False)
        return store.load(run_id)


manager = RunManager()


class ResearchRequest(BaseModel):
    query: str = Field(min_length=2, max_length=500)
    deep: bool = False


class SettingsRequest(BaseModel):
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    search_provider: str | None = None
    tavily_api_key: str | None = None


def _sse(event: dict[str, Any]) -> str:
    payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    return f"id: {event.get('seq', 0)}\ndata: {payload}\n\n"


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "version": __version__, "mode": settings.mode}


@app.get("/api/config")
async def config() -> dict[str, Any]:
    return {
        **settings.public_dict(),
        "version": __version__,
        "docs": "/api/docs",
        "samples": [
            "多智能体系统在企业知识工作中的落地路径与 ROI 评估",
            "LangGraph、AutoGen 与 CrewAI：2026 年 AI Agent 框架怎么选",
            "RAG 与长上下文模型在专业文档问答中的取舍",
            "如何为企业级 AI 应用设计引用溯源与幻觉评测体系",
        ],
    }


@app.post("/api/settings")
async def update_settings(body: SettingsRequest) -> dict[str, Any]:
    provider = (body.search_provider or "").strip().lower()
    if provider and provider not in {"auto", "so360", "360", "sogou", "bing", "tavily", "offline"}:
        raise HTTPException(status_code=400, detail="搜索源只能是 auto / so360 / sogou / bing / tavily / offline")

    candidate = copy.copy(settings)
    candidate.apply_llm(body.base_url, body.api_key, body.model)
    candidate.apply_search(body.search_provider, body.tavily_api_key)
    if candidate.llm_base_url and not candidate.llm_base_url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="Base URL 必须以 http:// 或 https:// 开头")

    tested = False
    if candidate.mode == "live" and body.api_key:
        client = LLMClient(candidate)
        try:
            await client.chat(
                [
                    {"role": "system", "content": "只回复 OK"},
                    {"role": "user", "content": "ping"},
                ],
                temperature=0,
                max_tokens=8,
            )
            tested = True
        except Exception as exc:  # noqa: BLE001 - report connection failure to the UI
            raise HTTPException(status_code=400, detail=f"连接测试失败：{exc}") from exc
        finally:
            await client.close()

    settings.apply_llm(body.base_url, body.api_key, body.model)
    settings.apply_search(body.search_provider, body.tavily_api_key)
    return {**settings.public_dict(), "tested": tested}


@app.post("/api/research")
async def create_research(body: ResearchRequest) -> dict[str, Any]:
    query = body.query.strip()
    if len(query) < 2:
        raise HTTPException(status_code=400, detail="研究问题太短")
    run = manager.create(query, body.deep)
    return {"run_id": run.id, "mode": run.mode, "model": run.model, "status": run.status}


@app.get("/api/runs")
async def list_runs(limit: int = 20) -> dict[str, Any]:
    active = [
        {
            "id": run.id,
            "query": run.query,
            "status": run.status,
            "mode": run.mode,
            "model": run.model,
            "created_at": run.created_at,
            "finished_at": run.finished_at,
            "sources": len(run.sources),
            "stats": run.stats,
        }
        for run in manager.runs.values()
        if run.status in {"queued", "running"}
    ]
    history = [item for item in store.list(limit=limit) if item["id"] not in manager.tasks]
    merged = active + [item for item in history if item["id"] not in {a["id"] for a in active}]
    return {"runs": merged[:limit]}


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str) -> dict[str, Any]:
    snapshot = manager.snapshot(run_id)
    if not snapshot:
        raise HTTPException(status_code=404, detail="任务不存在")
    run = manager.runs.get(run_id)
    if run:
        snapshot["events"] = run.replayable_events()
    return snapshot


@app.get("/api/runs/{run_id}/events")
async def run_events(run_id: str, request: Request) -> StreamingResponse:
    run = manager.runs.get(run_id)
    if not run:
        snapshot = store.load(run_id)
        if not snapshot:
            raise HTTPException(status_code=404, detail="任务不存在")

        async def replay():
            yield "retry: 2500\n\n"
            last_seq = 0
            for event in snapshot.get("events", []):
                last_seq = max(last_seq, event.get("seq", 0))
                yield _sse(event)
            yield _sse({"seq": last_seq + 1, "type": "stream_end", "agent": "system", "message": "", "data": {}})

        return StreamingResponse(
            replay(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        )

    queue = run.subscribe()

    async def generator():
        last_seq = 0
        try:
            yield "retry: 2500\n\n"
            for event in run.replayable_events():
                last_seq = max(last_seq, event["seq"])
                yield _sse(event)
            if run.status in {"done", "error", "cancelled"}:
                yield _sse({"seq": last_seq + 1, "type": "stream_end", "agent": "system", "message": "", "data": {}})
                return
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=12)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                if event["type"] != "token" and event["seq"] <= last_seq:
                    continue
                last_seq = max(last_seq, event["seq"])
                yield _sse(event)
                if event["type"] in {"done", "error", "cancelled"}:
                    break
        finally:
            run.unsubscribe(queue)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/runs/{run_id}/cancel")
async def cancel_run(run_id: str) -> dict[str, Any]:
    if not manager.cancel(run_id):
        raise HTTPException(status_code=409, detail="任务已结束或不存在")
    return {"ok": True}


@app.get("/api/runs/{run_id}/report.md")
async def download_report(run_id: str) -> Response:
    snapshot = manager.snapshot(run_id)
    if not snapshot or not snapshot.get("report"):
        raise HTTPException(status_code=404, detail="报告尚未生成")
    return Response(
        content=snapshot["report"],
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="deepscope-{run_id}.md"',
        },
    )


app.mount("/static", StaticFiles(directory=str(settings.web_dir)), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(settings.web_dir / "index.html")


@app.get("/favicon.ico")
async def favicon() -> RedirectResponse:
    return RedirectResponse(url="/static/favicon.svg")
