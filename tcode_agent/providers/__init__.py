"""LLM Providers.

Provider implementations are imported lazily so optional SDK dependencies do not
break package import when a provider is not being used.
"""

from .base import LLMProvider, ChatMessage, ChatResponse, ToolDefinition

__all__ = [
    "LLMProvider",
    "ChatMessage",
    "ChatResponse",
    "ToolDefinition",
    "AnthropicProvider",
    "OpenAIProvider",
    "OllamaProvider",
    "create_provider",
    "clone_provider",
]


def __getattr__(name: str):
    if name == "AnthropicProvider":
        from .anthropic import AnthropicProvider

        return AnthropicProvider
    if name == "OpenAIProvider":
        from .openai import OpenAIProvider

        return OpenAIProvider
    if name == "OllamaProvider":
        from .ollama import OllamaProvider

        return OllamaProvider
    if name in {"create_provider", "clone_provider"}:
        from .factory import clone_provider, create_provider

        return {"create_provider": create_provider, "clone_provider": clone_provider}[name]
    raise AttributeError(name)
