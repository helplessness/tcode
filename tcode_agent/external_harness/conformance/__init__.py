"""Harness Adapter conformance public API."""

from .report import ConformanceCase, HarnessConformanceReport
from .store import HarnessConformanceStore


async def run_conformance(*args, **kwargs):
    from .runner import run_conformance as _run_conformance

    return await _run_conformance(*args, **kwargs)

__all__ = [
    "ConformanceCase",
    "HarnessConformanceReport",
    "HarnessConformanceStore",
    "run_conformance",
]
