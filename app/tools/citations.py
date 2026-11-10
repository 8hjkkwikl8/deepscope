from __future__ import annotations

import re
from typing import Any

CITE_RE = re.compile(r"\[(S\d+)\]")


def extract_citations(text: str) -> list[str]:
    seen: list[str] = []
    for match in CITE_RE.finditer(text or ""):
        sid = match.group(1)
        if sid not in seen:
            seen.append(sid)
    return seen


def strip_generated_sources_section(report: str) -> str:
    """Remove an LLM-written sources section; the server appends the canonical one."""
    pattern = re.compile(r"\n#{1,3}\s*(参考来源|参考资料|References|Sources)\s*\n", re.I)
    matches = list(pattern.finditer(report or ""))
    if not matches:
        return (report or "").strip()
    cut = matches[-1].start()
    # Only strip if it is near the end (avoid deleting a mid-report heading).
    if len(report) - cut < 2500:
        return report[:cut].rstrip()
    return (report or "").strip()


def sanitize_citations(report: str, valid_ids: set[str]) -> tuple[str, list[str]]:
    invalid: list[str] = []

    def _replace(match: re.Match[str]) -> str:
        sid = match.group(1)
        if sid in valid_ids:
            return match.group(0)
        if sid not in invalid:
            invalid.append(sid)
        return ""

    cleaned = CITE_RE.sub(_replace, report)
    cleaned = re.sub(r"[ \t]+([，。；：、])", r"\1", cleaned)
    cleaned = re.sub(r"[，；、]\s*(?=[。\n])", "", cleaned)
    return cleaned, invalid


def compute_stats(report: str, valid_ids: set[str]) -> dict[str, Any]:
    cited = extract_citations(report)
    valid_cited = [sid for sid in cited if sid in valid_ids]
    invalid_cited = [sid for sid in cited if sid not in valid_ids]

    content_lines = []
    in_sources = False
    for raw in (report or "").splitlines():
        line = raw.strip()
        if re.match(r"^#{1,6}\s*(参考来源|参考资料|References|Sources)", line, re.I):
            in_sources = True
        if in_sources:
            continue
        if not line or line.startswith(("#", ">", "---", "```", "|")):
            continue
        if re.fullmatch(r"[-*_]{3,}", line):
            continue
        line = re.sub(r"^(\d+\.|[-*+])\s+", "", line)
        if len(line) < 12:
            continue
        content_lines.append(line)

    grounded = sum(1 for line in content_lines if any(sid in valid_ids for sid in extract_citations(line)))
    return {
        "char_count": len(re.sub(r"\s+", "", report or "")),
        "cited_sources": len(valid_cited),
        "valid_sources": len(valid_ids),
        "invalid_citations": invalid_cited,
        "source_coverage": round(len(valid_cited) / max(1, len(valid_ids)), 3),
        "grounded_lines": grounded,
        "content_lines": len(content_lines),
        "grounded_ratio": round(grounded / max(1, len(content_lines)), 3),
    }


def append_sources(report: str, sources: list[dict[str, Any]]) -> str:
    if not sources:
        return report.strip()
    lines = ["", "---", "", "## 参考来源", ""]
    for src in sources:
        title = (src.get("title") or src.get("url") or "").replace("[", "（").replace("]", "）")
        lines.append(f"- **[{src.get('id')}]** [{title}]({src.get('url')}) — {src.get('domain', '')}")
    lines.append("")
    lines.append("> 引用编号对应上方链接；检索结果可能存在时效性与立场偏差，重要结论请回到原文核验。")
    return (report.strip() + "\n" + "\n".join(lines)).strip()
