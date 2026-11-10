from __future__ import annotations

import asyncio
import time
from typing import Any

from .agents.critic import review_notes
from .agents.planner import make_plan
from .agents.researcher import research_subquestion
from .agents.writer import write_report
from .config import Settings
from .llm import LLMClient
from .models import Critique, Note, ResearchRun, SourceRegistry, SubQuestion, utc_now
from .store import RunStore
from .tools.citations import (
    append_sources,
    compute_stats,
    sanitize_citations,
    strip_generated_sources_section,
)
from .tools.fetch import PageFetcher
from .tools.search import SearchTool


class Orchestrator:
    def __init__(self, settings: Settings, store: RunStore) -> None:
        self.settings = settings
        self.store = store

    async def execute(self, run: ResearchRun) -> None:
        started = time.perf_counter()
        llm = LLMClient(self.settings)
        search = SearchTool(self.settings)
        fetcher = PageFetcher(self.settings)
        registry = SourceRegistry()
        run.status = "running"
        run.started_at = utc_now()
        run.emit(
            "run_started",
            agent="system",
            message=f"研究任务启动 · {run.mode == 'mock' and '演示模式' or run.model}",
            mode=run.mode,
            model=run.model,
            query=run.query,
        )
        if run.mode == "mock":
            run.emit(
                "warning",
                agent="system",
                message="当前未配置模型 API Key，使用内置演示模型；搜索与网页抓取仍会真实执行，报告内容仅用于展示流程。",
            )

        try:
            # 1. Plan ────────────────────────────────────────────
            run.emit("stage", agent="planner", message="拆解研究问题", stage="planning", label="规划", progress=6)
            run.emit("agent_status", agent="planner", status="thinking", detail="正在拆解研究问题")
            max_questions = self.settings.max_subquestions + (1 if run.deep else 0)
            plan = await make_plan(llm, run, run.query, min(5, max_questions))
            run.plan = plan
            for question in plan:
                run.emit(
                    "question",
                    agent="planner",
                    message=question.question,
                    question=question.to_dict(),
                )
            run.emit("agent_status", agent="planner", status="done", detail=f"拆出 {len(plan)} 个子问题")

            # 2. Research ────────────────────────────────────────
            run.emit("stage", agent="researcher", message="并行检索与阅读", stage="research", label="研究", progress=22)
            run.emit("agent_status", agent="researcher", status="thinking", detail=f"{len(plan)} 个研究员并行工作")
            research_sem = asyncio.Semaphore(3)
            fetch_sem = asyncio.Semaphore(4)

            async def safe_research(subq: SubQuestion) -> Note:
                async with research_sem:
                    try:
                        return await research_subquestion(
                            llm=llm,
                            run=run,
                            search=search,
                            fetcher=fetcher,
                            registry=registry,
                            subq=subq,
                            settings=self.settings,
                            fetch_sem=fetch_sem,
                        )
                    except Exception as exc:  # noqa: BLE001 - isolate a single subquestion
                        run.emit(
                            "warning",
                            agent="researcher",
                            message=f"{subq.id} 研究失败：{type(exc).__name__}: {exc}",
                        )
                        return Note(
                            question_id=subq.id,
                            question=subq.question,
                            summary="该子问题在执行过程中失败，未形成结论。",
                            key_points=[],
                            source_ids=[],
                        )

            notes = await asyncio.gather(*(safe_research(q) for q in plan))
            run.notes = list(notes)
            run.set_sources(registry.all())
            run.emit(
                "agent_status",
                agent="researcher",
                status="done",
                detail=f"完成 {len(notes)} 个问题 · {len(registry.all())} 个来源",
            )

            # 3. Review ──────────────────────────────────────────
            run.emit("stage", agent="critic", message="审阅证据缺口", stage="review", label="审阅", progress=62)
            run.emit("agent_status", agent="critic", status="thinking", detail="检查证据缺口与反方观点")
            critique = await review_notes(
                llm,
                run,
                run.query,
                notes,
                registry.ids(),
                max_followups=2 if run.deep else 1,
            )
            run.critique = critique
            run.emit(
                "critique",
                agent="critic",
                message=critique.verdict,
                verdict=critique.verdict,
                gaps=critique.gaps,
                followups=[f.to_dict() for f in critique.followups],
            )
            if critique.followups:
                run.emit(
                    "agent_status",
                    agent="critic",
                    status="done",
                    detail=f"发现 {len(critique.gaps)} 个缺口，追加 {len(critique.followups)} 个问题",
                )
                run.emit("agent_status", agent="researcher", status="thinking", detail="执行审阅追加检索")
                for extra in critique.followups:
                    run.emit("question", agent="critic", message=extra.question, question=extra.to_dict())
                extra_notes = await asyncio.gather(*(safe_research(q) for q in critique.followups))
                run.notes.extend(extra_notes)
                run.set_sources(registry.all())
                run.emit("agent_status", agent="researcher", status="done", detail="补充检索完成")
            else:
                run.emit("agent_status", agent="critic", status="done", detail="证据覆盖充分，无需补充")

            # 4. Write ───────────────────────────────────────────
            run.emit("stage", agent="writer", message="撰写研究简报", stage="writing", label="写作", progress=80)
            run.emit("agent_status", agent="writer", status="thinking", detail="基于笔记流式撰写")
            report = await write_report(
                llm=llm,
                run=run,
                query=run.query,
                notes=run.notes,
                critique=critique,
                registry=registry,
                deep=run.deep,
            )
            report = strip_generated_sources_section(report)
            valid_ids = set(registry.ids())
            cleaned, invalid_ids = sanitize_citations(report, valid_ids)
            stats = compute_stats(cleaned, valid_ids)
            stats["invalid_citation_ids"] = invalid_ids
            stats["source_count"] = len(registry.all())
            final_report = append_sources(cleaned, [s.to_dict() for s in registry.all()])
            run.report = final_report
            run.stats = stats
            run.set_sources(registry.all())
            run.usage = llm.usage_dict()
            run.emit("citation_check", agent="system", message="引用校验完成", stats=stats)
            run.emit(
                "report",
                agent="writer",
                message="研究简报生成完成",
                report=final_report,
                stats=stats,
            )
            run.emit("agent_status", agent="writer", status="done", detail=f"{stats.get('char_count', 0):,} 字 · 引用 {stats.get('cited_sources', 0)} 个来源")

            # 5. Done ────────────────────────────────────────────
            run.status = "done"
            run.finished_at = utc_now()
            elapsed = round(time.perf_counter() - started, 1)
            run.stats["duration_seconds"] = elapsed
            run.emit(
                "done",
                agent="system",
                message=f"研究完成 · 用时 {elapsed}s",
                duration_seconds=elapsed,
                stats=run.stats,
                usage=run.usage,
            )
            self.store.save(run)
        except asyncio.CancelledError:
            run.status = "cancelled"
            run.finished_at = utc_now()
            run.emit("cancelled", agent="system", message="任务已取消")
            self.store.save(run)
            raise
        except Exception as exc:  # noqa: BLE001 - surface a clean error to the UI
            run.status = "error"
            run.error = f"{type(exc).__name__}: {exc}"
            run.finished_at = utc_now()
            run.emit("error", agent="system", message=f"研究失败：{run.error}", detail=run.error)
            self.store.save(run)
        finally:
            await llm.close()
            await search.close()
            await fetcher.close()
