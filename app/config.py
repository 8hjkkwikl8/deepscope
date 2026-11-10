from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _env_str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


@dataclass
class Settings:
    llm_base_url: str = field(default_factory=lambda: _env_str("LLM_BASE_URL", "https://api.deepseek.com/v1") or "https://api.deepseek.com/v1")
    llm_api_key: str = field(default_factory=lambda: _env_str("LLM_API_KEY"))
    llm_model: str = field(default_factory=lambda: _env_str("LLM_MODEL", "deepseek-chat") or "deepseek-chat")
    llm_temperature: float = field(default_factory=lambda: _env_float("LLM_TEMPERATURE", 0.3))
    llm_max_tokens: int = field(default_factory=lambda: _env_int("LLM_MAX_TOKENS", 4096))
    force_mode: str = field(default_factory=lambda: _env_str("LLM_MODE").lower())

    search_provider: str = field(default_factory=lambda: _env_str("SEARCH_PROVIDER", "auto").lower() or "auto")
    tavily_api_key: str = field(default_factory=lambda: _env_str("TAVILY_API_KEY"))
    http_proxy: str = field(default_factory=lambda: _env_str("HTTP_PROXY"))

    max_subquestions: int = field(default_factory=lambda: max(2, min(6, _env_int("MAX_SUBQUESTIONS", 4))))
    fetch_top_k: int = field(default_factory=lambda: max(1, min(6, _env_int("FETCH_TOP_K", 3))))
    max_chars_per_page: int = field(default_factory=lambda: max(1500, _env_int("MAX_CHARS_PER_PAGE", 6000)))
    port: int = field(default_factory=lambda: _env_int("PORT", 8765))

    data_dir: Path = field(default_factory=lambda: ROOT / "data" / "runs")
    web_dir: Path = field(default_factory=lambda: ROOT / "web")

    @property
    def mode(self) -> str:
        if self.force_mode in {"mock", "demo"}:
            return "mock"
        if self.force_mode in {"live", "real"} and self.llm_api_key:
            return "live"
        return "live" if self.llm_api_key else "mock"

    def apply_llm(self, base_url: str | None = None, api_key: str | None = None, model: str | None = None) -> None:
        if base_url is not None:
            self.llm_base_url = base_url.strip() or self.llm_base_url
        if model is not None:
            self.llm_model = model.strip() or self.llm_model
        if api_key is not None:
            self.llm_api_key = api_key.strip()
        self.force_mode = ""

    def apply_search(self, provider: str | None = None, tavily_key: str | None = None) -> None:
        if provider:
            self.search_provider = provider.strip().lower()
        if tavily_key is not None:
            self.tavily_api_key = tavily_key.strip()

    def public_dict(self) -> dict:
        return {
            "mode": self.mode,
            "model": self.llm_model,
            "base_url": self.llm_base_url,
            "api_key_set": bool(self.llm_api_key),
            "search_provider": self.search_provider,
            "tavily_key_set": bool(self.tavily_api_key),
            "max_subquestions": self.max_subquestions,
            "fetch_top_k": self.fetch_top_k,
        }


settings = Settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)
