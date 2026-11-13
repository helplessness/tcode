"""Process-local catalog of immutable domain ontology packages.

Packages are import candidates and historical resolvers. They never replace the
platform-owned active system ontology merely by being installed or started.
"""
from __future__ import annotations

from pathlib import Path
from threading import RLock


_LOCK = RLock()
_PROVIDERS: dict[str, Path] = {}


def register_ontology_package(provider_id: str, knowledge_root) -> callable:
    """Register a validated, read-only domain package and return its disposer."""
    from .ontology_core.extraction import LocalKnowledge

    provider_id = str(provider_id or "").strip()
    if not provider_id:
        raise ValueError("ontology provider id required")
    root = Path(knowledge_root).resolve()
    knowledge = LocalKnowledge(root)
    knowledge.close()
    with _LOCK:
        current = _PROVIDERS.get(provider_id)
        if current is not None and current != root:
            raise ValueError("ontology provider id already registered")
        _PROVIDERS[provider_id] = root

    def dispose() -> bool:
        with _LOCK:
            if _PROVIDERS.get(provider_id) != root:
                return False
            del _PROVIDERS[provider_id]
            return True

    return dispose


def registered_system_ontologies() -> dict[str, Path]:
    """Compatibility alias for callers that only enumerate legacy providers."""
    with _LOCK:
        return dict(_PROVIDERS)


def registered_ontology_packages() -> dict[str, Path]:
    with _LOCK:
        return dict(_PROVIDERS)


def register_system_ontology(provider_id: str, knowledge_root) -> callable:
    """Deprecated compatibility alias; registration no longer activates a package."""
    return register_ontology_package(provider_id, knowledge_root)


def selected_system_ontology() -> Path | None:
    """Plugins cannot select the platform system ontology by registration."""
    return None
