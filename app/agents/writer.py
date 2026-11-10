from __future__ import annotations

from datetime import date

from ..llm import LLMClient, LLMError
from ..models import Critique, Note, ResearchRun, SourceRegistry

SYSTEM_PROMPT = """你是 DeepScope 的首席研究员（Writer），负责把团队笔记写成一份可直接阅读的研究简报。

写作要求：
1. 直接回答用户的问题，先给结论，再给论证；不要写“本文将从…展开”这类空话。
2. 每个关键结论后必须标注来源编号，例如 [S1] 或 [S2][S5]。只能使用给定来源，禁止编造编号。
3. 材料不足或存在冲突时，明确写出“现有证据不足以判断”，不要用常识补全事实。
4. 不使用夸张营销语；区分“事实”“推断”和“待验证假设”。
5. Markdown 格式，结构固定为：
## 摘要
## 核心发现
## 详细分析
## 风险、争议与不确定性
## 结论
6. 不要自行编写“参考来源”章节，系统会自动追加。
7. 全文 900-1600 个中文字符，信息密度优先，避免重复。"""


def _format_notes(notes: list[Note]) -> str:
    blocks = []
    for note in notes:
        points = "\n".join(f"- {p}" for p in note.key_points) or "- （无）"
        blocks.append(
            f"### {note.question_id} {note.question}\n"
            f"摘要：{note.summary}\n要点：\n{points}\n引用：{', '.join(note.source_ids) or '无'}"
        )
    return "\n\n".join(blocks)[:10000]


async def write_report(
    *,
    llm: LLMClient,
    run: ResearchRun,
    query: str,
    notes: list[Note],
    critique: Critique | None,
    registry: SourceRegistry,
    deep: bool,
) -> str:
    gaps = "；".join(critique.gaps) if critique and critique.gaps else "无"
    sources_text = registry.map_text()[:6000]
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"研究主题：{query}\n"
                f"报告日期：{date.today().isoformat()}\n"
                f"审阅结论：{critique.verdict if critique else '未审阅'}\n"
                f"已知证据缺口：{gaps}\n"
                f"研究深度：{'深度' if deep else '标准'}\n\n"
                f"可用来源：\n{sources_text}\n\n"
                f"团队笔记：\n{_format_notes(notes)}"
            ),
        },
    ]

    buffer = ""
    emitted = False
    try:
        async for chunk in llm.stream(messages, purpose="writer", temperature=0.35, max_tokens=4096):
            emitted = True
            buffer += chunk
            run.emit("token", agent="writer", message="", text=chunk)
    except LLMError as exc:
        if emitted and len(buffer) > 400:
            run.emit("warning", agent="writer", message=f"流式输出中断，保留已生成内容：{exc}")
        else:
            # Some gateways do not support SSE; retry once without streaming.
            run.emit("warning", agent="writer", message=f"流式输出不可用，切换为一次性生成：{exc}")
            buffer = await llm.chat(messages, purpose="writer", temperature=0.35, max_tokens=4096)
            for i in range(0, len(buffer), 40):
                run.emit("token", agent="writer", message="", text=buffer[i : i + 40])

    report = buffer.strip()
    if len(report) < 200:
        raise LLMError("写作 Agent 输出过短，已终止本次研究")
    return report
