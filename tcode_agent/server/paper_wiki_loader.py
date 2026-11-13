"""Load the bundled Paper Wiki without relying on the ambiguous ``plugins`` name."""

from __future__ import annotations

import importlib.util
import sys
import threading
from pathlib import Path
from types import ModuleType


_PAPER_WIKI_MODULE_NAME = "tcode_bundled_paper_wiki"
_PAPER_WIKI_MODULE_LOCK = threading.RLock()


def _paper_wiki_package_path() -> Path:
    return (
        Path(__file__).resolve().parents[2]
        / "plugins"
        / "paper_research_workflow"
        / "wiki"
    )


def _load_paper_wiki_module(module_name: str) -> ModuleType:
    package_path = _paper_wiki_package_path()
    entrypoint = package_path / "__init__.py"
    if not entrypoint.is_file():
        raise ImportError(f"Bundled Paper Wiki entrypoint not found: {entrypoint}")
    spec = importlib.util.spec_from_file_location(
        module_name,
        entrypoint,
        submodule_search_locations=[package_path.as_posix()],
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to create Paper Wiki module spec: {entrypoint}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        for loaded_name in tuple(sys.modules):
            if loaded_name == module_name or loaded_name.startswith(f"{module_name}."):
                sys.modules.pop(loaded_name, None)
        raise
    return module


def paper_wiki_module() -> ModuleType:
    """Return one process-wide Paper Wiki package loaded under a collision-free name."""

    with _PAPER_WIKI_MODULE_LOCK:
        loaded = sys.modules.get(_PAPER_WIKI_MODULE_NAME)
        if loaded is not None:
            return loaded
        return _load_paper_wiki_module(_PAPER_WIKI_MODULE_NAME)

