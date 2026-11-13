"""Provider factory utilities (TCode-style config-driven selection)."""

from __future__ import annotations

from typing import Optional

from ..config.manager import ConfigManager
from ..config.options import resolve_option_value
from .base import LLMProvider


def _provider_options(provider_cfg: dict) -> dict:
    if not isinstance(provider_cfg, dict):
        return {}
    options = provider_cfg.get("options", {})
    return options if isinstance(options, dict) else {}


def _provider_timeout_seconds(provider_cfg: dict) -> Optional[float]:
    options = _provider_options(provider_cfg)
    runtime_cfg = ConfigManager.get_runtime()
    raw_timeout = None
    for candidate in (
        options.get("timeoutSeconds"),
        options.get("timeout_seconds"),
        runtime_cfg.get("llm_timeout_seconds"),
        runtime_cfg.get("llmTimeoutSeconds"),
    ):
        if candidate not in (None, ""):
            raw_timeout = candidate
            break
    if raw_timeout in (None, ""):
        return None
    try:
        return float(raw_timeout)
    except (TypeError, ValueError):
        return None


def _normalize_ollama_base_url(base_url: str) -> str:
    value = (base_url or "").strip()
    if not value:
        return "http://localhost:11434"
    if value.endswith("/v1"):
        return value[:-3]
    return value


def _resolve_provider_selection(
    *,
    workdir: Optional[str] = None,
    model_ref: Optional[str] = None,
) -> dict:
    ConfigManager.load(workdir)

    normalized_model_ref = str(model_ref or "").strip()
    if not normalized_model_ref:
        return ConfigManager.resolve_model_selection()

    parsed = ConfigManager.parse_model_ref(normalized_model_ref)
    if not parsed:
        raise ValueError(
            f"无效模型引用: {normalized_model_ref}，应使用 provider/model 格式"
        )

    provider_id, model_id = parsed
    provider_cfg = ConfigManager.get_provider(provider_id)
    if not isinstance(provider_cfg, dict):
        raise ValueError(f"未找到 provider: {provider_id}")

    return {
        "provider_id": provider_id,
        "model_id": model_id,
        "provider": provider_cfg,
        "source": "model_override",
    }


def create_provider(
    workdir: Optional[str] = None,
    model_ref: Optional[str] = None,
    *,
    session_id: Optional[str] = None,
) -> LLMProvider:
    """Create provider from tcode.json model/provider config."""
    selection = _resolve_provider_selection(workdir=workdir, model_ref=model_ref)

    provider_id = str(selection.get("provider_id") or "").strip()
    model_id = str(selection.get("model_id") or "qwen3-coder-next:latest").strip()
    provider_cfg = selection.get("provider") if isinstance(selection.get("provider"), dict) else {}

    npm = str(provider_cfg.get("npm") or "").lower()
    options = _provider_options(provider_cfg)
    timeout_seconds = _provider_timeout_seconds(provider_cfg)

    def identified(provider: LLMProvider) -> LLMProvider:
        # Stable configured identity is security-relevant for sensitive tool
        # outputs such as desktop screenshots. Provider display names and model
        # names are not unique enough for authorization decisions.
        provider.provider_id = provider_id
        provider.model_ref = f"{provider_id}/{model_id}"
        return provider

    if provider_id.startswith("anthropic") or "anthropic" in npm:
        from .anthropic import AnthropicProvider

        api_key = resolve_option_value(options, "apiKey", "api_key")
        base_url = resolve_option_value(options, "baseURL", "base_url")
        return identified(AnthropicProvider(
            api_key=api_key,
            model=model_id,
            base_url=base_url or None,
            timeout_seconds=timeout_seconds,
        ))

    # OpenAI-compatible providers (including local Ollama /v1 gateway)
    if "openai-compatible" in npm or "openai" in npm or options.get("baseURL"):
        from .openai import OpenAIProvider

        api_key = resolve_option_value(options, "apiKey", "api_key")
        base_url = resolve_option_value(options, "baseURL", "base_url")
        api_mode = resolve_option_value(options, "apiMode", "api_mode") or ""
        if not api_mode and "openai-responses" in npm:
            api_mode = "responses"
        return identified(OpenAIProvider(
            api_key=api_key,
            base_url=base_url,
            model=model_id,
            timeout_seconds=timeout_seconds,
            api_mode=api_mode or None,
            session_id=session_id,
        ))

    if provider_id.startswith("ollama") or "ollama" in npm:
        from .ollama import OllamaProvider

        base_url = _normalize_ollama_base_url(
            resolve_option_value(
                options,
                "baseURL",
                "base_url",
                default="http://localhost:11434",
            )
        )
        return identified(OllamaProvider(base_url=base_url, model=model_id))

    from .ollama import OllamaProvider

    return identified(OllamaProvider(model=model_id))


def clone_provider(provider: LLMProvider) -> LLMProvider:
    """Clone provider for sub-agent use to avoid stream/session conflicts."""
    provider_name = str(getattr(provider, "name", "") or "").strip().lower()
    class_name = type(provider).__name__.lower()

    if provider_name == "ollama" or "ollama" in class_name:
        from .ollama import OllamaProvider

        return OllamaProvider(
            base_url=getattr(provider, "_base_url", "http://localhost:11434"),
            model=getattr(provider, "_model", "qwen3-coder-next:latest"),
        )

    if provider_name == "openai" or "openai" in class_name:
        from .openai import OpenAIProvider

        return OpenAIProvider(
            api_key=getattr(provider, "_api_key", None),
            base_url=getattr(provider, "_base_url", None),
            model=getattr(provider, "_model", None),
            timeout_seconds=getattr(provider, "_timeout_seconds", None),
            api_mode=getattr(provider, "api_mode", None),
        )

    if provider_name == "anthropic" or "anthropic" in class_name:
        from .anthropic import AnthropicProvider

        return AnthropicProvider(
            api_key=getattr(provider, "_api_key", None),
            model=getattr(provider, "_model", None),
            base_url=getattr(provider, "_base_url", None),
            timeout_seconds=getattr(provider, "_timeout_seconds", None),
        )

    return provider
