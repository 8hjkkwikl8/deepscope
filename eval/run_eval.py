"""Small offline-friendly evaluation harness for DeepScope.

Usage:
    python eval/run_eval.py --offline            # deterministic smoke eval (offline search + fake fetch)
    python eval/run_eval.py --offline --limit 3
    python eval/run_eval.py                      # uses your .env (live LLM if a key is configured)

It writes eval/report.md and eval/results.json. The metrics are intentionally simple:
they audit citation discipline and coverage, not answer quality. A real evaluation would
add human or LLM-as-judge scoring on top of these process metrics.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.models import ResearchRun  # noqa: E402
from app.orchestrator import Orchestrator  # noqa: E402
from app.store import RunStore  # noqa: E402
from app.tools.citations import extract_citations  # noqa: E402
from app.tools.fetch import Page, PageFetcher  # noqa: E402


async def fake_fetch(self: PageFetcher, url: str) -> Page:
    text = (
        "离线评测使用的公开资料片段，讨论任务拆解、检索增强、引用校验、成本控制与评测方法。"
        "该片段仅用于验证流水线与指标计算，不代表真实研究结论。"
    ) * 6
    return Page(url=url, title="离线评测资料", text=text, ok=True)


def keyword_coverage(report: str, terms: list[str]) -> float:
    if not terms:
        return 1.0
    hit = sum(1 for term in terms if term.lower() in (report or "").lower())
    return round(hit / len(terms), 3)


async def run_one(item: dict, settings: Settings, store: RunStore, offline: bool) -> dict:
    run_id = "rs_eval_" + item["id"]
    run = ResearchRun(
        run_id,
        item["query"],
        deep=bool(item.get("deep")),
        mode=settings.mode,
        model=settings.llm_model if settings.mode == "live" else "demo-planner-v1",
    )
    started = time.perf_counter()
    if offline:
        with patch.object(PageFetcher, "fetch", fake_fetch):
            await Orchestrator(settings, store).execute(run)
    else:
        await Orchestrator(settings, store).execute(run)
    elapsed = round(time.perf_counter() - started, 1)

    stats = run.stats or {}
    report = run.report or ""
    return {
        "id": item["id"],
        "query": item["query"],
        "deep": bool(item.get("deep")),
        "status": run.status,
        "duration_seconds": elapsed,
        "sources": len(run.sources),
        "notes": len(run.notes),
        "cited_sources": stats.get("cited_sources", 0),
        "grounded_ratio": stats.get("grounded_ratio", 0),
        "char_count": stats.get("char_count", len(report)),
        "invalid_citations": stats.get("invalid_citation_ids", []),
        "citations": len(extract_citations(report)),
        "keyword_coverage": keyword_coverage(report, item.get("must_cover") or []),
        "report_head": report[:160],
    }


def render_markdown(results: list[dict], mode: str, search: str) -> str:
    ok = [r for r in results if r["status"] == "done"]
    lines = [
        "# DeepScope 评测报告",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"- 运行模式：{mode} / 搜索源：{search}",
        f"- 样本数：{len(results)}，成功：{len(ok)}",
        "",
        "| # | 研究问题 | 状态 | 用时 | 来源 | 笔记 | 引用来源 | 有据率 | 字数 | 关键词覆盖 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        query = r["query"].replace("|", "／")
        lines.append(
            f"| {r['id']} | {query} | {r['status']} | {r['duration_seconds']}s | "
            f"{r['sources']} | {r['notes']} | {r['cited_sources']} | "
            f"{round(r['grounded_ratio'] * 100)}% | {r['char_count']} | "
            f"{round(r['keyword_coverage'] * 100)}% |"
        )
    if ok:
        avg = lambda key: sum(r[key] for r in ok) / len(ok)  # noqa: E731
        lines += [
            "",
            "## 汇总",
            "",
            f"- 平均用时：{avg('duration_seconds'):.1f}s",
            f"- 平均来源数：{avg('sources'):.1f}",
            f"- 平均引用来源：{avg('cited_sources'):.1f}",
            f"- 平均有据率：{avg('grounded_ratio') * 100:.0f}%",
            f"- 平均关键词覆盖：{avg('keyword_coverage') * 100:.0f}%",
            f"- 引用编号异常：{sum(len(r['invalid_citations']) for r in results)} 个",
        ]
    lines += [
        "",
        "## 指标说明",
        "",
        "- **有据率**：正文内容行中包含有效 [S#] 引用的比例（不含参考来源列表）。",
        "- **引用来源**：最终报告实际引用的不同来源数。",
        "- **关键词覆盖**：报告是否提及题目预设关键词，仅作为主题覆盖的粗粒度代理指标。",
        "- 该脚本衡量流程指标与引用纪律，不代表答案正确性；真实场景应叠加人工评分或 LLM-as-judge。",
        "",
    ]
    return "\n".join(lines)


async def main() -> None:
    parser = argparse.ArgumentParser(description="DeepScope evaluation harness")
    parser.add_argument("--questions", default=str(ROOT / "eval" / "questions.json"))
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 个问题")
    parser.add_argument("--offline", action="store_true", help="离线搜索 + 模拟抓取，便于快速复现")
    parser.add_argument("--deep", action="store_true", help="强制全部使用深度模式")
    parser.add_argument("--out", default=str(ROOT / "eval" / "report.md"))
    args = parser.parse_args()

    items = json.loads(Path(args.questions).read_text(encoding="utf-8"))
    if args.limit:
        items = items[: args.limit]
    if args.deep:
        for item in items:
            item["deep"] = True

    settings = Settings()
    settings.force_mode = "mock" if args.offline else settings.force_mode
    if args.offline:
        settings.search_provider = "offline"
    settings.http_proxy = ""
    settings.max_subquestions = 3 if args.offline else settings.max_subquestions
    settings.fetch_top_k = 2 if args.offline else settings.fetch_top_k

    store_dir = (ROOT / "eval" / ".runs") if args.offline else (ROOT / "data" / "runs")
    store = RunStore(store_dir)
    results = []
    for index, item in enumerate(items, 1):
        print(f"[{index}/{len(items)}] {item['id']} {item['query']}")
        result = await run_one(item, settings, store, offline=args.offline)
        print(
            f"    -> {result['status']} · {result['duration_seconds']}s · "
            f"{result['sources']} sources · {result['cited_sources']} cited · "
            f"{round(result['grounded_ratio'] * 100)}% grounded"
        )
        results.append(result)

    out = Path(args.out)
    out.write_text(render_markdown(results, settings.mode, settings.search_provider), encoding="utf-8")
    (out.parent / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nwritten: {out}")


if __name__ == "__main__":
    asyncio.run(main())
