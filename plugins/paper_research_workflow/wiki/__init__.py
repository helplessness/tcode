"""Workspace-local Paper Wiki.

The paper PDF and EvidenceStore remain the scientific source of truth.  This
package stores a reviewable and rebuildable knowledge projection. Agent-derived
facts are read-only to users; personal notes are stored through a separate API.
"""

from .compiler import PaperWikiCompiler
from .regenerator import PaperWikiLLMRegenerator
from .store import (
    PaperWikiStore,
    WikiConflictError,
    WikiNotFoundError,
    WikiValidationError,
)

__all__ = [
    "PaperWikiCompiler",
    "PaperWikiLLMRegenerator",
    "PaperWikiStore",
    "WikiConflictError",
    "WikiNotFoundError",
    "WikiValidationError",
]
