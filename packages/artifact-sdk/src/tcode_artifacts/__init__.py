"""Standalone file transport; no TCode runtime, TMCP or scheduler dependency."""
from .client import ArtifactIO

__all__ = ["ArtifactIO"]
__version__ = "0.1.0"
