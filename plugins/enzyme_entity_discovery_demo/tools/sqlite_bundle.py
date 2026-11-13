"""Compatibility imports for the standalone ontology core shipped with this plugin."""
import sys
from pathlib import Path

_CORE = str(Path(__file__).resolve().parents[1] / "core")
if _CORE not in sys.path:
    sys.path.insert(0, _CORE)
from enzyme_ontology_core.sqlite_store import build_database, file_digest, normalize_term
