"""Context engine abstraction layered on top of the existing compaction runtime."""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from typing import Optional


class ContextEngine(ABC):
    name: str = "context-engine"

    @abstractmethod
    async def maybe_compact(self, *, session, provider, on_start=None):
        """Run compaction or return None when no compaction is needed."""


class SessionCompactionContextEngine(ContextEngine):
    name = "session-compaction"

    async def maybe_compact(self, *, session, provider, on_start=None):
        from ..session.compaction import SessionCompaction, CompactionConfig

        model_context_limit = CompactionConfig.model_context_limit_from_env(default=128_000)
        provider_char_limit = None
        try:
            limit_getter = getattr(provider, "_max_input_chars", None)
            if callable(limit_getter):
                provider_char_limit = int(limit_getter())
        except Exception:
            provider_char_limit = None
        if provider_char_limit and provider_char_limit > 0:
            # 粗略按 4 chars ~= 1 token 对齐 provider 输入上限，避免 compaction 触发过晚。
            provider_token_limit = max(1_000, provider_char_limit // 4)
            model_context_limit = min(model_context_limit, provider_token_limit)
        config = CompactionConfig.from_env()
        return await SessionCompaction.auto_compact(
            session=session,
            provider=provider,
            model_context_limit=model_context_limit,
            config=config,
            on_start=on_start,
        )


class AssemblyFirstContextEngine(ContextEngine):
    """Keep the audit transcript intact and let ModelContextBuilder slice input.

    ModelContextBuilder remains the normal per-request selector.  When the
    configured pressure threshold is crossed, whole-session compaction creates
    a continuation summary and retains recent turns so long tasks can proceed.
    """

    name = "model-context-assembly"

    async def maybe_compact(self, *, session, provider, on_start=None):
        configured = os.environ.get("TCODE_CONTEXT_COMPACTION_ENABLED")
        if configured is None:
            # Backward compatibility for deployments that still use the old
            # opt-in name.  The new setting takes precedence when both exist.
            configured = os.environ.get("TCODE_LEGACY_SESSION_COMPACTION", "false")
        enabled = str(configured or "false").strip().lower() in {
            "1", "true", "yes", "on",
        }
        if not enabled:
            return None
        return await SessionCompactionContextEngine().maybe_compact(
            session=session,
            provider=provider,
            on_start=on_start,
        )


def get_default_context_engine() -> ContextEngine:
    return AssemblyFirstContextEngine()
