"""Start DeepScope locally: python run.py"""

from __future__ import annotations

import sys
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402


def main() -> None:
    print("=" * 62)
    print("  DeepScope · 多智能体深度研究助手")
    print(f"  模式: {'演示模式（未配置 API Key）' if settings.mode == 'mock' else settings.model}")
    print(f"  搜索: {settings.search_provider}")
    print(f"  地址: http://127.0.0.1:{settings.port}")
    print("=" * 62)
    uvicorn.run(
        "app.server:app",
        host="127.0.0.1",
        port=settings.port,
        log_level="info",
        access_log=False,
    )


if __name__ == "__main__":
    main()
