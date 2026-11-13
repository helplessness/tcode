"""Load the bundled Paper Library without relying on the ambiguous plugins name."""

from __future__ import annotations

import importlib.util
import sys
import threading
from pathlib import Path
from types import ModuleType


_PAPER_LIBRARY_MODULE_NAME = "tcode_bundled_paper_library"
_PAPER_LIBRARY_MODULE_LOCK = threading.RLock()


def _paper_library_path() -> Path:
    return (
        Path(__file__).resolve().parents[2]
        / "plugins"
        / "paper_research_workflow"
        / "library.py"
    )


def paper_library_module() -> ModuleType:
    """Return one process-wide Paper Library module under a collision-free name."""

    with _PAPER_LIBRARY_MODULE_LOCK:
        loaded = sys.modules.get(_PAPER_LIBRARY_MODULE_NAME)
        if loaded is not None:
            return loaded
        entrypoint = _paper_library_path()
        if not entrypoint.is_file():
            raise ImportError(f"Bundled Paper Library entrypoint not found: {entrypoint}")
        spec = importlib.util.spec_from_file_location(
            _PAPER_LIBRARY_MODULE_NAME,
            entrypoint,
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"Unable to create Paper Library module spec: {entrypoint}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[_PAPER_LIBRARY_MODULE_NAME] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(_PAPER_LIBRARY_MODULE_NAME, None)
            raise
        return module


__all__ = ["paper_library_module"]
