"""Memory system for persistent user/project memories."""

from .manager import MemoryManager, MemoryEntry
from .embedding_provider import EmbeddingProvider
from .retriever import build_embedding, semantic_rank
from .schema import STRUCTURED_MEMORY_KEYS, normalize_structured_fields
from .vector_store import FaissVectorStore
from .provider import MemoryProvider, MemoryPromptSection, RecalledMemory
from .provider_manager import MemoryProviderManager


_LAZY_SERVICE_EXPORTS = {
    "MEMORY_SCOPES",
    "MemoryOperationRejected",
    "MemoryServiceError",
    "ScopedMemoryService",
    "memory_target",
}


def __getattr__(name: str):
    if name in _LAZY_SERVICE_EXPORTS:
        from . import service

        value = getattr(service, name)
        globals()[name] = value
        return value
    raise AttributeError(name)

__all__ = [
    "MemoryManager",
    "MemoryEntry",
    "EmbeddingProvider",
    "FaissVectorStore",
    "build_embedding",
    "semantic_rank",
    "STRUCTURED_MEMORY_KEYS",
    "normalize_structured_fields",
    "MemoryProvider",
    "MemoryPromptSection",
    "RecalledMemory",
    "MemoryProviderManager",
    "MEMORY_SCOPES",
    "MemoryOperationRejected",
    "MemoryServiceError",
    "ScopedMemoryService",
    "memory_target",
]
