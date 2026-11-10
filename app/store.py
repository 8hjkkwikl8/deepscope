from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .models import ResearchRun

RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{4,40}$")


class RunStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)

    def save(self, run: ResearchRun) -> None:
        payload = run.persisted()
        target = self.directory / f"{run.id}.json"
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(target)

    def load(self, run_id: str) -> dict[str, Any] | None:
        if not RUN_ID_RE.match(run_id):
            return None
        path = self.directory / f"{run_id}.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def list(self, limit: int = 20) -> list[dict[str, Any]]:
        files = sorted(self.directory.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        items: list[dict[str, Any]] = []
        for path in files[: max(1, limit)]:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            items.append(
                {
                    "id": data.get("id"),
                    "query": data.get("query"),
                    "status": data.get("status"),
                    "mode": data.get("mode"),
                    "model": data.get("model"),
                    "created_at": data.get("created_at"),
                    "finished_at": data.get("finished_at"),
                    "sources": len(data.get("sources") or []),
                    "stats": data.get("stats") or {},
                    "report_excerpt": (data.get("report") or "")[:180],
                }
            )
        return items
