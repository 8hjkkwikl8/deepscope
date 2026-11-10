from __future__ import annotations

from ..llm import LLMClient
from ..models import ResearchRun, SubQuestion, extract_json

SYSTEM_PROMPT = """你是 DeepScope 研究团队的规划 Agent（Planner）。
你的任务是把用户的研究主题拆成 3-5 个互补、可检索、可验证的子问题。

要求：
1. 子问题必须覆盖：事实与现状、技术/机制、案例与数据、风险与反方证据。
2. 每个子问题配一个适合搜索引擎的中文查询词，查询词要具体，不要只重复主题。
3. 不要输出无法通过公开资料验证的私人信息类问题。
4. 只输出 JSON，不要输出 Markdown 代码块或解释。

输出格式：
{"subquestions":[{"question":"...","search_query":"...","rationale":"..."}]}"""


def _fallback_plan(query: str, count: int) -> list[SubQuestion]:
    templates = [
        ("核心概念、现状与主要参与者是什么？", "现状 定义 综述 主要玩家"),
        ("关键技术路径与系统架构是什么？", "技术架构 实现方案 原理"),
        ("有哪些真实落地案例与量化效果？", "落地案例 数据 效果 实践"),
        ("主要风险、争议与未来趋势是什么？", "风险 挑战 争议 趋势"),
        ("如何评估投入产出与适用边界？", "评测 成本 收益 适用边界"),
    ]
    questions = []
    for idx, (question, keywords) in enumerate(templates[:count], start=1):
        questions.append(
            SubQuestion(
                id=f"Q{idx}",
                question=f"围绕「{query}」，{question}",
                search_query=f"{query} {keywords}",
                rationale="规划 Agent 结构化兜底问题。",
            )
        )
    return questions


async def make_plan(llm: LLMClient, run: ResearchRun, query: str, max_questions: int) -> list[SubQuestion]:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"研究主题：{query}\n"
                f"最多拆成 {max_questions} 个子问题。"
                "如果主题很宽，优先保留能形成完整论证链的问题。"
            ),
        },
    ]
    try:
        raw = await llm.chat(messages, purpose="planner", temperature=0.2, max_tokens=1200, json_mode=True)
        data = extract_json(raw) or {}
        rows = data.get("subquestions") or []
    except Exception as exc:  # noqa: BLE001 - planner must never block the whole run
        run.emit("warning", agent="planner", message=f"规划输出解析失败，使用结构化兜底问题：{exc}")
        rows = []

    questions: list[SubQuestion] = []
    for idx, row in enumerate(rows[:max_questions], start=1):
        if not isinstance(row, dict):
            continue
        question = str(row.get("question") or "").strip()
        search_query = str(row.get("search_query") or question).strip()
        if len(question) < 8:
            continue
        questions.append(
            SubQuestion(
                id=f"Q{idx}",
                question=question[:300],
                search_query=search_query[:200],
                rationale=str(row.get("rationale") or "")[:300],
            )
        )
    if len(questions) < 2:
        questions = _fallback_plan(query, min(max_questions, 4))
    return questions
