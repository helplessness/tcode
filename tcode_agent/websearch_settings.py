"""Private, instance-wide settings for the built-in web search tool."""

import json
import os
from pathlib import Path


def settings_path() -> Path:
    return Path(os.environ.get("TCODE_WORKDIR") or os.getcwd()) / ".tcode" / "websearch.json"


def read_settings(path: Path | None = None) -> dict:
    try:
        data = json.loads((path or settings_path()).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict):
        raise ValueError("Invalid web search settings")
    return data


def provider(settings: dict) -> str:
    return settings.get("provider") or os.environ.get("TCODE_WEBSEARCH_PROVIDER", "").strip().lower() or "searxng"


def brave_key(settings: dict) -> str:
    return settings.get("brave_api_key") or os.environ.get("TCODE_BRAVE_SEARCH_API_KEY", "").strip()


def searxng_url(settings: dict) -> str:
    return settings.get("searxng_url") or os.environ.get("TCODE_SEARXNG_BASE_URL", "").strip() or "http://127.0.0.1:8888"
