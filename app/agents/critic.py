from __future__ import annotations

from ..llm import LLMClient
from ..models import Critique, Note, ResearchRun, SubQuestion, extract_json

SYSTEM_PROMPT = """你是 DeepScope 的审阅 Agent（Critic）。你的职责不是复述结论，而是找出证据缺口。

请检查研究笔记：
1. 是否只覆盖了单一视角，缺少反方证据或失败案例？
2. 是否有结论缺少来源支撑，或来源之间明显矛盾？
3. 是否缺少可量化的成本、效果、时延等指标？
4. 是否遗漏了对用户决策真正关键的问题？

最多提出 2 个补充检索问题。只有确实存在关键缺口时才提出；如果覆盖充分，返回空数组。
只输出 JSON：
{"verdict":"一句话审阅结论","gaps":["缺口1","缺口2"],"followups":[{"question":"...","search_query":"...","rationale":"..."}]}"""


def _fallback() -> Critique:
    return Critique(verdict="审阅 Agent 未返回有效结果，直接进入写作阶段。", gaps=[], followups=[])


async def review_notes(
    llm: LLMClient,
    run: ResearchRun,
    query: str,
    notes: list[Note],
    registry_ids: list[str],
    max_followups: int,
) -> Critique:
    if not notes:
        return _fallback()

    notes_text = []
    for note in notes:
        points = "\n".join(f"  - {p}" for p in note.key_points) or "  - （无要点）"
        notes_text.append(
            f"[{note.question_id}] {note.question}\n摘要：{note.summary}\n要点：\n{points}\n引用：{', '.join(note.source_ids) or '无'}"
        )
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"研究主题：{query}\n"
                f"可用来源编号：{', '.join(registry_ids) or '无'}\n\n"
                + "\n\n".join(notes_text)[:12000]
            ),
        },
    ]
    try:
        raw = await llm.chat(messages, purpose="critic", temperature=0.1, max_tokens=900, json_mode=True)
        data = extract_json(raw) or {}
        gaps = [str(g).strip()[:200] for g in (data.get("gaps") or []) if str(g).strip()][:5]
        rows = data.get("followups") or []
        followups: list[SubQuestion] = []
        for idx, row in enumerate(rows[:max_followups], start=1):
            if not isinstance(row, dict):
                continue
            question = str(row.get("question") or "").strip()
            if len(question) < 8:
                continue
            followups.append(
                SubQuestion(
                    id=f"F{idx}",
                    question=question[:300],
                    search_query=str(row.get("search_query") or question)[:200],
                    rationale=str(row.get("rationale") or "审阅 Agent 补充检索")[:300],
                    kind="followup",
                )
            )
        return Critique(
            verdict=str(data.get("verdict") or "已完成证据审阅。")[:400],
            gaps=gaps,
            followups=followups,
        )
    except Exception as exc:  # noqa: BLE001 - critic is best-effort
        run.emit("warning", agent="critic", message=f"审阅 Agent 调用失败，跳过补充检索：{exc}")
        return _fallback()
