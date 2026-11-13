"""Agent-level learning runtime.

Provides:
- reflection memory capture
- pre-compaction memory flush
- draft skill generation
- low-risk adaptive prompt hints
"""

from .manager import LearningManager

__all__ = ["LearningManager"]
